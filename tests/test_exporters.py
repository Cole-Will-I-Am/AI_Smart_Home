"""Part 5 — native life-safety export (Home Assistant automations)."""
import yaml
from homeops.exporters.homeassistant_export import export_ha_automations, leak_automation, fire_co_automation


def _r(mapping):
    return lambda k: mapping[k]


def test_export_is_valid_yaml_for_all_houses():
    text = export_ha_automations(["house_a", "house_b"])
    data = yaml.safe_load(text)
    assert isinstance(data, list) and len(data) == 6      # 3 life-safety automations x 2 houses
    assert "homeops_leak_shutoff_house_a" in [a["id"] for a in data]


def test_leak_automation_is_native_two_signal():
    a = leak_automation("house_a", _r({"leak": "binary_sensor.x", "flow": "sensor.y",
                                        "valve": "valve.z", "notify": "notify.n"}), threshold=30)
    assert a["trigger"][0]["entity_id"] == "binary_sensor.x" and a["trigger"][0]["to"] == "on"
    assert a["condition"][0]["condition"] == "numeric_state" and a["condition"][0]["above"] == 30
    assert any(act.get("service") == "valve.close_valve" for act in a["action"])


def test_fire_automation_unlocks_egress_and_stops_hvac():
    a = fire_co_automation("house_a", _r({"smoke_co": "binary_sensor.s", "egress_light": "light.e",
                                          "egress_lock": "lock.e", "hvac": "climate.h", "notify": "notify.n"}))
    services = [act["service"] for act in a["action"]]
    assert "lock.unlock" in services and "climate.set_hvac_mode" in services


def test_entity_map_overrides_defaults():
    text = export_ha_automations(["house_a"], entity_maps={"house_a": {"valve": "valve.custom_main"}})
    assert "valve.custom_main" in text
