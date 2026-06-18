---
name: setup
description: First-time setup of Bearpilot on a new machine — write the user's BlueBEAR config, get the bundled bash harness working, and install the bear-harness engine from the repo. Use when the user asks to "set me up", "get started", "install this", "configure BlueBEAR", or when bear-harness / the MCP server / the dashboard isn't installed yet. This is the front-door onboarding skill; it drives the /bearpilot:setup command.
---

# Setting up Bearpilot (the Claude-driven onboarding)

Bearpilot is **two separately-installed pieces**, and knowing which is where is the key to setup:

- **The plugin** (skills, slash commands, references, and a zero-dependency **bash harness**) — this
  is what's installed in Claude Code. When installed from the marketplace, *only the plugin folder*
  is copied into Claude's plugin cache, at `${CLAUDE_PLUGIN_ROOT}`. The bash harness ships inside it
  (`${CLAUDE_PLUGIN_ROOT}/harness/`).
- **The engine** — the `bear-harness` Python package (the CLI, the MCP server, the live dashboard,
  and the autonomous deploy loop). This lives in the **repo, not the plugin**, and is installed
  separately: **https://github.com/jaymd96/bearpilot**

So **don't assume `install.sh`, `hosts.toml.example`, or the engine sit next to the plugin** — for a
marketplace install they don't. Write the config directly, and get the engine from the repo.

## What "set up" means — three independent layers, install only what they need

| Layer | Needs | Gives them |
|---|---|---|
| **Bash harness** | just `ssh` (+ `rsync`) + the config below | `/connect`, `/new-job`, `/launch`, `/jobs`, `/status` — works the moment config is written, no engine |
| **MCP + dashboard** | the engine from the repo (`./install.sh`) + `hosts.toml` | in-chat tools, `ui://dashboard`, the live browser dashboard |
| **Autonomous deploy** | the engine **also installed on the cluster** (`scripts/setup-bluebear.sh` from the cloned repo) | `deploy` / `check` / `fetch` over SSH (the full vibe-code loop) |

Someone who only wants to submit and watch jobs needs nothing but `ssh` + the config. Don't over-install.

## The path

1. **Locate the engine — don't assume it's next to the plugin.** Check in order:
   `command -v bear-harness` (already installed) → `[ -f "${CLAUDE_PLUGIN_ROOT}/../install.sh" ]` (a
   local clone) → otherwise the plugin is standalone (marketplace) and the engine isn't here yet.

2. **Get the user's own cluster identity — default to the AskUserQuestion tool, not a prose ask.**
   In one call, ask four questions (free-text answers come via each question's **Other** field):
   **Username** (cluster login like `abc123`, *not* their laptop user — otherwise `ssh` defaults to
   the wrong user and fails *Permission denied*); **SLURM account / project** (gives the RDS root
   `/rds/projects/<g>/<account>`; offer "auto-discover over SSH" if they don't know it); **SSH access**
   already working? (yes / not yet / not sure — off-campus needs the VPN); and the **`~/.ssh/config`
   alias** (default `bluebear`). Prefer AskUserQuestion over prose whenever you need a decision.
   If the account is unknown, backfill it after SSH works via
   `ssh <alias> 'sacctmgr -nP show assoc user=$USER format=account,qos'`.

3. **Wire SSH** — add/confirm a `~/.ssh/config` Host block (HostName `bluebear.bham.ac.uk`, their
   User, `ControlMaster auto`, a `ControlPath`, `ControlPersist 10m`). Auth is the user's **own SSH
   public key** (non-interactive); off-campus requires the University **VPN**.

4. **Write both config files directly** (not from the `*.example` templates — those are in the repo,
   not the installed plugin). Never overwrite an existing file without asking.
   - `~/.config/bearpilot/env` (bash harness): `export BB_USER=… BB_ACCOUNT=… BB_RDS_ROOT=…`. The
     harness sources this and **fails fast** with a reminder until it's set.
   - `~/.config/bear-harness/hosts.toml` (MCP + dashboard): `default`, then a `[hosts.<alias>]` table
     with `ssh_alias` / `remote_rds_root` / `remote_inbox`.

5. **Verify, read-only.** `ssh -o BatchMode=yes <alias> 'squeue --me'` — a clean exit proves key auth
   + reachability + a valid account, zero compute. `${CLAUDE_PLUGIN_ROOT}/harness/bb-connect.sh` also
   prints the *live* cluster ground-truth. On failure, diagnose (wrong username, VPN off, key not
   added). **The bash harness is now usable.**

6. **Install the engine** (for the MCP tools, the dashboard, and `deploy`/`launch`):
   - on PATH already → done;
   - local clone → `bash "${CLAUDE_PLUGIN_ROOT}/../install.sh"`;
   - otherwise → point the user at the repo and have them run:
     ```bash
     git clone https://github.com/jaymd96/bearpilot
     cd bearpilot && ./install.sh
     ```
     then **restart Claude Code** so the `.mcp.json` server is picked up. The autonomous deploy loop
     additionally needs the engine on the cluster: `bash scripts/setup-bluebear.sh` from that clone
     (it reads the `~/.config/bearpilot/env` you wrote).

## Discipline to carry in from the start

- **Login nodes are orchestration-only** — never run heavy compute there (the `bluebear-basics` skill).
- **Never key a check on a PID** — login nodes are round-robin; trust shared-FS artifacts + `sacct`
  (the `observability` skill). Every probe here is read-only.
- The encoded account/QoS/GRES/RDS strings **change over time**; the single source of truth is
  `${CLAUDE_PLUGIN_ROOT}/references/cluster-ground-truth.md`, which carries its own re-probe commands.

Once verified, point the user at `bluebear-basics` to start, or `monitoring-dashboard` to run and
watch an experiment without the CLI.
