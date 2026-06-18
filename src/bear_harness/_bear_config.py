"""Cluster-local harness configuration — ``~/.config/bear-harness/bear.toml``.

Where ``pipeline.toml`` describes a *program*, ``bear.toml`` describes
the *cluster* that will run it: RDS paths, SLURM QoS, GPU GRES strings,
Apptainer image path, HuggingFace cache, CUDA module name. One
``bear.toml`` per host — operators edit it once after bootstrap, then
every ``launch`` reads it.

The local-mode config has a different shape: no SLURM, no apptainer,
just a few overridable paths and a Python interpreter. Both shapes live
behind the ``BearConfig`` dataclass and are dispatched by the ``mode``
field so the rest of the harness does not branch on it.

This module deliberately does not synthesise defaults for cluster
mode — bootstrap writes the file, ``launch`` reads it. If a field is
missing, that is a bootstrap bug and the user should re-run it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bear_harness._duration import DurationError, parse_walltime_seconds

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "bear-harness" / "bear.toml"


class BearConfigError(ValueError):
    """Raised for any structural problem with ``bear.toml``."""


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Ollama-backend parameters for ``[local.ollama]``.

    Only consulted when ``LocalConfig.backend == "ollama"``. The config
    may still be parsed when the backend is vLLM (so users can keep both
    sections ready and flip between them with a one-line edit).

    ``model`` is the Ollama model tag (e.g. ``llama3.2`` or
    ``llama3.2:3b``). ``host``/``port`` point at the Ollama daemon's
    OpenAI-compatible API — defaults match ``ollama serve``'s defaults.

    ``thinking_budget`` is the number of extra tokens reserved for
    chain-of-thought reasoning in models that support it (Qwen3,
    DeepSeek-R1). The shim inflates ``max_tokens`` by this amount so
    the model has headroom for internal reasoning without stealing from
    the caller's content budget. Set to ``0`` to disable thinking.
    """

    model: str
    host: str = "127.0.0.1"
    port: int = 11434
    thinking_budget: int = 4096


@dataclass(frozen=True, slots=True)
class LocalConfig:
    """Paths and defaults for ``--local`` mode.

    ``runs_dir`` is where per-run state (``run.json``, logs, artifacts
    tarball) lives. Defaults to ``/tmp/.bear-harness/runs`` so local
    mode Just Works without any bootstrap step.

    ``backend`` selects which local model runtime to use: ``"vllm"``
    (the pre-Phase-4 default — spawns ``vllm serve``) or ``"ollama"``
    (Mac-friendly path via ``OllamaBackend`` + ``MessagesShim``).
    ``ollama`` carries the Ollama-specific parameters and is required
    iff ``backend == "ollama"``.
    """

    runs_dir: Path = field(default_factory=lambda: Path("/tmp/.bear-harness/runs"))
    endpoints_dir: Path = field(default_factory=lambda: Path("/tmp/.bear-harness/endpoints"))
    python: str = "python3"
    default_vllm_port: int = 8000
    backend: str = "vllm"
    ollama: OllamaConfig | None = None


