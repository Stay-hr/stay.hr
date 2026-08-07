# Deploy from GitHub Actions (main → dedicated-hel1)

## PR CI vs deploy CI

| | PR CI | Deploy |
|--|--|--|
| Workflow | [`.github/workflows/pr-ci.yml`](../../.github/workflows/pr-ci.yml) | [`.github/workflows/deploy-production.yml`](../../.github/workflows/deploy-production.yml) |
| When | every `pull_request` | `push` to `main` / `workflow_dispatch` |
| Purpose | merge gate (tests) | production roll-out |
| Required check name | **`PR CI / backend`** (stable; suite content may evolve) | — |

Local `./scripts/run-tests-postgis.sh` is for developers; it is **not** a substitute for green PR CI.

**Follow-up (not in the first PR CI workflow):** re-enable `makemigrations --check --dry-run` after committing pending model/index migration drift on `main` (communications, properties, integrations, reservations). Until then that step would fail every PR.

### Branch protection (`main`) — enable right after PR CI merges

Settings → Branches → Branch protection rule for `main`:

1. Require a pull request before merging
2. Require status checks to pass → **`PR CI / backend`**
3. Require branches to be up to date before merging
4. Dismiss stale pull request approvals when new commits are pushed
5. Require linear history (if the repo uses squash merge)
6. Do not allow bypassing the above settings (admins included, if the plan allows)
7. Block force pushes and branch deletions

Do **not** wait for capability-based CI (follow-up PR) before turning this on — later PRs must already go through the gate.

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
