"""Strict loader for ``pipeline.toml`` — the bear-harness program contract.

Every consumer program drops a ``pipeline.toml`` at its repo root. The
harness loads it, validates it hard, and uses it to drive every
subsequent step (provisioning vLLM, building the entrypoint command,
collecting artifacts). The file is the **only** seam between the
harness and a consumer program — no Python imports, no convention-based
discovery, no backdoors.

Schema versioning is explicit: ``schema_version`` is a required string.
This loader accepts ``"1"`` and rejects everything else. Adding new
fields in a backwards-compatible way is fine; breaking changes bump the
version and keep the old loader around behind a dispatch.

Design rules enforced here:

- **Unknown top-level keys are errors**, not warnings. Consumer programs
  should not be able to smuggle arbitrary state through the manifest.
- **Required fields are checked up front.** Missing fields raise a
  single ``ManifestError`` with a path like ``model.default_model``.
- **No side effects.** Loading a manifest does not touch the file
  system beyond reading the one file. Substitution of ``$VAR``
  placeholders happens in a separate module at launch time.

The dataclasses here are frozen and deliberately simple — they exist
to structure the parsed TOML, not to carry business logic.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})

SUPPORTED_MODEL_APIS = frozenset({"anthropic_messages", "openai_chat"})

SUPPORTED_STATUS_MODES = frozenset({"file", "log", "none"})


class ManifestError(ValueError):
    """Raised for any structural problem with a ``pipeline.toml`` file.

    Always carries a human-readable path (e.g. ``model.default_model``)
    so operators can fix the manifest without grepping through the
    loader source.
    """


# ---------------------------------------------------------------------------
# Dataclass types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgramInfo:
    name: str
    version: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    python: str  # PEP 440-ish spec, e.g. ">=3.11,<4"
    install: tuple[str, ...] = ()
    prepare: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelSpec:
    api: str
    default_model: str
    min_context_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class EntrypointSpec:
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StatusSpec:
    mode: str = "file"
    file: str = "$OUTPUT_DIR/.bear-harness-status.json"
    heartbeat_s: int = 600


@dataclass(frozen=True, slots=True)
class ArtifactsSpec:
    collect: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourcesSpec:
    gpu_memory_gb: int = 40
    cpu_cores: int = 4
    ram_gb: int = 32
    walltime: str = "08:00:00"


@dataclass(frozen=True, slots=True)
class Manifest:
    """Parsed ``pipeline.toml`` with the file's origin path attached.

    The ``program_root`` attribute is the directory the manifest lives
    in — i.e. the repo root of the consumer program, used as
    ``$PROGRAM_ROOT`` during substitution.
    """

    schema_version: str
    program: ProgramInfo
    runtime: RuntimeSpec
    model: ModelSpec | None
    entrypoint: EntrypointSpec
    status: StatusSpec
    artifacts: ArtifactsSpec
    resources: ResourcesSpec
    program_root: Path
    preset: str = "vllm-pipeline"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_TOP_LEVEL_KEYS = {
    "schema_version",
    "preset",
    "program",
    "runtime",
    "model",
    "entrypoint",
    "status",
    "artifacts",
    "resources",
}


def load_manifest(path: str | Path) -> Manifest:
    """Load and validate a ``pipeline.toml`` file.

    ``path`` may point at the manifest file itself, or at the directory
    containing it (in which case we append ``pipeline.toml``). The
    resolved parent directory becomes ``Manifest.program_root``.
    """
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        p = p / "pipeline.toml"
    if not p.is_file():
        msg = f"pipeline.toml not found at {p}"
        raise ManifestError(msg)

    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {p}: {exc}"
        raise ManifestError(msg) from exc
    except OSError as exc:
        msg = f"failed to read {p}: {exc}"
        raise ManifestError(msg) from exc

    unknown = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        msg = f"unknown top-level keys in {p}: {sorted(unknown)}"
        raise ManifestError(msg)

    schema_version = _require_str(data, "schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        msg = (
            f"unsupported schema_version {schema_version!r}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
        raise ManifestError(msg)

    program = _parse_program(_require_table(data, "program"))
    runtime = _parse_runtime(_require_table(data, "runtime"))
    # [model] is preset-specific: the vLLM preset requires it, ETL has none. The base
    # loader accepts its absence; the selected preset validates its own sections (W4).
    model = _parse_model(_require_table(data, "model")) if "model" in data else None
    entrypoint = _parse_entrypoint(_require_table(data, "entrypoint"))
    status = _parse_status(data.get("status", {}))
    artifacts = _parse_artifacts(data.get("artifacts", {}))
    resources = _parse_resources(data.get("resources", {}))
    preset = _optional_str(data, "preset", default="vllm-pipeline")

    return Manifest(
        schema_version=schema_version,
        program=program,
        runtime=runtime,
        model=model,
        entrypoint=entrypoint,
        status=status,
        artifacts=artifacts,
        resources=resources,
        program_root=p.parent,
        preset=preset,
    )


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_program(d: dict) -> ProgramInfo:
    _reject_unknown(d, {"name", "version", "description"}, "program")
    return ProgramInfo(
        name=_require_str(d, "program.name", key="name"),
        version=_require_str(d, "program.version", key="version"),
        description=_optional_str(d, "description", default=""),
    )


def _parse_runtime(d: dict) -> RuntimeSpec:
    _reject_unknown(d, {"python", "install", "prepare", "teardown"}, "runtime")
    return RuntimeSpec(
        python=_require_str(d, "runtime.python", key="python"),
        install=_optional_str_tuple(d, "install"),
        prepare=_optional_str_tuple(d, "prepare"),
        teardown=_optional_str_tuple(d, "teardown"),
    )


def _parse_model(d: dict) -> ModelSpec:
    _reject_unknown(
        d,
        {"api", "default_model", "min_context_tokens"},
        "model",
    )
    api = _require_str(d, "model.api", key="api")
    if api not in SUPPORTED_MODEL_APIS:
        msg = (
            f"model.api must be one of {sorted(SUPPORTED_MODEL_APIS)}, got {api!r}"
        )
        raise ManifestError(msg)
    return ModelSpec(
        api=api,
        default_model=_require_str(d, "model.default_model", key="default_model"),
        min_context_tokens=_optional_int(d, "min_context_tokens", default=4096),
    )


def _parse_entrypoint(d: dict) -> EntrypointSpec:
    _reject_unknown(d, {"command", "env"}, "entrypoint")
    command = d.get("command")
    if not isinstance(command, list) or not command:
        msg = "entrypoint.command must be a non-empty array of strings"
        raise ManifestError(msg)
    for i, part in enumerate(command):
        if not isinstance(part, str):
            msg = f"entrypoint.command[{i}] must be a string, got {type(part).__name__}"
            raise ManifestError(msg)
    env = d.get("env", {})
    if not isinstance(env, dict):
        msg = f"entrypoint.env must be a table, got {type(env).__name__}"
        raise ManifestError(msg)
    for k, v in env.items():
        if not isinstance(v, str):
            msg = f"entrypoint.env.{k} must be a string, got {type(v).__name__}"
            raise ManifestError(msg)
    return EntrypointSpec(command=tuple(command), env=dict(env))


def _parse_status(d: dict) -> StatusSpec:
    _reject_unknown(d, {"mode", "file", "heartbeat_s"}, "status")
    mode = _optional_str(d, "mode", default="file")
    if mode not in SUPPORTED_STATUS_MODES:
        msg = f"status.mode must be one of {sorted(SUPPORTED_STATUS_MODES)}, got {mode!r}"
        raise ManifestError(msg)
    return StatusSpec(
        mode=mode,
        file=_optional_str(d, "file", default="$OUTPUT_DIR/.bear-harness-status.json"),
        heartbeat_s=_optional_int(d, "heartbeat_s", default=600),
    )


def _parse_artifacts(d: dict) -> ArtifactsSpec:
    _reject_unknown(d, {"collect"}, "artifacts")
    return ArtifactsSpec(collect=_optional_str_tuple(d, "collect"))


def _parse_resources(d: dict) -> ResourcesSpec:
    _reject_unknown(
        d,
        {"gpu_memory_gb", "cpu_cores", "ram_gb", "walltime"},
        "resources",
    )
    return ResourcesSpec(
        gpu_memory_gb=_optional_int(d, "gpu_memory_gb", default=40),
        cpu_cores=_optional_int(d, "cpu_cores", default=4),
        ram_gb=_optional_int(d, "ram_gb", default=32),
        walltime=_optional_str(d, "walltime", default="08:00:00"),
    )


# ---------------------------------------------------------------------------
# Primitive validators
# ---------------------------------------------------------------------------


def _require_table(data: dict, section: str) -> dict:
    sub = data.get(section)
    if not isinstance(sub, dict):
        msg = f"missing or invalid [{section}] section"
        raise ManifestError(msg)
    return sub


def _reject_unknown(d: dict, allowed: set[str], section: str) -> None:
    extras = set(d.keys()) - allowed
    if extras:
        msg = f"unknown keys in [{section}]: {sorted(extras)}"
        raise ManifestError(msg)


def _require_str(d: dict, path: str, *, key: str | None = None) -> str:
    k = key or path
    if k not in d:
        msg = f"missing required field {path}"
        raise ManifestError(msg)
    v = d[k]
    if not isinstance(v, str) or not v:
        msg = f"{path} must be a non-empty string"
        raise ManifestError(msg)
    return v


def _optional_str(d: dict, key: str, *, default: str) -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        msg = f"{key} must be a string, got {type(v).__name__}"
        raise ManifestError(msg)
    return v


def _optional_int(d: dict, key: str, *, default: int) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        msg = f"{key} must be an integer, got {type(v).__name__}"
        raise ManifestError(msg)
    return v


def _optional_str_tuple(d: dict, key: str) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list):
        msg = f"{key} must be an array of strings, got {type(v).__name__}"
        raise ManifestError(msg)
    for i, item in enumerate(v):
        if not isinstance(item, str):
            msg = f"{key}[{i}] must be a string, got {type(item).__name__}"
            raise ManifestError(msg)
    return tuple(v)


__all__ = [
    "SUPPORTED_MODEL_APIS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SUPPORTED_STATUS_MODES",
    "ArtifactsSpec",
    "EntrypointSpec",
    "Manifest",
    "ManifestError",
    "ModelSpec",
    "ProgramInfo",
    "ResourcesSpec",
    "RuntimeSpec",
    "StatusSpec",
    "load_manifest",
]
