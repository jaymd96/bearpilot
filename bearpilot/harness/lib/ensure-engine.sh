#!/usr/bin/env sh
# ensure-engine.sh — provision the bear-harness engine FROM PyPI on first use, pinned to the
# exact version this plugin ships (harness/engine.pin). Sourced by the bundled launchers
# (bear-harness-mcp.sh, bear-harness-dashboard.sh) so installing the plugin is enough — no
# separate clone or install.sh.
#
# Contract:
#   . "$(dirname "$0")/lib/ensure-engine.sh"
#   ensure_engine            # sets $ENGINE_BIN to a dir containing the console scripts
#   exec "$ENGINE_BIN/bear-harness-mcp" "$@"
#
# CRITICAL: this writes ONLY to stderr. The MCP server speaks JSON-RPC over stdout, so a single
# stray byte on stdout corrupts the protocol. All bootstrap chatter goes to >&2.
#
# Knobs (env):
#   BEAR_HARNESS_VENV        venv location (default ${XDG_DATA_HOME:-~/.local/share}/bearpilot/venv)
#   BEAR_HARNESS_VERSION     override the pinned version (else read from harness/engine.pin)
#   BEAR_HARNESS_FORCE_VENV  =1 → always use the managed venv, even if bear-harness is on PATH
#   BEAR_HARNESS_REINSTALL   =1 → force a fresh pip install into the venv
#   PIP_INDEX_URL            standard pip knob — point at a mirror / private index if needed

_ee_log() { printf '[ensure-engine] %s\n' "$*" >&2; }
_ee_die() { printf '[ensure-engine] ERROR: %s\n' "$*" >&2; exit 1; }

# Locate the plugin dir (which holds harness/engine.pin): prefer the installed plugin root.
_ee_plugin_dir() {
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "${CLAUDE_PLUGIN_ROOT}" ]; then
        printf '%s\n' "${CLAUDE_PLUGIN_ROOT}"; return 0
    fi
    # lib/ensure-engine.sh → ../.. = the plugin dir (bearpilot/)
    _here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    ( CDPATH= cd -- "${_here}/../.." && pwd )
}

# The pinned version: env override, else the first non-comment, non-blank line of engine.pin.
_ee_pin() {
    if [ -n "${BEAR_HARNESS_VERSION:-}" ]; then printf '%s\n' "${BEAR_HARNESS_VERSION}"; return 0; fi
    _pin_file="$(_ee_plugin_dir)/harness/engine.pin"
    [ -f "$_pin_file" ] || return 1
    grep -v '^[[:space:]]*#' "$_pin_file" | grep -v '^[[:space:]]*$' | head -1 | tr -d '[:space:]'
}

ensure_engine() {
    WANT="$(_ee_pin)" || _ee_die "no version pin (harness/engine.pin missing and BEAR_HARNESS_VERSION unset)"
    [ -n "$WANT" ] || _ee_die "version pin is empty"

    # 1. Respect an existing PATH install ONLY if it matches the pin, so the pin stays authoritative
    #    (a stale pipx/install.sh engine must NOT silently shadow a newer pin).
    if [ "${BEAR_HARNESS_FORCE_VENV:-0}" != "1" ] && command -v bear-harness-mcp >/dev/null 2>&1; then
        _have="$(bear-harness --version 2>/dev/null | awk '{print $NF}')"
        if [ "$_have" = "$WANT" ]; then
            ENGINE_BIN="$(dirname "$(command -v bear-harness-mcp)")"
            _ee_log "using bear-harness $_have already on PATH ($ENGINE_BIN)"
            return 0
        fi
        _ee_log "on-PATH bear-harness ${_have:-?} != pinned $WANT — using the managed venv instead"
    fi

    VENV="${BEAR_HARNESS_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/bearpilot/venv}"
    ENGINE_BIN="$VENV/bin"
    STAMP="$VENV/.engine-version"

    # 2. Reuse the venv if it already has the server at the pinned version.
    if [ "${BEAR_HARNESS_REINSTALL:-0}" != "1" ] \
        && [ -x "$ENGINE_BIN/bear-harness-mcp" ] \
        && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ]; then
        return 0
    fi

    # 3. Bootstrap / upgrade (serialised by an mkdir lock so two starts don't race).
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

    _ee_log "provisioning bear-harness==$WANT from PyPI into $VENV (first run / version change)…"
    [ -d "$VENV" ] || python3 -m venv "$VENV" >&2 || _ee_die "python3 -m venv failed"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip >&2 \
        || _ee_log "pip self-upgrade failed (continuing)"
    if ! "$VENV/bin/python" -m pip install --quiet "bear-harness[mcp]==$WANT" >&2; then
        rmdir "$LOCK" 2>/dev/null || true; trap - EXIT INT TERM
        _ee_die "pip install 'bear-harness[mcp]==$WANT' failed — first run needs PyPI reachable. Offline or PyPI down? run the repo's ./install.sh on a connected machine, or set PIP_INDEX_URL to a mirror."
    fi
    printf '%s\n' "$WANT" > "$STAMP"
    _ee_log "engine $WANT ready."
    rmdir "$LOCK" 2>/dev/null || true; trap - EXIT INT TERM
}
