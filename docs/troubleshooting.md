# Troubleshooting

Keyed to the failure symptom the operator actually sees. Each entry
lists the most likely root cause and the next diagnostic step.

---

## `bear-harness launch` hangs waiting for the endpoint file

**What it looks like**: `[status]` lines never appear. `squeue -u $USER`
shows the vLLM job in `R` (running), but `cat
$RDS_ROOT/.bear-harness/endpoints/*.json` returns nothing.

**Likely causes**:

1. vLLM is still loading the model weights. A 70B model can take 5–10
   minutes to load on two A100_80s — check `vllm-<jid>.out`:

   ```bash
   tail -f $RDS_ROOT/.bear-harness/runs/$JOB_ID/vllm-*.out
   ```

   Look for `Loading model weights` progress.

2. Compute node cannot reach Hugging Face. The vLLM log will show
   a hanging download or `Connection timed out`. The bootstrap probe
   only checked the login node — compute nodes can be firewalled
   differently. Workaround: pre-download the weights on the login
   node:

   ```bash
   HF_HOME=$RDS_ROOT/hf_cache python -c \
       "from huggingface_hub import snapshot_download; \
        snapshot_download('Qwen/Qwen2.5-7B-Instruct')"
   ```

3. The harness's `--boot-timeout` is shorter than the model's load
   time. Re-launch with `--boot-timeout 1800` (30 min) for large
   models.

---

## vLLM job fails immediately with `OutOfMemoryError`

**What it looks like**: vllm-<jid>.out ends with a CUDA OOM traceback
within 30s of start. `sacct -j <jid>` reports `OUT_OF_MEMORY`.

**Likely causes**:

1. Model doesn't fit on the selected GPU. A 7B model in FP16 needs
   ~14GB of VRAM plus KV cache headroom. On a 40GB A100 this is fine;
   on a shared 40GB A100 with `--gpu-memory-utilization` > 0.9 it may
   not be.

2. Context window too large. Add `max_model_len = 8192` to `[slurm]`
   in `bear.toml`.

3. Wrong `gpu_gres`. For 70B models you need
   `gpu_gres = "gpu:a100_80:2"` plus `tensor_parallel_size = 2`.

---

## Pipeline job starts but fails with `Connection refused`

**What it looks like**: `pipeline-<jid>.out` shows the entrypoint
exec'ing, then a `httpx.ConnectError: [Errno 111] Connection refused`.

**Likely causes**:

1. The pipeline started before vLLM was ready. This should not happen
   with `--dependency=after:$JID` plus the endpoint-file wait in the
   wrapper — but if vLLM died between endpoint-write and the first
   request, the stale file is still on disk. Check vLLM's log: did it
   exit after publishing the endpoint?

2. Wrong hostname in the endpoint file. BlueBEAR nodes advertise their
   hostname via `hostname -f`; if that resolves differently from the
   pipeline node's routing tables, the URL in the endpoint file may
   not be reachable. Inspect:

   ```bash
   cat $RDS_ROOT/.bear-harness/endpoints/<vllm_jid>.json
   curl -s http://<host>:<port>/v1/models -H "Authorization: Bearer <key>"
   ```

---

## `bear-harness launch` reports `vllm probe failed` with `does not list`

**What it looks like**:

```
vllm probe failed: endpoint does not list expected model 'Qwen/Qwen2.5-7B-Instruct'
```

**Cause**: `--served-model-name` and `--model` disagree. In SLURM mode
the wrapper derives both from the harness's `model` argument, so this
only happens if someone has edited the sbatch script by hand. Fix:
re-render by re-running `bear-harness launch`.

---

## `vllm probe failed: POST /v1/messages returned 404`

**What it looks like**:

```
vllm probe failed: POST /v1/messages returned 404 — upgrade vllm to a version that natively supports /v1/messages (>=0.11 or post PR #22627/#27882)
```

**Cause**: the vLLM apptainer image is older than late-2025 and does
not implement the Anthropic Messages API route. Re-pull:

```bash
rm $RDS_ROOT/.bear-harness/apptainer/vllm-openai.sif
bear-harness bootstrap --rds-root $RDS_ROOT --account <acc>
```

Or override the image tag: `--apptainer-image docker://vllm/vllm-openai:v0.11.0`.

---

## Pipeline sbatch stays `PD` forever with reason `Dependency`

**What it looks like**:

```
$ squeue -u $USER
JOBID  ST  REASON       NODELIST
111    R   None         gpu-01
222    PD  Dependency   (null)
```

**Cause**: the pipeline job is blocked on the vLLM job's state. Since
we use `--dependency=after:` (not `afterok`), this should release as
soon as vLLM enters `R` (running). If vLLM is still `PD`, the
dependency is correct — wait for scheduler.

If vLLM is `R` and the pipeline is still `PD Dependency`, check:

```bash
scontrol show job 222 | grep Dependency
```

If the dependency is `afterok` not `after`, someone modified the
sbatch template. Reset by re-running `bear-harness launch`.

---

## `scancel` did not kill vLLM

**What it looks like**: `bear-harness cancel` or manual `scancel`
exited cleanly, but the vLLM process continues consuming GPU.

**Cause**: SLURM sent `SIGTERM` but the wrapper's trap didn't propagate
it fast enough. Escalate:

```bash
scancel --signal=KILL <vllm_jid>
```

If the process still exists, it's running inside the apptainer
container and needs a `singularity exec ... kill -9`. Contact
BlueBEAR support.

---

## `bear-harness bootstrap` fails with `apptainer pull failed`

**Most common cause**: not enough quota in `$RDS_ROOT/.bear-harness/apptainer`.
The vllm-openai image is ~6GB. Check quota:

```bash
quota -s -u $USER
```

Next most common: the docker reference is invalid. The default is
`docker://vllm/vllm-openai:latest` — if that moves to a new tag layout,
pass an explicit tag:

```bash
bear-harness bootstrap ... --apptainer-image docker://vllm/vllm-openai:v0.11.0
```

---

## `module load CUDA/...` fails on the compute node

**What it looks like**: `vllm-<jid>.out` starts with:

```
Lmod has detected the following error: The following module(s) are unknown: "CUDA/12.1.1"
```

**Cause**: the module name in `bear.toml` doesn't match what the
compute node actually has. CUDA modules on BlueBEAR rotate. On a login
node, check:

```bash
module avail CUDA 2>&1 | grep -v '^$'
```

Pick a concrete version that is marked `(D)` (default) or known-good,
and set `cuda_module` in `bear.toml` accordingly.
