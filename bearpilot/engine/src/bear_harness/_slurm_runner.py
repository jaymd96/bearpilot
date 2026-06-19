"""SLURM-backed :class:`Runner` implementation.

Sibling of :class:`LocalSubprocessRunner` — same interface, different
backend. The abstraction in ``_runner.py`` lets the harness main loop
drive either without branching.

Design rules:

- **One shell seam.** Every shell call goes through ``run_shell`` which
  is a thin wrapper around ``subprocess.run``. Tests inject a fake
  ``ShellRunner`` so the entire runner can be exercised without
  touching ``sbatch``/``squeue``/``scancel``.
- **Scripts are written to disk.** ``sbatch --wrap`` is tempting, but
  BlueBEAR operators want to ``less`` the script after a failure. We
  render to ``$RUNS_DIR/<job_id>/<kind>.sbatch`` and feed that path
  to sbatch.
- **``--parsable`` always.** ``sbatch --parsable`` emits
  ``<jobid>[;<cluster>]`` on stdout and nothing else. No scraping of
  human-readable output.
- **Dependency after, not afterok.** The pipeline should start while
  vLLM is still running — that is the whole point. ``after:$JID``
  fires as soon as vLLM enters RUNNING state; ``afterok`` would wait
  for it to terminate successfully.
"""

from __future__ import annotations

