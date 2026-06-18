# The JobGraph Contract

**Spec 01 -- The foundational contract: jobs + edges + publishes/consumes + role, realised over SLURM**

<!-- LIVING. A spec is a CONTRACT, not an exploration. Kept true: superseded clauses are
     struck through in place; a full rewrite archives the original to specs/archive/.
     Subtypes: addendum (new subsystem) / revision (re-cuts existing specs) / slimmed
     (cedes scope to a neighbour with a "what X already handles" negative-scope section). -->

---
*Working draft, v0.1 -- the **base** contract of the corpus (the foundation Spec 00's reading guide maps); defines the **JobGraph**: the single closed vocabulary the preset-agnostic kernel honours, and nothing else.*

> **STATUS: EXTRACTED in W3 -- code-complete + green; cluster-verification pending.** The JobGraph now exists as a standalone artifact: the closed vocabulary in [`../src/bear_harness/_jobgraph.py`](../../../src/bear_harness/_jobgraph.py), the reference vLLM+pipeline preset that lowers to it in [`../src/bear_harness/_reference_preset.py`](../../../src/bear_harness/_reference_preset.py), and the generic kernel walker that realises it (`_realise_graph`) in [`../src/bear_harness/_launch.py`](../../../src/bear_harness/_launch.py). The launcher reads a JobGraph rather than a hard-coded server+worker pair, the vLLM flow is the *reference preset*, and the kernel carries **no vLLM spec-building** -- it is genuinely preset-agnostic. The extraction was behaviour-preserving (the full non-integration suite is green and `run.json` is byte-identical); the **real `bbshort` byte-identical run** that closes W3 per the roadmap is the remaining gate. Current state: `../docs/lanes.md`.

This is the keystone made concrete. The decision -- *the kernel is preset-agnostic; it honours the JobGraph contract, not the workload* -- is recorded in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md). That note says *what* and *why*; this spec says exactly *what the contract is*. The gap it closes: without a written contract, "preset-agnostic" is an aspiration that the next vLLM-specific shortcut quietly defeats. With it, the kernel has a closed surface to honour and presets have a stable surface to target, so the two can be built against the spec instead of against each other's current code.

---
## Table of contents

1. Summary
2. Why this contract
3. The core model
4. Lifecycle
5. Type system / the kernel<->preset wire
6. The reference vLLM+pipeline preset, mapped onto the contract
7. Testing
8. Comparison
9. Spec change inventory
10. Open questions

## 1. Summary

| Element | What it is | Realised as |
|---|---|---|
| **Job** | A unit of work | One `sbatch` submission (resources, dependencies, optional role) |
| **Edge `after`** | "Start once the dep leaves the queue" | sbatch `--dependency=after:<jobid>` |
| **Edge `afterok`** | "Start only if the dep succeeded" | sbatch `--dependency=afterok:<jobid>` |
| **`publishes`** | A job drops a typed record on the shared FS | A file under the `run_id` directory (e.g. `endpoint.json`) |
| **`consumes`** | A downstream job reads a record as environment | The published record injected as env (e.g. `$MODEL_BASE_URL`) |
| **`role=sidecar`** | A server job that exists only to serve its consumers | The kernel `scancel`s it once its consumers finish |
| **Topology** | The shape jobs+edges form | one of **single** / **bundle** / **coupled** / **dag** |

