# Upgrade and back up

## What is stored

Everything is in one place: the SQLite file `semula.db` in the data directory (`/data` in the image). Entries, versions, pins, per-project settings and modes, the review queue and the call log are all in it. With Postgres, the database holds it instead and `/data` is unused.

## Upgrade

Replace the container with the new image. The volume stays, and semula upgrades the database itself on start:

```bash
docker pull ghcr.io/leejianrong/semula:0.1.0
docker stop semula && docker rm semula
docker run -d --name semula -p 127.0.0.1:8080:8080 \
  -v semula-data:/data ghcr.io/leejianrong/semula:0.1.0
```

Three things to know:

- **Recordings made with one release open in the next.** The 0.x promise is that a release may change behaviour, and the [changelog](https://github.com/leejianrong/semula/blob/main/CHANGELOG.md) lists each change under "Changed".
- **There is no downgrade.** After an upgrade the database is migrated and an older release cannot read it. Back up first (below) if you might want to go back.
- **Pin a version in anything shared.** `ghcr.io/leejianrong/semula:latest` moves. Use `:0.1.0`, or `:0.1` to follow patch releases.

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

## Postgres

Back up with your usual `pg_dump`. semula migrates the schema on start, the same as with SQLite.
