#!/usr/bin/env bash
# bb-fetch.sh — pull a job's outputs back from RDS to your laptop.
#
#   bb-fetch.sh <name> [--dest DIR]     # fetch ${BB_JOBS_DIR}/<name>/  → ./bb-jobs/<name>/ (or DIR)
#   bb-fetch.sh --id <job_id> [--dest DIR]   # resolve the job dir from a job id, then fetch
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

NAME=""; JOB_ID=""; DEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --id)   JOB_ID="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    -*) bb_die "unknown option '$1'" ;;
    *)  NAME="$1"; shift ;;
  esac
done

bb_cm_start
bb_require_ssh

if [[ -z "$NAME" && -n "$JOB_ID" ]]; then
  REMOTE_DIR="$(bb_ssh "dirname \$(find ${BB_JOBS_DIR} ${BB_RUNS_DIR} -name 'slurm-${JOB_ID}.out' -o -name '*-${JOB_ID}.out' 2>/dev/null | head -1) 2>/dev/null")"
  [[ -n "$REMOTE_DIR" ]] || bb_die "could not locate a job dir for id ${JOB_ID}"
  NAME="$(basename "$REMOTE_DIR")"
else
  [[ -n "$NAME" ]] || bb_die "usage: bb-fetch.sh <name> [--dest DIR]   |   bb-fetch.sh --id <job_id>"
  REMOTE_DIR="${BB_JOBS_DIR}/${NAME}"
fi

DEST="${DEST:-./bb-jobs/${NAME}}"
mkdir -p "$DEST"
bb_log "Fetching ${BB_REMOTE}:${REMOTE_DIR}/ → ${DEST}/"
bb_rsync -az --progress "${BB_REMOTE}:${REMOTE_DIR}/" "${DEST}/"
bb_log "Done. Contents:"
ls -la "$DEST" >&2
