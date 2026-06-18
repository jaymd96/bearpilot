# A thick orchestrator runs on the login node; the agent drives it over SSH (no SSH inside the kernel)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** direction-set 2026-06-14
**Owner:** jamesamd · **Decided:** 2026-04-09 (Phase E, recorded in `docs/next-steps.md`); captured as a decision-note 2026-06-14
**Applies to:** the remote-control story — Phase E, the laptop ↔ login-node split, and the transport (MCP-over-SSH)
**Drives:** `ROADMAP.md` W2 (MCP-over-SSH transport), the orchestration-only invariant in [`CLAUDE.md`](../../../CLAUDE.md), and the new files planned in `docs/next-steps.md` (`_hosts.py`, `_remote.py`, remote `_cli.py` subcommands)

---
## Decision

The full `bear-harness launch` orchestrator runs **on the BlueBEAR login node**, owning the entire SLURM lifecycle, and the agent's laptop is a pure remote control that drives it over key-based SSH. The SLURM-touching code stays on the login node; **there is no SSH inside the kernel** — the kernel submits `sbatch` directly, locally, and the transport (a local MCP server over SSH with `ControlMaster` multiplexing) is a separate seam the laptop uses to talk to that login-node orchestrator. This is "Option B — thick remote, nohup'd orchestrator", chosen over "Option A — laptop orchestrates every shell call over SSH". The exact rationale is recorded in `docs/next-steps.md` §2.

**The orchestrator lives where the scheduler is, and the laptop only points at it** unless BlueBEAR's reap policy stops permitting long-running login-node processes — in which case the same binary runs as a tiny `bbcpu` sbatch (the documented fallback), with the laptop code unchanged.

## Why

