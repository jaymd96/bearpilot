"""End-to-end transport check over real ``ssh localhost`` — proves the round-trip
without a cluster. Marked ``integration`` (excluded from ``hatch run test``) and
skips cleanly where passwordless ssh/rsync to localhost is not available.

This exercises the *real* ``_default_run`` exec path (subprocess ssh + rsync with
ControlMaster), which the unit tests deliberately fake. The remaining unknowns —
``nohup`` longevity, SLURM, RDS — are the user-side ``bbshort`` canary.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bear_harness._hosts import Host
from bear_harness._remote import RemoteExecutor

pytestmark = pytest.mark.integration


def _ssh_localhost_works() -> bool:
    if shutil.which("ssh") is None:
        return False
    try:
        cp = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


@pytest.fixture
def localhost_exec(tmp_path: Path) -> RemoteExecutor:
    if not _ssh_localhost_works():
        pytest.skip("passwordless `ssh localhost` not available in this environment")
    host = Host(
        name="localhost",
        ssh_alias="localhost",
        remote_rds_root=str(tmp_path),
        remote_inbox=str(tmp_path / "inbox"),
    )
    return RemoteExecutor(host=host, cm_dir=tmp_path / "cm")


def test_run_echo(localhost_exec: RemoteExecutor) -> None:
    res = localhost_exec.run(["echo", "bear-harness-ok"])
    assert res.ok, res.stderr
    assert "bear-harness-ok" in res.stdout


def test_cat_roundtrip(localhost_exec: RemoteExecutor, tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("contents-123\n")
    assert localhost_exec.cat(str(f)).strip() == "contents-123"


def test_rsync_push_pull_roundtrip(localhost_exec: RemoteExecutor, tmp_path: Path) -> None:
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("payload")
    remote = tmp_path / "remote_inbox"
    push = localhost_exec.rsync_push(src, str(remote))
    assert push.ok, push.stderr
    assert (remote / "a.txt").read_text() == "payload"

    dest = tmp_path / "pulled"
    pull = localhost_exec.rsync_pull(str(remote / "a.txt"), dest)
    assert pull.ok, pull.stderr
    assert (dest / "a.txt").read_text() == "payload"
