#!/usr/bin/env sh
# check-engine-pin.sh — guard the plugin's engine pin against drift from the canonical version.
#
# The plugin ships an exact version pin (bearpilot/harness/engine.pin) that its launchers install
# from PyPI. That pin MUST equal the engine's own version (src/bear_harness/__about__.py), or the
# plugin would ask PyPI for a version that doesn't correspond to this checkout. This is the cheap,
# exact successor to the old vendored-mirror drift guard.
#
#   checks/check-engine-pin.sh [REPO_ROOT]
#
# BLOCKING: pin != __about__.py version → exit 1.
# ADVISORY: if `pip`/network is available and the pinned version isn't on PyPI yet, warn (exit 0) —
#           a just-tagged release can lag the index, so this must not gate a PR.
set -eu

ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
ABOUT="$ROOT/src/bear_harness/__about__.py"
PIN_FILE="$ROOT/bearpilot/harness/engine.pin"

[ -f "$ABOUT" ]    || { echo "check-engine-pin: missing $ABOUT" >&2; exit 1; }
[ -f "$PIN_FILE" ] || { echo "check-engine-pin: missing $PIN_FILE" >&2; exit 1; }

VER="$(sed -n 's/.*__version__ = "\([^"]*\)".*/\1/p' "$ABOUT" | head -1)"
PIN="$(grep -v '^[[:space:]]*#' "$PIN_FILE" | grep -v '^[[:space:]]*$' | head -1 | tr -d '[:space:]')"

if [ -z "$VER" ]; then echo "check-engine-pin: could not parse __version__ from $ABOUT" >&2; exit 1; fi
if [ -z "$PIN" ]; then echo "check-engine-pin: engine.pin has no version line" >&2; exit 1; fi

if [ "$VER" != "$PIN" ]; then
    echo "check-engine-pin: DRIFT — engine.pin ($PIN) != __about__.py ($VER)." >&2
    echo "  Bump BOTH together: src/bear_harness/__about__.py and bearpilot/harness/engine.pin." >&2
    exit 1
fi
echo "check-engine-pin: pin == version == $VER"

# Advisory: is the pinned version actually published? Never blocking (CDN/propagation lag, offline CI).
if command -v curl >/dev/null 2>&1; then
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/bear-harness/$PIN/json" 2>/dev/null || echo 000)"
    case "$code" in
        200) : ;;
        404) echo "check-engine-pin: NOTE — bear-harness==$PIN is not on PyPI yet (publish the tag before users hit it)." >&2 ;;
        *)   echo "check-engine-pin: NOTE — could not reach PyPI to confirm $PIN (http $code); skipping." >&2 ;;
    esac
fi
exit 0
