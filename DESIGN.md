# Two-House AI Operations Layer — Technical Design

Infrastructure-grade residential command-and-control for two adjacent properties
(**House A**, **House B**) with a shared AI operations layer. Local-first, human-
override-preserving, security-segmented, and cleanly transferable between houses.

> **Safety and legal frame (read first).** This design gives an AI broad *visibility*
> and graduated *control*. Every high-power, life-safety, and code-regulated system is
> professionally installed, retains an independent manual path, and is never solely
> dependent on the AI. The AI never bypasses a code-required safety system. Electrical,
> gas, generator, and utility-side work is performed by licensed professionals and
> inspected. Life-safety alarms (smoke/CO/fire) remain on their own UL-listed, hardwired,
> interconnected circuits as the primary system; the AI observes and augments, it does not
> replace them.

---

## A. Executive summary

A pair of **local-first Home Assistant control cores** (one per house) run every critical
automation on-premises, coordinated by an **AI operations layer** that has a complete
read model of both houses and graduated authority to act. Each house is a fully
independent controllable environment on its own network segments and hub; a **central
command interface** presents both but scopes every command to exactly one house, with
explicit confirmation required for cross-house or high-impact actions.

The AI operates under a **6-level permission model** (Observe → Prohibited). It directly
controls low/medium-risk systems (lighting, climate, non-critical plugs, exterior lights,
irrigation, camera modes), conditionally controls medium/high-impact systems with
confirmation and proper hardware (locks, security arming, garage/gate, water shutoff,
network quarantine), and only *monitors or recommends* on the highest-impact systems
(main breaker, utility side, life-safety). Core functions continue during internet loss
and during AI-layer failure, because the automations and fail-safe states live in local
hardware, not the cloud.

## B. System goals

1. **Complete operational picture** of both houses: power, water, climate, security,
   network, occupancy, and environment, with device-state feedback and event logs.
2. **Graduated, auditable control** — broad on low-risk systems, gated on high-impact ones.
3. **Local-first reliability** — every critical automation runs without cloud/internet.
4. **Human override everywhere** — physical switch/valve/key/breaker always works.
5. **Strong segmentation** — IoT, cameras, servers, guests, and trusted devices isolated.
6. **Clean two-house separation** with a shared but house-scoped command surface.
7. **Modular expansion** — cameras, sensors, solar/battery/generator, smart panels,
   gates, EV chargers, server racks, AI vision, and additional properties.

## C. Assumptions

- Two owner-occupied, adjacent single-family homes on one or two utility services.
- Owner can fund professional electrical/generator/solar/plumbing work where required.
- A low-latency private link can be run between houses (buried fiber/Cat6, or a
  point-to-point wireless bridge) for a site-to-site VLAN trunk / VPN.
- Reasonable broadband at each house, but the system must survive its loss.
- Owner wants *capability first* but accepts confirmation gates on dangerous actions.
- Local jurisdiction electrical/fire/egress codes govern; this design defers to them.

## D. Overall architecture

```
                 ┌─────────────────────────────────────────────┐
                 │        CENTRAL COMMAND (read-mostly)         │
                 │  Dashboard • AI ops layer • cross-house view │
                 │  house-scoped commands • global audit log    │
                 └───────────────┬───────────────┬──────────────┘
                                 │  site-to-site  │  (WireGuard over
                                 │  encrypted link │   dedicated inter-house trunk)
        ┌────────────────────────┴───────┐ ┌──────┴────────────────────────┐
        │            HOUSE A              │ │            HOUSE B             │
        │  ┌──────────────────────────┐  │ │  ┌──────────────────────────┐  │
        │  │ HA Control Core (HAOS)    │  │ │  │ HA Control Core (HAOS)    │  │
        │  │  local automations        │  │ │  │  local automations        │  │
        │  │  MQTT • Node-RED • Zigbee/ │  │ │  │  MQTT • Node-RED • Zigbee/ │  │
        │  │  Z-Wave/Thread coordinators│  │ │  │  Z-Wave/Thread coordinators│  │
        │  └──────────────┬───────────┘  │ │  └──────────────┬───────────┘  │
        │   Firewall/router (VLANs)      │ │   Firewall/router (VLANs)      │
        │   ├ Trusted  ├ IoT  ├ Cameras  │ │   ├ Trusted  ├ IoT  ├ Cameras  │
        │   ├ Servers  ├ Guest ├ Mgmt    │ │   ├ Servers  ├ Guest ├ Mgmt    │
        │   NVR • NAS • UPS • smart panel │ │   NVR • NAS • UPS • smart panel │
        └────────────────────────────────┘ └────────────────────────────────┘
```

