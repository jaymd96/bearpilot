#!/bin/bash
# {{JOBNAME}} — a SLURM array: fan one script out over many tasks (parameter sweeps,
# per-shard processing). Each task gets a distinct $SLURM_ARRAY_TASK_ID.
# Scaffolded by bb-new-job.sh.  Submit with:  bb-submit.sh {{JOBNAME}}.sbatch
#
#SBATCH --account={{ACCOUNT}}
#SBATCH --qos={{QOS}}
#SBATCH --cpus-per-task={{CPUS}}
#SBATCH --mem={{MEM}}
#SBATCH --time={{WALLTIME}}
#SBATCH --job-name={{JOBNAME}}
#SBATCH --output={{JOBS_DIR}}/{{JOBNAME}}/slurm-%A_%a.out
#SBATCH --array=0-3%4                            # >>> EDIT: indices 0..3, at most 4 concurrent (the %N cap) <<<
#SBATCH --mail-user={{MAILUSER}}
#SBATCH --mail-type=END,FAIL
set -euo pipefail

module purge
module load {{MODULE_LINE}}
RDS={{RDS_ROOT}}
TASK=$SLURM_ARRAY_TASK_ID
RUN={{JOBS_DIR}}/{{JOBNAME}}/${SLURM_ARRAY_JOB_ID}/task-${TASK}
mkdir -p "$RUN"
echo "host=$(hostname) array_job=$SLURM_ARRAY_JOB_ID task=$TASK start=$(date -Iseconds)"

# --- per-task work -----------------------------------------------------------
# Map $TASK → this task's parameter. Example: a list of seeds/shards.
PARAMS=(alpha beta gamma delta)                 # >>> EDIT: one entry per array index <<<
THIS="${PARAMS[$TASK]}"
echo "task $TASK handles: $THIS  (out: $RUN)"
# >>> YOUR COMMAND HERE <<<
# python3 process.py --shard "$THIS" --out "$RUN/out.json"

echo "{{JOBNAME}}_TASK_${TASK}_DONE rc=$? $(date -Iseconds)"
