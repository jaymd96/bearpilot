#!/usr/bin/env bash
# bb-submit.sh — push a local job dir to BlueBEAR and submit it with sbatch.
#
# Syncs the sbatch's containing directory to ${BB_JOBS_DIR}/<name>/ on RDS, submits, and
# prints the SLURM job id. With --watch it hands off to bb-watch.sh immediately.
#
#   bb-submit.sh path/to/<name>.sbatch [--watch] [--no-sync] [-- <extra sbatch args>]
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

SBATCH_FILE="${1:-}"; [[ -f "$SBATCH_FILE" ]] || bb_die "usage: bb-submit.sh <file.sbatch> [--watch] [--no-sync]"
shift
WATCH=0; SYNC=1; EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --watch)   WATCH=1; shift ;;
    --no-sync) SYNC=0; shift ;;
    --)        shift; EXTRA+=("$@"); break ;;
    *) bb_die "unknown option '$1'" ;;
  esac
done

LOCAL_DIR="$(cd "$(dirname "$SBATCH_FILE")" && pwd)"
SCRIPT="$(basename "$SBATCH_FILE")"
NAME="${SCRIPT%.sbatch}"
REMOTE_DIR="${BB_JOBS_DIR}/${NAME}"

bb_cm_start
bb_require_ssh

if [[ "$SYNC" == 1 ]]; then
  bb_log "Syncing ${LOCAL_DIR}/ → ${BB_REMOTE}:${REMOTE_DIR}/"
  bb_ssh "mkdir -p ${REMOTE_DIR}"
  bb_rsync -az --exclude='.git' --exclude='*.out' "${LOCAL_DIR}/" "${BB_REMOTE}:${REMOTE_DIR}/"
fi

bb_log "Submitting ${SCRIPT} on the cluster..."
# Run sbatch from the job dir so relative paths resolve; capture the id from its output.
SUBMIT_OUT="$(bb_ssh_login "cd ${REMOTE_DIR} && sbatch ${EXTRA[*]:-} ${SCRIPT}")" \
  || bb_die "sbatch failed:\n${SUBMIT_OUT}"
echo "$SUBMIT_OUT" >&2
JOB_ID="$(printf '%s\n' "$SUBMIT_OUT" | grep -oE '[0-9]+' | tail -1)"
[[ -n "$JOB_ID" ]] || bb_die "could not parse a job id from: ${SUBMIT_OUT}"

bb_log "Submitted job ${JOB_ID}  (logs → ${REMOTE_DIR}/slurm-${JOB_ID}.out)"
printf '%s\n' "$JOB_ID"   # the one line on stdout — pipe-friendly

if [[ "$WATCH" == 1 ]]; then
  bb_log "Watching ${JOB_ID}..."
  exec "${HERE}/bb-watch.sh" "$JOB_ID"
fi
