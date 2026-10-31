"""What the Action panel, notebooks and Zelos AI can do with the sources.

A failure raises with its reason: the caller sees a failed run only for an exception.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from zelos_log_lines import files
from zelos_log_lines.placing import Timing
from zelos_sdk.actions import action

from . import ACTION_PREFIX, keys, probe, remote, sshconfig
from .config import YEARS, SourceConfig, host_problem, source, start_ns, zone
from .worker import SourceWorker, state_of

#: Source name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, SourceWorker] = {}

_TIMEOUT_S = 5.0
_MIN_COMMAND_S, _MAX_COMMAND_S = 1.0, 120.0
_MAX_LINES = 1000
_MAX_OUTPUT_LINES = 2000
# The first run after an install imports for several seconds on macOS.
_STANDALONE_TIMEOUT_S = 30
# A ceiling for a file of gigabytes, not an expectation.
_CONVERT_TIMEOUT_S = 1800.0


def data_dir() -> Path | None:
    """The extension's writable directory, which an update carries to the new version."""
    value = os.environ.get("ZELOS_DATA_DIR")
    return Path(value) if value else None


_source = action.select(
    "source",
    title="Source name",
    choices=lambda *_: sorted(workers),
    required=False,
)


@action(
    "List Hosts",
    "The hosts in this computer's SSH config, as choices for a host field.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
    read_only=True,
    hidden=True,
)
def list_hosts() -> dict[str, Any]:
    hosts = sshconfig.hosts(Path.home() / ".ssh" / "config")
    choices = [{"value": h.alias, "label": h.alias, "detail": h.detail or None} for h in hosts]
    if not choices:
        return {
            "status": "success",
            "choices": [],
            "message": "No hosts in ~/.ssh/config. Type [user@]host, such as root@192.168.1.20.",
        }
    return {"status": "success", "choices": choices}


@action(
    "Auto-configure",
    "Keep the configured sources and add this computer's system log.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
    read_only=True,
    hidden=True,
)
@action.object("config", title="Configuration", properties={}, required=False)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    sources: list[Any] = [s for s in (config or {}).get("sources") or [] if not _blank(s)]
    has_local = any(
        isinstance(s, dict)
        and s.get("connection") == "local"
        and s.get("read", "system") == "system"
        for s in sources
    )
    if has_local:
        return {"status": "success", "config": {"sources": sources}, "message": "Nothing to add."}
    sources.append({"connection": "local", "read": "system"})
    message = "Added this computer's system log."
    return {"status": "success", "config": {"sources": sources}, "message": message}


def _blank(source: Any) -> bool:
    """An SSH source with nothing typed in, as a new form holds one."""
    return (
        isinstance(source, dict)
        and source.get("connection", "ssh") == "ssh"
        and not any(source.get(key) for key in ("host", "path", "command", "name"))
    )


@action(
    "Test Connection",
    "Connect to a device as a source with these SSH settings would, and say what it will read or "
    "what to fix.",
    timeout=probe.TIMEOUT_S + 5,
    standalone=True,
    # Not read-only: its host and ssh options can reach any server or run a local command.
)
@action.text(
    "host",
    title="Address",
    description="The device's address, as you'd type after ssh, such as me@my-board.",
)
@action.integer(
    "ssh_port",
    title="SSH port",
    description="Empty uses your SSH config, or 22.",
    minimum=1,
    maximum=65535,
    required=False,
)
@action.text(
    "key_path",
    title="Key file",
    description="A private key to use instead of your SSH agent, your default keys and the "
    "key from Show Access Key.",
    required=False,
    default="",
    widget="file_path_picker",
)
@action.select(
    "host_key_policy",
    title="Host key check",
    description="auto trusts any key, so a reflashed device keeps working; strict refuses a key "
    "known_hosts does not have.",
    choices=["auto", "strict"],
    required=False,
    default="auto",
)
@action.text(
    "ssh_options",
    title="Extra ssh options",
    description="Added to the ssh command, such as -J bastion for a jump host.",
    required=False,
    default="",
)
def test_connection(
    host: str,
    ssh_port: int | None = None,
    key_path: str = "",
    host_key_policy: str = "auto",
    ssh_options: str = "",
) -> dict[str, Any]:
    if problem := host_problem(host):
        raise ValueError(f"Address: {problem}.")
    advanced = {
        "ssh_port": ssh_port,
        "key_path": key_path,
        "host_key_policy": host_key_policy,
        "ssh_options": ssh_options,
    }
    try:
        cfg = source({"connection": "ssh", "host": host, "advanced": advanced})
    except ValueError:
        raise ValueError("Extra ssh options: a quote is not closed.") from None
    return _test(cfg)


