#!/usr/bin/env sh
# bear-harness-mcp.sh — the plugin's MCP server entry point (referenced from .mcp.json).
#
# Self-bootstrapping: installs the pinned bear-harness engine FROM PyPI into a private venv on
# first run (and re-installs when harness/engine.pin changes), then execs the real server.
# Installing the plugin is enough — no separate clone or install.sh. A matching PATH install
# (pipx/install.sh) is used as-is; a mismatched one is bypassed so the pin stays authoritative.
#
# stdout is the JSON-RPC stdio channel — ensure-engine.sh keeps all chatter on stderr.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "${HERE}/lib/ensure-engine.sh"
ensure_engine
exec "${ENGINE_BIN}/bear-harness-mcp" "$@"
