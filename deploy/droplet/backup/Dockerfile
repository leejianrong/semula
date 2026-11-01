# The backup job: scripts/backup.py with the Postgres 16 client (pg_dump, pg_restore) and openssl.
# Build from the repository root: docker build -f deploy/droplet/backup/Dockerfile .
FROM postgres:16-alpine
RUN apk add --no-cache python3 openssl
COPY scripts/backup.py /usr/local/bin/semula-backup
RUN chmod 0755 /usr/local/bin/semula-backup
ENTRYPOINT []
CMD ["semula-backup", "--help"]