**Control plane layers (per house):**

1. **Device layer** — sensors, actuators, locks, relays, smart breakers, cameras.
   Local protocols preferred: Zigbee, Z-Wave, Thread/Matter, PoE/RTSP, Modbus, dry-contact.
2. **Hub layer** — Home Assistant OS on dedicated hardware (primary + cold-spare image),
   Mosquitto MQTT broker, Zigbee2MQTT, Z-Wave JS, Thread border router, Node-RED.
3. **Automation layer** — local HA automations + Node-RED flows encode *all* critical
   logic. These run with no internet and no AI layer.
4. **AI operations layer** — reads state via the HA API / MQTT, reasons over both houses,
   and issues *scoped, permission-checked, logged* commands. It is an **operator**, not a
   dependency: if it stops, the house keeps running on layer 3.
5. **Interface layer** — central dashboard, mobile apps, voice (local Assist), intercoms.

## E. House A / House B separation model

- **Independent cores.** Each house runs its own HA instance, its own coordinators, its
  own automations, its own logs, and its own credentials. Neither can silently actuate the
  other's devices.
- **Independent networks.** Distinct IP supernets (e.g., `10.10.0.0/16` House A,
  `10.20.0.0/16` House B) and distinct VLAN IDs where practical. The inter-house link is a
  firewalled, encrypted trunk carrying only explicitly allowed flows (central dashboard,
  federated read state, backup replication).
- **House-scoped command context.** Every command carries an `active_house`. The AI must
  resolve a target house before acting. **Cross-house commands require explicit
  confirmation** naming the house. `last_commanded_house` is tracked to catch "wrong
  house" mistakes.
- **Separate identities & permissions.** Devices, users, roles, and audit logs are
  namespaced per house (`house_a.garage.door`, `house_b.garage.door`). Shared dashboards
  render both, but a click/command always resolves to one namespace.
- **Federated, not merged.** Central command reads from both and can *route* a command,
  but the authoritative controller for a device is always its own house's core.

## F. AI control hierarchy

```
Observe (all) ──► Recommend ──► Act (permission-checked, house-scoped, logged)
                                   │
     ┌───────────── Level gate ────┼───────────── Confirmation gate ─────────┐
     ▼                             ▼                                         ▼
  L1 routine (auto)         L2 security/utility (conditions/confirm)   L3 infra (approved HW + confirm)
                                                                        L4 recommend-only / L5 prohibited
```

- The AI proposes an action → the **policy engine** checks: permission level, target
  house, current mode (Home/Away/Night/Vacation/Emergency), preconditions, and
  confirmation requirement → executes via the house core → writes an **audit record** with
  rollback token where reversible.
- **Rate limits & interlocks** prevent runaway behavior (e.g., no more than N lock
  toggles/min; load-shed steps are staged; valve close is one-shot with cooldown).

## G. Device categories and capabilities

For every subsystem below, the same seven-point contract is stated:
**Monitor / Direct control / Requires confirmation / Professional install / Stays manual /
Failure modes / Recovery.** Sections H–M expand the high-stakes ones.

| Category | AI monitors | AI direct (L1–L2) | Confirm (L2–L3) | Pro install | Always manual |
|---|---|---|---|---|---|
| Lighting | state, power | on/off/dim, scenes | — | line-voltage relays | wall switch |
| HVAC/climate | temp, humidity, runtime | setpoint in range, fan | mode change, emergency shutoff | equipment, condensate | thermostat |
| Water | flow, pressure, leak | irrigation zones | main/branch shutoff | valve, plumbing | manual valve |
| Locks/access | lock state, events | lock; unlock (conditions) | unlock exterior, disarm | strike/egress wiring | physical key |
| Garage/gate | position, obstruction | close | open | opener safety | button/manual release |
| Cameras | streams, events | record mode, privacy | export/share | PoE cabling | n/a |
| Sensors | all states | — | — | smoke/CO circuit | test buttons |
| Electrical | per-circuit power | non-critical circuits, load-shed | critical circuits | panel/breakers | main breaker |
| Backup power | SoC, gen state, ATS | battery mode (approved) | generator start | inverter/ATS/gen | manual transfer |
| Network | device inventory, threats | IoT quarantine | firewall policy | rack/cabling | console access |
| Appliances/plugs | power, state | non-critical plugs | high-draw loads | 240V circuits | appliance controls |
| Notifications/intercom | delivery | send/announce | — | speaker wiring | phone |

