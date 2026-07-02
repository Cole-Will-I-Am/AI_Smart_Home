"""Home-Assistant-semantics simulator: applies device actions to the StateStore.

Models the physical dynamics that matter for the tests: the water main valve takes a couple
of ticks to close (and can *stall*), and the generator takes a couple of ticks to start (and
can *fail to start*). Everything else takes effect immediately. `tick()` resolves pending
transitions. Faults (`jam`/`stall`/`fail_start`) are honoured so fail-safe tests can prove
the manual override still works.
"""
from __future__ import annotations
from ..state import StateStore
from ..permissions import Intent

VALVE_TICKS = 2
GEN_TICKS = 2


class HASim:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.tick_n = 0
        self._pending: list[dict] = []   # {resolve, entity_id, state}

    def _schedule(self, delay: int, entity_id: str, new_state) -> None:
        self._pending.append({"resolve": self.tick_n + delay, "entity_id": entity_id, "state": new_state})

    def tick(self) -> None:
        self.tick_n += 1
        due = [p for p in self._pending if p["resolve"] <= self.tick_n]
        for p in due:
            self.state.set_state(p["entity_id"], p["state"])
        self._pending = [p for p in self._pending if p["resolve"] > self.tick_n]

    def apply(self, intent: Intent) -> dict:
        e = self.state.entity(intent.entity_id)
        if e is None:
            return {"ok": False, "message": f"no such entity {intent.entity_id}"}
        prior = e.state
        undo = {"entity_id": intent.entity_id, "state": prior}
        s, a, args = intent.subsystem, intent.action, intent.args

        def done(new_state, msg, reversible=True):
            self.state.set_state(intent.entity_id, new_state)
            return {"ok": True, "message": msg, "undo": undo if reversible else None}

        if s == "light":
            if a == "turn_on":
                return done("on", f"{intent.entity_id} on")
            if a == "turn_off":
                return done("off", f"{intent.entity_id} off")
            if a == "set_brightness":
                self.state.set_state(intent.entity_id, "on", brightness=args.get("brightness", 50))
                return {"ok": True, "message": f"{intent.entity_id} brightness {args.get('brightness', 50)}", "undo": undo}
        if s == "climate" and a == "set_temperature":
            lo, hi = e.attributes.get("min_f", 60), e.attributes.get("max_f", 82)
            t = max(lo, min(hi, int(args.get("temperature", prior))))
            return done(t, f"{intent.entity_id} set to {t}F")
        if s == "climate" and a in ("set_fan", "set_mode"):
            return done(args.get("value", a), f"{intent.entity_id} {a}")
        if s == "cover":
            return done(args.get("position", a if a != "set_position" else "open"), f"{intent.entity_id} {a}")
        if s == "plug":
            return done("on" if a == "turn_on" else "off", f"{intent.entity_id} {a}")
        if s == "speaker" and a == "announce":
            return done("announcing", f"announce: {args.get('message', '')}")
        if s == "lock":
            if e.attributes.get("jam"):
                return {"ok": False, "message": f"{intent.entity_id} JAM — use manual key"}
            return done("locked" if a == "lock" else "unlocked", f"{intent.entity_id} {a}")
        if s == "garage":
            return done("closed" if a == "close" else "open", f"{intent.entity_id} {a}")
        if s == "camera":
            if a == "set_mode":
                self.state.set_state(intent.entity_id, args.get("mode", "event"))
                return {"ok": True, "message": f"{intent.entity_id} mode {args.get('mode', 'event')}", "undo": undo}
            return {"ok": True, "message": f"{intent.entity_id} {a}", "undo": None}
        if s == "water":
            if a == "shutoff_main":
                if e.attributes.get("stall"):
                    self.state.set_state(intent.entity_id, "closing")   # stuck; manual override required
                    return {"ok": True, "message": f"{intent.entity_id} closing (STALL — needs manual lever)", "undo": undo}
                self.state.set_state(intent.entity_id, "closing")
                self._schedule(VALVE_TICKS, intent.entity_id, "closed")
                return {"ok": True, "message": f"{intent.entity_id} closing", "undo": undo}
            if a == "open_main":
                return done("open", f"{intent.entity_id} open")
            if a in ("irrigation_on", "irrigation_off"):
                return done("on" if a.endswith("on") else "off", f"{intent.entity_id} {a}")
        if s == "hvac" and a == "emergency_shutoff":
            return done("off", f"{intent.entity_id} circulation stopped")
        if s == "power":
            if a in ("breaker_on", "breaker_off"):
                return done("on" if a.endswith("on") else "off", f"{intent.entity_id} {a}")
            if a == "load_shed":
                return done(f"shedding:{args.get('tier', 'tier2')}", f"load shed {args.get('tier', 'tier2')}")
        if s == "evcharger" and a == "set_limit":
            return done(int(args.get("amps", 16)), f"{intent.entity_id} limit {args.get('amps', 16)}A")
        if s == "battery" and a == "set_mode":
            return done(args.get("mode", "backup"), f"{intent.entity_id} mode {args.get('mode', 'backup')}")
        if s == "generator" and a == "start":
            if e.attributes.get("fail_start"):
                return done("failed", f"{intent.entity_id} FAILED to start — manual start required")
            self.state.set_state(intent.entity_id, "starting")
            self._schedule(GEN_TICKS, intent.entity_id, "running")
            return {"ok": True, "message": f"{intent.entity_id} starting", "undo": undo}
        if s == "alarm":
            if a == "arm":
                return done(f"armed_{args.get('mode', 'home')}", f"alarm armed {args.get('mode', 'home')}")
            if a == "disarm":
                return done("disarmed", "alarm disarmed")
            if a == "escalate":
                return done("escalating", "alarm escalation started")
        return {"ok": False, "message": f"unhandled {s}.{a}"}
