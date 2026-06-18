---
description: Show what's running (and what just ran) on BlueBEAR
argument-hint: "[--all] [--since YYYY-MM-DD]"
allowed-tools: Bash
---

Report the user's BlueBEAR jobs from the authoritative, node-agnostic sources (live `squeue`,
`sacct` history, and the RDS run dirs) — never PIDs.

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/harness/bb-jobs.sh" $ARGUMENTS
```

Then summarise for the user:
- What is **running** vs **pending** (a `PD` job with reason `Dependency` is waiting on its
  upstream — that is normal, not stuck).
- Anything that **failed** today (with its `State`/`ExitCode` from `sacct`) and, applying the
  `observability` skill, the likely cause + next step.
- Offer to follow a specific job with `/bearpilot:status <job_id>` or fetch a
  finished job's outputs.
