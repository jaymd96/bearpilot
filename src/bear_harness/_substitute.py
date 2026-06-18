"""Literal ``$NAME`` substitution over a closed set of variables.

The harness refuses to run a shell for variable expansion. That would
let a consumer program smuggle arbitrary shell side effects into the
manifest — exactly the kind of blast radius we are engineering away.
Instead, substitution is a pure string-level replacement over a fixed
set of variable names, implemented in one place so it is trivially
auditable.

Rules:

- Recognised variables: everything in ``KNOWN_VARS``. Adding a new
  variable means editing that set in code — intentional friction.
- Unrecognised ``$NAME`` tokens raise ``SubstitutionError``. Silent
  pass-through would mean operators never find out when they typo a
  variable name in their manifest.
- Dollar literals can be escaped as ``$$``, which becomes ``$``. This
  matches the Makefile / Docker escape convention so operators have a
  single mental model.
- Only identifier-shaped names (``[A-Za-z_][A-Za-z0-9_]*``) are
  substituted. Anything else is either a literal dollar or a
  typing mistake.
- Substitution is non-recursive: the result of one substitution is
  not re-scanned. This keeps the rule set clear and stops infinite
  loops dead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Every variable name the harness is allowed to substitute. Extending
# this is a deliberate, reviewable change.
KNOWN_VARS: frozenset[str] = frozenset(
    {
        "PROGRAM_ROOT",
        "PYTHON",
        "OUTPUT_DIR",
        "STATUS_FILE",
        "MODEL_BASE_URL",
        "MODEL_API_KEY",
        "MODEL_NAME",
        "JOB_ID",
        "SLURM_VLLM_JOB_ID",
        "SLURM_PIPELINE_JOB_ID",
    }
)

# Matches $NAME or ${NAME} but leaves $$ alone (escape handled separately).
_VAR_PATTERN = re.compile(r"\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_ESCAPE = "\x00BEAR_HARNESS_DOLLAR\x00"


class SubstitutionError(ValueError):
    """Raised when a ``$NAME`` token refers to a variable the harness
    does not recognise, or a variable that is recognised but has no
    value supplied in the current context.
    """


def substitute(template: str, variables: Mapping[str, str]) -> str:
    """Substitute ``$NAME`` / ``${NAME}`` in ``template`` using ``variables``.

    Unknown variables — anything outside ``KNOWN_VARS`` — raise
    ``SubstitutionError`` even if present in ``variables``. This is
    deliberate: the known-var set is the contract, and the caller can
    pass a superset of the variables a given template actually needs.
    Variables that are known but missing from ``variables`` are also
    errors. ``$$`` escapes to a literal ``$``.
    """
    # Temporarily mask $$ so the regex cannot match it, then restore.
    masked = template.replace("$$", _ESCAPE)

    def _replace(match: re.Match[str]) -> str:
        name = match.group(2) or match.group(3)
        if name not in KNOWN_VARS:
            msg = (
                f"unknown substitution variable ${name!r}. "
                f"Allowed: {sorted(KNOWN_VARS)}"
            )
            raise SubstitutionError(msg)
        if name not in variables:
            msg = f"substitution variable ${name!r} has no value in current context"
            raise SubstitutionError(msg)
        return variables[name]

    replaced = _VAR_PATTERN.sub(_replace, masked)
    return replaced.replace(_ESCAPE, "$")


def substitute_all(
    templates: Iterable[str],
    variables: Mapping[str, str],
) -> tuple[str, ...]:
    """Apply :func:`substitute` to every element of an iterable."""
    return tuple(substitute(t, variables) for t in templates)


def substitute_env(
    env: Mapping[str, str],
    variables: Mapping[str, str],
) -> dict[str, str]:
    """Apply :func:`substitute` to every value in an environment dict.

    Keys are never substituted — they are environment variable names,
    not templates.
    """
    return {k: substitute(v, variables) for k, v in env.items()}


__all__ = [
    "KNOWN_VARS",
    "SubstitutionError",
    "substitute",
    "substitute_all",
    "substitute_env",
]
