"""CLI tests for the ``--remote`` / ``fetch`` / ``ps`` surface — faked SSH.

The seam: ``BEAR_HARNESS_HOSTS`` points at a temp ``hosts.toml`` and
``BEAR_HARNESS_REMOTE_RUNS`` redirects the pointer-file dir, so no real
``~/.config`` / ``~/.cache`` is touched; ``_cli._make_remote_executor`` is
monkeypatched to a :class:`RemoteExecutor` over a recording stub, so no socket
opens. These pin the agent/human contract; the real round-trip is the
``ssh localhost`` integration test + the user-side ``bbshort`` canary.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from bear_harness import _cli
from bear_harness._remote import RemoteExecutor, RemoteRun, SshResult, write_remote_run

_HOSTS = """\
default = "bb"

[hosts.bb]
ssh_alias = "bb"
remote_rds_root = "/rds/p"
remote_inbox = "/rds/p/.bear-harness/inbox"
"""


class _RecordingSsh:
    def __init__(self, script: list[SshResult] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._script = list(script or [])

    def __call__(self, argv: Sequence[str]) -> SshResult:
        self.calls.append(tuple(argv))
        return self._script.pop(0) if self._script else SshResult(0, "", "")

    @property
    def remote_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if c and c[0] == "ssh"]


@pytest.fixture
def remote_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hosts = tmp_path / "hosts.toml"
    hosts.write_text(_HOSTS)
    monkeypatch.setenv("BEAR_HARNESS_HOSTS", str(hosts))
    monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
    return tmp_path


def _patch_executor(monkeypatch: pytest.MonkeyPatch, shell: _RecordingSsh, cm_dir: Path) -> None:
    monkeypatch.setattr(
        _cli,
        "_make_remote_executor",
        lambda host: RemoteExecutor(host=host, run_shell=shell, cm_dir=cm_dir),
    )


def _seed_pointer() -> None:
    write_remote_run(
        RemoteRun(
            run_ref="prog-1",
            host="bb",
            node="bear-ln01",
            remote_run_dir="/rds/p/runs/prog-1",
            orchestrator_pid="9",
            inbox_dir="/rds/p/inbox/prog-1",
        )
    )


def test_ps_remote_runs_squeue_me(remote_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _RecordingSsh([SshResult(0, "JOBID NAME\n123 vllm\n", "")])
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["ps", "--remote", "bb", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["host"] == "bb" and "123" in payload["squeue"]
    assert "squeue --me" in shell.remote_commands


def test_launch_remote_emits_run_ref(
    remote_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prog = tmp_path / "prog"
    prog.mkdir()
    handle = json.dumps({"job_id": "prog-9", "run_dir": "/rds/p/runs/prog-9", "state": "running"})
    shell = _RecordingSsh(
        [
            SshResult(0, "", ""),  # rsync push
            SshResult(0, "pid=5\nbear-ln02\n", ""),  # nohup start
            SshResult(0, handle, ""),  # cat orchestrator.log
        ]
    )
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["launch", "--remote", "bb", str(prog), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_ref"] == "prog-9"
    assert payload["node"] == "bear-ln02"
    # it nohup'd the kernel's detached launch on the login node
    assert any("launch . --detach --json" in c for c in shell.remote_commands)


def test_status_remote_reads_run_json_over_ssh_cat(
    remote_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pointer()
    shell = _RecordingSsh([SshResult(0, json.dumps({"job_id": "prog-1", "state": "running"}), "")])
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["status", "--remote", "bb", "prog-1"])
    assert result.exit_code == 0, result.output
    assert "prog-1" in result.output and "running" in result.output
    assert "cat /rds/p/runs/prog-1/run.json" in shell.remote_commands


def test_cancel_remote_scancels_jobs(
    remote_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pointer()
    shell = _RecordingSsh(
        [
            SshResult(0, json.dumps({"vllm_job_id": "11", "pipeline_job_id": "22"}), ""),
            SshResult(0, "", ""),  # scancel
            SshResult(0, "", ""),  # kill
        ]
    )
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["cancel", "--remote", "bb", "prog-1"])
    assert result.exit_code == 0, result.output
    assert "scancel 11 22" in shell.remote_commands


def test_fetch_remote_pulls_tarball(
    remote_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_pointer()
    shell = _RecordingSsh([SshResult(0, "", "")])  # rsync pull
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["fetch", "prog-1", "--remote", "bb", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert any(
        c[0] == "rsync" and c[-2].endswith("/rds/p/runs/prog-1/artifacts.tar.gz")
        for c in shell.calls
    )


def test_unknown_host_exits_nonzero(
    remote_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _RecordingSsh()
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["ps", "--remote", "nope", "--json"])
    assert result.exit_code == 1


def test_local_commands_unaffected_by_remote_wiring(
    remote_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # status with a local (nonexistent) target still errors cleanly, no SSH.
    shell = _RecordingSsh()
    _patch_executor(monkeypatch, shell, remote_env / "cm")
    result = CliRunner().invoke(_cli.cli, ["status", "/no/such/run/dir"])
    assert result.exit_code == 1
    assert shell.calls == []  # never reached the SSH seam
