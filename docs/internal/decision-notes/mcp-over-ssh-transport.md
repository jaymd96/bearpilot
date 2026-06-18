# Reach SLURM only through its CLI over SSH — BlueBEAR has no REST door — and serve two front-ends from one SSH core (an MCP server for the agent, a `--remote` CLI for the human)

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** direction-set 2026-06-14 (W2 transport lane; not yet built)
**Owner:** jamesamd · **Decided:** 2026-06-14, after a source-level study of prior-art SLURM MCP servers
**Applies to:** the W2 transport — how the laptop reaches SLURM (the MCP server + the `--remote` CLI over one `_remote.py` SSH core), and the now-confirmed-closed REST option
**Drives:** `ROADMAP.md` W2 (MCP-over-SSH transport) and its "Things that won't happen" REST fence, the **Transport** primitive in [`PROJECT-VISION.md`](../PROJECT-VISION.md), the REST refusal in `spec-deferrals.md`, and the no-REST gotcha in [`references/bluebear-platform.md`](../../../references/bluebear-platform.md)

---
## Decision

Four linked commitments about the transport, all downstream of one environmental fact — **BlueBEAR exposes no REST door.**

1. **Reach SLURM only through its CLI over SSH** (`sbatch` / `squeue` / `sacct` / `scancel`). There is no `slurmrestd` on BlueBEAR and it issues no SLURM JWT — `ssh <login> 'which slurmrestd; scontrol token'` both fail (confirmed 2026-06-14). So the SSH-CLI seam is **the only path, not merely the chosen one**; a REST front-end is unreachable from our SSH-only vantage.
2. **One SSH core serves two front-ends.** `_remote.py` (the `SshExecutor` over `ControlMaster`, pinned to a node IP) is the single seam; a local **MCP server** is the agent's front-end and a **`--remote` CLI flag** is the human's front-end. Both lower to the same `bear-harness <verb> --json` invocations on the login node; neither re-implements SSH or SLURM access. The MCP module imports only `_remote.py` / `_hosts.py` — **never the kernel** (the no-SSH-in-the-kernel discipline, one layer up).
3. **`ControlMaster` is connection-reuse, not campaign liveness.** What survives a laptop disconnect is the `nohup`'d login-node orchestrator + filesystem-attached state (per [`login-node-orchestrator.md`](login-node-orchestrator.md)), reattachable by `run_id`. The transport may churn freely; the brain lives on the cluster.
4. **The MCP surface is more than verb-tools.** It also exposes discoverability **resources** (allowed QoS / partitions / GPU availability, via `sinfo` / `scontrol`) and guidance **prompts**, so the agent reads valid targets and self-corrects against the guardrail caps *before* it submits. The resource advertises; the default-deny gate ([`default-deny-guardrails.md`](default-deny-guardrails.md)) enforces.

## Why

- **The REST door is closed by the environment, not by us** — recording it as a confirmed, re-checkable fact stops a cold agent (or a future self) re-proposing "just front `slurmrestd`", which is the dominant shape elsewhere (e.g. Duke's <https://gitlab.oit.duke.edu/wjs/slurmmcp>). It is pinned redundantly — here, in [`references/bluebear-platform.md`](../../../references/bluebear-platform.md) (where an agent checks platform facts first), and in the ROADMAP "won't happen" fence — precisely because it is the easiest wrong turn.
- **Prior art validates the SSH-first topology** — <https://github.com/yidong72/slurm_mcp> is exactly this shape (laptop MCP → SSH → SLURM CLI) and runs against an SSH-only cluster today. Our differentiator is that the *brain* lives on the cluster (the orchestrator + FS state), so our transport stays thin — where that repo keeps interactive state in laptop memory and loses it on restart, we keep nothing in the transport and reattach by `run_id`.
- **Two front-ends over one core gives human/agent parity and a single tested seam** — the human `--remote` path and the agent MCP path cannot diverge because they share `_remote.py`. A human can drive the exact path the agent drives when debugging. No prior-art repo studied has this; they are agent-only.
- **Discoverability and guardrails compose** — a resource the agent can read (allowed QoS, free GPUs) lets it pick a valid target and avoid a denied-at-submit round-trip. This is borrowed from Duke's resources/prompts design, married to our default-deny gate.

## The tradeoff (read before relying on it)

Without a REST door we parse human-oriented CLI text (`squeue` / `sacct` / `scontrol`) rather than a typed schema, so the harness **owns the parsers** and must re-verify them when SLURM output shifts. We contain that cost by lifting battle-tested parse shapes from prior art and pinning the SLURM grammar in [`references/slurm-cli.md`](../../../references/slurm-cli.md). The upside is large: zero cluster-side service to stand up, the user's own SSH identity as the auth boundary (every action runs as the real user, like Duke's per-user-JWT principle but over SSH), and a kernel that stays SLURM-CLI-shaped. If BlueBEAR ever exposes `slurmrestd`, REST becomes a *reversible* option behind the same `_remote.py` seam — but that is not today.

## Alternatives considered (steelmanned)

<!-- Each: the proposal, why it was genuinely tempting (steelman it -- no straw men),
     why we didn't, and the FALSIFIABLE condition that would change our minds. -->

