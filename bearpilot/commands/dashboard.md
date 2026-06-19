---
description: Launch the live BlueBEAR experiment dashboard in the browser (auto-refreshing jobs + logs)
argument-hint: "[--port N] [host]"
allowed-tools: Bash
---

Start the live browser dashboard — a loopback web page that auto-refreshes the running-jobs
table and tails a run's log, so the user can watch experiments without the CLI.

Prerequisites: just a `hosts.toml` with at least one host. The engine installs itself — the
launcher below provisions the pinned `bear-harness` from PyPI into a private venv on first run
(one-time, needs network), so this works straight after a plugin install. A matching PATH install
(pipx / `install.sh`) is used as-is. Pick a host with `BEAR_HARNESS_MCP_HOST=<name>` and a port
with `BEAR_HARNESS_DASHBOARD_PORT` (default 8765).

Start it in the background (it's a long-running server) and report the URL. Prefer the bundled
launcher, which provisions the engine for you:

```bash
DASH="${CLAUDE_PLUGIN_ROOT}/harness/bear-harness-dashboard.sh"
[ -x "$DASH" ] || DASH="bear-harness-dashboard"   # fallback to a PATH install
BEAR_HARNESS_DASHBOARD_PORT="${PORT:-8765}" "$DASH"
```

Then:
- Tell the user to open `http://127.0.0.1:<port>/` — it refreshes on its own every few seconds.
- Note it binds to loopback only (it speaks for their SSH credentials; never expose it).
- To watch a specific job's log live, the user pastes a `run_ref` into the log box on the page.
- If the launcher's first-run bootstrap fails (offline — no PyPI), say so plainly and fall back to
  the bundled bash harness (`bb-watch.sh`), which needs nothing but ssh; or run the repo's
  `./install.sh` once on a connected machine.
- Stop it by ending the background process when the user is done.
