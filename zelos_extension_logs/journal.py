"""Journal entries, as `journalctl -o json` prints them: where each sits, and on the host clock."""

import json
from dataclasses import dataclass

from zelos_log_lines.clock import DeviceClock
from zelos_log_lines.formats.syslog import journal_field

from .probe import Probe

# Two clocks on NTP, or on crystals within ±100 ppm, run at most 200 ppm apart.
_DRIFT_PPM = 200
# journald reads an entry's two clocks one after the other.
_SAME_GAP_NS = 1_000_000
# How far an entry lands after one it ties with, as a fed line follows the one before.
_TIE_NS = 1_000


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

    The probe's reading of the monotonic clock places history by its distance from now. The
    monotonic clock stops while the board sleeps, so history written before a sleep lands that much
    late, never early. A boot's wall clock less its monotonic clock changes only when the board
    sleeps or its wall clock steps. When it grows, the monotonic clock fell behind and the boot's
    mapping starts over, seeded again unless the jump came after the probe.

    journald gives the lines it reads together one stamp. An entry its boot's monotonic clock
    puts at or after the one before lands at least a microsecond after it, so a boot's entries
    keep the journal's order.
    """

    def __init__(self, probe: Probe | None) -> None:
        self._probe = probe
        self._clocks: dict[str, DeviceClock] = {}
        self._gaps: dict[str, int] = {}
        # The last entry's boot, monotonic clock and time.
        self._last: tuple[str, int, int] | None = None

    def place(self, entry: Entry, host_ns: int) -> int:
        placed = self._mapped(entry, host_ns)
        if entry.monotonic_ns is None:
            return placed
        last = self._last
        if last is not None and last[0] == entry.boot_id and entry.monotonic_ns >= last[1]:
            placed = max(placed, last[2] + _TIE_NS)
        self._last = (entry.boot_id, entry.monotonic_ns, placed)
        return placed

    def _mapped(self, entry: Entry, host_ns: int) -> int:
        clock = self._clocks.get(entry.boot_id)
        if clock is None:
            if entry.monotonic_ns is None or not self._live(entry.boot_id):
                return self._wall_clock(entry, host_ns)
            clock = self._start(entry.boot_id, seeded=True)
        if entry.monotonic_ns is None:
            return host_ns
        if self._fell_behind(entry):
            clock = self._start(entry.boot_id, seeded=not self._after_probe(entry))
        return clock.map(host_ns, entry.monotonic_ns)[0]

    def _start(self, boot_id: str, *, seeded: bool) -> DeviceClock:
        clock = self._clocks[boot_id] = DeviceClock(drift_ppm=_DRIFT_PPM)
        probe = self._probe
        if seeded and probe is not None and probe.boot_id == boot_id:
            monotonic = probe.monotonic_ns
            if monotonic is not None:
                clock.seed(probe.host_ns, monotonic)
        return clock

    def _after_probe(self, entry: Entry) -> bool:
        probe = self._probe
        if probe is None or probe.journal_gap_ns is None or entry.realtime_ns is None:
            return True
        assert entry.monotonic_ns is not None
        return entry.realtime_ns - entry.monotonic_ns > probe.journal_gap_ns + _SAME_GAP_NS

    def _fell_behind(self, entry: Entry) -> bool:
        if entry.realtime_ns is None or entry.monotonic_ns is None:
            return False
        gap = entry.realtime_ns - entry.monotonic_ns
        last = self._gaps.get(entry.boot_id)
        self._gaps[entry.boot_id] = gap
        return last is not None and gap > last + _SAME_GAP_NS

    def _live(self, boot_id: str) -> bool:
        # The boot the probe saw, or one that started since: entries arrive as they are written.
        probe = self._probe
        return probe is None or probe.boot_id == boot_id or bool(self._clocks)

    def _wall_clock(self, entry: Entry, host_ns: int) -> int:
        probe = self._probe
        if entry.realtime_ns is None or probe is None or probe.clock_s is None:
            return host_ns
        return entry.realtime_ns + probe.host_ns - probe.clock_s * 1_000_000_000
