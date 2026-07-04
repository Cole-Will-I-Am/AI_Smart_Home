"""Tiny operator CLI: status, commands, and the ops lifecycle (validate -> preflight -> serve).

    homeops demo                                             # 3-minute guided tour, no hardware
    homeops init [1|2|3|4]                                   # write a deployment.yaml from a preset
    python -m homeops.cli status
    python -m homeops.cli command house_a light living_room turn_on
    homeops chat [house_a] [--model M] [--provider P] [--base-url URL] [--ollama M] [--timeout S]
                                                             # resident chat through ANY model you choose
    python -m homeops.cli ask [house_a]                       # alias for chat
    python -m homeops.cli soc [house_a]                      # Home-SOC situation report (readiness/incidents/drift)
    python -m homeops.cli twin [house_a]                     # estate digital twin (risk by room/subsystem/device)
    python -m homeops.cli safety-case                        # run the safety case (claims -> live tests)
    python -m homeops.cli certify <estate> <key> [deploy.yaml]  # signed commissioning certificate
    python -m homeops.cli validate  deploy/deployment.yaml   # static, offline lint (exit 1 on fail)
    python -m homeops.cli preflight deploy/deployment.yaml   # read-only live commissioning checks
    python -m homeops.cli serve     deploy/deployment.yaml   # long-running service (systemd unit)
"""
from __future__ import annotations
import sys
from .bootstrap import build_world
from .permissions import Intent, Operator


import os


# --- model selection: turn CLI flags + env into the ai: dict provider_from_config understands ---
# Precedence (highest first): explicit flags  >  HOMEOPS_AI_* env  >  ANTHROPIC/OPENAI key auto-detect
#   >  the deployment's ai: section  >  none (deterministic fallback). Fail-closed: a partial
# openai-compatible config raises a clear error rather than silently falling back.
def resolve_ai_config(args: dict, environ: dict | None = None, dep_ai: dict | None = None) -> dict:
    env = os.environ if environ is None else environ
    # 2a. explicit flags win
    if args.get("ollama") is not None:
        return {"provider": "openai-compatible", "model": args["ollama"],
                "base_url": args.get("base_url") or "http://127.0.0.1:11434/v1",
                # local CPU inference: first turn includes model load + a long snapshot prefill
                "timeout": float(args.get("timeout") or env.get("HOMEOPS_AI_TIMEOUT") or 300.0)}
    if args.get("provider") or args.get("base_url") or args.get("model"):
        ai = {"provider": args.get("provider")
              or ("openai-compatible" if args.get("base_url") else None),
              "model": args.get("model")}
        if args.get("base_url"):
            ai["base_url"] = args["base_url"]
        if args.get("timeout") or env.get("HOMEOPS_AI_TIMEOUT"):
            ai["timeout"] = float(args.get("timeout") or env["HOMEOPS_AI_TIMEOUT"])
        if ai["provider"]:
            return ai
    # 2b. HOMEOPS_AI_* environment
    if env.get("HOMEOPS_AI_PROVIDER") or env.get("HOMEOPS_AI_BASE_URL"):
        ai = {"provider": env.get("HOMEOPS_AI_PROVIDER")
              or ("openai-compatible" if env.get("HOMEOPS_AI_BASE_URL") else None),
              "model": args.get("model") or env.get("HOMEOPS_AI_MODEL")}
        if env.get("HOMEOPS_AI_BASE_URL"):
            ai["base_url"] = env["HOMEOPS_AI_BASE_URL"]
        if env.get("HOMEOPS_AI_TIMEOUT"):
            ai["timeout"] = float(env["HOMEOPS_AI_TIMEOUT"])
        if ai["provider"]:
            return ai
    # 2c. bare SDK key auto-detect (convenience — the classic path)
    if env.get("ANTHROPIC_API_KEY"):
        return {"provider": "anthropic", "model": args.get("model") or env.get("HOMEOPS_AI_MODEL")}
    if env.get("OPENAI_API_KEY"):
        return {"provider": "openai", "model": args.get("model") or env.get("HOMEOPS_AI_MODEL")}
    # 2d. deployment ai: section, else none
    if dep_ai:
        return dict(dep_ai)
    return {"provider": "none"}


