# The post-V1 horizon is depth of autonomous operation, bounded to the next cycle — not breadth

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** direction-set 2026-06-14
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the forward plan *beyond* V1 — what the vision and roadmap may promise past W4, and the scope of the next cycle (sweeps, eval, training+resume, the Python-builder authoring form)
**Drives:** [`PROJECT-VISION.md`](../PROJECT-VISION.md) ("The arc beyond V1") and `ROADMAP.md` (the gated Tier C — NEXT CYCLE)

---
## Decision

The horizon past V1 is **depth of autonomous operation, bounded to exactly one next cycle** — and it is **not breadth**. Concretely, the next cycle is the *unchanged* JobGraph contract exercised harder: three more presets over the same closed contract (**sweeps** on the `bundle` topology, **eval** as `coupled` fan-out, **training+resume** on a `dag` once checkpoint plumbing lands) plus one new authoring *form* (the **Python-builder**, behind a sandbox). The depth themes those presets serve are auto-retry/resume, budget management, multi-run orchestration, and richer observability — all *operation*, never *science*.

What is **refused, not deferred**: breadth — a second cluster / scheduler portability, agent-designed science, and an open platform/product. These are not a later horizon; they are the permanent fence (see [`bluebear-only.md`](bluebear-only.md) and [`PROJECT-VISION.md`](../PROJECT-VISION.md)). And there is **no multi-year north-star** beyond the next cycle: the roadmap reaches one cycle past V1 and stops, on purpose.

## Why

- **Depth is cheap precisely because the contract does not move.** The keystone ([`first-decision.md`](first-decision.md)) makes presets an open extension point over a closed contract, so every next-cycle item is "another preset (or authoring form) behind the same wire," never a kernel change. That is *why* the next cycle can be named now without committing the kernel to anything — and why naming it is low-risk.
- **An over-specified far future rots.** Docs, code, and memory all drift; a detailed multi-year roadmap is the fastest-rotting artifact of all, and a cold agent that reads it cannot tell live intent from stale aspiration. Bounding the horizon to one concrete, falsifiable cycle keeps the forward plan *true* rather than *inspiring-but-wrong*. This is the discipline the whole doc spine is built to hold.
- **Breadth would change what the tool is.** Multi-cluster portability buys a scheduler-abstraction tax we explicitly refuse ([`bluebear-only.md`](bluebear-only.md)); agent-designed science crosses the autonomy boundary the project rests on (autonomy is in *operation*, not *science* — [`PROJECT-VISION.md`](../PROJECT-VISION.md)). Calling either a "future horizon" would invite a cold agent to start building the fence away.
- **Sequencing the next cycle is not the same as starting it.** Promoting the deferred presets to roadmap phases fixes their *order* for when their triggers fire; it does not authorise building them. The disposition (DEFERRED/BLOCKED) and its trigger stay sovereign — see `spec-deferrals.md`.

## The tradeoff (read before relying on it)

A bounded horizon means bear-harness does not advertise a grand long-range vision — there is no "where this goes in three years" section, and there is deliberately nothing past the next cycle to point at. We accept that: a falsifiable two-cycle plan that stays true is worth more than an aspirational one that misleads the next session. The cost is paid in "vision narrative" we choose not to write; the benefit is a forward plan a cold agent can trust without re-deriving it.

Escalate / reconsider the *horizon length* (not the depth-not-breadth stance) only when the current next-cycle work has actually shipped — then extend by one concrete cycle, never by a speculative many. The stance itself (depth, bounded; breadth refused) is the durable commitment.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Publish a multi-year north-star roadmap.*** Tempting because a long arc is motivating, helps a reader see the ambition ("an autonomous research-operations platform"), and reads well. Rejected because an over-specified far future is exactly the drift the user is most worried about: it rots faster than the code, and a cold agent cannot distinguish a two-year aspiration from a committed plan. Would reconsider only if a *standing* (not hypothetical) requirement for a second cluster or external users emerged — which is itself currently a refusal in [`bluebear-only.md`](bluebear-only.md), so this reconsider is gated on that fence moving first.
- ***Leave the next cycle as loose deferrals — don't sequence it at all.*** Tempting because it commits to nothing and keeps the roadmap minimal. Rejected because, without sequencing, a cold agent reading `spec-deferrals.md` cannot tell a *deferred* preset (sweeps — will build, on a trigger) from a *refused* anti-goal (multi-cluster — never): both just look like "not now." Sequencing-with-triggers is the middle path that preserves the disposition distinction while making the order legible. Would reconsider if the deferral set grew so large that a sequenced tier became noise — it has four items, so it does not.
- ***Treat promotion as un-deferral — start building the depth presets alongside V1.*** Tempting because momentum is real and the presets are "just more of the same." Rejected because it violates both the V1 gate and each item's own trigger: the JobGraph contract is not extracted until W3, so building sweeps/eval now would re-couple workload knowledge into the kernel — the precise failure [`first-decision.md`](first-decision.md) forbids. Would reconsider per-item only when that item's trigger in `spec-deferrals.md` fires, which by construction is after V1.

## How it's wired

This note is the single citation target for "why is the horizon only one cycle, and only depth?" — so the LIVING docs can link it instead of re-arguing it inline. [`PROJECT-VISION.md`](../PROJECT-VISION.md)'s "The arc beyond V1" section names the depth themes and their presets; `ROADMAP.md`'s Tier C sequences them as gated phases. Nothing in code depends on this note; it constrains *what the planning docs may promise*, not what the kernel does. Current build state lives in `lanes.md`, not here.

## Reversibility

high — this is a framing/sequencing commitment recorded in docs; no code depends on it. Extending the horizon by a cycle, or re-prioritising within the next cycle, is a docs edit. The one thing not to reverse casually is the *breadth refusal*, which is owned by [`bluebear-only.md`](bluebear-only.md), not here.

## Reversal path (if it comes to that)

If "depth, bounded" turns out wrong, the failure almost always arrives as a breadth pressure (a second cluster, external users) — and that is a [`bluebear-only.md`](bluebear-only.md) reversal first; this note simply stops being the binding constraint once that fence moves. To extend the horizon (not reverse the stance): when the next cycle ships, supersede this note with a new dated note that names the *following* cycle — one concrete cycle at a time, never a speculative many. Do not retro-edit this note to "stay current"; write the next one and link back.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Direction set. Records the post-V1 horizon as *depth of autonomous operation, bounded to one next cycle* (sweeps/eval/training+resume/Python-builder over the unchanged contract), with breadth (multi-cluster, agent-designed science, open product) refused-not-deferred and no multi-year north-star. Gives [`PROJECT-VISION.md`](../PROJECT-VISION.md) ("The arc beyond V1") and `ROADMAP.md` (Tier C) their single rationale citation.

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
