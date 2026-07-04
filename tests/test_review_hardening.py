"""Regression witnesses for the end-to-end review patches.

Each test fails against the pre-patch code and passes after. Findings map:
  H1 real-mode clock advances · H2 hostile tool input refused · H3 HA path injection blocked ·
  H4 confirmation token survives a downstream refusal · H5 torn audit tail tolerated ·
  M1 provider error can't poison the transcript · M3 event history is bounded ·
  M4 incremental audit verification agrees with the whole-chain check.
"""
import tempfile

import pytest

from homeops import build_world
from homeops.adapters.homeassistant import HomeAssistantAdapter
from homeops.ai.ops_layer import OpsLayer
from homeops.ai.providers import Completion, Provider
from homeops.ai.session import ChatSession
from homeops.audit import AuditLog, AuditRecord
from homeops.deployment import DeploymentConfig
from homeops.events import EventBus, Event
from homeops.permissions import Intent, Operator
from homeops.service import Service


# --- H1: real-mode housekeeping advances the engine clock ------------------------------
def test_real_mode_housekeeping_advances_tick():
    dep = DeploymentConfig(mode="sim")           # a sim deployment, but we drive the real branch
    svc = Service(dep, secrets={}, world=build_world())
    svc.dep.mode = "real"                         # force the real housekeeping path
    start = svc.world.engine.tick
    svc._housekeep()
    svc._housekeep()
    assert svc.world.engine.tick == start + 2, "engine clock must advance in real mode too (H1)"


def test_frozen_clock_would_trap_cooldown_but_advancing_clock_clears_it(bare):
    # With a live clock, a one-shot cooldown eventually clears. (Pre-H1 the clock never moved,
    # so once fired the generator could never restart for the process lifetime.)
    op = Operator("system", "house_a", "auto")
    r1 = bare.router.execute(Intent("house_a", "generator", "main", "start", emergency=True), op)
    assert r1.status == "executed"
    r2 = bare.router.execute(Intent("house_a", "generator", "main", "start", emergency=True), op)
    assert r2.status == "refused" and "cooldown" in r2.message
    bare.engine.tick += 3                          # the real-mode clock would have advanced by now
    r3 = bare.router.execute(Intent("house_a", "generator", "main", "start", emergency=True), op)
    assert r3.status == "executed", "cooldown must clear once the clock advances (H1/L3)"


# --- H2: hostile / malformed model tool input is refused, never raised ------------------
def test_propose_command_missing_field_is_refused(world):
    ops = OpsLayer(world)
    out = ops._run_tool("propose_command", {"house_id": "house_a", "target": "x", "action": "turn_on"}, "house_a")
    assert out["status"] == "refused" and "subsystem" in out["message"]


def test_propose_command_string_args_is_refused(world):
    ops = OpsLayer(world)
    out = ops._run_tool("propose_command",
                        {"house_id": "house_a", "subsystem": "light", "target": "living_room",
                         "action": "turn_on", "args": "brightness=50"}, "house_a")
    assert out["status"] == "refused" and "args" in out["message"]


# --- H3: attacker-controlled values can't escape into the HA request path ---------------
def _recording_adapter():
    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        return 200, '{"state": "armed_away"}'
    return HomeAssistantAdapter("http://ha.local:8123", "tok", transport=transport), calls


def test_alarm_arm_mode_is_whitelisted():
    ad, calls = _recording_adapter()
    res = ad.apply(Intent("house_a", "alarm", "panel", "arm", {"mode": "away/../../../states"}))
    assert res["ok"] is False
    assert not any(".." in u for u in calls), "traversal must never reach a request URL (H3)"


def test_entity_target_traversal_is_rejected():
    ad, calls = _recording_adapter()
    res = ad.apply(Intent("house_a", "light", "x/../../config", "turn_on"))
    assert res["ok"] is False
    assert not any(".." in u for u in calls)


