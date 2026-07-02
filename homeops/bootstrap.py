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


def build_world(config_path: str = DEFAULT_CONFIG, register_automations: bool = True, adapter=None) -> World:
    """Build the world. `adapter=None` uses the in-process SimAdapter (default, for tests/demos);
    pass a real adapter (e.g. CompositeAdapter of HomeAssistantAdapter+OPNsenseAdapter) to drive
    actual hardware — nothing above the adapter layer changes."""
    houses = load_houses(config_path)
    state = StateStore(houses)
    bus = EventBus()
    ha = HASim(state)
    net = NetSim(state)
    engine = PermissionEngine()
    audit = AuditLog()
    adapter = adapter or SimAdapter(ha, net)
    router = CommandRouter(engine, state, adapter, audit)
    world = World(houses=houses, state=state, bus=bus, ha=ha, net=net,
                  engine=engine, audit=audit, adapter=adapter, router=router)
    if register_automations:
        automations.register(world)
    return world


def build_real_world(ha_base_url: str, ha_token: str, opn_base_url: str, opn_key: str, opn_secret: str,
                     config_path: str = DEFAULT_CONFIG, entity_map: dict | None = None,
                     event_map: dict | None = None, verify_tls: bool = True,
                     register_automations: bool = True) -> World:
    """Wire a World to a live Home Assistant (REST commands + WS events) and OPNsense (REST).

    `entity_map`: homeops entity_id -> real HA entity_id. `event_map`: HA entity_id ->
    {"type","when","house_id","data"} for the WebSocket event bridge. Call `start_event_bridge(world)`
    to begin translating real sensor events into the local-first automations.
    """
    from .adapters import HomeAssistantAdapter, OPNsenseAdapter, CompositeAdapter
    ha_ad = HomeAssistantAdapter(ha_base_url, ha_token, verify_tls=verify_tls, entity_map=entity_map or {})
    net_ad = OPNsenseAdapter(opn_base_url, opn_key, opn_secret, verify_tls=verify_tls)
    world = build_world(config_path, register_automations=register_automations,
                        adapter=CompositeAdapter(ha_ad, net_ad))
    world.ha_adapter = ha_ad          # type: ignore[attr-defined]
    world.event_map = event_map or {}  # type: ignore[attr-defined]
    return world


def start_event_bridge(world: World, daemon: bool = True):
    """Launch the HA WebSocket->EventBus bridge in a background thread (real deployments)."""
    import threading
    t = threading.Thread(target=world.ha_adapter.run_event_bridge,  # type: ignore[attr-defined]
                         args=(world.bus, world.event_map), daemon=daemon)  # type: ignore[attr-defined]
    t.start()
    return t
