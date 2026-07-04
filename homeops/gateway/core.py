"""Control Surface Gateway (Part 19) — one authenticated write path for every human/voice surface.

Every surface — phone app, wall tablet, Alexa, Google Home, HA Assist — collapses to the SAME
structured Intent and faces the SAME engine as the AI ops layer. No surface is privileged. The
gateway is a *pure translation* layer: it authenticates a device to a Principal, builds the
runtime Operator that Principal acts as (carrying its role cap + property scope), and hands the
(Intent, Operator) pair to CommandRouter.execute. It performs NO permission logic of its own —
the router remains the sole authority, so a per-surface policy can never drift from the real one.

Two things the stateless HTTP surface legitimately adds over the in-process ChatSession:

  * device authentication (hashed bearer tokens, via the existing IdentityStore), and
  * a PENDING REGISTRY: L2+ actions return confirm_required across a request boundary, so the
    pending intent — AND the engine's signed attestation (Part 18b) — must be held server-side
    until a *confirming* surface (can_confirm) approves it. Confirmation reissues the intent as
    the human operator, re-derives and verifies the attestation against the stored intent, and
    only then executes. The confirmation token lives engine -> gateway -> engine; it never
    crosses the network to a client and never enters any model's context.

This generalizes the project's central result one axis further: authority was already invariant
under substitution of the MODEL (Part 17); here it is invariant under substitution of the SURFACE.
"""
from __future__ import annotations
from dataclasses import dataclass
import secrets as _secrets
import threading

from ..permissions import Attestation, Intent
from ..identity import IdentityStore, Principal, operator_for


# A surface is metadata only — it has NO authority; the Principal behind the device does.
KNOWN_SURFACES = {"phone", "tablet", "alexa", "google", "ha_assist", "cli", "app"}


@dataclass
class Device:
    """A registered control surface. `principal` carries kind/max_level/houses/can_confirm."""
    device_id: str
    principal: Principal
    surface: str = "app"

    @property
    def can_confirm(self) -> bool:
        return self.principal.role.can_confirm


@dataclass
class Pending:
    pending_id: str
    intent: Intent
    level: int | None
    attestation: Attestation | None
    created_by_surface: str
    created_by_device: str
    house_id: str
    created_tick: int
    expires_tick: int
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "pending_id": self.pending_id,
            "intent": {"house_id": self.intent.house_id, "subsystem": self.intent.subsystem,
                       "target": self.intent.target, "action": self.intent.action,
                       "args": dict(self.intent.args)},
            "level": self.level,
            # the engine's ground-truth sentence for the UI to render (never a surface paraphrase):
            "effect": self.attestation.effect if self.attestation else None,
            "created_by_surface": self.created_by_surface,
            "created_by_device": self.created_by_device,
            "expires_tick": self.expires_tick,
            "message": self.message,
        }


