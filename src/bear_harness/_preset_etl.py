"""The ETL preset — the model-less de-risker that proves the kernel is preset-agnostic.

ETL (extract / transform / load) shares **none** of the reference vLLM+pipeline flow's
distinctive structure: no GPU, no server, no ``role=sidecar``, no endpoint record, no
edge. It lowers to the *single* JobGraph topology — one CPU job that runs the program's
entrypoint. Running it through the **unchanged kernel** is the concrete falsification of
"this is secretly a vLLM-only harness" (docs/internal/specs/01-foundational-contract.md §6); it is the
second preset whose existence the keystone (``docs/internal/decision-notes/first-decision.md``)
makes cheap.

Like the reference preset, this module is allowed to know its workload; the kernel is
not. It registers itself under the name ``"etl"`` at import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bear_harness._endpoint_discovery import EndpointRecord
from bear_harness._jobgraph import Job, JobGraph, Resources, Role
from bear_harness._manifest import Manifest
from bear_harness._preset import PresetContext, PresetError, register_preset
from bear_harness._runner import JobHandle, PipelineSpec, Runner
from bear_harness._substitute import substitute, substitute_all, substitute_env

__all__ = ["ETL_JOB", "EtlBackend", "EtlPreset"]

ETL_JOB = "etl"


@dataclass(slots=True)
class EtlBackend:
    """Realise the ETL graph's single CPU job on a runner — no endpoint, no dependency.

    Builds a :class:`PipelineSpec` directly (a model-less program has no
    :class:`~bear_harness._endpoint_discovery.EndpointRecord`, so
    ``_pipeline_launcher.build_pipeline_spec`` does not fit) and submits it via the CPU
    ``submit_pipeline`` path. With no incoming edge, ``depends_on`` is empty and the
    runner emits no ``--dependency`` (W4 S3).
    """

    context: PresetContext
    runner: Runner
    _worker_spec: PipelineSpec | None = field(default=None, init=False)

    def submit(
        self, job: Job, records: Mapping[str, EndpointRecord], depends_on: tuple[JobHandle, ...]
    ) -> JobHandle:
        ctx = self.context
        manifest = ctx.manifest
        # NB: duplicates the *non-model* half of _pipeline_launcher.build_pipeline_spec
        # (ETL has no endpoint, so PipelineLaunchContext does not fit). Rule of Three:
        # extract a shared base-variable helper when a third preset needs it.
        variables = {
            "PROGRAM_ROOT": str(manifest.program_root),
            "PYTHON": ctx.python,
            "OUTPUT_DIR": str(ctx.output_dir),
            "JOB_ID": ctx.job_id,
            "STATUS_FILE": "",
        }
        variables["STATUS_FILE"] = substitute(manifest.status.file, variables)
        command = substitute_all(manifest.entrypoint.command, variables)
        env = substitute_env(manifest.entrypoint.env, variables)
        env.setdefault("DATA_PIPELINE_STATUS_FILE", variables["STATUS_FILE"])
        env.setdefault("BEAR_HARNESS_JOB_ID", ctx.job_id)
        env.setdefault("BEAR_HARNESS_OUTPUT_DIR", str(ctx.output_dir))
        spec = PipelineSpec(
            command=command,
            env=env,
            cwd=manifest.program_root,
            log_path=ctx.worker_log,
            install=manifest.runtime.install,
            teardown=manifest.runtime.teardown,
            python=ctx.python,
            output_dir=ctx.output_dir,
            status_file=Path(variables["STATUS_FILE"]) if variables["STATUS_FILE"] else None,
            depends_on=depends_on,
        )
        self._worker_spec = spec
        return self.runner.submit_pipeline(spec)

    def status_file(self, default: Path) -> Path:
        if self._worker_spec is None:
            return default
        return Path(self._worker_spec.env.get("DATA_PIPELINE_STATUS_FILE", str(default)))


class EtlPreset:
    """A :class:`~bear_harness._preset.Preset`: a single model-less CPU job."""

    name = "etl"

    def lower(self, context: PresetContext) -> JobGraph:
        ov = context.overrides
        config = context.config
        if config.is_slurm:
            slurm = config.require_slurm()
            resources = Resources(
                qos=getattr(ov, "qos", None) or slurm.cpu_qos or slurm.qos,
                walltime=getattr(ov, "walltime", None) or slurm.walltime,
                gres=None,  # ETL reserves no GPU — that is the whole point
                cpus_per_task=slurm.cpus_per_task,
                mem_gb=slurm.mem_gb,
            )
        else:
            resources = Resources(
                qos=getattr(ov, "qos", None), walltime=getattr(ov, "walltime", None)
            )
        graph = JobGraph(jobs=(Job(name=ETL_JOB, resources=resources, role=Role.WORKER),))
        graph.validate()
        return graph

    def make_backend(self, context: PresetContext, runner: Runner) -> EtlBackend:
        return EtlBackend(context, runner)

    def validate_manifest(self, manifest: Manifest) -> None:
        if manifest.model is not None:
            msg = "the 'etl' preset is model-less; remove the [model] section from pipeline.toml"
            raise PresetError(msg)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "topology": "single",
            "summary": "a single CPU job running the program entrypoint; no GPU, no server, no model",
            "requires": [],
            "gpu": False,
        }


register_preset(EtlPreset())
