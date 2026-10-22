#!/usr/bin/env bash
# Build the CLI alone as one executable: dist/semula (HTTP commands only; see docs/RELEASING.md).
# The server, MCP stdio proxy and embeddings stay out: they pull in the whole server library, and
# the binary would grow from about 35 MB to hundreds. `semula serve` and `semula mcp` say so.
# Usage: scripts/build_cli_binary.sh [outdir]     Needs uv.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-dist}"

EXCLUDES=()
for m in semula.app semula.service semula.mcp_server semula.config semula.embeddings semula.db \
         semula.api_admin semula.api_openai semula.dashboard fastmcp mcp fastapi starlette uvicorn \
         sqlalchemy alembic indah pydantic pydantic_settings numpy fastembed psycopg; do
  EXCLUDES+=(--exclude-module "$m")
done

uv run --with 'pyinstaller==6.22.3' pyinstaller --onefile --clean --noconfirm \
  --name semula --copy-metadata semula "${EXCLUDES[@]}" \
  --distpath "$OUT" --workpath build/pyinstaller --specpath build/pyinstaller \
  scripts/cli_entry.py
ls -l "$OUT/semula"
