"""Shared Jinja2 environment used for rendering sbatch scripts.

Lives in its own module so both ``_vllm_launcher`` and
``_pipeline_launcher`` can import the same configured ``Environment``
without one depending on the other.

Kept intentionally strict:

- ``StrictUndefined`` — any unresolved template variable is an error.
  Sbatch scripts have no sensible default for "missing account" and
  silently producing an invalid script is worse than failing the render.
- ``trim_blocks`` / ``lstrip_blocks`` — keeps the generated bash
  readable by operators who will have to `less` these files on
  BlueBEAR when something goes wrong.
- ``keep_trailing_newline`` — POSIX shells expect a trailing newline.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_env() -> Environment:
    """Return a preconfigured ``Environment`` rooted at the templates dir."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # bash scripts — never HTML-escape
    )


def load_template(name: str) -> Template:
    """Fetch a named template from the bundled templates dir."""
    return build_env().get_template(name)


__all__ = ["TEMPLATES_DIR", "build_env", "load_template"]
