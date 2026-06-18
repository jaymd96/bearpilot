"""Tests for the agent-facing MCP server (``_mcp_server.py``).

The import-guard pins the load-bearing boundary — the MCP module must import only
``_remote`` / ``_hosts``, never the kernel — and is AST-based, so it runs even
without the ``mcp`` SDK installed. The smoke tests inject a faked SSH executor
(no sockets) and exercise each tool/resource, asserting they relay the cluster's
JSON, plus that everything is registered.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from bear_harness._hosts import Host
from bear_harness._remote import RemoteExecutor, RemoteRun, SshResult, write_remote_run

_SRC = Path(__file__).resolve().parents[1] / "src" / "bear_harness"
_MCP_SRC = _SRC / "_mcp_server.py"
_DASHBOARD_SRC = _SRC / "_dashboard.py"
_DASHBOARD_SERVER_SRC = _SRC / "_dashboard_server.py"

_KERNEL_MODULES = {
    "_launch",
    "_slurm_runner",
    "_vllm_launcher",
    "_pipeline_launcher",
    "_runner",
    "_bear_config",
    "_guardrails",
    "_manifest",
    "_results",
}


def _bear_harness_imports(src: Path) -> set[str]:
    """The set of ``bear_harness.<sub>`` modules a source file imports (AST, no exec)."""
    tree = ast.parse(src.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "." in node.module:
            pkg, sub = node.module.split(".", 1)
            if pkg == "bear_harness":
                found.add(sub)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bear_harness."):
                    found.add(alias.name.split(".", 1)[1])
    return found


def test_mcp_server_imports_only_transport_not_kernel() -> None:
    """The MCP module is a front-end: transport (_remote/_hosts) + presentation (_dashboard) only."""
    bh_imports = _bear_harness_imports(_MCP_SRC)
    assert bh_imports <= {"_remote", "_hosts", "_dashboard"}, (
        f"MCP server reaches beyond transport/presentation: {bh_imports}"
    )
    assert not (bh_imports & _KERNEL_MODULES)


def test_dashboard_module_is_presentation_only() -> None:
    """The ui:// renderer may lean on _remote's types but never the kernel or SSH."""
    bh_imports = _bear_harness_imports(_DASHBOARD_SRC)
    assert bh_imports <= {"_remote"}, f"_dashboard reaches beyond _remote: {bh_imports}"
    assert not (bh_imports & _KERNEL_MODULES)


def test_dashboard_server_imports_only_transport_and_presentation() -> None:
    """The live dashboard server is a front-end: transport + presentation, never kernel."""
    bh_imports = _bear_harness_imports(_DASHBOARD_SERVER_SRC)
    assert bh_imports <= {"_remote", "_hosts", "_dashboard"}, (
        f"_dashboard_server reaches beyond transport/presentation: {bh_imports}"
    )
    assert not (bh_imports & _KERNEL_MODULES)


# --- smoke tests (require the mcp SDK; skip cleanly if absent) ---------------


class _RecordingSsh:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script: list[SshResult] = []

    def script(self, *results: SshResult) -> None:
        self._script = list(results)

    def __call__(self, argv: Sequence[str]) -> SshResult:
        self.calls.append(tuple(argv))
        return self._script.pop(0) if self._script else SshResult(0, "", "")

    @property
    def remote_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if c and c[0] == "ssh"]


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    pytest.importorskip("mcp")
    monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
    from bear_harness import _mcp_server

    host = Host(name="bb", ssh_alias="bb", remote_rds_root="/rds/p", remote_inbox="/rds/p/inbox")
    shell = _RecordingSsh()
    _mcp_server.set_executor(RemoteExecutor(host=host, run_shell=shell, cm_dir=tmp_path / "cm"))
    try:
        yield _mcp_server, shell
    finally:
        _mcp_server.set_executor(None)


def _seed_pointer() -> None:
    write_remote_run(
        RemoteRun(
            run_ref="prog-1",
            host="bb",
            node="ln1",
            remote_run_dir="/rds/p/runs/prog-1",
            orchestrator_pid="9",
            inbox_dir="/rds/p/inbox/prog-1",
        )
    )


