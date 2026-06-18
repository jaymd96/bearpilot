"""Click CLI entrypoint for ``bear-harness``.

Subcommands:

- ``validate``   — parse a pipeline.toml and print its normalised form
- ``launch``     — run a pipeline program against a local or SLURM vLLM
- ``status``     — read a run.json and render the latest snapshot
- ``logs``       — tail vllm.log / pipeline.log for a given run
- ``cancel``     — best-effort cancel of an in-flight run
- ``list``       — list known runs under runs_dir
- ``bootstrap``  — (Phase C stub) prepare a BlueBEAR environment

The CLI is deliberately thin: everything testable lives in
``_launch.py``, ``_manifest.py``, etc. This file wires options to
those modules and formats output.
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import click
import httpx
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from bear_harness._bear_config import (
    BearConfigError,
    LocalConfig,
    config_path_from_env,
    default_local_config,
    load_bear_config,
)
from bear_harness._bootstrap import (
    BootstrapError,
    BootstrapOptions,
    run_bootstrap,
)
from bear_harness._guardrails import (
    GuardrailDecision,
    evaluate_guardrails,
    resource_request_for,
    resource_request_from_graph,
)
from bear_harness._hosts import Host, HostsConfigError, load_hosts
from bear_harness._jobgraph import JobGraphError
from bear_harness._launch import (
    LaunchOptions,
    LaunchResult,
    cleanup_launch,
    run_launch,
)
from bear_harness._local_ollama import OllamaBackend
from bear_harness._local_ollama_runner import LocalOllamaRunner
from bear_harness._manifest import ManifestError, load_manifest
from bear_harness._messages_shim_server import MessagesShim
from bear_harness._preset import PresetContext, PresetError, get_preset, list_presets
from bear_harness._remote import (
    RemoteError,
    RemoteExecutor,
    read_remote_run,
)
from bear_harness._results import ResultsError, ResultsReport, resolve_results
from bear_harness._runner import LocalSubprocessRunner, Runner
from bear_harness._slurm_runner import SlurmRunner
from bear_harness._status import format_status_line, read_status
from bear_harness._status_follow import follow_run

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared option helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_config(config_path: Path | None, *, local: bool):
    """Prefer --config, then env var, then default path, then local fallback.

    ``--local`` no longer short-circuits: if ``bear.toml`` exists at the
    default path it is loaded (it may configure the Ollama backend or
    override other local-mode defaults). The flag only gates the final
    fallback to ``default_local_config()`` when no file is found.
    """
    if config_path is not None:
        return load_bear_config(config_path)
    env_path = config_path_from_env()
    if env_path is not None:
        return load_bear_config(env_path)
    try:
        return load_bear_config(None)  # tries default path; returns local default if missing
    except BearConfigError:
        if local:
            return default_local_config()
        raise


def _build_ollama_runner(local_cfg: LocalConfig) -> tuple[LocalOllamaRunner, list]:
    """Wire an ``OllamaBackend`` + ``MessagesShim`` + inner pipeline runner.

    Returns the composite runner and a list of resources that need to be
    ``.close()``-ed in the CLI's finally block (currently just the
    upstream ``httpx.Client`` — the shim + ollama are stopped via the
    runner's cancel path during ``cleanup_launch``).
    """
    if local_cfg.ollama is None:  # pragma: no cover — schema validation prevents this
        msg = "internal error: ollama backend selected but no ollama config parsed"
        raise BearConfigError(msg)
    ollama_cfg = local_cfg.ollama

    ollama = OllamaBackend(
        model=ollama_cfg.model,
        host=ollama_cfg.host,
        port=ollama_cfg.port,
    )
    # The httpx.Client is lazy — it does not open a connection until the
    # first request lands, which happens after the shim starts forwarding.
    # So it is safe to construct before ``ollama.start()`` has run.
    # Generous timeout: thinking models (Qwen3, DeepSeek-R1) can spend
    # 30+ seconds on chain-of-thought before producing content.
    upstream = httpx.Client(base_url=f"{ollama.base_url}", timeout=300.0)
    shim = MessagesShim(
        upstream_client=upstream,
        served_model_name=ollama_cfg.model,
        thinking_budget=ollama_cfg.thinking_budget,
    )
    inner = LocalSubprocessRunner(endpoints_dir=local_cfg.endpoints_dir)
    runner = LocalOllamaRunner(
        endpoints_dir=local_cfg.endpoints_dir,
        ollama=ollama,
        shim=shim,
        pipeline_runner=inner,
    )
    return runner, [upstream]


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="bear-harness")
@click.option("-v", "--verbose", is_flag=True, help="Enable DEBUG logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Turnkey SLURM + vLLM harness for Python pipeline programs."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# Remote transport helpers (the --remote / fetch / ps / remote install surface)
# ---------------------------------------------------------------------------

_REMOTE_HELP = (
    "Drive a remote BlueBEAR host (by name from hosts.toml) over SSH instead of running locally."
)


def _remote_option(func: Callable) -> Callable:
    """Shared ``--remote HOST`` option for launch / status / logs / cancel."""
    return click.option(
        "--remote", "remote_host", default=None, metavar="HOST", help=_REMOTE_HELP
    )(func)


def _resolve_host(host_name: str | None) -> Host:
    """Resolve a host from ``hosts.toml`` (``BEAR_HARNESS_HOSTS`` overrides the path)."""
    import os

    env = os.environ.get("BEAR_HARNESS_HOSTS")
    return load_hosts(Path(env) if env else None).resolve(host_name)


def _make_remote_executor(host: Host) -> RemoteExecutor:
    """Build the SSH executor for ``host``. A seam: tests monkeypatch this."""
    return RemoteExecutor(host=host)


def _remote_inbox_name(program_dir: Path) -> str:
    return f"{program_dir.name}-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}"


def _remote_fail(msg: str, *, json_output: bool) -> NoReturn:
    (err_console if json_output else console).print(f"[red]error:[/red] {escape(msg)}")
    sys.exit(1)


def _launch_remote(host_name: str | None, program_dir: Path, *, json_output: bool) -> None:
    """Upload + launch a program on a remote host, detached; print the run-ref."""
    try:
        host = _resolve_host(host_name)
    except HostsConfigError as exc:
        _remote_fail(str(exc), json_output=json_output)
    ex = _make_remote_executor(host)
    try:
        run = ex.launch_detached(program_dir, _remote_inbox_name(program_dir))
    except RemoteError as exc:
        _remote_fail(str(exc), json_output=json_output)
    if json_output:
        click.echo(json.dumps(run.as_dict(), indent=2))
    else:
        console.print(
            f"[green]launched[/green] {escape(run.run_ref)} on {escape(host.name)}\n"
            f"reattach: bear-harness status --remote {host.name} {run.run_ref}"
        )


def _status_remote(host_name: str | None, run_ref: str) -> None:
    """Reattach to a remote run by ref and print its ``run.json`` (via ``ssh cat``)."""
    try:
        host = _resolve_host(host_name)
        run = read_remote_run(host.name, run_ref)
        state = _make_remote_executor(host).read_run_json(run)
    except (HostsConfigError, RemoteError, json.JSONDecodeError) as exc:
        _remote_fail(str(exc), json_output=True)
    console.print_json(data=state)


def _logs_remote(host_name: str | None, run_ref: str, which: str, lines: int) -> None:
    try:
        host = _resolve_host(host_name)
        run = read_remote_run(host.name, run_ref)
    except (HostsConfigError, RemoteError) as exc:
        _remote_fail(str(exc), json_output=False)
    ex = _make_remote_executor(host)
    for name in ("vllm", "pipeline"):
        if which not in {name, "both"}:
            continue
        res = ex.run(["tail", "-n", str(lines), f"{run.remote_run_dir}/{name}.log"])
        console.rule(f"{name}.log")
        console.print(res.stdout if res.ok else f"[yellow]({name}.log unavailable)[/yellow]")


def _cancel_remote(host_name: str | None, run_ref: str) -> None:
    try:
        host = _resolve_host(host_name)
        run = read_remote_run(host.name, run_ref)
    except (HostsConfigError, RemoteError) as exc:
        _remote_fail(str(exc), json_output=False)
    _make_remote_executor(host).cancel(run)
    console.print(f"[green]cancel requested[/green] for {escape(run_ref)} on {escape(host.name)}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@cli.command()
@click.argument(
    "program_dir",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
)
def validate(program_dir: Path) -> None:
    """Parse and validate a program's ``pipeline.toml``."""
    try:
        manifest = load_manifest(program_dir)
    except ManifestError as exc:
        console.print(f"[red]error:[/red] {exc}")
        sys.exit(1)

    table = Table(title=f"{manifest.program.name} v{manifest.program.version}")
    table.add_column("section")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("program", "name", manifest.program.name)
    table.add_row("program", "version", manifest.program.version)
    table.add_row("runtime", "python", manifest.runtime.python)
    table.add_row("preset", "name", manifest.preset)
    if manifest.model is not None:
        table.add_row("model", "api", manifest.model.api)
        table.add_row("model", "default_model", manifest.model.default_model)
    table.add_row("entrypoint", "command", " ".join(manifest.entrypoint.command))
    table.add_row("artifacts", "collect", ", ".join(manifest.artifacts.collect))
    table.add_row("resources", "gpu_memory_gb", str(manifest.resources.gpu_memory_gb))
    table.add_row("resources", "walltime", manifest.resources.walltime)
    console.print(table)
    console.print(f"[green]ok[/green] — manifest at {manifest.program_root / 'pipeline.toml'}")


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


