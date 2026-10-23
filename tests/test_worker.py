"""One source's worker, driven by scripted processes, a fake clock and a recording."""

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    FIXTURES,
    TIME,
    FakeClock,
    FakeProcess,
    Processes,
    Recording,
    wait_until,
)
from zelos_extension_logs import config, demo, oslog, probe, remote, worker
from zelos_extension_logs.worker import Cursors, SourceWorker, state_of

S = 1_000_000_000
# The board's clock and this computer's agree, unless a test says otherwise.
CLOCK_S = 1_700_000_000
BOOT = "f07dd35d996b498398f1e4347c93e456"
OTHER_BOOT = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
CURSOR_1 = f"s=1;i=1;b={BOOT};m=1;t=1;x=1"
CURSOR_2 = f"s=1;i=2;b={BOOT};m=2;t=2;x=2"
CURSOR_3 = f"s=1;i=3;b={BOOT};m=3;t=3;x=3"
PERMISSION = "No journal files were opened due to insufficient permissions."


# Every worker here runs on a computer with ssh.
pytestmark = pytest.mark.usefixtures("ssh")


def probe_output(
    *,
    uid: int = 0,
    user: str = "root",
    groups: str = "root",
    systemd: int | None = 252,
    logread: bool = False,
    files: tuple[str, ...] = (),
    boot: str = "f07dd35d-996b-4983-98f1-e4347c93e456",
    uptime: str = "500.00",
    clock: int = CLOCK_S,
    path: str | None = None,
    kernel_lag_s: float | None = None,
    dmesg: str | None = None,
    journal_cursor: str | None = None,
    end: bool = True,
) -> str:
    lines = [f"uid={uid}", f"user={user}", f"groups={groups}"]
    if systemd is not None:
        lines.append(f"systemd={systemd}")
    if logread:
        lines.append("logread=1")
    lines += [f"file={name}" for name in files]
    # A board that never slept: its journal's clocks agree with its uptime.
    monotonic_us = round(float(uptime) * 1e6) if uptime.replace(".", "", 1).isdecimal() else None
    latest = {"__CURSOR": journal_cursor} if journal_cursor else {}
    entry = (
        json.dumps(
            {
                **latest,
                "__REALTIME_TIMESTAMP": str(clock * 1_000_000),
                "__MONOTONIC_TIMESTAMP": str(monotonic_us),
            }
        )
        if monotonic_us is not None
        else ""
    )
    lines += [f"boot_id={boot}", f"journal={entry}", f"clock={clock}", f"clock_ns={clock * S}"]
    lines.append(f"uptime={uptime}")
    if kernel_lag_s is not None and monotonic_us is not None:
        stamped = monotonic_us - round(kernel_lag_s * 1e6)
        received = {"__MONOTONIC_TIMESTAMP": str(monotonic_us)}
        lines.append(
            f"kernel={json.dumps({**received, '_SOURCE_MONOTONIC_TIMESTAMP': str(stamped)})}"
        )
    if dmesg is not None:
        lines.append(f"dmesg={dmesg}")
    if path is not None:
        lines.append(f"path={path}")
    if end:
        lines.append("end=1")
    return "\n".join(lines) + "\n"


def entry(
    cursor: str,
    mono_s: float,
    message: object,
    priority: int | str | None = 6,
    program: str | None = "pump",
    boot: str = BOOT,
    **fields: Any,
) -> str:
    record: dict[str, Any] = {
        "__CURSOR": cursor,
        "_BOOT_ID": boot,
        "__MONOTONIC_TIMESTAMP": str(int(mono_s * 1_000_000)),
        "__REALTIME_TIMESTAMP": str((CLOCK_S - 500 + int(mono_s)) * 1_000_000),
        "MESSAGE": message,
    }
    if priority is not None:
        record["PRIORITY"] = str(priority)
    if program is not None:
        record["SYSLOG_IDENTIFIER"] = program
    record.update(fields)
    return json.dumps(record) + "\n"


def item(**changes: Any) -> dict[str, Any]:
    return {"connection": "ssh", "host": "root@bench-1", "name": "bench-1", **changes}


def source(**changes: Any) -> config.SourceConfig:
    return config.parse({"sources": [item(**changes)]}).sources[0]


def local(**changes: Any) -> config.SourceConfig:
    return config.parse({"sources": [{"connection": "local", "name": "dev", **changes}]}).sources[0]


def command(text: str, **changes: Any) -> config.SourceConfig:
    return local(read="command", command=text, **changes)


@dataclass
class Rig:
    worker: SourceWorker
    processes: Processes
    clock: FakeClock
    recording: Recording
    cursors: Cursors
    cursors_file: Path

    def start(self) -> None:
        self.worker.start()

    def wait_for(self, state: str) -> None:
        wait_until(lambda: self.worker.health.state == state)

    def stop(self) -> None:
        self.worker.stop()
        self.worker.join(5)
        assert not self.worker.is_alive()

    def run_until(self, condition: Callable[[], object] | str) -> None:
        """Run the worker until `condition` holds (or the state is the one named), then stop it."""
        self.start()
        try:
            if isinstance(condition, str):
                self.wait_for(condition)
            else:
                wait_until(condition)
        finally:
            self.stop()

    def rows(self, name: str = "bench-1") -> list[dict[str, Any]]:
        return self.recording.events().get(f"{name}/log", [])

    def programs(self, name: str = "bench-1") -> list[dict[str, Any]]:
        """The rows the device printed, without the extension's own notes."""
        return [row for row in self.rows(name) if row["name"] != worker.NOTES]

    def notes(self, name: str = "bench-1") -> list[tuple[str, str]]:
        """The extension's own notes, sorted: rows printed at the same time have no fixed order."""
        return sorted(
            (r["level"], r["message"]) for r in self.rows(name) if r["name"] == worker.NOTES
        )

    def saved(self) -> dict[str, Any]:
        return json.loads(self.cursors_file.read_text(encoding="utf-8"))


@pytest.fixture
def make(
    trace_file: Recording,
    tmp_path: Path,
    processes: Callable[..., Processes],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[..., Rig]]:
    rigs: list[Rig] = []
    # A drop retries after a moment in these tests, not after a second.
    monkeypatch.setattr(worker, "_FIRST_RETRY_S", 0.05)

    def build(
        cfg: config.SourceConfig | None = None,
        *scripted: FakeProcess,
        clock: FakeClock | None = None,
        access_key: Path | None = None,
        cursors_file: Path | None = None,
        retention: timedelta | None = probe.HISTORY,
    ) -> Rig:
        cfg = cfg or source()
        clock = clock or FakeClock(start_ns=CLOCK_S * S)
        cursors_file = cursors_file or tmp_path / "cursors.json"
        cursors = Cursors(cursors_file)
        fakes = processes(*scripted)
        rig = Rig(
            SourceWorker(
                cfg,
                trace_file.source,
                cursors,
                access_key,
                run=fakes,
                clock=clock,
                retention=retention,
            ),
            fakes,
            clock,
            trace_file,
            cursors,
            cursors_file,
        )
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.worker._stop_event.set()
        if rig.worker.is_alive():
            rig.worker.join(5)


def journal_session(*entries: str, rc: int = 0, stderr: str = "") -> FakeProcess:
    return FakeProcess("".join(entries), stderr, rc)


def probe_process(**changes: Any) -> FakeProcess:
    return FakeProcess(probe_output(**changes))


