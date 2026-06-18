---
name: monitoring-dashboard
description: Run and monitor BlueBEAR experiments end-to-end through the bear-harness MCP server, with a live visual dashboard instead of the CLI. Use when the user wants to vibe-code a complete experiment (describe it, deploy it, watch it, fetch results) without touching the terminal, wants a dashboard of running jobs, wants to see live logs, or asks to monitor experiments visually. Provides the MCP tools (deploy/dashboard/jobs/logs/status/fetch/cancel/check), the ui://dashboard resource, and how to render the dashboard as an inline widget.
---

# Monitoring BlueBEAR experiments (the no-CLI loop)

This skill turns the `bear-harness` MCP server into a vibe-code-and-watch loop: the user
describes an experiment, you deploy it, and you keep a live **dashboard + logs** in the chat —
no terminal required. It builds on the `bear-harness` skill (the deploy tool) and the
`observability` discipline (trust shared-FS state + `sacct`, never a PID).

## Prerequisites (one-time)

The MCP server is part of the bear-harness engine, which this repo bundles. The fastest path is
`./install.sh` from the repo root (or the `/bearpilot:setup` command / `setup` skill),
which installs it with the `[mcp]` extra and seeds `hosts.toml`. To do it by hand from the repo
root (one level up from this plugin folder):

```bash
pip install -e ".[mcp]"          # installs the `bear-harness-mcp` console script
# configure a host in ~/.config/bear-harness/hosts.toml (ssh_alias → a BlueBEAR login node);
# BEAR_HARNESS_MCP_HOST picks one. See hosts.toml.example at the repo root.
```

The plugin's `.mcp.json` registers the server as `bear-harness`; restart Claude Code to pick it
up. (No MCP server installed yet? Fall back to the bundled bash harness — the `observability`
skill — which needs nothing but ssh.)

## The tools you now have

| Tool | Use |
|---|---|
| `check(qos, walltime, gpu_gres)` | pre-flight a request against the default-deny guardrails |
| `deploy(program_dir)` | upload a program and launch it detached → returns a `run_ref` |
| `dashboard()` | **structured** snapshot: running/pending counts + job rows + known runs |
| `jobs()` | structured list of in-flight jobs (parsed `squeue --me`) |
| `status(run_ref)` | a run's `run.json` (reattach by ref) |
| `logs(run_ref, which, lines)` | tail vllm/pipeline logs |
| `fetch(run_ref)` | pull a finished run's artifacts to the laptop |
| `cancel(run_ref)` | scancel + reap |
| `commands(limit)` | the recent **command audit** (deploy/cancel), newest first — durable + cross-session |

Resources: `bear://guardrails/allowed` (the caps), `bear://commands` (the audit), **`ui://dashboard`**
(ready-made HTML dashboard). Prompts: `monitor_experiments`, `deploy_vllm_pipeline`,
`check_before_submit`.

`deploy` and `cancel` append a line to a shared-FS audit (`$RDS_ROOT/.bear-harness/launchpad-audit.jsonl`),
so "see all the commands" survives across sessions and is the same for everyone on the host.

## The loop

1. **Author.** Turn the user's description into a runnable program dir / `pipeline.toml`
   (see the `bear-harness` and `authoring-presets` skills). Keep it small enough to canary on
   `bbshort` first.
2. **Pre-flight.** Read `bear://guardrails/allowed`, then `check(...)` with the intended
   qos/walltime/gpu_gres. Only proceed if `allowed=true`; if denied, report which cap blocks it
   and the `bear.toml` key to widen — don't retry blindly.
3. **Deploy.** `deploy(program_dir)` → capture the `run_ref`. State it to the user so the run is
   reattachable from any future session.
4. **Monitor — render the dashboard.** Call `dashboard()` and render the JSON as a compact
   inline status widget (see below). Refresh on request or every ~10–20 s while something is
   `RUNNING`/`PENDING`. For one job, `logs(run_ref)` and report the state.
5. **Fetch.** When a run reads `COMPLETED`, `fetch(run_ref)` and summarise the artifacts.

## Rendering the dashboard

Two surfaces, same `DashboardSnapshot` data — use whichever the client supports:

- **Inline widget (works today):** call `dashboard()`, then render the returned JSON as a small
  status card — counts (running/pending/active), a job table with state badges
  (RUNNING=green, PENDING=amber, COMPLETED=blue, FAILED/TIMEOUT/OOM=red), and the known runs.
  Because you ran the deploy/monitor commands this turn, you can also include a **"recent
  commands"** strip from your own context — that's the "see all the commands" view.
- **`ui://dashboard` resource:** returns a self-contained HTML dashboard (jobs + runs + the
  shared audit) for hosts that render MCP-UI resources directly.
- **Live browser dashboard (truly-live):** run `bear-harness-dashboard` (ships in the base
  install — no `[mcp]` extra needed, just `hosts.toml`). It serves a loopback page at
  `http://127.0.0.1:8765/` that **auto-refreshes** the job table every few seconds and tails a
  run's log live — the genuine no-CLI watch. Launch it with `/bearpilot:dashboard`.

Honesty about "live": MCP tool calls are request/response — there's no push stream into chat,
so the inline widget and `ui://dashboard` are *snapshots* you re-poll and re-render. When the
user wants to *watch* continuously, point them at the browser dashboard above — that polls
client-side and updates on its own.

## Discipline carried over

- Trust `squeue`/`sacct` + shared-FS run state, **never a PID** — the dashboard is built on
  exactly those signals and `dashboard()` degrades (sets `error`, empty jobs) rather than
  blanking if `squeue` hiccups.
- Guardrails are **default-deny** and govern resources, not the science — always `check` before
  `deploy`.
- Verify any new SLURM/vLLM/GRES behaviour on a real `bbshort` run before relying on it.
