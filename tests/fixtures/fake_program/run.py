"""Tiny pipeline program for the local integration test.

Opens an httpx client pointed at the injected vLLM endpoint, makes
``--n`` ``/v1/messages`` calls, writes one JSONL result per call to
``$OUTPUT_DIR/results.jsonl``, and publishes progress into the status
file the harness is watching.

Deliberately dependency-light: only stdlib + httpx (which bear-harness
already pulls in). Tests that want the real pipeline code run against
DemoPipeline directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--n", type=int, default=5)
    args = parser.parse_args()

    import httpx

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"
    status_path = Path(args.status)

    started_at = time.time()

    def _status(state: str, completed: int, failed: int, msg: str) -> None:
        _write_status(
            status_path,
            {
                "schema_version": 1,
                "state": state,
                "started_at": started_at,
                "updated_at": time.time(),
                "total_runs": args.n,
                "completed_runs": completed,
                "failed_runs": failed,
                "current_round": 0,
                "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
                "message": msg,
            },
        )

    _status("running", 0, 0, "starting up")

    completed = 0
    failed = 0
    with httpx.Client(timeout=60.0) as client, results_path.open("w") as f:
        for i in range(args.n):
            payload = {
                "model": args.model,
                "max_tokens": 8,
                "messages": [
                    {"role": "user", "content": f"Say ONE number. This is call {i}."},
                ],
            }
            try:
                resp = client.post(
                    f"{args.base_url.rstrip('/')}/v1/messages",
                    headers={"Authorization": f"Bearer {args.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
                f.write(json.dumps({"i": i, "ok": True, "response": body}) + "\n")
                f.flush()
                completed += 1
            except Exception as exc:
                failed += 1
                f.write(json.dumps({"i": i, "ok": False, "error": str(exc)}) + "\n")
                f.flush()
            _status("running", completed, failed, f"call {i + 1}/{args.n}")

    terminal = "completed" if failed == 0 else "failed"
    _status(terminal, completed, failed, f"done: {completed} ok, {failed} failed")

    print(f"fake-program: {completed}/{args.n} calls ok", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


# Silence unused-import warnings if the module is imported for any reason.
_ = os
