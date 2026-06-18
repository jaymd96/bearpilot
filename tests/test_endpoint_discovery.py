"""Unit tests for ``bear_harness._endpoint_discovery``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bear_harness._endpoint_discovery import (
    EndpointDiscoveryError,
    EndpointProbeError,
    EndpointRecord,
    probe_endpoint,
    read_endpoint,
    wait_for_endpoint_file,
    write_endpoint_atomic,
)

# ---------------------------------------------------------------------------
# atomic write / read
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "e.json"
        record = EndpointRecord(
            base_url="http://127.0.0.1:8000/v1",
            api_key="key",
            model="m",
            job_id="42",
        )
        write_endpoint_atomic(path, record)
        assert path.is_file()
        loaded = read_endpoint(path)
        assert loaded == record

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c.json"
        write_endpoint_atomic(
            path,
            EndpointRecord(base_url="u", api_key="k", model="m", job_id="1"),
        )
        assert path.exists()

    def test_missing_fields_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"base_url": "x"}))
        with pytest.raises(EndpointDiscoveryError, match="missing fields"):
            read_endpoint(path)

    def test_unreadable_raises(self, tmp_path: Path) -> None:
        with pytest.raises(EndpointDiscoveryError, match="not found"):
            read_endpoint(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# wait loop
# ---------------------------------------------------------------------------


class TestWaitForEndpoint:
    def test_immediate_success(self, tmp_path: Path) -> None:
        path = tmp_path / "e.json"
        write_endpoint_atomic(
            path,
            EndpointRecord(base_url="u", api_key="k", model="m", job_id="j"),
        )
        rec = wait_for_endpoint_file(path, timeout_seconds=1.0)
        assert rec.model == "m"

    def test_appears_after_ticks(self, tmp_path: Path) -> None:
        path = tmp_path / "e.json"
        clock = [0.0]

        def _now() -> float:
            return clock[0]

        tick = [0]

        def _sleep(_s: float) -> None:
            tick[0] += 1
            clock[0] += 1.0
            if tick[0] == 3:
                write_endpoint_atomic(
                    path,
                    EndpointRecord(
                        base_url="u", api_key="k", model="late", job_id="j"
                    ),
                )

        rec = wait_for_endpoint_file(
            path,
            timeout_seconds=60.0,
            poll_interval_seconds=0.1,
            _sleep=_sleep,
            _now=_now,
        )
        assert rec.model == "late"

    def test_timeout(self, tmp_path: Path) -> None:
        clock = [0.0]
        ticks = [0]

        def _sleep(_s: float) -> None:
            ticks[0] += 1
            clock[0] += 10.0

        def _now() -> float:
            return clock[0]

        with pytest.raises(EndpointDiscoveryError, match="timed out"):
            wait_for_endpoint_file(
                tmp_path / "never.json",
                timeout_seconds=5.0,
                _sleep=_sleep,
                _now=_now,
            )

    def test_short_circuit_when_job_dead(self, tmp_path: Path) -> None:
        with pytest.raises(EndpointDiscoveryError, match="job exited"):
            wait_for_endpoint_file(
                tmp_path / "e.json",
                timeout_seconds=60.0,
                is_job_alive=lambda: False,
                _sleep=lambda _s: None,
                _now=lambda: 0.0,
            )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return json.dumps(self._body) if isinstance(self._body, dict | list) else str(self._body)

    def json(self) -> Any:
        return self._body


class _FakeHttpxClient:
    def __init__(self, script: dict[tuple[str, str], _FakeResponse]) -> None:
        self._script = script
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> _FakeHttpxClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        self.calls.append(("GET", url))
        return self._script[("GET", url)]

    def post(
        self, url: str, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        self.calls.append(("POST", url))
        return self._script[("POST", url)]


def _record() -> EndpointRecord:
    return EndpointRecord(
        base_url="http://h:8000/v1",
        api_key="k",
        model="m",
        job_id="1",
    )


class TestProbeEndpoint:
    def test_success(self) -> None:
        client = _FakeHttpxClient(
            {
                ("GET", "http://h:8000/v1/v1/models"): _FakeResponse(
                    200, {"data": [{"id": "m"}]}
                ),
                ("POST", "http://h:8000/v1/v1/messages"): _FakeResponse(
                    200, {"content": []}
                ),
            }
        )
        probe_endpoint(
            _record(),
            _client_factory=lambda **_kw: client,
            _sleep=lambda _s: None,
        )
        assert client.calls == [
            ("GET", "http://h:8000/v1/v1/models"),
            ("POST", "http://h:8000/v1/v1/messages"),
        ]

    def test_wrong_model_id(self) -> None:
        client = _FakeHttpxClient(
            {
                ("GET", "http://h:8000/v1/v1/models"): _FakeResponse(
                    200, {"data": [{"id": "other"}]}
                ),
            }
        )
        with pytest.raises(EndpointProbeError, match="does not list"):
            probe_endpoint(
                _record(),
                retries=1,
                _client_factory=lambda **_kw: client,
                _sleep=lambda _s: None,
            )

    def test_messages_404_suggests_upgrade(self) -> None:
        client = _FakeHttpxClient(
            {
                ("GET", "http://h:8000/v1/v1/models"): _FakeResponse(
                    200, {"data": [{"id": "m"}]}
                ),
                ("POST", "http://h:8000/v1/v1/messages"): _FakeResponse(
                    404, {"detail": "not found"}
                ),
            }
        )
        with pytest.raises(EndpointProbeError, match="upgrade"):
            probe_endpoint(
                _record(),
                retries=1,
                _client_factory=lambda **_kw: client,
                _sleep=lambda _s: None,
            )

    def test_retries_then_succeeds(self) -> None:
        attempts = [0]

        def factory(**_kw: Any) -> _FakeHttpxClient:
            attempts[0] += 1
            if attempts[0] == 1:
                return _FakeHttpxClient(
                    {
                        ("GET", "http://h:8000/v1/v1/models"): _FakeResponse(
                            500, {"error": "boot"}
                        ),
                    }
                )
            return _FakeHttpxClient(
                {
                    ("GET", "http://h:8000/v1/v1/models"): _FakeResponse(
                        200, {"data": [{"id": "m"}]}
                    ),
                    ("POST", "http://h:8000/v1/v1/messages"): _FakeResponse(
                        200, {}
                    ),
                }
            )

        probe_endpoint(
            _record(),
            retries=3,
            _client_factory=factory,
            _sleep=lambda _s: None,
        )
        assert attempts[0] == 2

    def test_network_error_wraps(self) -> None:
        class _Boom:
            def __enter__(self) -> _Boom:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def get(self, *_a: Any, **_k: Any) -> Any:
                raise httpx.ConnectError("refused")

        def factory(**_kw: Any) -> _FakeHttpxClient:
            return _Boom()  # type: ignore[return-value]

        with pytest.raises(EndpointProbeError, match="GET /v1/models failed"):
            probe_endpoint(
                _record(),
                retries=1,
                _client_factory=factory,
                _sleep=lambda _s: None,
            )
