"""Tests for the lazy ``results`` reattach logic (W1 PR 4).

``resolve_results`` takes a run directory -- the detached-deploy handle --
reads ``run.json`` off the shared FS (no in-memory handle), and locates or
lazily collects the artifacts tarball, so a fresh process can recover a run
from its ``run_id`` alone.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from bear_harness._results import ResultsError, resolve_results


def _write_run(
    run_dir: Path,
    *,
    state: str = "done",
    collect: tuple[str, ...] = ("output.txt",),
) -> Path:
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "output.txt").write_text("the result\n")
    (run_dir / "pipeline.log").write_text("fake pipeline log\n")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": run_dir.name,
                "state": state,
                "manifest_path": "/nonexistent/pipeline.toml",
                "model": "m",
                "output_dir": str(output_dir),
                "artifact_patterns": list(collect),
                "vllm_job_id": "v1",
                "pipeline_job_id": "p1",
                "base_url": "http://x:8000",
            }
        )
    )
    return run_dir


def test_collects_lazily_when_no_tarball(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "job-1")
    report = resolve_results(run_dir, collect=True)
    assert report.run_id == "job-1"
    assert report.state == "done"
    assert report.collected_now is True
    assert report.artifacts_tarball is not None
    assert report.artifacts_tarball.exists()
    with tarfile.open(report.artifacts_tarball) as tar:
        names = tar.getnames()
    assert "output.txt" in names
    assert any(n.endswith("pipeline.log") for n in names)


def test_locates_existing_tarball_without_recollecting(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "job-2")
    existing = run_dir / "artifacts.tar.gz"
    existing.write_bytes(b"pretend tarball")
    report = resolve_results(run_dir, collect=True)
    assert report.artifacts_tarball == existing
    assert report.collected_now is False
    assert existing.read_bytes() == b"pretend tarball"  # untouched


def test_no_collect_leaves_tarball_absent(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "job-3")
    report = resolve_results(run_dir, collect=False)
    assert report.artifacts_tarball is None
    assert report.collected_now is False


def test_missing_run_json_raises(tmp_path: Path) -> None:
    empty = tmp_path / "nope"
    empty.mkdir()
    with pytest.raises(ResultsError):
        resolve_results(empty, collect=True)


def test_report_as_dict_round_trips(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path / "job-4")
    report = resolve_results(run_dir, collect=True)
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["run_id"] == "job-4"
    assert payload["state"] == "done"
    assert payload["artifacts"].endswith("artifacts.tar.gz")
    assert payload["collected_now"] is True
