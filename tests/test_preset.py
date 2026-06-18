"""Tests for the preset extension point — protocol, registry, reference conformance.

The kernel selects a preset by name from the registry and never branches on a
workload (specs/01-foundational-contract.md §5). These tests pin the registry
semantics and that the reference vLLM+pipeline preset conforms to the protocol — the
groundwork the ETL preset (W4 S4) drops into unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._bear_config import BearConfig, SlurmConfig
from bear_harness._jobgraph import Topology
from bear_harness._manifest import load_manifest
from bear_harness._preset import (
    Backend,
    PresetContext,
    PresetError,
    get_preset,
    list_presets,
    register_preset,
)
from bear_harness._reference_preset import VllmPipelinePreset
from bear_harness._runner import JobHandle, JobState, Runner

_FIXTURE = Path(__file__).parent / "fixtures" / "minimal_pipeline.toml"


def _slurm_config() -> BearConfig:
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
        ),
    )


class _RecordingRunner(Runner):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_vllm(self, spec, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append("vllm")
        return JobHandle(job_id="v1", log_path=spec.log_path, kind="vllm")

    def submit_pipeline(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append("pipe")
        return JobHandle(job_id="p1", log_path=spec.log_path, kind="pipeline")

    def poll(self, handle):  # type: ignore[no-untyped-def]
        return JobState.COMPLETED

    def cancel(self, handle):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def context(tmp_path: Path) -> PresetContext:
    prog = tmp_path / "program"
    prog.mkdir()
    (prog / "pipeline.toml").write_text(_FIXTURE.read_text())
    return PresetContext(
        manifest=load_manifest(prog),
        config=_slurm_config(),
        job_id="prog-1",
        run_dir=tmp_path / "run",
        output_dir=tmp_path / "out",
        server_log=tmp_path / "vllm.log",
        worker_log=tmp_path / "pipe.log",
        endpoint_path=tmp_path / "ep.json",
        python="python3",
        model="stub-model",
    )


class TestRegistry:
    def test_reference_preset_is_registered_at_import(self):
        assert "vllm-pipeline" in list_presets()
        assert get_preset("vllm-pipeline").name == "vllm-pipeline"

    def test_unknown_preset_raises(self):
        with pytest.raises(PresetError):
            get_preset("does-not-exist")

    def test_duplicate_registration_raises(self):
        # vllm-pipeline self-registered at import; re-registering the name must raise.
        with pytest.raises(PresetError):
            register_preset(VllmPipelinePreset())


class TestVllmPipelinePresetConforms:
    def test_lower_returns_coupled_graph(self, context: PresetContext):
        g = get_preset("vllm-pipeline").lower(context)
        assert g.topology == Topology.COUPLED
        assert [j.name for j in g.jobs] == ["vllm", "pipeline"]

    def test_lower_threads_overrides(self, context: PresetContext):
        from types import SimpleNamespace

        ctx = PresetContext(
            manifest=context.manifest,
            config=context.config,
            job_id=context.job_id,
            run_dir=context.run_dir,
            output_dir=context.output_dir,
            server_log=context.server_log,
            worker_log=context.worker_log,
            endpoint_path=context.endpoint_path,
            python=context.python,
            model=context.model,
            overrides=SimpleNamespace(qos="bbshort", walltime=None, gpu_gres=None),
        )
        g = get_preset("vllm-pipeline").lower(ctx)
        assert g.job("vllm").resources.qos == "bbshort"
        assert g.job("pipeline").resources.qos == "bbshort"

    def test_make_backend_returns_a_backend(self, context: PresetContext):
        backend = get_preset("vllm-pipeline").make_backend(context, _RecordingRunner())
        assert isinstance(backend, Backend)

    def test_validate_manifest_passes_for_model_manifest(self, context: PresetContext):
        get_preset("vllm-pipeline").validate_manifest(context.manifest)  # must not raise

    def test_describe(self):
        d = get_preset("vllm-pipeline").describe()
        assert d["name"] == "vllm-pipeline"
        assert "topology" in d

    def test_validate_manifest_raises_without_model(self, tmp_path: Path):
        prog = tmp_path / "p"
        prog.mkdir()
        (prog / "pipeline.toml").write_text(
            'schema_version = "1"\npreset = "vllm-pipeline"\n'
            '[program]\nname = "x"\nversion = "1"\n'
            '[runtime]\npython = ">=3.11,<4"\n'
            '[entrypoint]\ncommand = ["echo"]\n'
        )
        manifest = load_manifest(prog)
        with pytest.raises(PresetError):
            get_preset("vllm-pipeline").validate_manifest(manifest)
