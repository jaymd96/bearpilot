#!/bin/bash
# {{JOBNAME}} — a single-A100 GPU job on BlueBEAR.
# Scaffolded by bb-new-job.sh.  Submit with:  bb-submit.sh {{JOBNAME}}.sbatch --watch
#
#SBATCH --account={{ACCOUNT}}
#SBATCH --qos={{QOS}}
{{GRES_LINE}}
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
module load {{MODULE_LINE}}                     # includes CUDA — version ROTATES, re-confirm if it fails here
RDS={{RDS_ROOT}}
export HF_HOME={{HF_CACHE}}
RUN={{JOBS_DIR}}/{{JOBNAME}}/$SLURM_JOB_ID
mkdir -p "$RUN"
echo "host=$(hostname) job=$SLURM_JOB_ID start=$(date -Iseconds)"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader   # expect A100-SXM4-40GB

# --- work --------------------------------------------------------------------
# >>> YOUR COMMAND HERE <<<
# A GPU is allocated (--gres). Use it directly, or inside a container with `apptainer exec --nv`.
echo "Replace this with your GPU command. Output dir: $RUN"
# python3 train.py --out "$RUN"

echo "{{JOBNAME}}_DONE rc=$? $(date -Iseconds)"
