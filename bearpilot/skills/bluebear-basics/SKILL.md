---
name: bluebear-basics
description: Start here for BlueBEAR. Connect to the University of Birmingham HPC cluster over SSH, understand the login-node-vs-compute-node model, and learn the account/QoS/GPU ground-truth. Use when the user is new to BlueBEAR, asks how to access or log in to the cluster, asks what QoS/GRES/account to use, or when any cluster value (QoS tier, GPU type, CUDA module) needs confirming before building a job.
---

# BlueBEAR basics

BlueBEAR is the University of Birmingham's shared SLURM HPC cluster. This skill is the
foundation; the others (`batch-jobs`, `gpu-and-serving`, `observability`, `bear-harness`,
`authoring-presets`) build on it.

**Golden rule, learned the hard way:** before emitting *any* cluster string — an SSH target, a
QoS tier, a GPU GRES, a CUDA module, an account — read the encoded ground-truth at
`${CLAUDE_PLUGIN_ROOT}/references/cluster-ground-truth.md` and the traps at
`${CLAUDE_PLUGIN_ROOT}/references/gotchas.md`. These values **rotate**, and a wrong one fails
the job at submit or on the compute node. Cite the file; don't recall from training.

## The mental model (internalise this first)

```
your laptop ──ssh──▶ login node (round-robin)  ──sbatch──▶ compute node (CPU / A100 GPU)
                     │                                       │
                     └─ ORCHESTRATION ONLY                   └─ where ALL real work runs
                        submit + watch, never heavy compute     (serving, training, builds)
                     state lives on RDS (shared FS), keyed so any session can reattach
```

Three consequences that drive everything else:

1. **Login nodes are orchestration-only.** Never run model serving, image builds, big
   compiles, or large downloads on a login node — wrap them in `sbatch`. A per-user limiter
   kills heavy login-node processes.
2. **Login nodes are round-robin**, so node-local state (PIDs, `/tmp`, `nohup`) lies across
   reconnects. Trust durable RDS artifacts + `sacct`, never a PID. (See the `observability`
   skill.)
3. **There is no SLURM REST door** (no `slurmrestd`, no JWT). Drive SLURM only via its CLI
   (`sbatch`/`squeue`/`sacct`/`scancel`) over SSH.

## Connect

```bash
# The cluster user is your-username — NOT your local mac user. Off-campus needs the University VPN.
ssh your-username@bluebear.bham.ac.uk 'echo connected; hostname'
```

Or use the bundled connector, which **pins** you to one login node (so watchers stay valid)
and prints the *live* ground-truth next to the encoded defaults so any drift is obvious:

```bash
${CLAUDE_PLUGIN_ROOT}/harness/bb-connect.sh
```

If that fails: are you on the VPN? Does key auth work? Did you use `your-username@` (not your local
user)? See `${CLAUDE_PLUGIN_ROOT}/references/gotchas.md` items 1–3.

## The ground-truth you'll need constantly

(Full table + re-probe commands in `cluster-ground-truth.md`. Summary:)

- **Account:** `your-project` (`--account`).
- **QoS:** `bbshort` (10-min, spans GPUs — the debug loop) · `bbgpu` (2-day GPU) ·
  `bbdefault` (CPU). ⚠ `bbcpu` does **not** exist here.
- **GPU:** `--gres=gpu:a100:1` (type is `a100`, cards are **40 GB**). Not `a100_40`/`a100_80`.
- **Modules:** `module load bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0 CUDA/12.6.0`
  (CUDA version rotates — `module avail CUDA`).
- **RDS root:** `/rds/projects/p/your-project` — durable shared filesystem; all run
  state lives here. `$HOME` is `/rds/homes/u/your-username`.

## Confirm a value is still live

When in doubt, re-probe on a login node rather than trusting any pinned value:

```bash
sacctmgr -nP show assoc user=$USER format=account,qos   # your account + allowed QoS
sinfo -o "%P %G %D %t"                                   # partitions + GRES + node state
module avail CUDA                                        # live CUDA modules (prefer (D) default)
```

## Where to go next

- Submit your first job → the **batch-jobs** skill.
- Serve a model on a GPU → the **gpu-and-serving** skill.
- Track running jobs / debug a failing one → the **observability** skill.
- Autonomous, reattachable, guardrailed deploys → the **bear-harness** skill.