class TestJournalFlow:
    def test_rows_carry_program_names_and_levels(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(
                entry(CURSOR_1, 400, "[warning] pressure high p=3.50bar", 6, "pump"),
                entry(CURSOR_2, 450, "usb 1-1: reset high-speed device", 4, "kernel"),
                entry(CURSOR_3, 499, "watchdog timeout", 3, "acme-app"),
            ),
        )

        rig.run_until("waiting")

        rows = rig.programs()
        assert [(r["name"], r["level"], r["message"]) for r in rows] == [
            ("pump", "warn", "pressure high p=3.50bar"),
            ("kernel", "warn", "usb 1-1: reset high-speed device"),
            ("acme-app", "error", "watchdog timeout"),
        ]

    def test_a_crash_over_entries_is_one_row_of_its_program(self, make: Callable[..., Rig]) -> None:
        crash = [
            "Traceback (most recent call last):",
            '  File "/opt/pump/run.py", line 8, in <module>',
            "ValueError: pressure out of range",
        ]
        rig = make(
            None,
            probe_process(),
            journal_session(
                *(entry(f"c{i}", 400 + i, text, 3, "pump") for i, text in enumerate(crash)),
                entry("c3", 404, '  File "/opt/valve/run.py", line 2', 6, "valve"),
            ),
        )

        rig.run_until("waiting")

        assert [(r["name"], r["message"]) for r in rig.programs()] == [
            ("pump", "\n".join(crash)),
            ("valve", '  File "/opt/valve/run.py", line 2'),
        ]

    def test_history_lands_before_now_by_its_monotonic_distance(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(uptime="500.00"),
            journal_session(
                entry(CURSOR_1, 400, "a"),
                entry(CURSOR_2, 450, "b"),
                entry(CURSOR_3, 499.5, "c"),
            ),
        )

        rig.run_until("waiting")

        assert [round(r[TIME] - CLOCK_S, 3) for r in rig.programs()] == [-100.0, -50.0, -0.5]

    def test_history_is_placed_from_when_the_probes_answer_arrived(
        self, make: Callable[..., Rig]
    ) -> None:
        clock = FakeClock(start_ns=CLOCK_S * S)

        class SlowProbe(FakeProcess):
            def communicate(
                self, input: bytes | None = None, timeout: float | None = None
            ) -> tuple[bytes, bytes]:
                # ssh connects, then the board reads its uptime: 0.4 s from start to answer.
                clock.advance(0.4)
                return super().communicate(input, timeout)

        rig = make(
            None,
            SlowProbe(probe_output(uptime="500.00")),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 499.5, "b")),
            clock=clock,
        )

        rig.run_until("waiting")

        assert [round(r[TIME] - CLOCK_S, 3) for r in rig.programs()] == [-99.6, -0.1]

    def test_the_stream_gets_newlines_on_windows_and_the_probe_does_not(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        woken: list[object] = []
        monkeypatch.setattr(remote, "wake", lambda process, stdin: woken.append(process))
        rig = make(None, probe_process(), journal_session(entry(CURSOR_1, 400, "a")))

        rig.run_until("waiting")

        assert woken == [rig.processes.started[1]]

    def test_the_commands_are_the_probe_then_the_journal(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session(entry(CURSOR_1, 400, "a")))

        rig.run_until("waiting")

        assert rig.processes.command(0) == probe.SCRIPT
        assert rig.processes.command(1) == (
            "journalctl -q -o json --all --output-fields="
            "MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_COMM,_SYSTEMD_UNIT,_PID,CODE_FILE,CODE_LINE,"
            f"_TRANSPORT -b --since=@{CLOCK_S - 3600}"
        )
        assert rig.processes.argvs[1][-1] == remote.wrap(rig.processes.command(1))
        assert rig.processes.argvs[1][-3:-1] == ["--", "root@bench-1"]

    def test_the_probe_runs_without_the_wrapper(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())

        rig.run_until("waiting")

        argv = rig.processes.argvs[0]
        assert isinstance(argv, list)
        assert argv[-3:] == ["--", "root@bench-1", probe.SCRIPT]

    def test_the_access_key_is_offered(self, make: Callable[..., Rig], tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.write_text("k", encoding="utf-8")
        rig = make(None, probe_process(), journal_session(), access_key=key)

        rig.run_until("waiting")

        argv = rig.processes.argvs[0]
        assert isinstance(argv, list)
        assert argv[argv.index("-i") + 1] == str(key)

    def test_byte_array_and_repeated_fields_from_the_fixtures(
        self, make: Callable[..., Rig]
    ) -> None:
        text = (FIXTURES / "journal-debian12-252.jsonl").read_text(encoding="utf-8")
        rig = make(None, probe_process(), FakeProcess(text))

        rig.run_until("waiting")

        by_name = {r["name"]: r for r in rig.programs()}
        assert by_name["ansi"]["message"] == "red"
        assert by_name["ansi"]["level"] == "error"
        assert by_name["repeat2"]["file"] == "a.c"
        assert by_name["systemd"]["file"].startswith("src/core/")
        assert len(rig.programs()) == len(text.splitlines())

    def test_an_entry_split_over_two_reads_is_one_row(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(None, probe_process(), stream)
        line = entry(CURSOR_1, 400, "pump started", 6, "pump").encode()
        rig.start()
        try:
            stream.send(line[:40])
            wait_until(lambda: rig.worker.health.state == "streaming")
            stream.send(line[40:] + entry(CURSOR_2, 410, "pump stopped").encode()[:10])
            wait_until(lambda: rig.worker.health.lines == 1)
            stream.send(entry(CURSOR_2, 410, "pump stopped").encode()[10:])
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert [r["message"] for r in rig.programs()] == ["pump started", "pump stopped"]

    def test_blank_lines_between_entries_are_skipped(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session("\n", entry(CURSOR_1, 400, "a"), "  \n\n", entry(CURSOR_2, 410, "b")),
        )

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs()] == ["a", "b"]

    def test_the_stream_ending_is_noted_and_retried(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
            probe_process(),
            journal_session(entry(CURSOR_2, 450, "b")),
        )

        rig.run_until("waiting")

        assert rig.notes() == [("warn", "[logs] root@bench-1: the log ended (exit 0).")]
        assert rig.worker.health.reconnects == 1


class TestHistory:
    def test_a_first_start_reads_the_boot_then_follows_from_its_last_entry(
        self, make: Callable[..., Rig]
    ) -> None:
        follow = FakeProcess(hold_open=True)
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
            probe_process(),
            follow,
        )
        rig.start()
        try:
            follow.send(entry(CURSOR_2, 450, "b") + entry(CURSOR_3, 499, "c"))
            wait_until(lambda: rig.worker.health.lines == 3)
        finally:
            rig.stop()

        history, followed = shlex.split(rig.processes.command(1)), rig.processes.command(3)
        assert "-b" in history and "-f" not in history
        assert shlex.split(followed)[:2] == ["journalctl", "-f"]
        assert followed.endswith(f"--cursor='{CURSOR_2}'")
        assert " -b" not in followed
        assert [r["message"] for r in rig.programs()] == ["a", "b", "c"]
        # Reading the history and following it are one start: nothing to note or count.
        assert rig.notes() == []
        assert rig.worker.health.reconnects == 0

    def test_history_with_no_entry_follows_from_the_boards_latest_entry(
        self, make: Callable[..., Rig]
    ) -> None:
        follow = FakeProcess(hold_open=True)
        rig = make(
            None,
            probe_process(journal_cursor=CURSOR_1),
            journal_session(),
            probe_process(journal_cursor=CURSOR_1),
            follow,
        )
        rig.start()
        try:
            follow.send(entry(CURSOR_1, 400, "old") + entry(CURSOR_2, 501, "new"))
            wait_until(lambda: rig.worker.health.lines == 1)
        finally:
            rig.stop()

        assert rig.processes.command(3).endswith(f"--cursor='{CURSOR_1}'")
        assert [r["message"] for r in rig.programs()] == ["new"]

    def test_history_with_no_entry_on_a_board_with_none_follows_new_lines(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(None, probe_process(), journal_session(), probe_process(), journal_session())

        rig.run_until(lambda: len(rig.processes.started) == 4)

        assert rig.processes.command(3).endswith(" -n 0")

    def test_history_cut_short_resumes_from_its_last_entry(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), rc=255, stderr="Connection reset by peer"),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
        )

        rig.run_until(lambda: rig.worker.health.lines == 2)

        assert rig.processes.command(3).endswith(f"--cursor='{CURSOR_1}'")
        assert [r["message"] for r in rig.programs()] == ["a", "b"]
        assert rig.worker.health.reconnects >= 1

    def test_a_partial_journal_is_noted_once_for_its_history_and_follow(
        self, make: Callable[..., Rig]
    ) -> None:
        follow = FakeProcess(hold_open=True)
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe"),
            journal_session(entry(CURSOR_1, 400, "a")),
            probe_process(uid=1000, user="probe", groups="probe"),
            follow,
        )
        rig.start()
        try:
            follow.send(entry(CURSOR_2, 450, "b"))
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert rig.notes() == [("warn", f"[logs] {PARTIAL}")]


KEY = "bench-1\0ssh\0root@bench-1"


FIXTURE_FILES = sorted(path.name for path in FIXTURES.glob("journal-*.jsonl"))


class TestJournalRows:
    """What the trace holds for entries the journal sends: levels, text and values."""

    def rows(
        self, make: Callable[..., Rig], message: object, priority: int | str | None = 6
    ) -> dict[str, Any]:
        rig = make(None, probe_process(), journal_session(entry(CURSOR_1, 400, message, priority)))
        rig.run_until("waiting")
        (row,) = rig.programs()
        return row

    def test_a_header_beats_the_priority(self, make: Callable[..., Rig]) -> None:
        """A service's standard output arrives at priority 6 whatever its logger printed."""
        row = self.rows(make, "[warning] disk low", 6)

        assert (row["level"], row["message"]) == ("warn", "disk low")

    def test_plain_text_takes_the_priority(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, "pump stopped", 3)["level"] == "error"

    def test_a_level_word_header_at_another_priority(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, "INFO: started", 4)["level"] == "info"

    @pytest.mark.parametrize(
        ("priority", "level"),
        [
            (0, "critical"),
            (1, "critical"),
            (2, "critical"),
            (3, "error"),
            (4, "warn"),
            (5, "info"),
            (6, "info"),
            (7, "debug"),
        ],
    )
    def test_each_priority(self, make: Callable[..., Rig], priority: int, level: str) -> None:
        assert self.rows(make, "pump stopped", priority)["level"] == level

    @pytest.mark.parametrize("priority", [None, 8, 9, "", "x", "-1", "3.5"])
    def test_no_usable_priority_is_info(
        self, make: Callable[..., Rig], priority: int | str | None
    ) -> None:
        assert self.rows(make, "pump stopped", priority)["level"] == "info"

    def test_a_syslog_prefix_in_the_text_sets_the_level(self, make: Callable[..., Rig]) -> None:
        row = self.rows(make, "<4>prefixed", 6)

        assert (row["level"], row["message"]) == ("warn", "prefixed")

    def test_red_plain_text_is_an_error(self, make: Callable[..., Rig]) -> None:
        row = self.rows(make, "\x1b[31mpump stopped\x1b[0m", 6)

        assert (row["level"], row["message"]) == ("error", "pump stopped")

    def test_yellow_plain_text_is_a_warning(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, "\x1b[33mcheck pump\x1b[0m", 6)["level"] == "warn"

    def test_green_plain_text_takes_the_priority(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, "\x1b[32mpump ok\x1b[0m", 3)["level"] == "error"

    def test_color_is_stripped_in_the_middle_of_a_message(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, "pump \x1b[1mfast\x1b[0m now", 6)["message"] == "pump fast now"

    def test_a_message_of_4096_bytes_is_whole(self, make: Callable[..., Rig]) -> None:
        message = "a" * 4096

        assert self.rows(make, message, 6)["message"] == message

    def test_a_null_message_is_an_empty_row(self, make: Callable[..., Rig]) -> None:
        assert self.rows(make, None, 6)["message"] == ""

    def test_an_entry_with_no_message_is_its_line_as_journalctl_printed_it(
        self, make: Callable[..., Rig]
    ) -> None:
        """As a saved journal export reads, so nothing the entry holds is lost."""
        raw = json.dumps({"__CURSOR": CURSOR_1, "_BOOT_ID": BOOT, "PRIORITY": "6"})
        rig = make(None, probe_process(), journal_session(raw + "\n"))

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs()] == [raw]

    def test_a_spdlog_line_keeps_its_values(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            source(advanced={"values": "on"}),
            probe_process(),
            journal_session(
                entry(CURSOR_1, 400, "[2026-01-01 12:00:00.123] [pump] [info] running rpm=1450", 6)
            ),
        )

        rig.run_until("waiting")

        events = trace_file.events()
        assert [r["level"] for r in printed(events, "bench-1")] == ["info"]
        assert [r["rpm"] for r in events["bench-1/pump"]] == [1450.0]

    def test_values_off_keeps_them_out(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            source(advanced={"values": "off"}),
            probe_process(),
            journal_session(
                entry(CURSOR_1, 400, "[2026-01-01 12:00:00.123] [pump] [info] running rpm=1450", 6)
            ),
        )

        rig.run_until("waiting")

        events = trace_file.events()
        assert [r["message"] for r in printed(events, "bench-1")] == ["running rpm=1450"]
        assert "bench-1/pump" not in events

    def test_a_stamp_in_the_text_does_not_place_the_entry(self, make: Callable[..., Rig]) -> None:
        """The entry's own clocks place it."""
        rig = make(
            None,
            probe_process(uptime="500.00"),
            journal_session(
                entry(CURSOR_1, 400, "[2026-01-01 12:00:00.123] [pump] [info] running", 6)
            ),
        )

        rig.run_until("waiting")

        assert [round(r[TIME] - CLOCK_S, 3) for r in rig.programs()] == [-100.0]

    @pytest.mark.parametrize("name", FIXTURE_FILES)
    def test_every_fixture_entry_is_a_row(self, make: Callable[..., Rig], name: str) -> None:
        raws = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        rig = make(None, probe_process(), FakeProcess("\n".join(raws) + "\n"))

        rig.run_until("waiting")

        assert len(rig.programs()) == len(raws)

    @pytest.mark.parametrize("name", FIXTURE_FILES)
    def test_fixture_rows_read_as_a_terminal_shows_them(
        self, make: Callable[..., Rig], name: str
    ) -> None:
        raws = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        rig = make(None, probe_process(), FakeProcess("\n".join(raws) + "\n"))

        rig.run_until("waiting")

        by_name: dict[str, dict[str, Any]] = {}
        for row in rig.programs():
            by_name.setdefault(row["name"], row)
        records: dict[str, dict[str, Any]] = {}
        for raw in raws:
            record = json.loads(raw)
            records.setdefault(record["SYSLOG_IDENTIFIER"], record)
        assert (by_name["kernel"]["level"], by_name["kernel"]["name"]) == ("info", "kernel")
        assert (by_name["ansi"]["message"], by_name["ansi"]["level"]) == ("red", "error")
        assert by_name["badutf8"]["message"] == "bad �� bytes"
        assert by_name["badutf8-syslog"]["message"] == "bad �� syslog"
        assert by_name["multiline"]["message"] == "two\nlines"
        assert (by_name["repeat2"]["file"], by_name["repeat2"]["line"]) == ("a.c", 1)
        assert by_name["systemd"]["file"] == records["systemd"]["CODE_FILE"]
        assert by_name["systemd"]["line"] == int(records["systemd"]["CODE_LINE"])
        assert by_name["long5k"]["message"] == records["long5k"]["MESSAGE"]


