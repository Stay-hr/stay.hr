# Architecture Decision Records

## Hierarchy

| Layer | Role |
|-------|------|
| **This README** | Process and criteria for how ADRs arise |
| **Individual ADRs** | Stable patterns |
| **Implementations** | Prove patterns in production |
| **PR review** | Check that new work applies the patterns |

## Platform principles

1. **Production is the source of architectural knowledge.**
2. **A standard follows a proven implementation, not the reverse.**
3. **An ADR exists to reduce future debates and increase consistency** — not only to document the past.
4. **Deviations are allowed, but must be justified and documented** (module docs / PR, or a new ADR).

## Proven before standardized

> **We standardize what has been proven in production, not what we assume in design.**

Consequences:

- An **ADR is not the start of development** — it is the result of a successful pattern maturing.
- A **PR review checklist is not bureaucracy** — it checks that new work follows an already-proven standard.
- **Exceptions are not holes in the standard** — they are conscious architectural decisions that deserve their own documentation (or a new ADR).

Do **not** write platform ADRs from speculative design. Prefer: production incident → minimal correct implementation → stabilize → measure → Production Ready milestone → extract pattern → platform ADR → review checklist → stable standard.

Reference maturation path: eVisitor v1 → [ADR 0016: External Integration Pattern](0016-external-integration-pattern.md).

## Stable platform standards

| ADR | Title | Notes |
|-----|-------|-------|
| [0016](0016-external-integration-pattern.md) | External Integration Pattern | Stable standard for outbound integrations |
| [0017](0017-document-intake-identity-consistency.md) | Document intake identity consistency | Cross-job identity hard match + UNIQUE |
| [0018](0018-continuous-integration-policy.md) | Continuous Integration Policy | PR CI gate ≠ deploy; latest SHA; deterministic CI |

Other ADRs in this folder decide specific modules or evolutions; they are not automatically “platform standards” until they have been proven and explicitly marked as such.