@cli.command()
@click.argument(
    "program_dir",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
)
@click.option("--local", is_flag=True, help="Run on the local host instead of SLURM.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
    help="Path to a bear.toml cluster config (default: ~/.config/bear-harness/bear.toml).",
)
@click.option("--model", default=None, help="Override manifest's model.default_model.")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Override the output directory the program writes into.",
)
@click.option("--dry-run", is_flag=True, help="Render commands, print, exit without submitting.")
@click.option(
    "--boot-timeout",
    type=float,
    default=900.0,
    help="Seconds to wait for the vLLM endpoint to become ready.",
)
@click.option(
    "--max-model-len",
    type=int,
    default=None,
    help="Override vLLM's --max-model-len.",
)
@click.option(
    "--python",
    "python_path",
    default=None,
    help="Python interpreter the pipeline program runs under (e.g. path to a venv's python).",
)
@click.option(
    "--gpu-gres",
    default=None,
    help="Override bear.toml gpu_gres (e.g. gpu:a100_80:2 for large models).",
)
@click.option(
    "--tensor-parallel-size",
    "tensor_parallel",
    type=int,
    default=None,
    help="Override bear.toml tensor_parallel_size.",
)
@click.option(
    "--extra-vllm-args",
    default=None,
    help='Extra vLLM flags (space-separated string, e.g. "--quantization fp8").',
)
@click.option(
    "--mem-gb",
    type=int,
    default=None,
    help="Override bear.toml mem_gb for the vLLM job (e.g. 160 for 70B models).",
)
@click.option(
    "--dtype",
    default=None,
    help="Override vLLM dtype (e.g. float16, bfloat16, auto).",
)
@click.option(
    "--qos",
    "qos_override",
    default=None,
    help="Override bear.toml QoS for both vLLM and pipeline jobs (e.g. bbshort for fast smoke tests).",
)
@click.option(
    "--walltime",
    "walltime_override",
    default=None,
    help='Override walltime for both vLLM and pipeline jobs (e.g. "00:10:00" — required with --qos bbshort, whose QoS cap is 10 minutes).',
)
@click.option(
    "--detach",
    is_flag=True,
    help="Submit the jobs and return a run handle without waiting for the run to finish (for agent / scripted use).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit the result as a JSON handle on stdout instead of a table (pairs with --detach).",
)
@_remote_option
@click.pass_context
def launch(
    ctx: click.Context,
    program_dir: Path,
    local: bool,
    config_path: Path | None,
    model: str | None,
    output_dir: Path | None,
    dry_run: bool,
    boot_timeout: float,
    max_model_len: int | None,
    python_path: str | None,
    gpu_gres: str | None,
    tensor_parallel: int | None,
    extra_vllm_args: str | None,
    mem_gb: int | None,
    dtype: str | None,
    qos_override: str | None,
    walltime_override: str | None,
    detach: bool,
    json_output: bool,
    remote_host: str | None,
) -> None:
    """Launch a pipeline program against a provisioned vLLM server.

    With ``--remote HOST`` the program is rsynced to the host and the orchestrator
    is started there detached (over SSH); the local run path is untouched.
    """
    if remote_host is not None:
        _launch_remote(remote_host, program_dir, json_output=json_output)
        return
    try:
        manifest = load_manifest(program_dir)
    except ManifestError as exc:
        console.print(f"[red]manifest error:[/red] {exc}")
        sys.exit(1)

    try:
        bear_config = _load_config(config_path, local=local)
    except BearConfigError as exc:
        console.print(f"[red]bear.toml error:[/red] {exc}")
        sys.exit(1)

    if not local and bear_config.is_local:
        # In --json mode keep stdout clean for the machine-readable handle.
        (err_console if json_output else console).print(
            "[yellow]warning:[/yellow] no SLURM config found; "
            "running in local mode. Pass --local to suppress this."
        )

    runner: Runner
    cleanup_extras: list = []  # resources to close in the finally block
    if bear_config.is_local:
        local_cfg = bear_config.require_local()
        if local_cfg.backend == "ollama":
            runner, cleanup_extras = _build_ollama_runner(local_cfg)
            # Default to the Ollama model name so the endpoint probe and
            # pipeline env vars use the name Ollama actually serves.
            if model is None:
                model = local_cfg.ollama.model  # type: ignore[union-attr]
        else:
            runner = LocalSubprocessRunner(
                endpoints_dir=local_cfg.endpoints_dir,
            )
    else:
        slurm_cfg = bear_config.require_slurm()
        runner = SlurmRunner(
            config=slurm_cfg,
            runs_dir=slurm_cfg.runs_dir,
        )

    # Concurrency-cap input: count the user's in-flight SLURM jobs via squeue
    # (node-agnostic, never PID). Local mode reserves no shared slots — no probe.
    concurrency_probe: Callable[[], int] | None = None
    if isinstance(runner, SlurmRunner):
        concurrency_probe = runner.count_active_jobs

    import shlex

    options = LaunchOptions(
        manifest=manifest,
        config=bear_config,
        model=model,
        output_dir=output_dir,
        python=python_path,
        dry_run=dry_run,
        vllm_boot_timeout_seconds=boot_timeout,
        max_model_len=max_model_len,
        extra_vllm_args=tuple(shlex.split(extra_vllm_args)) if extra_vllm_args else (),
        gpu_gres_override=gpu_gres,
        tensor_parallel_override=tensor_parallel,
        mem_gb_override=mem_gb,
        dtype=dtype,
        qos_override=qos_override,
        walltime_override=walltime_override,
    )

    result_ref: dict[str, LaunchResult | None] = {"result": None}
    stop_flag = {"stop": False}

    def _on_sigint(signum: int, frame: object) -> None:
        del signum, frame
        if stop_flag["stop"]:
            console.print("[red]second SIGINT received — exiting without cleanup[/red]")
            sys.exit(130)
        stop_flag["stop"] = True
        console.print("[yellow]SIGINT received — cancelling run...[/yellow]")
        if result_ref["result"] is not None:
            cleanup_launch(runner, result_ref["result"])

    # In --json mode, suppress streamed status lines so stdout carries only
    # the JSON handle (status still goes to the logger on stderr). A detached
    # run returns after the probe without streaming anyway.
    on_status = None if json_output else (lambda snap: console.print(format_status_line(snap)))
    previous = signal.signal(signal.SIGINT, _on_sigint)
    try:
        result = run_launch(
            options,
            runner,
            on_status=on_status,
            detach=detach,
            concurrency_probe=concurrency_probe,
        )
        result_ref["result"] = result
    finally:
        signal.signal(signal.SIGINT, previous)
        for resource in cleanup_extras:
            with contextlib.suppress(Exception):
                resource.close()

    if json_output:
        click.echo(json.dumps(result.as_dict(), indent=2))
    else:
        _render_launch_summary(result)
    # A detached deploy succeeds once the jobs are submitted (state "running");
    # a foreground run must reach a terminal "done".
    ok_states = {"done", "dry_run", "running"} if detach else {"done", "dry_run"}
    if result.final_state not in ok_states:
        sys.exit(1)


