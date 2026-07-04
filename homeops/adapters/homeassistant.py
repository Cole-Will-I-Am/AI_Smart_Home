"""Real Home Assistant adapter — REST for commands, WebSocket for the live event feed.

`apply()` maps a homeops Intent (subsystem/action/target) to an HA domain/service/entity and
POSTs `/api/services/<domain>/<service>`; `undo()` re-issues the inverse service for the
reversible subset (on/off, lock, cover, valve, alarm). `run_event_bridge()` connects the HA
WebSocket API, subscribes to `state_changed`, and translates configured entity changes into
homeops `Event`s — so the SAME local-first automations fire on real sensors.

The permission engine still gates everything upstream; this adapter only actuates approved,
already-validated intents.
"""
from __future__ import annotations
import json
import re
import ssl
from typing import Any, Callable

from .base import Adapter
from .http import HttpClient, Transport
from ..permissions import Intent
from ..events import Event, EventBus

# H3: a well-formed HA entity_id is `<domain>.<object_id>`; both halves are lowercase
# alphanumeric + underscore. Anything else (slashes, '..', spaces, uppercase, control chars)
# is refused before it can reach a request path.
_SAFE_ENTITY_ID = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")

# Reversible HA domains: prior state maps cleanly to an inverse service.
_REVERSIBLE = {"light", "switch", "lock", "cover", "valve", "alarm_control_panel"}

# H3: the only alarm arm modes HA defines. `alarm_arm_<mode>` is a request PATH segment, so the
# mode is whitelisted rather than interpolated raw.
_ALARM_ARM_MODES = {"home", "away", "night", "vacation", "custom_bypass"}

# Safety-impacting (domain, service) -> the state the device MUST reach for the command to
# count as executed. These get post-actuation verification (read-back) so "executed" means
# "confirmed", not merely "HTTP 200".
_VERIFY_EXPECT = {
    ("lock", "lock"): "locked", ("lock", "unlock"): "unlocked",
    ("valve", "close_valve"): "closed", ("valve", "open_valve"): "open",
}


def map_intent(intent: Intent, announce_service: tuple[str, str] = ("notify", "notify")) -> tuple[str, str, dict] | None:
    """(subsystem, action) -> (ha_domain, ha_service, service_data). None if unmapped."""
    s, a, args = intent.subsystem, intent.action, intent.args
    if s == "light":
        if a == "turn_on":
            return "light", "turn_on", {}
        if a == "turn_off":
            return "light", "turn_off", {}
        if a == "set_brightness":
            return "light", "turn_on", {"brightness_pct": args.get("brightness", 50)}
    if s == "plug":
        if a == "turn_on":
            return "switch", "turn_on", {}
        if a == "turn_off":
            return "switch", "turn_off", {}
        return None
    if s == "climate":
        if a == "set_temperature":
            return "climate", "set_temperature", {"temperature": args.get("temperature")}
        if a == "set_fan":
            return "climate", "set_fan_mode", {"fan_mode": args.get("value", "auto")}
        if a == "set_mode":
            return "climate", "set_hvac_mode", {"hvac_mode": args.get("value", "heat")}
    if s == "hvac" and a == "emergency_shutoff":
        return "climate", "set_hvac_mode", {"hvac_mode": "off"}
    if s == "cover":
        if a == "open":
            return "cover", "open_cover", {}
        if a == "close":
            return "cover", "close_cover", {}
        if a == "set_position":
            return "cover", "set_cover_position", {"position": args.get("position", 50)}
    if s == "garage":
        if a == "open":
            return "cover", "open_cover", {}
        if a == "close":
            return "cover", "close_cover", {}
        return None
    if s == "lock":
        # fail-closed: ONLY the explicit lock/unlock actions map. A stray/virtual action such as
        # "unlock_unknown" (which the router blocks as L4) must not silently become lock.unlock here.
        if a == "lock":
            return "lock", "lock", {}
        if a == "unlock":
            return "lock", "unlock", {}
        return None
    if s == "speaker" and a == "announce":
        return announce_service[0], announce_service[1], {"message": args.get("message", "")}
    if s == "camera":
        if a == "snapshot":
            return "camera", "snapshot", {"filename": args.get("filename", "/config/www/snapshot.jpg")}
        return None   # recording-mode/export is integration-specific; use service_overrides
    if s == "water":
        if a == "shutoff_main":
            return "valve", "close_valve", {}
        if a == "open_main":
            return "valve", "open_valve", {}
        if a in ("irrigation_on", "irrigation_off"):
            return "switch", "turn_on" if a.endswith("on") else "turn_off", {}
    if s == "power":
        if a in ("breaker_on", "breaker_off"):
            return "switch", "turn_on" if a.endswith("on") else "turn_off", {}
        if a == "load_shed":
            return "scene", "turn_on", {}   # a "load_shed" scene on the smart panel
    if s == "evcharger" and a == "set_limit":
        return "number", "set_value", {"value": args.get("amps", 16)}
    if s == "battery" and a == "set_mode":
        return "select", "select_option", {"option": args.get("mode", "backup")}
    if s == "generator" and a == "start":
        return "button", "press", {}
    if s == "alarm":
        if a == "arm":
            # H3: the mode becomes part of the request PATH; never interpolate an untrusted value.
            # Home Assistant only defines these arm services — anything else is rejected here rather
            # than sent as e.g. alarm_arm_away/../../../states.
            mode = str(args.get("mode", "home"))
            if mode not in _ALARM_ARM_MODES:
                return None
            return "alarm_control_panel", "alarm_arm_" + mode, {}
        if a == "disarm":
            return "alarm_control_panel", "alarm_disarm", {}
        if a == "escalate":
            return "script", "turn_on", {}
    return None


