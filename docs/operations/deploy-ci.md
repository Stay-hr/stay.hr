# Deploy from GitHub Actions (main → dedicated-hel1)

## Release workflow (project rule)

Default path for Stay.hr changes:

```text
implement → commit → PR → CI → merge → deploy (CI) → post-deploy check
```

| Step | What | Gate |
|------|------|------|
| Implement | Code + tests on a feature branch | — |
| PR | Open against `main` | Required |
| CI | **`PR CI / backend`** green on head SHA | Merge blocked until green |
| Merge | Squash/merge via PR | Branch protection |
| Deploy | Automatic **Deploy production** on `push` to `main` | Actions run must succeed |
| Post-deploy | Optional smoke (health, dry-run CLI) | Only **after** deploy; never before PR/CI |

**Do not** treat production dry-run, manual migrate/seed, or manual `./scripts/deploy.sh` / `remote-deploy` as the next step after implementation. Those are post-deploy or incident/hotfix paths only when explicitly requested.

Cursor agents: [`.cursor/rules/release-workflow.mdc`](../../.cursor/rules/release-workflow.mdc) (`alwaysApply`).

## PR CI vs deploy CI

| | PR CI | Deploy |
|--|--|--|
| Workflow | [`.github/workflows/pr-ci.yml`](../../.github/workflows/pr-ci.yml) | [`.github/workflows/deploy-production.yml`](../../.github/workflows/deploy-production.yml) |
| When | every `pull_request` | `push` to `main` / `workflow_dispatch` |
| Purpose | merge gate (tests) | production roll-out |
| Required check name | **`PR CI / backend`** (stable; suite content may evolve) | — |

Local `./scripts/run-tests-postgis.sh` (default: `apps.integrations.tests`) is for developers; it is **not** a substitute for green PR CI.

**PR CI capability (v2):** always `check` + migrate + health. Path filters select **CI-safe** suites (e.g. `reservations` → address normalizer + mrz/phone/nationality; `billing` → ePorezna). Shared `core` / `config` / `requirements.txt` → expanded safe suite. Full package suites remain local until Actions parity.

**Nightly:** [`.github/workflows/nightly.yml`](../../.github/workflows/nightly.yml) runs broader PostGIS suites on a schedule (`0 3 * * *` UTC) and `workflow_dispatch`. It is **not** a required check; failures stay red and upload a log artifact (ADR [0018](../architecture/adr/0018-continuous-integration-policy.md)). `test_postgis` sets `GUEST_CHECKIN_WEB_ONLY=False` so lifecycle suites match local docker-run expectations. WhatsApp send paths read `WHATSAPP_ACCESS_TOKEN` from the environment; CI and `test_postgis` set a dummy token so `send_credentials_ok()` does not return `missing_credentials` when host `.env` is absent. Actions runners also need `app.stay.hr` / `admin.stay.hr` in `ALLOWED_HOSTS` (via `test_postgis` + workflow env) — otherwise Django returns **400 DisallowedHost** on reception login / admin. Admin template tests use non-manifest static storage (no `collectstatic` on CI).



### Branch protection (`main`)

Enable with the idempotent script (preferred over clicking through the UI):

```bash
./scripts/enable-main-branch-protection.sh
# optional: APPROVING_REVIEW_COUNT=1 ./scripts/enable-main-branch-protection.sh
```

The script verifies `gh auth`, confirms a successful **`PR CI / backend`** job exists, PUTs protection, then GETs and prints a short verification summary.

Rules applied:

1. Require a pull request before merging
2. Require status checks to pass → **`PR CI / backend`**
3. Require branches to be up to date before merging (strict)
4. Dismiss stale pull request approvals when new commits are pushed
5. Require linear history
6. Enforce for admins; block force pushes and branch deletions

Do **not** wait for capability-based CI before turning this on — later PRs must already go through the gate.

#### Latest SHA (required checks)

GitHub requires the status check to be green on the **latest commit SHA** of the PR head, not an older push. After a new commit or force-push, re-run **PR CI** (or push again); a green check on a previous SHA does not satisfy the gate.

#### If merge is blocked — do not disable protection

1. Confirm head SHA: `gh pr view <n> --json headRefOid -q .headRefOid`
2. Confirm the check ran on that SHA: `gh pr checks <n>`
3. Re-run the failed/missing workflow on the latest SHA
4. Only then investigate the failure

Do **not** turn off branch protection or remove the required check to “just merge”.

## What runs (deploy)

Workflow [`.github/workflows/deploy-production.yml`](../../.github/workflows/deploy-production.yml):

- **Triggers:** `push` to `main`, `workflow_dispatch`
- **Action:** SSH to production → `git pull --ff-only origin main` → [`./scripts/deploy.sh`](../../scripts/deploy.sh)
- **Concurrency:** one deploy at a time (`deploy-production`)

`deploy.sh` rebuilds Django/Celery and/or Next images when backend/frontend sources (or migrations) changed since the last image build; otherwise restarts services.

## Required secrets

Repository → **Settings → Secrets and variables → Actions**:

| Secret | Example | Notes |
|--------|---------|--------|
| `DEPLOY_HOST` | `65.108.196.92` | Production SSH host (hel1) |
| `DEPLOY_USER` | `root` or deploy user | Must be able to run docker compose in the stack dir |
| `DEPLOY_SSH_KEY` | PEM private key | Matching public key in `~/.ssh/authorized_keys` on the server |

Optional:

| Secret | Default |
|--------|---------|
| `DEPLOY_SSH_PORT` | `22` |
| `DEPLOY_PATH` | `/opt/stacks/stay.hr` |

### Suggested deploy key

On the server (once):

```bash
# On a secure machine — generate a deploy-only key (no passphrase for CI):
ssh-keygen -t ed25519 -f stay-hr-deploy -C "github-actions-deploy" -N ""

# Install public key on hel1 for DEPLOY_USER
ssh dedicated-hel1 'mkdir -p ~/.ssh && chmod 700 ~/.ssh'
ssh dedicated-hel1 'cat >> ~/.ssh/authorized_keys' < stay-hr-deploy.pub
```

In GitHub:

```bash
gh secret set DEPLOY_HOST --body '65.108.196.92'
gh secret set DEPLOY_USER --body 'YOUR_USER'
gh secret set DEPLOY_SSH_KEY < stay-hr-deploy   # private key file
```

Restrict the key if possible (ForcedCommand / limited user). Do **not** commit private keys.

## Manual / local remote deploy

From WSL/Linux (same remote steps as CI):

```bash
./scripts/remote-deploy.sh
./scripts/remote-deploy.sh --dry-run
```

Uses local SSH config host `dedicated-hel1` by default (`DEPLOY_SSH_HOST` / `DEPLOY_REMOTE_PATH` overrides).

Windows: `scripts/remote-deploy.ps1` (local-only; gitignored).

## Verify

After merge to `main`:

1. Actions → **Deploy production** should be green
2. `https://app.stay.hr` / `https://api.stay.hr` respond
3. On server: `cd /opt/stacks/stay.hr && git rev-parse HEAD` matches `origin/main`
