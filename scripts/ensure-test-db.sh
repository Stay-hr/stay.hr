#!/usr/bin/env bash
# Create stay_platform_test_db on shared PostGIS if missing (idempotent).
#
# Usage:
#   ./scripts/ensure-test-db.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TEST_DB_NAME="${TEST_DB_NAME:-stay_platform_test_db}"
# Django test runner uses TEST["NAME"] = test_<TEST_DB_NAME> (see test_postgis.py).
DJANGO_TEST_DB_NAME="test_${TEST_DB_NAME}"
DB_USER="${DB_USER:-stay}"
POSTGIS_CONTAINER="${POSTGIS_CONTAINER:-postgis}"

log() { printf '==> %s\n' "$*"; }

ensure_db() {
  local name="$1"
  local exists
  exists="$(docker exec "$POSTGIS_CONTAINER" psql -U postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${name}'" | tr -d '[:space:]')"
  if [[ "$exists" == "1" ]]; then
    log "Database ${name} already exists"
  else
    log "Creating database ${name} (owner ${DB_USER})"
    docker exec "$POSTGIS_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -c \
      "CREATE DATABASE \"${name}\" OWNER \"${DB_USER}\";"
  fi
  log "Ensuring postgis extension on ${name}"
  docker exec "$POSTGIS_CONTAINER" psql -U postgres -d "$name" -v ON_ERROR_STOP=1 -c \
    "CREATE EXTENSION IF NOT EXISTS postgis;"
}

if ! docker ps --format '{{.Names}}' | grep -qx "$POSTGIS_CONTAINER"; then
  printf 'ERROR: PostGIS container %q is not running.\n' "$POSTGIS_CONTAINER" >&2
  exit 1
fi

ensure_db "$TEST_DB_NAME"
ensure_db "$DJANGO_TEST_DB_NAME"

# Allow Django to create/drop throwaway test DBs if needed.
docker exec "$POSTGIS_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -c \
  "ALTER USER \"${DB_USER}\" CREATEDB;" >/dev/null

log "Test databases ready: ${TEST_DB_NAME}, ${DJANGO_TEST_DB_NAME}"
