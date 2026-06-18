# vLLM `serve` — OpenAI-compatible HTTP API + `serve` CLI reference crib

<!-- SNAPSHOT-WITH-PROVENANCE. One file = one external surface the project drives.
     Allowed to age -- but it carries its source so staleness is detectable and refresh is
     mechanical. Provenance-at-top + refresh-contract-at-bottom around a distilled body. -->

> **Canonical source:** <https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html> (serving API) and <https://docs.vllm.ai/en/latest/serving/engine_args.html> (`vllm serve` flags). Anthropic-route support landed via vLLM PRs [#22627](https://github.com/vllm-project/vllm/pull/22627) and [#27882](https://github.com/vllm-project/vllm/pull/27882).
> **Version / pin:** `docker://vllm/vllm-openai` — **`>=0.11.0`** is the floor, because that is the first image that natively serves `POST /v1/messages` (the Anthropic Messages route). The bootstrap default tag is `:latest`; pin an explicit tag (e.g. `:v0.11.0`) when reproducibility matters.
> **Why this crib:** `src/bear_harness/_vllm_launcher.py` assembles the `vllm serve` argv and `src/bear_harness/_endpoint_discovery.py` probes the live server. This crib exists to prevent **two real bugs already hit**: (1) double-`/v1` URLs (`/v1/v1/messages` 404s) when code treats `base_url` as already-suffixed; (2) a silent **401 on every call** because vLLM's `--api-key` middleware wants `Authorization: Bearer <key>`, **not** `x-api-key`.

---
## What it is

`vllm serve <model>` boots an OpenAI-compatible HTTP server on a GPU node. From vLLM 0.11 it *also* serves the Anthropic Messages route `POST /v1/messages`, which is exactly what bear-harness's pipeline programs (and the Anthropic SDK) speak — see [`anthropic-messages-api.md`](anthropic-messages-api.md). bear-harness publishes the server root as `base_url` in the endpoint record, then probes `GET /v1/models` and `POST /v1/messages` before declaring the server ready.

---
## Surface that matters  (flags / endpoints / shapes)

### HTTP routes (relative to the server ROOT)

```text
GET  /v1/models      → 200 + {"data":[{"id":"<served-model-name>", ...}]}   # readiness + model-name check
POST /v1/messages    → Anthropic Messages route (vLLM >=0.11). max_tokens=1 probe must 200.
POST /v1/chat/completions   → OpenAI chat route (the dialect /v1/messages translates to internally)

# base_url is the ROOT: http://<host>:<port>   — NO trailing /v1.
# The probe appends /v1/models and /v1/messages; the Anthropic SDK appends /v1/messages.
```

### Auth header (the trap)

```http
Authorization: Bearer <api-key>      # CORRECT — vLLM --api-key middleware reads this
x-api-key: <api-key>                 # WRONG for vLLM — ignored → middleware returns 401
```

### `vllm serve` CLI flags bear-harness emits

```text
vllm serve <model> \
  --host <host> \
  --port <port> \
  --api-key <key> \                 # enables the Authorization: Bearer middleware
  --served-model-name <name> \      # the id /v1/models reports; defaults to <model>
  --max-model-len <int> \           # clamp context window to fit GPU memory
  --tensor-parallel-size <int> \    # shard across N GPUs (e.g. 2 for 70B on 2x a100_80)
  --gpu-memory-utilization <0..1> \ # fraction of VRAM vLLM may claim (KV-cache headroom)
  --dtype <auto|float16|bfloat16> \ # weight dtype
  --enable-prefix-caching           # reuse shared prefixes across requests
```

---
## Gotchas / quirks / version traps

