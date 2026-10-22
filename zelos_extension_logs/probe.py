"""What a board keeps: which log to follow, who we are there, and its clock."""

import shlex
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

# One POSIX sh line, run before every stream. BusyBox prints `date +%N` as nothing, so the clock is
# whole seconds and uptime gives the fine part. `logread` is tried, not just found: without
# syslogd's shared-memory buffer it exists and fails. `end` proves the output arrived whole.
SCRIPT = (
    'p() { printf \'%s=%s\\n\' "$1" "$2"; }; '
    'p uid "$(id -u 2>/dev/null)"; '
    'p user "$(id -un 2>/dev/null)"; '
    "p groups \"$(id -Gn 2>/dev/null | tr ' ' ',')\"; "
    "if command -v journalctl >/dev/null 2>&1; then "
    'p systemd "$(journalctl --version 2>/dev/null'
    " | sed -n '1s/^systemd \\([0-9][0-9]*\\).*/\\1/p')\"; fi; "
    "if command -v logread >/dev/null 2>&1 && logread >/dev/null 2>&1; then p logread 1; fi; "
    'for f in /var/log/messages /var/log/syslog; do [ -r "$f" ] && p file "$f"; done; '
    'p boot_id "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
    "p uptime \"$(cut -d' ' -f1 /proc/uptime 2>/dev/null)\"; "
    'p clock "$(date +%s 2>/dev/null)"; '
    "p end 1"
)

# Groups that read every journal entry. Anyone else sees only their own.
_JOURNAL_READERS = {"root", "systemd-journal", "adm", "wheel"}
# The journal fields a row needs; the cursor, both clocks and the boot ID always come too.
_FIELDS = (
    "MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_COMM,_SYSTEMD_UNIT,_PID,CODE_FILE,CODE_LINE,_TRANSPORT"
)
# How far back a first start reads the journal when the agent does not say what it keeps.
HISTORY = timedelta(hours=1)
# The probe's whole run, its connect included.
TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True)
class Probe:
    """One probe's answers, and the host time halfway through it."""

    uid: int | None
    user: str
    groups: frozenset[str]
    systemd: int | None
    logread: bool
    files: tuple[str, ...]
    boot_id: str
    uptime_ns: int | None
    clock_s: int | None
    host_ns: int

    @property
    def partial_journal(self) -> bool:
        """True when the journal will show only this user's own entries."""
        return self.uid != 0 and not (self.groups & _JOURNAL_READERS)


def partial(user: str) -> str:
    """What a user who sees only their own journal entries should do about it."""
    return (
        f"{user} is not root and not in systemd-journal, so the journal shows only its own "
        f"entries. On the board, run: sudo usermod -aG systemd-journal {user}"
    )


class Truncated(Exception):
    """The probe's output ended before its last line."""


def parse(output: str, host_ns: int) -> Probe:
    """The probe's `key=value` lines."""
    values: dict[str, str] = {}
    files: list[str] = []
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "file":
            files.append(value)
        else:
            values[key] = value
    if values.get("end") != "1":
        raise Truncated
    uptime = values.get("uptime", "")
    return Probe(
        uid=int(values["uid"]) if values.get("uid", "").isdecimal() else None,
        user=values.get("user", ""),
        groups=frozenset(g for g in values.get("groups", "").split(",") if g),
        systemd=int(values["systemd"]) if values.get("systemd", "").isdecimal() else None,
        logread=values.get("logread") == "1",
        files=tuple(files),
        boot_id=values.get("boot_id", "").replace("-", ""),
        uptime_ns=_seconds_ns(uptime),
        clock_s=int(values["clock"]) if values.get("clock", "").isdecimal() else None,
        host_ns=host_ns,
    )


def _seconds_ns(text: str) -> int | None:
    whole, _, fraction = text.partition(".")
    if not whole.isdecimal() or (fraction and not fraction.isdecimal()):
        return None
    return int(whole) * 1_000_000_000 + int((fraction + "000000000")[:9])


Kind = Literal["journal", "logread", "file"]


@dataclass(frozen=True, slots=True)
class SystemLog:
    """The command that follows a board's system log, and what it prints."""

    kind: Kind
    command: str
    # The cursor the journal resumes at: its first entry is that one again, already recorded.
    resumes_at: str | None = None


class NoLog(Exception):
    """The board keeps no log this extension reads."""


def system_log(
    probe: Probe,
    cursor: str | None,
    history: Literal["kept", "new"],
    retention: timedelta | None = HISTORY,
) -> SystemLog:
    """The journal where there is one, else BusyBox's logread, else a syslog file.

    With `history` kept, a journal without a cursor starts at the current boot, or later where
    `retention`, how long the agent keeps data, is shorter; None is no limit.
    """
    if probe.systemd is not None:
        command = "journalctl -f -o json --all"
        if probe.systemd >= 236:
            command += f" --output-fields={_FIELDS}"
        if cursor:
            # systemd 245 to 252 skip one more entry --after-cursor when the cursor is vacuumed.
            return SystemLog("journal", f"{command} --cursor={shlex.quote(cursor)}", cursor)
        # --no-tail: -f sends only the last 10 entries, even with --since=@0 on systemd 247.
        if history == "kept" and retention is None:
            return SystemLog("journal", f"{command} -b --no-tail")
        if history == "kept" and retention is not None and probe.clock_s is not None:
            # The board's own clock: --since compares with the journal's wall-clock stamps.
            since = max(0, probe.clock_s - int(retention.total_seconds()))
            return SystemLog("journal", f"{command} -b --no-tail --since=@{since}")
        return SystemLog("journal", f"{command} -n 0")
    if probe.logread:
        return SystemLog("logread", "logread -f")
    if probe.files:
        return SystemLog("file", tail(probe.files[0]))
    raise NoLog


def tail(path: str) -> str:
    """Follow a file from its next line, through rotation."""
    return f"tail -n 0 -F {shlex.quote(path)}"
