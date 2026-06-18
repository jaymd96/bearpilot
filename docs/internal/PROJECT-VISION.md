# bear-harness -- what this project is

> bear-harness is a control plane that lets an LLM agent autonomously deploy and run **human-designed** workloads on the University of Birmingham's BlueBEAR SLURM cluster, through a small preset programming model. It is a reliable **lab tool** for a small research group on **one cluster** -- it is deliberately **not** a portable, multi-cluster, scheduler-agnostic platform, and it does not design the science.

---
## The whole loop, in one sentence

**From a laptop, the agent submits a **JobGraph** over key-based SSH to a thick **login-node orchestrator** -> the preset-agnostic **kernel** realises it under a **preset** on SLURM, within default-deny guardrails, autonomously -> results come back, reattachable by **run_id**, with nobody babysitting SLURM.**

## The whole loop, in one paragraph

The agent runs on a laptop and never touches SLURM directly. It speaks to a local **MCP** server (the **transport**) which holds a key-based SSH connection -- multiplexed over a `ControlMaster` socket -- to a BlueBEAR login node. On that login node sits a **kernel**: the preset-agnostic control plane that submits `sbatch` jobs directly, so no SLURM-touching code and no SSH ever live inside the kernel's logic. The agent's unit of work is a **JobGraph** -- the contract of jobs, edges (`after`/`afterok`), and `publishes`/`consumes` records that downstream jobs read off the shared filesystem. A **preset** is the human- or LLM-authored unit that targets that contract; the reference preset is `vLLM+pipeline` (a server job publishes its endpoint, a worker job consumes it, the server carries `role=sidecar` so it is `scancel`-ed once its consumers finish). Deploy is **detached**: the kernel returns a **run_id** handle in seconds rather than blocking on the run. Because every piece of state -- `run.json` (the harness state machine), `.bear-harness-status.json` (the program heartbeat), `endpoint.json`, and the artifacts tarball -- is **filesystem-attached** on the shared FS and keyed by `run_id`, any session, even a brand-new one, can reattach by `run_id` to read status, stream logs, and pull results lazily. The keystone is that the kernel honours the JobGraph contract and nothing more: it does not know what a preset computes.

## The whole loop, as a diagram

```
  LAPTOP                          LOGIN NODE (orchestration-only)              COMPUTE
 ┌────────────┐                  ┌──────────────────────────────────┐       ┌──────────┐
 │  LLM agent │                  │  KERNEL  (preset-agnostic)        │       │ SLURM    │
 │     │      │   MCP over       │  ┌────────────────────────────┐  │ sbatch│ jobs     │
 │  ┌──▼───┐  │   key-based SSH  │  │ realise JobGraph under a    │  │──────▶│ ┌──────┐ │
 │  │ MCP  │──┼──────────────────┼─▶│ PRESET  (e.g. vLLM+pipeline)│  │       │ │server│ │
 │  │server│  │  (ControlMaster  │  │  ──── the kernel<->preset   │  │◀──────│ │ +    │ │
 │  └──┬───┘  │   multiplexed)   │  │       wire is the JobGraph  │  │ squeue│ │worker│ │
 │     │      │                  │  └────────────┬───────────────┘  │ sacct │ └──┬───┘ │
 │  run_id ◀──┼──────────────────┼───────────────┤ detached deploy  │       └────┼─────┘
 └────────────┘   handle back    └───────────────┼──────────────────┘            │
                  in seconds                      │ filesystem-attached state     │
                                          ┌───────▼────────────────────────┐     │
                                          │ SHARED FS  (keyed by run_id)    │◀────┘
                                          │  run.json · .bear-harness-      │
                                          │  status.json · endpoint.json ·  │
                                          │  artifacts tarball              │
                                          └─────────────────────────────────┘
        reattach by run_id ───────────────────────▲  any session, even a new one
```

---
## The core substrate commitment (and what it forces)

