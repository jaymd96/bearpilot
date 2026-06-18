#!/usr/bin/env bash
# common.sh — shared config + helpers for the Bearpilot harness.
#
# Source this from any harness script:  . "$(dirname "$0")/lib/common.sh"
#
# Every value below is overridable with the matching BB_* environment variable, so the
# harness adapts to another account/cluster without editing this file:
#     BB_ACCOUNT=other-project bb-submit.sh ...
#
# The canonical values are the cluster ground-truth — see ../references/cluster-ground-truth.md.
# They ROTATE; re-probe on a login node when a job fails at submit or on the compute node.

# ---------------------------------------------------------------------------
# Per-user config — written once by /bearpilot:setup (or copy bearpilot.env.example to the
# path below and edit it). This keeps your cluster identity OUT of the repo: nothing here is
# a real account. The MCP server + dashboard read their own per-user config separately, from
# ~/.config/bear-harness/hosts.toml — /bearpilot:setup writes both from one set of answers.
# ---------------------------------------------------------------------------
BEARPILOT_ENV="${BEARPILOT_ENV:-${XDG_CONFIG_HOME:-${HOME}/.config}/bearpilot/env}"
[ -f "${BEARPILOT_ENV}" ] && . "${BEARPILOT_ENV}"

# ---------------------------------------------------------------------------
# Identity & access  (per-user — no defaults; supplied via the env file above or BB_* vars)
# ---------------------------------------------------------------------------
BB_USER="${BB_USER:-}"                                    # the CLUSTER user (NOT your local user)
BB_HOST="${BB_HOST:-bluebear.bham.ac.uk}"                 # universal — the BlueBEAR login host
BB_REMOTE="${BB_REMOTE:-${BB_USER}@${BB_HOST}}"
BB_HOME="${BB_HOME:-}"                                    # optional — rarely needed

# ---------------------------------------------------------------------------
# SLURM account / QoS / resources
# ---------------------------------------------------------------------------
BB_ACCOUNT="${BB_ACCOUNT:-}"                         # per-user — your SLURM project account
BB_QOS_SHORT="${BB_QOS_SHORT:-bbshort}"              # MaxWall 00:10:00, spans GPUs — the debug loop
BB_QOS_GPU="${BB_QOS_GPU:-bbgpu}"                    # MaxWall 2-00:00:00 — real GPU serving
BB_QOS_CPU="${BB_QOS_CPU:-bbdefault}"               # CPU QoS (NOT bbcpu — absent on this account)
BB_GRES="${BB_GRES:-gpu:a100:1}"                     # type is `a100`, 40 GB cards
BB_MAIL_USER="${BB_MAIL_USER:-}"                    # optional — SLURM job-state notifications

# ---------------------------------------------------------------------------
# Modules (bear-apps/2024a toolchain) — CUDA version ROTATES
# ---------------------------------------------------------------------------
BB_PYTHON_MODULE="${BB_PYTHON_MODULE:-bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0}"
BB_CUDA_MODULE="${BB_CUDA_MODULE:-CUDA/12.6.0}"
BB_MODULE_LINE="${BB_MODULE_LINE:-${BB_PYTHON_MODULE} ${BB_CUDA_MODULE}}"

# ---------------------------------------------------------------------------
# Storage (RDS) — the durable source of truth for run state
# ---------------------------------------------------------------------------
BB_RDS_ROOT="${BB_RDS_ROOT:-}"                       # per-user — /rds/projects/<g>/<account>
BB_HARNESS_DIR="${BB_HARNESS_DIR:-${BB_RDS_ROOT}/.bear-harness}"
BB_RUNS_DIR="${BB_RUNS_DIR:-${BB_HARNESS_DIR}/runs}"
BB_JOBS_DIR="${BB_JOBS_DIR:-${BB_HARNESS_DIR}/launchpad}"   # where THIS harness keeps its job dirs
BB_HF_CACHE="${BB_HF_CACHE:-${BB_RDS_ROOT}/hf_cache}"
BB_SIF="${BB_SIF:-${BB_HARNESS_DIR}/apptainer/vllm-openai.sif}"

# ---------------------------------------------------------------------------
# SSH connection multiplexing (pin to ONE login node for a watch)
# ---------------------------------------------------------------------------
BB_CM_SOCKET="${BB_CM_SOCKET:-${HOME}/.ssh/cm-bearpilot}"
BB_CM_PERSIST="${BB_CM_PERSIST:-8h}"
BB_SSH_CONNECT_TIMEOUT="${BB_SSH_CONNECT_TIMEOUT:-10}"

