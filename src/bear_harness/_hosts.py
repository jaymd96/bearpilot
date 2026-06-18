"""Laptop-side host registry — ``~/.config/bear-harness/hosts.toml``.

Where ``bear.toml`` configures the *cluster* (read on the login node), this file
configures the *laptop's* view of the clusters it can reach over SSH: the SSH
alias (which delegates keys / jumphosts / 2FA to ``~/.ssh/config``), the remote
RDS root + inbox, the remote ``bear-harness`` binary, and a local cache dir for
fetched artifacts.

It is consumed only by the transport (``_remote.py``) and the MCP server — never
by the kernel, which knows nothing of laptops. See
``docs/decision-notes/mcp-over-ssh-transport.md`` and ``docs/next-steps.md`` §2.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOSTS_PATH = Path.home() / ".config" / "bear-harness" / "hosts.toml"


class HostsConfigError(ValueError):
    """Raised for any structural problem with ``hosts.toml``."""


@dataclass(frozen=True, slots=True)
class Host:
    """One reachable cluster, as the laptop sees it.

    ``ssh_alias`` is a ``~/.ssh/config`` ``Host`` entry — all networking (keys,
    jumphosts, ``ControlPersist``, ``ServerAliveInterval``, 2FA) lives there, not
    here; the harness does not reinvent SSH config. ``remote_binary`` is the
    ``bear-harness`` on the login node's PATH (installed by ``remote install``).
    ``artifacts_cache`` is the laptop dir ``fetch`` pulls into. Remote paths are
    kept as plain strings — they are resolved on the cluster, not the laptop.
    """

    name: str
    ssh_alias: str
    remote_rds_root: str
    remote_inbox: str
    remote_binary: str = "bear-harness"
    artifacts_cache: str = "~/.cache/bear-harness/fetched"


@dataclass(frozen=True, slots=True)
class HostsConfig:
    """The parsed ``hosts.toml``: the set of hosts plus the default host name."""

    hosts: tuple[Host, ...] = ()
    default: str | None = None

    def resolve(self, name: str | None = None) -> Host:
        """Return the named host, or the configured default when ``name`` is None."""
        target = name or self.default
        if target is None:
            msg = "no host specified and no default set in hosts.toml"
            raise HostsConfigError(msg)
        for h in self.hosts:
            if h.name == target:
                return h
        known = ", ".join(sorted(h.name for h in self.hosts)) or "(none)"
        msg = f"unknown host {target!r} (known: {known})"
        raise HostsConfigError(msg)


_TOP_LEVEL_KEYS = {"default", "hosts"}
_HOST_KEYS = {"ssh_alias", "remote_rds_root", "remote_inbox", "remote_binary", "artifacts_cache"}
_HOST_REQUIRED = {"ssh_alias", "remote_rds_root", "remote_inbox"}


def load_hosts(path: str | Path | None = None) -> HostsConfig:
    """Load ``hosts.toml``. A missing default file yields an empty registry.

    The caller normally passes ``None`` (use the default path). A missing default
    file is not an error — there may simply be no remote hosts configured yet — so
    an empty :class:`HostsConfig` is returned. An explicit path that does not
    exist *is* an error.
    """
    p = Path(path).expanduser().resolve() if path else DEFAULT_HOSTS_PATH
    if not p.is_file():
        if path is None:
            return HostsConfig()
        msg = f"hosts.toml not found at {p}"
        raise HostsConfigError(msg)

    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        msg = f"failed to parse {p}: {exc}"
        raise HostsConfigError(msg) from exc

    unknown = set(data.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        msg = f"unknown top-level keys in {p}: {sorted(unknown)}"
        raise HostsConfigError(msg)

    default = data.get("default")
    if default is not None and not isinstance(default, str):
        msg = "hosts.toml 'default' must be a string"
        raise HostsConfigError(msg)

    raw_hosts = data.get("hosts", {})
    if not isinstance(raw_hosts, dict):
        msg = "hosts.toml [hosts] must be a table of host tables"
        raise HostsConfigError(msg)

    hosts = tuple(_parse_host(name, body) for name, body in raw_hosts.items())

    if default is not None and not any(h.name == default for h in hosts):
        msg = f"hosts.toml default {default!r} names no [hosts.*] entry"
        raise HostsConfigError(msg)

    return HostsConfig(hosts=hosts, default=default)


def _parse_host(name: str, d: object) -> Host:
    if not isinstance(d, dict):
        msg = f"[hosts.{name}] must be a table"
        raise HostsConfigError(msg)
    unknown = set(d.keys()) - _HOST_KEYS
    if unknown:
        msg = f"unknown keys in [hosts.{name}]: {sorted(unknown)}"
        raise HostsConfigError(msg)
    missing = _HOST_REQUIRED - set(d.keys())
    if missing:
        msg = f"missing required keys in [hosts.{name}]: {sorted(missing)}"
        raise HostsConfigError(msg)
    return Host(
        name=name,
        ssh_alias=str(d["ssh_alias"]),
        remote_rds_root=str(d["remote_rds_root"]),
        remote_inbox=str(d["remote_inbox"]),
        remote_binary=str(d.get("remote_binary", "bear-harness")),
        artifacts_cache=str(d.get("artifacts_cache", "~/.cache/bear-harness/fetched")),
    )


__all__ = [
    "DEFAULT_HOSTS_PATH",
    "Host",
    "HostsConfig",
    "HostsConfigError",
    "load_hosts",
]
