"""Fire-and-forget notify engine — backends fire, and failures are swallowed.

The load-bearing property here is negative: a backend that raises or hangs must
NEVER propagate into the run. These tests inject fake command/webhook seams so no
subprocess or network call happens, and assert both the happy path (placeholders
substituted, ``BEAR_NOTIFY_*`` env exported, both backends fire) and the swallow
guarantee (a raising backend is recorded in ``errors`` while the other still
fires and nothing is re-raised).
"""

from __future__ import annotations

from bear_harness._bear_config import NotifyConfig
from bear_harness._notify import (
    NotifyEvent,
    NotifyOutcome,
    fire_notification,
    should_fire,
)


def _event(event: str = "done", *, state: str | None = None, error: str = "") -> NotifyEvent:
    return NotifyEvent(
        event=event,
        run_id="job-1",
        state=state or event,
        run_dir="/runs/job-1",
        model="SmolLM2-1.7B",
        error=error,
    )


class _Recorder:
    """Captures backend calls so tests assert without a subprocess or a socket."""

    def __init__(self) -> None:
        self.commands: list = []
        self.webhooks: list = []

    def run_command(self, argv, env, timeout) -> None:  # type: ignore[no-untyped-def]
        self.commands.append((argv, dict(env), timeout))

    def post_webhook(self, url, payload, timeout) -> None:  # type: ignore[no-untyped-def]
        self.webhooks.append((url, dict(payload), timeout))


class TestShouldFire:
    def test_disabled_config_never_fires(self) -> None:
        assert should_fire(NotifyConfig(), "done") is False
        assert should_fire(NotifyConfig(), "failed") is False

    def test_enabled_gates_on_event(self) -> None:
        cfg = NotifyConfig(command=("true",))
        assert should_fire(cfg, "done") is True
        assert should_fire(cfg, "failed") is True
        assert should_fire(cfg, "weird") is False

    def test_on_done_and_on_fail_toggles(self) -> None:
        assert should_fire(NotifyConfig(command=("x",), on_done=False), "done") is False
        assert should_fire(NotifyConfig(command=("x",), on_fail=False), "failed") is False


class TestSkip:
    def test_no_backend_is_skipped_not_an_error(self) -> None:
        out = fire_notification(NotifyConfig(), _event("done"))
        assert out.skipped is True
        assert out.fired == ()
        assert out.errors == ()
        assert "no backend" in out.skipped_reason

    def test_event_gated_off_is_skipped_without_calling_a_backend(self) -> None:
        rec = _Recorder()
        cfg = NotifyConfig(webhook_url="https://hook", on_fail=False)
        out = fire_notification(
            cfg, _event("failed"), run_command=rec.run_command, post_webhook=rec.post_webhook
        )
        assert out.skipped is True
        assert out.fired == ()
        assert rec.webhooks == []  # gated off => the poster is never invoked


class TestCommandBackend:
    def test_fires_with_substituted_placeholders_and_env(self) -> None:
        rec = _Recorder()
        cfg = NotifyConfig(command=("notify", "{event}:{run_id}", "{state}"))
        out = fire_notification(
            cfg, _event("done"), run_command=rec.run_command, post_webhook=rec.post_webhook
        )
        assert out.fired == ("command",)
        assert out.errors == ()
        argv, env, timeout = rec.commands[0]
        assert argv == ("notify", "done:job-1", "done")
        assert env["BEAR_NOTIFY_EVENT"] == "done"
        assert env["BEAR_NOTIFY_RUN_ID"] == "job-1"
        assert env["BEAR_NOTIFY_MODEL"] == "SmolLM2-1.7B"
        assert timeout == cfg.timeout_seconds
        assert rec.webhooks == []  # webhook not configured => not called

    def test_unknown_placeholder_is_left_intact(self) -> None:
        rec = _Recorder()
        cfg = NotifyConfig(command=("x", "{bogus}", "{run_id}"))
        fire_notification(cfg, _event("done"), run_command=rec.run_command)
        assert rec.commands[0][0] == ("x", "{bogus}", "job-1")


class TestWebhookBackend:
    def test_posts_the_event_payload(self) -> None:
        rec = _Recorder()
        cfg = NotifyConfig(webhook_url="https://hooks.example/x")
        out = fire_notification(
            cfg,
            _event("failed", error="boom"),
            run_command=rec.run_command,
            post_webhook=rec.post_webhook,
        )
        assert out.fired == ("webhook",)
        url, payload, _timeout = rec.webhooks[0]
        assert url == "https://hooks.example/x"
        assert payload["event"] == "failed"
        assert payload["error"] == "boom"
        assert rec.commands == []  # command not configured => not called


class TestBothBackends:
    def test_both_fire_when_both_configured(self) -> None:
        rec = _Recorder()
        cfg = NotifyConfig(command=("c",), webhook_url="https://h")
        out = fire_notification(
            cfg, _event("done"), run_command=rec.run_command, post_webhook=rec.post_webhook
        )
        assert set(out.fired) == {"command", "webhook"}
        assert out.errors == ()
        assert len(rec.commands) == 1
        assert len(rec.webhooks) == 1


class TestFireAndForget:
    """The cardinal property: a backend failure is swallowed, never propagated."""

    def test_raising_command_is_swallowed_and_recorded(self) -> None:
        def boom(argv, env, timeout):  # type: ignore[no-untyped-def]
            raise RuntimeError("subprocess exploded")

        # Must NOT raise — the whole point of fire-and-forget.
        out = fire_notification(NotifyConfig(command=("c",)), _event("done"), run_command=boom)
        assert out.fired == ()
        assert any("subprocess exploded" in e for e in out.errors)

    def test_one_backend_failing_does_not_stop_the_other(self) -> None:
        rec = _Recorder()

        def boom(argv, env, timeout):  # type: ignore[no-untyped-def]
            raise RuntimeError("nope")

        cfg = NotifyConfig(command=("c",), webhook_url="https://h")
        out = fire_notification(
            cfg, _event("done"), run_command=boom, post_webhook=rec.post_webhook
        )
        assert out.fired == ("webhook",)  # webhook still fired despite command blowing up
        assert any("command:" in e for e in out.errors)
        assert len(rec.webhooks) == 1

    def test_timeout_error_is_swallowed(self) -> None:
        def slow(argv, env, timeout):  # type: ignore[no-untyped-def]
            raise TimeoutError("hung")

        out = fire_notification(NotifyConfig(command=("c",)), _event("done"), run_command=slow)
        assert out.fired == ()
        assert out.errors  # recorded, not raised


class TestSerialisation:
    def test_event_payload_is_all_strings(self) -> None:
        payload = _event("done").as_payload()
        assert set(payload) == {"event", "run_id", "state", "run_dir", "model", "error"}
        assert all(isinstance(v, str) for v in payload.values())

    def test_outcome_as_dict(self) -> None:
        out = NotifyOutcome(fired=("command",), errors=("webhook: x",))
        assert out.as_dict() == {
            "fired": ["command"],
            "errors": ["webhook: x"],
            "skipped_reason": "",
        }
