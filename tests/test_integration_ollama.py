"""End-to-end local integration test: real Ollama + MessagesShim + fake_program.

Skipped unless ``OLLAMA_INTEGRATION_MODEL`` is set in the environment AND
the ``ollama`` binary is on PATH. Set it to a small model tag like
``llama3.2`` or ``qwen3:0.6b`` to keep the test fast.

The test drives the *real* harness — real ``OllamaBackend`` (starts or
attaches to a real Ollama daemon, pulls the model if absent), real
``MessagesShim`` (loopback Anthropic→OpenAI translator), real
``LocalOllamaRunner`` composing them, and the same ``fake_program``
fixture used by the vLLM integration test.

Assertions mirror ``test_integration_local.py``:

- ``run.json`` reached ``done``
- status file transitioned to ``completed``
- ``results.jsonl`` has N ``ok`` lines
- artifacts tarball exists and contains the expected files

If any assertion fails, the harness's own artifact collection still
runs — the operator gets a populated tarball + both logs for
post-mortem.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest

from bear_harness._bear_config import BearConfig, LocalConfig, OllamaConfig
from bear_harness._launch import LaunchOptions, run_launch
from bear_harness._local_ollama import OllamaBackend
from bear_harness._local_ollama_runner import LocalOllamaRunner
from bear_harness._manifest import load_manifest
from bear_harness._messages_shim_server import MessagesShim
from bear_harness._runner import LocalSubprocessRunner

FAKE_PROGRAM = Path(__file__).parent / "fixtures" / "fake_program"
MODEL_ENV = "OLLAMA_INTEGRATION_MODEL"


def _ollama_available() -> bool:
    return shutil.which("ollama") is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(MODEL_ENV, "") == "",
        reason=f"{MODEL_ENV} not set; skipping real-ollama integration test",
    ),
    pytest.mark.skipif(
        not _ollama_available(),
        reason="ollama CLI not on PATH",
    ),
]


def test_local_ollama_end_to_end(tmp_path: Path) -> None:
    """Full stack: Ollama daemon → MessagesShim → fake_program via run_launch."""
    model = os.environ[MODEL_ENV]

    manifest = load_manifest(FAKE_PROGRAM)
    ollama_cfg = OllamaConfig(model=model)
    config = BearConfig(
        mode="local",
        local=LocalConfig(
            runs_dir=tmp_path / "runs",
            endpoints_dir=tmp_path / "endpoints",
            backend="ollama",
            ollama=ollama_cfg,
        ),
    )
    options = LaunchOptions(
        manifest=manifest,
        config=config,
        model=model,
        vllm_boot_timeout_seconds=300.0,
        status_poll_interval_seconds=1.0,
    )

    # Wire the real Ollama stack — same wiring as _cli._build_ollama_runner
    ollama = OllamaBackend(
        model=ollama_cfg.model,
        host=ollama_cfg.host,
        port=ollama_cfg.port,
    )
    upstream = httpx.Client(base_url=ollama.base_url)
    shim = MessagesShim(
        upstream_client=upstream,
        served_model_name=model,
    )
    inner = LocalSubprocessRunner(
        endpoints_dir=config.require_local().endpoints_dir,
    )
    runner = LocalOllamaRunner(
        endpoints_dir=config.require_local().endpoints_dir,
        ollama=ollama,
        shim=shim,
        pipeline_runner=inner,
    )

    try:
        result = run_launch(options, runner)
    finally:
        # Ensure cleanup even if run_launch raises
        with contextlib.suppress(Exception):
            shim.stop()
        with contextlib.suppress(Exception):
            ollama.stop()
        upstream.close()

    # -- Assertions (mirror test_integration_local.py) ---------------------

    assert result.final_state == "done", (
        f"expected done, got {result.final_state}: {result.error}"
    )
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
        assert entry["ok"] is True, f"call {entry.get('i')} failed: {entry.get('error')}"

    # artifacts tarball contains results.jsonl
    assert result.artifacts_tarball is not None
    with tarfile.open(result.artifacts_tarball, "r:gz") as tar:
        names = set(tar.getnames())
    assert "results.jsonl" in names
    assert ".bear-harness-status.json" in names