- **The laptop must be allowed to disconnect** — a campaign can run for hours. With Option A (laptop orchestrates everything via `ssh bluebear <argv>` per call, files rsync'd lazily), if the laptop closes, loses wifi, or the SSH `ControlMaster` socket expires mid-campaign, the orchestrator dies and vLLM burns GPU-hours to walltime with no one to `scancel` it. A thick login-node orchestrator under `nohup` survives the laptop closing at any point. This is the decisive reason in `docs/next-steps.md`.
- **It was empirically gated, not assumed** — Option B was only adopted after verifying BlueBEAR permits multi-hour `nohup`'d login-node processes (the recorded test: `ssh bluebear && nohup sleep 7200 & disown && exit`; the process was still `ALIVE` an hour later, 2026-04-09). Without that gate we'd have fallen back to the orchestrator-as-sbatch pattern. This is exploration-then-crystallise: run the experiment, don't reason about it.
- **No SSH inside the kernel keeps the seam clean** — the SLURM runner, `_launch.py`, the vLLM/pipeline launchers, and the sbatch templates know nothing about a laptop. The remote-side binary is the *existing* `bear-harness launch`, unchanged; the remote doesn't know or care a laptop is talking to it. The transport is additive (new `_hosts.py` / `_remote.py` + a `--remote` flag), so the cluster-side code carries zero remote-control complexity.
- **It respects the orchestration-only invariant** — the login node only *submits and watches*; the heavy compute (model serving) is on GPU nodes via `sbatch`. A thick orchestrator on the login node is orchestration, not heavy compute, so it's exactly what the login node is *for*. See [`CLAUDE.md`](../../../CLAUDE.md) and [`bluebear-only.md`](bluebear-only.md).
- **State stays filesystem-attached and reattachable** — the laptop tracks each remote run by a tiny pointer file (host + remote run dir + orchestrator PID); everything else is derived by `ssh cat`'ing `run.json` and the status file in the run dir. No state is duplicated, and reattach-by-`run_id` works from any laptop. This is the observability discipline made concrete: watchers key on shared-FS artifacts + `sacct`, never on PID liveness (see [`CLAUDE.md`](../../../CLAUDE.md)).

## The tradeoff (read before relying on it)

We depend on a BlueBEAR policy we don't control: that multi-hour `nohup`'d login-node processes are permitted. It's verified today, but it's a site policy that could change, and a long-lived login-node process is a slightly unusual citizen on a shared login node. We accept that because the alternative (Option A) makes the laptop a single point of failure for a multi-hour GPU campaign, and because the fallback is cheap and already specified. The orchestrator PID in the pointer file is also node-local state on round-robin login nodes — which is exactly why liveness is judged from shared-FS artifacts + `sacct`, not from polling that PID.

Escalate / reconsider when BlueBEAR's reap policy changes and long-running login-node processes stop being permitted — then switch the startup path to the `bbcpu` sbatch fallback (below); the laptop code does not move.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Option A — laptop orchestrates everything via `ssh bluebear <argv>` per shell call, files rsync'd lazily.*** Tempting because it needs *nothing* installed on the login node, keeps all orchestration logic on the well-understood laptop, and is conceptually the thinnest possible remote story. Rejected because it's fragile for long runs: if the laptop closes, loses wifi, or the SSH `ControlMaster` socket expires mid-campaign, the orchestrator dies and vLLM burns GPU-hours to walltime with no one to `scancel` it (verbatim from `docs/next-steps.md`). Would reconsider only if campaigns became short enough that a laptop staying connected throughout were a safe assumption — which the multi-hour 2,700-run campaign disproves.
- ***Orchestrator-as-sbatch — run the same `bear-harness launch` binary inside a tiny `bbcpu` sbatch instead of under `nohup`.*** Tempting because it sidesteps the login-node longevity question entirely: SLURM owns the orchestrator's lifetime, so no reap-policy dependency. Rejected *as the default* only because the `nohup` gate passed, and `nohup` is simpler (no extra partition, no sbatch wrapper, faster startup); it is kept as the documented fallback (~30 extra lines, same code, same artefacts) if the reap policy ever changes. Would adopt immediately if multi-hour `nohup`'d login-node processes stopped being permitted — this is the codified reversal path, not a rejection on the merits.

## How it's wired

Direction set 2026-04-09 in `docs/next-steps.md` §2; the transport lands in W2 (MCP-over-SSH) per `ROADMAP.md`. The planned new files (laptop-side, ~500 lines total) are `src/bear_harness/_hosts.py` (the `~/.config/bear-harness/hosts.toml` loader), `src/bear_harness/_remote.py` (SSH exec wrapper, rsync push/pull, orchestrator lifecycle via `nohup`, poll via `ssh cat`, cancel via `ssh kill` + `ssh scancel`), and remote subcommands on `src/bear_harness/_cli.py` (`remote install` + a `--remote <host>` flag on `launch`/`status`/`logs`/`cancel`, plus `fetch` / `ps`). **Zero changes** to `_slurm_runner.py`, `_launch.py`, `_vllm_launcher.py`, `_pipeline_launcher.py`, or the sbatch templates — the no-SSH-in-the-kernel guarantee. SSH networking (keys, jumphosts, `ControlPersist`, 2FA) is delegated to `~/.ssh/config` via an `ssh_alias` indirection; the harness does not reinvent SSH config. Current build state lives in `lanes.md`, not here.

Verify (the gate that made this viable, re-runnable): confirm BlueBEAR still permits long-running login-node processes —
```bash
ssh bluebear 'nohup sleep 7200 >/dev/null 2>&1 & disown; echo started $!'
# ... reconnect later ...
ssh bluebear 'ps -p <pid> -o stat= || echo REAPED'
```
If this is `REAPED`, switch to the sbatch fallback before relying on remote launches.

## Reversibility

medium — switching to the sbatch fallback is a localised change to the orchestrator startup path only (`ssh host nohup ...` → `ssh host sbatch orchestrator.sbatch`); the laptop code and all artefacts stay the same. It's medium rather than high because it depends on a site policy we don't control, so the *trigger* for reversal is external, even though the *mechanism* is small.

## Reversal path (if it comes to that)

If BlueBEAR's reap policy stops permitting long-running login-node processes: degrade to the orchestrator-as-sbatch pattern — a tiny `bbcpu` sbatch that runs exactly the same `bear-harness launch` binary. The laptop code is unchanged; only the startup path changes from `ssh host nohup ...` to `ssh host sbatch orchestrator.sbatch`. Load-bearing on the way out: the pointer-file / `ssh cat` observability model still works because it keys on shared-FS artifacts + `sacct`, not on the orchestrator's PID — so the only thing that moves is *how the orchestrator is started*, not how it's watched. Do not build this fallback speculatively (per `docs/next-steps.md`); build it the day the policy changes.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Captured the Phase E "thick remote orchestrator" decision (originally recorded 2026-04-09 in `docs/next-steps.md` §2) as a standalone decision-note, faithfully preserving Option A's rejection, the verified `nohup` gate, and the `bbcpu` sbatch fallback. `docs/next-steps.md` is the predecessor source and is superseded-by this note for the *decision* (it remains the live forward-plan doc).

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
