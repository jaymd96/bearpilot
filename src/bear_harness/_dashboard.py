"""Presentation-only: render a :class:`DashboardSnapshot` to HTML.

Two surfaces share this one renderer:

- the MCP ``ui://dashboard`` resource — a static, self-contained page
  (:func:`render_dashboard_html`); and
- the live browser dashboard (``_dashboard_server``) — which polls and swaps in
  just the body fragment (:func:`render_dashboard_body`), reusing :data:`DASHBOARD_CSS`.

Deliberately separate from the transport (``_remote``) and the server
(``_mcp_server``): pure string templating, no SSH, no kernel. ``tests/test_mcp_server.py``
extends the import-guard here too — it may import ``_remote`` (for the snapshot
types) and stdlib only. The HTML is self-contained (own colours + a
``prefers-color-scheme`` dark block, no external resources) so it renders in a
sandboxed host.
"""

from __future__ import annotations

from html import escape

from bear_harness._remote import DashboardSnapshot, JobRow

# The SLURM %T codes (references/slurm-cli.md) → badge classes.
_RUNNING = "RUNNING"
_PENDING = "PENDING"
_DONE = {"COMPLETED"}
_FAILED = ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED")

DASHBOARD_CSS = """
:root{--bg:#fff;--surface:#f6f6f4;--text:#1a1a18;--dim:#6b6b66;--bd:rgba(0,0,0,.12);
--run-b:#e6f4ea;--run-f:#137333;--pend-b:#fef7e0;--pend-f:#8a5300;
--done-b:#e8f0fe;--done-f:#1a56c4;--fail-b:#fce8e6;--fail-f:#a50e0e;--unk-b:#eee;--unk-f:#555;}
@media(prefers-color-scheme:dark){:root{--bg:#1f1f1d;--surface:#2a2a27;--text:#ececea;--dim:#a0a09a;--bd:rgba(255,255,255,.14);
--run-b:#10331f;--run-f:#7fd6a0;--pend-b:#3a2e0a;--pend-f:#f0c050;--done-b:#0c2447;--done-f:#85b7eb;
--fail-b:#3a1212;--fail-f:#f09595;--unk-b:#333;--unk-f:#aaa;}}
*{box-sizing:border-box}body{margin:0;padding:18px;background:var(--bg);color:var(--text);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.hd{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.ttl{font-size:17px;font-weight:500}.host{font-size:12px;color:var(--dim)}
.cards{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.card{background:var(--surface);border-radius:8px;padding:10px 14px;min-width:90px}
.card .n{font-size:22px;font-weight:500}.card .k{font-size:12px;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:400;color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--bd)}
td{padding:8px;border-bottom:1px solid var(--bd);vertical-align:top}
.name{font-weight:500}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.dim{color:var(--dim)}.empty{color:var(--dim);background:var(--surface);padding:14px;border-radius:8px}
.b{display:inline-block;font-size:12px;padding:2px 9px;border-radius:6px}
.b.run{background:var(--run-b);color:var(--run-f)}.b.pend{background:var(--pend-b);color:var(--pend-f)}
.b.done{background:var(--done-b);color:var(--done-f)}.b.fail{background:var(--fail-b);color:var(--fail-f)}
.b.unk{background:var(--unk-b);color:var(--unk-f)}
.sec{margin-top:16px}.sec .lbl{font-size:12px;color:var(--dim);margin-bottom:6px}
.sec ul{margin:0;padding:0;list-style:none}.sec li{padding:3px 0;font-size:13px}
""".strip()


def _badge(state: str) -> str:
    s = state.upper()
    if s == _RUNNING:
        cls = "run"
    elif s == _PENDING:
        cls = "pend"
    elif s in _DONE:
        cls = "done"
    elif any(s.startswith(f) for f in _FAILED):
        cls = "fail"
    else:
        cls = "unk"
    return f'<span class="b {cls}">{escape(state or "—")}</span>'


def _job_row(j: JobRow) -> str:
    reason = (
        f'<br><span class="dim">{escape(j.reason)}</span>'
        if j.reason and j.state.upper() == _PENDING
        else ""
    )
    return (
        "<tr>"
        f'<td><span class="name">{escape(j.name)}</span><br>'
        f'<span class="mono dim">{escape(j.job_id)}</span></td>'
        f'<td class="mono">{escape(j.qos)}</td>'
        f"<td>{_badge(j.state)}{reason}</td>"
        f'<td class="mono">{escape(j.elapsed or "—")}</td>'
        f'<td class="mono">{escape(j.time_limit or "—")}</td>'
        "</tr>"
    )


def _section(label: str, items_html: str) -> str:
    return f'<div class="sec"><div class="lbl">{escape(label)}</div><ul>{items_html}</ul></div>'


def render_dashboard_body(snapshot: DashboardSnapshot) -> str:
    """The dashboard content (no doctype/style) — what the live server swaps in."""
    jobs = snapshot.jobs
    if jobs:
        rows = "".join(_job_row(j) for j in jobs)
        table = (
            '<table><thead><tr><th>Job</th><th>QoS</th><th>State</th>'
            "<th>Elapsed</th><th>Limit</th></tr></thead><tbody>"
            f"{rows}</tbody></table>"
        )
    else:
        note = escape(snapshot.error) if snapshot.error else "No active jobs."
        table = f'<p class="empty">{note}</p>'

    runs = ""
    if snapshot.runs:
        items = "".join(
            f'<li><span class="mono">{escape(r.run_ref)}</span>'
            f'<span class="dim"> · {escape(r.remote_run_dir)}</span></li>'
            for r in snapshot.runs
        )
        runs = _section("Known runs (reattach by ref)", items)

    cmds = ""
    if snapshot.commands:
        items = "".join(
            f'<li><span class="dim">{escape(str(c.get("ts", "")))}</span> '
            f'<span class="mono">{escape(str(c.get("verb", "")))}</span> '
            f'<span class="dim">{escape(str(c.get("detail", "")))}</span></li>'
            for c in snapshot.commands
        )
        cmds = _section("Recent commands (shared audit)", items)

    return (
        f'<div class="hd"><span class="ttl">BlueBEAR experiments</span>'
        f'<span class="host">{escape(snapshot.host)}</span></div>'
        '<div class="cards">'
        f'<div class="card"><div class="k">Running</div><div class="n">{snapshot.running}</div></div>'
        f'<div class="card"><div class="k">Pending</div><div class="n">{snapshot.pending}</div></div>'
        f'<div class="card"><div class="k">Active</div><div class="n">{snapshot.active}</div></div>'
        "</div>"
        f"{table}{runs}{cmds}"
    )


_HEAD = (
    '<!doctype html><html lang="en"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>BlueBEAR experiments</title>"
)


def render_dashboard_html(snapshot: DashboardSnapshot) -> str:
    """A static, self-contained HTML dashboard (the MCP ``ui://dashboard`` surface)."""
    return f"{_HEAD}<style>{DASHBOARD_CSS}</style>{render_dashboard_body(snapshot)}</html>"


__all__ = ["DASHBOARD_CSS", "render_dashboard_body", "render_dashboard_html"]