def _test(cfg: SourceConfig) -> dict[str, Any]:
    argv = remote.ssh_argv(cfg, probe.SCRIPT, keys.path(data_dir()))
    done = remote.run(argv, timeout=probe.TIMEOUT_S)
    if done is None:
        raise TimeoutError(f"{cfg.host} did not answer in time.")
    try:
        board = probe.parse(done.stdout.decode("utf-8", "replace"), time.time_ns())
    except probe.Truncated:
        failure = remote.explain(done.stderr.decode("utf-8", "replace"), cfg.host or "")
        raise RuntimeError(failure.message) from None
    try:
        log = probe.system_log(board, None, "new")
    except probe.NoLog:
        raise RuntimeError(
            f"Connected to {cfg.host}, but it has no system log: no journal, logread or "
            "readable syslog file. Set Log to A file or A command's output."
        ) from None
    reads = {
        "journal": f"the journal (systemd {board.systemd})",
        "logread": "BusyBox's logread",
        "file": board.files[0] if board.files else "a syslog file",
    }[log.kind]
    message = f"Connected to {cfg.host} as {board.user or 'its user'}; it will read {reads}."
    if log.kind == "journal" and board.partial_journal:
        message += f" {probe.partial(board.user)}"
    return {"status": "success", "message": message, "reads": log.kind, "user": board.user}


@action(
    "Show Access Key",
    "This computer's key for devices, made on first use, and the line that adds it to a device.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
)
def show_access_key() -> dict[str, Any]:
    key = keys.ensure(data_dir())
    public_key = keys.public(key)
    return {
        "status": "success",
        "public_key": public_key,
        "install": keys.install_line(public_key),
        "message": "Run the install line on the device, in any shell you have there.",
    }


@action(
    "Convert Log File",
    "Convert a saved log file to a Zelos trace (.trz), each row at the time its line printed. "
    "Runs without the extension running.",
    timeout=_CONVERT_TIMEOUT_S,
    standalone=True,
)
@action.text(
    "input_file",
    title="Log file",
    description="Plain, or compressed with gzip, bzip2, xz or zstd.",
    widget="file_path_picker",
)
@action.text(
    "output_file",
    title="Output (.trz)",
    description="Empty writes it beside the log file.",
    required=False,
    default="",
    widget="file_path_picker",
)
@action.boolean(
    "overwrite",
    title="Overwrite an existing output",
    required=False,
    default=False,
    widget="toggle",
)
@action.text(
    "time_zone",
    title="Time zone",
    description="The time zone of stamps that print none, such as Europe/Berlin. Empty uses this "
    "computer's.",
    required=False,
    default="",
)
@action.integer(
    "year",
    title="Year",
    description="The year of the last stamp that prints none. Empty takes it from the file's last "
    "change.",
    minimum=YEARS[0],
    maximum=YEARS[-1],
    required=False,
)
@action.text(
    "start_time",
    title="Start time",
    description="When the first line printed, for a file that prints only uptime, such as "
    "2026-10-21T14:03:01+02:00. Empty ends the file at its last change.",
    required=False,
    default="",
)
def convert_log_file(
    input_file: str,
    output_file: str = "",
    overwrite: bool = False,
    time_zone: str = "",
    year: int | None = None,
    start_time: str = "",
) -> dict[str, Any]:
    path = Path(input_file).expanduser()
    output, lines = files.convert(
        path,
        Path(output_file).expanduser() if output_file.strip() else None,
        source=ACTION_PREFIX,
        timing=Timing(zone(time_zone), year, start_ns(start_time)),
        overwrite=overwrite,
    )
    return {
        "status": "success",
        "input_file": str(path),
        "output_file": str(output),
        "lines": lines,
    }


