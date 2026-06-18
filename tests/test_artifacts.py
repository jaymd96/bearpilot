"""Unit tests for ``bear_harness._artifacts``."""

from __future__ import annotations

import tarfile
from pathlib import Path

from bear_harness._artifacts import collect_artifacts


class TestCollectArtifacts:
    def test_collects_globbed_files(self, tmp_path: Path) -> None:
        output = tmp_path / "out"
        output.mkdir()
        (output / "results.json").write_text('{"n": 1}')
        (output / "notes.txt").write_text("hello")
        (output / "ignored.bin").write_bytes(b"\x00\x01")
        tarball = tmp_path / "artifacts.tar.gz"

        collect_artifacts(
            output_dir=output,
            patterns=["*.json", "*.txt"],
            destination=tarball,
        )

        assert tarball.exists()
        with tarfile.open(tarball, "r:gz") as tar:
            names = set(tar.getnames())
        assert "results.json" in names
        assert "notes.txt" in names
        assert "ignored.bin" not in names

    def test_missing_glob_is_logged_not_fatal(self, tmp_path: Path) -> None:
        output = tmp_path / "out"
        output.mkdir()
        (output / "a.json").write_text("1")
        tarball = tmp_path / "a.tar.gz"
        collect_artifacts(
            output_dir=output,
            patterns=["*.json", "nonexistent/*.log"],
            destination=tarball,
        )
        assert tarball.exists()

    def test_extra_logs_land_under_logs_prefix(self, tmp_path: Path) -> None:
        output = tmp_path / "out"
        output.mkdir()
        log = tmp_path / "vllm.log"
        log.write_text("boot ok\n")
        tarball = tmp_path / "a.tar.gz"
        collect_artifacts(
            output_dir=output,
            patterns=[],
            extra_files=(log,),
            destination=tarball,
        )
        with tarfile.open(tarball, "r:gz") as tar:
            assert "logs/vllm.log" in tar.getnames()

    def test_nested_glob(self, tmp_path: Path) -> None:
        output = tmp_path / "out"
        (output / "logs").mkdir(parents=True)
        (output / "logs" / "a.log").write_text("x")
        (output / "logs" / "b.log").write_text("y")
        tarball = tmp_path / "a.tar.gz"
        collect_artifacts(
            output_dir=output,
            patterns=["logs/*.log"],
            destination=tarball,
        )
        with tarfile.open(tarball, "r:gz") as tar:
            names = set(tar.getnames())
        assert "logs/a.log" in names
        assert "logs/b.log" in names
