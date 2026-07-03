"""Tiny operator CLI: status, commands, and the ops lifecycle (validate -> preflight -> serve).

    python -m homeops.cli status
    python -m homeops.cli command house_a light living_room turn_on
    python -m homeops.cli ask [house_a]                       # resident chat (confirm/deny/house X/exit)
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


def ask(world, house: str) -> int:
    """Resident chat REPL. Uses the live Claude client when ANTHROPIC_API_KEY is set,
    otherwise the deterministic fallback (still engine-gated, still audited)."""
    import os
    from .ai.session import ChatSession
    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
        except ImportError:
            print("(anthropic package missing — running on the deterministic fallback)")
    elif os.environ.get("OPENAI_API_KEY"):
        try:
            import openai
            client = openai.OpenAI()
        except ImportError:
            print("(openai package missing — running on the deterministic fallback)")
    session = ChatSession(world, client=client, active_house=house,
                          model=os.environ.get("HOMEOPS_AI_MODEL"))
    if client is not None:
        print(f"(model: {session.provider.name}/{session.model})")
    print(f"HouseCommand chat — active house: {house}. Commands: confirm [n] · deny [n] · house <id> · exit")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        low = line.lower()
        if low in ("exit", "quit"):
            return 0
        if low.startswith("house "):
            try:
                session.switch_house(line.split(None, 1)[1])
                print(f"(active house -> {session.active_house})")
            except KeyError as e:
                print(f"(unknown house {e})")
            continue
        if low.startswith(("confirm", "deny")):
            parts = low.split()
            idx = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
            r = session.confirm(idx) if parts[0] == "confirm" else session.deny(idx)
            print(f"  -> {r['status']}: {r['message']}")
            continue
        out = session.ask(line)
        for a in out.get("actions", []):
            label = a.get("intent", {}).get("action") or a.get("cmd") or a.get("tool", "?")
            print(f"  [{a.get('status','?')}] {label}: {a.get('message','')}")
        if out.get("final"):
            print(f"hc> {out['final']}")
        for i, pnd in enumerate(out.get("pending", []), 1):
            print(f"  ⏳ awaiting confirmation {i}: {pnd}   (type: confirm {i})")


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
    if argv[0] == "ask":
        return ask(world, argv[1] if len(argv) > 1 else "house_a")
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
