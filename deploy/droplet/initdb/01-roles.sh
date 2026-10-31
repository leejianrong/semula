#!/bin/sh
# Runs once, when the database is first created. The image made `semula_owner` (the superuser
# named by POSTGRES_USER) and the `semula` database. The app must connect as a role that owns no
# table and bypasses nothing, or row-level security does not apply to it, so make that role and
# let it read and write whatever the owner's migrations create.
set -eu
psql -v ON_ERROR_STOP=1 -v pw="$SEMULA_DB_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE semula LOGIN PASSWORD :'pw' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
REVOKE ALL ON DATABASE semula FROM PUBLIC;
GRANT CONNECT ON DATABASE semula TO semula;
GRANT USAGE ON SCHEMA public TO semula;
ALTER DEFAULT PRIVILEGES FOR ROLE semula_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO semula;
ALTER DEFAULT PRIVILEGES FOR ROLE semula_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO semula;
SQL
