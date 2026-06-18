"""Tests for the live browser dashboard (``_dashboard_server.py``).

The router (``handle``) is a pure function over an injected executor, so every
route is exercised without binding a socket — faked SSH, no network. The shell
page wires the poller; the fragment is body-only; the log route tails a run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from bear_harness._dashboard_server import handle, render_page_shell
from bear_harness._hosts import Host
from bear_harness._remote import RemoteExecutor, RemoteRun, SshResult, write_remote_run


class _Ssh:
    def __init__(self, script: list[SshResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = list(script or [])

    def __call__(self, argv: Sequence[str]) -> SshResult:
        self.calls.append(tuple(argv))
        return self._script.pop(0) if self._script else SshResult(0, "", "")


def _exec(tmp_path: Path, *results: SshResult) -> RemoteExecutor:
    host = Host(name="bb", ssh_alias="bb", remote_rds_root="/rds/p", remote_inbox="/rds/p/inbox")
    return RemoteExecutor(host=host, run_shell=_Ssh(list(results)), cm_dir=tmp_path / "cm")


# snapshot(with_commands) makes two ssh calls: squeue, then the audit tail.
_SQUEUE = SshResult(0, "7|route|bbshort|RUNNING|00:01|00:10:00|1|n1\n", "")
_AUDIT = SshResult(0, '{"ts":"2026-06-18T10:00:00","verb":"deploy","detail":"route","host":"bb"}\n', "")


def test_index_serves_live_shell(tmp_path: Path) -> None:
    status, ctype, body = handle("/", {}, _exec(tmp_path, _SQUEUE, _AUDIT))
    assert status == 200 and "html" in ctype
    assert "BlueBEAR experiments" in body
    assert "setInterval(tick" in body  # the live poller is wired
    assert "/fragment" in body and "/api/logs" in body
    assert "route" in body  # the live job rendered into the initial body


def test_fragment_is_body_only(tmp_path: Path) -> None:
    status, _ctype, body = handle("/fragment", {}, _exec(tmp_path, _SQUEUE, _AUDIT))
    assert status == 200
    assert not body.lower().startswith("<!doctype")
    assert "route" in body and "Running" in body


def test_dashboard_json_route(tmp_path: Path) -> None:
    status, ctype, body = handle("/api/dashboard.json", {}, _exec(tmp_path, _SQUEUE, _AUDIT))
    assert status == 200 and ctype == "application/json"
    data = json.loads(body)
    assert data["running"] == 1
    assert data["commands"][0]["verb"] == "deploy"


def test_logs_route_requires_run_ref(tmp_path: Path) -> None:
    status, _ctype, body = handle("/api/logs", {}, _exec(tmp_path))
    assert status == 400 and "run_ref" in body


def test_logs_route_tails_selected_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
    write_remote_run(RemoteRun("prog-1", "bb", "ln1", "/rds/p/runs/prog-1", "9", "/i"))
    ex = _exec(tmp_path, SshResult(0, "vllm boot ok", ""), SshResult(0, "pipeline line", ""))
    status, ctype, body = handle("/api/logs", {"run_ref": "prog-1", "lines": "80"}, ex)
    assert status == 200 and "text/plain" in ctype
    assert "vllm boot ok" in body and "pipeline line" in body


def test_unknown_path_is_404(tmp_path: Path) -> None:
    status, _ctype, body = handle("/nope", {}, _exec(tmp_path))
    assert status == 404 and "not found" in body


def test_page_shell_embeds_refresh_interval() -> None:
    page = render_page_shell("<div>x</div>", refresh=5)
    assert "const REFRESH=5;" in page
    assert "<div>x</div>" in page
