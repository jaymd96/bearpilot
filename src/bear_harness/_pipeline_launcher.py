"""Build the ``PipelineSpec`` consumed by the runner to start the program.

Inputs are a parsed ``Manifest`` plus the substitution context (the
endpoint URL, output dir, job id, etc.). Outputs are a materialised
command + env with every ``$NAME`` token replaced. The runner itself
never sees a raw manifest — substitution is forced here so nothing
downstream has to know about template variables.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from bear_harness._bear_config import SlurmConfig
from bear_harness._endpoint_discovery import EndpointRecord
from bear_harness._manifest import Manifest
from bear_harness._runner import PipelineSpec
from bear_harness._substitute import substitute, substitute_all, substitute_env
from bear_harness._template_env import load_template


@dataclass(frozen=True, slots=True)
class PipelineLaunchContext:
    """Everything needed to realise a pipeline command from a manifest.

    The harness main loop owns construction of this record after the
    vLLM endpoint is known — only then do ``MODEL_BASE_URL`` /
    ``MODEL_API_KEY`` / ``MODEL_NAME`` have real values.
    """

    manifest: Manifest
    endpoint: EndpointRecord
    output_dir: Path
    python: str
    job_id: str
    slurm_vllm_job_id: str = ""
    slurm_pipeline_job_id: str = ""

    def build_variables(self) -> dict[str, str]:
        """Return the ``$NAME → value`` mapping for :mod:`_substitute`."""
        status_spec = self.manifest.status
        # Resolve STATUS_FILE from the manifest's status.file template.
        # This is the one place we substitute recursively — status.file
        # usually reads "$OUTPUT_DIR/.bear-harness-status.json", and the
        # pipeline command template then references $STATUS_FILE.
        partial = {
            "PROGRAM_ROOT": str(self.manifest.program_root),
            "PYTHON": self.python,
            "OUTPUT_DIR": str(self.output_dir),
            "MODEL_BASE_URL": self.endpoint.base_url,
            "MODEL_API_KEY": self.endpoint.api_key,
            "MODEL_NAME": self.endpoint.model,
            "JOB_ID": self.job_id,
            "SLURM_VLLM_JOB_ID": self.slurm_vllm_job_id,
            "SLURM_PIPELINE_JOB_ID": self.slurm_pipeline_job_id,
            # Provide STATUS_FILE with a pre-substitution placeholder so
            # the next substitute() call does not see an unresolved
            # variable when templates reference it.
            "STATUS_FILE": "",
        }
        status_file = substitute(status_spec.file, partial)
        partial["STATUS_FILE"] = status_file
        return partial


def build_pipeline_spec(
    ctx: PipelineLaunchContext,
    *,
    log_path: Path,
) -> PipelineSpec:
    """Substitute the manifest entrypoint and return a ``PipelineSpec``."""
    variables = ctx.build_variables()
    command = substitute_all(ctx.manifest.entrypoint.command, variables)
    env = substitute_env(ctx.manifest.entrypoint.env, variables)
    # Every pipeline command implicitly sees the status file path so
    # the consumer program can write to it without re-parsing args.
    env.setdefault("DATA_PIPELINE_STATUS_FILE", variables["STATUS_FILE"])
    env.setdefault("BEAR_HARNESS_JOB_ID", ctx.job_id)
    env.setdefault("BEAR_HARNESS_OUTPUT_DIR", str(ctx.output_dir))

    return PipelineSpec(
        command=command,
        env=env,
        cwd=ctx.manifest.program_root,
        log_path=log_path,
        install=ctx.manifest.runtime.install,
        teardown=ctx.manifest.runtime.teardown,
        python=ctx.python,
        output_dir=ctx.output_dir,
        status_file=Path(variables["STATUS_FILE"]) if variables["STATUS_FILE"] else None,
    )


@dataclass(frozen=True, slots=True)
class SlurmPipelineOptions:
    """Inputs to :func:`render_slurm_pipeline_script`.

    The entrypoint command and env are already substituted — this
    layer does not re-touch manifest variables. It just renders the
    bash wrapper that waits for the endpoint file, sources a venv,
    runs install hooks, and execs the entrypoint.
    """

    job_id: str
    job_name: str
    vllm_job_id: str
    runs_dir: Path
    endpoints_dir: Path
    output_dir: Path
    status_file: Path
    python: str
    program_root: Path
    entrypoint_command: tuple[str, ...]
    extra_env: dict[str, str]
    install_commands: tuple[str, ...] = ()
    teardown_commands: tuple[str, ...] = ()
    pipeline_qos: str = ""  # set by SlurmRunner from cpu_qos/qos; never hardcode a tier
    cpus_per_task: int = 4
    mem_gb: int = 16
    walltime: str = "08:00:00"
    cuda_module: str | None = None


def render_slurm_pipeline_script(
    options: SlurmPipelineOptions,
    config: SlurmConfig,
) -> str:
    """Render ``pipeline.sbatch.j2`` into a concrete sbatch script."""
    template = load_template("pipeline.sbatch.j2")
    return template.render(
        job_id=options.job_id,
        job_name=options.job_name,
        account=config.account,
        pipeline_qos=options.pipeline_qos,
        cpus_per_task=options.cpus_per_task,
        mem_gb=options.mem_gb,
        walltime=options.walltime,
        cuda_module=options.cuda_module or "",
        vllm_job_id=options.vllm_job_id,
        runs_dir=str(options.runs_dir),
        endpoints_dir=str(options.endpoints_dir),
        output_dir=str(options.output_dir),
        status_file=str(options.status_file),
        boot_timeout_seconds=config.boot_timeout_seconds,
        python=options.python,
        program_root=str(options.program_root),
        entrypoint_command=shlex.join(options.entrypoint_command),
        install_commands=list(options.install_commands),
        teardown_commands=list(options.teardown_commands),
        extra_env=dict(options.extra_env),
        mail_user=config.mail_user,
        mail_events=config.mail_events,
    )


__all__ = [
    "PipelineLaunchContext",
    "SlurmPipelineOptions",
    "build_pipeline_spec",
    "render_slurm_pipeline_script",
]
