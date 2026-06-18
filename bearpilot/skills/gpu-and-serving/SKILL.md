---
name: gpu-and-serving
description: Run GPU jobs and serve LLMs on BlueBEAR A100s. Use when the user wants to use a GPU, run CUDA/PyTorch work, serve a model with vLLM, run inference against a served endpoint, use Apptainer/Singularity containers, or fit a model on a 40 GB A100. Covers the proven apptainer-exec vLLM serve recipe (boot-wait loop, Bearer auth, /v1 routes), tensor parallelism, and the DiffusionGemma diffusion-LLM notes.
---

# GPU jobs & model serving

BlueBEAR's GPUs are **40 GB A100s** (`A100-SXM4-40GB`). Request them with
`--gres=gpu:a100:1` (or `:2`). Read `bluebear-basics` first; sizing and the exact strings are
in `${CLAUDE_PLUGIN_ROOT}/references/cluster-ground-truth.md`.

> **bbshort can request GPUs.** `--qos=bbshort --gres=gpu:a100:1` is valid — bbshort is *not*
> GPU-gated; its only limit is the 10-minute walltime. So debug GPU plumbing fast on bbshort,
> and move to `bbgpu` only when the real boot/run exceeds 10 minutes.

## A plain GPU job

```bash
H=${CLAUDE_PLUGIN_ROOT}/harness
$H/bb-new-job.sh gpu my-train            # qos bbgpu, --gres gpu:a100:1, CUDA module loaded
# edit the >>> YOUR COMMAND HERE <<< block, then:
$H/bb-submit.sh bb-jobs/my-train/my-train.sbatch --watch
```

Inside the job, confirm the card and use it directly or via a container:

```bash
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader   # → A100-SXM4-40GB, 40960 MiB
```

## Serving a model with vLLM (the proven recipe)

This recipe is battle-tested on this cluster. Scaffold it:

```bash
$H/bb-new-job.sh vllm serve-qwen          # qos bbgpu, gres gpu:a100:1, boot-wait loop included
# edit MODEL=... (and optionally the client command), then submit --watch
```

The non-obvious parts the template gets right (each has cost a real run — see
`${CLAUDE_PLUGIN_ROOT}/references/gotchas.md` item 8):

- **`apptainer exec`, NOT `apptainer run`.** `run` invokes the image runscript, which strips
  quotes off JSON args and breaks `--hf-overrides '{...}'`.
- **`--nv`** exposes the host GPU into the container; **`--bind "$HF_HOME:$HF_HOME"`** lets it
  read the weight cache. Omit either and it silently fails (no GPU / can't find weights).
- **Boot-wait loop:** poll `http://127.0.0.1:$PORT/v1/models` with
  `Authorization: Bearer $KEY` until it answers (or the server PID dies). vLLM routes are
  `/v1/...` (not `/v1/v1/...`) and auth is **Bearer**, not `x-api-key`.
- **Pick a random high port** per job (`8300 + RANDOM%90`) to avoid collisions on a shared node.

Skeleton (the template fills the cluster strings):

```bash
SIF=/rds/.../.bear-harness/apptainer/vllm-openai.sif
export HF_HOME=/rds/.../hf_cache
apptainer exec --nv --bind "$HF_HOME:$HF_HOME" --env HF_HOME="$HF_HOME" "$SIF" \
  vllm serve "$MODEL" --host 0.0.0.0 --port "$PORT" --api-key "$KEY" \
  --max-model-len 8192 --gpu-memory-utilization 0.85 --enforce-eager &
# ... boot-wait loop on /v1/models ... then your client ... then kill the server.
```

## Sizing for a 40 GB A100

- A 7B model (e.g. `Qwen/Qwen2.5-7B-Instruct`) fits comfortably at FP16.
- An INT8 ~26 GB checkpoint fits at ~35 GB used (leave headroom — `--gpu-memory-utilization`
  ≤ 0.85, lower for diffusion-sampler buffers).
- A 70B model needs **two** GPUs: `--gres=gpu:a100:2` **and** vLLM `--tensor-parallel-size 2`
  (the count must match — mismatch wastes a GPU or OOMs).
- No FP8 on Ampere; NVFP4 needs Blackwell. The vendor's headline throughput numbers are
  usually H100+FP8 — expect less on A100.

## DiffusionGemma (diffusion LLM) notes

If serving Google's DiffusionGemma (`aidendle94/diffusiongemma-26B-A4B-it-INT8-dynamic`):

- Needs a **newer vLLM image** than the default `.sif` (the diffusion build is
  `vllm-openai-gemma.sif`; the old `vllm-openai.sif` is too old). Pass it via the template's
  `SIF=` line.
- **Must** pass `--hf-overrides '{"diffusion_sampler":"entropy_bound","diffusion_entropy_bound":0.1}'`
  or it emits empty output. Also `--trust-remote-code --max-num-seqs 4`.
- It has a **~24 s one-time Triton-JIT cold start**; warm, it's ~1.2× faster per stream than
  AR Qwen-7B but the aggregate over a short run can be slower. Pre-warm if you measure
  throughput.
- Do **not** let it inherit AR-tuned `--gpu-memory-utilization 0.95` — that OOMs the diffusion
  sampler buffers on a 40 GB card; use ~0.75.

## Server + worker as one managed unit

When you want a server job to publish an endpoint that a *separate* worker job consumes (with
the server auto-cancelled when the worker finishes), that's a JobGraph **coupled topology** —
the job of **bear-harness**, not a hand-rolled sbatch. See the `bear-harness` skill.
