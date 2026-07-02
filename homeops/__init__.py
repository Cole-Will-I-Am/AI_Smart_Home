"""homeops — runnable, mocked reference implementation of the two-house AI ops layer.

See DESIGN.md (sections A–AA) for the architecture and config/houses.example.yaml
for the canonical two-house model. This package simulates both houses entirely in
software: no physical hardware, no real Home Assistant / OPNsense instance.
"""
from .bootstrap import build_world, build_real_world, start_event_bridge, World  # noqa: F401
