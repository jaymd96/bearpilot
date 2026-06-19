"""The pure default-deny guardrail rule engine.

Given the effective SLURM resource request a launch *would* reserve and the
configured caps, decide — BEFORE any sbatch — whether it is allowed, and if not
say exactly which cap was breached and which ``bear.toml`` key to widen. The
engine is side-effect-free and every output is a frozen dataclass (rule-output
discipline), so it is trivially testable without a cluster and is called
identically from two places: the kernel at submit (authoritative, un-bypassable)
and the CLI dry-run surface (advisory, for agent feedback).

It governs RESOURCES only — QoS / walltime / GPU-hours / concurrency — and never
inspects the science (model, prompts, campaign size). See
``docs/decision-notes/default-deny-guardrails.md``.

Local runs reserve no SLURM resources: a :class:`ResourceRequest` with no qos,
no walltime and ``gpu_count == 0`` passes every cap. The guardrail binds on
cluster submissions, not laptop subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass

from bear_harness._bear_config import BearConfig, GuardrailConfig
from bear_harness._duration import gpu_count_from_gres, parse_walltime_seconds
from bear_harness._jobgraph import JobGraph

__all__ = [
    "GuardrailDecision",
    "GuardrailViolation",
    "ResourceRequest",
    "evaluate_guardrails",
    "resource_request_for",
    "resource_request_from_graph",
]


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """The effective SLURM resources a launch *would* reserve.

    Built from the launch's effective qos / walltime / gres (override or config)
    plus the live concurrency probe. A pure-local run reserves nothing: ``qos``
    and ``walltime`` are ``None`` and ``gpu_count`` is 0, so every cap passes.
    """

    qos: str | None = None
    walltime: str | None = None
    gpu_count: int = 0
    jobs_in_launch: int = 2  # the reference vLLM+pipeline flow submits two jobs
    concurrent_jobs_in_flight: int = 0

    @classmethod
    def from_slurm(
        cls,
        *,
        qos: str | None,
        walltime: str | None,
        gres: str | None,
        jobs_in_launch: int = 2,
        concurrent_jobs_in_flight: int = 0,
    ) -> ResourceRequest:
        """Build a request from raw SLURM strings, parsing the GRES GPU count."""
        return cls(
            qos=qos,
            walltime=walltime,
            gpu_count=gpu_count_from_gres(gres or ""),
            jobs_in_launch=jobs_in_launch,
            concurrent_jobs_in_flight=concurrent_jobs_in_flight,
        )


@dataclass(frozen=True, slots=True)
class GuardrailViolation:
    """One breached cap, with the value, the limit, and the key to widen."""

    cap: str
    requested: str
    allowed: str
    config_key: str
    message: str

    def as_dict(self) -> dict:
        return {
            "cap": self.cap,
            "requested": self.requested,
            "allowed": self.allowed,
            "config_key": self.config_key,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    """The verdict: allowed, the (possibly several) violations, and the estimate."""

    allowed: bool
    violations: tuple[GuardrailViolation, ...] = ()
    est_gpu_hours: float = 0.0

    def reason(self) -> str:
        """A one-line human summary naming every breached cap + key to widen."""
        if self.allowed:
            return "within caps"
        return "; ".join(v.message for v in self.violations)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "est_gpu_hours": round(self.est_gpu_hours, 4),
            "violations": [v.as_dict() for v in self.violations],
        }


def evaluate_guardrails(request: ResourceRequest, config: GuardrailConfig) -> GuardrailDecision:
    """Evaluate every cap and return the verdict — reporting ALL violations.

    Deliberately does NOT short-circuit: an autonomous agent should see every
    wall it hit in one pass, not discover them one widening at a time.
    """
    violations: list[GuardrailViolation] = []

    # Reservation-ceiling GPU-hours estimate — what the job COULD burn if it ran
    # to walltime (the cluster bills the reservation, so capping the ceiling is
    # the right pre-submit lever; cf. "prevention beats reaction" in the note).
    est_gpu_hours = 0.0
    if request.walltime is not None and request.gpu_count > 0:
        wall_hours = parse_walltime_seconds(request.walltime) / 3600.0
        est_gpu_hours = request.gpu_count * wall_hours

    # 1. QoS allowlist (default-deny) — only binds when a tier is actually asked for.
    if request.qos and request.qos not in config.qos_allowlist:
        permitted = ", ".join(config.qos_allowlist) or "(none)"
        violations.append(
            GuardrailViolation(
                cap="qos_allowlist",
                requested=request.qos,
                allowed=permitted,
                config_key="[guardrails].qos_allowlist",
                message=(
                    f"QoS {request.qos!r} is not allowed (permitted: {permitted}); "
                    "widen [guardrails].qos_allowlist"
                ),
            )
        )

    # 2. Walltime ceiling.
    if request.walltime is not None:
        requested_s = parse_walltime_seconds(request.walltime)
        ceiling_s = parse_walltime_seconds(config.max_walltime)
        if requested_s > ceiling_s:
            violations.append(
                GuardrailViolation(
                    cap="max_walltime",
                    requested=request.walltime,
                    allowed=config.max_walltime,
                    config_key="[guardrails].max_walltime",
                    message=(
                        f"walltime {request.walltime} exceeds the ceiling "
                        f"{config.max_walltime}; widen [guardrails].max_walltime"
                    ),
                )
            )

    # 3. GPU-hours budget.
    if est_gpu_hours > config.gpu_hours_budget:
        violations.append(
            GuardrailViolation(
                cap="gpu_hours_budget",
                requested=f"{est_gpu_hours:.3g}",
                allowed=f"{config.gpu_hours_budget:.3g}",
                config_key="[guardrails].gpu_hours_budget",
                message=(
                    f"estimated {est_gpu_hours:.3g} GPU-hours exceeds the budget "
                    f"{config.gpu_hours_budget:.3g}; widen [guardrails].gpu_hours_budget"
                ),
            )
        )

    # 4. Concurrency cap.
    needed = request.concurrent_jobs_in_flight + request.jobs_in_launch
    if needed > config.max_concurrent_jobs:
        violations.append(
            GuardrailViolation(
                cap="max_concurrent_jobs",
                requested=str(needed),
                allowed=str(config.max_concurrent_jobs),
                config_key="[guardrails].max_concurrent_jobs",
                message=(
                    f"{needed} concurrent jobs "
                    f"({request.concurrent_jobs_in_flight} in flight + "
                    f"{request.jobs_in_launch} this launch) exceeds the cap "
                    f"{config.max_concurrent_jobs}; widen [guardrails].max_concurrent_jobs"
                ),
            )
        )

    return GuardrailDecision(
        allowed=not violations,
        violations=tuple(violations),
        est_gpu_hours=est_gpu_hours,
    )


def resource_request_for(
    config: BearConfig,
    *,
    qos_override: str | None = None,
    walltime_override: str | None = None,
    gpu_gres_override: str | None = None,
    jobs_in_launch: int = 2,
    concurrent_jobs_in_flight: int = 0,
) -> ResourceRequest:
    """Build the effective :class:`ResourceRequest` from config + overrides.

    Effective qos / walltime / gres is the per-launch override or, failing that,
    the ``bear.toml`` SLURM default. Local mode has no SLURM defaults, so an
    un-overridden request reserves nothing. Shared by the kernel gate and the
    CLI ``check`` / dry-run surface so they cannot diverge.
    """
    if config.is_slurm:
        slurm = config.require_slurm()
        eff_qos = qos_override or slurm.qos
        eff_walltime = walltime_override or slurm.walltime
        eff_gres = gpu_gres_override or slurm.gpu_gres
    else:
        eff_qos = qos_override
        eff_walltime = walltime_override
        eff_gres = gpu_gres_override
    return ResourceRequest.from_slurm(
        qos=eff_qos,
        walltime=eff_walltime,
        gres=eff_gres,
        jobs_in_launch=jobs_in_launch,
        concurrent_jobs_in_flight=concurrent_jobs_in_flight,
    )


def resource_request_from_graph(
    graph: JobGraph, *, concurrent_jobs_in_flight: int = 0
) -> ResourceRequest:
    """Build the effective :class:`ResourceRequest` from a lowered :class:`JobGraph`.

    The binding job for the GPU-hours estimate and the QoS allowlist is the one
    reserving the most GPUs; with none, the first job (its CPU QoS / walltime still bind
    the allowlist and the walltime ceiling). ``jobs_in_launch`` is the graph's job count.
    A model-less graph (no GPU job) reserves zero GPUs, so an ETL launch is CPU-checked
    rather than phantom-GPU-checked. For the reference coupled graph this yields the same
    request the per-config builder did — the server is the binding job — so the gate is
    byte-compatible there and merely *correct* for a non-GPU preset.
    """
    primary = max(graph.jobs, key=lambda j: j.resources.gpu_count)
    return ResourceRequest.from_slurm(
        qos=primary.resources.qos,
        walltime=primary.resources.walltime,
        gres=primary.resources.gres,
        jobs_in_launch=len(graph.jobs),
        concurrent_jobs_in_flight=concurrent_jobs_in_flight,
    )
