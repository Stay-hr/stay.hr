#!/usr/bin/env bash
# Prove deploy.sh rebuild detection does not let docker compose drain the find list.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat >"$TMP/docker" <<'EOF'
#!/usr/bin/env bash
drain_mark="${DEPLOY_TEST_DRAIN_MARK:-}"
if [[ -n "$drain_mark" ]]; then
  if IFS= read -r -d '' _chunk; then
    printf 'drained\n' >"$drain_mark"
  fi
fi
if [[ "${1:-}" == compose && "${2:-}" == images ]]; then
  printf 'fakeimage\n'
  exit 0
fi
if [[ "${1:-}" == inspect ]]; then
  printf '2020-01-01T00:00:00Z\n'
  exit 0
fi
exit 0
EOF
chmod +x "$TMP/docker"

# Load only the detection helpers from deploy.sh (stop before pending_migrations).
eval "$(
  awk '
    /^service_image_id\(\)/ { keep=1 }
    /^pending_migrations_in_db\(\)/ { exit }
    keep { print }
  ' "$ROOT/scripts/deploy.sh"
)"

export PATH="$TMP:$PATH"
export DEPLOY_TEST_DRAIN_MARK="$TMP/drained"

newer="$TMP/newer.ts"
: >"$newer"
# File is newer than the mocked 2020-01-01 image.
touch -d '2024-01-01' "$newer"

if ! files_newer_than_service_image web-reception < <(printf '%s\0' "$newer"); then
  echo "expected rebuild detection to see newer source file" >&2
  exit 1
fi

if [[ -f "$DEPLOY_TEST_DRAIN_MARK" ]]; then
  echo "docker compose consumed the find list on stdin" >&2
  exit 1
fi

echo "ok: rebuild detection keeps the find list away from docker compose images"