def _render_launch_summary(result: LaunchResult) -> None:
    table = Table(title=f"launch {result.job_id}")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("state", result.final_state)
    table.add_row("run_dir", str(result.run_dir))
    table.add_row("output_dir", str(result.output_dir))
    if result.endpoint is not None:
        table.add_row("base_url", result.endpoint.base_url)
        table.add_row("model", result.endpoint.model)
    if result.artifacts_tarball is not None:
        table.add_row("artifacts", str(result.artifacts_tarball))
    if result.guardrail is not None:
        verdict = "within caps" if result.guardrail["allowed"] else "DENIED"
        table.add_row(
            "guardrail",
            f"{verdict} (est {result.guardrail['est_gpu_hours']} GPU-hours)",
        )
        for violation in result.guardrail["violations"]:
            # escape: messages carry "[guardrails].key" which Rich would eat as markup
            table.add_row("cap breached", escape(violation["message"]))
    if result.error:
        table.add_row("error", escape(result.error))
    console.print(table)


def _render_guardrail_decision(decision: GuardrailDecision) -> None:
    table = Table(title="guardrail check")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("allowed", "yes" if decision.allowed else "NO")
    table.add_row("est_gpu_hours", f"{decision.est_gpu_hours:.3g}")
    for violation in decision.violations:
        # escape: messages carry "[guardrails].key" which Rich would eat as markup
        table.add_row("cap breached", escape(violation.message))
    console.print(table)


