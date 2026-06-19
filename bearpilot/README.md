# Bearpilot

> A Claude Code plugin that makes it easy to build, submit, observe, and recover jobs on the
> University of Birmingham **BlueBEAR** SLURM cluster — from a first `sbatch` to autonomous
> vLLM serving. It encodes the cluster ground-truth and the hard-won operational discipline so
> an agent (or a human) gets it right the first time instead of rediscovering the traps.

## What's in here

```
bearpilot/
├── .claude-plugin/
│   ├── plugin.json            the plugin manifest (declares skills, commands, mcpServers)
│   └── marketplace.json       marketplace registration (install like any other plugin)
├── .mcp.json                  registers the bear-harness MCP server (via the self-bootstrapping launcher)
├── skills/                    progressive knowledge, simple → very advanced
│   ├── bluebear-basics/       connect, the login/compute model, the ground-truth   ← start here
│   ├── batch-jobs/            sbatch fundamentals + the scaffold→submit→watch loop
│   ├── gpu-and-serving/       A100 jobs + the proven vLLM/apptainer serve recipe
│   ├── observability/         track running jobs; the shared-FS-not-PID discipline
│   ├── bear-harness/          the LLM-callable deploy tool (JobGraph, guardrails, reattach)
│   ├── monitoring-dashboard/  the no-CLI loop: deploy + watch via MCP, render the dashboard
│   └── authoring-presets/     extend bear-harness with a new workload type
├── commands/                  /connect · /new-job · /launch · /jobs · /status
├── references/                the citable knowledge core
│   ├── cluster-ground-truth.md   account, QoS, GRES, modules, RDS paths (with provenance)
│   ├── gotchas.md                the 11 things that bite first
│   └── links.md                  official docs + SLURM CLI + bear-harness pointers
└── harness/                   a zero-dependency bash harness + engine launchers
    ├── lib/common.sh          shared config (BB_* overrides) + SSH/rsync discipline
    ├── lib/ensure-engine.sh   installs the pinned bear-harness engine from PyPI on first use
    ├── engine.pin             the exact engine version the plugin installs (== src/__about__.py)
    ├── bear-harness-mcp.sh  bear-harness-dashboard.sh   self-bootstrapping engine launchers
    ├── bb-connect.sh  bb-new-job.sh  bb-submit.sh  bb-watch.sh  bb-jobs.sh  bb-fetch.sh
    └── templates/             cpu · gpu · vllm-serve · array · python-venv sbatch templates
```

## Using it (the 60-second tour)

Slash commands (after the plugin is installed in Claude Code):

```
/bearpilot:connect              pin a login node + probe live ground-truth
/bearpilot:new-job gpu my-train scaffold a correct sbatch
/bearpilot:launch bb-jobs/my-train/my-train.sbatch    submit + follow
/bearpilot:jobs --all           what's running / what just ran
/bearpilot:status <job_id>      follow one job to completion
```

Or drive the bundled harness directly (no install, pure bash):

```bash
H=bearpilot/harness
$H/bb-connect.sh
$H/bb-new-job.sh cpu hello --short
$H/bb-submit.sh bb-jobs/hello/hello.sbatch --watch
$H/bb-fetch.sh hello
```

The skills activate automatically when you ask Claude about BlueBEAR — "how do I serve a model
on the cluster", "what's running on BlueBEAR", "this job keeps failing", "set up an autonomous
deploy" — each routes to the right layer.

## Live dashboard — monitor without the CLI

Two surfaces, both driven by the `monitoring-dashboard` skill:

- **In chat (MCP):** the plugin registers the **bear-harness MCP server** (`.mcp.json`) — typed
  tools (`deploy` · `check` · `dashboard` · `jobs` · `logs` · `status` · `fetch` · `cancel` ·
  `commands`), the `bear://guardrails/allowed` + `bear://commands` resources, and a
  **`ui://dashboard`** HTML resource. Vibe-code a whole experiment — describe it, deploy it,
  watch a jobs dashboard + logs render in the chat. (The engine auto-installs from PyPI, pinned —
  the MCP server self-bootstraps on first use; no separate setup.)
- **In the browser (truly live):** `/bearpilot:dashboard` serves a loopback page at
  `http://127.0.0.1:8765/` that **auto-refreshes** the job table and tails a run's log on its own.
  It runs the bundled launcher, which provisions the pinned engine from PyPI on first use.

Every `deploy`/`cancel` is written to a shared-FS **command audit**
(`$RDS_ROOT/.bear-harness/launchpad-audit.jsonl`), so "see all the commands" is durable and the
same across sessions and users (read it via the `commands` tool, `bear://commands`, or either
dashboard). Honest limit: MCP tool calls are request/response (the in-chat dashboard is a
snapshot you re-poll); the browser dashboard is the continuously-updating view. Prefer
zero Python? The bundled bash harness below needs nothing but `ssh`.

## Two paths, by need

| You want… | Use |
|---|---|
| One job (CPU/GPU/array) on the cluster, fast, no install | the **bundled bash harness** (`batch-jobs`, `gpu-and-serving`, `observability` skills) |
| Server+worker as one unit, guardrails, reattach-by-`run_id`, an autonomous agent | **bear-harness** (`bear-harness`, `authoring-presets` skills) |

## How it works (understanding)

Three facts about BlueBEAR drive every design choice, and they're baked into the harness so you
can't regress past them:

1. **Login nodes are orchestration-only** — real work goes through `sbatch`.
2. **Login nodes are round-robin** — node-local state (PIDs/`/tmp`/`nohup`) lies; trust the
   shared filesystem + `sacct`. Every watcher here keys on durable artifacts, never a PID.
3. **No SLURM REST door** — drive SLURM via its CLI over SSH.

Cluster strings (QoS, GRES, CUDA modules, RDS paths) **change over time** as the cluster is updated. The single source of truth is
[`references/cluster-ground-truth.md`](references/cluster-ground-truth.md), written
snapshot-with-provenance so its staleness is detectable; `bb-connect.sh` prints the *live*
values next to the encoded ones so drift surfaces immediately.

## Modifying it (another account / cluster)

Every default lives in [`harness/lib/common.sh`](harness/lib/common.sh) behind a `BB_*` env
var. Point the harness elsewhere without editing a file:

```bash
BB_USER=abc123 BB_ACCOUNT=other-project BB_RDS_ROOT=/rds/projects/x/other \
  bearpilot/harness/bb-connect.sh
```

To extend the knowledge, edit the `references/` files (keep the provenance footer current) and
add skills under `skills/<name>/SKILL.md`.

---

*License: Apache-2.0. The plugin installs its companion **bear-harness** engine from PyPI, pinned in
[`harness/engine.pin`](harness/engine.pin); the canonical source + deep docs live one level up at the
repo root. See the repo-root [`README.md`](../README.md) for the front-door setup.*
