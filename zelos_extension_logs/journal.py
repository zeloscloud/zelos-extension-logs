"""Journal entries, as `journalctl -o json` prints them: where each sits, and on the host clock."""

import json
from dataclasses import dataclass

from zelos_log_lines.clock import DeviceClock
from zelos_log_lines.formats.syslog import journal_field

from .probe import Probe


@dataclass(frozen=True, slots=True)
class Entry:
    """Where one journal entry sits in the journal, and its two clocks."""

    cursor: str
    boot_id: str
    monotonic_ns: int | None
    realtime_ns: int | None


def _microseconds_ns(value: object) -> int | None:
    digits = journal_field(value)
    return int(digits) * 1000 if digits and digits.isdecimal() else None


def read(line: bytes) -> Entry | None:
    """An entry, or None for a line that is not one, such as journalctl's own error."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or "__CURSOR" not in record:
        return None
    return Entry(
        cursor=journal_field(record.get("__CURSOR")) or "",
        boot_id=journal_field(record.get("_BOOT_ID")) or "",
        monotonic_ns=_microseconds_ns(record.get("__MONOTONIC_TIMESTAMP")),
        realtime_ns=_microseconds_ns(record.get("__REALTIME_TIMESTAMP")),
    )


class Placement:
    """Entry times on the host clock: each boot's monotonic clock, mapped per boot.

    An entry from an earlier boot, which a resume can bring, has no live mapping: it takes its
    wall-clock stamp, shifted by how far the board's clock was from this computer's at the probe.
    """

    def __init__(self, probe: Probe | None) -> None:
        self._probe = probe
        self._clocks: dict[str, DeviceClock] = {}

    def place(self, entry: Entry, host_ns: int) -> int:
        clock = self._clocks.get(entry.boot_id)
        if clock is None:
            if entry.monotonic_ns is None or not self._live(entry.boot_id):
                return self._wall_clock(entry, host_ns)
            clock = self._clocks[entry.boot_id] = DeviceClock()
            probe = self._probe
            if probe is not None and probe.boot_id == entry.boot_id and probe.uptime_ns:
                clock.seed(probe.host_ns, probe.uptime_ns)
        if entry.monotonic_ns is None:
            return host_ns
        return clock.map(host_ns, entry.monotonic_ns)[0]

    def _live(self, boot_id: str) -> bool:
        # The boot the probe saw, or one that started since: entries arrive as they are written.
        probe = self._probe
        return probe is None or probe.boot_id == boot_id or bool(self._clocks)

    def _wall_clock(self, entry: Entry, host_ns: int) -> int:
        probe = self._probe
        if entry.realtime_ns is None or probe is None or probe.clock_s is None:
            return host_ns
        return entry.realtime_ns + probe.host_ns - probe.clock_s * 1_000_000_000
