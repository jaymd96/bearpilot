# Contributing to bear-harness

These are the conventions this repo runs on — written down so every contributor
(human or agent) does a consistent job. [`CLAUDE.md`](../../CLAUDE.md) is the agent-specific
entry point and points here.

The single rule that generates most of the others: **small, sequenced changes, each
with a descriptive trail, reconciled against the code AND against a real `bbshort` run.**

bear-harness lets an LLM autonomously deploy and run human-designed workloads on the
BlueBEAR SLURM cluster. It runs on a **shared** cluster against a **finite** GPU-hour
budget, so "small and reversible" is not a style preference here — it is how an
autonomous agent stays a good tenant. The reliability bar and the two environment
invariants below are the floor; everything else is process.

---

## Branches

| Prefix | For |
|---|---|
| `feature/<slug>` | new capability or phase sub-PR |
| `fix/<slug>` | bug fix |
| `refactor/<slug>` | behavior-preserving restructuring |
| `docs/<slug>` | documentation-only |
| `test/<slug>` | tests-only |
| `chore/<slug>` | tooling, deps, housekeeping |

Branch off latest `main`. **Never commit to `main` directly. Never force-push it.**
Don't bypass the gate (`hatch run lint && hatch run test`).

## Commits

Conventional Commits: `type(scope): subject`. Imperative, lower-case, no trailing
period. Body explains the *why*. Agent commits carry a `Co-Authored-By:` trailer.
Commit secrets-free — no SSH private keys, no vLLM `--api-key` values, no Anthropic keys.

## Pull requests

Small and focused (one logical change). Body = Summary + Test plan. The test plan
**must** include the real verification step: `hatch run lint && hatch run test` green,
**and** — for any change that touches SLURM submission, the launcher, transport, or a
preset — a real `bbshort` run with its `run_id` quoted (see
[`docs/runbooks/validation.md`](../runbooks/validation.md)). Tests passing is
necessary but not sufficient; HPC behaviour is only true once a real job confirms it.
Merge after review + green checks.

## Phased delivery

Large work → a named phase → sequenced sub-PRs, each independently shippable. The forward
sequence is version/phase-keyed (W1 kernel → W2 guardrails + MCP-over-SSH + notify →
W3 extract the JobGraph contract → W4 ETL preset + authoring kit), not calendar-dated —
see `docs/ROADMAP.md`. Don't land a whole phase as one PR.

## Review

Non-trivial PRs get an adversarial review before merge — look for what breaks at 3am,
not what could be slightly better. For this repo, "3am" is concrete: a job that completes
with zero successful calls and reports success, a watcher keyed on a PID that the
round-robin login node rotated out from under it, an endpoint URL baked at submit time
that no longer resolves. Review against the reliability bar, not just the diff.

## Where things live (documentation taxonomy)

<!-- THIS TABLE IS THE SOURCE OF TRUTH for which doc genre owns which question.
     A new file under docs/ or specs/ that has no home here is a bug: pick a genre or
     extend the table. -->

| Location | Holds | Living/Snapshot |
|---|---|---|
| `docs/decision-notes/` | thesis-named ADRs. The *why* (+ steelmanned alternatives + reversal). | snapshot |
| `docs/runbooks/` | operational procedures (the `bbshort` iteration loop, run-validation/observability). | living |
| `docs/DEPLOYMENT.md` | the from-scratch stand-up walkthrough. | living |
| `docs/authoring.md` | how to write a preset against the JobGraph contract. | living |
| `references/` | one crib per **external** surface we drive (cited, pinned, dated). | snapshot-w/-provenance |
| `specs/` | the **contract** (numbered specs; reading-guide is the cross-ref hub). | living |
| `docs/PROJECT-VISION.md` · `docs/lanes.md` | living framing: the value loop + the status snapshot. | living |
| `docs/ROADMAP.md` | the forward build sequence (links to lanes for current state). | living |
| `docs/spec-deferrals.md` | scope deliberately not built, with revisit-triggers. | snapshot |

**Altitude rule (why each genre lives where it does).** Put a fact at the altitude that
matches its change-rate. The *how* is volatile → it lives in **code** (the source of
truth); docs hold *contracts and principles* (the JobGraph contract, the invariants);
vision and decision-notes hold the *why* (durable rationale). Most drift is a fact written
one altitude too low — a line number, or a current SLURM flag or vLLM route frozen into a
doc instead of cited from its crib. When in doubt, write the principle, link the code.

