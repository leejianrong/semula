#!/usr/bin/env python3
"""Back up and restore semula's database: one entry point for SQLite and Postgres (SEM-120).

    backup.py backup           make a backup now (and upload it, prune old ones, ping)
    backup.py restore --file F put a backup into a fresh data directory or database
    backup.py check            fail if the newest backup is too old (for an alert)

Which kind of database it is comes from ``SEMULA_DATABASE_URL`` (else the SQLite file in
``SEMULA_DATA_DIR``), the same variables the server reads. Standard library only, so it runs on a
host, in the semula image or in the small image under ``deploy/droplet/backup``. Postgres needs
``pg_dump`` and ``pg_restore`` on PATH; encryption needs ``openssl``.

A SQLite backup is made with SQLite's online backup API, never by copying the file, so it is
consistent while the server is writing, and its integrity is checked before it is kept. A Postgres
backup is ``pg_dump --format=custom``. Settings, all in the environment:

    SEMULA_BACKUP_DIR           where backups are kept (default ./backups)
    SEMULA_BACKUP_KEEP_DAYS     delete older ones, here and in the bucket (default 14)
    SEMULA_BACKUP_PASSPHRASE    encrypt with AES-256 (openssl); without it files are plain
    SEMULA_BACKUP_S3_ENDPOINT   e.g. https://fra1.digitaloceanspaces.com (any S3-compatible store)
    SEMULA_BACKUP_S3_REGION     e.g. fra1
    SEMULA_BACKUP_S3_BUCKET, SEMULA_BACKUP_S3_PREFIX (default semula/)
    SEMULA_BACKUP_S3_ACCESS_KEY, SEMULA_BACKUP_S3_SECRET_KEY
    SEMULA_RESTORE_OWNER        uid:gid to give a restored SQLite file (the image runs as 10001)
    SEMULA_BACKUP_PING_URL      GET on success, and url + /fail on failure (a dead-man's switch,
                                for example healthchecks.io, alerts when the ping stops coming)
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

STAMP = "%Y%m%dT%H%M%SZ"
NAME = re.compile(r"semula-(\d{8}T\d{6}Z)\.(sqlite\.gz|pgdump)(\.enc)?$")


class BackupError(Exception):
    pass


# --- which database ---------------------------------------------------------------------------
def database_url(env: Mapping[str, str]) -> str:
    given = env.get("SEMULA_DATABASE_URL")
    if given:
        return given
    return f"sqlite:///{Path(env.get('SEMULA_DATA_DIR', './data')) / 'semula.db'}"


def kind_of(url: str) -> str:
    if url.startswith(("postgres://", "postgresql")):
        return "postgres"
    if url.startswith("sqlite"):
        return "sqlite"
    raise BackupError(f"cannot back up this database: {url.split('://', 1)[0]}://")


def sqlite_path(url: str) -> Path:
    return Path(re.sub(r"^sqlite(\+\w+)?:///", "", url))


# --- SQLite -----------------------------------------------------------------------------------
def integrity(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        found = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if found != [("ok",)]:
        raise BackupError(f"{path.name} failed its integrity check: {found[:3]}")


def dump_sqlite(url: str, dest: Path) -> None:
    """A consistent copy of a database that may be in use: SQLite's own backup API."""
    source = sqlite_path(url)
    if not source.exists():
        raise BackupError(f"no database at {source}")
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "copy.db"
        src = sqlite3.connect(source, timeout=30)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        integrity(copy)
        with copy.open("rb") as raw, gzip.open(dest, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)


def handover(directory: Path, target: Path, owner: str | None) -> None:
    """As root (the backup container) give the data directory and the file to the user the
    server runs as, ``SEMULA_RESTORE_OWNER=uid:gid``; else to whoever owns the directory."""
    try:
        if owner:
            uid, gid = (int(part) for part in owner.split(":"))
            os.chown(directory, uid, gid)
        else:
            stat = directory.stat()
            uid, gid = stat.st_uid, stat.st_gid
        os.chown(target, uid, gid)
    except (AttributeError, PermissionError, ValueError):
        pass


def restore_sqlite(archive: Path, into: Path, force: bool, owner: str | None = None) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    target = into / "semula.db"
    if target.exists() and not force:
        raise BackupError(f"{target} exists; restore into a fresh directory, or pass --force")
    with tempfile.TemporaryDirectory(dir=into) as scratch:
        built = Path(scratch) / "semula.db"
        with gzip.open(archive, "rb") as packed, built.open("wb") as out:
            shutil.copyfileobj(packed, out)
        integrity(built)  # never put a broken file where the server will open it
        os.replace(built, target)
    handover(into, target, owner)
    return target


