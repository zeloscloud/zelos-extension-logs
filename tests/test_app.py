"""The extension's life cycle: workers start with run() and stop when it is told to."""

import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import zelos_sdk

from tests.conftest import FakeProcess, Processes, Recording, wait_until
from zelos_extension_logs import ACTION_PREFIX, actions, app, config, remote

DEMO = {"connection": "demo"}


@pytest.fixture(autouse=True)
def clean_up() -> Iterator[None]:
    """run() leaves its workers in `actions.workers` and sets a log level; neither may leak."""
    level = logging.getLogger("zelos_extension_logs").level
    before = list(logging.getLogger().handlers)
    yield
    for worker in list(actions.workers.values()):
        worker.stop()
        worker.join(5)
    actions.workers.clear()
    logging.getLogger("zelos_extension_logs").setLevel(level)
    for handler in list(logging.getLogger().handlers):
        if handler not in before:
            logging.getLogger().removeHandler(handler)


def settings(*sources: dict[str, Any], **advanced: Any) -> config.Settings:
    raw: dict[str, Any] = {"sources": list(sources)}
    if advanced:
        raw["advanced"] = advanced
    return config.parse(raw)


class Running:
    """`app.run()` in a thread, until `stop` is set."""

    def __init__(self, recording: Recording, tmp_path: Path, **kwargs: Any) -> None:
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.data_dir = tmp_path / "data"
        self._thread = threading.Thread(
            target=self._run,
            kwargs={
                "source": recording.source,
                "data_dir": self.data_dir,
                "stop": self.stop,
                **kwargs,
            },
        )

    def _run(self, **kwargs: Any) -> None:
        try:
            app.run(**kwargs)
        except BaseException as e:
            self.error = e

    def __enter__(self) -> "Running":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.finish()

    def finish(self) -> float:
        """Stop it and return how long stopping took."""
        started = time.monotonic()
        self.stop.set()
        self._thread.join(10)
        assert not self._thread.is_alive(), "run() did not return"
        assert self.error is None, self.error
        return time.monotonic() - started


@pytest.fixture
def running(trace_file: Recording, tmp_path: Path) -> Callable[..., Running]:
    def start(**kwargs: Any) -> Running:
        return Running(trace_file, tmp_path, **kwargs)

    return start


