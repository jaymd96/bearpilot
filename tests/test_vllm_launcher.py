"""Unit tests for ``bear_harness._vllm_launcher``.

Covers both the local argv builder (Phase B) and the SLURM template
renderer (Phase C). The SLURM tests deliberately assert on
substantive lines rather than full-file snapshots — snapshot tests of
shell scripts are brittle to whitespace changes without adding real
safety.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._bear_config import SlurmConfig
from bear_harness._vllm_launcher import (
    LocalVllmOptions,
    SlurmVllmOptions,
    build_local_vllm_spec,
    render_slurm_vllm_script,
)


def _slurm_config(**overrides: object) -> SlurmConfig:
    base = {
        "account": "proj1",
        "qos": "bbgpu",
        "gpu_gres": "gpu:a100_40:1",
        "cpus_per_task": 8,
        "mem_gb": 64,
        "walltime": "08:00:00",
        "cuda_module": "CUDA/12.1.1",
        "apptainer_sif": Path("/rds/apptainer/vllm-openai.sif"),
        "hf_cache": Path("/rds/hf_cache"),
        "runs_dir": Path("/rds/runs"),
        "endpoints_dir": Path("/rds/endpoints"),
        "boot_timeout_seconds": 900,
        "vllm_port_range": (8000, 8099),
        "tensor_parallel_size": 1,
        "max_model_len": None,
    }
    base.update(overrides)
    return SlurmConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Local argv builder
# ---------------------------------------------------------------------------


class TestBuildLocalVllmSpec:
    def test_minimal_argv(self, tmp_path: Path) -> None:
        opts = LocalVllmOptions(
            model="m",
            log_path=tmp_path / "v.log",
            endpoint_path=tmp_path / "e.json",
            port=8123,
            api_key="k",
        )
        spec = build_local_vllm_spec(opts)
        assert spec.serve_command[:2] == ("vllm", "serve")
        assert "m" in spec.serve_command
        assert "--port" in spec.serve_command
        assert "8123" in spec.serve_command
        assert "--api-key" in spec.serve_command
        assert "k" in spec.serve_command
        # base_url is the server ROOT: probe appends /v1/models, the
        # anthropic SDK appends /v1/messages.
        assert spec.base_url == "http://127.0.0.1:8123"

    def test_auto_port_and_key(self, tmp_path: Path) -> None:
        opts = LocalVllmOptions(
            model="m",
            log_path=tmp_path / "v.log",
            endpoint_path=tmp_path / "e.json",
        )
        spec = build_local_vllm_spec(opts)
        assert spec.api_key  # non-empty generated key
        # a port from somewhere in 8000-8099
        idx = spec.serve_command.index("--port")
        assert 8000 <= int(spec.serve_command[idx + 1]) <= 8099


# ---------------------------------------------------------------------------
# SLURM template
# ---------------------------------------------------------------------------


class TestRenderSlurmVllmScript:
    def _opts(self, **overrides: object) -> SlurmVllmOptions:
        base = {
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "job_id": "prog-20260409-deadbeef",
            "job_name": "vllm-prog-20260409-deadbeef",
            "runs_dir": Path("/rds/runs"),
            "endpoints_dir": Path("/rds/endpoints"),
        }
        base.update(overrides)
        return SlurmVllmOptions(**base)  # type: ignore[arg-type]

    def test_single_gpu_script(self) -> None:
        script = render_slurm_vllm_script(self._opts(), _slurm_config())

        # SBATCH headers
        assert "#SBATCH --account=proj1" in script
        assert "#SBATCH --qos=bbgpu" in script
        assert "#SBATCH --gres=gpu:a100_40:1" in script
        assert "#SBATCH --cpus-per-task=8" in script
        assert "#SBATCH --mem=64G" in script
        assert "#SBATCH --time=08:00:00" in script

        # Module & env
        assert "module load CUDA/12.1.1" in script
        assert 'export HF_HOME="/rds/hf_cache"' in script

        # Port scan
        assert "for candidate in $(seq 8000 8099)" in script

        # Apptainer invocation
        assert "apptainer run --nv" in script
        assert "/rds/apptainer/vllm-openai.sif" in script
        # The HF cache must be visible INSIDE the container at the same
        # path HF_HOME points to — host env passes through to the
        # container, where /rds is otherwise not mounted.
        assert '--bind "/rds/hf_cache:/rds/hf_cache"' in script
        assert '--env HF_HOME="/rds/hf_cache"' in script

        # The endpoint file must be named after the HARNESS job id —
        # run_launch polls endpoints_dir/<harness_job_id>.json. Naming
        # it after $SLURM_JOB_ID strands the orchestrator.
        assert 'ENDPOINT_FINAL="/rds/endpoints/prog-20260409-deadbeef.json"' in script

        # Published base_url is the server ROOT (no /v1): probe_endpoint
        # appends /v1/models and the anthropic SDK appends /v1/messages.
        assert 'BASE_URL="http://${HOSTNAME_FQDN}:${PORT}"' in script
        assert '--model "Qwen/Qwen2.5-7B-Instruct"' in script
        assert "--tensor-parallel-size 1" in script

        # No max_model_len line since it was None
        assert "--max-model-len" not in script

        # Endpoint write
        assert "/rds/endpoints/prog-20260409-deadbeef.json" in script

    def test_tensor_parallel_two(self) -> None:
        cfg = _slurm_config(
            tensor_parallel_size=2,
            gpu_gres="gpu:a100_80:2",
            max_model_len=16384,
        )
        script = render_slurm_vllm_script(self._opts(), cfg)
        assert "#SBATCH --gres=gpu:a100_80:2" in script
        assert "--tensor-parallel-size 2" in script
        assert "--max-model-len 16384" in script

    def test_tensor_parallel_four(self) -> None:
        cfg = _slurm_config(tensor_parallel_size=4, gpu_gres="gpu:a100_80:4")
        script = render_slurm_vllm_script(self._opts(), cfg)
        assert "--tensor-parallel-size 4" in script

    def test_extra_args_appear_in_script(self) -> None:
        opts = self._opts(extra_args=("--quantization", "awq"))
        script = render_slurm_vllm_script(opts, _slurm_config())
        assert "--quantization" in script
        assert "awq" in script

    def test_mail_directives_present_when_mail_user_set(self) -> None:
        cfg = _slurm_config(mail_user="you@example.com", mail_events="BEGIN,END,FAIL")
        script = render_slurm_vllm_script(self._opts(), cfg)
        assert "#SBATCH --mail-user=you@example.com" in script
        assert "#SBATCH --mail-type=BEGIN,END,FAIL" in script

    def test_config_extra_vllm_args_appear_in_script(self) -> None:
        cfg = _slurm_config(extra_vllm_args=(
            "--performance-mode", "throughput",
            "--num-scheduler-steps", "10",
        ))
        script = render_slurm_vllm_script(self._opts(), cfg)
        assert "--performance-mode" in script
        assert "throughput" in script
        assert "--num-scheduler-steps" in script
        assert "10" in script

    def test_config_and_per_launch_extra_args_both_appear(self) -> None:
        cfg = _slurm_config(extra_vllm_args=("--performance-mode", "throughput"))
        opts = self._opts(extra_args=("--quantization", "awq"))
        script = render_slurm_vllm_script(opts, cfg)
        assert "--performance-mode" in script
        assert "--quantization" in script

    def test_mail_directives_absent_when_mail_user_none(self) -> None:
        cfg = _slurm_config(mail_user=None)
        script = render_slurm_vllm_script(self._opts(), cfg)
        assert "--mail-user" not in script
        assert "--mail-type" not in script

    # ---------------------------------------------------------------
    # Per-launch SLURM overrides
    # ---------------------------------------------------------------

    def test_gpu_gres_override_wins_over_config(self) -> None:
        opts = self._opts(gpu_gres_override="gpu:a100_80:2")
        cfg = _slurm_config(gpu_gres="gpu:a100:1")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --gres=gpu:a100_80:2" in script
        assert "gpu:a100:1" not in script

    def test_gpu_gres_falls_back_to_config_when_no_override(self) -> None:
        opts = self._opts()  # no gpu_gres_override
        cfg = _slurm_config(gpu_gres="gpu:a100:1")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --gres=gpu:a100:1" in script

    def test_tensor_parallel_override_wins_over_config(self) -> None:
        opts = self._opts(tensor_parallel_override=2)
        cfg = _slurm_config(tensor_parallel_size=1)
        script = render_slurm_vllm_script(opts, cfg)
        assert "--tensor-parallel-size 2" in script

    def test_mem_gb_override_wins_over_config(self) -> None:
        opts = self._opts(mem_gb_override=160)
        cfg = _slurm_config(mem_gb=64)
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --mem=160G" in script
        assert "--mem=64G" not in script

    def test_qos_override_wins_over_config(self) -> None:
        opts = self._opts(qos_override="bbshort")
        cfg = _slurm_config(qos="bbgpu")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --qos=bbshort" in script
        assert "bbgpu" not in script

    def test_qos_falls_back_to_config_when_no_override(self) -> None:
        opts = self._opts()  # no qos_override
        cfg = _slurm_config(qos="bbgpu")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --qos=bbgpu" in script

    def test_walltime_override_wins_over_config(self) -> None:
        opts = self._opts(walltime_override="00:10:00")
        cfg = _slurm_config(walltime="08:00:00")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --time=00:10:00" in script
        assert "08:00:00" not in script

    def test_walltime_falls_back_to_config_when_no_override(self) -> None:
        opts = self._opts()  # no walltime_override
        cfg = _slurm_config(walltime="04:00:00")
        script = render_slurm_vllm_script(opts, cfg)
        assert "#SBATCH --time=04:00:00" in script

    def test_stages_sif_on_node_local_scratch_with_rds_fallback(self) -> None:
        # BlueBEAR docs (advanced_jobs#use-local-disk-space): heavy I/O
        # belongs on node-local /scratch. Boots were random-reading the
        # 8.3GB SIF over /rds (~4 min cold); one sequential cp to
        # $BB_WORKDIR then mounting locally is the win. /scratch absent
        # or full must fall back to the RDS path.
        script = render_slurm_vllm_script(self._opts(), _slurm_config())
        assert (
            'BB_WORKDIR=$(mktemp -d "/scratch/${USER}_${SLURM_JOB_ID}.XXXXXX")'
            in script
        )
        assert 'export TMPDIR="$BB_WORKDIR"' in script
        # Default SIF is the RDS path; staging swaps it when space allows.
        assert 'SIF="/rds/apptainer/vllm-openai.sif"' in script
        assert 'SIF="$BB_WORKDIR/vllm-openai.sif"' in script
        # The apptainer invocation mounts whichever won.
        assert '"$SIF" \\' in script
        # Scratch dir is removed by the cleanup trap.
        assert 'rm -rf "$BB_WORKDIR"' in script

    def test_strict_undefined_raises_on_missing(self) -> None:
        # Sanity: render with a template variable omitted should raise.
        # We exercise this by constructing a config missing a required
        # field via dataclass replace would not compile; instead check
        # that SlurmConfig does enforce all required fields up front.
        with pytest.raises(TypeError):
            SlurmConfig()  # type: ignore[call-arg]
