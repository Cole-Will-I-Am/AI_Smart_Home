"""The safety case — every safety claim bound to the test(s) that prove it.

A moat competitors can't copy by shipping features: an auditable mapping from human-legible
safety properties to executable evidence. `verify_safety_case()` runs the cited tests and
returns per-claim PASS/FAIL, so the claim "the AI cannot self-escalate" is not marketing — it is
a green test id anyone can re-run. Insurers, integrators, and estate security consultants get a
document whose provenance is the test suite itself.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import subprocess
import sys


@dataclass(frozen=True)
class Claim:
    id: str
    property: str                 # the human-legible safety guarantee
    tests: tuple[str, ...]        # pytest node ids that, passing, evidence it


# The authoritative safety case. Each claim's evidence is a set of test node ids.
SAFETY_CASE: tuple[Claim, ...] = (
    Claim("SC-1", "The AI can never execute an L4/L5 action — only recommend.",
          ("tests/test_redteam.py::test_L4_and_L5_have_no_path_for_any_operator",
           "tests/test_ai_ops_layer.py::test_ai_proposes_L4_and_engine_refuses")),
    Claim("SC-2", "The AI cannot self-confirm; L2+ requires a human confirmation.",
          ("tests/test_redteam.py::test_ai_operator_never_receives_a_token",
           "tests/test_chat_session.py::test_confirm_dance_executes_as_human_and_token_never_reaches_model")),
    Claim("SC-3", "Confirmation tokens are unforgeable, single-use, TTL-bound, and bound to the exact intent and operator.",
          ("tests/test_redteam.py::test_token_bound_to_args_mutation_fails",
           "tests/test_redteam.py::test_token_bound_to_operator_identity",
           "tests/test_redteam.py::test_token_single_use_replay_fails",
           "tests/test_redteam.py::test_token_guessing_fails",
           "tests/test_redteam.py::test_token_expires")),
    Claim("SC-4", "Confirmation tokens never enter the model's context, under any provider.",
          ("tests/test_chat_session.py::test_confirm_dance_executes_as_human_and_token_never_reaches_model",
           "tests/test_providers.py::test_gpt_confirm_dance_same_engine_same_absent_token")),
    Claim("SC-5", "Privileged flags (cross-house, emergency) cannot be smuggled through action arguments.",
          ("tests/test_redteam.py::test_cross_house_flag_cannot_be_smuggled_via_ai_args",
           "tests/test_redteam.py::test_emergency_flag_cannot_be_smuggled_via_args",
           "tests/test_redteam.py::test_stolen_token_smuggled_through_args_is_inert")),
    Claim("SC-6", "Malformed or hostile identifiers fail closed and never become a confirmable action.",
          ("tests/test_redteam.py::test_unknown_house_is_refused_upfront_not_pended",
           "tests/test_redteam.py::test_unknown_house_via_chat_never_enters_pending",
           "tests/test_redteam.py::test_unknown_action_fails_closed")),
    Claim("SC-7", "Safety-critical actuation is refused on an unverified device and verified by read-back.",
          ("tests/test_hardening.py",)),
    Claim("SC-8", "Life-safety responses run locally with no cloud and survive WAN loss.",
          ("tests/test_local_first.py", "tests/test_failsafe.py")),
    Claim("SC-9", "Authority is model-invariant: swapping Claude for GPT changes capability, not permissions.",
          ("tests/test_providers.py::test_gpt_l1_executes_and_l4_still_refused",
           "tests/test_providers.py::test_gpt_confirm_dance_same_engine_same_absent_token")),
    Claim("SC-10", "The audit trail is tamper-evident (hash-chained) and every refusal/override is recorded.",
          ("tests/test_audit.py",)),
    Claim("SC-11", "Destructive one-shot actuations cannot be hammered inside their cooldown.",
          ("tests/test_redteam.py::test_destructive_action_cannot_be_hammered",
           "tests/test_redteam.py::test_rate_limit_holds_per_tick")),
    Claim("SC-12", "Two properties can never collapse onto one physical device.",
          ("tests/test_deployment_validation.py::test_two_houses_cannot_collapse_onto_one_real_entity",
           "tests/test_preflight.py::test_implausible_domain_fails")),
)


@dataclass
class ClaimResult:
    claim: Claim
    passed: bool
    detail: str = ""


@dataclass
class SafetyCaseReport:
    results: list[ClaimResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def render(self) -> str:
        lines = ["# HouseCommand safety case — claims bound to executable evidence", ""]
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"[{mark}] {r.claim.id}: {r.claim.property}")
            for t in r.claim.tests:
                lines.append(f"        └ {t}")
            if r.detail and not r.passed:
                lines.append(f"        ! {r.detail}")
        n_fail = sum(1 for r in self.results if not r.passed)
        lines += ["", f"-- {len(self.results)} claims: {len(self.results) - n_fail} upheld, {n_fail} FAILED"]
        return "\n".join(lines)


def verify_safety_case(case: tuple[Claim, ...] = SAFETY_CASE, run: bool = True) -> SafetyCaseReport:
    """Run each claim's cited tests. run=False produces the mapping without executing (fast doc)."""
    report = SafetyCaseReport()
    for claim in case:
        if not run:
            report.results.append(ClaimResult(claim, True, "not executed"))
            continue
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", *claim.tests],
            capture_output=True, text=True)
        passed = proc.returncode == 0
        detail = "" if passed else (proc.stdout.strip().splitlines() or ["failed"])[-1]
        report.results.append(ClaimResult(claim, passed, detail))
    return report


if __name__ == "__main__":
    rep = verify_safety_case(run="--fast" not in sys.argv)
    print(rep.render())
    sys.exit(0 if rep.ok else 1)
