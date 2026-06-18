---
description: Submit a BlueBEAR sbatch job and follow it to completion
argument-hint: "<path/to/job.sbatch> [--no-sync]"
allowed-tools: Bash
---

Push a job to BlueBEAR, submit it, and watch it to a terminal state.

Argument: the path to the `.sbatch` file (`$1`).

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/harness/bb-submit.sh" $ARGUMENTS --watch
```

Then:
- The watcher prints one line per state change and tails the job's `.out` log; it exits 0 on
  `COMPLETED`, non-zero on failure. Report the outcome plainly.
- If it fails, apply the `observability` skill: read the `.out` log and the `sacct` exit
  code/`MaxRSS`, identify the failure signature (bad QoS/GRES rejected at submit; rotated CUDA
  module; OOM; TIMEOUT; HF unreachable), and propose the one-variable fix — then relaunch on
  `bbshort` (the iteration loop), don't speculatively rewrite.
- On success, offer to fetch the outputs: `${CLAUDE_PLUGIN_ROOT}/harness/bb-fetch.sh <name>`.

Before relying on any SLURM/vLLM/GRES behaviour for a real campaign, confirm it on a real
`bbshort` run first.
