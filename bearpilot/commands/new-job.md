---
description: Scaffold a ready-to-edit BlueBEAR sbatch job from a proven template
argument-hint: "<cpu|gpu|vllm|array|python> <name> [--short] [--qos Q] [--walltime HH:MM:SS]"
allowed-tools: Bash
---

Scaffold a correct sbatch job for BlueBEAR from a template, filling in all cluster strings.

Arguments: `$ARGUMENTS` (kind = `$1`, name = `$2`).

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/harness/bb-new-job.sh" $ARGUMENTS
```

Then:
- Open the generated `bb-jobs/<name>/<name>.sbatch` and help the user fill in the
  `>>> YOUR COMMAND HERE <<<` block for their actual workload.
- Apply the relevant skill: `gpu-and-serving` for `gpu`/`vllm` kinds (sizing for a 40 GB A100,
  the apptainer-exec recipe), `batch-jobs` for `cpu`/`array`/`python`.
- Remind them that artifacts must be written to the RDS `$RUN` dir, not node-local `/tmp`.
- Suggest the next step: `/bearpilot:launch bb-jobs/<name>/<name>.sbatch`.

If the user did not pass a kind and name, ask which of `cpu | gpu | vllm | array | python` they
want and a short job name.
