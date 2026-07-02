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
import secrets

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
    def __init__(self) -> None:
        self._by_token: dict[str, Principal] = {}   # hashed token -> Principal

    def register(self, principal_id: str, role_name: str, houses="*", token: str | None = None) -> str:
        tok = token or secrets.token_urlsafe(16)
        scope = "*" if houses == "*" else frozenset(houses)
        self._by_token[_hash(tok)] = Principal(principal_id, ROLES[role_name], scope)
        return tok   # returned once; only the hash is stored

    def authenticate(self, token: str) -> Principal | None:
        return self._by_token.get(_hash(token or ""))


def operator_for(principal: Principal, active_house: str) -> Operator:
    """Build the runtime Operator a Principal acts as, carrying its role cap + property scope."""
    return Operator(kind=principal.role.kind, active_house=active_house, name=principal.id,
                    max_level=principal.role.max_level, houses=principal.houses)