The kernel is **preset-agnostic**: it honours the JobGraph contract -- jobs, edges, and `publishes`/`consumes` records -- and it does **not** know what any preset computes. One control plane serves every topology (single / bundle / coupled / dag); presets are an open extension point; `vLLM+pipeline` is just the reference implementation, not a special case baked into the kernel. This is the keystone decision -- see [`decision-notes/first-decision.md`](decision-notes/first-decision.md) -- and it is the contract defined in [`specs/01-foundational-contract.md`](specs/01-foundational-contract.md).

**Consequence for the forward plan:** never add a shortcut that lets the kernel reach past the JobGraph into preset-specific behaviour. A new workload is a new preset that targets the contract -- it is never a special case threaded into the kernel. When W3 extracts the contract from the working vLLM flow, the change must be behaviour-preserving precisely *because* the kernel was never allowed to depend on what vLLM does.

---
## What bear-harness is (and is not)

**It is.** A BlueBEAR-only control plane that gives an LLM agent fully autonomous **operation** -- deploy, schedule, recover, collect, notify, iterate on a running experiment -- of workloads a **human** designed, all within default-deny guardrails. The JobGraph contract abstracts *workloads*, not schedulers.

**It is not** (per [`specs/01-foundational-contract.md`](specs/01-foundational-contract.md)):

- **Multi-cluster or scheduler-agnostic.** Portability is an explicit non-goal; the contract abstracts the workload, not the scheduler, so there is no scheduler-abstraction tax. See [`decision-notes/bluebear-only.md`](decision-notes/bluebear-only.md).
- **Reached over a REST API.** BlueBEAR exposes no `slurmrestd`, so bear-harness drives SLURM only through its CLI over SSH; the "MCP server fronts slurmrestd" shape common elsewhere is closed *for this cluster*. See [`decision-notes/mcp-over-ssh-transport.md`](decision-notes/mcp-over-ssh-transport.md).
- **Agent-designed science.** Autonomy is in *operation*, not *science*. The human owns the hypothesis, the experimental design, and the choices; the agent executes what the human designed and only iterates on the *running* of it.
- **An open platform or product (yet), and never login-node heavy compute or unbounded GPU-hours.** It is a lab tool for a small research group; heavy work goes through `sbatch` only, and guardrails cap resources by default.

---
## The architectural inheritance

| From | What we adopt |
|---|---|
| **BlueBEAR / SLURM** | The cluster, its QoS tiers (`bbshort`/`bbgpu`/`bbcpu`), GPU GRES, the shared RDS filesystem, the module system, and Apptainer -- taken as given and optimised for, not abstracted away. |
| **bear-harness adds** | The preset-agnostic kernel + the JobGraph contract: detached deploy, filesystem-attached state, reattach-by-`run_id`, default-deny guardrails, MCP-over-SSH transport, and a declarative preset authoring kit. |

---
## The primitives

<!-- The coined-vocabulary glossary every other doc cites. Keep it to one line per term. -->

| Primitive | What it is |
|---|---|
| **Kernel** | The preset-agnostic control plane on the login node: detached deploy -> `run_id` handle -> status/logs/results, all filesystem-attached so any session reattaches by `run_id`. Honours the JobGraph contract; does not know what a preset computes. |
| **JobGraph** | The contract: jobs + edges (`after`/`afterok`) + `publishes`/`consumes` (a job drops a typed record on the shared FS; a downstream job reads it as env) + `role=sidecar`. Topologies: single / bundle (SLURM array) / coupled (server+worker) / dag. |
| **Preset** | A human- or LLM-authored unit that targets the JobGraph contract. `vLLM+pipeline` is the reference preset (server publishes the endpoint, worker consumes it, `role=sidecar`); ETL is the second, GPU-free, server-free preset. |
| **Handle / run_id** | The opaque key returned by a detached deploy. Every artifact on the shared FS is keyed by it, so reattachment is just "give me the `run_id`". |
| **Transport** | One SSH core (`_remote.py`, the `SshExecutor` over `ControlMaster`) to a login node, behind two front-ends -- an **MCP server** for the agent, a **`--remote` CLI** for the human -- both lowering to `bear-harness <verb> --json` on the login node. SLURM is reached only via its CLI over SSH (BlueBEAR has no REST door); no SSH inside the kernel. See [`decision-notes/mcp-over-ssh-transport.md`](decision-notes/mcp-over-ssh-transport.md). |
| **Sidecar** | A server job (`role=sidecar` in the JobGraph) that is `scancel`-ed automatically once its consuming jobs finish -- so a vLLM server never outlives the worker it serves. |
| **State** | `run.json` (harness state machine) + `.bear-harness-status.json` (program heartbeat) + `endpoint.json` + artifacts tarball -- all on the shared FS, keyed by `run_id`. |

