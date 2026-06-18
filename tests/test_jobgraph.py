"""Tests for the JobGraph contract vocabulary.

The contract is specs/01-foundational-contract.md §3 — the closed vocabulary the
preset-agnostic kernel honours. These tests pin the dataclass shapes, the derived
topology, the structural invariants enforced by ``validate()``, and the
deterministic ``as_dict()`` serialisation. Pure data, no cluster.
"""

from __future__ import annotations

import pytest

from bear_harness._jobgraph import (
    Edge,
    EdgeKind,
    Job,
    JobGraph,
    JobGraphError,
    Record,
    Resources,
    Role,
    Topology,
)

ENDPOINT = Record(name="endpoint", filename="endpoint.json", env_var="MODEL_BASE_URL")


def _gpu_res() -> Resources:
    return Resources(qos="bbgpu", walltime="00:10:00", gres="gpu:a100:1", cpus_per_task=8, mem_gb=64)


def _cpu_res() -> Resources:
    return Resources(qos="bbshort", walltime="00:10:00", cpus_per_task=4, mem_gb=16)


def _coupled() -> JobGraph:
    """The reference coupled shape: a sidecar server + a worker joined by an after-edge."""
    server = Job(name="server", resources=_gpu_res(), role=Role.SIDECAR, publishes=(ENDPOINT,))
    worker = Job(name="worker", resources=_cpu_res(), consumes=(ENDPOINT,))
    return JobGraph(jobs=(server, worker), edges=(Edge("server", "worker", EdgeKind.AFTER),))


class TestResources:
    def test_defaults_all_none(self):
        r = Resources()
        assert (r.qos, r.walltime, r.gres) == (None, None, None)
        assert (r.cpus_per_task, r.mem_gb, r.array) == (None, None, None)
        assert r.gpu_count == 0

    def test_gpu_count_reuses_duration_parser(self):
        assert _gpu_res().gpu_count == 1
        assert Resources(gres="gpu:a100:2").gpu_count == 2
        assert _cpu_res().gpu_count == 0

    def test_as_dict(self):
        assert _cpu_res().as_dict() == {
            "qos": "bbshort",
            "walltime": "00:10:00",
            "gres": None,
            "cpus_per_task": 4,
            "mem_gb": 16,
            "array": None,
        }

    def test_frozen(self):
        with pytest.raises(AttributeError):
            _cpu_res().qos = "bbgpu"  # type: ignore[misc]


class TestRecord:
    def test_as_dict(self):
        assert ENDPOINT.as_dict() == {
            "name": "endpoint",
            "filename": "endpoint.json",
            "env_var": "MODEL_BASE_URL",
        }


class TestEdge:
    def test_kinds(self):
        assert EdgeKind.AFTER.value == "after"
        assert EdgeKind.AFTEROK.value == "afterok"

    def test_as_dict_serialises_kind_to_value(self):
        assert Edge("server", "worker", EdgeKind.AFTER).as_dict() == {
            "upstream": "server",
            "downstream": "worker",
            "kind": "after",
        }


class TestJob:
    def test_defaults_worker_no_records(self):
        j = Job(name="w", resources=_cpu_res())
        assert j.role is Role.WORKER
        assert j.publishes == ()
        assert j.consumes == ()

    def test_as_dict(self):
        j = Job(name="server", resources=_gpu_res(), role=Role.SIDECAR, publishes=(ENDPOINT,))
        d = j.as_dict()
        assert d["name"] == "server"
        assert d["role"] == "sidecar"
        assert d["publishes"] == [ENDPOINT.as_dict()]
        assert d["consumes"] == []
        assert d["resources"]["gres"] == "gpu:a100:1"


class TestTopology:
    def test_single(self):
        assert JobGraph(jobs=(Job("solo", _cpu_res()),)).topology == Topology.SINGLE

    def test_bundle_is_a_lone_array_job(self):
        j = Job("sweep", Resources(qos="bbshort", walltime="00:10:00", array="0-9"))
        assert JobGraph(jobs=(j,)).topology == Topology.BUNDLE

    def test_coupled_has_a_sidecar(self):
        assert _coupled().topology == Topology.COUPLED

    def test_dag_is_the_general_case(self):
        a = Job("a", _cpu_res())
        b = Job("b", _cpu_res())
        g = JobGraph(jobs=(a, b), edges=(Edge("a", "b", EdgeKind.AFTEROK),))
        assert g.topology == Topology.DAG


class TestLookup:
    def test_job_by_name(self):
        assert _coupled().job("worker").name == "worker"

    def test_missing_job_raises(self):
        with pytest.raises(JobGraphError):
            _coupled().job("ghost")


class TestValidate:
    def test_valid_coupled_passes(self):
        _coupled().validate()  # must not raise

    def test_empty_graph_rejected(self):
        with pytest.raises(JobGraphError):
            JobGraph(jobs=()).validate()

    def test_duplicate_names_rejected(self):
        g = JobGraph(jobs=(Job("dup", _cpu_res()), Job("dup", _cpu_res())))
        with pytest.raises(JobGraphError):
            g.validate()

    def test_edge_to_unknown_job_rejected(self):
        g = JobGraph(jobs=(Job("a", _cpu_res()),), edges=(Edge("a", "ghost", EdgeKind.AFTER),))
        with pytest.raises(JobGraphError):
            g.validate()

    def test_self_loop_rejected(self):
        g = JobGraph(jobs=(Job("a", _cpu_res()),), edges=(Edge("a", "a", EdgeKind.AFTER),))
        with pytest.raises(JobGraphError):
            g.validate()

    def test_cycle_rejected(self):
        a = Job("a", _cpu_res())
        b = Job("b", _cpu_res())
        g = JobGraph(
            jobs=(a, b),
            edges=(Edge("a", "b", EdgeKind.AFTER), Edge("b", "a", EdgeKind.AFTER)),
        )
        with pytest.raises(JobGraphError):
            g.validate()

    def test_consume_without_publisher_rejected(self):
        worker = Job("worker", _cpu_res(), consumes=(ENDPOINT,))
        with pytest.raises(JobGraphError):
            JobGraph(jobs=(worker,)).validate()


class TestSerialisation:
    def test_graph_as_dict(self):
        d = _coupled().as_dict()
        assert d["topology"] == Topology.COUPLED
        assert [j["name"] for j in d["jobs"]] == ["server", "worker"]
        assert d["edges"] == [{"upstream": "server", "downstream": "worker", "kind": "after"}]
