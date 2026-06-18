#!/usr/bin/env bash
# bb-new-job.sh — scaffold a ready-to-edit sbatch job from a proven template.
#
# Substitutes the cluster ground-truth (account, QoS, GRES, modules, RDS paths) into a
# template so you get a correct sbatch without memorising any cluster string.
#
#   bb-new-job.sh <kind> <name> [options]
#
#   kinds:   cpu     a plain CPU job            (qos bbdefault)
#            gpu     a single-A100 GPU job      (qos bbgpu, gres gpu:a100:1)
#            vllm    serve a model with vLLM    (qos bbgpu, gres gpu:a100:1, boot-wait loop)
#            array   a SLURM array (fan-out)    (qos bbshort)
#            python  run a Python script in a venv (qos bbshort)
#
#   options: --qos Q  --walltime HH:MM:SS  --gres G  --cpus N  --mem NNG
#            --dir DIR (where to write; default ./bb-jobs/<name>)
#            --short   shorthand for --qos bbshort --walltime 00:10:00 (the debug loop)
#
# Example:
#   bb-new-job.sh gpu my-train --walltime 02:00:00
#   bb-new-job.sh python quick-test --short
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

KIND="${1:-}"; NAME="${2:-}"
[[ -n "$KIND" && -n "$NAME" ]] || bb_die "usage: bb-new-job.sh <cpu|gpu|vllm|array|python> <name> [options]"
shift 2 || true

# --- per-kind defaults ---------------------------------------------------------
case "$KIND" in
  cpu)    QOS="$BB_QOS_CPU"; GRES=""; CPUS=4; MEM=16G; WALL=01:00:00; TMPL=cpu-job.sbatch ;;
  gpu)    QOS="$BB_QOS_GPU"; GRES="$BB_GRES"; CPUS=8; MEM=64G; WALL=01:00:00; TMPL=gpu-job.sbatch ;;
  vllm)   QOS="$BB_QOS_GPU"; GRES="$BB_GRES"; CPUS=8; MEM=64G; WALL=02:00:00; TMPL=vllm-serve.sbatch ;;
  array)  QOS="$BB_QOS_SHORT"; GRES=""; CPUS=2; MEM=8G; WALL=00:10:00; TMPL=array-job.sbatch ;;
  python) QOS="$BB_QOS_SHORT"; GRES=""; CPUS=4; MEM=16G; WALL=00:10:00; TMPL=python-venv-job.sbatch ;;
  *) bb_die "unknown kind '$KIND' (want: cpu|gpu|vllm|array|python)" ;;
esac

DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --qos)      QOS="$2"; shift 2 ;;
    --walltime) WALL="$2"; shift 2 ;;
    --gres)     GRES="$2"; shift 2 ;;
    --cpus)     CPUS="$2"; shift 2 ;;
    --mem)      MEM="$2"; shift 2 ;;
    --dir)      DIR="$2"; shift 2 ;;
    --short)    QOS="$BB_QOS_SHORT"; WALL=00:10:00; shift ;;
    *) bb_die "unknown option '$1'" ;;
  esac
done

DIR="${DIR:-./bb-jobs/${NAME}}"
TEMPLATE="${HERE}/templates/${TMPL}"
[[ -f "$TEMPLATE" ]] || bb_die "template not found: $TEMPLATE"
mkdir -p "$DIR"
OUT="${DIR}/${NAME}.sbatch"

# A GRES line only for GPU kinds (the {{GRES_LINE}} token expands to a full #SBATCH line or "").
if [[ -n "$GRES" ]]; then GRES_LINE="#SBATCH --gres=${GRES}"; else GRES_LINE="# (no GPU requested)"; fi

# --- token substitution --------------------------------------------------------
# Use a sed program with | delimiters (paths contain /). Values here never contain |.
sed \
  -e "s|{{JOBNAME}}|${NAME}|g" \
  -e "s|{{ACCOUNT}}|${BB_ACCOUNT}|g" \
  -e "s|{{QOS}}|${QOS}|g" \
  -e "s|{{GRES_LINE}}|${GRES_LINE}|g" \
  -e "s|{{GRES}}|${GRES}|g" \
  -e "s|{{WALLTIME}}|${WALL}|g" \
  -e "s|{{CPUS}}|${CPUS}|g" \
  -e "s|{{MEM}}|${MEM}|g" \
  -e "s|{{MAILUSER}}|${BB_MAIL_USER}|g" \
  -e "s|{{MODULE_LINE}}|${BB_MODULE_LINE}|g" \
  -e "s|{{RDS_ROOT}}|${BB_RDS_ROOT}|g" \
  -e "s|{{RUNS_DIR}}|${BB_RUNS_DIR}|g" \
  -e "s|{{JOBS_DIR}}|${BB_JOBS_DIR}|g" \
  -e "s|{{HF_CACHE}}|${BB_HF_CACHE}|g" \
  -e "s|{{SIF}}|${BB_SIF}|g" \
  "$TEMPLATE" > "$OUT"

bb_log "Scaffolded ${KIND} job → ${OUT}"
bb_log "  account=${BB_ACCOUNT}  qos=${QOS}  walltime=${WALL}  cpus=${CPUS}  mem=${MEM}${GRES:+  gres=${GRES}}"
echo
echo "Next:"
echo "  1. Edit the marked  >>> YOUR COMMAND HERE <<<  section in ${OUT}"
echo "  2. Submit + watch:   bb-submit.sh ${OUT}"
