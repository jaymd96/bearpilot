---
description: Set up Bearpilot on this machine — write your BlueBEAR config, get the bundled bash harness working, and install the bear-harness engine from the repo. The Claude-driven setup path.
argument-hint: "[ssh-alias]"
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
---

Drive a first-time setup of Bearpilot for the user, end to end. Follow the `setup` skill. Do the
mechanical parts yourself; only ask for the few values personal to their account.

**Read this first — how the plugin was installed changes setup.** When installed from the
marketplace, Claude Code copies *only the plugin folder* into its plugin cache — **separate from the
`bear-harness` engine and `install.sh`, which live in the repo, not the plugin.** So do NOT assume
`${CLAUDE_PLUGIN_ROOT}/../install.sh` exists; it usually won't. Two consequences:

- The **bundled bash harness** (`${CLAUDE_PLUGIN_ROOT}/harness/`) ships *with* the plugin and works as
  soon as the config is written — no engine needed.
- The **engine** (the `bear-harness` CLI, the MCP server, the live dashboard, and the autonomous
  `deploy` loop) comes from the repo: **https://github.com/jaymd96/bearpilot**

Steps:

1. **Locate the engine — don't assume it's next to the plugin.** Check, in order:
   - `command -v bear-harness` → already installed; skip the install in step 6.
   - `[ -f "${CLAUDE_PLUGIN_ROOT}/../install.sh" ]` → you're in a *local clone*; the engine + installer
     are at `${CLAUDE_PLUGIN_ROOT}/..`.
   - otherwise → the plugin was installed on its own (the usual marketplace case); the engine isn't
     here. Carry on with config now — you'll point the user at the repo in step 6.

2. **Gather the user's cluster identity — DEFAULT to the AskUserQuestion tool, not prose.** Make one
   AskUserQuestion call with these four questions (free-text values come in via each question's
   built-in **Other** field):
   - header **Username** — "What's your BlueBEAR username? (your cluster login, e.g. `abc123` — NOT
     your laptop user). Pick *Other* to type it." · options: *My BlueBEAR username (type via Other)* ·
     *I don't have BlueBEAR access yet*.
   - header **SLURM account** — "Do you know your SLURM account / project? (it gives the RDS root
     `/rds/projects/<g>/<account>`)." · options: *Yes — I'll enter it (via Other)* · *No —
     auto-discover it over SSH and backfill*.
   - header **SSH access** — "Is SSH key access to BlueBEAR already set up and working? (off-campus
     also needs the University VPN)." · options: *Yes — `ssh` already connects* · *Not yet — I'll set
     up a key* · *Not sure*.
   - header **Host alias** — "Which alias should I use for the BlueBEAR host in `~/.ssh/config`?" ·
     options: *bluebear (default)* · *A different alias (via Other)*. (Use `$1` if they passed one.)

   If they don't know the SLURM account, carry on — auto-discover it once SSH works
   (`ssh <alias> 'sacctmgr -nP show assoc user=$USER format=account,qos'`) and backfill the config.
   In general, prefer AskUserQuestion over a prose question whenever you need a decision from them.

3. **Wire SSH.** Ensure `~/.ssh/config` has a `Host <alias>` block (HostName `bluebear.bham.ac.uk`,
   their User, `ControlMaster auto`, a `ControlPath`, `ControlPersist 10m`). Off-campus needs the VPN.

4. **Write both config files DIRECTLY** from the gathered values — do **not** rely on the `*.example`
   templates (they're in the repo, not the installed plugin). Never clobber an existing file without
   asking.
   - `~/.config/bearpilot/env` (the bundled bash harness reads this):
     ```
     export BB_USER="<username>"
     export BB_ACCOUNT="<account>"
     export BB_RDS_ROOT="/rds/projects/<g>/<account>"
     ```
   - `~/.config/bear-harness/hosts.toml` (the MCP server + dashboard read this):
     ```
     default = "<alias>"
     [hosts.<alias>]
     ssh_alias       = "<alias>"
     remote_rds_root = "/rds/projects/<g>/<account>"
     remote_inbox    = "/rds/projects/<g>/<account>/.bear-harness/inbox"
     ```

5. **Verify — read-only, no compute.** `ssh -o BatchMode=yes <alias> 'squeue --me'` (and optionally
   `${CLAUDE_PLUGIN_ROOT}/harness/bb-connect.sh`). A clean exit proves key auth + reachability + a
   valid account. On failure, diagnose (wrong username, VPN off, key not added) — don't retry blindly.
   **The bash harness is now ready** — they can use `/bearpilot:new-job`, `/bearpilot:launch`, etc.

6. **Install the engine** (needed for the MCP tools, the dashboard, and `deploy`/`launch`). Based on
   step 1:
   - already on PATH → nothing to do.
   - local clone → run `bash "${CLAUDE_PLUGIN_ROOT}/../install.sh"`.
   - otherwise → give the user the repo link and these exact commands:
     ```bash
     git clone https://github.com/jaymd96/bearpilot
     cd bearpilot
     ./install.sh
     ```
     That puts `bear-harness`, `bear-harness-mcp`, and `bear-harness-dashboard` on PATH (and seeds the
     same config you just wrote). **Restart Claude Code** afterward so the MCP server is picked up.
   - For the autonomous **deploy** loop, the engine also installs on the cluster once — from the
     cloned repo: `bash scripts/setup-bluebear.sh` (it reads the `~/.config/bearpilot/env` you wrote
     and fails fast if it's not set).

Never run heavy compute on the login node, and never key a check on a PID — that's the
`bluebear-basics` and `observability` skills.
