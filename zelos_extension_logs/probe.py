"""What a board keeps: which log to follow, who we are there, and its clock."""

import json
import shlex
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

# One POSIX sh line, run before every stream. BusyBox prints `date +%N` as is, so there the clock is
# whole seconds. `logread` is tried, not just found: without syslogd's shared-memory buffer it
# exists and fails. `end` proves the output arrived whole.
_BODY = (
    'p() { printf \'%s=%s\\n\' "$1" "$2"; }; '
    'p uid "$(id -u 2>/dev/null)"; '
    'p user "$(id -un 2>/dev/null)"; '
    "p groups \"$(id -Gn 2>/dev/null | tr ' ' ',')\"; "
    "if command -v journalctl >/dev/null 2>&1; then "
    'p systemd "$(journalctl --version 2>/dev/null'
    " | sed -n '1s/^systemd \\([0-9][0-9]*\\).*/\\1/p')\"; "
    'p journal "$(journalctl -b -n 1 -o json --output-fields=_BOOT_ID 2>/dev/null'
    ' || journalctl -b -n 1 -o json 2>/dev/null)"; '
    'p kernel "$(journalctl -k -b -n 1 -o json --output-fields=_SOURCE_MONOTONIC_TIMESTAMP'
    ' 2>/dev/null)"; fi; '
    "if command -v logread >/dev/null 2>&1 && logread >/dev/null 2>&1; then p logread 1; fi; "
    'for f in /var/log/messages /var/log/syslog; do [ -r "$f" ] && p file "$f"; done; '
    'p boot_id "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
    'p dmesg "$(dmesg 2>/dev/null | head -n 1)"; '
    'p clock "$(date +%s 2>/dev/null)"; '
    'p clock_ns "$(date +%s%N 2>/dev/null)"; '
    "p uptime \"$(cut -d' ' -f1 /proc/uptime 2>/dev/null)\"; "
)
SCRIPT = f"{_BODY}p end 1"

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
# journald imports the kernel's buffer when it starts at boot, stamping those entries late; an entry
# the kernel stamped later than this reached it as it was printed.
_LIVE_KERNEL_NS = 60_000_000_000


@dataclass(frozen=True, slots=True)
class Probe:
    """One probe's answers, and the host time they arrived at."""

    uid: int | None
    user: str
    groups: frozenset[str]
    systemd: int | None
    logread: bool
    files: tuple[str, ...]
    boot_id: str
    # Counts time asleep, unlike the journal's monotonic clock.
    uptime_ns: int | None
    clock_s: int | None
    clock_ns: int | None
    # The boot's latest journal entry: its wall clock less its monotonic clock.
    journal_gap_ns: int | None
    # When the answer arrived: the board read its clocks before then.
    host_ns: int
    # How far the kernel's own clock, which stamps its lines, ran behind the monotonic clock when
    # it printed its latest line: it is not corrected by NTP, so it drifts.
    kernel_lag_ns: int | None = None
    # The oldest line in the kernel's buffer, as dmesg prints it first: a command whose first
    # stamped line is this one prints this kernel's lines.
    kernel_oldest: str = ""
    # The file a source follows: ok, missing, directory or unreadable; empty when none was asked.
    path: str = ""
    # The boot's latest journal entry when the probe ran.
    journal_cursor: str = ""

    @property
    def monotonic_ns(self) -> int | None:
        """The board's monotonic clock when it answered, the clock journal entries carry: its wall
        clock less the journal's gap, rounded down, and at most its uptime."""
        clock = self.clock_ns
        if clock is None and self.clock_s is not None:
            clock = self.clock_s * 1_000_000_000
        if clock is None or self.journal_gap_ns is None:
            return None
        monotonic = clock - self.journal_gap_ns
        return monotonic if self.uptime_ns is None else min(monotonic, self.uptime_ns)

    @property
    def kernel_ns(self) -> int | None:
        """The kernel's clock when the board answered, the clock dmesg's lines carry: the
        monotonic clock less the kernel's lag where the journal measured it, else the uptime."""
        monotonic = self.monotonic_ns
        if monotonic is None:
            return self.uptime_ns
        return monotonic if self.kernel_lag_ns is None else monotonic - self.kernel_lag_ns

    @property
    def partial_journal(self) -> bool:
        """True when the journal will show only this user's own entries."""
        return self.uid != 0 and not (self.groups & _JOURNAL_READERS)