import getpass
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bear_harness._bear_config import SlurmConfig
from bear_harness._pipeline_launcher import (
    SlurmPipelineOptions,
    render_slurm_pipeline_script,
)
from bear_harness._runner import (
    JobHandle,
    JobState,
    PipelineSpec,
    Runner,
    VllmSpec,
)
from bear_harness._vllm_launcher import SlurmVllmOptions, render_slurm_vllm_script

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shell seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Thin wrapper over ``CompletedProcess`` for test injection."""

    returncode: int
    stdout: str
    stderr: str


ShellRunner = Callable[[Sequence[str]], ShellResult]


def _default_run(argv: Sequence[str]) -> ShellResult:
    """Real ``subprocess.run`` implementation used in production."""
    logger.debug("exec %s", list(argv))
    cp = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )
    return ShellResult(
        returncode=cp.returncode,
        stdout=cp.stdout or "",
        stderr=cp.stderr or "",
    )


# ---------------------------------------------------------------------------
# SLURM state mapping
# ---------------------------------------------------------------------------


# See `man squeue` — %T codes. Anything not listed here maps to UNKNOWN,
# which is safe (non-terminal, will be re-polled).
_SLURM_STATE_TO_JOB_STATE: dict[str, str] = {
    "PENDING": JobState.PENDING,
    "CONFIGURING": JobState.PENDING,
    "RESV_DEL_HOLD": JobState.PENDING,
    "REQUEUE_FED": JobState.PENDING,
    "REQUEUE_HOLD": JobState.PENDING,
    "REQUEUED": JobState.PENDING,
    "RESIZING": JobState.RUNNING,
    "SIGNALING": JobState.RUNNING,
    "SUSPENDED": JobState.RUNNING,
    "RUNNING": JobState.RUNNING,
    "STAGE_OUT": JobState.RUNNING,
    "COMPLETING": JobState.RUNNING,
    "COMPLETED": JobState.COMPLETED,
    "FAILED": JobState.FAILED,
    "NODE_FAIL": JobState.FAILED,
    "BOOT_FAIL": JobState.FAILED,
    "OUT_OF_MEMORY": JobState.FAILED,
    "PREEMPTED": JobState.CANCELLED,
    "DEADLINE": JobState.FAILED,
    "TIMEOUT": JobState.FAILED,
    "CANCELLED": JobState.CANCELLED,
    "REVOKED": JobState.CANCELLED,
    "SPECIAL_EXIT": JobState.FAILED,
}


def _map_state(raw: str) -> str:
    """Translate a SLURM ``%T`` state code to our ``JobState`` constant."""
    # SLURM occasionally prints composite states with a trailing '+'
    # (e.g. ``CANCELLED+``). Strip decorations before looking up.
    core = raw.strip().rstrip("+").upper()
    return _SLURM_STATE_TO_JOB_STATE.get(core, JobState.UNKNOWN)


# ---------------------------------------------------------------------------
# SlurmRunner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SlurmRunner(Runner):
    """``Runner`` backed by ``sbatch`` / ``squeue`` / ``scancel``.

    Build it with a :class:`SlurmConfig` (from ``bear.toml``) and a
    ``runs_dir`` (from ``bear-harness launch``). The runner itself is
    stateless beyond per-launch QoS/walltime overrides captured from the
    sidecar submit. Job dependencies come from ``PipelineSpec.depends_on``
    (the graph edge), realised as ``--dependency=after:$JID``; a worker with
    no incoming edge (e.g. ETL) gets no dependency. The source of truth for
    job state is SLURM via ``squeue``/``sacct``.
    """

    config: SlurmConfig
    runs_dir: Path
    run_shell: ShellRunner = field(default=_default_run)
    _qos_override: str | None = None
    _walltime_override: str | None = None

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:
        """Render the vLLM sbatch script from ``spec`` + config and submit it.

        The :class:`VllmSpec` is the ``Runner`` interface's payload;
        in SLURM mode the real argv is built by the Jinja template
        from :class:`SlurmConfig`. ``spec.serve_command`` (which would
        be the local-mode argv) is only consulted for metadata
        recovery (model id), not executed.

        ``overrides`` is an optional ``SlurmOverrides`` dataclass carrying
        per-launch overrides for GPU tier, tensor parallelism, etc.
        """
        overrides = kwargs.get("overrides")
        job_id = _job_id_from_log_path(spec.log_path)
        options = SlurmVllmOptions(
            model=_model_from_spec(spec),
            job_id=job_id,
            job_name=f"vllm-{job_id}",
            runs_dir=self.runs_dir,
            endpoints_dir=self.config.endpoints_dir,
            served_model_name=spec.served_model_name,
            gpu_gres_override=getattr(overrides, "gpu_gres", None),
            tensor_parallel_override=getattr(overrides, "tensor_parallel_size", None),
            mem_gb_override=getattr(overrides, "mem_gb", None),
            qos_override=getattr(overrides, "qos", None),
            walltime_override=getattr(overrides, "walltime", None),
            max_model_len=getattr(overrides, "max_model_len", None),
            gpu_memory_utilization=getattr(overrides, "gpu_memory_utilization", None),
            dtype=getattr(overrides, "dtype", None),
            extra_args=getattr(overrides, "extra_vllm_args", ()),
        )
        script = render_slurm_vllm_script(options, self.config)
        script_path = self._write_script(job_id, "vllm", script)

        slurm_job_id = self._sbatch_submit(script_path)
        self._qos_override = getattr(overrides, "qos", None)
        self._walltime_override = getattr(overrides, "walltime", None)
        logger.info(
            "submitted vllm sbatch slurm_job=%s harness_job=%s script=%s",
            slurm_job_id,
            job_id,
            script_path,
        )
        return JobHandle(
            job_id=slurm_job_id,
            log_path=spec.log_path,
            kind="vllm",
        )

    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
        """Render the pipeline sbatch script and submit it after the vLLM job.

        ``spec`` carries the fully-substituted entrypoint command and
        env, plus install/teardown hooks copied from the manifest.
        The SLURM wrapper sources a temp venv, runs the install
        commands inside it, exports the env vars, then execs the
        entrypoint.
        """
        if spec.output_dir is None or spec.status_file is None:
            msg = "SlurmRunner requires PipelineSpec.output_dir and .status_file to be set"
            raise RuntimeError(msg)

        job_id = _job_id_from_log_path(spec.log_path)
        # Dependencies come from the graph edge (PipelineSpec.depends_on), not from
        # mutable runner state: a coupled worker depends on its sidecar; a single-job
        # (ETL) worker depends on nothing and gets no --dependency. The first dep id
        # also gates the endpoint-wait block in the template (empty => no wait).
        dep_ids = tuple(h.job_id for h in spec.depends_on)
        # Pipeline (CPU) QoS/walltime fall back to the CONFIGURED values, not a
        # hardcoded "bbcpu"/8h: the bbshort canary proved "bbcpu" is not a valid
        # QoS on every BlueBEAR account. Precedence: per-launch override >
        # [slurm].cpu_qos > [slurm].qos.
        pipeline_qos = self._qos_override or self.config.cpu_qos or self.config.qos
        pipeline_walltime = self._walltime_override or self.config.walltime
        options = SlurmPipelineOptions(
            job_id=job_id,
            job_name=f"pipeline-{job_id}",
            vllm_job_id=dep_ids[0] if dep_ids else "",
            runs_dir=self.runs_dir,
            endpoints_dir=self.config.endpoints_dir,
            output_dir=spec.output_dir,
            status_file=spec.status_file,
            python=spec.python,
            program_root=spec.cwd,
            entrypoint_command=spec.command,
            extra_env=dict(spec.env),
            install_commands=spec.install,
            teardown_commands=spec.teardown,
            cuda_module=self.config.cuda_module,
            pipeline_qos=pipeline_qos,
            walltime=pipeline_walltime,
        )
        script = render_slurm_pipeline_script(options, self.config)
        script_path = self._write_script(job_id, "pipeline", script)

        dependency_args = (f"--dependency=after:{':'.join(dep_ids)}",) if dep_ids else ()
        slurm_job_id = self._sbatch_submit(script_path, extra_args=dependency_args)
        logger.info(
            "submitted pipeline sbatch slurm_job=%s depends_on=%s",
            slurm_job_id,
            ",".join(dep_ids) or "(none)",
        )
        return JobHandle(
            job_id=slurm_job_id,
            log_path=spec.log_path,
            kind="pipeline",
        )

    def poll(self, handle: JobHandle) -> str:
        """Return the current job state, translating SLURM codes to our constants.

        Uses ``squeue -h -j <id> -o %T``. If the job has already left
        the active queue, squeue prints nothing and we fall back to
        ``sacct`` to pick up the terminal state.
        """
        result = self.run_shell(("squeue", "-h", "-j", handle.job_id, "-o", "%T"))
        raw = result.stdout.strip()
        if raw:
            return _map_state(raw)
        # Job no longer in squeue — check sacct for terminal state.
        sacct = self.run_shell(
            (
                "sacct",
                "-n",
                "-P",
                "-j",
                handle.job_id,
                "-o",
                "JobID,State",
            )
        )
        for line in sacct.stdout.splitlines():
            parts = line.split("|", maxsplit=1)
            if len(parts) != 2:
                continue
            jid, state = parts
            # Only the main job record — skip ``.batch`` / ``.extern`` steps.
            if jid == handle.job_id:
                return _map_state(state)
        return JobState.UNKNOWN

    def cancel(self, handle: JobHandle) -> None:
        """Best-effort ``scancel``. Failures are logged and swallowed."""
        result = self.run_shell(("scancel", handle.job_id))
        if result.returncode != 0:
            logger.warning(
                "scancel %s failed rc=%d stderr=%s",
                handle.job_id,
                result.returncode,
                result.stderr.strip(),
            )

    def count_active_jobs(self) -> int:
        """Count this user's active (pending + running) SLURM jobs via ``squeue``.

        Backs the guardrail concurrency cap. Keys on ``squeue`` — node-agnostic
        and authoritative — never on PID liveness (the observability invariant).
        A ``squeue`` failure is logged and counted as 0 so a transient hiccup
        cannot wedge every launch; the other caps still bind regardless.
        """
        who = getpass.getuser()
        result = self.run_shell(("squeue", "-h", "-u", who, "-o", "%i"))
        if result.returncode != 0:
            logger.warning(
                "squeue concurrency count failed rc=%d stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return 0
        return sum(1 for line in result.stdout.splitlines() if line.strip())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sbatch_submit(
        self,
        script_path: Path,
        *,
        extra_args: Sequence[str] = (),
    ) -> str:
        """Submit ``script_path`` to SLURM and return the parsed job id."""
        argv: list[str] = ["sbatch", "--parsable", *extra_args, str(script_path)]
        result = self.run_shell(argv)
        if result.returncode != 0:
            msg = (
                f"sbatch failed rc={result.returncode} "
                f"stderr={result.stderr.strip()!r} script={script_path}"
            )
            raise RuntimeError(msg)
        raw = result.stdout.strip()
        if not raw:
            msg = f"sbatch returned empty stdout for {script_path}"
            raise RuntimeError(msg)
        return raw.split(";", maxsplit=1)[0]

    def _write_script(self, job_id: str, kind: str, body: str) -> Path:
        """Persist a rendered sbatch script under ``<runs_dir>/<job_id>/<kind>.sbatch``."""
        run_dir = self.runs_dir / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        script_path = run_dir / f"{kind}.sbatch"
        script_path.write_text(body)
        script_path.chmod(0o750)
        return script_path


def _job_id_from_log_path(log_path: Path) -> str:
    """Harness job id is the run-dir name — recovered from the log path.

    ``log_path`` looks like ``<runs_dir>/<job_id>/{vllm,pipeline}.log``.
    """
    return log_path.parent.name


def _model_from_spec(spec: VllmSpec) -> str:
    """Recover the underlying model id from the ``VllmSpec``.

    ``build_local_vllm_spec`` places the model as positional arg #2
    (``vllm serve <model> ...``). We fall back to ``served_model_name``
    which is the same in local mode.
    """
    cmd = spec.serve_command
    if len(cmd) >= 3 and cmd[0] == "vllm" and cmd[1] == "serve":
        return cmd[2]
    return spec.served_model_name


__all__ = [
    "ShellResult",
    "ShellRunner",
    "SlurmRunner",
]
