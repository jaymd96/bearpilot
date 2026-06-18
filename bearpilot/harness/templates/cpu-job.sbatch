#!/bin/bash
# {{JOBNAME}} — a plain CPU job on BlueBEAR.
# Scaffolded by bb-new-job.sh; cluster strings are filled from the ground-truth.
# Submit with:  bb-submit.sh {{JOBNAME}}.sbatch --watch
#
#SBATCH --account={{ACCOUNT}}
#SBATCH --qos={{QOS}}
#SBATCH --cpus-per-task={{CPUS}}
#SBATCH --mem={{MEM}}
#SBATCH --time={{WALLTIME}}
#SBATCH --job-name={{JOBNAME}}
#SBATCH --output={{JOBS_DIR}}/{{JOBNAME}}/slurm-%j.out
#SBATCH --mail-user={{MAILUSER}}
#SBATCH --mail-type=END,FAIL
set -euo pipefail

# --- environment -------------------------------------------------------------
module purge
module load {{MODULE_LINE}}
RDS={{RDS_ROOT}}
RUN={{JOBS_DIR}}/{{JOBNAME}}/$SLURM_JOB_ID      # durable per-run output dir on RDS
mkdir -p "$RUN"
echo "host=$(hostname) job=$SLURM_JOB_ID start=$(date -Iseconds)"

# --- work --------------------------------------------------------------------
# >>> YOUR COMMAND HERE <<<
# Anything that runs on CPU. Write durable artifacts under "$RUN" (NOT /tmp — it is
# node-local and ephemeral). Example:
echo "Replace this with your command. Output dir: $RUN"
# python3 my_script.py --out "$RUN/result.json"

echo "{{JOBNAME}}_DONE rc=$? $(date -Iseconds)"
