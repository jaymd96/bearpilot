# bear-harness external-reference library — index

<!-- LIVING. The discovery surface for the crib library. The agent reads this FIRST,
     picks the crib for the surface it is about to drive, and cites it by relative path.
     Individual cribs are SNAPSHOT-with-provenance; this index promises to be current. -->

> Every external surface bear-harness (or its agent) drives gets exactly ONE crib here.
> Cite cribs by relative path, never by re-pasting the URL or re-typing a flag. Snapshots are
> allowed to age — each carries its provenance so staleness is detectable.
>
> **Enforced by:** `checks/check-drift.sh` (the references-provenance lint) — asserts every crib
> has a canonical-source header + version pin + a footer refresh-trigger, and that this index lists
> every crib (no orphan cribs, no rows pointing at a missing file).

| # | Crib | External surface | Canonical source (host) | Pinned version | USED? | One-line role (which module it feeds) | Last refreshed |
|---|---|---|---|---|---|---|---|
| 1 | [`slurm-cli.md`](slurm-cli.md) | SLURM CLI (`sbatch`/`squeue`/`sacct`/`scancel`) | slurm.schedmd.com | Slurm 23.x on BlueBEAR | [USED — PRIMARY] | the only SLURM grammar the kernel knows → `src/bear_harness/_slurm_runner.py` | 2026-06-14 |
| 2 | [`vllm-serve-api.md`](vllm-serve-api.md) | vLLM `serve` — OpenAI/Anthropic HTTP API + `serve` CLI | docs.vllm.ai | `vllm/vllm-openai` >=0.11.0 | [USED — PRIMARY] | reference-preset server: argv + readiness probe → `src/bear_harness/_vllm_launcher.py`, `src/bear_harness/_endpoint_discovery.py` | 2026-06-14 |
| 3 | [`anthropic-messages-api.md`](anthropic-messages-api.md) | Anthropic Messages API + Python SDK (`messages.create`, `base_url`, `default_headers`) | docs.anthropic.com | Messages API stable / `anthropic` SDK 0.x | [USED — PRIMARY] | worker dialect: Anthropic⇄OpenAI shim + endpoint wiring → `src/bear_harness/_messages_shim.py` | 2026-06-14 |
| 4 | [`bluebear-platform.md`](bluebear-platform.md) | BlueBEAR platform (QoS, GPU GRES, RDS, modules, Apptainer, /scratch) | docs.bear.bham.ac.uk | live BlueBEAR cluster, 2026-06 | [USED — PRIMARY] | the only cluster this targets: platform strings + RDS layout → `src/bear_harness/_bear_config.py`, `src/bear_harness/_bootstrap.py` | 2026-06-14 |

<!--
  Tags:
    [USED — PRIMARY] / [USED]  the harness depends on this surface; the crib is load-bearing.
    [CANDIDATE]                captured but not yet wired into the build.
  An observed-but-rejected surface is NOT listed here — it is cited at its call site as
  "cited not adopted".
-->

<!-- when to expand me: this index needs no extra structure until the library has several cribs.
     If two cribs ever describe the SAME surface, that is a bug — merge them (one surface, one crib).
     Split a crib only by DISTINCT decision surface (a tool's CLI vs its HTTP API). -->
