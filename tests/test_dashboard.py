"""Part 6 — operator oversight dashboard (HTML renderer)."""
import os
from homeops import build_world
from homeops.dashboard import render_dashboard, write_dashboard
from homeops.simulator import scenarios

CFG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "portfolio.example.yaml")


def test_dashboard_renders_all_properties_and_audit_ok():
    w = build_world(CFG)
    html = render_dashboard(w, "Test Portfolio")
    assert html.startswith("<!doctype html>")
    for hid, house in w.houses.items():
        assert house.alias in html
    assert "audit chain intact" in html


def test_dashboard_surfaces_leak_and_offline():
    w = build_world(CFG)
    ids = list(w.houses)
    scenarios.leak(w, ids[0]); w.tick(2)
    w.health.mark_offline(f"{ids[1]}.lock.front_door")
    html = render_dashboard(w)
    assert "urgent" in html and "offline" in html
    assert "shutoff_main" in html          # the leak response appears in the activity log


def test_dashboard_flags_tampering():
    w = build_world(CFG)
    w.router.execute.__self__  # noqa: touch (no-op)
    from homeops.permissions import Intent, Operator
    r = w.router.execute(Intent(list(w.houses)[0], "light", "living_room", "turn_on"),
                         Operator("owner", list(w.houses)[0]))
    assert r.status == "executed"
    object.__setattr__(w.audit._records[0], "status", "prohibited")   # tamper
    html = render_dashboard(w)
    assert "AUDIT TAMPERING DETECTED" in html


def test_write_dashboard(tmp_path):
    w = build_world(CFG)
    p = str(tmp_path / "dash.html")
    write_dashboard(w, p)
    assert os.path.exists(p) and "<!doctype html>" in open(p).read()
