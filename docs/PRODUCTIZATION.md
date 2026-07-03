# Productization plan — turning the reference impl into a pilot-ready product

This tracks the engineering work that moves `homeops` from a **mocked reference implementation**
to something that could safely run a real pilot home. Ordered by the S1→S2 severity from
`docs/STRATEGY.md`. Each part ships as its own commit with tests.

> Reality anchor: none of this makes the system production-safe for real homes on its own. Real
> deployment still requires an independent security review, verified fail-safe on real hardware,
> licensed-professional installation, and a liability/insurance structure. See `docs/STRATEGY.md`.

## Parts

- [x] **Part 1 — Tamper-evident + persistent audit** (S1). Hash-chained, append-only, crash-recoverable
      audit log with `verify_chain()`. Turns "audit completeness" into an evidence trail an insurer
      or dispute could rely on.
- [x] **Part 2 — Verified-actuation framework + device health** (S1). First-class "command → verify
      the device actually reached the commanded state → else fail" across adapters, plus per-device
      health/heartbeat so a dead device is known, not assumed working.
- [x] **Part 3 — Identity, roles & capabilities (RBAC)** (S1). Authenticated principals
      (owner / estate-manager / installer / monitor / ai / system / guest) with explicit capability
      sets and per-property scoping. Removes the "trust-open" plane.
- [x] **Part 4 — Multi-property control plane** (S2). Generalize the two-house model to N properties
      with per-property isolation and a portfolio view — the estate/family-office shape.
- [x] **Part 5 — Native life-safety export** (S1). Emit the life-safety subset (leak, fire/CO, freeze)
      as native Home Assistant / Node-RED automation definitions so they survive the Python process.
- [x] **Part 6 — Operator dashboard** (S2). A real oversight UI for the managed-monitoring tier.
- [x] **Part 7 — Runtime service** (S1). A long-running, systemd-managed daemon (`homeops.cli serve`):
      housekeeping loop, HA event bridge, liveness heartbeat, and a READ-ONLY HTTP surface
      (`/`, `/healthz`) that is token-gated and refuses non-loopback binds without a bearer token.
      No write path exists on the network surface by construction. Hardened unit file + installer
      in `deploy/`.
- [x] **Part 8 — Secrets & deployment hardening** (S1). Fail-closed secrets loading (0600-enforced
      env file, environment override), a deployment descriptor as the single installer-edited file,
      and `homeops.cli validate`: offline lint that refuses partial entity maps, cross-house entity
      collapse, missing persistent audit in real mode, and unauthenticated exposure.
- [x] **Part 9 — Read-only preflight commissioning** (S1). `homeops.cli preflight`: GET-only probes
      of live HA/OPNsense proving token validity, that every mapped entity exists, that each
      mapping's HA domain is plausible for its subsystem, and current device availability — the
      maximal hardware-facing verification possible without actuation. The residual gap is
      irreducible in software: supervised actuation trials with a human at each device.

- [x] **Part 10 — Resident chat + operational charter** (S2). Stateful back-and-forth
      (`homeops/ai/session.py`, `cli ask`): conversation memory across turns, per-turn estate
      snapshot, and an in-dialogue confirmation dance in which the token travels
      engine -> resident -> engine and provably never enters the model's context (regression-tested
      literally: issued tokens are asserted absent from every model call). The SYSTEM_PROMPT is now
      a full operational charter: identity/purpose, chain of authority (the AI at the bottom),
      permission ladder, confirmation protocol, epistemic conduct, scope & privacy.

## Not in scope for these parts (require the real world)
- Independent security review / penetration test of the actuation plane.
- Verified fail-safe on real heterogeneous hardware.
- Secrets management + secure transport hardening for a specific deployment.
- Legal / insurance / E&O / professional-install contracts.
