"""Tests for the read-only monitoring layer on the SSH core (``_remote.py``).

``list_jobs`` parses header-less ``squeue --me`` output into :class:`JobRow`s, and
``dashboard_snapshot`` aggregates those with the laptop's known run pointers,
degrading (never crashing) when the poll fails. All SSH is faked — no sockets —
mirroring ``test_remote``'s recording stub. The renderer (``_dashboard``) is a
pure function over the snapshot, so it is exercised here too.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from bear_harness._dashboard import render_dashboard_html
from bear_harness._hosts import Host
from bear_harness._remote import (
    DashboardSnapshot,
    JobRow,
    RemoteError,
    RemoteExecutor,
    RemoteRun,
    SshResult,
    write_remote_run,
)


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


def _exec(tmp_path: Path, *results: SshResult) -> RemoteExecutor:
    host = Host(name="bb", ssh_alias="bb", remote_rds_root="/rds/p", remote_inbox="/rds/p/inbox")
    return RemoteExecutor(host=host, run_shell=_RecordingSsh(list(results)), cm_dir=tmp_path / "cm")


_LINE = "45408821|route-diff|bbshort|RUNNING|00:04:12|00:10:00|1|bear-pg0208"


class TestListJobs:
    def test_parses_pipe_delimited_rows(self, tmp_path: Path) -> None:
        ex = _exec(tmp_path, SshResult(0, _LINE + "\n", ""))
        (job,) = ex.list_jobs()
        assert job == JobRow(
            job_id="45408821",
            name="route-diff",
            qos="bbshort",
            state="RUNNING",
            elapsed="00:04:12",
            time_limit="00:10:00",
            nodes="1",
            reason="bear-pg0208",
        )

    def test_uses_headerless_squeue_me(self, tmp_path: Path) -> None:
        ex = _exec(tmp_path, SshResult(0, "", ""))
        ex.list_jobs()
        # shlex-quoted so the remote shell can't interpret the '|' field separator.
        assert ex.run_shell.remote_commands == ["squeue --me -h -o '%i|%j|%q|%T|%M|%l|%D|%R'"]

    def test_empty_output_is_no_jobs(self, tmp_path: Path) -> None:
        assert _exec(tmp_path, SshResult(0, "\n  \n", "")).list_jobs() == ()

    def test_failure_raises(self, tmp_path: Path) -> None:
        ex = _exec(tmp_path, SshResult(1, "", "boom"))
        try:
            ex.list_jobs()
        except RemoteError as exc:
            assert "boom" in str(exc)
        else:  # pragma: no cover - the assert above must fire
            raise AssertionError("expected RemoteError")

    def test_reason_with_pipes_does_not_shift_columns(self, tmp_path: Path) -> None:
        # A nodelist/reason containing '|' must be rejoined into the last field.
        line = "9|j|bbgpu|PENDING|0:00|1:00|1|(Resources|held)"
        (job,) = _exec(tmp_path, SshResult(0, line, "")).list_jobs()
        assert job.reason == "(Resources|held)" and job.nodes == "1"


class TestDashboardSnapshot:
    def test_counts_and_known_runs(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
        write_remote_run(
            RemoteRun("prog-1", "bb", "ln1", "/rds/p/runs/prog-1", "9", "/rds/p/inbox/prog-1")
        )
        write_remote_run(  # a run on a DIFFERENT host must be filtered out
            RemoteRun("other", "elsewhere", "ln2", "/x", "1", "/y")
        )
        two = "7|a|bbshort|RUNNING|00:01|00:10:00|1|n1\n8|b|bbgpu|PENDING|0:00|2-0|1|Dependency"
        snap = _exec(tmp_path, SshResult(0, two, "")).dashboard_snapshot()
        assert (snap.running, snap.pending, snap.active) == (1, 1, 2)
        assert [r.run_ref for r in snap.runs] == ["prog-1"]
        assert snap.error == ""

    def test_degrades_when_squeue_fails(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
        snap = _exec(tmp_path, SshResult(1, "", "slurm down")).dashboard_snapshot()
        assert snap.jobs == () and snap.active == 0
        assert "slurm down" in snap.error

    def test_with_commands_includes_audit_tail(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
        ex = _exec(
            tmp_path,
            SshResult(0, "7|a|bbshort|RUNNING|0:01|0:10|1|n", ""),  # squeue
            SshResult(0, '{"verb":"deploy","detail":"x"}\n', ""),  # audit tail
        )
        snap = ex.dashboard_snapshot(with_commands=True)
        assert snap.running == 1
        assert snap.commands[0]["verb"] == "deploy"


class TestAudit:
    def test_record_command_appends_jsonl_line(self, tmp_path: Path) -> None:
        ex = _exec(tmp_path, SshResult(0, "", ""))
        entry = ex.record_command("deploy", "prog-1", ts="2026-06-18T10:00:00+00:00")
        assert entry == {
            "ts": "2026-06-18T10:00:00+00:00",
            "verb": "deploy",
            "detail": "prog-1",
            "host": "bb",
        }
        cmd = ex.run_shell.calls[-1][-1]
        assert "launchpad-audit.jsonl" in cmd and "mkdir -p" in cmd and "deploy" in cmd

    def test_read_audit_is_newest_first(self, tmp_path: Path) -> None:
        two = '{"ts":"t1","verb":"deploy","detail":"a"}\n{"ts":"t2","verb":"cancel","detail":"b"}\n'
        out = _exec(tmp_path, SshResult(0, two, "")).read_audit(10)
        assert [c["verb"] for c in out] == ["cancel", "deploy"]  # last line (newest) first

    def test_read_audit_empty_when_no_file(self, tmp_path: Path) -> None:
        assert _exec(tmp_path, SshResult(0, "", "")).read_audit() == ()

    def test_read_audit_skips_corrupt_lines(self, tmp_path: Path) -> None:
        out = _exec(tmp_path, SshResult(0, 'not json\n{"verb":"deploy"}\n', "")).read_audit()
        assert [c["verb"] for c in out] == ["deploy"]


class TestTailLogs:
    def test_tails_both_logs_off_shared_fs(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BEAR_HARNESS_REMOTE_RUNS", str(tmp_path / "rr"))
        write_remote_run(RemoteRun("prog-1", "bb", "ln1", "/rds/p/runs/prog-1", "9", "/i"))
        ex = _exec(tmp_path, SshResult(0, "VLLM up", ""), SshResult(0, "pipe done", ""))
        out = ex.tail_run_logs("prog-1")
        assert "VLLM up" in out and "pipe done" in out
        assert any("tail -n 50 /rds/p/runs/prog-1/vllm.log" in c for c in ex.run_shell.remote_commands)


class TestRenderHtml:
    def _snap(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            host="bb",
            jobs=(JobRow("7", "route-diff", "bbshort", "RUNNING", "00:04", "00:10:00", "1", "n1"),),
            runs=(RemoteRun("prog-1", "bb", "ln1", "/rds/p/runs/prog-1", "9", "/i"),),
        )

    def test_renders_self_contained_html(self) -> None:
        html = render_dashboard_html(self._snap())
        assert html.lower().startswith("<!doctype html>")
        assert "route-diff" in html and "RUNNING" in html
        assert "prog-1" in html  # known run listed
        assert "prefers-color-scheme" in html  # dark mode baked in

    def test_escapes_dynamic_text(self) -> None:
        snap = DashboardSnapshot(
            host="bb",
            jobs=(JobRow("1", "<script>x</script>", "q", "RUNNING", "", "", "1", ""),),
            runs=(),
        )
        html = render_dashboard_html(snap)
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_state_message(self) -> None:
        html = render_dashboard_html(DashboardSnapshot(host="bb", jobs=(), runs=()))
        assert "No active jobs" in html

    def test_error_surfaces_in_empty_state(self) -> None:
        html = render_dashboard_html(DashboardSnapshot(host="bb", jobs=(), runs=(), error="squeue failed"))
        assert "squeue failed" in html

    def test_renders_recent_commands_section(self) -> None:
        snap = DashboardSnapshot(
            host="bb",
            jobs=(),
            runs=(),
            commands=({"ts": "2026-06-18T10:00", "verb": "deploy", "detail": "route"},),
        )
        html = render_dashboard_html(snap)
        assert "Recent commands" in html and "deploy" in html and "route" in html
