"""Local-first automation blueprints (DESIGN.md §Y).

These run entirely on the event bus + router with the **system** operator and NO reference to
the AI layer — that is what makes them local-first. Destructive emergency responses set
`emergency=True`, which the router treats as a pre-authorized local response (leak shutoff,
fire egress unlock, grid-loss shed, freeze heat, perimeter lock). Everything is audited.
"""
from __future__ import annotations
from .events import Event
from .permissions import Intent, Operator

ABNORMAL_FLOW = 30.0
SHED_WATTS = 15000
FREEZE_F = 38


def register(world) -> None:
    R = world.router

    def op(house_id: str) -> Operator:
        return Operator(kind="system", active_house=house_id, name="local-automations")

    def do(house_id, subsystem, target, action, args=None, emergency=False):
        return R.execute(Intent(house_id=house_id, subsystem=subsystem, target=target,
                                action=action, args=args or {}, emergency=emergency), op(house_id))

    def on_event(ev: Event) -> None:
        h = ev.house_id

        # 1. Water leak — TRUE two-signal shutoff: re-read BOTH independent channels from the
        #    state store at actuation time (a wet leak sensor AND an abnormal flow reading).
        #    The event payload alone is never trusted — a spoofed/stale event won't close the valve.
        if ev.type == "leak":
            sensor_wet = bool(ev.entity_id) and world.state.get_state(ev.entity_id) == "wet"
            flow = world.state.get_state(f"{h}.sensor.flow_meter")
            flow_bad = False
            try:
                flow_abnormal = flow is not None and float(flow) >= ABNORMAL_FLOW
            except (TypeError, ValueError):
                flow_bad = True
                flow_abnormal = True
            if sensor_wet and flow_abnormal:
                do(h, "water", "main_valve", "shutoff_main", emergency=True)
                reason = "wet sensor + unreadable flow" if flow_bad else "wet sensor + abnormal flow"
                world.notify(h, f"Leak confirmed ({reason}): main water shutting off", urgent=True)

        # 2. Unknown device joins the network -> quarantine
        elif ev.type == "network_join":
            do(h, "network", "firewall", "quarantine", {"mac": ev.data.get("mac")}, emergency=True)
            world.notify(h, f"New device quarantined: {ev.data.get('mac')}")

        # 3. Exterior motion at night -> floods + record + snapshot alert
        elif ev.type == "motion" and world.state.houses[h].mode == "night":
            do(h, "light", "exterior_front", "turn_on")
            do(h, "camera", "front_door", "set_mode", {"mode": "event"}, emergency=True)
            world.notify(h, "Night motion: floodlights on, recording, snapshot sent")

        # 4. High power draw -> cap EV charging + recommend load shed
        elif ev.type == "power_draw" and ev.data.get("watts", 0) > SHED_WATTS:
            do(h, "evcharger", "main", "set_limit", {"amps": 8}, emergency=True)
            R.recommend(h, "Power draw high: recommend load-shed tier2", op(h))

        # 5. Grid failure -> battery backup + shed nonessential + keep critical powered
        elif ev.type == "grid" and ev.data.get("status") == "down":
            do(h, "battery", "main", "set_mode", {"mode": "backup"}, emergency=True)
            do(h, "power", "load_shed", "load_shed", {"tier": "nonessential"}, emergency=True)
            world.notify(h, "On backup power: security/network/hub kept powered")

        # 7. Internet failure -> local mode (locals keep running; nothing to actuate)
        elif ev.type == "wan" and ev.data.get("status") == "down":
            world.notify(h, "WAN down: local automations active (locks/lights/HVAC/water/alarms)")

        # 8. Fire / CO (verified) -> egress lights, unlock designated egress, stop HVAC, announce
        elif ev.type == "smoke_co" and ev.data.get("verified"):
            do(h, "light", "exterior_front", "turn_on")
            do(h, "lock", "egress_side", "unlock", emergency=True)     # designated egress only
            do(h, "hvac", "main", "emergency_shutoff", emergency=True)
            do(h, "speaker", "intercom", "announce", {"message": "Fire/CO — evacuate"}, emergency=True)
            world.notify(h, "Fire/CO: egress unlocked, HVAC stopped, occupants notified", urgent=True)

        # 9. Freeze risk -> raise heat in the vulnerable zone
        elif ev.type == "temp" and ev.data.get("temp", 99) <= FREEZE_F:
            do(h, "climate", "thermostat_main", "set_temperature", {"temperature": 72})
            world.notify(h, "Freeze risk: heating vulnerable zone")

        # 11. Statistical anomaly (from the baseline vigilance tier) -> ADVISORY ONLY.
        #     Statistics are evidence, not authority: notify, never actuate. Physical
        #     actuation still requires the independent signals the rules above demand.
        elif ev.type == "anomaly":
            d = ev.data
            world.notify(h, (f"Anomaly: {ev.entity_id} {d.get('metric')}={d.get('value')} "
                             f"(expected ~{d.get('expected')}, z={d.get('z')}, n={d.get('n')})"),
                         urgent=float(d.get("z", 0)) >= 8.0)

        # 12. Composite inference (from the deterministic fusion tier) -> ADVISORY ONLY.
        #     Higher-order evidence enriches L0 and informs residents; it never actuates.
        elif ev.type == "inference":
            d = ev.data
            world.notify(h, f"Inference advisory: {d.get('inference_type')} — {d.get('message', '')}",
                         urgent=d.get("severity") in {"urgent", "critical"})

        # 10. Suspicious activity -> exterior lights, record, lock exterior doors
        elif ev.type == "perimeter" and ev.data.get("suspicious"):
            do(h, "light", "exterior_front", "turn_on")
            do(h, "camera", "front_door", "set_mode", {"mode": "event"}, emergency=True)
            do(h, "lock", "front_door", "lock", emergency=True)
            do(h, "lock", "back_door", "lock", emergency=True)
            world.notify(h, "Suspicious activity: exterior locked, lights on, recording", urgent=True)

    world.bus.subscribe(on_event)
