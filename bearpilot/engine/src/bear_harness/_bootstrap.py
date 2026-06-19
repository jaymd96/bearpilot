"""``bear-harness bootstrap`` — prepare a BlueBEAR environment.

Bootstrap is a one-shot script that walks an operator through the
irreversible "first time on this cluster" steps:

1. Verify ``apptainer`` is on ``PATH``.
2. Create the RDS directory tree for runs, endpoints, logs, the
   apptainer image, and the HuggingFace cache.
3. Pull ``vllm/vllm-openai:<tag>`` into the apptainer image cache, if
   the ``.sif`` does not already exist.
4. Probe ``https://huggingface.co`` from the login node as a best-effort
   reachability check. (Compute nodes may still be firewalled; bootstrap
   cannot verify that.)
5. Show the operator the CUDA modules available and capture their choice.
6. Render ``bear.toml`` from the bundled ``bear.toml.example`` template
   pre-filled with the discovered values.
7. Print a checklist of things the operator still has to verify by hand.

Every step is idempotent. Re-running bootstrap after it has already
succeeded is safe and will update ``bear.toml`` without re-pulling the
image or clobbering the runs directory.

The module deliberately does not depend on the rest of the harness
runtime. Importing ``_bootstrap`` pulls in only stdlib and the
configuration-writer in ``_bear_config``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bear_harness._template_env import TEMPLATES_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class BootstrapError(RuntimeError):
    """Raised for any fatal bootstrap problem the operator must fix."""


@dataclass(frozen=True, slots=True)
class BootstrapOptions:
    """User-facing inputs to :func:`run_bootstrap`.

    ``rds_root`` is the project-scoped area under ``/rds/projects/...``
    where the harness keeps runs, endpoints, logs and the apptainer
    image. ``account`` is the BlueBEAR project code used for sbatch
    ``--account``. ``apptainer_image`` is the docker reference that
    apptainer will ``pull`` into the image cache.
    """

    rds_root: Path
    account: str
    apptainer_image: str = "docker://vllm/vllm-openai:latest"
    cuda_module: str = "CUDA/12.1.1"
    gpu_gres: str = "gpu:a100_40:1"
    qos: str = "bbgpu"
    cpus_per_task: int = 8
    mem_gb: int = 64
    walltime: str = "08:00:00"
    config_path: Path | None = None  # defaults to ~/.config/bear-harness/bear.toml
    skip_pull: bool = False
    mail_user: str | None = None


@dataclass(slots=True)
class BootstrapReport:
    """What bootstrap actually did, for the CLI to summarise."""

    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config_path: Path | None = None
    sif_path: Path | None = None

    def step(self, msg: str) -> None:
        self.steps.append(msg)
        logger.info(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        logger.warning(msg)


# ---------------------------------------------------------------------------
# Shell seam — identical shape to ``_slurm_runner.ShellResult`` so the
# same fake can stub both.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


def _default_run(argv: Sequence[str]) -> CommandResult:
    cp = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        returncode=cp.returncode,
        stdout=cp.stdout or "",
        stderr=cp.stderr or "",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_bootstrap(
    options: BootstrapOptions,
    *,
    run_shell: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    writer: Callable[[Path, str], None] | None = None,
) -> BootstrapReport:
    """Execute the bootstrap steps and return a structured report.

    Every external effect is injected so tests can stub ``apptainer
    pull``, ``module avail``, and filesystem writes.
    """
    shell = run_shell or _default_run
    which_fn = which or shutil.which
    write = writer or _default_writer
    report = BootstrapReport()

    _check_apptainer(which_fn, report)
    sif_path = _ensure_rds_tree(options, report)
    report.sif_path = sif_path
    if not options.skip_pull:
        _pull_image(options, sif_path, shell, report)
    else:
        report.step(f"skipping apptainer pull (sif target: {sif_path})")
    _probe_hf_reachable(shell, report)
    _detect_cuda_modules(options, shell, report)
    config_path = _write_bear_toml(options, sif_path, write, report)
    report.config_path = config_path
    _print_checklist(options, report)
    return report


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _check_apptainer(which_fn: Callable[[str], str | None], report: BootstrapReport) -> None:
    """Fail early if apptainer is not on PATH."""
    path = which_fn("apptainer")
    if path is None:
        msg = (
            "apptainer not found on PATH. BlueBEAR requires `module load "
            "Apptainer` before running bootstrap (or add it to your "
            ".bashrc)."
        )
        raise BootstrapError(msg)
    report.step(f"apptainer found at {path}")


def _ensure_rds_tree(options: BootstrapOptions, report: BootstrapReport) -> Path:
    """Create the RDS directory tree and return the target .sif path."""
    rds = options.rds_root
    subdirs = [
        rds / ".bear-harness" / "endpoints",
        rds / ".bear-harness" / "runs",
        rds / ".bear-harness" / "logs",
        rds / ".bear-harness" / "apptainer",
        rds / "hf_cache",
    ]
    for d in subdirs:
        d.mkdir(parents=True, exist_ok=True)
    report.step(f"created rds tree under {rds}")
    return rds / ".bear-harness" / "apptainer" / "vllm-openai.sif"


def _pull_image(
    options: BootstrapOptions,
    sif_path: Path,
    shell: CommandRunner,
    report: BootstrapReport,
) -> None:
    """Pull the vLLM apptainer image if it isn't already present.

    Idempotent: an existing .sif is left alone. Re-pulling intentionally
    requires ``rm``-ing the file first — bootstrap does not trigger a
    long-running download on every rerun.
    """
    if sif_path.exists():
        report.step(f"apptainer image already present: {sif_path}")
        return
    report.step(f"apptainer pull {options.apptainer_image} -> {sif_path}")
    result = shell(("apptainer", "pull", str(sif_path), options.apptainer_image))
    if result.returncode != 0:
        msg = (
            f"apptainer pull failed rc={result.returncode} "
            f"stderr={result.stderr.strip()!r}"
        )
        raise BootstrapError(msg)


def _probe_hf_reachable(shell: CommandRunner, report: BootstrapReport) -> None:
    """Best-effort reachability check for Hugging Face from the login node.

    This is not authoritative — compute nodes can be firewalled
    independently. Used only to give the operator an early heads-up if
    the login node can't reach HF.
    """
    result = shell(("curl", "-sSfI", "--max-time", "5", "https://huggingface.co/"))
    if result.returncode == 0:
        report.step("huggingface.co reachable from login node")
    else:
        report.warn(
            "huggingface.co unreachable from login node; "
            "compute nodes may still be able to fetch weights — verify manually"
        )


def _detect_cuda_modules(
    options: BootstrapOptions,
    shell: CommandRunner,
    report: BootstrapReport,
) -> None:
    """Run ``module avail CUDA`` and record the available modules.

    Bootstrap does not try to auto-pick — operators override
    ``cuda_module`` in ``bear.toml`` after inspecting the report.
    """
    result = shell(("bash", "-lc", "module avail CUDA 2>&1"))
    if result.returncode != 0:
        report.warn(
            "`module avail CUDA` failed — default cuda_module "
            f"{options.cuda_module!r} kept; verify manually"
        )
        return
    modules = [
        line.strip()
        for line in result.stdout.splitlines()
        if "CUDA" in line and "-----" not in line
    ]
    if modules:
        report.step(f"found CUDA modules: {modules[:5]}{'…' if len(modules) > 5 else ''}")
    else:
        report.warn("no CUDA modules reported by `module avail`")


def _write_bear_toml(
    options: BootstrapOptions,
    sif_path: Path,
    write: Callable[[Path, str], None],
    report: BootstrapReport,
) -> Path:
    """Render ``bear.toml.example`` with operator-supplied substitutions."""
    template = (TEMPLATES_DIR / "bear.toml.example").read_text()
    rds = str(options.rds_root)
    body = (
        template.replace(
            "/rds/projects/CHANGE/CHANGE/.bear-harness/apptainer/vllm-openai.sif",
            str(sif_path),
        )
        .replace(
            "/rds/projects/CHANGE/CHANGE/hf_cache",
            f"{rds}/hf_cache",
        )
        .replace(
            "/rds/projects/CHANGE/CHANGE/.bear-harness/runs",
            f"{rds}/.bear-harness/runs",
        )
        .replace(
            "/rds/projects/CHANGE/CHANGE/.bear-harness/endpoints",
            f"{rds}/.bear-harness/endpoints",
        )
        .replace('account       = "CHANGE_ME"', f'account       = "{options.account}"')
        .replace('cuda_module   = "CUDA/12.1.1"', f'cuda_module   = "{options.cuda_module}"')
        .replace('gpu_gres      = "gpu:a100_40:1"', f'gpu_gres      = "{options.gpu_gres}"')
        .replace('qos           = "bbgpu"', f'qos           = "{options.qos}"')
        .replace("cpus_per_task = 8", f"cpus_per_task = {options.cpus_per_task}")
        .replace("mem_gb        = 64", f"mem_gb        = {options.mem_gb}")
        .replace('walltime      = "08:00:00"', f'walltime      = "{options.walltime}"')
    )
    if options.mail_user is not None:
        body = body.replace(
            '# mail_user   = "you@example.com"',
            f'mail_user   = "{options.mail_user}"',
        )

    config_path = options.config_path or (Path.home() / ".config" / "bear-harness" / "bear.toml")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write(config_path, body)
    report.step(f"wrote {config_path}")
    return config_path


def _default_writer(path: Path, body: str) -> None:
    """Default ``writer`` callable used in production."""
    path.write_text(body)


def _print_checklist(options: BootstrapOptions, report: BootstrapReport) -> None:
    """Append a manual checklist to the report for things bootstrap can't verify."""
    rds = options.rds_root
    report.step(
        "MANUAL CHECKLIST — items bootstrap cannot verify automatically:\n"
        f"  1. Your BlueBEAR project code is {options.account!r} — confirm with your PI.\n"
        f"  2. The RDS area {rds} has enough quota for HF weights (80GB recommended).\n"
        "  3. Compute nodes can reach huggingface.co (run `bear-harness launch --dry-run` "
        "and inspect the generated sbatch before submitting for real).\n"
        "  4. Your SLURM user is whitelisted for the `bbgpu` QoS.\n"
        "  5. If you plan to run 70B models, override gpu_gres to `gpu:a100_80:2` "
        "and tensor_parallel_size = 2 in bear.toml."
    )


__all__ = [
    "BootstrapError",
    "BootstrapOptions",
    "BootstrapReport",
    "CommandResult",
    "CommandRunner",
    "run_bootstrap",
]
