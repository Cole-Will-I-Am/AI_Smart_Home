"""Simulator-backed adapter: routes network actions to NetSim, everything else to HASim."""
from __future__ import annotations
from .base import Adapter
from ..permissions import Intent
from ..simulator import HASim, NetSim


class SimAdapter(Adapter):
    def __init__(self, ha: HASim, net: NetSim) -> None:
        self.ha = ha
        self.net = net

    def apply(self, intent: Intent) -> dict:
        if intent.subsystem == "network":
            return self.net.apply(intent)
        return self.ha.apply(intent)

    def undo(self, undo: dict) -> None:
        if "net_restore" in undo:
            house_id, mac, vlan = undo["net_restore"]
            self.net.inventory[house_id][mac] = vlan
        elif "entity_id" in undo:
            # cancel any scheduled transition first, or it would clobber the restored state later
            self.ha.cancel_pending(undo["entity_id"])
            self.ha.state.set_state(undo["entity_id"], undo["state"])
