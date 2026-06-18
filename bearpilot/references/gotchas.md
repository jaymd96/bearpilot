# BlueBEAR gotchas — the things that bite first

Each of these has cost a real run. They are ordered roughly by how early you'll hit them.

## 1. Login nodes are orchestration-only

Image builds, weight downloads at scale, compiles, and model serving go through `sbatch`
(`bbshort` for short jobs). The login node only *submits and watches*. A per-user limiter
kills heavy login-node processes, and node `/tmp` is too small for a `.sif` build (use
node-local `/scratch` inside the job instead). **This is the first invariant an agent
violates.** If you catch yourself running `apptainer pull`, a big `pip install` of compiled
wheels, or `vllm serve` directly over SSH — stop, and wrap it in an sbatch job.

## 2. Login nodes are round-robin → node-local state lies

You may land on a different login node every connection. So **PIDs, `/tmp`, and `nohup` do
not survive a reconnect.** A watcher that polls a PID will lie to you. Two consequences:

- **Pin SSH to one node** for the duration of a watch — open a ControlMaster (the bundled
  `harness/bb-connect.sh` does this) so every subsequent `ssh` reuses that one connection.
- **Key every watcher on durable artifacts**, never on a process: the shared-FS run files
  under `$RDS_ROOT/.bear-harness/runs/<id>/` plus `sacct`/`squeue`. See the *observability*
  skill.

## 3. The cluster user is `your-username`, not your local user

`ssh bluebear` resolves to `your-laptop-user@…` and fails. Always `your-username@bluebear.bham.ac.uk`.
Off-campus needs the University VPN.

## 4. `bbcpu` does not exist on this account

A CPU job that requests `--qos=bbcpu` is rejected at submit. The valid CPU QoS is
`bbdefault` (or `bbshort` for short jobs). Confirm with `sacctmgr -nP show assoc user=$USER`.

## 5. GRES is `gpu:a100:N`, not `a100_40`/`a100_80`

Older docs cite `a100_40` / `a100_80`. Those are **invalid** here and fail at submit. The
real type is `a100`, and the cards are **40 GB**. Size models to fit 40 GB (≈26 GB INT8
checkpoint leaves headroom; a 70B needs `gpu:a100:2` + `--tensor-parallel-size 2`).

## 6. CUDA module names rotate

`module load CUDA/12.6.0` works today; a stale value fails on the compute node with
`Lmod ... module(s) are unknown`. Re-confirm with `module avail CUDA` and prefer the `(D)`
default. The job's *preamble* is where this bites — it loads fine on the login node where you
tested interactively but fails on the GPU node hours later.

## 7. `bbshort` is ~10 minutes — but it CAN request GPUs

`bbshort` is **not** GPU-gated: `--qos=bbshort --gres=gpu:a100:1` is valid. Its only limit is
the **10-minute walltime**. So the bbshort-vs-bbgpu choice is *always* a walltime call, never
a GPU-access one. Use `bbshort` to debug plumbing fast; move to `bbgpu` only when the real
work (e.g. a large-model boot) exceeds 10 minutes.

## 8. vLLM: `apptainer exec`, not `run`; routes are `/v1/...`; auth is Bearer

- Use `apptainer exec` — `apptainer run` invokes the image runscript, which **strips quotes
  off JSON args** and breaks `--hf-overrides '{"...":...}'`.
- vLLM serves at `/v1/models`, `/v1/chat/completions`, `/v1/messages` — **not** `/v1/v1/...`.
- vLLM's `--api-key` expects `Authorization: Bearer <key>`, **not** `x-api-key`.
- Forgetting `--nv` → the container sees no GPU. Forgetting `--bind` → it can't read
  `hf_cache` or write `runs`.

## 9. Compute and login nodes can have different network egress

The login node may reach Hugging Face while a compute node is firewalled off (or vice-versa).
If a serving job hangs forever waiting for weights, the compute node probably can't reach HF —
pre-download the weights into `$RDS_ROOT/hf_cache` (`export HF_HOME=...`) on the login node
first.

## 10. The pipeline/dependent job sits `PD` until its dependency runs

With `--dependency=after:<jobid>` (or `afterok:`), `squeue` shows the dependent job `PD`
(pending) with reason `Dependency` until the upstream transitions to `R`. That's expected, not
a hang. (For a server+worker pair, the edge is usually `after:` — the worker waits for the
server to *start*, then polls the endpoint file — not `afterok:`, which would wait for the
server to *finish*.)

## 11. GRES count must match `--tensor-parallel-size`

`--gres=gpu:a100:2` without `--tensor-parallel-size 2` wastes a GPU; the reverse OOMs or fails
to start.
