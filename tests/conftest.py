"""Shared by every test: an isolated trace recording, scripted stand-ins for the processes a worker
starts, a fake clock, and the actions run as the agent runs them."""

import io
import os
import queue
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import zelos_sdk

from zelos_extension_logs import ACTION_PREFIX, actions, remote

FIXTURES = Path(__file__).parent / "fixtures"

# Not reader.time_range(): it raises on a trace that holds no rows.
ALL_TIME = ("1970-01-01T00:00:00Z", "2200-01-01T00:00:00Z")

# No field can be named this: sanitizing turns `@` into `_`, so a printed `time_s` stays a field.
TIME = "@time_s"
SSH = "/usr/bin/ssh"


@dataclass
class Recording:
    """A trace source in its own namespace, written to `path`."""

    source: zelos_sdk.TraceSource
    path: Path
    writer: zelos_sdk.TraceWriter

    def events(self) -> dict[str, list[dict[str, Any]]]:
        """Close the recording and read it back with read_events()."""
        self.source.flush()
        # A .trz reads back empty until its writer has closed it.
        self.writer.close()
        return read_events(self.path)


def read_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Event name (`demo/log`) to its rows, each `{TIME: float, field: value, ...}`."""
    events: dict[str, list[dict[str, Any]]] = {}
    with zelos_sdk.TraceReader(str(path)) as reader:
        segments = [segment.id for segment in reader.list_data_segments()]
        for source in reader.list_fields():
            for event in source.events:
                fields = [field.path for field in event.fields]
                data = reader.query(segments, fields, *ALL_TIME).to_arrow()
                # Fields are named `<source>/<event>.<field>`, the time `time_s` beside `time_ns`;
                # an event with no rows returns b"".
                prefix = f"{source.name}/{event.name}."
                rows = pa.ipc.open_stream(data).read_all().to_pylist() if data else []
                events[event.name] = [
                    {
                        key.removeprefix(prefix) if key.startswith(prefix) else TIME: value
                        for key, value in row.items()
                        if key != "time_ns"
                    }
                    for row in rows
                ]
    return events


def recording(path: Path) -> Recording:
    """A recording at `path` that never touches the global source or an agent."""
    namespace = zelos_sdk.TraceNamespace(f"test-{uuid.uuid4().hex}")
    source = zelos_sdk.TraceSource(ACTION_PREFIX, namespace=namespace)
    writer = zelos_sdk.TraceWriter(str(path), namespace=namespace)
    writer.open()
    return Recording(source, path, writer)


@pytest.fixture
def trace_file(tmp_path: Path) -> Iterator[Recording]:
    trace = recording(tmp_path / "trace.trz")
    yield trace
    trace.writer.close()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with an empty ~/.ssh, so no real key or config is read."""
    directory = tmp_path / "home"
    (directory / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(directory))
    monkeypatch.setenv("USERPROFILE", str(directory))
    return directory


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> str:
    """A computer with ssh: nothing starts it, scripted processes stand in."""
    monkeypatch.setattr(remote, "openssh", lambda tool: SSH)
    return SSH


class _Stdin:
    """A process's stdin; closing it ends a wrapped remote command, as it does on a board."""

    def __init__(self, on_close: Callable[[], None]) -> None:
        self.closed = False
        self._on_close = on_close

    def close(self) -> None:
        self.closed = True
        self._on_close()

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass


