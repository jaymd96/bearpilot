# Anthropic Messages API + Python SDK (`messages.create`, `base_url`, `default_headers`) — API reference crib

<!-- SNAPSHOT-WITH-PROVENANCE. One file = one external surface the project drives.
     Allowed to age -- but it carries its source so staleness is detectable and refresh is
     mechanical. Provenance-at-top + refresh-contract-at-bottom around a distilled body. -->

> **Canonical source:** <https://docs.anthropic.com/en/api/messages> (Messages API) and <https://github.com/anthropics/anthropic-sdk-python> (Python SDK: `base_url`, `default_headers`, `messages.create`).
> **Version / pin:** `anthropic` Python SDK 0.x (Messages API stable). The wire shape pinned here is the Messages **request/response** contract — request `{model, max_tokens, messages, system?}`, response `{role, content[], stop_reason, usage{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}}`.
> **Why this crib:** bear-harness's pipeline programs talk the Anthropic Messages dialect, but the server is **vLLM**, not api.anthropic.com. `src/bear_harness/_messages_shim.py` translates Anthropic⇄OpenAI and `src/bear_harness/_endpoint_discovery.py` points the SDK at vLLM. This crib exists to pin the exact SDK knobs — `base_url` (the vLLM root) and `default_headers` (so we can send **both** auth dialects) — and the `usage` token field names that the shim must emit so an SDK `Message` deserialises cleanly.

---
## What it is

The Anthropic Messages API is the request/response contract bear-harness's workloads are written against (e.g. DemoPipeline's `VLLMBatchLLMClient` fakes the Anthropic Message Batches API via per-request `/v1/messages` calls). bear-harness redirects that SDK traffic at a vLLM server by setting the client's `base_url` to the vLLM root and supplying `default_headers` carrying the auth. Because vLLM and Anthropic disagree on the auth header (Bearer vs `x-api-key`), bear-harness sends **both dialects** so the same client works against either backend.

---
## Surface that matters  (endpoints / SDK knobs / shapes)

### Client construction (pointing the SDK at vLLM)

```python
from anthropic import Anthropic   # (or AsyncAnthropic)

client = Anthropic(
    base_url="http://<gpu-host>:<port>",   # vLLM ROOT — no /v1; SDK appends /v1/messages
    api_key="<vllm-api-key>",              # SDK sends this as x-api-key by default
    default_headers={                       # add the dialect vLLM actually reads:
        "Authorization": f"Bearer {api_key}",
    },
)
```

### The call

```python
msg = client.messages.create(
    model="<served-model-name>",   # must match vLLM --served-model-name (see vllm-serve-api.md)
    max_tokens=1024,
    system="optional system string OR list of content blocks",
    messages=[{"role": "user", "content": "..."}],
)
```

### Response shape the shim must produce (so an SDK `Message` deserialises)

```jsonc
{
  "id": "msg_...",
  "type": "message",
  "role": "assistant",
  "model": "<served-model-name>",
  "content": [{"type": "text", "text": "..."}],
  "stop_reason": "end_turn",          // mapped from OpenAI finish_reason
  "usage": {
    "input_tokens":  <int>,           // from OpenAI usage.prompt_tokens
    "output_tokens": <int>,           // from OpenAI usage.completion_tokens
    "cache_creation_input_tokens": 0, // shim emits 0 (vLLM has no Anthropic cache accounting)
    "cache_read_input_tokens":     0
  }
}
```

---
## Gotchas / quirks / version traps

- **`base_url` is the vLLM ROOT, no `/v1`.** The SDK appends `/v1/messages` itself. Setting `base_url="http://host:port/v1"` produces `/v1/v1/messages` → 404. Same invariant as [`vllm-serve-api.md`](vllm-serve-api.md).
- **Send BOTH auth dialects.** The Anthropic SDK's *native* auth header is `x-api-key`; vLLM's `--api-key` middleware reads `Authorization: Bearer`. Set `api_key=` (→ `x-api-key`) **and** `default_headers={"Authorization": f"Bearer {key}"}`. Sending only `x-api-key` at vLLM → 401 → silent per-act failure (the `ZeroSuccessfulCallsError` pattern). This is the same bug catalogued in [`vllm-serve-api.md`](vllm-serve-api.md), seen from the client side.
- **`stop_reason` vs `finish_reason`.** OpenAI chat returns `finish_reason`; Anthropic returns `stop_reason`. The shim maps `stop`/`length`/… → `end_turn`/`max_tokens`/…. A missing/unmapped value will make downstream `stop_reason` checks misbehave.
- **`usage` token field names differ from OpenAI.** Anthropic uses `input_tokens`/`output_tokens`; OpenAI uses `prompt_tokens`/`completion_tokens`. The shim renames them. `cache_creation_input_tokens`/`cache_read_input_tokens` are Anthropic-only and the shim emits `0` (vLLM has no equivalent accounting) — do not assume cache savings exist when reading these off a vLLM-backed run.
- **`system` can be a string OR a list of content blocks.** The shim normalises both forms to a single string before forwarding to OpenAI. Don't assume `system` is always a `str`.
- **There is also a loopback shim path for non-OpenAI backends.** `MessagesShim` (the HTTP server form) exposes `POST /v1/messages` on a local port for backends like Ollama that don't speak `/v1/messages` natively; vLLM does, so against vLLM the SDK talks to it directly. Pick the right path per backend.

