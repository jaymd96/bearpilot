#!/bin/sh
# install.sh — set up Bearpilot on a fresh machine.
#
# Usage:
#   ./install.sh            Install the engine + seed config, then print next steps
#   ./install.sh --check    Check prerequisites only (no changes)
#   ./install.sh --help
#
# What it does (idempotent — safe to re-run):
#   1. Checks prerequisites (python3 >= 3.11, ssh, rsync; gh optional).
#   2. Installs the bear-harness engine with the MCP extra so the console scripts
#      `bear-harness`, `bear-harness-mcp`, `bear-harness-dashboard` are on your PATH
#      (prefers pipx; falls back to a repo-local .venv + ~/.local/bin symlinks).
#   3. Seeds ~/.config/bear-harness/hosts.toml from hosts.toml.example (never overwrites).
#   4. Prints how to add the Claude Code plugin and a read-only verification probe.
#
# It does NOT touch the cluster and does NOT need network access to BlueBEAR.
# POSIX-compliant — works on Linux, macOS, and WSL.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ── Helpers ──────────────────────────────────────────────────────────
info()  { printf '\n==> %s\n' "$1"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# ── Prerequisite checks ──────────────────────────────────────────────
check_prereqs() {
    info "Checking prerequisites"
    have python3 || fail "python3 not found (need 3.11+)."
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
    else
        fail "python3 is older than 3.11 — install a newer Python."
    fi
    have ssh   || fail "ssh not found (needed to reach the login node)."
    ok "ssh"
    have rsync || fail "rsync not found (needed to ship programs + fetch results)."
    ok "rsync"
    if have gh; then ok "gh (optional — for installing the plugin from a remote repo)"; \
        else warn "gh not found (optional) — clone the repo locally to install the plugin."; fi
}

# ── Engine install ───────────────────────────────────────────────────
install_engine() {
    info "Installing the bear-harness engine (with the [mcp] extra)"
    if have pipx; then
        pipx install --force ".[mcp]"
        pipx ensurepath >/dev/null 2>&1 || true
        ok "Installed via pipx — console scripts are on your PATH."
    else
        warn "pipx not found — using a repo-local .venv instead (pipx is the smoother path:"
        warn "  'python3 -m pip install --user pipx && pipx ensurepath', then re-run this script)."
        python3 -m venv "$REPO_ROOT/.venv"
        # shellcheck disable=SC1091
        . "$REPO_ROOT/.venv/bin/activate"
        python3 -m pip install --quiet --upgrade pip
        python3 -m pip install --quiet -e ".[mcp]"
        deactivate
        ok "Installed into $REPO_ROOT/.venv"
        link_scripts_to_local_bin
    fi
    verify_scripts
}

# When we fall back to a venv, the .mcp.json server invokes `bear-harness-mcp` as a
# bare command — so symlink the three console scripts somewhere on PATH.
link_scripts_to_local_bin() {
    LOCAL_BIN="$HOME/.local/bin"
    mkdir -p "$LOCAL_BIN"
    for s in bear-harness bear-harness-mcp bear-harness-dashboard; do
        ln -sf "$REPO_ROOT/.venv/bin/$s" "$LOCAL_BIN/$s"
    done
    ok "Symlinked console scripts into $LOCAL_BIN"
    case ":$PATH:" in
        *":$LOCAL_BIN:"*) : ;;
        *) warn "$LOCAL_BIN is not on your PATH — add it so Claude Code can launch the MCP server:";
           warn "  export PATH=\"\$HOME/.local/bin:\$PATH\"   # add to ~/.zshrc or ~/.bashrc" ;;
    esac
}

verify_scripts() {
    for s in bear-harness bear-harness-mcp bear-harness-dashboard; do
        if have "$s"; then ok "$s is on PATH"; else
            warn "$s is NOT on PATH yet — open a new shell (or fix PATH per the note above)."; fi
    done
}

# ── Config seeding ───────────────────────────────────────────────────
seed_hosts() {
    info "Seeding the laptop host config"
    CFG_DIR="$HOME/.config/bear-harness"
    CFG="$CFG_DIR/hosts.toml"
    if [ -f "$CFG" ]; then
        ok "hosts.toml already exists at $CFG (left untouched)"
    else
        mkdir -p "$CFG_DIR"
        cp "$REPO_ROOT/hosts.toml.example" "$CFG"
        ok "Wrote a starter $CFG"
        warn "Edit it for YOUR account: ssh_alias (a ~/.ssh/config Host), remote_rds_root,"
        warn "remote_inbox. See bearpilot/references/cluster-ground-truth.md."
        warn "Or just ask Claude: run /bearpilot:setup and it fills this in for you."
    fi
}

# The bundled bash harness reads its identity from ~/.config/bearpilot/env (BB_* vars),
# separate from the MCP's hosts.toml. Seed it from the template if absent.
seed_harness_env() {
    info "Seeding the bash-harness config"
    ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/bearpilot"
    ENV_FILE="$ENV_DIR/env"
    if [ -f "$ENV_FILE" ]; then
        ok "harness env already exists at $ENV_FILE (left untouched)"
    else
        mkdir -p "$ENV_DIR"
        cp "$REPO_ROOT/bearpilot.env.example" "$ENV_FILE"
        ok "Wrote a starter $ENV_FILE"
        warn "Edit it for YOUR account: BB_USER, BB_ACCOUNT, BB_RDS_ROOT. Until you do, the"
        warn "bash harness fails fast with a reminder. /bearpilot:setup fills it in for you."
    fi
}

# ── Next steps ───────────────────────────────────────────────────────
print_next_steps() {
    info "Next steps"
    cat <<EOF
  1. Add the plugin in Claude Code (either path works):
       • from GitHub (public repo):      /plugin marketplace add jaymd96/bearpilot
       • local clone:                    /plugin marketplace add $REPO_ROOT
     then:  /plugin install bearpilot@bearpilot   (restart Claude Code)
     NB: the plugin self-installs the engine from PyPI on first MCP/dashboard use; this
     script is only needed for a PATH-wide `bear-harness` CLI or an offline pre-seed.

  2. Configure your cluster — state your identity ONCE. Easiest: open this folder in Claude
     Code and say "set me up for BlueBEAR" (/bearpilot:setup); it writes both config files.
     By hand, fill in YOUR values in:
       • ~/.config/bear-harness/hosts.toml   (MCP + dashboard; + a matching ~/.ssh/config Host)
       • ~/.config/bearpilot/env             (bash harness: BB_USER, BB_ACCOUNT, BB_RDS_ROOT)

  3. Verify (read-only, no compute):
       bear-harness --help
       ssh -o BatchMode=yes <your-ssh-alias> 'squeue --me'   # confirms key auth + reachability

  The bundled bash harness (bearpilot/harness/) needs nothing but ssh — once ~/.config/bearpilot/env
  has your identity (step 2), you can use it immediately, even before the engine install.
EOF
}

# ── Main ─────────────────────────────────────────────────────────────
case "${1:-install}" in
    --help|-h) sed -n '2,19p' "$0"; exit 0 ;;
    --check)   check_prereqs; exit 0 ;;
    install|*) check_prereqs; install_engine; seed_hosts; seed_harness_env; print_next_steps;
               info "Done — Bearpilot is installed." ;;
esac
