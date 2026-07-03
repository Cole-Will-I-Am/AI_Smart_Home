"""`python -m homeops.gateway serve <deployment.yaml>` — the WRITE-path server.

Fail-closed exposure, mirroring homeops.service: a non-loopback bind without HOMEOPS_GATEWAY_TOKEN
refuses to start. Devices still authenticate per-request with their own bearer tokens; the
gateway secret is a second, transport-level gate for non-loopback exposure.
"""
from __future__ import annotations
import sys

from ..deployment import load_deployment, validate_deployment, has_failures, render_results
from ..secrets import load_secrets
from ..service import build_service_world
from ..deployment import _is_loopback
from .core import Gateway
from .api import serve


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "serve":
        print(__doc__)
        return 2
    dep = load_deployment(argv[1])
    secrets = load_secrets(dep.secrets_file)
    results = validate_deployment(dep, dash_token_present=bool(secrets.get("HOMEOPS_DASH_TOKEN")))
    if has_failures(results):
        print(render_results(results, "validate"), file=sys.stderr)
        return 1
    gw_token = secrets.get("HOMEOPS_GATEWAY_TOKEN")
    if not _is_loopback(dep.dash_host) and not gw_token:
        print("refusing non-loopback gateway bind without HOMEOPS_GATEWAY_TOKEN", file=sys.stderr)
        return 1
    world = build_service_world(dep, secrets)
    gw = Gateway(world)
    httpd = serve(gw, host=dep.dash_host, port=dep.dash_port + 1)
    print(f"homeops gateway up: write path http://{dep.dash_host}:{dep.dash_port + 1}/v1 "
          f"(per-device bearer{', token-gated' if gw_token else ''})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
