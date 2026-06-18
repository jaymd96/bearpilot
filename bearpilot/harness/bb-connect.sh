#!/usr/bin/env bash
# bb-connect.sh — open a pinned SSH master to BlueBEAR and probe the LIVE ground-truth.
#
# Round-robin login nodes mean node-local state lies; this pins you to ONE node (a
# ControlMaster) for the session and prints the cluster's own answers next to this
# harness's encoded defaults, so any drift (a renamed QoS, a rotated CUDA module) is
# visible before it fails a job.
#
#   bb-connect.sh            # connect + probe
#   bb-connect.sh --stop     # tear the pinned master down
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "${HERE}/lib/common.sh"

if [[ "${1:-}" == "--stop" ]]; then bb_cm_stop; exit 0; fi

bb_cm_start
bb_require_ssh
bb_log "Connected. Probing live cluster ground-truth (compare against the encoded defaults)..."

echo "──────────────────────────────────────────────────────────────────────"
bb_show_config
echo "──────────────────────────────────────────────────────────────────────"

bb_ssh_login '
  echo "=== whoami / host ===";        whoami; hostname
  echo "=== account + QoS (live) ===";  sacctmgr -nP show assoc user=$USER format=account,qos 2>/dev/null || echo "(sacctmgr unavailable)"
  echo "=== partitions + GRES (live) ==="; sinfo -o "%P %G %D %t" 2>/dev/null | head -20 || echo "(sinfo unavailable)"
  echo "=== CUDA modules (live — pick the (D) default) ==="; module avail CUDA 2>&1 | grep -i cuda | head -10 || echo "(no module system?)"
' || bb_warn "Some probes failed — the cluster may have changed shape; re-read references/cluster-ground-truth.md."

echo "──────────────────────────────────────────────────────────────────────"
bb_log "Checking the RDS run-state layout exists..."
bb_ssh "for d in '${BB_RUNS_DIR}' '${BB_HF_CACHE}' '${BB_HARNESS_DIR}/apptainer'; do
          if [ -d \"\$d\" ]; then echo \"  ok   \$d\"; else echo \"  MISS \$d\"; fi
        done"

bb_log "Pinned and ready. The master persists ~${BB_CM_PERSIST}; run 'bb-connect.sh --stop' to close it."