@cli.command()
@click.option("--local", is_flag=True, help="Force local mode (ignore SLURM config).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a bear.toml (defaults to the standard location).",
)
@click.option("--qos", "qos_override", default=None, help="QoS tier to check.")
@click.option(
    "--walltime",
    "walltime_override",
    default=None,
    help="Walltime to check (HH:MM:SS).",
)
@click.option("--gpu-gres", "gpu_gres", default=None, help="GPU GRES to check.")
@click.option("--json", "json_output", is_flag=True, help="Emit the decision as JSON on stdout.")
def check(
    local: bool,
    config_path: Path | None,
    qos_override: str | None,
    walltime_override: str | None,
    gpu_gres: str | None,
    json_output: bool,
) -> None:
    """Evaluate the default-deny guardrails for a would-be launch, without submitting.

    The agent-facing pre-flight — "would this request be allowed?". Exits
    non-zero if denied, naming each breached cap and the bear.toml key to widen.
    """
    try:
        bear_config = _load_config(config_path, local=local)
    except BearConfigError as exc:
        (err_console if json_output else console).print(f"[red]bear.toml error:[/red] {exc}")
        sys.exit(1)

    request = resource_request_for(
        bear_config,
        qos_override=qos_override,
        walltime_override=walltime_override,
        gpu_gres_override=gpu_gres,
    )
    decision = evaluate_guardrails(request, bear_config.guardrails)

    if json_output:
        click.echo(json.dumps(decision.as_dict(), indent=2))
    else:
        _render_guardrail_decision(decision)
    if not decision.allowed:
        sys.exit(1)


