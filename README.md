# AI_Smart_Home — Two-House AI Operations Layer

Infrastructure-grade residential command-and-control architecture for two adjacent
properties (**House A**, **House B**) sharing one AI operations layer. Designed for
**capability first**, but built on three non-negotiables: **local-first reliability**,
**human override on every system**, and **strong network segmentation**.

## What this is

A serious residential "AI ops layer" — not a consumer smart-home kit — that can monitor,
coordinate, and control power, backup/solar/generator, lighting, HVAC, water, locks/access,
garage/gates, cameras, perimeter and life-safety sensors, network/cybersecurity, appliances,
intercoms, and occupancy across both houses, under a graduated permission model that keeps
dangerous actions behind hardware, confirmation, and human approval.

## Core principles

- **Local-first:** every critical automation runs on-premises (Home Assistant + Node-RED)
  with no cloud and no internet. The AI *augments* the house; it is never a dependency.
- **Human override everywhere:** physical switch, valve, key, breaker, and thermostat always
  work. A per-house "AI hold" suspends AI actuation while local automations keep running.
- **Segmented & hardened:** separate VLANs for trusted, IoT, cameras, servers, guest, and
  automation; WireGuard-only remote access; MFA; audit logging; no default creds, no exposed
  management ports.
- **Two-house separation:** independent cores, networks, identities, and logs. Every command
  resolves to exactly one house; cross-house or high-impact actions require explicit confirm.
- **Safe high-power integration:** panels, breakers, generator, solar, battery, ATS, and
  egress hardware are professionally installed and inspected. The AI monitors freely and
  controls only through approved equipment; it never bypasses a code-required safety system.

## AI permission model (6 levels)

| Level | Name | AI capability |
|---|---|---|
| 0 | Observe | read all sensors, cameras, meters, logs, network, power, water, environment |
| 1 | Routine | direct: lights, thermostats (in range), fans, blinds, speakers, non-critical plugs, scenes, notifications |
| 2 | Security/Utility | conditioned/confirmed: locks, arm/disarm, garage (confirm), exterior lights, water shutoff, irrigation, IoT quarantine, camera modes, alarm escalation |
| 3 | Power/Infra | approved HW + confirm: smart panel/breakers, load-shed, generator start, battery modes, EV limits, HVAC emergency shutoff, whole-house water shutoff, firewall policy |
| 4 | Recommend only | main breaker, utility side, permanent firewall restructure, life-safety changes, unlocking for unknown persons — **notify a human, no auto-execute** |
| 5 | Prohibited | bypass electrical safety, disable smoke/CO, meter tampering, illegal lock defeat, disable emergency systems, interfere with responders |

L4/L5 actions have **no execution path** exposed to the AI — only a recommend/notify path.

## Repository

- **[DESIGN.md](DESIGN.md)** — the full structured technical plan, sections **A–AA**:
  architecture, house separation, control hierarchy, per-subsystem capability contracts
  (monitor / direct / confirm / professional / manual / failure / recovery), electrical,
  water, security, cameras, HVAC, network, local server, command interface, permission
  levels, automation policy, emergency logic, transfer plan, install phases, testing,
  maintenance, risk controls, example commands & automations, BOM, and the deployment
  checklist.
- **[config/houses.example.yaml](config/houses.example.yaml)** — the configuration schema
  realized as a working two-house example (role-based, so House B is a parameterized copy).
- **`homeops/`** — a **runnable, mocked reference implementation** that simulates *both houses
  entirely in software* (no hardware, no real HA/OPNsense): permission engine, command router,
  local-first automations, device/network simulators behind adapter interfaces, and a Claude
  ops layer. See below.

## Run the reference implementation

Everything in `DESIGN.md` is exercised in software so the architecture and the permission
model can be validated before any hardware is bought. Local-first (automations run below the
AI), human-override-preserving, and the AI can only *propose* — the deterministic engine
decides.

```bash
pip install -r requirements.txt          # PyYAML + pytest (anthropic only for the live test)
pytest -q                                # full offline suite: permissions, router, automations, fail-safe,
                                         #   local-first, AI-ops, audit, health, RBAC, portfolio, exporters, dashboard
python scripts/run_scenario.py all       # replays leak / grid-loss / fire-CO / intrusion / rogue-device with asserted timelines
python scripts/demo.py                   # end-to-end two-house demo (cross-house guard, WAN-down local-first, L4 refusal)
python -m homeops.cli status             # dashboard of both houses
python -m homeops.cli validate  deploy/deployment.example.yaml   # offline lint (fail-closed)
python -m homeops.cli preflight /etc/homeops/deployment.yaml     # read-only live commissioning
python -m homeops.cli serve     /etc/homeops/deployment.yaml     # systemd-managed runtime
```

The Claude ops layer (`homeops/ai/`) uses `claude-opus-4-8` with adaptive thinking and a
cached system prefix, and proposes actions through gated, audited tools. The offline suite
drives it with a scripted mock (no network); an optional live smoke test runs with
`ANTHROPIC_API_KEY=… pytest -m live`. When the API/internet is unavailable or a house is on
"AI hold," it degrades to the deterministic fallback — the house is never in the AI's hands
for safety.

Design choices: the simulator is **in-process and synchronous** (deterministic, CI-runnable,
no external infra); real Home Assistant and OPNsense drop in behind the `homeops/adapters/`
interfaces without touching the engine, automations, or AI code. Models are dataclass-based
to keep the dependency surface minimal.

### Driving real hardware

