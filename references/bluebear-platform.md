# BlueBEAR platform (QoS tiers, GPU GRES, RDS, modules, Apptainer, node-local scratch) — platform crib

<!-- SNAPSHOT-WITH-PROVENANCE. One file = one external surface the project drives.
     Allowed to age -- but it carries its source so staleness is detectable and refresh is
     mechanical. Provenance-at-top + refresh-contract-at-bottom around a distilled body. -->

> **Canonical source:** <https://docs.bear.bham.ac.uk/> — the University of Birmingham BEAR / BlueBEAR official HPC documentation (the docs URL is cited from the ops walkthrough [`../docs/bluebear.md`](../docs/bluebear.md) and the decision note [`../docs/decision-notes/bluebear-only.md`](../docs/internal/decision-notes/bluebear-only.md)).
> **Version / pin:** the live BlueBEAR cluster as of 2026-06. Cluster strings (QoS names, GPU GRES, CUDA module versions, RDS path layout) **rotate** — confirm on a login node before pinning a value in `bear.toml`. CUDA modules in particular move; the troubleshooting runbook shows `module avail CUDA` failing when `bear.toml` lags the cluster.
> **Why this crib:** BlueBEAR is the *only* target — portability is a non-goal ([`../docs/decision-notes/bluebear-only.md`](../docs/internal/decision-notes/bluebear-only.md)). `src/bear_harness/_bear_config.py` holds these platform strings (`qos`, `gpu_gres`, RDS paths, `cuda_module`) and `src/bear_harness/_bootstrap.py` provisions the RDS layout + pulls the Apptainer image. This crib exists so the agent cites cluster facts instead of recalling them from stale training — a wrong GRES string or QoS tier fails the job at submit, and a wrong CUDA module fails it on the compute node.

---
## What it is

BlueBEAR is BlueBEAR — the University of Birmingham's shared SLURM HPC cluster. bear-harness runs entirely on it: a thick orchestrator on a **login node** (orchestration-only — never heavy compute) submits `sbatch` jobs that run vLLM on GPU nodes and pipeline programs on CPU nodes. This crib owns the cluster-specific *values* (QoS tiers, GRES strings, storage paths, the module system, Apptainer flags, node-local scratch); the *grammar* of the SLURM commands lives in [`slurm-cli.md`](slurm-cli.md).

---
## Surface that matters  (QoS / GRES / storage / modules / Apptainer)

### QoS tiers (`--qos=`)

```text
bbshort   fast-track, MaxWall 00:10:00 (probed 2026-06-14), spans ALL node types incl. GPUs.
          → the iteration-loop workhorse: ~5-min debug cycles. (See the validation runbook.)
bbgpu     standard GPU QoS, MaxWall 2-00:00:00 — the vLLM server runs here.
bbdefault standard QoS, MaxWall 10-00:00:00 — a valid CPU QoS for the pipeline/worker job.
# ⚠ "bbcpu" is NOT a universal QoS — it is ABSENT on the your-project account
#   (assoc QoS, probed 2026-06-14: bbshort / bbgpu / bbdefault / bbondemand / bbportalgpu).
#   The pipeline job's QoS is configurable via [slurm].cpu_qos (default: fall back to qos);
#   never hardcode bbcpu. Confirm per account: `sacctmgr -nP show assoc user=$USER`.
```

### GPU GRES (`--gres=gpu:<type>:<count>`)

```text
gpu:a100:1        one A100        — the real GRES type here is `a100` (probed 2026-06-14 from
                                    the working bear.toml + a live bbshort run); `a100_40` /
                                    `a100_80` are NOT valid GRES names on this cluster.
gpu:a100:2        two A100s       — pair with --tensor-parallel-size 2 for larger models
```

### Storage

```text
RDS (Research Data Store)  /rds/projects/<initial>/<project-code>/...
    → durable shared filesystem. bear-harness keys ALL state here by run_id:
      $RDS_ROOT/.bear-harness/{endpoints,runs,logs,apptainer}/  and  $RDS_ROOT/hf_cache/
      (run.json, .bear-harness-status.json, endpoint.json, artifacts tarball)
node-local /scratch        fast, per-node, EPHEMERAL. Good for transient compute scratch;
    → NEVER the source of truth for run state — it does not survive the node or a reattach.
```

