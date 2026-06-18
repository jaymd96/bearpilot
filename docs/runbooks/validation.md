# Validation runbook -- bear-harness

The operational moment this optimises for: you changed something that only fails on the real cluster
(an sbatch template, a vLLM flag, a launcher substitution) and you need to know *fast* whether the fix
holds. The whole runbook is one procedure -- **the bbshort iteration loop** -- wrapped in the incident
triage that feeds it. **The one load-bearing rule: key every watcher on the authoritative shared-FS
status source -- `run.json`, `.bear-harness-status.json`, and `sacct` -- NEVER on PID liveness.**
BlueBEAR round-robins login nodes, so a process "missing" from `pgrep` may just be running on the other
node; node-local state lies. This is the observability invariant in
[`CLAUDE.md`](../../CLAUDE.md) and [`references/bluebear-platform.md`](../../references/bluebear-platform.md),
and it is the reason this loop is reliable.

> Why bbshort? The `bbshort` QoS has a 10-minute walltime cap and spans all nodes including GPUs, so jobs
> start in 0--60s instead of queuing for hours. That turns a failed launch into a ~3-minute cost, not a
> queue-day -- which is what makes a tight diagnose -> fix -> relaunch loop possible at all. See the QoS
> tiers in [`references/bluebear-platform.md`](../../references/bluebear-platform.md).

## Quick reference

| Component | Source / signal | Key metric | Bad value |
|---|---|---|---|
| Harness state machine | `run.json` (shared FS, keyed by `run_id`) | last `state` | `failed`, or stuck in `booting` past `--boot-timeout` |
| Program heartbeat | `.bear-harness-status.json` (shared FS) | `runs` / `round` / `tokens` advancing | frozen counters, or `successful_calls == 0` at completion |
| SLURM ledger | `sacct -n -P -j <jid> -o JobID,State` | terminal State | `FAILED`, `OUT_OF_MEMORY`, `TIMEOUT`, `CANCELLED` |
| Live queue | `squeue -h -j <jid> -o %T` | current state | `PD Dependency` while vLLM is already `R` |
| vLLM endpoint | `endpoint.json` (published by server job) | host:port reachable | missing file, or `/v1/models` 401/404 |
| Per-job stdout | `vllm-<jid>.out` / `pipeline-<jid>.out` in the run dir | last lines | CUDA OOM, `Connection refused`, Lmod error |

All of these live under `$RDS_ROOT/.bear-harness/runs/<run_id>/` on the shared filesystem and are readable
from any login node, any session -- that is the filesystem-attached, reattach-by-`run_id` contract from
[`docs/decision-notes/first-decision.md`](../internal/decision-notes/first-decision.md). Read them; do not infer
state from a process you think you remember launching.

---

## 1. Launch hangs waiting for the endpoint

**Symptom:** `[status]` lines never appear. `squeue -u $USER` shows the vLLM job in `R`, but
`endpoint.json` for the run never materialises and the detached deploy never returns its `run_id` handle.

**Triage:**
1. `tail -f $RDS_ROOT/.bear-harness/runs/<run_id>/vllm-*.out` -- is the model still loading? A 70B model
   on two A100_80s takes 5--10 min; look for `Loading model weights` progress before assuming a hang.
2. If the log shows a hanging Hugging Face download or `Connection timed out` -> the compute node cannot
   reach HF, even though the bootstrap probe (which ran on the *login* node) passed. Compute nodes are
   firewalled differently.
3. If the log is healthy but slow and the harness times out first -> `--boot-timeout` is shorter than the
   real load time.

**Resolution:** For a firewalled compute node, pre-download weights once on the login node into the shared
cache (`HF_HOME=$RDS_ROOT/hf_cache python -c "from huggingface_hub import snapshot_download;
snapshot_download('<model>')"`) -- this is light I/O, acceptable on a login node; an actual *serve* is not
(see the login-nodes invariant in [`CLAUDE.md`](../../CLAUDE.md)). For a slow-but-healthy load, relaunch
with `--boot-timeout 1800`. Because deploy is detached and filesystem-attached, you do not lose the run by
walking away -- reattach later by `run_id`.

## 2. vLLM dies immediately with OutOfMemory

