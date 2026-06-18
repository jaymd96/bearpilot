# BlueBEAR cluster ground-truth

> **Canonical source:** <https://docs.bear.bham.ac.uk/> — the University of Birmingham
> BEAR/BlueBEAR official HPC documentation.
> **Snapshot pin:** the live cluster as of **2026-06**, probed from a login node and from
> working jobs (≈18 completed runs + live `bbshort` canaries on 2026-06-11 / 06-14 / 06-18).
> **Why this file exists:** cluster strings (QoS names, GPU GRES, CUDA module versions, RDS
> paths) **rotate**. Cite this file instead of recalling values from training — a wrong GRES
> string or QoS tier fails the job at *submit*; a wrong CUDA module fails it on the *compute
> node*. When a value here looks stale, re-probe on a login node (commands below) before
> trusting it.

These values back the bundled harness (`harness/lib/common.sh`) and every skill. Override any
of them with the matching `BB_*` environment variable — see `harness/lib/common.sh`.

---

## Identity & access

| Fact | Value | Notes |
|---|---|---|
| SSH target | `your-username@bluebear.bham.ac.uk` | The cluster user is **`your-username`**, NOT your local mac user. `ssh bluebear` defaults to `your-laptop-user@` and fails with *Permission denied*. Always specify `your-username@`. |
| Auth | SSH **public key**, non-interactive | `ssh -o BatchMode=yes your-username@bluebear.bham.ac.uk 'echo ok'` succeeds with no password/2FA for an authorised key. Kerberos/password are also offered but not needed. |
| Off-campus | University **VPN** required | Without VPN, `bluebear.bham.ac.uk:22` is unreachable. |
| Landing node | `bear-pg-loginNN` (round-robin) | You may land on a *different* login node each connection — this is why node-local state lies (see Gotchas). |
| `$HOME` | `/rds/homes/u/your-username` | |

## SLURM account & QoS (`--account` / `--qos`)

| Fact | Value | Notes |
|---|---|---|
| Account | `your-project` | The `--account` value. Confirm yours: `sacctmgr -nP show assoc user=$USER format=account,qos`. |
| `bbshort` | MaxWall **00:10:00** | Fast-track; spans **all** node types **including GPUs**. The iteration-loop workhorse (~5-min debug cycles). Jobs start in 0–60 s. |
| `bbgpu` | MaxWall **2-00:00:00** | Standard GPU QoS — the real vLLM serving job runs here. |
| `bbdefault` | MaxWall **10-00:00:00** | Standard CPU QoS — valid for pipeline/worker/ETL jobs. |
| also present | `bbondemand`, `bbportalgpu` | On this account (probed 2026-06-14). |
| ⚠ **NOT present** | `bbcpu` | `bbcpu` is **absent** on `your-project`. Never hardcode it — a CPU job that asks for `bbcpu` is rejected at submit. Use `bbdefault` (or `bbshort`). |

## GPU resources (`--gres=gpu:<type>:<count>`)

| Fact | Value | Notes |
|---|---|---|
| GRES type | **`a100`** | The real GRES name is `a100`. `a100_40` / `a100_80` are **NOT valid** here (older docs cite them — stale). |
| A100 memory | **40 GB** (`A100-SXM4-40GB`) | These are 40 GB cards. No FP8 (Ampere); NVFP4 needs Blackwell. Size models accordingly. |
| One GPU | `--gres=gpu:a100:1` | |
| Two GPUs | `--gres=gpu:a100:2` | Pair with vLLM `--tensor-parallel-size 2` for larger models. |

## Modules (Lmod toolchain `bear-apps/2024a`)

```text
# Python 3.12 toolchain (one module line — load all four together):
module load bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0 CUDA/12.6.0

# Pieces, if you need them à la carte:
PYTHON: bear-apps/2024a/live GCCcore/13.3.0 Python/3.12.3-GCCcore-13.3.0
CUDA:   CUDA/12.6.0          # ⚠ VERSIONS ROTATE — confirm with `module avail CUDA`, prefer the (D) default
APPTAINER: module load Apptainer   # the container runtime (needed on the login node to pull a .sif)
```

⚠ **CUDA module names rotate.** A `CUDA/12.6.0` that works this month can fail on a compute
node next month with `Lmod ... module(s) are unknown`. Always re-confirm: `module avail CUDA`.
(Older notes cite `CUDA/12.1.1` — that is **stale**; the live value is `CUDA/12.6.0`.)

## Storage (RDS — Research Data Store)

| Fact | Value | Notes |
|---|---|---|
| `RDS_ROOT` | `/rds/projects/p/your-project` | Durable shared filesystem. The source of truth for all run state. |
| Layout (already provisioned) | `$RDS_ROOT/.bear-harness/{runs,endpoints,logs,apptainer,wheels,venvs}` and `$RDS_ROOT/hf_cache` | Created by prior sessions; `hf_cache` is the Hugging Face weight cache (`export HF_HOME=$RDS_ROOT/hf_cache`). |
| Pre-built image | `$RDS_ROOT/.bear-harness/apptainer/vllm-openai.sif` (≈7.8 GB) | Standard vLLM image. A diffusion-capable build also exists: `vllm-openai-gemma.sif`. |
| Node-local scratch | `/scratch` | Fast, per-node, **EPHEMERAL**. Good for transient compute; **never** the source of truth — it does not survive the node or a reattach. |
| Quota | finite (~80 GB headroom for weights + `.sif`) | `apptainer pull` fails on quota exhaustion. |

## Container runtime (Apptainer)

```bash
module load Apptainer
# Pull (do this AS A JOB or accept it on the login node only if small — heavy pulls go via sbatch):
apptainer pull <sif> docker://vllm/vllm-openai:<tag>
# Exec (NOT run — `run` strips JSON arg quotes, which breaks --hf-overrides):
apptainer exec --nv \                         # --nv exposes host NVIDIA GPUs into the container
  --bind "$HF_HOME:$HF_HOME" \                 # --bind mounts RDS paths so the container sees hf_cache / runs
  --env HF_HOME="$HF_HOME" \
  "$SIF" vllm serve <model> ...
```

## What is NOT available

| Fact | Consequence |
|---|---|
| **No `slurmrestd` / no SLURM REST endpoint** | `which slurmrestd` and `scontrol token` both fail (no `auth/jwt`). Drive SLURM **only** via its CLI over SSH (`sbatch`/`squeue`/`sacct`/`scancel`). Do not reach for a REST client. |
| **Login-node heavy compute** | Forbidden by policy and a per-user limiter. Image builds, weight downloads at scale, model serving → all go via `sbatch`. The login node only submits and watches. |

---

## Re-probe commands (run on a login node when a value looks stale)

```bash
sacctmgr -nP show assoc user=$USER format=account,qos   # your account + QoS allowlist
sinfo -o "%P %G %D %t"                                   # partitions, GRES, node counts/state
scontrol show config | grep -i maxtime                  # cluster time defaults
module avail CUDA                                        # live CUDA modules (pick the (D) default)
sacctmgr show qos format=name,maxwall%15                # QoS walltime ceilings
nvidia-smi --query-gpu=name,memory.total --format=csv   # (inside a GPU job) the actual card
```

---

*Snapshot drafted against the live BlueBEAR cluster (2026-06) from <https://docs.bear.bham.ac.uk/>,
cross-checked against working jobs and `bear-harness/references/bluebear-platform.md`. **Refresh on:**
any QoS/partition rename; a GPU-fleet change (new GRES, A100→H100); a CUDA-module rotation that
breaks a job; an RDS path-layout change; the next BlueBEAR maintenance/upgrade announcement.*