- **The base URL is the server ROOT, with no `/v1`.** `base_url = http://<host>:<port>`. Routes are `/v1/models` and `/v1/messages`. If any consumer pre-appends `/v1` to `base_url`, you get `/v1/v1/...` → **404**. The endpoint record's `base_url` is normalised with `.rstrip("/")` and the probe appends the path — keep that invariant; never bake `/v1` into the stored `base_url`.
- **Auth is `Authorization: Bearer <key>`, NOT `x-api-key`.** vLLM's `--api-key` middleware only honours the Bearer header. Sending `x-api-key` (the *Anthropic-native* header) against vLLM yields a 401, and because pipeline programs may swallow per-act errors this surfaces as **every act failing silently / zero successful calls** — the `ZeroSuccessfulCallsError` pattern. The Anthropic adapter must therefore send the Bearer dialect when pointed at vLLM (see [`anthropic-messages-api.md`](anthropic-messages-api.md) for how bear-harness sends both auth dialects).
- **`/v1/messages` 404s on pre-0.11 images.** `GET /v1/models` will happily return 200 while `POST /v1/messages` 404s, because the OpenAI routes predate the Anthropic route. This is *exactly* why the readiness probe checks **both** routes, not just `/v1/models`. The fix is to re-pull a `>=0.11.0` image (or `--apptainer-image docker://vllm/vllm-openai:v0.11.0`).
- **`--served-model-name` must match what callers request.** The readiness probe asserts the requested model id appears in `/v1/models`. If `--served-model-name` and `--model` disagree (only possible via a hand-edited sbatch script), the probe fails with `does not list expected model`. Re-render via `bear-harness launch`.
- **Model load time ≠ scheduler "RUNNING".** SLURM may report the job `R` while vLLM is still loading weights (5–10 min for a 70B). The endpoint file appears only after vLLM is actually listening — never treat `R` as "ready". The probe + `--boot-timeout` are what gate readiness.
- **OOM is a serve-flag problem, not a model problem.** Tune `--gpu-memory-utilization` down and/or `--max-model-len` down; pick the right GRES. See [`bluebear-platform.md`](bluebear-platform.md) for GRES strings and the OOM triage in the validation runbook.

---
## How bear-harness uses this

<!-- Maps every external token to the code path that emits it. Cite by FILE PATH only -- never a line number. -->

| External concept | bear-harness call site | File ref (path, NEVER line number) |
|---|---|---|
| `base_url = http://<host>:<port>` (root, no `/v1`) | Computed when assembling the `VllmSpec`; published into the endpoint record | `src/bear_harness/_vllm_launcher.py`, `src/bear_harness/_endpoint_discovery.py` |
| `GET /v1/models` readiness probe | `_probe_models` — asserts 200 + expected model id in `data[].id` | `src/bear_harness/_endpoint_discovery.py` |
| `POST /v1/messages` (max_tokens=1) readiness probe | `_probe_messages` — catches pre-0.11 images that 404 the Anthropic route | `src/bear_harness/_endpoint_discovery.py` |
| `Authorization: Bearer <key>` | Probe builds `headers = {"Authorization": f"Bearer {api_key}"}` | `src/bear_harness/_endpoint_discovery.py` |
| `vllm serve <model> --host/--port/--api-key/--served-model-name` | Argv assembled in `build_local_vllm_spec` | `src/bear_harness/_vllm_launcher.py` |
| `--max-model-len` / `--gpu-memory-utilization` / `--dtype` (+ `--tensor-parallel-size`, mem) | Optional tuning knobs threaded through `SlurmVllmOptions` → rendered sbatch | `src/bear_harness/_vllm_launcher.py`, template `vllm.sbatch.j2` |
| Endpoint published for the worker to consume (`role=sidecar` server) | The endpoint record is the typed "publishes" record the pipeline job consumes | `src/bear_harness/_endpoint_discovery.py`; contract in [`../specs/01-foundational-contract.md`](../docs/internal/specs/01-foundational-contract.md) |

This is the **reference preset's** server half: vLLM publishes the endpoint, the worker consumes it, the server is `role=sidecar` — the canonical instance of the JobGraph contract ([`../docs/decision-notes/first-decision.md`](../docs/internal/decision-notes/first-decision.md)). The "detached deploy returns after the probe" cut exists because the pipeline command bakes `$MODEL_BASE_URL` at submit time, so the endpoint must be known first ([`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../docs/internal/decision-notes/detached-deploy-cut-after-probe.md)).

---
## Open questions to resolve when we wire it

1. Confirm `--enable-prefix-caching` interaction with the Anthropic `/v1/messages` path on the pinned image — is the cache keyed before or after the dialect translation? Source: a real `bbshort` run with repeated prefixes + vLLM metrics.
2. Whether `--tensor-parallel-size > 1` changes the `base_url`/readiness timing (rank-0 listens; verify the probe still races correctly). Source: the Step-4 70B validation run in [`../docs/runbooks/validation.md`](../docs/runbooks/validation.md).

---
*Crib drafted 2026-06-14 against `vllm/vllm-openai >=0.11.0`, from the vLLM serving docs cross-checked against `src/bear_harness/_vllm_launcher.py` and `src/bear_harness/_endpoint_discovery.py`. Update on: a vLLM image major/minor bump; any change to `/v1/messages` route support or its auth middleware; the next time `vllm serve --help` differs from the flag list above; if the Anthropic route's request/response shape diverges from [`anthropic-messages-api.md`](anthropic-messages-api.md).*

<!-- when to expand me: split only by DISTINCT decision surface -- the vllm serve CLI vs the HTTP
     API already share enough context to live together; split out the CLI only if the engine-arg
     surface grows unwieldy. Do not restate Anthropic request/response shapes here -- link to
     anthropic-messages-api.md. -->
