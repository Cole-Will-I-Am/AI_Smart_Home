"""Deterministic composite inference tier below the AI.

The baseline tier reports single-signal anomalies. This tier fuses multiple
independent signals into higher-order typed `inference` events for L0 awareness.
It is deliberately advisory-only: it publishes events and never calls the router.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .automations import ABNORMAL_FLOW
from .events import Event


@dataclass(frozen=True)
class InferenceRule:
    name: str
    signals: tuple[dict[str, Any], ...]
    message: str
    entity: str | None = None
    severity: str = "advisory"
    window_events: int = 80


DEFAULT_RULES: tuple[InferenceRule, ...] = (
    InferenceRule(
        name="leak_suspected",
        entity="sensor.flow_meter",
        message="Water leak suspected: pressure drop anomaly plus abnormal flow",
        signals=(
            {"kind": "anomaly", "entity": "sensor.pressure", "direction": "falling"},
            {"kind": "state_threshold", "entity": "sensor.flow_meter", "op": ">=", "value": ABNORMAL_FLOW},
        ),
    ),
    InferenceRule(
        name="ventilation_fault",
        entity="sensor.co2",
        message="Ventilation fault suspected: CO2 anomaly while occupancy is zero",
        signals=(
            {"kind": "anomaly", "entity": "sensor.co2", "direction": "rising"},
            {"kind": "state_threshold", "entity": "sensor.occupancy", "op": "==", "value": 0},
        ),
    ),
    InferenceRule(
        name="device_fault",
        message="Device fault suspected: phantom power draw while the house is unoccupied",
        signals=(
            {"kind": "anomaly", "metric": "watts", "direction": "rising"},
            {"kind": "house_unoccupied"},
        ),
    ),
)


def _entity_id_for(house_id: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = str(raw)
    if raw.startswith("house_"):
        return raw
    if raw.count(".") == 1:
        return f"{house_id}.{raw}"
    return raw


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cmp(actual: float, op: str, expected: float) -> bool:
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    return False


class InferenceRegistry:
    """Bus-attached rule engine: advisory composite events out, no actuation."""

    def __init__(self, rules: tuple[InferenceRule, ...] | None = None) -> None:
        self.rules = tuple(rules or DEFAULT_RULES)
        self.world = None
        self._published_keys: set[tuple[int, str, str, str | None]] = set()

    def attach(self, world) -> "InferenceRegistry":
        self.world = world
        world.bus.subscribe(self._on_event)
        return self

    def _on_event(self, ev: Event) -> None:
        if self.world is None or ev.type == "inference":
            return
        for rule in self.rules:
            evidence = self._evaluate(rule, ev.house_id)
            if evidence is None:
                continue
            entity_id = _entity_id_for(ev.house_id, rule.entity) or ev.entity_id
            key = (ev.tick, ev.house_id, rule.name, entity_id)
            if key in self._published_keys:
                continue
            self._published_keys.add(key)
            self.world.bus.publish(Event(
                "inference",
                ev.house_id,
                entity_id,
                {
                    "inference_type": rule.name,
                    "advisory": True,
                    "severity": rule.severity,
                    "message": rule.message,
                    "signals": evidence,
                },
                ev.tick,
            ))

    def _evaluate(self, rule: InferenceRule, house_id: str) -> list[dict] | None:
        evidence = []
        for signal in rule.signals:
            ev = self._match_signal(house_id, signal, rule.window_events)
            if ev is None:
                return None
            evidence.append(ev)
        return evidence

    def _match_signal(self, house_id: str, signal: dict[str, Any], window_events: int) -> dict | None:
        kind = signal.get("kind")
        if kind == "anomaly":
            return self._match_anomaly(house_id, signal, window_events)
        if kind == "state_threshold":
            return self._match_state_threshold(house_id, signal)
        if kind == "state_equals":
            return self._match_state_equals(house_id, signal)
        if kind == "house_unoccupied":
            return self._match_unoccupied(house_id)
        return None

    def _match_anomaly(self, house_id: str, signal: dict[str, Any], window_events: int) -> dict | None:
        assert self.world is not None
        entity = _entity_id_for(house_id, signal.get("entity"))
        metric = signal.get("metric")
        direction = signal.get("direction")
        for ev in reversed(self.world.bus.recent(window_events, house_id=house_id)):
            if ev.type != "anomaly":
                continue
            if entity is not None and ev.entity_id != entity:
                continue
            if metric is not None and ev.data.get("metric") != metric:
                continue
            value = _as_float(ev.data.get("value"))
            expected = _as_float(ev.data.get("expected"))
            if value is None or expected is None:
                continue
            if direction == "rising" and value <= expected:
                continue
            if direction == "falling" and value >= expected:
                continue
            return {
                "kind": "anomaly",
                "entity_id": ev.entity_id,
                "value": value,
                "expected": expected,
                "metric": ev.data.get("metric"),
                "direction": "rising" if value > expected else "falling",
            }
        return None

    def _match_state_threshold(self, house_id: str, signal: dict[str, Any]) -> dict | None:
        assert self.world is not None
        entity_id = _entity_id_for(house_id, signal.get("entity"))
        actual = _as_float(self.world.state.get_state(entity_id)) if entity_id else None
        expected = _as_float(signal.get("value"))
        op = str(signal.get("op", "=="))
        if actual is None or expected is None or not _cmp(actual, op, expected):
            return None
        return {"kind": "state_threshold", "entity_id": entity_id, "op": op, "value": actual, "expected": expected}

    def _match_state_equals(self, house_id: str, signal: dict[str, Any]) -> dict | None:
        assert self.world is not None
        entity_id = _entity_id_for(house_id, signal.get("entity"))
        actual = self.world.state.get_state(entity_id) if entity_id else None
        expected = signal.get("value")
        if actual != expected and str(actual) != str(expected):
            return None
        return {"kind": "state_equals", "entity_id": entity_id, "value": actual}

    def _match_unoccupied(self, house_id: str) -> dict | None:
        assert self.world is not None
        occ_id = f"{house_id}.sensor.occupancy"
        occ = _as_float(self.world.state.get_state(occ_id))
        if occ == 0:
            return {"kind": "house_unoccupied", "entity_id": occ_id, "value": 0}
        house = self.world.houses.get(house_id)
        if house is not None and house.mode in {"away", "vacation"}:
            return {"kind": "house_unoccupied", "mode": house.mode}
        return None