## H. Electrical and power control architecture

**Design.** A **smart electrical panel** (e.g., Span/Lumin) or **smart breakers**
(Eaton/Leviton) provide per-circuit metering and switching, professionally installed to
code. Whole-home energy monitoring via panel-native CTs or an IoTaWatt/Emporia meter.
Backup: **battery** (Powerwall/Enphase/FranklinWH/EG4), **solar** (Enphase/SolarEdge/
Sol-Ark), **generator** (Generac/Kohler) with a code-compliant **Automatic Transfer
Switch (ATS)** or interlock. EV charger with load management.

- **Monitor (L0):** whole-home and per-circuit power, voltage, battery State-of-Charge,
  solar production, generator run state/fuel, ATS position, grid-up/down, EV charge rate.
- **Direct control (L1–L3, approved HW):** toggle *non-critical* circuits/plugs; stage
  **load-shedding**; cap **EV charge current**; select **battery backup reserve/mode**
  within approved bounds.
- **Requires confirmation (L3):** switching *critical* circuits, **generator start
  routine**, HVAC emergency circuit shutoff.
- **Recommend only (L4):** main-breaker operation, utility-side changes, reconfiguring
  transfer logic.
- **Prohibited (L5):** bypassing overcurrent protection, defeating ATS interlocks,
  meter tampering, back-feeding without approved transfer equipment.
- **Professional install:** panel, breakers, ATS, generator, solar, battery, EV circuit.
- **Stays manual:** main breaker, individual breaker handles, generator manual start,
  manual transfer.
- **Failure modes:** smart-panel controller offline (circuits hold last state, manual
  handles work); false load-shed; generator fails to start; battery misreport.
- **Recovery:** circuits fail to *manually operable*; load-shed auto-reverts on cooldown/
  grid restore; generator has manual start + service contract; SoC cross-checked against
  inverter Modbus before any shed decision.

## I. Water control architecture

**Design.** Motorized **main shutoff** (Moen Flo / Phyn, or a motorized ball valve driven
by a Shelly/relay) with **flow + pressure** telemetry; **leak sensors** (Zigbee) at every
wet location; **irrigation** via local controller (OpenSprinkler/Rachio local).

- **Monitor:** flow rate, line pressure, per-sensor leak/flood state, irrigation status,
  pipe-freeze-risk temps.
- **Direct control (L2):** irrigation zones on/off/schedule; exterior spigot plugs.
- **Requires confirmation / auto with rules (L2–L3):** **whole-house shutoff** — auto-
  closes on the leak-**and**-abnormal-flow rule (below), otherwise confirmed.
- **Professional install:** main valve tie-in, pressure sensor, backflow, any re-plumbing.
- **Stays manual:** manual gate/ball valve at the meter and at fixtures.
- **Failure modes:** valve motor stall; false leak; sensor battery dead; pressure sensor
  drift; freeze burst upstream of valve.
- **Recovery:** valve has manual override lever; leak rule requires *two* signals (sensor
  + flow) to auto-close, reducing false trips; low-battery and stale-sensor alerts;
  freeze logic pre-emptively heats vulnerable zones and alerts before failure.

## J. Security and access-control architecture

**Design.** A **UL-listed alarm panel** (DSC/Honeywell via Konnected/Envisalink, or a pro
monitored panel) remains the life-safety/monitored primary. HA integrates it for state and
approved arming. **Locks** are Z-Wave/Matter (Schlage/Yale) with local hubs. Optional
commercial **access control** (keypads/readers) for gates/outbuildings.

