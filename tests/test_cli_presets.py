"""CLI tests for the ``presets`` group — the declarative authoring kit (W4 S5)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bear_harness._cli import cli

_ETL_FIXTURE = Path(__file__).parent / "fixtures" / "etl_pipeline.toml"


def test_presets_list_shows_both_presets():
    result = CliRunner().invoke(cli, ["presets", "list"])
    assert result.exit_code == 0
    assert "vllm-pipeline" in result.output
    assert "etl" in result.output


def test_presets_describe_emits_json():
    result = CliRunner().invoke(cli, ["presets", "describe", "etl"])
    assert result.exit_code == 0
    assert '"name": "etl"' in result.output


def test_presets_describe_unknown_exits_nonzero():
    result = CliRunner().invoke(cli, ["presets", "describe", "does-not-exist"])
    assert result.exit_code == 1


def test_presets_validate_accepts_etl(tmp_path: Path):
    prog = tmp_path / "etl"
    prog.mkdir()
    (prog / "pipeline.toml").write_text(_ETL_FIXTURE.read_text())
    result = CliRunner().invoke(cli, ["presets", "validate", str(prog), "--local", "--json"])
    assert result.exit_code == 0
    assert '"ok": true' in result.output
    assert '"topology": "single"' in result.output


def test_presets_validate_rejects_vllm_without_model(tmp_path: Path):
    # Default preset is vllm-pipeline, which requires [model] — a manifest without it
    # is rejected BEFORE any submit (the pre-flight the authoring kit exists for).
    prog = tmp_path / "bad"
    prog.mkdir()
    (prog / "pipeline.toml").write_text(
        'schema_version = "1"\n'
        '[program]\nname = "x"\nversion = "1"\n'
        '[runtime]\npython = ">=3.11,<4"\n'
        '[entrypoint]\ncommand = ["echo"]\n'
    )
    result = CliRunner().invoke(cli, ["presets", "validate", str(prog), "--local", "--json"])
    assert result.exit_code == 1
    assert '"ok": false' in result.output
