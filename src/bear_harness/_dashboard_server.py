"""A local, live browser dashboard for BlueBEAR experiments — the truly-live surface.

MCP tool calls are request/response, so the inline/``ui://`` dashboard is a
snapshot. This is the answer when you want to *watch*: a tiny stdlib HTTP server
on ``127.0.0.1`` that the browser polls every few seconds, re-rendering the live
job table and tailing a run's log — no CLI, no extra dependencies.

It is a transport/presentation front-end like ``_mcp_server``: it drives the same
``_remote`` SSH core and reuses ``_dashboard``'s renderer, and imports **nothing**
from the kernel (``tests/test_dashboard_server.py`` pins that with an import-guard).
Bind stays on loopback — the server speaks for your SSH credentials, so it must
never be exposed to the network.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bear_harness._dashboard import DASHBOARD_CSS, render_dashboard_body
from bear_harness._hosts import load_hosts
from bear_harness._remote import RemoteError, RemoteExecutor

_DEFAULT_PORT = 8765
_DEFAULT_REFRESH = 8


def render_page_shell(body: str, *, refresh: int = _DEFAULT_REFRESH) -> str:
    """The full live page: the dashboard body in ``#app`` + a poller + a log pane."""
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>BlueBEAR experiments (live)</title>"
        f"<style>{DASHBOARD_CSS}"
        ".bar{display:flex;gap:8px;align-items:center;margin:14px 0 6px}"
        ".bar input{flex:0 0 220px;padding:6px 8px;font:12px ui-monospace,monospace}"
        ".bar button{padding:6px 12px}"
        "pre#logpane{background:var(--surface);border-radius:8px;padding:12px;overflow:auto;"
        "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;max-height:340px}"
        ".tick{font-size:12px;color:var(--dim)}</style>"
        f'<div id="app">{body}</div>'
        '<div class="sec"><div class="lbl">Live log <span class="tick" id="tick"></span></div>'
        '<div class="bar"><input id="runref" placeholder="run_ref to tail" />'
        '<button onclick="tailNow()">Tail</button></div>'
        '<pre id="logpane">Enter a run_ref above to tail its log.</pre></div>'
        "<script>"
        f"const REFRESH={int(refresh)};"
        "const $=id=>document.getElementById(id);"
        "async function refreshApp(){try{const r=await fetch('/fragment',{cache:'no-store'});"
        "if(r.ok)$('app').innerHTML=await r.text();}catch(e){}}"
        "async function refreshLog(){const ref=$('runref').value.trim();if(!ref)return;"
        "try{const r=await fetch('/api/logs?lines=80&run_ref='+encodeURIComponent(ref),{cache:'no-store'});"
        "$('logpane').textContent=r.ok?await r.text():'(no log: '+r.status+')';}catch(e){}}"
        "function tailNow(){refreshLog();}"
        "async function tick(){await refreshApp();await refreshLog();"
        "$('tick').textContent='updated '+new Date().toLocaleTimeString();}"
        "setInterval(tick,REFRESH*1000);"
        "</script></html>"
    )


def handle(
    path: str,
    query: dict[str, str],
    executor: RemoteExecutor,
    *,
    runs_dir: Path | None = None,
    refresh: int = _DEFAULT_REFRESH,
) -> tuple[int, str, str]:
    """Pure router: ``(path, query, executor) -> (status, content_type, body)``.

    Kept side-effect-free over an injected executor so the routes are unit-tested
    without binding a socket.
    """
    if path in ("/", "/index.html"):
        snap = executor.dashboard_snapshot(with_commands=True, runs_dir=runs_dir)
        return 200, "text/html; charset=utf-8", render_page_shell(
            render_dashboard_body(snap), refresh=refresh
        )
    if path == "/fragment":
        snap = executor.dashboard_snapshot(with_commands=True, runs_dir=runs_dir)
        return 200, "text/html; charset=utf-8", render_dashboard_body(snap)
    if path == "/api/dashboard.json":
        snap = executor.dashboard_snapshot(with_commands=True, runs_dir=runs_dir)
        return 200, "application/json", json.dumps(snap.as_dict())
    if path == "/api/logs":
        run_ref = query.get("run_ref", "").strip()
        if not run_ref:
            return 400, "text/plain; charset=utf-8", "run_ref query parameter is required"
        which = query.get("which", "both")
        try:
            lines = int(query.get("lines", "60"))
        except ValueError:
            lines = 60
        try:
            body = executor.tail_run_logs(run_ref, which=which, lines=lines, runs_dir=runs_dir)
        except RemoteError as exc:
            return 404, "text/plain; charset=utf-8", str(exc)
        return 200, "text/plain; charset=utf-8", body
    return 404, "text/plain; charset=utf-8", f"not found: {path}"


class DashboardHandler(BaseHTTPRequestHandler):
    """Thin shim: parse the URL, delegate to :func:`handle`, write the response."""

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's required handler name
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        status, content_type, body = handle(
            parts.path,
            query,
            self.server.executor,  # type: ignore[attr-defined]
            refresh=self.server.refresh,  # type: ignore[attr-defined]
        )
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        """Silence the default per-request stderr logging."""


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], executor: RemoteExecutor, refresh: int) -> None:
        super().__init__(addr, DashboardHandler)
        self.executor = executor
        self.refresh = refresh


def serve(
    executor: RemoteExecutor,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    refresh: int = _DEFAULT_REFRESH,
) -> _DashboardServer:
    """Build (but do not block on) the loopback dashboard server. Caller serves it."""
    return _DashboardServer((host, port), executor, refresh)


def main() -> None:
    """Console entry point: ``bear-harness-dashboard`` — serve on 127.0.0.1."""
    host_name = os.environ.get("BEAR_HARNESS_MCP_HOST")
    hosts_env = os.environ.get("BEAR_HARNESS_HOSTS")
    port = int(os.environ.get("BEAR_HARNESS_DASHBOARD_PORT", str(_DEFAULT_PORT)))
    cfg = load_hosts(Path(hosts_env) if hosts_env else None)
    executor = RemoteExecutor(host=cfg.resolve(host_name))
    httpd = serve(executor, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"BlueBEAR dashboard for {executor.host.name} → {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


__all__ = ["DashboardHandler", "handle", "render_page_shell", "serve"]
