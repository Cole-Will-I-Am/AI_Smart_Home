"""Control Surface Gateway — one authenticated write path for every human/voice surface.

Every surface collapses to the same structured Intent and faces the same CommandRouter as the
AI ops layer; the gateway performs no permission logic of its own (the router is the sole
authority). See core.Gateway for the logic and api for the HTTP surface.
"""
from .core import Gateway, Device, Pending, KNOWN_SURFACES

__all__ = ["Gateway", "Device", "Pending", "KNOWN_SURFACES"]