**Living vs snapshot (how to edit each).**
- **LIVING** docs (CLAUDE.md, CONTRIBUTING, specs, runbooks, vision, lanes, roadmap)
  promise to be true *now*. Keep them small, at principle-altitude, reconciled against
  code. Edit in place; strike through superseded clauses, don't delete.
- **SNAPSHOT** docs (plans, decision-notes, provenance, deferral entries) are dated,
  append-only records. **Never edit them to "keep them current."** When reality moves
  on, write a NEW record that supersedes + links forward; the old one stays as history.
  Drift is irrelevant for a snapshot — it records what was true on a date.

## Documentation hygiene (reconcile against code AND a real run)

- **Verify claims against live code/schema AND a real `bbshort` run before relying on them.**
  Refutation > confirmation. An HPC claim that only passed in tests is a hypothesis, not a fact.
- **Single source of truth: one fact, one place.** Everything else LINKS to it.
  Duplication is the seed of drift. Current counts and verified-against run ids live in
  `docs/lanes.md` — link that anchor, don't restate it.
- **Cite stable anchors** — a code path (e.g. `src/bear_harness/_launch.py`), a
  decision-note slug, a crib filename, a spec section — **NEVER line numbers.** Line
  numbers rot on the next edit.
- **Date-stamp time-sensitive claims** (ISO `YYYY-MM-DD`); mark `shipped` vs `planned`.
- `docs/provenance/` summaries and any agent skill/instruction files are docs too —
  reconcile them.
- Fix or flag stale docs on sight.

## Citation discipline (internal + external)

- **Internal:** link by relative path to the ONE doc that owns a fact; cite a
  decision-note by its thesis-slug, a spec by its number+section. Prose/docs get
  markdown links; code/commits/specs get backticked identifiers. Never restate a fact
  you can link.
- **External:** every external surface we drive gets exactly ONE crib in `references/`
  (the four we maintain are listed in [`references/00-index.md`](../../references/00-index.md):
  [`slurm-cli.md`](../../references/slurm-cli.md), [`vllm-serve-api.md`](../../references/vllm-serve-api.md),
  [`anthropic-messages-api.md`](../../references/anthropic-messages-api.md), and
  [`bluebear-platform.md`](../../references/bluebear-platform.md)). **Never recall an external
  API from training** — `--dependency=after:` (not `afterok`), the `/v1/messages` route (not
  `/v1/v1/...`), the `Authorization: Bearer` header (not `x-api-key`): these are real bugs
  the cribs exist to prevent. The canonical URL appears once (the crib's header). Specs,
  docs, code comments, and agent prompts cite the crib by relative path — never the URL,
  never a re-typed flag/endpoint. When upstream drifts you edit one crib; every consumer's
  link still resolves. A crib without a canonical-source header + version pin + footer
  refresh-trigger is malformed, and `checks/check-drift.sh` (the references-provenance lint)
  fails it. Add a retrieval date when the body quotes verbatim, and mark verbatim extracts
  as such; everything else is the team's paraphrase. Self-demote drift-prone values (live
  endpoint URLs, GRES tags, rotating versions) to a runtime probe rather than freezing them.

## Memory hints (for the agent curating project memory)

- **Write the *why*, not the *what*.** A named file or function is a *lead*, not a fact
  — verify before asserting (recall is point-in-time and goes stale).
- **One fact, one place** + links. Prune a memory the moment it contradicts a newer one;
  consolidate periodically.
- Keep memory a small index of durable *why* + pointers, not a transcript.

## Environments

bear-harness is **BlueBEAR-only by design** — portability across clusters or schedulers
is an explicit non-goal; see
[`docs/decision-notes/bluebear-only.md`](decision-notes/bluebear-only.md). The
JobGraph contract abstracts *workloads*, not *schedulers*, so there is no environment
matrix here — there is one cluster, and the "environment" choice is a SLURM QoS partition,
not a deploy stage.

Two QoS tiers matter day-to-day (full tier list and GRES tags live in
[`references/bluebear-platform.md`](../../references/bluebear-platform.md)):

- **`bbshort`** — the ~10-minute fast-track that spans all nodes including GPUs. This is
  the iteration environment. Every change that touches SLURM, the launcher, transport, or
  a preset is verified here before you rely on it.
- **`bbgpu` / `bbcpu`** — the real run tiers. Reach for them once a `bbshort` run confirms
  the change works; never use them as your debug loop.

Guardrails select and cap these tiers (default-deny QoS allowlist, walltime ceiling,
concurrency cap, GPU-hour budget) — see the safety invariants below.

