"""Tests for ``LocalOllamaRunner`` — the Runner that composes
``OllamaBackend`` + ``MessagesShim`` for Mac local mode.

The runner is tested end-to-end in-process with zero subprocesses:

- Fake ``OllamaBackend`` (just records ``start``/``stop``).
- Real ``MessagesShim`` with an ``httpx.MockTransport`` upstream — this
  proves the endpoint URL the runner writes actually serves real
  Anthropic ``/v1/messages`` traffic by round-tripping a request through
  the shim to the fake upstream.
- Stubbed inner pipeline ``Runner`` for ``submit_pipeline`` delegation.

Everything observable on the outside of the runner (endpoint file
contents, shim reachability, pipeline delegation, cancel semantics) is
asserted directly rather than mocked.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from bear_harness._endpoint_discovery import read_endpoint
from bear_harness._local_ollama_runner import LocalOllamaRunner
from bear_harness._messages_shim_server import MessagesShim
from bear_harness._runner import (
    JobHandle,
    JobState,
    PipelineSpec,
    Runner,
    VllmSpec,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeOllama:
    """Minimal duck-typed stand-in for ``OllamaBackend``.

    Conforms to the ``OllamaLike`` protocol expected by the runner.
    """

    base_url: str = "http://upstream.test/v1"
    started: bool = False
    stopped: bool = False
    start_raises: Exception | None = None

    def start(self) -> None:
        if self.start_raises is not None:
            raise self.start_raises
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@dataclass(slots=True)
class _FakePipelineRunner(Runner):
    """Stub inner ``Runner`` that only handles the pipeline path."""

    submitted: list[PipelineSpec] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)
    pipeline_state: str = JobState.COMPLETED

    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:  # pragma: no cover
        msg = "inner pipeline runner must never be asked for vllm"
        raise AssertionError(msg)

    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
        self.submitted.append(spec)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text("fake pipeline log\n")
        return JobHandle(
            job_id="fake-pipeline-42",
            log_path=spec.log_path,
            kind="pipeline",
        )

    def poll(self, handle: JobHandle) -> str:
        self.polled.append(handle.job_id)
        return self.pipeline_state

    def cancel(self, handle: JobHandle) -> None:
        self.cancelled.append(handle.job_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_upstream() -> list[httpx.Request]:
    return []


@pytest.fixture
def upstream_client(captured_upstream: list[httpx.Request]) -> httpx.Client:
    """``httpx.Client`` stubbed with ``MockTransport`` — stand-in for Ollama."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured_upstream.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "llama3.2",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://upstream.test/v1",
    )


@pytest.fixture
def shim(upstream_client: httpx.Client) -> Iterator[MessagesShim]:
    s = MessagesShim(
        upstream_client=upstream_client,
        served_model_name="llama3.2",
    )
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def runner_harness(
    tmp_path: Path, shim: MessagesShim
) -> Iterator[tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner]]:
    fake_ollama = _FakeOllama()
    inner = _FakePipelineRunner()
    r = LocalOllamaRunner(
        endpoints_dir=tmp_path / "endpoints",
        ollama=fake_ollama,
        shim=shim,
        pipeline_runner=inner,
    )
    try:
        yield r, fake_ollama, inner
    finally:
        if fake_ollama.started and not fake_ollama.stopped:
            fake_ollama.stop()
        shim.stop()


def _make_vllm_spec(tmp_path: Path) -> VllmSpec:
    run_dir = tmp_path / "runs" / "prog"
    run_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "endpoints").mkdir(parents=True, exist_ok=True)
    return VllmSpec(
        serve_command=("ignored-by-ollama-runner",),
        env={},
        log_path=run_dir / "vllm.log",
        endpoint_path=tmp_path / "endpoints" / "prog.json",
        served_model_name="llama3.2",
        base_url="http://ignored",
        api_key="dummy-key",
    )


def _make_pipeline_spec(tmp_path: Path) -> PipelineSpec:
    run_dir = tmp_path / "runs" / "prog"
    run_dir.mkdir(parents=True, exist_ok=True)
    return PipelineSpec(
        command=("python", "-c", "print('hi')"),
        env={},
        cwd=tmp_path,
        log_path=run_dir / "pipeline.log",
    )


# ---------------------------------------------------------------------------
# submit_vllm
# ---------------------------------------------------------------------------