**Symptom:** `vllm-<jid>.out` ends with a CUDA OOM traceback within ~30s of start.

**Triage:**
1. `sacct -n -P -j <jid> -o JobID,State` -- confirm the terminal state is `OUT_OF_MEMORY`, not a
   different failure wearing OOM's clothes. `sacct` is the post-queue authoritative ledger; once a job
   leaves `squeue` it is the only place its exit state survives (see
   [`references/slurm-cli.md`](../../references/slurm-cli.md)).
2. If `OUT_OF_MEMORY` -> the model + KV-cache does not fit the requested GPU. A 7B FP16 model needs ~14GB
   plus headroom; on a shared 40GB A100 with `--gpu-memory-utilization > 0.9` it can tip over.

**Resolution:** Shrink the footprint or grow the allocation. Lower `--max-model-len` (e.g. `8192`) to cut
KV-cache pressure, or move to `gpu_gres = "gpu:a100_80:2"` with `tensor_parallel_size = 2` for large
models -- the GRES tiers (`a100_40`, `a100_80`) are in
[`references/bluebear-platform.md`](../../references/bluebear-platform.md), and `--gres` syntax is in
[`references/slurm-cli.md`](../../references/slurm-cli.md). This is a resource cap problem, not a science
one; guardrails govern resources only (see
[`docs/decision-notes/default-deny-guardrails.md`](../internal/decision-notes/default-deny-guardrails.md)).

## 3. Pipeline fails with Connection refused / 401 / 404

**Symptom:** `pipeline-<jid>.out` exec's the entrypoint, then dies with
`httpx.ConnectError: [Errno 111] Connection refused`, or every act fails with a silent 401, or a request
returns 404.

**Triage:**
1. Read `endpoint.json` for the run and curl the published URL directly:
   `curl -s http://<host>:<port>/v1/models -H "Authorization: Bearer <key>"`.
2. If it 404s on `/v1/models` -> you have a **double-prefix** bug: the base_url must be the server *root*
   with no trailing `/v1`, so the SDK forms `/v1/models`, not `/v1/v1/models`. This is a real bug the crib
   exists to prevent -- see [`references/vllm-serve-api.md`](../../references/vllm-serve-api.md).
3. If every request 401s **silently** (the run "completes" with zero successful calls) -> wrong auth
   header. vLLM's `--api-key` middleware accepts `Authorization: Bearer <key>`, NOT `x-api-key`. This
   exact bug caused a silent total-failure run. The Anthropic adapter must send the Bearer dialect; see
   [`references/anthropic-messages-api.md`](../../references/anthropic-messages-api.md) and
   [`references/vllm-serve-api.md`](../../references/vllm-serve-api.md).
