# bear-harness Specification Reading Guide

**Spec 00 -- Navigating the JobGraph-contract corpus**

<!-- LIVING. The cross-reference hub of the spec tree. Read this first.
     Absorbs the glossary as its primitive index (§4) unless term collisions grow severe. -->

---
*Updated June 2026. What's in the spec family, how the pieces fit, what supersedes what, and the reading orders. Read this first.*

Two things up front:
**Addendums are authoritative for their topic** over the base contract.
**Some primitives may be superseded** -- flagged where they appear.

The bear-harness spec corpus exists to pin **one thing**: the **JobGraph contract** -- the closed vocabulary the preset-agnostic kernel honours, and the only thing it honours. This is the keystone commitment recorded in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md). Everything else in the repository (the runner, the cribs, the guardrails, the presets) hangs off that contract; the specs are where the contract itself is written down so presets and the kernel can be built against a shared, citable surface rather than against each other's current code.

## 1. The corpus at a glance

```
00 reading-guide          · this file -- the map, the reading orders, the primitive index
01 foundational-contract  · THE JOBGRAPH CONTRACT (DRAFT, extracted in W3) -- jobs, edges,
                            publishes/consumes, role=sidecar, the four topologies, the
                            kernel<->preset wire
```

Today the corpus is two files: this guide and the one base contract. The contract is a **DRAFT** -- it specifies the *target* the W3 extraction must hit (lift the JobGraph out of the working vLLM flow behaviour-preservingly), not code that exists yet. New subsystems get their own numbered spec as they are extracted; they do not become sections of Spec 01.

## 2. Specs

| Spec | Title | Authoritative for |
|---|---|---|
| [`00-reading-guide.md`](00-reading-guide.md) | Specification Reading Guide | Corpus map, reading orders by audience, the primitive index/glossary, what supersedes what |
| [`01-foundational-contract.md`](01-foundational-contract.md) | The JobGraph Contract | The JobGraph data model (jobs, `after`/`afterok` edges, `publishes`/`consumes`, `role=sidecar`), the four topologies (single/bundle/coupled/dag), the kernel<->preset wire (the keystone invariant), and how the reference vLLM+pipeline preset maps onto it |

**Status of Spec 01:** DRAFT -- being extracted in W3. It describes the contract the kernel *will* honour once the JobGraph is lifted out of `src/bear_harness/_launch.py`; it is the design target, **not** an as-built description. See `../docs/ROADMAP.md` (W3) and `../docs/lanes.md` for where the code actually is.

## 3. Reading orders by audience

### Preset author (building a workload to run on bear-harness)
1. **Spec 01** (JobGraph Contract) §3 -- the only surface you target. Express your workload as jobs + edges + publishes/consumes + a role; you never reach into the kernel.
2. **Spec 01** §6 -- the reference vLLM+pipeline mapping. Read this as the worked example before writing your own preset.
3. [`../docs/decision-notes/declarative-presets-first.md`](../decision-notes/declarative-presets-first.md) -- why your preset must reduce to inspectable data validated against caps before submit (the authoring form constraint).

### Kernel contributor (working on `src/bear_harness/` the control plane)
1. [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md) -- the keystone: you honour the contract and nothing else. Read this before touching the realiser.
2. **Spec 01** (JobGraph Contract) §3, §4 -- the data model and lifecycle you must realise across all four topologies with one code path.
3. **Spec 01** §5 -- the type system / wire you must not leak workload knowledge through.
4. `../docs/ROADMAP.md` (W3) -- the extraction that makes the code match Spec 01, and the behaviour-preserving constraint.

### Operator (running campaigns from a laptop, recovering runs)
1. [`../docs/PROJECT-VISION.md`](../PROJECT-VISION.md) §"The whole loop" -- the value loop and the filesystem-attached state keyed by `run_id`.
2. **Spec 01** (JobGraph Contract) §4 -- the lifecycle, so you know what `run.json` / `.bear-harness-status.json` / `endpoint.json` / artifacts mean when you reattach.
3. [`../docs/runbooks/validation.md`](../../runbooks/validation.md) -- the bbshort iteration loop (the highest-value operational procedure: diagnose → TDD fix → relaunch → watch on shared-FS artifacts, never PID).

### Reviewer (deciding whether a change is sound)
1. **Spec 01** (JobGraph Contract) §2 -- the forcing functions; a change that defeats one of these is wrong regardless of how it looks.
2. **Spec 01** §8 -- the comparison against the naive alternatives, so you can spot a regression toward a per-workload or per-topology code path.
3. `../docs/spec-deferrals.md` -- the DEFERRED/REFUSED/BLOCKED fence, so you don't approve scope that the contract deliberately excludes.

## 4. The primitive index

<!-- Doubles as the glossary: every coined term -> its canonical home, and what it is NOT. -->

