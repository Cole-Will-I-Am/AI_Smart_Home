"""Commissioning certificate — a signed, verifiable record that an estate passed acceptance.

The tangible deliverable at handover. It runs the emergency drills against the real (or
simulated) world, records outcomes, snapshots the deployment's fail-closed posture, embeds the
safety-case result and the audit-chain head, and HMAC-signs the whole document. Anyone holding
the site key can re-verify the signature; any later edit invalidates it. This is what an insurer
files and an integrator hands the owner — evidence, not assurance.

The signature proves integrity/provenance of the CERTIFICATE, not the safety of the house; the
drills and the safety case carry the safety meaning. Honest by construction.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import datetime as _dt
import hashlib
import hmac
import json

from .deployment import DeploymentConfig, validate_deployment
from .safety_case import verify_safety_case


@dataclass
class Drill:
    name: str
    entity: str
    expected: str
    observed: str
    passed: bool


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def run_acceptance_drills(world) -> list[Drill]:
    """Replay the life-safety drills and record entity outcomes. Uses the simulator's scenarios;
    against a real world these drive real devices, so a passing certificate means the physical
    fail-safes actually fired."""
    from .simulator import scenarios
    drills: list[Drill] = []

    def check(name, entity, expected):
        observed = world.state.get_state(entity)
        drills.append(Drill(name, entity, expected, str(observed), observed == expected))

    # each drill acts on house_a in the reference world; a real deployment iterates its houses
    scenarios.leak(world, "house_a"); world.tick(2)
    check("water-leak → main valve closes", "house_a.water.main_valve", "closed")

    scenarios.fire_co(world, "house_a"); world.tick(2)
    check("fire/CO → designated egress unlocks", "house_a.lock.egress_side", "unlocked")
    check("fire/CO → HVAC stops (no smoke spread)", "house_a.hvac.main", "off")

    scenarios.intrusion(world, "house_a"); world.tick(2)
    check("intrusion → perimeter lighting on", "house_a.light.exterior_front", "on")
    check("intrusion → front camera records", "house_a.camera.front_door", "event")

    return drills


@dataclass
class Certificate:
    estate: str
    issued_at: str
    mode: str
    drills: list[dict]
    safety_case_ok: bool
    safety_claims: int
    deployment_posture: list[dict]
    audit_chain_head: str
    audit_chain_ok: bool
    version: str = "1.0"
    signature: str | None = None
    _signed_fields: tuple = field(default=(), repr=False)

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("signature", None)
        d.pop("_signed_fields", None)
        return d

    def canonical(self) -> str:
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))


def _sign(canonical: str, key: str) -> str:
    return hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def issue_certificate(world, dep: DeploymentConfig, signing_key: str,
                      estate: str = "Estate", run_safety_case: bool = True,
                      dash_token_present: bool = False) -> Certificate:
    drills = run_acceptance_drills(world)
    sc = verify_safety_case(run=run_safety_case)
    posture = [{"severity": s, "check": c, "detail": d}
               for s, c, d in validate_deployment(dep, dash_token_present=dash_token_present)]
    chain_ok, _ = world.audit.verify_chain()
    cert = Certificate(
        estate=estate, issued_at=_now(), mode=dep.mode,
        drills=[asdict(x) for x in drills],
        safety_case_ok=sc.ok, safety_claims=len(sc.results),
        deployment_posture=posture,
        audit_chain_head=world.audit.head, audit_chain_ok=chain_ok,
    )
    cert.signature = _sign(cert.canonical(), signing_key)
    return cert


def verify_certificate(cert_json: str, signing_key: str) -> tuple[bool, str]:
    """Re-verify a certificate's signature. Returns (ok, reason)."""
    try:
        data = json.loads(cert_json)
    except ValueError as e:
        return False, f"unparseable certificate: {e}"
    sig = data.pop("signature", None)
    if not sig:
        return False, "certificate has no signature"
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    expected = _sign(canonical, signing_key)
    if not hmac.compare_digest(sig, expected):
        return False, "signature mismatch — certificate was altered or the key is wrong"
    return True, "signature valid"


def all_drills_passed(cert: Certificate) -> bool:
    return all(d["passed"] for d in cert.drills)


def render_certificate(cert: Certificate) -> str:
    lines = [f"HouseCommand Commissioning Certificate v{cert.version}",
             f"  estate:     {cert.estate}",
             f"  issued:     {cert.issued_at}   mode: {cert.mode}",
             f"  audit head: {cert.audit_chain_head[:16]}…  chain_ok={cert.audit_chain_ok}",
             f"  safety case: {'UPHELD' if cert.safety_case_ok else 'FAILED'} ({cert.safety_claims} claims)",
             "  acceptance drills:"]
    for d in cert.drills:
        lines.append(f"    [{'PASS' if d['passed'] else 'FAIL'}] {d['name']} "
                     f"→ {d['entity']}={d['observed']} (want {d['expected']})")
    fails = [p for p in cert.deployment_posture if p["severity"] == "fail"]
    lines.append(f"  deployment posture: {len(fails)} failing checks")
    lines.append(f"  signature:  {cert.signature[:32]}…" if cert.signature else "  signature:  UNSIGNED")
    return "\n".join(lines)
