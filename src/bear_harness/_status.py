"""Watch the pipeline status file and render it for operators.

The consumer program writes a JSON status file (see DemoPipeline's
``_harness_status.HarnessStatusWriter``). The harness reads it on a
timer while the program is running and renders it as a Rich table, so
the operator has live feedback without needing to tail the log.

Reading is best-effort: a missing file means "program hasn't started
writing yet", a parse error means "program crashed mid-flush — we'll
retry next tick". Neither should crash the harness.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Parsed contents of the status file at a given instant."""

    state: str
    started_at: float
    updated_at: float
    total_runs: int
    completed_runs: int
    failed_runs: int
    current_round: int
    tokens: dict[str, int]
    message: str
    raw: dict[str, Any]

    @property
    def progress_fraction(self) -> float:
        if self.total_runs <= 0:
            return 0.0
        return min(1.0, self.completed_runs / self.total_runs)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.updated_at - self.started_at)

    @property
    def is_terminal(self) -> bool:
        return self.state in {"completed", "failed"}


def read_status(path: Path) -> StatusSnapshot | None:
    """Return a snapshot, or ``None`` if the file is missing or unreadable.

    Never raises: a corrupt status file must not take down the
    harness. Callers distinguish missing/corrupt from clean by the
    ``None`` return.
    """
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return StatusSnapshot(
        state=str(data.get("state", "unknown")),
        started_at=float(data.get("started_at", 0.0)),
        updated_at=float(data.get("updated_at", 0.0)),
        total_runs=int(data.get("total_runs", 0)),
        completed_runs=int(data.get("completed_runs", 0)),
        failed_runs=int(data.get("failed_runs", 0)),
        current_round=int(data.get("current_round", 0)),
        tokens=dict(data.get("tokens", {})),
        message=str(data.get("message", "")),
        raw=data,
    )


def format_status_line(snap: StatusSnapshot) -> str:
    """Return a single-line status suitable for log streaming."""
    frac = snap.progress_fraction * 100
    return (
        f"[status] state={snap.state} progress={snap.completed_runs}/"
        f"{snap.total_runs} ({frac:.0f}%) failed={snap.failed_runs} "
        f"round={snap.current_round} "
        f"tok_in={snap.tokens.get('input', 0)} "
        f"tok_out={snap.tokens.get('output', 0)} "
        f"cache_r={snap.tokens.get('cache_read', 0)} "
        f"msg={snap.message!r}"
    )


def poll_status_forever(
    path: Path,
    *,
    interval_seconds: float = 10.0,
    should_continue: Callable[[], bool] | None = None,
    on_snapshot: Callable[[StatusSnapshot], None] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> None:
    """Poll ``path`` forever (or until ``should_continue`` returns False).

    Designed to run on a daemon thread. Swallows all exceptions from
    ``on_snapshot`` so a broken renderer never crashes the loop.
    """
    sleep = _sleep or time.sleep
    cont = should_continue or (lambda: True)
    while cont():
        snap = read_status(path)
        if snap is not None and on_snapshot is not None:
            with contextlib.suppress(Exception):
                on_snapshot(snap)
        if snap is not None and snap.is_terminal:
            return
        sleep(interval_seconds)


__all__ = [
    "StatusSnapshot",
    "format_status_line",
    "poll_status_forever",
    "read_status",
]
