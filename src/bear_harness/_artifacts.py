"""Collect artifacts from a finished run into a tarball.

The manifest's ``[artifacts] collect = [...]`` field lists globs
relative to ``$OUTPUT_DIR``. After the pipeline job terminates
(regardless of success), the harness globs each pattern, gathers the
matched files, and archives them into ``artifacts.tar.gz`` inside the
run directory. Raw log files are also copied in — operators should
never need to SSH into the node to collect debugging data.

The collector is intentionally tolerant: missing matches for a glob
are logged and skipped, not errored. A failed run that produced no
outputs still gets a tarball (possibly containing only the logs), so
``bear-harness status <job>`` always has something to hand back.
"""

from __future__ import annotations

import logging
import tarfile
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def collect_artifacts(
    *,
    output_dir: Path,
    patterns: Iterable[str],
    extra_files: Iterable[Path] = (),
    destination: Path,
) -> Path:
    """Create a gzipped tarball from files matching ``patterns`` + extras.

    Returns the ``destination`` path for convenience. Creates parent
    directories as needed. Existing tarballs are overwritten — each
    run gets a fresh one.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    patterns_list = list(patterns)
    collected: list[tuple[Path, str]] = []

    output_dir = output_dir.resolve()
    for pat in patterns_list:
        matches = list(output_dir.glob(pat))
        if not matches:
            logger.info("artifact pattern %r matched no files under %s", pat, output_dir)
            continue
        for match in matches:
            if match.is_file():
                arcname = str(match.relative_to(output_dir))
                collected.append((match, arcname))

    for extra in extra_files:
        if extra.is_file():
            collected.append((extra, f"logs/{extra.name}"))

    with tarfile.open(destination, "w:gz") as tar:
        for path, arcname in collected:
            try:
                tar.add(path, arcname=arcname)
            except (OSError, tarfile.TarError):
                logger.warning("failed to add %s to tarball", path, exc_info=True)

    logger.info(
        "collected %d artifact files into %s",
        len(collected),
        destination,
    )
    return destination


__all__ = ["collect_artifacts"]
