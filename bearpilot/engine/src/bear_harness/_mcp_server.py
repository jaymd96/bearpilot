"""The agent-facing MCP server — a thin front-end over the SSH core.

This is the *agent's* face on the transport; the human's face is the
``bear-harness ... --remote`` CLI. Both lower to the same ``_remote.py`` SSH core
and the same ``bear-harness <verb> --json`` invocations on the login node.

Built on the official MCP SDK's ``FastMCP``. It imports ONLY ``_remote`` and
``_hosts`` — never the kernel (``_launch`` / ``_slurm_runner`` / the launchers):
the no-SSH-in-the-kernel discipline, one layer up. ``tests/test_mcp_server.py``
enforces that boundary with an import guard. Runs on the laptop; the ``mcp``
dependency is the ``bear-harness[mcp]`` extra, never installed cluster-side.

The MCP surface is more than verb-tools: ``bear://guardrails/allowed`` lets the
agent discover the resource envelope, and the prompts steer it to pre-flight
``check`` before ``deploy``. See ``docs/decision-notes/mcp-over-ssh-transport.md``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from bear_harness._dashboard import render_dashboard_html
from bear_harness._hosts import load_hosts
from bear_harness._remote import RemoteExecutor, read_remote_run

mcp = FastMCP("bear-harness")

_executor: RemoteExecutor | None = None


def set_executor(executor: RemoteExecutor | None) -> None:
    """Inject (or reset) the SSH executor — the seam tests use to avoid sockets."""
    global _executor
    _executor = executor


def _get_executor() -> RemoteExecutor:
    """Lazily build the executor from ``hosts.toml`` (``BEAR_HARNESS_MCP_HOST`` picks one)."""
    global _executor
    if _executor is None:
        hosts_env = os.environ.get("BEAR_HARNESS_HOSTS")
        host_name = os.environ.get("BEAR_HARNESS_MCP_HOST")
        cfg = load_hosts(Path(hosts_env) if hosts_env else None)
        _executor = RemoteExecutor(host=cfg.resolve(host_name))
    return _executor


def _inbox_name(program_dir: str) -> str:
    return f"{Path(program_dir).name}-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"


@mcp.tool()
def deploy(program_dir: str) -> str:
    """Upload a local pipeline program to BlueBEAR and launch it detached.

    Returns the run handle as JSON (``run_ref`` reattaches later). Read
    ``bear://guardrails/allowed`` and call ``check`` first to avoid a denial.
    """
    ex = _get_executor()
    run = ex.launch_detached(Path(program_dir), _inbox_name(program_dir))
    ex.record_command("deploy", run.run_ref)
    return json.dumps(run.as_dict())


@mcp.tool()
def status(run_ref: str) -> str:
    """Return a run's current ``run.json`` (read off the shared FS via ``ssh cat``)."""
    ex = _get_executor()
    run = read_remote_run(ex.host.name, run_ref)
    return json.dumps(ex.read_run_json(run))


@mcp.tool()
def logs(run_ref: str, which: str = "both", lines: int = 50) -> str:
    """Tail a run's vllm/pipeline logs from the login node."""
    return _get_executor().tail_run_logs(run_ref, which=which, lines=lines)


@mcp.tool()
def cancel(run_ref: str) -> str:
    """Cancel a run: ``scancel`` its SLURM jobs and reap the orchestrator."""
    ex = _get_executor()
    run = read_remote_run(ex.host.name, run_ref)
    ex.cancel(run)
    ex.record_command("cancel", run_ref)
    return f"cancel requested for {run_ref} on {ex.host.name}"


@mcp.tool()
def check(qos: str | None = None, walltime: str | None = None, gpu_gres: str | None = None) -> str:
    """Pre-flight a would-be request against the guardrails (returns the decision JSON)."""
    ex = _get_executor()
    argv = [ex.host.remote_binary, "check", "--json"]
    if qos:
        argv += ["--qos", qos]
    if walltime:
        argv += ["--walltime", walltime]
    if gpu_gres:
        argv += ["--gpu-gres", gpu_gres]
    return ex.run(argv).stdout