- **Monitor:** arm state, zone faults, lock state, door/window contacts, entry logs,
  duress/keypad events, camera-correlated events.
- **Direct control (L2, conditioned):** arm; **auto-lock** exterior doors on Away/Night;
  exterior lighting; camera recording modes; alarm **escalation** workflow.
- **Requires confirmation (L2–L4):** **unlock exterior doors**, **disarm**, opening for a
  visitor. Unlocking for an *unknown* person is **L4 recommend-only** and never automatic.
- **Life-safety egress:** on verified fire/CO, approved interior egress doors may unlock —
  but egress hardware is code-required to allow manual exit *regardless* of AI/power state
  (fail-safe from inside). Perimeter deadbolts default **fail-secure**; only specifically
  designated egress doors are on fail-safe strikes with fire-alarm interlock.
- **Professional install:** alarm panel wiring, electric strikes, egress hardware, gate
  operators.
- **Stays manual:** physical keys, interior thumb-turns, panic hardware, keypad codes.
- **Failure modes:** lock jam/low battery; false alarm; strike stuck; disarm spoofing.
- **Recovery:** mechanical key + interior manual egress always work; monitored panel
  independent of HA; disarm/unlock require confirmed operator identity + MFA; all
  access events immutably logged.

## K. Camera and perimeter-control architecture

**Design.** **PoE cameras** on an isolated **Camera VLAN** with **no internet route**;
local NVR — **Frigate** (with a Coral/edge-TPU for on-prem object/person detection) or
UniFi Protect / Blue Iris. Recording to local NAS with retention policy. Perimeter fusion
of motion (PIR + mmWave), glass-break, vibration, contact, and beam sensors.

- **Monitor:** live streams, motion/object events, line-crossing, tamper, sensor states.
- **Direct control (L2):** recording mode (continuous/event/privacy), floodlight relays,
  event snapshots, siren/announce on the perimeter workflow.
- **Requires confirmation (L2):** exporting/sharing footage off-site.
- **Professional install:** PoE cabling, exterior mounts, floodlight line-voltage.
- **Stays manual:** none required, but physical privacy shutters on indoor cameras.
- **Failure modes:** NVR disk full/offline; camera offline; false person detection; PoE
  switch failure (put NVR + cameras on a UPS-backed PoE switch).
- **Recovery:** cameras cache to SD or fail to independent recording; NVR health alerts +
  RAID/ZFS; detection thresholds tuned; cameras never depend on cloud.

## L. HVAC and environmental-control architecture

**Design.** Smart thermostats with **local** control (ecobee/Honeywell via local API, or
Zooz/Zigbee stats; Z-Wave for fully-local). Multi-zone dampers where present. Environmental
sensor mesh: temp, humidity, CO₂/VOC air-quality, and flood/freeze.

- **Monitor:** zone temps/humidity, runtime, filter status, AQI, freeze-risk points.
- **Direct control (L1):** setpoints **within approved ranges**, fan, schedules, scenes,
  humidifier/dehumidifier plugs, blinds for solar-load management.
- **Requires confirmation (L3):** mode changes outside range; **emergency HVAC shutoff**
  (e.g., halt circulation on smoke/CO).
- **Professional install:** equipment, refrigerant lines, condensate, dampers, wiring.
- **Stays manual:** thermostat face controls.
- **Failure modes:** thermostat offline (equipment keeps last schedule locally); sensor
  drift; damper stuck; over/under-heat.
- **Recovery:** thermostats retain local schedules without hub; range clamps prevent unsafe
  setpoints; freeze logic overrides toward safety; manual thermostat always available.

## M. Network and cybersecurity architecture

**Design.** Per house: a capable firewall/router (**OPNsense/pfSense** or **UniFi UDM-SE**),
managed PoE switches, and WPA3 APs. **VLAN segmentation** with least-privilege inter-VLAN
rules:

| VLAN | Contains | Internet | Can reach hub | Reachable from |
|---|---|---|---|---|
| Trusted | phones, laptops | yes | yes (app/API) | — |
| IoT | plugs, bulbs, sensors, TVs | restricted/none | via MQTT/API only | Trusted (out) |
| Cameras | NVR, PoE cams | **none** | NVR↔hub only | Servers (NVR mgmt) |
| Servers/Mgmt | HA, NAS, NVR, switch/router mgmt | updates only | — | Trusted (MFA) |
| Guest | visitors | yes, isolated | no | — |
| Automation | coordinators, bridges | none | hub only | Servers |