A **JobGraph** is a set of jobs, a set of edges over them, a set of publish/consume record-flows, and per-job roles -- nothing more. A **preset** is an authored unit that lowers a workload to exactly this data; the **kernel** realises that data on SLURM and tracks it as filesystem-attached state keyed by `run_id`. The contract is *closed* (a fixed vocabulary) and presets are *open* (any number, authored without kernel changes). That asymmetry -- closed contract, open presets -- is the entire architecture: it is what lets one control plane serve every topology and lets an autonomous agent add a workload without touching the control plane ([`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md)).

The kernel honours this contract **and nothing else**. It does not know what a job *computes*, only that it is a job with resources, edges, records, and a role. The day a real workload needs something the contract cannot say, the contract is extended once -- in the open, where every preset inherits it -- never patched per-workload inside the kernel.

## 2. Why this contract

Three forcing functions; defeat any one and the architecture collapses back to a workload-specific harness:

**One control plane must serve all four topologies, or "never lose results" forks into four guarantees.** A single job, a SLURM array bundle, a coupled server+worker, and a full DAG share one deploy/handle/status/results/reattach machinery -- precisely because all four are *the same data* (jobs + edges + records + roles) in different shapes. If the kernel branched per topology, the durable-results promise would have to be re-kept four times. The JobGraph unifies them behind one realiser so the guarantee is kept once. (Per-topology orchestrators were the rejected alternative: [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md).)

**The kernel must read only the graph, or presets stop being an open extension point.** An autonomous agent authors presets ([`../docs/PROJECT-VISION.md`](../PROJECT-VISION.md)). It can only do so *additively* -- without a kernel change -- if the kernel's entire input is the JobGraph vocabulary. The moment the kernel hard-codes a workload assumption (a vLLM-shaped server, an ETL-shaped batch), the next preset must fork the kernel, and the authoring story is dead. The contract being the *sole* input is the load-bearing property.

**Schedulers sit below the contract line, deliberately.** The JobGraph abstracts WORKLOADS, not SCHEDULERS. Edges *are* sbatch `--dependency`, bundles *are* SLURM arrays -- the contract names BlueBEAR directly and pays no portability tax ([`../docs/decision-notes/bluebear-only.md`](../decision-notes/bluebear-only.md)). A preset is portable across the contract; the scheduler is not meant to be portable across clusters, because the whole value is exploiting one cluster well. Cite the scheduler surface, never recall it: [`../references/slurm-cli.md`](../../../references/slurm-cli.md).

## 3. The core model

```python
# The JobGraph contract -- the closed vocabulary the kernel honours.
# Frozen dataclasses (rule-output discipline, line length 100). Extracted in W3 -> _jobgraph.py.

from dataclasses import dataclass
from enum import Enum

class EdgeKind(Enum):
    AFTER = "after"        # start once dep LEAVES the queue (success or not)
    AFTEROK = "afterok"    # start only if dep SUCCEEDED

class Role(Enum):
    WORKER = "worker"      # default: a job that runs to completion
    SIDECAR = "sidecar"    # a server torn down (scancel) once its consumers finish

@dataclass(frozen=True)
class Record:
    """A typed record a job drops on the shared FS for a downstream job to read as env."""
    name: str              # logical name, e.g. "endpoint"
    filename: str          # where it lands under the run_id dir, e.g. "endpoint.json"
    env_var: str           # how a consumer reads it, e.g. "MODEL_BASE_URL"

@dataclass(frozen=True)
class Job:
    name: str
    resources: "Resources"          # qos / walltime / gres / array -- BlueBEAR-shaped
    role: Role = Role.WORKER
    publishes: tuple[Record, ...] = ()
    consumes: tuple[Record, ...] = ()

@dataclass(frozen=True)
class Edge:
    upstream: str          # Job.name
    downstream: str        # Job.name
    kind: EdgeKind

@dataclass(frozen=True)
class JobGraph:
    jobs: tuple[Job, ...]
    edges: tuple[Edge, ...]
    # topology is DERIVED from (jobs, edges, roles), not declared -- see §3 topologies
```

Read the four pieces at their point of use:

- **Jobs** are sbatch submissions. `resources` carries the BlueBEAR-shaped request (QoS tier, walltime, GRES, array spec) that the realiser turns into sbatch flags -- cite [`../references/bluebear-platform.md`](../../../references/bluebear-platform.md) for the strings, never recall them. Each job's resources are checked against the default-deny guardrails before submit ([`../docs/decision-notes/default-deny-guardrails.md`](../decision-notes/default-deny-guardrails.md)); the contract carries the request, the guardrails decide whether it's allowed.

- **Edges** order execution and are realised as sbatch `--dependency`. The distinction is load-bearing and a real bug already hit it: the pipeline edge in the reference flow is `after:` (start once the server job is *running/queued past submit*), **not** `afterok:` -- a worker that waited for `afterok` on a long-lived server job would never start, because a sidecar server does not "succeed" in the SLURM sense, it gets scancelled. Use `after:` for the server→worker edge; reserve `afterok:` for genuine success-gated chains. Cite [`../references/slurm-cli.md`](../../../references/slurm-cli.md) for the exact flag syntax.

- **publishes / consumes** is the data channel, orthogonal to edges. A publisher writes a `Record` file under the `run_id` directory on the shared FS; a consumer reads it as `env_var`. In the reference flow the server *publishes* `endpoint.json` and the worker *consumes* it as `$MODEL_BASE_URL`. Note: today that URL is baked into the pipeline command **at submit time** (`$MODEL_BASE_URL` substitution in `src/bear_harness/_pipeline_launcher.py`), which is exactly why detached deploy returns *after* the vLLM probe -- the endpoint must be known before the pipeline is submitted ([`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../decision-notes/detached-deploy-cut-after-probe.md)). Runtime injection (read the record at *run* time) is a deferred optimisation, not a contract change (`../docs/spec-deferrals.md`).

