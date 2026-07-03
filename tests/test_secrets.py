"""Part 8: secrets are fail-closed — insecure storage is a refusal, not a warning."""
import os

import pytest

from homeops.secrets import (SecretsError, check_file_permissions, load_secrets,
                             parse_env_file, require, required_for_mode)


def _write(tmp_path, mode, text="HOMEOPS_HA_TOKEN=abc\n"):
    p = tmp_path / "secrets.env"
    p.write_text(text)
    os.chmod(p, mode)
    return str(p)


def test_world_readable_secrets_refused(tmp_path):
    p = _write(tmp_path, 0o644)
    with pytest.raises(SecretsError, match="group/other"):
        load_secrets(p, environ={})


def test_group_readable_secrets_refused(tmp_path):
    with pytest.raises(SecretsError):
        check_file_permissions(_write(tmp_path, 0o640))


def test_0600_secrets_accepted_and_parsed(tmp_path):
    p = _write(tmp_path, 0o600, "# comment\nHOMEOPS_HA_URL='https://ha:8123'\nHOMEOPS_HA_TOKEN=tok\n")
    s = load_secrets(p, environ={})
    assert s["HOMEOPS_HA_URL"] == "https://ha:8123" and s["HOMEOPS_HA_TOKEN"] == "tok"


def test_environment_overrides_file(tmp_path):
    p = _write(tmp_path, 0o600, "HOMEOPS_HA_TOKEN=from_file\n")
    s = load_secrets(p, environ={"HOMEOPS_HA_TOKEN": "from_env"})
    assert s["HOMEOPS_HA_TOKEN"] == "from_env"


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(SecretsError, match="does not exist"):
        load_secrets(str(tmp_path / "nope.env"), environ={})


def test_no_file_env_only_is_fine():
    s = load_secrets(None, environ={"HOMEOPS_HA_URL": "https://x"})
    assert s == {"HOMEOPS_HA_URL": "https://x"}


def test_required_keys_by_mode():
    assert required_for_mode("sim") == ()
    req = required_for_mode("real", opnsense=True)
    assert "HOMEOPS_HA_TOKEN" in req and "HOMEOPS_OPN_SECRET" in req
    with pytest.raises(SecretsError, match="missing required"):
        require({"HOMEOPS_HA_URL": "x"}, required_for_mode("real"))


def test_parse_env_ignores_junk():
    d = parse_env_file("\n# c\nnot a pair\nK=v with spaces\nQ=\"quoted\"\n")
    assert d == {"K": "v with spaces", "Q": "quoted"}