---
## What this repo implements

| Layer | Status (audited 2026-06-14) |
|---|---|
| Kernel -- detached deploy + `--json` + reattach-by-`run_id` (W1) | In progress (deploy slice code-complete). `_launch.py` has the `detach` parameter, `LaunchResult.as_dict()`, and the post-probe cut; `_cli.py` wires `--detach`/`--json`. Green: `TestDetach`, `TestLaunchResultSerialisation`, `tests/test_cli.py` (11 pass, ruff clean). Left for W1: the lazy `results` verb and the real `bbshort` validation run. |
| Guardrails + MCP-over-SSH + notify (W2) | Not started -- default-deny QoS allowlist / walltime ceiling / concurrency cap / dry-run gate; the `SshExecutor` MCP server; notify-on-done. |
| JobGraph contract extracted from the vLLM flow (W3) | Not started -- behaviour-preserving extraction; the vLLM flow becomes the reference preset; the kernel becomes formally preset-agnostic. |
| ETL preset + declarative authoring kit (W4) | Not started -- second preset plus `validate_preset` / `dry_run` / `describe_preset` / `list_presets`. |

What's genuinely left (see `lanes.md`): W1's detached-deploy slice is code-complete and green, but W1 is not *done* -- the lazy `results` verb and the real `bbshort` validation run remain, and a green suite is not the gate. Everything from guardrails onward (W2-W4) is unstarted. There are no guardrails yet, no MCP transport yet, and the JobGraph contract is still implicit inside the vLLM flow rather than extracted.

---
## The arc beyond V1 (the next cycle: depth, bounded)

V1 (W1->W4) makes the kernel durable, safe, and preset-agnostic. The **next cycle is
depth of autonomous operation, not breadth** -- the same closed JobGraph contract
exercised harder, never a new cluster and never hands-off science. Concretely it is three
more presets over the unchanged contract plus a second authoring *form* -- each already
named and deferred-with-a-trigger in `spec-deferrals.md` and
sequenced as a gated phase in `ROADMAP.md`. Nothing here is a contract
change; that is precisely why it is cheap to defer -- the keystone
([`decision-notes/first-decision.md`](decision-notes/first-decision.md)) makes presets an
open extension point, and the horizon's "depth, bounded -- not breadth" framing lives in
[`decision-notes/depth-not-breadth-next-cycle.md`](decision-notes/depth-not-breadth-next-cycle.md).

| Depth theme (next cycle) | The named work | JobGraph shape |
|---|---|---|
| Multi-run orchestration | **sweeps** preset | `bundle` (SLURM array) |
| Coupled fan-out at scale | **eval** preset | `coupled`, many workers |
| Resumable long jobs (a reliability-bar promise) | **training+resume** preset | `dag` + checkpoint plumbing (BLOCKED) |
| Richer / safer authoring | **Python-builder** form behind a sandbox | unchanged -- lowers to the same data |

This is the whole arc -- there is no multi-year north-star beyond it. Breadth
(multi-cluster, agent-designed science, an open product) stays a permanent **refusal**,
not a deferred horizon -- see `ROADMAP.md`, "Things that won't happen".

## The single most important invariant

