"""Wire the whole world together from config/houses.example.yaml."""
from __future__ import annotations
from dataclasses import dataclass, field
import os

from .model import load_houses, House
from .state import StateStore
from .events import EventBus
from .permissions import PermissionEngine
from .audit import AuditLog
from .simulator import HASim, NetSim
from .adapters import SimAdapter
from .router import CommandRouter
from . import automations

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "houses.example.yaml")


@dataclass
class World:
    houses: dict[str, House]
    state: StateStore
    bus: EventBus
    ha: HASim
    net: NetSim
    engine: PermissionEngine
    audit: AuditLog
    adapter: SimAdapter
    router: CommandRouter
    notifications: list = field(default_factory=list)

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.engine.tick += 1
            self.ha.tick()

    def notify(self, house_id: str, message: str, urgent: bool = False) -> None:
        self.notifications.append({"house_id": house_id, "message": message, "urgent": urgent,
                                   "tick": self.engine.tick})


def build_world(config_path: str = DEFAULT_CONFIG, register_automations: bool = True) -> World:
    houses = load_houses(config_path)
    state = StateStore(houses)
    bus = EventBus()
    ha = HASim(state)
    net = NetSim(state)
    engine = PermissionEngine()
    audit = AuditLog()
    adapter = SimAdapter(ha, net)
    router = CommandRouter(engine, state, adapter, audit)
    world = World(houses=houses, state=state, bus=bus, ha=ha, net=net,
                  engine=engine, audit=audit, adapter=adapter, router=router)
    if register_automations:
        automations.register(world)
    return world
