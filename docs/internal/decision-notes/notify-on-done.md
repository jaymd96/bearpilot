# Notify on done/fail is opt-in and fire-and-forget — a run's success never depends on whether its notification was delivered

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** implemented 2026-06-14 (W2 Lane C1; detached-path firing deferred to Lane C2)
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the W2 notify layer — terminal-transition notification (`command` / `webhook` backends), the `[notify]` config section
**Drives:** `ROADMAP.md` W2 (guardrails + MCP-over-SSH + notify), the reliability bar ("notify on done/fail") referenced from [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`PROJECT-VISION.md`](../PROJECT-VISION.md)

---
## Decision

Notify answers the reliability bar "ping me when a run finishes or fails" so nobody babysits SLURM. Two decisions shape it:

1. **Opt-in, not default-on.** An absent `[notify]` section sends nothing. This is the deliberate INVERSE of [`default-deny-guardrails`](default-deny-guardrails.md): a guardrail that is absent must still *deny* (safety), but a notifier that is absent must stay *silent* — a notification is a convenience, and firing nothing is its safe default. There is no harm in not notifying, only noise in notifying by surprise.

2. **Fire-and-forget, always.** A misconfigured or failing backend (a webhook that 500s, a command that exits non-zero or hangs) is logged and swallowed — never raised into the run, never allowed to block it past a per-backend timeout. **A run's terminal state is computed and persisted independently of whether its notification was delivered.** The invariant is encoded at the kernel boundary (the terminal-transition helper swallows any notifier exception), not left to the discipline of each backend.

Two harness-side backends: `command` (an argv, with `{event}` / `{run_id}` / `{state}` / `{run_dir}` / `{model}` / `{error}` placeholders and the same fields as `BEAR_NOTIFY_*` env vars) and `webhook` (a JSON `POST`). **Email is not a harness backend** — SLURM already sends native job-event email via `[slurm].mail_user` / `mail_events`; the harness does not grow an SMTP client.

## Why

- **Reliability bar, not a feature** — "notify on done/fail" is one of the four reliability requirements the project commits to (see [`PROJECT-VISION.md`](../PROJECT-VISION.md)). The whole point of unbabysat autonomy is that nobody is watching `squeue`; a terminal ping is how the human re-enters the loop without polling.
- **Fire-and-forget protects the result** — the cardinal sin would be a notification failure that derailed a completed run's terminal handling. Notification is downstream of, and subordinate to, the run outcome, so it is encoded that way: the kernel swallows any notifier exception at its own boundary. This is the "encode constraints, don't rely on discipline" rule applied to a reliability invariant.
- **Opt-in matches reality and avoids surprise** — defaulting notification *on* begs the question "to what channel, whose webhook?" There is no safe universal default; opt-in is the honest one. Today's single user configures it once.
- **Email via SLURM, not SMTP** — reuse the platform's native mechanism instead of dragging credentials and an SMTP dependency into the harness. Fewer moving parts, and no second email path to keep consistent.

## The tradeoff (read before relying on it)

Fire-and-forget means a *silently undelivered* notification: if your webhook is misconfigured, the run still completes correctly but you may never hear about it. We accept that deliberately — the alternative (a notification failure that blocks or fails a run) is strictly worse. The outcome is not lost: `run.json` `notes.notify` records `fired` / `errors`, so a missing ping is diagnosable after the fact; it is simply never a run-breaker. Mitigate by checking that record (or the `--json` handle's `notify` field) after the first run with a new backend.

## Alternatives considered (steelmanned)

<!-- Each: the proposal, why it was genuinely tempting (steelman it), why we didn't,
     and the FALSIFIABLE condition that would change our minds. -->

- ***Default-on notification.*** Tempting because "it just works out of the box" and a user can't forget to enable it. Rejected because there is no safe universal default channel — defaulting on would either no-op (no channel) or spam (someone's stale webhook), and a surprise notification is worse than a silent absence. Would reconsider if a single obvious per-user channel ever existed (it doesn't on a shared HPC login node).
- ***Block the run until the notification is acknowledged (at-least-once delivery).*** Tempting for "never miss a ping". Rejected because it inverts the dependency: a flaky webhook would hold a *finished* run hostage and burn the very GPU-hours the guardrails protect. The run outcome must never wait on a side-channel. Would reconsider only if a delivery guarantee were ever needed for a *correctness* reason — it isn't; notification is advisory.
- ***A harness SMTP / email backend.*** Tempting for parity with the original plan's "command / webhook / email" list. Rejected because SLURM already emails on `BEGIN,END,FAIL` via `mail_user`; a second email path would duplicate it and pull credentials + an SMTP dependency into the harness. The backend seam stays open if a harness-level email is ever genuinely needed — but the default answer is "use SLURM's".

## How it's wired

Implemented in W2 Lane C1: `[notify]` → `NotifyConfig` (opt-in; `_bear_config.py`), the pure `fire_notification` engine with injected `command` / `webhook` seams (`_notify.py`), and the kernel firing at the **blocking-path** terminal transition and the early-failure path (`_launch.py` `_notify_terminal`), with the outcome surfaced in `run.json` `notes.notify` and the `--json` handle. The **detached-path** terminal — a deploy that returned a handle whose pipeline finishes off-process minutes later — is **deferred to Lane C2**: it belongs to the `nohup`'d login-node orchestrator's `sacct`-backed tail (never PID liveness; see [`login-node-orchestrator`](login-node-orchestrator.md) and the observability discipline in [`CLAUDE.md`](../../../CLAUDE.md)), and it will reuse this same engine — build a `NotifyEvent` from the observed terminal state and call `fire_notification`. Current build state lives in `lanes.md`, not here.

Verify: a configured backend fires exactly once at done / failed; a backend that raises leaves `final_state` unchanged (the run still completes); an absent `[notify]` leaves `notify` null in the handle and no `notes.notify` in `run.json`.

## Reversibility

high — notify is config plus one isolated module. Silencing it is removing the `[notify]` section (no code change); the engine has no callers but the two terminal transitions, and the kernel treats a null / skipped outcome as the normal case.

## Reversal path (if it comes to that)

Remove the `[notify]` section to silence it. To remove the feature entirely, delete the `_notify_terminal` calls in `_launch.py` and the `_notify.py` module — nothing else depends on them. The one property not to reverse casually is the fire-and-forget swallow at the kernel boundary: that is what guarantees notify can never break a run.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Decided + implemented (W2 Lane C1). Notify is opt-in (the inverse of guardrails' default-deny) and fire-and-forget (swallowed at the kernel boundary, encoded not trusted). Backends: `command` + `webhook`; email delegated to SLURM `mail_user`. Blocking-path firing landed and green; detached-path firing deferred to Lane C2 (the login-node orchestrator's `sacct` tail). Outcome surfaced in `run.json` `notes.notify` + the `--json` handle.