def test_a_demo_source_writes_rows_into_the_trace(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    with running(settings=settings(DEMO)) as app_running:
        wait_until(
            lambda: actions.workers.get("demo") and actions.workers["demo"].health.lines >= 3
        )
        app_running.finish()

    events = trace_file.events()
    rows = events["demo/log"]
    messages = {r["message"] for r in rows}
    # The pump's printed values are signals.
    assert {"rpm", "flow"} <= set(events["demo/pump"][0])
    assert "Booting Linux on physical CPU 0x0" in messages
    assert "pump service started" in messages
    assert {r["name"] for r in rows} >= {"kernel", "pump"}
    assert len(rows) >= 3
    # A row's time is a real one, not the 1970 the demo board's own clock starts at.
    assert all(abs(r["@time_s"] - time.time()) < 60 for r in rows)


def test_the_demo_runs_for_a_few_seconds_without_a_connection(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    with running(settings=settings(DEMO)) as app_running:
        time.sleep(3)
        lines = actions.workers["demo"].health.lines
        app_running.finish()

    rows = trace_file.events()["demo/log"]
    assert lines >= 6
    assert len(rows) >= lines
    assert actions.workers["demo"].health.state == "stopped"


def test_the_position_is_kept_in_the_data_directory(
    running: Callable[..., Running],
) -> None:
    with running(settings=settings(DEMO)) as app_running:
        wait_until(
            lambda: actions.workers.get("demo") and actions.workers["demo"].health.lines >= 3
        )
        app_running.finish()

    saved = json.loads((app_running.data_dir / "cursors.json").read_text(encoding="utf-8"))
    assert list(saved) == ["demo\0demo\0"]
    assert saved["demo\0demo\0"]["cursor"].startswith("s=demo;")


def test_each_source_gets_a_worker_under_its_name(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    ssh = {
        "connection": "ssh",
        "host": "root@bench-1",
        "name": "bench-1",
        "read": "file",
        "path": "/x",
    }
    board = FakeProcess("[2026-01-01 12:00:00.123] [pump] [info] started\n", hold_open=True)
    fakes = Processes(FakeProcess(PROBE), board)

    with running(settings=settings(DEMO, ssh), runner=fakes) as app_running:
        wait_until(
            lambda: (
                len(actions.workers) == 2
                and all(w.health.lines >= 1 for w in actions.workers.values())
            )
        )
        assert sorted(actions.workers) == ["bench-1", "demo"]
        app_running.finish()

    events = trace_file.events()
    assert any(r["message"] == "started" for r in events["bench-1/log"])
    assert "demo/log" in events
    assert fakes.command(1) == "tail -n 0 -F /x 2>/dev/null"
    fakes.close()


def test_stop_ends_every_worker_within_a_second(
    running: Callable[..., Running],
) -> None:
    streams = [FakeProcess(hold_open=True) for _ in range(3)]
    sources = [
        {"connection": "local", "read": "command", "command": f"tail{n}", "name": f"dev-{n}"}
        for n in range(3)
    ]
    fakes = Processes(*streams)

    with running(settings=settings(*sources), runner=fakes) as app_running:
        wait_until(lambda: len(fakes.started) == 3)
        workers = list(actions.workers.values())
        took = app_running.finish()

    assert took < 3
    assert all(not w.is_alive() for w in workers)
    assert all(s.stdin.closed for s in streams)
    fakes.close()


def python(code: str) -> str:
    """A command line that runs `code` in this Python, in POSIX sh and in cmd.exe alike."""
    return f'"{sys.executable}" -c "{code}"'


@pytest.mark.skipif(sys.platform == "win32", reason="Windows stops each source with taskkill")
def test_stopping_many_sources_whose_processes_outlive_stdin_takes_one_second(
    running: Callable[..., Running],
) -> None:
    command = python("import time; print('up', flush=True); time.sleep(30)")
    sources = [
        {"connection": "local", "read": "command", "command": command, "name": f"dev-{n}"}
        for n in range(8)
    ]

    with running(settings=settings(*sources)) as app_running:
        wait_until(
            lambda: (
                len(actions.workers) == 8 and all(w.health.lines for w in actions.workers.values())
            ),
            timeout=20,
        )
        workers = list(actions.workers.values())
        took = app_running.finish()

    assert took < 1.5
    assert not any(w.is_alive() for w in workers)


def test_a_process_that_ignores_sigterm_is_killed_on_the_way_out(
    running: Callable[..., Running],
) -> None:
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('up', flush=True); time.sleep(30)"
    )
    source = {"connection": "local", "read": "command", "command": python(code), "name": "dev"}

    with running(settings=settings(source)) as app_running:
        wait_until(lambda: "dev" in actions.workers and actions.workers["dev"].health.lines)
        worker = actions.workers["dev"]
        took = app_running.finish()

    assert took < 1.5
    assert not worker.is_alive()


def test_the_trace_log_handler_is_removed_and_the_sdk_actions_are_left_out(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    with running(settings=settings(DEMO)) as app_running:
        wait_until(
            lambda: actions.workers.get("demo") and actions.workers["demo"].health.lines >= 1
        )
        assert len(root.handlers) == len(before) + 1
        logging.getLogger("zelos_extension_logs.app_test").warning("from the extension")
        logging.getLogger("zelos_sdk.actions.run").warning("an action failed")
        app_running.finish()

    assert root.handlers == before
    log = trace_file.events()["log"]
    assert [r["message"] for r in log] == ["from the extension"]
    assert log[0]["level"] == "warn"


def test_the_log_level_comes_from_the_settings(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    with running(settings=settings(DEMO, log_level="ERROR")) as app_running:
        wait_until(
            lambda: actions.workers.get("demo") and actions.workers["demo"].health.lines >= 1
        )
        logging.getLogger("zelos_extension_logs.app_test").warning("quiet")
        logging.getLogger("zelos_extension_logs.app_test").error("loud")
        app_running.finish()

    assert [r["message"] for r in trace_file.events()["log"]] == ["loud"]
    assert logging.getLogger("zelos_extension_logs").level == logging.ERROR


def test_running_again_replaces_the_workers(
    running: Callable[..., Running],
) -> None:
    actions.workers["stale"] = object()  # type: ignore[assignment]

    with running(settings=settings(DEMO)) as app_running:
        wait_until(lambda: "demo" in actions.workers)
        assert list(actions.workers) == ["demo"]
        app_running.finish()


class TestBadConfig:
    def write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: object) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))

    def test_exits_with_one_and_logs_the_sentence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.write(tmp_path, monkeypatch, {"sources": [{"connection": "ssh"}]})

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as caught:
            app.run(stop=threading.Event())

        assert caught.value.code == 1
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.getMessage() for r in errors] == [
            "Source 1, Host: enter [user@]host or a Host from your SSH config."
        ]
        assert errors[0].name == "zelos_extension_logs.app"
        assert actions.workers == {}

    def test_every_problem_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.write(
            tmp_path,
            monkeypatch,
            {"sources": [{"connection": "ssh"}, {"connection": "local", "read": "file"}]},
        )

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
            app.run(stop=threading.Event())

        assert [r.getMessage() for r in caplog.records] == [
            "Source 1, Host: enter [user@]host or a Host from your SSH config.",
            "Source 2, File: enter the file to follow.",
        ]

    def test_no_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ZELOS_CONFIG_PATH", str(tmp_path / "absent.json"))

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as caught:
            app.run(stop=threading.Event())

        assert caught.value.code == 1
        assert [r.getMessage() for r in caplog.records] == ["Add at least one source."]

    def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.json"
        path.write_text("{oops", encoding="utf-8")
        monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
            app.run(stop=threading.Event())

        assert caplog.records[0].getMessage().startswith("The configuration is not valid JSON")

    def test_nothing_starts_before_the_configuration_is_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.write(tmp_path, monkeypatch, {"sources": [{"connection": "ssh"}]})
        started = threading.active_count()

        with pytest.raises(SystemExit):
            app.run(stop=threading.Event())

        assert threading.active_count() == started

    def test_a_good_file_is_the_default_configuration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trace_file: Recording
    ) -> None:
        self.write(tmp_path, monkeypatch, {"sources": [DEMO]})
        stop = threading.Event()
        thread = threading.Thread(
            target=app.run,
            kwargs={"source": trace_file.source, "data_dir": tmp_path / "d", "stop": stop},
        )
        thread.start()
        try:
            wait_until(lambda: "demo" in actions.workers)
        finally:
            stop.set()
            thread.join(10)

        assert not thread.is_alive()


