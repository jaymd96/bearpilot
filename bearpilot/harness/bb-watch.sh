#!/usr/bin/env bash
# bb-watch.sh — follow a BlueBEAR job to a terminal state using durable signals only:
# sacct/squeue state (node-agnostic accounting) + the job's shared-FS .out log. NEVER a PID.
#
#   bb-watch.sh <job_id> [--interval SECONDS] [--no-log]
#
# Prints one line per STATE CHANGE, periodically tails the log, and exits 0 on COMPLETED,
# non-zero on any failure state — so it composes in scripts and survives a reconnect to a
# different login node.
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

JOB_ID="${1:-}"; [[ "$JOB_ID" =~ ^[0-9]+ ]] || bb_die "usage: bb-watch.sh <job_id> [--interval N] [--no-log]"
shift
INTERVAL=10; SHOW_LOG=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --no-log)   SHOW_LOG=0; shift ;;
    *) bb_die "unknown option '$1'" ;;
  esac
done

bb_cm_start
bb_require_ssh

TERMINAL='COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|DEADLINE|PREEMPTED|BOOT_FAIL'

# Resolve the StdOut path once (works while the job is on the controller); else search RDS.
LOG_PATH="$(bb_ssh_login "scontrol show job ${JOB_ID} 2>/dev/null | sed -n 's/.*StdOut=//p' | head -1")"
if [[ -z "$LOG_PATH" ]]; then
  LOG_PATH="$(bb_ssh "find ${BB_JOBS_DIR} ${BB_RUNS_DIR} -name 'slurm-${JOB_ID}.out' -o -name '*-${JOB_ID}.out' 2>/dev/null | head -1")"
fi
[[ -n "$LOG_PATH" ]] && bb_log "log: ${LOG_PATH}" || bb_warn "no .out log located yet (job may not have started)"

bb_log "Watching job ${JOB_ID} (every ${INTERVAL}s; trusting sacct + shared-FS, never a PID)..."
LAST_STATE=""; LOG_LINES=0
while true; do
  # sacct is authoritative and includes terminal states; squeue covers the brief pre-sacct window.
  STATE="$(bb_ssh_login "sacct -nj ${JOB_ID} --format=State%24 2>/dev/null | head -1 | tr -d ' '")"
  [[ -z "$STATE" ]] && STATE="$(bb_ssh_login "squeue -hj ${JOB_ID} -o %T 2>/dev/null | head -1")"
  [[ -z "$STATE" ]] && STATE="UNKNOWN"

  if [[ "$STATE" != "$LAST_STATE" ]]; then
    printf '\033[36m[bb %s]\033[0m %s\n' "$(date +%H:%M:%S)" "job ${JOB_ID} → ${STATE}" >&2
    LAST_STATE="$STATE"
  fi

  if [[ "$SHOW_LOG" == 1 && -n "$LOG_PATH" ]]; then
    NEW="$(bb_ssh "tail -n +$((LOG_LINES + 1)) '${LOG_PATH}' 2>/dev/null")"
    if [[ -n "$NEW" ]]; then
      printf '%s\n' "$NEW"
      LOG_LINES=$(( LOG_LINES + $(printf '%s\n' "$NEW" | wc -l) ))
    fi
  fi

  if [[ "$STATE" =~ ^($TERMINAL) ]]; then
    bb_log "Terminal: ${STATE}."
    bb_ssh_login "sacct -j ${JOB_ID} --format=JobID%14,JobName%20,State%16,Elapsed,ExitCode,MaxRSS 2>/dev/null" || true
    [[ "$STATE" == COMPLETED* ]] && exit 0 || exit 1
  fi
  sleep "$INTERVAL"
done
