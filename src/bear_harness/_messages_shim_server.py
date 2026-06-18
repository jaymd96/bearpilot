"""Tiny HTTP server wrapping the pure Anthropic↔OpenAI translator functions.

``MessagesShim`` exposes a single endpoint — ``POST /v1/messages`` — on a
loopback port, translates each incoming Anthropic request to OpenAI Chat
Completions, forwards it through an injected ``httpx.Client`` to an upstream
that speaks the OpenAI dialect (Ollama, llama.cpp, etc.), and translates the
response back to Anthropic shape.

The upstream HTTP client is taken as a constructor parameter so tests can
swap in an ``httpx.MockTransport`` without touching the network. In production
the caller builds an ``httpx.Client(base_url="http://localhost:11434/v1")``
pointing at the locally-running Ollama.

Why stdlib ``http.server`` and not FastAPI/Starlette: the shim's whole job is
to be a one-request-at-a-time local loopback translator that starts in
milliseconds. Pulling in ASGI + an async runtime for one route that never
crosses a loopback would cost more than it saves.
"""

from __future__ import annotations

import http.server
import json
import threading
from types import TracebackType
from typing import Any

import httpx

from bear_harness._messages_shim import (
    anthropic_request_to_openai,
    openai_response_to_anthropic,
)

_MESSAGES_PATH = "/v1/messages"
_MODELS_PATH = "/v1/models"
_UPSTREAM_PATH = "/chat/completions"


class MessagesShim:
    """Loopback HTTP server translating ``/v1/messages`` to OpenAI Chat Completions.

    Lifecycle: ``start()`` binds the listener and spawns a daemon thread
    running ``serve_forever``. ``stop()`` shuts the server down cleanly.
    Both are idempotent. Also usable as a context manager.

    Port selection: pass ``port=0`` (default) to bind an ephemeral port; read
    the actual port off the ``port`` property after ``start()``. Pass a fixed
    port only when a sibling process needs to reach the shim at a known URL.

    ``thinking_budget`` inflates ``max_tokens`` sent to the upstream so
    thinking models (Qwen3, DeepSeek-R1) have headroom for chain-of-thought
    without stealing from the caller's content budget. Set to ``0`` to
    disable thinking.
    """

    def __init__(
        self,
        *,
        upstream_client: httpx.Client,
        host: str = "127.0.0.1",
        port: int = 0,
        served_model_name: str | None = None,
        thinking_budget: int = 0,
    ) -> None:
        self._upstream = upstream_client
        self._host = host
        self._requested_port = port
        self._served_model_name = served_model_name
        self._thinking_budget = thinking_budget
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The actual bound port, or ``0`` before ``start()``."""
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        handler_cls = _build_handler(
            self._upstream, self._served_model_name, self._thinking_budget
        )
        self._server = http.server.ThreadingHTTPServer(
            (self._host, self._requested_port),
            handler_cls,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="messages-shim",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._server = None
            self._thread = None

    def __enter__(self) -> MessagesShim:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def _build_handler(
    upstream: httpx.Client,
    served_model_name: str | None,
    thinking_budget: int = 0,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Close over the upstream client to keep the handler class self-contained.

    Each ``ThreadingHTTPServer`` instantiates a fresh handler per request, so
    the upstream client has to be reachable via closure (or a class attribute).
    A closure keeps the handler class local to one shim instance, which avoids
    leaking state across unrelated shims in the same process — important for
    tests that stand up multiple shims back-to-back.

    ``served_model_name`` configures the synthetic ``GET /v1/models`` route.
    When ``None`` the route 404s; when set, it returns an OpenAI-shaped list
    containing that one model. The model name has already been validated
    upstream (``OllamaBackend._ensure_model_pulled`` in the Ollama case) by
    the time the shim is reachable, so synthesising the response is both
    correct and faster than forwarding to the backend on every probe.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence default stderr logging; the shim is a background component
        # and its per-request chatter would clutter test output.
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            if self.path != _MESSAGES_PATH:
                self._send_plain(404, "not found")
                return

            raw = self._read_body()
            try:
                req_body = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                self._send_plain(400, f"invalid json: {e}")
                return

            try:
                openai_req = anthropic_request_to_openai(
                    req_body, thinking_budget=thinking_budget
                )
            except (KeyError, TypeError) as e:
                self._send_plain(400, f"invalid request shape: {e}")
                return

            try:
                upstream_resp = upstream.post(_UPSTREAM_PATH, json=openai_req)
            except httpx.HTTPError as e:
                self._send_plain(502, f"upstream error: {e}")
                return

            if upstream_resp.status_code >= 400:
                self._forward_upstream_error(upstream_resp)
                return

            try:
                anthropic_body = openai_response_to_anthropic(upstream_resp.json())
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._send_plain(502, f"malformed upstream response: {e}")
                return

            body = json.dumps(anthropic_body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == _MODELS_PATH:
                if served_model_name is None:
                    self._send_plain(404, "not found")
                    return
                self._send_models_response(served_model_name)
                return
            if self.path == _MESSAGES_PATH:
                self._send_plain(405, "method not allowed")
                return
            self._send_plain(404, "not found")

        def _send_models_response(self, model: str) -> None:
            """Return a synthetic OpenAI ``/v1/models`` list for ``probe_endpoint``."""
            body = json.dumps(
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model"}],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- helpers ----------------------------------------------------

        def _read_body(self) -> bytes:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _forward_upstream_error(self, upstream_resp: httpx.Response) -> None:
            body = upstream_resp.content
            content_type = upstream_resp.headers.get("content-type", "text/plain")
            self.send_response(upstream_resp.status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_plain(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


__all__ = ["MessagesShim"]
