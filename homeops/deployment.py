"""Deployment descriptor + config validation — the one file an installer edits.

A deployment YAML pins everything operational: mode (sim|real), the houses config, the audit
path, the dashboard bind/port, entity/event maps for the real adapters, and where secrets live.
Secrets themselves NEVER appear here (see homeops.secrets). `validate_deployment()` is the
static gate: it re-runs the fail-closed checks (strict entity-map coverage, duplicate real
entities, loopback-vs-token dashboard rule) without touching the network, so an installer can
lint a deployment before ever pointing it at a house. Network-touching checks live in
homeops.preflight.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import ipaddress
import os
import urllib.parse

import yaml

from .bootstrap import DEFAULT_CONFIG, controllable_entities
from .model import load_houses


@dataclass
class DeploymentConfig:
    mode: str = "sim"                       # sim | real
    houses_config: str = DEFAULT_CONFIG
    audit_path: str | None = None           # None = in-memory (sim/demo only)
    state_dir: str | None = None            # heartbeat/status files
    dash_host: str = "127.0.0.1"
    dash_port: int = 8787
    tick_seconds: float = 2.0               # sim-mode world tick / real-mode housekeeping cadence
    verify_tls: bool = True
    opnsense: bool = False                  # real mode: is an OPNsense adapter part of this deployment?
    event_bridge: bool = False              # real mode: run the HA WebSocket bridge?
    secrets_file: str | None = None
    entity_map: dict[str, str] = field(default_factory=dict)
    event_map: dict = field(default_factory=dict)
    ai: dict = field(default_factory=dict)   # BYO-model plug (Part 17); see providers.provider_from_config
    source_path: str | None = None


def load_deployment(path: str) -> DeploymentConfig:
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    dep = doc.get("deployment", doc)
    base = os.path.dirname(os.path.abspath(path))

    def _p(v):  # resolve paths relative to the deployment file
        if not v:
            return v
        return v if os.path.isabs(v) else os.path.join(base, v)

    cfg = DeploymentConfig(
        mode=str(dep.get("mode", "sim")),
        houses_config=_p(dep.get("houses_config")) or DEFAULT_CONFIG,
        audit_path=_p(dep.get("audit_path")),
        state_dir=_p(dep.get("state_dir")),
        dash_host=str(dep.get("dash_host", "127.0.0.1")),
        dash_port=int(dep.get("dash_port", 8787)),
        tick_seconds=float(dep.get("tick_seconds", 2.0)),
        verify_tls=bool(dep.get("verify_tls", True)),
        opnsense=bool(dep.get("opnsense", False)),
        event_bridge=bool(dep.get("event_bridge", False)),
        secrets_file=_p(dep.get("secrets_file")),
        entity_map=dict(dep.get("entity_map") or {}),
        event_map=dict(dep.get("event_map") or {}),
        ai=dict(dep.get("ai") or {}),
        source_path=os.path.abspath(path),
    )
    return cfg


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_deployment(dep: DeploymentConfig, dash_token_present: bool = False) -> list[tuple[str, str, str]]:
    """Static, offline validation. Returns [(severity, check, detail)] where severity is
    'fail' | 'warn' | 'ok'. Callers refuse to proceed on any 'fail'."""
    out: list[tuple[str, str, str]] = []

    def ok(c, d):  out.append(("ok", c, d))
    def warn(c, d): out.append(("warn", c, d))
    def fail(c, d): out.append(("fail", c, d))

    # mode
    if dep.mode not in ("sim", "real"):
        fail("mode", f"unknown mode {dep.mode!r} (expected sim|real)")
        return out
    ok("mode", dep.mode)

    # houses config parses
    try:
        houses = load_houses(dep.houses_config)
        n_ent = sum(len(h.entities) for h in houses.values())
        ok("houses_config", f"{len(houses)} properties, {n_ent} entities ({dep.houses_config})")
    except Exception as e:                                  # noqa: BLE001 — report, don't crash a linter
        fail("houses_config", f"{dep.houses_config}: {e}")
        return out

    # real-mode entity map: total coverage (fail-closed) and no cross-house collapse
    if dep.mode == "real":
        need = controllable_entities(houses)
        unmapped = [e for e in need if e not in dep.entity_map]
        if unmapped:
            fail("entity_map.coverage",
                 f"{len(unmapped)}/{len(need)} controllable entities unmapped, e.g. {unmapped[:4]}")
        else:
            ok("entity_map.coverage", f"all {len(need)} controllable entities explicitly mapped")
        seen: dict[str, str] = {}
        dups = []
        for local, real in dep.entity_map.items():
            if real in seen:
                dups.append(f"{seen[real]} and {local} -> {real}")
            seen[real] = local
        if dups:
            fail("entity_map.distinct", "two local entities share one real entity: " + "; ".join(dups[:3]))
        else:
            ok("entity_map.distinct", "no two properties collapse onto one real entity")
        if not dep.audit_path:
            fail("audit.persistence", "real mode requires a persistent audit_path (evidence trail)")
        else:
            ok("audit.persistence", dep.audit_path)
        if not dep.verify_tls:
            warn("tls", "verify_tls=false — acceptable only for self-signed appliances on a trusted VLAN")
        else:
            ok("tls", "certificate verification on")

    # dashboard exposure rule: non-loopback bind REQUIRES a bearer token
    if _is_loopback(dep.dash_host):
        ok("dashboard.bind", f"loopback ({dep.dash_host}:{dep.dash_port})")
    elif dash_token_present:
        ok("dashboard.bind", f"non-loopback {dep.dash_host}:{dep.dash_port} with bearer token")
    else:
        fail("dashboard.bind",
             f"{dep.dash_host}:{dep.dash_port} is non-loopback and HOMEOPS_DASH_TOKEN is unset — "
             "refusing an unauthenticated network-facing surface")

    # BYO-model plug (Part 17): static checks only — never constructs a client
    if dep.ai:
        prov = str(dep.ai.get("provider", "none")).lower().replace("_", "-")
        if prov in ("none", "off", ""):
            ok("ai.provider", "none — deterministic-only (always safe)")
        elif prov in ("anthropic", "openai"):
            ok("ai.provider", f"{prov} (SDK; key via {dep.ai.get('key_env', prov.upper() + '_API_KEY')})")
        elif prov == "openai-compatible":
            base = dep.ai.get("base_url")
            if not base:
                fail("ai.base_url", "openai-compatible requires an explicit base_url")
            else:
                host = urllib.parse.urlparse(base).hostname or ""
                if base.startswith("http://") and not _is_loopback(host) and not dep.ai.get("allow_insecure"):
                    fail("ai.transport",
                         f"{base} is plaintext and non-loopback — the estate snapshot travels in every "
                         "request; use https or set ai.allow_insecure: true explicitly")
                else:
                    ok("ai.transport", base)
            if not dep.ai.get("model"):
                fail("ai.model", "openai-compatible requires an explicit model name")
            else:
                ok("ai.model", str(dep.ai["model"]))
        else:
            fail("ai.provider", f"unknown provider {dep.ai.get('provider')!r} "
                                "(anthropic | openai | openai-compatible | none)")
        b = dep.ai.get("l1_daily_budget")
        if b is not None and (not isinstance(b, int) or b < 1):
            fail("ai.l1_daily_budget", f"must be a positive integer, got {b!r}")
        elif b is not None:
            ok("ai.l1_daily_budget", f"{b} AI-originated L1 actuations/house/day")

    if dep.event_bridge and dep.mode == "real":
        try:
            import websocket  # noqa: F401
            ok("event_bridge.dep", "websocket-client available")
        except ImportError:
            fail("event_bridge.dep", "event_bridge=true but the optional websocket-client package is missing")
    return out


def has_failures(results: list[tuple[str, str, str]]) -> bool:
    return any(sev == "fail" for sev, _, _ in results)


def render_results(results: list[tuple[str, str, str]], title: str) -> str:
    icon = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"== {title} =="]
    for sev, check, detail in results:
        lines.append(f"[{icon[sev]}] {check}: {detail}")
    n_fail = sum(1 for s, _, _ in results if s == "fail")
    n_warn = sum(1 for s, _, _ in results if s == "warn")
    lines.append(f"-- {len(results)} checks: {n_fail} fail, {n_warn} warn")
    return "\n".join(lines)
