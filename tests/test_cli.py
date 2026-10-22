"""The command line: saved log files converted to traces, or one replayed into the agent."""

import gzip
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import zelos_sdk
from zelos_log_lines import files
from zelos_log_lines.placing import Timing

from tests.conftest import TIME, Recording, read_events
from zelos_extension_logs import ACTION_PREFIX, cli

# Zephyr lines that print only the device's uptime: a file of them ends at its last change.
TEXT = "[00:00:01.000,000] <inf> dcdc: rail=13.6V\n[00:00:03.000,000] <wrn> bms: cell low\n"
CHANGED_S = 1_792_600_000


def saved(directory: Path, name: str = "app.log", data: str | bytes = TEXT) -> Path:
    path = directory / name
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    os.utime(path, (CHANGED_S, CHANGED_S))
    return path


def sources(trace: Path) -> set[str]:
    with zelos_sdk.TraceReader(str(trace)) as reader:
        return {source.name for source in reader.list_fields()}


class TestConvert:
    def test_files_may_come_after_an_option(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = saved(tmp_path, "a.log"), saved(tmp_path, "b.log")
        assert cli.main(["convert", str(first), "--zone", "UTC", str(second)]) == 0
        assert len(capsys.readouterr().out.splitlines()) == 2

    def test_an_unknown_option_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exited:
            cli.main(["convert", str(saved(tmp_path)), "--speed", "2"])
        assert exited.value.code == 2

    def test_the_trace_goes_beside_the_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = saved(tmp_path)

        assert cli.main(["convert", str(path)]) == 0

        trace = tmp_path / "app.trz"
        assert capsys.readouterr().out == f"{path} -> {trace} (2 lines)\n"
        assert sources(trace) == {ACTION_PREFIX}
        rows = read_events(trace)["app/log"]
        assert [(row["level"], row["message"], row[TIME]) for row in rows] == [
            ("info", "dcdc: rail=13.6V", CHANGED_S - 2.0),
            ("warn", "bms: cell low", float(CHANGED_S)),
        ]

    def test_each_file_gets_its_own_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = saved(tmp_path, "app.log")
        second = saved(tmp_path, "bench-1.log.gz", gzip.compress(TEXT.encode()))

        assert cli.main(["convert", str(first), str(second)]) == 0

        assert capsys.readouterr().out.splitlines() == [
            f"{first} -> {tmp_path / 'app.trz'} (2 lines)",
            f"{second} -> {tmp_path / 'bench-1.trz'} (2 lines)",
        ]
        assert "bench-1/log" in read_events(tmp_path / "bench-1.trz")

    def test_output_and_name_and_start(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = saved(tmp_path)
        trace = tmp_path / "run.trz"

        assert (
            cli.main(
                [
                    *("convert", str(path), "-o", str(trace)),
                    *("--name", "bench-1", "--start", "2026-10-21T14:03:01+02:00"),
                ]
            )
            == 0
        )

        assert capsys.readouterr().out == f"{path} -> {trace} (2 lines)\n"
        start_s = 1_792_584_181.0
        assert [row[TIME] for row in read_events(trace)["bench-1/log"]] == [
            start_s,
            start_s + 2,
        ]

    def test_the_zone_and_year_reach_the_conversion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        def convert(path: Path, output: Path | None, **kwargs: Any) -> tuple[Path, int]:
            calls.append({"path": path, "output": output, **kwargs})
            return tmp_path / "app.trz", 2

        monkeypatch.setattr(files, "convert", convert)
        path = saved(tmp_path)

        assert cli.main(["convert", str(path), "--zone", "Europe/Berlin", "--year", "2025"]) == 0

        assert calls == [
            {
                "path": path,
                "output": None,
                "source": ACTION_PREFIX,
                "name": None,
                "timing": Timing(ZoneInfo("Europe/Berlin"), 2025, None),
                "overwrite": False,
            }
        ]

    def test_one_output_takes_one_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = saved(tmp_path, "app.log"), saved(tmp_path, "bench-1.log")

        with pytest.raises(SystemExit) as exited:
            cli.main(["convert", str(first), str(second), "-o", str(tmp_path / "run.trz")])

        assert exited.value.code == 2
        assert "-o names one trace, but 2 files were given" in capsys.readouterr().err
        assert list(tmp_path.glob("*.trz")) == []

    def test_an_existing_trace_is_kept_unless_forced(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = saved(tmp_path)
        trace = tmp_path / "app.trz"
        trace.write_bytes(b"an earlier trace")

        assert cli.main(["convert", str(path)]) == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"{path}: The output exists: {trace}. Add -f to overwrite it.\n"
        assert trace.read_bytes() == b"an earlier trace"

        assert cli.main(["convert", "-f", str(path)]) == 0

        assert capsys.readouterr().out == f"{path} -> {trace} (2 lines)\n"
        assert len(read_events(trace)["app/log"]) == 2

    def test_a_file_that_fails_does_not_stop_the_rest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "gone.log"
        damaged = saved(tmp_path, "old.log.gz", gzip.compress(TEXT.encode())[:-12])
        path = saved(tmp_path)

        assert cli.main(["convert", str(missing), str(damaged), str(path)]) == 1

        captured = capsys.readouterr()
        assert captured.err.splitlines() == [
            f"{missing}: No such file or directory.",
            f"{damaged}: Compressed file ended before the end-of-stream marker was reached.",
        ]
        assert captured.out == f"{path} -> {tmp_path / 'app.trz'} (2 lines)\n"
        assert sorted(trace.name for trace in tmp_path.glob("*.trz")) == ["app.trz"]

    def test_a_forced_conversion_of_a_missing_file_keeps_its_trace(self, tmp_path: Path) -> None:
        trace = tmp_path / "gone.trz"
        trace.write_bytes(b"an earlier trace")

        assert cli.main(["convert", "-f", str(tmp_path / "gone.log")]) == 1

        assert trace.read_bytes() == b"an earlier trace"

    @pytest.mark.parametrize(
        ("option", "value", "problem"),
        [
            ("--zone", "Mars/Base", "'Mars/Base' is not an IANA time zone, such as Europe/Berlin"),
            ("--year", "26", "must be from 1970 to 2261"),
            (
                "--start",
                "2026-10-21 14:03",
                "'2026-10-21 14:03' is not an ISO 8601 time with an offset, such as "
                "2026-10-21T14:03:01+02:00",
            ),
        ],
    )
    def test_a_timing_option_it_cannot_read(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        option: str,
        value: str,
        problem: str,
    ) -> None:
        path = saved(tmp_path)

        with pytest.raises(SystemExit) as exited:
            cli.main(["convert", str(path), option, value])

        assert exited.value.code == 2
        assert f"argument {option}: {problem}" in capsys.readouterr().err
        assert not (tmp_path / "app.trz").exists()


class Agent:
    """Stands in for the agent: the global source is a recording, and init() is noted."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, recording: Recording) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        def init_global_source(*args: Any, **kwargs: Any) -> zelos_sdk.TraceSource:
            self.calls.append(("init_global_source", args, kwargs))
            return recording.source

        def init(*args: Any, **kwargs: Any) -> None:
            self.calls.append(("init", args, kwargs))

        monkeypatch.setattr(zelos_sdk, "init_global_source", init_global_source)
        monkeypatch.setattr(zelos_sdk, "init", init)


@pytest.fixture
def signals() -> Any:
    """Puts back the handlers a replay installs."""
    earlier = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, handler in earlier.items():
        signal.signal(s, handler)


@pytest.mark.usefixtures("signals")
class TestReplay:
    def test_the_file_streams_into_the_agent_the_first_line_now(
        self,
        tmp_path: Path,
        trace_file: Recording,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        agent = Agent(monkeypatch, trace_file)
        # Printed at the same uptime: each line follows the one before by 1 µs.
        path = saved(
            tmp_path,
            data="[00:00:01.000,000] <inf> pump: started\n"
            "[00:00:01.000,000] <inf> pump: running rpm=1450\n",
        )
        started = time.time()

        assert cli.main(["replay", str(path), "--name", "bench-1"]) == 0

        assert capsys.readouterr().out == f"{path}: 2 lines replayed\n"
        assert agent.calls == [
            ("init_global_source", (ACTION_PREFIX,), {}),
            ("init", (), {"name": ACTION_PREFIX}),
        ]
        events = trace_file.events()
        rows = events["bench-1/log"]
        assert [row["message"] for row in rows] == ["pump: started", "pump: running rpm=1450"]
        assert all(started - 1 < row[TIME] < time.time() + 1 for row in rows)
        assert [row["rpm"] for row in events["bench-1/pump"]] == [1450]

    def test_a_missing_file(
        self,
        tmp_path: Path,
        trace_file: Recording,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        agent = Agent(monkeypatch, trace_file)
        missing = tmp_path / "gone.log"

        assert cli.main(["replay", str(missing)]) == 1

        assert capsys.readouterr().err == f"{missing}: No such file.\n"
        assert agent.calls == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_a_signal_stops_it(
        self,
        signum: signal.Signals,
        tmp_path: Path,
        trace_file: Recording,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        Agent(monkeypatch, trace_file)
        # The second line printed an hour after the first.
        path = saved(
            tmp_path,
            data="[00:00:01.000,000] <inf> a: one\n[01:00:01.000,000] <inf> a: two\n",
        )
        timer = threading.Timer(0.5, os.kill, (os.getpid(), signum))
        started = time.monotonic()
        try:
            timer.start()
            assert cli.main(["replay", str(path)]) == 0
        finally:
            timer.cancel()

        assert time.monotonic() - started < 5
        assert capsys.readouterr().out == f"{path}: 1 line replayed\n"
        assert [row["message"] for row in trace_file.events()["app/log"]] == ["a: one"]