@dataclass(frozen=True, slots=True)
class SlurmConfig:
    """BlueBEAR-specific SLURM + apptainer configuration."""

    account: str
    qos: str
    gpu_gres: str
    cpus_per_task: int
    mem_gb: int
    walltime: str
    cuda_module: str
    apptainer_sif: Path
    hf_cache: Path
    runs_dir: Path
    endpoints_dir: Path
    cpu_qos: str | None = None  # QoS for the CPU pipeline job; None => fall back to qos
    boot_timeout_seconds: int = 900
    vllm_port_range: tuple[int, int] = (8000, 8099)
    tensor_parallel_size: int = 1
    max_model_len: int | None = None
    mail_user: str | None = None
    mail_events: str = "BEGIN,END,FAIL"
    extra_vllm_args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    """Default-deny resource caps for ``[guardrails]`` — governs RESOURCES, never science.

    The autonomy boundary is the whole point: these caps bound *what the agent can
    spend* (QoS tier, walltime, GPU-hours, concurrency), never *what it studies*
    (model, prompts, campaign size). See
    ``docs/decision-notes/default-deny-guardrails.md``.

    Default-DENY lives in the *defaults*: a missing ``[guardrails]`` section yields
    this tight built-in leash, NOT an unbounded one — the agent starts constrained
    and a human widens it explicitly in config. Caps are in BlueBEAR units
    (``references/bluebear-platform.md``):

    - ``qos_allowlist`` — permitted QoS tiers. Default ``("bbshort",)``: only the
      ~10-minute smoke tier until a human adds ``bbgpu`` / ``bbcpu``.
    - ``max_walltime`` — ceiling as a SLURM ``HH:MM:SS`` string. Default
      ``"00:10:00"`` (bbshort's cap).
    - ``max_concurrent_jobs`` — simultaneous SLURM jobs. Default ``2``: the
      reference vLLM+pipeline flow fits; a second concurrent campaign is denied.
    - ``gpu_hours_budget`` — reservation ceiling for one launch,
      ``gpu_count * walltime_hours``. Default ``1.0``.
    - ``require_dry_run`` — when true, a launch with no prior dry-run is denied.
    """

    qos_allowlist: tuple[str, ...] = ("bbshort",)
    max_walltime: str = "00:10:00"
    max_concurrent_jobs: int = 2
    gpu_hours_budget: float = 1.0
    require_dry_run: bool = False


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """Opt-in fire-and-forget notification on terminal run states — ``[notify]``.

    Notify answers the reliability bar "ping me when a run finishes or fails" so
    nobody has to babysit SLURM. It is the deliberate INVERSE of ``[guardrails]``
    on one axis: notify is *opt-in* — an absent ``[notify]`` section sends
    nothing — because notification is a convenience, not a safety gate. Firing
    nothing is the safe default; an absent guardrail, by contrast, must still
    deny. See ``docs/decision-notes/notify-on-done.md``.

    Two harness-side backends, both fire-and-forget (a misconfigured one is
    logged and swallowed — never raised into the run, never allowed to hang it):

    - ``command`` — an argv run as a subprocess. ``{event}`` / ``{run_id}`` /
      ``{state}`` / ``{run_dir}`` / ``{model}`` / ``{error}`` placeholders are
      substituted into each element, and the same fields are exported as
      ``BEAR_NOTIFY_*`` environment variables.
    - ``webhook_url`` — a JSON ``POST`` of the event payload.

    Email is intentionally NOT a harness backend: SLURM already sends native
    job-event email via ``[slurm].mail_user`` / ``mail_events``. Use that — the
    harness does not grow an SMTP client.

    ``on_done`` / ``on_fail`` gate which terminal transitions fire.
    ``timeout_seconds`` bounds each backend so a hung webhook or command can
    never stall a run.
    """

    on_done: bool = True
    on_fail: bool = True
    command: tuple[str, ...] = ()
    webhook_url: str | None = None
    timeout_seconds: float = 10.0

    @property
    def enabled(self) -> bool:
        """True iff at least one backend is configured (else notify is a no-op)."""
        return bool(self.command) or self.webhook_url is not None


