#!/usr/bin/env bash
# Deploy stay.hr: rebuild + up when migrations are pending, migration files,
# backend source, or frontend source changed since the image was built;
# otherwise restart only.
#
# Usage:
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --help
#
# Requires: docker compose stack in repo root (stay.hr).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $arg" >&2; usage 1 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }

service_image_id() {
  local service="$1"
  # docker compose reads stdin; never inherit the find file list.
  docker compose images -q "$service" </dev/null 2>/dev/null | head -n1
}

service_image_created_epoch() {
  local service="$1"
  local image_id created_ts
  image_id="$(service_image_id "$service")"
  [[ -n "$image_id" ]] || return 1

  created_ts="$(docker inspect -f '{{.Created}}' "$image_id" </dev/null 2>/dev/null || true)"
  [[ -n "$created_ts" ]] || return 1

  date -d "$created_ts" +%s 2>/dev/null \
    || date -j -f '%Y-%m-%dT%H:%M:%S' "${created_ts%%.*}" +%s 2>/dev/null \
    || return 1
}

files_newer_than_epoch() {
  local image_ts file_ts
  image_ts="$1"

  while IFS= read -r -d '' f; do
    [[ -f "$f" ]] || continue
    file_ts="$(stat -c '%Y' "$f" 2>/dev/null || stat -f '%m' "$f")"
    if [[ "$file_ts" -gt "$image_ts" ]]; then
      return 0
    fi
  done

  return 1
}

files_newer_than_service_image() {
  local service="$1"
  local image_ts list

  # Snapshot find output first. `docker compose images` consumes stdin, which
  # previously emptied this list and skipped required frontend/backend rebuilds.
  list="$(mktemp)"
  cat >"$list"
  if ! image_ts="$(service_image_created_epoch "$service" </dev/null)"; then
    rm -f "$list"
    return 0
  fi
  if files_newer_than_epoch "$image_ts" <"$list"; then
    rm -f "$list"
    return 0
  fi
  rm -f "$list"
  return 1
}

migration_files_newer_than_image() {
  files_newer_than_service_image django < <(
    find backend/apps -path '*/migrations/*.py' ! -name '__init__.py' -print0 2>/dev/null
  )
}

backend_source_newer_than_image() {
  files_newer_than_service_image django < <(
    find backend -name '*.py' -print0 2>/dev/null
    printf '%s\0' requirements.txt Dockerfile docker-entrypoint.sh
  )
}

frontend_source_newer_than_image() {
  files_newer_than_service_image web-reception < <(
    find web/booking web/reception \
      \( -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' \
         -o -name '*.json' -o -name '*.css' -o -name 'Dockerfile' \) \
      ! -path '*/node_modules/*' ! -path '*/.next/*' -print0 2>/dev/null
    printf '%s\0' \
      web/booking/package.json web/booking/package-lock.json \
      web/reception/package.json web/reception/package-lock.json
  )
}

pending_migrations_in_db() {
  if docker compose ps --status running django -q 2>/dev/null | grep -q .; then
    ! docker compose exec -T django python manage.py migrate --check >/dev/null 2>&1
    return
  fi

  log "django not running; starting temporarily for migration check..."
  docker compose up -d django
  sleep 2
  ! docker compose exec -T django python manage.py migrate --check >/dev/null 2>&1
}

needs_backend_rebuild=false
needs_frontend_rebuild=false
backend_reason=""
frontend_reason=""

if migration_files_newer_than_image; then
  needs_backend_rebuild=true
  backend_reason="migration files changed since last image build"
elif backend_source_newer_than_image; then
  needs_backend_rebuild=true
  backend_reason="backend source changed since last image build"
elif pending_migrations_in_db; then
  needs_backend_rebuild=true
  backend_reason="unapplied database migrations"
fi

if frontend_source_newer_than_image; then
  needs_frontend_rebuild=true
  frontend_reason="frontend source changed since last image build"
fi

if $needs_backend_rebuild; then
  log "Backend rebuild required ($backend_reason)"
  docker compose build django celery-worker celery-beat
  docker compose up -d django celery-worker celery-beat
fi

if $needs_frontend_rebuild; then
  log "Frontend rebuild required ($frontend_reason)"
  docker compose build web-booking web-reception
  docker compose up -d web-booking web-reception
fi

if ! $needs_backend_rebuild && ! $needs_frontend_rebuild; then
  log "No rebuild needed; restarting services"
  docker compose restart
fi

log "Running Django system check..."
docker compose exec -T django python manage.py check

if [[ -x "$REPO_ROOT/scripts/verify-demo-evisitor.sh" ]]; then
  "$REPO_ROOT/scripts/verify-demo-evisitor.sh"
fi

log "Done."
docker compose ps
