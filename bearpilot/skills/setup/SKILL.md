---
name: setup
description: First-time setup of Bearpilot on a new machine — install the bundled bear-harness engine, configure SSH + the laptop hosts.toml for the user's own BlueBEAR account, add the Claude Code plugin, and verify reachability read-only. Use when the user just cloned/received this repo, asks to "set me up", "get started", "install this", "configure BlueBEAR", or when bear-harness / the MCP server / the dashboard isn't installed yet. This is the front-door onboarding skill; it drives the /bearpilot:setup command.
---

# Setting up Bearpilot (the Claude-driven onboarding)

This repo bundles two things that ship together: the **plugin** (skills, commands, references, a
zero-dependency bash harness) in `bearpilot/`, and the **engine** — the full
`bear-harness` Python package (kernel + MCP server + live dashboard) — at the repo root. Your job
here is to get a newcomer from a fresh clone to a verified connection with as little CLI as possible.

## What "set up" means (three independent layers — install only what they need)

| Layer | Needs | Gives them |
|---|---|---|
| **Bash harness** | just `ssh` (+ `rsync`) | `/connect`, `/new-job`, `/launch`, `/jobs`, `/status` — works immediately, no Python install |
| **MCP + dashboard** | `pip install ".[mcp]"` (the engine) + `hosts.toml` | in-chat tools, `ui://dashboard`, the live browser dashboard |
| **Autonomous deploy** | the engine **also installed on the cluster** (`scripts/setup-bluebear.sh`) | `deploy`/`check`/`fetch` over SSH (the full vibe-code loop) |

A recipient who only wants to submit and watch jobs needs nothing but ssh. Don't over-install.

## The path

1. **Check prerequisites.** From the repo root: `./install.sh --check` (python3 ≥ 3.11, ssh, rsync;
   gh optional). Stop and name the fix if anything's missing.

2. **Get the user's own cluster identity** — these are personal; the repo ships only placeholders
   (`your-username` / `your-project`), never a real account. Ask for:
   - **BlueBEAR username** (e.g. `abc123`; *not* their laptop username — `ssh bluebear` would
     otherwise default to the wrong user and fail with *Permission denied*).
   - **SLURM account / project** → the **RDS root** is `/rds/projects/<g>/<account>`. If SSH already
     works, confirm with `ssh <alias> 'sacctmgr -nP show assoc user=$USER format=account,qos'`.
   - an **ssh alias** for `~/.ssh/config` (default `bluebear`).

3. **Wire SSH** — add/confirm a `~/.ssh/config` Host block (HostName `bluebear.bham.ac.uk`, their
   User, `ControlMaster auto`, a `ControlPath`, `ControlPersist 10m`). Auth is the user's **own SSH
   public key** (non-interactive); off-campus requires the University **VPN**.

4. **Write both per-user config files** from the one set of answers (the installer seeds both from
   templates if absent; never overwrite an existing file without asking):
   - `~/.config/bear-harness/hosts.toml` (MCP + dashboard) — set `ssh_alias` / `remote_rds_root` /
     `remote_inbox` from `hosts.toml.example`.
   - `~/.config/bearpilot/env` (bash harness) — set `BB_USER` / `BB_ACCOUNT` / `BB_RDS_ROOT` (and
     optionally `BB_MAIL_USER`) from `bearpilot.env.example`. The harness sources this file and
     **fails fast** with a clear reminder until these are set, so don't skip it.

5. **Install the engine** — `./install.sh` (prefers `pipx` so `bear-harness`, `bear-harness-mcp`,
   `bear-harness-dashboard` land on PATH where the plugin's `.mcp.json` can launch the server; a
   `.venv` + `~/.local/bin` symlink fallback is used if pipx is absent — then PATH matters).

6. **Add the plugin** — `/plugin marketplace add jaymd96/bearpilot` (collaborator with
   repo read access) **or** `/plugin marketplace add <repo-root>` (local clone), then
   `/plugin install bearpilot@bearpilot`; restart Claude Code.

7. **Verify, read-only.** `ssh -o BatchMode=yes <alias> 'squeue --me'` — a clean exit proves key
   auth + reachability + a valid account, with zero compute. `harness/bb-connect.sh` additionally
   prints the *live* cluster ground-truth next to the encoded values so drift surfaces. Report the
   result plainly; on failure, diagnose (wrong username, VPN off, key not added) — don't retry blindly.

## Discipline to carry in from the start

- **Login nodes are orchestration-only** — never run heavy compute there (that's the `bluebear-basics`
  skill). All real work goes via `sbatch` (`bbshort` for short canaries).
- **Never key a check on a PID** — login nodes are round-robin; trust shared-FS artifacts + `sacct`
  (the `observability` skill). Every probe here is read-only.
- The encoded account/QoS/GRES/RDS strings **rotate**; the single source of truth is
  `${CLAUDE_PLUGIN_ROOT}/references/cluster-ground-truth.md`, which carries its own re-probe commands.

Once verified, point the user at `bluebear-basics` to start, or `monitoring-dashboard` to run and
watch an experiment without the CLI.