@dataclass(frozen=True, slots=True)
class BearConfig:
    mode: str  # "local" | "slurm"
    local: LocalConfig | None = None
    slurm: SlurmConfig | None = None
    guardrails: GuardrailConfig = field(default_factory=GuardrailConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def is_slurm(self) -> bool:
        return self.mode == "slurm"

    def require_local(self) -> LocalConfig:
        if self.local is None:
            msg = "bear.toml is not in local mode"
            raise BearConfigError(msg)
        return self.local

    def require_slurm(self) -> SlurmConfig:
        if self.slurm is None:
            msg = "bear.toml is not in slurm mode"
            raise BearConfigError(msg)
        return self.slurm


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_TOP_LEVEL_KEYS = {"mode", "local", "slurm", "guardrails", "notify"}


def default_local_config() -> BearConfig:
    """Return a ready-to-use local-mode config with no file I/O."""
    return BearConfig(mode="local", local=LocalConfig())


def default_guardrails() -> GuardrailConfig:
    """The tight built-in leash used when ``[guardrails]`` is absent.

    Default-DENY: an absent ``[guardrails]`` section is not "unbounded", it is
    this leash. Widening it is a deliberate human edit, never an agent inference.
    """
    return GuardrailConfig()


def default_notify() -> NotifyConfig:
    """The default when ``[notify]`` is absent: notification OFF.

    Notify is opt-in (the inverse of guardrails' default-deny): with no backend
    configured, a run completes silently. A human opts in by adding a backend.
    """
    return NotifyConfig()


def load_bear_config(path: str | Path | None = None) -> BearConfig:
    """Load ``bear.toml``. Falls back to a default local config when missing.

    The caller normally passes ``None``. A missing default file is not
    an error: we synthesise a local-mode config on the fly so developers
    on laptops never have to run bootstrap.
    """
    p = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    if not p.is_file():
        if path is None:
            return default_local_config()
        msg = f"bear.toml not found at {p}"
        raise BearConfigError(msg)

    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {p}: {exc}"
        raise BearConfigError(msg) from exc

    unknown = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        msg = f"unknown top-level keys in {p}: {sorted(unknown)}"
        raise BearConfigError(msg)

    mode = data.get("mode", "local")
    if mode not in {"local", "slurm"}:
        msg = f"bear.toml mode must be 'local' or 'slurm', got {mode!r}"
        raise BearConfigError(msg)

    # Guardrails are a top-level section: they govern resources in *either* mode,
    # so they are parsed independently of the local/slurm dispatch. Absent => leash.
    guardrails = (
        _parse_guardrails(data["guardrails"]) if "guardrails" in data else default_guardrails()
    )
    # Notify is also a top-level section (fires in either mode). Absent => silent.
    notify = _parse_notify(data["notify"]) if "notify" in data else default_notify()

    if mode == "local":
        return BearConfig(
            mode="local",
            local=_parse_local(data.get("local", {})),
            guardrails=guardrails,
            notify=notify,
        )
    return BearConfig(
        mode="slurm",
        slurm=_parse_slurm(data.get("slurm", {})),
        guardrails=guardrails,
        notify=notify,
    )


_LOCAL_KEYS = {
    "runs_dir",
    "endpoints_dir",
    "python",
    "default_vllm_port",
    "backend",
    "ollama",
}
_VALID_BACKENDS = {"vllm", "ollama"}
_OLLAMA_KEYS = {"model", "host", "port", "thinking_budget"}


def _parse_local(d: dict) -> LocalConfig:
    unknown = set(d.keys()) - _LOCAL_KEYS
    if unknown:
        msg = f"unknown keys in [local]: {sorted(unknown)}"
        raise BearConfigError(msg)

    backend = d.get("backend", "vllm")
    if backend not in _VALID_BACKENDS:
        msg = f"[local].backend must be one of {sorted(_VALID_BACKENDS)}, got {backend!r}"
        raise BearConfigError(msg)

    ollama_raw = d.get("ollama")
    ollama_cfg = _parse_ollama(ollama_raw) if ollama_raw is not None else None

    if backend == "ollama" and ollama_cfg is None:
        msg = '[local].backend = "ollama" requires a [local.ollama] sub-table'
        raise BearConfigError(msg)

    return LocalConfig(
        runs_dir=Path(d.get("runs_dir", "/tmp/.bear-harness/runs")).expanduser(),
        endpoints_dir=Path(d.get("endpoints_dir", "/tmp/.bear-harness/endpoints")).expanduser(),
        python=d.get("python", "python3"),
        default_vllm_port=int(d.get("default_vllm_port", 8000)),
        backend=backend,
        ollama=ollama_cfg,
    )


def _parse_ollama(d: dict) -> OllamaConfig:
    if not isinstance(d, dict):
        msg = "[local.ollama] must be a table"
        raise BearConfigError(msg)
    unknown = set(d.keys()) - _OLLAMA_KEYS
    if unknown:
        msg = f"unknown keys in [local.ollama]: {sorted(unknown)}"
        raise BearConfigError(msg)
    if "model" not in d:
        msg = "[local.ollama] requires a model field"
        raise BearConfigError(msg)
    return OllamaConfig(
        model=str(d["model"]),
        host=str(d.get("host", "127.0.0.1")),
        port=int(d.get("port", 11434)),
        thinking_budget=int(d.get("thinking_budget", 4096)),
    )


_SLURM_REQUIRED = {
    "account",
    "qos",
    "gpu_gres",
    "cpus_per_task",
    "mem_gb",
    "walltime",
    "cuda_module",
    "apptainer_sif",
    "hf_cache",
    "runs_dir",
    "endpoints_dir",
}

_SLURM_OPTIONAL = {
    "cpu_qos",
    "boot_timeout_seconds",
    "vllm_port_range",
    "tensor_parallel_size",
    "max_model_len",
    "mail_user",
    "mail_events",
    "extra_vllm_args",
}


def _parse_slurm(d: dict) -> SlurmConfig:
    unknown = set(d.keys()) - (_SLURM_REQUIRED | _SLURM_OPTIONAL)
    if unknown:
        msg = f"unknown keys in [slurm]: {sorted(unknown)}"
        raise BearConfigError(msg)
    missing = _SLURM_REQUIRED - set(d.keys())
    if missing:
        msg = f"missing required keys in [slurm]: {sorted(missing)}"
        raise BearConfigError(msg)

    port_range = tuple(d.get("vllm_port_range", [8000, 8099]))
    if len(port_range) != 2 or not all(isinstance(x, int) for x in port_range):
        msg = "slurm.vllm_port_range must be [low, high] integers"
        raise BearConfigError(msg)

    return SlurmConfig(
        account=str(d["account"]),
        qos=str(d["qos"]),
        cpu_qos=(str(d["cpu_qos"]) if d.get("cpu_qos") is not None else None),
        gpu_gres=str(d["gpu_gres"]),
        cpus_per_task=int(d["cpus_per_task"]),
        mem_gb=int(d["mem_gb"]),
        walltime=str(d["walltime"]),
        cuda_module=str(d["cuda_module"]),
        apptainer_sif=Path(d["apptainer_sif"]).expanduser(),
        hf_cache=Path(d["hf_cache"]).expanduser(),
        runs_dir=Path(d["runs_dir"]).expanduser(),
        endpoints_dir=Path(d["endpoints_dir"]).expanduser(),
        boot_timeout_seconds=int(d.get("boot_timeout_seconds", 900)),
        vllm_port_range=(int(port_range[0]), int(port_range[1])),
        tensor_parallel_size=int(d.get("tensor_parallel_size", 1)),
        max_model_len=(int(d["max_model_len"]) if d.get("max_model_len") is not None else None),
        mail_user=str(d["mail_user"]) if d.get("mail_user") is not None else None,
        mail_events=str(d.get("mail_events", "BEGIN,END,FAIL")),
        extra_vllm_args=tuple(str(a) for a in d.get("extra_vllm_args", ())),
    )


_GUARDRAILS_KEYS = {
    "qos_allowlist",
    "max_walltime",
    "max_concurrent_jobs",
    "gpu_hours_budget",
    "require_dry_run",
}


def _parse_guardrails(d: dict) -> GuardrailConfig:
    if not isinstance(d, dict):
        msg = "[guardrails] must be a table"
        raise BearConfigError(msg)
    unknown = set(d.keys()) - _GUARDRAILS_KEYS
    if unknown:
        msg = f"unknown keys in [guardrails]: {sorted(unknown)}"
        raise BearConfigError(msg)

    allowlist = d.get("qos_allowlist", ["bbshort"])
    if not isinstance(allowlist, list) or not all(isinstance(x, str) for x in allowlist):
        msg = "[guardrails].qos_allowlist must be a list of strings"
        raise BearConfigError(msg)

    walltime = str(d.get("max_walltime", "00:10:00"))
    try:
        parse_walltime_seconds(walltime)
    except DurationError as exc:
        msg = f"[guardrails].max_walltime is not a valid SLURM walltime: {exc}"
        raise BearConfigError(msg) from exc

    return GuardrailConfig(
        qos_allowlist=tuple(allowlist),
        max_walltime=walltime,
        max_concurrent_jobs=int(d.get("max_concurrent_jobs", 2)),
        gpu_hours_budget=float(d.get("gpu_hours_budget", 1.0)),
        require_dry_run=bool(d.get("require_dry_run", False)),
    )


_NOTIFY_KEYS = {"on_done", "on_fail", "command", "webhook_url", "timeout_seconds"}


def _parse_notify(d: dict) -> NotifyConfig:
    if not isinstance(d, dict):
        msg = "[notify] must be a table"
        raise BearConfigError(msg)
    unknown = set(d.keys()) - _NOTIFY_KEYS
    if unknown:
        msg = f"unknown keys in [notify]: {sorted(unknown)}"
        raise BearConfigError(msg)

    command = d.get("command", [])
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        msg = "[notify].command must be a list of strings (an argv)"
        raise BearConfigError(msg)

    webhook = d.get("webhook_url")
    if webhook is not None and not isinstance(webhook, str):
        msg = "[notify].webhook_url must be a string"
        raise BearConfigError(msg)

    timeout = d.get("timeout_seconds", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        msg = "[notify].timeout_seconds must be a positive number"
        raise BearConfigError(msg)

    return NotifyConfig(
        on_done=bool(d.get("on_done", True)),
        on_fail=bool(d.get("on_fail", True)),
        command=tuple(command),
        webhook_url=webhook,
        timeout_seconds=float(timeout),
    )


def config_path_from_env() -> Path | None:
    """Optional override: ``BEAR_HARNESS_CONFIG`` env var points to a bear.toml."""
    raw = os.environ.get("BEAR_HARNESS_CONFIG")
    return Path(raw) if raw else None


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "BearConfig",
    "BearConfigError",
    "GuardrailConfig",
    "LocalConfig",
    "NotifyConfig",
    "OllamaConfig",
    "SlurmConfig",
    "config_path_from_env",
    "default_guardrails",
    "default_local_config",
    "default_notify",
    "load_bear_config",
]
