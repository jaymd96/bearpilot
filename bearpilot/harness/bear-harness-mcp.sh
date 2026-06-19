#!/usr/bin/env sh
# bear-harness-mcp.sh — the plugin's MCP server entry point (referenced from .mcp.json).
#
# Self-bootstrapping: provisions the engine VENDORED in the plugin (bearpilot/engine/) into a
# private venv on first run, then execs the real server. Installing the plugin is enough —
# no separate clone or install.sh. An existing PATH install (pipx/install.sh) is used as-is.
#
# stdout is the JSON-RPC stdio channel — ensure-engine.sh keeps all chatter on stderr.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "${HERE}/lib/ensure-engine.sh"
ensure_engine
exec "${ENGINE_BIN}/bear-harness-mcp" "$@"
