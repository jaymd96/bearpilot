---
name: observability
description: Track, monitor, and debug running jobs on BlueBEAR. Use when the user asks what's running on the cluster, wants to monitor or watch a job, check job status or history, follow logs, debug a failing or hung job, or recover/reattach to a run after disconnecting. Encodes the core discipline — pin SSH to one round-robin login node and trust shared-FS artifacts plus sacct, never a PID.
---

# Observing BlueBEAR jobs

The single most important operational lesson on this cluster, because getting it wrong makes
you chase ghosts:

> **Pin SSH to one login node, and key every watcher on durable signals — the shared-FS run
> files plus `sacct`/`squeue` — NEVER on a PID.** Login nodes are round-robin, so PIDs,
> `/tmp`, and `nohup` do not survive a reconnect. A PID-based watcher will lie to you.

## What's running right now

```bash
H=${CLAUDE_PLUGIN_ROOT}/harness
$H/bb-jobs.sh            # live: your queued/running jobs (squeue)
$H/bb-jobs.sh --all      # + today's finished jobs (sacct) + the newest RDS run dirs
```

Equivalently, by hand (authoritative, node-agnostic):

```bash
ssh your-username@bluebear.bham.ac.uk 'squeue -u $USER -o "%.10i %.22j %.9q %.8T %.10M %.10l %R"'
ssh your-username@bluebear.bham.ac.uk 'sacct -u $USER -S today --format=JobID,JobName,State,Elapsed,ExitCode,MaxRSS'
```

`squeue` shows live state (`PD` pending → `R` running → gone); `sacct` is the durable
accounting record (it still answers after the job leaves the queue, and it survives the
round-robin login nodes). A dependent job sitting `PD` with reason `Dependency` is normal — it
waits for its upstream to start.

## Follow one job to completion

```bash
$H/bb-watch.sh <job_id>             # one line per STATE CHANGE + tails the .out log
$H/bb-watch.sh <job_id> --interval 5
```

It exits **0** on `COMPLETED` and **non-zero** on any failure state
(`FAILED`/`TIMEOUT`/`OUT_OF_MEMORY`/`CANCELLED`/…), so it composes in scripts. It polls
`sacct` + the shared-FS `.out` log — never a PID — so it keeps working even if your SSH
reconnects to a different login node mid-watch.

## Pin a login node first (why `bb-connect.sh` matters)

`bb-connect.sh` opens an SSH **ControlMaster** to one login node and persists it (~8h). Every
subsequent `bb-*` call reuses that one connection, so a watch stays attached to the *same*
node's view and you're not re-authenticating each poll. Tear it down with
`bb-connect.sh --stop`. (All scripts still work without it — they just open a fresh connection
each call — but a long watch should pin.)

## Debug a failing job — follow the data

1. **Read the `.out` log** — it is the ground truth, not your hypothesis:
   `$H/bb-watch.sh <id>` (live) or read `…/launchpad/<name>/slurm-<id>.out` /
   `…/<name>/<id>/serve.log` for a vLLM server.
2. **Check `sacct` for the real exit code and `MaxRSS`** — `OUT_OF_MEMORY` vs a code-1
   `FAILED` vs `TIMEOUT` point at very different fixes (more `--mem` / a bug / more `--time`).
3. **Reproduce on bbshort** with one variable changed (the iteration loop in the `batch-jobs`
   skill), not a speculative rewrite.

Common signatures:

| Symptom | Likely cause |
|---|---|
| `FAILED` instantly, nothing in log | bad `--account`/`--qos`/`--gres` (rejected at submit) — check `bb-jobs.sh --all` ExitCode and re-read `cluster-ground-truth.md` |
| `Lmod ... unknown` in the log | CUDA module rotated → `module avail CUDA` |
| serving job hangs, no endpoint | compute node can't reach Hugging Face → pre-cache weights in `$RDS/hf_cache` (gotcha 9) |
| `OUT_OF_MEMORY` | raise `--mem` (CPU RAM) or lower vLLM `--gpu-memory-utilization` / `--max-model-len` (GPU RAM) |
| `TIMEOUT` | job exceeded `--time`/QoS MaxWall — move bbshort→bbgpu/bbdefault |

## Reattach after disconnecting

Because state is on the shared filesystem keyed by job id, you lose nothing by closing your
laptop. Reconnect and:

```bash
$H/bb-jobs.sh --all          # find the job again
$H/bb-watch.sh <job_id>      # resume following it
$H/bb-fetch.sh --id <job_id> # pull its outputs once terminal
```

For first-class reattach-by-`run_id` of a whole multi-job campaign (not just one sbatch),
that's **bear-harness** — see that skill.
