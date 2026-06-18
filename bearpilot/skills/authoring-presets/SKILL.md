---
name: authoring-presets
description: Author a new bear-harness preset to teach the cluster a new workload type. Use when the user wants to add a new kind of workload (training, fine-tuning, sweeps/arrays, eval, a custom pipeline) to bear-harness, write or validate a preset, define a JobGraph, or understand the Preset/Backend contract and registry. This is the most advanced topic — it extends the harness rather than just using it.
---

# Authoring a bear-harness preset

A **preset** teaches bear-harness a new *workload type* by lowering a small declarative
manifest into a **JobGraph** that the preset-agnostic kernel already knows how to realise. The
keystone invariant: **the kernel honours the JobGraph contract; it does not know what a preset
computes.** Two presets, one kernel, zero kernel diff — that is the agnosticism the design is
built to prove. Read the `bear-harness` skill first.

> The *shape* below is stable; the exact symbols live in the repo and are the source of truth —
> read `src/bear_harness/_preset.py` (protocols + registry), `_reference_preset.py` (vLLM+pipeline),
> `_preset_etl.py` (the no-GPU de-risker), `_diffusion_preset.py` (a `VllmPipelinePreset`
> subclass), and `specs/01-foundational-contract.md` (the JobGraph contract).

## The JobGraph (what every preset must produce)

Jobs + edges + publish/consume + roles, expressing one of four topologies:

- **single** — one job (e.g. ETL: a CPU job, no server, no record, no edge).
- **bundle** — a SLURM array.
- **coupled** — a server publishes a record (its endpoint); a worker consumes it; the server is
  `role=sidecar`, `scancel`led once the worker finishes. (vLLM + pipeline.)
- **dag** — arbitrary `after`/`afterok` edges.

`Resources` is BlueBEAR-named and all-optional: `qos` / `walltime` / `gres` / `cpus_per_task` /
`mem_gb` / `array`. **No workload-named field crosses the wire** — the kernel sees jobs and
edges, never "the vLLM job" (roles, not types).

## The two protocols + the registry

```python
# Shape (consult the real code for exact signatures):
class Preset(Protocol):
    name: str
    def validate_manifest(self, manifest) -> None: ...      # reject bad input PRE-submit
    def lower(self, ctx: PresetContext) -> JobGraph: ...     # manifest → validated JobGraph

class Backend(Protocol):
    # how each job's sbatch is actually built (the thin, swappable submit layer)
    ...

register("my-workload", MyPreset())     # presets self-register
get("my-workload")                       # run_launch selects by the manifest's `preset` field
list()                                   # powers `bear-harness presets list`
```

The manifest carries a `preset` field (default `vllm-pipeline`, so existing manifests are
untouched) and an optional `[model]` section. A preset that needs no model (ETL) forbids it; one
that needs a GPU server (vLLM) requires it.

## The recipe — add a preset in five moves

1. **Define the workload manifest** — the smallest declarative surface a user authors (TOML).
   Decide what's required vs optional; ETL is the minimal example (single CPU job, no `[model]`).
2. **Implement `validate_manifest`** — reject bad input *before* anything is submitted (this is
   half of why presets exist: loud, early failure).
3. **Implement `lower(ctx) -> JobGraph`** — build the jobs, set `Resources`, wire edges and
   `publishes`/`consumes`, mark any server `role=sidecar`. Call `JobGraph.validate()`.
4. **Register it** — `register("my-workload", MyPreset())`; reference it via the manifest's
   `preset = "my-workload"`.
5. **Prove agnosticism** — an end-to-end test that the workload runs through the **unchanged**
   `run_launch` (kernel diff = 0). Mirror `TestEtlPreset`.

## Subclass before you build from scratch

If your workload is "vLLM serving + a worker, but tweaked" (a different image, different serve
flags), **subclass `VllmPipelinePreset`** rather than re-implementing. The diffusion preset does
exactly this: `serve_profile="diffusion"`, a separate `vllm-diffusion.sbatch.j2` (apptainer
**exec**, baked diffusion-sampler `--hf-overrides`), and a per-launch `--apptainer-image`
override — while the autoregressive `vllm.sbatch.j2` stays byte-identical. Note its hard-won
gotcha: the diffusion serve must **not** inherit AR-tuned global `extra_vllm_args` (a
`--gpu-memory-utilization 0.95` OOMs the diffusion buffers on a 40 GB card).

## Validate without submitting (the authoring kit)

```bash
bear-harness presets list                  # registered presets
bear-harness presets describe <name>       # its manifest shape
bear-harness presets validate <manifest>   # manifest → preset.validate_manifest → lower →
                                           # JobGraph.validate → guardrail cap-check, all PRE-submit
```

`validate` is your red-green loop for a new preset — it exercises the whole lowering + the
default-deny gate without touching SLURM. Then prove it for real on a **bbshort** run (small
model, seconds of GPU) before relying on it.

## Guardrails apply automatically

The resource gate derives its `ResourceRequest` from the **lowered graph**, so an ETL CPU job
is CPU-checked (not phantom-GPU-checked) and a vLLM job is GPU-checked — you get default-deny
enforcement for free, with no preset-specific guardrail code.
