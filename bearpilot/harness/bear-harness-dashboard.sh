#!/usr/bin/env sh
# bear-harness-dashboard.sh — the plugin's live dashboard entry point (used by /bearpilot:dashboard).
#
# Same self-bootstrap as the MCP launcher: installs the pinned bear-harness engine from PyPI into a
# private venv on first run, then execs the dashboard server. Installing the plugin is enough; a
# matching PATH install is used as-is. All bootstrap chatter stays on stderr.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "${HERE}/lib/ensure-engine.sh"
ensure_engine
exec "${ENGINE_BIN}/bear-harness-dashboard" "$@"