def test_legitimate_alarm_arm_still_works():
    ad, calls = _recording_adapter()
    res = ad.apply(Intent("house_a", "alarm", "panel", "arm", {"mode": "away"}))
    assert res["ok"] is True
    assert any("alarm_arm_away" in u for u in calls)


# --- H4: a confirmation token is not burned by a later refusal --------------------------
def test_token_survives_a_rate_limited_confirm(bare):
    op = Operator("owner", "house_a", "resident")
    for _ in range(5):                             # exhaust the per-tick rate budget for (house_a, lock)
        bare.router.execute(Intent("house_a", "lock", "front_door", "lock"), op)
    i = Intent("house_a", "lock", "front_door", "unlock")
    r1 = bare.router.execute(i, op)
    assert r1.status == "confirm_required" and r1.confirm_token
    i.confirm_token = r1.confirm_token
    r2 = bare.router.execute(i, op)                # refused by the rate gate — must NOT spend the token
    assert r2.status == "refused" and "rate" in r2.message
    bare.engine.tick += 1                          # next tick: rate budget resets
    r3 = bare.router.execute(i, op)                # SAME token must still authorize
    assert r3.status == "executed", "a token must not be consumed by a downstream refusal (H4)"


# --- H5: a crash-torn final audit line is dropped, not fatal ----------------------------
def test_torn_audit_tail_is_tolerated():
    path = tempfile.mktemp(suffix=".jsonl")
    log = AuditLog(path)
    for i in range(3):
        log.record(AuditRecord(i, "owner", "house_a", "light", "x", "turn_on", {}, 1, "executed", "ok"))
    data = open(path).read()
    open(path, "w").write(data[:-20])              # truncate the final line, as a mid-write crash would
    reloaded = AuditLog(path)                       # must NOT raise
    assert len(reloaded.records) == 2
    assert reloaded._torn_tail is not None
    assert reloaded.verify_chain()[0]


def test_corruption_before_the_tail_still_raises():
    path = tempfile.mktemp(suffix=".jsonl")
    log = AuditLog(path)
    for i in range(3):
        log.record(AuditRecord(i, "owner", "house_a", "light", "x", "turn_on", {}, 1, "executed", "ok"))
    lines = open(path).read().splitlines()
    lines[0] = "{ this is not json"                # corruption in the MIDDLE is not a torn write
    open(path, "w").write("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        AuditLog(path)


# --- M1: a provider exception cannot leave two consecutive user turns -------------------
class _FlakyProvider(Provider):
    name, default_model = "flaky", "m"

    def __init__(self):
        self.n = 0

    def complete(self, **kw):
        self.n += 1
        if self.n == 1:
            raise ConnectionError("network blip")
        return Completion(text="ok")


def test_provider_error_degrades_without_poisoning_transcript():
    w = build_world()
    s = ChatSession(w, client=_FlakyProvider(), model="m")
    out = s.ask("turn on the lights")             # provider raises on the first call
    assert out.get("degraded")                     # degraded to the deterministic fallback
    s.ask("try again")                             # second turn must be accepted
    roles = [m["role"] for m in s.messages]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:])), \
        "a failed turn must not leave an orphaned user message (M1)"


# --- M3: event history is bounded -------------------------------------------------------
def test_event_history_is_bounded():
    bus = EventBus(history_limit=100)
    for i in range(1000):
        bus.publish(Event("motion", "house_a", tick=i))
    assert len(bus.history) == 100
    assert bus.recent(5)[-1].tick == 999          # newest events are retained


# --- M4: incremental verification agrees with the whole-chain check ---------------------
def test_incremental_verification_matches_whole_chain():
    log = AuditLog()
    for i in range(50):
        log.record(AuditRecord(i, "o", "house_a", "light", "x", "turn_on", {}, 1, "executed", "ok"))
    assert log.verify_incremental() is True
    for i in range(50, 60):                         # append more, then re-check only the new tail
        log.record(AuditRecord(i, "o", "house_a", "light", "x", "turn_on", {}, 1, "executed", "ok"))
    assert log.verify_incremental() is True
    assert log.verify_chain()[0] is True