- **Monitor (L0):** device inventory, new-device joins, traffic anomalies, IDS/IPS
  (Suricata) alerts, firewall logs, WAN status, cert/expiry, failed-auth attempts.
- **Direct control (L2):** **quarantine** a newly-joined/anomalous device to an isolated
  VLAN; block a MAC; toggle guest network.
- **Requires confirmation (L3):** firewall **policy** changes, VLAN re-assignment,
  opening any inbound path.
- **Recommend only (L4):** permanent firewall restructuring, exposing services.
- **Prohibited (L5):** disabling the firewall, port-forwarding management interfaces,
  default credentials, undocumented remote access.
- **Remote access:** **WireGuard** only, key-based, to a bastion on Mgmt VLAN; **no**
  port-forwarded admin panels; MFA on all admin logins; per-service least privilege;
  device certs where supported; all admin actions logged.
- **Professional install:** structured cabling, rack, inter-house link.
- **Stays manual:** console/serial access to firewall and switches.
- **Failure modes:** firewall failure (fail-closed for inbound; LAN keeps working);
  rogue device; misconfig lockout.
- **Recovery:** config backups + cold-spare firewall image; out-of-band console; local LAN
  and automations survive WAN loss; quarantine is reversible and logged.

## N. Local server / hub architecture

- **Per house:** dedicated mini-PC (Intel N100/NUC class) running Home Assistant OS as the
  **control core**; a second identical unit or a restorable snapshot as **cold spare**.
- **Broker/coordinators:** Mosquitto MQTT, Zigbee2MQTT (PoE Zigbee coordinator, e.g.
  SLZB-06), Z-Wave JS (800-series stick), Thread/Matter border router, Node-RED.
- **Storage:** NAS (TrueNAS/Synology, ZFS/RAID) for camera footage, backups, and config
  snapshots; nightly encrypted config backups replicated to the *other house* and to
  offline/off-site media.
- **Power protection:** UPS (with NUT monitoring) on router, switch, hub, NVR, and NAS so
  the "nervous system" rides through outages and shuts down cleanly.
- **Time & logging:** local NTP, central syslog, immutable audit store for AI actions.
- **Failure modes / recovery:** hub failure → house runs on last-pushed local automations
  in device firmware/relays where possible, and restores from snapshot to the cold spare in
  minutes; NAS failure → cameras keep local buffers; UPS covers clean shutdown.

## O. AI command interface

- **Surfaces:** central web dashboard (both houses, house-scoped actions), mobile app
  (HA Companion), local **voice** (HA Assist, on-device), intercoms/announcements, and a
  structured command API the AI ops layer calls.
- **Every command is a structured intent:**
  `{house, subsystem, target, action, args, mode_context, operator, confirm_token?}`.
- **Resolution pipeline:** identify house → permission check → mode/precondition check →
  confirmation if required → execute via that house's core → log with rollback token →
  report result and new state.
- **Human override is first-class:** any physical control, and a global "AI hold" switch
  per house that suspends AI actuation while leaving local automations running.

## P. AI permission levels (authoritative)

- **Level 0 — Observe:** read all sensors, logs, cameras, meters, thermostats, locks,
  doors, network, water, power, environment.
- **Level 1 — Routine control (direct):** lights, thermostats (approved range), fans,
  blinds, speakers, non-critical plugs, scenes, notifications, interior automations.
- **Level 2 — Security & utility (conditioned/confirmed):** door locks (approved
  conditions), arm/disarm (approved conditions), garage doors (confirm), exterior lighting,
  smart water shutoff, irrigation, IoT quarantine, camera recording modes, alarm escalation.
- **Level 3 — Power & infrastructure (approved HW + confirm):** smart panel/breakers, load
  shedding, generator start routine, battery backup modes, EV charge limits, HVAC emergency
  shutoff, whole-house water shutoff, firewall policy changes.