class FakeProcess:
    """Stands in for `subprocess.Popen[bytes]`.

    `stdout` is delivered through a real pipe, so `os.read(process.stdout.fileno())` works. The
    pipe's writing end closes once everything is written, or, with `hold_open`, when stdin closes
    or `end()` is called: a stream that stays quiet until it is stopped, and that `send()` feeds
    one chunk at a time. `wait()` never times out, so `remote.terminate` never signals a process
    group on behalf of a fake pid.
    """

    # Never a live pid: nothing signals it, and a mistaken killpg would find no group.
    pid = 2**22 + 12345

    def __init__(
        self,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
        returncode: int = 0,
        *,
        hold_open: bool = False,
        hangs: bool = False,
    ) -> None:
        self._stdout = stdout.encode() if isinstance(stdout, str) else stdout
        self._stderr = stderr.encode() if isinstance(stderr, str) else stderr
        self._exit = returncode
        self._hangs = hangs
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self.returncode: int | None = None if hold_open else returncode
        self.stdin = _Stdin(self.end)
        self.communicated = False
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stderr = io.BytesIO(self._stderr)
        if self._stdout:
            self._queue.put(self._stdout)
        if not hold_open:
            self._queue.put(None)
        # A thread, so that more than a pipe's buffer can be scripted. It alone writes and closes.
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self._feeder.start()

    def _feed(self) -> None:
        try:
            while (data := self._queue.get()) is not None:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._write_fd, view[:32768]) :]
        except OSError:
            pass
        finally:
            os.close(self._write_fd)

    def send(self, data: bytes | str) -> None:
        """Print `data` now; the stream stays open."""
        self._queue.put(data.encode() if isinstance(data, str) else data)

    def end(self) -> None:
        """The stream's EOF, once what was sent is written."""
        if self.returncode is None:
            self.returncode = self._exit
            self._queue.put(None)

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        if self._hangs:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        self.communicated = True
        self.end()
        return self._stdout, self._stderr

    def wait(self, timeout: float | None = None) -> int:
        self.end()
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.end()

    def terminate(self) -> None:
        self.end()

    def close(self) -> None:
        """Release both ends of the pipe."""
        self.end()
        self._queue.put(None)
        self._feeder.join(5)
        self.stdout.close()


_WRAP_HEAD = "trap ended USR1; { "
_WRAP_TAIL = "; s=$?; kill -USR1 $$ 2>/dev/null; exit $s; } & while read -r _; do :; done"
_MERGED_HEAD, _MERGED_TAIL = "( eval ", " ) 2>&1"


def unwrap(text: str) -> str:
    """The command inside what `remote.wrap` or `remote.merged` made, or `text` when neither."""
    if text.startswith("c='"):
        session = shlex.split(text)[0].removeprefix("c=").removesuffix(";")
        text = session.split(_WRAP_HEAD, 1)[1].removesuffix(_WRAP_TAIL)
    if text.startswith(_MERGED_HEAD) and text.endswith(_MERGED_TAIL):
        (command,) = shlex.split(text.removeprefix(_MERGED_HEAD).removesuffix(_MERGED_TAIL))
        return command
    return text


class Processes:
    """The worker's `run=` hook: hands out one scripted process per call, in order.

    The first call over ssh is the probe. A call past the script raises
    OSError, which a worker reports as a failure to start, and is recorded in `unscripted`.
    """

    def __init__(self, *scripted: FakeProcess) -> None:
        self._queue = list(scripted)
        self.started: list[FakeProcess] = []
        self.argvs: list[list[str] | str] = []
        self.stderrs: list[int] = []
        self.unscripted = 0

    def __call__(self, argv: list[str] | str, stdin: int, stderr: int) -> subprocess.Popen[bytes]:
        self.argvs.append(argv)
        self.stderrs.append(stderr)
        if not self._queue:
            self.unscripted += 1
            raise OSError("no more scripted processes")
        process = self._queue.pop(0)
        self.started.append(process)
        return process  # type: ignore[return-value]

    def command(self, index: int) -> str:
        """What ran in call `index`: the command on the board or the shell's `-c` text, without
        the wrapper that ends it with the ssh session."""
        argv = self.argvs[index]
        return unwrap(argv if isinstance(argv, str) else argv[-1])

    def close(self) -> None:
        for process in self.started + self._queue:
            process.close()


@pytest.fixture
def processes() -> Iterator[Callable[..., Processes]]:
    """Make a `Processes` from scripted processes; the pipes close when the test ends."""
    made: list[Processes] = []

    def make(*scripted: FakeProcess) -> Processes:
        factory = Processes(*scripted)
        made.append(factory)
        return factory

    yield make
    for factory in made:
        factory.close()


class FakeClock:
    """A clock in nanoseconds that moves only when told to, or `step` per reading."""

    def __init__(self, start_ns: int = 1_700_000_000_000_000_000, step_ns: int = 0) -> None:
        self.ns = start_ns
        self.step_ns = step_ns

    def __call__(self) -> int:
        now = self.ns
        self.ns += self.step_ns
        return now

    def advance(self, seconds: float) -> None:
        self.ns += int(seconds * 1e9)


def run(name: str, **params: Any) -> Any:
    """What the action returns when the agent runs it with these parameters."""
    return getattr(actions, name)._action.execute(**params).value


def wait_until(condition: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)
