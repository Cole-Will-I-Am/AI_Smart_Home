"""Part 12: the moat artifacts — a machine-checkable safety case and a signed,
tamper-evident commissioning certificate. These are the deliverables competitors cannot
fake, because their provenance is the test suite and a site secret."""
import json


from homeops.deployment import DeploymentConfig
from homeops.certificate import (all_drills_passed, issue_certificate, render_certificate,
                                 run_acceptance_drills, verify_certificate)
from homeops.safety_case import SAFETY_CASE, Claim, verify_safety_case


# ---- safety case -------------------------------------------------------------------
def test_every_claim_cites_at_least_one_test():
    assert SAFETY_CASE
    for c in SAFETY_CASE:
        assert c.tests, f"{c.id} cites no evidence"
        assert c.id and c.property


def test_safety_case_mapping_is_stable_without_running():
    rep = verify_safety_case(run=False)
    assert len(rep.results) == len(SAFETY_CASE)
    assert "safety case" in rep.render()


def test_a_true_claim_passes_when_run():
    # a single, fast, self-contained claim executed for real
    claim = Claim("SC-TEST", "L4/L5 have no execution path",
                  ("tests/test_redteam.py::test_L4_and_L5_have_no_path_for_any_operator",))
    rep = verify_safety_case((claim,), run=True)
    assert rep.ok and rep.results[0].passed


def test_a_bogus_citation_fails_loudly():
    claim = Claim("SC-BOGUS", "this cannot be proven",
                  ("tests/test_redteam.py::test_this_does_not_exist",))
    rep = verify_safety_case((claim,), run=True)
    assert not rep.ok and not rep.results[0].passed


# ---- acceptance drills -------------------------------------------------------------
def test_drills_pass_against_reference_world(world):
    drills = run_acceptance_drills(world)
    assert drills and all(d.passed for d in drills), \
        [(d.name, d.observed, d.expected) for d in drills if not d.passed]


# ---- certificate signing & verification --------------------------------------------
def _cert(world, key="site-secret"):
    return issue_certificate(world, DeploymentConfig(), signing_key=key,
                             estate="Test Estate", run_safety_case=False)


def test_certificate_is_signed_and_reverifies(world):
    cert = _cert(world)
    assert cert.signature and all_drills_passed(cert)
    blob = json.dumps({**cert.payload(), "signature": cert.signature})
    ok, reason = verify_certificate(blob, "site-secret")
    assert ok, reason


def test_wrong_key_rejected(world):
    cert = _cert(world)
    blob = json.dumps({**cert.payload(), "signature": cert.signature})
    ok, _ = verify_certificate(blob, "not-the-key")
    assert not ok


def test_any_tamper_invalidates(world):
    cert = _cert(world)
    payload = cert.payload()
    payload["estate"] = "Forged Manor"                      # flip one field
    blob = json.dumps({**payload, "signature": cert.signature})
    ok, reason = verify_certificate(blob, "site-secret")
    assert not ok and "altered" in reason


def test_tampering_with_a_drill_result_invalidates(world):
    cert = _cert(world)
    payload = cert.payload()
    payload["drills"][0]["passed"] = True
    payload["drills"][0]["observed"] = "faked"              # pretend a failed drill passed
    blob = json.dumps({**payload, "signature": cert.signature})
    ok, _ = verify_certificate(blob, "site-secret")
    assert not ok                                           # the signature covers the drills


def test_unsigned_certificate_rejected(world):
    cert = _cert(world)
    blob = json.dumps(cert.payload())                       # no signature field
    ok, reason = verify_certificate(blob, "site-secret")
    assert not ok and "no signature" in reason


def test_certificate_embeds_audit_head_and_render(world):
    cert = _cert(world)
    assert cert.audit_chain_ok and len(cert.audit_chain_head) == 64
    text = render_certificate(cert)
    assert "Commissioning Certificate" in text and "Test Estate" in text