@cli.command()
@click.option("--local", is_flag=True, help="Force local mode (ignore SLURM config).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a bear.toml (defaults to the standard location).",
)
@click.option("--json", "json_output", is_flag=True, help="Emit the caps as JSON on stdout.")
def caps(local: bool, config_path: Path | None, json_output: bool) -> None:
    """Print the resource caps an agent must stay within (the guardrail allowlist).

    The discoverability companion to ``check``: ``check`` answers "is THIS request
    allowed?", ``caps`` advertises the envelope itself so an agent can choose a
    valid request up front. The MCP ``bear://guardrails/allowed`` resource is this
    command read over SSH.
    """
    try:
        bear_config = _load_config(config_path, local=local)
    except BearConfigError as exc:
        (err_console if json_output else console).print(f"[red]bear.toml error:[/red] {exc}")
        sys.exit(1)
    g = bear_config.guardrails
    payload = {
        "qos_allowlist": list(g.qos_allowlist),
        "max_walltime": g.max_walltime,
        "max_concurrent_jobs": g.max_concurrent_jobs,
        "gpu_hours_budget": g.gpu_hours_budget,
        "require_dry_run": g.require_dry_run,
    }
    if json_output:
        click.echo(json.dumps(payload, indent=2))
    else:
        table = Table(title="guardrail caps")
        table.add_column("cap")
        table.add_column("value", overflow="fold")
        for k, v in payload.items():
            table.add_row(k, str(v))
        console.print(table)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("target")
@_remote_option
@click.option(
    "--follow",
    is_flag=True,
    help="Keep polling and print one line per change until the run reaches a terminal state.",
)
@click.option(
    "--interval",
    type=float,
    default=10.0,
    show_default=True,
    help="Poll interval in seconds for --follow.",
)
def status(target: str, remote_host: str | None, follow: bool, interval: float) -> None:
    """Print the latest run.json + program status snapshot for a run.

    Locally ``target`` is a run directory; with ``--remote HOST`` it is a remote
    run-ref (resolved via the laptop pointer file, read back over ``ssh cat``).
    """
    if remote_host is not None:
        _status_remote(remote_host, target)
        return
    run_dir = Path(target)
    run_json = run_dir / "run.json"
    if not run_json.is_file() and not follow:
        console.print(f"[red]error:[/red] no run.json in {target}")
        sys.exit(1)
    if run_json.is_file():
        run_state = json.loads(run_json.read_text())
        console.print_json(data=run_state)
    status_file = run_dir / "output" / ".bear-harness-status.json"
    snap = read_status(status_file)
    if snap is not None:
        console.print(format_status_line(snap))

    if not follow:
        return
    final = follow_run(
        run_dir,
        emit=console.print,
        interval_seconds=interval,
    )
    if final not in {"done", "dry_run"}:
        sys.exit(1)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("target")
