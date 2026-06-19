# bearpilot/engine — GENERATED, do not edit by hand

This is a mirror of the canonical `bear-harness` engine at the repository root
(`/src/bear_harness` + `/pyproject.toml`), vendored here so the engine ships inside the
plugin's marketplace `source` (`./bearpilot`). The bundled MCP/dashboard launcher
(`bearpilot/harness/lib/ensure-engine.sh`) `pip install`s this directory into a private
venv on first use, so installing the plugin is enough — no separate clone or `install.sh`.

Edit the engine at the repo root, then regenerate this mirror:

    scripts/vendor-engine.sh

CI and the pre-commit hook run `scripts/vendor-engine.sh --check` to block drift.
