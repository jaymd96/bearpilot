# The launchpad harness

A **zero-dependency** (pure bash + ssh + rsync) harness for building, submitting, watching,
and fetching BlueBEAR jobs. It is the *simple* on-ramp — no Python, no install. When you
outgrow it (autonomous deploys, server+worker JobGraphs, guardrails, reattach-by-`run_id`),
graduate to **bear-harness** — see the `bear-harness` skill.

Everything here encodes the cluster ground-truth (`../references/cluster-ground-truth.md`)
and the operational discipline (`../references/gotchas.md`) so you can't accidentally regress
into the traps: it pins SSH to one round-robin login node, keys every watcher on shared-FS
state + `sacct` (never a PID), and fills correct QoS/GRES/module strings into every job.

## The loop

```bash
HARNESS=./bearpilot/harness        # (or ${CLAUDE_PLUGIN_ROOT}/harness inside Claude Code)

$HARNESS/bb-connect.sh                       # 1. pin one login node + probe live ground-truth
$HARNESS/bb-new-job.sh gpu my-train          # 2. scaffold a correct sbatch from a template
#    ...edit bb-jobs/my-train/my-train.sbatch — fill the  >>> YOUR COMMAND HERE <<<  block
$HARNESS/bb-submit.sh bb-jobs/my-train/my-train.sbatch --watch   # 3. push + submit + follow
$HARNESS/bb-jobs.sh --all                    #    what's running / what just ran
$HARNESS/bb-fetch.sh my-train                # 4. pull outputs back to ./bb-jobs/my-train/
```

## Scripts

| Script | Does |
|---|---|
| `lib/common.sh` | Shared config (every value overridable via a `BB_*` env var) + SSH/rsync helpers. Sourced by all scripts. |
| `bb-connect.sh` | Opens a pinned SSH ControlMaster to one login node; probes live account/QoS/GRES/CUDA next to the encoded defaults so drift is visible. `--stop` tears it down. |
| `bb-new-job.sh` | Scaffolds `<name>.sbatch` from a template (`cpu`/`gpu`/`vllm`/`array`/`python`), substituting cluster strings. `--short` = the 10-min `bbshort` debug profile. |
| `bb-submit.sh` | rsyncs the job dir to RDS, submits, prints the job id. `--watch` hands off to `bb-watch.sh`. |
| `bb-watch.sh` | Follows a job to a terminal state via `sacct` + the shared-FS `.out` log. Exits 0 on COMPLETED, non-zero on failure. **Never polls a PID.** |
| `bb-jobs.sh` | Live `squeue` + (with `--all`) today's `sacct` history + the newest RDS run dirs. |
| `bb-fetch.sh` | rsyncs a job's outputs back to your laptop (by name or `--id <job_id>`). |

## Templates (`templates/`)

`cpu-job` · `gpu-job` · `vllm-serve` (the proven apptainer-exec serve recipe with a boot-wait
loop) · `array-job` (SLURM array fan-out) · `python-venv-job` (persistent RDS venv). Each
carries a clearly-marked `>>> YOUR COMMAND HERE <<<` block and writes durable artifacts to a
per-run dir on RDS (never node-local `/tmp`).

## Overriding for another account / cluster

Every default lives in `lib/common.sh` behind a `BB_*` env var. To point the harness at a
different project or user without editing anything:

```bash
BB_USER=abc123 BB_ACCOUNT=other-project BB_RDS_ROOT=/rds/projects/x/other \
  ./bb-connect.sh
```

## What this harness deliberately does NOT do

Long-lived detached campaigns, dependency edges between a server and its worker, default-deny
resource guardrails, notify-on-done, and reattach-by-`run_id` from a fresh session — those are
**bear-harness's** job (the advanced path). This harness is the fast, legible, dependency-free
way to get a single job (or array) onto the cluster and its results back.
