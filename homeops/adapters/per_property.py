"""Per-property adapter routing.

Real multi-property deployments must NOT share one Home Assistant / OPNsense endpoint across homes
(the two-house-collapse risk). This routes each intent to that property's own adapter (its own HA +
OPNsense credentials), so House A and the lake house are physically separate control planes behind
one router. Undo is tagged with its property so rollbacks route back to the right adapter.
"""
from __future__ import annotations
from .base import Adapter
from ..permissions import Intent


class PerPropertyAdapter(Adapter):
    def __init__(self, by_house: dict[str, Adapter], default: Adapter | None = None) -> None:
        self.by_house = by_house
        self.default = default

    def _adapter_for(self, house_id: str) -> Adapter | None:
        return self.by_house.get(house_id, self.default)

    def apply(self, intent: Intent) -> dict:
        ad = self._adapter_for(intent.house_id)
        if ad is None:
            return {"ok": False, "message": f"no adapter registered for property {intent.house_id}"}
        res = ad.apply(intent)
        if res.get("undo"):
            res = dict(res)
            res["undo"] = {"_house": intent.house_id, "inner": res["undo"]}   # tag for undo routing
        return res

    def undo(self, undo: dict) -> None:
        ad = self._adapter_for(undo.get("_house"))
        inner = undo.get("inner")
        if ad is not None and inner is not None:
            ad.undo(inner)
