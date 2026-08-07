# ADR 0018: Continuous Integration Policy

## Status

Accepted (2026-08) — proven by PR CI (#48), branch protection script (#49), and capability-based selection (#50) on `main`.

## Summary

**Why:** “Locally green, PR without CI” let regressions reach `main` unnoticed. Deploy success is not a substitute for a merge gate.

**How:** Every pull request must pass the stable required check **`PR CI / backend`** on the **latest head SHA**. Deploy stays on `main` only. Capability path filters select CI-safe suites; shared/core/config changes expand the smoke. Nightly runs broader suites without blocking merge. CI must be **deterministic**.

## Decision

1. **Deploy ≠ test CI.** [`.github/workflows/deploy-production.yml`](../../../.github/workflows/deploy-production.yml) rolls out production; [`.github/workflows/pr-ci.yml`](../../../.github/workflows/pr-ci.yml) is the merge gate. Local `./scripts/run-tests-postgis.sh` helps developers; it is **not** a substitute for green PR CI.

2. **Every PR must be green.** Branch protection on `main` requires **`PR CI / backend`**. The check name is stable; suite content may evolve without changing protection (see [`scripts/enable-main-branch-protection.sh`](../../../scripts/enable-main-branch-protection.sh)).

3. **Latest SHA only.** A required status check must succeed on the current PR head commit. After a new push or force-push, re-run CI; a green result on an older SHA does not satisfy the gate.

4. **Do not disable protection ad hoc.** If merge is blocked, verify head SHA vs check run SHA and re-run the workflow. Do not remove required checks to “just merge”.

5. **Capability-based selection.** Path filters map app changes to suites. Shared / cross-cutting paths (`backend/apps/core/**`, `backend/config/**`, `requirements.txt`) use **expanded smoke**, never a single unrelated capability. Full package suites may remain local until Actions parity is complete; expand CI-safe labels deliberately.

6. **CI must be deterministic.** PR and nightly tests must not depend on wall-clock flakiness, the public internet, or production services. The same commit must yield the same result in CI. Prefer fakes/fixtures; mark unavoidable external calls explicitly and keep them out of the merge gate.

7. **Nightly is non-blocking.** Broader / slower suites run on a schedule for visibility; they are not required checks.

## Consequences

- Merge to `main` without green **`PR CI / backend`** on the latest SHA is not allowed.
- Expanding what the gate runs is a workflow change, not a branch-protection change.
- Flaky or network-bound tests are treated as defects in the suite, not as reasons to weaken protection.

## References

- Ops: [`docs/operations/deploy-ci.md`](../../operations/deploy-ci.md)
- Workflow: [`.github/workflows/pr-ci.yml`](../../../.github/workflows/pr-ci.yml)
- Protection: [`scripts/enable-main-branch-protection.sh`](../../../scripts/enable-main-branch-protection.sh)
- PR template: [`.github/pull_request_template.md`](../../../.github/pull_request_template.md)
