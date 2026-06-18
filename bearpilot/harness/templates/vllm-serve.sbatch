#!/bin/bash
# {{JOBNAME}} — serve a model with vLLM on an A100, then run a client command against it.
# Proven recipe: apptainer EXEC (not run — run strips JSON-arg quotes), --nv for the GPU,
# --bind for the HF cache, a boot-wait loop polling /v1/models with a Bearer key.
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
set -uo pipefail

# --- config (edit MODEL; the rest is filled from ground-truth) ---------------
RDS={{RDS_ROOT}}
export HF_HOME={{HF_CACHE}}
SIF={{SIF}}                                     # vLLM image (.sif). A diffusion build: vllm-openai-gemma.sif
MODEL="Qwen/Qwen2.5-7B-Instruct"                # >>> EDIT: the model to serve (must fit a 40 GB A100) <<<
PORT=$(( 8300 + (RANDOM % 90) ))
KEY="bb-{{JOBNAME}}-key"
RUN={{JOBS_DIR}}/{{JOBNAME}}/$SLURM_JOB_ID
mkdir -p "$RUN"; SERVELOG=$RUN/serve.log
BOOT_TIMEOUT=600                                # bbgpu: raise for big models. bbshort caps the whole job at 10 min.

module purge
module load {{MODULE_LINE}}
echo "host=$(hostname) port=$PORT job=$SLURM_JOB_ID start=$(date -Iseconds)"

# --- serve -------------------------------------------------------------------
apptainer exec --nv --bind "$HF_HOME:$HF_HOME" --env HF_HOME="$HF_HOME" "$SIF" \
  vllm serve "$MODEL" \
  --host 0.0.0.0 --port "$PORT" --api-key "$KEY" \
  --max-model-len 8192 --gpu-memory-utilization 0.85 \
  --enforce-eager > "$SERVELOG" 2>&1 &
VLLM_PID=$!

# --- wait for readiness (poll /v1/models; vLLM wants Authorization: Bearer) --
START=$(date +%s); DEADLINE=$(( START + BOOT_TIMEOUT )); READY=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then echo "VLLM_DIED_EARLY"; tail -n 30 "$SERVELOG"; break; fi
  if curl -fsS -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then READY=1; break; fi
  sleep 5
done
echo "READY=$READY boot_seconds=$(( $(date +%s) - START ))"
if [ "$READY" -ne 1 ]; then echo "ABORT_NO_SERVER"; kill -TERM "$VLLM_PID" 2>/dev/null; exit 3; fi
nvidia-smi --query-gpu=memory.used --format=csv,noheader

# --- client command ----------------------------------------------------------
# The server is live at  http://127.0.0.1:$PORT  with api-key $KEY.
# >>> YOUR CLIENT COMMAND HERE <<<
echo "Server up. Replace this with your client. Endpoint: http://127.0.0.1:$PORT  key: $KEY"
# curl -fsS -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
#   "http://127.0.0.1:$PORT/v1/chat/completions" \
#   -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}" | tee "$RUN/reply.json"

# --- teardown ----------------------------------------------------------------
kill -TERM "$VLLM_PID" 2>/dev/null || true
echo "{{JOBNAME}}_DONE $(date -Iseconds)"
