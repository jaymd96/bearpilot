"""Tests for the reference vLLM+pipeline preset — the lowering to a JobGraph.

The reference preset (specs/01-foundational-contract.md §6) lowers the vLLM+pipeline
workload to a JobGraph: a ``role=sidecar`` server that publishes the endpoint record,
a worker that consumes it, and one ``after`` edge between them — the *coupled*
topology. These tests pin that the lowering faithfully reproduces the existing flow's
resource resolution (the GPU/CPU split, the ``cpu_qos`` precedence, the per-launch
override that applies to both jobs). This is the proof that the contract can *express*
the reference flow before any kernel is wired to read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._bear_config import BearConfig, LocalConfig, SlurmConfig
from bear_harness._endpoint_discovery import EndpointRecord
from bear_harness._jobgraph import EdgeKind, Role, Topology
from bear_harness._manifest import load_manifest
from bear_harness._reference_preset import (
    ENDPOINT_RECORD,
    ReferenceBackend,
    ReferenceInputs,
    build_reference_jobgraph,
)
from bear_harness._runner import JobHandle, JobState, Runner

_FIXTURE = Path(__file__).parent / "fixtures" / "minimal_pipeline.toml"


def _slurm_config(cpu_qos: str | None = None) -> BearConfig:
    return BearConfig(
        mode="slurm",
        slurm=SlurmConfig(
            account="proj",
            qos="bbgpu",
            gpu_gres="gpu:a100:1",
            cpus_per_task=8,
            mem_gb=64,
            walltime="00:10:00",
            cuda_module="CUDA/12.6.0",
            apptainer_sif=Path("/x.sif"),
            hf_cache=Path("/hf"),
            runs_dir=Path("/runs"),
            endpoints_dir=Path("/ep"),
            cpu_qos=cpu_qos,
        ),
    )


class TestShape:
    def test_two_jobs_one_after_edge_coupled(self):
        g = build_reference_jobgraph(_slurm_config())
        assert [j.name for j in g.jobs] == ["vllm", "pipeline"]
        assert g.topology == Topology.COUPLED
        assert len(g.edges) == 1
        edge = g.edges[0]
        assert (edge.upstream, edge.downstream, edge.kind) == ("vllm", "pipeline", EdgeKind.AFTER)

    def test_server_is_a_sidecar_that_publishes_the_endpoint(self):
        g = build_reference_jobgraph(_slurm_config())
        server = g.job("vllm")
        assert server.role is Role.SIDECAR
        assert server.publishes == (ENDPOINT_RECORD,)
        assert server.consumes == ()
        assert server.resources.gres == "gpu:a100:1"
        assert server.resources.gpu_count == 1

    def test_worker_consumes_the_endpoint_and_reserves_no_gpu(self):
        g = build_reference_jobgraph(_slurm_config())
        worker = g.job("pipeline")
        assert worker.role is Role.WORKER
        assert worker.consumes == (ENDPOINT_RECORD,)
        assert worker.publishes == ()
        assert worker.resources.gres is None
        assert worker.resources.gpu_count == 0

    def test_endpoint_record_is_the_root_url_env(self):
        # The published record is the OpenAI-compatible server ROOT url (no /v1).
        assert ENDPOINT_RECORD.name == "endpoint"
        assert ENDPOINT_RECORD.filename == "endpoint.json"
        assert ENDPOINT_RECORD.env_var == "MODEL_BASE_URL"

    def test_built_graph_is_structurally_valid(self):
        # build_reference_jobgraph validates before returning; assert it does not raise.
        build_reference_jobgraph(_slurm_config())


class TestResourceResolution:
    def test_worker_qos_falls_back_to_gpu_qos_when_no_cpu_qos(self):
        g = build_reference_jobgraph(_slurm_config(cpu_qos=None))
        assert g.job("vllm").resources.qos == "bbgpu"
        assert g.job("pipeline").resources.qos == "bbgpu"

    def test_cpu_qos_sets_the_worker_qos_only(self):
        g = build_reference_jobgraph(_slurm_config(cpu_qos="bbdefault"))
        assert g.job("vllm").resources.qos == "bbgpu"  # server unaffected
        assert g.job("pipeline").resources.qos == "bbdefault"

    def test_qos_override_applies_to_both_jobs(self):
        g = build_reference_jobgraph(_slurm_config(cpu_qos="bbdefault"), qos_override="bbshort")
        assert g.job("vllm").resources.qos == "bbshort"
        assert g.job("pipeline").resources.qos == "bbshort"

    def test_walltime_override_applies_to_both_jobs(self):
        g = build_reference_jobgraph(_slurm_config(), walltime_override="00:05:00")
        assert g.job("vllm").resources.walltime == "00:05:00"
        assert g.job("pipeline").resources.walltime == "00:05:00"

    def test_gpu_gres_override_changes_server_only(self):
        g = build_reference_jobgraph(_slurm_config(), gpu_gres_override="gpu:a100:2")
        assert g.job("vllm").resources.gres == "gpu:a100:2"
        assert g.job("vllm").resources.gpu_count == 2
        assert g.job("pipeline").resources.gres is None


def _local_config() -> BearConfig:
    return BearConfig(mode="local", local=LocalConfig())


class _RecordingRunner(Runner):
    """Minimal runner that records the specs it is handed — no subprocess."""

    def __init__(self) -> None:
        self.vllm_spec: object | None = None
        self.vllm_kwargs: dict = {}
        self.pipeline_spec: object | None = None

    def submit_vllm(self, spec, **kwargs):  # type: ignore[no-untyped-def]
        self.vllm_spec = spec
        self.vllm_kwargs = kwargs
        return JobHandle(job_id="vllm-1", log_path=spec.log_path, kind="vllm")

    def submit_pipeline(self, spec):  # type: ignore[no-untyped-def]
        self.pipeline_spec = spec
        return JobHandle(job_id="pipe-1", log_path=spec.log_path, kind="pipeline")

    def poll(self, handle):  # type: ignore[no-untyped-def]
        return JobState.COMPLETED

    def cancel(self, handle):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def inputs(tmp_path: Path) -> ReferenceInputs:
    prog = tmp_path / "program"
    prog.mkdir()
    (prog / "pipeline.toml").write_text(_FIXTURE.read_text())
    return ReferenceInputs(
        model="stub-model",
        manifest=load_manifest(prog),
        output_dir=tmp_path / "out",
        vllm_log=tmp_path / "vllm.log",
        pipeline_log=tmp_path / "pipe.log",
        endpoint_path=tmp_path / "ep.json",
        job_id="prog-123",
        python="python3",
    )


class TestLocalModeLowering:
    def test_local_graph_same_shape_empty_resources(self):
        g = build_reference_jobgraph(_local_config())
        assert g.topology == Topology.COUPLED
        assert [j.name for j in g.jobs] == ["vllm", "pipeline"]
        server = g.job("vllm")
        assert server.role is Role.SIDECAR
        assert server.resources.qos is None
        assert server.resources.gres is None
        assert g.job("pipeline").resources.gpu_count == 0

    def test_local_override_still_recorded(self):
        g = build_reference_jobgraph(_local_config(), qos_override="bbshort")
        assert g.job("vllm").resources.qos == "bbshort"
        assert g.job("pipeline").resources.qos == "bbshort"


class TestReferenceBackend:
    def test_sidecar_dispatches_to_submit_vllm(self, inputs: ReferenceInputs):
        runner = _RecordingRunner()
        backend = ReferenceBackend(inputs, runner)
        handle = backend.submit(build_reference_jobgraph(_local_config()).job("vllm"), {}, ())
        assert handle.kind == "vllm"
        assert runner.vllm_spec is not None
        assert runner.vllm_spec.endpoint_path == inputs.endpoint_path
        assert runner.pipeline_spec is None

    def test_worker_dispatches_to_pipeline_consuming_endpoint(self, inputs: ReferenceInputs):
        runner = _RecordingRunner()
        backend = ReferenceBackend(inputs, runner)
        endpoint = EndpointRecord(
            base_url="http://host:8000", api_key="k", model="m", job_id="vllm-1"
        )
        worker = build_reference_jobgraph(_local_config()).job("pipeline")
        handle = backend.submit(worker, {"endpoint": endpoint}, ())
        assert handle.kind == "pipeline"
        assert runner.pipeline_spec is not None
        assert runner.vllm_spec is None  # the WORKER role never hit the vLLM path

    def test_status_file_default_before_worker_submit(
        self, inputs: ReferenceInputs, tmp_path: Path
    ):
        backend = ReferenceBackend(inputs, _RecordingRunner())
        default = tmp_path / "default-status.json"
        assert backend.status_file(default) == default
