"""Journal entries, as `journalctl -o json` prints them: where each sits, and on the host clock."""

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.conftest import FIXTURES
from zelos_extension_logs import journal
from zelos_extension_logs.journal import Entry, Placement
from zelos_extension_logs.probe import Probe

S = 1_000_000_000
BOOT = "f07dd35d996b498398f1e4347c93e456"
OTHER_BOOT = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"

FIXTURE_FILES = sorted(path.name for path in FIXTURES.glob("journal-*.jsonl"))


def line(message: object, priority: object = None, **fields: Any) -> bytes:
    """One journal JSON line, with the fields every entry carries."""
    record: dict[str, Any] = {
        "__CURSOR": "s=1;i=1;b=1;m=1;t=1;x=1",
        "_BOOT_ID": BOOT,
        "__MONOTONIC_TIMESTAMP": "5000000",
        "__REALTIME_TIMESTAMP": "1700000000000000",
        "MESSAGE": message,
    }
    if priority is not None:
        record["PRIORITY"] = str(priority)
    record.update(fields)
    return json.dumps(record).encode()


def fixture_lines(name: str) -> list[bytes]:
    return (FIXTURES / name).read_bytes().splitlines()


class TestFixtures:
    """Real entries from systemd 245 to 257."""

    @pytest.mark.parametrize("name", FIXTURE_FILES)
    def test_every_entry_reads(self, name: str) -> None:
        raws = fixture_lines(name)

        entries = [journal.read(raw) for raw in raws]

        assert len(raws) >= 10
        for raw, entry in zip(raws, entries, strict=True):
            assert entry is not None, raw[:80]
            record = json.loads(raw)
            assert entry.cursor == record["__CURSOR"]
            assert entry.boot_id == record["_BOOT_ID"] == BOOT
            assert entry.monotonic_ns == int(record["__MONOTONIC_TIMESTAMP"]) * 1000
            assert entry.realtime_ns == int(record["__REALTIME_TIMESTAMP"]) * 1000


class TestEntry:
    def test_clocks_are_microseconds_as_nanoseconds(self) -> None:
        entry = journal.read(line("x", 6))

        assert entry is not None
        assert entry.monotonic_ns == 5_000_000_000
        assert entry.realtime_ns == 1_700_000_000_000_000_000
        assert entry.boot_id == BOOT

    def test_missing_or_broken_clocks_are_none(self) -> None:
        entry = journal.read(line("x", 6, __MONOTONIC_TIMESTAMP="soon", __REALTIME_TIMESTAMP=None))

        assert entry is not None
        assert (entry.monotonic_ns, entry.realtime_ns) == (None, None)

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"   ",
            b"not json",
            b"[]",
            b"5",
            b'"text"',
            b"{}",
            b'{"MESSAGE": "no cursor"}',
            b"\xff\xfe\x00",
            b"\x1b[0;1;31mFailed to seek to cursor: Invalid argument\x1b[0m",
            b"No journal files were opened due to insufficient permissions.",
        ],
    )
    def test_a_line_that_is_not_an_entry_is_none(self, raw: bytes) -> None:
        assert journal.read(raw) is None

    @given(
        st.dictionaries(
            st.sampled_from(
                ["MESSAGE", "PRIORITY", "_BOOT_ID", "__MONOTONIC_TIMESTAMP", "__REALTIME_TIMESTAMP"]
            ),
            st.none()
            | st.text(max_size=30)
            | st.lists(st.integers(min_value=0, max_value=300), max_size=8)
            | st.lists(st.text(max_size=5), max_size=3),
            max_size=10,
        )
    )
    def test_any_entry_with_a_cursor_reads(self, fields: dict[str, Any]) -> None:
        raw = json.dumps({"__CURSOR": "c", **fields}).encode()

        entry = journal.read(raw)

        assert entry is not None
        assert entry.cursor == "c"


def entry_at(
    mono_s: float | None,
    real_s: float | None = None,
    boot: str = BOOT,
) -> Entry:
    return Entry(
        cursor="c",
        boot_id=boot,
        monotonic_ns=None if mono_s is None else int(mono_s * S),
        realtime_ns=None if real_s is None else int(real_s * S),
    )


def board(**changes: Any) -> Probe:
    fields: dict[str, Any] = {
        "uid": 0,
        "user": "root",
        "groups": frozenset({"root"}),
        "systemd": 252,
        "logread": False,
        "files": (),
        "boot_id": BOOT,
        "uptime_ns": 500 * S,
        "clock_s": 2_000_000_000,
        "host_ns": 1000 * S,
    }
    return Probe(**{**fields, **changes})


