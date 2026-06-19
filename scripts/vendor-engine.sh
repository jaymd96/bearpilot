#!/usr/bin/env sh
# vendor-engine.sh — mirror the canonical bear-harness engine into the plugin so it
# SHIPS with a marketplace install.
#
# The plugin's marketplace `source` is ./bearpilot, so only that subtree is copied to a
# user's plugin cache. The engine (src/bear_harness + pyproject.toml) lives at the repo
# root, OUTSIDE ./bearpilot — so a marketplace install would otherwise leave it behind.
# This script copies the canonical engine into bearpilot/engine/, where the bundled MCP
# launcher (bearpilot/harness/lib/ensure-engine.sh) can pip-install it on first use.
#
# The repo root stays the single source of truth (build, tests, install.sh all use it);
# bearpilot/engine/ is a GENERATED mirror. CI / the pre-commit hook run `--check` so the
# mirror can never silently drift from the source.
#
#   scripts/vendor-engine.sh           # regenerate bearpilot/engine/ from root src/ + pyproject
#   scripts/vendor-engine.sh --check   # exit nonzero if the mirror is stale (no writes)
#
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_PKG="$REPO_ROOT/src/bear_harness"
SRC_PYPROJECT="$REPO_ROOT/pyproject.toml"
DEST="$REPO_ROOT/bearpilot/engine"

MODE="${1:-write}"

have() { command -v "$1" >/dev/null 2>&1; }
have rsync || { echo "vendor-engine: rsync required" >&2; exit 2; }

# Materialise a fresh mirror into $1.
build_into() {
    target="$1"
    mkdir -p "$target/src"
    # --delete so a file removed from the source is removed from the mirror too.
    rsync -a --delete \
        --exclude='__pycache__/' --exclude='*.py[cod]' --exclude='.DS_Store' \
        "$SRC_PKG/" "$target/src/bear_harness/"
    cp "$SRC_PYPROJECT" "$target/pyproject.toml"
    cat > "$target/README.md" <<'EOF'
# bearpilot/engine — GENERATED, do not edit by hand

This is a mirror of the canonical `bear-harness` engine at the repository root
(`/src/bear_harness` + `/pyproject.toml`), vendored here so the engine ships inside the
plugin's marketplace `source` (`./bearpilot`). The bundled MCP/dashboard launcher
(`bearpilot/harness/lib/ensure-engine.sh`) `pip install`s this directory into a private
venv on first use, so installing the plugin is enough — no separate clone or `install.sh`.

Edit the engine at the repo root, then regenerate this mirror:

    scripts/vendor-engine.sh

CI and the pre-commit hook run `scripts/vendor-engine.sh --check` to block drift.
EOF
}

if [ "$MODE" = "--check" ]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    build_into "$TMP/engine"
    # Compare the freshly-built mirror against the committed one.
    if diff -r --exclude='__pycache__' --exclude='*.pyc' "$TMP/engine" "$DEST" >/dev/null 2>&1; then
        echo "vendor-engine: bearpilot/engine/ is in sync with root src/ + pyproject.toml"
        exit 0
    fi
    echo "vendor-engine: DRIFT — bearpilot/engine/ is stale. Run: scripts/vendor-engine.sh" >&2
    diff -r --exclude='__pycache__' --exclude='*.pyc' "$DEST" "$TMP/engine" >&2 || true
    exit 1
fi

build_into "$DEST"
echo "vendor-engine: regenerated $DEST from root src/bear_harness + pyproject.toml"
