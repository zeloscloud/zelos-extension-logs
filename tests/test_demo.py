"""The demo board's journal."""

import json
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from zelos_log_lines import formats, values

from tests.conftest import FakeClock
from zelos_extension_logs import demo, journal

S = 1_000_000_000


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generator waits a tick between rounds; the fake clock moves without it."""
    monkeypatch.setattr(demo, "_TICK_S", 0.0)


def two_boots(clock: FakeClock | None = None) -> list[tuple[dict[str, Any], int]]:
    """Every entry of the first boot and the start of the second, with the time printed."""
    clock = clock or FakeClock(start_ns=1_700_000_000 * S, step_ns=100_000_000)
    stop = threading.Event()
    seen: list[tuple[dict[str, Any], int]] = []
    boots: set[str] = set()
    for raw, printed in demo.lines(stop, clock):
        entry = json.loads(raw)
        seen.append((entry, printed))
        boots.add(entry["_BOOT_ID"])
        if len(boots) == 2 and sum(e["_BOOT_ID"] == entry["_BOOT_ID"] for e, _ in seen) > 6:
            stop.set()
    return seen


@pytest.fixture
def run() -> list[tuple[dict[str, Any], int]]:
    return two_boots()


def test_every_line_is_a_journal_entry_the_reader_reads(
    run: list[tuple[dict[str, Any], int]],
) -> None:

    for entry, _ in run:
        raw = json.dumps(entry).encode()
        read = journal.read(raw)

        assert read is not None
        assert read.cursor == entry["__CURSOR"]
        assert read.boot_id == entry["_BOOT_ID"]
        assert read.monotonic_ns == int(entry["__MONOTONIC_TIMESTAMP"]) * 1000


def test_lines_end_with_a_line_end() -> None:
    stop = threading.Event()
    lines = demo.lines(stop, FakeClock(step_ns=1_000_000))

    for _ in range(5):
        raw, _ = next(lines)

        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
    stop.set()


def test_entries_carry_the_fields_a_row_needs(run: list[tuple[dict[str, Any], int]]) -> None:
    for entry, _ in run:
        assert re.fullmatch(r"[0-9a-f]{32}", entry["_BOOT_ID"])
        assert entry["PRIORITY"].isdecimal()
        assert 0 <= int(entry["PRIORITY"]) <= 7
        assert entry["SYSLOG_IDENTIFIER"] in {"kernel", "pump", "m4"}
        assert entry["_TRANSPORT"] in {"kernel", "stdout", "syslog"}
        assert isinstance(entry["MESSAGE"], str)
        assert entry["__REALTIME_TIMESTAMP"].isdecimal()
        assert entry["__MONOTONIC_TIMESTAMP"].isdecimal()


def test_cursors_name_their_boot_and_never_repeat(run: list[tuple[dict[str, Any], int]]) -> None:
    cursors = [entry["__CURSOR"] for entry, _ in run]

    assert len(set(cursors)) == len(cursors)
    for entry, _ in run:
        assert entry["__CURSOR"].endswith(f";b={entry['_BOOT_ID']}")
    sequence = [int(c.split(";")[1].removeprefix("i="), 16) for c in cursors]
    assert sequence == list(range(1, len(run) + 1))


def test_the_board_restarts_with_a_new_boot_id(run: list[tuple[dict[str, Any], int]]) -> None:
    boots = list(dict.fromkeys(entry["_BOOT_ID"] for entry, _ in run))

    assert len(boots) == 2
    first = [e for e, _ in run if e["_BOOT_ID"] == boots[0]]
    second = [e for e, _ in run if e["_BOOT_ID"] == boots[1]]
    assert int(first[-1]["__MONOTONIC_TIMESTAMP"]) > 85 * 1_000_000
    assert int(second[0]["__MONOTONIC_TIMESTAMP"]) < 5 * 1_000_000


def test_each_boot_starts_with_the_kernel_and_the_pump(
    run: list[tuple[dict[str, Any], int]],
) -> None:
    for boot in dict.fromkeys(entry["_BOOT_ID"] for entry, _ in run):
        start = [e for e, _ in run if e["_BOOT_ID"] == boot][:2]

        assert start[0]["SYSLOG_IDENTIFIER"] == "kernel"
        assert start[0]["MESSAGE"] == "Booting Linux on physical CPU 0x0"
        assert start[1]["SYSLOG_IDENTIFIER"] == "pump"
        assert start[1]["MESSAGE"].endswith("[pump] [info] pump service started")


def test_the_wall_clock_reads_1970_until_the_network_sets_it(
    run: list[tuple[dict[str, Any], int]],
) -> None:
    boots = list(dict.fromkeys(entry["_BOOT_ID"] for entry, _ in run))
    first = [e for e, _ in run if e["_BOOT_ID"] == boots[0]]
    second = [e for e, _ in run if e["_BOOT_ID"] == boots[1]]
    late = [e for e in first if int(e["__MONOTONIC_TIMESTAMP"]) > 11_000_000]

    # Every boot starts at 1970; only the first lasts past the 10 s the network takes.
    for entries in (first, second):
        early = [e for e in entries if int(e["__MONOTONIC_TIMESTAMP"]) < 9_000_000]
        assert early
        assert all(int(e["__REALTIME_TIMESTAMP"]) < 10_000_000 for e in early)
    assert late
    assert all(int(e["__REALTIME_TIMESTAMP"]) > 1_600_000_000 * 1_000_000 for e in late)


def test_text_stamps_follow_the_wall_clock_too(run: list[tuple[dict[str, Any], int]]) -> None:
    pump = [e for e, _ in run if e["SYSLOG_IDENTIFIER"] == "pump"]

    assert pump[0]["MESSAGE"].startswith("[1970-01-01 00:00:00.")
    assert any(e["MESSAGE"].startswith("[2023-") for e in pump)


def test_the_monotonic_clock_follows_the_clock_it_is_given(
    run: list[tuple[dict[str, Any], int]],
) -> None:
    first_boot = run[0][0]["_BOOT_ID"]
    entries = [(e, printed) for e, printed in run if e["_BOOT_ID"] == first_boot]
    start = entries[0][1]

    for entry, printed in entries:
        monotonic_us = int(entry["__MONOTONIC_TIMESTAMP"])
        assert abs((printed - start) // 1000 - monotonic_us) < 1_000_000


def test_printed_times_never_go_back(run: list[tuple[dict[str, Any], int]]) -> None:
    printed = [at for _, at in run]

    assert printed == sorted(printed)


def test_the_programs_say_what_a_small_controller_says(
    run: list[tuple[dict[str, Any], int]],
) -> None:
    records = [formats.read(json.dumps(raw)) for raw, _ in run]

    running = [r for r in records if r.name == "pump" and "running" in r.message]
    assert running
    assert all("rpm=" in r.message and "flow=" in r.message for r in running)
    assert any(
        r.name == "pump" and r.level == "warn" and r.message.startswith("pressure high")
        for r in records
    )
    assert any(r.name == "m4" and r.level == "error" for r in records)
    assert any(r.name == "m4" and r.level == "info" for r in records)
    assert any(r.name == "kernel" and r.level == "warn" for r in records)


def test_each_program_prints_the_same_values_on_every_line(
    run: list[tuple[dict[str, Any], int]],
) -> None:
    # A program's signals are fixed 2 s after its first values, so a name it prints later is lost.
    reader = values.ValueReader()
    printed: dict[str, set[frozenset[str]]] = {}
    for entry, _ in run:
        record = reader.read(formats.read(json.dumps(entry)))
        if record.values:
            printed.setdefault(record.name, set()).add(frozenset(v.name for v in record.values))

    assert printed["pump"] == {frozenset({"rpm", "flow"})}
    assert all(len(names) == 1 for names in printed.values())


def test_the_same_clock_gives_the_same_entries() -> None:
    def take() -> list[bytes]:
        stop = threading.Event()
        clock = FakeClock(start_ns=1_700_000_000 * S, step_ns=100_000_000)
        out = []
        for raw, _ in demo.lines(stop, clock):
            out.append(raw)
            if len(out) == 40:
                stop.set()
        # Boot ids are random; the rest must not be.
        return [re.sub(rb"[0-9a-f]{32}", b"BOOT", raw) for raw in out]

    assert take() == take()


def test_a_stopped_generator_yields_nothing() -> None:
    stop = threading.Event()
    stop.set()

    assert list(demo.lines(stop)) == []


def test_stop_ends_it_once_the_round_in_hand_is_written() -> None:
    stop = threading.Event()
    lines: Iterator[tuple[bytes, int]] = demo.lines(stop, FakeClock(step_ns=1_000_000))
    next(lines)

    stop.set()

    # The first round holds three lines, one already taken; the next round never starts.
    assert len(list(lines)) == 2


def test_real_time_starts_at_once() -> None:
    stop = threading.Event()
    started = time.time_ns()

    raw, printed = next(demo.lines(stop))
    stop.set()

    entry = json.loads(raw)
    assert entry["MESSAGE"] == "Booting Linux on physical CPU 0x0"
    assert started <= printed <= time.time_ns()
