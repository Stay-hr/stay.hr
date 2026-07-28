#!/usr/bin/env bash
# Remote deploy to production (dedicated-hel1): git pull + ./scripts/deploy.sh
#
# Usage:
#   ./scripts/remote-deploy.sh
#   ./scripts/remote-deploy.sh --dry-run
#   DEPLOY_SSH_HOST=dedicated-hel1 DEPLOY_REMOTE_PATH=/opt/stacks/stay.hr ./scripts/remote-deploy.sh
#
# Requires local SSH access to DEPLOY_SSH_HOST (see docs/operations/deploy-ci.md).

set -euo pipefail

SSH_HOST="${DEPLOY_SSH_HOST:-dedicated-hel1}"
REMOTE_PATH="${DEPLOY_REMOTE_PATH:-/opt/stacks/stay.hr}"
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    --dry-run) DRY_RUN=true ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

REMOTE_SCRIPT=$(cat <<EOF
set -euo pipefail
cd $(printf '%q' "$REMOTE_PATH")
echo "==> Remote \$(hostname) \$(pwd)"
echo "==> Before: \$(git rev-parse --short HEAD) (\$(git branch --show-current))"
git fetch origin main
git checkout main
git pull --ff-only origin main
echo "==> After pull: \$(git rev-parse --short HEAD)"
./scripts/deploy.sh
echo "==> Deploy finished"
EOF
)

if $DRY_RUN; then
  echo "Would run: ssh $(printf '%q' "$SSH_HOST") <<'EOF'"
  echo "$REMOTE_SCRIPT"
  echo "EOF"
  exit 0
fi

echo "==> SSH $SSH_HOST → $REMOTE_PATH"
ssh "$SSH_HOST" "$REMOTE_SCRIPT"
