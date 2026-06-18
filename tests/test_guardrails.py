"""Unit tests for the pure default-deny guardrail rule engine (``_guardrails``).

No cluster, no sbatch — the engine is a pure function over a resource request and
the configured caps. The load-bearing properties verified here:

- each cap denies and names itself + the key to widen,
- ALL violations are reported (no short-circuit),
- a pure-local request (no qos / no gres) passes even the tight leash,
- the decision is frozen and round-trips through ``as_dict``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from bear_harness._bear_config import GuardrailConfig
from bear_harness._guardrails import (
    GuardrailDecision,
    GuardrailViolation,
    ResourceRequest,
    evaluate_guardrails,
    resource_request_from_graph,
)
from bear_harness._jobgraph import Edge, EdgeKind, Job, JobGraph, Resources, Role


def _cfg(**overrides: object) -> GuardrailConfig:
    """A GuardrailConfig starting from the tight default, with overrides."""
    return replace(GuardrailConfig(), **overrides)


def _coupled_graph() -> JobGraph:
    server = Job(
        "vllm", Resources(qos="bbgpu", walltime="00:10:00", gres="gpu:a100:1"), role=Role.SIDECAR
    )
    worker = Job("pipeline", Resources(qos="bbdefault", walltime="00:10:00"), role=Role.WORKER)
    return JobGraph(jobs=(server, worker), edges=(Edge("vllm", "pipeline", EdgeKind.AFTER),))


def _etl_graph() -> JobGraph:
    job = Job("etl", Resources(qos="bbdefault", walltime="00:10:00"), role=Role.WORKER)
    return JobGraph(jobs=(job,))


class TestResourceRequestFromGraph:
    """The W4 graph-derived request: a model-less graph is CPU-checked, not
    phantom-GPU-checked; the reference coupled graph binds on its GPU server."""

    def test_coupled_binds_on_the_gpu_server(self):
        req = resource_request_from_graph(_coupled_graph())
        assert req.qos == "bbgpu"  # the GPU (sidecar) job is the binding one
        assert req.gpu_count == 1
        assert req.jobs_in_launch == 2

    def test_etl_reserves_no_gpu(self):
        req = resource_request_from_graph(_etl_graph())
        assert req.qos == "bbdefault"
        assert req.gpu_count == 0
        assert req.jobs_in_launch == 1

    def test_in_flight_is_threaded(self):
        req = resource_request_from_graph(_etl_graph(), concurrent_jobs_in_flight=3)
        assert req.concurrent_jobs_in_flight == 3


class TestEvaluateGuardrails:
    def test_qos_not_in_allowlist_denied(self) -> None:
        req = ResourceRequest(qos="bbgpu", walltime="00:10:00", gpu_count=1)
        decision = evaluate_guardrails(req, GuardrailConfig())  # allowlist=(bbshort,)
        assert decision.allowed is False
        viol = next(v for v in decision.violations if v.cap == "qos_allowlist")
        assert "qos_allowlist" in viol.config_key  # names the key to widen
        assert "bbgpu" in viol.message

    def test_qos_in_allowlist_allowed(self) -> None:
        req = ResourceRequest(qos="bbshort", walltime="00:10:00", gpu_count=1)
        decision = evaluate_guardrails(req, GuardrailConfig())
        assert decision.allowed is True
        assert decision.violations == ()

    def test_walltime_over_ceiling_denied(self) -> None:
        cfg = _cfg(qos_allowlist=("bbgpu",), max_walltime="00:10:00", gpu_hours_budget=100.0)
        req = ResourceRequest(qos="bbgpu", walltime="01:00:00", gpu_count=1)
        decision = evaluate_guardrails(req, cfg)
        assert decision.allowed is False
        assert any(v.cap == "max_walltime" for v in decision.violations)

    def test_gpu_hours_over_budget_denied(self) -> None:
        cfg = _cfg(qos_allowlist=("bbgpu",), max_walltime="10:00:00", gpu_hours_budget=1.0)
        # 2 GPUs * 1h = 2.0 GPU-hours > 1.0 budget
        req = ResourceRequest(qos="bbgpu", walltime="01:00:00", gpu_count=2)
        decision = evaluate_guardrails(req, cfg)
        assert decision.allowed is False
        assert any(v.cap == "gpu_hours_budget" for v in decision.violations)
        assert decision.est_gpu_hours == pytest.approx(2.0)

    def test_concurrency_over_cap_denied(self) -> None:
        cfg = _cfg(qos_allowlist=("bbshort",), max_concurrent_jobs=2)
        req = ResourceRequest(
            qos="bbshort",
            walltime="00:10:00",
            gpu_count=1,
            jobs_in_launch=2,
            concurrent_jobs_in_flight=2,  # cluster already busy
        )
        decision = evaluate_guardrails(req, cfg)
        assert decision.allowed is False
        assert any(v.cap == "max_concurrent_jobs" for v in decision.violations)

    def test_multiple_violations_all_reported(self) -> None:
        """A request breaching three caps reports all three — no short-circuit."""
        cfg = _cfg(qos_allowlist=("bbshort",), max_walltime="00:10:00", gpu_hours_budget=0.1)
        req = ResourceRequest(qos="bbgpu", walltime="01:00:00", gpu_count=2)
        decision = evaluate_guardrails(req, cfg)
        caps = {v.cap for v in decision.violations}
        assert {"qos_allowlist", "max_walltime", "gpu_hours_budget"} <= caps

    def test_pure_local_request_passes_the_tight_leash(self) -> None:
        """No qos, no gres => reserves nothing => allowed even under defaults."""
        decision = evaluate_guardrails(ResourceRequest(), GuardrailConfig())
        assert decision.allowed is True
        assert decision.est_gpu_hours == 0.0

    def test_reason_summarises_violations(self) -> None:
        req = ResourceRequest(qos="bbgpu", walltime="00:10:00", gpu_count=1)
        decision = evaluate_guardrails(req, GuardrailConfig())
        assert "qos" in decision.reason().lower()
        # the allowed case is a clean one-liner
        assert evaluate_guardrails(ResourceRequest(), GuardrailConfig()).reason() == ("within caps")


class TestDecisionShape:
    def test_decision_is_frozen(self) -> None:
        decision = evaluate_guardrails(ResourceRequest(), GuardrailConfig())
        with pytest.raises(FrozenInstanceError):
            decision.allowed = False  # type: ignore[misc]

    def test_violation_is_frozen(self) -> None:
        viol = GuardrailViolation("c", "r", "a", "k", "m")
        with pytest.raises(FrozenInstanceError):
            viol.cap = "x"  # type: ignore[misc]

    def test_as_dict_round_trips(self) -> None:
        req = ResourceRequest(qos="bbgpu", walltime="01:00:00", gpu_count=2)
        payload = evaluate_guardrails(req, GuardrailConfig()).as_dict()
        assert payload["allowed"] is False
        assert isinstance(payload["violations"], list)
        assert payload["violations"][0]["config_key"]
        assert payload["est_gpu_hours"] == pytest.approx(2.0)

    def test_decision_type(self) -> None:
        assert isinstance(
            evaluate_guardrails(ResourceRequest(), GuardrailConfig()), GuardrailDecision
        )


class TestResourceRequestFromSlurm:
    def test_from_slurm_parses_gres_gpu_count(self) -> None:
        req = ResourceRequest.from_slurm(qos="bbgpu", walltime="01:00:00", gres="gpu:a100_80:2")
        assert req.gpu_count == 2
        assert req.qos == "bbgpu"

    def test_from_slurm_no_gres_is_zero_gpus(self) -> None:
        req = ResourceRequest.from_slurm(qos="bbcpu", walltime="01:00:00", gres=None)
        assert req.gpu_count == 0