- **role=sidecar** is the server-lifecycle marker. A `SIDECAR` job is a server that exists only to serve its consumers; the kernel `scancel`s it once every job that consumes its published record has finished. This is the contract's one piece of *lifecycle* knowledge -- it knows a sidecar must be torn down, but not what the sidecar serves.

### The four topologies (derived, not declared)

A topology is the *shape* a JobGraph takes; the realiser handles all four with one code path. It is derived from (jobs, edges, roles), not a field a preset sets:

- **single** -- one job, no edges. A lone batch task. The degenerate case the whole machinery still serves identically (deploy → `run_id` → status/logs/results/reattach).
- **bundle** -- one job spec fanned across a parameter grid as a **SLURM array** (`Resources` carries the array spec). "One job times N parameter sets" over the same publishes/consumes plumbing. The *sweeps* preset is the deferred consumer of this topology (`../docs/spec-deferrals.md`); the topology itself is in the contract now.
- **coupled** -- a server job (`role=sidecar`) plus one or more worker jobs joined by an `after` edge and an endpoint `publishes`/`consumes` record. **This is the reference vLLM+pipeline shape** (§6). The *eval* preset generalises it to many consumers (coupled fan-out, deferred).
- **dag** -- an arbitrary directed acyclic graph of jobs over `after`/`afterok` edges with record-flows along them. The general case; the other three are constrained instances of it.

The point of deriving rather than declaring: a preset never *says* "I am coupled." It produces jobs, edges, records, and roles; the shape falls out. So a new topology-shaped workload is a new arrangement of the *same* vocabulary, not a new kernel branch.

## 4. Lifecycle

The kernel realises a JobGraph as a detached deploy and tracks it as **filesystem-attached state keyed by `run_id`** -- the property that makes any session, even a brand-new one, reattach by `run_id`. The lifecycle is identical across all four topologies (that identity is the §2 forcing function made operational):

1. **Deploy (detached).** The kernel submits the graph's jobs to SLURM in dependency order (`sbatch --parsable` to capture job IDs; edges become `--dependency` on the dependent submissions -- see [`../references/slurm-cli.md`](../../../references/slurm-cli.md)), writes the initial `run.json`, and returns a `run_id` handle. For the reference coupled topology, deploy returns *after the vLLM probe confirms the endpoint is live* (so the pipeline can bake in `$MODEL_BASE_URL`), not after merely submitting both jobs -- [`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../decision-notes/detached-deploy-cut-after-probe.md). The deploy *interface* is identical whether the cut is after-probe or instant-return.
2. **Run.** Jobs execute on compute nodes under SLURM. A publisher drops its `Record` on the shared FS; a consumer reads it. A `sidecar` server serves its consumers.
3. **Observe.** Status/logs come from durable shared-FS artifacts + `sacct`, **never** PID liveness (round-robin login nodes make node-local state unreliable -- [`../CLAUDE.md`](../../../CLAUDE.md) observability discipline). The watcher keys on `run.json` (harness state machine) + `.bear-harness-status.json` (program heartbeat) + `sacct -j` (post-queue fallback when a job has left the `squeue` window).
4. **Teardown.** When a sidecar's consumers all finish, the kernel `scancel`s the sidecar. Results are gathered lazily into the artifacts tarball under `run_id`.
5. **Reattach.** Any session re-opens the run by `run_id` off the shared-FS state -- nobody babysits SLURM. This is the never-lose-results bar: filesystem-attached + lazy results + reattach-by-`run_id`.