| Primitive | Canonical spec | What it is | Distinct from |
|---|---|---|---|
| **Kernel** | Spec 01 §5 | The preset-agnostic control plane: detached deploy → `run_id` handle → status/logs/results, all filesystem-attached so any session reattaches by `run_id`. It honours the JobGraph contract and nothing else. | **Preset** -- the kernel does not know what a preset *computes*; it only realises the graph the preset produces. |
| **JobGraph** | Spec 01 §3 | The contract: jobs + edges (`after`/`afterok`) + `publishes`/`consumes` + `role=sidecar`. The closed vocabulary every preset reduces to. | **Preset** -- the JobGraph is the *output* a preset lowers to; a preset is the authored unit, the JobGraph is its data form. |
| **Job** | Spec 01 §3 | One sbatch submission: a unit of work the kernel hands to SLURM with its resources, dependencies, and (optionally) a role. | **Topology** -- a job is one node; the topology is the *shape* the jobs+edges form. |
| **Edge** (`after` / `afterok`) | Spec 01 §3 | An ordering dependency between jobs, realised as an sbatch `--dependency`. `after` = start once the dep leaves the queue; `afterok` = start only if the dep succeeded. | **publishes/consumes** -- an edge orders execution; a publish/consume passes *data*. They are independent and often both present. |
| **publishes / consumes** | Spec 01 §3 | A typed-record handoff: a job drops a record on the shared FS (`publishes`), a downstream job reads it as environment (`consumes`). | **Edge** -- carries data, not ordering. A consumer still needs an edge if it must *wait* for the publisher. |
| **role=sidecar** | Spec 01 §3 | A marker on a server job that makes the kernel `scancel` it once its consumers finish (server lives only to serve its workers). | **A worker job** -- the sidecar is the server that is torn down; the worker is the consumer whose completion triggers the teardown. |
| **Topology** | Spec 01 §3 | The shape a JobGraph takes: **single** / **bundle** (SLURM array) / **coupled** (server+worker) / **dag**. One realiser serves all four. | **Preset** -- many presets can share a topology; the topology is structural, the preset is the workload. |
| **Preset** | Spec 01 §6 | A human- or LLM-authored unit that targets the JobGraph contract. vLLM+pipeline is the *reference* preset; ETL is the second (the de-risker: no GPU, no server). | **Kernel** -- a preset is an open extension point *above* the closed contract; the kernel never grows to accommodate a specific preset. |
| **Transport** | [`../docs/PROJECT-VISION.md`](../PROJECT-VISION.md) §primitives | A local MCP server driving the login-node orchestrator over key-based SSH (ControlMaster multiplexing). The SLURM-touching code stays on the login node; no SSH inside the kernel. | **Kernel** -- the transport carries commands to the orchestrator; the kernel is the orchestration logic itself. Cross-ref: [`../docs/decision-notes/login-node-orchestrator.md`](../decision-notes/login-node-orchestrator.md). |
| **State** (`run.json`, `.bear-harness-status.json`, `endpoint.json`, artifacts) | Spec 01 §4 | The filesystem-attached run state under the shared FS, keyed by `run_id`: harness state machine + program heartbeat + published endpoint + results tarball. | **A PID / node-local state** -- state is durable shared-FS, never node-local; watchers key on it + `sacct`, never on PID liveness (see [`../CLAUDE.md`](../../../CLAUDE.md) observability discipline). |

## 5. Superseded primitives

| Old term | Replaced by | In spec |
|---|---|---|
| *(none yet)* | -- | -- |

The corpus is new; nothing has been superseded. When W3 extracts the contract and the hard-coded "server+worker pair" in `src/bear_harness/_launch.py` becomes "a `coupled` JobGraph," that is a *code* migration described by Spec 01, not a primitive rename -- so it will not appear here unless a coined term is actually retired. Strike-through-in-place is the rule; this table records only retirements.

## 6. Architectural inheritances

| From | What this corpus adopts |
|---|---|
| BlueBEAR SLURM (University of Birmingham HPC) | The scheduler the JobGraph is realised onto. Edges are sbatch `--dependency`, bundles are SLURM arrays, jobs are sbatch submissions. The contract abstracts WORKLOADS, deliberately **not** SCHEDULERS -- portability is a non-goal ([`../docs/decision-notes/bluebear-only.md`](../decision-notes/bluebear-only.md)). Cluster facts are pinned in [`../references/bluebear-platform.md`](../../../references/bluebear-platform.md) and [`../references/slurm-cli.md`](../../../references/slurm-cli.md). |
| The working vLLM+pipeline flow | The reference topology the contract is extracted *from*: server publishes the endpoint, worker consumes it, `role=sidecar` tears the server down. W3 demotes it to one preset among many without changing its behaviour. See Spec 01 §6 and [`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../decision-notes/detached-deploy-cut-after-probe.md). |
| The filesystem-attached state model | `run_id`-keyed durable state on the shared FS as the substrate that makes reattach-from-any-session a first-class guarantee -- the reason a general workflow engine was rejected as the kernel ([`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md)). |

## 7. Documents not in this corpus

The reference library ([`references/`](../../../references/) -- the SLURM/vLLM/Anthropic/BlueBEAR cribs the agent must cite rather than recall) and the decision-notes ([`decision-notes/`](../decision-notes/) -- dated, append-only rationale; the keystone is [`first-decision.md`](../decision-notes/first-decision.md)) live outside the numbered corpus -- cite them by path, not by spec number.

**The archive convention.** A spec is a CONTRACT, kept true in place: a superseded clause is struck through where it stands, with a note naming the superseding spec -- never silently rewritten, because other documents cite it. A *full rewrite* is the only thing that moves a spec: the original is copied verbatim into [`archive/`](archive/) (filename preserved, datestamped) and the new version takes the live number. The archive is therefore the spec's history, and a live spec at `NN-slug.md` is always the current contract for that number. ([`archive/`](archive/) holds only a `.gitkeep` today -- nothing has been rewritten yet.)

---
*End of reading guide. Start with the audience-appropriate order from §3.*

<!-- when to expand me: hoist §4 into a standalone GLOSSARY.md only if term collisions grow
     beyond what a table can hold. Add a CURATION.md/AUDIT.md only once the corpus drifts
     (>=4-5 specs) -- not before. -->
