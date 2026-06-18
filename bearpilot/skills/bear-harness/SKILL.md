---
name: bear-harness
description: Use bear-harness, the LLM-callable deploy tool for BlueBEAR, for autonomous and reattachable runs. Use when the user wants an agent to deploy and run workloads on the cluster without babysitting, needs a server+worker (vLLM + pipeline) coupled job, wants resource guardrails / budget limits, wants to reattach to a run by run_id from a fresh session, or wants to drive SLURM over SSH via an MCP server. This is the advanced path beyond the bundled bash harness.
---

# bear-harness — autonomous deploys on BlueBEAR

The bundled bash harness (the `batch-jobs`/`observability` skills) is great for a single
sbatch. **bear-harness** is the step up: an LLM agent submits a **JobGraph** over SSH, a
preset-agnostic kernel realises it on SLURM under **default-deny guardrails**, and all state
is filesystem-attached so any session reattaches a run by `run_id` — nobody babysits SLURM.

Use bear-harness when you need any of: a coupled **server + worker** (e.g. vLLM serves an
endpoint a pipeline job consumes, server auto-cancelled when the worker finishes); a long
detached campaign you reattach to later; resource **guardrails / budgets**; or an **agent**
driving the cluster autonomously.

## The primitives (one glossary)

- **Kernel** — the preset-agnostic control plane: detached deploy → `run_id` handle →
  `status`/`logs`/`results`, all filesystem-attached so any session reattaches by `run_id`.
- **JobGraph** — *the contract*: jobs + edges (`after`/`afterok`) + `publishes`/`consumes` (a
  job drops a typed record on the shared FS; a downstream job reads it as env) + `role=sidecar`
  (a server is `scancel`led once its consumers finish). Four topologies: **single**, **bundle**
  (a SLURM array), **coupled** (server+worker), **dag**.
- **Preset** — a human- or LLM-authored unit targeting the contract. **vLLM+pipeline** is the
  reference preset; **ETL** (no GPU, no server) is the second. (Writing one → the
  `authoring-presets` skill.)
- **Transport** — a local **MCP server over key-based SSH** (ControlMaster multiplexing). The
  SLURM-touching orchestrator lives on the login node; *no SSH inside the kernel*.
- **State** — `run.json` (state machine) + `.bear-harness-status.json` (program heartbeat) +
  `endpoint.json` + an artifacts tarball, all on RDS, keyed by `run_id`.

## CLI surface

```bash
bear-harness validate <pipeline.toml>    # parse + print the normalised JobGraph (no submit)
bear-harness launch   <pipeline.toml>    # realise the flow on SLURM (vLLM) or a local backend
bear-harness status   <run_id|run.json>  # latest snapshot (reattach by run_id)
bear-harness logs     <run_id>           # tail vllm.log / pipeline.log
bear-harness cancel   <run_id>           # best-effort cancel
bear-harness list                        # known runs under runs_dir
bear-harness bootstrap                   # provision the RDS layout + pull the vLLM image
bear-harness presets list|describe|validate   # the declarative authoring kit
# launch flags: --detach (return run_id immediately) · --json (machine-readable) · --dry-run
```

## Default-deny guardrails (read before an agent launches)

Guardrails govern **resources, never the science**: a QoS allowlist, a walltime ceiling, a
concurrency cap, a dry-run gate, GPU-hour bounds. They are **default-deny** — an *absent*
`[guardrails]` section is a tight `bbshort`-only leash, not unbounded. The gate runs *before*
sbatch, so a denied launch produces `state=denied` and never submits.

⚠ **Migration trap:** if the live `bear.toml` has **no `[guardrails]` section**, upgrading the
cluster CLI makes the new default-deny gate **deny the human `bbgpu` workflow**. Before/at
upgrade, add to `bear.toml`:

```toml
[guardrails]
qos_allowlist = ["bbshort", "bbgpu"]
max_walltime  = "2-00:00:00"
# + a realistic gpu_hours_budget / concurrency cap
```

…or run the new version from an isolated venv for canaries so the existing workflow stays
untouched.

## Install / update on the cluster

This repo bundles the bear-harness engine, so the setup script is right here at the repo root. It
builds a wheel, rsyncs it, installs, bootstraps the RDS layout, and configures the shell:

```bash
bash scripts/setup-bluebear.sh            # first-time: build + install + bootstrap + shell config
bash scripts/setup-bluebear.sh update     # just rebuild + push the wheel (~60 s)
```

Verify any SLURM/vLLM/GRES change on a **real bbshort run** before relying on it — CI cannot
prove cluster behaviour. The proven iteration loop lives in the repo's
`docs/runbooks/validation.md`.

## Drive it from an agent (MCP over SSH)

The `bear-harness-mcp` server (optional `[mcp]` extra) is the agent front-end: it exposes
`launch`/`status`/`logs`/`cancel`/`fetch`/`ps`/`caps` tools and a `bear://guardrails/allowed`
resource, all over **one** SSH core (the `--remote` CLI flag is the human equivalent). The
detached login-node orchestrator (`nohup`/`setsid` + filesystem-attached state) is what
survives a laptop disconnect — ControlMaster is connection-reuse, **not** campaign liveness.

## When to use which

| Need | Tool |
|---|---|
| One sbatch (CPU/GPU/array), fast, no install | the bundled bash harness (`batch-jobs` skill) |
| Server + worker as one managed unit | bear-harness (coupled JobGraph) |
| Detached campaign, reattach by `run_id` later | bear-harness |
| Resource guardrails / budgets / autonomous agent | bear-harness |
| A brand-new workload type | bear-harness **preset** (`authoring-presets` skill) |

See `${CLAUDE_PLUGIN_ROOT}/references/links.md` for the repo's deep-doc paths (PROJECT-VISION,
the JobGraph contract spec, the cribs).
