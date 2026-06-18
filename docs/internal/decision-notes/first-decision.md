# The kernel is preset-agnostic: it honours the JobGraph contract, not the workload (one control plane, an open set of presets on top)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** adopted·live
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the whole control plane — `src/bear_harness/` kernel and every preset that targets the contract
**Drives:** `ROADMAP.md` (W3 "extract the JobGraph contract"), [`PROJECT-VISION.md`](../PROJECT-VISION.md), the spec contract [`specs/01-foundational-contract.md`](../specs/01-foundational-contract.md), and every other decision-note in this directory (this is the keystone they hang from)

---
## Decision

The kernel is the **preset-agnostic control plane**: detached deploy → `run_id` handle → status/logs/results, all filesystem-attached so any session (even a brand-new one) reattaches by `run_id`. It realises a **JobGraph** — jobs + edges (`after` / `afterok`) + publishes/consumes + `role=sidecar`, across the single / bundle / coupled / dag topologies — and it stops there. It does **not** know what any **preset** computes. vLLM+pipeline is merely the *reference* preset; ETL is the second. Presets are an open extension point above a closed contract.

**The kernel honours the JobGraph contract, not the workload** unless a workload need cannot be expressed as jobs + edges + publishes/consumes + a role — in which case the contract is extended once, in the open, and every preset inherits it.

## Why

- **One control plane for all topologies** — the same deploy/status/results machinery serves a single job, a SLURM array bundle, a coupled server+worker, and a full DAG. We do not grow a new orchestrator per workload shape; we grow new presets over one realiser.
- **Presets are an open extension point** — a preset is a human- or LLM-authored unit that targets the contract. Because the kernel reads only the JobGraph, a new preset (ETL, sweeps, eval, training) is additive and needs no kernel change. This is what lets an autonomous agent author presets without touching the control plane.
- **vLLM+pipeline is just the reference** — the working vLLM flow already realises the publishes/consumes + `role=sidecar` pattern (server publishes the endpoint, worker consumes it, server is `scancel`led when its consumers finish). W3 extracts the contract *from* that flow behaviour-preservingly, demoting it to one preset among many. The agnostic kernel is the residue.
- **It encodes the autonomy boundary** — autonomy is in OPERATION, not SCIENCE. A control plane that knew what a workload computed would be tempted to make scientific choices. One that knows only the contract structurally cannot. See [`PROJECT-VISION.md`](../PROJECT-VISION.md).

## The tradeoff (read before relying on it)

We pay an indirection tax. A preset author cannot reach "down" into the kernel for a workload-specific shortcut — every capability a preset needs must first be expressible in the JobGraph vocabulary. The day a real workload wants something the contract can't say (a conditional edge, a fan-in barrier, a typed record shape we didn't anticipate), the cost lands on the *contract*, not the preset: it's a breaking change to the one thing every preset depends on. We accept that because a leaky shortcut would re-couple the kernel to a workload and dissolve the whole property.

Escalate / reconsider when a second or third preset each needs a *different* kernel-level special-case to function, or when the JobGraph vocabulary starts accreting workload-named fields (a sign the abstraction is leaking).

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***A vLLM-specific harness — bake the server+worker pattern straight into the control plane.*** Tempting because it's the only workload we have running today, it would be simpler with one consumer, and every line would be directly testable against a real run. Rejected because the second preset (ETL: no GPU, no server) would then require forking the control plane, and the autonomous-authoring story collapses — an agent can't add a workload shape the kernel has hard-coded against. Would reconsider if we became confident bear-harness will only ever run vLLM-shaped workloads, which the ETL de-risker is specifically designed to disprove.
- ***Per-topology orchestrators — one code path for single jobs, another for arrays, another for coupled server+worker.*** Tempting because each path could be optimal and independently simple. Rejected because status/logs/results/reattach would fork four ways, and "never lose results" becomes four separate guarantees to keep true. The JobGraph unifies them behind one realiser. Would reconsider if the topologies proved to share almost no machinery in practice — they don't; deploy/handle/reattach is identical across all four.
- ***A general workflow engine (Airflow/Nextflow/Snakemake) as the kernel.*** Tempting because DAG execution is solved and battle-tested. Rejected because those engines abstract *schedulers and workloads both*, carry a daemon/operational footprint we don't want on a login node, and don't give us the filesystem-attached reattach-by-`run_id` property as a first-class guarantee. The contract abstracts WORKLOADS, deliberately not SCHEDULERS (see [`bluebear-only.md`](bluebear-only.md)). Would reconsider if maintaining the realiser ever costs more than adopting and constraining one of these engines behind the same JobGraph interface.

## How it's wired

The kernel lives in `src/bear_harness/`. Today the realiser is the SLURM path (`src/bear_harness/_slurm_runner.py`, `src/bear_harness/_launch.py`) driving the vLLM reference preset (`src/bear_harness/_vllm_launcher.py`, `src/bear_harness/_pipeline_launcher.py`); W3 lifts the JobGraph contract out of `_launch.py` so the launcher reads a graph rather than a hard-coded server+worker pair. State is filesystem-attached: `run.json` (harness state machine), `.bear-harness-status.json` (program heartbeat), `endpoint.json`, and the artifacts tarball — all under the shared FS keyed by `run_id`, which is what makes reattach work from any session. The contract itself is specified in [`specs/01-foundational-contract.md`](../specs/01-foundational-contract.md).

Verify (the agnosticism claim is falsifiable): run the two presets back-to-back on the same harness binary with **zero** kernel diff between them —
```bash
hatch run test                              # contract + launch tests, incl. TestDetach
# on BlueBEAR, the Phase-D cross-validation:
bear-harness launch path/to/your/pipeline --model Qwen/Qwen2.5-7B-Instruct
bear-harness launch path/to/example/summarize-dir --model Qwen/Qwen2.5-7B-Instruct
```
If both complete with no harness change between them, the kernel is agnostic in practice, not just in principle.

## Reversibility

medium — the contract is the commitment. Code on either side of it is replaceable; the JobGraph *vocabulary* is the load-bearing API every preset depends on, so changing it is a coordinated migration, not a local edit.

## Reversal path (if it comes to that)

To collapse back to a workload-specific kernel: pick the one surviving workload, inline its preset's submit logic into `_launch.py`, and delete the JobGraph indirection layer. What's load-bearing on the way out is every *other* preset — each one becomes dead the moment the kernel stops reading the contract, so a reversal is really a decision to support exactly one workload forever. The filesystem-attached state (`run.json`, status file, `endpoint.json`, artifacts) is independent of this choice and survives either way; that machinery is what you must not lose.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Keystone note authored. Records the preset-agnostic-kernel commitment as the architecture's spine; W3 (extract the JobGraph contract from the working vLLM flow) is the phase that makes the code match this note. Verified against `src/bear_harness/_launch.py` (the detach handle + filesystem-attached `run.json` already exist) and the two-preset plan in `ROADMAP.md`.

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
