# Guardrails are default-deny and govern resources, not science (an autonomous agent on a shared cluster starts on a tight leash and widens explicitly)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** direction-set 2026-06-14
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the W2 guardrail layer — QoS allowlist, walltime ceiling, concurrency cap, GPU-hours budget, dry-run gate
**Drives:** `ROADMAP.md` W2 (guardrails + MCP-over-SSH + notify), the safety invariants referenced from [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`CLAUDE.md`](../../../CLAUDE.md)

---
## Decision

The guardrail layer is **default-deny**: an autonomous agent may only use resources that have been explicitly allowed. It caps QoS (an allowlist of permitted tiers), walltime (a ceiling), concurrency (a cap on simultaneous jobs), and GPU-hours (a budget), and it gates risky actions behind a dry-run. It governs **resources only** — it never constrains *experimental* choices (which model, which prompts, which hyperparameters, how many runs in a campaign). The human owns the science; the guardrails own the bill.

**Start the agent on a tight leash and widen it explicitly, in config** unless a resource is on the allowlist — anything not permitted is denied, and widening is a deliberate human edit, never an agent inference.

## Why

- **Shared cluster, unknown budget ceiling** — a fully autonomous agent on BlueBEAR is spending a *shared* resource against a budget whose ceiling isn't statically known. Default-deny is the only safe starting posture: the failure mode of "agent quietly burns the group's GPU-hours" is unacceptable, and default-*allow* makes it the default outcome. This is the same risk that makes login-node heavy compute forbidden (see [`bluebear-only.md`](bluebear-only.md) and the orchestration-only rule in [`CLAUDE.md`](../../../CLAUDE.md)).
- **Resources, not science, is the right cut** — it mirrors the autonomy boundary the whole project rests on: autonomy is in OPERATION, not SCIENCE (see [`PROJECT-VISION.md`](../PROJECT-VISION.md)). Guardrails that touched experimental choices would be the agent making scientific decisions through the back door. Capping *what it can spend* leaves the human's hypothesis and design untouched while still making autonomy safe.
- **Caps compose with the QoS reality** — the allowlist and walltime ceiling map directly onto BlueBEAR's QoS tiers and walltime limits captured in [`references/bluebear-platform.md`](../../../references/bluebear-platform.md) (e.g. `bbshort`'s 10-minute fast-track for iteration vs `bbgpu` for real runs). The guardrail isn't an abstract policy; it's expressed in the cluster's own units.
- **The dry-run gate makes intent inspectable before spend** — a risky action renders to data (the sbatch it *would* submit) and is checked against the caps before any GPU-hour is committed. This pairs with the declarative-presets stance (see [`declarative-presets-first.md`](declarative-presets-first.md)): authored work reduces to inspectable data validated against caps *before* submit.

## The tradeoff (read before relying on it)

Default-deny means friction: legitimate work will hit a wall the first time it needs a tier, a longer walltime, or more concurrency than the allowlist grants, and the human has to widen the config before the agent can proceed. We accept that friction deliberately — a tight leash that occasionally blocks good work is far cheaper than a loose one that occasionally authorises a runaway campaign on shared hardware. The cost is borne at *widening time* (a human edit), which is exactly where we want a human in the loop.

Escalate / reconsider the *defaults* (not the default-deny stance) when the friction of widening dominates real usage — tune the starting allowlist/ceilings, but never flip to default-allow.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Default-allow with post-hoc limits — let the agent run, alert/kill when a budget is exceeded.*** Tempting because it's frictionless for the common case and "we'll catch overruns after the fact". Rejected because on a shared cluster the damage (consumed GPU-hours, displaced group jobs) is done *before* the post-hoc check fires, and the observability discipline (never trust PID liveness; see [`CLAUDE.md`](../../../CLAUDE.md)) makes reliable real-time killing of a runaway harder than preventing the submission in the first place. Would reconsider only if the cluster itself enforced a hard per-user budget below the level we'd ever worry about — it doesn't, so prevention beats reaction.
- ***Guardrails that also constrain experimental choices (e.g. cap campaign size, restrict models).*** Tempting because it feels like a stronger safety net — fewer runs, fewer surprises. Rejected because it crosses the autonomy boundary: the human owns the hypothesis and design, and a guardrail that vetoed "run the 2,700-run campaign" or "use the 70B model" would be the tool making a scientific call. The right lever is the *resource* (GPU-hours budget), which bounds cost without dictating science. Would reconsider if a class of experimental choice turned out to be a pure resource-risk in disguise — but even then, express it as a resource cap, not a science cap.
- ***No guardrails in V1 — single trusted user, add them later.*** Tempting because today's only user is the author, who won't sabotage himself, so this is the lowest-effort path to the first win. Rejected because the *whole point* is unbabysat autonomy: an agent driving deploys without a human watching is exactly the situation where an unbounded mistake (a wrong walltime, a runaway concurrency) costs real shared GPU-hours. The guardrails are what make "nobody babysitting SLURM" safe rather than reckless. Would reconsider the *timing* only if W1 slipped and W2 had to follow — but they ship as the same first win for this reason.

## How it's wired

Direction-set as of this date; the guardrail layer lands in W2 (see `ROADMAP.md`). It sits between the agent-facing surface (the MCP server, also W2) and submission: the caps are read from config, a dry-run renders the would-be sbatch to data, and the allowlist/ceilings/concurrency/GPU-hours checks run before `sbatch` is ever called. The cluster units the caps speak are pinned in [`references/bluebear-platform.md`](../../../references/bluebear-platform.md). Current build state lives in `lanes.md`, not here.

Verify (once W2 lands): submit a request that exceeds each cap with no explicit allow and confirm it is denied *before* `sbatch`, and that the denial names the cap and the config key to widen.

## Reversibility

high — guardrails are config. The default-deny *stance* is the commitment; the specific allowlist, ceilings, caps, and budget are all tunable without code change, and widening is a one-line config edit.

## Reversal path (if it comes to that)

The caps themselves reverse trivially (edit config). Reversing the *stance* to default-allow would mean removing the deny-by-default check at the submission boundary — strongly discouraged, and the one thing in this note that should not be reversed casually, because it's the property that makes unbabysat autonomy safe on shared hardware. If a specific cap is wrong, widen that cap; do not remove the gate.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Direction set. Records default-deny + resources-not-science as the guardrail posture for W2, justified by the autonomy-in-operation boundary and the shared-cluster/unknown-budget risk. Caps expressed in BlueBEAR QoS/walltime units per [`references/bluebear-platform.md`](../../../references/bluebear-platform.md). Not yet implemented (W2).

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