class TestSubmitVllm:
    def test_starts_ollama_and_shim_then_writes_endpoint(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, ollama, _ = runner_harness
        spec = _make_vllm_spec(tmp_path)
        handle = r.submit_vllm(spec)

        assert ollama.started
        assert r.shim.port > 0  # shim listening

        # Endpoint file written with the shim's loopback URL (no /v1 suffix;
        # probe_endpoint appends it).
        assert spec.endpoint_path.exists()
        record = read_endpoint(spec.endpoint_path)
        assert record.base_url == r.shim.base_url
        assert "/v1" not in record.base_url.rsplit(":", maxsplit=1)[-1]
        assert record.model == "llama3.2"
        assert record.api_key == "dummy-key"

        # vLLM-slot handle + non-empty log for artifact collection.
        assert handle.kind == "vllm"
        assert spec.log_path.exists()
        assert spec.log_path.read_text()  # non-empty

    def test_endpoint_serves_anthropic_messages_via_shim(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
        captured_upstream: list[httpx.Request],
    ) -> None:
        """POST /v1/messages to the endpoint URL reaches the Ollama upstream."""
        r, _, _ = runner_harness
        spec = _make_vllm_spec(tmp_path)
        r.submit_vllm(spec)
        record = read_endpoint(spec.endpoint_path)

        resp = httpx.post(
            f"{record.base_url}/v1/messages",
            json={
                "model": "llama3.2",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message"
        assert body["content"] == [{"type": "text", "text": "pong"}]
        assert len(captured_upstream) == 1
        assert captured_upstream[0].url.path.endswith("/chat/completions")

    def test_endpoint_serves_models_probe(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        """GET /v1/models on the endpoint URL is what ``probe_endpoint`` hits."""
        r, _, _ = runner_harness
        spec = _make_vllm_spec(tmp_path)
        r.submit_vllm(spec)
        record = read_endpoint(spec.endpoint_path)
        resp = httpx.get(f"{record.base_url}/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert any(m["id"] == "llama3.2" for m in body["data"])

    def test_accepts_overrides_kwarg(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        """submit_vllm must accept an overrides kwarg (ignored in local mode)."""
        from bear_harness._launch import SlurmOverrides

        r, ollama, _ = runner_harness
        spec = _make_vllm_spec(tmp_path)
        handle = r.submit_vllm(spec, overrides=SlurmOverrides(qos="bbshort"))
        assert handle.kind == "vllm"
        assert ollama.started

    def test_ollama_start_failure_propagates_and_rolls_back(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, ollama, _ = runner_harness
        ollama.start_raises = RuntimeError("ollama gone")
        spec = _make_vllm_spec(tmp_path)
        with pytest.raises(RuntimeError, match="ollama gone"):
            r.submit_vllm(spec)
        # Rollback: shim not bound, endpoint file absent.
        assert not spec.endpoint_path.exists()
        assert r.shim.port == 0


# ---------------------------------------------------------------------------
# submit_pipeline
# ---------------------------------------------------------------------------


class TestSubmitPipeline:
    def test_delegates_to_inner_runner(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, _, inner = runner_harness
        spec = _make_pipeline_spec(tmp_path)
        handle = r.submit_pipeline(spec)
        assert handle.kind == "pipeline"
        assert handle.job_id == "fake-pipeline-42"
        assert inner.submitted == [spec]


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


class TestPoll:
    def test_vllm_handle_running_after_submit(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, _, _ = runner_harness
        handle = r.submit_vllm(_make_vllm_spec(tmp_path))
        assert r.poll(handle) == JobState.RUNNING

    def test_pipeline_handle_delegates_to_inner(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, _, inner = runner_harness
        handle = r.submit_pipeline(_make_pipeline_spec(tmp_path))
        inner.pipeline_state = JobState.FAILED
        assert r.poll(handle) == JobState.FAILED
        assert "fake-pipeline-42" in inner.polled


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_vllm_handle_stops_shim_and_ollama(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, ollama, _ = runner_harness
        handle = r.submit_vllm(_make_vllm_spec(tmp_path))
        assert r.shim.port > 0  # sanity
        r.cancel(handle)
        assert ollama.stopped
        assert r.shim.port == 0  # shim stopped
        assert JobState.is_terminal(r.poll(handle))

    def test_cancel_vllm_is_idempotent(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, _, _ = runner_harness
        handle = r.submit_vllm(_make_vllm_spec(tmp_path))
        r.cancel(handle)
        r.cancel(handle)  # second cancel must not raise

    def test_cancel_pipeline_handle_delegates(
        self,
        tmp_path: Path,
        runner_harness: tuple[LocalOllamaRunner, _FakeOllama, _FakePipelineRunner],
    ) -> None:
        r, _, inner = runner_harness
        handle = r.submit_pipeline(_make_pipeline_spec(tmp_path))
        r.cancel(handle)
        assert "fake-pipeline-42" in inner.cancelled
