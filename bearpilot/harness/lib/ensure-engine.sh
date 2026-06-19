#!/usr/bin/env sh
# ensure-engine.sh — provision the bear-harness engine on first use, from the copy
# VENDORED inside the plugin, so installing the plugin is enough (no separate clone or
# install.sh). Sourced by the bundled launchers (bear-harness-mcp.sh, bear-harness-dashboard.sh).
#
# Contract:
#   . "$(dirname "$0")/lib/ensure-engine.sh"
#   ensure_engine            # sets $ENGINE_BIN to a dir containing the console scripts
#   exec "$ENGINE_BIN/bear-harness-mcp" "$@"
#
# CRITICAL: this writes ONLY to stderr. The MCP server speaks JSON-RPC over stdout, so a
# single stray byte on stdout corrupts the protocol. All bootstrap chatter goes to >&2.
#
# Knobs (env):
#   BEAR_HARNESS_VENV        venv location (default ${XDG_DATA_HOME:-~/.local/share}/bearpilot/venv)
#   BEAR_HARNESS_FORCE_VENV  =1 → always use the vendored venv, even if bear-harness is on PATH
#   BEAR_HARNESS_REINSTALL   =1 → force a fresh pip install into the venv

_ee_log() { printf '[ensure-engine] %s\n' "$*" >&2; }
_ee_die() { printf '[ensure-engine] ERROR: %s\n' "$*" >&2; exit 1; }

# Locate the vendored engine: prefer the installed plugin root, else this file's repo-relative path.
_ee_engine_dir() {
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "${CLAUDE_PLUGIN_ROOT}/engine" ]; then
        printf '%s\n' "${CLAUDE_PLUGIN_ROOT}/engine"; return 0
    fi
    # lib/ensure-engine.sh → ../../engine  (bearpilot/harness/lib → bearpilot/engine)
    _here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    if [ -d "${_here}/../../engine" ]; then
        ( CDPATH= cd -- "${_here}/../../engine" && pwd ); return 0
    fi
    return 1
}

_ee_engine_version() {   # read __version__ from the vendored package
    _vf="$1/src/bear_harness/__about__.py"
    [ -f "$_vf" ] || { printf 'unknown\n'; return; }
    sed -n 's/.*__version__ = "\([^"]*\)".*/\1/p' "$_vf" | head -1
}

ensure_engine() {
    # 1. Respect an existing PATH install (pipx / install.sh) unless told to force the venv.
    if [ "${BEAR_HARNESS_FORCE_VENV:-0}" != "1" ] && command -v bear-harness-mcp >/dev/null 2>&1; then
        ENGINE_BIN="$(dirname "$(command -v bear-harness-mcp)")"
        _ee_log "using bear-harness already on PATH ($ENGINE_BIN)"
        return 0
    fi

    ENGINE_DIR="$(_ee_engine_dir)" || _ee_die "vendored engine not found (expected \$CLAUDE_PLUGIN_ROOT/engine or ../../engine)"
    VENV="${BEAR_HARNESS_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/bearpilot/venv}"
    ENGINE_BIN="$VENV/bin"
    WANT="$(_ee_engine_version "$ENGINE_DIR")"
    STAMP="$VENV/.engine-version"

    # 2. Reuse the venv if it already has the server at the matching version.
    if [ "${BEAR_HARNESS_REINSTALL:-0}" != "1" ] \
        && [ -x "$ENGINE_BIN/bear-harness-mcp" ] \
        && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ]; then
        return 0
    fi

    # 3. Bootstrap (serialised by a mkdir lock so two starts don't race).
    command -v python3 >/dev/null 2>&1 || _ee_die "python3 not found (need >= 3.11)"
    LOCK="$VENV.lock"
    _tries=0
    while ! mkdir "$LOCK" 2>/dev/null; do
        _tries=$((_tries + 1))
        [ "$_tries" -gt 120 ] && _ee_die "timed out waiting for another bootstrap to finish ($LOCK)"
        sleep 1
    done
    # shellcheck disable=SC2064
    trap "rmdir '$LOCK' 2>/dev/null || true" EXIT INT TERM

    # Re-check after acquiring the lock — another process may have just finished.
    if [ "${BEAR_HARNESS_REINSTALL:-0}" != "1" ] \
        && [ -x "$ENGINE_BIN/bear-harness-mcp" ] \
        && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ]; then
        rmdir "$LOCK" 2>/dev/null || true; trap - EXIT INT TERM; return 0
    fi

    _ee_log "provisioning the bear-harness engine (v$WANT) into $VENV — first run only…"
    [ -d "$VENV" ] || python3 -m venv "$VENV" >&2 || _ee_die "python3 -m venv failed"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip >&2 \
        || _ee_log "pip self-upgrade failed (continuing)"
    if ! "$VENV/bin/python" -m pip install --quiet "${ENGINE_DIR}[mcp]" >&2; then
        rmdir "$LOCK" 2>/dev/null || true; trap - EXIT INT TERM
        _ee_die "pip install of the engine failed — need network for PyPI deps on first run. Offline? run the repo's ./install.sh, or use a wheels-bundled build."
    fi
    printf '%s\n' "$WANT" > "$STAMP"
    _ee_log "engine ready."
    rmdir "$LOCK" 2>/dev/null || true; trap - EXIT INT TERM
}