class TestPlacement:
    def test_a_seeded_history_burst_lands_before_the_probe_by_its_monotonic_distance(self) -> None:
        placement = Placement(board())

        # The journal sends its history at once, a moment after the probe.
        placed = [
            placement.place(entry_at(mono), 1000 * S + 20_000_000) for mono in (100, 300, 499.9)
        ]

        assert placed == [600 * S, 800 * S, int(999.9 * S)]

    def test_a_later_live_entry_refines_the_mapping(self) -> None:
        placement = Placement(board())
        placement.place(entry_at(400), 1000 * S)

        # Arriving 0.5 s after the seed predicts: the seed's offset still holds.
        late = placement.place(entry_at(505), 1005 * S + 500_000_000)
        # Arriving 0.2 s before it: the journal's clock was ahead, so the offset moves.
        early = placement.place(entry_at(508), 1007 * S + 800_000_000)
        after = placement.place(entry_at(509), 1009 * S)

        assert late == 1005 * S
        assert early == 1007 * S + 800_000_000
        assert after == 1008 * S + 800_000_000

    def test_a_board_whose_wall_clock_reads_1970_still_lines_up(self) -> None:
        placement = Placement(board(clock_s=40))

        placed = placement.place(entry_at(450, real_s=12), 1000 * S)

        assert placed == 950 * S

    def test_an_entry_from_another_boot_uses_its_wall_clock_shifted_by_the_probes_offset(
        self,
    ) -> None:
        # The board's clock ran an hour ahead of this computer's when the probe ran.
        probe = board(clock_s=1000 + 3600, host_ns=1000 * S)
        placement = Placement(probe)

        placed = placement.place(entry_at(80, real_s=1000 + 3600 - 120, boot=OTHER_BOOT), 1001 * S)

        assert placed == 880 * S

    def test_history_of_the_old_boot_then_the_current_boot(self) -> None:
        placement = Placement(board(clock_s=1000, host_ns=1000 * S))

        old = placement.place(entry_at(80, real_s=900, boot=OTHER_BOOT), 1001 * S)
        current = placement.place(entry_at(480), 1001 * S)

        assert old == 900 * S
        assert current == 980 * S

    def test_a_boot_that_starts_after_the_probe_is_placed_as_it_arrives(self) -> None:
        placement = Placement(board())
        placement.place(entry_at(499), 1000 * S)

        first = placement.place(entry_at(2, boot=OTHER_BOOT), 1100 * S)
        second = placement.place(entry_at(3, boot=OTHER_BOOT), 1101 * S)

        assert first == 1100 * S
        assert second == 1101 * S

    def test_an_old_boot_with_no_clock_on_the_board_takes_host_time(self) -> None:
        placement = Placement(board(clock_s=None))

        assert placement.place(entry_at(80, real_s=900, boot=OTHER_BOOT), 1234 * S) == 1234 * S

    def test_an_old_boot_entry_without_a_wall_clock_takes_host_time(self) -> None:
        placement = Placement(board())

        assert placement.place(entry_at(None, None, boot=OTHER_BOOT), 1234 * S) == 1234 * S

    def test_without_a_probe_the_first_entry_takes_host_time(self) -> None:
        placement = Placement(None)

        assert placement.place(entry_at(5), 77 * S) == 77 * S

    def test_without_a_probe_later_entries_keep_their_distance(self) -> None:
        placement = Placement(None)
        placement.place(entry_at(5), 77 * S)

        assert placement.place(entry_at(6), 78 * S) == 78 * S
        assert placement.place(entry_at(8), 80 * S) == 80 * S

    def test_a_probe_without_an_uptime_seeds_nothing(self) -> None:
        placement = Placement(board(uptime_ns=None))

        assert placement.place(entry_at(100), 1000 * S) == 1000 * S

    def test_an_entry_with_no_monotonic_clock_takes_host_time(self) -> None:
        placement = Placement(board())
        placement.place(entry_at(400), 1000 * S)

        assert placement.place(entry_at(None), 1005 * S) == 1005 * S

    def test_a_burst_with_no_probe_is_placed_by_arrival(self) -> None:
        """Without a probe nothing says how old the history is, so it arrives as new."""
        placement = Placement(None)

        placed = [placement.place(entry_at(m), 1000 * S) for m in (1, 2, 3)]

        assert placed == [1000 * S] * 3

    def test_boots_do_not_share_a_clock(self) -> None:
        placement = Placement(None)
        placement.place(entry_at(1000, boot=BOOT), 5000 * S)

        assert placement.place(entry_at(10, boot=OTHER_BOOT), 5100 * S) == 5100 * S
        assert placement.place(entry_at(1010, boot=BOOT), 5010 * S) == 5010 * S