def build_chat_session(world, args: dict, environ: dict | None = None, house: str = "house_a"):
    """Resolve a provider from flags/env and build a model-agnostic ChatSession. Returns
    (session, banner). No network is touched here — providers construct lazily."""
    from .ai.providers import provider_from_config
    from .ai.session import ChatSession
    ai = resolve_ai_config(args, environ)
    provider, model = provider_from_config(ai)     # raises on a partial/invalid config (fail-closed)
    session = ChatSession(world, client=provider, active_house=house, model=model)
    if provider is None:
        banner = "(no model configured — deterministic fallback; still engine-gated and audited)"
    else:
        banner = f"(model: {session.provider.name}/{session.model})"
    return session, banner


def _parse_chat_args(argv: list[str]) -> tuple[str, dict]:
    """argv after the subcommand -> (house, flags). Accepts a bare house positional plus
    --model/--provider/--base-url/--ollama."""
    house, args = "house_a", {}
    it = iter(argv)
    for tok in it:
        if tok in ("--model", "--provider", "--base-url", "--ollama", "--timeout"):
            key = tok.lstrip("-").replace("-", "_")
            args[key] = next(it, None)
        elif not tok.startswith("-"):
            house = tok
    return house, args


def status(world) -> None:
    for hid, house in world.houses.items():
        print(f"== {hid} ({house.alias}) mode={house.mode} wan={'up' if house.wan_up else 'DOWN'} "
              f"grid={'up' if house.grid_up else 'DOWN'} ai_hold={house.ai_hold}")
        for sub in ("lock", "alarm", "water", "power", "battery", "generator", "light"):
            ents = [e for e in house.entities.values() if e.subsystem == sub]
            if ents:
                print("   " + sub + ": " + ", ".join(f"{e.name}={e.state}" for e in ents))


def chat(world, house: str, args: dict | None = None) -> int:
    """Resident chat REPL, model-agnostic: runs through whatever LLM the flags/env select
    (Claude, GPT, or any OpenAI-compatible/Ollama endpoint), else a deterministic fallback.
    Confirmations show the ENGINE's attested effect — ground truth, not the model's prose."""
    try:
        session, banner = build_chat_session(world, args or {}, house=house)
    except ValueError as e:
        print(f"model configuration error: {e}")
        return 2
    print(banner)
    _print_first_run_hint(session)
    print(f"HouseCommand chat — active house: {house}. "
          "Commands: confirm [n] · deny [n] · house <id> · exit")
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
        try:
            out = session.ask(line)
        except Exception as e:   # a flaky/unauthorized endpoint must not kill the session
            print(f"  (model error: {type(e).__name__}: {str(e)[:160]})")
            print("  (the house is unaffected — no intent was proposed; try again or switch model)")
            continue
        if out.get("degraded"):
            print(f"  (degraded: {out['degraded']})")
        for a in out.get("actions", []):
            label = a.get("intent", {}).get("action") or a.get("cmd") or a.get("tool", "?")
            print(f"  [{a.get('status','?')}] {label}: {a.get('message','')}")
        if out.get("final"):
            print(f"hc> {out['final']}")
        # Surface the engine's signed effect sentence for each pending item — the deed, not the prose.
        for i, p in enumerate(session.pending, 1):
            print(f"  \u23f3 confirm {i}: {p.effect}   (type: confirm {i})")


_SIM_HINT = (
    "HouseCommand is running in SIMULATION \u2014 no real devices will be controlled.\n"
    "Try:\n"
    '  \u2022 "Turn on the living room lights"\n'
    '  \u2022 "Unlock the front door"          (watch it ask you to confirm)\n'
    '  \u2022 "What happens if there is a leak?"'
)


