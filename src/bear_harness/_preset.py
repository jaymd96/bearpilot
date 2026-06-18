"""The preset extension point — the open side of the closed contract.

The kernel honours the JobGraph contract (``_jobgraph.py``) and nothing else; a
*preset* is the open extension point that lowers a workload to that contract and
realises it on a runner. The kernel selects a preset **by name** from a registry and
never branches on a workload — that asymmetry (closed contract, open presets) is the
architecture (``docs/decision-notes/first-decision.md``). Authoring is
declarative-first (``docs/decision-notes/declarative-presets-first.md``).

A preset provides four things:

- ``lower(context)`` → a :class:`~bear_harness._jobgraph.JobGraph` (the contract data:
  jobs + edges + records + roles);
- ``make_backend(context, runner)`` → a :class:`Backend` the generic kernel walker
  calls to submit each job (and to find the worker's status file);
- ``validate_manifest(manifest)`` → raises if the manifest lacks what this preset needs
  (the vLLM preset requires ``[model]``; ETL forbids it) — the pre-submit half of the
  authoring kit;
- ``describe()`` → a small dict for ``describe_preset`` / ``list_presets``.

:class:`PresetContext` is the kernel→preset DTO: everything a preset's backend might
need to realise a graph, flattened so a preset never imports the kernel (no import
cycle). Built-in presets self-register at import; the kernel imports their modules only
to populate the registry, then dispatches by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from bear_harness._bear_config import BearConfig
from bear_harness._endpoint_discovery import EndpointRecord
from bear_harness._jobgraph import Job, JobGraph
from bear_harness._manifest import Manifest
from bear_harness._runner import JobHandle, Runner

__all__ = [
    "Backend",
    "Preset",
    "PresetContext",
    "PresetError",
    "get_preset",
    "list_presets",
    "register_preset",
]


class PresetError(ValueError):
    """Raised for preset selection / registration / manifest-validation problems."""


@dataclass(frozen=True, slots=True)
class PresetContext:
    """The kernel→preset realisation context — paths + resolved fields + manifest.

    Built by the kernel from its launch options and run-dir layout, then handed to a
    preset's ``lower`` / ``make_backend``. One shared shape for all presets (the vLLM
    preset reads ``model`` / ``endpoint_path`` / ``server_log``; ETL reads
    ``output_dir`` / ``worker_log``), so a preset depends only on this DTO, never on the
    kernel's internals.

    ``overrides`` is the per-launch SLURM-overrides object the runner reads via
    ``getattr`` (the ``Runner.submit_vllm`` ``**kwargs`` contract); typed ``object`` so
    the preset layer stays decoupled from where it is defined.
    """

    manifest: Manifest
    config: BearConfig
    job_id: str
    run_dir: Path
    output_dir: Path
    server_log: Path
    worker_log: Path
    endpoint_path: Path
    python: str
    model: str = ""
    overrides: object | None = None
    boot_timeout_seconds: float = 900.0


@runtime_checkable
class Backend(Protocol):
    """Realise a graph's jobs on a runner — the per-job submission the kernel walker calls."""

    def submit(
        self, job: Job, records: Mapping[str, EndpointRecord], depends_on: tuple[JobHandle, ...]
    ) -> JobHandle: ...

    def status_file(self, default: Path) -> Path: ...


@runtime_checkable
class Preset(Protocol):
    """The open extension point: lower a workload to the contract and realise it."""

    name: str

    def lower(self, context: PresetContext) -> JobGraph: ...

    def make_backend(self, context: PresetContext, runner: Runner) -> Backend: ...

    def validate_manifest(self, manifest: Manifest) -> None: ...

    def describe(self) -> dict: ...


# ---------------------------------------------------------------------------
# Registry — import-to-register; the kernel dispatches by name, never branches.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Preset] = {}


def register_preset(preset: Preset) -> None:
    """Register a preset under its ``name``. Raises on a duplicate name."""
    if preset.name in _REGISTRY:
        msg = f"a preset named {preset.name!r} is already registered"
        raise PresetError(msg)
    _REGISTRY[preset.name] = preset


def get_preset(name: str) -> Preset:
    """Look up a registered preset by name, or raise :class:`PresetError`."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        msg = f"unknown preset {name!r}; registered: {known}"
        raise PresetError(msg) from None


def list_presets() -> tuple[str, ...]:
    """The registered preset names, sorted."""
    return tuple(sorted(_REGISTRY))
