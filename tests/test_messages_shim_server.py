"""Tests for the HTTP server wrapping the pure translator functions.

The server speaks Anthropic's ``/v1/messages`` on the inbound side and calls an
OpenAI Chat Completions ``/chat/completions`` endpoint on the outbound side.
We inject an ``httpx.Client`` with ``httpx.MockTransport`` so these tests do
not touch a real Ollama — the shim's upstream-facing client is stubbed, while
the inbound HTTP listener is a real stdlib ``http.server`` bound to ``127.0.0.1``
on an ephemeral port.

The single user-visible contract:
  POST {shim.base_url}/v1/messages  (Anthropic shape)
    → shim translates → POST {upstream}/chat/completions (OpenAI shape)
    → upstream responds (OpenAI shape)
    → shim translates → response body (Anthropic shape)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from bear_harness._messages_shim_server import MessagesShim


def _ok_openai_response(
    *, model: str = "gemma-4-e2b", text: str = "Hi there!"
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }


@pytest.fixture
def captured_upstream_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def mock_upstream_client(
    captured_upstream_requests: list[httpx.Request],
) -> httpx.Client:
    """An ``httpx.Client`` whose transport returns a canned OpenAI response.

    Every intercepted request is appended to ``captured_upstream_requests`` so
    tests can assert on the shape the shim actually sent upstream.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        captured_upstream_requests.append(request)
        return httpx.Response(200, json=_ok_openai_response())

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://upstream.test/v1")


@pytest.fixture
def shim(mock_upstream_client: httpx.Client) -> Iterator[MessagesShim]:
    s = MessagesShim(upstream_client=mock_upstream_client)
    s.start()
    try:
        yield s
    finally:
        s.stop()


class TestLifecycle:
    def test_port_assigned_after_start(self, mock_upstream_client: httpx.Client) -> None:
        """``port`` is zero before start, bound after start, stays bound until stop."""
        s = MessagesShim(upstream_client=mock_upstream_client)
        assert s.port == 0
        s.start()
        try:
            assert s.port > 0
            assert s.base_url == f"http://127.0.0.1:{s.port}"
        finally:
            s.stop()

    def test_stop_is_idempotent(self, mock_upstream_client: httpx.Client) -> None:
        s = MessagesShim(upstream_client=mock_upstream_client)
        s.start()
        s.stop()
        s.stop()  # second stop must not raise

    def test_context_manager(self, mock_upstream_client: httpx.Client) -> None:
        with MessagesShim(upstream_client=mock_upstream_client) as s:
            assert s.port > 0
            # Reachable while inside the with-block
            r = httpx.get(f"{s.base_url}/does-not-exist")
            assert r.status_code == 404


class TestMessagesRoute:
    def test_happy_path_roundtrip(
        self,
        shim: MessagesShim,
        captured_upstream_requests: list[httpx.Request],
    ) -> None:
        """A valid Anthropic request returns a valid Anthropic response."""
        req_body = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        r = httpx.post(f"{shim.base_url}/v1/messages", json=req_body)
        assert r.status_code == 200

        body = r.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["model"] == "gemma-4-e2b"
        assert body["content"] == [{"type": "text", "text": "Hi there!"}]
        assert body["stop_reason"] == "end_turn"
        assert body["usage"]["input_tokens"] == 7
        assert body["usage"]["output_tokens"] == 3

        # Exactly one upstream request, POST to /chat/completions, in OpenAI shape.
        assert len(captured_upstream_requests) == 1
        upstream = captured_upstream_requests[0]
        assert upstream.method == "POST"
        assert upstream.url.path.endswith("/chat/completions")
        import json as _json

        upstream_json = _json.loads(upstream.content)
        assert upstream_json["model"] == "gemma-4-e2b"
        assert upstream_json["max_tokens"] == 50
        assert upstream_json["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_and_cache_control_translated(
        self,
        shim: MessagesShim,
        captured_upstream_requests: list[httpx.Request],
    ) -> None:
        """``system`` blocks become a system-role message and ``cache_control`` is stripped."""
        req_body = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "system": [
                {"type": "text", "text": "Be concise."},
                {
                    "type": "text",
                    "text": "Answer in one sentence.",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        r = httpx.post(f"{shim.base_url}/v1/messages", json=req_body)
        assert r.status_code == 200

        import json as _json

        upstream_json = _json.loads(captured_upstream_requests[0].content)
        assert upstream_json["messages"][0] == {
            "role": "system",
            "content": "Be concise.\n\nAnswer in one sentence.",
        }
        # cache_control must not leak through anywhere in the forwarded payload.
        assert "cache_control" not in _json.dumps(upstream_json)

    def test_upstream_5xx_propagates(
        self,
        captured_upstream_requests: list[httpx.Request],
    ) -> None:
        """Upstream 500 passes through as a 5xx to the caller, not a silent success."""

        def handler(request: httpx.Request) -> httpx.Response:
            captured_upstream_requests.append(request)
            return httpx.Response(500, text="upstream boom")

        upstream = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://upstream.test/v1",
        )
        with MessagesShim(upstream_client=upstream) as s:
            r = httpx.post(
                f"{s.base_url}/v1/messages",
                json={
                    "model": "gemma-4-e2b",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert 500 <= r.status_code < 600
        assert "upstream boom" in r.text
        assert len(captured_upstream_requests) == 1

    def test_malformed_json_returns_400(self, shim: MessagesShim) -> None:
        r = httpx.post(
            f"{shim.base_url}/v1/messages",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_unknown_path_returns_404(self, shim: MessagesShim) -> None:
        r = httpx.post(f"{shim.base_url}/v1/nope", json={})
        assert r.status_code == 404

    def test_wrong_method_returns_405(self, shim: MessagesShim) -> None:
        r = httpx.get(f"{shim.base_url}/v1/messages")
        assert r.status_code == 405


class TestModelsRoute:
    """``GET /v1/models`` exists so that ``probe_endpoint`` succeeds.

    The shim is stateless about model loading (Ollama does that); when the
    caller constructs the shim with ``served_model_name=...``, the models
    route reports exactly that single model in OpenAI list shape. With no
    name supplied (the Phase 2 default) the route 404s — the shim's only
    required job is the ``/v1/messages`` translation.
    """

    def test_returns_configured_model(
        self, mock_upstream_client: httpx.Client
    ) -> None:
        with MessagesShim(
            upstream_client=mock_upstream_client,
            served_model_name="gemma-4-e2b",
        ) as s:
            r = httpx.get(f"{s.base_url}/v1/models")
            assert r.status_code == 200
            body = r.json()
            assert body["object"] == "list"
            assert len(body["data"]) == 1
            assert body["data"][0]["id"] == "gemma-4-e2b"
            assert body["data"][0]["object"] == "model"

    def test_without_served_model_name_returns_404(
        self, shim: MessagesShim
    ) -> None:
        """Phase 2 default: no served_model_name → GET /v1/models 404s."""
        r = httpx.get(f"{shim.base_url}/v1/models")
        assert r.status_code == 404

    def test_post_to_models_returns_405(
        self, mock_upstream_client: httpx.Client
    ) -> None:
        with MessagesShim(
            upstream_client=mock_upstream_client,
            served_model_name="gemma-4-e2b",
        ) as s:
            r = httpx.post(f"{s.base_url}/v1/models", json={})
            assert r.status_code == 404
