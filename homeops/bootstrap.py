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
from .health import HealthRegistry
from .identity import IdentityStore
from .delegations import DelegationRegistry
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
    health: HealthRegistry
    identity: IdentityStore
    delegations: "DelegationRegistry" = None
    notifications: list = field(default_factory=list)
    anomaly_monitor: object | None = None   # vigilance tier (attached in build_world)

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.engine.tick += 1
            self.ha.tick()

    def notify(self, house_id: str, message: str, urgent: bool = False) -> None:
        self.notifications.append({"house_id": house_id, "message": message, "urgent": urgent,
                                   "tick": self.engine.tick})


def build_world(config_path: str = DEFAULT_CONFIG, register_automations: bool = True, adapter=None,
                audit_path: str | None = None, ai_l1_daily_budget: int = 60,
                attest_key: bytes | None = None, persist_dir: str | None = None) -> World:
    """Build the world. `adapter=None` uses the in-process SimAdapter (default, for tests/demos);
    pass a real adapter (e.g. CompositeAdapter of HomeAssistantAdapter+OPNsenseAdapter) to drive
    actual hardware — nothing above the adapter layer changes. `audit_path` persists the
    tamper-evident audit chain to an append-only JSONL file (reloaded + re-verified on restart)."""
    houses = load_houses(config_path)
    engine = PermissionEngine(ai_l1_daily_budget=ai_l1_daily_budget, attest_key=attest_key)
    audit = AuditLog(audit_path)
    state = StateStore(houses, audit=audit, clock=lambda: engine.tick)
    bus = EventBus()
    ha = HASim(state)
    net = NetSim(state)
    adapter = adapter or SimAdapter(ha, net)
    health = HealthRegistry()
    if isinstance(adapter, SimAdapter):
        for h in houses.values():
            for eid in h.entities:
                health.heartbeat(eid, 0)   # sim devices are in-process and responsive at boot
    router = CommandRouter(engine, state, adapter, audit, health=health)
    # R6: enrollments and standing consent survive a restart when persist_dir is set (real mode).
    id_path = del_path = None
    if persist_dir:
        os.makedirs(persist_dir, exist_ok=True)
        id_path = os.path.join(persist_dir, "identity.json")
        del_path = os.path.join(persist_dir, "delegations.json")
    identity = IdentityStore(path=id_path)
    delegations = DelegationRegistry(path=del_path)
    world = World(houses=houses, state=state, bus=bus, ha=ha, net=net,
                  engine=engine, audit=audit, adapter=adapter, router=router, health=health,
                  identity=identity, delegations=delegations)
    if register_automations:
        automations.register(world)
        from .baseline import AnomalyMonitor
        world.anomaly_monitor = AnomalyMonitor(world).attach()
    return world


def controllable_entities(houses) -> list[str]:
    """Entity ids that the HA adapter must be able to actuate (excludes observe-only sensors and
    the network subsystem, which OPNsense handles)."""
    out = []
    for h in houses.values():
        for e in h.entities.values():
            if e.actions and e.subsystem not in ("network", "sensor"):
                out.append(e.entity_id)
    return out


def build_real_world(ha_base_url: str, ha_token: str, opn_base_url: str, opn_key: str, opn_secret: str,
                     config_path: str = DEFAULT_CONFIG, entity_map: dict | None = None,
                     event_map: dict | None = None, verify_tls: bool = True,
                     register_automations: bool = True, strict_entity_map: bool = True) -> World:
    """Wire a World to a live Home Assistant (REST commands + WS events) and OPNsense (REST).

    `entity_map`: homeops entity_id -> real HA entity_id. `event_map`: HA entity_id ->
    {"type","when","house_id","data"} for the WebSocket event bridge. Call `start_event_bridge(world)`
    to begin translating real sensor events into the local-first automations.

    With `strict_entity_map=True` (default), startup FAILS if any controllable entity in either
    house lacks an explicit HA mapping — this is what prevents House A and House B from silently
    collapsing onto the same real HA entity (e.g. both -> `light.kitchen`).
    """
    from .adapters import HomeAssistantAdapter, OPNsenseAdapter, CompositeAdapter
    entity_map = entity_map or {}
    if strict_entity_map:
        houses = load_houses(config_path)
        unmapped = [eid for eid in controllable_entities(houses) if eid not in entity_map]
        if unmapped:
            raise ValueError(
                f"{len(unmapped)} controllable entities have no explicit HA mapping "
                f"(two houses would collapse onto shared entities). Map them all, e.g.: {unmapped[:5]} ...")
    ha_ad = HomeAssistantAdapter(ha_base_url, ha_token, verify_tls=verify_tls,
                                 entity_map=entity_map, strict_entity_map=strict_entity_map)
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
