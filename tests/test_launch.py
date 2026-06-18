"""Tests for the ``run_launch`` orchestration flow using a stub runner.

These tests exercise the full 12-step control flow from ``_launch.py``
without spawning any real subprocess. A stub runner simulates vLLM by
writing an endpoint file synchronously inside ``submit_vllm`` and
short-circuiting the pipeline command so it exits 0 immediately.

The HTTP probe is bypassed by monkeypatching ``httpx.Client``-returning
factory to a tiny fake that always returns 200.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import bear_harness._endpoint_discovery as _ed
from bear_harness._bear_config import BearConfig, LocalConfig
from bear_harness._endpoint_discovery import (
    EndpointRecord,
    write_endpoint_atomic,
)
from bear_harness._launch import LaunchOptions, run_launch
from bear_harness._manifest import load_manifest
from bear_harness._notify import NotifyEvent, NotifyOutcome
from bear_harness._runner import JobHandle, JobState, PipelineSpec, Runner, VllmSpec

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_pipeline.toml"


# ---------------------------------------------------------------------------
# Fake HTTP client: both /v1/models and /v1/messages return 200
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> Any:
        return self._body


class _FakeHttpxClient:
    def __init__(self, model: str) -> None:
        self._model = model

    def __enter__(self) -> _FakeHttpxClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        return _FakeResponse(200, {"data": [{"id": self._model}]})

    def post(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> _FakeResponse:
        return _FakeResponse(200, {"content": []})


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_probe(record: EndpointRecord, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_ed, "probe_endpoint", _fake_probe)
    # The launch module imports the name directly; patch there too.
    import bear_harness._launch as _launch

    monkeypatch.setattr(_launch, "probe_endpoint", _fake_probe)


# ---------------------------------------------------------------------------
# Stub runner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeJob:
    handle: JobHandle
    state: str = JobState.RUNNING


@dataclass(slots=True)
class StubRunner(Runner):
    """A runner that pretends to run vLLM and the pipeline.

    ``submit_vllm`` writes an endpoint file synchronously and records
    a RUNNING handle. ``submit_pipeline`` writes a status file claiming
    the pipeline finished, then marks the handle COMPLETED. ``poll``
    returns the recorded state; ``cancel`` is a no-op.

    ``fail_pipeline`` flips the pipeline's final state to FAILED so
    the failure path can be exercised.
    """

    base_url: str = "http://127.0.0.1:8000/v1"
    fail_pipeline: bool = False
    skip_endpoint_write: bool = False
    jobs: dict[str, _FakeJob] = field(default_factory=dict)

    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:
        handle = JobHandle(job_id="vllm-1", log_path=spec.log_path, kind="vllm")
        self.jobs[handle.job_id] = _FakeJob(handle=handle, state=JobState.RUNNING)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text("fake vllm log\n")
        if not self.skip_endpoint_write:
            write_endpoint_atomic(
                spec.endpoint_path,
                EndpointRecord(
                    base_url=self.base_url,
                    api_key=spec.api_key,
                    model=spec.served_model_name,
                    job_id=handle.job_id,
                ),
            )
        return handle

    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
        handle = JobHandle(job_id="pipe-1", log_path=spec.log_path, kind="pipeline")
        state = JobState.FAILED if self.fail_pipeline else JobState.COMPLETED
        self.jobs[handle.job_id] = _FakeJob(handle=handle, state=state)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text("fake pipeline log\n")
        # Also drop a plausible output file so artifact collection has
        # something to pack.
        (spec.cwd / "output").mkdir(parents=True, exist_ok=True)
        return handle

    def poll(self, handle: JobHandle) -> str:
        job = self.jobs.get(handle.job_id)
        if job is None:
            return JobState.UNKNOWN
        return job.state

    def cancel(self, handle: JobHandle) -> None:
        job = self.jobs.get(handle.job_id)
        if job is not None:
            job.state = JobState.CANCELLED


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest_dir(tmp_path: Path) -> Path:
    p = tmp_path / "program"
    p.mkdir()
    (p / "pipeline.toml").write_text(FIXTURE.read_text())
    return p


@pytest.fixture
def local_config(tmp_path: Path) -> BearConfig:
    return BearConfig(
        mode="local",
        local=LocalConfig(
            runs_dir=tmp_path / "runs",
            endpoints_dir=tmp_path / "endpoints",
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_successful_launch(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner()
        result = run_launch(options, runner, _sleep=lambda _s: None)
        assert result.final_state == "done"
        assert result.endpoint is not None
        assert result.endpoint.model == "stub-model"
        assert result.artifacts_tarball is not None
        assert result.artifacts_tarball.exists()
        # Final run.json must exist and report "done"
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert run_json["state"] == "done"
        assert run_json["vllm_job_id"] == "vllm-1"
        assert run_json["pipeline_job_id"] == "pipe-1"

    def test_dry_run_exits_before_submit(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            dry_run=True,
        )
        runner = StubRunner()
        result = run_launch(options, runner, _sleep=lambda _s: None)
        assert result.final_state == "dry_run"
        assert result.vllm_handle is None
        assert not runner.jobs


class TestEtlPreset:
    """The model-less ETL preset runs through the UNCHANGED kernel — the concrete
    falsification of "secretly vLLM-only" (specs/01-foundational-contract.md §6).
    """

    def test_etl_runs_with_no_server(self, tmp_path: Path, local_config: BearConfig) -> None:
        prog = tmp_path / "etlprog"
        prog.mkdir()
        (prog / "pipeline.toml").write_text(
            (Path(__file__).parent / "fixtures" / "etl_pipeline.toml").read_text()
        )
        options = LaunchOptions(
            manifest=load_manifest(prog),
            config=local_config,
            status_poll_interval_seconds=0.0,
        )
        result = run_launch(options, StubRunner(), _sleep=lambda _s: None)
        assert result.final_state == "done"
        assert result.vllm_handle is None  # no sidecar — ETL is model-less
        assert result.pipeline_handle is not None
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert run_json["state"] == "done"
        assert run_json["vllm_job_id"] == ""  # no server was ever submitted


class TestFailurePaths:
    def test_endpoint_never_appears(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        """If the vllm runner never writes the endpoint file, launch fails fast."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            vllm_boot_timeout_seconds=0.01,
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner(skip_endpoint_write=True)
        result = run_launch(options, runner, _sleep=lambda _s: None)
        assert result.final_state == "failed"
        assert result.error is not None
        assert "endpoint" in result.error.lower() or "timed out" in result.error.lower()

    def test_pipeline_exits_nonzero(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner(fail_pipeline=True)
        result = run_launch(options, runner, _sleep=lambda _s: None)
        assert result.final_state == "failed"
        assert result.artifacts_tarball is not None
        # Even on failure we still collect artifacts (at least the logs).
        assert result.artifacts_tarball.exists()


class TestEnvSubstitution:
    def test_pipeline_command_sees_resolved_vars(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        """The pipeline command received by the runner has $VARS resolved."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            status_poll_interval_seconds=0.0,
        )

        captured: dict[str, PipelineSpec] = {}

        class _CapturingRunner(StubRunner):
            def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
                captured["spec"] = spec
                return super().submit_pipeline(spec)

        runner = _CapturingRunner()
        run_launch(options, runner, _sleep=lambda _s: None)
        spec = captured["spec"]
        # $PYTHON should be substituted to the runner's python path
        assert "$PYTHON" not in spec.command
        # DATA_PIPELINE_STATUS_FILE injected into env
        assert "DATA_PIPELINE_STATUS_FILE" in spec.env
        assert spec.env["DATA_PIPELINE_STATUS_FILE"].endswith(".bear-harness-status.json")
        assert spec.env["PYTHONUNBUFFERED"] == "1"


class TestDetach:
    """Detached launch: submit the jobs, hand back a handle, do NOT babysit.

    This is the seam that makes the harness an LLM tool — ``deploy``
    returns in bounded time with a run id, and ``status`` / ``results``
    read filesystem state afterwards. The cut sits after the vLLM probe
    (the endpoint must be known to bake the pipeline command), so a
    detached run still fails loudly if the server never comes up.
    """

    def test_detach_returns_after_submit_without_babysitting(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner()
        result = run_launch(options, runner, detach=True, _sleep=lambda _s: None)
        # Both jobs were submitted and the endpoint was discovered + probed.
        assert result.vllm_handle is not None
        assert result.pipeline_handle is not None
        assert result.endpoint is not None
        # ...but the run was NOT driven to completion.
        assert result.final_state == "running"
        assert result.artifacts_tarball is None
        # vLLM must stay up — the pipeline still depends on it.
        assert runner.jobs["vllm-1"].state == JobState.RUNNING
        # run.json reflects a live, submitted run an observer can attach to.
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert run_json["state"] == "running"
        assert run_json["vllm_job_id"] == "vllm-1"
        assert run_json["pipeline_job_id"] == "pipe-1"

    def test_detach_still_fails_when_endpoint_never_appears(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            vllm_boot_timeout_seconds=0.01,
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner(skip_endpoint_write=True)
        result = run_launch(options, runner, detach=True, _sleep=lambda _s: None)
        assert result.final_state == "failed"
        assert result.error is not None


class TestLaunchResultSerialisation:
    """``LaunchResult.as_dict`` is the stable agent-facing handle schema."""

    def test_as_dict_carries_everything_to_reattach(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        runner = StubRunner()
        result = run_launch(options, runner, detach=True, _sleep=lambda _s: None)
        payload = json.loads(json.dumps(result.as_dict()))  # must round-trip
        assert payload["job_id"] == result.job_id
        assert payload["state"] == "running"
        assert payload["run_dir"] == str(result.run_dir)
        assert payload["vllm_job_id"] == "vllm-1"
        assert payload["pipeline_job_id"] == "pipe-1"
        assert payload["base_url"] == runner.base_url
        assert payload["model"] == "stub-model"


class TestGuardrails:
    """The default-deny gate is authoritative and un-bypassable, at submit.

    A denied request must never reach the runner — no sbatch, no subprocess —
    and the denial must be recorded in run.json + the JSON handle. The gate
    reads only RESOURCES (qos / walltime / GPU-hours / concurrency), never the
    science (model / manifest).
    """

    def test_denied_request_never_reaches_the_runner(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
    ) -> None:
        """qos not on the (default bbshort-only) allowlist => denied before submit."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            qos_override="bbgpu",  # not on the tight default allowlist
            status_poll_interval_seconds=0.0,
        )

        class _ExplodingRunner(StubRunner):
            def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:
                msg = "submit_vllm must NOT be called on a denied launch"
                raise AssertionError(msg)

        runner = _ExplodingRunner()
        result = run_launch(options, runner, _sleep=lambda _s: None)
        assert result.final_state == "denied"
        assert not runner.jobs  # nothing was submitted
        assert "bbgpu" in (result.error or "")
        # the JSON handle surfaces the denial for the agent
        assert result.as_dict()["guardrail"]["allowed"] is False  # type: ignore[index]

    def test_denied_writes_violations_to_run_json(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(manifest=manifest, config=local_config, qos_override="bbgpu")
        result = run_launch(options, StubRunner(), _sleep=lambda _s: None)
        assert result.final_state == "denied"
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert run_json["state"] == "denied"
        caps = [v["cap"] for v in run_json["notes"]["guardrail"]["violations"]]
        assert "qos_allowlist" in caps

    def test_allowed_request_proceeds_to_completion(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        """A default local run (no qos / no gres) passes the leash and runs."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        result = run_launch(options, StubRunner(), _sleep=lambda _s: None)
        assert result.final_state == "done"  # the gate let it through

    def test_guardrail_ignores_model_and_manifest(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
    ) -> None:
        """Resources-not-science: identical resources => identical decision (model differs)."""
        manifest = load_manifest(manifest_dir)

        def _decide(model: str) -> dict:
            options = LaunchOptions(
                manifest=manifest,
                config=local_config,
                model=model,
                qos_override="bbgpu",  # not in the default allowlist => denied
                dry_run=True,
            )
            result = run_launch(options, StubRunner(), _sleep=lambda _s: None)
            assert result.guardrail is not None
            return result.guardrail

        dec_a = _decide("tiny-model")
        dec_b = _decide("huge-70b-model")
        assert dec_a["allowed"] is False
        assert dec_b["allowed"] is False
        assert [v["cap"] for v in dec_a["violations"]] == [v["cap"] for v in dec_b["violations"]]

    def test_concurrency_probe_denies_when_cluster_busy(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest, config=local_config, status_poll_interval_seconds=0.0
        )
        # probe reports the cluster already at the default cap (2)
        result = run_launch(
            options, StubRunner(), concurrency_probe=lambda: 2, _sleep=lambda _s: None
        )
        assert result.final_state == "denied"
        run_json = json.loads((result.run_dir / "run.json").read_text())
        caps = [v["cap"] for v in run_json["notes"]["guardrail"]["violations"]]
        assert "max_concurrent_jobs" in caps


class TestNotify:
    """Notify fires fire-and-forget at terminal transitions, and never breaks a run.

    The kernel calls the injected ``notifier`` at the blocking-path terminal
    (done / failed) and at the early-failure path; the outcome is surfaced in the
    JSON handle and ``run.json``. Notify is OFF by default (opt-in), and a
    notifier that raises can never derail the run — the reliability bar is
    encoded at the kernel boundary, not trusted to the backend.
    """

    def test_off_by_default_leaves_notify_none(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        result = run_launch(options, StubRunner(), _sleep=lambda _s: None)
        assert result.final_state == "done"
        assert result.notify is None  # opt-in: no backend configured => silent
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert "notify" not in run_json["notes"]

    def test_fires_done_on_successful_terminal(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        events: list[NotifyEvent] = []

        def fake_notifier(event: NotifyEvent) -> NotifyOutcome:
            events.append(event)
            return NotifyOutcome(fired=("test",))

        result = run_launch(options, StubRunner(), notifier=fake_notifier, _sleep=lambda _s: None)
        assert result.final_state == "done"
        assert [e.event for e in events] == ["done"]
        assert events[0].run_id == result.job_id
        # surfaced in the handle + run.json notes
        assert result.notify == {"fired": ["test"], "errors": [], "skipped_reason": ""}
        run_json = json.loads((result.run_dir / "run.json").read_text())
        assert run_json["notes"]["notify"]["fired"] == ["test"]

    def test_fires_failed_on_pipeline_failure(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )
        events: list[NotifyEvent] = []

        def fake_notifier(event: NotifyEvent) -> NotifyOutcome:
            events.append(event)
            return NotifyOutcome(fired=("test",))

        result = run_launch(
            options, StubRunner(fail_pipeline=True), notifier=fake_notifier, _sleep=lambda _s: None
        )
        assert result.final_state == "failed"
        assert [e.event for e in events] == ["failed"]

    def test_fires_failed_on_early_failure_path(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        """An early failure (no endpoint) is a terminal 'failed' too — it must notify."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            vllm_boot_timeout_seconds=0.01,
            status_poll_interval_seconds=0.0,
        )
        events: list[NotifyEvent] = []

        def fake_notifier(event: NotifyEvent) -> NotifyOutcome:
            events.append(event)
            return NotifyOutcome(fired=("test",))

        result = run_launch(
            options,
            StubRunner(skip_endpoint_write=True),
            notifier=fake_notifier,
            _sleep=lambda _s: None,
        )
        assert result.final_state == "failed"
        assert [e.event for e in events] == ["failed"]

    def test_a_raising_notifier_never_breaks_the_run(
        self,
        manifest_dir: Path,
        local_config: BearConfig,
        patch_probe: None,
    ) -> None:
        """Reliability bar, encoded at the kernel boundary: notify can't derail a run."""
        manifest = load_manifest(manifest_dir)
        options = LaunchOptions(
            manifest=manifest,
            config=local_config,
            model="stub-model",
            status_poll_interval_seconds=0.0,
        )

        def boom(event: NotifyEvent) -> NotifyOutcome:
            msg = "notifier exploded"
            raise RuntimeError(msg)

        result = run_launch(options, StubRunner(), notifier=boom, _sleep=lambda _s: None)
        assert result.final_state == "done"  # run completed despite notify blowing up
        assert result.notify is None
