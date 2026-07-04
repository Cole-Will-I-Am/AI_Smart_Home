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
        "name": "explain_action",
        "description": ("Read the permission policy for an action before proposing it: level, "
                        "confirmation need, safety-critical status, delegability, and reason."),
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "subsystem": {"type": "string"},
                "action": {"type": "string"},
            },
            "required": ["subsystem", "action"],
        },
    },
    {
        "name": "device_health",
        "description": "Read device health status: ok, stale, offline, or unknown. Pure read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "entity_id": {"type": "string", "description": "Fully-qualified entity id, or subsystem.name with house_id"},
            },
        },
    },
    {
        "name": "list_pending_confirmations",
        "description": "List pending human confirmations visible to this operator. Never returns confirmation tokens.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string"}},
        },
    },
    {
        "name": "read_audit_tail",
        "description": "Read recent audit records with token and secret fields redacted. Pure read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "n": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "situation",
        "description": "Compact cross-house operational summary honoring the operator's property scope. Pure read.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string"}},
        },
    },
    {
        "name": "trend",
        "description": "Read baseline trend for an entity: rising/falling/steady plus slope and magnitude. Pure read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "entity_id": {"type": "string", "description": "Fully-qualified entity id, or subsystem.name with house_id"},
            },
            "required": ["house_id", "entity_id"],
        },
    },
    {
        "name": "list_routines",
        "description": "List installed standing automations, last-fired tick, and budget remaining. Never returns tokens.",
        "input_schema": {
            "type": "object",
            "properties": {"house_id": {"type": "string"}},
        },
    },
    {
        "name": "propose_routine",
        "description": ("Validate and return a standing routine SPEC for a resident owner to install. "
                        "This does not install or execute anything."),
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string"},
                "when": {"description": "Simple live-state or recent-event predicate, same style as propose_plan step when"},
                "then_steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "house_id": {"type": "string"},
                            "subsystem": {"type": "string"},
                            "target": {"type": "string"},
                            "action": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["subsystem", "target", "action"],
                    },
                },
            },
            "required": ["house_id", "when", "then_steps"],
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
        "name": "propose_plan",
        "description": ("Propose an ordered multi-step plan. Each step is evaluated independently "
                        "through the same permission engine; gated steps return confirm_required "
                        "unless covered by standing delegation. A false 'when' predicate skips "
                        "that step. No confirmation tokens are returned."),
        "input_schema": {
            "type": "object",
            "properties": {
                "house_id": {"type": "string", "description": "Default house for steps; each step may name its own house_id explicitly"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "house_id": {"type": "string"},
                            "subsystem": {"type": "string"},
                            "target": {"type": "string"},
                            "action": {"type": "string"},
                            "args": {"type": "object"},
                            "when": {"description": "Simple live-state predicate, e.g. {'entity_id':'house_a.battery.main','equals':'grid'} or 'house_a.battery.main == grid'"},
                        },
                        "required": ["subsystem", "target", "action"],
                    },
                },
            },
            "required": ["house_id", "steps"],
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
