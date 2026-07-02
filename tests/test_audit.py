"""Part 1 — tamper-evident + persistent audit log."""
import json
import pytest
from homeops.audit import AuditLog, AuditRecord, GENESIS


def rec(action="turn_on", status="executed"):
    return AuditRecord(0, "owner", "house_a", "light", "living_room", action, {}, 1, status, "msg")


def test_chain_verifies_and_advances_head():
    log = AuditLog()
    for i in range(5):
        log.record(rec(action=f"a{i}"))
    ok, bad = log.verify_chain()
    assert ok and bad == -1
    assert log.head != GENESIS


def test_in_memory_tamper_is_detected():
    log = AuditLog()
    for i in range(3):
        log.record(rec(action=f"a{i}"))
    # an attacker edits history (frozen dataclass -> force it)
    object.__setattr__(log._records[1], "status", "prohibited")
    ok, bad = log.verify_chain()
    assert not ok and bad == 1


def test_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    log = AuditLog(p)
    for i in range(4):
        log.record(rec(action=f"a{i}"))
    head = log.head
    reloaded = AuditLog(p)          # loads and re-verifies on construction
    assert len(reloaded.records) == 4 and reloaded.head == head
    assert reloaded.verify_chain()[0]


def test_persisted_tamper_is_detected_on_load(tmp_path):
    p = str(tmp_path / "audit.jsonl")
    log = AuditLog(p)
    for i in range(3):
        log.record(rec(action=f"a{i}"))
    # edit a record on disk but leave its stored hash -> integrity check must fail on reload
    lines = open(p).read().splitlines()
    entry = json.loads(lines[1])
    entry["record"]["status"] = "prohibited"
    lines[1] = json.dumps(entry)
    open(p, "w").write("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        AuditLog(p)


def test_world_audit_is_verifiable(world):
    from homeops.simulator import scenarios
    scenarios.leak(world, "house_a")
    world.tick(2)
    ok, _ = world.audit.verify_chain()
    assert ok and len(world.audit.records) > 0