class HomeAssistantAdapter(Adapter):
    def __init__(self, base_url: str, token: str, transport: Transport | None = None, verify_tls: bool = True,
                 entity_map: dict[str, str] | None = None,
                 service_overrides: dict[tuple[str, str], tuple[str, str, dict]] | None = None,
                 announce_service: tuple[str, str] = ("notify", "notify"),
                 strict_entity_map: bool = False, verify_safety: bool = True) -> None:
        self.token = token
        self.verify_tls = verify_tls
        self.http = HttpClient(base_url, default_headers={"Authorization": f"Bearer {token}"},
                               transport=transport, verify_tls=verify_tls)
        self.entity_map = entity_map or {}
        self.overrides = service_overrides or {}
        self.announce = announce_service
        # strict_entity_map: never fall back to a house-stripped f"{domain}.{target}" — fail closed
        # so House A and House B can't collapse onto the same real HA entity.
        self.strict_entity_map = strict_entity_map
        self.verify_safety = verify_safety
        self.ws_url = base_url.replace("http", "ws", 1).rstrip("/") + "/api/websocket"

    # --- REST command side ---------------------------------------------------
    def _resolve(self, intent: Intent):
        m = self.overrides.get((intent.subsystem, intent.action)) or map_intent(intent, self.announce)
        if not m:
            return None
        domain, service, data = m
        mapped = self.entity_map.get(intent.entity_id)
        if mapped is None and self.strict_entity_map:
            return None   # fail closed — no house-stripped fallback
        ha_entity = mapped or f"{domain}.{intent.target}"
        # H3: ha_entity is interpolated into /api/states/<id> and the service body. In non-strict
        # mode it derives from intent.target, which on the AI path is model-controlled. Reject any
        # id that could escape the intended endpoint (path traversal, slashes, whitespace, control
        # chars). A legitimate HA entity_id is `<domain>.<object_id>` over [a-z0-9_.].
        if not _SAFE_ENTITY_ID.match(ha_entity):
            return None
        return domain, service, dict(data), ha_entity

    def _get_state(self, ha_entity: str) -> dict | None:
        _, obj = self.http.request("GET", f"/api/states/{ha_entity}")
        return obj if isinstance(obj, dict) else None

    def _undo_for(self, domain: str, ha_entity: str, prior: dict | None) -> dict | None:
        if not prior or domain not in _REVERSIBLE:
            return None
        ps = prior.get("state")
        inv = None
        if domain in ("light", "switch"):
            inv = "turn_on" if ps == "on" else "turn_off"
        elif domain == "lock":
            inv = "lock" if ps == "locked" else "unlock"
        elif domain == "cover":
            inv = "open_cover" if ps in ("open", "opening") else "close_cover"
        elif domain == "valve":
            inv = "open_valve" if ps in ("open", "opening") else "close_valve"
        elif domain == "alarm_control_panel":
            if ps == "disarmed":
                inv = "alarm_disarm"
            elif isinstance(ps, str) and ps.startswith("armed_"):
                inv = "alarm_arm_" + ps[len("armed_"):]
        if not inv:
            return None
        return {"ha_undo": {"domain": domain, "service": inv, "entity_id": ha_entity}}

    def apply(self, intent: Intent) -> dict:
        r = self._resolve(intent)
        if not r:
            reason = "unmapped entity (strict)" if self.strict_entity_map and intent.entity_id not in self.entity_map \
                else "no HA mapping"
            return {"ok": False, "message": f"{reason} for {intent.subsystem}.{intent.action} ({intent.entity_id})"}
        domain, service, data, ha_entity = r
        prior = self._get_state(ha_entity)
        undo = self._undo_for(domain, ha_entity, prior)
        body = {"entity_id": ha_entity}
        body.update({k: v for k, v in data.items() if v is not None})
        status, _ = self.http.request("POST", f"/api/services/{domain}/{service}", json_body=body)
        if status >= 300:
            return {"ok": False, "message": f"HA {domain}.{service} -> HTTP {status}"}
        # Post-actuation verification for safety-impacting actions: HTTP 200 is not proof the
        # physical device moved. Read the state back and require it to be what we commanded.
        expect = _VERIFY_EXPECT.get((domain, service))
        if self.verify_safety and expect is not None:
            after = self._get_state(ha_entity)
            actual = after.get("state") if after else None
            if actual != expect:
                return {"ok": False,
                        "message": f"HA {domain}.{service} {ha_entity} UNVERIFIED (state={actual!r}, expected {expect!r})",
                        "undo": None}
        # verified=True tells the router this adapter already confirmed the outcome against the real
        # device — the router must NOT re-verify against the (sim-style) state store.
        return {"ok": True, "message": f"HA {domain}.{service} {ha_entity}", "undo": undo, "verified": True}

    def undo(self, undo: dict) -> None:
        u = undo.get("ha_undo")
        if u:
            self.http.request("POST", f"/api/services/{u['domain']}/{u['service']}",
                              json_body={"entity_id": u["entity_id"]})

    # --- WebSocket event side ------------------------------------------------
    def _default_ws_connect(self):
        try:
            from websocket import create_connection   # optional dep: pip install websocket-client
        except ImportError as e:  # pragma: no cover - only hit at runtime without the dep
            raise RuntimeError("HA event bridge needs `pip install websocket-client`") from e
        sslopt = {"cert_reqs": ssl.CERT_NONE} if not self.verify_tls else None
        return create_connection(self.ws_url, sslopt=sslopt)

    def run_event_bridge(self, bus: EventBus, event_map: dict[str, dict],
                         connect: Callable[[], Any] | None = None) -> None:
        """Subscribe to HA state_changed and publish mapped events onto the bus.

        `event_map`: {ha_entity_id: {"type": "leak", "when": "on", "house_id": "house_a", "data": {...}}}
        Blocks until the connection closes (recv() returns None). Run in a thread in production.
        """
        conn = connect() if connect else self._default_ws_connect()
        conn.recv()   # {"type":"auth_required"}
        conn.send(json.dumps({"type": "auth", "access_token": self.token}))
        conn.recv()   # {"type":"auth_ok"}
        conn.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        conn.recv()   # {"id":1,"type":"result","success":true}
        while True:
            raw = conn.recv()
            if not raw:
                break
            m = json.loads(raw)
            if m.get("type") != "event":
                continue
            data = m.get("event", {}).get("data", {})
            ent = data.get("entity_id")
            new_state = (data.get("new_state") or {}).get("state")
            spec = event_map.get(ent)
            if spec and new_state == spec.get("when"):
                bus.publish(Event(spec["type"], spec.get("house_id", ""), ent,
                                  {**spec.get("data", {}), "state": new_state}, 0))
