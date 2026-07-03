"""Part 9: preflight is read-only commissioning — it proves the map matches the house
and NEVER actuates. Fake transports, exactly like the real-adapter tests."""
import json

from homeops.bootstrap import DEFAULT_CONFIG, controllable_entities
from homeops.deployment import DeploymentConfig
from homeops.model import load_houses
from homeops.preflight import failed, render_report, run_preflight

SECRETS = {"HOMEOPS_HA_URL": "https://ha:8123", "HOMEOPS_HA_TOKEN": "tok",
           "HOMEOPS_DASH_TOKEN": "dash"}

# subsystem -> a plausible HA domain for fabricating a coherent fake inventory
_DOM = {"light": "light", "plug": "switch", "lock": "lock", "cover": "cover", "garage": "cover",
        "climate": "climate", "hvac": "climate", "water": "valve", "power": "switch",
        "evcharger": "number", "battery": "select", "generator": "button",
        "alarm": "alarm_control_panel", "camera": "camera", "scene": "scene",
        "speaker": "media_player"}


def _dep_and_states(break_missing=False, break_domain=False, unavailable=False):
    houses = load_houses(DEFAULT_CONFIG)
    emap, states = {}, []
    for eid in controllable_entities(houses):
        sub = next(h.entities[eid].subsystem for h in houses.values() if eid in h.entities)
        real = f"{_DOM.get(sub, 'switch')}.{eid.replace('.', '_')}"
        emap[eid] = real
        states.append({"entity_id": real, "state": "on"})
    if break_missing:
        states.pop()                                  # one mapped entity absent from HA
    if break_domain:
        k = next(iter(emap))
        emap[k] = "vacuum." + emap[k].split(".", 1)[1]  # implausible domain for its subsystem
        states.append({"entity_id": emap[k], "state": "docked"})
    if unavailable:
        states[0]["state"] = "unavailable"
    dep = DeploymentConfig(mode="real", audit_path="/tmp/a.jsonl", entity_map=emap)
    return dep, states


def _transport(states, record):
    def t(method, url, headers, body):
        record.append((method, url))
        assert method == "GET", f"preflight issued a non-GET: {method} {url}"   # the invariant
        if url.endswith("/api/config"):
            return 200, json.dumps({"version": "2026.6", "state": "RUNNING"})
        if url.endswith("/api/states"):
            return 200, json.dumps(states)
        return 404, "{}"
    return t


def test_clean_preflight_passes_and_is_get_only():
    dep, states = _dep_and_states()
    calls = []
    checks = run_preflight(dep, SECRETS, transport=_transport(states, calls))
    assert not failed(checks), render_report(checks)
    assert calls and all(m == "GET" for m, _ in calls)


def test_missing_real_entity_fails():
    dep, states = _dep_and_states(break_missing=True)
    checks = run_preflight(dep, SECRETS, transport=_transport(states, []))
    assert failed(checks)
    assert any(c.name == "map.exists" and c.severity == "fail" for c in checks)


def test_implausible_domain_fails():
    dep, states = _dep_and_states(break_domain=True)
    checks = run_preflight(dep, SECRETS, transport=_transport(states, []))
    assert any(c.name == "map.domains" and c.severity == "fail" for c in checks)


def test_unavailable_device_warns_but_does_not_block():
    dep, states = _dep_and_states(unavailable=True)
    checks = run_preflight(dep, SECRETS, transport=_transport(states, []))
    assert any(c.name == "map.available" and c.severity == "warn" for c in checks)
    assert not failed(checks)


def test_rejected_token_fails_early():
    dep, states = _dep_and_states()
    def t(method, url, headers, body):
        return 401, "{}"
    checks = run_preflight(dep, SECRETS, transport=t)
    assert any(c.name == "ha.auth" and c.severity == "fail" for c in checks)


def test_static_failure_skips_live_probes():
    dep = DeploymentConfig(mode="real", audit_path=None, entity_map={})   # statically broken
    calls = []
    checks = run_preflight(dep, SECRETS, transport=_transport([], calls))
    assert failed(checks) and calls == []             # no network touched


def test_sim_mode_probes_nothing():
    calls = []
    checks = run_preflight(DeploymentConfig(), SECRETS, transport=_transport([], calls))
    assert calls == [] and not failed(checks)