4. If `pipeline-<jid>.out` shows `POST /v1/messages returned 404` -> the vLLM image predates the Anthropic
   Messages route (needs >=0.11 or post PR #22627/#27882). Re-pull the SIF or pin a newer
   `--apptainer-image`.
5. If it is a genuine `Connection refused` while vLLM is healthy -> stale endpoint: vLLM died between
   writing `endpoint.json` and serving the first request. Check whether vLLM exited *after* publishing.

**Resolution:** Fix the auth/URL dialect at its source -- the launcher bakes the endpoint into the
pipeline command at submit time (`$MODEL_BASE_URL` substitution in
[`src/bear_harness/_pipeline_launcher.py`](../../src/bear_harness/_pipeline_launcher.py)), which is also
*why* detached deploy returns only after the vLLM probe, not after submitting both jobs (see
[`docs/decision-notes/detached-deploy-cut-after-probe.md`](../internal/decision-notes/detached-deploy-cut-after-probe.md)).
The silent-401 class is the canonical reason for the loud-failure bar: a run that makes zero successful
calls must raise, not complete green -- the `ZeroSuccessfulCallsError` pattern. Never let a 401 storm look
like success.

## 4. Pipeline stays `PD Dependency` forever

**Symptom:** `squeue -u $USER` shows the vLLM job `R` but the pipeline job `PD` with reason `Dependency`.

**Triage:**
1. `squeue -h -j <vllm_jid> -o %T` -- is vLLM actually `R`? If it is still `PD`, the dependency is
   correct; wait for the scheduler.
2. If vLLM is `R` and the pipeline is still blocked -> `scontrol show job <pipeline_jid> | grep Dependency`.

**Resolution:** The contract uses `--dependency=after:` (release when the dependency *starts*), NOT
`afterok` -- so a running vLLM should release the pipeline immediately. The distinction is deliberate and
documented in [`references/slurm-cli.md`](../../references/slurm-cli.md). If `scontrol` reports `afterok`,
someone hand-edited the sbatch template; re-render by re-running `bear-harness launch`. This `after` /
`publishes` / `consumes` wiring is the JobGraph edge semantics
([`docs/decision-notes/first-decision.md`](../internal/decision-notes/first-decision.md)).

## 5. scancel did not kill vLLM (sidecar leak)

**Symptom:** cancel exited cleanly but the vLLM job keeps consuming GPU after its consumer finished.

**Triage:**
1. `sacct -n -P -j <vllm_jid> -o JobID,State` -- is it still `RUNNING`?
2. If yes -> the `SIGTERM` from `--signal`/`scancel` did not propagate into the apptainer container fast
   enough.

**Resolution:** Escalate with `scancel --signal=KILL <vllm_jid>`
([`references/slurm-cli.md`](../../references/slurm-cli.md)). The server job carries `role=sidecar` in the
JobGraph precisely so it is scancelled when its consumers finish
([`docs/decision-notes/first-decision.md`](../internal/decision-notes/first-decision.md)); a leaked sidecar is a
guardrail concern because it burns GPU-hours against the cap
([`docs/decision-notes/default-deny-guardrails.md`](../internal/decision-notes/default-deny-guardrails.md)).

## 6. `module load CUDA/...` fails on the compute node

**Symptom:** `vllm-<jid>.out` opens with `Lmod has detected the following error: The following module(s)
are unknown: "CUDA/12.1.1"`.

**Triage:**
1. On a login node: `module avail CUDA 2>&1 | grep -v '^$'` -- list what is actually installed.
2. CUDA modules on BlueBEAR rotate; the pinned name in `bear.toml` has drifted past what the node offers.

**Resolution:** Pick a concrete version marked `(D)` (default) or known-good and set `cuda_module` in
`bear.toml`. The module system is documented in
[`references/bluebear-platform.md`](../../references/bluebear-platform.md). Folding this into the loop:
this is exactly the kind of one-line environment drift the bbshort loop catches in one ~5-minute rung.

---

## Procedure: the bbshort iteration loop

<!-- The proven, highest-value operational procedure. One variable changes per rung so each failure
     names its own layer. Each step's "why" is tied to a constraint or decision-note. -->

This is the proven debug cycle -- five launches in ~35 minutes, four template bugs found and fixed, run
completed (verified 2026-06-11). One rung is ~5 minutes. Change **one** variable per rung so each failure
names its own layer.

1. **Diagnose from the shared-FS job logs.** Read the failed job's `.out` (`vllm-<jid>.out` /
   `pipeline-<jid>.out`) and the JSONL state in `$RDS_ROOT/.bear-harness/runs/<run_id>/` -- the sbatch
   logs are ground truth. Confirm the terminal state with `sacct`, never with `pgrep` or a remembered PID.
   *Why:* round-robin login nodes make node-local state unreliable; only shared-FS artifacts + the SLURM
   ledger are authoritative (observability invariant in [`CLAUDE.md`](../../CLAUDE.md) and
   [`references/bluebear-platform.md`](../../references/bluebear-platform.md)).

2. **Fix with a failing test first (red -> green).** Write the failing test before the fix, mirroring the
   existing patterns (e.g. `TestDetach`, `TestLaunchResultSerialisation`, the qos-override tests). Run the
   *full* suite with `set -o pipefail` -- a bare `pytest | tail` swallows the failure. The W1 launch
   changes live in [`src/bear_harness/_launch.py`](../../src/bear_harness/_launch.py) (detach param,
   `LaunchResult.as_dict()`, the early-return cut after the vLLM probe) with the `--detach`/`--json`
   wiring in [`src/bear_harness/_cli.py`](../../src/bear_harness/_cli.py). *Why:* invariants enforced by a
   test cannot regress; the loud-failure bar is only real if a test asserts it. See
   [`CONTRIBUTING.md`](../internal/CONTRIBUTING.md) for the green-checks gate (`hatch run test`, `hatch run
   lint`, `ty`).

