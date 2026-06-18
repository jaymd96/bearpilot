"""Pure parsers for SLURM resource strings — walltime and GRES.

Small, dependency-free helpers the guardrail layer reuses to turn a
BlueBEAR-shaped resource request into the numbers it caps. Kept apart from
the guardrail rule engine so the ``bear.toml`` config validator (which only
needs walltime parsing) can reuse them without importing the rule engine.

The formats are SLURM/BlueBEAR-native (see ``references/slurm-cli.md`` and
``references/bluebear-platform.md``), not invented here:

- **walltime** — the SLURM ``--time`` forms: a bare integer (minutes),
  ``MM:SS``, ``HH:MM:SS``, ``D-HH``, ``D-HH:MM`` and ``D-HH:MM:SS``. ``bear.toml``
  always uses ``HH:MM:SS`` (e.g. ``00:10:00`` for bbshort, ``08:00:00`` for bbgpu).
- **GRES** — the SLURM ``--gres`` form ``gpu:<type>:<count>`` (e.g.
  ``gpu:a100_80:2``); ``gpu:<count>`` and a bare ``gpu`` (count 1) also occur.
  A CPU-only / empty spec is 0 GPUs.
"""

from __future__ import annotations

__all__ = ["DurationError", "gpu_count_from_gres", "parse_walltime_seconds"]


class DurationError(ValueError):
    """Raised for a walltime string that is not a recognised SLURM form."""


def parse_walltime_seconds(walltime: str) -> int:
    """Parse a SLURM ``--time`` string to whole seconds.

    Follows SLURM's grammar: without a ``D-`` day prefix the colon fields are
    ``minutes`` / ``minutes:seconds`` / ``hours:minutes:seconds``; with a day
    prefix they are ``hours`` / ``hours:minutes`` / ``hours:minutes:seconds``.
    Raises :class:`DurationError` on anything malformed.
    """
    raw = walltime.strip()
    if not raw:
        msg = "empty walltime string"
        raise DurationError(msg)

    days = 0
    rest = raw
    has_day_prefix = "-" in raw
    if has_day_prefix:
        day_str, _, rest = raw.partition("-")
        if not day_str.isdigit():
            msg = f"invalid days field in walltime {walltime!r}"
            raise DurationError(msg)
        days = int(day_str)

    parts = rest.split(":")
    if not parts or not all(p.isdigit() for p in parts):
        msg = f"invalid walltime {walltime!r}"
        raise DurationError(msg)
    nums = [int(p) for p in parts]

    if has_day_prefix:
        # days-hours[:minutes[:seconds]]
        if len(nums) == 1:
            hours, minutes, seconds = nums[0], 0, 0
        elif len(nums) == 2:
            hours, minutes, seconds = nums[0], nums[1], 0
        elif len(nums) == 3:
            hours, minutes, seconds = nums
        else:
            msg = f"invalid walltime {walltime!r}"
            raise DurationError(msg)
    else:
        # minutes | minutes:seconds | hours:minutes:seconds
        if len(nums) == 1:
            hours, minutes, seconds = 0, nums[0], 0
        elif len(nums) == 2:
            hours, minutes, seconds = 0, nums[0], nums[1]
        elif len(nums) == 3:
            hours, minutes, seconds = nums
        else:
            msg = f"invalid walltime {walltime!r}"
            raise DurationError(msg)

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def gpu_count_from_gres(gres: str) -> int:
    """Count GPUs in a SLURM ``--gres`` string, e.g. ``gpu:a100_80:2`` -> 2.

    Recognised forms per ``gpu`` entry: ``gpu:<type>:<count>``, ``gpu:<count>``,
    ``gpu:<type>`` (count 1) and a bare ``gpu`` (count 1). Non-``gpu`` entries
    (e.g. ``tmp:100``) contribute 0; multiple comma-separated entries are summed.
    A CPU-only or empty spec is 0.
    """
    if not gres or not gres.strip():
        return 0

    total = 0
    for entry in gres.split(","):
        fields = entry.strip().split(":")
        if not fields or fields[0] != "gpu":
            continue
        last = fields[-1]
        if last == "gpu":
            total += 1  # bare "gpu"
        elif last.isdigit():
            total += int(last)
        else:
            total += 1  # "gpu:<type>" with no explicit count
    return total