## Checks (the green-checks gate)

`hatch run lint` · `hatch run test` — run both before opening a PR; CI runs the same.
Concretely (the conventions this repo enforces):

- **Tests:** `hatch run test` = `pytest -m 'not integration'`. The full suite incl. real
  vLLM subprocesses is `hatch run test-all` / `hatch run test-integration` — run those
  before relying on a transport or preset change.
- **Lint:** `hatch run lint` = `ruff check`; `hatch run fmt` = `ruff format`. **Line
  length 100.**
- **Types:** `ty` for type-checking — type hints everywhere.
- **Frozen dataclasses** for rule/result outputs (e.g. `LaunchResult` in
  `src/bear_harness/_launch.py`) — immutable, deterministic, hashable.
- **Python 3.11+** (`requires-python = ">=3.11,<4"`).
- **A real `bbshort` run** is part of the gate for any HPC-touching change — not optional,
  not deferrable to "later". See [`docs/runbooks/validation.md`](../runbooks/validation.md).

**Contract-specific gate.** A change to the JobGraph contract or a preset must pass its
validator (`validate_preset`) and a `dry_run` before submit — an autonomous agent's
authored preset reduces to inspectable data validated against the guardrail caps *before*
anything hits SLURM. See
[`docs/decision-notes/declarative-presets-first.md`](decision-notes/declarative-presets-first.md).

**Doc-drift gate.** `sh checks/check-drift.sh .` runs the link/anchor checker + references-provenance
lint (blocking) and a git-freshness staleness surfacer (advisory). Wire it once with
`git config core.hooksPath .githooks`; CI runs the same script. See [`checks/README.md`](../../checks/README.md).

## Safety invariants (don't regress these)

<!-- The 2–5 load-bearing rules that, if broken, break the project. Each links its
     decision-note. This is the security/safety slot. -->

**The reliability bar (the promise to the human who is *not* babysitting SLURM).** These
four properties are what make autonomous operation trustworthy; a PR that weakens any of
them is a regression even if every test passes:

- **Never lose results.** State is filesystem-attached and keyed by `run_id` (`run.json`,
  `.bear-harness-status.json`, `endpoint.json`, the artifacts tarball on the shared FS);
  results are lazy and any session reattaches by `run_id`. This is the keystone — see
  [`docs/decision-notes/first-decision.md`](decision-notes/first-decision.md).
- **Fail loud and diagnosable.** No silent zero-output completion — the
  `ZeroSuccessfulCallsError` pattern (a run that made zero successful calls is a failure,
  not a success). A 401 where every act fails silently is the bug this exists to catch.
- **Resumable long jobs** (the training preset, next cycle) — a long run survives
  pre-emption and resumes, it does not restart from zero.
- **Notify on done/fail** — the agent pings when a run finishes or breaks; nobody polls.

**The two environment invariants (an agent violates these first).** These also head
[`CLAUDE.md`](../../CLAUDE.md); they are repeated here because they are the floor under every
other rule:

- **Login nodes are orchestration-only.** Heavy work — image builds, extractions, compiles,
  model serving — goes through `sbatch` (`bbshort` for short jobs). Never run heavy compute
  on a login node. The thick orchestrator that submits `sbatch` directly lives on the login
  node and the agent drives it over SSH (no SSH inside the kernel); see
  [`docs/decision-notes/login-node-orchestrator.md`](decision-notes/login-node-orchestrator.md).
  Never weaken this to make an obstacle go away.
- **Observability is keyed on durable shared-FS artifacts, never on PID liveness.**
  Round-robin login nodes make node-local state (PIDs, `/tmp`, `nohup`) unreliable. Pin SSH
  to a node IP; key every watcher on `run.json` + the status JSON + `sacct` (the post-queue
  fallback) — never on a PID.

**Guardrails are default-deny and govern resources, not science.** A fully autonomous agent
on a shared cluster with a finite budget starts on a tight leash and widens explicitly:
the allowlist caps QoS, walltime, concurrency, and GPU-hours — it never constrains the
human's experimental choices. See
[`docs/decision-notes/default-deny-guardrails.md`](decision-notes/default-deny-guardrails.md).

- Never commit secrets.

<!-- when to expand me: add a "Subpackages" section and per-component routing only when
     a directory becomes independently buildable with its own decisions. Add a numbered
     decision-ID scheme only if thesis-named notes stop disambiguating (large corpus).
     Promote a safety invariant to a `docs/THREAT_MODEL.md` only once the catalogue
     outgrows this section. Default: stay flat. -->
