"""Fire-and-forget notification on terminal run states.

Answers the reliability bar "ping me when a run finishes or fails" so nobody has
to babysit SLURM. The cardinal property is in the name: notification is
*fire-and-forget* — a misconfigured webhook or a command that exits non-zero is
logged and swallowed, NEVER raised into the run and never allowed to hang it. A
run's success does not depend on whether its notification was delivered. This is
the whole reason the broad ``except`` blocks below exist and are correct.

Two harness-side backends, ``command`` and ``webhook``; email is delegated to
SLURM-native ``--mail-user`` (see :class:`NotifyConfig`). Both backends are
injected as seams (``run_command`` / ``post_webhook``) so the engine is
unit-testable without spawning a subprocess or making a network call.

The kernel fires this at the *blocking-path* terminal today (W2 Lane C1). The
*detached-path* terminal — a deploy that returned a handle and whose pipeline
finishes minutes later off-process — is the login-node orchestrator's job (W2
Lane C2), and it reuses this same engine: build a :class:`NotifyEvent` from the
``sacct``-observed terminal state and call :func:`fire_notification`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from json import dumps

from bear_harness._bear_config import NotifyConfig

logger = logging.getLogger(__name__)

__all__ = [
    "CommandRunner",
    "NotifyEvent",
    "NotifyOutcome",
    "WebhookPoster",
    "fire_notification",
    "should_fire",
]

# The two terminal events worth announcing. A pre-submit guardrail denial is NOT
# here: it is surfaced synchronously to the caller, not a run that ran and ended.
_PLACEHOLDER_KEYS = ("event", "run_id", "state", "run_dir", "model", "error")


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """A terminal run transition worth announcing.

    ``event`` is the coarse outcome (``"done"`` / ``"failed"``) that gates
    ``on_done`` / ``on_fail``; ``state`` is the precise ``run.json`` terminal
    state it came from (which may be ``"cancelled"`` as well). The fields are all
    strings so they drop straight into a webhook JSON body, command placeholders,
    and ``BEAR_NOTIFY_*`` environment variables without conversion.
    """

    event: str
    run_id: str
    state: str
    run_dir: str
    model: str = ""
    error: str = ""

    def as_payload(self) -> dict[str, str]:
        """Flat string map — the webhook body and the command's env/placeholders."""
        return {
            "event": self.event,
            "run_id": self.run_id,
            "state": self.state,
            "run_dir": self.run_dir,
            "model": self.model,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """What the notifier did — for ``run.json`` / the JSON handle.

    Never a failure signal: a populated ``errors`` tuple means a backend failed
    and was *swallowed*, not that the run failed. ``skipped_reason`` is non-empty
    exactly when nothing fired (notify disabled, or this event class gated off).
    """

    fired: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    skipped_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)

    def as_dict(self) -> dict:
        return {
            "fired": list(self.fired),
            "errors": list(self.errors),
            "skipped_reason": self.skipped_reason,
        }


# Injected seams. Defaulted to the real subprocess / urllib implementations;
# tests pass fakes to assert behaviour without a subprocess or a network call.
CommandRunner = Callable[[tuple[str, ...], Mapping[str, str], float], None]
WebhookPoster = Callable[[str, dict, float], None]


def should_fire(config: NotifyConfig, event_name: str) -> bool:
    """Whether this event should notify, by config gating alone (no I/O)."""
    if not config.enabled:
        return False
    if event_name == "done":
        return config.on_done
    if event_name == "failed":
        return config.on_fail
    return False


def _safe_format(template: str, payload: Mapping[str, str]) -> str:
    """Substitute ``{key}`` tokens for known keys; leave anything else intact.

    Uses plain replacement rather than ``str.format`` so an unknown ``{token}``
    or a literal brace in the operator's argv never raises — fire-and-forget
    extends to the template itself.
    """
    out = template
    for key in _PLACEHOLDER_KEYS:
        out = out.replace("{" + key + "}", payload.get(key, ""))
    return out


def _default_run_command(
    argv: tuple[str, ...], env_overlay: Mapping[str, str], timeout: float
) -> None:
    """Run ``argv`` with the ``BEAR_NOTIFY_*`` overlay on top of the real env.

    ``capture_output`` keeps backend chatter out of the run console; ``check``
    turns a non-zero exit into the error :func:`fire_notification` records.
    """
    subprocess.run(
        argv,
        env={**os.environ, **env_overlay},
        timeout=timeout,
        check=True,
        capture_output=True,
    )


def _default_post_webhook(url: str, payload: dict, timeout: float) -> None:
    data = dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # url is operator-configured, not arbitrary user input
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def fire_notification(
    config: NotifyConfig,
    event: NotifyEvent,
    *,
    run_command: CommandRunner | None = None,
    post_webhook: WebhookPoster | None = None,
) -> NotifyOutcome:
    """Deliver ``event`` over every configured backend, fire-and-forget.

    Returns a :class:`NotifyOutcome` describing what happened; it NEVER raises
    and NEVER blocks longer than ``config.timeout_seconds`` per backend. A
    backend that errors is recorded in ``errors`` and logged — the other backend
    still fires, and the caller's terminal transition is unaffected.
    """
    if not should_fire(config, event.event):
        reason = "no backend configured" if not config.enabled else f"{event.event} disabled"
        return NotifyOutcome(skipped_reason=reason)

    runner = run_command or _default_run_command
    poster = post_webhook or _default_post_webhook
    payload = event.as_payload()
    fired: list[str] = []
    errors: list[str] = []

    if config.command:
        argv = tuple(_safe_format(part, payload) for part in config.command)
        env_overlay = {f"BEAR_NOTIFY_{key.upper()}": value for key, value in payload.items()}
        try:
            runner(argv, env_overlay, config.timeout_seconds)
            fired.append("command")
        except Exception as exc:  # fire-and-forget: log + swallow EVERY backend failure
            errors.append(f"command: {exc}")
            logger.warning("notify command backend failed (swallowed): %s", exc)

    if config.webhook_url is not None:
        try:
            poster(config.webhook_url, payload, config.timeout_seconds)
            fired.append("webhook")
        except Exception as exc:  # fire-and-forget: log + swallow EVERY backend failure
            errors.append(f"webhook: {exc}")
            logger.warning("notify webhook backend failed (swallowed): %s", exc)

    return NotifyOutcome(fired=tuple(fired), errors=tuple(errors))