@_remote_option
@click.option("--which", type=click.Choice(["vllm", "pipeline", "both"]), default="both")
@click.option("-n", "--lines", type=int, default=50, help="Tail last N lines.")
def logs(target: str, remote_host: str | None, which: str, lines: int) -> None:
    """Print the tail of one or both log files from a run (local dir or --remote run-ref)."""
    if remote_host is not None:
        _logs_remote(remote_host, target, which, lines)
        return
    run_dir = Path(target)
    if not run_dir.is_dir():
        console.print(f"[red]error:[/red] no such run dir: {target}")
        sys.exit(1)
    for name in ("vllm", "pipeline"):
        if which not in {name, "both"}:
            continue
        log = run_dir / f"{name}.log"
        if not log.is_file():
            console.print(f"[yellow]({name}.log not found)[/yellow]")
            continue
        console.rule(f"{name}.log")
        content = log.read_text(errors="replace").splitlines()
        for line in content[-lines:]:
            console.print(line)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@cli.command()
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit the result as JSON instead of a table.",
)
@click.option(
    "--no-collect",
    "no_collect",
    is_flag=True,
    help="Only locate an existing tarball; do not collect on demand.",
)
def results(run_dir: Path, json_output: bool, no_collect: bool) -> None:
    """Locate (and lazily collect) a run's artifacts by run directory.

    Reattach to a detached run from its run dir alone: reads run.json off the
    shared FS, then returns the artifacts tarball — collecting it on demand
    from the program's output dir if a detached deploy left it uncollected.
    """
    try:
        report = resolve_results(run_dir, collect=not no_collect)
    except ResultsError as exc:
        (err_console if json_output else console).print(f"[red]error:[/red] {exc}")
        sys.exit(1)
    if json_output:
        click.echo(json.dumps(report.as_dict(), indent=2))
    else:
        _render_results(report)


def _render_results(report: ResultsReport) -> None:
    table = Table(title=f"results {report.run_id}")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("state", report.state)
    table.add_row("run_dir", str(report.run_dir))
    table.add_row("output_dir", str(report.output_dir))
    if report.artifacts_tarball is not None:
        label = "artifacts (collected now)" if report.collected_now else "artifacts"
        table.add_row(label, str(report.artifacts_tarball))
    else:
        table.add_row("artifacts", "(none yet)")
    if report.status is not None and report.status.message:
        table.add_row("message", report.status.message)
    console.print(table)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("target")
@_remote_option
def cancel(target: str, remote_host: str | None) -> None:
    """Cancel a run.

    Locally, kills the run's vllm + pipeline PIDs. With ``--remote HOST``
    (``target`` is a run-ref), ``scancel``s the SLURM jobs and reaps the
    orchestrator on the captured login node.
    """
    if remote_host is not None:
        _cancel_remote(remote_host, target)
        return
    run_dir = Path(target)
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        console.print(f"[red]error:[/red] no run.json in {target}")
        sys.exit(1)
    state = json.loads(run_json.read_text())
    for key in ("vllm_job_id", "pipeline_job_id"):
        pid_s = state.get(key, "")
        if not pid_s:
            continue
        try:
            import os

            os.kill(int(pid_s), signal.SIGTERM)
            console.print(f"sent SIGTERM to {key}={pid_s}")
        except (ProcessLookupError, ValueError):
            console.print(f"[yellow]{key}={pid_s} not running[/yellow]")


# ---------------------------------------------------------------------------
# fetch / ps — remote-only verbs
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("run_ref")
@_remote_option
@click.option("--json", "json_output", is_flag=True, help="Emit the result as JSON on stdout.")
def fetch(run_ref: str, remote_host: str | None, json_output: bool) -> None:
    """Pull a remote run's artifacts tarball down to the local cache."""
    try:
        host = _resolve_host(remote_host)
        run = read_remote_run(host.name, run_ref)
    except (HostsConfigError, RemoteError) as exc:
        _remote_fail(str(exc), json_output=json_output)
    dest = Path(host.artifacts_cache).expanduser() / run_ref
    res = _make_remote_executor(host).rsync_pull(f"{run.remote_run_dir}/artifacts.tar.gz", dest)
    if json_output:
        click.echo(
            json.dumps(
                {"run_ref": run_ref, "host": host.name, "fetched_to": str(dest), "ok": res.ok},
                indent=2,
            )
        )
    elif res.ok:
        console.print(f"[green]fetched[/green] {escape(run_ref)} -> {dest}")
    else:
        console.print(f"[yellow]fetch incomplete:[/yellow] {escape(res.stderr.strip())}")
    if not res.ok:
        sys.exit(1)


