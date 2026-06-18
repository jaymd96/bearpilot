"""``bear_harness._status_follow`` — one line per change, stop on terminal.

Ported from the shell prototype proven on BlueBEAR (stream-status.sh,
2026-06-11): poll run.json + the program status file + squeue, emit a
composed line ONLY when it differs from the previous one, and exit when
the harness run reaches a terminal state. The squeue portion must not
contain elapsed-time fields — the prototype's first version included
``%M`` and emitted a "change" every poll.
"""

from __future__ import annotations

import json
from pathlib import Path

from bear_harness._status_follow import (
    DEFAULT_SQUEUE_FORMAT,
    TERMINAL_RUN_STATES,
    default_squeue,
    follow_run,
)


def _write_run_json(run_dir: Path, state: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"state": state}))


def _write_status(run_dir: Path, **fields: object) -> None:
    out = run_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    base: dict[str, object] = {
        "state": "running",
        "total_runs": 1,
        "completed_runs": 0,
        "failed_runs": 0,
        "current_round": 0,
        "tokens": {"input": 0, "output": 0},
        "message": "",
    }
    base.update(fields)
    (out / ".bear-harness-status.json").write_text(json.dumps(base))


class _Script:
    """Drives follow_run deterministically: mutate state per sleep call."""

    def __init__(self, run_dir: Path, steps: list) -> None:
        self.run_dir = run_dir
        self.steps = list(steps)
        self.sleeps = 0

    def sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        if self.steps:
            step = self.steps.pop(0)
            step(self.run_dir)


def test_unchanged_state_emits_once(tmp_path: Path) -> None:
    _write_run_json(tmp_path, "running")
    _write_status(tmp_path)
    emitted: list[str] = []
    script = _Script(
        tmp_path,
        steps=[
            lambda d: None,  # poll 2 sees identical state
            lambda d: _write_run_json(d, "done"),  # poll 3 terminal
        ],
    )

    final = follow_run(
        tmp_path,
        emit=emitted.append,
        run_squeue=lambda: "vllm-x=RUNNING",
        _sleep=script.sleep,
    )

    assert final == "done"
    # poll1 (running) + poll3 (done) — poll2 was identical, no line.
    assert len(emitted) == 2
    assert "harness=running" in emitted[0]
    assert "harness=done" in emitted[1]


def test_program_status_change_emits_new_line(tmp_path: Path) -> None:
    _write_run_json(tmp_path, "running")
    _write_status(tmp_path, tokens={"input": 10, "output": 2})
    emitted: list[str] = []
    script = _Script(
        tmp_path,
        steps=[
            lambda d: _write_status(
                d, tokens={"input": 99, "output": 41}, current_round=2
            ),
            lambda d: _write_run_json(d, "done"),
        ],
    )

    follow_run(
        tmp_path,
        emit=emitted.append,
        run_squeue=lambda: "",
        _sleep=script.sleep,
    )

    assert len(emitted) == 3
    assert "tok_in=10" in emitted[0]
    assert "tok_in=99" in emitted[1]
    assert "round=2" in emitted[1]


def test_missing_run_json_reports_unknown_then_recovers(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    emitted: list[str] = []
    script = _Script(
        tmp_path,
        steps=[lambda d: _write_run_json(d, "failed")],
    )

    final = follow_run(
        tmp_path,
        emit=emitted.append,
        run_squeue=lambda: "",
        _sleep=script.sleep,
    )

    assert final == "failed"
    assert "harness=?" in emitted[0]
    assert "harness=failed" in emitted[-1]


def test_terminal_states_cover_failure_modes() -> None:
    assert {"done", "failed", "cancelled", "dry_run"} <= TERMINAL_RUN_STATES


def test_squeue_format_excludes_elapsed_time_fields() -> None:
    # %M (elapsed) changes every poll and defeats change-dedup — the
    # exact bug the shell prototype shipped with.
    assert "%j" in DEFAULT_SQUEUE_FORMAT
    assert "%T" in DEFAULT_SQUEUE_FORMAT
    assert "%M" not in DEFAULT_SQUEUE_FORMAT
    assert "%L" not in DEFAULT_SQUEUE_FORMAT


def test_default_squeue_swallows_missing_binary(monkeypatch) -> None:
    import subprocess

    def _boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("squeue not on PATH")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert default_squeue() == ""


def test_cli_follow_exits_zero_on_done_run(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bear_harness._cli import cli

    _write_run_json(tmp_path, "done")
    _write_status(tmp_path, state="completed", completed_runs=1)

    result = CliRunner().invoke(
        cli, ["status", str(tmp_path), "--follow", "--interval", "0.01"]
    )

    assert result.exit_code == 0, result.output
    assert "harness=done" in result.output


def test_cli_follow_exits_nonzero_on_failed_run(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bear_harness._cli import cli

    _write_run_json(tmp_path, "failed")

    result = CliRunner().invoke(
        cli, ["status", str(tmp_path), "--follow", "--interval", "0.01"]
    )

    assert result.exit_code == 1
    assert "harness=failed" in result.output
