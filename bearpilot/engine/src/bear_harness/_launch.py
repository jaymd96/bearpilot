"""The ``launch`` control flow — lower a workload to a JobGraph, realise it on a runner.

This module is the heart of the harness. It drives a full run from a parsed
manifest and a configured runner, independently of how the CLI wraps it. The CLI
(``_cli.py``) is a thin layer that parses options and calls :func:`run_launch`.
Tests call :func:`run_launch` directly with stub runners — no subprocess is needed
to exercise the control flow.

The flow (W3 — the contract extracted):

1. Resolve run metadata (job_id, run_dir, log paths) and write ``run.json`` in
   ``initializing``.
2. Evaluate the default-deny guardrails (a dry-run reports here; a real launch is
   gated before any submit).
3. Lower the workload to a :class:`~bear_harness._jobgraph.JobGraph` (the reference
   vLLM+pipeline preset) and walk it generically (:func:`_realise_graph`): submit
   each job in dependency order, await + probe each published record, thread records
   to downstream consumers. The kernel reads the graph; the backend
   (``_reference_preset.ReferenceBackend``) holds the vLLM-specific spec-building.
4. With ``detach=True`` the flow returns once every job is submitted and the endpoint
   probed (the seam that makes ``deploy`` an LLM tool: bounded return, a run id to
   attach to later). Otherwise it streams status, waits for the worker, collects
   artifacts, scancels the sidecar(s) (read from the graph's roles), and transitions
   to done / failed.

The SIGINT handler is installed by the CLI, not by this module, so a test can drive
``run_launch`` without touching the signal module.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bear_harness import (
    _preset_etl,  # noqa: F401  (registers the etl preset)
    _reference_preset,  # noqa: F401  (registers the vllm-pipeline preset)
)
from bear_harness._artifacts import collect_artifacts
from bear_harness._bear_config import BearConfig
from bear_harness._endpoint_discovery import (
    EndpointDiscoveryError,
    EndpointProbeError,
    EndpointRecord,
    probe_endpoint,
    wait_for_endpoint_file,
)
from bear_harness._guardrails import (
    evaluate_guardrails,
    resource_request_from_graph,
)
from bear_harness._jobgraph import JobGraph, JobGraphError, Role
from bear_harness._manifest import Manifest
from bear_harness._notify import NotifyEvent, NotifyOutcome, fire_notification
from bear_harness._preset import Backend, PresetContext, PresetError, get_preset
from bear_harness._runner import JobHandle, JobState, Runner
from bear_harness._status import StatusSnapshot, format_status_line, read_status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlurmOverrides:
    """Per-launch overrides for SLURM + vLLM settings.

    Any field left as ``None`` / empty means "use the bear.toml default".
    These travel alongside the ``VllmSpec`` and ``PipelineSpec`` so the
    caller can switch GPU tier, tensor parallelism, or vLLM flags without
    editing ``bear.toml``.
    """

    gpu_gres: str | None = None
    tensor_parallel_size: int | None = None
    mem_gb: int | None = None
    extra_vllm_args: tuple[str, ...] = ()
    dtype: str | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    qos: str | None = None
    walltime: str | None = None


@dataclass(slots=True)
class LaunchOptions:
    """User-facing inputs to :func:`run_launch`.

    ``model`` overrides ``manifest.model.default_model`` when set.
    ``output_dir`` defaults to ``run_dir/output`` if None. ``extra_vllm_args``
    is passed straight through to the vLLM CLI, escape-free.
    """

    manifest: Manifest
    config: BearConfig
    model: str | None = None
    output_dir: Path | None = None
    run_dir_override: Path | None = None
    python: str | None = None
    dry_run: bool = False
    extra_vllm_args: tuple[str, ...] = ()
    max_model_len: int | None = None
    vllm_boot_timeout_seconds: float = 900.0
    status_poll_interval_seconds: float = 10.0
    gpu_gres_override: str | None = None
    tensor_parallel_override: int | None = None
    mem_gb_override: int | None = None
    dtype: str | None = None
    qos_override: str | None = None
    walltime_override: str | None = None


@dataclass(slots=True)
class LaunchResult:
    """Return value of :func:`run_launch`.

    ``final_state`` is one of ``done`` / ``failed`` / ``cancelled`` and
    mirrors ``state`` in the final ``run.json``.
    """

    job_id: str
    run_dir: Path
    output_dir: Path
    endpoint: EndpointRecord | None
    vllm_handle: JobHandle | None
    pipeline_handle: JobHandle | None
    artifacts_tarball: Path | None
    final_state: str
    error: str | None = None
    last_status: StatusSnapshot | None = None
    guardrail: dict | None = None
    notify: dict | None = None

    def as_dict(self) -> dict[str, object]:
        """The machine-readable handle — agent-facing projection of a run.

        Everything a caller needs to reason about or reattach to a run
        by id: the ids, the run dir, the resolved state, the endpoint.
        Kept JSON-serialisable (paths → str) so the CLI ``--json`` path
        is a one-liner and this shape is the stable contract a tool
        wraps. Mirrors ``_RunState.as_dict`` for run.json.
        """
        return {
            "job_id": self.job_id,
            "state": self.final_state,
            "run_dir": str(self.run_dir),
            "output_dir": str(self.output_dir),
            "vllm_job_id": self.vllm_handle.job_id if self.vllm_handle else "",
            "pipeline_job_id": (self.pipeline_handle.job_id if self.pipeline_handle else ""),
            "base_url": self.endpoint.base_url if self.endpoint else "",
            "model": self.endpoint.model if self.endpoint else "",
            "artifacts": (str(self.artifacts_tarball) if self.artifacts_tarball else None),
            "error": self.error,
            "guardrail": self.guardrail,
            "notify": self.notify,
        }


@dataclass(slots=True)
class _RunDirLayout:
    """Paths inside a run directory."""

    run_dir: Path
    output_dir: Path
    vllm_log: Path
    pipeline_log: Path
    endpoint_path: Path
    state_file: Path
    artifacts_tarball: Path


def _build_layout(
    *,
    runs_dir: Path,
    endpoints_dir: Path,
    job_id: str,
    output_dir_override: Path | None,
) -> _RunDirLayout:
    run_dir = runs_dir / job_id
    output_dir = output_dir_override or (run_dir / "output")
    return _RunDirLayout(
        run_dir=run_dir,
        output_dir=output_dir,
        vllm_log=run_dir / "vllm.log",
        pipeline_log=run_dir / "pipeline.log",
        endpoint_path=endpoints_dir / f"{job_id}.json",
        state_file=run_dir / "run.json",
        artifacts_tarball=run_dir / "artifacts.tar.gz",
    )


# ---------------------------------------------------------------------------
# run.json writer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunState:
    """Per-run state file (``run.json``). Not the program status file.

    Distinct from ``.bear-harness-status.json`` which is written by the
    program — ``run.json`` is written by the harness and tracks
    harness-level progress.
    """

    job_id: str
    state: str
    manifest_path: str
    model: str
    started_at: float
    updated_at: float = 0.0
    vllm_job_id: str = ""
    pipeline_job_id: str = ""
    base_url: str = ""
    last_error: str = ""
    notes: dict = field(default_factory=dict)
    output_dir: str = ""
    artifact_patterns: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "state": self.state,
            "manifest_path": self.manifest_path,
            "model": self.model,
            "started_at": self.started_at,
            "started_at_iso": datetime.fromtimestamp(self.started_at, tz=UTC).isoformat(),
            "updated_at": self.updated_at,
            "vllm_job_id": self.vllm_job_id,
            "pipeline_job_id": self.pipeline_job_id,
            "base_url": self.base_url,
            "last_error": self.last_error,
            "notes": self.notes,
            "output_dir": self.output_dir,
            "artifact_patterns": list(self.artifact_patterns),
        }


def _write_state(path: Path, state: _RunState) -> None:
    state.updated_at = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.as_dict(), indent=2) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Main launch flow
# ---------------------------------------------------------------------------


def _make_job_id(program_name: str) -> str:
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"{program_name}-{ts}-{uuid4().hex[:8]}"


def _resolve_paths(options: LaunchOptions) -> tuple[str, _RunDirLayout]:
    config = options.config
    if config.is_local:
        local = config.require_local()
        runs_dir = local.runs_dir
        endpoints_dir = local.endpoints_dir
    else:
        slurm = config.require_slurm()
        runs_dir = slurm.runs_dir
        endpoints_dir = slurm.endpoints_dir

    job_id = _make_job_id(options.manifest.program.name)
    layout = _build_layout(
        runs_dir=options.run_dir_override or runs_dir,
        endpoints_dir=endpoints_dir,
        job_id=job_id,
        output_dir_override=options.output_dir,
    )
    return job_id, layout


def run_launch(
    options: LaunchOptions,
    runner: Runner,
    *,
    on_status: Callable[[StatusSnapshot], None] | None = None,
    detach: bool = False,
    concurrency_probe: Callable[[], int] | None = None,
    notifier: Callable[[NotifyEvent], NotifyOutcome] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> LaunchResult:
    """Execute the launch control flow for one program run.

    Caller owns signal handling: if the process is SIGINT-ed mid-run,
    the caller should call :func:`cleanup_launch` on the returned
    partial result. This keeps the core flow linear and testable.

    With ``detach=True`` the flow stops once every job is submitted and
    each published endpoint probed, returning a handle in ``running``
    state — it does not wait for the pipeline, collect artifacts, or
    cancel the sidecar. This is the seam that makes ``deploy`` an LLM tool:
    bounded return time, a run id to attach to later. The cut sits
    *after* the probe because the pipeline command bakes in the
    endpoint url at submit time, so a detached run still fails loudly
    if the server never comes up.
    """
    sleep = _sleep or time.sleep
    # The model is the vLLM preset's concern; a model-less preset (ETL) resolves to "".
    manifest_model = options.manifest.model
    model = options.model or (manifest_model.default_model if manifest_model else "")
    notify_config = options.config.notify
    notify_fn = notifier or (lambda event: fire_notification(notify_config, event))

    job_id, layout = _resolve_paths(options)
    layout.run_dir.mkdir(parents=True, exist_ok=True)
    layout.output_dir.mkdir(parents=True, exist_ok=True)

    run_state = _RunState(
        job_id=job_id,
        state="initializing",
        manifest_path=str(options.manifest.program_root / "pipeline.toml"),
        model=model,
        started_at=time.time(),
        output_dir=str(layout.output_dir),
        artifact_patterns=tuple(options.manifest.artifacts.collect),
    )
    _write_state(layout.state_file, run_state)

    logger.info("launch job_id=%s model=%s run_dir=%s", job_id, model, layout.run_dir)

    # Select the preset and lower the workload to a JobGraph (pure — no submit yet). The
    # manifest declares its preset; an unknown preset, or a manifest missing what the
    # preset needs, is a clean terminal failure rather than a crash.
    overrides = SlurmOverrides(
        gpu_gres=options.gpu_gres_override,
        tensor_parallel_size=options.tensor_parallel_override,
        mem_gb=options.mem_gb_override,
        extra_vllm_args=options.extra_vllm_args,
        dtype=options.dtype,
        max_model_len=options.max_model_len,
        qos=options.qos_override,
        walltime=options.walltime_override,
    )
    context = PresetContext(
        manifest=options.manifest,
        config=options.config,
        job_id=job_id,
        run_dir=layout.run_dir,
        output_dir=layout.output_dir,
        server_log=layout.vllm_log,
        worker_log=layout.pipeline_log,
        endpoint_path=layout.endpoint_path,
        python=options.python or _default_python(options.config),
        model=model,
        overrides=overrides,
        boot_timeout_seconds=options.vllm_boot_timeout_seconds,
    )
    try:
        preset = get_preset(options.manifest.preset)
        preset.validate_manifest(options.manifest)
        graph = preset.lower(context)
    except (PresetError, JobGraphError) as exc:
        return _fail(
            layout,
            run_state,
            error=str(exc),
            vllm_handle=None,
            pipeline_handle=None,
            endpoint=None,
            notifier=notify_fn,
            model=model,
        )

    # Default-deny guardrails, derived from the LOWERED GRAPH so a model-less ETL launch
    # is CPU-checked, not phantom-GPU-checked. Evaluated once: a dry-run reports the
    # verdict without submitting; a real launch is gated just below. Governs RESOURCES
    # only (qos / walltime / GPU-hours / concurrency), never science.
    in_flight = concurrency_probe() if concurrency_probe is not None else 0
    decision = evaluate_guardrails(
        resource_request_from_graph(graph, concurrent_jobs_in_flight=in_flight),
        options.config.guardrails,
    )

    if options.dry_run:
        # A dry-run that WOULD be denied reports "denied" (non-zero exit) so an
        # agent's pre-flight check fails clearly; an allowed one is "dry_run".
        state = "dry_run" if decision.allowed else "denied"
        run_state.state = state
        run_state.notes["guardrail"] = decision.as_dict()
        _write_state(layout.state_file, run_state)
        return LaunchResult(
            job_id=job_id,
            run_dir=layout.run_dir,
            output_dir=layout.output_dir,
            endpoint=None,
            vllm_handle=None,
            pipeline_handle=None,
            artifacts_tarball=None,
            final_state=state,
            error=None if decision.allowed else decision.reason(),
            guardrail=decision.as_dict(),
        )

    # Authoritative, un-bypassable gate: a denied request never reaches the runner — no
    # sbatch is issued, the denial is written to run.json, the failure is loud.
    if not decision.allowed:
        run_state.state = "denied"
        run_state.last_error = decision.reason()
        run_state.notes["guardrail"] = decision.as_dict()
        _write_state(layout.state_file, run_state)
        logger.warning("launch denied by guardrails: %s", decision.reason())
        return LaunchResult(
            job_id=job_id,
            run_dir=layout.run_dir,
            output_dir=layout.output_dir,
            endpoint=None,
            vllm_handle=None,
            pipeline_handle=None,
            artifacts_tarball=None,
            final_state="denied",
            error=decision.reason(),
            guardrail=decision.as_dict(),
        )

    # The graph is allowed — realise it on the runner. The kernel walks the graph
    # generically (submit order, published-record flow, sidecar teardown); the preset's
    # backend holds the workload-specific spec-building.
    backend = preset.make_backend(context, runner)

    walked = _realise_graph(
        graph,
        backend,
        runner=runner,
        run_state=run_state,
        layout=layout,
        model=model,
        notifier=notify_fn,
        sleep=sleep,
        boot_timeout_seconds=options.vllm_boot_timeout_seconds,
    )
    if isinstance(walked, LaunchResult):
        return walked  # a submit / endpoint / probe failure: run.json + notify already done

    # Map graph jobs back to the persisted handle fields by ROLE (byte-compat: the
    # sidecar's id is vllm_job_id, its consumer's is pipeline_job_id).
    sidecar_jobs = [j for j in graph.jobs if j.role is Role.SIDECAR]
    worker_jobs = [j for j in graph.jobs if j.role is Role.WORKER]
    vllm_handle = walked.handles.get(sidecar_jobs[0].name) if sidecar_jobs else None
    pipeline_handle = walked.handles.get(worker_jobs[0].name) if worker_jobs else None
    endpoint = None
    if sidecar_jobs and sidecar_jobs[0].publishes:
        endpoint = walked.records.get(sidecar_jobs[0].publishes[0].name)

    if detach:
        # Detached deploy: the jobs are live and the run dir is the handle. Stop here
        # — no babysitting wait, no artifact sweep, and crucially DO NOT cancel the
        # sidecar (the worker still needs it). Cleanup + collection move downstream,
        # keyed by run id: walltime / the orchestrator reaps the sidecar, ``results``
        # collects artifacts on demand.
        return LaunchResult(
            job_id=job_id,
            run_dir=layout.run_dir,
            output_dir=layout.output_dir,
            endpoint=endpoint,
            vllm_handle=vllm_handle,
            pipeline_handle=pipeline_handle,
            artifacts_tarball=None,
            final_state="running",
        )

    # Stream status + wait for the worker to reach a terminal state.
    status_file = backend.status_file(layout.output_dir / ".bear-harness-status.json")
    last_snapshot = _wait_for_pipeline(
        runner=runner,
        pipeline_handle=pipeline_handle,
        status_file=status_file,
        poll_interval=options.status_poll_interval_seconds,
        on_status=on_status,
        sleep=sleep,
    )

    final_pipeline_state = runner.poll(pipeline_handle)
    success = final_pipeline_state == JobState.COMPLETED

    # Collect artifacts.
    try:
        tarball = collect_artifacts(
            output_dir=layout.output_dir,
            patterns=options.manifest.artifacts.collect,
            extra_files=(layout.vllm_log, layout.pipeline_log),
            destination=layout.artifacts_tarball,
        )
    except Exception:
        logger.exception("artifact collection failed")
        tarball = None

    # Teardown: scancel every sidecar once its consumers have finished — read from the
    # graph's roles (the kernel knows a sidecar must be torn down, not what it served).
    _cancel_sidecars(graph, walked.handles, runner)

    run_state.state = "done" if success else "failed"
    if not success:
        run_state.last_error = f"pipeline terminal state: {final_pipeline_state}"
    # Fire-and-forget notify on the terminal transition (blocking path). The detached
    # path returns "running" above and never reaches here — its terminal notify is the
    # login-node orchestrator's job (W2 Lane C2), reusing this engine.
    notify_result = _notify_terminal(
        notify_fn,
        run_state=run_state,
        layout=layout,
        model=model,
        event_name="done" if success else "failed",
        error=run_state.last_error,
    )
    _write_state(layout.state_file, run_state)

    return LaunchResult(
        job_id=job_id,
        run_dir=layout.run_dir,
        output_dir=layout.output_dir,
        endpoint=endpoint,
        vllm_handle=vllm_handle,
        pipeline_handle=pipeline_handle,
        artifacts_tarball=tarball,
        final_state=run_state.state,
        last_status=last_snapshot,
        notify=notify_result,
    )


@dataclass(slots=True)
class _RealiseOk:
    """A successful graph walk: the submitted handles + the published records."""

    handles: dict[str, JobHandle]
    records: dict[str, EndpointRecord]


def _first_record(records: dict[str, EndpointRecord]) -> EndpointRecord | None:
    return next(iter(records.values()), None)


def _cancel_sidecars(graph: JobGraph, handles: dict[str, JobHandle], runner: Runner) -> None:
    """scancel every submitted sidecar — teardown read from the graph's roles."""
    for job in graph.jobs:
        if job.role is Role.SIDECAR and job.name in handles:
            try:
                runner.cancel(handles[job.name])
            except Exception:
                logger.exception("sidecar cancel during cleanup failed: %s", job.name)


