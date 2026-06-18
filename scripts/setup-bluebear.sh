#!/usr/bin/env bash
# setup-bluebear.sh — Install or update bear-harness on BlueBEAR.
#
# Usage:
#   bash scripts/setup-bluebear.sh           # Full first-time setup
#   bash scripts/setup-bluebear.sh update    # Just rebuild + push wheel
#
# "update" mode: build wheel, rsync, pip install — nothing else touched.
# Full mode:     build, rsync, install, bootstrap, configure shell, verify.
#
# Prerequisites:
#   - SSH key auth to your-username@bluebear (no password prompts)
#   - University VPN if off-campus

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REMOTE_USER="your-username"
REMOTE_HOST="bluebear"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

PROJECT_RDS="/rds/projects/p/your-project"
ACCOUNT="your-project"
MAIL_USER="you@example.com"

# Modules (bear-apps/2024a toolchain)
PYTHON_MODULE="bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0"
CUDA_MODULE="CUDA/12.6.0"
GPU_GRES="gpu:a100:1"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE="${1:-full}"
if [[ "$MODE" != "full" && "$MODE" != "update" ]]; then
    echo "Usage: $0 [full|update]"
    echo "  full   — first-time setup (build, install, bootstrap, shell config)"
    echo "  update — push new wheel only (build, install)"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 0: Check SSH connectivity
# ---------------------------------------------------------------------------

echo "==> Checking SSH connectivity to ${REMOTE}..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" 'echo ok' >/dev/null 2>&1; then
    echo "ERROR: Cannot SSH to ${REMOTE}."
    echo "  - Are you on the university VPN?"
    echo "  - Does 'ssh ${REMOTE}' work interactively?"
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
    echo "ERROR: No wheel found in dist/"
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

bear-harness bootstrap \\
    --rds-root "${PROJECT_RDS}" \\
    --account "${ACCOUNT}" \\
    --cuda-module "${CUDA_MODULE}" \\
    --gpu-gres "${GPU_GRES}" \\
    --mail-user "${MAIL_USER}" \\
    --config-path "\$HOME/.config/bear-harness/bear.toml" \\
    --skip-pull

echo "[remote] bootstrap complete."
BOOTSTRAP_SCRIPT

# ---------------------------------------------------------------------------
# Step 5: Configure shell (full mode only)
# ---------------------------------------------------------------------------

echo "==> Configuring .bashrc for bear-harness..."
ssh -o BatchMode=yes "$REMOTE" bash <<'BASHRC_SCRIPT'
set -euo pipefail

MARKER="# >>> bear-harness >>>"
if grep -q "$MARKER" ~/.bashrc 2>/dev/null; then
    echo "[remote] .bashrc already configured — skipping."
else
    cat >> ~/.bashrc <<'EOF'

# >>> bear-harness >>>
# Load Python 3.12 and set paths for bear-harness
module load bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export HF_HOME="/rds/projects/p/your-project/hf_cache"
# <<< bear-harness <<<
EOF
    echo "[remote] .bashrc updated."
fi
BASHRC_SCRIPT

# ---------------------------------------------------------------------------
# Step 6: Verify (full mode only)
# ---------------------------------------------------------------------------

echo "==> Verifying installation..."
ssh -o BatchMode=yes "$REMOTE" bash -l <<'VERIFY_SCRIPT'
set -euo pipefail

echo "[verify] Python: $(python3 --version)"
echo "[verify] bear-harness: $(which bear-harness)"
echo "[verify] HF_HOME: $HF_HOME"

if [[ -f ~/.config/bear-harness/bear.toml ]]; then
    echo "[verify] bear.toml exists"
    bear-harness validate --help >/dev/null 2>&1 && echo "[verify] CLI loads OK"
else
    echo "[verify] WARNING: bear.toml not found!"
fi

for dir in .bear-harness/runs .bear-harness/endpoints .bear-harness/apptainer hf_cache; do
    if [[ -d "/rds/projects/p/your-project/$dir" ]]; then
        echo "[verify] ${dir} exists"
    else
        echo "[verify] WARNING: ${dir} missing"
    fi
done

echo ""
echo "Setup complete. Next steps:"
echo "  1. Pull the vLLM apptainer image (if not done already) — as a"
echo "     SLURM job, never on the login node (the per-user limiter"
echo "     kills heavy builds, and node /tmp is too small — the script"
echo "     uses node-local /scratch):"
echo "     scp scripts/sif-build.sbatch your-username@bluebear:"
echo "     ssh your-username@bluebear sbatch sif-build.sbatch"
echo ""
echo "  2. Test launch:"
echo "     bear-harness launch --dry-run path/to/your/pipeline --model google/gemma-4-2b-it"
echo ""
echo "  3. Quick smoke test (bbshort, 10 min max — TODO: verify bbshort+GPU works post-maintenance):"
echo "     bear-harness launch path/to/your/pipeline --model google/gemma-4-2b-it --qos bbshort --boot-timeout 300"
VERIFY_SCRIPT

echo ""
echo "=== DONE ==="
