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
