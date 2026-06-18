# BlueBEAR-only: portability is a non-goal, because the contract abstracts workloads, not schedulers

<!-- SNAPSHOT. The filename IS the citation id -- name it for the THESIS, not a number.
     Dated, append-only. Once written, this note is never rewritten to "stay current" --
     a new note supersedes it and links back. Drift is irrelevant; it records a date. -->

**Status:** adopted·live
**Owner:** jamesamd · **Decided:** 2026-06-14
**Applies to:** the entire scope fence — every runner, every crib, every guardrail assumes one cluster
**Drives:** [`PROJECT-VISION.md`](../PROJECT-VISION.md) (scope fence), `ROADMAP.md` (hard non-goals), the SLURM crib [`references/slurm-cli.md`](../../../references/slurm-cli.md), the platform crib [`references/bluebear-platform.md`](../../../references/bluebear-platform.md)

---
## Decision

bear-harness targets the University of Birmingham's BlueBEAR SLURM cluster, and only BlueBEAR. We optimise hard for one machine: its QoS tiers (`bbshort` / `bbgpu` / `bbcpu`), its GPU GRES names (`a100_40`, `a100_80`), its RDS storage layout, its module system, its Apptainer setup. Portability — multi-cluster, scheduler-agnostic execution — is an explicit non-goal, not an unbuilt feature.

**Abstract the WORKLOAD, never the SCHEDULER** unless a second cluster becomes a real requirement for the user's own research group — at which point a backend is added deliberately, behind the existing JobGraph contract, not retrofitted under pressure.

## Why

- **No scheduler-abstraction tax** — every line of the runner can name `sbatch`, `squeue`, `sacct`, `--gres`, `--qos` directly (see [`references/slurm-cli.md`](../../../references/slurm-cli.md)). We never pay for an indirection layer that buys portability nobody asked for. The crib captures the *real* BlueBEAR surface, not a lowest-common-denominator one.
- **The contract already draws the right line** — the [keystone decision](first-decision.md) makes the kernel abstract WORKLOADS via the JobGraph. Schedulers sit *below* that line on purpose. A workload (vLLM+pipeline, ETL) is portable across presets; the scheduler is not meant to be portable across clusters, because the whole value is exploiting one cluster well.
- **It's a lab tool, not a product** — the audience is a small research group on the one cluster they all have accounts on. "Runs anywhere" would be effort spent on users who don't exist. See the IS / IS-NOT fence in [`PROJECT-VISION.md`](../PROJECT-VISION.md).
- **Cluster specifics are where the bugs live** — apptainer flags, CUDA module names, QoS walltime ceilings, `/rds` paths, HF-Hub reachability from compute nodes. A portable abstraction would have to *hide* exactly the details we most need to get right. Better to encode them once, accurately, in [`references/bluebear-platform.md`](../../../references/bluebear-platform.md).

## The tradeoff (read before relying on it)

If the user moves institutions, or a collaborator on a different cluster wants to run the same campaigns, none of the runner transfers as-is — paths, partition names, GRES strings, and module loads are all BlueBEAR-shaped and would need a parallel backend. We are knowingly betting that this won't happen within V1's horizon, and that if it does, the JobGraph contract is the seam that makes a second backend *additive* rather than a rewrite.

Escalate / reconsider when a second cluster becomes a standing requirement for the user's group (not a hypothetical), or when the user's BlueBEAR access ends.

## Alternatives considered (steelmanned)

<!-- This folds in the "why-not" register. Each: the proposal, why it was genuinely
     tempting (steelman it -- no straw men), why we didn't, and the FALSIFIABLE condition
     that would change our minds. -->

- ***Scheduler-agnostic from day one — a `Backend` abstraction over SLURM / PBS / LSF / k8s.*** Tempting because the platform-design instinct says build the seam while it's cheap, and "what if a collaborator is on a different cluster" feels prudent. Rejected because there is exactly one consumer and one cluster, so the abstraction would be designed against a single example — the worst possible condition for getting an abstraction right — and every cribbed flag would have to be softened to a portable subset. The JobGraph already gives us the *one* seam worth having (workload portability); a second seam (scheduler portability) is speculative generality. Would reconsider the moment a real second cluster appears, and the contract is deliberately positioned so that backend can be slotted in behind it.
- ***Target a generic "SLURM" rather than BlueBEAR specifically.*** Tempting because SLURM is SLURM and it sounds only marginally less portable. Rejected because the QoS tiers, GRES names, RDS paths, and module system are site policy, not SLURM — the parts that actually break on first run are precisely the BlueBEAR-specific ones (see the first-run failure modes in [`bluebear.md`](../../bluebear.md)). Pretending to target generic SLURM would lose the very details the cribs exist to pin. Would reconsider if the group standardised on a second SLURM site with compatible policy — even then we'd capture that site as its own crib, not blur the two.

## How it's wired

Cluster facts are pinned in two cribs the agent must cite rather than recall: [`references/slurm-cli.md`](../../../references/slurm-cli.md) (the `sbatch`/`squeue`/`sacct`/`scancel` surface) and [`references/bluebear-platform.md`](../../../references/bluebear-platform.md) (QoS tiers, GPU GRES, RDS, modules, Apptainer). The canonical upstream source is the BlueBEAR official documentation, <https://docs.bear.bham.ac.uk/>, referenced from the ops walkthrough [`bluebear.md`](../../bluebear.md). The runner that bakes these in is `src/bear_harness/_slurm_runner.py` plus the sbatch templates under `src/bear_harness/templates/`.

Verify: every scheduler-touching string in the runner should resolve to a BlueBEAR fact captured in a crib —
```bash
grep -rIn -E "sbatch|squeue|sacct|--gres|--qos|a100_40|a100_80|/rds" src/bear_harness/
```
No portable-backend abstraction layer should appear between this code and `sbatch`.

## Reversibility

high — a second backend could be added later behind the JobGraph contract without disturbing existing presets. This is a *not now* decision, not a *not ever* architectural lock. What would be expensive is having built the abstraction prematurely; deferring it costs almost nothing.

## Reversal path (if it comes to that)

To add a second cluster: introduce a `Backend` seam at the realiser boundary (where `_slurm_runner.py` meets the JobGraph), implement the new site as a sibling runner with its own crib under `references/`, and gate selection on a host/config value. The JobGraph contract, the presets, and all filesystem-attached state (`run.json`, status file, artifacts) are untouched — they sit above the scheduler line by design. The load-bearing risk on the way out is the cribs silently encoding BlueBEAR assumptions that a second site violates; budget time to audit each cribbed flag against the new site's policy before trusting it.

---
## Decision history

<!-- Append-only, newest first; one dated ISO line per change.
     SNAPSHOT discipline: never rewrite a line above; only append here. -->

- **2026-06-14** — Note authored. Records BlueBEAR-only as a deliberate scope fence (portability = explicit non-goal), justified by the workload-not-scheduler line drawn in [`first-decision.md`](first-decision.md). Verified against the scope fence in [`PROJECT-VISION.md`](../PROJECT-VISION.md) and the cluster-specific surface in `src/bear_harness/_slurm_runner.py`.

<!-- when to expand me: if rejected alternatives across many notes start getting re-litigated,
     hoist them into a standalone docs/why-not.md keyed by proposal name. Until then, the
     steelmanned-alternatives section above is enough -- keep the why-not WITH the decision. -->
