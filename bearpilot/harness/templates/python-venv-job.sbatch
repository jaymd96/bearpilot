#!/bin/bash
# {{JOBNAME}} — run a Python script in a per-project virtualenv on RDS.
# The venv is created once on RDS and reused; deps install on first run only.
# Scaffolded by bb-new-job.sh.  Submit with:  bb-submit.sh {{JOBNAME}}.sbatch --watch
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

module purge
module load {{MODULE_LINE}}
RDS={{RDS_ROOT}}
export HF_HOME={{HF_CACHE}}
VENV={{JOBS_DIR}}/venvs/{{JOBNAME}}              # persistent venv on RDS (reused across runs)
RUN={{JOBS_DIR}}/{{JOBNAME}}/$SLURM_JOB_ID
mkdir -p "$RUN" "$(dirname "$VENV")"
echo "host=$(hostname) job=$SLURM_JOB_ID start=$(date -Iseconds)"

# --- venv (create once, reuse thereafter) ------------------------------------
if [ ! -d "$VENV" ]; then
  echo "creating venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  # >>> EDIT: your dependencies (installed once) <<<
  # "$VENV/bin/pip" install numpy pandas
fi
source "$VENV/bin/activate"
python --version

# --- work --------------------------------------------------------------------
# Your code is in this job dir (rsync'd by bb-submit.sh). $PWD is the job dir on the cluster.
# >>> YOUR COMMAND HERE <<<
echo "Replace this with your Python command. Output dir: $RUN"
# python my_script.py --out "$RUN/result.json"

echo "{{JOBNAME}}_DONE rc=$? $(date -Iseconds)"