def _print_first_run_hint(session) -> None:
    """Reassure a first-time user that nothing physical can happen in sim/fallback mode."""
    if getattr(session, "provider", None) is None:
        print(_SIM_HINT)


def demo(_world=None) -> int:
    """A 3-minute guided tour \u2014 no network, no hardware. Shows the one idea that matters:
    the AI (and every surface) can only PROPOSE; a deterministic engine decides and logs."""
    from .simulator import scenarios
    w = build_world()
    owner = Operator("owner", "house_a", "you")
    print("HouseCommand demo \u2014 simulated estate; nothing real is touched.\n")

    print("1) A routine action (L1) executes directly:")
    r = w.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner)
    print(f"   turn on living-room lights -> {r.status}: {r.message}")

    print("\n2) A security action (L2) is NOT taken without your confirmation:")
    r = w.router.execute(Intent("house_a", "lock", "front_door", "unlock"), owner)
    print(f"   unlock front door -> {r.status}: {r.message}")

    print("\n3) The house protects itself locally \u2014 a confirmed leak shuts the main valve")
    print("   (no AI, no cloud; two independent signals required):")
    scenarios.leak(w, "house_a"); w.tick(2)
    print(f"   main water valve -> {w.state.get_state('house_a.water.main_valve')}")

    print("\n4) The AI proposes a dangerous action (L4) \u2014 the engine has no way to run it:")
    r = w.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"),
                         Operator("ai", "house_a", "ai-ops"))
    print(f"   AI: unlock for an unknown person -> {r.status}: {r.message}")

    ok, _ = w.audit.verify_chain()
    print(f"\nEvery step above was logged. Tamper-evident audit chain intact: {ok}.")
    print("\nNext:  homeops chat        (talk to the house)")
    print("       homeops status      (see everything at a glance)")
    return 0


_PRESETS = {
    "1": ("sim",         "Simulated demo \u2014 no hardware, safe to explore"),
    "2": ("ha",          "Home Assistant only"),
    "3": ("ha_opnsense", "Home Assistant + OPNsense (network quarantine / firewall)"),
    "4": ("estate",      "Estate / multiple properties"),
}


def _deployment_yaml(kind: str) -> str:
    head = ("# Generated by `homeops init`. Lifecycle: validate -> preflight -> serve.\n"
            "# Secrets NEVER live here (they go in the 0600 secrets file below).\n"
            "deployment:\n")
    common = ("  houses_config: config/houses.example.yaml\n"
              "  audit_path: /var/lib/homeops/audit.jsonl\n"
              "  state_dir:  /var/lib/homeops\n"
              "  dash_host: 127.0.0.1        # a non-loopback bind REQUIRES HOMEOPS_DASH_TOKEN\n"
              "  dash_port: 8787\n"
              "  secrets_file: /etc/homeops/secrets.env\n")
    stub = ("  # Map EVERY controllable entity explicitly, or startup fails closed:\n"
            "  entity_map: {}\n"
            "  #   house_a.light.living_room: light.hc_a_living_room\n")
    if kind == "sim":
        # sim needs nothing else: in-memory audit, loopback dashboard, no secrets.
        return head + "  mode: sim\n  houses_config: config/houses.example.yaml\n"
    if kind == "ha":
        return head + "  mode: real\n" + common + "  verify_tls: true\n" + stub
    if kind == "ha_opnsense":
        return head + "  mode: real\n" + common + "  verify_tls: true\n  opnsense: true\n" + stub
    return (head + "  mode: real\n" + common +
            "  verify_tls: true\n  opnsense: true\n  event_bridge: true\n" + stub)