- **Level 4 — Recommend only:** main-breaker changes, utility-side changes, permanent
  firewall restructuring, life-safety system changes, disabling alarms, unlocking for
  unknown persons, anything that could trap/injure/endanger/expose occupants.
- **Level 5 — Prohibited:** bypass electrical safety; disable smoke/CO alarms; tamper with
  meters; illegally defeat locks/access control; disable emergency systems; open doors for
  unknown persons without authorization; unsafe electrical/gas/water/fire changes;
  interfere with emergency responders.

**Enforcement:** the level is a property of the *action*, checked server-side by the policy
engine, not something the AI can self-escalate. Confirmation tokens are single-use and
house-scoped. L4/L5 actions have no execution path exposed to the AI at all — only a
"recommend" path that notifies a human.

## Q. Automation policy

- **Local-first:** all critical automations live in the house core (HA/Node-RED) and run
  with no internet and no AI layer. The AI *augments* them; it is not in the safety path.
- **Modes:** Home, Away, Night, Vacation, Guest, **Emergency** — modes gate which AI actions
  are permitted and change fail-safe defaults.
- **Two-signal rule** for destructive/auto actions (e.g., water auto-shutoff needs leak +
  abnormal flow; intrusion needs sensor + camera corroboration where possible).
- **Staged & reversible:** load-shedding and quarantine are staged and auto-revert on
  condition clear; every AI action carries a rollback token where reversible.
- **Rate limits & interlocks** on locks, valves, breakers, and generator start.
- **House scoping** enforced on every rule; cross-house effects require explicit confirm.

## R. Emergency response logic

- **Fire/CO (verified):** turn on egress-path lighting; **shut off HVAC circulation** if
  configured; **unlock only designated egress doors** if safe (and they are mechanically
  operable regardless); announce on intercoms; notify occupants and monitoring; do **not**
  auto-unlock perimeter to outsiders.
- **Grid failure:** verify battery SoC / generator; **shed nonessential loads**; keep
  security, network, hubs, medical, and fridge/freezer powered; alert; hold until restore.
- **Water leak (two-signal):** close smart main valve; kill affected circuits if wet-area
  electrical risk; notify **urgent**.
- **Freeze risk:** raise heat in vulnerable zones; open cabinet-relief automations; notify.
- **Intrusion (suspicious activity):** exterior floods on; cameras record; lock exterior
  doors; announce; snapshot alert; escalate on corroboration.
- **Network compromise:** quarantine offending device; snapshot logs; notify; recommend
  (not auto) broader firewall changes.
- **Every emergency:** logged, human-notified, and reversible where safe; life-safety
  hardware remains independent of the AI.

## S. Transfer plan from House A to House B

The design is **template-driven** so House B is a parameterized copy, not a rebuild.

1. **Model as data:** each house is fully described by a `houses.<id>` config block
   (rooms, zones, devices, sensors, cameras, locks, power, water, HVAC, network segments,
   automations, permissions, emergency rules, backup profile). See `config/houses.example.yaml`.
2. **Reusable automation packages:** critical logic (leak, freeze, intrusion, grid-loss,
   fire) is written as **parameterized blueprints** referencing roles (`role: main_water_valve`)
   not device IDs.
3. **Bootstrapping House B:** stand up identical core hardware → restore the automation
   package set → import the House B config block → run device discovery and **map roles to
   real devices** → validate with the testing plan → enable AI at Level 0, then raise levels
   as each subsystem passes validation.
4. **Namespacing:** all identities become `house_b.*`; networks use House B's supernet/VLANs;
   the central dashboard federates the new house read-only first, then enables scoped control.
5. **Divergence handling:** house-specific differences (panel model, valve type) are captured
   in the config block; the shared blueprints stay identical, easing future houses.

## T. Installation phases

- **Phase 0 — Network & core:** firewall, VLANs, switches, APs, HA core, MQTT, coordinators,
  UPS, NAS, remote access (WireGuard). Validate segmentation.
- **Phase 1 — Observe everywhere (L0):** deploy sensors (contact/motion/leak/temp/AQI),
  cameras + NVR, energy monitoring, thermostats, network monitoring. AI read-only.
- **Phase 2 — Routine control (L1):** lighting, climate-in-range, plugs, scenes,
  notifications, blinds. Enable local automations.