@mcp.tool()
def fetch(run_ref: str) -> str:
    """Pull a run's artifacts tarball down to the laptop cache; returns JSON."""
    ex = _get_executor()
    run = read_remote_run(ex.host.name, run_ref)
    dest = Path(ex.host.artifacts_cache).expanduser() / run_ref
    res = ex.rsync_pull(f"{run.remote_run_dir}/artifacts.tar.gz", dest)
    return json.dumps({"run_ref": run_ref, "fetched_to": str(dest), "ok": res.ok})


@mcp.tool()
def ps() -> str:
    """List your in-flight SLURM jobs on the host (``squeue --me``)."""
    return _get_executor().run(["squeue", "--me"]).stdout


@mcp.tool()
def jobs() -> str:
    """Structured list of your in-flight SLURM jobs (parsed ``squeue --me``).

    Unlike ``ps`` (raw text), this returns JSON rows — ``job_id``/``name``/``qos``/
    ``state``/``elapsed``/``time_limit``/``nodes``/``reason`` — for rendering or filtering.
    """
    ex = _get_executor()
    return json.dumps([j.as_dict() for j in ex.list_jobs()])


@mcp.tool()
def dashboard() -> str:
    """One structured snapshot for the experiment dashboard.

    Aggregates live jobs (counts + rows), the known run-refs on this host, and the
    recent shared-FS command audit. Degrades gracefully (``error`` set, empty
    ``jobs``) if ``squeue`` hiccups, so the monitor never blanks. Render it as a
    status widget, or read ``ui://dashboard`` for ready-made HTML.
    """
    ex = _get_executor()
    return json.dumps(ex.dashboard_snapshot(with_commands=True).as_dict())


@mcp.tool()
def commands(limit: int = 20) -> str:
    """The recent command audit (deploy/cancel) for this host — newest first, as JSON.

    Durable + cross-session: it reads the shared-FS audit log, so it answers "what
    has been run here" regardless of which session issued the commands.
    """
    ex = _get_executor()
    return json.dumps(list(ex.read_audit(limit)))


@mcp.resource("bear://guardrails/allowed")
def guardrails_allowed() -> str:
    """The resource caps a request must stay within (``bear-harness caps --json``)."""
    ex = _get_executor()
    return ex.run([ex.host.remote_binary, "caps", "--json"]).stdout


@mcp.resource("bear://commands")
def commands_audit() -> str:
    """The shared-FS command audit (newest first) — durable across sessions."""
    ex = _get_executor()
    return json.dumps(list(ex.read_audit(20)))


@mcp.resource("ui://dashboard", mime_type="text/html")
def dashboard_ui() -> str:
    """A self-contained HTML dashboard of live jobs + known runs + audit (MCP-UI)."""
    ex = _get_executor()
    return render_dashboard_html(ex.dashboard_snapshot(with_commands=True))


@mcp.prompt()
def deploy_vllm_pipeline(program_dir: str) -> str:
    """Guide a safe deploy: discover caps, pre-flight, then deploy."""
    return (
        f"Deploy the pipeline program at {program_dir} to BlueBEAR. "
        "First read the bear://guardrails/allowed resource, then call check() with the "
        "intended qos/walltime/gpu_gres. Only call deploy() if check reports allowed=true. "
        "Report the run_ref so the run can be reattached with status()."
    )


@mcp.prompt()
def check_before_submit() -> str:
    """Remind: never submit without a guardrail pre-flight."""
    return (
        "Before any deploy, read bear://guardrails/allowed and call check() with the intended "
        "qos/walltime/gpu_gres. If denied, report which cap blocks it and the bear.toml key to "
        "widen rather than retrying blindly."
    )


@mcp.prompt()
def monitor_experiments() -> str:
    """Guide the agent to render and follow the live experiment dashboard."""
    return (
        "Call dashboard() for a structured snapshot of running/pending jobs and known runs, and "
        "render it as a compact status widget (or read the ui://dashboard resource for ready HTML). "
        "For a specific job, tail logs(run_ref) and report the state; when a run reads COMPLETED, "
        "call fetch(run_ref) to pull its artifacts. Trust squeue/sacct state, never a PID."
    )


def main() -> None:
    """stdio entry point for the ``bear-harness-mcp`` console script."""
    mcp.run()


__all__ = ["main", "mcp", "set_executor"]