**The kernel honours the JobGraph contract and nothing else: it never depends on what a preset computes.** Every other property the design promises -- one control plane for all topologies, presets as an open extension point, a behaviour-preserving W3 extraction -- follows from this. If the kernel ever needs to know it is running vLLM, the keystone has cracked. Full statement and reversibility in [`decision-notes/first-decision.md`](decision-notes/first-decision.md).

## When this model is wrong

This map describes the *intended* contract; the repo has not finished realising it. Stop trusting the map and read the code when:

- **You are reading W1-era code.** The JobGraph contract is not yet extracted -- as of W3 it is still implicit inside the vLLM flow. Today's `src/bear_harness/_launch.py` is closer to "a vLLM launcher with a detach flag" than to "a preset-agnostic kernel". The clean separation is a target, not a current fact.
- **You hit the detached-deploy timing.** Deploy returns after the *vLLM probe*, not after both jobs are submitted, because the pipeline command bakes the endpoint URL at submit time (`$MODEL_BASE_URL` substitution in `_pipeline_launcher.py`). Instant-return is a later optimisation; see [`decision-notes/detached-deploy-cut-after-probe.md`](decision-notes/detached-deploy-cut-after-probe.md).
- **You assume node-local state is reliable.** Round-robin login nodes make PIDs, `/tmp`, and `nohup` untrustworthy. Every watcher keys on shared-FS artifacts + `sacct`, never on PID liveness. If your mental model relies on a process staying put, it is wrong here.

---
## Where to read next

| Goal | Document |
|---|---|
| **Build next** | `ROADMAP.md` |
| **Current state** | `lanes.md` |
| **Operate it** | [`runbooks/`](../runbooks/) |
| **The contract** | [`specs/00-reading-guide.md`](specs/00-reading-guide.md) -> [`specs/01-foundational-contract.md`](specs/01-foundational-contract.md) |
| **The keystone decision** | [`decision-notes/first-decision.md`](decision-notes/first-decision.md) |
| **The scope fence** | [`decision-notes/bluebear-only.md`](decision-notes/bluebear-only.md) |
| **External surfaces (cite, don't recall)** | [`references/00-index.md`](../../references/00-index.md) |

<!-- + a `DEPLOYMENT.md` "Run it" row once that reserved genre exists (the from-scratch stand-up).
     The `runbooks/` directory is scaffolded; `DEPLOYMENT.md` is reserved and appears on trigger. -->

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change. LIVING doc, but this log is append-only. -->

- **2026-06-14** -- Recorded the transport shape + the closed REST door: SLURM is reached only via its CLI over SSH (BlueBEAR exposes no `slurmrestd`), and one `_remote.py` SSH core serves two front-ends (an MCP server for the agent, `--remote` for the human). Sharpened the **Transport** primitive and added the "not reached over a REST API" fence; rationale + prior-art credits in [`decision-notes/mcp-over-ssh-transport.md`](decision-notes/mcp-over-ssh-transport.md). No scope or status change.
- **2026-06-14** -- Added "The arc beyond V1": named the next-cycle depth themes (sweeps / eval / training+resume / Python-builder), each tied to its deferred preset and its gated `ROADMAP.md` Tier C phase, with the horizon rationale in [`decision-notes/depth-not-breadth-next-cycle.md`](decision-notes/depth-not-breadth-next-cycle.md). Promotion + naming only -- no new scope, no status counts (those stay in `lanes.md`).
- **2026-06-14** -- First fill of the vision from the facts pack: keystone preset-agnostic-kernel commitment ([`decision-notes/first-decision.md`](decision-notes/first-decision.md)), BlueBEAR-only scope fence ([`decision-notes/bluebear-only.md`](decision-notes/bluebear-only.md)), primitives glossary, and the W1 in-progress / W2-W4 unstarted status (see `lanes.md`).

<!-- when to expand me: split the optional MODEL sections (layered stack, when-wrong,
     operator decision points) into a dedicated docs/MODEL.md only if this file grows past
     ~1 screen of synthesis. Until then keep the map and the vision in one place. -->
