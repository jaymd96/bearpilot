#!/bin/bash
#SBATCH --account=your-project
#SBATCH --qos=bbshort
#SBATCH --time=00:10:00
#SBATCH --cpus-per-task=16
#SBATCH --mem=48G
#SBATCH --job-name=sif-build
#SBATCH --output=/rds/projects/p/your-project/.bear-harness/apptainer/sif-build-%j.out

set -euo pipefail
RDS=/rds/projects/p/your-project
cd "$RDS/.bear-harness/apptainer"
export APPTAINER_CACHEDIR="$RDS/.bear-harness/apptainer/cache"

BASE=""
for c in /scratch "${TMPDIR:-}"; do
  if [ -n "$c" ] && [ -d "$c" ] && [ -w "$c" ]; then BASE="$c"; break; fi
done
[ -z "$BASE" ] && BASE="$RDS/.bear-harness/apptainer"
export APPTAINER_TMPDIR="$BASE/build-tmp-$SLURM_JOB_ID"
mkdir -p "$APPTAINER_TMPDIR"
trap 'rm -rf "$APPTAINER_TMPDIR"' EXIT
echo "build host: $(hostname)  tmpdir: $APPTAINER_TMPDIR  start: $(date -Iseconds)"
df -h "$APPTAINER_TMPDIR" | tail -1

apptainer pull --force vllm-openai.sif docker://vllm/vllm-openai:latest
ls -la vllm-openai.sif
echo "--- vllm flag audit ---"
apptainer exec vllm-openai.sif vllm serve --help > vllm-help.txt 2>&1 || true
for f in performance-mode num-scheduler-steps enforce-eager enable-prefix-caching gpu-memory-utilization; do
  grep -q -- "--$f" vllm-help.txt && echo "FLAG_OK --$f" || echo "FLAG_MISSING --$f"
done
apptainer exec vllm-openai.sif python3 -c "import vllm; print('vllm_version', vllm.__version__)" 2>/dev/null | tail -1
echo "SIF_BUILD_OK end: $(date -Iseconds)"