### Module system (Lmod)

```text
module load Apptainer            # the container runtime (required on the login node to pull)
module load CUDA/<version>       # e.g. CUDA/12.1.1 — VERSIONS ROTATE; confirm with `module avail CUDA`
module avail CUDA                # list installed CUDA modules; pick one marked (D) default or known-good
```

### Apptainer (the container runtime)

```text
apptainer pull <sif> docker://vllm/vllm-openai:<tag>   # bootstrap pulls the vLLM image (~6GB)
apptainer exec --nv \                                   # --nv exposes the host NVIDIA GPUs into the container
               --bind <host_path>:<container_path> \    # --bind mounts RDS paths (hf_cache, runs) inside
               <sif> <cmd>
```

---
## Gotchas / quirks / version traps

- **Login nodes are ORCHESTRATION-ONLY.** Image builds, weight downloads at scale, model serving — all go through `sbatch` (`bbshort` for short jobs). Running heavy compute on a login node is the first invariant an agent violates; it is pinned at the top of [`../CLAUDE.md`](../CLAUDE.md). The whole architecture rests on this ([`../docs/decision-notes/login-node-orchestrator.md`](../docs/internal/decision-notes/login-node-orchestrator.md)).
- **Login nodes are ROUND-ROBIN → node-local state lies.** PIDs, `/tmp`, `nohup` do not survive a reconnect because you may land on a different login node. Pin SSH to a node IP and key every watcher on durable RDS artifacts + `sacct`, **never on PID liveness** ([`../CLAUDE.md`](../CLAUDE.md) observability discipline). This is *why* the kernel is filesystem-attached and reattachable by run_id.
- **No `slurmrestd` / no REST door.** BlueBEAR exposes no SLURM REST endpoint and issues no SLURM JWT — `which slurmrestd` and `scontrol token` both fail (confirmed 2026-06-14). Drive SLURM **only** through its CLI over SSH (`sbatch`/`squeue`/`sacct`/`scancel`); do not reach for a `slurmrestd` client. The REST-fronting MCP shape common elsewhere does not apply here — re-probe only if a BlueBEAR upgrade announces one. ([`../docs/decision-notes/mcp-over-ssh-transport.md`](../docs/internal/decision-notes/mcp-over-ssh-transport.md).)
- **CUDA module names rotate.** A `bear.toml` `cuda_module` that worked last month can fail on the compute node with `Lmod ... module(s) are unknown`. Always re-confirm with `module avail CUDA` and prefer the `(D)` default. (Troubleshooting: `module load CUDA/... fails on the compute node`.)
- **`bbshort` walltime is ~10 minutes and spans GPUs.** That is exactly what makes the ~5-min iteration loop possible — but a real vLLM boot of a 70B model exceeds it. Use `bbshort` to debug the harness plumbing; use `bbgpu` for the real serving job.
- **Compute nodes and login nodes can have DIFFERENT network egress.** The bootstrap Hugging Face probe only checks the *login* node; a compute node may still be firewalled off HF (or vice-versa). Pre-downloading weights into `$RDS_ROOT/hf_cache` on the login node is the workaround when a compute node can't reach HF. (Troubleshooting: `launch hangs waiting for the endpoint file`.)
- **RDS quota is finite and the Apptainer image is ~6GB.** `apptainer pull` fails on quota exhaustion. Keep ~80GB headroom for weights + the `.sif`. (Troubleshooting: `bootstrap fails with apptainer pull failed`.)
- **Apptainer needs `--nv` for GPUs and `--bind` for RDS.** Forgetting `--nv` means vLLM sees no GPU; forgetting `--bind` means the container can't read `hf_cache` or write `runs`.
- **GRES count must match `--tensor-parallel-size`.** `gpu:a100_80:2` without `tensor_parallel_size = 2` wastes a GPU; the reverse OOMs or fails to start.

