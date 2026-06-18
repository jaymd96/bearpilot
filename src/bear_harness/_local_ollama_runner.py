"""Runner that composes OllamaBackend + MessagesShim for Mac local mode.

Sibling of :class:`LocalSubprocessRunner` (local vLLM) and
:class:`SlurmRunner` (BlueBEAR). Instead of spawning a vLLM process, it
starts an Ollama daemon (via :class:`OllamaBackend`), stands up the
Anthropic↔OpenAI :class:`MessagesShim` in front of it, and writes an
endpoint file pointing at the shim — from the perspective of
``run_launch`` and the pipeline program, this looks indistinguishable
from a vLLM server.

**Why another Runner class instead of a LocalBackend protocol layer.**
The existing ``StubRunner`` in ``test_launch.py`` already proves that a
Runner can write the endpoint file synchronously inside ``submit_vllm``
and return immediately. Reusing that shape keeps ``_launch.py``
completely untouched and avoids premature abstraction for a single new
backend. When a third local backend lands (hypothetical) the right time
to extract a ``LocalBackend`` protocol is then, not now.

**Why delegate pipeline submission to an inner Runner.** All the
subprocess-management machinery (log redirection, wait threads,
process-group SIGTERM, terminal-state tracking) already lives in
``LocalSubprocessRunner``. Duplicating it here would be pure rework.
The runner holds a reference to any ``Runner`` implementation that can
handle ``submit_pipeline`` — in production that is a
``LocalSubprocessRunner``; in tests it is a stub.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bear_harness._endpoint_discovery import EndpointRecord, write_endpoint_atomic
from bear_harness._messages_shim_server import MessagesShim
from bear_harness._runner import (
    JobHandle,
    JobState,
    PipelineSpec,
    Runner,
    VllmSpec,
)

logger = logging.getLogger(__name__)


class OllamaLike(Protocol):
    """Duck-typed protocol that :class:`OllamaBackend` satisfies structurally.

    Declared here rather than imported from ``_local_ollama`` so this
    module has no hard dependency on the Ollama module — test fakes can
    drop in without reaching across the ``_local_ollama`` surface. The
    real :class:`OllamaBackend` satisfies this protocol by nature of its
    existing public interface (no adapter needed).
    """

    base_url: str

    def start(self) -> None: ...
    def stop(self) -> None: ...


@dataclass(slots=True)
class LocalOllamaRunner(Runner):
    """Composite Runner: Ollama daemon + Anthropic shim + delegated pipeline runner.

    Lifecycle:

    - ``submit_vllm(spec)``:

      1. ``ollama.start()`` — ensures the daemon is up and the requested
         model is pulled. Blocking; may take minutes on first pull.
      2. ``shim.start()`` — binds the loopback Anthropic translator. If
         this fails, ``ollama.stop()`` is called to roll back.
      3. Touch ``spec.log_path`` so artifact collection has something to
         tar. The file contains a single line describing the wiring —
         useful for post-mortem debugging.
      4. Write an endpoint file atomically pointing at ``shim.base_url``.
         Crucially, ``base_url`` is stored **without** a ``/v1`` suffix
         so that ``probe_endpoint``'s ``f"{base}/v1/models"`` path
         construction lands on the correct shim route.
      5. Return a synthetic ``JobHandle`` with ``kind="vllm"`` so the
         main launch loop treats it as the model slot.

    - ``submit_pipeline(spec)``: delegate verbatim to ``pipeline_runner``.
    - ``poll(handle)``: for the vLLM slot, return internal state (RUNNING
      after submit, CANCELLED after cancel); for pipeline handles, delegate.
    - ``cancel(handle)``: for the vLLM slot, stop shim then ollama and
      mark terminal; for pipeline handles, delegate. Idempotent.
    """

    endpoints_dir: Path
    ollama: OllamaLike
    shim: MessagesShim
    pipeline_runner: Runner
    _vllm_handle: JobHandle | None = field(default=None, init=False)
    _vllm_state: str = field(default=JobState.UNKNOWN, init=False)

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:
        """Stand up ollama + shim and publish the endpoint file.

        ``spec.serve_command`` is intentionally ignored — the Ollama
        path does not execute a ``vllm serve`` process. The spec is
        consulted for ``endpoint_path``, ``served_model_name``,
        ``api_key``, and ``log_path`` only.
        """
        self.ollama.start()
        try:
            self.shim.start()
        except Exception:
            # Shim failed to bind — roll back the ollama startup so
            # a retried submit does not attach to a half-configured state.
            try:
                self.ollama.stop()
            except Exception:
                logger.exception("ollama.stop() rollback failed")
            raise

        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text(
            f"bear-harness LocalOllamaRunner: "
            f"ollama={self.ollama.base_url} shim={self.shim.base_url}\n"
        )

        job_id = f"ollama-{os.getpid()}-{self.shim.port}"
        record = EndpointRecord(
            # probe_endpoint appends "/v1/models" — store the bare host
            # so the final URL is correct.
            base_url=self.shim.base_url,
            # Shim ignores auth; preserved so run.json and caller-side
            # logging stay consistent with the vLLM path's shape.
            api_key=spec.api_key,
            model=spec.served_model_name,
            job_id=job_id,
        )
        write_endpoint_atomic(spec.endpoint_path, record)
        logger.info(
            "LocalOllamaRunner ready: endpoint=%s shim=%s",
            spec.endpoint_path,
            self.shim.base_url,
        )

        handle = JobHandle(
            job_id=job_id,
            log_path=spec.log_path,
            kind="vllm",
        )
        self._vllm_handle = handle
        self._vllm_state = JobState.RUNNING
        return handle

    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
        return self.pipeline_runner.submit_pipeline(spec)

    def poll(self, handle: JobHandle) -> str:
        if self._is_vllm_handle(handle):
            return self._vllm_state
        return self.pipeline_runner.poll(handle)

    def cancel(self, handle: JobHandle) -> None:
        if self._is_vllm_handle(handle):
            self._cancel_vllm()
            return
        self.pipeline_runner.cancel(handle)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_vllm_handle(self, handle: JobHandle) -> bool:
        return (
            self._vllm_handle is not None
            and handle.job_id == self._vllm_handle.job_id
        )

    def _cancel_vllm(self) -> None:
        if self._vllm_state in JobState.TERMINAL:
            return  # already cancelled — idempotent
        self._vllm_state = JobState.CANCELLED
        try:
            self.shim.stop()
        except Exception:
            logger.exception("shim.stop() failed during cancel")
        try:
            self.ollama.stop()
        except Exception:
            logger.exception("ollama.stop() failed during cancel")


__all__ = [
    "LocalOllamaRunner",
    "OllamaLike",
]
