"""CLI wiring tests — the agent-facing ``--detach`` / ``--json`` surface of ``launch``.

Uses Click's ``CliRunner`` with ``--dry-run`` so the JSON-handle path is
exercised without spawning a real vLLM/pipeline subprocess. The detach control
flow itself is covered at the ``run_launch`` level in ``test_launch.py``; these
tests pin the CLI contract: the flags parse, and ``--json`` emits a single
machine-readable handle on stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bear_harness._cli import cli

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_pipeline.toml"


def _program(tmp_path: Path) -> Path:
    p = tmp_path / "program"
    p.mkdir()
    (p / "pipeline.toml").write_text(FIXTURE.read_text())
    return p


def test_launch_json_emits_a_parseable_handle(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(cli, ["launch", str(prog), "--local", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # stdout must be JSON, nothing else
    assert payload["state"] == "dry_run"
    assert payload["job_id"]
    for key in ("run_dir", "output_dir", "vllm_job_id", "pipeline_job_id"):
        assert key in payload


def test_launch_detach_and_json_flags_compose(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(
        cli, ["launch", str(prog), "--local", "--dry-run", "--detach", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "dry_run"


def test_launch_without_json_renders_a_table(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(cli, ["launch", str(prog), "--local", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry_run" in result.output
    # The human path is a Rich table, not JSON.
    assert not result.output.lstrip().startswith("{")


def test_results_reattaches_and_emits_json(tmp_path: Path) -> None:
    # Simulate the run dir a detached deploy leaves behind: run.json + an
    # uncollected output dir. `results` should reattach and collect lazily.
    run_dir = tmp_path / "job-x"
    out = run_dir / "output"
    out.mkdir(parents=True)
    (out / "output.txt").write_text("the result\n")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-x",
                "state": "done",
                "manifest_path": "/n/pipeline.toml",
                "model": "m",
                "output_dir": str(out),
                "artifact_patterns": ["output.txt"],
            }
        )
    )
    result = CliRunner().invoke(cli, ["results", str(run_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "job-x"
    assert payload["artifacts"].endswith("artifacts.tar.gz")
    assert payload["collected_now"] is True


def test_launch_denied_qos_exits_nonzero(tmp_path: Path) -> None:
    # A non-allowlisted qos is denied by the kernel gate BEFORE any submit —
    # so this never spawns vLLM despite not being --dry-run.
    prog = _program(tmp_path)
    result = CliRunner().invoke(cli, ["launch", str(prog), "--local", "--qos", "bbgpu"])
    assert result.exit_code == 1, result.output
    assert "denied" in result.output.lower()
    # the config key to widen must survive Rich rendering (not eaten as markup)
    assert "guardrails" in result.output


def test_launch_denied_qos_json_surfaces_guardrail(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(cli, ["launch", str(prog), "--local", "--qos", "bbgpu", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "denied"
    assert payload["guardrail"]["allowed"] is False
    assert any(v["cap"] == "qos_allowlist" for v in payload["guardrail"]["violations"])


def test_launch_dry_run_reports_guardrail_within_caps(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(cli, ["launch", str(prog), "--local", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "dry_run"
    assert payload["guardrail"]["allowed"] is True
    assert "est_gpu_hours" in payload["guardrail"]


def test_launch_dry_run_would_be_denied_exits_nonzero(tmp_path: Path) -> None:
    prog = _program(tmp_path)
    result = CliRunner().invoke(
        cli, ["launch", str(prog), "--local", "--dry-run", "--qos", "bbgpu"]
    )
    assert result.exit_code == 1, result.output
    assert "denied" in result.output.lower() or "qos" in result.output.lower()


def test_check_allowed_exits_zero() -> None:
    result = CliRunner().invoke(cli, ["check", "--local"])
    assert result.exit_code == 0, result.output


def test_check_denied_json_names_cap_and_key() -> None:
    result = CliRunner().invoke(cli, ["check", "--local", "--qos", "bbgpu", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["allowed"] is False
    viol = next(v for v in payload["violations"] if v["cap"] == "qos_allowlist")
    assert "qos_allowlist" in viol["config_key"]


def test_caps_emits_the_allowlist_json() -> None:
    # The discoverability source the MCP bear://guardrails/allowed resource reads.
    result = CliRunner().invoke(cli, ["caps", "--local", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "bbshort" in payload["qos_allowlist"]
    for key in ("max_walltime", "max_concurrent_jobs", "gpu_hours_budget", "require_dry_run"):
        assert key in payload
