---
description: Launch the live BlueBEAR experiment dashboard in the browser (auto-refreshing jobs + logs)
argument-hint: "[--port N] [host]"
allowed-tools: Bash
---

Start the live browser dashboard — a loopback web page that auto-refreshes the running-jobs
table and tails a run's log, so the user can watch experiments without the CLI.

Prerequisites: bear-harness installed (the `bear-harness-dashboard` script ships in the base
install — no `[mcp]` extra needed) and a `hosts.toml` with at least one host. Pick a host with
`BEAR_HARNESS_MCP_HOST=<name>` and a port with `BEAR_HARNESS_DASHBOARD_PORT` (default 8765).

Start it in the background (it's a long-running server) and report the URL:

```bash
BEAR_HARNESS_DASHBOARD_PORT="${PORT:-8765}" bear-harness-dashboard
```

Then:
- Tell the user to open `http://127.0.0.1:<port>/` — it refreshes on its own every few seconds.
- Note it binds to loopback only (it speaks for their SSH credentials; never expose it).
- To watch a specific job's log live, the user pastes a `run_ref` into the log box on the page.
- If `bear-harness-dashboard` isn't found, the package isn't installed — point them at the
  `monitoring-dashboard` skill's prerequisites, or fall back to the bundled bash harness
  (`bb-watch.sh`) which needs nothing but ssh.
- Stop it by ending the background process when the user is done.
