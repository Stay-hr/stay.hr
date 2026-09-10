#!/bin/sh
set -e

cd /app/backend

mkdir -p media

# deploy.sh starts django, celery-worker and celery-beat in one `up -d` and compose
# declares no ordering, so every service migrating means concurrent migrate processes
# on one database. Only the web role writes the schema; workers wait for it.
STARTUP_MODE="${STARTUP_MODE:-web}"
STARTUP_MIGRATION_WAIT_ATTEMPTS="${STARTUP_MIGRATION_WAIT_ATTEMPTS:-20}"
STARTUP_MIGRATION_WAIT_INTERVAL="${STARTUP_MIGRATION_WAIT_INTERVAL:-3}"

await_migrations() {
  attempt=1
  while [ "$attempt" -le "$STARTUP_MIGRATION_WAIT_ATTEMPTS" ]; do
    if python manage.py migrate --check >/dev/null 2>&1; then
      echo "startup: schema is up to date"
      return 0
    fi
    echo "startup: waiting for migrations (${attempt}/${STARTUP_MIGRATION_WAIT_ATTEMPTS})"
    attempt=$((attempt + 1))
    sleep "$STARTUP_MIGRATION_WAIT_INTERVAL"
  done

  # The loop discards output to keep the wait quiet. A timeout can also mean bad
  # config or an unreachable database, so surface the real error before exiting.
  echo "startup: migrations still pending after ${STARTUP_MIGRATION_WAIT_ATTEMPTS} checks" >&2
  python manage.py migrate --check
}

case "$STARTUP_MODE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    ;;
  worker)
    await_migrations
    ;;
  *)
    echo "startup: unknown STARTUP_MODE='${STARTUP_MODE}' (expected web or worker)" >&2
    exit 1
    ;;
esac

exec "$@"
