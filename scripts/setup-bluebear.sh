#!/usr/bin/env bash
# setup-bluebear.sh — Install or update the bear-harness engine on BlueBEAR.
#
# Usage:
#   bash scripts/setup-bluebear.sh           # Full first-time setup
#   bash scripts/setup-bluebear.sh update    # Just rebuild + push wheel
#
# "update" mode: build wheel, rsync, pip install — nothing else touched.
# Full mode:     build, rsync, install, bootstrap, configure shell, verify.
#
# Prerequisites:
#   - Your identity configured (run /bearpilot:setup, or set BB_USER / BB_ACCOUNT /
#     BB_RDS_ROOT in ~/.config/bearpilot/env, or pass them as env vars below).
#   - SSH key auth to <your-username>@bluebear (no password prompts), VPN if off-campus.
#   - `hatch` available locally (used to build the wheel).

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — read YOUR identity from ~/.config/bearpilot/env (the same file the bash
# harness uses) or from env-var overrides. Fails fast if it's unset or still a
# placeholder, so the script never silently tries to ssh as `your-username`.
# ---------------------------------------------------------------------------
BEARPILOT_ENV="${BEARPILOT_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/bearpilot/env}"
# shellcheck disable=SC1090
[ -f "$BEARPILOT_ENV" ] && . "$BEARPILOT_ENV"

REMOTE_USER="${REMOTE_USER:-${BB_USER:-}}"
REMOTE_HOST="${REMOTE_HOST:-bluebear}"          # an ~/.ssh/config Host (delegates keys/jumphosts)
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

PROJECT_RDS="${PROJECT_RDS:-${BB_RDS_ROOT:-}}"
ACCOUNT="${ACCOUNT:-${BB_ACCOUNT:-}}"
MAIL_USER="${MAIL_USER:-${BB_MAIL_USER:-}}"

# Universal BlueBEAR constants (override only if the cluster changes them).
PYTHON_MODULE="${PYTHON_MODULE:-bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0}"
CUDA_MODULE="${CUDA_MODULE:-CUDA/12.6.0}"
GPU_GRES="${GPU_GRES:-gpu:a100:1}"

_blp_unset() { case "$1" in "" | *your-* | *YOUR-*) return 0 ;; *) return 1 ;; esac; }
_missing=""
_blp_unset "$REMOTE_USER" && _missing="$_missing BB_USER"
_blp_unset "$ACCOUNT"     && _missing="$_missing BB_ACCOUNT"
_blp_unset "$PROJECT_RDS" && _missing="$_missing BB_RDS_ROOT"
if [ -n "$_missing" ]; then
    echo "ERROR: cluster identity not configured (still unset or on placeholders):$_missing" >&2
    echo "  Fix it any one way:" >&2
    echo "    • run  /bearpilot:setup            (writes $BEARPILOT_ENV for you)" >&2
    echo "    • edit $BEARPILOT_ENV  (set BB_USER / BB_ACCOUNT / BB_RDS_ROOT)" >&2
    echo "    • run: REMOTE_USER=abc123 ACCOUNT=my-proj PROJECT_RDS=/rds/projects/m/my-proj \\" >&2
    echo "             bash scripts/setup-bluebear.sh" >&2
    exit 1
fi
# Default the SLURM notification address if not given (override with BB_MAIL_USER).
[ -z "$MAIL_USER" ] && MAIL_USER="${REMOTE_USER}@bham.ac.uk"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE="${1:-full}"
if [[ "$MODE" != "full" && "$MODE" != "update" ]]; then
    echo "Usage: $0 [full|update]"
    echo "  full   — first-time setup (build, install, bootstrap, shell config)"
    echo "  update — push new wheel only (build, install)"
    exit 1
fi

echo "==> Target: ${REMOTE}  ·  account ${ACCOUNT}  ·  RDS ${PROJECT_RDS}"

# ---------------------------------------------------------------------------
# Step 0: Check SSH connectivity
# ---------------------------------------------------------------------------

echo "==> Checking SSH connectivity to ${REMOTE}..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" 'echo ok' >/dev/null 2>&1; then
    echo "ERROR: Cannot SSH to ${REMOTE}."
    echo "  - Are you on the university VPN?"
    echo "  - Does 'ssh ${REMOTE}' work interactively? (key auth, ~/.ssh/config Host '${REMOTE_HOST}')"
    exit 1
fi
echo "    Connected."

# ---------------------------------------------------------------------------
# Step 1: Build wheel locally
# ---------------------------------------------------------------------------