The `run_id`-keyed state files are the contract's durable footprint: `run.json`, `.bear-harness-status.json`, `endpoint.json` (a published `Record`), and the artifacts tarball -- all on the shared FS. Failures are loud and diagnosable (no silent zero-output completion -- the `ZeroSuccessfulCallsError` pattern), which is what makes a detached run safe to walk away from. See the operational loop in [`../docs/runbooks/validation.md`](../../runbooks/validation.md).

## 5. Type system / the kernel<->preset wire

**This is the keystone invariant.** The wire between kernel and preset is exactly the `JobGraph` of §3 -- *nothing crosses it but that data*. The kernel's only input is a `JobGraph`; a preset's only output is a `JobGraph`. The kernel cannot reach up into a preset (there is nothing to reach for -- a preset is gone once it has produced its graph) and a preset cannot reach down into the kernel (its only lever is the graph it emits). The contract is the membrane.

```
   preset (vLLM+pipeline / ETL / ...)        kernel (src/bear_harness/, preset-agnostic)
   ─────────────────────────────────         ─────────────────────────────────────────────
   knows the WORKLOAD                         knows the CONTRACT
        │                                          │
        │  lowers workload to ──► JobGraph ──►  realises on SLURM
        │     jobs + edges +                       (sbatch + --dependency + array)
        │     publishes/consumes +             tracks filesystem-attached state by run_id
        │     role                             scancels role=sidecar on consumer-finish
        ▼                                          ▼
   THE WIRE IS THE JobGraph AND ONLY THE JobGraph
```

What this buys, stated as falsifiable invariants the kernel must satisfy:

- **Agnosticism is testable.** Two presets (vLLM+pipeline and ETL) run on the *same kernel binary* with **zero kernel diff** between them. If both complete with no harness change, the kernel is agnostic in practice, not just in principle -- this is the verification recorded in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md).
- **No workload-named field crosses the wire.** The `JobGraph` vocabulary contains no `model`, no `endpoint_route`, no `etl_source` -- only `Job`/`Edge`/`Record`/`Role`. A workload-named field appearing in the contract is the smoke signal that the abstraction is leaking (the escalation trigger in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md)).
- **Presets reduce to inspectable data.** Because the wire is data, an authored preset can be validated against the guardrail caps *before* submit (`validate_preset` / `dry_run` in W4). This is why declarative authoring ships first and the Python-builder waits behind a sandbox -- [`../docs/decision-notes/declarative-presets-first.md`](../decision-notes/declarative-presets-first.md).

The transport (the agent driving the login-node orchestrator over key-based SSH) sits *outside* this wire entirely -- the SLURM-touching code stays on the login node, and no SSH lives inside the kernel ([`../docs/decision-notes/login-node-orchestrator.md`](../decision-notes/login-node-orchestrator.md)). The kernel<->preset wire is purely the JobGraph; the laptop<->login-node wire is purely the transport. Keeping them separate is why the kernel never grows an SSH dependency.

## 6. The reference vLLM+pipeline preset, mapped onto the contract

The vLLM+pipeline flow is the **reference preset** -- the one W3 extracts the contract *from*. Mapping it onto §3 is the worked example every other preset imitates:

