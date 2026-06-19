"""Runner abstraction: submit a job, poll it, cancel it.

Two backends inherit from :class:`Runner`:

- :class:`LocalSubprocessRunner` (in this module) — spawns processes on
  the host. First-class path, not a test backdoor. Developers use
  ``bear-harness launch --local`` on laptops and in CI.
- ``SlurmRunner`` (Phase C, in ``_slurm_runner.py``) — wraps ``sbatch``
  / ``squeue`` / ``scancel``.

Both runners treat jobs as opaque ``JobHandle`` tokens. The harness
main loop never touches subprocesses or sbatch directly.

The :class:`LocalSubprocessRunner` deliberately does **not** pipe
stdout into memory: logs go to files so SIGKILL of the harness does
not drop them. The ``tail_log`` method lets the CLI stream them to
stdout if the user wants live output.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bear_harness._endpoint_discovery import EndpointRecord, write_endpoint_atomic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobHandle:
    """Opaque reference to a submitted job.

    ``job_id`` is whatever the backend uses internally: a SLURM job id
    for ``SlurmRunner``, a string-ified PID for ``LocalSubprocessRunner``.
    ``log_path`` is where the harness will look for stdout/stderr.
    """

    job_id: str
    log_path: Path
    kind: str  # "vllm" | "pipeline"


class JobState:
    """Terminal / non-terminal job state constants.

    Kept as a string enum rather than ``enum.Enum`` because it is
    easier to compare against SLURM's ``%T`` output, which is also a
    string. ``UNKNOWN`` is the safe default when the backend has no
    record of the job.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls.TERMINAL


@dataclass(frozen=True, slots=True)
class VllmSpec:
    """Everything a runner needs to spawn a vLLM server.

    ``serve_command`` is the full argv. ``endpoint_path`` is where the
    runner must write the endpoint JSON once vLLM is listening.
    ``model`` and ``served_model_name`` are carried along for the
    endpoint record.
    """

    serve_command: tuple[str, ...]
    env: Mapping[str, str]
    log_path: Path
    endpoint_path: Path
    served_model_name: str
    base_url: str
    api_key: str
    boot_timeout_seconds: float = 900.0


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    """Everything a runner needs to spawn a pipeline program.

    ``depends_on`` is a (possibly empty) list of job handles this
    pipeline must wait for. For ``LocalSubprocessRunner`` the
    dependency is enforced by the harness main loop (it waits for the
    endpoint probe before calling ``submit_pipeline``), so this field
    is informational. For ``SlurmRunner`` it becomes ``--dependency=after:JID``.

    ``install`` / ``teardown`` mirror the ``runtime.install`` /
    ``runtime.teardown`` manifest hooks. They are no-ops for the local
    runner (which assumes the developer has already pip-installed
    their program) but the SLURM wrapper runs them inside the compute
    node's tmp venv.
    """

    command: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    log_path: Path
    depends_on: tuple[JobHandle, ...] = ()
    install: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()
    python: str = "python3"
    output_dir: Path | None = None
    status_file: Path | None = None


# ---------------------------------------------------------------------------
# Abstract Runner
# ---------------------------------------------------------------------------


class Runner(ABC):
    """Abstract interface every backend implements.

    Lifecycle: ``submit_vllm(spec)`` → ``poll(handle)`` until the
    endpoint file appears → ``submit_pipeline(spec)`` → ``poll(handle)``
    until terminal → ``cancel(handle)`` for cleanup. The harness main
    loop is written against this interface, not any concrete runner.
    """

    @abstractmethod
    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle: ...

    @abstractmethod
    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle: ...

    @abstractmethod
    def poll(self, handle: JobHandle) -> str: ...

    @abstractmethod
    def cancel(self, handle: JobHandle) -> None: ...

    def is_alive(self, handle: JobHandle) -> bool:
        """Convenience: true if ``poll`` reports a non-terminal state."""
        return not JobState.is_terminal(self.poll(handle))


# ---------------------------------------------------------------------------
# LocalSubprocessRunner
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LocalProcess:
    popen: subprocess.Popen[bytes]
    handle: JobHandle
    wait_thread: threading.Thread | None = None
    endpoint_writer: threading.Thread | None = None
    returncode: int | None = None
    cancelled: bool = False


