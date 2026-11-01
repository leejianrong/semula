"""Back up and restore the database (SEM-120): ``scripts/backup.py``, one entry point for SQLite and
Postgres. The drill that matters: a backup made while the server is in use, restored into a fresh
data directory, boots semula and gives the entry back. Postgres is drilled by
``scripts/smoke_backup_pg.sh`` (it needs a server and a client of the same version)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import importlib.util
import shutil
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from semula.app import create_app
from semula.config import Settings
from semula.db import Database
from semula.service import Service

pytestmark = pytest.mark.integration

SPEC = importlib.util.spec_from_file_location(
    "backup_script", Path(__file__).resolve().parents[2] / "scripts" / "backup.py"
)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
sys.modules["backup_script"] = backup
SPEC.loader.exec_module(backup)

REQUEST = {"messages": [{"role": "user", "content": "what is two plus two"}]}
ANSWER = {"content": "four"}


def seeded(data: Path, entries: int = 1) -> tuple[Service, str]:
    service = Service(Database(f"sqlite:///{data / 'semula.db'}"), Settings(data_dir=data))
    first = ""
    for i in range(entries):
        entry, _ = service.upsert_entry(
            "default", {"messages": [{"role": "user", "content": f"q{i} " * 3}]}, ANSWER
        )
        first = first or str(entry["id"])
    return service, first


def env_for(data: Path, out: Path, **more: str) -> dict[str, str]:
    return {"SEMULA_DATA_DIR": str(data), "SEMULA_BACKUP_DIR": str(out), **more}


def served_answer(data: Path, entry_id: str) -> str:
    """Boot semula on a data directory and read an entry back over its API."""
    with TestClient(create_app(Settings(data_dir=data))) as client:
        got = client.get(f"/api/v1/entries/{entry_id}")
        assert got.status_code == 200, got.text
        return str(got.json()["responses"][0]["message"]["content"])


# --- SQLite -----------------------------------------------------------------------------------
def test_a_backup_restores_into_a_fresh_directory_and_semula_boots_from_it(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    _, entry_id = seeded(data)
    out = tmp_path / "backups"
    assert backup.main(["backup"], env_for(data, out)) == 0
    [made] = list(out.iterdir())
    assert made.name.endswith(".sqlite.gz")
    fresh = tmp_path / "fresh"
    assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], {}) == 0
    assert served_answer(fresh, entry_id) == "four"


def test_what_is_still_in_the_write_ahead_log_is_in_the_backup(tmp_path: Path) -> None:
    """semula runs SQLite in WAL mode: a commit may live only in the -wal file, which copying
    ``semula.db`` would miss. The server is still running here (its connection is open)."""
    data = tmp_path / "live"
    data.mkdir()
    service, _ = seeded(data)
    holder = sqlite3.connect(data / "semula.db")  # keeps the WAL from being checkpointed away
    holder.execute("PRAGMA wal_autocheckpoint=0")
    late, _ = service.upsert_entry(
        "default", {"messages": [{"role": "user", "content": "written last"}]}, {"content": "late"}
    )
    assert (data / "semula.db-wal").stat().st_size > 0
    out = tmp_path / "backups"
    assert backup.main(["backup"], env_for(data, out)) == 0
    fresh = tmp_path / "fresh"
    [made] = list(out.iterdir())
    assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], {}) == 0
    assert served_answer(fresh, str(late["id"])) == "late"
    holder.close()


def test_a_backup_made_while_the_server_writes_is_whole(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    service, _ = seeded(data, entries=20)
    stop = threading.Event()

    def write() -> None:
        i = 0
        while not stop.is_set():
            service.upsert_entry(
                "default", {"messages": [{"role": "user", "content": f"busy {i}"}]}, ANSWER
            )
            i += 1

    writer = threading.Thread(target=write)
    writer.start()
    try:
        out = tmp_path / "backups"
        for _ in range(3):
            assert backup.main(["backup"], env_for(data, out)) == 0
    finally:
        stop.set()
        writer.join()
    for made in out.iterdir():
        fresh = tmp_path / f"fresh-{made.name}"
        assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], {}) == 0


def test_a_broken_database_is_not_kept_as_a_backup(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    seeded(data, entries=40)
    db = data / "semula.db"
    raw = bytearray(db.read_bytes())
    for page in range(2, len(raw) // 4096):  # scribble over every page but the first
        raw[page * 4096 : page * 4096 + 64] = b"\xff" * 64
    db.write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        (data / f"semula.db{suffix}").unlink(missing_ok=True)
    out = tmp_path / "backups"
    assert backup.main(["backup"], env_for(data, out)) == 1
    assert not out.exists() or not any(p.name.startswith("semula-") for p in out.iterdir())


def test_a_copy_that_fails_its_integrity_check_is_not_kept(tmp_path: Path) -> None:
    """Readable, so the backup API copies it, but wrong: a row that breaks a CHECK constraint."""
    data = tmp_path / "live"
    data.mkdir()
    conn = sqlite3.connect(data / "semula.db")
    conn.execute("CREATE TABLE t (a INTEGER CHECK (a > 0))")
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("INSERT INTO t VALUES (-1)")
    conn.commit()
    conn.close()
    out = tmp_path / "backups"
    assert backup.main(["backup"], env_for(data, out)) == 1
    assert not out.exists() or list(out.iterdir()) == []


def test_a_restore_does_not_replace_a_database_unless_told_to(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    seeded(data)
    out = tmp_path / "backups"
    backup.main(["backup"], env_for(data, out))
    [made] = list(out.iterdir())
    assert backup.main(["restore", "--file", str(made), "--into", str(data)], {}) == 1
    assert backup.main(["restore", "--file", str(made), "--into", str(data), "--force"], {}) == 0


def test_the_kind_of_database_comes_from_the_url() -> None:
    assert backup.kind_of("postgresql+psycopg://u:p@h/db") == "postgres"
    assert backup.kind_of("sqlite:////data/semula.db") == "sqlite"
    assert backup.database_url({"SEMULA_DATA_DIR": "/d"}) == "sqlite:////d/semula.db"
    with pytest.raises(backup.BackupError):
        backup.kind_of("mysql://x/y")


def test_a_postgres_password_never_reaches_the_command_line() -> None:
    args, env, schema = backup.pg_parts(
        "postgresql+psycopg://semula_owner:s3cr%40t@db:5433/semula?options=-csearch_path%3Dsem_ab"
    )
    assert "s3cr@t" not in " ".join(args) and env["PGPASSWORD"] == "s3cr@t"
    assert args[:4] == ["-h", "db", "-p", "5433"] and schema == "sem_ab"


# --- encryption -------------------------------------------------------------------------------
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@needs_openssl
def test_an_encrypted_backup_is_not_readable_without_the_passphrase(tmp_path: Path) -> None:
    data = tmp_path / "live"
    data.mkdir()
    _, entry_id = seeded(data)
    out = tmp_path / "backups"
    env = env_for(data, out, SEMULA_BACKUP_PASSPHRASE="correct horse")
    assert backup.main(["backup"], env) == 0
    [made] = list(out.iterdir())  # only the sealed file: no plain copy is left behind
    assert made.name.endswith(".sqlite.gz.enc")
    assert b"SQLite format" not in made.read_bytes() and b"four" not in made.read_bytes()
    fresh = tmp_path / "fresh"
    assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], {}) == 1
    wrong = {"SEMULA_BACKUP_PASSPHRASE": "wrong"}
    assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], wrong) == 1
    assert not (fresh / "semula.db").exists()
    right = {"SEMULA_BACKUP_PASSPHRASE": "correct horse"}
    assert backup.main(["restore", "--file", str(made), "--into", str(fresh)], right) == 0
    assert served_answer(fresh, entry_id) == "four"


# --- a bucket ---------------------------------------------------------------------------------
KEY, SECRET, REGION = "AKIATEST", "secretkey", "fra1"


def expected_signature(method: str, path: str, query: str, headers: dict[str, str]) -> str:
    """AWS Signature V4, written out again from the specification, apart from the script."""
    signed = ["host", "x-amz-content-sha256", "x-amz-date"]
    canonical = "\n".join(
        [
            method,
            path,
            query,
            "".join(f"{h}:{headers[h]}\n" for h in signed),
            ";".join(signed),
            headers["x-amz-content-sha256"],
        ]
    )
    day = headers["x-amz-date"][:8]
    scope = f"{day}/{REGION}/s3/aws4_request"
    text = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            headers["x-amz-date"],
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{SECRET}".encode()
    for part in (day, REGION, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


class FakeSpaces(BaseHTTPRequestHandler):
    objects: dict[str, bytes]
    pings: list[str]

    def log_message(self, *_: Any) -> None:
        pass

    def _checked(self) -> tuple[str, dict[str, list[str]], bytes] | None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        sent = headers.get("authorization", "")
        ok = (
            hashlib.sha256(body).hexdigest() == headers.get("x-amz-content-sha256")
            and f"Credential={KEY}/" in sent
            and sent.endswith(
                "Signature=" + expected_signature(self.command, parsed.path, parsed.query, headers)
            )
        )
        if not ok:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"SignatureDoesNotMatch")
            return None
        return parsed.path, urllib.parse.parse_qs(parsed.query), body

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:  # noqa: N802
        got = self._checked()
        if got:
            self.objects[got[0]] = got[2]
            self._reply(200)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ping"):
            self.pings.append(self.path)
            return self._reply(200, b"OK")
        got = self._checked()
        if not got:
            return
        path, query, _ = got
        if path.rstrip("/") == "/bucket":
            prefix = "/bucket/" + query.get("prefix", [""])[0]
            keys = sorted(k[len("/bucket/") :] for k in self.objects if k.startswith(prefix))
            items = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            xml = f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}</ListBucketResult>'
            return self._reply(200, xml.encode())
        if path in self.objects:
            return self._reply(200, self.objects[path])
        self._reply(404)

    def do_DELETE(self) -> None:  # noqa: N802
        got = self._checked()
        if got:
            self.objects.pop(got[0], None)
            self._reply(204)


@pytest.fixture
def spaces() -> Any:
    handler = type("Handler", (FakeSpaces,), {"objects": {}, "pings": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handler.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield handler
    server.shutdown()


def bucket_env(spaces: Any, **more: str) -> dict[str, str]:
    return {
        "SEMULA_BACKUP_S3_ENDPOINT": spaces.url,
        "SEMULA_BACKUP_S3_REGION": REGION,
        "SEMULA_BACKUP_S3_BUCKET": "bucket",
        "SEMULA_BACKUP_S3_ACCESS_KEY": KEY,
        "SEMULA_BACKUP_S3_SECRET_KEY": SECRET,
        **more,
    }


def test_a_backup_is_uploaded_and_restores_from_the_bucket(tmp_path: Path, spaces: Any) -> None:
    data = tmp_path / "live"
    data.mkdir()
    _, entry_id = seeded(data)
    env = {**env_for(data, tmp_path / "backups"), **bucket_env(spaces)}
    assert backup.main(["backup"], env) == 0
    [(path, body)] = spaces.objects.items()
    assert path.startswith("/bucket/semula/semula-") and path.endswith(".sqlite.gz")
    assert body == next((tmp_path / "backups").iterdir()).read_bytes()
    fresh = tmp_path / "fresh"
    assert backup.main(["restore", "--remote", "latest", "--into", str(fresh)], env) == 0
    assert served_answer(fresh, entry_id) == "four"


def test_old_backups_are_deleted_here_and_in_the_bucket_and_recent_ones_are_kept(
    tmp_path: Path, spaces: Any
) -> None:
    data = tmp_path / "live"
    data.mkdir()
    seeded(data)
    out = tmp_path / "backups"
    out.mkdir()
    now = dt.datetime.now(dt.UTC)
    old = (now - dt.timedelta(days=15)).strftime(backup.STAMP)
    recent = (now - dt.timedelta(days=13)).strftime(backup.STAMP)
    env = {**env_for(data, out), **bucket_env(spaces)}
    for stamp in (old, recent):
        name = f"semula-{stamp}.sqlite.gz"
        (out / name).write_bytes(b"x")
        backup.Bucket(env).put(name, out / name)
    assert backup.main(["backup"], env) == 0
    here = sorted(p.name for p in out.iterdir())
    there = sorted(k.rsplit("/", 1)[1] for k in spaces.objects)
    for names in (here, there):
        assert f"semula-{old}.sqlite.gz" not in names
        assert f"semula-{recent}.sqlite.gz" in names and len(names) == 2


def test_a_bucket_that_refuses_us_fails_the_backup_and_says_so(tmp_path: Path, spaces: Any) -> None:
    data = tmp_path / "live"
    data.mkdir()
    seeded(data)
    env = {
        **env_for(data, tmp_path / "backups"),
        **bucket_env(spaces, SEMULA_BACKUP_S3_SECRET_KEY="the wrong secret"),
        "SEMULA_BACKUP_PING_URL": spaces.url + "/ping/abc",
    }
    assert backup.main(["backup"], env) == 1
    assert spaces.pings == ["/ping/abc/fail"] and not spaces.objects


def test_a_successful_backup_pings_and_a_missing_one_is_noticed(
    tmp_path: Path, spaces: Any
) -> None:
    data = tmp_path / "live"
    data.mkdir()
    seeded(data)
    out = tmp_path / "backups"
    env = {**env_for(data, out), "SEMULA_BACKUP_PING_URL": spaces.url + "/ping/abc"}
    assert backup.main(["check"], env) == 1  # nothing yet: the alert fires
    assert backup.main(["backup"], env) == 0
    assert spaces.pings == ["/ping/abc"]
    assert backup.main(["check"], env) == 0
    [made] = list(out.iterdir())
    when = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=40)).strftime(backup.STAMP)
    stale = out / f"semula-{when}.sqlite.gz"
    made.rename(stale)
    assert backup.main(["check"], env) == 1  # the newest is 40 hours old: the alert fires
    assert backup.main(["check", "--max-hours", "48"], env) == 0
