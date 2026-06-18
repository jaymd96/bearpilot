"""Unit tests for ``bear_harness._substitute``."""

from __future__ import annotations

import pytest

from bear_harness._substitute import (
    KNOWN_VARS,
    SubstitutionError,
    substitute,
    substitute_all,
    substitute_env,
)


class TestBasicSubstitution:
    def test_plain_variable(self) -> None:
        assert substitute("$OUTPUT_DIR/x", {"OUTPUT_DIR": "/tmp/run"}) == "/tmp/run/x"

    def test_braced_variable(self) -> None:
        assert (
            substitute("prefix-${MODEL_NAME}-suffix", {"MODEL_NAME": "Llama"})
            == "prefix-Llama-suffix"
        )

    def test_multiple_variables(self) -> None:
        out = substitute(
            "$PYTHON -m foo --url=$MODEL_BASE_URL --key=$MODEL_API_KEY",
            {
                "PYTHON": "python3",
                "MODEL_BASE_URL": "http://h:8000/v1",
                "MODEL_API_KEY": "dummy",
            },
        )
        assert out == "python3 -m foo --url=http://h:8000/v1 --key=dummy"

    def test_escape_dollar(self) -> None:
        assert substitute("price: $$5", {}) == "price: $5"

    def test_escape_then_variable(self) -> None:
        assert (
            substitute("$$$PROGRAM_ROOT/$$", {"PROGRAM_ROOT": "/p"})
            == "$/p/$"
        )

    def test_no_variables(self) -> None:
        assert substitute("plain text", {}) == "plain text"


class TestErrorCases:
    def test_unknown_variable_name(self) -> None:
        with pytest.raises(SubstitutionError, match="unknown substitution"):
            substitute("$NOT_A_VAR", {"NOT_A_VAR": "x"})

    def test_missing_value(self) -> None:
        with pytest.raises(SubstitutionError, match="has no value"):
            substitute("$OUTPUT_DIR", {})

    def test_all_known_vars_exist(self) -> None:
        # Sanity: the closed set is non-empty and contains the things
        # the launch flow actually uses.
        assert "OUTPUT_DIR" in KNOWN_VARS
        assert "MODEL_BASE_URL" in KNOWN_VARS
        assert "PYTHON" in KNOWN_VARS


class TestHelpers:
    def test_substitute_all(self) -> None:
        out = substitute_all(
            ("$PYTHON", "-c", "print($$)"),
            {"PYTHON": "python3"},
        )
        assert out == ("python3", "-c", "print($)")

    def test_substitute_env(self) -> None:
        out = substitute_env(
            {"STATUS_PATH": "$OUTPUT_DIR/status.json", "FLAG": "1"},
            {"OUTPUT_DIR": "/out"},
        )
        assert out == {"STATUS_PATH": "/out/status.json", "FLAG": "1"}
