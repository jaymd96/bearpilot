"""Unit tests for ``bear_harness._slurm_runner``.

Every shell invocation is routed through the injected ``run_shell``
seam so these tests never touch real sbatch/squeue/scancel.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bear_harness._bear_config import SlurmConfig
from bear_harness._runner import JobHandle, JobState, PipelineSpec, VllmSpec
from bear_harness._slurm_runner import (
    ShellResult,
    SlurmRunner,
    _map_state,
)


def _slurm_config(tmp_path: Path) -> SlurmConfig:
    return SlurmConfig(
        account="proj1",
        qos="bbgpu",
        gpu_gres="gpu:a100_40:1",
        cpus_per_task=8,
        mem_gb=64,
        walltime="08:00:00",
        cuda_module="CUDA/12.1.1",
        apptainer_sif=tmp_path / "apptainer" / "vllm.sif",
        hf_cache=tmp_path / "hf_cache",
        runs_dir=tmp_path / "runs",
        endpoints_dir=tmp_path / "endpoints",
        boot_timeout_seconds=900,
        vllm_port_range=(8000, 8099),
        tensor_parallel_size=1,
        max_model_len=None,
    )


class _RecordingShell:
    """Fake ``ShellRunner`` that records calls and replays scripted responses."""

    def __init__(self, script: list[ShellResult]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = list(script)

    def __call__(self, argv: Sequence[str]) -> ShellResult:
        self.calls.append(tuple(argv))
        if not self._script:
            return ShellResult(returncode=0, stdout="", stderr="")
        return self._script.pop(0)


def _make_vllm_spec(tmp_path: Path, job_id: str = "prog-123") -> VllmSpec:
    run_dir = tmp_path / "runs" / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return VllmSpec(
        serve_command=("vllm", "serve", "Qwen/Qwen2.5-7B-Instruct"),
        env={},
        log_path=run_dir / "vllm.log",
        endpoint_path=tmp_path / "endpoints" / f"{job_id}.json",
        served_model_name="Qwen/Qwen2.5-7B-Instruct",
        base_url="http://placeholder/v1",
        api_key="k",
    )


def _make_pipeline_spec(
    tmp_path: Path, job_id: str = "prog-123", *, depends_on: tuple[JobHandle, ...] = ()
) -> PipelineSpec:
    run_dir = tmp_path / "runs" / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return PipelineSpec(
        command=("python", "-m", "my_prog", "--config", "x.yaml"),
        env={"FOO": "bar"},
        cwd=tmp_path / "program",
        log_path=run_dir / "pipeline.log",
        install=("python -m pip install -e .",),
        teardown=("echo done",),
        python="python3",
        output_dir=output_dir,
        status_file=output_dir / ".bear-harness-status.json",
        depends_on=depends_on,
    )


# ---------------------------------------------------------------------------
# pipeline QoS (regression: the bbshort canary found a hardcoded "bbcpu")
# ---------------------------------------------------------------------------


class TestPipelineQos:
    """The CPU pipeline job must NOT hardcode 'bbcpu' — that QoS is absent on
    some BlueBEAR accounts (proven by the 2026-06-14 bbshort canary).
    Precedence: per-launch override > [slurm].cpu_qos > [slurm].qos.
    """

    def _render_pipeline(
        self, tmp_path: Path, cfg: SlurmConfig, *, qos_override: str | None = None
    ) -> str:
        shell = _RecordingShell([ShellResult(0, "111\n", ""), ShellResult(0, "222\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        # The launch --qos override reaches the runner via submit_vllm's overrides,
        # which stashes it so the pipeline job reuses it (mirrors run_launch).
        overrides = SimpleNamespace(qos=qos_override) if qos_override is not None else None
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))
        return (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()

    def test_defaults_to_config_qos_not_bbcpu(self, tmp_path: Path) -> None:
        body = self._render_pipeline(tmp_path, _slurm_config(tmp_path))  # qos=bbgpu
        assert "#SBATCH --qos=bbgpu" in body
        assert "bbcpu" not in body

    def test_cpu_qos_overrides_main_qos(self, tmp_path: Path) -> None:
        cfg = replace(_slurm_config(tmp_path), cpu_qos="bbdefault")
        assert "#SBATCH --qos=bbdefault" in self._render_pipeline(tmp_path, cfg)

    def test_launch_qos_override_wins(self, tmp_path: Path) -> None:
        body = self._render_pipeline(tmp_path, _slurm_config(tmp_path), qos_override="bbshort")
        assert "#SBATCH --qos=bbshort" in body


# ---------------------------------------------------------------------------
# submit_vllm
# ---------------------------------------------------------------------------


class TestSubmitVllm:
    def test_writes_script_and_parses_jobid(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "12345\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        spec = _make_vllm_spec(tmp_path)
        handle = runner.submit_vllm(spec)

        assert handle.job_id == "12345"
        assert handle.kind == "vllm"
        script_path = cfg.runs_dir / "prog-123" / "vllm.sbatch"
        assert script_path.is_file()
        body = script_path.read_text()
        assert "#SBATCH --account=proj1" in body
        assert "Qwen/Qwen2.5-7B-Instruct" in body

        # sbatch call shape
        assert len(shell.calls) == 1
        argv = shell.calls[0]
        assert argv[0] == "sbatch"
        assert argv[1] == "--parsable"
        assert argv[-1] == str(script_path)

    def test_config_extra_vllm_args_in_rendered_script(self, tmp_path: Path) -> None:
        from dataclasses import fields as dc_fields

        cfg = _slurm_config(tmp_path)
        cfg = SlurmConfig(
            **{
                **{f.name: getattr(cfg, f.name) for f in dc_fields(cfg)},
                "extra_vllm_args": (
                    "--performance-mode",
                    "throughput",
                    "--num-scheduler-steps",
                    "10",
                ),
            }
        )
        shell = _RecordingShell([ShellResult(0, "12345\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.submit_vllm(_make_vllm_spec(tmp_path))
        body = (cfg.runs_dir / "prog-123" / "vllm.sbatch").read_text()
        assert "--performance-mode" in body
        assert "throughput" in body
        assert "--num-scheduler-steps" in body

    def test_slurm_overrides_change_rendered_script(self, tmp_path: Path) -> None:
        from bear_harness._launch import SlurmOverrides

        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "12345\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        overrides = SlurmOverrides(
            gpu_gres="gpu:a100_80:2",
            tensor_parallel_size=2,
            mem_gb=160,
            extra_vllm_args=("--quantization", "fp8"),
            dtype="bfloat16",
        )
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        body = (cfg.runs_dir / "prog-123" / "vllm.sbatch").read_text()
        assert "#SBATCH --gres=gpu:a100_80:2" in body
        assert "--tensor-parallel-size 2" in body
        assert "#SBATCH --mem=160G" in body
        assert "--quantization" in body
        assert "fp8" in body
        assert "--dtype bfloat16" in body

    def test_qos_override_changes_rendered_script(self, tmp_path: Path) -> None:
        from bear_harness._launch import SlurmOverrides

        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "12345\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        overrides = SlurmOverrides(qos="bbshort")
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        body = (cfg.runs_dir / "prog-123" / "vllm.sbatch").read_text()
        assert "#SBATCH --qos=bbshort" in body
        assert "bbgpu" not in body

    def test_walltime_override_changes_rendered_script(self, tmp_path: Path) -> None:
        from bear_harness._launch import SlurmOverrides

        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "12345\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        overrides = SlurmOverrides(walltime="00:10:00")
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        body = (cfg.runs_dir / "prog-123" / "vllm.sbatch").read_text()
        assert "#SBATCH --time=00:10:00" in body

    def test_parsable_cluster_suffix_stripped(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "99887;bluebear\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        handle = runner.submit_vllm(_make_vllm_spec(tmp_path))
        assert handle.job_id == "99887"

    def test_sbatch_failure_raises(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(1, "", "Invalid qos")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        with pytest.raises(RuntimeError, match="sbatch failed"):
            runner.submit_vllm(_make_vllm_spec(tmp_path))

    def test_empty_stdout_raises(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        with pytest.raises(RuntimeError, match="empty stdout"):
            runner.submit_vllm(_make_vllm_spec(tmp_path))


# ---------------------------------------------------------------------------
# submit_pipeline
# ---------------------------------------------------------------------------


class TestSubmitPipeline:
    def test_dependency_after_vllm(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),  # vllm sbatch
                ShellResult(0, "22222\n", ""),  # pipeline sbatch
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        vllm_handle = runner.submit_vllm(_make_vllm_spec(tmp_path))
        handle = runner.submit_pipeline(_make_pipeline_spec(tmp_path, depends_on=(vllm_handle,)))

        assert handle.job_id == "22222"
        assert handle.kind == "pipeline"
        pipeline_argv = shell.calls[1]
        assert "--dependency=after:11111" in pipeline_argv

        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "#SBATCH --dependency=after:11111" in script
        # Endpoint file is named after the HARNESS job id (matches what
        # the vllm sbatch publishes and what run_launch polls).
        assert 'ENDPOINT_FILE="' + str(cfg.endpoints_dir / "prog-123.json") + '"' in script
        assert "python -m pip install -e ." in script
        # Install hooks run relative commands (pip install -e '.') — the
        # cd into program_root must come BEFORE them, not just before
        # the entrypoint.
        assert script.index('cd "') < script.index("python -m pip install -e .")
        assert "echo done || true" in script  # teardown hook
        assert 'export FOO="bar"' in script
        assert "python -m my_prog --config x.yaml" in script

    def test_pipeline_script_has_mail_directives_when_configured(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        # Rebuild with mail fields
        cfg = SlurmConfig(
            **{
                **{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()},
                "mail_user": "you@example.com",
                "mail_events": "BEGIN,END,FAIL",
            }
        )
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),
                ShellResult(0, "22222\n", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.submit_vllm(_make_vllm_spec(tmp_path))
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))

        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "#SBATCH --mail-user=you@example.com" in script
        assert "#SBATCH --mail-type=BEGIN,END,FAIL" in script

    def test_pipeline_script_omits_mail_when_not_configured(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),
                ShellResult(0, "22222\n", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.submit_vllm(_make_vllm_spec(tmp_path))
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))

        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "--mail-user" not in script
        assert "--mail-type" not in script

    def test_qos_override_changes_pipeline_script(self, tmp_path: Path) -> None:
        from bear_harness._launch import SlurmOverrides

        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),  # vllm sbatch
                ShellResult(0, "22222\n", ""),  # pipeline sbatch
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        overrides = SlurmOverrides(qos="bbshort")
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))
        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "#SBATCH --qos=bbshort" in script

    def test_walltime_override_changes_pipeline_script(self, tmp_path: Path) -> None:
        from bear_harness._launch import SlurmOverrides

        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),  # vllm sbatch
                ShellResult(0, "22222\n", ""),  # pipeline sbatch
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        overrides = SlurmOverrides(walltime="00:10:00")
        runner.submit_vllm(_make_vllm_spec(tmp_path), overrides=overrides)
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))
        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "#SBATCH --time=00:10:00" in script

    def test_pipeline_venv_prefers_node_local_scratch(self, tmp_path: Path) -> None:
        # SLURM does not set TMPDIR on BlueBEAR and node /tmp is small
        # (a 20GB extraction filled it, 2026-06-11). BB_WORKDIR on
        # /scratch must be exported as TMPDIR BEFORE the venv path is
        # computed, so the venv lands on node-local disk.
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "11111\n", ""),
                ShellResult(0, "22222\n", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.submit_vllm(_make_vllm_spec(tmp_path))
        runner.submit_pipeline(_make_pipeline_spec(tmp_path))

        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert 'BB_WORKDIR=$(mktemp -d "/scratch/${USER}_${SLURM_JOB_ID}.XXXXXX")' in script
        assert 'export TMPDIR="$BB_WORKDIR"' in script
        assert script.index("BB_WORKDIR=$(mktemp") < script.index('VENV_DIR="${TMPDIR:-/tmp}')
        assert 'rm -rf "$BB_WORKDIR"' in script

    def test_no_dependency_when_no_depends_on(self, tmp_path: Path) -> None:
        # A worker with no incoming edge (e.g. ETL) submits with NO --dependency and
        # NO endpoint-wait block — proving the template's vLLM coupling is gated off.
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "33333\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        handle = runner.submit_pipeline(_make_pipeline_spec(tmp_path))
        assert handle.job_id == "33333"
        pipeline_argv = shell.calls[0]
        assert not any("--dependency" in arg for arg in pipeline_argv)
        script = (cfg.runs_dir / "prog-123" / "pipeline.sbatch").read_text()
        assert "#SBATCH --dependency" not in script
        assert "Wait for the vLLM endpoint file" not in script

    def test_requires_output_and_status_paths(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "11111\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.submit_vllm(_make_vllm_spec(tmp_path))
        spec = _make_pipeline_spec(tmp_path)
        bad = PipelineSpec(
            command=spec.command,
            env=spec.env,
            cwd=spec.cwd,
            log_path=spec.log_path,
            install=spec.install,
            teardown=spec.teardown,
            python=spec.python,
            output_dir=None,
            status_file=None,
        )
        with pytest.raises(RuntimeError, match="output_dir"):
            runner.submit_pipeline(bad)


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


class TestPoll:
    def _handle(self, tmp_path: Path) -> JobHandle:
        return JobHandle(
            job_id="55555",
            log_path=tmp_path / "runs" / "foo" / "pipeline.log",
            kind="pipeline",
        )

    def test_running(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "RUNNING\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        assert runner.poll(self._handle(tmp_path)) == JobState.RUNNING

    def test_pending(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "PENDING\n", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        assert runner.poll(self._handle(tmp_path)) == JobState.PENDING

    def test_falls_back_to_sacct_after_squeue_empty(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "\n", ""),  # squeue empty
                ShellResult(0, "55555|COMPLETED\n55555.batch|COMPLETED\n", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        assert runner.poll(self._handle(tmp_path)) == JobState.COMPLETED
        assert shell.calls[0][0] == "squeue"
        assert shell.calls[1][0] == "sacct"

    def test_sacct_reports_failed(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "", ""),
                ShellResult(0, "55555|FAILED\n", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        assert runner.poll(self._handle(tmp_path)) == JobState.FAILED

    def test_unknown_when_no_record(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell(
            [
                ShellResult(0, "", ""),
                ShellResult(0, "", ""),
            ]
        )
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        assert runner.poll(self._handle(tmp_path)) == JobState.UNKNOWN


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_scancel_argv(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(0, "", "")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        runner.cancel(
            JobHandle(
                job_id="77777",
                log_path=tmp_path / "x" / "p.log",
                kind="pipeline",
            )
        )
        assert shell.calls[0] == ("scancel", "77777")

    def test_scancel_failure_is_swallowed(self, tmp_path: Path) -> None:
        cfg = _slurm_config(tmp_path)
        shell = _RecordingShell([ShellResult(1, "", "job not found")])
        runner = SlurmRunner(config=cfg, runs_dir=cfg.runs_dir, run_shell=shell)
        # Should not raise.
        runner.cancel(
            JobHandle(
                job_id="77777",
                log_path=tmp_path / "x" / "p.log",
                kind="pipeline",
            )
        )


# ---------------------------------------------------------------------------
# State mapper
# ---------------------------------------------------------------------------


class TestMapState:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("RUNNING", JobState.RUNNING),
            ("PENDING", JobState.PENDING),
            ("COMPLETED", JobState.COMPLETED),
            ("FAILED", JobState.FAILED),
            ("CANCELLED+", JobState.CANCELLED),
            ("NODE_FAIL", JobState.FAILED),
            ("BOGUS", JobState.UNKNOWN),
            ("", JobState.UNKNOWN),
        ],
    )
    def test_map(self, raw: str, expected: str) -> None:
        assert _map_state(raw) == expected


# ---------------------------------------------------------------------------
# count_active_jobs — backs the guardrail concurrency cap (squeue, never PID)
# ---------------------------------------------------------------------------


class TestCountActiveJobs:
    def test_counts_squeue_lines(self, tmp_path: Path) -> None:
        shell = _RecordingShell([ShellResult(returncode=0, stdout="123\n124\n125\n", stderr="")])
        runner = SlurmRunner(
            config=_slurm_config(tmp_path),
            runs_dir=tmp_path / "runs",
            run_shell=shell,
        )
        assert runner.count_active_jobs() == 3
        # keyed on squeue (node-agnostic), never on a PID
        assert shell.calls[0][0] == "squeue"

    def test_empty_queue_is_zero(self, tmp_path: Path) -> None:
        shell = _RecordingShell([ShellResult(returncode=0, stdout="\n", stderr="")])
        runner = SlurmRunner(
            config=_slurm_config(tmp_path),
            runs_dir=tmp_path / "runs",
            run_shell=shell,
        )
        assert runner.count_active_jobs() == 0

    def test_squeue_failure_counts_zero(self, tmp_path: Path) -> None:
        """A squeue hiccup must not wedge launches — other caps still bind."""
        shell = _RecordingShell([ShellResult(returncode=1, stdout="", stderr="slurm down")])
        runner = SlurmRunner(
            config=_slurm_config(tmp_path),
            runs_dir=tmp_path / "runs",
            run_shell=shell,
        )
        assert runner.count_active_jobs() == 0
