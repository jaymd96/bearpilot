# Links & related resources

## Official BlueBEAR documentation

- **BEAR / BlueBEAR docs (canonical):** <https://docs.bear.bham.ac.uk/>
  Consult for SLURM/GPU/storage specifics, maintenance announcements, and any value this
  plugin's `references/cluster-ground-truth.md` flags as rotation-prone.

## SLURM CLI quick reference

The grammar of the commands this plugin drives. Full official docs: <https://slurm.schedmd.com/>.

| Command | Use |
|---|---|
| `sbatch <script>` | Submit a batch job; prints `Submitted batch job <id>`. |
| `squeue -u $USER` | Your queued/running jobs (state `PD`/`R`, reason, node). |
| `squeue -u $USER --long` | + time limits and more detail. |
| `sacct -j <id> --format=JobID,JobName,State,Elapsed,ExitCode,MaxRSS` | Accounting for a finished/running job (authoritative, survives node round-robin). |
| `sacct -u $USER -S today` | All your jobs since midnight. |
| `scancel <id>` | Cancel a job (or `scancel <id1> <id2>` for a server+worker pair). |
| `scontrol show job <id>` | Full live detail for a job. |
| `sinfo -o "%P %G %D %t"` | Partitions, GRES, node counts and state. |
| `sacctmgr -nP show assoc user=$USER` | Your account + the QoS you're allowed. |

## The companion tool: bear-harness

This plugin's **advanced** path. `bear-harness` is an LLM-callable deploy tool that submits a
**JobGraph** over SSH and realises it on BlueBEAR's SLURM under default-deny guardrails — state
is filesystem-attached, so any session reattaches a run by `run_id`.

This repo bundles the `bear-harness` engine, so the tool and its deep docs live right here. Key
docs (relative to the repo root, i.e. one level up from this plugin folder):

- `README.md` — the front door / CLI surface.
- `docs/PROJECT-VISION.md` — the loop + the `Kernel · JobGraph · Preset · Transport · State` primitives.
- `docs/bluebear.md` — the human first-run walkthrough.
- `docs/runbooks/validation.md` — the proven `bbshort` iteration loop.
- `references/` — one crib per external surface (`slurm-cli.md`, `vllm-serve-api.md`,
  `anthropic-messages-api.md`, `bluebear-platform.md`).
- `specs/01-foundational-contract.md` — the JobGraph contract.

## Prior art (SLURM + MCP)

- `yidong72/slurm_mcp` — SSH-first SLURM MCP (closest to the bear-harness transport shape).
- Duke `wjs/slurmmcp` — `slurmrestd`-based; **won't run here** (BlueBEAR exposes no REST door).