def init(argv: list[str]) -> int:
    """Write a starter deployment.yaml from a preset. Non-destructive: refuses to clobber."""
    import os as _os
    choice = argv[0] if argv else None
    if choice is None:
        print("Choose a starting point:")
        for k, (_, label) in _PRESETS.items():
            print(f"  {k}. {label}")
        try:
            choice = input("preset [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            choice = "1"
    if choice not in _PRESETS:
        print(f"unknown preset {choice!r} (expected 1-4)")
        return 1
    kind, label = _PRESETS[choice]
    path = "deployment.yaml"
    if _os.path.exists(path):
        print(f"{path} already exists \u2014 leaving it untouched.")
        return 1
    with open(path, "w") as f:
        f.write(_deployment_yaml(kind))
    print(f"wrote {path}  ({label})")
    if kind == "sim":
        print("next:  homeops demo        # or: homeops validate deployment.yaml")
    else:
        print("next:  edit entity_map + /etc/homeops/secrets.env, then:")
        print("       homeops validate deployment.yaml   # offline lint")
        print("       homeops preflight deployment.yaml  # read-only live checks")
    return 0


def main(argv: list[str]) -> int:
    world = build_world()
    if not argv or argv[0] == "status":
        status(world)
        return 0
    if argv[0] == "demo":
        return demo()
    if argv[0] == "init":
        return init(argv[1:])
    if argv[0] == "command" and len(argv) >= 5:
        house, subsystem, target, action = argv[1:5]
        r = world.router.execute(Intent(house, subsystem, target, action),
                                 Operator("owner", house, "cli"))
        print(f"{r.status}: {r.message}" + (f"  (confirm token: {r.confirm_token})" if r.confirm_token else ""))
        return 0
    if argv[0] in ("chat", "ask"):
        house, args = _parse_chat_args(argv[1:])
        return chat(world, house, args)
    if argv[0] == "soc":
        from . import soc as _soc
        import json as _json
        house = argv[1] if len(argv) > 1 else "house_a"
        print(_json.dumps(_soc.situation_report(world, house), indent=2, default=str))
        return 0
    if argv[0] == "twin":
        from .twin import EstateTwin
        import json as _json
        house = argv[1] if len(argv) > 1 else "house_a"
        print(_json.dumps(EstateTwin(world).to_dict(house), indent=2, default=str))
        return 0
    if argv[0] == "safety-case":
        from .safety_case import verify_safety_case
        rep = verify_safety_case(run="--fast" not in argv)
        print(rep.render())
        return 0 if rep.ok else 1
    if argv[0] == "certify" and len(argv) >= 3:
        from .certificate import issue_certificate, render_certificate, all_drills_passed
        from .deployment import load_deployment, DeploymentConfig
        import json as _json
        estate, key = argv[1], argv[2]
        dep = load_deployment(argv[3]) if len(argv) > 3 else DeploymentConfig()
        cert = issue_certificate(world, dep, signing_key=key, estate=estate,
                                 run_safety_case="--fast" not in argv)
        print(render_certificate(cert))
        out_path = f"certificate-{estate.replace(' ', '_').lower()}.json"
        with open(out_path, "w") as f:
            _json.dump({**cert.payload(), "signature": cert.signature}, f, indent=2)
        print(f"\n  written: {out_path}  (verify with the same key)")
        return 0 if (all_drills_passed(cert) and cert.safety_case_ok) else 1
    if argv[0] in ("validate", "preflight", "serve") and len(argv) >= 2:
        from .deployment import load_deployment, validate_deployment, has_failures, render_results
        from .secrets import load_secrets
        try:
            dep = load_deployment(argv[1])
        except FileNotFoundError:
            print(f"deployment file not found: {argv[1]!r} "
                  "(start from deploy/deployment.example.yaml)")
            return 1
        except Exception as e:   # malformed YAML/schema — refuse cleanly, never traceback
            print(f"invalid deployment file {argv[1]!r}: {type(e).__name__}: {e}")
            return 1
        from .secrets import SecretsError
        try:
            secrets = load_secrets(dep.secrets_file)
        except SecretsError as e:
            print(f"secrets error: {e} (see deploy/secrets.example.env)")
            return 1
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


def main_entry() -> int:
    """Console-script entry point (`homeops ...`)."""
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