def save_cursor(
    rig: Rig, cursor: str = CURSOR_2, boot: str | None = BOOT, age_s: float = 0.0
) -> None:
    saved: dict[str, Any] = {"cursor": cursor, "saved_ns": int(rig.clock.ns - age_s * S)}
    if boot is not None:
        saved["boot_id"] = boot
    rig.cursors.put(KEY, saved)


class TestCursors:
    def test_get_returns_what_was_put(self, tmp_path: Path) -> None:
        cursors = Cursors(tmp_path / "cursors.json")

        cursors.put("a", {"cursor": "c1"})
        cursors.put("b", {"cursor": "c2"})

        assert cursors.get("a") == {"cursor": "c1"}
        assert cursors.get("b") == {"cursor": "c2"}
        assert cursors.get("c") is None

    def test_survives_a_new_instance(self, tmp_path: Path) -> None:
        Cursors(tmp_path / "cursors.json").put("a", {"cursor": "c1"})

        assert Cursors(tmp_path / "cursors.json").get("a") == {"cursor": "c1"}

    def test_creates_its_directory_and_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "nested" / "cursors.json"

        Cursors(path).put("a", {"cursor": "c1"})

        assert sorted(p.name for p in path.parent.iterdir()) == ["cursors.json"]

    def test_without_a_path_nothing_is_kept(self) -> None:
        cursors = Cursors(None)

        cursors.put("a", {"cursor": "c1"})

        assert cursors.get("a") is None

    @pytest.mark.parametrize("text", ["", "{not json", "[1, 2]", '"text"', "5"])
    def test_a_damaged_file_reads_as_empty(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "cursors.json"
        path.write_text(text, encoding="utf-8")
        cursors = Cursors(path)

        assert cursors.get("a") is None
        cursors.put("a", {"cursor": "c1"})
        assert cursors.get("a") == {"cursor": "c1"}

    def test_a_saved_value_that_is_not_an_object_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "cursors.json"
        path.write_text(json.dumps({"a": "text", "b": ["x"]}), encoding="utf-8")

        assert Cursors(path).get("a") is None
        assert Cursors(path).get("b") is None

    def test_puts_from_threads_do_not_lose_each_other(self, tmp_path: Path) -> None:
        cursors = Cursors(tmp_path / "cursors.json")

        threads = [
            threading.Thread(target=cursors.put, args=(f"k{n}", {"cursor": str(n)}))
            for n in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert {n: (cursors.get(f"k{n}") or {}).get("cursor") for n in range(20)} == {
            n: str(n) for n in range(20)
        }

    def test_a_save_waits_for_a_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows refuses to replace a file that another handle has open."""
        path = tmp_path / "cursors.json"
        cursors = Cursors(path)
        cursors.put("a", {"cursor": "c1"})
        reading = threading.Event()
        read_text, replace = Path.read_text, Path.replace

        def slow_read(self: Path, *args: Any, **kwargs: Any) -> str:
            if threading.current_thread().name != "reader":
                return read_text(self, *args, **kwargs)
            reading.set()
            threading.Event().wait(0.3)
            try:
                return read_text(self, *args, **kwargs)
            finally:
                reading.clear()

        def windows_replace(self: Path, target: Any) -> Path:
            if reading.is_set():
                raise PermissionError(13, "The file is being used by another process")
            return replace(self, target)

        monkeypatch.setattr(Path, "read_text", slow_read)
        monkeypatch.setattr(Path, "replace", windows_replace)
        reader = threading.Thread(target=cursors.get, args=("a",), name="reader")
        reader.start()
        assert reading.wait(5)

        cursors.put("b", {"cursor": "c2"})
        reader.join(5)

        assert cursors.get("b") == {"cursor": "c2"}


class TestResume:
    def test_the_cursor_is_saved(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
        )

        rig.run_until("waiting")

        assert rig.saved() == {KEY: {"cursor": CURSOR_2, "boot_id": BOOT, "saved_ns": CLOCK_S * S}}

    def test_the_cursor_is_saved_while_streaming_and_again_when_it_stops(
        self, make: Callable[..., Rig]
    ) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(None, probe_process(), stream)
        rig.start()
        try:
            stream.send(entry(CURSOR_1, 400, "a"))
            wait_until(lambda: rig.cursors_file.exists())
            assert rig.saved()[KEY]["cursor"] == CURSOR_1

            # Within 5 s of the last save: held until the stream ends.
            stream.send(entry(CURSOR_2, 410, "b"))
            wait_until(lambda: rig.worker.health.lines == 2)
            assert rig.saved()[KEY]["cursor"] == CURSOR_1
        finally:
            rig.stop()

        assert rig.saved()[KEY]["cursor"] == CURSOR_2

    def test_a_cursor_is_saved_again_after_five_seconds(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(None, probe_process(), stream)
        rig.start()
        try:
            stream.send(entry(CURSOR_1, 400, "a"))
            wait_until(lambda: rig.cursors_file.exists())
            rig.clock.advance(6)
            stream.send(entry(CURSOR_2, 410, "b"))
            wait_until(lambda: rig.saved()[KEY]["cursor"] == CURSOR_2)
        finally:
            rig.stop()

    def test_the_next_session_resumes_at_the_saved_cursor(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
            probe_process(),
            journal_session(entry(CURSOR_3, 460, "c")),
        )

        rig.run_until(lambda: rig.worker.health.reconnects >= 2)

        second = rig.processes.command(3)
        assert f"--cursor='{CURSOR_2}'" in second
        assert "--since" not in second
        assert " -n 0" not in second
        assert [r["message"] for r in rig.programs()] == ["a", "b", "c"]

    def test_with_new_lines_only_a_dropped_connection_resumes_where_it_stopped(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(advanced={"history": "new"}),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
            probe_process(),
            journal_session(entry(CURSOR_3, 460, "c")),
        )

        rig.run_until(lambda: rig.worker.health.reconnects >= 2)

        assert " -n 0" in rig.processes.command(1)
        assert f"--cursor='{CURSOR_2}'" in rig.processes.command(3)
        assert [r["message"] for r in rig.programs()] == ["a", "b", "c"]

    def test_the_first_entry_equal_to_the_cursor_is_dropped(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
            probe_process(),
            # journalctl prints the cursor's own entry first, already recorded.
            journal_session(entry(CURSOR_2, 450, "b"), entry(CURSOR_3, 460, "c")),
        )

        rig.run_until(lambda: rig.worker.health.reconnects >= 2)

        assert [r["message"] for r in rig.programs()] == ["a", "b", "c"]
        assert rig.worker.health.lines == 3

    def test_a_first_entry_that_is_not_the_cursor_is_kept(self, make: Callable[..., Rig]) -> None:
        """The journal vacuumed the cursor's entry: it starts at the nearest one."""
        rig = make(None, probe_process(), journal_session(entry(CURSOR_3, 460, "c")))
        save_cursor(rig, CURSOR_1)

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs()] == ["c"]

    def test_an_entry_that_equals_the_cursor_later_in_the_stream_is_kept(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_3, 460, "c"), entry(CURSOR_2, 470, "again")),
        )
        save_cursor(rig, CURSOR_2)

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs()] == ["c", "again"]

    def test_a_cursor_saved_before_the_first_run_is_used(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, age_s=60)

        rig.run_until("waiting")

        assert f"--cursor='{CURSOR_2}'" in rig.processes.command(1)

    def test_a_cursor_an_hour_old_is_still_used(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, age_s=3599)

        rig.run_until("waiting")

        assert "--cursor=" in rig.processes.command(1)

    def test_a_cursor_older_than_an_hour_is_ignored(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, age_s=3601)

        rig.run_until("waiting")

        command = rig.processes.command(1)
        assert "--cursor" not in command
        assert f"--since=@{CLOCK_S - 3600}" in command

    def test_a_first_start_reads_as_far_back_as_the_agent_keeps_data(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(None, probe_process(), journal_session(), retention=timedelta(hours=6))

        rig.run_until(lambda: len(rig.processes.started) == 2)

        assert rig.processes.command(1).endswith(f" -b --since=@{CLOCK_S - 6 * 3600}")

    def test_a_cursor_the_agent_still_keeps_data_for_is_used(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(None, probe_process(), journal_session(), retention=timedelta(hours=6))
        save_cursor(rig, CURSOR_2, age_s=5 * 3600)

        rig.run_until("waiting")

        assert f"--cursor='{CURSOR_2}'" in rig.processes.command(1)

    def test_with_no_time_limit_any_cursor_is_used(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session(), retention=None)
        save_cursor(rig, CURSOR_2, age_s=30 * 86400)

        rig.run_until("waiting")

        assert f"--cursor='{CURSOR_2}'" in rig.processes.command(1)

    @pytest.mark.parametrize(
        "saved",
        [
            {"cursor": CURSOR_2},
            {"cursor": CURSOR_2, "saved_ns": "soon"},
            {"cursor": "", "saved_ns": CLOCK_S * S},
            {"cursor": 5, "saved_ns": CLOCK_S * S},
        ],
    )
    def test_a_saved_position_that_is_not_usable_is_ignored(
        self, make: Callable[..., Rig], saved: dict[str, Any]
    ) -> None:
        rig = make(None, probe_process(), journal_session())
        rig.cursors.put(KEY, saved)

        rig.run_until("waiting")

        assert "--cursor" not in rig.processes.command(1)

    def test_history_new_ignores_the_cursor(self, make: Callable[..., Rig]) -> None:
        rig = make(source(advanced={"history": "new"}), probe_process(), journal_session())
        save_cursor(rig, CURSOR_2)

        rig.run_until("waiting")

        command = rig.processes.command(1)
        assert "--cursor" not in command
        assert "--since" not in command
        assert " -n 0" in command

    def test_each_source_keeps_its_own_cursor(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())
        rig.cursors.put("other\0ssh\0root@bench-1", {"cursor": CURSOR_3, "saved_ns": CLOCK_S * S})

        rig.run_until("waiting")

        assert "--cursor" not in rig.processes.command(1)

    def test_a_boot_change_is_noted(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, boot=OTHER_BOOT)

        rig.run_until("waiting")

        assert ("warn", "[logs] device restarted") in rig.notes()

    @pytest.mark.parametrize("boot", [BOOT, None, ""])
    def test_the_same_boot_or_an_unknown_one_is_not_a_restart(
        self, make: Callable[..., Rig], boot: str | None
    ) -> None:
        rig = make(None, probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, boot=boot)

        rig.run_until("waiting")

        assert ("warn", "[logs] device restarted") not in rig.notes()

    def test_a_restart_is_noted_even_when_history_ignores_the_cursor(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(source(advanced={"history": "new"}), probe_process(), journal_session())
        save_cursor(rig, CURSOR_2, boot=OTHER_BOOT)

        rig.run_until("waiting")

        assert ("warn", "[logs] device restarted") in rig.notes()

    def test_entries_of_the_old_boot_are_placed_by_their_wall_clock(
        self, make: Callable[..., Rig]
    ) -> None:
        old_wall_s = CLOCK_S - 7200
        old = entry(CURSOR_1, 100, "before the restart", boot=OTHER_BOOT).replace(
            f'"{(CLOCK_S - 400) * 1_000_000}"', f'"{old_wall_s * 1_000_000}"'
        )
        rig = make(
            None,
            probe_process(),
            journal_session(old, entry(CURSOR_2, 450, "after the restart")),
        )

        rig.run_until("waiting")

        times = {r["message"]: r[TIME] for r in rig.programs()}
        assert times["before the restart"] == pytest.approx(old_wall_s)
        assert times["after the restart"] == pytest.approx(CLOCK_S - 50)

    def test_a_cursor_that_cannot_be_saved_does_not_stop_the_stream(
        self, make: Callable[..., Rig], tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
            cursors_file=blocker / "cursors.json",
        )

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs()] == ["a"]
        assert "cannot save the journal position" in caplog.text


PARTIAL = (
    "probe is not root and not in systemd-journal, so the journal shows only its own entries. "
    "Where the journal is, run: sudo usermod -aG systemd-journal probe"
)


class TestFailures:
    def test_a_refused_login_is_lasting_and_says_how_to_fix_it(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            FakeProcess("", "root@bench-1: Permission denied (publickey).", 255),
            probe_process(),
        )

        rig.run_until("waiting")

        message = (
            "root@bench-1 refused the login. Run Show Access Key and add its line to the "
            "device, or set Key file."
        )
        assert rig.worker.health.message == message
        assert rig.notes() == [("error", f"[logs] {message}")]
        # A lasting failure is not retried at once: the second scripted probe is never started.
        assert len(rig.processes.started) == 1

    def test_a_refused_connection_is_a_warning_and_is_retried(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(advanced={"history": "new"}),
            FakeProcess("", "ssh: connect to host bench-1 port 22: Connection refused", 255),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert rig.notes()[0] == (
            "warn",
            "[logs] root@bench-1 refused the connection. Check that an SSH server runs there.",
        )
        assert len(rig.processes.started) == 3

    def test_a_probe_that_prints_nothing_names_the_host(self, make: Callable[..., Rig]) -> None:
        rig = make(None, FakeProcess("", "", 1))

        rig.run_until("waiting")

        assert rig.worker.health.message == "ssh to root@bench-1 failed."

    def test_a_probe_that_is_cut_short_is_truncated_not_trusted(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None, FakeProcess(probe_output(end=False), "kex_exchange_identification: x", 255)
        )

        rig.run_until("waiting")

        assert (
            rig.worker.health.message
            == "ssh to root@bench-1 failed: kex_exchange_identification: x"
        )
        assert len(rig.processes.started) == 1

    def test_a_probe_that_does_not_answer_in_time(self, make: Callable[..., Rig]) -> None:
        rig = make(None, FakeProcess(hangs=True))

        rig.run_until("waiting")

        assert rig.worker.health.message == "root@bench-1 did not answer the probe in time."
        assert rig.notes() == [("warn", f"[logs] {rig.worker.health.message}")]

    def test_no_ssh_client(self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "openssh", lambda tool: None)
        rig = make(None)

        rig.run_until("waiting")

        assert rig.worker.health.message == str(remote.SshMissing())
        assert rig.notes() == [("error", f"[logs] {rig.worker.health.message}")]
        assert rig.processes.argvs == []

    def test_a_process_that_cannot_start(self, make: Callable[..., Rig]) -> None:
        rig = make(source(read="file", path="/var/log/acme/run.log"))

        rig.run_until("waiting")

        assert rig.worker.health.message == "Cannot start /usr/bin/ssh: no more scripted processes."
        assert rig.notes() == [("error", f"[logs] {rig.worker.health.message}")]

    def test_a_probe_that_cannot_start_says_why(self, make: Callable[..., Rig]) -> None:
        rig = make(None)

        rig.run_until("waiting")

        assert rig.worker.health.message == "Cannot start /usr/bin/ssh: no more scripted processes."
        assert len(rig.notes()) == 1

    def test_a_board_with_no_log(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(systemd=None))

        rig.run_until("waiting")

        assert rig.worker.health.message == (
            "No system log found: no journal, logread or readable syslog file. Set Log to A file "
            "or A command's output."
        )
        assert rig.notes() == [("error", f"[logs] {rig.worker.health.message}")]
        assert len(rig.processes.started) == 1

    def test_a_repeated_message_is_noted_once(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            FakeProcess("", "Connection refused", 255),
            FakeProcess("", "Connection refused", 255),
            FakeProcess("", "Connection refused", 255),
        )

        rig.run_until(lambda: len(rig.processes.started) == 3)

        assert len(rig.notes()) == 1

    def test_a_non_root_user_is_told_the_journal_is_partial(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe"),
            journal_session(entry(CURSOR_1, 400, "a")),
        )

        rig.run_until("waiting")

        assert ("warn", f"[logs] {PARTIAL}") in rig.notes()

    def test_a_user_who_reads_everything_is_not(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe,systemd-journal"),
            journal_session(entry(CURSOR_1, 400, "a")),
        )

        rig.run_until("waiting")

        assert all("usermod" not in message for _, message in rig.notes())

    def test_journalctls_insufficient_permissions_text_sets_the_health_message(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe"),
            FakeProcess(PERMISSION + "\n", hold_open=True),
        )
        rig.start()
        try:
            wait_until(lambda: rig.worker.health.message == PARTIAL)
        finally:
            rig.stop()

        assert ("error", f"[logs] {PARTIAL}") in rig.notes()
        assert ("warn", f"[logs] {PERMISSION}") in rig.notes()

    def test_the_permission_message_stays_while_the_users_own_entries_stream(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe"),
            FakeProcess(PERMISSION + "\n" + entry(CURSOR_1, 400, "mine"), hold_open=True),
        )
        rig.start()
        try:
            wait_until(lambda: rig.worker.health.lines == 1)
            assert rig.worker.health.message == PARTIAL
        finally:
            rig.stop()

    def test_the_permission_message_outlives_journalctls_exit(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(uid=1000, user="probe", groups="probe"),
            FakeProcess(PERMISSION + "\n", "", 1),
        )

        rig.run_until("waiting")

        assert rig.worker.health.message == PARTIAL

    def test_text_between_entries_is_noted_as_a_warning(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(
                entry(CURSOR_1, 400, "a"),
                "Journal file corrupted\n",
                entry(CURSOR_2, 410, "b"),
            ),
        )

        rig.run_until("waiting")

        assert ("warn", "[logs] Journal file corrupted") in rig.notes()
        assert [r["message"] for r in rig.programs()] == ["a", "b"]

    def test_a_lost_connection_after_streaming_is_retried_not_lasting(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(
                entry(CURSOR_1, 400, "a"), rc=255, stderr="root@bench-1: Permission denied (x)."
            ),
            probe_process(),
            journal_session(entry(CURSOR_2, 410, "b")),
        )

        rig.run_until(lambda: rig.worker.health.lines == 2)

        assert len(rig.processes.started) == 4
        assert rig.notes()[0][0] == "warn"

    def test_a_refused_login_while_following_is_lasting_when_nothing_streamed(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(rc=255, stderr="root@bench-1: Permission denied (publickey)."),
            probe_process(),
        )

        rig.run_until("waiting")

        assert rig.notes()[0][0] == "error"
        assert len(rig.processes.started) == 2

    def test_a_log_that_ends_with_another_status(self, make: Callable[..., Rig]) -> None:
        rig = make(None, probe_process(), journal_session(rc=2))

        rig.run_until("waiting")

        assert rig.worker.health.message == "root@bench-1: the log ended (exit 2)."


class _GatedStdin:
    """A real stdin pipe whose flush waits until another thread has closed it, as stop() may."""

    def __init__(self, file: Any) -> None:
        self._file = file
        self.flushing = threading.Event()
        self._closed = threading.Event()

    def flush(self) -> None:
        self.flushing.set()
        self._closed.wait(5)
        self._file.flush()

    def close(self) -> None:
        self._file.close()
        self._closed.set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group")
def test_stopping_during_the_probe_is_no_error(
    make: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    gates: list[_GatedStdin] = []

    def start(argv: list[str] | str, stdin: int, stderr: int) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        if process.stdin is not None:
            gates.append(_GatedStdin(process.stdin))
            process.stdin = gates[-1]  # type: ignore[assignment]
        return process

    rig = make()
    rig.worker._run = start
    rig.start()
    wait_until(lambda: rig.worker._process is not None)
    if gates:
        assert gates[0].flushing.wait(5)
    rig.stop()

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    # The stop ended the probe: that is no failure of the board.
    assert rig.notes() == []
    assert rig.worker.health.message == ""


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script stands in for ssh")
def test_ssh_that_prints_much_to_stderr_does_not_stall_the_log(
    make: Callable[..., Rig], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As `-v` in Extra ssh options does, for as long as the session lasts."""
    chatty = tmp_path / "ssh"
    chatty.write_text(
        "#!/bin/sh\n"
        f"case \"$*\" in *'p uid'*) printf {shlex.quote(probe_output())}; exit 0;; esac\n"
        "head -c 200000 /dev/zero | tr '\\0' x >&2\necho started\nexec sleep 30\n",
        encoding="utf-8",
    )
    chatty.chmod(0o755)
    monkeypatch.setattr(remote, "openssh", lambda tool: str(chatty))
    rig = make(source(read="command", command="dmesg -w"))
    rig.worker._run = worker._run

    rig.run_until(lambda: rig.worker.health.lines)

    assert [r["message"] for r in rig.programs()] == ["started"]


class TestBackoff:
    """`run()` driven in this thread, with an event that records how long it was told to wait."""

    class Waits(threading.Event):
        def __init__(self, stop_after: int) -> None:
            super().__init__()
            self.waited: list[float] = []
            self._stop_after = stop_after

        def wait(self, timeout: float | None = None) -> bool:
            self.waited.append(timeout or 0.0)
            if len(self.waited) >= self._stop_after:
                self.set()
            return self.is_set()

    @pytest.fixture(autouse=True)
    def first_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    def run(self, rig: Rig, waits: int) -> list[float]:
        # After `make`, which shortens it for the tests that run a thread.
        self.monkeypatch.setattr(worker, "_FIRST_RETRY_S", 1.0)
        recorder = self.Waits(waits)
        rig.worker._stop_event = recorder
        rig.worker.run()
        return recorder.waited

    def test_a_failure_doubles_the_wait_up_to_a_minute(self, make: Callable[..., Rig]) -> None:
        refused = [FakeProcess("", "Connection refused", 255) for _ in range(8)]
        rig = make(None, *refused)

        assert self.run(rig, 8) == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]

    def test_a_lasting_failure_waits_a_minute(self, make: Callable[..., Rig]) -> None:
        rig = make(None, FakeProcess("", "Permission denied", 255), FakeProcess("", "x", 255))

        assert self.run(rig, 1) == [60.0]

    def test_a_session_that_stayed_up_a_minute_starts_the_wait_again(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(advanced={"history": "new"}),
            FakeProcess("", "Connection refused", 255),
            FakeProcess("", "Connection refused", 255),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
            FakeProcess("", "Connection refused", 255),
        )
        sessions = rig.worker._session
        count = iter(range(1, 100))

        def third_stays_up() -> worker.Outcome:
            outcome = sessions()
            if next(count) == 3:
                rig.clock.advance(60)
            return outcome

        rig.worker._session = third_stays_up  # type: ignore[method-assign]

        assert self.run(rig, 4) == [2.0, 4.0, 1.0, 2.0]

    def test_a_command_that_fails_at_once_backs_off_though_it_printed(
        self, make: Callable[..., Rig]
    ) -> None:
        usage = "dmesg: option requires an argument -- 'f'\n"
        rig = make(command("dmesg -f"), *[FakeProcess(usage, "", 1) for _ in range(5)])

        assert self.run(rig, 5) == [2.0, 4.0, 8.0, 16.0, 32.0]

    def test_each_drop_is_counted(self, make: Callable[..., Rig]) -> None:
        rig = make(None, *[FakeProcess("", "Connection refused", 255) for _ in range(3)])

        self.run(rig, 3)

        assert rig.worker.health.reconnects == 3
        assert rig.worker.health.state == "stopped"

    def test_a_bug_in_a_session_is_logged_and_retried(
        self,
        make: Callable[..., Rig],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = make(None)

        def broken() -> tuple[bool, str]:
            raise RuntimeError("boom")

        monkeypatch.setattr(rig.worker, "_session", broken)

        assert self.run(rig, 2) == [2.0, 4.0]
        assert "bench-1: unexpected error" in caplog.text
        assert "boom" in caplog.text


def printed(events: dict[str, list[dict[str, Any]]], stream: str) -> list[dict[str, Any]]:
    return [row for row in events[f"{stream}/log"] if row["name"] != worker.NOTES]


class TestText:
    def test_spdlog_lines_are_parsed_with_their_values(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            command("pump-controller"),
            FakeProcess(
                "[2026-01-01 12:00:00.123] [pump] [info] running rpm=1450 flow=12.00L/min\n"
                "[2026-01-01 12:00:01.123] [pump] [warning] pressure high\n"
                "[2026-01-01 12:00:02.123] [pump] [error] valve stuck\n"
            ),
        )

        rig.run_until("ended")

        events = trace_file.events()
        assert sorted((r["message"], r["name"], r["level"]) for r in printed(events, "dev")) == [
            ("pressure high", "pump", "warn"),
            ("running rpm=1450 flow=12.00L/min", "pump", "info"),
            ("valve stuck", "pump", "error"),
        ]
        assert [row["rpm"] for row in events["dev/pump"]] == [1450.0]

    def test_a_last_line_without_a_line_end_is_written_without_its_values(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(command("pump-controller"), FakeProcess("rpm=1450"))

        rig.run_until("ended")

        events = trace_file.events()
        assert [r["message"] for r in printed(events, "dev")] == ["rpm=1450"]
        assert "dev/values" not in events

    def test_a_line_that_pauses_without_its_end_is_written(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(command("flash"), stream)
        rig.start()
        try:
            stream.send("Waiting for device...")
            wait_until(lambda: rig.worker.stream_state().pending_since is not None)
            rig.clock.advance(0.1)
            wait_until(lambda: rig.worker.health.lines == 1, timeout=2)
        finally:
            rig.stop()

        assert [r["message"] for r in rig.programs("dev")] == ["Waiting for device..."]

    def test_values_off_strips_the_values(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            command("pump-controller", advanced={"values": "off"}),
            FakeProcess("[2026-01-01 12:00:00.123] [pump] [info] running rpm=1450\n"),
        )

        rig.run_until("ended")

        events = trace_file.events()
        assert list(events) == ["dev/log"]
        assert printed(events, "dev")[0]["message"] == "running rpm=1450"

    def test_values_on_adds_them_to_the_system_log(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            source(advanced={"values": "on"}),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "running rpm=1450", 6, "pump")),
        )

        rig.run_until("waiting")

        assert [r["rpm"] for r in trace_file.events()["bench-1/pump"]] == [1450.0]

    def test_values_are_off_for_the_system_log_by_default(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "running rpm=1450", 6, "pump")),
        )

        rig.run_until("waiting")

        assert list(trace_file.events()) == ["bench-1/log"]

    def test_kernel_uptime_stamps_are_placed_by_the_device_clock(
        self, make: Callable[..., Rig]
    ) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(command("dmesg -w"), stream)
        rig.start()
        try:
            stream.send("[    1.000000] usb 1-1: new device\n")
            wait_until(lambda: rig.worker.health.lines == 1)
            rig.clock.advance(10.5)
            # 0.5 s after the device printed it: the device's own clock says 10 s.
            stream.send("[   11.000000] usb 1-1: reset\n")
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        times = [r[TIME] for r in rig.programs("dev")]
        assert times == [pytest.approx(CLOCK_S), pytest.approx(CLOCK_S + 10.0)]

    def test_a_device_restart_is_noted(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(command("dmesg -w"), stream)
        rig.start()
        try:
            for stamp, seconds in ((100.0, 0), (200.0, 5), (3.0, 10), (4.0, 11)):
                rig.clock.advance(seconds)
                stream.send(f"[{stamp:12.6f}] kernel: tick\n")
            wait_until(lambda: rig.worker.health.lines == 4)
        finally:
            rig.stop()

        assert ("warn", "[logs] device restarted") in rig.notes("dev")

    def test_a_last_line_without_a_line_end_is_kept(self, make: Callable[..., Rig]) -> None:
        rig = make(command("tail"), FakeProcess("first\nsecond"))

        rig.run_until("ended")

        assert sorted(r["message"] for r in rig.programs("dev")) == ["first", "second"]

    def test_a_command_that_finishes_with_zero_ends_the_source(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(command("echo hi"), FakeProcess("hi\n"), FakeProcess("hi\n"))

        rig.start()
        rig.worker.join(5)

        assert not rig.worker.is_alive()
        assert rig.worker.health.state == "ended"
        assert rig.worker.health.message == (
            "The command finished. It runs again when the extension restarts."
        )
        assert len(rig.processes.started) == 1
        assert [r["message"] for r in rig.programs("dev")] == ["hi"]

    def test_a_command_that_ends_over_ssh_with_zero_ends_the_source_too(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(read="command", command="dmesg"),
            FakeProcess(probe_output()),
            FakeProcess("hi\n"),
        )

        rig.start()
        rig.worker.join(5)

        assert rig.worker.health.state == "ended"

    def test_a_command_that_fails_is_run_again_with_a_note_about_the_gap(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Long enough to move the clock before the second run starts.
        monkeypatch.setattr(worker, "_FIRST_RETRY_S", 1.0)
        rig = make(
            command("flaky"),
            FakeProcess("one\n", "", 1),
            FakeProcess("two\n", "", 0),
        )
        rig.start()
        try:
            rig.wait_for("waiting")
            rig.clock.advance(30)
            wait_until(lambda: rig.worker.health.state == "ended")
        finally:
            rig.stop()

        assert rig.notes("dev") == [
            ("warn", "[logs] Reconnected after 30 s; lines printed meanwhile are missing."),
            ("warn", "[logs] The command finished. It runs again when the extension restarts."),
            ("warn", "[logs] this computer: the log ended (exit 1)."),
        ]
        assert rig.worker.health.gaps == 1
        assert sorted(r["message"] for r in rig.programs("dev")) == ["one", "two"]

    def test_the_journal_resumes_and_so_notes_no_gap(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
            probe_process(),
            journal_session(entry(CURSOR_2, 410, "b")),
        )

        rig.run_until(lambda: rig.worker.health.reconnects >= 2)

        assert rig.worker.health.gaps == 0
        assert all("Reconnected" not in message for _, message in rig.notes())

    def test_a_local_command_runs_in_the_shell_with_stderr_merged(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(command("dmesg -w"), FakeProcess("x\n"))

        rig.run_until("ended")

        assert rig.processes.argvs == [remote.local_argv("dmesg -w")]
        assert rig.processes.stderrs == [subprocess.STDOUT]

    def test_a_local_command_never_gets_newlines(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        woken: list[object] = []
        monkeypatch.setattr(remote, "wake", lambda process, stdin: woken.append(process))
        rig = make(command("cat"), FakeProcess("x\n"))

        rig.run_until("ended")

        assert woken == []

    def test_a_file_over_ssh_is_followed_with_tail(self, make: Callable[..., Rig]) -> None:
        rig = make(
            source(read="file", path="/var/log/acme/run.log"),
            FakeProcess(probe_output()),
            FakeProcess("[2026-01-01 12:00:00.123] [pump] [info] started\n", hold_open=True),
        )
        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert rig.processes.command(1) == "tail -n 0 -F /var/log/acme/run.log 2>/dev/null"
        assert rig.processes.argvs[1][-1] == remote.wrap(rig.processes.command(1))
        assert rig.worker.health.reading == "file"
        assert [r["message"] for r in rig.programs()] == ["started"]

    def test_a_file_name_with_a_space_is_quoted(self, make: Callable[..., Rig]) -> None:
        rig = make(
            source(read="file", path="/var/log/my app.log"),
            FakeProcess(probe_output()),
            FakeProcess("x\n"),
        )

        rig.run_until("waiting")

        assert rig.processes.command(1) == "tail -n 0 -F '/var/log/my app.log' 2>/dev/null"

    def test_a_quiet_file_over_ssh_is_streaming_once_the_probe_passes(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(read="file", path="/var/log/quiet.log"),
            FakeProcess(probe_output()),
            FakeProcess("", hold_open=True),
        )

        rig.run_until("streaming")

        assert rig.worker.health.lines == 0

    def test_a_quiet_local_command_is_streaming_once_it_starts(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(command("quiet"), FakeProcess("", hold_open=True))

        rig.run_until("streaming")

        assert rig.worker.health.lines == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
    def test_stderr_of_every_statement_of_a_command_reaches_the_log(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(command("echo err >&2; echo out"))
        # A real shell: only running the text shows where its redirection lands.
        rig.worker._run = worker._run

        rig.run_until("ended")

        assert sorted(r["message"] for r in rig.programs("dev")) == ["err", "out"]

    def test_a_command_over_ssh_runs_as_given(self, make: Callable[..., Rig]) -> None:
        rig = make(
            source(read="command", command="dmesg -w | cat"),
            FakeProcess(probe_output()),
            FakeProcess("x\n"),
        )

        rig.run_until("ended")

        assert rig.processes.command(1) == "dmesg -w | cat"
        assert rig.worker.health.reading == "command"

    def test_the_system_log_of_a_busybox_board_is_logread(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(systemd=None, logread=True),
            FakeProcess("Oct 21 18:19:49 b-alp-C user.warn root: warn line one\n"),
        )

        rig.run_until("waiting")

        assert rig.processes.command(1) == "logread -f"
        assert rig.worker.health.reading == "logread"
        (row,) = rig.programs()
        assert (row["level"], row["message"]) == ("warn", "warn line one")

    @pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("busybox-*.txt")))
    def test_busybox_lines_from_the_fixtures(self, make: Callable[..., Rig], name: str) -> None:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        rig = make(None, probe_process(systemd=None, logread=True), FakeProcess(text))

        rig.run_until("waiting")

        rows = {r["message"]: r for r in rig.programs()}
        assert len(rig.programs()) == len(text.splitlines())
        # `-S` drops the priority, so nothing says more than info.
        with_priority = not name.endswith("-S.txt")
        expected = {
            "warn line one": "warn",
            "err line two": "error",
            "info with tag": "info",
            "multi word  message, with: colons": "critical",
            "debug line": "debug",
            "notice": "info",
            "emerg": "critical",
        }
        for message, level in expected.items():
            assert rows[message]["level"] == (level if with_priority else "info"), message
        assert rows["warn line one"]["name"] == "root"
        assert rows["info with tag"]["name"] == "mytag"
        assert rows["multi word  message, with: colons"]["name"] == "svc"

    def test_a_syslog_file_is_followed_with_tail(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(systemd=None, files=("/var/log/messages",)),
            FakeProcess("Oct 21 18:19:49 b-alp-C daemon.err root: err line two\n"),
        )

        rig.run_until("waiting")

        assert rig.processes.command(1) == "tail -n 0 -F /var/log/messages 2>/dev/null"
        assert [r["level"] for r in rig.programs()] == ["error"]

    def test_a_local_system_log_is_not_wrapped(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        rig = make(
            local(read="system", advanced={"history": "new"}),
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a")),
        )

        rig.run_until("waiting")

        assert [argv[:2] for argv in rig.processes.argvs] == [["/bin/sh", "-c"]] * 2
        assert not any("kill -TERM" in argv[-1] for argv in rig.processes.argvs)
        assert rig.processes.command(0) == probe.SCRIPT
        assert rig.processes.command(1).startswith("journalctl -f -o json --all ")
        assert [r["message"] for r in rig.programs("dev")] == ["a"]


class TestStateAndSample:
    def test_a_new_worker(self, make: Callable[..., Rig]) -> None:
        rig = make()

        assert state_of(rig.worker) == {
            "name": "bench-1",
            "connection": "ssh",
            "host": "root@bench-1",
            "read": "system",
            "state": "starting",
            "reading": "",
            "message": "",
            "lines": 0,
            "reconnects": 0,
            "gaps": 0,
            "boot_id": "",
            "last_line_ns": None,
            "long_lines": 0,
            "late_names": 0,
            "errors": 0,
            "signals": [],
        }

    def test_the_signals_of_the_values_it_read(self, make: Callable[..., Rig]) -> None:
        rig = make(command("tail"), FakeProcess("pump: rpm=1450 flow=12.0\n"))

        rig.run_until("ended")

        state = state_of(rig.worker)
        signals = state["signals"]
        assert isinstance(signals, list)
        assert [(s["event"], s["field"]) for s in signals] == [
            ("dev/values", "rpm"),
            ("dev/values", "flow"),
        ]
        assert (state["long_lines"], state["late_names"], state["errors"]) == (0, 0, 0)

    def test_after_a_journal_session(self, make: Callable[..., Rig]) -> None:
        rig = make(
            None,
            probe_process(),
            journal_session(entry(CURSOR_1, 400, "a"), entry(CURSOR_2, 450, "b")),
            probe_process(),
            journal_session(),
        )

        rig.run_until("waiting")

        state = state_of(rig.worker)
        assert state["state"] == "stopped"
        assert state["reading"] == "journal"
        assert state["lines"] == 2
        assert state["boot_id"] == BOOT
        assert state["last_line_ns"] == (CLOCK_S - 50) * S
        assert state["reconnects"] == 1
        assert state["message"] == "root@bench-1: the log ended (exit 0)."

    def test_streaming_state_clears_the_message(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(None, FakeProcess("", "Connection refused", 255), probe_process(), stream)
        rig.start()
        try:
            wait_until(lambda: rig.worker.health.message != "")
            wait_until(lambda: rig.worker.health.state == "streaming")
            assert rig.worker.health.message == ""
        finally:
            rig.stop()

    def test_a_stopped_worker(self, make: Callable[..., Rig]) -> None:
        stream = FakeProcess(hold_open=True)
        rig = make(None, probe_process(), stream)
        rig.start()
        wait_until(lambda: rig.worker.health.state == "streaming")

        rig.stop()

        assert rig.worker.health.state == "stopped"
        assert stream.stdin.closed

    def test_a_line_written_on_the_way_out_leaves_the_worker_stopped(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def chunks(_: SourceWorker, __: object) -> Iterator[tuple[bytes, int]]:
            yield b"crash in pro", CLOCK_S * S
            raise RuntimeError("read failed")

        monkeypatch.setattr(SourceWorker, "_chunks", chunks)
        rig = make(command("dmesg -w"), FakeProcess(hold_open=True))
        rig.run_until("waiting")

        # The line the session was printing is written as the worker stops.
        assert [row["message"] for row in rig.programs("dev")] == ["crash in pro"]
        assert rig.worker.health.state == "stopped"

    def test_a_demo_worker_streams_without_a_process(
        self, make: Callable[..., Rig], trace_file: Recording
    ) -> None:
        cfg = config.parse({"sources": [{"connection": "demo"}]}).sources[0]
        rig = make(cfg)

        rig.run_until(lambda: rig.worker.health.lines >= 3)

        assert rig.processes.argvs == []
        assert rig.worker.health.reading == ""
        assert rig.worker.health.state == "stopped"
        messages = {r["message"] for r in printed(trace_file.events(), "demo")}
        assert "Booting Linux on physical CPU 0x0" in messages

    def test_a_demo_worker_writes_what_a_small_controller_says(
        self, make: Callable[..., Rig], trace_file: Recording, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(demo, "_TICK_S", 0.0)
        cfg = config.parse({"sources": [{"connection": "demo"}]}).sources[0]
        rig = make(cfg)

        rig.run_until(lambda: rig.worker.health.lines >= 60)

        rows = printed(trace_file.events(), "demo")
        running = [r for r in rows if r["name"] == "pump" and "running" in r["message"]]
        assert running
        assert all(r["level"] == "info" and "rpm=" in r["message"] for r in running)
        assert any(r["name"] == "pump" and r["level"] == "warn" for r in rows)
        assert any(r["name"] == "m4" and r["level"] == "error" for r in rows)
        assert any(r["name"] == "kernel" and r["level"] == "warn" for r in rows)

    def test_sample_holds_the_messages_recorded(self, make: Callable[..., Rig]) -> None:
        rig = make(command("tail"), FakeProcess("one\ntwo\nthree\n"))

        rig.run_until("ended")

        assert rig.worker.sample() == ["one", "two", "three"]

    def test_sample_holds_the_last_two_hundred(self, make: Callable[..., Rig]) -> None:
        rig = make(command("tail"), FakeProcess("".join(f"line {n}\n" for n in range(250))))

        rig.run_until("ended")

        sample = rig.worker.sample()
        assert len(sample) == 200
        assert sample[0] == "line 50"
        assert sample[-1] == "line 249"
        assert rig.worker.health.lines == 250

    def test_sample_is_a_copy(self, make: Callable[..., Rig]) -> None:
        rig = make(command("tail"), FakeProcess("one\n"))
        rig.run_until("ended")

        rig.worker.sample().append("x")

        assert rig.worker.sample() == ["one"]

    def test_sample_keeps_values_out_when_switched_off(self, make: Callable[..., Rig]) -> None:
        rig = make(
            command("tail", advanced={"values": "off"}),
            FakeProcess("[2026-01-01 12:00:00.123] [pump] [info] running rpm=1450\n"),
        )

        rig.run_until("ended")

        assert rig.worker.sample() == ["running rpm=1450"]


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(text)


def rename(path: Path, to: Path) -> None:
    """Rename `path`, again while the worker has it open for a moment: Windows refuses then."""
    deadline = time.monotonic() + 5
    while True:
        try:
            path.rename(to)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


class TestLocalFile:
    """A file on this computer is followed as `tail -F` follows one, which Windows does not have."""

    def follow(self, make: Callable[..., Rig], path: Path) -> Rig:
        rig = make(local(read="file", path=str(path)))
        rig.start()
        return rig

    def messages(self, rig: Rig) -> list[str]:
        return [r["message"] for r in rig.programs("dev")]

    def test_it_runs_no_program_so_it_works_on_windows(
        self, make: Callable[..., Rig], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        path = tmp_path / "pump.txt"
        path.write_text("", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "pump started\n")
            wait_until(lambda: rig.worker.health.lines == 1)
        finally:
            rig.stop()

        assert rig.processes.argvs == []
        assert self.messages(rig) == ["pump started"]
        assert rig.worker.health.reading == "file"

    def test_it_reads_what_is_added_after_it_starts(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        path.write_text("before\n", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "one\ntwo\n")
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two"]

    def test_a_file_that_appears_later_is_read_from_its_start(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        waiting = f"No file {path} yet. Its lines are read once it appears."
        rig = self.follow(make, path)
        try:
            wait_until(lambda: rig.worker.health.message == waiting)
            path.write_text("one\ntwo\n", encoding="utf-8")
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two"]
        assert rig.notes("dev") == [("warn", f"[logs] {waiting}")]

    def test_a_truncated_file_is_read_from_its_start(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        path.write_text("an old line\n", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "one\n")
            wait_until(lambda: rig.worker.health.lines == 1)
            path.write_text("two\n", encoding="utf-8")
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two"]

    def test_a_rotated_file_is_followed_to_the_new_one(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        path.write_text("", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "one\n")
            wait_until(lambda: rig.worker.health.lines == 1)
            rename(path, tmp_path / "pump.log.1")
            path.write_text("two\n", encoding="utf-8")
            wait_until(lambda: rig.worker.health.lines == 2)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two"]
        assert rig.notes("dev") == []

    def test_lines_written_just_before_a_rotation_are_read(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        path.write_text("", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "one\n")
            wait_until(lambda: rig.worker.health.lines == 1)
            append(path, "two\n")
            rename(path, tmp_path / "pump.log.1")
            path.write_text("three\n", encoding="utf-8")
            wait_until(lambda: rig.worker.health.lines == 3)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two", "three"]

    def test_a_deleted_file_is_read_to_its_end_and_its_successor_from_the_start(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        path = tmp_path / "pump.log"
        path.write_text("", encoding="utf-8")
        rig = self.follow(make, path)
        try:
            rig.wait_for("streaming")
            append(path, "one\n")
            wait_until(lambda: rig.worker.health.lines == 1)
            append(path, "two\n")
            path.unlink()
            path.write_text("three\n", encoding="utf-8")
            wait_until(lambda: rig.worker.health.lines == 3)
        finally:
            rig.stop()

        assert self.messages(rig) == ["one", "two", "three"]

    def test_a_file_it_cannot_read_is_reported(
        self, make: Callable[..., Rig], tmp_path: Path
    ) -> None:
        rig = self.follow(make, tmp_path)
        try:
            wait_until(lambda: rig.worker.health.message.startswith(f"Cannot read {tmp_path}: "))
        finally:
            rig.stop()

    def test_stopping_ends_it_at_once(self, make: Callable[..., Rig], tmp_path: Path) -> None:
        path = tmp_path / "pump.log"
        path.write_text("", encoding="utf-8")
        rig = self.follow(make, path)
        rig.wait_for("streaming")
        started = time.monotonic()

        rig.stop()

        assert time.monotonic() - started < 1


class Waits(threading.Event):
    """A stop event whose waits move the clock instead of blocking."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.clock.advance(timeout)
        return self.is_set()


def saved(directory: Path, text: str) -> Path:
    path = directory / "app.log"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (CLOCK_S, CLOCK_S))
    return path


def kernel(uptime_s: float, text: str) -> str:
    return f"[{uptime_s:12.6f}] {text}\n"


class TestFileOverSsh:
    def test_the_probe_asks_about_the_file(self, make: Callable[..., Rig]) -> None:
        rig = make(source(read="file", path="/x"), FakeProcess(probe_output(path="ok")))

        rig.run_until("waiting")

        assert rig.processes.command(0) == probe.script("/x")

    def test_a_file_not_there_yet_is_noted_and_followed_once_it_appears(
        self, make: Callable[..., Rig]
    ) -> None:
        tail = FakeProcess(hold_open=True)
        rig = make(source(read="file", path="/x"), FakeProcess(probe_output(path="missing")), tail)
        rig.start()
        try:
            wait_until(lambda: len(rig.processes.started) == 2)
            assert rig.worker.health.state == "connecting"
            assert (
                rig.worker.health.message == "No file /x yet. Its lines are read once it appears."
            )
            tail.send(b"appeared\n")
            rig.wait_for("streaming")
        finally:
            rig.stop()

        assert rig.notes() == [
            ("warn", "[logs] No file /x yet. Its lines are read once it appears.")
        ]
        assert [r["message"] for r in rig.programs()] == ["appeared"]
        # It can appear before tail starts: all of it is new.
        assert rig.processes.command(1) == "tail -n +1 -F /x 2>/dev/null"

    def test_a_file_that_is_there_is_followed_from_its_next_line(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(source(read="file", path="/x"), FakeProcess(probe_output(path="ok")))

        rig.run_until("waiting")

        assert rig.processes.command(1) == "tail -n 0 -F /x 2>/dev/null"

    @pytest.mark.parametrize(
        ("state", "reason"), [("unreadable", "Permission denied"), ("directory", "Is a directory")]
    )
    def test_a_file_it_cannot_read_is_reported_and_tried_again(
        self, make: Callable[..., Rig], state: str, reason: str
    ) -> None:
        rig = make(source(read="file", path="/x"), FakeProcess(probe_output(path=state)))

        rig.run_until("waiting")

        assert len(rig.processes.started) == 1
        assert rig.notes() == [("warn", f"[logs] Cannot read /x: {reason}.")]


class TestSavedPlace:
    def test_lines_printed_while_the_extension_was_stopped_are_noted_missing(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            source(read="file", path="/x"),
            FakeProcess(probe_output()),
            FakeProcess("two\n", hold_open=True),
        )
        # The run before saved its place 30 s ago.
        rig.cursors.put(
            rig.worker._cursor_key,
            {"boot_id": BOOT, "position": None, "saved_ns": (CLOCK_S - 30) * S},
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert rig.notes() == [
            ("warn", "[logs] Reconnected after 30 s; lines printed meanwhile are missing.")
        ]


class TestKernel:
    """`dmesg -w` prints the kernel's buffer, then follows it; run again, it prints it again."""

    BUFFER = kernel(100, "Booting Linux") + kernel(499, "eth0: link up")
    # What `dmesg | head -n 1` printed in the probe: the buffer's oldest line.
    OLDEST = "[  100.000000] Booting Linux"

    def probe(self, **changes: Any) -> FakeProcess:
        return FakeProcess(probe_output(**{"dmesg": self.OLDEST, **changes}))

    def dmesg(self, **changes: Any) -> config.SourceConfig:
        return source(read="command", command="dmesg -w", **changes)

    def test_the_buffer_lands_at_its_own_times_by_the_probes_uptime(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(self.dmesg(), self.probe(), FakeProcess(self.BUFFER, hold_open=True))

        rig.run_until(lambda: rig.worker.health.lines == 2)

        # The probe read an uptime of 500 s at the host's CLOCK_S.
        assert [r[TIME] for r in rig.programs()] == [CLOCK_S - 400.0, CLOCK_S - 1.0]

    def test_the_buffer_is_placed_by_the_kernels_own_clock_where_the_journal_measured_it(
        self, make: Callable[..., Rig]
    ) -> None:
        # The kernel's clock runs 2 s behind the monotonic clock, as NTP never corrects it.
        rig = make(
            self.dmesg(),
            self.probe(kernel_lag_s=2),
            FakeProcess(
                kernel(100, "Booting Linux") + kernel(497, "eth0: link up"), hold_open=True
            ),
        )

        rig.run_until(lambda: rig.worker.health.lines == 2)

        assert [r[TIME] for r in rig.programs()] == [CLOCK_S - 398.0, CLOCK_S - 1.0]

    def test_a_rerun_after_a_drop_records_only_what_is_new(self, make: Callable[..., Rig]) -> None:
        rig = make(
            self.dmesg(),
            self.probe(),
            FakeProcess(self.BUFFER, "", 1),
            self.probe(),
            FakeProcess(self.BUFFER + kernel(510, "usb 1-1: new device"), hold_open=True),
        )

        rig.run_until(lambda: rig.worker.health.lines == 3)

        assert [r["message"] for r in rig.programs()] == [
            "Booting Linux",
            "eth0: link up",
            "usb 1-1: new device",
        ]
        assert rig.notes() == [("warn", "[logs] root@bench-1: the log ended (exit 1).")]

    def test_the_last_line_read_is_saved(self, make: Callable[..., Rig]) -> None:
        rig = make(self.dmesg(), self.probe(), FakeProcess(self.BUFFER, hold_open=True))

        rig.run_until(lambda: rig.worker.health.lines == 2)

        assert rig.saved()[rig.worker._cursor_key] == {
            "boot_id": BOOT,
            "position": [499 * S, "[  499.000000] eth0: link up"],
            "saved_ns": CLOCK_S * S,
        }

    def test_a_restarted_extension_goes_on_from_the_saved_line(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            self.dmesg(),
            self.probe(),
            FakeProcess(self.BUFFER + kernel(510, "usb 1-1: new device"), hold_open=True),
        )
        rig.cursors.put(
            rig.worker._cursor_key,
            {
                "boot_id": BOOT,
                "position": [499 * S, "[  499.000000] eth0: link up"],
                "saved_ns": (CLOCK_S - 30) * S,
            },
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert [r["message"] for r in rig.programs()] == ["usb 1-1: new device"]
        assert rig.notes() == []

    def test_after_a_reboot_the_new_boots_buffer_is_recorded(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            self.dmesg(),
            self.probe(),
            FakeProcess(self.BUFFER, "", 1),
            self.probe(
                boot="0b1a5e1d-0000-4000-8000-000000000001",
                uptime="30.00",
                dmesg="[    0.500000] Booting Linux again",
            ),
            FakeProcess(kernel(0.5, "Booting Linux again"), hold_open=True),
        )

        rig.run_until(lambda: rig.worker.health.lines == 3)

        assert [r["message"] for r in rig.programs()][-1] == "Booting Linux again"
        assert ("warn", "[logs] device restarted") in rig.notes()

    def test_another_boards_console_keeps_every_line_and_is_placed_as_it_arrives(
        self, make: Callable[..., Rig]
    ) -> None:
        """A command relaying a second board's console prints kernel stamps of its own."""
        relay = kernel(1.1, "acme: tick 1") + kernel(1.2, "acme: tick 2")
        rig = make(
            source(read="command", command="cat /dev/ttyUSB0"),
            self.probe(),
            FakeProcess(relay, hold_open=True),
            retention=timedelta(seconds=60),
        )

        rig.run_until(lambda: rig.worker.health.lines == 2)

        assert [(r["message"], r[TIME]) for r in rig.programs()] == [
            ("acme: tick 1", float(CLOCK_S)),
            ("acme: tick 2", CLOCK_S + 0.000001),
        ]
        assert rig.saved()[rig.worker._cursor_key]["position"] is None

    def test_new_lines_only_leaves_the_buffer_out(self, make: Callable[..., Rig]) -> None:
        rig = make(
            self.dmesg(advanced={"history": "new"}),
            self.probe(),
            FakeProcess(self.BUFFER + kernel(501, "after the start"), hold_open=True),
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert [r["message"] for r in rig.programs()] == ["after the start"]

    def test_history_goes_back_only_as_far_as_the_agent_keeps_data(
        self, make: Callable[..., Rig]
    ) -> None:
        rig = make(
            self.dmesg(),
            self.probe(),
            FakeProcess(self.BUFFER, hold_open=True),
            retention=timedelta(seconds=60),
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert [r["message"] for r in rig.programs()] == ["eth0: link up"]

    def test_this_computers_kernel_maps_a_local_command(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        boot = tmp_path / "boot_id"
        boot.write_text("f07dd35d-996b-4983-98f1-e4347c93e456\n")
        monkeypatch.setattr(worker, "_BOOT_ID", boot)
        monkeypatch.setattr(worker.time, "monotonic_ns", lambda: 500 * S)
        monkeypatch.setattr(worker, "_oldest_kernel_line", lambda: self.OLDEST)
        rig = make(command("dmesg -w"), FakeProcess(self.BUFFER, hold_open=True))

        # Linux only while the lines are read: the stop signals the process as this platform does.
        with monkeypatch.context() as linux:
            linux.setattr(worker.sys, "platform", "linux")
            rig.start()
            wait_until(lambda: rig.worker.health.lines == 2)
        rig.stop()

        assert [r[TIME] for r in rig.programs("dev")] == [CLOCK_S - 400.0, CLOCK_S - 1.0]

    def test_automatic_values_leave_the_kernels_lines_out(self, make: Callable[..., Rig]) -> None:
        rig = make(
            self.dmesg(),
            self.probe(),
            FakeProcess(kernel(499, "SLUB: CPUs=4, Nodes=1") + "rpm=5\n", hold_open=True),
        )

        rig.run_until(lambda: rig.worker.health.lines == 2)

        events = rig.recording.events()
        assert "bench-1/kernel" not in events
        assert [r["rpm"] for r in events["bench-1/values"]] == [5]

    def test_values_on_reads_the_kernels_lines_too(self, make: Callable[..., Rig]) -> None:
        rig = make(
            self.dmesg(advanced={"values": "on"}),
            self.probe(),
            FakeProcess(kernel(499, "SLUB: CPUs=4, Nodes=1"), hold_open=True),
        )

        rig.run_until(lambda: rig.worker.health.lines == 1)

        assert [r["CPUs"] for r in rig.recording.events()["bench-1/kernel"]] == [4]


def mac_entry(message: str, kind: str = "Default", stamp: str = "12:30:55.030238") -> str:
    return (
        json.dumps(
            {
                "eventType": "logEvent",
                "messageType": kind,
                "timestamp": f"2026-10-23 {stamp}-0700",
                "processImagePath": "/usr/libexec/rapportd",
                "eventMessage": message,
            }
        )
        + "\n"
    )


class TestThisComputersLog:
    """On macOS and Windows, the system log of this computer is its own log, not a journal."""

    # 2026-10-23 12:30:55.030238 -0700.
    STAMP_S = 1_792_783_855.030238

    def test_a_mac_follows_its_unified_log_with_no_probe(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        rig = make(
            local(read="system"),
            FakeProcess(mac_entry("started") + mac_entry("link down", "Error", "12:30:56.5")),
        )

        rig.run_until("waiting")

        assert rig.processes.command(0) == oslog.MACOS
        assert [(r["name"], r["level"], r["message"]) for r in rig.programs("dev")] == [
            ("rapportd", "info", "started"),
            ("rapportd", "error", "link down"),
        ]
        times = [r[TIME] for r in rig.programs("dev")]
        assert times == pytest.approx([self.STAMP_S, 1_792_783_856.5], abs=1e-6)
        assert rig.worker.health.reading == "unified_log"

    def test_an_activity_is_left_out(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        activity = json.loads(mac_entry("state update")) | {"eventType": "activityCreateEvent"}
        rig = make(local(read="system"), FakeProcess(json.dumps(activity) + "\n" + mac_entry("a")))

        rig.run_until("waiting")

        assert [r["message"] for r in rig.programs("dev")] == ["a"]

    def test_entries_stamped_alike_keep_their_order(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        rig = make(
            local(read="system"), FakeProcess(mac_entry("a") + mac_entry("b") + mac_entry("c"))
        )

        rig.run_until("waiting")

        rows = rig.programs("dev")
        assert [r["message"] for r in rows] == ["a", "b", "c"]
        assert [round((r[TIME] - self.STAMP_S) * 1e6) for r in rows] == [0, 1, 2]

    def test_what_the_log_command_says_itself_is_a_note(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        rig = make(local(read="system"), FakeProcess("log: Cannot run while sandboxed\n"))

        rig.run_until("waiting")

        assert ("warn", "[logs] log: Cannot run while sandboxed") in rig.notes("dev")

    def test_windows_follows_its_event_log(
        self, make: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        record = {
            "time": "2026-10-23T19:30:55.0302380Z",
            "level": 3,
            "source": "Service Control Manager",
            "message": "A timeout was reached.\r\nThe service did not respond.",
        }
        rig = make(local(read="system"), FakeProcess(json.dumps(record) + "\n"))

        rig.run_until("waiting")

        assert rig.processes.command(0) == oslog.WINDOWS
        assert [(r["name"], r["level"], r["message"]) for r in rig.programs("dev")] == [
            (
                "Service Control Manager",
                "warn",
                "A timeout was reached.\nThe service did not respond.",
            )
        ]
        assert [r[TIME] for r in rig.programs("dev")] == pytest.approx([self.STAMP_S], abs=1e-6)
        assert rig.worker.health.reading == "event_log"
