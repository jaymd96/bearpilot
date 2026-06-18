"""Follow a run live: one composed line per CHANGE, stop on terminal.

Python port of the shell prototype proven on BlueBEAR (stream-status.sh,
Phase H9): each poll composes one line from three node-agnostic sources —

- ``run.json`` (harness-level state machine, written by ``run_launch``),
- the program status file (``output/.bear-harness-status.json``, the
  per-LM-call heartbeat), and
- ``squeue`` for the user's queue.

The composed line is the dedup unit: emit only when it differs from the
previous poll. Everything in it must therefore be *state*, never
render-time arithmetic — squeue's elapsed column (``%M``) is excluded
by construction because it changes every poll and turns the stream into
a metronome (the prototype shipped that bug).

All reads are best-effort: a missing run.json reads as state ``?``, a
missing/corrupt status file contributes nothing, a missing ``squeue``
binary (local mode) contributes an empty queue. The loop only exits on
a terminal harness state, so it works from any login node — the truth
lives on the shared filesystem, not in process liveness.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bear_harness._status import format_status_line, read_status

#: run.json states after which nothing further will happen.
TERMINAL_RUN_STATES = frozenset({"done", "failed", "cancelled", "dry_run"})

#: Job name + state only. Deliberately NO elapsed/remaining time fields
#: (%M / %L) — they change every poll and defeat change-deduplication.
DEFAULT_SQUEUE_FORMAT = "%j=%T"


@dataclass(frozen=True, slots=True)
class FollowTick:
    """One poll's composed view — ``line`` is the dedup key."""

    line: str
    run_state: str


def read_run_state(run_dir: Path) -> str:
    """Return run.json's ``state``, or ``"?"`` if missing/corrupt."""
    try:
        data = json.loads((run_dir / "run.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "?"
    if not isinstance(data, dict):
        return "?"
    return str(data.get("state", "?"))


def default_squeue() -> str:
    """The user's queue as ``name=STATE`` pairs; ``""`` on any failure."""
    user = os.environ.get("USER", "")
    try:
        cp = subprocess.run(
            ["squeue", "-h", "-u", user, "-o", DEFAULT_SQUEUE_FORMAT],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(cp.stdout.split())


def compose_tick(
    run_dir: Path,
    *,
    run_squeue: Callable[[], str] = default_squeue,
) -> FollowTick:
    """Compose one poll's line from run.json + squeue + status file."""
    state = read_run_state(run_dir)
    parts = [f"harness={state}"]
    queue = run_squeue()
    parts.append(queue if queue else "-")
    snap = read_status(run_dir / "output" / ".bear-harness-status.json")
    if snap is not None:
        parts.append(format_status_line(snap))
    return FollowTick(line=" | ".join(parts), run_state=state)


def follow_run(
    run_dir: Path,
    *,
    emit: Callable[[str], None],
    interval_seconds: float = 10.0,
    run_squeue: Callable[[], str] = default_squeue,
    _sleep: Callable[[float], None] | None = None,
) -> str:
    """Poll until run.json reaches a terminal state; return that state.

    ``emit`` receives each composed line exactly once per change.
    """
    sleep = _sleep or time.sleep
    last: str | None = None
    while True:
        tick = compose_tick(run_dir, run_squeue=run_squeue)
        if tick.line != last:
            emit(tick.line)
            last = tick.line
        if tick.run_state in TERMINAL_RUN_STATES:
            return tick.run_state
        sleep(interval_seconds)


__all__ = [
    "DEFAULT_SQUEUE_FORMAT",
    "TERMINAL_RUN_STATES",
    "FollowTick",
    "compose_tick",
    "default_squeue",
    "follow_run",
    "read_run_state",
]
