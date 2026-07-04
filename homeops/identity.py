"""Identity, roles, and capabilities (RBAC) for the managed-service model.

Removes the "trust-open" plane: every command runs as an authenticated Principal whose Role caps the
maximum permission level it can reach and whose scope limits which properties it may touch. This is
what lets an estate manager, an installer, or a 24/7 monitoring operator each hold exactly the
authority their job needs — and no more — over exactly the properties they're assigned.

Auth here is a simple hashed bearer token (the reference-impl stand-in); a production build issues
these from an IdP / mTLS / hardware-key flow. The *authorization* model (roles, caps, scope) is the
part that matters and is real.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
import secrets
import threading
import time

from .permissions import Operator


@dataclass(frozen=True)
class Role:
    name: str
    kind: str          # owner | ai | system | guest — maps to the router's existing trust logic
    max_level: int     # highest permission level this role may reach (L4/L5 are globally unreachable)
    can_confirm: bool  # may supply confirmation tokens for L2+ actions


# The role catalog for the estate/managed-service market.
ROLES: dict[str, Role] = {
    "owner":          Role("owner", "owner", 3, True),
    "estate_manager": Role("estate_manager", "owner", 2, True),   # security/utility, not power/infra
    "installer":      Role("installer", "owner", 3, True),        # full setup authority (audited as installer)
    "monitor":        Role("monitor", "owner", 1, True),          # 24/7 NOC: observe + routine only
    "ai":             Role("ai", "ai", 3, False),                 # kind-logic still forces human confirm for L2+
    "system":         Role("system", "system", 3, False),         # local automations
    "guest":          Role("guest", "guest", 1, False),
}


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role
    houses: object     # "*" or a frozenset of house ids this principal is scoped to


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class IdentityStore:
    def __init__(self, path: str | None = None) -> None:
        self._by_token: dict[str, Principal] = {}   # hashed token -> Principal
        self._expiry: dict[str, float] = {}         # R5: hashed token -> wall-clock expiry; absent = never
        self._lock = threading.Lock()               # R2: enroll/revoke/authenticate mutate shared maps
        self._path = path                           # R6: None = in-memory; a path persists enrollments
        if path and os.path.exists(path):
            self._load()

    # --- R6: durable enrollment across restart -------------------------------------------
    def _snapshot(self) -> list[dict]:
        out = []
        for h, p in self._by_token.items():
            scope = "*" if p.houses == "*" else sorted(p.houses)
            out.append({"h": h, "id": p.id, "role": p.role.name,
                        "houses": scope, "expires": self._expiry.get(h)})
        return out

    def _save(self) -> None:
        """Atomic, 0600 snapshot. Only HASHED tokens are stored — never a bearer secret.
        Callers hold self._lock. A restart reloads exactly these enrollments (R6)."""
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._snapshot(), f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)   # rename is atomic — a crash never leaves a half-written store

    def _load(self) -> None:
        with open(self._path) as f:
            for e in json.load(f):
                houses = "*" if e["houses"] == "*" else frozenset(e["houses"])
                self._by_token[e["h"]] = Principal(e["id"], ROLES[e["role"]], houses)
                if e.get("expires") is not None:
                    self._expiry[e["h"]] = e["expires"]

    def register(self, principal_id: str, role_name: str, houses="*", token: str | None = None,
                 ttl_seconds: float | None = None) -> str:
        tok = token or secrets.token_urlsafe(16)
        scope = "*" if houses == "*" else frozenset(houses)
        h = _hash(tok)
        with self._lock:
            self._by_token[h] = Principal(principal_id, ROLES[role_name], scope)
            if ttl_seconds is not None:
                self._expiry[h] = time.time() + ttl_seconds
            else:
                self._expiry.pop(h, None)
            self._save()
        return tok   # returned once; only the hash is stored

    def authenticate(self, token: str) -> Principal | None:
        h = _hash(token or "")
        with self._lock:
            p = self._by_token.get(h)
            if p is None:
                return None
            exp = self._expiry.get(h)
            if exp is not None and time.time() >= exp:
                self._by_token.pop(h, None)   # R5: expired credential auto-purges on use
                self._expiry.pop(h, None)
                self._save()                  # R6: the purge is durable too
                return None
            return p

    def revoke(self, token: str) -> bool:
        """R5: immediately invalidate a device/bearer token. True if a credential was removed."""
        h = _hash(token or "")
        with self._lock:
            self._expiry.pop(h, None)
            gone = self._by_token.pop(h, None) is not None
            if gone:
                self._save()
            return gone

    def revoke_principal(self, principal_id: str) -> int:
        """R5: revoke every credential a principal holds (e.g. a departed delegate). Returns count."""
        with self._lock:
            hs = [h for h, pr in self._by_token.items() if pr.id == principal_id]
            for h in hs:
                self._by_token.pop(h, None)
                self._expiry.pop(h, None)
            if hs:
                self._save()
            return len(hs)


def operator_for(principal: Principal, active_house: str) -> Operator:
    """Build the runtime Operator a Principal acts as, carrying its role cap + property scope."""
    return Operator(kind=principal.role.kind, active_house=active_house, name=principal.id,
                    max_level=principal.role.max_level, houses=principal.houses)