def test_without_a_source_the_global_one_is_named_and_the_sdk_started(
    monkeypatch: pytest.MonkeyPatch, trace_file: Recording, tmp_path: Path
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def init_global_source(*args: Any, **kwargs: Any) -> zelos_sdk.TraceSource:
        calls.append(("init_global_source", args, kwargs))
        return trace_file.source

    def init(*args: Any, **kwargs: Any) -> None:
        calls.append(("init", args, kwargs))

    monkeypatch.setattr(zelos_sdk, "init_global_source", init_global_source)
    monkeypatch.setattr(zelos_sdk, "init", init)
    stop = threading.Event()
    stop.set()

    app.run(settings=settings(DEMO, prefix="Boards"), data_dir=tmp_path, stop=stop)

    assert calls == [
        ("init_global_source", ("Boards",), {}),
        ("init", (), {"name": ACTION_PREFIX, "actions": True}),
    ]


class FakeAgent:
    """Stands in for `zelos_sdk.Agent`: answers `info()` with these settings, or fails."""

    def __init__(self, settings: object, error: Exception | None = None) -> None:
        self.settings = settings
        self.error = error

    def __call__(self) -> "FakeAgent":
        return self

    def __enter__(self) -> "FakeAgent":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def info(self) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(settings=self.settings)


PROBE = (
    "uid=0\nuser=root\ngroups=root\nsystemd=252\n"
    "boot_id=f07dd35d-996b-4983-98f1-e4347c93e456\n"
    'journal={"__REALTIME_TIMESTAMP":"1700000000000000","__MONOTONIC_TIMESTAMP":"500000000"}\n'
    "clock=1700000000\nclock_ns=1700000000000000000\nuptime=500.00\nend=1\n"
)


@pytest.mark.parametrize(
    ("agent", "since"),
    [
        (
            FakeAgent(SimpleNamespace(data_retention=timedelta(hours=6))),
            f" -b --since=@{1_700_000_000 - 6 * 3600}",
        ),
        (FakeAgent(SimpleNamespace(data_retention=None)), " -b"),
        (FakeAgent(None), f" -b --since=@{1_700_000_000 - 3600}"),
        (
            FakeAgent(None, zelos_sdk.AgentError("no agent")),
            f" -b --since=@{1_700_000_000 - 3600}",
        ),
    ],
    ids=["six hours", "no limit", "settings unknown", "no answer"],
)
def test_a_first_start_reads_the_journal_as_far_back_as_the_agent_keeps_data(
    monkeypatch: pytest.MonkeyPatch,
    trace_file: Recording,
    tmp_path: Path,
    agent: FakeAgent,
    since: str,
) -> None:
    monkeypatch.setattr(zelos_sdk, "init_global_source", lambda *a, **k: trace_file.source)
    monkeypatch.setattr(zelos_sdk, "init", lambda *a, **k: None)
    monkeypatch.setattr(zelos_sdk, "Agent", agent)
    fakes = Processes(FakeProcess(PROBE), FakeProcess(hold_open=True))
    board = {"connection": "ssh", "host": "root@bench-1", "name": "bench-1"}
    stop = threading.Event()
    thread = threading.Thread(
        target=app.run,
        kwargs={"settings": settings(board), "data_dir": tmp_path, "runner": fakes, "stop": stop},
    )
    thread.start()
    try:
        wait_until(lambda: len(fakes.started) == 2)
    finally:
        stop.set()
        thread.join(10)
        fakes.close()

    assert fakes.command(1).endswith(since)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_it(signum: signal.Signals, trace_file: Recording, tmp_path: Path) -> None:
    """With no `stop` event, run() installs its handlers, and a signal ends it."""
    earlier = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    # If run() never installed its own, the signal would end pytest: this one only counts.
    seen: list[int] = []
    for s in earlier:
        signal.signal(s, lambda n, _f: seen.append(n))
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signum))
    started = time.monotonic()
    try:
        timer.start()
        app.run(source=trace_file.source, settings=settings(DEMO), data_dir=tmp_path)
    finally:
        timer.cancel()
        for s, handler in earlier.items():
            signal.signal(s, handler)

    assert seen == []
    assert 0.9 < time.monotonic() - started < 5


def test_the_runner_is_the_workers_process_starter(
    running: Callable[..., Running], trace_file: Recording
) -> None:
    fakes = Processes(FakeProcess("first\nsecond\n"))
    cfg = {"connection": "local", "read": "command", "command": "tail", "name": "dev"}

    with running(settings=settings(cfg), runner=fakes) as app_running:
        wait_until(lambda: "dev" in actions.workers)
        wait_until(lambda: actions.workers["dev"].health.state == "ended")
        app_running.finish()

    assert fakes.argvs == [remote.local_argv("tail")]
    rows = trace_file.events()["dev/log"]
    assert sorted(r["message"] for r in rows if r["name"] != "logs") == ["first", "second"]
    fakes.close()
