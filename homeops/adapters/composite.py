"""Route by subsystem: network -> OPNsense, everything else -> Home Assistant.

Presents the single `Adapter.apply/undo` surface the CommandRouter expects, so a real
deployment is a drop-in replacement for SimAdapter with no engine/automation/AI changes.
"""
from __future__ import annotations
from .base import Adapter
from ..permissions import Intent


class CompositeAdapter(Adapter):
    def __init__(self, home: Adapter, network: Adapter) -> None:
        self.home = home
        self.network = network

    def apply(self, intent: Intent) -> dict:
        if intent.subsystem == "network":
            return self.network.apply(intent)
        return self.home.apply(intent)

    def undo(self, undo: dict) -> None:
        if "opn_del" in undo:
            self.network.undo(undo)
        else:
            self.home.undo(undo)
