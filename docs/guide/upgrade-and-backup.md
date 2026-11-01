# Upgrade and back up

## What is stored

Everything is in one place: the SQLite file `semula.db` in the data directory (`/data` in the image). Entries, versions, pins, per-project settings and modes, the review queue and the call log are all in it. With Postgres, the database holds it instead and `/data` is unused.

## Upgrade

Replace the container with the new image. The volume stays, and semula upgrades the database itself on start:

```bash
docker pull ghcr.io/leejianrong/semula:0.4.0
docker stop semula && docker rm semula
docker run -d --name semula -p 127.0.0.1:8080:8080 \
  -v semula-data:/data ghcr.io/leejianrong/semula:0.4.0
```

Three things to know:

- **Recordings made with one release open in the next.** The 0.x promise is that a release may change behaviour, and the [changelog](https://github.com/leejianrong/semula/blob/main/CHANGELOG.md) lists each change under "Changed".
- **There is no downgrade.** After an upgrade the database is migrated and an older release cannot read it. Back up first ([Back up](#back-up)) if you might want to go back. Starting a release that is too old for the database stops with a message instead of a traceback:

    ```text
    error: This database was made by a newer semula (revision 0009) and cannot be downgraded. Run a release at least as new as the one that wrote it, or restore a backup taken before the upgrade.
    ```
- **Pin a version in anything shared.** `ghcr.io/leejianrong/semula:latest` moves. Use `:0.4.0`, or `:0.4` to follow patch releases.

## Back up

Two ways, for two jobs.

**A full copy of the volume**, for restoring this exact server. Stop semula first, so the database file is complete, then copy the volume to a tarball:

```bash
docker stop semula
docker run --rm -v semula-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/semula-backup.tgz -C /data .
docker start semula
```

**An export per project**, for sharing and for git. It is portable across versions and servers, and the files diff cleanly:

```bash
docker exec semula semula export support-bot --dir /tmp/cassettes
docker cp semula:/tmp/cassettes ./cassettes
```

An export holds entries and approved aliases. It leaves out hit counts, the call log, the review queue and each project's mode, so it is a record of *what was recorded*, not a copy of the server.

## Restore

Put the tarball into a fresh volume, make it writable by the semula user, and start from it:

```bash
docker volume create semula-restore
docker run --rm -v semula-restore:/data -v "$PWD":/backup alpine \
  sh -c 'tar xzf /backup/semula-backup.tgz -C /data && chown -R 10001 /data'
docker run -d --name semula-restored -p 127.0.0.1:8099:8080 \
  -v semula-restore:/data ghcr.io/leejianrong/semula
```

Or import an export into any running server: `docker exec semula semula import /tmp/cassettes`.

## Automatic backups, SQLite or Postgres

For a hosted server, or any server you do not want to lose, `scripts/backup.py` (in the repository, standard library only) makes a backup, encrypts it, uploads it, deletes old ones and tells an alert service it worked. One entry point serves both databases and picks by `SEMULA_DATABASE_URL`, the variable the server reads (else the SQLite file in `SEMULA_DATA_DIR`):

```bash
scripts/backup.py backup                        # make one now
scripts/backup.py restore --file backups/semula-20261101T023000Z.sqlite.gz.enc --into /fresh/data
scripts/backup.py check --max-hours 30          # exit 1 when the newest backup is too old
```

- **SQLite** is copied with SQLite's online backup API, never by copying the file: a copy of `semula.db` alone misses what is still in the write-ahead log (`semula.db-wal`) and can catch a write half done. The copy is checked with `PRAGMA integrity_check` before it is kept, so a damaged database fails the backup instead of being saved as if it were good. The server keeps running.
- **Postgres** is `pg_dump --format=custom`, which needs `pg_dump` and `pg_restore` of the server's version on the machine (the backup image below has them). The password goes in the environment, never on the command line. Dump as the database **owner**: the app's role is held back by row-level security and would dump no rows.
- **Encryption** is AES-256 with `openssl`, keyed by `SEMULA_BACKUP_PASSPHRASE`. Keep that passphrase somewhere other than the droplet: without it a backup cannot be opened. A sealed backup leaves no plain copy behind. (`openssl enc` does not authenticate; the integrity check on restore is what catches a damaged file.)
- **Off-site** goes to any S3-compatible bucket, such as a DigitalOcean Space, signed in the script (no SDK): set the settings below and every backup is uploaded after it is made. A backup that only lives on the droplet does not survive losing the droplet.
- **Retention** deletes what is older than 14 days, in the backup directory and in the bucket.
- **An alert** needs something outside the droplet. Set `SEMULA_BACKUP_PING_URL` to a [healthchecks.io](https://healthchecks.io)-style check: the script requests it after a good backup and requests `<url>/fail` after a bad one, and the service alerts you when the ping stops coming, even if the droplet is dead. `check` is the same test on the droplet itself (the backup container is *unhealthy* when the newest backup is over 30 hours old).

| Setting | Meaning |
|---|---|
| `SEMULA_BACKUP_DIR` | Where backups are kept (default `./backups`) |
| `SEMULA_BACKUP_KEEP_DAYS` | Delete older ones (default `14`) |
| `SEMULA_BACKUP_PASSPHRASE` | Encrypt with this; empty means plain files |
| `SEMULA_BACKUP_S3_ENDPOINT` | For example `https://fra1.digitaloceanspaces.com` |
| `SEMULA_BACKUP_S3_REGION` | For example `fra1` |
| `SEMULA_BACKUP_S3_BUCKET` | The bucket; `SEMULA_BACKUP_S3_PREFIX` is the folder in it (default `semula/`) |
| `SEMULA_BACKUP_S3_ACCESS_KEY`, `SEMULA_BACKUP_S3_SECRET_KEY` | A key that can read, write and delete in that bucket only |
| `SEMULA_BACKUP_PING_URL` | Requested after a backup, and `/fail` after a failed one |
| `SEMULA_RESTORE_OWNER` | `uid:gid` to give a restored SQLite file to (the image runs as `10001:10001`) |

**On the droplet** the backup service in `deploy/droplet/backup` runs it for you: a backup when it starts, then one a day at 02:30 UTC, retried in five minutes if it fails. Add it to the stack you already run, with your `.env` carrying the settings above:

```bash
cd deploy/droplet
docker compose -f compose.yaml -f backup/compose.postgres.yaml up -d --wait        # Postgres
docker compose -f compose.sqlite.yaml -f backup/compose.sqlite.yaml up -d --wait   # SQLite
```

Backups wait in the `backups` volume as well as in the bucket. Turn on **droplet snapshots** in the DigitalOcean console too (Backups, weekly): they cover the whole machine, the dumps cover the data and can be restored anywhere.

### Restore

A restore goes into a **fresh** place: a new data directory (SQLite) or a new, empty database (Postgres). It refuses to replace an existing `semula.db` unless you pass `--force`. On a new droplet:

1. Install Docker, clone the repository, put your `.env` back (it holds the passphrase, which you kept elsewhere).
2. Fetch the newest backup: `scripts/backup.py restore --remote latest --into ...` does it from the bucket with the `SEMULA_BACKUP_S3_*` settings; or copy a file over and pass `--file`.
3. **Postgres:** `docker compose up -d db`, then `docker compose -f compose.yaml -f backup/compose.postgres.yaml run --rm --no-deps backup semula-backup restore --file /backups/<name>` (copy the file into the `backups` volume first, or use `--remote latest`). **SQLite:** the same with `-f compose.sqlite.yaml -f backup/compose.sqlite.yaml` and `--into /data`.
4. `docker compose up -d --wait`. The migrate step brings an older dump up to date, then the app starts on the restored data.

`scripts/smoke_backup.sh <hosted image> [postgres|sqlite]` does exactly this on the real stack and checks it: it makes a workspace and a token, takes an encrypted backup, destroys the database volume, restores from the file alone, and shows the token still works. It runs in CI, and it printed these times on a laptop with a tiny database: a backup in about 1 s, a restore in about 7 s (Postgres) or under 1 s (SQLite). A real database takes longer, in proportion to its size; time your own the first time.

**What has and has not been tried.** The drill above runs against a local stack. A restore onto a fresh *droplet* from a bucket was not done: that needs the droplet and a Space (SEM-113). The upload is checked against a stand-in S3 server that verifies the request signature, not against DigitalOcean's, so run `scripts/backup.py backup` once with the real settings and look in the bucket before you rely on it.

## Postgres

Back up with `scripts/backup.py` as above, or with your usual `pg_dump`. semula migrates the schema on start, the same as with SQLite.
