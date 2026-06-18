"""Unit tests for ``bear.toml`` loader — Phase 4 local-backend additions.

Coverage scope is deliberately narrow: Phase B / C added ``LocalConfig`` /
``SlurmConfig`` / the loader without a dedicated test file, and Phase 4
adds a new ``[local].backend`` field plus a ``[local.ollama]`` sub-table.
These tests exercise only the new surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bear_harness._bear_config import (
    BearConfigError,
    default_local_config,
    load_bear_config,
)


def _write_toml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "bear.toml"
    p.write_text(body)
    return p


class TestBackendField:
    def test_default_backend_is_vllm(self, tmp_path: Path) -> None:
        """Pre-Phase-4 behaviour: no ``backend`` key → defaults to ``vllm``."""
        path = _write_toml(
            tmp_path,
            'mode = "local"\n[local]\nruns_dir = "/tmp/r"\n',
        )
        cfg = load_bear_config(path)
        local = cfg.require_local()
        assert local.backend == "vllm"
        assert local.ollama is None

    def test_default_factory_is_vllm(self) -> None:
        """``default_local_config()`` (used when ``bear.toml`` is missing)
        must keep the pre-Phase-4 default so existing users are unaffected."""
        cfg = default_local_config()
        assert cfg.require_local().backend == "vllm"

    def test_explicit_vllm_backend(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            'mode = "local"\n[local]\nbackend = "vllm"\n',
        )
        local = load_bear_config(path).require_local()
        assert local.backend == "vllm"
        assert local.ollama is None

    def test_unknown_backend_raises(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            'mode = "local"\n[local]\nbackend = "nonsense"\n',
        )
        with pytest.raises(BearConfigError, match="backend"):
            load_bear_config(path)


class TestOllamaSubTable:
    def test_ollama_backend_with_inline_model(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[local]\nbackend = "ollama"\n[local.ollama]\nmodel = "llama3.2"\n'
        path = _write_toml(tmp_path, body)
        local = load_bear_config(path).require_local()
        assert local.backend == "ollama"
        assert local.ollama is not None
        assert local.ollama.model == "llama3.2"
        assert local.ollama.host == "127.0.0.1"
        assert local.ollama.port == 11434

    def test_ollama_host_and_port_overrides(self, tmp_path: Path) -> None:
        body = (
            'mode = "local"\n'
            "[local]\n"
            'backend = "ollama"\n'
            "[local.ollama]\n"
            'model = "mistral-small"\n'
            'host = "10.0.0.5"\n'
            "port = 12345\n"
        )
        path = _write_toml(tmp_path, body)
        local = load_bear_config(path).require_local()
        assert local.ollama is not None
        assert local.ollama.host == "10.0.0.5"
        assert local.ollama.port == 12345

    def test_ollama_backend_without_sub_table_raises(self, tmp_path: Path) -> None:
        """``backend = "ollama"`` with no ``[local.ollama]`` is a config error."""
        path = _write_toml(
            tmp_path,
            'mode = "local"\n[local]\nbackend = "ollama"\n',
        )
        with pytest.raises(BearConfigError, match="ollama"):
            load_bear_config(path)

    def test_ollama_sub_table_without_model_raises(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[local]\nbackend = "ollama"\n[local.ollama]\nhost = "127.0.0.1"\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match="model"):
            load_bear_config(path)

    def test_ollama_sub_table_permitted_with_vllm_backend(self, tmp_path: Path) -> None:
        """``[local.ollama]`` can sit next to ``backend = "vllm"`` unused.

        This lets users pre-configure both backends and flip a single
        ``backend =`` line to switch, without deleting the unused section.
        """
        body = 'mode = "local"\n[local]\nbackend = "vllm"\n[local.ollama]\nmodel = "llama3.2"\n'
        path = _write_toml(tmp_path, body)
        local = load_bear_config(path).require_local()
        assert local.backend == "vllm"
        assert local.ollama is not None
        assert local.ollama.model == "llama3.2"

    def test_unknown_key_in_ollama_sub_table_raises(self, tmp_path: Path) -> None:
        body = (
            'mode = "local"\n'
            "[local]\n"
            'backend = "ollama"\n'
            "[local.ollama]\n"
            'model = "llama3.2"\n'
            'garbage = "nope"\n'
        )
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match=r"garbage|unknown"):
            load_bear_config(path)


# ---------------------------------------------------------------------------
# SLURM mail notification config
# ---------------------------------------------------------------------------

_MINIMAL_SLURM = """\
mode = "slurm"
[slurm]
account       = "proj1"
qos           = "bbgpu"
gpu_gres      = "gpu:a100_40:1"
cpus_per_task = 8
mem_gb        = 64
walltime      = "08:00:00"
cuda_module   = "CUDA/12.1.1"
apptainer_sif = "/rds/apptainer/vllm.sif"
hf_cache      = "/rds/hf_cache"
runs_dir      = "/rds/runs"
endpoints_dir = "/rds/endpoints"
"""


class TestSlurmMailConfig:
    """Parsing of optional ``mail_user`` and ``mail_events`` fields."""

    def test_default_mail_user_is_none(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, _MINIMAL_SLURM)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.mail_user is None

    def test_default_mail_events(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, _MINIMAL_SLURM)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.mail_events == "BEGIN,END,FAIL"

    def test_explicit_mail_user(self, tmp_path: Path) -> None:
        body = _MINIMAL_SLURM + 'mail_user = "alice@example.com"\n'
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.mail_user == "alice@example.com"

    def test_explicit_mail_events(self, tmp_path: Path) -> None:
        body = _MINIMAL_SLURM + 'mail_user = "a@b.com"\nmail_events = "ALL"\n'
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.mail_events == "ALL"

    def test_mail_events_without_mail_user_is_valid(self, tmp_path: Path) -> None:
        """Setting events without a user is harmless — SLURM ignores it."""
        body = _MINIMAL_SLURM + 'mail_events = "END"\n'
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.mail_user is None
        assert cfg.mail_events == "END"


class TestExtraVllmArgs:
    """Parsing of optional ``extra_vllm_args`` list."""

    def test_default_is_empty_tuple(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, _MINIMAL_SLURM)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.extra_vllm_args == ()

    def test_parses_list_of_strings(self, tmp_path: Path) -> None:
        body = (
            _MINIMAL_SLURM
            + 'extra_vllm_args = ["--performance-mode", "throughput", "--num-scheduler-steps", "10"]\n'
        )
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path).require_slurm()
        assert cfg.extra_vllm_args == (
            "--performance-mode",
            "throughput",
            "--num-scheduler-steps",
            "10",
        )


# ---------------------------------------------------------------------------
# Default-deny guardrails config ([guardrails])
# ---------------------------------------------------------------------------


class TestGuardrailConfig:
    """``[guardrails]`` parsing + the load-bearing default-DENY default.

    The critical invariant: an *absent* ``[guardrails]`` section must yield a
    tight built-in leash, never an unbounded one — that is the default-deny
    posture (docs/decision-notes/default-deny-guardrails.md).
    """

    def test_absent_section_defaults_to_tight_leash(self, tmp_path: Path) -> None:
        """No ``[guardrails]`` => bbshort-only allowlist + finite caps, NOT open."""
        from bear_harness._duration import parse_walltime_seconds

        path = _write_toml(tmp_path, 'mode = "local"\n[local]\nruns_dir = "/tmp/r"\n')
        g = load_bear_config(path).guardrails
        assert g.qos_allowlist == ("bbshort",)  # default-deny: smoke tier only
        assert g.max_concurrent_jobs > 0  # finite
        assert g.gpu_hours_budget > 0  # finite
        assert parse_walltime_seconds(g.max_walltime) > 0  # a real finite ceiling

    def test_default_local_config_has_tight_guardrails(self) -> None:
        """The synthesised config (used when bear.toml is missing) is also leashed."""
        g = default_local_config().guardrails
        assert g.qos_allowlist == ("bbshort",)
        assert g.gpu_hours_budget > 0

    def test_guardrails_parsed_from_toml(self, tmp_path: Path) -> None:
        body = (
            'mode = "local"\n'
            "[guardrails]\n"
            'qos_allowlist = ["bbshort", "bbgpu"]\n'
            'max_walltime = "08:00:00"\n'
            "max_concurrent_jobs = 4\n"
            "gpu_hours_budget = 16.0\n"
            "require_dry_run = true\n"
        )
        path = _write_toml(tmp_path, body)
        g = load_bear_config(path).guardrails
        assert g.qos_allowlist == ("bbshort", "bbgpu")
        assert g.max_walltime == "08:00:00"
        assert g.max_concurrent_jobs == 4
        assert g.gpu_hours_budget == 16.0
        assert g.require_dry_run is True

    def test_guardrails_compose_with_slurm_mode(self, tmp_path: Path) -> None:
        """``[guardrails]`` is top-level — it sits alongside ``[slurm]``."""
        body = _MINIMAL_SLURM + '[guardrails]\nqos_allowlist = ["bbgpu"]\n'
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path)
        assert cfg.is_slurm
        assert cfg.guardrails.qos_allowlist == ("bbgpu",)

    def test_unknown_guardrails_key_raises(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[guardrails]\nqos_allowlist = ["bbshort"]\nbogus = 1\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match=r"bogus|unknown"):
            load_bear_config(path)

    def test_malformed_walltime_raises(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[guardrails]\nmax_walltime = "not-a-time"\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match="walltime"):
            load_bear_config(path)

    def test_qos_allowlist_must_be_strings(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[guardrails]\nqos_allowlist = [1, 2]\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match="allowlist"):
            load_bear_config(path)


class TestNotifyConfig:
    """``[notify]`` parsing + the opt-in default (the inverse of guardrails).

    Notify is a convenience, not a safety gate, so an *absent* ``[notify]``
    section yields a DISABLED notifier (silent), not a defaulted-on one — the
    deliberate inverse of ``[guardrails]``'s default-deny.
    """

    def test_absent_section_is_disabled(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, 'mode = "local"\n')
        n = load_bear_config(path).notify
        assert n.enabled is False
        assert n.command == ()
        assert n.webhook_url is None

    def test_default_local_config_is_disabled(self) -> None:
        assert default_local_config().notify.enabled is False

    def test_parsed_from_toml(self, tmp_path: Path) -> None:
        body = (
            'mode = "local"\n'
            "[notify]\n"
            "on_done = true\n"
            "on_fail = false\n"
            'command = ["notify-send", "{event}"]\n'
            'webhook_url = "https://hooks.example/x"\n'
            "timeout_seconds = 5.0\n"
        )
        path = _write_toml(tmp_path, body)
        n = load_bear_config(path).notify
        assert n.enabled is True
        assert n.on_done is True
        assert n.on_fail is False
        assert n.command == ("notify-send", "{event}")
        assert n.webhook_url == "https://hooks.example/x"
        assert n.timeout_seconds == 5.0

    def test_command_only_enables(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, 'mode = "local"\n[notify]\ncommand = ["x"]\n')
        assert load_bear_config(path).notify.enabled is True

    def test_webhook_only_enables(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path, 'mode = "local"\n[notify]\nwebhook_url = "https://h"\n')
        assert load_bear_config(path).notify.enabled is True

    def test_composes_with_slurm_mode(self, tmp_path: Path) -> None:
        body = _MINIMAL_SLURM + '[notify]\nwebhook_url = "https://h"\n'
        path = _write_toml(tmp_path, body)
        cfg = load_bear_config(path)
        assert cfg.is_slurm
        assert cfg.notify.webhook_url == "https://h"

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[notify]\nbogus = 1\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match=r"bogus|unknown"):
            load_bear_config(path)

    def test_command_must_be_list_of_strings(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[notify]\ncommand = "echo hi"\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match="command"):
            load_bear_config(path)

    def test_timeout_must_be_positive(self, tmp_path: Path) -> None:
        body = 'mode = "local"\n[notify]\ntimeout_seconds = 0\n'
        path = _write_toml(tmp_path, body)
        with pytest.raises(BearConfigError, match="timeout"):
            load_bear_config(path)
