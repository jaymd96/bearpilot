"""Anthropic ↔ OpenAI Chat Completions translation layer.

Exposes pure functions (``anthropic_request_to_openai``,
``openai_response_to_anthropic``) which later phases wrap in an HTTP server.
The shim lets bear-harness drive Mac-friendly local model runtimes like Ollama,
which speak OpenAI Chat Completions, from pipeline programs that speak
Anthropic's ``/v1/messages``.

Why the shim exists: vLLM natively speaks ``/v1/messages`` but doesn't run on
Apple Silicon. Ollama and llama.cpp run great on Apple Silicon but only speak
the OpenAI dialect. DemoPipeline's ``VLLMBatchLLMClient`` fakes the Anthropic
Message Batches API entirely in Python via per-request ``/v1/messages`` calls
(see ``data_pipeline/llm/_vllm_batch_client.py:12-18``), so the shim only needs
to implement a single endpoint — no stateful batch proxy, no
``/v1/messages/batches`` implementation.
"""

from __future__ import annotations

from typing import Any

# OpenAI chat completions use ``finish_reason``; Anthropic uses ``stop_reason``.
# The mapping covers everything vLLM / Ollama / llama.cpp actually emit.
# ``tool_calls`` maps to ``tool_use`` even though Phase 1 doesn't forward tools —
# if a later phase adds tool-call support, this pre-positioning avoids a
# separate migration.
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}

# Sampling parameters that both dialects share verbatim. Anything else the
# caller sends is dropped silently rather than forwarded blindly, because a
# forwarded unknown field will cause Ollama to 400 the whole request.
_PASSTHROUGH_KEYS = ("temperature", "top_p", "stop_sequences")


def _flatten_content_blocks(blocks: list[dict[str, Any]]) -> str:
    """Concatenate a list of text content blocks into a single string.

    Non-text blocks (image, audio) are dropped — text-only runtimes cannot
    consume them and no current pipeline program emits them. ``cache_control``
    metadata on each block evaporates: the shim cannot preserve caching
    semantics that the backend does not support, and leaking the field through
    would cause Ollama to reject the request as an unknown field.

    User-message blocks are joined with an empty separator because they are
    conceptually one continuous stream of text that happened to be chunked
    (often to attach ``cache_control`` to a prefix).
    """
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _flatten_system(system: str | list[dict[str, Any]]) -> str:
    """Normalise Anthropic ``system`` (string or list of blocks) to a string.

    String → as-is. List-of-blocks → joined with a blank line between each
    block's text, preserving the section-break semantics that DemoPipeline
    relies on when it splits a long system prompt into ``cache_control``-marked
    chunks.
    """
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")


def _normalise_message_content(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return _flatten_content_blocks(content)


def anthropic_request_to_openai(
    req: dict[str, Any],
    *,
    thinking_budget: int = 0,
) -> dict[str, Any]:
    """Convert an Anthropic ``/v1/messages`` request body to OpenAI chat shape.

    Handles:
    - ``system`` (string or list of text blocks) → prepended system-role message.
    - ``messages[].content`` (string or list of text blocks) → plain string.
    - ``cache_control`` silently stripped (Ollama 400s on unknown fields).
    - ``max_tokens`` / ``temperature`` / ``top_p`` / ``stop_sequences`` passed through.

    When ``thinking_budget > 0`` and ``think`` is enabled, ``max_tokens``
    is inflated by the budget so the model has headroom for both internal
    reasoning and the actual answer. The caller's original ``max_tokens``
    represents the *content* budget; the thinking budget is extra.

    Drops:
    - non-text content blocks (images, audio) — text-only runtimes can't use them.
    - ``tools`` / ``tool_choice`` — out of scope for Phase 1; add in a later phase
      if a pipeline program needs them.
    """
    out: dict[str, Any] = {"model": req["model"]}

    if "max_tokens" in req:
        out["max_tokens"] = req["max_tokens"] + thinking_budget

    messages: list[dict[str, Any]] = []

    if "system" in req:
        messages.append(
            {
                "role": "system",
                "content": _flatten_system(req["system"]),
            }
        )

    for msg in req.get("messages", []):
        messages.append(
            {
                "role": msg["role"],
                "content": _normalise_message_content(msg["content"]),
            }
        )

    out["messages"] = messages

    # Enable structured thinking for models that support it (Qwen3,
    # DeepSeek-R1). When ``think`` is ``True``, the model separates
    # chain-of-thought into a ``reasoning`` field and the actual answer
    # into ``content``. The response translator extracts ``content`` only,
    # giving the caller a clean answer while the model still benefits from
    # internal reasoning. Silently ignored by models that don't support it.
    out["think"] = True

    for key in _PASSTHROUGH_KEYS:
        if key in req:
            out[key] = req[key]

    return out


def openai_response_to_anthropic(resp: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI chat completion response to Anthropic ``Message`` shape.

    The output dict is deserialisable by the Anthropic SDK's ``Message`` model,
    so ``VLLMBatchLLMClient._do_one`` (which awaits
    ``async_client.messages.create``) treats it as a normal message.

    ``stop_sequence`` is always emitted as ``None`` because OpenAI's response
    format does not carry the matched stop sequence back out. ``cache_creation``
    and ``cache_read`` input tokens are always zero — the shim cannot fabricate
    caching metadata the upstream didn't produce.
    """
    choice = resp["choices"][0]
    message = choice["message"]
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")

    usage = resp.get("usage", {}) or {}

    # Prefer ``content``; fall back to ``reasoning`` for models that split
    # chain-of-thought output into a separate field (Qwen3, DeepSeek-R1 via
    # Ollama). When ``think: false`` is honoured this path is not needed, but
    # it provides a safety net for backends that ignore the flag.
    text = message.get("content") or message.get("reasoning") or ""

    return {
        "id": resp.get("id", ""),
        "type": "message",
        "role": message.get("role", "assistant"),
        "model": resp.get("model", ""),
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


__all__ = [
    "anthropic_request_to_openai",
    "openai_response_to_anthropic",
]
