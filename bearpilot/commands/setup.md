---
description: Set up Bearpilot on this machine — install the engine, write hosts.toml for the user's account, and verify reachability (the Claude-driven setup path)
argument-hint: "[ssh-alias]"
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

Drive a first-time setup of Bearpilot for the user, end to end, so they don't have to
read the CLI. Follow the `setup` skill. Be concise; do the mechanical parts yourself and only
ask the user for the few values that are personal to their account.

Steps:

1. **Prereqs.** Run `bash "${CLAUDE_PLUGIN_ROOT}/../install.sh" --check` (or from the repo root,
   `./install.sh --check`). If a prerequisite is missing, tell the user exactly what to install
   and stop. (The plugin folder sits inside the repo; the engine + install.sh are at the repo root.)

2. **Gather the user's cluster identity.** These differ per person — the values shipped in the
   repo are the author's defaults. Use AskUserQuestion (or just ask) for:
   - their **BlueBEAR username** (e.g. `abc123` — NOT their laptop username),
   - their **SLURM account / project** (find it together with `ssh … 'sacctmgr -nP show assoc user=$USER format=account,qos'` once SSH works), which gives the **RDS root** `/rds/projects/<g>/<account>`,
   - an **ssh alias** to use in `~/.ssh/config` (default `bluebear`; or `$1` if they passed one).

3. **Wire SSH + both config files** (one set of answers → both surfaces).
   - Ensure `~/.ssh/config` has a `Host <alias>` block (HostName `bluebear.bham.ac.uk`, their
     User, ControlMaster auto, a ControlPath, ControlPersist 10m). Show it; let them confirm or
     paste their key setup. Off-campus needs the University VPN.
   - Seed `~/.config/bear-harness/hosts.toml` from `hosts.toml.example` (MCP + dashboard) → set
     ssh_alias / remote_rds_root / remote_inbox.
   - Seed `~/.config/bearpilot/env` from `bearpilot.env.example` (bash harness) → set BB_USER /
     BB_ACCOUNT / BB_RDS_ROOT (the harness fails fast until these are set).
   - Never clobber an existing config file without asking.

4. **Install the engine.** Run `bash install.sh` (no args). Confirm `bear-harness`,
   `bear-harness-mcp`, `bear-harness-dashboard` resolve on PATH afterward (the script reports this;
   if it used a venv fallback, make sure `~/.local/bin` is on PATH).

5. **Add the plugin** (if not already): tell them to run `/plugin marketplace add jaymd96/bearpilot`
   (collaborator) **or** `/plugin marketplace add <repo-root>` (local clone), then
   `/plugin install bearpilot@bearpilot` and restart Claude Code.

6. **Verify — read-only, no compute.** Run `ssh -o BatchMode=yes <alias> 'squeue --me'` (and
   optionally `${CLAUDE_PLUGIN_ROOT}/harness/bb-connect.sh`). A clean exit with the user's empty/jobs
   table proves key auth + reachability + a correct account. Report the result plainly; if it fails,
   diagnose (wrong username, VPN, key not added) rather than retrying blindly.

Never run heavy compute on the login node, and never key any check on a PID — that discipline is in
the `bluebear-basics` and `observability` skills. If the engine isn't installed, the bundled bash
harness still works with nothing but ssh.
