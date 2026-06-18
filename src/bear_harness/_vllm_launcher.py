"""Build the ``VllmSpec`` used by the runner to start vLLM.

Two production backends consume this module:

- **Local mode**: we run ``vllm serve ...`` on the host directly.
- **SLURM mode** (Phase C): we render a Jinja template of the same
  arguments into an sbatch script, which runs inside an Apptainer
  image on a GPU node.

Both paths share the same argv construction so the local-mode smoke
test actually exercises the same flag-set as the cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bear_harness._bear_config import SlurmConfig
from bear_harness._runner import VllmSpec, pick_free_port, random_api_key
from bear_harness._template_env import load_template


@dataclass(frozen=True, slots=True)
class LocalVllmOptions:
    """Inputs to :func:`build_local_vllm_spec`."""

    model: str
    log_path: Path
    endpoint_path: Path
    served_model_name: str | None = None  # defaults to model
    host: str = "127.0.0.1"
    port: int | None = None  # auto-pick if None
    api_key: str | None = None  # auto-generate if None
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    dtype: str | None = None  # "auto" | "float16" | "bfloat16" | ...
    extra_args: tuple[str, ...] = ()
    boot_timeout_seconds: float = 900.0


def build_local_vllm_spec(options: LocalVllmOptions) -> VllmSpec:
    """Turn ``LocalVllmOptions`` into a ``VllmSpec`` the runner can submit.

    Picks a free port if none was supplied, generates an API key if
    none was supplied, and assembles the ``vllm serve`` argv. The
    served-model-name defaults to the same string as the model — a
    local-mode default that avoids user confusion.
    """
    port = options.port if options.port is not None else pick_free_port(options.host)
    api_key = options.api_key or random_api_key()
    served = options.served_model_name or options.model
    # Server root, no /v1 — probe_endpoint appends /v1/models and the
    # anthropic SDK appends /v1/messages.
    base_url = f"http://{options.host}:{port}"

    cmd: list[str] = [
        "vllm",
        "serve",
        options.model,
        "--host",
        options.host,
        "--port",
        str(port),
        "--api-key",
        api_key,
        "--served-model-name",
        served,
    ]
    if options.gpu_memory_utilization is not None:
        cmd += ["--gpu-memory-utilization", f"{options.gpu_memory_utilization}"]
    if options.max_model_len is not None:
        cmd += ["--max-model-len", str(options.max_model_len)]
    if options.dtype is not None:
        cmd += ["--dtype", options.dtype]
    cmd.extend(options.extra_args)

    return VllmSpec(
        serve_command=tuple(cmd),
        env={},
        log_path=options.log_path,
        endpoint_path=options.endpoint_path,
        served_model_name=served,
        base_url=base_url,
        api_key=api_key,
        boot_timeout_seconds=options.boot_timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class SlurmVllmOptions:
    """Inputs to :func:`render_slurm_vllm_script`.

    The Apptainer image, HF cache and module name are pulled from
    :class:`SlurmConfig`; this dataclass only carries per-launch
    overrides (the model to serve, the run-scoped paths, and optional
    tuning knobs). The job_id, account etc. flow in from :class:`SlurmConfig`
    at render time so operators configure them once in ``bear.toml``.
    """

    model: str
    job_id: str
    job_name: str
    runs_dir: Path
    endpoints_dir: Path
    served_model_name: str | None = None
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    dtype: str | None = None
    extra_args: tuple[str, ...] = ()
    gpu_gres_override: str | None = None
    tensor_parallel_override: int | None = None
    mem_gb_override: int | None = None
    qos_override: str | None = None
    walltime_override: str | None = None


def render_slurm_vllm_script(options: SlurmVllmOptions, config: SlurmConfig) -> str:
    """Render ``vllm.sbatch.j2`` into a concrete sbatch script.

    Returns the script body as a string; the caller is responsible for
    writing it somewhere and passing its path to ``sbatch``. Keeping
    rendering separate from IO makes the script-construction logic
    trivial to snapshot-test.
    """
    template = load_template("vllm.sbatch.j2")
    max_model_len = options.max_model_len if options.max_model_len is not None else config.max_model_len
    return template.render(
        job_id=options.job_id,
        job_name=options.job_name,
        account=config.account,
        qos=options.qos_override or config.qos,
        gpu_gres=options.gpu_gres_override or config.gpu_gres,
        cpus_per_task=config.cpus_per_task,
        mem_gb=options.mem_gb_override or config.mem_gb,
        walltime=options.walltime_override or config.walltime,
        cuda_module=config.cuda_module,
        apptainer_sif=str(config.apptainer_sif),
        hf_cache=str(config.hf_cache),
        runs_dir=str(options.runs_dir),
        endpoints_dir=str(options.endpoints_dir),
        model=options.model,
        served_model_name=options.served_model_name or options.model,
        tensor_parallel_size=options.tensor_parallel_override or config.tensor_parallel_size,
        max_model_len=max_model_len,
        gpu_memory_utilization=options.gpu_memory_utilization,
        dtype=options.dtype,
        extra_args=list(config.extra_vllm_args) + list(options.extra_args),
        boot_timeout_seconds=config.boot_timeout_seconds,
        port_low=config.vllm_port_range[0],
        port_high=config.vllm_port_range[1],
        mail_user=config.mail_user,
        mail_events=config.mail_events,
    )


__all__ = [
    "LocalVllmOptions",
    "SlurmVllmOptions",
    "build_local_vllm_spec",
    "render_slurm_vllm_script",
]
