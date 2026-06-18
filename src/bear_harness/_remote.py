"""The one SSH core — the laptop's seam to a BlueBEAR login node.

Both front-ends (the agent's MCP server and the human's ``--remote`` CLI flag)
lower to this module; it is the *only* place SSH lives. The kernel never imports
it (no SSH inside the kernel — see
``docs/decision-notes/login-node-orchestrator.md`` and
``docs/decision-notes/mcp-over-ssh-transport.md``).

Design points the canary will confirm but the unit tests already pin:

- **Injectable exec seam.** Every cluster touch goes through ``SshRunner`` (a
  ``Callable[[Sequence[str]], SshResult]``), exactly like ``_slurm_runner``'s
  ``ShellRunner``. The default shells out to ``ssh`` / ``rsync``; tests inject a
  recording stub and never open a socket.
- **Connection reuse via ``ControlMaster``.** A persistent master socket
  multiplexes subsequent commands onto the *same* login node — the practical
  node-pinning mechanism on round-robin login nodes. Keys / jumphosts / 2FA stay
  in ``~/.ssh/config`` (the ``ssh_alias`` indirection); we do not reinvent SSH.
- **Brain on the cluster; state on shared RDS.** ``status`` / ``logs`` / ``fetch``
  are node-independent — they read run state off the shared filesystem via
  ``ssh cat`` / ``rsync``, never by polling a PID. The laptop keeps only a tiny
  pointer file so any session reattaches by run-ref.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bear_harness._hosts import Host

# Laptop-side scratch: the ControlMaster sockets and the run pointer files.
_CACHE_DIR = Path.home() / ".cache" / "bear-harness"
_CM_DIR = _CACHE_DIR / "cm"
_REMOTE_RUNS_DIR = _CACHE_DIR / "remote-runs"
_CONTROL_PERSIST = "120s"


class RemoteError(RuntimeError):
    """Raised when a remote operation fails in a way the caller must handle."""


@dataclass(frozen=True, slots=True)
class SshResult:
    """Thin wrapper over a completed ``ssh`` / ``rsync`` invocation (test seam)."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


SshRunner = Callable[[Sequence[str]], SshResult]


def _default_run(argv: Sequence[str]) -> SshResult:
    """Real ``subprocess.run`` implementation used in production."""
    cp = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return SshResult(returncode=cp.returncode, stdout=cp.stdout or "", stderr=cp.stderr or "")


# squeue, header-less and '|'-delimited so it parses without column heuristics.
# Fields: jobid | name | qos | state | time-used | time-limit | nodes | reason.
# GRES is deliberately omitted — its squeue format code is a version trap
# (references/slurm-cli.md); the dashboard shows qos/state/elapsed, which are stable.
_SQUEUE_FORMAT = "%i|%j|%q|%T|%M|%l|%D|%R"
_SQUEUE_FIELDS = 8


@dataclass(frozen=True, slots=True)
class JobRow:
    """One row of ``squeue --me`` — a live SLURM job, parsed into fields."""

    job_id: str
    name: str
    qos: str
    state: str
    elapsed: str
    time_limit: str
    nodes: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "qos": self.qos,
            "state": self.state,
            "elapsed": self.elapsed,
            "time_limit": self.time_limit,
            "nodes": self.nodes,
            "reason": self.reason,
        }


