# Getting started

This takes you from nothing to a first job running on BlueBEAR. Pick the path that matches how you
like to work, do the one-time setup, then copy-paste a complete first run.

## Before you start

You need:

- A **BlueBEAR account** (University of Birmingham) with a project allocation, and the University
  **VPN** if you're off campus.
- **SSH key access** to `bluebear.bham.ac.uk` (so connections don't prompt for a password).
- For the CLI / plugin paths: **Python 3.11+**, plus `ssh` and `rsync`. The bash-harness path needs
  only `ssh` + `rsync`.

Don't know your project code or RDS path? You'll find them during setup (or run
`ssh <you>@bluebear.bham.ac.uk 'sacctmgr -nP show assoc user=$USER format=account,qos'`).

## 1. Pick a path

| If you want… | Go to |
|---|---|
| **Claude Code to drive it** (natural language, a dashboard) | [Path A — the plugin](#path-a-claude-code-plugin) |
| **A command-line tool** you call yourself | [Path B — the `bear-harness` CLI](#path-b-the-bear-harness-cli) |
| **The lightest thing that works**, just `ssh` | [Path C — the bash harness](#path-c-the-bash-harness) |

Installing the plugin (Path A) also gives you B and C — they share one config.

## 2. Configure once

Bearpilot never ships a real account; you point it at yours one time. The fastest way is to let
Claude do it — open this folder in Claude Code and say *"set me up for BlueBEAR"*
(`/bearpilot:setup`). To do it by hand, it's an `~/.ssh/config` host entry plus two small files in
your home directory. All three are explained in **[Configuration](CONFIGURATION.md)** — set them up
now, then come back.

A 30-second sanity check that your access works (read-only, no compute):

```bash
ssh -o BatchMode=yes <your-ssh-alias> 'squeue --me'   # prints your (probably empty) job list
```

If that prints a table, you're ready.

---

## Path A — Claude Code plugin

After `./install.sh` and `/plugin install bearpilot@bearpilot` (see the [README](../README.md#install)),
just describe what you want. Bearpilot's skills turn it into the right cluster commands and keep you
to the safe path (short canary first, never heavy compute on a login node).

Try these, in order:

```
/bearpilot:connect            # pin a login node and show your live cluster ground-truth
"scaffold a short CPU job that prints hostname and date, and submit it on bbshort"
/bearpilot:jobs               # what's queued / running / just finished
/bearpilot:dashboard          # a live browser dashboard of your jobs + logs
```

To run something real end to end, ask: *"run the example ETL job on the cluster and fetch the
results"* — Claude will validate it, check it against your limits, submit it, watch it, and pull the
output back. For a model-serving example, ask it to *"serve a small model on an A100 and run a test
prompt against it."*

## Path B — the `bear-harness` CLI

After `./install.sh`, `bear-harness` is on your PATH. Here's a complete first run using the **ETL
example** that ships with the repo — a tiny CPU-only job (no GPU, no model), so it's cheap and fast.

```bash
# Inspect the job description (no cluster needed — this just parses and prints):
bear-harness validate tests/fixtures/etl_pipeline.toml

# Pre-flight it against your resource limits (still no submit):
bear-harness check --qos bbshort

# One-time: install the engine on the login node and prepare your RDS workspace.
# (scripts/setup-bluebear.sh does this; or /bearpilot:setup did it during setup.)
bash scripts/setup-bluebear.sh

# Submit it to the cluster over SSH, detached — returns a run id in seconds:
bear-harness launch tests/fixtures/etl_pipeline.toml --remote <your-host> --detach --json
#   → {"run_id": "etl-demo-2026...", ...}

# Reconnect to it any time, from any session:
bear-harness status  --remote <your-host> <run_id>
bear-harness logs    --remote <your-host> <run_id>
bear-harness fetch   --remote <your-host> <run_id>    # pull artifacts back to your laptop
```

`<your-host>` is the host name in your `hosts.toml` (default `bluebear`). Swap
`tests/fixtures/etl_pipeline.toml` for your own `pipeline.toml` once this works. The model-serving
(vLLM) flow is the same shape with a `[model]` block — see
[Running on BlueBEAR](bluebear.md) for that walkthrough.

## Path C — the bash harness

No Python, no engine install — just `ssh`. From the repo root:

```bash
H=bearpilot/harness

$H/bb-connect.sh                      # pin a login node, print live ground-truth
$H/bb-new-job.sh cpu hello --short    # scaffold a short CPU sbatch into bb-jobs/hello/
$H/bb-submit.sh bb-jobs/hello/hello.sbatch --watch   # submit + follow to completion
$H/bb-fetch.sh hello                  # pull its output back
```

Edit the scaffolded `bb-jobs/hello/hello.sbatch` to run whatever you like before submitting. The
harness keys every watcher on the shared filesystem + `sacct`, never a PID, so it survives BlueBEAR's
round-robin login nodes.

---

## How do I know it worked?

- The job reaches a terminal state — `COMPLETED` in `bear-harness status` / `bb-jobs.sh`, or `sacct`
  showing `COMPLETED` with exit code `0:0`.
- `fetch` (or `bb-fetch.sh`) pulls back an artifacts directory; the ETL example writes `etl ok` to
  its log.
- For a served model, `bear-harness logs` shows the endpoint coming up and the worker getting `200`s.

If a job won't start or a model won't load, see **[Troubleshooting](troubleshooting.md)** (keyed by
the symptom you actually see) and the **[validation loop](runbooks/validation.md)** (how to debug a
run on `bbshort` in ~5-minute cycles).

## What next

- **Serve a model + run a worker** as one unit → [Running on BlueBEAR](bluebear.md).
- **Watch experiments without the terminal** → the `monitoring-dashboard` skill and
  `/bearpilot:dashboard`.
- **Write your own job shape** (a "preset") → the `authoring-presets` skill and the
  [job contract](internal/specs/01-foundational-contract.md).
- **Understand the cluster's quirks** → `bearpilot/references/gotchas.md` (the things that bite
  first) and `bearpilot/references/cluster-ground-truth.md`.
