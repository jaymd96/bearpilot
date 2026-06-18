# Internal docs — the design record

These notes are about **how bearpilot is designed and how to contribute to it** — not how to use
it. If you just want to run jobs on BlueBEAR, you're in the wrong place:

➡ **[Getting Started](../GETTING-STARTED.md)** · **[Configuration](../CONFIGURATION.md)** ·
**[Running on BlueBEAR](../bluebear.md)**

What lives here:

- **[PROJECT-VISION.md](PROJECT-VISION.md)** — what the `bear-harness` engine is (and deliberately
  isn't), and the loop it runs.
- **[decision-notes/](decision-notes/)** — the architecture decisions, each recorded with its
  rationale and the alternatives that were weighed and rejected.
- **[specs/](specs/)** — the formal **JobGraph contract** the engine honours. Read this if you're
  writing a new preset or changing the kernel.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to build, test, and land changes (Python 3.11+,
  Hatch, Ruff, the docs-drift gate).

The reference cribs the engine cites at runtime (SLURM / vLLM / Anthropic / BlueBEAR) live in
[`../../references/`](../../references/).