# --- Postgres ---------------------------------------------------------------------------------
def pg_parts(url: str) -> tuple[list[str], dict[str, str], str | None]:
    """Arguments and environment for pg_dump/pg_restore from a URL, so the password is never in
    argv; and the schema if the URL pins one (``options=-csearch_path=...``, used by tests)."""
    parsed = urllib.parse.urlparse(re.sub(r"^postgresql\+\w+", "postgresql", url))
    args = ["-h", parsed.hostname or "localhost", "-p", str(parsed.port or 5432)]
    if parsed.username:
        args += ["-U", urllib.parse.unquote(parsed.username)]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = urllib.parse.unquote(parsed.password)
    options = urllib.parse.parse_qs(parsed.query).get("options", [""])[0]
    schema = re.search(r"search_path=([\w$]+)", options)
    args += ["-d", parsed.path.lstrip("/")]
    return args, env, schema.group(1) if schema else None


def run(cmd: list[str], env: Mapping[str, str] | None = None) -> None:
    if shutil.which(cmd[0]) is None:
        raise BackupError(f"`{cmd[0]}` is not installed here")
    done = subprocess.run(cmd, env=dict(env) if env else None, capture_output=True, text=True)
    if done.returncode != 0:
        raise BackupError(f"{cmd[0]} failed: {done.stderr.strip()[-400:]}")


def dump_postgres(url: str, dest: Path) -> None:
    args, env, schema = pg_parts(url)
    extra = [f"--schema={schema}"] if schema else []
    run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", *extra, *args, "-f",
         str(dest)], env)  # fmt: skip


def restore_postgres(archive: Path, url: str) -> None:
    args, env, _ = pg_parts(url)
    run(["pg_restore", "--no-owner", "--no-privileges", "--clean", "--if-exists",
         "--exit-on-error", *args, str(archive)], env)  # fmt: skip


# --- encryption -------------------------------------------------------------------------------
def crypt(decrypt: bool, source: Path, dest: Path, passphrase: str) -> None:
    env = {**os.environ, "SEMULA_BACKUP_PASSPHRASE": passphrase}
    run(["openssl", "enc", "-d" if decrypt else "-e", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
         "-salt", "-in", str(source), "-out", str(dest), "-pass", "env:SEMULA_BACKUP_PASSPHRASE"],
        env)  # fmt: skip


