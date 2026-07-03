"""Read-only commissioning preflight — the maximal bridge to real hardware without actuation.

Runs GET-only probes against a live Home Assistant (and optionally OPNsense) and cross-checks
them against the deployment: API reachable, token accepted, every mapped entity actually exists,
every mapped entity's HA domain is plausible for its homeops subsystem, safety-verify read-back
coverage. It NEVER calls a service, never changes state, never sends a POST. The output is a
commissioning report an installer signs off before the first supervised actuation test.

The remaining gap after a clean preflight is irreducible in software: verified actuation on the
physical device, which requires a human at the device. This module shrinks the unknown to that.
"""
from __future__ import annotations
from dataclasses import dataclass

from .adapters.http import HttpClient, Transport
from .deployment import DeploymentConfig, validate_deployment
from .model import load_houses

# subsystem -> HA domains that can plausibly implement it (from adapters.homeassistant.map_intent)
PLAUSIBLE_DOMAINS: dict[str, set[str]] = {
    "light": {"light", "switch"},
    "plug": {"switch"},
    "lock": {"lock"},
    "cover": {"cover"},
    "garage": {"cover", "switch"},
    "climate": {"climate"},
    "hvac": {"climate", "switch"},
    "water": {"valve", "switch"},
    "power": {"switch", "scene"},
    "evcharger": {"number"},
    "battery": {"select"},
    "generator": {"button", "switch", "script"},
    "alarm": {"alarm_control_panel", "script"},
    "camera": {"camera", "select", "switch", "script"},
    "scene": {"scene", "script"},
    "speaker": {"media_player", "notify"},
}


@dataclass
class Check:
    severity: str   # ok | warn | fail
    name: str
    detail: str


def _subsystem_of(local_entity_id: str, houses) -> str | None:
    for h in houses.values():
        e = h.entities.get(local_entity_id)
        if e is not None:
            return e.subsystem
    return None


def run_preflight(dep: DeploymentConfig, secrets: dict[str, str],
                  transport: Transport | None = None,
                  opn_transport: Transport | None = None) -> list[Check]:
    """All checks are GETs. `transport`/`opn_transport` are injectable for offline tests,
    exactly like the adapters themselves."""
    checks: list[Check] = []

    def ok(n, d):   checks.append(Check("ok", n, d))
    def warn(n, d): checks.append(Check("warn", n, d))
    def fail(n, d): checks.append(Check("fail", n, d))

    # 0. static validation first — network probes are pointless on a broken descriptor
    static = validate_deployment(dep, dash_token_present=bool(secrets.get("HOMEOPS_DASH_TOKEN")))
    checks.extend(Check(s, f"static.{c}", d) for s, c, d in static)
    if any(c.severity == "fail" for c in checks):
        fail("preflight", "static validation failed — skipping live probes")
        return checks
    if dep.mode != "real":
        warn("preflight", "mode=sim — nothing live to probe")
        return checks

    ha_url, ha_token = secrets.get("HOMEOPS_HA_URL", ""), secrets.get("HOMEOPS_HA_TOKEN", "")
    http = HttpClient(ha_url, default_headers={"Authorization": f"Bearer {ha_token}"},
                      transport=transport, verify_tls=dep.verify_tls)

    # 1. HA reachable + token accepted
    try:
        status, obj = http.request("GET", "/api/config")
    except Exception as e:                                   # noqa: BLE001 — connection-level failure
        fail("ha.reachable", f"{ha_url}: {e}")
        return checks
    if status == 401:
        fail("ha.auth", "token rejected (401)")
        return checks
    if status != 200 or not isinstance(obj, dict):
        fail("ha.reachable", f"GET /api/config -> HTTP {status}")
        return checks
    ok("ha.reachable", f"Home Assistant {obj.get('version', '?')} at {ha_url}")
    if obj.get("state") not in (None, "RUNNING"):
        warn("ha.state", f"core state is {obj.get('state')!r}")

    # 2. entity inventory
    status, states = http.request("GET", "/api/states")
    if status != 200 or not isinstance(states, list):
        fail("ha.states", f"GET /api/states -> HTTP {status}")
        return checks
    real_entities = {s.get("entity_id"): s for s in states if isinstance(s, dict)}
    ok("ha.states", f"{len(real_entities)} entities visible to this token")

    houses = load_houses(dep.houses_config)
    missing, mismatched, unavailable = [], [], []
    for local, real in dep.entity_map.items():
        st = real_entities.get(real)
        if st is None:
            missing.append(f"{local} -> {real}")
            continue
        if st.get("state") in ("unavailable", "unknown"):
            unavailable.append(real)
        sub = _subsystem_of(local, houses)
        dom = real.split(".", 1)[0]
        plaus = PLAUSIBLE_DOMAINS.get(sub or "", None)
        if plaus is not None and dom not in plaus:
            mismatched.append(f"{local} ({sub}) -> {real} (domain {dom})")
    if missing:
        fail("map.exists", f"{len(missing)} mapped entities absent from HA, e.g. {missing[:3]}")
    else:
        ok("map.exists", f"all {len(dep.entity_map)} mapped entities exist in HA")
    if mismatched:
        fail("map.domains", f"{len(mismatched)} implausible domain mappings, e.g. {mismatched[:3]}")
    else:
        ok("map.domains", "every mapping's HA domain is plausible for its subsystem")
    if unavailable:
        warn("map.available", f"{len(unavailable)} mapped entities currently unavailable/unknown: "
             f"{unavailable[:4]} — health gate would refuse safety actuation on these now")
    else:
        ok("map.available", "no mapped entity is unavailable right now")

    # 3. OPNsense (optional, GET only)
    if dep.opnsense:
        import base64
        auth = base64.b64encode(
            f"{secrets.get('HOMEOPS_OPN_KEY','')}:{secrets.get('HOMEOPS_OPN_SECRET','')}".encode()).decode()
        opn = HttpClient(secrets.get("HOMEOPS_OPN_URL", ""),
                         default_headers={"Authorization": f"Basic {auth}"},
                         transport=opn_transport or transport, verify_tls=dep.verify_tls)
        try:
            st, _ = opn.request("GET", "/api/core/firmware/status")
        except Exception as e:                               # noqa: BLE001
            st, _ = -1, None
            fail("opnsense.reachable", f"{secrets.get('HOMEOPS_OPN_URL','')}: {e}")
        if st == 200:
            ok("opnsense.reachable", "API reachable, key accepted")
        elif st == 401:
            fail("opnsense.auth", "API key/secret rejected (401)")
        elif st != -1:
            fail("opnsense.reachable", f"GET firmware/status -> HTTP {st}")

    n_fail = sum(1 for c in checks if c.severity == "fail")
    (ok if n_fail == 0 else fail)("preflight", f"{'CLEAR' if n_fail == 0 else 'BLOCKED'} — "
                                  f"{n_fail} failing checks; actuation trials remain a supervised, "
                                  f"human-at-the-device step")
    return checks


def render_report(checks: list[Check]) -> str:
    lines = ["== homeops preflight (read-only; no actuation) =="]
    lines += [f"[{c.severity.upper()}] {c.name}: {c.detail}" for c in checks]
    return "\n".join(lines)


def failed(checks: list[Check]) -> bool:
    return any(c.severity == "fail" for c in checks)
