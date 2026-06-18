<p align="center">
  <img src="docs/assets/bearpilot-banner.png" alt="Bearpilot — Claude Code cockpit for BlueBEAR SLURM jobs" width="100%">
</p>

# Bearpilot

> Run jobs on the University of Birmingham **BlueBEAR** SLURM cluster from Claude Code (or a plain
> terminal): scaffold a job, submit it, watch it, and pull the results back — and, when you want,
> hand the whole deploy-and-monitor loop to an AI agent. Bearpilot encodes the cluster's quirks so
> you get it right the first time instead of rediscovering them.

It's specific to BlueBEAR (University of Birmingham). You bring your own cluster account; bearpilot
brings the know-how.

## What you can do

- **Submit and watch a job** without memorising SLURM flags — scaffold from a template, submit, and follow it to the end.
- **Serve a model on an A100** with vLLM and run a worker against it as one managed unit.
- **Deploy and walk away** — start a job in the background, close your laptop, and reconnect to it later by its run id from any session.
- **Monitor without the terminal** — a live browser dashboard and in-chat status when you drive it from Claude Code.
- **Stay safe on a shared cluster** — resource limits (queue, walltime, GPU-hours) are checked *before* anything is submitted, so a bad request fails loudly instead of burning your allocation.

## Pick your path

Bearpilot is one tool with three ways in — choose by how you like to work:

| You want… | Use | Needs |
|---|---|---|
| **Claude Code to drive it** — slash commands, skills, a live dashboard | the **plugin** | Claude Code + a one-time install |
| **A command-line tool** you call from any shell | the **`bear-harness` CLI** | `./install.sh` (Python 3.11+) |
| **The absolute minimum** — submit & watch with nothing but `ssh` | the **bash harness** | just `ssh` + `rsync` |

They share one cluster config, and the two installs complement each other: a repo clone +
`./install.sh` gives you the **engine** (the `bear-harness` CLI, the MCP server, and the dashboard),
while the **plugin** adds the Claude Code cockpit and bundles the **bash harness**. Note: installing
the plugin from the marketplace *on its own* does **not** include the engine — for the CLI, MCP, and
dashboard, clone the repo and run `./install.sh`.

## Install

```bash
git clone <this-repo> bearpilot && cd bearpilot
./install.sh        # checks prerequisites, installs the engine, seeds your config files
```

Then add the **plugin** — two ways:

**Claude Desktop (graphical) — via Customize**

1. Open **Customize** in the left sidebar (the hub for connectors, skills, and plugins).
2. Go to **Plugins → Marketplaces** and add this marketplace: `jaymd96/bearpilot`. *(If your version
   doesn't offer "add marketplace" there yet, run `/plugin marketplace add jaymd96/bearpilot` once in
   the chat — it'll then show up here.)*
3. Switch to **Discover**, find **bearpilot**, choose an install scope (**User** = available
   everywhere), and click **Install**.
4. Run `/reload-plugins` (or restart Claude), and **approve** the bear-harness MCP server if prompted.

**Slash commands (any client)**

```
/plugin marketplace add jaymd96/bearpilot      # or: /plugin marketplace add <path-to-your-clone>
/plugin install bearpilot@bearpilot            # then /reload-plugins (or restart)
```

**Or just ask Claude to do all of this** — open this folder in Claude Code and say *"set me up for
BlueBEAR"* (`/bearpilot:setup`). Prefer the bash harness only? It needs nothing but `ssh` — no
Python install at all.

→ **New here? [Getting Started](docs/GETTING-STARTED.md)** walks you from install to a complete
first job you can copy-paste.

## Configure (once)

Bearpilot never ships a real account — you point it at *yours* one time, and it lives in your home
directory, never in the repo. The fastest way is `/bearpilot:setup` (it asks for your BlueBEAR
username, project, and SSH details and writes everything for you). To do it by hand, it's two small
config files plus an `~/.ssh/config` entry — all explained in **[Configuration](docs/CONFIGURATION.md)**.

## A few words you'll see

| Term | What it means for you |
|---|---|
| **run id** | A handle for one job run. Keep it and you can check status, tail logs, or pull results back later — even from a brand-new session. |
| **the engine** (`bear-harness`) | The program that actually submits and tracks your jobs. The plugin drives it; you can also call it directly as a CLI. |
| **preset** | A ready-made job shape (e.g. "serve a vLLM model + run a worker"). You pick one; you don't have to build it. |
| **endpoint** | The URL a served model listens on, so a worker job can send it requests. |
| **QoS** | A BlueBEAR job queue with limits (e.g. `bbshort` = fast, ≤10 min; `bbgpu` = long GPU jobs). |
| **GRES** | How you ask SLURM for a GPU (e.g. `gpu:a100:1` = one A100). |

## How it works (under the hood)

Bearpilot is a **plugin** (the cockpit — skills, slash commands, a dashboard) plus **bear-harness**
(the engine it drives — also a CLI you can use directly). The engine submits your jobs to SLURM
over SSH, tracks their state on the cluster's shared filesystem so any session can reattach by run
id, and enforces your resource limits before submitting. That's all you need to use it; the design
rationale and the formal job contract live in [`docs/internal/`](docs/internal/) for people who
want to contribute.

## Docs

- **[Getting Started](docs/GETTING-STARTED.md)** — pick a path, configure, run your first job.
- **[Configuration](docs/CONFIGURATION.md)** — point bearpilot at your BlueBEAR account.
- **[Running on BlueBEAR](docs/bluebear.md)** — the from-scratch cluster walkthrough.
- **[Troubleshooting](docs/troubleshooting.md)** — when a job won't start or a model won't load.
- **[The plugin](bearpilot/README.md)** — its skills, slash commands, and the bundled bash harness.
- **[For contributors](docs/internal/)** — design notes, the job contract, how to hack on it.

## License

Apache-2.0 — see [LICENSE](LICENSE).