@cli.command()
@_remote_option
@click.option("--json", "json_output", is_flag=True, help="Emit the squeue summary as JSON.")
def ps(remote_host: str | None, json_output: bool) -> None:
    """Show your in-flight SLURM jobs on a remote host (``squeue --me``)."""
    try:
        host = _resolve_host(remote_host)
    except HostsConfigError as exc:
        _remote_fail(str(exc), json_output=json_output)
    res = _make_remote_executor(host).run(["squeue", "--me"])
    if json_output:
        click.echo(json.dumps({"host": host.name, "squeue": res.stdout, "ok": res.ok}, indent=2))
    elif res.ok:
        console.print(res.stdout or "[dim](no active jobs)[/dim]")
    else:
        console.print(f"[red]squeue failed:[/red] {escape(res.stderr.strip())}")
    if not res.ok:
        sys.exit(1)


# ---------------------------------------------------------------------------
# remote install — one-time cluster-side setup
# ---------------------------------------------------------------------------


@cli.group()
def remote() -> None:
    """Manage the cluster-side bear-harness binary on a remote host."""


@remote.command("install")
@click.argument("host_name", required=False)
def remote_install(host_name: str | None) -> None:
    """Push the local wheel to the host and ``pip install --user`` it (idempotent)."""
    try:
        host = _resolve_host(host_name)
    except HostsConfigError as exc:
        _remote_fail(str(exc), json_output=False)
    wheels = sorted(Path("dist").glob("bear_harness-*.whl"))
    if not wheels:
        _remote_fail("no wheel under ./dist — run `hatch build` first", json_output=False)
    wheel = wheels[-1]
    ex = _make_remote_executor(host)
    remote_wheels = f"{host.remote_rds_root}/.bear-harness/wheels"
    mk = ex.run(["mkdir", "-p", remote_wheels])
    if not mk.ok:
        _remote_fail(f"remote install (mkdir) failed: {mk.stderr.strip()}", json_output=False)
    up = ex.rsync_push_file(wheel, remote_wheels)
    if not up.ok:
        _remote_fail(f"remote install (upload) failed: {up.stderr.strip()}", json_output=False)
    inst = ex.run(["pip", "install", "--user", "--force-reinstall", f"{remote_wheels}/{wheel.name}"])
    if not inst.ok:
        _remote_fail(f"remote install (install) failed: {inst.stderr.strip()}", json_output=False)
    console.print(f"[green]installed[/green] {wheel.name} on {host.name}")


# ---------------------------------------------------------------------------
# presets — the declarative authoring kit (W4)
# ---------------------------------------------------------------------------


@cli.group()
def presets() -> None:
    """Inspect and validate the available presets (the declarative authoring kit)."""


@presets.command("list")
def presets_list() -> None:
    """List the registered presets and their topology/summary."""
    table = Table(title="presets")
    table.add_column("name")
    table.add_column("topology")
    table.add_column("summary", overflow="fold")
    for name in list_presets():
        d = get_preset(name).describe()
        table.add_row(name, str(d.get("topology", "")), str(d.get("summary", "")))
    console.print(table)


@presets.command("describe")
@click.argument("name")
def presets_describe(name: str) -> None:
    """Print a preset's contract shape as JSON."""
    try:
        preset = get_preset(name)
    except PresetError as exc:
        console.print(f"[red]error:[/red] {escape(str(exc))}")
        sys.exit(1)
    click.echo(json.dumps(preset.describe(), indent=2))


