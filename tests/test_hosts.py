"""Tests for the laptop-side host registry (``_hosts.py``).

Mirrors the strict-key parse discipline of ``test_bear_config.py``: a full
round-trip, optional-field defaults, and an unknown/missing-key raise for every
table level. No SSH or filesystem-of-the-cluster involvement — this is pure
laptop-side TOML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._hosts import Host, HostsConfig, HostsConfigError, load_hosts

_SAMPLE = """\
default = "bluebear"

[hosts.bluebear]
ssh_alias = "bluebear"
remote_rds_root = "/rds/projects/a/abc-proj"
remote_inbox = "/rds/projects/a/abc-proj/.bear-harness/inbox"
remote_binary = "bear-harness"
artifacts_cache = "~/.cache/bear-harness/fetched/bluebear"
"""

_MINIMAL = '[hosts.h]\nssh_alias = "h"\nremote_rds_root = "/r"\nremote_inbox = "/r/inbox"\n'


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "hosts.toml"
    p.write_text(text)
    return p


class TestLoadHosts:
    def test_round_trips_a_full_host(self, tmp_path: Path) -> None:
        cfg = load_hosts(_write(tmp_path, _SAMPLE))
        assert cfg.default == "bluebear"
        assert cfg.resolve() == Host(
            name="bluebear",
            ssh_alias="bluebear",
            remote_rds_root="/rds/projects/a/abc-proj",
            remote_inbox="/rds/projects/a/abc-proj/.bear-harness/inbox",
            remote_binary="bear-harness",
            artifacts_cache="~/.cache/bear-harness/fetched/bluebear",
        )

    def test_defaults_fill_optional_fields(self, tmp_path: Path) -> None:
        host = load_hosts(_write(tmp_path, _MINIMAL)).resolve("h")
        assert host.remote_binary == "bear-harness"
        assert host.artifacts_cache == "~/.cache/bear-harness/fetched"

    def test_unknown_top_level_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HostsConfigError, match="unknown top-level"):
            load_hosts(_write(tmp_path, "bogus = 1\n" + _MINIMAL))

    def test_unknown_host_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HostsConfigError, match="unknown keys"):
            load_hosts(_write(tmp_path, _MINIMAL + "bogus = 1\n"))

    def test_missing_required_host_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HostsConfigError, match="missing required keys"):
            load_hosts(_write(tmp_path, '[hosts.h]\nssh_alias = "h"\n'))

    def test_resolve_unknown_host_raises(self, tmp_path: Path) -> None:
        cfg = load_hosts(_write(tmp_path, _SAMPLE))
        with pytest.raises(HostsConfigError, match="unknown host"):
            cfg.resolve("nope")

    def test_resolve_without_name_or_default_raises(self, tmp_path: Path) -> None:
        cfg = load_hosts(_write(tmp_path, _MINIMAL))
        with pytest.raises(HostsConfigError, match="no host specified"):
            cfg.resolve()

    def test_default_naming_missing_host_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HostsConfigError, match="names no"):
            load_hosts(_write(tmp_path, 'default = "ghost"\n' + _MINIMAL))

    def test_missing_file_with_explicit_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(HostsConfigError, match="not found"):
            load_hosts(tmp_path / "nope.toml")

    def test_empty_registry_resolve_raises(self) -> None:
        # The shape returned when path=None and no default file exists.
        with pytest.raises(HostsConfigError, match="no host specified"):
            HostsConfig().resolve()
