from homeops.simulator import scenarios
from homeops.ai import OpsLayer


def test_locals_run_with_wan_and_ai_down(world):
    world.houses["house_a"].wan_up = False    # internet down
    world.houses["house_a"].ai_hold = True    # AI actuation suspended
    scenarios.leak(world, "house_a")
    world.tick(2)
    # the leak still auto-shuts the valve — automations live below the AI
    assert world.state.get_state("house_a.water.main_valve") == "closed"


def test_ai_hold_forces_fallback(world):
    world.houses["house_a"].ai_hold = True
    ai = OpsLayer(world, client=object())     # client present, but ai_hold wins
    out = ai.run("arm night", "house_a")
    assert out["mode"] == "fallback"


def test_wan_down_forces_fallback(world):
    world.houses["house_a"].wan_up = False
    ai = OpsLayer(world, client=object())
    out = ai.run("all lights off", "house_a")
    assert out["mode"] == "fallback"


def test_no_client_forces_fallback(world):
    ai = OpsLayer(world, client=None)
    out = ai.run("arm night", "house_a")
    assert out["mode"] == "fallback"