3. **Update and push via `setup-bluebear.sh`.** Run `bash scripts/setup-bluebear.sh update` -- it builds
   the wheel, rsyncs it, and force-reinstalls on the cluster (~60s). (`full` does the first-time install;
   `update` is the inner-loop command.) *Why:* one command makes deploy reproducible and keeps the cluster
   copy exactly equal to the local fix -- see [`scripts/setup-bluebear.sh`](../../scripts/setup-bluebear.sh).

4. **Relaunch under bbshort.** Submit with the short QoS and a bounded boot timeout, e.g.
   `bear-harness launch <pipeline>.bbshort.toml --qos bbshort --walltime 00:10:00 --boot-timeout 480
   --max-model-len 8192 --extra-vllm-args "--enforce-eager"`, nohup'd on a **pinned login node IP**
   (e.g. `172.31.3.100`). *Why:* bbshort starts in 0--60s so a failed attempt costs ~3 min not a
   queue-day; pinning the SSH node keeps the orchestrator and its watcher on the same machine so the PID
   watcher does not get a false "process gone" from a wrong-node read (observability invariant,
   [`CLAUDE.md`](../../CLAUDE.md)). QoS tiers: [`references/bluebear-platform.md`](../../references/bluebear-platform.md).

5. **Watch via the status stream, keyed on durable artifacts.** Run the stream loop
   (`~/stream-status.sh`, backed by [`src/bear_harness/_status_follow.py`](../../src/bear_harness/_status_follow.py))
   plus the local Monitor tool: emit one line per *change* of (`run.json` state | `squeue` jobs |
   `.bear-harness-status.json` heartbeat: runs/round/tokens/message). Dedup on those fields, excluding
   elapsed-time, or it spams every poll. *Why:* every signal is a shared-FS artifact or `sacct` -- all
   node-agnostic and authoritative -- so the watcher is correct regardless of which login node answers.
   **Never** key completion on PID liveness via the round-robin hostname.

Loop back to step 1 with the next single change. When a rung goes fully green on the cluster, the fix is
real -- per the convention that an HPC change is only trusted after a real bbshort run
([`CONTRIBUTING.md`](../internal/CONTRIBUTING.md)).

> Two failures this loop will *not* let you confuse with success, by design:
> - A run that makes **zero successful calls** must raise (`ZeroSuccessfulCallsError`), not complete green
>   -- the silent-401 class in incident 3.
> - A job that left `squeue` has its real fate only in `sacct`; absence from `squeue` is never "passed".

<!-- Record corrections inline, never silently:
     *(Corrected <YYYY-MM-DD>: previously said X -- wrong because Y. Correct: Z.)* -->

## Folded-in source

The incident entries above graduate [`docs/troubleshooting.md`](../troubleshooting.md) (keyed to the
operator-visible symptom) into this runbook's Symptom/Triage/Resolution grammar, and route every external
gotcha to its crib: SLURM CLI -> [`references/slurm-cli.md`](../../references/slurm-cli.md); vLLM serve API
-> [`references/vllm-serve-api.md`](../../references/vllm-serve-api.md); Anthropic Messages API ->
[`references/anthropic-messages-api.md`](../../references/anthropic-messages-api.md); BlueBEAR platform ->
[`references/bluebear-platform.md`](../../references/bluebear-platform.md). `docs/troubleshooting.md` is
**superseded-by** this runbook for the symptoms covered here; consult it for any symptom not yet folded.

<!-- when to expand me: LIVING doc. Split into oncall.md / validation.md / dr-backup.md once
     one file mixes more than ~2 distinct grammars (incident triage vs DR cadence vs a recurring
     procedure). Today this file holds exactly two -- symptom triage (incidents 1-6) and one recurring
     procedure (the bbshort loop) -- which is the cap. Add a third grammar (e.g. a DR/restore cadence)
     and split before writing it. -->