def _parse_squeue(stdout: str) -> tuple[JobRow, ...]:
    """Parse header-less, '|'-delimited ``squeue`` output into :class:`JobRow`s.

    Short rows are padded (a missing trailing reason is common); extra '|' in the
    reason field are rejoined so a nodelist with a pipe never shifts columns.
    """
    rows: list[JobRow] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < _SQUEUE_FIELDS:
            parts += [""] * (_SQUEUE_FIELDS - len(parts))
        elif len(parts) > _SQUEUE_FIELDS:
            parts = [*parts[: _SQUEUE_FIELDS - 1], "|".join(parts[_SQUEUE_FIELDS - 1 :])]
        rows.append(
            JobRow(
                job_id=parts[0],
                name=parts[1],
                qos=parts[2],
                state=parts[3],
                elapsed=parts[4],
                time_limit=parts[5],
                nodes=parts[6],
                reason=parts[7],
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class RemoteRun:
    """The laptop's pointer to one remote campaign — the whole reattach key.

    Everything else is derived by ``ssh cat``'ing ``remote_run_dir/run.json`` on
    the shared filesystem, so this stays tiny and any session can reattach. The
    ``node`` (concrete login-node hostname captured at launch) exists ONLY so
    ``cancel`` can reach the right node to reap the orchestrator process; status
    never uses it (RDS is cluster-global).
    """

    run_ref: str
    host: str
    node: str
    remote_run_dir: str
    orchestrator_pid: str
    inbox_dir: str

    def as_dict(self) -> dict:
        return {
            "run_ref": self.run_ref,
            "host": self.host,
            "node": self.node,
            "remote_run_dir": self.remote_run_dir,
            "orchestrator_pid": self.orchestrator_pid,
            "inbox_dir": self.inbox_dir,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RemoteRun:
        return cls(
            run_ref=str(d["run_ref"]),
            host=str(d["host"]),
            node=str(d.get("node", "")),
            remote_run_dir=str(d["remote_run_dir"]),
            orchestrator_pid=str(d.get("orchestrator_pid", "")),
            inbox_dir=str(d.get("inbox_dir", "")),
        )


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """One aggregated view for the experiment dashboard: live jobs + known runs.

    ``error`` is populated (and ``jobs`` left empty) when the ``squeue`` poll
    fails, so the dashboard always renders — degrade, never crash — mirroring the
    observability discipline (the dashboard is a monitor, not a gate).
    """

    host: str
    jobs: tuple[JobRow, ...]
    runs: tuple[RemoteRun, ...]
    error: str = ""
    commands: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def running(self) -> int:
        return sum(1 for j in self.jobs if j.state.upper() == "RUNNING")

    @property
    def pending(self) -> int:
        return sum(1 for j in self.jobs if j.state.upper() == "PENDING")

    @property
    def active(self) -> int:
        return len(self.jobs)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "running": self.running,
            "pending": self.pending,
            "active": self.active,
            "jobs": [j.as_dict() for j in self.jobs],
            "runs": [r.as_dict() for r in self.runs],
            "commands": [dict(c) for c in self.commands],
            "error": self.error,
        }


def _runs_dir(override: Path | None = None) -> Path:
    """Resolve the laptop pointer-file dir: explicit arg > env > default.

    The ``BEAR_HARNESS_REMOTE_RUNS`` env override exists so the CLI can be driven
    in tests without writing into the real ``~/.cache``.
    """
    if override is not None:
        return override
    env = os.environ.get("BEAR_HARNESS_REMOTE_RUNS")
    return Path(env) if env else _REMOTE_RUNS_DIR


def pointer_path(host: str, run_ref: str, *, runs_dir: Path | None = None) -> Path:
    """Laptop path of the pointer file for ``host``/``run_ref``."""
    return _runs_dir(runs_dir) / f"{host}-{run_ref}.json"


def write_remote_run(run: RemoteRun, *, runs_dir: Path | None = None) -> Path:
    target = _runs_dir(runs_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = pointer_path(run.host, run.run_ref, runs_dir=target)
    path.write_text(json.dumps(run.as_dict(), indent=2))
    return path


def read_remote_run(host: str, run_ref: str, *, runs_dir: Path | None = None) -> RemoteRun:
    path = pointer_path(host, run_ref, runs_dir=_runs_dir(runs_dir))
    if not path.is_file():
        msg = f"no pointer file for {host}/{run_ref} at {path}"
        raise RemoteError(msg)
    return RemoteRun.from_dict(json.loads(path.read_text()))


def list_remote_runs(*, runs_dir: Path | None = None) -> list[RemoteRun]:
    target = _runs_dir(runs_dir)
    if not target.is_dir():
        return []
    out: list[RemoteRun] = []
    for p in sorted(target.glob("*.json")):
        try:
            out.append(RemoteRun.from_dict(json.loads(p.read_text())))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def _control_opts(*, cm_dir: Path = _CM_DIR) -> list[str]:
    """``ControlMaster`` opts that multiplex onto one persistent login-node socket."""
    cm_dir.mkdir(parents=True, exist_ok=True)
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={cm_dir}/%C",
        "-o",
        f"ControlPersist={_CONTROL_PERSIST}",
    ]


@dataclass(slots=True)
class RemoteExecutor:
    """Drive one :class:`Host` over SSH. All cluster I/O flows through ``run_shell``."""

    host: Host
    run_shell: SshRunner = _default_run
    cm_dir: Path = _CM_DIR

    # -- low-level ssh / rsync -------------------------------------------------

    def _ssh_argv(self, remote_command: str, *, node: str | None = None) -> list[str]:
        """``ssh <controlmaster opts> <alias> <command>``.

        ``node`` overrides the connection host (via ``-o Hostname=``) so a reaper
        can target the *specific* login node a campaign was launched on; status
        paths leave it None and ride the multiplexed alias.
        """
        argv = ["ssh", *_control_opts(cm_dir=self.cm_dir)]
        if node:
            argv += ["-o", f"Hostname={node}"]
        argv += [self.host.ssh_alias, remote_command]
        return argv

    def run(self, remote_argv: Sequence[str], *, node: str | None = None) -> SshResult:
        """Run an argv on the login node (quoted as one remote command)."""
        return self.run_shell(self._ssh_argv(shlex.join(remote_argv), node=node))

    def cat(self, remote_path: str) -> str:
        """Return the contents of a remote file (the ``ssh cat`` reattach path)."""
        res = self.run(["cat", remote_path])
        if not res.ok:
            msg = f"could not read {remote_path} on {self.host.name}: {res.stderr.strip()}"
            raise RemoteError(msg)
        return res.stdout

    def _rsync_e(self) -> str:
        """The ``-e`` transport string so rsync reuses the same ControlMaster."""
        return shlex.join(["ssh", *_control_opts(cm_dir=self.cm_dir)])

    def rsync_push(self, local: Path, remote_dir: str) -> SshResult:
        argv = [
            "rsync",
            "-az",
            "--delete",
            "-e",
            self._rsync_e(),
            f"{local}/",
            f"{self.host.ssh_alias}:{remote_dir}/",
        ]
        return self.run_shell(argv)

    def rsync_pull(self, remote_path: str, local: Path) -> SshResult:
        local.mkdir(parents=True, exist_ok=True)
        argv = [
            "rsync",
            "-az",
            "-e",
            self._rsync_e(),
            f"{self.host.ssh_alias}:{remote_path}",
            f"{local}/",
        ]
        return self.run_shell(argv)

    def rsync_push_file(self, local_file: Path, remote_dir: str) -> SshResult:
        """Push a single file (e.g. a wheel) into a remote directory."""
        argv = [
            "rsync",
            "-az",
            "-e",
            self._rsync_e(),
            str(local_file),
            f"{self.host.ssh_alias}:{remote_dir}/",
        ]
        return self.run_shell(argv)

    # -- campaign lifecycle ----------------------------------------------------

    def launch_detached(
        self,
        local_program_dir: Path,
        inbox_name: str,
        *,
        poll_attempts: int = 30,
        sleep: Callable[[float], None] = time.sleep,
        runs_dir: Path | None = None,
    ) -> RemoteRun:
        """Upload a program and start the orchestrator on the login node, detached.

        Mirrors ``next-steps.md`` §2: rsync the program into the remote inbox,
        ``nohup bear-harness launch . --detach --json`` so it survives the laptop
        closing, capture the orchestrator PID + the concrete login node, then poll
        ``orchestrator.log`` for the kernel's JSON handle to learn the run dir.
        Returns the persisted :class:`RemoteRun`.
        """
        inbox = f"{self.host.remote_inbox}/{inbox_name}"
        push = self.rsync_push(local_program_dir, inbox)
        if not push.ok:
            msg = f"rsync of {local_program_dir} to {self.host.name}:{inbox} failed: {push.stderr.strip()}"
            raise RemoteError(msg)

        log = f"{inbox}/orchestrator.log"
        binary = self.host.remote_binary
        start = (
            f"cd {shlex.quote(inbox)} && "
            f"nohup {shlex.quote(binary)} launch . --detach --json > {shlex.quote(log)} 2>&1 & "
            'echo "pid=$!"; hostname'
        )
        started = self.run(["sh", "-lc", start])
        if not started.ok:
            msg = f"failed to start orchestrator on {self.host.name}: {started.stderr.strip()}"
            raise RemoteError(msg)
        pid, node = _parse_pid_and_host(started.stdout)

        handle = self._await_handle(log, attempts=poll_attempts, sleep=sleep)
        run = RemoteRun(
            run_ref=str(handle.get("job_id") or inbox_name),
            host=self.host.name,
            node=node,
            remote_run_dir=str(handle.get("run_dir", "")),
            orchestrator_pid=pid,
            inbox_dir=inbox,
        )
        write_remote_run(run, runs_dir=runs_dir)
        return run

    def _await_handle(
        self, log_path: str, *, attempts: int, sleep: Callable[[float], None]
    ) -> dict:
        """Poll ``orchestrator.log`` until the kernel's ``--json`` handle appears."""
        for i in range(attempts):
            res = self.run(["cat", log_path])
            if res.ok:
                handle = _first_json_object(res.stdout)
                if handle is not None:
                    return handle
            if i < attempts - 1:
                sleep(1.0)
        msg = f"orchestrator on {self.host.name} produced no JSON handle in {log_path}"
        raise RemoteError(msg)

    def read_run_json(self, run: RemoteRun) -> dict:
        """The reattach read: ``ssh cat remote_run_dir/run.json`` (node-independent)."""
        return json.loads(self.cat(f"{run.remote_run_dir}/run.json"))

    def cancel(self, run: RemoteRun) -> tuple[SshResult, ...]:
        """Stop a campaign: ``scancel`` its SLURM jobs, then reap the orchestrator.

        ``scancel`` is node-independent (SLURM is cluster-global) and is the
        load-bearing step — it stops the GPU spend. The orchestrator-PID kill is
        best-effort cleanup, aimed at the captured ``node``; a miss is logged, not
        fatal, because the jobs are already cancelled.
        """
        results: list[SshResult] = []
        try:
            state = self.read_run_json(run)
            job_ids = [
                str(state.get(k, "")) for k in ("vllm_job_id", "pipeline_job_id")
            ]
        except (RemoteError, json.JSONDecodeError):
            job_ids = []
        live_jobs = [j for j in job_ids if j]
        if live_jobs:
            results.append(self.run(["scancel", *live_jobs]))
        if run.orchestrator_pid:
            results.append(
                self.run(["kill", run.orchestrator_pid], node=run.node or None)
            )
        return tuple(results)

    # -- monitoring (read-only, node-independent) ------------------------------

    def list_jobs(self) -> tuple[JobRow, ...]:
        """Your in-flight SLURM jobs, parsed (``squeue --me`` — never a PID)."""
        res = self.run(["squeue", "--me", "-h", "-o", _SQUEUE_FORMAT])
        if not res.ok:
            msg = f"squeue failed on {self.host.name}: {res.stderr.strip()}"
            raise RemoteError(msg)
        return _parse_squeue(res.stdout)

    def tail_run_logs(
        self, run_ref: str, *, which: str = "both", lines: int = 50, runs_dir: Path | None = None
    ) -> str:
        """Tail a run's vllm/pipeline logs off the shared FS (the shared log path).

        Reused by both the MCP ``logs`` tool and the live dashboard server, so the
        log-reading shape lives in one place.
        """
        run = read_remote_run(self.host.name, run_ref, runs_dir=runs_dir)
        out: list[str] = []
        for name in ("vllm", "pipeline"):
            if which not in {name, "both"}:
                continue
            res = self.run(["tail", "-n", str(lines), f"{run.remote_run_dir}/{name}.log"])
            out.append(f"=== {name}.log ===\n{res.stdout}")
        return "\n".join(out)

    def _audit_path(self) -> str:
        return f"{self.host.remote_rds_root}/.bear-harness/launchpad-audit.jsonl"

    def record_command(self, verb: str, detail: str = "", *, ts: str | None = None) -> dict:
        """Append one JSONL line to the shared-FS command audit (best-effort).

        Durable + cross-session: any future session reads ``read_audit`` to see
        every deploy/cancel run against this host, regardless of who issued it. A
        small JSONL line is below ``PIPE_BUF`` so the append is atomic under
        concurrent writers. Failure to record never derails the underlying action.
        """
        entry = {
            "ts": ts or datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "verb": verb,
            "detail": detail,
            "host": self.host.name,
        }
        line = json.dumps(entry, separators=(",", ":"))
        audit_dir = f"{self.host.remote_rds_root}/.bear-harness"
        cmd = (
            f"mkdir -p {shlex.quote(audit_dir)} && "
            f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(self._audit_path())}"
        )
        self.run(["sh", "-lc", cmd])
        return entry

    def read_audit(self, limit: int = 20) -> tuple[dict, ...]:
        """The last ``limit`` audit entries, newest first. Empty if no audit yet."""
        res = self.run(
            ["sh", "-lc", f"tail -n {int(limit)} {shlex.quote(self._audit_path())} 2>/dev/null || true"]
        )
        entries: list[dict] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
        return tuple(reversed(entries))

    def dashboard_snapshot(
        self, *, runs_dir: Path | None = None, with_commands: bool = False, audit_limit: int = 10
    ) -> DashboardSnapshot:
        """Aggregate live jobs + the laptop's known run-refs (+ optional audit).

        Degrades to an empty job list with ``error`` set if ``squeue`` fails, so a
        transient cluster hiccup never blanks the whole dashboard. ``with_commands``
        adds the shared-FS audit tail (one extra ``tail`` round-trip).
        """
        error = ""
        try:
            jobs = self.list_jobs()
        except RemoteError as exc:
            jobs, error = (), str(exc)
        runs = tuple(
            r for r in list_remote_runs(runs_dir=runs_dir) if r.host == self.host.name
        )
        commands: tuple[dict, ...] = ()
        if with_commands:
            try:
                commands = self.read_audit(audit_limit)
            except RemoteError:
                commands = ()
        return DashboardSnapshot(
            host=self.host.name, jobs=jobs, runs=runs, error=error, commands=commands
        )


def _parse_pid_and_host(stdout: str) -> tuple[str, str]:
    """Extract ``pid=<n>`` and the trailing ``hostname`` line from the start output."""
    pid = ""
    node = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            pid = line[len("pid=") :].strip()
        elif line and not line.startswith("pid="):
            node = line
    return pid, node


def _first_json_object(text: str) -> dict | None:
    """Return the first parseable top-level JSON object in ``text`` (the handle)."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(obj, dict):
                    return obj
    return None


__all__ = [
    "DashboardSnapshot",
    "JobRow",
    "RemoteError",
    "RemoteExecutor",
    "RemoteRun",
    "SshResult",
    "SshRunner",
    "list_remote_runs",
    "pointer_path",
    "read_remote_run",
    "write_remote_run",
]