def _realise_graph(
    graph: JobGraph,
    backend: Backend,
    *,
    runner: Runner,
    run_state: _RunState,
    layout: _RunDirLayout,
    model: str,
    notifier: Callable[[NotifyEvent], NotifyOutcome],
    sleep: Callable[[float], None],
    boot_timeout_seconds: float,
) -> _RealiseOk | LaunchResult:
    """Walk the JobGraph generically, realising each job on the runner via the backend.

    Submits jobs in dependency order (the preset emits the graph's job tuple
    topologically); after a publisher, waits for its record file (sidecar
    liveness-checked) and probes it, threading the record to downstream consumers.
    State labels and error strings are interpolated from ``job.name`` + role so
    ``run.json`` stays byte-identical to the pre-extraction flow (W3 plan D2). On any
    submit / endpoint / probe failure it scancels the submitted sidecars, writes the
    terminal ``failed`` state (firing notify), and returns the failure
    :class:`LaunchResult`.

    The endpoint readiness check (``wait_for_endpoint_file`` + ``probe_endpoint``)
    lives here, not in the backend, so the CLI/test probe monkeypatch still applies; a
    per-record-type verification strategy is a later generalisation (today every
    published record is the endpoint).
    """
    handles: dict[str, JobHandle] = {}
    records: dict[str, EndpointRecord] = {}

    def _sidecar_handle() -> JobHandle | None:
        for j in graph.jobs:
            if j.role is Role.SIDECAR and j.name in handles:
                return handles[j.name]
        return None

    for job in graph.jobs:
        # A job's dependencies are its incoming graph edges' upstreams (already
        # submitted, since we walk in dependency order). No incoming edge => no dep.
        deps = tuple(handles[e.upstream] for e in graph.edges if e.downstream == job.name)
        try:
            handle = backend.submit(job, records, deps)
        except Exception as exc:
            _cancel_sidecars(graph, handles, runner)
            return _fail(
                layout,
                run_state,
                error=f"failed to submit {job.name}: {exc}",
                vllm_handle=_sidecar_handle(),
                pipeline_handle=None,
                endpoint=_first_record(records),
                notifier=notifier,
                model=model,
            )
        handles[job.name] = handle

        if job.role is Role.SIDECAR:
            run_state.vllm_job_id = handle.job_id
            run_state.state = f"{job.name}_submitted"
        else:
            run_state.pipeline_job_id = handle.job_id
            run_state.state = "running"
        _write_state(layout.state_file, run_state)

        for record in job.publishes:
            try:
                endpoint = wait_for_endpoint_file(
                    layout.endpoint_path,
                    timeout_seconds=boot_timeout_seconds,
                    is_job_alive=lambda h=handle: runner.is_alive(h),
                    _sleep=sleep,
                )
            except EndpointDiscoveryError as exc:
                _cancel_sidecars(graph, handles, runner)
                return _fail(
                    layout,
                    run_state,
                    error=str(exc),
                    vllm_handle=_sidecar_handle(),
                    pipeline_handle=None,
                    endpoint=None,
                    notifier=notifier,
                    model=model,
                )
            records[record.name] = endpoint
            run_state.base_url = endpoint.base_url
            run_state.state = f"{job.name}_ready_unverified"
            _write_state(layout.state_file, run_state)

            try:
                probe_endpoint(endpoint, _sleep=sleep)
            except EndpointProbeError as exc:
                _cancel_sidecars(graph, handles, runner)
                return _fail(
                    layout,
                    run_state,
                    error=f"{job.name} probe failed: {exc}",
                    vllm_handle=_sidecar_handle(),
                    pipeline_handle=None,
                    endpoint=endpoint,
                    notifier=notifier,
                    model=model,
                )
            run_state.state = f"{job.name}_ready"
            _write_state(layout.state_file, run_state)

    return _RealiseOk(handles=handles, records=records)