# ---------------------------------------------------------------------------
# Logging helpers (stderr, so stdout stays clean for piping job ids etc.)
# ---------------------------------------------------------------------------
bb_log()  { printf '\033[36m[bb]\033[0m %s\n'    "$*" >&2; }
bb_warn() { printf '\033[33m[bb] WARN:\033[0m %s\n' "$*" >&2; }
bb_err()  { printf '\033[31m[bb] ERROR:\033[0m %s\n' "$*" >&2; }
bb_die()  { bb_err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# SSH — always BatchMode (key auth, never prompt) and reuse the pinned master if present.
# A missing ControlPath socket just means "no master to reuse" → a normal direct connection.
# ---------------------------------------------------------------------------
bb_ssh() {
    ssh -o BatchMode=yes \
        -o ConnectTimeout="${BB_SSH_CONNECT_TIMEOUT}" \
        -o ControlPath="${BB_CM_SOCKET}" \
        "${BB_REMOTE}" "$@"
}

# Run a remote command through a LOGIN shell (so modules / ~/.bashrc PATH are available).
bb_ssh_login() {
    bb_ssh "bash -l -c $(bb_shquote "$*")"
}

# rsync over the SAME pinned/BatchMode SSH transport. Args are passed straight to rsync,
# so callers control direction:  bb_rsync -az "$local/" "${BB_REMOTE}:$remote/"
bb_rsync() {
    rsync -e "ssh -o BatchMode=yes -o ConnectTimeout=${BB_SSH_CONNECT_TIMEOUT} -o ControlPath=${BB_CM_SOCKET}" "$@"
}

# Shell-quote a string for safe remote execution.
bb_shquote() { printf "%s" "$1" | sed "s/'/'\\\\''/g; 1s/^/'/; \$s/\$/'/"; }

# ---------------------------------------------------------------------------
# ControlMaster lifecycle — pin SSH to one round-robin login node for the watch.
# ---------------------------------------------------------------------------
bb_cm_check() { ssh -o ControlPath="${BB_CM_SOCKET}" -O check "${BB_REMOTE}" 2>/dev/null; }

bb_cm_start() {
    if bb_cm_check; then bb_log "ControlMaster already up (pinned)."; return 0; fi
    bb_log "Opening pinned SSH master to ${BB_REMOTE} (persist ${BB_CM_PERSIST})..."
    ssh -fN -M \
        -o BatchMode=yes \
        -o ConnectTimeout="${BB_SSH_CONNECT_TIMEOUT}" \
        -o ControlPath="${BB_CM_SOCKET}" \
        -o ControlPersist="${BB_CM_PERSIST}" \
        "${BB_REMOTE}" \
        || bb_die "Could not open SSH master. On VPN? Is key auth set up for ${BB_REMOTE}?"
    bb_cm_check && bb_log "Pinned to one login node for the next ${BB_CM_PERSIST}."
}

bb_cm_stop() {
    if bb_cm_check; then
        ssh -o ControlPath="${BB_CM_SOCKET}" -O exit "${BB_REMOTE}" 2>/dev/null \
            && bb_log "Closed pinned SSH master."
    fi
}

# ---------------------------------------------------------------------------
# Connectivity guard — call at the top of any script that touches the cluster.
# ---------------------------------------------------------------------------
bb_require_ssh() {
    bb_ssh 'echo ok' >/dev/null 2>&1 \
        || bb_die "Cannot SSH to ${BB_REMOTE}. Check: VPN connected? key auth works? (BB_USER is your cluster login, not your local user)"
}

# ---------------------------------------------------------------------------
# Discoverability — print the resolved config (what every job will inherit).
# ---------------------------------------------------------------------------
bb_show_config() {
    cat >&2 <<EOF
[bb] resolved config (override any with the BB_* env var):
     remote    = ${BB_REMOTE}
     account   = ${BB_ACCOUNT}
     qos       = short:${BB_QOS_SHORT}  gpu:${BB_QOS_GPU}  cpu:${BB_QOS_CPU}
     gres      = ${BB_GRES}
     modules   = ${BB_MODULE_LINE}
     rds_root  = ${BB_RDS_ROOT}
     runs_dir  = ${BB_RUNS_DIR}
     jobs_dir  = ${BB_JOBS_DIR}
     hf_cache  = ${BB_HF_CACHE}
     sif       = ${BB_SIF}
EOF
}

# ---------------------------------------------------------------------------
# Config guard — fail fast with a clear message when per-user identity is unset.
# Universal cluster constants (host, QoS, GRES, modules) ship with real defaults;
# only YOUR identity must be supplied — via /bearpilot:setup, ~/.config/bearpilot/env,
# or BB_* env vars. Called at load so every harness script validates on source.
# ---------------------------------------------------------------------------
# A value counts as "unset" if it is empty OR still one of the example placeholders, so a
# freshly-seeded env file that hasn't been edited yet fails fast rather than silently trying
# to ssh as `your-bluebear-username`.
_bb_unset() {
    case "$1" in
        "" | *your-* | *YOUR-* | *"<your"* ) return 0 ;;
        * ) return 1 ;;
    esac
}

bb_require_config() {
    _bb_missing=""
    _bb_unset "${BB_USER}"     && _bb_missing="${_bb_missing} BB_USER"
    _bb_unset "${BB_ACCOUNT}"  && _bb_missing="${_bb_missing} BB_ACCOUNT"
    _bb_unset "${BB_RDS_ROOT}" && _bb_missing="${_bb_missing} BB_RDS_ROOT"
    [ -z "${_bb_missing}" ] && return 0
    bb_err "BlueBEAR identity not configured (still unset or on the example placeholders):${_bb_missing}"
    bb_err "Fix it any one way:"
    bb_err "  • run  /bearpilot:setup            (writes ${BEARPILOT_ENV} for you)"
    bb_err "  • edit ${BEARPILOT_ENV}  (seeded from bearpilot.env.example — replace the your-* values)"
    bb_err "  • export BB_USER=… BB_ACCOUNT=… BB_RDS_ROOT=…  before running"
    exit 1
}

bb_require_config
