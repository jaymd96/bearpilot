#!/usr/bin/env sh
# bear-harness-dashboard.sh — the plugin's live dashboard entry point (used by /bearpilot:dashboard).
#
# Same self-bootstrap as the MCP launcher: provisions the vendored engine into a private venv on
# first run, then execs the dashboard server. Installing the plugin is enough. An existing PATH
# install (pipx/install.sh) is used as-is. All bootstrap chatter stays on stderr.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "${HERE}/lib/ensure-engine.sh"
ensure_engine
exec "${ENGINE_BIN}/bear-harness-dashboard" "$@"
