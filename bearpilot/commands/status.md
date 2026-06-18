---
description: Follow a specific BlueBEAR job to completion (state changes + live log)
argument-hint: "<job_id> [--interval SECONDS]"
allowed-tools: Bash
---

Follow a single BlueBEAR job, keyed on durable signals (`sacct` state + the shared-FS `.out`
log) so it survives a reconnect to a different login node.

Argument: the SLURM job id (`$1`).

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/harness/bb-watch.sh" $ARGUMENTS
```

Then:
- Report the terminal state. Exit 0 means `COMPLETED`; non-zero means a failure state — name
  which (`FAILED` / `TIMEOUT` / `OUT_OF_MEMORY` / `CANCELLED` / …).
- On failure, apply the `observability` skill: read the tail of the `.out` log and the `sacct`
  `ExitCode`/`MaxRSS`, give the most likely cause, and propose the single-variable fix.
- On success, offer to fetch outputs with `${CLAUDE_PLUGIN_ROOT}/harness/bb-fetch.sh --id $1`.

If no job id was given, run `/bearpilot:jobs` first to find the id.