def test_status_tool_relays_run_json(server: tuple) -> None:
    mod, shell = server
    _seed_pointer()
    shell.script(SshResult(0, json.dumps({"job_id": "prog-1", "state": "running"}), ""))
    assert json.loads(mod.status("prog-1"))["state"] == "running"
    assert "cat /rds/p/runs/prog-1/run.json" in shell.remote_commands


def test_ps_tool_runs_squeue_me(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, "JOBID\n7\n", ""))
    assert "7" in mod.ps()
    assert "squeue --me" in shell.remote_commands


def test_check_tool_passes_overrides(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, '{"allowed": false}', ""))
    assert json.loads(mod.check(qos="bbgpu"))["allowed"] is False
    assert any("check --json --qos bbgpu" in c for c in shell.remote_commands)


def test_guardrails_resource_reads_caps(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, '{"qos_allowlist": ["bbshort"]}', ""))
    assert "bbshort" in json.loads(mod.guardrails_allowed())["qos_allowlist"]
    assert any(c.endswith("caps --json") for c in shell.remote_commands)


def test_cancel_tool_scancels(server: tuple) -> None:
    mod, shell = server
    _seed_pointer()
    shell.script(
        SshResult(0, json.dumps({"vllm_job_id": "11", "pipeline_job_id": "22"}), ""),
        SshResult(0, "", ""),
        SshResult(0, "", ""),
    )
    mod.cancel("prog-1")
    assert "scancel 11 22" in shell.remote_commands


def test_jobs_tool_returns_structured_rows(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, "45408821|route-diff|bbshort|RUNNING|00:04:12|00:10:00|1|bear-pg0208\n", ""))
    rows = json.loads(mod.jobs())
    assert rows[0]["job_id"] == "45408821"
    assert rows[0]["state"] == "RUNNING"
    assert rows[0]["qos"] == "bbshort"
    assert any("squeue --me -h -o" in c for c in shell.remote_commands)


def test_dashboard_tool_aggregates_jobs_and_runs(server: tuple) -> None:
    mod, shell = server
    _seed_pointer()  # one known run on host "bb"
    shell.script(
        SshResult(0, "7|a|bbshort|RUNNING|00:01|00:10:00|1|n1\n8|b|bbgpu|PENDING|0:00|2-00:00:00|1|Dependency\n", "")
    )
    snap = json.loads(mod.dashboard())
    assert snap["host"] == "bb"
    assert snap["running"] == 1 and snap["pending"] == 1 and snap["active"] == 2
    assert snap["runs"][0]["run_ref"] == "prog-1"
    assert snap["error"] == ""


def test_dashboard_degrades_when_squeue_fails(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(1, "", "slurm down"))
    snap = json.loads(mod.dashboard())
    assert snap["jobs"] == [] and snap["active"] == 0
    assert "slurm down" in snap["error"]


def test_dashboard_ui_renders_html(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, "45409110|vllm-qwen|bbgpu|RUNNING|00:12:33|2-00:00:00|1|n2\n", ""))
    html = mod.dashboard_ui()
    assert "<html" in html.lower()
    assert "vllm-qwen" in html and "RUNNING" in html
    assert "BlueBEAR experiments" in html


def test_commands_tool_reads_audit(server: tuple) -> None:
    mod, shell = server
    shell.script(SshResult(0, '{"verb":"deploy","detail":"prog-1","ts":"t"}\n', ""))
    out = json.loads(mod.commands())
    assert out[0]["verb"] == "deploy"
    assert any("launchpad-audit.jsonl" in c for c in shell.remote_commands)


def test_tools_resources_prompts_are_registered(server: tuple) -> None:
    mod, _shell = server
    tools = {t.name for t in asyncio.run(mod.mcp.list_tools())}
    expected_tools = {"deploy", "status", "logs", "cancel", "check", "fetch", "ps", "jobs", "dashboard", "commands"}
    assert expected_tools <= tools
    resources = {str(r.uri) for r in asyncio.run(mod.mcp.list_resources())}
    assert {"bear://guardrails/allowed", "bear://commands", "ui://dashboard"} <= resources
    prompts = {p.name for p in asyncio.run(mod.mcp.list_prompts())}
    assert {"deploy_vllm_pipeline", "check_before_submit", "monitor_experiments"} <= prompts
