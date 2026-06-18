"""Unit tests for the pure SLURM duration / GRES parsers (``_duration``).

These back the guardrail layer's reservation-ceiling estimate and the
``bear.toml`` walltime validator, so they are table-driven and exhaustive on
the formats ``bear.toml`` and SLURM overrides actually use.
"""

from __future__ import annotations

import pytest

from bear_harness._duration import (
    DurationError,
    gpu_count_from_gres,
    parse_walltime_seconds,
)


class TestParseWalltimeSeconds:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("00:10:00", 600),  # bbshort's 10-minute cap
            ("08:00:00", 28800),  # a typical bbgpu run
            ("01:00:00", 3600),
            ("00:00:30", 30),
            ("10:30", 630),  # MM:SS
            ("5", 300),  # bare integer = minutes
            ("1-00:00:00", 86400),  # D-HH:MM:SS
            ("1-12", 129600),  # D-HH (1 day 12 hours)
            ("0-00:10:00", 600),  # explicit zero days
            ("2-00:00", 172800),  # D-HH:MM
        ],
    )
    def test_valid_forms(self, text: str, expected: int) -> None:
        assert parse_walltime_seconds(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "abc", "1:2:3:4", "--", "1-", "12:xx", ":", "1::2"],
    )
    def test_malformed_raises(self, text: str) -> None:
        with pytest.raises(DurationError):
            parse_walltime_seconds(text)


class TestGpuCountFromGres:
    @pytest.mark.parametrize(
        ("gres", "expected"),
        [
            ("gpu:a100_80:2", 2),
            ("gpu:a100_40:1", 1),
            ("gpu:2", 2),
            ("gpu", 1),
            ("gpu:a100_80", 1),  # type, no explicit count
            ("", 0),
            ("   ", 0),
            ("cpu:4", 0),  # non-gpu gres
            ("tmp:100", 0),
            ("gpu:a100_80:2,tmp:100", 2),  # summed over gpu entries only
        ],
    )
    def test_count(self, gres: str, expected: int) -> None:
        assert gpu_count_from_gres(gres) == expected
