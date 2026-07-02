"""OPNsense-semantics simulator: VLAN membership, device inventory, quarantine, IDS.

Faithful to the *behaviour* the network-control modules need (move a device to an isolated
VLAN, add a firewall rule) without a real firewall. A real OPNsense/pfSense adapter would
implement the same `apply`/`quarantine` surface.
"""
from __future__ import annotations
from ..state import StateStore
from ..permissions import Intent


class NetSim:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        # per house: mac -> vlan
        self.inventory: dict[str, dict[str, str]] = {h: {} for h in state.houses}
        self.firewall_rules: dict[str, list[str]] = {h: [] for h in state.houses}

    def join(self, house_id: str, mac: str, vlan: str = "trusted") -> None:
        self.inventory[house_id][mac] = vlan

    def vlan_of(self, house_id: str, mac: str) -> str | None:
        return self.inventory[house_id].get(mac)

    def apply(self, intent: Intent) -> dict:
        s, a, args = intent.subsystem, intent.action, intent.args
        if s != "network":
            return {"ok": False, "message": f"not a network action: {s}.{a}"}
        if a == "quarantine":
            mac = args.get("mac", "")
            prior = self.inventory[intent.house_id].get(mac, "unknown")
            self.inventory[intent.house_id][mac] = "iot_guest"   # isolated VLAN
            return {"ok": True, "message": f"quarantined {mac} -> iot_guest",
                    "undo": {"net_restore": (intent.house_id, mac, prior)}}
        if a == "firewall_policy":
            rule = args.get("rule", "policy-change")
            self.firewall_rules[intent.house_id].append(rule)
            self.state.set_state(f"{intent.house_id}.network.firewall", "policy_changed")
            return {"ok": True, "message": f"firewall rule added: {rule}", "undo": None}
        return {"ok": False, "message": f"unhandled network.{a}"}
