"""The reference vLLM+pipeline preset — lower the workload to a :class:`JobGraph`.

This is the **reference preset** of docs/internal/specs/01-foundational-contract.md §6. A preset's whole job is to lower a workload to the
closed JobGraph vocabulary — here, the *coupled* shape: a ``role=sidecar`` vLLM
server that publishes the ``endpoint`` record, a pipeline worker that consumes it,
joined by a single ``after`` edge (NOT ``afterok`` — a sidecar is scancelled, it
never "succeeds"; see ``_jobgraph.EdgeKind``).

This module is allowed to know about vLLM — *presets* carry workload knowledge; the
*kernel* must not (``docs/decision-notes/first-decision.md``). It owns three things:

1. :func:`build_reference_jobgraph` — the lowering to contract data (the graph).
2. :class:`ReferenceBackend` — the *realisation* of that graph's jobs on a runner:
   how to build each job's submission spec and which runner method submits it,
   dispatched by the contract :class:`~bear_harness._jobgraph.Role`. This is the
   vLLM-aware seam the generic kernel walker (W3 S3b) calls — the kernel itself
   stays workload-agnostic.
3. :class:`VllmPipelinePreset` — the :class:`~bear_harness._preset.Preset` that binds
   the two together behind the registry name ``"vllm-pipeline"`` (W4). It self-registers
   at import; the kernel selects it by name, never by importing it for its logic.

The resource resolution mirrors the existing flow exactly so the extraction is
behaviour-preserving: the server (GPU) job takes ``qos``/``walltime``/``gres`` from
``[slurm]`` with overrides; the worker (CPU) job takes the ``cpu_qos`` precedence
(override → ``[slurm].cpu_qos`` → ``[slurm].qos``, the bbcpu fix) and reserves no GPU.
Local mode reserves nothing — the structure is identical, the resources empty.

To avoid a kernel↔preset import cycle the backend takes :class:`ReferenceInputs`
(primitives the kernel lowers its launch options to), never ``LaunchOptions``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from bear_harness._bear_config import BearConfig
from bear_harness._endpoint_discovery import EndpointRecord
from bear_harness._jobgraph import Edge, EdgeKind, Job, JobGraph, Record, Resources, Role
from bear_harness._manifest import Manifest
from bear_harness._pipeline_launcher import PipelineLaunchContext, build_pipeline_spec
from bear_harness._preset import PresetContext, PresetError, register_preset
from bear_harness._runner import JobHandle, PipelineSpec, Runner
from bear_harness._vllm_launcher import LocalVllmOptions, build_local_vllm_spec

__all__ = [
    "ENDPOINT_RECORD",
    "PIPELINE_JOB",
    "SERVER_JOB",
    "ReferenceBackend",
    "ReferenceInputs",
    "VllmPipelinePreset",
    "build_reference_jobgraph",
]

# The names the preset gives its two jobs. The kernel treats them as opaque (it reads
# roles and edges, not names) — but it *interpolates* them into the persisted state
# labels and error strings, so they are kept stable: "vllm" yields "vllm_submitted" /
# "vllm_ready" exactly as the pre-extraction flow wrote them (byte-compat, see the W3
# plan D2). run.json's vllm_job_id / pipeline_job_id are likewise keyed by role to these.
SERVER_JOB = "vllm"
PIPELINE_JOB = "pipeline"

# The one published record: the OpenAI-compatible server ROOT url (no /v1 suffix — the
# routes are /v1/models, /v1/messages; a /v1/v1 double-prefix was a real bug, see
# references/vllm-serve-api.md). The worker reads it as $MODEL_BASE_URL.
ENDPOINT_RECORD = Record(name="endpoint", filename="endpoint.json", env_var="MODEL_BASE_URL")


def build_reference_jobgraph(
    config: BearConfig,
    *,
    qos_override: str | None = None,
    walltime_override: str | None = None,
    gpu_gres_override: str | None = None,
) -> JobGraph:
    """Lower the vLLM+pipeline workload to a validated coupled :class:`JobGraph`.

    Works in both ``slurm`` and ``local`` mode (local reserves nothing — same
    structure, empty resources). The per-launch overrides mirror the launch
    ``--qos`` / ``--walltime`` / ``--gres`` flags: ``qos_override`` and
    ``walltime_override`` apply to *both* jobs (a launch tightens the whole flow),
    while ``gpu_gres_override`` changes only the GPU server. The returned graph is
    validated before it is handed back.
    """
    if config.is_slurm:
        slurm = config.require_slurm()
        server_resources = Resources(
            qos=qos_override or slurm.qos,
            walltime=walltime_override or slurm.walltime,
            gres=gpu_gres_override or slurm.gpu_gres,
            cpus_per_task=slurm.cpus_per_task,
            mem_gb=slurm.mem_gb,
        )
        worker_resources = Resources(
            qos=qos_override or slurm.cpu_qos or slurm.qos,
            walltime=walltime_override or slurm.walltime,
            gres=None,  # the pipeline worker is CPU-only — it reserves no GPU
            cpus_per_task=slurm.cpus_per_task,
            mem_gb=slurm.mem_gb,
        )
    else:
        # Local mode: the runner ignores SLURM resources, so the graph carries only
        # the (usually absent) per-launch overrides. Structure is identical to slurm.
        server_resources = Resources(
            qos=qos_override, walltime=walltime_override, gres=gpu_gres_override
        )
        worker_resources = Resources(qos=qos_override, walltime=walltime_override)

    server = Job(
        name=SERVER_JOB,
        resources=server_resources,
        role=Role.SIDECAR,
        publishes=(ENDPOINT_RECORD,),
    )
    worker = Job(
        name=PIPELINE_JOB,
        resources=worker_resources,
        role=Role.WORKER,
        consumes=(ENDPOINT_RECORD,),
    )
    graph = JobGraph(
        jobs=(server, worker),
        edges=(Edge(SERVER_JOB, PIPELINE_JOB, EdgeKind.AFTER),),
    )
    graph.validate()
    return graph


@dataclass(frozen=True, slots=True)
class ReferenceInputs:
    """Primitives the reference backend needs to realise a graph on a runner.

    The kernel lowers its ``LaunchOptions`` + run-dir layout to this so the preset
    never imports the kernel (no import cycle). ``overrides`` is the SLURM-overrides
    object the runner reads via ``getattr`` (the existing ``Runner.submit_vllm``
    ``**kwargs`` contract) — typed ``object`` so the preset stays decoupled from it.
    """

    model: str
    manifest: Manifest
    output_dir: Path
    vllm_log: Path
    pipeline_log: Path
    endpoint_path: Path
    job_id: str
    python: str
    max_model_len: int | None = None
    extra_vllm_args: tuple[str, ...] = ()
    vllm_boot_timeout_seconds: float = 900.0
    overrides: object | None = None


@dataclass(slots=True)
class ReferenceBackend:
    """Realise the reference graph's jobs on a runner — dispatch by Role; bake records.

    The generic kernel walker calls :meth:`submit` once per graph job in dependency
    order. A ``SIDECAR`` job becomes the vLLM server (``submit_vllm``); a ``WORKER``
    job becomes the pipeline (``submit_pipeline``) with the endpoint record it
    consumes baked into the command at submit time. The worker's spec is retained so
    the kernel can read its status-file path for the post-submit wait.
    """

    inputs: ReferenceInputs
    runner: Runner
    _worker_spec: PipelineSpec | None = field(default=None, init=False)

    def submit(
        self, job: Job, records: Mapping[str, EndpointRecord], depends_on: tuple[JobHandle, ...]
    ) -> JobHandle:
        """Submit one job; ``records`` holds upstream payloads, ``depends_on`` the edges."""
        if job.role is Role.SIDECAR:
            return self._submit_server()
        return self._submit_worker(job, records, depends_on)

    def status_file(self, default: Path) -> Path:
        """The worker's status-file path (from its spec env), or ``default``.

        The program may redirect its heartbeat via ``DATA_PIPELINE_STATUS_FILE``;
        the kernel reads this to know which file to watch for live status.
        """
        if self._worker_spec is None:
            return default
        return Path(self._worker_spec.env.get("DATA_PIPELINE_STATUS_FILE", str(default)))

    def _submit_server(self) -> JobHandle:
        spec = build_local_vllm_spec(
            LocalVllmOptions(
                model=self.inputs.model,
                log_path=self.inputs.vllm_log,
                endpoint_path=self.inputs.endpoint_path,
                max_model_len=self.inputs.max_model_len,
                extra_args=self.inputs.extra_vllm_args,
                boot_timeout_seconds=self.inputs.vllm_boot_timeout_seconds,
            )
        )
        return self.runner.submit_vllm(spec, overrides=self.inputs.overrides)

    def _submit_worker(
        self, job: Job, records: Mapping[str, EndpointRecord], depends_on: tuple[JobHandle, ...]
    ) -> JobHandle:
        endpoint = records[job.consumes[0].name]
        ctx = PipelineLaunchContext(
            manifest=self.inputs.manifest,
            endpoint=endpoint,
            output_dir=self.inputs.output_dir,
            python=self.inputs.python,
            job_id=self.inputs.job_id,
        )
        spec = replace(
            build_pipeline_spec(ctx, log_path=self.inputs.pipeline_log),
            depends_on=depends_on,
        )
        self._worker_spec = spec
        return self.runner.submit_pipeline(spec)


class VllmPipelinePreset:
    """The reference preset — the coupled vLLM server + pipeline worker flow.

    A :class:`~bear_harness._preset.Preset`: it lowers to the coupled JobGraph
    (:func:`build_reference_jobgraph`) and realises it with :class:`ReferenceBackend`.
    This is the preset the kernel selects by default and the only place vLLM knowledge
    lives now that the kernel is preset-agnostic.
    """

    name = "vllm-pipeline"

    def lower(self, context: PresetContext) -> JobGraph:
        ov = context.overrides
        return build_reference_jobgraph(
            context.config,
            qos_override=getattr(ov, "qos", None),
            walltime_override=getattr(ov, "walltime", None),
            gpu_gres_override=getattr(ov, "gpu_gres", None),
        )

    def make_backend(self, context: PresetContext, runner: Runner) -> ReferenceBackend:
        ov = context.overrides
        return ReferenceBackend(
            ReferenceInputs(
                model=context.model,
                manifest=context.manifest,
                output_dir=context.output_dir,
                vllm_log=context.server_log,
                pipeline_log=context.worker_log,
                endpoint_path=context.endpoint_path,
                job_id=context.job_id,
                python=context.python,
                max_model_len=getattr(ov, "max_model_len", None),
                extra_vllm_args=getattr(ov, "extra_vllm_args", ()),
                vllm_boot_timeout_seconds=context.boot_timeout_seconds,
                overrides=ov,
            ),
            runner,
        )

    def validate_manifest(self, manifest: Manifest) -> None:
        if manifest.model is None:
            msg = "the 'vllm-pipeline' preset requires a [model] section in pipeline.toml"
            raise PresetError(msg)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "topology": "coupled",
            "summary": (
                "vLLM server (role=sidecar) + pipeline worker; the server publishes the "
                "endpoint record, the worker consumes it as $MODEL_BASE_URL"
            ),
            "requires": ["[model]"],
            "gpu": True,
        }


register_preset(VllmPipelinePreset())
