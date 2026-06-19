"""The JobGraph contract — the closed vocabulary the preset-agnostic kernel honours.

This is the foundational contract of the corpus, specified in
``docs/internal/specs/01-foundational-contract.md`` §3. A **JobGraph**
is a set of jobs, a set of
edges over them, the publish/consume record-flows between them, and per-job roles —
*nothing more*. A **preset** lowers a workload to exactly this data; the **kernel**
realises that data on SLURM and tracks it as filesystem-attached state keyed by
``run_id``. The contract is *closed* (this fixed vocabulary) and presets are *open*
(any number, authored without kernel changes). That asymmetry is the architecture
(``docs/decision-notes/first-decision.md``).

This module is pure data + structural validation: no SLURM, no I/O, no workload
knowledge. The vLLM realisation strings (QoS tiers, GRES forms) are cited from
``references/bluebear-platform.md`` by the *preset* that builds a graph, never
recalled here — this module only carries the BlueBEAR-shaped request, it does not
mint cluster strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bear_harness._duration import gpu_count_from_gres

__all__ = [
    "Edge",
    "EdgeKind",
    "Job",
    "JobGraph",
    "JobGraphError",
    "Record",
    "Resources",
    "Role",
    "Topology",
]


class JobGraphError(ValueError):
    """Raised when a :class:`JobGraph` violates a structural invariant.

    Structural means: a malformed graph the kernel must refuse *before* it touches
    SLURM — an edge naming a non-existent job, a duplicate job name, a cycle, or a
    consumed record that nothing publishes. It does NOT mean a resource-policy
    breach: that is the guardrail layer's job (``_guardrails.py``), which inspects
    the same ``Resources`` after the graph is known to be well-formed.
    """


class EdgeKind(Enum):
    """How a downstream job waits on its upstream — realised as sbatch ``--dependency``.

    The distinction is load-bearing (a real bug already hit it): the reference
    server→worker edge is ``AFTER`` (start once the server has left the submit
    queue / is running), **not** ``AFTEROK`` — a ``role=sidecar`` server never
    "succeeds" in SLURM's sense, it gets scancelled, so an ``afterok`` worker
    would never start. Reserve ``AFTEROK`` for genuine success-gated chains.
    See ``references/slurm-cli.md`` for the exact flag syntax.
    """

    AFTER = "after"  # start once the dep LEAVES the queue (success or not)
    AFTEROK = "afterok"  # start only if the dep SUCCEEDED


class Role(Enum):
    """A job's lifecycle role — the contract's one piece of lifecycle knowledge.

    ``WORKER`` runs to completion. ``SIDECAR`` is a server that exists only to
    serve its consumers; the kernel ``scancel``s it once every job that consumes
    its published record has finished. The kernel knows a sidecar must be torn
    down — not what it serves.
    """

    WORKER = "worker"
    SIDECAR = "sidecar"


class Topology:
    """The four shapes a JobGraph can take — *derived* from (jobs, edges, roles).

    A topology is never declared by a preset; it falls out of the data, so a new
    topology-shaped workload is a new arrangement of the *same* vocabulary, not a
    new kernel branch. Kept as string constants (mirroring ``JobState``) so the
    values serialise straight into ``as_dict()`` without an enum dance.
    """

    SINGLE = "single"  # one job, no edges, no array
    BUNDLE = "bundle"  # one job fanned across a SLURM array (Resources.array set)
    COUPLED = "coupled"  # a role=sidecar server + worker(s) — the reference vLLM shape
    DAG = "dag"  # the general directed-acyclic case; the others are constrained instances


@dataclass(frozen=True, slots=True)
class Resources:
    """A job's BlueBEAR-shaped resource request — minimal and cluster-named.

    Per ``docs/decision-notes/bluebear-only.md`` there is no portable subset: the
    fields are SLURM/BlueBEAR strings (``references/bluebear-platform.md``). Every
    field is optional with a ``None`` default so a CPU-only worker (no ``gres``)
    and a pure-local job (nothing reserved) are both expressible — and a local
    request, carrying no ``qos``/``walltime`` and zero GPUs, passes every
    guardrail cap. ``array`` is the lone ``bundle``-topology hook (a SLURM array
    spec such as ``"0-9"``); ``None`` means "not an array".

    The exact field set resolves spec open-question 10.1 (W3). Environment-level
    strings the *realiser* supplies (account, CUDA module, apptainer image, RDS
    paths, tensor-parallel size) are deliberately NOT here — they are not per-job
    *requests*, they are how the one cluster is wired.
    """

    qos: str | None = None
    walltime: str | None = None
    gres: str | None = None
    cpus_per_task: int | None = None
    mem_gb: int | None = None
    array: str | None = None

    @property
    def gpu_count(self) -> int:
        """GPUs this request reserves — reuses the shared GRES parser, never re-derived."""
        return gpu_count_from_gres(self.gres or "")

    def as_dict(self) -> dict:
        return {
            "qos": self.qos,
            "walltime": self.walltime,
            "gres": self.gres,
            "cpus_per_task": self.cpus_per_task,
            "mem_gb": self.mem_gb,
            "array": self.array,
        }


@dataclass(frozen=True, slots=True)
class Record:
    """A typed record a job drops on the shared FS for a downstream job to read as env.

    The data channel, orthogonal to edges: a publisher writes ``filename`` under
    the ``run_id`` directory; a consumer reads it as ``env_var``. The reference
    flow's ``endpoint`` record is ``Record("endpoint", "endpoint.json",
    "MODEL_BASE_URL")`` — the OpenAI-compatible server *root* URL (no ``/v1``
    suffix; ``references/vllm-serve-api.md``).
    """

    name: str  # logical name, e.g. "endpoint"
    filename: str  # where it lands under the run_id dir, e.g. "endpoint.json"
    env_var: str  # how a consumer reads it, e.g. "MODEL_BASE_URL"

    def as_dict(self) -> dict:
        return {"name": self.name, "filename": self.filename, "env_var": self.env_var}


@dataclass(frozen=True, slots=True)
class Edge:
    """An ordering constraint between two jobs, realised as sbatch ``--dependency``."""

    upstream: str  # Job.name the downstream waits on
    downstream: str  # Job.name that waits
    kind: EdgeKind

    def as_dict(self) -> dict:
        return {"upstream": self.upstream, "downstream": self.downstream, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class Job:
    """A unit of work — realised as one sbatch submission.

    Carries only contract metadata: a ``name`` (unique within the graph), its
    BlueBEAR-shaped :class:`Resources`, its :class:`Role`, and the records it
    ``publishes`` / ``consumes``. It deliberately holds no ``model``, no
    ``endpoint_route``, no workload-named field — such a field appearing here is
    the smoke signal that the abstraction is leaking (spec §5).
    """

    name: str
    resources: Resources
    role: Role = Role.WORKER
    publishes: tuple[Record, ...] = ()
    consumes: tuple[Record, ...] = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "resources": self.resources.as_dict(),
            "role": self.role.value,
            "publishes": [r.as_dict() for r in self.publishes],
            "consumes": [r.as_dict() for r in self.consumes],
        }


@dataclass(frozen=True, slots=True)
class JobGraph:
    """A set of jobs, the edges over them, and the record-flows along them — the wire.

    The single thing that crosses the kernel↔preset boundary: a preset's only
    output, the kernel's only input. ``topology`` is derived, never declared;
    ``validate()`` enforces the structural invariants the kernel refuses to submit
    without. Frozen + slots (rule-output discipline); ``as_dict()`` is
    deterministic so a graph round-trips for logging and comparison.
    """

    jobs: tuple[Job, ...]
    edges: tuple[Edge, ...] = ()

    def job(self, name: str) -> Job:
        """Return the job named ``name`` or raise :class:`JobGraphError`."""
        for j in self.jobs:
            if j.name == name:
                return j
        raise JobGraphError(f"no job named {name!r} in graph")

    @property
    def topology(self) -> str:
        """Derive the shape from (jobs, edges, roles) — one of :class:`Topology`.

        A lone job is ``SINGLE`` (or ``BUNDLE`` if it carries an array spec). Any
        ``role=sidecar`` present makes it ``COUPLED`` (the reference vLLM+pipeline
        shape). Everything else is the general ``DAG``.
        """
        if len(self.jobs) == 1 and not self.edges:
            return Topology.BUNDLE if self.jobs[0].resources.array else Topology.SINGLE
        if any(j.role is Role.SIDECAR for j in self.jobs):
            return Topology.COUPLED
        return Topology.DAG

    def validate(self) -> None:
        """Refuse a structurally malformed graph *before* the kernel touches SLURM.

        Enforces: at least one job; unique job names; every edge endpoint names a
        real job and is not a self-loop; the edge set is acyclic (it is a DAG); and
        every consumed record is published by some job (the data channel is
        connected). Raises :class:`JobGraphError` on the first violation it proves.
        """
        if not self.jobs:
            raise JobGraphError("a JobGraph must contain at least one job")

        names = [j.name for j in self.jobs]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise JobGraphError(f"duplicate job names: {duplicates}")
        known = set(names)

        for e in self.edges:
            if e.upstream not in known:
                raise JobGraphError(f"edge upstream {e.upstream!r} is not a job in the graph")
            if e.downstream not in known:
                raise JobGraphError(f"edge downstream {e.downstream!r} is not a job in the graph")
            if e.upstream == e.downstream:
                raise JobGraphError(f"edge is a self-loop on {e.upstream!r}")

        self._check_acyclic()
        self._check_records_connected()

    def as_dict(self) -> dict:
        return {
            "topology": self.topology,
            "jobs": [j.as_dict() for j in self.jobs],
            "edges": [e.as_dict() for e in self.edges],
        }

    # ------------------------------------------------------------------
    # Internal invariant checks
    # ------------------------------------------------------------------

    def _check_acyclic(self) -> None:
        """Kahn's algorithm: a graph with a cycle cannot drain to zero in-degree."""
        indegree = {j.name: 0 for j in self.jobs}
        adjacency: dict[str, list[str]] = {j.name: [] for j in self.jobs}
        for e in self.edges:
            adjacency[e.upstream].append(e.downstream)
            indegree[e.downstream] += 1

        ready = [n for n, d in indegree.items() if d == 0]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for nxt in adjacency[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)

        if visited != len(self.jobs):
            raise JobGraphError("JobGraph has a cycle")

    def _check_records_connected(self) -> None:
        published = {r.name for j in self.jobs for r in j.publishes}
        for j in self.jobs:
            for r in j.consumes:
                if r.name not in published:
                    raise JobGraphError(
                        f"job {j.name!r} consumes record {r.name!r} that no job publishes"
                    )
