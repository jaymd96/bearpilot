"""Ollama process manager for Mac local mode.

Wraps the ``ollama`` CLI as a bear-harness-facing backend so the
harness's Mac-local path can serve an OpenAI-compatible model runtime
without linking SLURM or vLLM. Phase 4 introduces a ``LocalBackend``
protocol under which this class and the existing vLLM-local path become
interchangeable siblings; Phase 3 (this file) delivers the Ollama half
of that pair as a standalone unit so it can be exercised independently.

Responsibilities:

- Verify the ``ollama`` binary is on PATH (fail fast with a helpful
  message — the first-run failure mode is "brew install ollama").
- Detect whether an existing ``ollama`` daemon is already reachable on
  ``:11434`` and attach to it if so. Spawning a second ``ollama serve``
  while one is already bound to the port would fail; attaching is the
  right default for developers who already run Ollama outside of
  bear-harness.
- Otherwise spawn ``ollama serve`` in a detached session and poll the
  port until it opens. Record that we own the daemon so ``stop()`` can
  terminate it on the way out — and, crucially, leave externally-owned
  daemons alone.
- Pull the requested model with ``ollama pull <model>`` unless
  ``ollama list`` already shows it, keyed by exact name match or an
  implicit ``:latest`` tag for bare names.
- Expose ``.base_url`` pointing at the OpenAI-compatible endpoint so the
  messages-shim server can point its upstream ``httpx.Client`` at it.

All side-effectful operations route through three injected seams —
``which``, ``run_shell``, and ``spawn_daemon`` — plus a ``probe_port``
callback for the port-up probe. Tests stub these; production uses
``shutil.which`` / ``subprocess.run`` / ``subprocess.Popen`` /
``socket.create_connection`` respectively.

The import of ``ShellRunner``/``ShellResult``/``_default_run`` from
``_slurm_runner`` is a temporary Phase 3 expedient — Phase 4 will
promote those three names to a shared ``_shell.py`` once there is a
second consumer beyond the SLURM runner.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from bear_harness._slurm_runner import ShellRunner, _default_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception types
# ---------------------------------------------------------------------------


class OllamaNotInstalledError(RuntimeError):
    """Raised when the ``ollama`` binary is not on PATH."""


class OllamaBootTimeoutError(RuntimeError):
    """Raised when a spawned ``ollama serve`` never opens its port."""


# ---------------------------------------------------------------------------
# Seams (callables injected for testability)
# ---------------------------------------------------------------------------


class DaemonProcess(Protocol):
    """Structural subset of ``subprocess.Popen`` that ``OllamaBackend`` relies on."""

    def terminate(self) -> None: ...
    def poll(self) -> int | None: ...


DaemonSpawner = Callable[[Sequence[str]], DaemonProcess]
PortProbe = Callable[[str, int], bool]
Which = Callable[[str], str | None]


def _default_which(cmd: str) -> str | None:
    """``shutil.which`` pinned to a single positional argument.

    Wrapped so the default matches the ``Which`` type alias exactly —
    ``shutil.which`` itself has extra keyword arguments that confuse
    strict type-checkers when used as a drop-in default.
    """
    return shutil.which(cmd)


def _default_spawn_daemon(argv: Sequence[str]) -> DaemonProcess:
    """Spawn ``argv`` as a detached daemon and return the ``Popen`` object.

    ``start_new_session=True`` puts the child in its own process group so
    SIGINT on the harness parent does not cascade into the Ollama daemon
    — we want explicit ``stop()`` to be the only path that kills it.
    Stdout/stderr are routed to ``/dev/null``: the harness does not try
    to capture Ollama server logs today (Phase 5's integration test will
    reconsider this).
    """
    logger.debug("spawn ollama daemon: %s", list(argv))
    return subprocess.Popen(
        list(argv),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _default_probe_port(host: str, port: int) -> bool:
    """Return ``True`` iff a TCP connect to ``(host, port)`` succeeds quickly.

    Used both to detect a pre-existing daemon (one-shot at ``start()``)
    and to poll a freshly-spawned one until it is listening. A short
    timeout keeps the polling loop responsive.
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# OllamaBackend
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OllamaBackend:
    """Manages a local Ollama daemon + model pull for Mac local mode.

    Lifecycle::

        start()  → verify install → detect-or-spawn daemon → pull model
        stop()   → terminate the daemon iff this instance spawned it

    Also usable as a context manager. The ``base_url`` property is a
    pure derivation of ``host``/``port`` and may be read before
    ``start()`` — useful for wiring the shim's upstream ``httpx.Client``
    before the daemon is actually up.
    """

    model: str
    host: str = "127.0.0.1"
    port: int = 11434
    which: Which = field(default=_default_which)
    run_shell: ShellRunner = field(default=_default_run)
    spawn_daemon: DaemonSpawner = field(default=_default_spawn_daemon)
    probe_port: PortProbe = field(default=_default_probe_port)
    daemon_boot_timeout_seconds: float = 30.0
    daemon_poll_interval_seconds: float = 0.2

    _owned_daemon: DaemonProcess | None = field(default=None, init=False)

    @property
    def base_url(self) -> str:
        """OpenAI-compatible base URL the shim points its upstream at."""
        return f"http://{self.host}:{self.port}/v1"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Ensure the daemon is running and the model is pulled."""
        self._check_installed()
        self._ensure_daemon_running()
        self._ensure_model_pulled()

    def stop(self) -> None:
        """Terminate the daemon iff ``start()`` spawned it. Idempotent."""
        if self._owned_daemon is None:
            return
        try:
            self._owned_daemon.terminate()
        finally:
            self._owned_daemon = None

    def __enter__(self) -> OllamaBackend:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_installed(self) -> None:
        if self.which("ollama") is None:
            msg = (
                "ollama binary not found on PATH. Install from https://ollama.com "
                "or 'brew install ollama'."
            )
            raise OllamaNotInstalledError(msg)

    def _ensure_daemon_running(self) -> None:
        if self.probe_port(self.host, self.port):
            logger.debug(
                "attaching to existing ollama daemon at %s:%d", self.host, self.port
            )
            return
        logger.info("spawning ollama daemon (ollama serve)")
        self._owned_daemon = self.spawn_daemon(("ollama", "serve"))
        self._wait_for_daemon_ready()

    def _wait_for_daemon_ready(self) -> None:
        deadline = time.monotonic() + self.daemon_boot_timeout_seconds
        while time.monotonic() < deadline:
            if self.probe_port(self.host, self.port):
                return
            time.sleep(self.daemon_poll_interval_seconds)
        msg = (
            f"ollama daemon did not become reachable at {self.host}:{self.port} "
            f"within {self.daemon_boot_timeout_seconds:.0f}s"
        )
        raise OllamaBootTimeoutError(msg)

    def _ensure_model_pulled(self) -> None:
        if self._model_already_local():
            logger.debug("model %s already present in ollama", self.model)
            return
        logger.info("pulling ollama model %s", self.model)
        result = self.run_shell(("ollama", "pull", self.model))
        if result.returncode != 0:
            msg = (
                f"ollama pull {self.model} failed rc={result.returncode} "
                f"stderr={result.stderr.strip()!r}"
            )
            raise RuntimeError(msg)

    def _model_already_local(self) -> bool:
        """Return True iff ``ollama list`` shows ``self.model``.

        ``ollama list`` prints a tab-separated table with a ``NAME\tID\t...``
        header row. A bare name like ``llama3.2`` is considered present if
        the list contains either ``llama3.2`` or ``llama3.2:latest``
        (Ollama's default tag). A tagged name like ``llama3.2:3b`` must
        match literally — ``:latest`` does not substitute.
        """
        result = self.run_shell(("ollama", "list"))
        if result.returncode != 0:
            logger.warning(
                "ollama list rc=%d stderr=%s; assuming model absent",
                result.returncode,
                result.stderr.strip(),
            )
            return False

        want = self.model
        want_default = want if ":" in want else f"{want}:latest"

        # Skip the header row (first line).
        for line in result.stdout.splitlines()[1:]:
            first_col = line.split("\t", maxsplit=1)[0].strip()
            if not first_col:
                continue
            if first_col in (want, want_default):
                return True
        return False


__all__ = [
    "DaemonProcess",
    "DaemonSpawner",
    "OllamaBackend",
    "OllamaBootTimeoutError",
    "OllamaNotInstalledError",
    "PortProbe",
    "Which",
]