@action(
    "Run Command",
    "Run a shell command on a source's host and return what it prints.",
    timeout=_MAX_COMMAND_S + 5,
)
@_source
@action.text("command", title="Command")
@action.number(
    "timeout_s",
    title="Timeout (s)",
    minimum=_MIN_COMMAND_S,
    maximum=_MAX_COMMAND_S,
    required=False,
    default=10.0,
)
def run_command(
    source: str | None = None, *, command: str, timeout_s: float = 10.0
) -> dict[str, Any]:
    if not _MIN_COMMAND_S <= timeout_s <= _MAX_COMMAND_S:
        raise ValueError(f"timeout_s must be {_MIN_COMMAND_S} to {_MAX_COMMAND_S}.")
    cfg = _worker(source).cfg
    if cfg.connection == "demo":
        raise ValueError("The demo device runs no commands.")
    ssh = cfg.connection == "ssh"
    argv = (
        remote.ssh_argv(cfg, remote.wrap(command), keys.path(data_dir()))
        if ssh
        else remote.local_argv(command)
    )
    done = remote.run(
        argv,
        timeout=timeout_s,
        hold_stdin=ssh,
        stderr=subprocess.PIPE if ssh else subprocess.STDOUT,
    )
    if done is None:
        raise TimeoutError(f"The command did not finish in {timeout_s:g} s.")
    if ssh and done.returncode == 255:
        failure = remote.explain(done.stderr.decode("utf-8", "replace"), cfg.host or "")
        raise RuntimeError(failure.message)
    output = done.stdout.decode("utf-8", "replace").splitlines()
    if done.cut:
        # The first line kept is the end of a longer one.
        output = output[1:]
    return {
        "status": "success",
        "exit_code": done.returncode,
        "output": output[-_MAX_OUTPUT_LINES:],
        "truncated": done.cut or len(output) > _MAX_OUTPUT_LINES,
    }


@action(
    "Get State",
    "State, health and counters of a source.",
    timeout=_TIMEOUT_S,
    read_only=True,
    hidden=True,
)
@_source
def get_state(source: str | None = None) -> dict[str, Any]:
    return {"status": "success", **state_of(_worker(source))}


@action(
    "Sample",
    "The last lines a source recorded, as messages.",
    timeout=_TIMEOUT_S,
    read_only=True,
    hidden=True,
)
@_source
@action.integer("lines", title="Lines", minimum=1, maximum=_MAX_LINES, required=False, default=50)
def sample(source: str | None = None, lines: int = 50) -> dict[str, Any]:
    if not 1 <= lines <= _MAX_LINES:
        raise ValueError(f"lines must be 1 to {_MAX_LINES}.")
    return {"status": "success", "lines": _worker(source).sample()[-lines:]}


def _worker(name: str | None) -> SourceWorker:
    """The named source's worker; unset, the only one."""
    if not name:
        if not workers:
            raise RuntimeError("No sources are running. Start the extension.")
        if len(workers) > 1:
            names = ", ".join(sorted(workers))
            raise ValueError(f"Several sources are running; set source to one of: {names}.")
        return next(iter(workers.values()))
    worker = workers.get(name)
    if worker is None:
        known = ", ".join(sorted(workers)) or "none"
        raise ValueError(f"No source named '{name}'. Sources: {known}.")
    return worker