- **Phase 3 — Security & utility (L2):** locks, alarm integration, garage/gate, exterior
  lighting, water shutoff + irrigation, IoT quarantine, camera modes.
- **Phase 4 — Power & infrastructure (L3):** smart panel/breakers, load-shedding, battery/
  solar/generator/ATS integration, EV load management, HVAC emergency shutoff, firewall
  policy control. All professionally installed and inspected.
- **Phase 5 — AI ops layer & central command:** enable reasoning, cross-house dashboard,
  house-scoped command routing, audit + rollback.
- **Phase 6 — Second house / expansion:** replicate via the transfer plan.

## U. Testing and validation plan

- **Per device:** state feedback accuracy, manual override works, loss-of-power behavior.
- **Segmentation:** verify IoT/Camera VLANs cannot reach the internet or Trusted; verify
  quarantine works and reverts; run a rogue-device drill.
- **Local-first drills:** pull WAN — confirm locks, lights, HVAC, water shutoff, alarms,
  and automations still function. Pull the AI layer — confirm the house still runs.
- **Fail-safe drills:** simulate leak (two-signal), freeze, grid-loss (transfer + shed),
  fire/CO (egress unlock + HVAC + notify), intrusion. Confirm reversibility and logging.
- **Permission tests:** attempt an L4/L5 action via the AI path — must be refused and
  logged. Cross-house command — must demand confirmation.
- **Recovery:** restore core from snapshot to cold spare within target RTO; NVR/NAS
  degraded-mode; firewall cold-spare swap.

## V. Maintenance and upgrade plan

- **Backups:** nightly encrypted config snapshots, cross-house + off-site; test restores
  quarterly.
- **Updates:** staged (test on House A non-critical first), maintenance windows, changelog
  review; never auto-update firewall/panel firmware without a rollback plan.
- **Battery/health:** monitor lock/sensor batteries, UPS batteries, NVR disks, generator
  fuel/oil and monthly exercise (professional service contract).
- **Security hygiene:** rotate keys/certs, review firewall/audit logs, patch cadence,
  annual penetration self-test of the segmentation.
- **Expansion:** add devices as roles in the config block; blueprints pick them up.

## W. Risk controls

- **Human override on every controlled system** (switch/valve/key/breaker/thermostat).
- **Life-safety independence:** smoke/CO/fire and monitored alarm remain primary and are
  never gated behind the AI.
- **Least privilege & no self-escalation:** L4/L5 have no AI execution path.
- **Two-signal + staged + reversible** destructive actions; rate limits/interlocks.
- **Segmentation + WireGuard-only remote + MFA + audit logging**, no default creds, no
  exposed management.
- **House scoping** prevents wrong-house actuation; cross-house needs confirmation.
- **Fail-safe defaults** chosen per subsystem (egress fail-safe from inside; perimeter
  fail-secure; loads fail to manual; WAN inbound fail-closed).

## X. Example command set

```
arm house_a night
set house_a.living_room lights 30%
set house_b.thermostat.upstairs 68F            # in-range → direct
shutoff house_a water main                      # two-signal auto OR confirmed
close house_b.garage.main                        # direct
open  house_b.garage.main                        # requires confirmation
quarantine house_a device 3c:6a:9d:..            # → IoT/guest VLAN
record house_a cameras all event-mode
shed house_a loads tier2                          # staged load-shedding (approved panel)
limit house_a evcharger 16A
start house_b generator                           # L3, confirm + approved ATS
recommend house_a firewall restructure            # L4 → notify human only
unlock house_a.front_door                         # confirm + operator MFA; never for unknown
```

Cross-house example: `set house_b … ` while `active_house=house_a` →
*"This command targets **House B**. Confirm House B? (y/N)"*

## Y. Example automations (local-first blueprints)