| Contract element | vLLM+pipeline instantiation |
|---|---|
| **Topology** | **coupled** -- a server job + a worker (pipeline) job |
| **Server job** | The vLLM serve job. `role=sidecar`. `publishes` the `endpoint` record. Resources: a GPU GRES + a QoS tier from [`../references/bluebear-platform.md`](../../../references/bluebear-platform.md). Current code: `src/bear_harness/_vllm_launcher.py`. |
| **Worker job** | The pipeline job. `consumes` the `endpoint` record as `$MODEL_BASE_URL`. Current code: `src/bear_harness/_pipeline_launcher.py`. |
| **Edge** | `after:<server_jobid>` (server→worker) -- **not** `afterok:`; a sidecar server is scancelled, it does not "succeed". |
| **`publishes` (server)** | `Record(name="endpoint", filename="endpoint.json", env_var="MODEL_BASE_URL")` -- the OpenAI-compatible server **root** URL (no `/v1` suffix; the routes are `/v1/models`, `/v1/messages`, never `/v1/v1/...` -- [`../references/vllm-serve-api.md`](../../../references/vllm-serve-api.md)). |
| **`consumes` (worker)** | The same `endpoint` record, injected as `$MODEL_BASE_URL`, currently baked into the pipeline command **at submit time** -- hence the deploy cut is after the probe ([`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../decision-notes/detached-deploy-cut-after-probe.md)). |
| **`role=sidecar` teardown** | When the pipeline (the sole consumer) finishes, the kernel `scancel`s the vLLM server -- it existed only to serve the worker. |

Two gotchas this mapping pins, both from real bugs (cite the cribs, don't recall): the server URL published is the **root**, not `…/v1` (a `/v1/v1/...` double-prefix was a real failure), and the worker authenticates to vLLM with `Authorization: Bearer <key>`, **not** `x-api-key` (the wrong header caused a 401 where every call failed silently -- exactly the loud-failure bar the kernel must defend). See [`../references/vllm-serve-api.md`](../../../references/vllm-serve-api.md) and [`../references/anthropic-messages-api.md`](../../../references/anthropic-messages-api.md) (the Anthropic adapter pointed at vLLM sends both auth dialects).

The **second** preset, ETL, is the de-risker: no GPU, no server -- a `single`-or-`dag`-topology preset with no `role=sidecar` and no endpoint record. It proves the contract carries a workload that shares *none* of the reference flow's distinctive structure, which is the concrete falsification of "this is secretly a vLLM-only harness" (`../docs/spec-deferrals.md`, "Not deferred").

## 7. Testing

The contract is verified at two altitudes:

- **Unit / contract tests (CI, `hatch run test`, `-m 'not integration'`).** The JobGraph dataclasses, edge realisation (`after` vs `afterok` → correct `--dependency` string), record publish/consume env wiring, and sidecar-teardown logic are testable without a cluster -- frozen dataclasses with deterministic `as_dict()` serialisation. The W1 tests `TestDetach` and `TestLaunchResultSerialisation` exist and are currently red against `src/bear_harness/_launch.py` (which already carries the detach parameter, `LaunchResult.as_dict()`, and the post-probe early-return cut); W3 extends this with JobGraph-level contract tests.
- **Integration / real-run verification (a real `bbshort` run).** CI cannot prove cluster behaviour -- every SLURM/vLLM/GRES claim is verified on a real `bbshort` run before being relied on. The agnosticism claim is itself a test: run vLLM+pipeline and ETL back-to-back on the same binary with zero kernel diff (the cross-validation in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md)). The proven debug cycle -- diagnose from shared-FS JSONL + sbatch `.out` → TDD fix → update `setup-bluebear.sh` → relaunch under `bbshort` (~5 min/cycle) → watch on `run.json` + status JSON + `sacct` (never PID) -- is the bbshort iteration loop in [`../docs/runbooks/validation.md`](../../runbooks/validation.md).

## 8. Comparison

### vs a vLLM-specific harness (bake server+worker straight into the kernel)

Tempting because vLLM+pipeline is the only workload running today and one consumer is simpler with the pattern hard-coded. It fails because the second preset (ETL: no GPU, no server) would then require *forking the kernel*, and the autonomous-authoring story collapses -- an agent cannot add a workload shape the kernel has hard-coded against. The contract demotes vLLM+pipeline to the *reference* preset precisely so adding ETL is additive. (Steelmanned and rejected in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md).)

### vs a general workflow engine (Airflow / Nextflow / Snakemake as the kernel)

Tempting because DAG execution is solved and battle-tested. It fails on three counts: those engines abstract *schedulers and workloads both* (we deliberately abstract only workloads -- [`../docs/decision-notes/bluebear-only.md`](../decision-notes/bluebear-only.md)); they carry a daemon/operational footprint we don't want on a login node (which is orchestration-only -- [`../CLAUDE.md`](../../../CLAUDE.md)); and none gives us **filesystem-attached reattach-by-`run_id` as a first-class guarantee**, which is the never-lose-results bar. The JobGraph is the small closed contract that buys workload-portability *without* importing a scheduler abstraction or a daemon. (Rejected in [`../docs/decision-notes/first-decision.md`](../decision-notes/first-decision.md); the reconsider condition is recorded there.)

## 9. Spec change inventory

<!-- Revisions only -- the exact clauses this spec re-cuts in its predecessors. -->

Spec 01 is the **base contract** of the corpus -- it has no predecessor spec to re-cut. (Spec 00 is the reading guide/map, not a contract Spec 01 revises.) When a future addendum spec re-cuts a clause here, that addendum lists the change in *its* inventory and the affected clause below is struck through in place with a pointer to the superseding spec. Nothing is struck through yet.

## 10. Open questions

**10.1 What is the canonical shape of a `Resources` request?** ~~§3 leaves `Resources` as a placeholder type...~~ **RESOLVED in W3.** `Resources` is `qos` / `walltime` / `gres` / `cpus_per_task` / `mem_gb` / `array` -- all optional, BlueBEAR-named (no portable subset, per [`../docs/decision-notes/bluebear-only.md`](../decision-notes/bluebear-only.md)); `gpu_count` derives from `gres` via the shared `gpu_count_from_gres` parser so the request feeds `evaluate_guardrails` unchanged. `array` is the lone `bundle`-topology hook (`None` today). All-`None` is a pure-local request that reserves nothing. See [`../src/bear_harness/_jobgraph.py`](../../../src/bear_harness/_jobgraph.py).

**10.2 Submit-time vs run-time record injection.** Today `publishes`/`consumes` is realised by baking the published value into the consumer's command at *submit* time (`$MODEL_BASE_URL` substitution), which forces the after-probe deploy cut. A run-time injection mechanism (consumer reads `endpoint.json` from the shared FS at run time) would unlock instant-return detach. **Lean:** keep submit-time injection for V1; it is a pure latency optimisation with *no contract impact*, explicitly deferred. Resolves in **a NEXT-CYCLE phase**, on the trigger in `../docs/spec-deferrals.md` and [`../docs/decision-notes/detached-deploy-cut-after-probe.md`](../decision-notes/detached-deploy-cut-after-probe.md).

**10.3 Does the `dag` topology need conditional or fan-in edges?** §3 gives `after`/`afterok` edges only. A real DAG workload might want a conditional edge or a fan-in barrier the current vocabulary can't express -- and per the keystone, that cost lands on the *contract* (a breaking change every preset depends on), not on the preset. **Lean:** do **not** add edge kinds speculatively; add one only when a real workload cannot be expressed without it, extending the contract once in the open. The escalation trigger -- a second/third preset each needing a *different* kernel-level special-case -- is the signal to revisit. Resolves **when a deferred preset (eval fan-out, training+resume) forces it** (`../docs/spec-deferrals.md`).

---
*End of The JobGraph Contract. v0.1 -- extracted in W3 (code-complete + green); the real `bbshort` byte-identical run is the remaining gate before W3 is fully done.*

<!-- when to expand me: this is one subsystem's contract. A new subsystem gets its OWN
     numbered spec, not a section here. When a clause is superseded, strike it through in
     place and note the superseding spec -- do not silently rewrite a contract others cite. -->
