"""Unit tests for ``LocalSubprocessRunner``.

These tests spawn real subprocesses but pick commands that are
guaranteed to exit quickly and don't require a real vLLM. The
endpoint-writer thread is exercised via a fake vLLM that prints the
expected ready marker then sleeps.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from bear_harness._endpoint_discovery import read_endpoint
from bear_harness._runner import (
    JobState,
    LocalSubprocessRunner,
    PipelineSpec,
    VllmSpec,
    pick_free_port,
)


def _runner(tmp_path: Path) -> LocalSubprocessRunner:
    return LocalSubprocessRunner(
        endpoints_dir=tmp_path / "endpoints",
        poll_interval_seconds=0.05,
    )


def _wait_terminal(runner: LocalSubprocessRunner, handle, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = runner.poll(handle)
        if JobState.is_terminal(state):
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {handle.job_id} never reached terminal state")


class TestPipelineLifecycle:
    def test_successful_pipeline(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        spec = PipelineSpec(
            command=(sys.executable, "-c", "print('hi')"),
            env={},
            cwd=tmp_path,
            log_path=tmp_path / "pipeline.log",
        )
        handle = runner.submit_pipeline(spec)
        assert _wait_terminal(runner, handle) == JobState.COMPLETED
        assert (tmp_path / "pipeline.log").read_text().strip() == "hi"

    def test_failed_pipeline(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        spec = PipelineSpec(
            command=(sys.executable, "-c", "import sys; sys.exit(7)"),
            env={},
            cwd=tmp_path,
            log_path=tmp_path / "pipeline.log",
        )
        handle = runner.submit_pipeline(spec)
        assert _wait_terminal(runner, handle) == JobState.FAILED

    def test_cancel_running_pipeline(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        spec = PipelineSpec(
            command=(sys.executable, "-c", "import time; time.sleep(30)"),
            env={},
            cwd=tmp_path,
            log_path=tmp_path / "pipeline.log",
        )
        handle = runner.submit_pipeline(spec)
        # Give it a moment to start.
        time.sleep(0.2)
        assert runner.poll(handle) == JobState.RUNNING
        runner.cancel(handle)
        assert _wait_terminal(runner, handle) == JobState.CANCELLED


class TestVllmEndpointWriter:
    def test_endpoint_written_when_ready_marker_emitted(self, tmp_path: Path) -> None:
        """A fake 'vllm' prints the ready marker, the runner writes the JSON."""
        endpoint_path = tmp_path / "endpoints" / "job.json"
        spec = VllmSpec(
            serve_command=(
                sys.executable,
                "-c",
                "import time, sys\n"
                "print('Uvicorn running on http://127.0.0.1:8000', flush=True)\n"
                "time.sleep(10)\n",
            ),
            env={},
            log_path=tmp_path / "vllm.log",
            endpoint_path=endpoint_path,
            served_model_name="fake-model",
            base_url="http://127.0.0.1:8000/v1",
            api_key="dummy",
            boot_timeout_seconds=10.0,
        )
        runner = _runner(tmp_path)
        handle = runner.submit_vllm(spec)
        try:
            # Poll until endpoint file appears (or fail after 5s).
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not endpoint_path.exists():
                time.sleep(0.05)
            assert endpoint_path.exists(), "endpoint file was not written"
            record = read_endpoint(endpoint_path)
            assert record.model == "fake-model"
            assert record.base_url == "http://127.0.0.1:8000/v1"
            assert record.job_id == handle.job_id
        finally:
            runner.cancel(handle)
            _wait_terminal(runner, handle)


class TestPickFreePort:
    def test_returns_usable_port(self) -> None:
        port = pick_free_port(low=55000, high=55100)
        assert 55000 <= port <= 55100

    def test_no_free_port(self) -> None:
        with pytest.raises(RuntimeError, match="no free port"):
            pick_free_port(low=0, high=-1)


def test_random_api_key_never_starts_with_dash() -> None:
    """A dash-leading key turns `--api-key <key>` into two option flags
    downstream (argparse: "expected one argument")."""
    from bear_harness._runner import random_api_key

    for _ in range(64):
        key = random_api_key()
        assert key.startswith("bh-")
        assert not key.startswith("-")