class Gateway:
    """Stateless-per-request core, injectable for tests (no socket needed).

    `now_tick` reads the engine clock so pending TTLs share the world's tick, keeping tests
    deterministic and matching the confirm-token TTL that already lives on the engine.
    """

    def __init__(self, world, identity: IdentityStore | None = None, pending_ttl: int = 40) -> None:
        self.world = world
        self.identity = identity or world.identity
        self.pending_ttl = pending_ttl
        self._devices: dict[str, Device] = {}
        self._pending: dict[str, Pending] = {}
        self._lock = threading.RLock()   # R2: _pending/_devices shared across gateway threads

    # ---- device enrollment (reference-impl: hashed bearer via IdentityStore) --------------
    def enroll(self, device_id: str, role: str, houses="*", surface: str = "app",
               token: str | None = None) -> str:
        tok = self.identity.register(device_id, role, houses=houses, token=token)
        principal = self.identity.authenticate(tok)
        self._devices[device_id] = Device(device_id, principal, surface)
        return tok   # returned once; only the hash is stored

    def _device_for(self, token: str) -> Device | None:
        principal = self.identity.authenticate(token or "")
        if principal is None:
            return None
        for d in self._devices.values():
            if d.principal.id == principal.id:
                return d
        # authenticated principal without a registered Device row (e.g. enrolled elsewhere):
        return Device(principal.id, principal, "app")

    # ---- housekeeping ---------------------------------------------------------------------
    def _now(self) -> int:
        return self.world.router.engine.tick

    def _sweep(self) -> None:
        now = self._now()
        with self._lock:
            self._pending = {k: p for k, p in self._pending.items() if p.expires_tick >= now}

    # ---- the one write path ---------------------------------------------------------------
    def submit_intent(self, token: str, body: dict) -> dict:
        """Authenticate -> translate -> route. Returns a JSON-able result. All authority is the
        router's; the gateway only refuses malformed input and unauthenticated/out-of-scope
        callers up front (fail-closed) so a bad request never becomes a pending a human might
        approve."""
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized", "message": "unknown or missing device token"}
        try:
            house_id = str(body["house_id"])
            subsystem = str(body["subsystem"])
            target = str(body["target"])
            action = str(body["action"])
        except (KeyError, TypeError):
            return {"status": "bad_request",
                    "message": "house_id, subsystem, target, action are required"}
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return {"status": "bad_request", "message": "args must be an object"}
        surface = body.get("surface", device.surface)

        operator = operator_for(device.principal, active_house=house_id)
        intent = Intent(house_id=house_id, subsystem=subsystem, target=target, action=action,
                        args=dict(args))
        # NOTE: the gateway never sets confirm_cross_house or confirm_token from client input —
        # exactly like the AI tool surface, a human confirmation must originate the token.
        r = self.world.router.execute(intent, operator)

        out = {"status": r.status, "level": r.level, "message": r.message}
        if r.rollback_token:
            out["rollback_token"] = r.rollback_token
        if r.status == "confirm_required":
            # Hold the intent + engine attestation server-side; only a confirming surface can
            # approve it. The natural_language field (if any) is stored for audit, never trusted.
            self._sweep()
            with self._lock:
                pid = "pnd_" + _secrets.token_urlsafe(9)
                now = self._now()
                p = Pending(pending_id=pid, intent=intent, level=r.level, attestation=r.attestation,
                            created_by_surface=surface, created_by_device=device.device_id,
                            house_id=house_id, created_tick=now, expires_tick=now + self.pending_ttl,
                            message=r.message)
                self._pending[pid] = p
            out["pending_id"] = pid
            out["effect"] = r.attestation.effect if r.attestation else None
        return out

    def confirm(self, token: str, pending_id: str) -> dict:
        """A confirming surface approves a held intent. Reissues it AS THE HUMAN OPERATOR, verifies
        the stored attestation against the intent about to run, receives the engine token, executes.
        Token path: engine -> gateway -> engine — never to any client, never to any model."""
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized", "message": "unknown or missing device token"}
        if not device.can_confirm:
            return {"status": "refused",
                    "message": f"device {device.device_id} ({device.principal.role.name}) "
                               "may not confirm — use a confirming surface (phone/tablet)"}
        self._sweep()
        with self._lock:
            p = self._pending.get(pending_id)
        if p is None:
            return {"status": "not_found", "message": f"no pending confirmation {pending_id!r} "
                                                      "(expired, denied, or already handled)"}
        operator = operator_for(device.principal, active_house=p.house_id)
        cross = p.house_id != operator.active_house  # operator.active_house == p.house_id here, so False
        intent = Intent(p.intent.house_id, p.intent.subsystem, p.intent.target, p.intent.action,
                        dict(p.intent.args), confirm_cross_house=cross)

        # Ground-truth guard (Part 18b) — the human is about to consent to whatever `p.effect`
        # showed them. Re-derive the attestation from the engine and refuse any mismatch, so a
        # tampered/stale pending cannot convert a tap into a different deed.
        eng = self.world.router.engine
        if p.attestation is not None:
            truth = eng.attest(intent, operator, eng.level(intent.subsystem, intent.action))
            if not eng.verify_attestation(p.attestation) or p.attestation.effect != truth.effect:
                with self._lock:
                    self._pending.pop(pending_id, None)
                return {"status": "refused",
                        "message": "attestation did not verify — refusing to execute unverified consent"}

        r = self.world.router.execute(intent, operator)
        if r.status == "confirm_required" and r.confirm_token:
            intent.confirm_token = r.confirm_token   # engine -> gateway -> engine; never a client
            r = self.world.router.execute(intent, operator)
        if r.status in ("executed", "prohibited", "recommend_only", "refused", "unverified"):
            with self._lock:
                self._pending.pop(pending_id, None)   # terminal outcome consumes the pending
        out = {"status": r.status, "level": r.level, "message": r.message,
               "confirmed_by": device.principal.id}
        if r.rollback_token:
            out["rollback_token"] = r.rollback_token
        return out

    def deny(self, token: str, pending_id: str, reason: str = "") -> dict:
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized", "message": "unknown or missing device token"}
        with self._lock:
            p = self._pending.pop(pending_id, None)
        if p is None:
            return {"status": "not_found", "message": f"no pending confirmation {pending_id!r}"}
        # audited as a first-class decision through the router's advisory path
        self.world.router.recommend(
            p.house_id, f"DENIED via {device.surface} by {device.principal.id}: "
                        f"{p.intent.subsystem}.{p.intent.action}"
                        + (f" ({reason})" if reason else ""),
            operator_for(device.principal, p.house_id))
        return {"status": "denied", "pending_id": pending_id, "denied_by": device.principal.id}

    # ---- reads ----------------------------------------------------------------------------
    def list_pending(self, token: str, house_id: str | None = None) -> dict:
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized"}
        self._sweep()
        scope = device.principal.houses
        with self._lock:
            items = [p.to_dict() for p in self._pending.values()
                     if (house_id is None or p.house_id == house_id)
                     and (scope == "*" or p.house_id in scope)]   # R9: a device sees only its own scope
        return {"pending": items}

    def state(self, token: str, house_id: str | None = None) -> dict:
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized"}
        houses = {}
        scope = device.principal.houses
        for hid, h in self.world.houses.items():
            if house_id and hid != house_id:
                continue
            if scope != "*" and hid not in scope:
                continue    # a device only sees the properties it is scoped to
            houses[hid] = {
                "mode": h.mode, "wan_up": h.wan_up, "grid_up": h.grid_up, "ai_hold": h.ai_hold,
                "entities": {e.entity_id: e.state for e in h.entities.values()},
            }
        return {"houses": houses}

    def events(self, token: str, house_id: str | None = None, n: int = 20) -> dict:
        device = self._device_for(token)
        if device is None:
            return {"status": "unauthorized"}
        scope = device.principal.houses
        if house_id is not None and scope != "*" and house_id not in scope:
            return {"events": []}   # R9: requested a house outside this device's scope
        evs = self.world.bus.recent(n=n, house_id=house_id)
        if scope != "*":
            evs = [e for e in evs if e.house_id in scope]   # R9: filter cross-scope events
        return {"events": [{"type": e.type, "house": e.house_id, "data": e.data} for e in evs]}
