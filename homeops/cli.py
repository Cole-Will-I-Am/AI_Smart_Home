"""Tiny operator CLI: status, commands, and the ops lifecycle (validate -> preflight -> serve).

    python -m homeops.cli status
    python -m homeops.cli command house_a light living_room turn_on
    python -m homeops.cli validate  deploy/deployment.yaml   # static, offline lint (exit 1 on fail)
    python -m homeops.cli preflight deploy/deployment.yaml   # read-only live commissioning checks
    python -m homeops.cli serve     deploy/deployment.yaml   # long-running service (systemd unit)
"""
from __future__ import annotations
import sys
from .bootstrap import build_world
from .permissions import Intent, Operator


def status(world) -> None:
    for hid, house in world.houses.items():
        print(f"== {hid} ({house.alias}) mode={house.mode} wan={'up' if house.wan_up else 'DOWN'} "
              f"grid={'up' if house.grid_up else 'DOWN'} ai_hold={house.ai_hold}")
        for sub in ("lock", "alarm", "water", "power", "battery", "generator", "light"):
            ents = [e for e in house.entities.values() if e.subsystem == sub]
            if ents:
                print("   " + sub + ": " + ", ".join(f"{e.name}={e.state}" for e in ents))


def main(argv: list[str]) -> int:
    world = build_world()
    if not argv or argv[0] == "status":
        status(world)
        return 0
    if argv[0] == "command" and len(argv) >= 5:
        house, subsystem, target, action = argv[1:5]
        r = world.router.execute(Intent(house, subsystem, target, action),
                                 Operator("owner", house, "cli"))
        print(f"{r.status}: {r.message}" + (f"  (confirm token: {r.confirm_token})" if r.confirm_token else ""))
        return 0
    if argv[0] in ("validate", "preflight", "serve") and len(argv) >= 2:
        from .deployment import load_deployment, validate_deployment, has_failures, render_results
        from .secrets import load_secrets
        dep = load_deployment(argv[1])
        secrets = load_secrets(dep.secrets_file)
        if argv[0] == "validate":
            res = validate_deployment(dep, dash_token_present=bool(secrets.get("HOMEOPS_DASH_TOKEN")))
            print(render_results(res, f"validate {argv[1]}"))
            return 1 if has_failures(res) else 0
        if argv[0] == "preflight":
            from .preflight import run_preflight, render_report, failed
            checks = run_preflight(dep, secrets)
            print(render_report(checks))
            return 1 if failed(checks) else 0
        from .service import Service
        return Service(dep, secrets=secrets).run()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
