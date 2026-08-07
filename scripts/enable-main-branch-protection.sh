#!/usr/bin/env bash
# Enable branch protection on main for stay.hr (idempotent).
#
# Requires: gh auth with admin:repo (or org owner) scopes.
# Precondition: workflow "PR CI" has succeeded at least once so GitHub
# knows the check context "PR CI / backend".
#
# Usage:
#   ./scripts/enable-main-branch-protection.sh
#   OWNER=Stay-hr REPO=stay.hr BRANCH=main ./scripts/enable-main-branch-protection.sh

set -euo pipefail

OWNER="${OWNER:-Stay-hr}"
REPO="${REPO:-stay.hr}"
BRANCH="${BRANCH:-main}"
REQUIRED_CHECK="${REQUIRED_CHECK:-PR CI / backend}"
# 0 = require PR without mandating human approval (solo-friendly). Bump to 1 for teams.
APPROVING_REVIEW_COUNT="${APPROVING_REVIEW_COUNT:-0}"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

log "==> Checking gh auth"
if ! gh auth status >/dev/null 2>&1; then
  die "gh is not authenticated. Run: gh auth login"
fi
gh auth status

log "==> Resolving repo ${OWNER}/${REPO}"
gh api "repos/${OWNER}/${REPO}" --jq .full_name >/dev/null \
  || die "cannot access repos/${OWNER}/${REPO}"

log "==> Verifying required check context exists: ${REQUIRED_CHECK}"
# Prefer a successful PR CI run that recorded this job name.
success_runs="$(
  gh run list --repo "${OWNER}/${REPO}" --workflow pr-ci.yml --limit 20 \
    --json databaseId,conclusion \
    --jq '[.[] | select(.conclusion == "success") | .databaseId] | length'
)"
if [[ "${success_runs}" -lt 1 ]]; then
  die "no successful \"PR CI\" workflow run found. Merge a PR that runs PR CI first (e.g. #48), then re-run this script."
fi

# Confirm the job display name appeared in a recent successful run's jobs.
job_match=0
while read -r run_id; do
  [[ -z "${run_id}" ]] && continue
  matched="$(
    gh api "repos/${OWNER}/${REPO}/actions/runs/${run_id}/jobs" \
      --jq "[.jobs[] | select(.name == \"${REQUIRED_CHECK}\" and .conclusion == \"success\")] | length"
  )"
  if [[ "${matched}" -ge 1 ]]; then
    job_match=1
    break
  fi
done < <(
  gh run list --repo "${OWNER}/${REPO}" --workflow pr-ci.yml --limit 10 \
    --json databaseId,conclusion --jq '.[] | select(.conclusion=="success") | .databaseId'
)

if [[ "${job_match}" -ne 1 ]]; then
  die "successful PR CI runs exist, but none contain job \"${REQUIRED_CHECK}\". Check the workflow job name."
fi
log "    found successful job: ${REQUIRED_CHECK}"

log "==> PUT branch protection on ${BRANCH}"
# Use raw JSON input — nested arrays are unreliable with -f/-F alone.
payload="$(cat <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["${REQUIRED_CHECK}"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": ${APPROVING_REVIEW_COUNT}
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
)"

echo "${payload}" | gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" \
  --input - >/dev/null

log "==> Verifying protection"
strict="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq .required_status_checks.strict)"
contexts="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq '.required_status_checks.contexts | join(", ")')"
dismiss="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq .required_pull_request_reviews.dismiss_stale_reviews)"
linear="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq .required_linear_history.enabled)"
force="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq .allow_force_pushes.enabled)"
admins="$(gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq .enforce_admins.enabled)"

[[ "${strict}" == "true" ]] || die "strict up-to-date is not enabled"
echo "${contexts}" | grep -Fq "${REQUIRED_CHECK}" || die "required check missing from contexts: ${contexts}"
[[ "${dismiss}" == "true" ]] || die "dismiss stale reviews is not enabled"
[[ "${linear}" == "true" ]] || die "linear history is not enabled"
[[ "${force}" == "false" ]] || die "force push is not blocked"
[[ "${admins}" == "true" ]] || die "enforce_admins is not enabled"

log ""
log "✓ Branch protection enabled"
log "✓ Required check: ${REQUIRED_CHECK}"
log "✓ Require up-to-date: enabled"
log "✓ Dismiss stale approvals: enabled"
log "✓ Linear history: enabled"
log "✓ Force push / delete: blocked"
log "✓ Enforce admins: enabled"
log "✓ Required approving reviews: ${APPROVING_REVIEW_COUNT}"
log ""
log "Done. Do not disable protection ad hoc if a PR is blocked — re-run CI on the latest SHA first."
