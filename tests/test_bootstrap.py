"""Unit tests for ``bear_harness._bootstrap``.

Every external effect (``apptainer``, ``module``, ``curl``, filesystem
writes) is injected so these tests never touch a real BlueBEAR.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from bear_harness._bear_config import load_bear_config
from bear_harness._bootstrap import (
    BootstrapError,
    BootstrapOptions,
    CommandResult,
    run_bootstrap,
)


class _ScriptedShell:
    """Fake ``CommandRunner`` that pattern-matches argv prefixes."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        t = tuple(argv)
        self.calls.append(t)
        # Prefix match: if a key is a prefix of the argv, return it.
        for key, result in self._responses.items():
            if t[: len(key)] == key:
                return result
        return CommandResult(returncode=0, stdout="", stderr="")


def _options(tmp_path: Path, **overrides: object) -> BootstrapOptions:
    base = {
        "rds_root": tmp_path / "rds",
        "account": "proj1",
        "apptainer_image": "docker://vllm/vllm-openai:latest",
        "cuda_module": "CUDA/12.1.1",
        "config_path": tmp_path / "config" / "bear.toml",
    }
    base.update(overrides)
    return BootstrapOptions(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_creates_rds_tree(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(0, "", ""),
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(
                    0, "------- CUDA -------\nCUDA/12.1.1\nCUDA/12.4.0\n", ""
                ),
            }
        )
        report = run_bootstrap(
            _options(tmp_path),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )

        rds = tmp_path / "rds"
        assert (rds / ".bear-harness" / "endpoints").is_dir()
        assert (rds / ".bear-harness" / "runs").is_dir()
        assert (rds / ".bear-harness" / "apptainer").is_dir()
        assert (rds / "hf_cache").is_dir()
        assert report.sif_path == rds / ".bear-harness" / "apptainer" / "vllm-openai.sif"

    def test_writes_bear_toml_with_account_substituted(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(0, "", ""),
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "CUDA/12.1.1\n", ""),
            }
        )
        report = run_bootstrap(
            _options(tmp_path, account="datagen-2026"),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert report.config_path is not None
        body = report.config_path.read_text()
        assert 'account       = "datagen-2026"' in body
        assert str(tmp_path / "rds" / "hf_cache") in body
        # And it must round-trip through the real loader:
        cfg = load_bear_config(report.config_path)
        assert cfg.is_slurm
        assert cfg.require_slurm().account == "datagen-2026"

    def test_skip_pull_avoids_shell_call(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        run_bootstrap(
            _options(tmp_path, skip_pull=True),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert not any(call[:2] == ("apptainer", "pull") for call in shell.calls)

    def test_existing_sif_is_not_repulled(self, tmp_path: Path) -> None:
        sif = tmp_path / "rds" / ".bear-harness" / "apptainer" / "vllm-openai.sif"
        sif.parent.mkdir(parents=True, exist_ok=True)
        sif.write_bytes(b"existing")
        shell = _ScriptedShell(
            {
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        run_bootstrap(
            _options(tmp_path),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert sif.read_bytes() == b"existing"
        assert not any(call[:2] == ("apptainer", "pull") for call in shell.calls)


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_missing_apptainer_raises(self, tmp_path: Path) -> None:
        shell = _ScriptedShell({})
        with pytest.raises(BootstrapError, match="apptainer not found"):
            run_bootstrap(
                _options(tmp_path),
                run_shell=shell,
                which=lambda _name: None,
            )

    def test_pull_failure_raises(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(1, "", "disk full"),
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        with pytest.raises(BootstrapError, match="apptainer pull failed"):
            run_bootstrap(
                _options(tmp_path),
                run_shell=shell,
                which=lambda _name: "/usr/bin/apptainer",
            )

    def test_mail_user_written_to_bear_toml(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(0, "", ""),
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        report = run_bootstrap(
            _options(tmp_path, mail_user="you@example.com"),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert report.config_path is not None
        body = report.config_path.read_text()
        assert 'mail_user' in body
        assert "you@example.com" in body
        cfg = load_bear_config(report.config_path)
        assert cfg.require_slurm().mail_user == "you@example.com"

    def test_no_mail_user_leaves_commented(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(0, "", ""),
                ("curl",): CommandResult(0, "", ""),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        report = run_bootstrap(
            _options(tmp_path),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert report.config_path is not None
        cfg = load_bear_config(report.config_path)
        assert cfg.require_slurm().mail_user is None

    def test_hf_unreachable_warns_not_fatal(self, tmp_path: Path) -> None:
        shell = _ScriptedShell(
            {
                ("apptainer", "pull"): CommandResult(0, "", ""),
                ("curl",): CommandResult(6, "", "could not resolve host"),
                ("bash", "-lc"): CommandResult(0, "", ""),
            }
        )
        report = run_bootstrap(
            _options(tmp_path),
            run_shell=shell,
            which=lambda _name: "/usr/bin/apptainer",
        )
        assert any("huggingface.co unreachable" in w for w in report.warnings)