```yaml
# 1. Water leak — two-signal shutoff
trigger: leak_sensor.any == wet AND flow_meter.rate > abnormal_threshold
action: [ close(role.main_water_valve), notify(urgent, "Leak: main water shut off"),
          log(event, severity=urgent) ]

# 2. Unknown device joins network
trigger: network.new_device
action: [ quarantine(device -> vlan.iot_guest), notify("New device quarantined: {mac}") ]

# 3. Exterior motion at night
trigger: exterior_motion AND mode == night
action: [ on(role.floodlights, zone), camera.record(zone, event), notify_snapshot() ]

# 4. High power draw
trigger: whole_home.power > shed_threshold
action: [ limit(role.evcharger, low), delay(deferrable_appliances),
          recommend("load shed tier2") ]

# 5. Grid failure
trigger: grid.status == down
action: [ verify(battery.soc, generator.ready), shed(nonessential),
          keep_powered(security, network, hub, fridge), notify("On backup power") ]

# 6. Cross-house guard
trigger: command.target_house != command_context.active_house
action: require_explicit_confirmation(target_house)

# 7. Internet failure
trigger: wan.status == down
action: maintain_local(locks, lights, hvac, water_shutoff, alarms)   # no cloud dependency

# 8. Fire / CO (verified)
trigger: smoke_or_co.verified
action: [ on(egress_path_lights), unlock(role.designated_egress_doors) if safe,
          hvac.stop_circulation() if configured, announce(intercoms), notify(occupants) ]

# 9. Pipe-freeze risk
trigger: zone.temp near freeze_risk
action: [ raise_heat(vulnerable_zones), notify("Freeze risk: heating vulnerable zones") ]

# 10. Suspicious activity near either house
trigger: perimeter.suspicious(house)
action: [ on(role.exterior_lighting, house), camera.record(house),
          lock(role.exterior_doors, house), notify(house, snapshot) ]
```

## Z. Bill of materials — categories

- **Network:** firewall/router, managed PoE switches, WPA3 APs, inter-house link
  (fiber/PtP), UPS units, rack/enclosure, cabling.
- **Compute/storage:** 2× control-core mini-PCs per house (primary+spare), NAS + disks.
- **Radios/bridges:** Zigbee coordinator (PoE), Z-Wave 800 stick, Thread/Matter border
  router, MQTT (software).
- **Sensors:** contact, motion (PIR + mmWave), glass-break, vibration, beam/perimeter,
  leak/flood, temp/humidity, air-quality (CO₂/VOC), freeze.
- **Life-safety:** interconnected hardwired smoke/CO (code primary) + monitored alarm panel.
- **Cameras:** PoE cameras, NVR + edge-TPU (Frigate/Coral) or Protect/Blue Iris.
- **Access:** smart deadbolts, electric strikes/egress hardware (pro), keypads/readers,
  garage openers (local bridge, e.g., ratgdo), gate operator.
- **Water:** motorized main valve + flow/pressure sensor, leak sensors, irrigation controller.
- **Electrical/power:** smart panel or smart breakers, energy meter/CTs, relays/contactors,
  battery, solar inverter/PV, generator + ATS, EV charger w/ load management.
- **HVAC:** locally-controllable thermostats, zone dampers, humidifier/dehumidifier control.
- **Interface/alerts:** speakers/intercoms, in-wall tablets, siren/strobe.

## AA. Final deployment checklist

- [ ] VLANs verified: IoT/Camera cannot reach internet or Trusted; Guest isolated.
- [ ] WireGuard-only remote access; no forwarded admin ports; MFA on all admin logins.
- [ ] No default credentials anywhere; device certs where supported; audit logging on.
- [ ] Each control core has a tested cold-spare restore within target RTO.
- [ ] UPS on router/switch/hub/NVR/NAS; NUT clean-shutdown tested.
- [ ] Every controlled device has a verified manual override.
- [ ] Life-safety smoke/CO/alarm independent of AI; egress doors manually operable.
- [ ] Two-signal + reversible confirmed for water shutoff, load-shed, quarantine.
- [ ] Electrical/generator/solar/battery/ATS professionally installed **and inspected**.
- [ ] Permission engine refuses L4/L5 via AI path (tested) and logs the attempt.
- [ ] Cross-house command demands explicit house confirmation (tested).
- [ ] Local-first drill passed: WAN down and AI-layer down both leave the house operable.
- [ ] Emergency drills passed: leak, freeze, grid-loss, fire/CO, intrusion — all logged.
- [ ] Backups encrypted, cross-house + off-site, restore tested.
- [ ] House A → House B transfer validated via config block + blueprints.