echo "==> Building bear-harness wheel..."
cd "$REPO_DIR"
rm -rf dist/
hatch build -t wheel 2>&1 | tail -3
WHEEL=$(ls dist/*.whl 2>/dev/null | head -1)
if [[ -z "$WHEEL" ]]; then
    echo "ERROR: No wheel found in dist/ (is 'hatch' installed locally?)"
    exit 1
fi
echo "    Built: $(basename "$WHEEL")"

# ---------------------------------------------------------------------------
# Step 2: Rsync wheel to BlueBEAR
# ---------------------------------------------------------------------------

REMOTE_WHEELS="${PROJECT_RDS}/.bear-harness/wheels"

echo "==> Syncing wheel to ${REMOTE}:${REMOTE_WHEELS}/"
ssh -o BatchMode=yes "$REMOTE" "mkdir -p ${REMOTE_WHEELS}"
rsync -az --progress "$WHEEL" "${REMOTE}:${REMOTE_WHEELS}/"
echo "    Done."

# ---------------------------------------------------------------------------
# Step 3: Install on BlueBEAR
# ---------------------------------------------------------------------------

WHEEL_NAME="$(basename "$WHEEL")"

echo "==> Installing bear-harness on BlueBEAR..."
ssh -o BatchMode=yes "$REMOTE" bash -l <<INSTALL_SCRIPT
set -euo pipefail

module purge
module load ${PYTHON_MODULE}

echo "[remote] Python: \$(python3 --version)"

python3 -m pip install --user --force-reinstall --no-deps \
    "${REMOTE_WHEELS}/${WHEEL_NAME}" 2>&1 | tail -5

# Dependencies — idempotent, only installs if missing/outdated
python3 -m pip install --user \
    'click>=8.0' 'rich>=13.0' 'jinja2>=3.1' 'httpx>=0.25' 'tomli-w>=1.0' 2>&1 | tail -5

export PATH="\$HOME/.local/bin:\$PATH"
echo "[remote] bear-harness: \$(bear-harness --version 2>&1 || echo 'installed')"
echo "[remote] location: \$(which bear-harness)"
INSTALL_SCRIPT

if [[ "$MODE" == "update" ]]; then
    echo ""
    echo "=== UPDATE DONE ==="
    echo "Wheel pushed and installed. bear.toml and shell config untouched."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 4: Bootstrap (full mode only)
# ---------------------------------------------------------------------------

echo "==> Running bootstrap on BlueBEAR..."
ssh -o BatchMode=yes "$REMOTE" bash -l <<BOOTSTRAP_SCRIPT
set -euo pipefail

module purge
module load ${PYTHON_MODULE}
export PATH="\$HOME/.local/bin:\$PATH"

bear-harness bootstrap \
    --rds-root "${PROJECT_RDS}" \
    --account "${ACCOUNT}" \
    --cuda-module "${CUDA_MODULE}" \
    --gpu-gres "${GPU_GRES}" \
    --mail-user "${MAIL_USER}" \
    --config-path "\$HOME/.config/bear-harness/bear.toml" \
    --skip-pull

echo "[remote] bootstrap complete."
BOOTSTRAP_SCRIPT

# ---------------------------------------------------------------------------
# Step 5: Configure shell (full mode only)
# ---------------------------------------------------------------------------
# The real RDS root + module line are passed to the remote shell as env vars
# (RDS_ROOT / PYMOD), so the values baked into ~/.bashrc are correct, while
# $HOME / $PATH stay literal so they evaluate at login time.

echo "==> Configuring .bashrc for bear-harness..."
ssh -o BatchMode=yes "$REMOTE" "RDS_ROOT='${PROJECT_RDS}' PYMOD='${PYTHON_MODULE}' bash" <<'BASHRC_SCRIPT'
set -euo pipefail

MARKER="# >>> bear-harness >>>"
if grep -q "$MARKER" ~/.bashrc 2>/dev/null; then
    echo "[remote] .bashrc already configured — skipping."
else
    cat >> ~/.bashrc <<EOF

# >>> bear-harness >>>
# Load Python 3.12 and set paths for bear-harness
module load ${PYMOD} 2>/dev/null || true
export PATH="\$HOME/.local/bin:\$PATH"
export HF_HOME="${RDS_ROOT}/hf_cache"
# <<< bear-harness <<<
EOF
    echo "[remote] .bashrc updated."
fi
BASHRC_SCRIPT

# ---------------------------------------------------------------------------
# Step 6: Verify (full mode only)
# ---------------------------------------------------------------------------

echo "==> Verifying installation..."
ssh -o BatchMode=yes "$REMOTE" "RDS_ROOT='${PROJECT_RDS}' ACCT='${ACCOUNT}' REMOTE_TARGET='${REMOTE}' bash -l" <<'VERIFY_SCRIPT'
set -euo pipefail

echo "[verify] Python: $(python3 --version)"
echo "[verify] bear-harness: $(which bear-harness)"
echo "[verify] HF_HOME: ${HF_HOME:-<not set yet — open a new shell>}"

if [[ -f ~/.config/bear-harness/bear.toml ]]; then
    echo "[verify] bear.toml exists"
    bear-harness validate --help >/dev/null 2>&1 && echo "[verify] CLI loads OK"
else
    echo "[verify] WARNING: bear.toml not found!"
fi

for dir in .bear-harness/runs .bear-harness/endpoints .bear-harness/apptainer hf_cache; do
    if [[ -d "${RDS_ROOT}/$dir" ]]; then
        echo "[verify] ${dir} exists"
    else
        echo "[verify] WARNING: ${dir} missing"
    fi
done

echo ""
echo "Setup complete. Next steps:"
echo "  1. Pull the vLLM apptainer image (if not done already) — as a SLURM job,"
echo "     never on the login node:"
echo "       scp scripts/sif-build.sbatch ${REMOTE_TARGET}:"
echo "       ssh ${REMOTE_TARGET} 'sbatch --account=${ACCT} --export=ALL,BB_RDS_ROOT=${RDS_ROOT} sif-build.sbatch'"
echo ""
echo "  2. Dry-run a launch (reads the rendered sbatch — no submit):"
echo "       bear-harness launch --dry-run path/to/your/pipeline"
echo ""
echo "  3. Quick smoke test on the short queue:"
echo "       bear-harness launch tests/fixtures/etl_pipeline.toml --qos bbshort"
VERIFY_SCRIPT

echo ""
echo "=== DONE ==="