The same engine/automations/AI run unchanged against a live **Home Assistant** (REST for
commands, WebSocket for the live event feed) and **OPNsense** (REST) — the only thing that
changes is the adapter:

```python
from homeops import build_real_world, start_event_bridge

world = build_real_world(
    ha_base_url="http://homeassistant.local:8123", ha_token="<HA long-lived token>",
    opn_base_url="https://opnsense.local", opn_key="<key>", opn_secret="<secret>",
    entity_map={"house_a.lock.front_door": "lock.front_door"},        # homeops id -> real HA entity
    event_map={"binary_sensor.leak_kitchen": {"type": "leak", "when": "on",
                                              "house_id": "house_a", "data": {"flow": 45}}},
    verify_tls=False,   # common for self-signed appliances
)
start_event_bridge(world)   # HA state_changed -> the same local-first automations
```

- **`homeops/adapters/homeassistant.py`** — maps every intent to an HA `domain.service` call,
  reads prior state for rollback of the reversible subset (on/off, lock, cover, valve, alarm),
  and bridges HA `state_changed` events onto the bus. Command actuation is stdlib-only
  (`urllib`); the WebSocket bridge needs the optional `websocket-client` package.
- **`homeops/adapters/opnsense.py`** — IoT quarantine (add host to a firewall alias +
  reconfigure) and firewall-policy rules over the OPNsense API.
- **`homeops/adapters/composite.py`** — routes `network` → OPNsense, everything else → HA.

Both are unit-tested offline against a fake HTTP transport and a fake WebSocket connection
(`tests/test_real_adapters.py`), so no live services or extra deps are needed to run the suite.

### Hardening from external review

The permission model was adversarially reviewed (by a different LLM) and hardened; each fix has
a regression test in `tests/test_hardening.py`:

- The AI can no longer self-confirm cross-house actions (the `confirm_cross_house` flag was removed
  from the AI tool surface — a human must confirm).
- Confirmation tokens are now unguessable (`secrets`) and bound to the **full intent (including
  args) and the operator identity** — a token can't be reused with different args or by a different
  operator.
- The leak "two-signal" rule re-reads **both independent state channels** (wet sensor AND abnormal
  flow) at actuation time; a spoofed or stale `leak` event no longer closes the valve.
- Rollback cancels pending physical transitions (an undone valve close won't sneak to "closed" two
  ticks later).
- The real HA adapter **verifies safety-impacting actions** by reading device state back — HTTP 200
  is not treated as proof a valve closed or a lock threw.
- The AI fallback runs as an AI-limited operator, never silently as `owner`.
- Adapter action mappings are **fail-closed** (a stray `unlock_unknown` can't become `lock.unlock`),
  OPNsense checks every mutating call, and rollbacks + manual overrides are now audited.
- `build_real_world(strict_entity_map=True)` **fails startup** if any controllable entity lacks an
  explicit HA mapping — preventing House A and House B from collapsing onto the same real entities.

> **Deployment caveat (by design, not a bug):** in this reference implementation the local-first
> automations run in the Python event bus. A production deployment should ALSO express the
> life-safety subset (leak, fire/CO, freeze) as **native Home Assistant / Node-RED automations** so
> they keep running even if this Python process dies. The Python layer is the AI-coordination and
> validation tier; it is not the last line of defense. `homeops.exporters` generates exactly those
> native HA automations — see below.

### Pilot-hardening modules (`docs/PRODUCTIZATION.md`)

Beyond the core reference impl, these move it toward a pilot-ready managed service (each with tests):

- **`homeops/audit.py`** — tamper-evident, hash-chained, append-only audit (`verify_chain()`), optional
  JSONL persistence reloaded + re-verified on restart.
- **`homeops/health.py`** — per-device health/heartbeat; the router refuses safety-critical actuation on
  an offline/stale device and records `unverified` when a device accepts a command but doesn't move.
- **`homeops/identity.py`** — RBAC: authenticated principals (owner / estate-manager / installer /
  monitor / …) with a role capability cap and per-property scope.
- **`homeops/portfolio.py`** + **`config/portfolio.example.yaml`** — N-property portfolio view (the
  estate/family-office shape); **`homeops/adapters/per_property.py`** routes each property to its own HA/OPNsense.
- **`homeops/exporters/`** — emits native Home Assistant life-safety automation YAML (leak/freeze/fire-CO).
- **`homeops/dashboard.py`** — self-contained HTML operator oversight view (`render_dashboard`).

The full commercialization analysis and blocker ranking are in **`docs/STRATEGY.md`** and
**`docs/PRODUCTIZATION.md`**. Reality check: this is pilot-hardening *scaffolding* — real deployment
still requires an independent security review, verified fail-safe on real hardware, licensed-professional
installation, and a liability/insurance structure.

## Reference stack (local-first)

Home Assistant OS cores (primary + cold spare per house) · Mosquitto MQTT · Zigbee2MQTT ·
Z-Wave JS · Thread/Matter border router · Node-RED · OPNsense/UniFi firewall with VLANs +
Suricata IDS/IPS · Frigate NVR + edge-TPU on an isolated camera VLAN · smart panel
(Span/Lumin) or smart breakers · Powerwall/FranklinWH battery · solar + generator + ATS ·
motorized water main + flow/pressure + leak mesh · Z-Wave/Matter locks + monitored alarm
panel · UPS + NAS (ZFS) · WireGuard remote access.

> **Scope note.** This is an architecture and configuration blueprint. Electrical,
> generator, solar/battery, gas, plumbing, egress, and life-safety work must be performed by
> licensed professionals to local code and inspected. The design defers to code and keeps
> every life-safety system independent of the AI.
