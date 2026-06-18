# CLAUDE.md — guidance for agents working with bearpilot

This repo is **bearpilot**: a Claude Code plugin + the `bear-harness` engine for running jobs on
the University of Birmingham **BlueBEAR** SLURM cluster. If you're helping someone *use* it, lean on
the plugin's skills (`bearpilot/skills/`) and [`docs/GETTING-STARTED.md`](./docs/GETTING-STARTED.md).
Three rules about this cluster you'll trip over first:

## Login nodes are orchestration-only

**Never run heavy compute on a BlueBEAR login node.** Image builds, extractions, compiles, and
model serving go through `sbatch` (use `bbshort` for short jobs) — the login node only submits and
watches. The whole design rests on this: a thick orchestrator on the login node submits sbatch
directly and the agent drives it over SSH (no SSH inside the engine). Platform notes:
[`docs/bluebear.md`](./docs/bluebear.md); rationale for contributors:
[`docs/internal/decision-notes/login-node-orchestrator.md`](./docs/internal/decision-notes/login-node-orchestrator.md).

## Observability — trust shared-FS artifacts, never PID liveness

**Pin SSH to a node, and key every watcher on durable shared-filesystem artifacts + `sacct` —
never on a PID.** BlueBEAR's login nodes are round-robin, so node-local state (PIDs, `/tmp`,
`nohup`) is unreliable across reconnects. A watcher that polls a PID will lie to you; one that polls
`run.json` + `.bear-harness-status.json` + `sacct` is durable and reattachable by run id. The proven
loop is in [`docs/runbooks/validation.md`](./docs/runbooks/validation.md).

## Cite the cribs, don't recall

Before emitting any call to an external surface this project drives — the **SLURM CLI**
(`sbatch`/`squeue`/`sacct`/`scancel`), the **vLLM serve API**, the **Anthropic Messages API**, or
any **BlueBEAR platform string** (QoS tier, GPU GRES, module name) — read the matching crib under
[`references/`](./references/) (index: [`references/00-index.md`](./references/00-index.md)) and cite
it by path. These surfaces are version-specific and your training is stale relative to the pinned
snapshot. Real bugs already hit here (vLLM routes are `/v1/...` not `/v1/v1/...`; its `--api-key`
wants `Authorization: Bearer`, not `x-api-key`; the pipeline edge is SLURM `after:`, not `afterok:`).
And **verify any SLURM/vLLM/GRES change on a real `bbshort` run** — CI can't prove cluster behaviour.

---

Using the tool → [`docs/GETTING-STARTED.md`](./docs/GETTING-STARTED.md). Design rationale, the formal
job contract, and how to contribute → [`docs/internal/`](./docs/internal/).
