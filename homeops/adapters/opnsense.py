"""Real OPNsense adapter — REST API for IoT quarantine and firewall policy.

Quarantine = add the offending host to a firewall alias (host group) that a
deny/isolate rule references, then reconfigure. `undo()` removes it. Auth is HTTP Basic with
the API key/secret pair; `verify_tls=False` supports the common self-signed-cert appliance.
Handles the `network` subsystem only — pair it with the HA adapter via CompositeAdapter.
"""
from __future__ import annotations
import base64

from .base import Adapter
from .http import HttpClient, Transport
from ..permissions import Intent


class OPNsenseAdapter(Adapter):
    def __init__(self, base_url: str, api_key: str, api_secret: str, transport: Transport | None = None,
                 verify_tls: bool = True, quarantine_alias: str = "quarantine") -> None:
        auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self.http = HttpClient(base_url, default_headers={"Authorization": f"Basic {auth}"},
                               transport=transport, verify_tls=verify_tls)
        self.alias = quarantine_alias

    def apply(self, intent: Intent) -> dict:
        if intent.subsystem != "network":
            return {"ok": False, "message": "OPNsense adapter handles the network subsystem only"}
        a, args = intent.action, intent.args
        if a == "quarantine":
            addr = args.get("ip") or args.get("mac")
            if not addr:
                return {"ok": False, "message": "quarantine needs an ip or mac in args"}
            status, _ = self.http.request("POST", f"/api/firewall/alias_util/add/{self.alias}",
                                          json_body={"address": addr})
            if status >= 300:
                return {"ok": False, "message": f"alias add -> HTTP {status}"}
            # a successful alias add is meaningless until reconfigure applies it — check it too
            rc_status, _ = self.http.request("POST", "/api/firewall/alias/reconfigure", json_body={})
            if rc_status >= 300:
                return {"ok": False, "message": f"alias reconfigure -> HTTP {rc_status} (quarantine NOT active)"}
            return {"ok": True, "message": f"quarantined {addr} to alias '{self.alias}'",
                    "undo": {"opn_del": addr}}
        if a == "firewall_policy":
            rule = args.get("rule") or {"action": "block", "description": args.get("description", "ai-policy")}
            status, _ = self.http.request("POST", "/api/firewall/filter/addRule", json_body=rule)
            if status >= 300:
                return {"ok": False, "message": f"addRule -> HTTP {status}"}
            ap_status, _ = self.http.request("POST", "/api/firewall/filter/apply", json_body={})
            if ap_status >= 300:
                return {"ok": False, "message": f"filter apply -> HTTP {ap_status} (rule NOT active)"}
            return {"ok": True, "message": "firewall rule added and applied", "undo": None}
        return {"ok": False, "message": f"unhandled network.{a}"}

    def undo(self, undo: dict) -> None:
        addr = undo.get("opn_del")
        if addr:
            self.http.request("POST", f"/api/firewall/alias_util/delete/{self.alias}",
                              json_body={"address": addr})
            self.http.request("POST", "/api/firewall/alias/reconfigure", json_body={})
