"""Unit tests for ``bear_harness._local_ollama``.

Every shell call, daemon spawn, and port probe is routed through an
injected seam, so these tests never touch a real ``ollama`` binary.
The pattern mirrors ``test_slurm_runner.py``'s ``_RecordingShell``.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from bear_harness._local_ollama import (
    OllamaBackend,
    OllamaBootTimeoutError,
    OllamaNotInstalledError,
)
from bear_harness._slurm_runner import ShellResult


class _RecordingShell:
    """Fake ``ShellRunner`` that returns scripted results keyed by argv prefix.

    A response registered under ``("ollama", "pull")`` will match any argv
    whose first two elements are ``("ollama", "pull")`` — useful because
    the real pull call has a third positional argument (the model name)
    that tests don't want to hard-code in the lookup.
    """

    def __init__(
        self,
        responses: dict[tuple[str, ...], ShellResult] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses or {}

    def __call__(self, argv: Sequence[str]) -> ShellResult:
        argv_tuple = tuple(argv)
        self.calls.append(argv_tuple)
        for key, resp in self._responses.items():
            if argv_tuple[: len(key)] == key:
                return resp
        return ShellResult(returncode=0, stdout="", stderr="")


class _FakeDaemon:
    """Duck-typed Popen stand-in satisfying ``DaemonProcess`` protocol."""

    def __init__(self) -> None:
        self.terminated = False
        self._rc: int | None = None

    def terminate(self) -> None:
        self.terminated = True
        self._rc = 0

    def poll(self) -> int | None:
        return self._rc


class _SpawnRecorder:
    """Fake ``DaemonSpawner`` that records calls and hands out one ``_FakeDaemon``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.daemon = _FakeDaemon()

    def __call__(self, argv: Sequence[str]) -> _FakeDaemon:
        self.calls.append(tuple(argv))
        return self.daemon


def _backend(
    *,
    model: str = "llama3.2",
    ollama_installed: bool = True,
    list_output: str = "NAME\tID\nllama3.2:latest\tabc\n",
    probe_port_returns: bool | list[bool] = True,
    spawn: _SpawnRecorder | None = None,
    shell: _RecordingShell | None = None,
    daemon_boot_timeout_seconds: float = 1.0,
    daemon_poll_interval_seconds: float = 0.005,
    host: str = "127.0.0.1",
    port: int = 11434,
) -> tuple[OllamaBackend, _RecordingShell, _SpawnRecorder]:
    """Builder that keeps test bodies focused on the specific thing under test."""
    shell = shell or _RecordingShell(
        {("ollama", "list"): ShellResult(0, list_output, "")}
    )
    spawn = spawn or _SpawnRecorder()
    if isinstance(probe_port_returns, bool):
        probe_fn = lambda _h, _p: probe_port_returns  # noqa: E731
    else:
        probes = iter(probe_port_returns)

        def probe_fn(_h: str, _p: int) -> bool:
            return next(probes)

    backend = OllamaBackend(
        model=model,
        host=host,
        port=port,
        which=(lambda _cmd: "/usr/local/bin/ollama") if ollama_installed else (lambda _cmd: None),
        run_shell=shell,
        spawn_daemon=spawn,
        probe_port=probe_fn,
        daemon_boot_timeout_seconds=daemon_boot_timeout_seconds,
        daemon_poll_interval_seconds=daemon_poll_interval_seconds,
    )
    return backend, shell, spawn


# ---------------------------------------------------------------------------
# Install check
# ---------------------------------------------------------------------------


class TestInstallCheck:
    def test_raises_if_ollama_not_on_path(self) -> None:
        backend, _, _ = _backend(ollama_installed=False)
        with pytest.raises(OllamaNotInstalledError):
            backend.start()


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


