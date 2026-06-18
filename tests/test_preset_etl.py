"""Tests for the ETL preset — the model-less de-risker that proves agnosticism.

ETL shares none of the reference vLLM flow's structure (no GPU, no server, no record,
no edge) and lowers to the *single* topology. These tests pin that shape and the
backend's no-endpoint CPU submission; the end-to-end "runs through the unchanged
kernel" proof lives in tests/test_launch.py::TestEtlPreset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._bear_config import BearConfig, LocalConfig, SlurmConfig
from bear_harness._jobgraph import Role, Topology
from bear_harness._manifest import load_manifest
from bear_harness._preset import PresetContext, PresetError, get_preset, list_presets
from bear_harness._runner import JobHandle, JobState, Runner

_ETL_FIXTURE = Path(__file__).parent / "fixtures" / "etl_pipeline.toml"


def _local() -> BearConfig:
    return BearConfig(mode="local", local=LocalConfig())


def _slurm() -> BearConfig:
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
            cpu_qos="bbdefault",
        ),
    )


class _RecordingRunner(Runner):
    def __init__(self) -> None:
        self.vllm_spec: object | None = None
        self.pipeline_spec: object | None = None

    def submit_vllm(self, spec, **kwargs):  # type: ignore[no-untyped-def]
        self.vllm_spec = spec
        return JobHandle(job_id="v1", log_path=spec.log_path, kind="vllm")

    def submit_pipeline(self, spec):  # type: ignore[no-untyped-def]
        self.pipeline_spec = spec
        return JobHandle(job_id="e1", log_path=spec.log_path, kind="pipeline")

    def poll(self, handle):  # type: ignore[no-untyped-def]
        return JobState.COMPLETED

    def cancel(self, handle):  # type: ignore[no-untyped-def]
        return None


def _context(tmp_path: Path, config: BearConfig) -> PresetContext:
    prog = tmp_path / "prog"
    prog.mkdir()
    (prog / "pipeline.toml").write_text(_ETL_FIXTURE.read_text())
    return PresetContext(
        manifest=load_manifest(prog),
        config=config,
        job_id="etl-1",
        run_dir=tmp_path / "run",
        output_dir=tmp_path / "out",
        server_log=tmp_path / "s.log",
        worker_log=tmp_path / "w.log",
        endpoint_path=tmp_path / "ep.json",
        python="python3",
    )


class TestEtlRegistered:
    def test_registered_alongside_vllm(self):
        names = list_presets()
        assert "etl" in names
        assert "vllm-pipeline" in names  # both presets, one kernel
        assert get_preset("etl").name == "etl"


class TestEtlLower:
    def test_single_topology_no_gpu(self, tmp_path: Path):
        g = get_preset("etl").lower(_context(tmp_path, _slurm()))
        assert g.topology == Topology.SINGLE
        assert len(g.jobs) == 1
        assert len(g.edges) == 0
        job = g.jobs[0]
        assert job.role is Role.WORKER
        assert job.resources.gres is None
        assert job.resources.gpu_count == 0
        assert job.resources.qos == "bbdefault"  # the cpu_qos, never a GPU tier
        assert job.publishes == ()
        assert job.consumes == ()

    def test_lowers_in_local_mode(self, tmp_path: Path):
        assert get_preset("etl").lower(_context(tmp_path, _local())).topology == Topology.SINGLE


class TestEtlValidateManifest:
    def test_model_less_manifest_ok(self, tmp_path: Path):
        get_preset("etl").validate_manifest(_context(tmp_path, _local()).manifest)  # no raise

    def test_forbids_a_model_section(self, tmp_path: Path):
        prog = tmp_path / "p"
        prog.mkdir()
        (prog / "pipeline.toml").write_text(
            'schema_version = "1"\npreset = "etl"\n'
            '[program]\nname = "x"\nversion = "1"\n'
            '[runtime]\npython = ">=3.11,<4"\n'
            '[model]\napi = "openai_chat"\ndefault_model = "m"\n'
            '[entrypoint]\ncommand = ["echo"]\n'
        )
        with pytest.raises(PresetError):
            get_preset("etl").validate_manifest(load_manifest(prog))


class TestEtlBackend:
    def test_submit_is_a_cpu_job_with_no_model_env_no_dependency(self, tmp_path: Path):
        runner = _RecordingRunner()
        ctx = _context(tmp_path, _local())
        backend = get_preset("etl").make_backend(ctx, runner)
        job = get_preset("etl").lower(ctx).jobs[0]
        handle = backend.submit(job, {}, ())
        assert handle.kind == "pipeline"
        spec = runner.pipeline_spec
        assert spec is not None
        assert spec.depends_on == ()
        assert "MODEL_BASE_URL" not in spec.env  # no endpoint baked in
        assert runner.vllm_spec is None  # no server ever submitted


class TestEtlDescribe:
    def test_describe(self):
        d = get_preset("etl").describe()
        assert d["name"] == "etl"
        assert d["topology"] == "single"
        assert d["gpu"] is False
