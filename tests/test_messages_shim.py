"""Unit tests for the pure translator functions in ``bear_harness._messages_shim``.

The translator converts Anthropic Messages API requests to OpenAI Chat Completions
requests (and responses back the other way). Its purpose is to let Ollama — which
speaks the OpenAI dialect — serve pipeline programs that speak the Anthropic
dialect. DemoPipeline's ``VLLMBatchLLMClient`` (``_vllm_batch_client.py:12-18``)
already fakes the Anthropic Message Batches protocol in Python by firing many
concurrent ``/v1/messages`` calls, so the shim only needs a single endpoint.

These tests cover the pure conversion logic only; the HTTP server that wraps
them lives in a sibling test file once Phase 1 is green.
"""

from __future__ import annotations

import json

from bear_harness._messages_shim import (
    anthropic_request_to_openai,
    openai_response_to_anthropic,
)


class TestAnthropicRequestToOpenAI:
    def test_simple_request_passthrough(self) -> None:
        """String content + no system should pass through almost unchanged."""
        req = {
            "model": "gemma-4-e2b",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = anthropic_request_to_openai(req)
        assert result["model"] == "gemma-4-e2b"
        assert result["max_tokens"] == 100
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["think"] is True

    def test_system_string_promoted_to_first_message(self) -> None:
        """Anthropic's top-level ``system`` string becomes a system-role message."""
        req = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "system": "You are concise.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(req)
        assert "system" not in result
        assert result["messages"] == [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "Hi"},
        ]

    def test_system_as_list_of_blocks_joined(self) -> None:
        """System-as-blocks (DemoPipeline's ``cache_control`` shape) joins into one string."""
        req = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "system": [
                {"type": "text", "text": "You are helpful."},
                {
                    "type": "text",
                    "text": "Be concise.",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(req)
        assert result["messages"][0] == {
            "role": "system",
            "content": "You are helpful.\n\nBe concise.",
        }

    def test_user_content_blocks_flattened(self) -> None:
        """List-of-text-blocks on a user message collapses to a plain string."""
        req = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First part."},
                        {"type": "text", "text": " Second part."},
                    ],
                }
            ],
        }
        result = anthropic_request_to_openai(req)
        assert result["messages"][0]["content"] == "First part. Second part."

    def test_cache_control_silently_stripped(self) -> None:
        """cache_control must not leak through — Ollama rejects unknown fields."""
        req = {
            "model": "gemma-4-e2b",
            "max_tokens": 50,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Hello",
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                }
            ],
        }
        result = anthropic_request_to_openai(req)
        assert result["messages"][0]["content"] == "Hello"
        # Belt-and-braces: no cache_control string anywhere in the output tree.
        assert "cache_control" not in json.dumps(result)


class TestOpenAIResponseToAnthropic:
    def test_simple_response_conversion(self) -> None:
        resp = {
            "id": "chatcmpl-abc123",
            "model": "gemma-4-e2b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi there!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        result = openai_response_to_anthropic(resp)
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "gemma-4-e2b"
        assert result["content"] == [{"type": "text", "text": "Hi there!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["stop_sequence"] is None
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_length_finish_reason_maps_to_max_tokens(self) -> None:
        resp = {
            "id": "chatcmpl-abc",
            "model": "gemma-4-e2b",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 100,
                "total_tokens": 110,
            },
        }
        result = openai_response_to_anthropic(resp)
        assert result["stop_reason"] == "max_tokens"

    def test_upstream_id_is_preserved(self) -> None:
        """The upstream response id passes through so caller-side logging stays coherent."""
        resp = {
            "id": "chatcmpl-unique-42",
            "model": "gemma-4-e2b",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "x"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        result = openai_response_to_anthropic(resp)
        assert result["id"] == "chatcmpl-unique-42"

    def test_reasoning_fallback_when_content_empty(self) -> None:
        """Qwen3/DeepSeek-R1 put CoT in ``reasoning``; use it if ``content`` is empty."""
        resp = {
            "id": "chatcmpl-think",
            "model": "qwen3:1.7b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": "The answer is 42.",
                    },
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        }
        result = openai_response_to_anthropic(resp)
        assert result["content"] == [{"type": "text", "text": "The answer is 42."}]

    def test_content_preferred_over_reasoning(self) -> None:
        """When both ``content`` and ``reasoning`` are present, ``content`` wins."""
        resp = {
            "id": "chatcmpl-both",
            "model": "qwen3:1.7b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello!",
                        "reasoning": "thinking...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        result = openai_response_to_anthropic(resp)
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
