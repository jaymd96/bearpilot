# SLURM CLI (`sbatch` / `squeue` / `sacct` / `scancel`) — CLI reference crib

<!-- SNAPSHOT-WITH-PROVENANCE. One file = one external surface the project drives.
     Allowed to age -- but it carries its source so staleness is detectable and refresh is
     mechanical. Provenance-at-top + refresh-contract-at-bottom around a distilled body. -->

> **Canonical source:** <https://slurm.schedmd.com/man_index.html> (SchedMD man pages: `sbatch`, `squeue`, `sacct`, `scancel`)
> **Version / pin:** Slurm 23.x as deployed on BlueBEAR (the cluster's installed version is authoritative; see [`bluebear-platform.md`](bluebear-platform.md) for how to confirm with `sinfo --version` on a login node). Flags below are stable across 21.x–24.x but **verify on a real `bbshort` run** before relying on a behaviour.
> **Why this crib:** `src/bear_harness/_slurm_runner.py` is the *only* code that shells out to SLURM. This crib exists to prevent the dependency-edge bug (the pipeline edge is `--dependency=after:` **not** `afterok:` — `afterok` would block the pipeline until vLLM *terminated successfully*, which never happens for a sidecar server) and the output-scraping bug (`sbatch --parsable` and `squeue -h -o %T` give machine-readable output; everything else is human-readable and fragile).

---
## What it is

The four SLURM command-line tools the login-node orchestrator drives to submit, watch, and tear down jobs. bear-harness treats SLURM as the source of truth for job state: it **submits** with `sbatch`, **polls live state** with `squeue`, **falls back to `sacct`** for the terminal state once a job has left the active queue, and **cancels** with `scancel`. There is exactly one shell seam (`run_shell` in `_slurm_runner.py`); tests inject a fake `ShellRunner` so the whole runner is exercisable without a real cluster.

---
## Surface that matters  (flags / endpoints / shapes)

```text
# SUBMIT — always --parsable so stdout is "<jobid>[;<cluster>]" and nothing else.
sbatch --parsable \
       --dependency=after:<VLLM_JOB_ID> \   # pipeline edge: starts when vLLM enters RUNNING
       <path/to/job.sbatch>                  # script written to disk, NOT --wrap (operators `less` it)

# sbatch directives that matter (set in the .sbatch script body or as flags):
--gres=gpu:a100_40:1        # GPU GRES request (see bluebear-platform.md for the strings)
--qos=<tier>                # QoS tier — bbshort / bbgpu / bbcpu (see bluebear-platform.md)
--time=HH:MM:SS             # walltime ceiling
--signal=B:SIGTERM@<sec>    # ask SLURM to SIGTERM the batch shell N sec before walltime
                            #   (the vLLM wrapper traps it to remove its endpoint file)

# POLL LIVE STATE — header-less, single field, machine-parseable:
squeue -h -j <JOB_ID> -o %T
#   -h        no header line
#   -j <id>   restrict to this job
#   -o %T     print only the job State (RUNNING, PENDING, ...). Empty output = job left the queue.

# TERMINAL-STATE FALLBACK — once squeue prints nothing, sacct holds the final state:
sacct -n -P -j <JOB_ID> -o JobID,State
#   -n        no header
#   -P        parsable, '|'-delimited (NOT --parsable; sacct spells it -P / --parsable2)
#   -j <id>   this job
#   -o ...    select columns; first matching row's State is the terminal state

# CANCEL — best-effort; failures are logged and swallowed:
scancel <JOB_ID>
scancel --signal=KILL <JOB_ID>   # escalation when the SIGTERM trap was too slow
```

### `%T` job-state codes (the ones the runner maps)

```text
RUNNING      → job is live (vLLM ready; pipeline `after:` dependency releases here)
PENDING      → queued / waiting (e.g. reason "Dependency" or "Resources")
COMPLETED    → finished, exit 0
FAILED       → finished, nonzero exit
CANCELLED    → scancel'd (squeue may also show CANCELLED+ / CANCELLED by <uid>)
TIMEOUT      → hit the --time walltime ceiling
OUT_OF_MEMORY→ OOM-killed (also surfaced by sacct as OUT_OF_MEMORY)
NODE_FAIL    → node died under the job
```
Anything not in this set maps to `UNKNOWN` in `_slurm_runner.py` rather than crashing the poller.

---
## Gotchas / quirks / version traps

- **`--dependency=after:` NOT `afterok:` for the pipeline edge.** `after:<jid>` releases the pipeline as soon as vLLM enters `RUNNING` — that is the whole point of a coupled server+worker topology. `afterok:` would wait for vLLM to *terminate successfully*, which a long-lived `role=sidecar` server never does; the pipeline would sit `PD Dependency` forever. If `scontrol show job <pipeline_jid> | grep Dependency` shows `afterok`, someone hand-edited the template — re-render via `bear-harness launch`.
- **`sbatch --parsable` vs `sacct -P` are different spellings.** `sbatch` uses `--parsable` (emits `<jobid>[;<cluster>]`). `sacct` uses `-P` / `--parsable2` (`|`-delimited rows). Do not mix them up — `sacct --parsable` is the *deprecated* `|`-with-trailing-delimiter form.
- **`squeue` goes silent once a job leaves the active queue.** Empty `squeue -h -j <id> -o %T` output does NOT mean RUNNING-with-no-state — it means the job is gone from the queue. You MUST fall back to `sacct` for the terminal state. Polling `squeue` alone will report a finished job as "unknown" forever.
- **`sacct` may lag squeue by seconds.** Accounting is flushed asynchronously; a job that just left `squeue` can briefly show no `sacct` row. Treat a missing `sacct` row as "not yet terminal, retry", not as failure.
- **Never key liveness on a PID.** BlueBEAR login nodes are round-robin (see [CLAUDE.md](../CLAUDE.md) observability discipline); the only durable truth is the job id + `squeue`/`sacct` + shared-FS artifacts. PIDs and `nohup` state do not survive a reconnect.
- **GRES / QoS / walltime strings are cluster-specific** — they live in [`bluebear-platform.md`](bluebear-platform.md), not here. This crib owns the *grammar*; that crib owns the *values*.

---
## How bear-harness uses this

<!-- Maps every external token to the code path that emits it. Cite by FILE PATH only -- never a line number. -->

| External concept | bear-harness call site | File ref (path, NEVER line number) |
|---|---|---|
| `sbatch --parsable <script>` | The single submit path; parses `<jobid>[;<cluster>]` off stdout | `src/bear_harness/_slurm_runner.py` |
| `--dependency=after:<VLLM_JOB_ID>` | The coupled pipeline edge, injected as an `extra_args` flag when submitting the pipeline job after the vLLM job | `src/bear_harness/_slurm_runner.py` |
| `--gres` / `--qos` / `--time` / `--signal` directives | Baked into the rendered sbatch scripts from `SlurmConfig` + per-launch overrides | `src/bear_harness/_vllm_launcher.py`, `src/bear_harness/_pipeline_launcher.py`, templates `vllm.sbatch.j2` / `pipeline.sbatch.j2` |
| `squeue -h -j <id> -o %T` | Live-state poll in the runner's status method | `src/bear_harness/_slurm_runner.py` |
| `%T` → `JobState` mapping | The state-code translator (`RUNNING`/`PENDING`/… → constants) | `src/bear_harness/_slurm_runner.py` |
| `sacct -n -P -j <id> -o JobID,State` | Terminal-state fallback once `squeue` is empty | `src/bear_harness/_slurm_runner.py` |
| `scancel <id>` (+ `--signal=KILL`) | Best-effort cancel, failures logged and swallowed | `src/bear_harness/_slurm_runner.py` |
| Sidecar teardown (scancel a `role=sidecar` server when consumers finish) | The JobGraph contract's sidecar lifecycle drives the scancel call | `src/bear_harness/_slurm_runner.py`; contract in [`../specs/01-foundational-contract.md`](../docs/internal/specs/01-foundational-contract.md) |

The keystone reason this crib is load-bearing: the kernel honours the JobGraph contract, not the workload ([`../docs/decision-notes/first-decision.md`](../docs/internal/decision-notes/first-decision.md)) — so the SLURM grammar here is the *only* scheduler vocabulary the kernel knows, and BlueBEAR-only ([`../docs/decision-notes/bluebear-only.md`](../docs/internal/decision-notes/bluebear-only.md)) means we optimise for exactly this CLI without a scheduler-abstraction tax.

---
## Open questions to resolve when we wire it

1. SLURM array submission (`sbatch --array=`) for the **bundle** topology (sweeps) is not yet driven — confirm the `--array` index syntax and how array-task state aggregates under `sacct` before W-next-cycle sweeps. Source: a real `bbshort` array submission.
2. `--signal` lead time vs the vLLM wrapper's actual SIGTERM-to-cleanup latency — the troubleshooting note (`scancel did not kill vLLM`) shows the trap can be slow. Measure on a real run and pin the lead time.

---
*Crib drafted 2026-06-14 against Slurm 23.x as deployed on BlueBEAR, from the SchedMD man pages cross-checked against `src/bear_harness/_slurm_runner.py`. Update on: a BlueBEAR Slurm major-version bump; any change to `%T` state-code output; the next time `squeue`/`sacct`/`sbatch --help` differs from the extracts above; the first time the bundle (array) topology lands.*

<!-- when to expand me: split only by DISTINCT decision surface -- e.g. if array/heterogeneous-job
     submission grows its own non-trivial flag set, that could earn a sibling crib. Self-demote
     drift-prone values: cluster-specific GRES/QoS strings live in bluebear-platform.md, not here. -->