---
## How bear-harness uses this

<!-- Maps every external token to the code path that emits it. Cite by FILE PATH only -- never a line number. -->

| External concept | bear-harness call site | File ref (path, NEVER line number) |
|---|---|---|
| `base_url` = vLLM root | Published into the endpoint record and injected as `$MODEL_BASE_URL` into the pipeline command | `src/bear_harness/_endpoint_discovery.py`, `src/bear_harness/_pipeline_launcher.py`, `src/bear_harness/_substitute.py` |
| `default_headers={"Authorization": f"Bearer {key}"}` + `x-api-key` | Both auth dialects sent so the client works against vLLM (Bearer) and Anthropic (x-api-key) | `src/bear_harness/_endpoint_discovery.py` (probe uses Bearer); shim/adapter path in `src/bear_harness/_messages_shim_server.py` |
| `messages.create(...)` request → OpenAI chat | `anthropic_request_to_openai` (normalises `system`, maps fields) | `src/bear_harness/_messages_shim.py` |
| OpenAI chat response → Anthropic `Message` | `openai_response_to_anthropic` (maps `finish_reason`→`stop_reason`, renames usage fields) | `src/bear_harness/_messages_shim.py` |
| `usage.input_tokens` / `output_tokens` | Renamed from OpenAI `prompt_tokens`/`completion_tokens`; cache fields hard-zeroed | `src/bear_harness/_messages_shim.py` |
| `$MODEL_BASE_URL` / `$MODEL_API_KEY` / `$MODEL_NAME` substitution into the pipeline command | The allowlisted variable set the harness injects after the endpoint is known | `src/bear_harness/_substitute.py`, `src/bear_harness/_pipeline_launcher.py` |
| Loopback `POST /v1/messages` shim server (non-vLLM backends) | `MessagesShim` HTTP server translating Anthropic⇄OpenAI on a local port | `src/bear_harness/_messages_shim_server.py` |

The worker consumes the endpoint the vLLM server published — the consume side of the reference preset ([`../docs/decision-notes/first-decision.md`](../docs/internal/decision-notes/first-decision.md)). The endpoint URL is baked into the pipeline command at submit time, which is why detached deploy returns *after* the probe ([`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../docs/internal/decision-notes/detached-deploy-cut-after-probe.md)).

---
## Open questions to resolve when we wire it

1. Streaming (`stream=True`) is not yet driven through the shim — confirm the SSE event sequence vLLM emits on `/v1/messages` matches what the Anthropic SDK's stream parser expects. Source: a `bbshort` run with a streaming pipeline.
2. Tool-use / `tool_use` content blocks: not yet translated by the shim. Pin the request/response mapping before any tool-calling preset. Source: Anthropic Messages tool-use docs + a vLLM tool-calling smoke test.

---
*Crib drafted 2026-06-14 against the Anthropic Messages API (stable) and `anthropic` Python SDK 0.x, cross-checked against `src/bear_harness/_messages_shim.py` and `src/bear_harness/_endpoint_discovery.py`. Update on: an Anthropic Messages wire-shape change (new `usage` fields, `content` block types); an `anthropic` SDK major bump that changes `base_url`/`default_headers` behaviour; the first time streaming or tool-use is wired through the shim.*

<!-- when to expand me: split only by DISTINCT decision surface -- e.g. a dedicated "Messages
     Batches API" crib if/when the harness drives real batches rather than per-request calls.
     Do not restate vLLM's serve flags or routes here -- link to vllm-serve-api.md. -->
