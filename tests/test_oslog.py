"""This computer's log on macOS and Windows: the command that follows it, and its entries."""

import base64
import shlex
from typing import Any

import pytest

from zelos_extension_logs import oslog

# As `log stream --style ndjson` prints an entry, less the fields Logs does not read.
MAC: dict[str, Any] = {
    "eventType": "logEvent",
    "messageType": "Default",
    "timestamp": "2026-10-23 12:30:55.030238-0700",
    "processImagePath": "/usr/bin/logger",
    "processID": 4198,
    "subsystem": "",
    "category": "",
    "eventMessage": "ZA mac canary rpm=5",
}
# As the event log poller prints a record.
WINDOWS: dict[str, Any] = {
    "time": "2026-10-23T19:39:06.4412602Z",
    "level": 4,
    "source": "ZelosLogsSys",
    "message": "ZA win system info",
}


class TestMacos:
    def test_an_entry(self) -> None:
        assert oslog.read("darwin", MAC) == oslog.Entry(
            "ZA mac canary rpm=5", "info", "logger", 1_792_783_855_030_238_000
        )

    @pytest.mark.parametrize(
        ("kind", "level"),
        [
            ("Default", "info"),
            ("Info", "info"),
            ("Debug", "debug"),
            ("Error", "error"),
            ("Fault", "critical"),
        ],
    )
    def test_each_level(self, kind: str, level: str) -> None:
        entry = oslog.read("darwin", {**MAC, "messageType": kind})

        assert entry is not None and entry.level == level

    def test_the_program_is_the_process_name(self) -> None:
        entry = oslog.read("darwin", {**MAC, "processImagePath": "/kernel"})

        assert entry is not None and entry.program == "kernel"

    def test_an_activity_is_no_entry(self) -> None:
        assert oslog.read("darwin", {**MAC, "eventType": "activityCreateEvent"}) is None

    def test_a_stamp_it_cannot_read_is_no_entry(self) -> None:
        assert oslog.read("darwin", {**MAC, "timestamp": "yesterday"}) is None


class TestWindows:
    def test_an_entry(self) -> None:
        assert oslog.read("win32", WINDOWS) == oslog.Entry(
            "ZA win system info", "info", "ZelosLogsSys", 1_792_784_346_441_260_000
        )

    @pytest.mark.parametrize(
        ("number", "level"),
        [(0, "info"), (1, "critical"), (2, "error"), (3, "warn"), (4, "info"), (5, "debug")],
    )
    def test_each_level(self, number: int, level: str) -> None:
        entry = oslog.read("win32", {**WINDOWS, "level": number})

        assert entry is not None and entry.level == level

    def test_a_message_over_lines_keeps_them(self) -> None:
        entry = oslog.read("win32", {**WINDOWS, "message": "first\r\nsecond\r\n"})

        assert entry is not None and entry.message == "first\nsecond"

    def test_a_time_with_no_zone_is_no_entry(self) -> None:
        assert oslog.read("win32", {**WINDOWS, "time": "2026-10-23T19:39:06.4412602"}) is None

    def test_a_record_with_no_message_is_no_entry(self) -> None:
        assert oslog.read("win32", {k: v for k, v in WINDOWS.items() if k != "message"}) is None


class TestCommand:
    def test_macos_streams_its_unified_log(self) -> None:
        assert oslog.command("darwin") == "/usr/bin/log stream --style ndjson"

    def test_windows_polls_the_system_and_application_logs(self) -> None:
        words = shlex.split(oslog.command("win32") or "")
        script = base64.b64decode(words[words.index("-EncodedCommand") + 1]).decode("utf-16-le")

        assert words[0] == "powershell.exe"
        assert "$logs = 'System', 'Application'" in script
        assert "Get-WinEvent" in script

    def test_linux_reads_its_journal_or_syslog_instead(self) -> None:
        assert oslog.command("linux") is None
