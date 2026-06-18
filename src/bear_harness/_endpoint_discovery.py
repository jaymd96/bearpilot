"""Atomic endpoint publishing + discovery + health probing for vLLM.

The vLLM launcher (local subprocess or SLURM sbatch script) writes its
URL + API key into a JSON file inside ``endpoints_dir``. The harness
main loop polls for that file, then probes the URL to confirm the
model is actually loaded. The split between "file exists" and "health
probe passes" matters: SLURM may allocate the job and run the wrapper
script in under a second, while model weights can take minutes to
materialise on the GPU. Only once ``/v1/models`` returns 200 AND
``/v1/messages`` accepts a trivial 1-token request do we treat the
endpoint as ready.

All file writes go through ``write_endpoint_atomic`` (write-temp +
rename) so the watcher never reads a half-flushed file.

Health checks are synchronous ``httpx`` calls — the harness is a
top-level driver, not a low-latency async pipeline, so the extra
complexity of an async client is not worth it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EndpointDiscoveryError(RuntimeError):
    """Raised when an endpoint never appears or never goes healthy."""


class EndpointProbeError(RuntimeError):
    """Raised when the endpoint file appears but the vLLM HTTP probe fails."""


@dataclass(frozen=True, slots=True)
class EndpointRecord:
    """Contents of an endpoint JSON file written by a vLLM wrapper.

    ``job_id`` is the SLURM job id or local PID of the vLLM process.
    ``model`` is the served-model-name the wrapper passed on the CLI,
    which must match the name the pipeline program will request.
    """

    base_url: str
    api_key: str
    model: str
    job_id: str

    def to_dict(self) -> dict[str, str]:
        return dict(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EndpointRecord:
        required = {"base_url", "api_key", "model", "job_id"}
        missing = required - set(d.keys())
        if missing:
            msg = f"endpoint JSON missing fields: {sorted(missing)}"
            raise EndpointDiscoveryError(msg)
        return cls(
            base_url=str(d["base_url"]),
            api_key=str(d["api_key"]),
            model=str(d["model"]),
            job_id=str(d["job_id"]),
        )


def write_endpoint_atomic(path: Path, record: EndpointRecord) -> None:
    """Atomically write an endpoint record to ``path`` (write-temp + rename).

    Creates parent directories as needed. Rename is atomic on POSIX,
    so any reader either sees the full old file, the full new file, or
    (if the file did not exist) no file at all — never a truncated
    half-flush.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record.to_dict(), f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the temp file on any failure.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def read_endpoint(path: Path) -> EndpointRecord:
    """Read an endpoint record written by :func:`write_endpoint_atomic`."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        msg = f"endpoint file not found: {path}"
        raise EndpointDiscoveryError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"endpoint file {path} is not valid JSON: {exc}"
        raise EndpointDiscoveryError(msg) from exc
    if not isinstance(data, dict):
        msg = f"endpoint file {path} must contain a JSON object"
        raise EndpointDiscoveryError(msg)
    return EndpointRecord.from_dict(data)


def wait_for_endpoint_file(
    path: Path,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 5.0,
    is_job_alive: Callable[[], bool] | None = None,
    _sleep: Callable[[float], None] | None = None,
    _now: Callable[[], float] | None = None,
) -> EndpointRecord:
    """Poll ``path`` until it exists, then parse and return the record.

    If ``is_job_alive`` is supplied it is called between polls. Any
    return value that is not truthy short-circuits the wait with a
    clear error message — this is how the harness catches the case
    where SLURM ran the wrapper, vLLM crashed, and the endpoint file
    therefore never appeared. The ``_sleep`` / ``_now`` hooks exist
    solely so tests can drive the loop without real time.
    """
    sleep = _sleep or time.sleep
    now = _now or time.monotonic
    deadline = now() + timeout_seconds
    while True:
        if path.exists():
            return read_endpoint(path)
        if is_job_alive is not None and not is_job_alive():
            msg = (
                f"job exited before endpoint file {path.name} appeared; "
                "check the vLLM log for the failure"
            )
            raise EndpointDiscoveryError(msg)
        if now() >= deadline:
            msg = (
                f"timed out after {timeout_seconds:.0f}s waiting for endpoint "
                f"file {path}"
            )
            raise EndpointDiscoveryError(msg)
        sleep(poll_interval_seconds)


def probe_endpoint(
    record: EndpointRecord,
    *,
    timeout_seconds: float = 120.0,
    retries: int = 3,
    retry_delay: float = 2.0,
    _client_factory: Callable[..., httpx.Client] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> None:
    """Health-check the vLLM server behind ``record``.

    Two probes run in sequence:

    1. ``GET /v1/models`` must return 200 and list a model whose ``id``
       matches ``record.model``. This catches the case where the
       wrapper launched vLLM with the wrong ``--served-model-name``.
    2. ``POST /v1/messages`` with ``max_tokens=1`` must return 200.
       This catches old vLLM versions predating the native Messages
       route — which otherwise look healthy on ``/v1/models`` but
       404 on ``/v1/messages``.

    Failures raise ``EndpointProbeError`` with the HTTP context. The
    caller is expected to surface this via logs and the run state file.
    """
    factory = _client_factory or (lambda **kw: httpx.Client(**kw))
    sleep = _sleep or time.sleep
    base = record.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {record.api_key}"}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with factory(timeout=timeout_seconds) as client:
                _probe_models(client, base, headers, record.model)
                _probe_messages(client, base, headers, record.model)
            logger.info("vLLM endpoint %s healthy (model=%s)", base, record.model)
            return
        except EndpointProbeError as exc:
            last_error = exc
            logger.warning(
                "vLLM probe attempt %d/%d failed: %s",
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                sleep(retry_delay)
    assert last_error is not None
    raise last_error


def _probe_models(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    expected_model: str,
) -> None:
    try:
        resp = client.get(f"{base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        msg = f"GET /v1/models failed: {exc}"
        raise EndpointProbeError(msg) from exc
    if resp.status_code != 200:
        msg = f"GET /v1/models returned {resp.status_code}: {resp.text[:200]}"
        raise EndpointProbeError(msg)
    try:
        body = resp.json()
    except ValueError as exc:
        msg = f"/v1/models returned non-JSON body: {resp.text[:200]}"
        raise EndpointProbeError(msg) from exc
    ids = [m.get("id") for m in body.get("data", [])]
    if expected_model not in ids:
        msg = (
            f"/v1/models does not list expected model {expected_model!r}; "
            f"got {ids}"
        )
        raise EndpointProbeError(msg)


def _probe_messages(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    model: str,
) -> None:
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = client.post(f"{base}/v1/messages", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        msg = f"POST /v1/messages failed: {exc}"
        raise EndpointProbeError(msg) from exc
    if resp.status_code != 200:
        msg = (
            f"POST /v1/messages returned {resp.status_code}: "
            f"{resp.text[:200]}. If this is 404, your vLLM is older "
            "than vllm-project/vllm#22627 — upgrade to a build that "
            "natively implements /v1/messages."
        )
        raise EndpointProbeError(msg)


__all__ = [
    "EndpointDiscoveryError",
    "EndpointProbeError",
    "EndpointRecord",
    "probe_endpoint",
    "read_endpoint",
    "wait_for_endpoint_file",
    "write_endpoint_atomic",
]
