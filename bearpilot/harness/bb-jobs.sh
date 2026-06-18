#!/usr/bin/env bash
# bb-jobs.sh — what's running (and what just ran) on BlueBEAR, from the authoritative,
# node-agnostic sources: live squeue + sacct accounting. Never PIDs.
#
#   bb-jobs.sh                 # live: your queued/running jobs
#   bb-jobs.sh --all           # + today's finished jobs (sacct)
#   bb-jobs.sh --since 2026-06-01   # sacct history from a date
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

SINCE=""; ALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   ALL=1; shift ;;
    --since) SINCE="$2"; ALL=1; shift 2 ;;
    *) bb_die "unknown option '$1'" ;;
  esac
done
SINCE="${SINCE:-today}"

bb_cm_start
bb_require_ssh

echo "=== LIVE — queued / running (squeue) ============================="
bb_ssh_login 'squeue -u $USER -o "%.10i %.22j %.9P %.9q %.8T %.10M %.10l %.6D %R" 2>/dev/null || echo "(squeue unavailable)"'

if [[ "$ALL" == 1 ]]; then
  echo
  echo "=== HISTORY — since ${SINCE} (sacct) ============================="
  bb_ssh_login "sacct -u \$USER -S ${SINCE} --format=JobID%14,JobName%22,State%14,Elapsed,ExitCode,MaxRSS 2>/dev/null || echo '(sacct unavailable)'"
fi

echo
echo "=== HARNESS RUN STATE — newest run dirs on RDS ==================="
# The durable, reattachable source of truth (survives the round-robin login nodes).
bb_ssh "ls -1dt ${BB_RUNS_DIR}/*/ 2>/dev/null | head -8 || echo '(no runs dir yet)'"
echo "(inspect one with: bb-watch.sh <job_id>   or read its run.json / *.out)"
