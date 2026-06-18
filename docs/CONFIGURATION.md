# Configuration

Bearpilot ships **no real account** — only placeholders like `your-username` / `your-project`. You
point it at your own BlueBEAR account once, and that lives in your home directory, never in the repo.

## The fast way

Open this folder in Claude Code and say *"set me up for BlueBEAR"* (`/bearpilot:setup`). It asks for
your username, project, and SSH details and writes all three pieces below for you, then verifies the
connection read-only. If you'd rather do it by hand, read on.

## What you're configuring (and why there are three pieces)

| Piece | Lives in | Drives |
|---|---|---|
| **SSH host entry** | `~/.ssh/config` | *all* connections (keys, jump-hosts, connection reuse) — shared by everything |
| **Host config** | `~/.config/bear-harness/hosts.toml` | the engine, the MCP server, the live dashboard, and `bear-harness --remote` |
| **Harness env** | `~/.config/bearpilot/env` | the bundled bash harness (the `BB_*` variables) |

You don't necessarily need all three:

- **Bash harness only** → `~/.ssh/config` + `~/.config/bearpilot/env`.
- **CLI / plugin / dashboard** → `~/.ssh/config` + `~/.config/bear-harness/hosts.toml`.
- **Everything** → all three (this is what `/bearpilot:setup` and `./install.sh` set up).

The two config files keep your identity out of the repo; `./install.sh` seeds both from the
`*.example` templates if they don't exist yet.

## 1. SSH (`~/.ssh/config`)

All the networking lives here, where it belongs — bearpilot never reinvents SSH config. Add a host
block (replace `<your-username>`):

```sshconfig
Host bluebear
    HostName       bluebear.bham.ac.uk
    User           <your-username>          # your CLUSTER login (e.g. abc123), NOT your laptop user
    ControlMaster  auto
    ControlPath    ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

Use **key auth** (so connections are non-interactive) and connect over the University **VPN** when
off campus. Test it: `ssh -o BatchMode=yes bluebear 'echo ok'` should print `ok` with no prompt.

## 2. Host config (`~/.config/bear-harness/hosts.toml`)

The engine, the dashboard, and `--remote` read this. Copy `hosts.toml.example` from the repo root
and fill in your values:

```toml
default = "bluebear"

[hosts.bluebear]
ssh_alias       = "bluebear"                              # must match the ~/.ssh/config Host above
remote_rds_root = "/rds/projects/p/your-project"          # /rds/projects/<first-letter>/<account>
remote_inbox    = "/rds/projects/p/your-project/.bear-harness/inbox"
```

`ssh_alias` is the only link to networking — it points at the `~/.ssh/config` block, so the username,
keys, and jump-hosts come from there.

## 3. Harness env (`~/.config/bearpilot/env`)

The bash harness reads its identity from `BB_*` environment variables. Copy `bearpilot.env.example`
from the repo root and fill in:

```bash
export BB_USER="your-username"                  # your cluster login (NOT your laptop user)
export BB_ACCOUNT="your-project"                # your SLURM project account
export BB_RDS_ROOT="/rds/projects/p/your-project"
# export BB_MAIL_USER="you@example.com"         # optional — SLURM job-state emails
```

Until these are set, the bash harness **fails fast** with a reminder rather than silently trying to
connect as `your-username`. The universal cluster constants (the queue names, the `a100` GRES, the
CUDA module) ship as real defaults — you only override one of those if the cluster changes it.

## Finding your values

| Value | How to find it |
|---|---|
| **username** | your BlueBEAR login (the `abc123`-style id you SSH in with) |
| **account / project** | `ssh bluebear 'sacctmgr -nP show assoc user=$USER format=account,qos'` |
| **RDS root** | `/rds/projects/<first-letter-of-account>/<account>` |
| **queues you may use** | the `qos` column from the `sacctmgr` command above |

The live, citable cluster facts are in
[`../bearpilot/references/cluster-ground-truth.md`](../bearpilot/references/cluster-ground-truth.md)
(with re-probe commands for when a value looks stale).