@dataclass(slots=True)
class LocalSubprocessRunner(Runner):
    """Runner that spawns jobs as subprocesses on the local host.

    First-class code path — developers use this daily via
    ``bear-harness launch --local``. Not a test backdoor.

    Responsibilities beyond "spawn processes":

    - Pipe each job's stdout + stderr to a log file (opened in append
      mode so a reattaching watcher sees everything). Not PIPEs — a
      full PIPE buffer would wedge the subprocess.
    - For vLLM jobs, spawn a background thread that polls the spawned
      process's stdout log for the ready signal, then writes the
      endpoint JSON file. This mirrors the SLURM wrapper's
      responsibility of writing the endpoint file itself.
    - Track terminal state per handle so ``poll`` can report
      COMPLETED / FAILED after the process exits without holding a
      ``Popen`` reference forever.

    The default ``ready_predicate`` matches vLLM's "Uvicorn running on"
    line. Callers can override it (e.g. for tests that use a stub
    server that prints a different marker).
    """

    endpoints_dir: Path
    # predicate: inspects one stdout line, returns True when ready
    ready_predicate: Callable[[str], bool] = field(
        default=lambda line: "Uvicorn running on" in line or "Application startup complete" in line
    )
    poll_interval_seconds: float = 0.2
    _processes: dict[str, _LocalProcess] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    def submit_vllm(self, spec: VllmSpec, **kwargs: object) -> JobHandle:
        log = _open_log(spec.log_path)
        env = _merge_env(spec.env)
        logger.info("spawn vllm: %s", shlex.join(spec.serve_command))
        popen = subprocess.Popen(
            list(spec.serve_command),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        handle = JobHandle(
            job_id=str(popen.pid),
            log_path=spec.log_path,
            kind="vllm",
        )
        entry = _LocalProcess(popen=popen, handle=handle)
        entry.wait_thread = _start_wait_thread(entry, self._lock)
        entry.endpoint_writer = _start_endpoint_writer_thread(
            entry,
            spec=spec,
            predicate=self.ready_predicate,
            poll_interval=self.poll_interval_seconds,
        )
        with self._lock:
            self._processes[handle.job_id] = entry
        return handle

    def submit_pipeline(self, spec: PipelineSpec) -> JobHandle:
        log = _open_log(spec.log_path)
        env = _merge_env(spec.env)
        spec.cwd.mkdir(parents=True, exist_ok=True)
        logger.info("spawn pipeline: %s (cwd=%s)", shlex.join(spec.command), spec.cwd)
        popen = subprocess.Popen(
            list(spec.command),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(spec.cwd),
            start_new_session=True,
        )
        handle = JobHandle(
            job_id=str(popen.pid),
            log_path=spec.log_path,
            kind="pipeline",
        )
        entry = _LocalProcess(popen=popen, handle=handle)
        entry.wait_thread = _start_wait_thread(entry, self._lock)
        with self._lock:
            self._processes[handle.job_id] = entry
        return handle

    def poll(self, handle: JobHandle) -> str:
        with self._lock:
            entry = self._processes.get(handle.job_id)
        if entry is None:
            return JobState.UNKNOWN
        if entry.returncode is None:
            return JobState.RUNNING
        if entry.cancelled:
            return JobState.CANCELLED
        return JobState.COMPLETED if entry.returncode == 0 else JobState.FAILED

    def cancel(self, handle: JobHandle) -> None:
        with self._lock:
            entry = self._processes.get(handle.job_id)
        if entry is None or entry.returncode is not None:
            return
        entry.cancelled = True
        popen = entry.popen
        try:
            # Kill the whole process group so vllm child workers die too.
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            popen.terminate()
        try:
            popen.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            logger.warning(
                "process %s did not exit after SIGTERM; escalating to SIGKILL",
                popen.pid,
            )
            try:
                os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
            except OSError:
                popen.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_log(path: Path):
    """Open ``path`` for append + line-buffered writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab", buffering=0)


def _merge_env(overrides: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(overrides)
    return env


def _start_wait_thread(entry: _LocalProcess, lock: threading.Lock) -> threading.Thread:
    """Spawn a daemon thread that waits on Popen and records the returncode."""

    def _wait() -> None:
        rc = entry.popen.wait()
        with lock:
            entry.returncode = rc

    t = threading.Thread(target=_wait, daemon=True, name=f"wait-{entry.popen.pid}")
    t.start()
    return t


def _start_endpoint_writer_thread(
    entry: _LocalProcess,
    *,
    spec: VllmSpec,
    predicate: Callable[[str], bool],
    poll_interval: float,
) -> threading.Thread:
    """Watch the vLLM log for the ready marker, then write the endpoint JSON.

    We tail the log rather than hook into the process's stdout stream
    because the Popen stdout is already being redirected into the log
    file. A second reader of the same FD is fragile; a log tailer is
    not.
    """

    def _run() -> None:
        deadline = time.monotonic() + spec.boot_timeout_seconds
        last_pos = 0
        while time.monotonic() < deadline:
            if entry.returncode is not None:
                return  # Process died before ready marker.
            try:
                if spec.log_path.exists():
                    with spec.log_path.open("rb") as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if predicate(line):
                            record = EndpointRecord(
                                base_url=spec.base_url,
                                api_key=spec.api_key,
                                model=spec.served_model_name,
                                job_id=entry.handle.job_id,
                            )
                            write_endpoint_atomic(spec.endpoint_path, record)
                            logger.info(
                                "vllm ready; wrote endpoint file %s",
                                spec.endpoint_path,
                            )
                            return
            except OSError:
                logger.debug("tail of vllm log failed", exc_info=True)
            time.sleep(poll_interval)
        logger.warning(
            "vllm boot timeout (%.0fs) elapsed without ready marker",
            spec.boot_timeout_seconds,
        )

    t = threading.Thread(target=_run, daemon=True, name=f"vllm-ready-{entry.popen.pid}")
    t.start()
    return t


def random_api_key() -> str:
    """Helper for callers who need an ephemeral API key for local vLLM.

    Prefixed so the token can never start with ``-`` —
    ``token_urlsafe`` occasionally leads with a dash, and an argv like
    ``--api-key -yfi...`` makes argparse read the key as the next
    option flag ("expected one argument").
    """
    return f"bh-{secrets.token_urlsafe(24)}"


def pick_free_port(host: str = "127.0.0.1", low: int = 8000, high: int = 8099) -> int:
    """Return the first free TCP port in the range, or raise RuntimeError.

    Local-mode callers use this to avoid colliding when two harness
    runs share a laptop. SLURM jobs get the same logic inside the
    wrapper script (Phase C), not here.
    """
    import socket

    for port in range(low, high + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    msg = f"no free port in [{low}, {high}]"
    raise RuntimeError(msg)


__all__ = [
    "JobHandle",
    "JobState",
    "LocalSubprocessRunner",
    "PipelineSpec",
    "Runner",
    "VllmSpec",
    "pick_free_port",
    "random_api_key",
]


# A small shim so ``Sequence`` doesn't end up unused if future refactors drop it.
_ = Sequence
