"""Operator oversight dashboard — the managed-monitoring "one pane of glass".

Renders a self-contained HTML snapshot of the whole portfolio from a World: per-property mode,
connectivity, safety-device state, offline devices, urgent alerts, recent audited activity, and a
live estate-wide audit-integrity check. Stdlib-only (no server, no deps) so it's testable and can be
written to a file or served read-only. This is the surface the 24/7 monitoring tier is sold on.
"""
from __future__ import annotations
import html

from .portfolio import portfolio_summary

_CSS = """
:root{--bg:#0e1116;--card:#171b22;--line:#232935;--fg:#e6edf3;--muted:#8b98a9;
--ok:#2ea043;--warn:#d29922;--bad:#f85149;--info:#388bfd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.rollup{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:150px}
.stat .n{font-size:22px;font-weight:600}.stat .l{color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.pill.ok{color:#7ee2a8;border-color:#1c3a2a}.pill.warn{color:#e8c37a;border-color:#3a3320}
.pill.bad{color:#ff9b93;border-color:#3a2020}.pill.info{color:#9ac4ff;border-color:#20304a}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.chip{font-size:11px;padding:3px 8px;border-radius:6px;background:#10141a;border:1px solid var(--line)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}.d-bad{background:var(--bad)}.d-info{background:var(--info)}.d-muted{background:var(--muted)}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12.5px}
td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}
.badge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:600}
.badge.ok{background:#0f2a18;color:#7ee2a8}.badge.bad{background:#2a1010;color:#ff9b93}
.st-executed{color:#7ee2a8}.st-refused,.st-prohibited,.st-unverified{color:#ff9b93}
.st-confirm_required,.st-manual_override{color:#e8c37a}.st-recommended,.st-recommend_only,.st-rollback{color:#9ac4ff}
.section{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin:26px 0 8px}
"""


def _tone(state) -> str:
    s = str(state).lower()
    if s in ("open", "locked", "running", "nominal", "active", "on"):
        return "ok"
    if s in ("failed", "offline", "unavailable"):
        return "bad"
    if s in ("closing", "unlocked", "starting") or s.startswith("armed") or s.startswith("shedding"):
        return "warn"
    return "muted"


def _esc(x) -> str:
    return html.escape(str(x))


def _pill(label: str, tone: str = "muted") -> str:
    return f'<span class="pill {tone}">{_esc(label)}</span>'


def render_dashboard(world, portfolio_name: str = "Estate Portfolio", audit_tail: int = 18) -> str:
    s = portfolio_summary(world)
    audit_badge = ('<span class="badge ok">audit chain intact</span>' if s["audit_intact"]
                   else '<span class="badge bad">AUDIT TAMPERING DETECTED</span>')

    cards = []
    for hid, p in s["properties"].items():
        conn = []
        conn.append(_pill("WAN up", "ok") if p["wan_up"] else _pill("WAN DOWN", "bad"))
        conn.append(_pill("grid up", "ok") if p["grid_up"] else _pill("GRID DOWN", "bad"))
        conn.append(_pill(f"mode: {p['mode']}", "info"))
        if p["ai_hold"]:
            conn.append(_pill("AI hold", "warn"))
        if p["urgent_alerts"]:
            conn.append(_pill(f"{p['urgent_alerts']} urgent", "bad"))
        if p["offline_devices"]:
            conn.append(_pill(f"{len(p['offline_devices'])} offline", "bad"))
        chips = "".join(
            f'<span class="chip"><span class="dot d-{_tone(v)}"></span>{_esc(k)}: {_esc(v)}</span>'
            for k, v in sorted(p["safety"].items()))
        cards.append(
            f'<div class="card"><h2>{_esc(p["alias"])} '
            f'<span class="pill">{_esc(hid)}</span></h2>'
            f'<div class="pills">{"".join(conn)}</div>'
            f'<div class="chips">{chips}</div></div>')

    rows = []
    for r in reversed(world.audit.records[-audit_tail:]):
        tgt = f"{_esc(r.house_id)}.{_esc(r.subsystem)}.{_esc(r.target)}.{_esc(r.action)}"
        rows.append(
            f'<tr><td>{r.tick}</td><td>{_esc(r.operator)}</td><td>{tgt}</td>'
            f'<td class="st-{_esc(r.status)}">{_esc(r.status)}</td><td>{_esc(r.message)}</td></tr>')
    audit_table = ('<table><tr><th>t</th><th>operator</th><th>action</th><th>status</th><th>message</th></tr>'
                   + "".join(rows) + "</table>") if rows else '<div class="sub">no activity yet</div>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(portfolio_name)} — homeops oversight</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>{_esc(portfolio_name)} <span style="font-size:13px">{audit_badge}</span></h1>
<div class="sub">homeops operator oversight · AI proposes, the engine decides, every action is logged &amp; reversible</div>
<div class="rollup">
<div class="stat"><div class="n">{s['n_properties']}</div><div class="l">properties</div></div>
<div class="stat"><div class="n">{s['total_offline_devices']}</div><div class="l">offline devices</div></div>
<div class="stat"><div class="n">{s['total_urgent_alerts']}</div><div class="l">urgent alerts</div></div>
<div class="stat"><div class="n">{'OK' if s['audit_intact'] else 'FAIL'}</div><div class="l">audit integrity</div></div>
</div>
<div class="section">Properties</div>
<div class="grid">{"".join(cards)}</div>
<div class="section">Recent audited activity</div>{audit_table}
</div></body></html>"""


def write_dashboard(world, path: str, portfolio_name: str = "Estate Portfolio") -> None:
    with open(path, "w") as f:
        f.write(render_dashboard(world, portfolio_name))