def partial(user: str) -> str:
    """What a user who sees only their own journal entries should do about it."""
    return (
        f"{user} is not root and not in systemd-journal, so the journal shows only its own "
        f"entries. Where the journal is, run: sudo usermod -aG systemd-journal {user}"
    )


def script(path: str | None = None) -> str:
    """The probe, which for a file also says whether the file is there to read."""
    if path is None:
        return SCRIPT
    q = shlex.quote(path)
    return (
        f"{_BODY}if [ -d {q} ]; then p path directory; elif [ -r {q} ]; then p path ok; "
        f"elif [ -e {q} ]; then p path unreadable; else p path missing; fi; p end 1"
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
    clock_ns = values.get("clock_ns", "")
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
        clock_ns=int(clock_ns) if clock_ns.isdecimal() else None,
        journal_gap_ns=_gap_ns(values.get("journal", "")),
        journal_cursor=_cursor(values.get("journal", "")),
        kernel_lag_ns=_lag_ns(values.get("kernel", "")),
        kernel_oldest=values.get("dmesg", ""),
        host_ns=host_ns,
        path=values.get("path", ""),
    )


def _gap_ns(entry: str) -> int | None:
    try:
        record = json.loads(entry)
        realtime = int(record["__REALTIME_TIMESTAMP"])
        monotonic = int(record["__MONOTONIC_TIMESTAMP"])
    except (ValueError, TypeError, KeyError):
        return None
    return (realtime - monotonic) * 1000


def _cursor(entry: str) -> str:
    try:
        cursor = json.loads(entry)["__CURSOR"]
    except (ValueError, TypeError, KeyError):
        return ""
    return cursor if isinstance(cursor, str) else ""


def _lag_ns(entry: str) -> int | None:
    try:
        record = json.loads(entry)
        received = int(record["__MONOTONIC_TIMESTAMP"]) * 1000
        stamped = int(record["_SOURCE_MONOTONIC_TIMESTAMP"]) * 1000
    except (ValueError, TypeError, KeyError):
        return None
    return received - stamped if stamped > _LIVE_KERNEL_NS else None


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
    # The command prints the boot's history and ends; the journal is then followed from its last
    # entry.
    history: bool = False


class NoLog(Exception):
    """The board keeps no log this extension reads."""


def system_log(
    probe: Probe,
    cursor: str | None,
    history: Literal["kept", "new"],
    retention: timedelta | None = HISTORY,
) -> SystemLog:
    """The journal where there is one, else BusyBox's logread, else a syslog file.

    With `history` kept, a journal without a cursor prints the current boot, or less where
    `retention`, how long the agent keeps data, is shorter; None is no limit. The boot is read
    whole and not followed: following with a field match such as -b, systemd 249 can skip an entry
    another journal file got meanwhile. A cursor follows with no match.
    """
    if probe.systemd is not None:
        fields = f" --output-fields={_FIELDS}" if probe.systemd >= 236 else ""
        follow = f"journalctl -f -o json --all{fields}"
        if cursor:
            # systemd 245 to 252 skip one more entry --after-cursor when the cursor is vacuumed.
            return SystemLog("journal", f"{follow} --cursor={shlex.quote(cursor)}", cursor)
        # -q: an empty boot prints no "-- No entries --".
        boot = f"journalctl -q -o json --all{fields} -b"
        if history == "kept" and retention is None:
            return SystemLog("journal", boot, history=True)
        if history == "kept" and retention is not None and probe.clock_s is not None:
            # The board's own clock: --since compares with the journal's wall-clock stamps.
            since = max(0, probe.clock_s - int(retention.total_seconds()))
            return SystemLog("journal", f"{boot} --since=@{since}", history=True)
        return SystemLog("journal", f"{follow} -n 0")
    if probe.logread:
        return SystemLog("logread", "logread -f")
    if probe.files:
        return SystemLog("file", tail(probe.files[0]))
    raise NoLog


def tail(path: str, *, whole: bool = False) -> str:
    """Follow a file through rotation from its next line, or `whole` from its first. tail's own
    words, such as that the file was replaced, are no lines of the file."""
    return f"tail -n {'+1' if whole else '0'} -F {shlex.quote(path)} 2>/dev/null"
