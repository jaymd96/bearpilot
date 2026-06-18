# Running bear-harness on BlueBEAR

This document is the manual first-run checklist for the University of
Birmingham's BlueBEAR HPC cluster. It assumes you already have:

- A BlueBEAR account and a project code (the SLURM `--account` value).
- SSH access from your laptop to a BlueBEAR login node.
- Quota for at least ~80GB in your project's RDS area (to cache model
  weights and the vLLM apptainer image).

bear-harness provisions a vLLM server on a GPU node, waits for it to
listen on `/v1/messages`, then runs your pipeline program against it
from a CPU node with `--dependency=after:$VLLM_JOB`. Every external
action goes through SLURM — no long-running login-node processes, no
SSH tunnels, no port forwarding.

---

## 1. One-time bootstrap

On a login node:

```bash
module load Apptainer
pip install --user bear-harness

bear-harness bootstrap \
    --rds-root /rds/projects/<initial>/<project-code> \
    --account <your-project-code> \
    --cuda-module CUDA/12.1.1 \
    --gpu-gres gpu:a100_40:1
```

Bootstrap will:

1. Verify `apptainer` is on `PATH`.
2. Create `$RDS_ROOT/.bear-harness/{endpoints,runs,logs,apptainer}` and
   `$RDS_ROOT/hf_cache`.
3. Pull `vllm/vllm-openai:latest` into
   `$RDS_ROOT/.bear-harness/apptainer/vllm-openai.sif`.
4. Probe `https://huggingface.co` from the login node. If the probe
   fails, bootstrap warns but does not abort — compute nodes may still
   be able to reach HF even when login nodes cannot.
5. Show you the CUDA modules available.
6. Write `~/.config/bear-harness/bear.toml` with the values you passed.
7. Print a manual checklist of things that cannot be auto-verified.

After bootstrap, **inspect `~/.config/bear-harness/bear.toml`** and
edit as needed:

- `gpu_gres` — upgrade to `gpu:a100_80:1` for 40GB+ models, or
  `gpu:a100_80:2` with `tensor_parallel_size = 2` for 70B models.
- `mem_gb` — ~2x the model's parameter count in GB, plus headroom.
- `walltime` — the job wall-time limit.
- `max_model_len` — if your model's default context window is too
  generous for the available GPU memory, clamp it here.

---

## 2. Validate a program

Before submitting a real job, validate that your program's
`pipeline.toml` parses cleanly:

```bash
bear-harness validate path/to/your/pipeline
```

You should see a Rich table with the resolved manifest. If this fails,
fix the manifest before proceeding.

---

## 3. Dry-run a launch

```bash
bear-harness launch path/to/your/pipeline --dry-run
```

This writes `run.json` and exits without submitting sbatch. Inspect:

```bash
ls $RDS_ROOT/.bear-harness/runs/
```

There is a new run directory. Look inside the next step's output
(`vllm.sbatch` and `pipeline.sbatch` once you actually launch) and
eyeball them before a real submission.

---

## 4. Real launch — the 5-step progression

Do these in order. Each step is a pass/fail gate for the next. If
step 3 works you probably have a working stack; steps 4 and 5 stress
larger models.

### Step 1 — smoke (135M model)

```bash
bear-harness launch tests/fixtures/fake_program \
    --model HuggingFaceTB/SmolLM2-135M-Instruct \
    --boot-timeout 600
```

Expected: endpoint file appears within ~2 min, the fake program runs
5 messages calls, artifacts tarball is written.

### Step 2 — 1.7B model, real fake program

```bash
bear-harness launch tests/fixtures/fake_program \
    --model HuggingFaceTB/SmolLM2-1.7B-Instruct \
    --boot-timeout 900
```

### Step 3 — 7B model, DemoPipeline

```bash
bear-harness launch path/to/your/pipeline \
    --model Qwen/Qwen2.5-7B-Instruct \
    --boot-timeout 900
```

Expected on a40_40: vLLM boot ~3 min, the first pipeline batch runs
within ~5 min of submission, DemoPipeline's status file transitions
`running → completed`, artifacts tarball contains `campaign.db`,
`state.json`, nonempty logs.

### Step 4 — 70B model

Edit `bear.toml`:

```toml
gpu_gres             = "gpu:a100_80:2"
mem_gb               = 160
tensor_parallel_size = 2
```

Then:

```bash
bear-harness launch path/to/your/pipeline \
    --model meta-llama/Llama-3.3-70B-Instruct \
    --boot-timeout 1800
```

This is the demanding case. If it works, the stack is production-ready.

### Step 5 — repeat with a different program

Prove the harness is not DemoPipeline-shaped:

```bash
bear-harness launch path/to/example/summarize-dir \
    --model Qwen/Qwen2.5-7B-Instruct
```

No harness changes should be required between step 3 and step 5.

---

## 5. Monitoring a live run

```bash
bear-harness list
bear-harness status <runs_dir>/<job_id>
bear-harness logs <runs_dir>/<job_id> --which vllm -n 200
bear-harness logs <runs_dir>/<job_id> --which pipeline -n 200
```

`squeue -u $USER` will show both the `vllm-<jobid>` and
`pipeline-<jobid>` jobs. The pipeline job sits `PD` (pending) with
reason `Dependency` until vLLM transitions to `R`.

---

## 6. Cancelling

```bash
bear-harness cancel <runs_dir>/<job_id>
```

Or for SLURM mode:

```bash
scancel <vllm_job_id> <pipeline_job_id>
```

The vLLM sbatch wrapper traps SIGTERM and removes its endpoint file on
cleanup, so a cancelled job does not leave stale `endpoints/*.json`
that could confuse a retry.

---

## 7. Common first-run failures

See [`troubleshooting.md`](troubleshooting.md).
