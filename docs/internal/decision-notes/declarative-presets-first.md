# Declarative presets first; the Python-builder form earns its way in behind a sandbox (an autonomous author's work must reduce to inspectable data validated against caps before submit)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** direction-set 2026-06-14
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the preset authoring form — the W4 declarative authoring kit and the deferred Python-builder authoring form
**Drives:** `ROADMAP.md` W4 (ETL preset + declarative authoring kit: `validate_preset` / `dry_run` / `describe_preset` / `list_presets`) and the next-cycle "Python-builder authoring form" lane

---
## Decision

A preset is authored as **declarative data** — a typed description of jobs, edges, publishes/consumes, and roles that the kernel reads as a JobGraph. The first-class authoring kit is declarative: `validate_preset`, `dry_run`, `describe_preset`, `list_presets`. A richer Python-builder authoring form (writing a preset as code) is **deferred** to a later cycle and, when it lands, must run behind a sandbox so that it still *reduces to the same inspectable data* before anything is submitted.

**An authored preset must reduce to inspectable data, validated against the caps, before submit** unless we are willing to let an autonomous agent submit code we cannot inspect first — which we are not.

## Why

- **The author may be the agent** — presets are human- *or LLM-authored*. When an autonomous agent writes the preset, "inspect before submit" is the load-bearing safety property: a declarative preset is just data the [guardrail layer](default-deny-guardrails.md) can validate against the QoS/walltime/concurrency/GPU-hours caps *before* a single `sbatch`. Arbitrary author-supplied Python cannot be validated the same way — it has to *run* to reveal what it does.
- **It composes with default-deny and the dry-run gate** — the W4 kit's `dry_run` and `validate_preset` are exactly the inspect-and-check step that [default-deny guardrails](default-deny-guardrails.md) require. Declarative-first makes that step a pure function of data; the Python-builder form would otherwise punch a hole in it.
- **It honours the contract boundary** — the [keystone decision](first-decision.md) says the kernel reads a JobGraph and nothing else. A declarative preset *is* a JobGraph description; the kernel stays agnostic. A code-form preset only stays compatible if it, too, ultimately emits that same data — which is precisely the constraint the sandbox enforces.
- **ETL proves the declarative form is enough** — the second preset (ETL: no GPU, no server) is deliberately chosen to exercise the declarative authoring kit on a workload unlike vLLM+pipeline. If a no-server, no-GPU pipeline and a coupled server+worker pipeline both express cleanly as declarative data, the form has earned its primacy.

## The tradeoff (read before relying on it)

Declarative-first means some presets are more awkward to express than they'd be in code — anything that wants real control flow, computed fan-out, or programmatic job generation has to be encoded as data (or wait for the builder form). We accept that awkwardness for V1 because it's the price of "inspect before submit", and because the workloads on the near roadmap (ETL, the vLLM reference) express cleanly as data. The Python-builder form isn't rejected — it's gated on a sandbox that preserves the inspect-before-submit property, so it pays for the expressiveness with the cost of building that sandbox.

Escalate / reconsider when a genuinely useful preset *cannot* be expressed declaratively without contortion (the un-defer trigger for the Python-builder lane) — then build the sandboxed builder form, not a raw code-eval path.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Python-builder authoring first — let authors write presets as code from day one.*** Tempting because it's the most expressive and natural form for a programmer, and an LLM is fluent at emitting Python. Rejected for V1 because author-supplied code has to *execute* before you can know what it submits, which defeats validate-against-caps-before-submit for an autonomous author — the exact situation where pre-submission inspection matters most. Would reconsider once a sandbox exists that runs the builder and captures its emitted JobGraph data *without* side effects, so the inspect-before-submit property survives; that's the form the next-cycle lane targets, not raw code-eval.
- ***A general workflow DSL instead of typed declarative data.*** Tempting because a DSL could be more expressive than flat data while still being inspectable. Rejected because it's a parser, a grammar, and a spec to maintain for a need two presets don't yet have — speculative generality. Typed declarative data validated against the caps is the smallest thing that meets the safety bar. Would reconsider if the declarative form started accreting ad-hoc conventions that a real grammar would clean up — but that's a sign of growth, not a V1 requirement.
- ***No authoring kit — presets are hand-written by the maintainer only.*** Tempting because it's zero work and the only author today is the author. Rejected because the vision is *autonomous* authoring: an agent that can add a preset is the point, and an agent needs `validate_preset` / `dry_run` / `describe_preset` / `list_presets` to author safely and discover what exists. Would reconsider only if autonomous authoring were dropped from scope — which would gut the north star.

## How it's wired

Direction-set as of this date; the declarative authoring kit lands in W4 alongside the ETL preset (see `ROADMAP.md`). The kit's four verbs — `validate_preset`, `dry_run`, `describe_preset`, `list_presets` — operate on the declarative preset *as data* and check it against the [guardrail caps](default-deny-guardrails.md) before any submission. The Python-builder authoring form is a next-cycle lane, gated on a sandbox. Current build state lives in `lanes.md`, not here.

Verify (once W4 lands): author a preset, run `validate_preset` + `dry_run`, and confirm the full JobGraph (jobs, edges, publishes/consumes, roles) is rendered to inspectable data and checked against the caps *with no `sbatch` issued*.

## Reversibility

high — the authoring *form* is independent of the kernel and the contract. Adding the Python-builder form later, or changing the declarative schema, is additive work behind the same JobGraph contract; nothing downstream of the kernel cares which form authored the data.

## Reversal path (if it comes to that)

To admit the Python-builder form: build the sandbox that executes an author's builder and captures its emitted JobGraph *without side effects*, then route that captured data through the *same* `validate_preset` / `dry_run` / cap-checks the declarative form uses. Load-bearing on the way out: the builder must not gain a path that submits before its emitted data is inspected and cap-checked — the sandbox exists precisely to preserve inspect-before-submit. The declarative form, the kernel, and the contract are untouched.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Direction set. Records declarative-presets-first with the Python-builder form deferred behind a sandbox, justified by the inspect-before-submit requirement an autonomous author imposes and its composition with [default-deny guardrails](default-deny-guardrails.md). Authoring kit (`validate_preset`/`dry_run`/`describe_preset`/`list_presets`) scheduled for W4. Not yet implemented.

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
