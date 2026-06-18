---
name: batch-jobs
description: Build and submit SLURM batch jobs on BlueBEAR. Use when the user wants to write an sbatch script, run a CPU job, a parameter sweep / array job, or a Python script on the cluster; when they want to submit a job and watch it; or when iterating fast on a job with the bbshort 10-minute debug loop. Covers the bundled zero-dependency harness (scaffold → submit → watch → fetch) and the raw sbatch directives underneath it.
---

# Batch jobs on BlueBEAR

The bread and butter: package work as an `sbatch` script, submit it, watch it run, pull the
results back. Read `bluebear-basics` first if you haven't — especially the login-node rule.

## The fastest path (the bundled harness)

The harness at `${CLAUDE_PLUGIN_ROOT}/harness/` is pure bash + ssh + rsync — no install. It
fills every cluster string for you and bakes in the SSH/observability discipline.

```bash
H=${CLAUDE_PLUGIN_ROOT}/harness

$H/bb-connect.sh                              # pin a login node + sanity-check ground-truth
$H/bb-new-job.sh cpu my-first --short         # scaffold ./bb-jobs/my-first/my-first.sbatch
#   → --short = the bbshort 10-min debug profile. Drop it for a longer bbdefault job.
#   → edit the  >>> YOUR COMMAND HERE <<<  block in the generated file
$H/bb-submit.sh bb-jobs/my-first/my-first.sbatch --watch   # rsync + sbatch + follow to terminal
$H/bb-fetch.sh my-first                        # pull outputs back to ./bb-jobs/my-first/
```

`bb-new-job.sh` kinds: `cpu` · `gpu` · `vllm` · `array` · `python`. Each picks a sensible QoS
and resources (override with `--qos`, `--walltime`, `--gres`, `--cpus`, `--mem`). For GPUs and
serving, see the `gpu-and-serving` skill.

## The bbshort iteration loop (how to debug fast)

`bbshort` jobs start in **0–60 s** and cap at **10 minutes**, so a failed attempt costs ~3
minutes, not a queue-day. The proven cycle — **change one variable per attempt**:

1. **Diagnose** from the job's `.out` log (the ground truth), not from guesses:
   `$H/bb-watch.sh <job_id>` or read `…/slurm-<id>.out`.
2. **Fix** one thing in the local `.sbatch`.
3. **Relaunch** under bbshort: `$H/bb-submit.sh <file> --watch` (re-add `--short` when scaffolding,
   or set `#SBATCH --qos=bbshort` / `--time=00:10:00`).

Use bbshort to debug the *plumbing*; switch to `bbgpu`/`bbdefault` only once it works and the
real job needs more than 10 minutes.

## What's under the hood (raw sbatch)

The harness writes ordinary sbatch scripts. The directives that matter on BlueBEAR:

```bash
#!/bin/bash
#SBATCH --account=your-project     # REQUIRED — your project code
#SBATCH --qos=bbshort                          # bbshort(10m) | bbgpu(2d) | bbdefault(CPU). NOT bbcpu.
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:10:00                        # must be ≤ the QoS MaxWall
#SBATCH --job-name=my-first
#SBATCH --output=/rds/projects/p/your-project/.bear-harness/launchpad/my-first/slurm-%j.out
set -euo pipefail
module purge
module load bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0 CUDA/12.6.0
RUN=/rds/.../my-first/$SLURM_JOB_ID; mkdir -p "$RUN"   # write artifacts to RDS, never /tmp
# ... your command, writing to "$RUN" ...
```

Submit + inspect manually:

```bash
ssh your-username@bluebear.bham.ac.uk 'sbatch my-first.sbatch'   # → "Submitted batch job <id>"
ssh your-username@bluebear.bham.ac.uk 'squeue -u $USER'          # PD (pending) → R (running) → gone
ssh your-username@bluebear.bham.ac.uk 'sacct -j <id> --format=JobID,State,Elapsed,ExitCode'
```

## Array jobs (fan-out / sweeps)

Scaffold with `bb-new-job.sh array <name>`. The key directive is `--array=0-N%C` (indices
`0..N`, at most `C` concurrent). Each task reads `$SLURM_ARRAY_TASK_ID` to pick its slice.
Template at `${CLAUDE_PLUGIN_ROOT}/harness/templates/array-job.sbatch`.

## Common first-run failures

- **Rejected at submit** → almost always a bad QoS (`bbcpu`!), a bad GRES string, or
  `--time` over the QoS MaxWall. Re-check against `cluster-ground-truth.md`.
- **`Lmod ... module(s) are unknown` on the compute node** → the CUDA module rotated; run
  `module avail CUDA` and update the `module load` line.
- **Output went to the wrong place / job dir** → `--output`'s directory must exist at submit;
  the harness creates it for you (rsync). Writing artifacts to `/tmp` loses them — use the RDS
  `$RUN` dir.

When a job is misbehaving, switch to the **observability** skill.