---
## How bear-harness uses this

<!-- Maps every external token to the code path that emits it. Cite by FILE PATH only -- never a line number. -->

| External concept | bear-harness call site | File ref (path, NEVER line number) |
|---|---|---|
| `qos` (bbshort / bbgpu / bbcpu) | `SlurmConfig.qos`, rendered into `--qos` in the sbatch scripts; per-launch `qos_override` | `src/bear_harness/_bear_config.py`, `src/bear_harness/_vllm_launcher.py` |
| `gpu_gres` (`gpu:a100_40:1`, `gpu:a100_80:2`) | `SlurmConfig` GRES string → `--gres`; per-launch `gpu_gres_override` | `src/bear_harness/_bear_config.py`, `src/bear_harness/_vllm_launcher.py`, template `vllm.sbatch.j2` |
| RDS layout `$RDS_ROOT/.bear-harness/{endpoints,runs,logs,apptainer}` + `hf_cache` | Created by `bootstrap`; all run state keyed here by run_id | `src/bear_harness/_bootstrap.py`, `src/bear_harness/_endpoint_discovery.py` |
| `module load Apptainer` / `CUDA/<ver>` | `apptainer` presence verified at bootstrap; `cuda_module` from `SlurmConfig` baked into the sbatch preamble | `src/bear_harness/_bootstrap.py`, `src/bear_harness/_bear_config.py` |
| `apptainer pull docker://vllm/vllm-openai:<tag>` | Bootstrap pulls the `.sif`; tag overridable via `--apptainer-image` | `src/bear_harness/_bootstrap.py` |
| `apptainer exec --nv --bind ...` | The vLLM sbatch wrapper runs the server inside the container with GPU + RDS binds | template `vllm.sbatch.j2`, `src/bear_harness/_vllm_launcher.py` |
| `--account <project-code>` | The BlueBEAR project code; `SlurmConfig` / bootstrap `--account` | `src/bear_harness/_bear_config.py`, `src/bear_harness/_bootstrap.py` |

Guardrails govern these resource knobs (QoS tier, walltime ceiling, concurrency, GPU-hours), default-deny, never the science ([`../docs/decision-notes/default-deny-guardrails.md`](../docs/internal/decision-notes/default-deny-guardrails.md)). The platform doc [`../docs/bluebear.md`](../docs/bluebear.md) is the human first-run walkthrough this crib distils for the agent; that doc is **superseded-as-agent-reference by this crib** for the platform-string facts (it remains the canonical step-by-step for a human operator).

---
## Open questions to resolve when we wire it

1. The exact live QoS walltime ceilings and the GPU partition names — pin them from `sacctmgr show qos` / `sinfo -o "%P %G"` on a login node, since the values here are the documented intent, not a freshly-probed snapshot. Source: a login-node probe + <https://docs.bear.bham.ac.uk/>.
2. Whether node-local `/scratch` buys a meaningful win for the ETL preset (no GPU, no server) vs RDS — measure on a real `bbshort` ETL run before committing the path.

---
*Crib drafted 2026-06-14 against the live BlueBEAR cluster, from <https://docs.bear.bham.ac.uk/> distilled via [`../docs/bluebear.md`](../docs/bluebear.md) and cross-checked against `src/bear_harness/_bear_config.py` and `src/bear_harness/_bootstrap.py`. Update on: any BlueBEAR QoS/partition rename; a GPU-fleet change (new GRES types, A100→H100); a CUDA-module rotation that breaks `bear.toml`; an RDS path-layout change; the next BlueBEAR maintenance/upgrade announcement.*

<!-- when to expand me: split only by DISTINCT decision surface -- e.g. if the storage story
     (RDS quotas, /scratch staging, BEAR Portal) grows its own non-trivial procedure it could
     earn a sibling. Self-demote drift-prone values: never freeze a CUDA module version or a
     QoS walltime here as gospel -- record the SHAPE and point at the login-node probe. -->