class TestDaemonLifecycle:
    def test_attaches_to_existing_daemon(self) -> None:
        """If the port is already open, do not spawn a new daemon."""
        backend, _, spawn = _backend(probe_port_returns=True)
        backend.start()
        assert spawn.calls == []

    def test_spawns_daemon_if_port_closed(self) -> None:
        """Port closed → ``ollama serve`` is spawned and we wait for the port."""
        backend, _, spawn = _backend(probe_port_returns=[False, True])
        backend.start()
        assert spawn.calls == [("ollama", "serve")]

    def test_daemon_boot_timeout_raises(self) -> None:
        """If the spawned daemon never opens its port, raise ``OllamaBootTimeout``."""
        backend, _, _ = _backend(
            probe_port_returns=False,
            daemon_boot_timeout_seconds=0.05,
            daemon_poll_interval_seconds=0.01,
        )
        with pytest.raises(OllamaBootTimeoutError):
            backend.start()

    def test_stop_terminates_owned_daemon(self) -> None:
        backend, _, spawn = _backend(probe_port_returns=[False, True])
        backend.start()
        backend.stop()
        assert spawn.daemon.terminated

    def test_stop_leaves_external_daemon_alone(self) -> None:
        """A daemon we did not spawn must not be terminated by us."""
        backend, _, spawn = _backend(probe_port_returns=True)
        backend.start()
        backend.stop()
        assert not spawn.daemon.terminated

    def test_stop_is_idempotent(self) -> None:
        backend, _, _ = _backend(probe_port_returns=[False, True])
        backend.start()
        backend.stop()
        backend.stop()  # second stop must not raise

    def test_context_manager(self) -> None:
        spawn = _SpawnRecorder()
        shell = _RecordingShell(
            {("ollama", "list"): ShellResult(0, "NAME\tID\nllama3.2:latest\tabc\n", "")}
        )
        probes = iter([False, True])
        backend = OllamaBackend(
            model="llama3.2",
            which=lambda _cmd: "/usr/local/bin/ollama",
            run_shell=shell,
            spawn_daemon=spawn,
            probe_port=lambda _h, _p: next(probes),
            daemon_boot_timeout_seconds=1.0,
            daemon_poll_interval_seconds=0.005,
        )
        with backend as b:
            assert b.base_url == "http://127.0.0.1:11434/v1"
        assert spawn.daemon.terminated


# ---------------------------------------------------------------------------
# Model pull
# ---------------------------------------------------------------------------


class TestModelPull:
    def test_pulls_model_if_not_present(self) -> None:
        backend, shell, _ = _backend(
            model="llama3.2",
            list_output="NAME\tID\nother-model:latest\tdef\n",
        )
        backend.start()
        assert ("ollama", "pull", "llama3.2") in shell.calls

    def test_skips_pull_if_model_already_local(self) -> None:
        backend, shell, _ = _backend(
            model="llama3.2",
            list_output="NAME\tID\nllama3.2:latest\tabc\n",
        )
        backend.start()
        assert ("ollama", "pull", "llama3.2") not in shell.calls

    def test_bare_name_matches_latest_tag(self) -> None:
        """``llama3.2`` (no tag) matches ``llama3.2:latest`` in the list output."""
        backend, shell, _ = _backend(
            model="llama3.2",
            list_output="NAME\tID\nllama3.2:latest\tabc\n",
        )
        backend.start()
        assert ("ollama", "pull", "llama3.2") not in shell.calls

    def test_tagged_name_matches_exact(self) -> None:
        """``llama3.2:3b`` matches only the exact tag, not ``:latest``."""
        backend, shell, _ = _backend(
            model="llama3.2:3b",
            list_output="NAME\tID\nllama3.2:3b\tabc\nllama3.2:latest\tdef\n",
        )
        backend.start()
        assert ("ollama", "pull", "llama3.2:3b") not in shell.calls

    def test_tagged_name_pulls_if_only_latest_present(self) -> None:
        backend, shell, _ = _backend(
            model="llama3.2:3b",
            list_output="NAME\tID\nllama3.2:latest\tdef\n",
        )
        backend.start()
        assert ("ollama", "pull", "llama3.2:3b") in shell.calls

    def test_pull_failure_raises(self) -> None:
        shell = _RecordingShell(
            {
                ("ollama", "list"): ShellResult(0, "", ""),
                ("ollama", "pull"): ShellResult(1, "", "model not found"),
            }
        )
        backend, _, _ = _backend(model="nonexistent-model", shell=shell)
        with pytest.raises(RuntimeError, match="ollama pull"):
            backend.start()


# ---------------------------------------------------------------------------
# base_url
# ---------------------------------------------------------------------------


class TestBaseUrl:
    def test_default_base_url(self) -> None:
        backend, _, _ = _backend()
        assert backend.base_url == "http://127.0.0.1:11434/v1"

    def test_custom_host_and_port(self) -> None:
        backend, _, _ = _backend(host="10.0.0.5", port=9000)
        assert backend.base_url == "http://10.0.0.5:9000/v1"

    def test_base_url_available_before_start(self) -> None:
        """``.base_url`` is a pure derivation; callers can read it before ``start()``."""
        backend, _, _ = _backend()
        assert backend.base_url == "http://127.0.0.1:11434/v1"
