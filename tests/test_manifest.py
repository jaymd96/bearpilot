"""Unit tests for ``bear_harness._manifest``."""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._manifest import (
    Manifest,
    ManifestError,
    load_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_pipeline.toml"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "pipeline.toml"
    p.write_text(body)
    return p


class TestHappyPath:
    def test_load_minimal_fixture(self) -> None:
        manifest = load_manifest(FIXTURE)
        assert isinstance(manifest, Manifest)
        assert manifest.schema_version == "1"
        assert manifest.program.name == "minimal"
        assert manifest.program.version == "0.0.1"
        assert manifest.runtime.python == ">=3.11,<4"
        assert manifest.model.api == "anthropic_messages"
        assert manifest.model.default_model == "stub-model"
        assert manifest.entrypoint.command[0] == "$PYTHON"
        assert manifest.entrypoint.env == {"PYTHONUNBUFFERED": "1"}
        assert manifest.artifacts.collect == ("output.txt",)
        assert manifest.resources.gpu_memory_gb == 8

    def test_load_by_directory(self, tmp_path: Path) -> None:
        (tmp_path / "pipeline.toml").write_text(FIXTURE.read_text())
        manifest = load_manifest(tmp_path)
        assert manifest.program.name == "minimal"
        assert manifest.program_root == tmp_path.resolve()


class TestPreset:
    def test_preset_defaults_to_vllm_pipeline(self) -> None:
        # An existing manifest with no `preset` field keeps the reference flow.
        assert load_manifest(FIXTURE).preset == "vllm-pipeline"

    def test_explicit_preset(self, tmp_path: Path) -> None:
        _write(tmp_path, 'preset = "etl"\n' + FIXTURE.read_text())
        assert load_manifest(tmp_path).preset == "etl"

    def test_model_is_optional(self, tmp_path: Path) -> None:
        # A model-less program (ETL) loads fine; the preset validates its own sections.
        _write(
            tmp_path,
            'schema_version = "1"\npreset = "etl"\n'
            '[program]\nname = "etl-prog"\nversion = "0.1.0"\n'
            '[runtime]\npython = ">=3.11,<4"\n'
            '[entrypoint]\ncommand = ["echo", "hi"]\n',
        )
        manifest = load_manifest(tmp_path)
        assert manifest.model is None
        assert manifest.preset == "etl"


class TestSchemaErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path)

    def test_unsupported_schema_version(self, tmp_path: Path) -> None:
        _write(tmp_path, 'schema_version = "2"\n[program]\nname="x"\nversion="1"\n')
        with pytest.raises(ManifestError, match="unsupported schema_version"):
            load_manifest(tmp_path)

    def test_unknown_top_level_key(self, tmp_path: Path) -> None:
        body = FIXTURE.read_text() + "\n[unknown]\nfoo = 'bar'\n"
        _write(tmp_path, body)
        with pytest.raises(ManifestError, match="unknown top-level keys"):
            load_manifest(tmp_path)

    def test_unknown_section_key(self, tmp_path: Path) -> None:
        body = FIXTURE.read_text().replace(
            '[program]',
            '[program]\nstrange_field = "oops"',
        )
        _write(tmp_path, body)
        with pytest.raises(ManifestError, match="unknown keys in \\[program\\]"):
            load_manifest(tmp_path)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            'schema_version = "1"\n[program]\nversion = "0.1.0"\n'
            '[runtime]\npython = ">=3.11,<4"\n'
            '[model]\napi = "anthropic_messages"\ndefault_model = "m"\n'
            '[entrypoint]\ncommand = ["echo"]\n',
        )
        with pytest.raises(ManifestError, match=r"program\.name"):
            load_manifest(tmp_path)

    def test_bad_model_api(self, tmp_path: Path) -> None:
        body = FIXTURE.read_text().replace("anthropic_messages", "invalid_api")
        _write(tmp_path, body)
        with pytest.raises(ManifestError, match=r"model\.api"):
            load_manifest(tmp_path)

    def test_empty_command(self, tmp_path: Path) -> None:
        body = FIXTURE.read_text().replace(
            'command = [\n  "$PYTHON", "-c",\n'
            '  "import sys; print(\'hello from\', sys.executable)"\n]',
            "command = []",
        )
        _write(tmp_path, body)
        with pytest.raises(ManifestError, match="non-empty array"):
            load_manifest(tmp_path)

    def test_non_string_env_value(self, tmp_path: Path) -> None:
        body = FIXTURE.read_text().replace(
            'env = { PYTHONUNBUFFERED = "1" }',
            "env = { PYTHONUNBUFFERED = 1 }",
        )
        _write(tmp_path, body)
        with pytest.raises(ManifestError, match=r"entrypoint\.env"):
            load_manifest(tmp_path)

    def test_bad_toml(self, tmp_path: Path) -> None:
        _write(tmp_path, "this is not = = valid toml\n")
        with pytest.raises(ManifestError, match="failed to parse"):
            load_manifest(tmp_path)
