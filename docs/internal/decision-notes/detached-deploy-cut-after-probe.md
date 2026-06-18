# Detached deploy returns after the vLLM probe, not after submitting both jobs (because the pipeline command bakes the endpoint URL in at submit time)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** adopted·live
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the kernel's detached-deploy path — `src/bear_harness/_launch.py` and the `--detach` CLI wiring
**Drives:** `ROADMAP.md` W1 (the kernel: detached deploy + reattach-by-`run_id`), the reliability bar in [`PROJECT-VISION.md`](../PROJECT-VISION.md)

---
## Decision

`launch(..., detach=True)` cuts the flow **after the vLLM endpoint is probed**, not the instant both jobs are submitted. It still submits both jobs (the vLLM server and the pipeline worker), still probes the endpoint to confirm vLLM is live, and only then early-returns a `running` `LaunchResult` handle keyed by `run_id`. The deploy *interface* — submit, get a handle back, reattach later — is identical to a hypothetical instant-return; only the moment of return differs.

**The cut sits after the probe, because the endpoint URL must be known before the pipeline is submitted** unless and until we add runtime endpoint injection — which is a later optimisation, not a contract change.

## Why

- **The pipeline command bakes the endpoint URL at submit time** — `src/bear_harness/_pipeline_launcher.py` substitutes `$MODEL_BASE_URL` (and the other env) into the pipeline command *before* the pipeline sbatch is written. So the endpoint has to exist and be confirmed reachable before the pipeline job can be submitted at all. Probing first is not caution; it's a data dependency.
- **It keeps failures loud** — because the probe runs inside the deploy call, a detached run that can't reach vLLM fails *at deploy*, with a diagnosable error, rather than silently submitting a pipeline job that will burn its walltime hitting a dead endpoint. This is the reliability bar's "no silent zero-output completion" applied to the detach path. The early-return code path in `_launch.py` is explicit that the cut is *after* the probe precisely so a detached run still fails loudly.
- **Bounded return, durable handle** — the caller gets a `run_id` in seconds-to-minutes (probe time, not campaign time) and `LaunchResult.as_dict()` serialises the handle to `run.json` on the shared FS. Any later session reattaches by `run_id`. That's the W1 deliverable.
- **The interface is forward-compatible** — instant-return (submit both, return immediately, inject the endpoint into the pipeline at runtime) would change only *when* this function returns, not its signature or its `run.json` contract. We can adopt it later without touching callers.

## The tradeoff (read before relying on it)

"Detached" today still blocks for vLLM boot + probe — for a 70B model on cold storage that can be many minutes, not seconds. So the laptop-side caller is held longer than the word "detach" suggests. We accept that for V1 because the alternative (instant return) requires runtime endpoint injection into an already-submitted pipeline job, which is real new machinery, and because blocking through the probe is exactly what buys the loud-failure guarantee. The handle and reattach semantics are already correct; only the return latency is suboptimal.

Escalate / reconsider when probe-time latency becomes a felt problem (e.g. driving many large-model deploys back-to-back from the laptop), at which point build runtime endpoint injection and move the cut earlier — the interface won't move.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Instant return — submit both jobs, return in seconds, inject the endpoint into the pipeline at runtime.*** Tempting because it's what "detached" intuitively means and it minimises laptop-side wait. Rejected *for now* because the pipeline command currently has `$MODEL_BASE_URL` substituted at submit time in `_pipeline_launcher.py`; instant return needs the pipeline to discover its endpoint at *runtime* (read `endpoint.json` from the shared FS on startup) instead of having it baked in. That's a real change to how the worker is launched, and the probe-after-submit version delivers the same handle/reattach interface without it. Would build it the moment probe latency is a felt cost — and because the deploy interface is identical, that's a localised change behind a stable signature.
- ***Return immediately after submit with no probe at all.*** Tempting because it's the fastest possible return and "SLURM will report failures anyway". Rejected because it trades away the loud-failure guarantee: a misconfigured endpoint (wrong route, wrong auth header — the exact `/v1/v1` and `x-api-key` bugs the [vLLM crib](../../../references/vllm-serve-api.md) exists to prevent) would produce a pipeline job that runs to walltime emitting zero successful calls, the silent-zero-output failure the reliability bar forbids. Would reconsider only if endpoint health were proven another way before the pipeline starts — but then we've just moved the probe, not removed it.

## How it's wired

The detach branch and its post-probe early return live in `src/bear_harness/_launch.py` (the `detach` parameter on `launch`, the early-return that constructs a `running` `LaunchResult`, and `LaunchResult.as_dict()` for the `run.json` serialisation). The submit-time endpoint substitution that forces the ordering is in `src/bear_harness/_pipeline_launcher.py` (`MODEL_BASE_URL` → `endpoint.base_url`). The `--detach` / `--json` CLI surface in `src/bear_harness/_cli.py` is PENDING as of this date (W1 in progress).

Verify:
```bash
hatch run test -- tests/test_launch.py::TestDetach \
                  tests/test_launch.py::TestLaunchResultSerialisation
```
Note (W1 in progress, 2026-06-14): `TestDetach` and `TestLaunchResultSerialisation` are written and currently **red**; the `_cli.py` `--detach`/`--json` wiring that turns them green is pending. The `_launch.py` detach parameter, `as_dict()`, and the post-probe cut already exist. Current state lives in `lanes.md`, not here.

## Reversibility

high — moving the cut earlier (instant return) is a localised change behind a stable deploy interface. Callers and the `run.json` contract don't move; only the return latency and the pipeline's endpoint-discovery mechanism change.

## Reversal path (if it comes to that)

To switch to instant return: make the pipeline worker read `endpoint.json` from the shared FS at startup instead of consuming a baked-in `$MODEL_BASE_URL`, then move the early-return in `_launch.py` to *before* the probe (submit both, return). Load-bearing on the way out: the loud-failure property must be preserved some other way (e.g. the worker self-checks the endpoint and writes a diagnosable error to its status file on first failure) — do not let instant-return silently reintroduce zero-output completions. The `run_id` handle and `LaunchResult` shape are unchanged.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Note authored. Records the post-probe cut as forced by submit-time endpoint substitution, with instant-return deferred behind an identical interface. Verified against the detach path in `src/bear_harness/_launch.py` and the `MODEL_BASE_URL` substitution in `src/bear_harness/_pipeline_launcher.py`; `TestDetach`/`TestLaunchResultSerialisation` present and red (W1 in progress).

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
