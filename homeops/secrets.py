"""Fail-closed secrets loading for real deployments.

Secrets (HA token, OPNsense key/secret, dashboard bearer token) never live in the repo, the
houses config, or the deployment descriptor. They come from process environment variables or a
KEY=VALUE env file whose permissions are ENFORCED: the file must be a regular file, owned by the
invoking user (or root), and readable by owner only (no group/other bits). A world-readable
secrets file is a refusal, not a warning — the process will not start.

Precedence: process environment overrides the file (lets systemd `Environment=`/drop-ins win).
"""
from __future__ import annotations
import os
import stat

# The keys a real-mode deployment may use. Only HA_URL/HA_TOKEN are strictly required;
# the rest are conditional (see required_for_mode).
KNOWN_KEYS = (
    "HOMEOPS_HA_URL", "HOMEOPS_HA_TOKEN",
    "HOMEOPS_OPN_URL", "HOMEOPS_OPN_KEY", "HOMEOPS_OPN_SECRET",
    "HOMEOPS_DASH_TOKEN",
)


class SecretsError(RuntimeError):
    """Raised when secrets are missing or stored insecurely. Always fail-closed."""


def check_file_permissions(path: str) -> None:
    """Refuse group/other-readable secrets. Raises SecretsError; returns None if acceptable."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise SecretsError(f"secrets path {path!r} is not a regular file")
    if st.st_uid not in (os.geteuid(), 0):
        raise SecretsError(f"secrets file {path!r} must be owned by the service user or root")
    if st.st_mode & 0o077:
        raise SecretsError(
            f"secrets file {path!r} is group/other-accessible "
            f"(mode {stat.S_IMODE(st.st_mode):04o}); require 0600 — refusing to start")


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; '#' comments and blank lines ignored; optional single/double quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_secrets(path: str | None = None, environ: dict | None = None) -> dict[str, str]:
    """Merge secrets file (if given) with process env; env wins. Enforces file permissions."""
    environ = os.environ if environ is None else environ
    merged: dict[str, str] = {}
    if path:
        if not os.path.exists(path):
            raise SecretsError(f"secrets file {path!r} does not exist")
        check_file_permissions(path)
        with open(path) as f:
            merged.update(parse_env_file(f.read()))
    for k in KNOWN_KEYS:
        if environ.get(k):
            merged[k] = environ[k]
    return merged


def required_for_mode(mode: str, event_bridge: bool = False, opnsense: bool = False) -> tuple[str, ...]:
    if mode != "real":
        return ()
    req = ["HOMEOPS_HA_URL", "HOMEOPS_HA_TOKEN"]
    if opnsense:
        req += ["HOMEOPS_OPN_URL", "HOMEOPS_OPN_KEY", "HOMEOPS_OPN_SECRET"]
    return tuple(req)


def require(secrets: dict[str, str], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if not secrets.get(k)]
    if missing:
        raise SecretsError(f"missing required secrets: {', '.join(missing)} "
                           f"(set in environment or the 0600 secrets file)")