def _default_python(config: BearConfig) -> str:
    if config.is_local:
        return config.require_local().python
    return "python3"


def _wait_for_pipeline(
    *,
    runner: Runner,
    pipeline_handle: JobHandle,
    status_file: Path,
    poll_interval: float,
    on_status,  # type: ignore[no-untyped-def]
    sleep,  # type: ignore[no-untyped-def]
) -> StatusSnapshot | None:
    last: StatusSnapshot | None = None
    while True:
        state = runner.poll(pipeline_handle)
        snap = read_status(status_file)
        if snap is not None:
            last = snap
            if on_status is not None:
                try:
                    on_status(snap)
                except Exception:
                    logger.debug("on_status callback raised", exc_info=True)
            else:
                logger.info(format_status_line(snap))
        if JobState.is_terminal(state):
            return last
        sleep(poll_interval)


def _notify_terminal(
    notifier: Callable[[NotifyEvent], NotifyOutcome],
    *,
    run_state: _RunState,
    layout: _RunDirLayout,
    model: str,
    event_name: str,
    error: str,
) -> dict | None:
    """Fire the terminal notification; record it in ``run.json`` notes if it fired.

    Fire-and-forget all the way down: ``notifier`` never raises (see
    ``_notify.fire_notification``), so a notification failure can never derail
    the terminal transition. Returns the outcome dict when a backend fired (for
    the JSON handle), or ``None`` when notify was disabled/gated off — keeping
    ``run.json`` and the handle clean in the common notify-off case.
    """
    try:
        outcome = notifier(
            NotifyEvent(
                event=event_name,
                run_id=run_state.job_id,
                state=run_state.state,
                run_dir=str(layout.run_dir),
                model=model,
                error=error,
            )
        )
    except Exception:  # notify must NEVER derail a terminal transition
        # Belt-and-suspenders: the default notifier (fire_notification) already
        # swallows backend failures, but the kernel guarantees the reliability bar
        # at its own boundary rather than trusting whatever callable was injected.
        logger.warning("notifier raised (swallowed); terminal state is unaffected", exc_info=True)
        return None
    if outcome.skipped:
        return None
    run_state.notes["notify"] = outcome.as_dict()
    return outcome.as_dict()


