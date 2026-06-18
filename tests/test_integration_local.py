"""End-to-end local integration test: real tiny vLLM + fake_program.

Skipped unless ``VLLM_INTEGRATION_MODEL`` is set in the environment.
CI and the manual Phase B check both set it to a very small model
(e.g. ``HuggingFaceTB/SmolLM2-135M-Instruct``) so the test runs in
under a minute on a laptop GPU or CPU fallback.

The test drives the *real* harness — no stub runner. It spawns a
real ``vllm serve``, waits for the endpoint file and the HTTP probe,
runs ``fake_program/run.py`` against the live URL, and finally
asserts that:

- the harness's ``run.json`` reached ``done``
- the status file transitioned to ``completed``
- the results.jsonl has 5 ok lines
- the artifacts tarball exists and contains results.jsonl

If any of that fails, the harness's own artifact collection runs first
and the operator gets a populated tarball + both logs, same as on the
cluster.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from bear_harness._bear_config import BearConfig, LocalConfig
from bear_harness._launch import LaunchOptions, run_launch
from bear_harness._manifest import load_manifest
from bear_harness._runner import LocalSubprocessRunner

FAKE_PROGRAM = Path(__file__).parent / "fixtures" / "fake_program"
MODEL_ENV = "VLLM_INTEGRATION_MODEL"


def _vllm_available() -> bool:
    return shutil.which("vllm") is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(MODEL_ENV, "") == "",
        reason=f"{MODEL_ENV} not set; skipping real-vllm integration test",
    ),
    pytest.mark.skipif(
        not _vllm_available(),
        reason="vllm CLI not on PATH",
    ),
]


def test_local_vllm_end_to_end(tmp_path: Path) -> None:
    model = os.environ[MODEL_ENV]

    manifest = load_manifest(FAKE_PROGRAM)
    config = BearConfig(
        mode="local",
        local=LocalConfig(
            runs_dir=tmp_path / "runs",
            endpoints_dir=tmp_path / "endpoints",
        ),
    )
    options = LaunchOptions(
        manifest=manifest,
        config=config,
        model=model,
        vllm_boot_timeout_seconds=600.0,
        status_poll_interval_seconds=2.0,
        max_model_len=1024,
    )
    runner = LocalSubprocessRunner(endpoints_dir=config.require_local().endpoints_dir)
    result = run_launch(options, runner)

    assert result.final_state == "done", result.error
    assert result.endpoint is not None
    assert result.endpoint.model == model

    # run.json reached done
    run_json = json.loads((result.run_dir / "run.json").read_text())
    assert run_json["state"] == "done"

    # status file reached completed
    status_file = result.output_dir / ".bear-harness-status.json"
    assert status_file.is_file()
    status = json.loads(status_file.read_text())
    assert status["state"] == "completed"
    assert status["completed_runs"] == 5
    assert status["failed_runs"] == 0

    # results.jsonl has 5 ok lines
    results = (result.output_dir / "results.jsonl").read_text().splitlines()
    assert len(results) == 5
    for line in results:
        entry = json.loads(line)
        assert entry["ok"] is True

    # artifacts tarball contains results.jsonl
    assert result.artifacts_tarball is not None
    with tarfile.open(result.artifacts_tarball, "r:gz") as tar:
        names = set(tar.getnames())
    assert "results.jsonl" in names
    assert ".bear-harness-status.json" in names
