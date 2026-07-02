"""Adapter interface. The engine/automations/AI code only ever calls `apply(intent)`, so a
real Home Assistant adapter (HA WebSocket/REST) or a real OPNsense adapter can replace the
simulator without touching anything above this line.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from ..permissions import Intent


class Adapter(ABC):
    @abstractmethod
    def apply(self, intent: Intent) -> dict:
        """Perform the action. Return {"ok": bool, "message": str, "undo": dict|None}."""
        raise NotImplementedError

    @abstractmethod
    def undo(self, undo: dict) -> None:
        """Reverse a previously-applied reversible action (rollback)."""
        raise NotImplementedError