# --- an S3-compatible bucket (DigitalOcean Spaces), signed with SigV4, no dependency -----------
class Bucket:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.endpoint = env["SEMULA_BACKUP_S3_ENDPOINT"].rstrip("/")
        self.region = env.get("SEMULA_BACKUP_S3_REGION") or "us-east-1"
        self.name = env["SEMULA_BACKUP_S3_BUCKET"]
        self.prefix = (env.get("SEMULA_BACKUP_S3_PREFIX") or "semula/").lstrip("/")
        self.access = env["SEMULA_BACKUP_S3_ACCESS_KEY"]
        self.secret = env["SEMULA_BACKUP_S3_SECRET_KEY"]

    @staticmethod
    def configured(env: Mapping[str, str]) -> bool:
        needed = ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY")
        return all(env.get(f"SEMULA_BACKUP_S3_{n}") for n in needed)

    def _sign(self, method: str, key: str, query: dict[str, str], body_hash: str,
              now: dt.datetime) -> dict[str, str]:  # fmt: skip
        host = urllib.parse.urlparse(self.endpoint).netloc
        path = "/" + urllib.parse.quote(f"{self.name}/{key}".rstrip("/") or self.name, safe="/")
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(query.items())
        )
        headers = {"host": host, "x-amz-content-sha256": body_hash, "x-amz-date": stamp}
        signed = ";".join(sorted(headers))
        canonical = "\n".join(
            [method, path, canonical_query,
             "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, body_hash]
        )  # fmt: skip
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )

        def mac(key_: bytes, msg: str) -> bytes:
            return hmac.new(key_, msg.encode(), hashlib.sha256).digest()

        k = mac(f"AWS4{self.secret}".encode(), day)
        for part in (self.region, "s3", "aws4_request"):
            k = mac(k, part)
        signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access}/{scope}, SignedHeaders={signed}, "
            f"Signature={signature}"
        )
        return headers

    def _request(self, method: str, key: str, query: dict[str, str] | None = None,
                 data: Path | None = None) -> bytes:  # fmt: skip
        query = query or {}
        body_hash = hashlib.sha256(b"").hexdigest()
        if data is not None:
            digest = hashlib.sha256()
            with data.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            body_hash = digest.hexdigest()
        headers = self._sign(method, key, query, body_hash, dt.datetime.now(dt.UTC))
        path = urllib.parse.quote(f"{self.name}/{key}".rstrip("/") or self.name, safe="/")
        url = self.endpoint + "/" + path.lstrip("/")
        if query:
            url += "?" + "&".join(
                f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
                for k, v in sorted(query.items())
            )
        headers = {("Host" if k == "host" else k): v for k, v in headers.items()}
        if data is not None:
            headers["Content-Length"] = str(data.stat().st_size)
        with data.open("rb") if data is not None else open(os.devnull, "rb") as handle:
            req = urllib.request.Request(url, method=method, headers=headers,
                                         data=handle if data is not None else None)  # fmt: skip
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                raise BackupError(
                    f"the bucket refused {method} {key}: {exc.code} {exc.read()[:200]!r}"
                ) from exc

    def put(self, name: str, path: Path) -> None:
        self._request("PUT", self.prefix + name, data=path)

    def get(self, name: str, dest: Path) -> None:
        dest.write_bytes(self._request("GET", self.prefix + name))

    def delete(self, name: str) -> None:
        self._request("DELETE", self.prefix + name)

    def names(self) -> list[str]:
        body = self._request("GET", "", {"list-type": "2", "prefix": self.prefix})
        root = ET.fromstring(body)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        keys = [k.text or "" for k in root.iter(f"{ns}Key")]
        return sorted(k[len(self.prefix) :] for k in keys if k.startswith(self.prefix))


# --- the commands -----------------------------------------------------------------------------
def stamped(name: str) -> dt.datetime | None:
    found = NAME.fullmatch(name)
    return dt.datetime.strptime(found.group(1), STAMP).replace(tzinfo=dt.UTC) if found else None


def make_backup(env: Mapping[str, str], now: dt.datetime | None = None) -> tuple[Path, str]:
    """Make the file in the backup directory; returns it and the kind of database."""
    url = database_url(env)
    kind = kind_of(url)
    out = Path(env.get("SEMULA_BACKUP_DIR", "./backups"))
    out.mkdir(parents=True, exist_ok=True)
    when = (now or dt.datetime.now(dt.UTC)).strftime(STAMP)
    plain = out / f"semula-{when}.{'sqlite.gz' if kind == 'sqlite' else 'pgdump'}"
    try:
        (dump_sqlite if kind == "sqlite" else dump_postgres)(url, plain)
    except BaseException as exc:
        plain.unlink(missing_ok=True)  # never leave half a backup that looks like a whole one
        if isinstance(exc, sqlite3.Error):
            raise BackupError(f"SQLite could not read the database: {exc}") from exc
        raise
    passphrase = env.get("SEMULA_BACKUP_PASSPHRASE")
    if not passphrase:
        return plain, kind
    sealed = plain.with_name(plain.name + ".enc")
    try:
        crypt(False, plain, sealed, passphrase)
    finally:
        plain.unlink(missing_ok=True)  # an encrypted backup leaves no plain copy behind
    return sealed, kind


def prune(env: Mapping[str, str], bucket: Bucket | None, now: dt.datetime) -> list[str]:
    keep = dt.timedelta(days=float(env.get("SEMULA_BACKUP_KEEP_DAYS", "14")))
    gone = []
    local = Path(env.get("SEMULA_BACKUP_DIR", "./backups"))
    for path in sorted(local.glob("semula-*")) if local.exists() else []:
        made = stamped(path.name)
        if made and now - made > keep:
            path.unlink()
            gone.append(path.name)
    if bucket:
        for name in bucket.names():
            made = stamped(name)
            if made and now - made > keep:
                bucket.delete(name)
                gone.append(f"bucket:{name}")
    return gone


