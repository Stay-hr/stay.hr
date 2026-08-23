#!/usr/bin/env bash
# Unit-test deploy.sh SHA bake helpers (no Docker).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# Load only the SHA helper functions (stop before deploy.sh captures DEPLOY_SHA).
eval "$(
  awk '
    /^resolve_deploy_sha\(\)/ { keep=1 }
    /^DEPLOY_SHA=/ { exit }
    keep { print }
  ' "$ROOT/scripts/deploy.sh"
)"

if require_production_deploy_sha "" 2>/dev/null; then
  fail "empty SHA should be rejected"
fi
if require_production_deploy_sha "unknown" 2>/dev/null; then
  fail "unknown should be rejected"
fi
if require_production_deploy_sha "UNKNOWN" 2>/dev/null; then
  fail "UNKNOWN should be rejected"
fi
if require_production_deploy_sha "abc1234" 2>/dev/null; then
  fail "short SHA should be rejected (full SHA is the deploy contract)"
fi
if require_production_deploy_sha "not-a-sha" 2>/dev/null; then
  fail "non-hex should be rejected"
fi
full="0123456789abcdef0123456789abcdef01234567"
require_production_deploy_sha "$full" || fail "40-char hex SHA should be accepted"

git_repo="$TMP/repo"
git init -q "$git_repo"
git -C "$git_repo" -c user.email=test@example.com -c user.name=test \
  commit --allow-empty -q -m init
got="$(resolve_deploy_sha "$git_repo")"
want="$(git -C "$git_repo" rev-parse HEAD)"
[[ "$got" == "$want" ]] || fail "resolve_deploy_sha got $got want $want"
require_production_deploy_sha "$got" || fail "SHA from git repo should pass production check"
[[ -z "$(resolve_deploy_sha "$TMP")" ]] || fail "non-repo should yield empty SHA"

env_file="$TMP/.env"
printf 'DB_HOST=postgis\n' >"$env_file"
if env_overrides_stay_git_sha "$env_file"; then
  fail "absent STAY_GIT_SHA should not count as override"
fi
printf 'STAY_GIT_SHA=deadbeef\n' >>"$env_file"
env_overrides_stay_git_sha "$env_file" || fail "STAY_GIT_SHA= in .env should count as override"
printf '# STAY_GIT_SHA=deadbeef\n' >"$env_file"
if env_overrides_stay_git_sha "$env_file"; then
  fail "commented STAY_GIT_SHA should not count as override"
fi
if env_overrides_stay_git_sha "$TMP/missing.env"; then
  fail "missing .env should not count as override"
fi

grep -F 'DEPLOY_SHA="$(resolve_deploy_sha "$REPO_ROOT")"' "$ROOT/scripts/deploy.sh" >/dev/null \
  || fail "DEPLOY_SHA must be captured once from the checkout"
grep -F -- '--build-arg STAY_GIT_SHA="$DEPLOY_SHA"' "$ROOT/scripts/deploy.sh" >/dev/null \
  || fail "backend build must pass --build-arg STAY_GIT_SHA"
if grep -Eq '^[[:space:]]*STAY_GIT_SHA=' "$ROOT/.env.example"; then
  fail ".env.example must not assign STAY_GIT_SHA at runtime"
fi
grep -F 'ARG STAY_GIT_SHA=unknown' "$ROOT/Dockerfile" >/dev/null \
  || fail "Dockerfile must keep ARG STAY_GIT_SHA=unknown as local fallback"

echo "ok: deploy SHA bake helpers and production wiring"