- ***A `slurmrestd` REST front-end (the Duke <https://gitlab.oit.duke.edu/wjs/slurmmcp> shape).*** Tempting: a typed schema instead of fragile text parsing, and a clean per-user JWT auth model where every action runs as the real user. Rejected because **BlueBEAR exposes no `slurmrestd` and issues no SLURM JWT** (`which slurmrestd; scontrol token` both fail), so it cannot be reached at all from our SSH-only vantage — closed by the environment, not on the merits. Reconsider only if BlueBEAR ever stands up a reachable `slurmrestd`; the `_remote.py` seam is transport-pluggable, so the door is reversible.
- ***The MCP server *on* the login node, stdio piped over SSH (the <https://github.com/dongwookim-ml/slurm-mcp> shape).*** Tempting: the simplest SSH-only design — no `ControlMaster`, SSH *is* the MCP transport, SLURM CLI is local to the server. Rejected because it ties the server's life to the SSH pipe and leaves no path for the human `--remote` CLI to share the core; our brain-on-cluster + dual-front-end split wants the server on the laptop and the durable state on the cluster. Reconsider if the dual front-end were ever dropped.
- ***`asyncssh` inside the server (the <https://github.com/yidong72/slurm_mcp> shape).*** Tempting: a clean in-process connection object, easy to pin to a node IP, in-process reconnect — proven to work SSH-only. **Not rejected — adopted as the named fallback** if subprocess `ssh` + `ControlMaster` pinning proves fiddly on BlueBEAR's round-robin login nodes. We default to subprocess `ssh` because it gets the human `--remote` CLI over the identical mechanism and takes no async-SSH dependency into a layer we want thin and auditable; we switch to `asyncssh` the day `ControlMaster` pinning fights us.
- ***An agent-only surface (no human CLI).*** Tempting: less to build. Rejected — human/agent parity over one tested core is the point; debugging the agent's path requires a human to drive the very same seam.

## How it's wired

Lands in W2 (MCP-over-SSH), building directly on [`login-node-orchestrator.md`](login-node-orchestrator.md). New laptop-side modules: `src/bear_harness/_remote.py` (the `SshExecutor` over `ControlMaster` pinned to a node IP, orchestrator lifecycle, poll via `ssh cat`) and `src/bear_harness/_hosts.py` (host loader); a thin MCP server module over `_remote.py` exposing verb-tools + discoverability resources + guidance prompts; and a `--remote` flag on `src/bear_harness/_cli.py` sharing the same core. **Zero kernel changes** — no SSH inside the kernel. From the 2026-06-14 study we lift, reimplemented-not-copied, the `salloc --no-shell` + `srun --jobid=` reconnect pattern and the parse shapes (pipe-delimited `squeue -h -o`, array-aware id split, `scontrol`→dict) from <https://github.com/yidong72/slurm_mcp> (which ships **no LICENSE** — patterns only), and the resources/prompts surface + per-user-credential principle from <https://gitlab.oit.duke.edu/wjs/slurmmcp> (MIT). Current build state lives in `lanes.md`, not here.

Verify (the closed door is re-checkable):
```bash
ssh <bluebear-login> 'which slurmrestd; scontrol token'
# both fail today (2026-06-14). If either ever succeeds, the REST option re-opens.
```

## Reversibility

high for the transport mechanism — subprocess `ssh` ↔ `asyncssh`, and CLI ↔ a future REST executor, are localised changes behind the `_remote.py` seam; the front-ends and the kernel do not move. The *no-REST fact* itself is an external environmental constraint, reversible only if BlueBEAR changes — but the design is positioned so that change costs only a new executor, not a rewrite.

## Reversal path (if it comes to that)

If BlueBEAR exposes `slurmrestd` and issues JWTs: add a REST executor behind the `_remote.py` seam as an alternative transport (the Duke shape becomes reachable); the MCP surface, the `--remote` CLI, and the kernel stay put. If `ControlMaster` pinning proves unreliable on the round-robin login nodes: swap the `_remote.py` exec core to `asyncssh`; nothing above it moves. Do not build either speculatively — build it the day the trigger fires.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Implemented B1+B2 (code-complete, cluster-unverified): `_hosts.py` + `_remote.py` (injectable SSH seam, `ControlMaster`, `nohup` detached launch, `ssh cat` reattach, pointer-file `RemoteRun`, node-pinned `cancel` that never polls a PID) + `--remote` / `fetch` / `ps` / `remote install` on the CLI + a `caps --json` verb + the `bear-harness-mcp` server on the official `mcp` SDK (7 tools + `bear://guardrails/allowed` + 2 prompts; an AST import-guard test proves it imports only `_remote` / `_hosts`, never the kernel). `mcp` is an optional `[mcp]` extra so the cluster-side CLI stays lean. 327 tests green, ruff clean, plus an `ssh localhost` integration test. The live "free GPUs / partitions" resource is deferred (needs `sinfo` / `scontrol` parsers + a cluster); the denied-before-submit + nohup-survives-disconnect proof is the user-side `bbshort` canary. Status in `../lanes.md`.
- **2026-06-14** — Decided after a source-level study of three prior-art SLURM MCP servers. Confirmed BlueBEAR has no REST door (`which slurmrestd; scontrol token` both fail), closing the `slurmrestd` / Duke option. Committed to the SSH-CLI seam with one `_remote.py` core serving an MCP agent front-end + a `--remote` human front-end; subprocess `ssh` + `ControlMaster` as default with `asyncssh` as the named fallback; the MCP surface gains discoverability resources + prompts. Builds on [`login-node-orchestrator.md`](login-node-orchestrator.md).