@presets.command("validate")
@click.argument(
    "program_dir",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
)
@click.option("--local", is_flag=True, help="Force local mode (ignore SLURM config).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a bear.toml (defaults to the standard location).",
)
@click.option("--json", "json_output", is_flag=True, help="Emit the result as JSON on stdout.")
def presets_validate(
    program_dir: Path,
    local: bool,
    config_path: Path | None,
    json_output: bool,
) -> None:
    """Validate a program end-to-end WITHOUT submitting: manifest → JobGraph → caps.

    The pre-submit half of the authoring kit and the agent's safety check: load the
    manifest, validate it against its declared preset, lower it to a JobGraph, and check
    the graph's resources against the guardrail caps. Exits non-zero on any failure —
    an invalid manifest, an unknown preset, a malformed graph, or a cap breach.
    """
    try:
        manifest = load_manifest(program_dir)
        bear_config = _load_config(config_path, local=local)
        preset = get_preset(manifest.preset)
        preset.validate_manifest(manifest)
        context = PresetContext(
            manifest=manifest,
            config=bear_config,
            job_id="validate",
            run_dir=Path("."),
            output_dir=Path("."),
            server_log=Path("."),
            worker_log=Path("."),
            endpoint_path=Path("."),
            python="python3",
        )
        graph = preset.lower(context)
    except (ManifestError, BearConfigError, PresetError, JobGraphError) as exc:
        if json_output:
            click.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            console.print(f"[red]invalid:[/red] {escape(str(exc))}")
        sys.exit(1)

    decision = evaluate_guardrails(resource_request_from_graph(graph), bear_config.guardrails)
    if json_output:
        payload = {
            "ok": decision.allowed,
            "preset": manifest.preset,
            "topology": graph.topology,
            "jobs": [j.as_dict() for j in graph.jobs],
            "guardrail": decision.as_dict(),
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            f"preset [bold]{manifest.preset}[/bold] · topology [bold]{graph.topology}[/bold] "
            f"· {len(graph.jobs)} job(s)"
        )
        _render_guardrail_decision(decision)
    if not decision.allowed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@cli.command("list")
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
)
def list_runs(runs_dir: Path | None) -> None:
    """List known runs under ``runs_dir`` with their state."""
    if runs_dir is None:
        runs_dir = default_local_config().require_local().runs_dir
    if not runs_dir.is_dir():
        console.print(f"[yellow]no runs directory at {runs_dir}[/yellow]")
        return
    table = Table(title=f"runs under {runs_dir}")
    table.add_column("job_id")
    table.add_column("state")
    table.add_column("model")
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        run_json = entry / "run.json"
        if not run_json.is_file():
            continue
        try:
            state = json.loads(run_json.read_text())
        except json.JSONDecodeError:
            continue
        table.add_row(
            state.get("job_id", entry.name),
            state.get("state", "?"),
            state.get("model", "?"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# bootstrap — Phase C stub
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--rds-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="Project-scoped RDS directory, e.g. /rds/projects/X/Y.",
)
@click.option(
    "--account",
    required=True,
    help="BlueBEAR SLURM --account value (your project code).",
)
@click.option(
    "--apptainer-image",
    default="docker://vllm/vllm-openai:latest",
    show_default=True,
    help="Docker reference apptainer should pull for the vLLM image.",
)
@click.option(
    "--cuda-module",
    default="CUDA/12.1.1",
    show_default=True,
    help="Module name to load before running vLLM.",
)
@click.option(
    "--gpu-gres",
    default="gpu:a100_40:1",
    show_default=True,
    help="SLURM --gres string for the vLLM job.",
)
@click.option(
    "--qos",
    default="bbgpu",
    show_default=True,
    help="SLURM QoS for the vLLM job.",
)
@click.option(
    "--config-path",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override bear.toml destination (default: ~/.config/bear-harness/bear.toml).",
)
@click.option(
    "--skip-pull",
    is_flag=True,
    help="Skip `apptainer pull` (assume the .sif is already present).",
)
@click.option(
    "--mail-user",
    default=None,
    help="Email address for SLURM job notifications (BEGIN/END/FAIL).",
)
def bootstrap(
    rds_root: Path,
    account: str,
    apptainer_image: str,
    cuda_module: str,
    gpu_gres: str,
    qos: str,
    config_path: Path | None,
    skip_pull: bool,
    mail_user: str | None,
) -> None:
    """Bootstrap a BlueBEAR environment: pull the vLLM apptainer image, create RDS
    directories, detect CUDA modules, and write ``bear.toml``.
    """
    options = BootstrapOptions(
        rds_root=rds_root,
        account=account,
        apptainer_image=apptainer_image,
        cuda_module=cuda_module,
        gpu_gres=gpu_gres,
        qos=qos,
        config_path=config_path,
        skip_pull=skip_pull,
        mail_user=mail_user,
    )
    try:
        report = run_bootstrap(options)
    except BootstrapError as exc:
        console.print(f"[red]bootstrap error:[/red] {exc}")
        sys.exit(2)

    for step in report.steps:
        console.print(f"[green]✓[/green] {step}")
    for warning in report.warnings:
        console.print(f"[yellow]![/yellow] {warning}")
    if report.config_path is not None:
        console.print(f"\n[bold]config written to[/bold] {report.config_path}")


if __name__ == "__main__":  # pragma: no cover
    cli()
