"""Dedicated tool schemas for the Claude ops layer.

Dedicated tools (not a bash tool) so the harness can gate/audit each action: `propose_command`
is validated by the permission engine before anything happens, and there is deliberately no
tool that can execute an L4/L5 action — the only path for those is `recommend`.
"""
from __future__ import annotations

TOOLS = [
    {
        "name": "read_state",
        "description": "Read the current state of one house or both. Use before acting.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string", "description": "house_a or house_b; omit for both"}},
        },
    },
    {
        "name": "list_recent_events",
        "description": "List recent sensor/network/grid events for situational awareness.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string"}},
        },
    },
    {
        "name": "propose_command",
        "description": ("Propose a control action. The permission engine validates and executes it. "
                        "L1 executes; L2/L3 return confirm_required (a human must confirm); "
                        "L4/L5 are refused — use recommend instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "subsystem": {"type": "string", "description": "light, climate, lock, alarm, garage, camera, water, power, evcharger, battery, generator, hvac, network, plug, cover, speaker"},
                "target": {"type": "string", "description": "device name, e.g. front_door, main_valve, thermostat_main"},
                "action": {"type": "string", "description": "e.g. turn_on, set_temperature, lock, unlock, arm, quarantine, shutoff_main"},
                "args": {"type": "object", "description": "action arguments, e.g. {\"temperature\": 68}"},
            },
            "required": ["house_id", "subsystem", "target", "action"],
        },
    },
    {
        "name": "recommend",
        "description": "Recommend an action to a human without executing it. The ONLY path for L4/L5 items.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["house_id", "message"],
        },
    },
]
