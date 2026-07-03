"""Part 8: the deployment descriptor is linted fail-closed before anything runs."""
import os

from homeops.bootstrap import DEFAULT_CONFIG, controllable_entities
from homeops.deployment import (DeploymentConfig, has_failures, load_deployment,
                                validate_deployment)
from homeops.model import load_houses


def _full_map():
    return {eid: f"mapped.{i}" for i, eid in
            enumerate(controllable_entities(load_houses(DEFAULT_CONFIG)))}


def test_sim_defaults_validate_clean():
    res = validate_deployment(DeploymentConfig())
    assert not has_failures(res)


def test_real_mode_requires_total_entity_map():
    dep = DeploymentConfig(mode="real", audit_path="/tmp/a.jsonl")
    res = validate_deployment(dep)
    assert has_failures(res)
    assert any(c == "entity_map.coverage" and s == "fail" for s, c, _ in res)


def test_real_mode_full_map_passes_coverage():
    dep = DeploymentConfig(mode="real", audit_path="/tmp/a.jsonl", entity_map=_full_map())
    res = validate_deployment(dep)
    assert not any(c == "entity_map.coverage" and s == "fail" for s, c, _ in res)


def test_two_houses_cannot_collapse_onto_one_real_entity():
    m = _full_map()
    ids = list(m)
    m[ids[0]] = m[ids[1]] = "light.shared"          # the classic A/B collapse
    dep = DeploymentConfig(mode="real", audit_path="/tmp/a.jsonl", entity_map=m)
    res = validate_deployment(dep)
    assert any(c == "entity_map.distinct" and s == "fail" for s, c, _ in res)


def test_real_mode_requires_persistent_audit():
    dep = DeploymentConfig(mode="real", entity_map=_full_map())
    res = validate_deployment(dep)
    assert any(c == "audit.persistence" and s == "fail" for s, c, _ in res)


def test_nonloopback_dashboard_without_token_refused():
    dep = DeploymentConfig(dash_host="0.0.0.0")
    assert has_failures(validate_deployment(dep, dash_token_present=False))
    assert not has_failures(validate_deployment(dep, dash_token_present=True))


def test_loopback_dashboard_needs_no_token():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert not has_failures(validate_deployment(DeploymentConfig(dash_host=host)))


def test_descriptor_loads_and_resolves_relative_paths(tmp_path):
    y = tmp_path / "dep.yaml"
    y.write_text("deployment:\n  mode: sim\n  audit_path: state/audit.jsonl\n")
    dep = load_deployment(str(y))
    assert dep.mode == "sim"
    assert dep.audit_path == str(tmp_path / "state" / "audit.jsonl")
    assert os.path.isabs(dep.audit_path)


def test_unknown_mode_fails():
    assert has_failures(validate_deployment(DeploymentConfig(mode="prod?")))
