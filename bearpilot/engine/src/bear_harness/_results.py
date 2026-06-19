"""Reattach to a run by its directory and resolve its artifacts.

The ``results`` verb closes the detached-deploy loop. A detached run returns
a ``run_id`` handle and leaves artifacts uncollected (``artifacts_tarball``
is ``None``). :func:`resolve_results` reads ``run.json`` off the shared
filesystem — no in-memory handle — and locates the artifacts tarball,
collecting it on demand from the program's output dir if the run never did
(the lazy-results half of W1). Everything it needs (the output dir and the
artifact patterns) is persisted in ``run.json`` by ``_launch``, so no
manifest re-load is required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bear_harness._artifacts import collect_artifacts
from bear_harness._status import StatusSnapshot, read_status


class ResultsError(RuntimeError):
    """Raised when a directory has no readable ``run.json`` to reattach to."""


@dataclass(frozen=True, slots=True)
class ResultsReport:
    """What a caller gets back from reattaching to a run by its directory."""

    run_id: str
    state: str
    run_dir: Path
    output_dir: Path
    artifacts_tarball: Path | None
    collected_now: bool
    status: StatusSnapshot | None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "run_dir": str(self.run_dir),
            "output_dir": str(self.output_dir),
            "artifacts": (
                str(self.artifacts_tarball) if self.artifacts_tarball else None
            ),
            "collected_now": self.collected_now,
            "message": self.status.message if self.status else None,
        }


def resolve_results(run_dir: Path, *, collect: bool = True) -> ResultsReport:
    """Reattach to a run by ``run_dir`` and locate/collect its artifacts.

    Reads ``run.json`` for the run's state, output dir, and artifact
    patterns. If a tarball already exists it is reported as-is (never
    re-collected over). Otherwise, when ``collect`` is true, one is built on
    demand from the output dir — the lazy path a detached deploy leaves
    behind. Raises :class:`ResultsError` if ``run.json`` is missing or
    unreadable.
    """
    run_json = run_dir / "run.json"
    if not run_json.is_file():
        msg = f"no run.json in {run_dir} — not a run directory?"
        raise ResultsError(msg)
    try:
        state = json.loads(run_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"unreadable run.json in {run_dir}: {exc}"
        raise ResultsError(msg) from exc

    output_dir = Path(state.get("output_dir") or (run_dir / "output"))
    patterns = tuple(state.get("artifact_patterns", ()))
    tarball = run_dir / "artifacts.tar.gz"

    artifacts: Path | None = None
    collected_now = False
    if tarball.exists():
        artifacts = tarball
    elif collect:
        extra = [
            p
            for p in (run_dir / "vllm.log", run_dir / "pipeline.log")
            if p.is_file()
        ]
        if output_dir.is_dir() or extra:
            artifacts = collect_artifacts(
                output_dir=output_dir,
                patterns=patterns,
                extra_files=extra,
                destination=tarball,
            )
            collected_now = True

    status = read_status(output_dir / ".bear-harness-status.json")

    return ResultsReport(
        run_id=str(state.get("job_id", run_dir.name)),
        state=str(state.get("state", "?")),
        run_dir=run_dir,
        output_dir=output_dir,
        artifacts_tarball=artifacts,
        collected_now=collected_now,
        status=status,
    )


__all__ = ["ResultsError", "ResultsReport", "resolve_results"]