def ping(env: Mapping[str, str], ok: bool) -> None:
    url = env.get("SEMULA_BACKUP_PING_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url if ok else url.rstrip("/") + "/fail", timeout=15).read()
    except OSError as exc:
        print(f"warning: could not reach the ping URL: {exc}", file=sys.stderr)


def cmd_backup(env: Mapping[str, str]) -> int:
    try:
        made, kind = make_backup(env)
        print(f"{kind} backup: {made} ({made.stat().st_size} bytes)")
        bucket = Bucket(env) if Bucket.configured(env) else None
        if bucket:
            bucket.put(made.name, made)
            print(f"uploaded to {bucket.name}/{bucket.prefix}{made.name}")
        else:
            print("no bucket configured: this backup is on this machine only", file=sys.stderr)
        for name in prune(env, bucket, dt.datetime.now(dt.UTC)):
            print(f"pruned {name}")
    except (BackupError, OSError, KeyError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        ping(env, False)
        return 1
    ping(env, True)
    return 0


def unseal(source: Path, env: Mapping[str, str], scratch: Path) -> Path:
    if not source.name.endswith(".enc"):
        return source
    passphrase = env.get("SEMULA_BACKUP_PASSPHRASE")
    if not passphrase:
        raise BackupError("this backup is encrypted: set SEMULA_BACKUP_PASSPHRASE")
    opened = scratch / source.name[: -len(".enc")]
    crypt(True, source, opened, passphrase)
    return opened


def cmd_restore(env: Mapping[str, str], file: str | None, remote: str | None, into: str | None,
                force: bool) -> int:  # fmt: skip
    try:
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            if remote:
                bucket = Bucket(env)
                name = remote if remote != "latest" else (bucket.names() or [""])[-1]
                if not NAME.fullmatch(name):
                    raise BackupError("no backup in the bucket")
                source = work / name
                bucket.get(name, source)
            elif file:
                source = Path(file)
            else:
                raise BackupError("say which backup: --file PATH or --remote NAME|latest")
            archive = unseal(source, env, work)
            if archive.name.endswith(".sqlite.gz"):
                target = restore_sqlite(
                    archive,
                    Path(into or env.get("SEMULA_DATA_DIR", "./data")),
                    force,
                    env.get("SEMULA_RESTORE_OWNER") or None,
                )
                print(f"restored {target}; start semula with SEMULA_DATA_DIR={target.parent}")
            else:
                url = into or database_url(env)
                if kind_of(url) != "postgres":
                    raise BackupError("a Postgres backup needs a Postgres URL: --into URL")
                restore_postgres(archive, url)
                print("restored into the database; start semula against it")
    except (BackupError, OSError, KeyError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


def newest(env: Mapping[str, str]) -> tuple[str, dt.datetime] | None:
    names = Bucket(env).names() if Bucket.configured(env) else []
    if not names:
        local = Path(env.get("SEMULA_BACKUP_DIR", "./backups"))
        names = [p.name for p in local.glob("semula-*")] if local.exists() else []
    stamps = [(n, s) for n in names if (s := stamped(n))]
    return max(stamps, key=lambda pair: pair[1]) if stamps else None


def cmd_check(env: Mapping[str, str], max_hours: float) -> int:
    try:
        found = newest(env)
    except (BackupError, OSError, KeyError) as exc:
        print(f"cannot look for backups: {exc}", file=sys.stderr)
        return 1
    if found is None:
        print("no backup found", file=sys.stderr)
        return 1
    age = (dt.datetime.now(dt.UTC) - found[1]).total_seconds() / 3600
    if age > max_hours:
        print(f"the newest backup, {found[0]}, is {age:.1f} hours old (limit {max_hours:g})",
              file=sys.stderr)  # fmt: skip
        return 1
    print(f"newest backup {found[0]} is {age:.1f} hours old")
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="backup.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="make a backup now, upload it, prune old ones")
    restore = sub.add_parser("restore", help="put a backup into a fresh place")
    restore.add_argument("--file", help="a local backup file")
    restore.add_argument("--remote", help="a name in the bucket, or latest")
    restore.add_argument("--into", help="SQLite: a data directory; Postgres: a database URL")
    restore.add_argument("--force", action="store_true", help="replace an existing semula.db")
    check = sub.add_parser("check", help="fail when the newest backup is too old")
    check.add_argument("--max-hours", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.command == "backup":
        return cmd_backup(env)
    if args.command == "restore":
        return cmd_restore(env, args.file, args.remote, args.into, args.force)
    return cmd_check(env, args.max_hours)


if __name__ == "__main__":
    sys.exit(main())