def _fail(
    layout: _RunDirLayout,
    state: _RunState,
    *,
    error: str,
    vllm_handle: JobHandle | None,
    pipeline_handle: JobHandle | None,
    endpoint: EndpointRecord | None,
    notifier: Callable[[NotifyEvent], NotifyOutcome],
    model: str,
) -> LaunchResult:
    state.state = "failed"
    state.last_error = error
    # An early failure (vllm/pipeline submit, endpoint, probe) is a terminal
    # "failed" transition too — fire on_fail so nobody waits on a dead run.
    notify_result = _notify_terminal(
        notifier,
        run_state=state,
        layout=layout,
        model=model,
        event_name="failed",
        error=error,
    )
    _write_state(layout.state_file, state)
    logger.error("launch failed: %s", error)
    return LaunchResult(
        job_id=state.job_id,
        run_dir=layout.run_dir,
        output_dir=layout.output_dir,
        endpoint=endpoint,
        vllm_handle=vllm_handle,
        pipeline_handle=pipeline_handle,
        artifacts_tarball=None,
        final_state="failed",
        error=error,
        notify=notify_result,
    )


def cleanup_launch(runner: Runner, result: LaunchResult) -> None:
    """Best-effort cleanup after a SIGINT or exception.

    Idempotent: safe to call even if the launch completed normally.
    """
    for handle in (result.pipeline_handle, result.vllm_handle):
        if handle is None:
            continue
        try:
            runner.cancel(handle)
        except Exception:
            logger.debug("cleanup cancel failed for %s", handle.job_id, exc_info=True)


__all__ = [
    "LaunchOptions",
    "LaunchResult",
    "cleanup_launch",
    "run_launch",
]
