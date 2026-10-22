"""What the Action panel, notebooks and Zelos AI can do with the sources."""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from zelos_log_lines import files
from zelos_log_lines.placing import Timing
from zelos_sdk.actions import SelectField, action, register_field_type

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


def _first_source() -> str | None:
    return next(iter(workers), None)


@register_field_type("logs_source")
class _SourceField(SelectField):
    """A source name field whose form default is the first configured source."""

    def to_json_schema(self, current_values: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = super().to_json_schema(current_values)
        # The SDK's `default` is fixed at import; the sources are known only once the app runs.
        schema["default"] = _first_source()
        return schema


_source = action.input(
    "source",
    type="logs_source",
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
    "Keep the configured sources and add this computer's system log on Linux, or the demo device "
    "when there are no sources and no system log here.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
    read_only=True,
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
    if sys.platform == "linux" and not has_local:
        sources.append({"connection": "local", "read": "system"})
        message = "Added this computer's system log."
    elif sources:
        message = "Nothing to add."
    else:
        sources = [{"connection": "demo"}]
        message = "The system log is read on Linux; added the demo device."
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
    "Connect to a host as a source with these SSH settings would, and say what it will read or "
    "what to fix.",
    timeout=probe.TIMEOUT_S + 5,
    standalone=True,
    read_only=True,
)
@action.text("host", title="Host", description="[user@]host, or a Host from your SSH config.")
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
    "access key.",
    required=False,
    default="",
    widget="file_path_picker",
)
@action.select(
    "host_key_policy",
    title="Host key check",
    description="auto trusts the board's key; strict refuses a key known_hosts does not have.",
    choices=["auto", "strict"],
    required=False,
    default="auto",
)
@action.text(
    "ssh_options",
    title="Extra ssh options",
    description="Passed to ssh before the options above, such as -J bastion.",
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
        return {"status": "error", "message": f"Host: {problem}."}
    advanced = {
        "ssh_port": ssh_port,
        "key_path": key_path,
        "host_key_policy": host_key_policy,
        "ssh_options": ssh_options,
    }
    try:
        cfg = source({"connection": "ssh", "host": host, "advanced": advanced})
    except ValueError:
        return {"status": "error", "message": "Extra ssh options: a quote is not closed."}
    return _test(cfg)


def _test(cfg: SourceConfig) -> dict[str, Any]:
    try:
        argv = remote.ssh_argv(cfg, probe.SCRIPT, keys.path(data_dir()))
    except remote.SshMissing as e:
        return {"status": "error", "message": str(e)}
    done = remote.run(argv, timeout=probe.TIMEOUT_S)
    if done is None:
        return {"status": "error", "message": f"{cfg.host} did not answer in time."}
    try:
        board = probe.parse(done.stdout.decode("utf-8", "replace"), time.time_ns())
    except probe.Truncated:
        failure = remote.explain(done.stderr.decode("utf-8", "replace"), cfg.host or "")
        return {"status": "error", "message": failure.message}
    try:
        log = probe.system_log(board, None, "new")
    except probe.NoLog:
        return {
            "status": "error",
            "message": f"Connected to {cfg.host}, but it keeps no log this extension reads.",
        }
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
    "This computer's key for boards, made on first use, and the line that adds it to a board.",
    timeout=_STANDALONE_TIMEOUT_S,
    standalone=True,
)
def show_access_key() -> dict[str, Any]:
    try:
        key = keys.ensure(data_dir())
    except keys.KeyFailure as e:
        return {"status": "error", "message": str(e)}
    public_key = keys.public(key)
    return {
        "status": "success",
        "public_key": public_key,
        "install": keys.install_line(public_key),
        "message": "Run the install line on the board, in any shell you have there.",
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
    # A failure raises: a standalone action's exit status is how its caller learns of it.
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
        return {
            "status": "error",
            "message": f"timeout_s must be {_MIN_COMMAND_S} to {_MAX_COMMAND_S}.",
        }
    worker = _worker(source)
    if isinstance(worker, dict):
        return worker
    cfg = worker.cfg
    if cfg.connection == "demo":
        return {"status": "error", "message": "The demo device runs no commands."}
    ssh = cfg.connection == "ssh"
    try:
        argv = (
            remote.ssh_argv(cfg, remote.wrap(command), keys.path(data_dir()))
            if ssh
            else remote.local_argv(command)
        )
    except remote.SshMissing as e:
        return {"status": "error", "message": str(e)}
    done = remote.run(
        argv,
        timeout=timeout_s,
        hold_stdin=ssh,
        stderr=subprocess.PIPE if ssh else subprocess.STDOUT,
    )
    if done is None:
        return {"status": "error", "message": f"The command did not finish in {timeout_s:g} s."}
    if ssh and done.returncode == 255:
        failure = remote.explain(done.stderr.decode("utf-8", "replace"), cfg.host or "")
        return {"status": "error", "message": failure.message}
    output = done.stdout.decode("utf-8", "replace").splitlines()
    return {
        "status": "success",
        "exit_code": done.returncode,
        "output": output[-_MAX_OUTPUT_LINES:],
        "truncated": len(output) > _MAX_OUTPUT_LINES,
    }


@action(
    "Get State",
    "State, health and counters of a source.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_source
def get_state(source: str | None = None) -> dict[str, Any]:
    worker = _worker(source)
    if isinstance(worker, dict):
        return worker
    return {"status": "success", **state_of(worker)}


@action(
    "Sample",
    "The last lines a source recorded, as messages.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_source
@action.integer("lines", title="Lines", minimum=1, maximum=_MAX_LINES, required=False, default=50)
def sample(source: str | None = None, lines: int = 50) -> dict[str, Any]:
    if not 1 <= lines <= _MAX_LINES:
        return {"status": "error", "message": f"lines must be 1 to {_MAX_LINES}."}
    worker = _worker(source)
    if isinstance(worker, dict):
        return worker
    return {"status": "success", "lines": worker.sample()[-lines:]}


def _worker(name: str | None) -> SourceWorker | dict[str, Any]:
    name = name or _first_source()
    if name is None:
        return {"status": "error", "message": "No sources are running. Start the extension."}
    worker = workers.get(name)
    if worker is None:
        known = ", ".join(sorted(workers)) or "none"
        return {"status": "error", "message": f"No source named '{name}'. Sources: {known}."}
    return worker
