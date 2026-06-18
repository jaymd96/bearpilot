"""Tests for the SSH core (``_remote.py``) — entirely faked SSH, no sockets.

A ``_RecordingSsh`` stub stands in for the ``SshRunner`` seam (mirroring
``test_slurm_runner``'s ``_RecordingShell``): it records every argv and replays
scripted results. These pin the *shapes* — the ControlMaster opts, the detached
``nohup … launch --detach --json`` start, the node-independent ``ssh cat``
reattach, and a ``cancel`` that ``scancel``s jobs + reaps on the right node and
NEVER polls a PID for liveness. The real round-trip is the ``ssh localhost``
integration test + the user-side ``bbshort`` canary.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from bear_harness._hosts import Host
from bear_harness._remote import (
    RemoteError,
    RemoteExecutor,
    RemoteRun,
    SshResult,
    _first_json_object,
    _parse_pid_and_host,
    list_remote_runs,
    read_remote_run,
    write_remote_run,
)


class _RecordingSsh:
    """Fake ``SshRunner`` that records calls and replays scripted responses."""

    def __init__(self, script: list[SshResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = list(script or [])

    def __call__(self, argv: Sequence[str]) -> SshResult:
        self.calls.append(tuple(argv))
        if self._script:
            return self._script.pop(0)
        return SshResult(0, "", "")

    @property
    def remote_commands(self) -> list[str]:
        """The last argument of every ssh call — the quoted remote command."""
        return [c[-1] for c in self.calls if c and c[0] == "ssh"]


def _host() -> Host:
    return Host(
        name="bluebear",
        ssh_alias="bluebear",
        remote_rds_root="/rds/p",
        remote_inbox="/rds/p/.bear-harness/inbox",
    )


def _exec(tmp_path: Path, shell: _RecordingSsh) -> RemoteExecutor:
    return RemoteExecutor(host=_host(), run_shell=shell, cm_dir=tmp_path / "cm")


class TestPointerFile:
    def test_round_trips(self, tmp_path: Path) -> None:
        run = RemoteRun(
            run_ref="prog-x",
            host="bluebear",
            node="bear-ln01",
            remote_run_dir="/rds/p/runs/prog-x",
            orchestrator_pid="123",
            inbox_dir="/rds/p/inbox/prog-x",
        )
        write_remote_run(run, runs_dir=tmp_path)
        assert read_remote_run("bluebear", "prog-x", runs_dir=tmp_path) == run
        assert list_remote_runs(runs_dir=tmp_path) == [run]

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RemoteError, match="no pointer file"):
            read_remote_run("bluebear", "ghost", runs_dir=tmp_path)


class TestSshAndRsyncShapes:
    def test_run_uses_controlmaster_and_quotes_one_command(self, tmp_path: Path) -> None:
        shell = _RecordingSsh()
        _exec(tmp_path, shell).run(["bear-harness", "status", "--json", "/rds/p/runs/x"])
        argv = shell.calls[0]
        assert argv[0] == "ssh"
        assert "ControlMaster=auto" in argv
        assert "bluebear" in argv
        assert argv[-1] == "bear-harness status --json /rds/p/runs/x"
        assert any(a.startswith("ControlPath=") and "cm" in a for a in argv)

    def test_cat_returns_stdout(self, tmp_path: Path) -> None:
        shell = _RecordingSsh([SshResult(0, "hello\n", "")])
        assert _exec(tmp_path, shell).cat("/rds/p/f") == "hello\n"

    def test_cat_raises_on_failure(self, tmp_path: Path) -> None:
        shell = _RecordingSsh([SshResult(1, "", "No such file")])
        with pytest.raises(RemoteError, match="could not read"):
            _exec(tmp_path, shell).cat("/rds/p/missing")

    def test_rsync_push_reuses_ssh_transport(self, tmp_path: Path) -> None:
        shell = _RecordingSsh()
        _exec(tmp_path, shell).rsync_push(tmp_path / "prog", "/rds/p/inbox/prog-x")
        argv = shell.calls[0]
        assert argv[0] == "rsync"
        assert argv[-1] == "bluebear:/rds/p/inbox/prog-x/"
        transport = argv[argv.index("-e") + 1]
        assert transport.startswith("ssh ") and "ControlMaster=auto" in transport


class TestLaunchDetached:
    def test_writes_pointer_from_the_kernel_handle(self, tmp_path: Path) -> None:
        handle = json.dumps(
            {"job_id": "prog-1", "run_dir": "/rds/p/runs/prog-1", "state": "running"}
        )
        shell = _RecordingSsh(
            [
                SshResult(0, "", ""),  # rsync push
                SshResult(0, "pid=4242\nbear-ln03\n", ""),  # nohup start
                SshResult(0, f"booting...\n{handle}\n", ""),  # cat orchestrator.log
            ]
        )
        run = _exec(tmp_path, shell).launch_detached(
            tmp_path / "prog",
            "prog-20260614",
            sleep=lambda _f: None,
            runs_dir=tmp_path / "rr",
        )
        assert (run.run_ref, run.node, run.orchestrator_pid) == ("prog-1", "bear-ln03", "4242")
        assert run.remote_run_dir == "/rds/p/runs/prog-1"
        # the start command nohups the kernel's detached launch
        start_cmd = shell.calls[1][-1]
        assert "nohup" in start_cmd
        assert "launch . --detach --json" in start_cmd
        # the pointer is persisted for reattach-by-run-ref
        assert read_remote_run("bluebear", "prog-1", runs_dir=tmp_path / "rr").node == "bear-ln03"

    def test_raises_when_no_handle_appears(self, tmp_path: Path) -> None:
        shell = _RecordingSsh(
            [
                SshResult(0, "", ""),  # rsync
                SshResult(0, "pid=1\nln\n", ""),  # start
                SshResult(0, "no json yet", ""),  # cat (then default empty)
            ]
        )
        with pytest.raises(RemoteError, match="no JSON handle"):
            _exec(tmp_path, shell).launch_detached(
                tmp_path / "prog",
                "n",
                poll_attempts=2,
                sleep=lambda _f: None,
                runs_dir=tmp_path / "rr",
            )


class TestCancel:
    def test_scancels_jobs_and_reaps_on_the_right_node(self, tmp_path: Path) -> None:
        run = RemoteRun(
            run_ref="prog-1",
            host="bluebear",
            node="bear-ln03",
            remote_run_dir="/rds/p/runs/prog-1",
            orchestrator_pid="4242",
            inbox_dir="/rds/p/inbox/prog-1",
        )
        shell = _RecordingSsh(
            [
                SshResult(0, json.dumps({"vllm_job_id": "111", "pipeline_job_id": "222"}), ""),
                SshResult(0, "", ""),  # scancel
                SshResult(0, "", ""),  # kill
            ]
        )
        _exec(tmp_path, shell).cancel(run)
        cmds = shell.remote_commands
        assert "scancel 111 222" in cmds
        assert "kill 4242" in cmds
        # the reaper targets the captured login node, not a random round-robin one
        kill_call = next(c for c in shell.calls if c[-1] == "kill 4242")
        assert "Hostname=bear-ln03" in kill_call

    def test_never_polls_pid_liveness(self, tmp_path: Path) -> None:
        run = RemoteRun(
            run_ref="prog-1",
            host="bluebear",
            node="bear-ln03",
            remote_run_dir="/rds/p/runs/prog-1",
            orchestrator_pid="4242",
            inbox_dir="/rds/p/inbox/prog-1",
        )
        shell = _RecordingSsh([SshResult(0, json.dumps({"vllm_job_id": "111"}), "")])
        _exec(tmp_path, shell).cancel(run)
        joined = " | ".join(" ".join(c) for c in shell.calls)
        assert "kill -0" not in joined  # the liveness-poll anti-pattern
        assert " ps " not in f" {joined} "


class TestHelpers:
    def test_parse_pid_and_host(self) -> None:
        assert _parse_pid_and_host("pid=99\nbear-ln01\n") == ("99", "bear-ln01")

    def test_first_json_object_skips_noise(self) -> None:
        assert _first_json_object('log line\n{"a": 1} trailing')["a"] == 1
        assert _first_json_object("no json here") is None
