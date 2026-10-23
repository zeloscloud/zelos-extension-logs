"""One source: probe, follow its log, reconnect when it drops."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Literal

import zelos_sdk
from zelos_log_lines.clock import US
from zelos_log_lines.lines import Line
from zelos_log_lines.records import Level, LogRecord
from zelos_log_lines.stream import LogStream, StreamState

from . import demo, journal, oslog, probe, remote
from .config import SourceConfig

logger = logging.getLogger(__name__)

# The name of the extension's own notes in a source's log, beside the programs' names.
NOTES = "logs"
# Reconnect delays: a drop retries soon, a lasting failure (a refused login) once a minute.
_FIRST_RETRY_S = 1.0
_LONGEST_RETRY_S = 60.0
# How often a source's position is saved.
_SAVE_S = 5.0
_SAMPLE_LINES = 200
# ssh's last lines, read as they come: an undrained pipe would stall a chatty ssh, as with -v.
_SAID_LINES = 50
_READ_BYTES = 65536
# How often a file on this computer is looked at for new lines.
_POLL_S = 0.25
# journalctl's words when the user may not read the system journal.
_NO_PERMISSION = "insufficient permissions"
# This computer's boot, for the kernel's lines a command run here prints.
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# What the probe says of a file that cannot be followed, as the local follower's error says it.
_UNREADABLE = {"directory": "Is a directory", "unreadable": "Permission denied"}
# What the system log of this computer is where it is no journal or syslog.
_OS_LOGS = {"darwin": "unified_log", "win32": "event_log"}

State = Literal["starting", "connecting", "streaming", "waiting", "ended", "stopped"]
# How a session ended: try again soon, try again once a minute, never (a command that finished), or
# at once to follow the journal whose history it read.
Outcome = Literal["retry", "lasting", "done", "follow"]


@dataclass(slots=True)
class Health:
    """What Get State reports for a source."""

    state: State = "starting"
    reading: str = ""
    message: str = ""
    lines: int = 0
    reconnects: int = 0
    gaps: int = 0
    boot_id: str = ""
    last_line_ns: int | None = None


class Cursors:
    """Each source's position, in the extension's data directory, which updates keep: the
    journal's cursor, or the kernel's last line a text source read, and when it was saved."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, object] | None:
        with self._lock:
            saved = self._read().get(key)
        return saved if isinstance(saved, dict) else None

    def put(self, key: str, value: dict[str, object]) -> None:
        if self._path is None:
            return
        with self._lock:
            data = self._read()
            data[key] = value
            tmp = self._path.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._path)

    def _read(self) -> dict[str, object]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


Runner = Callable[[list[str] | str, int, int], subprocess.Popen[bytes]]


def _open_shared(path: Path) -> BinaryIO:
    """`path` for reading, opened so a logger can still rename or delete it."""
    if sys.platform != "win32" or os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    generic_read, open_existing, normal = 0x80000000, 3, 0x80
    share = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE
    handle = create_file(str(path), generic_read, share, None, open_existing, normal, None)
    if handle in (None, ctypes.c_void_p(-1).value):  # INVALID_HANDLE_VALUE
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")


def _oldest_kernel_line() -> str:
    """This computer's oldest kernel line, as dmesg prints it first; empty when dmesg cannot."""
    try:
        done = subprocess.run(["dmesg"], capture_output=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace").partition("\n")[0]


def _run(argv: list[str] | str, stdin: int, stderr: int) -> subprocess.Popen[bytes]:
    return remote.popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr)


class SourceWorker(threading.Thread):
    """Follows one source's log until stopped, reconnecting whenever it ends."""

    def __init__(
        self,
        cfg: SourceConfig,
        source: zelos_sdk.TraceSource,
        cursors: Cursors,
        access_key: Path | None,
        *,
        run: Runner | None = None,
        clock: Callable[[], int] = time.time_ns,
        retention: timedelta | None = probe.HISTORY,
    ) -> None:
        """`retention` is how long the agent keeps data, None for no limit: how far back a first
        start reads the journal, and how old a saved position may be."""
        super().__init__(name=f"logs-{cfg.name}", daemon=True)
        self.cfg = cfg
        self.health = Health()
        self._stream = LogStream(
            source,
            cfg.name,
            notes=NOTES,
            device_time=cfg.time_source == "device",
            values=cfg.values,
            kernel_values=cfg.kernel_values,
            on_line=self._on_line,
        )
        self._cursors = cursors
        self._access_key = access_key
        self._run = run or _run
        self._now = clock
        self._retention = retention
        self._stop_event = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._sample: deque[str] = deque(maxlen=_SAMPLE_LINES)
        self._lock = threading.Lock()
        self._cursor_key = f"{cfg.name}\0{cfg.connection}\0{cfg.host or ''}"
        self._run_cursor = ""
        # The boot of a text source's last start in this run, "" when it has none; None before.
        self._text_boot: str | None = None
        self._dropped_at: int | None = None
        self._user = "this user"
        self._refused = False
        # The last session read the journal's history, and this one follows it.
        self._read_history = False

    def stop(self, *, kill: bool = False) -> None:
        """Stop following, and signal the process to stop, or with `kill` to die, without
        waiting."""
        self._stop_event.set()
        process = self._process
        if process is not None:
            remote.stop(process, kill=kill)

    def sample(self) -> list[str]:
        with self._lock:
            return list(self._sample)

    def stream_state(self) -> StreamState:
        return self._stream.state()

    def run(self) -> None:
        try:
            self._follow()
        finally:
            self._stream.close()

    def _follow(self) -> None:
        delay = _FIRST_RETRY_S
        while not self._stop_event.is_set():
            started = self._now()
            try:
                outcome = self._session()
            except Exception:
                # A bug must not end the source: it is logged and the source tries again.
                logger.exception("%s: unexpected error", self.cfg.name)
                outcome = "retry"
            if self._stop_event.is_set():
                break
            if outcome == "done":
                self.health.state = "ended"
                return
            if outcome == "follow":
                continue
            self.health.state = "waiting"
            self.health.reconnects += 1
            self._dropped_at = self._dropped_at or self._now()
            # A session that ends within the longest wait backs off, whatever it printed first.
            lasted = self._now() - started >= _LONGEST_RETRY_S * 1e9
            delay = _FIRST_RETRY_S if lasted else min(delay * 2, _LONGEST_RETRY_S)
            self._stop_event.wait(_LONGEST_RETRY_S if outcome == "lasting" else delay)
        self.health.state = "stopped"

    def _session(self) -> Outcome:
        """Probe, follow, and return how the session ended."""
        self._refused = False
        follows, self._read_history = self._read_history, False
        if self.cfg.connection == "demo":
            self._connected()
            self._follow_journal(demo.lines(self._stop_event, self._now), None)
            return "retry"
        self.health.state = "connecting"
        board: probe.Probe | None = None
        resumes_at: str | None = None
        history = False
        native = oslog.command(sys.platform) if self.cfg.connection == "local" else None
        if self.cfg.read == "system" and native is not None:
            command, kind = native, _OS_LOGS[sys.platform]
        elif self.cfg.read == "system":
            probed = self._probe()
            if isinstance(probed, remote.Failure):
                return self._failed(probed)
            board = probed
            self._user = board.user or self._user
            try:
                # A history that printed no entry and left no position is not read again.
                kept = "new" if follows else self.cfg.history
                log = probe.system_log(board, self._resume_cursor(), kept, self._retention)
            except probe.NoLog:
                return self._failed(
                    remote.Failure(
                        "No log found: no journalctl, no working logread, and no readable "
                        "/var/log/messages or /var/log/syslog.",
                        True,
                    )
                )
            command, kind, resumes_at, history = log.command, log.kind, log.resumes_at, log.history
            if board.partial_journal and kind == "journal" and not follows:
                self._note("warn", probe.partial(board.user))
            self._note_restart(board)
        elif self.cfg.read == "file" and self.cfg.connection == "local":
            assert self.cfg.path is not None
            return self._follow_local(Path(self.cfg.path).expanduser())
        else:
            if self.cfg.connection == "ssh":
                probed = self._probe()
                if isinstance(probed, remote.Failure):
                    return self._failed(probed)
                board = probed
                self._user = board.user or self._user
                self._note_restart(board)
            if self.cfg.read == "file":
                assert self.cfg.path is not None
                if board is not None and board.path in _UNREADABLE:
                    reason = _UNREADABLE[board.path]
                    return self._failed(
                        remote.Failure(f"Cannot read {self.cfg.path}: {reason}.", False)
                    )
                missing = board is not None and board.path == "missing"
                if missing:
                    self._fail(
                        f"No file {self.cfg.path} yet. Its lines are read once it appears.", False
                    )
                # Every line of a file the probe found missing is new, also one written before tail
                # starts.
                command, kind = probe.tail(self.cfg.path, whole=missing), "file"
            else:
                assert self.cfg.command is not None
                command, kind = self.cfg.command, "command"
        self.health.reading = kind
        if kind not in ("journal", *_OS_LOGS.values()):
            self._text_start(board)
        gap = self._note_gap(kind)
        process = self._start(command)
        if isinstance(process, remote.Failure):
            return self._failed(process)
        said: deque[bytes] = deque(maxlen=_SAID_LINES)
        drain = threading.Thread(target=said.extend, args=(process.stderr or (),), daemon=True)
        drain.start()
        # A quiet log prints nothing until its next line; the probe, or a local start, proved the
        # way there, unless the file is still to appear.
        if board is None or board.path != "missing":
            self._connected()
        if kind == "journal":
            streamed = self._follow_journal(self._chunks(process), board, resumes_at)
        elif kind in _OS_LOGS.values():
            streamed = self._follow_entries(self._chunks(process))
        else:
            streamed = self._follow_text(self._chunks(process), gap)
        remote.terminate(process, timeout=0.5)
        self._process = None
        drain.join(1.0)
        err = b"".join(said).decode("utf-8", "replace")
        if history and process.returncode == 0 and not self._stop_event.is_set():
            if not streamed and board is not None and board.journal_cursor:
                # Nothing since the history's start: follow from the boot's latest entry.
                self._save(board.journal_cursor)
            self._read_history = True
            return "follow"
        return self._ended(process.returncode, err, streamed, kind)

    def _follow_local(self, path: Path) -> Outcome:
        self.health.reading = "file"
        self._text_start(None)
        self._note_gap("file")
        try:
            self._follow_text(self._appended(path))
            return "retry"
        except OSError as e:
            return self._failed(remote.Failure(f"Cannot read {path}: {e.strerror or e}.", False))

    def _appended(self, path: Path) -> Iterator[tuple[bytes, int]]:
        """What is added to the file at `path`, as `tail -F` follows it: from its end, or from its
        start when it appears later, again from the start when it is truncated or replaced. A
        replaced or deleted file is read to its end first, so a logger's last lines before it
        rotates are kept."""
        held: BinaryIO | None = None
        identity: tuple[int, int] | None = None
        from_end = True
        try:
            while not self._stop_event.is_set():
                try:
                    named = path.stat()
                except FileNotFoundError:
                    named = None
                    if identity is None:
                        self._fail(
                            f"No file {path} yet. Its lines are read once it appears.", False
                        )
                chunk = held.read(_READ_BYTES) if held is not None else b""
                if not chunk and named is not None:
                    if (named.st_dev, named.st_ino) != identity:
                        if held is not None:
                            held.close()
                            held = None
                        try:
                            held = _open_shared(path)
                        except FileNotFoundError:
                            continue
                        stat = os.fstat(held.fileno())
                        if identity is None:
                            self._connected()
                        identity = (stat.st_dev, stat.st_ino)
                        if from_end:
                            held.seek(0, os.SEEK_END)
                    elif held is not None and named.st_size < held.tell():
                        held.seek(0)
                    if held is not None:
                        chunk = held.read(_READ_BYTES)
                from_end = False
                if chunk:
                    yield chunk, self._now()
                else:
                    self._stop_event.wait(_POLL_S)
        finally:
            if held is not None:
                held.close()

    def _probe(self) -> probe.Probe | remote.Failure:
        process = self._start(
            probe.script(self.cfg.path if self.cfg.read == "file" else None), wrapped=False
        )
        if isinstance(process, remote.Failure):
            return process
        try:
            out, err = process.communicate(timeout=probe.TIMEOUT_S)
        except subprocess.TimeoutExpired:
            remote.terminate(process)
            return remote.Failure(f"{self._where()} did not answer the probe in time.", False)
        finally:
            self._process = None
        try:
            return probe.parse(out.decode("utf-8", "replace"), self._now())
        except probe.Truncated:
            return self._explain((err or b"").decode("utf-8", "replace"))

    def _start(
        self, command: str, *, wrapped: bool = True
    ) -> subprocess.Popen[bytes] | remote.Failure:
        argv: list[str] | str
        # ssh's own errors stay apart, to explain a failed connection; a local command's are output.
        stderr = subprocess.STDOUT
        if self.cfg.connection == "ssh":
            remote_command = remote.wrap(command) if wrapped else command
            try:
                argv = remote.ssh_argv(self.cfg, remote_command, self._access_key)
            except remote.SshMissing as e:
                return remote.Failure(str(e), True)
            stderr = subprocess.PIPE
        else:
            argv = remote.local_argv(command)
        try:
            # A probe reads nothing; a stream's stdin stays open, as a wrapped command needs.
            stdin = subprocess.PIPE if wrapped else subprocess.DEVNULL
            self._process = self._run(argv, stdin, stderr)
        except OSError as e:
            program = argv if isinstance(argv, str) else argv[0]
            return remote.Failure(f"Cannot start {program}: {e}.", True)
        if self.cfg.connection == "ssh" and wrapped and self._process.stdin is not None:
            remote.wake(self._process, self._process.stdin)
        return self._process

    def _chunks(self, process: subprocess.Popen[bytes]) -> Iterator[tuple[bytes, int]]:
        stdout = process.stdout
        assert stdout is not None
        while not self._stop_event.is_set():
            chunk = os.read(stdout.fileno(), _READ_BYTES)
            if not chunk:
                return
            yield chunk, self._now()

    def _ended(self, returncode: int, err: str, streamed: bool, kind: str) -> Outcome:
        """Report why the process ended, with what it printed to stderr, and whether to try
        again."""
        if self._stop_event.is_set():
            return "retry"
        if self._refused:
            return "lasting"
        if self.cfg.connection == "ssh" and returncode == 255:
            failure = self._explain(err)
            # A login that worked before is not refused for good.
            return self._failed(replace(failure, lasting=failure.lasting and not streamed))
        if kind == "command" and returncode == 0:
            # Run again, it would print the same lines again.
            self._fail("The command finished. It runs again when the extension restarts.", False)
            return "done"
        return self._failed(
            remote.Failure(f"{self._where()}: the log ended (exit {returncode}).", False)
        )

    def _follow_journal(
        self,
        chunks: Iterator[tuple[bytes, int]],
        board: probe.Probe | None,
        resumes_at: str | None = None,
    ) -> bool:
        placement = journal.Placement(board)
        pending = b""
        streamed = False
        last_save = 0
        cursor = ""
        for chunk, host_ns in chunks:
            pending += chunk
            *complete, pending = pending.split(b"\n")
            for raw in complete:
                if not raw.strip():
                    continue
                entry = journal.read(raw)
                if entry is None:
                    self._journal_text(raw.decode("utf-8", "replace"), host_ns)
                    continue
                skip, resumes_at = resumes_at, None
                if entry.cursor == skip:
                    continue
                streamed = True
                cursor = entry.cursor
                self.health.boot_id = entry.boot_id
                time_ns = (
                    None if self.cfg.time_source == "arrival" else placement.place(entry, host_ns)
                )
                # The stream reads the entry's message, level, program and source line.
                self._stream.write(raw.decode("utf-8", "replace"), host_ns, time_ns=time_ns)
                if time_ns is not None:
                    # The entry's own time: a resumed journal places its backlog in the past.
                    self.health.last_line_ns = time_ns
            if cursor and host_ns - last_save >= _SAVE_S * 1e9:
                last_save = host_ns
                self._save(cursor)
        if cursor:
            self._save(cursor)
        return streamed

    def _follow_entries(self, chunks: Iterator[tuple[bytes, int]]) -> bool:
        """This computer's log on macOS or Windows, one JSON entry a line."""
        pending = b""
        streamed = False
        # The last entry's own time, and where it was placed.
        last: tuple[int, int] | None = None
        for chunk, host_ns in chunks:
            pending += chunk
            *complete, pending = pending.split(b"\n")
            for raw in complete:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except ValueError:
                    record = None
                if not isinstance(record, dict):
                    # The log command's own words, such as why it cannot read.
                    self._note("warn", text, host_ns)
                    continue
                entry = oslog.read(sys.platform, record)
                if entry is None:
                    continue
                streamed = True
                time_ns = None
                if self.cfg.time_source == "device":
                    # Entries stamped alike keep their order a microsecond apart.
                    tied = last is not None and entry.time_ns == last[0]
                    time_ns = last[1] + US if last is not None and tied else entry.time_ns
                    last = (entry.time_ns, time_ns)
                self._stream.write(
                    entry.message,
                    host_ns,
                    default_level=entry.level,
                    name=entry.program,
                    time_ns=time_ns,
                )
        return streamed

    def _journal_text(self, text: str, host_ns: int) -> None:
        # journalctl's own words, such as a refused read, arrive between the entries.
        if _NO_PERMISSION in text:
            self._refused = True
            self._fail(probe.partial(self._user), True)
        self._note("warn", text.strip(), host_ns)

    def _follow_text(
        self, chunks: Iterator[tuple[bytes, int]], gap: tuple[int, int] | None = None
    ) -> bool:
        """`gap` holds when a command was last read and when it ran again: its first lines tell
        whether it prints again what it printed meanwhile, as `dmesg -w` prints the kernel's."""
        lines, repeated = self.health.lines, self._stream.repeated
        last_save = 0
        for chunk, host_ns in chunks:
            self._stream.feed(chunk, host_ns)
            if gap is not None and (self.health.lines > lines or self._stream.repeated > repeated):
                if self._stream.repeated == repeated:
                    self._note_missed(*gap)
                gap = None
            if host_ns - last_save >= _SAVE_S * 1e9:
                last_save = host_ns
                self._save_text()
        self._stream.end()
        self._save_text()
        if gap is not None:
            self._dropped_at = gap[0]
        return self.health.lines > lines

    def _on_line(self, line: Line, record: LogRecord) -> None:
        # A journal is streaming once its probe passes; other logs once a line arrives.
        if self.health.state == "connecting":
            self._connected()
        self.health.lines += 1
        self.health.last_line_ns = line.host_ns
        with self._lock:
            self._sample.append(record.message)

    def _note(self, level: Level, message: str, time_ns: int | None = None) -> None:
        if not message:
            return
        self._stream.note(level, f"[{NOTES}] {message}", time_ns or self._now())

    def _connected(self) -> None:
        self.health.state = "streaming"
        self.health.message = ""

    def _fail(self, message: str, lasting: bool) -> None:
        if message != self.health.message:
            logger.warning("%s: %s", self.cfg.name, message)
            self._note("error" if lasting else "warn", message)
        self.health.message = message

    def _failed(self, failure: remote.Failure) -> Outcome:
        # A stop ends what was under way; what that says is no failure of the source.
        if not self._stop_event.is_set():
            self._fail(failure.message, failure.lasting)
        return "lasting" if failure.lasting else "retry"

    def _explain(self, stderr: str) -> remote.Failure:
        return remote.explain(stderr, self._where())

    def _where(self) -> str:
        return self.cfg.host or "this computer"

    def _resume_cursor(self) -> str | None:
        # Within a run the journal goes on where it stopped; History decides where a run starts.
        if self._run_cursor:
            return self._run_cursor
        saved = self._cursors.get(self._cursor_key)
        if not saved or self.cfg.history == "new":
            return None
        saved_at = saved.get("saved_ns")
        # Older than the agent keeps data: start from what it keeps instead.
        if not isinstance(saved_at, int) or (
            self._retention is not None
            and self._now() - saved_at > self._retention.total_seconds() * 1e9
        ):
            return None
        cursor = saved.get("cursor")
        return cursor if isinstance(cursor, str) and cursor else None

    def _save(self, cursor: str) -> None:
        self._run_cursor = cursor
        try:
            self._cursors.put(
                self._cursor_key,
                {"cursor": cursor, "boot_id": self.health.boot_id, "saved_ns": self._now()},
            )
        except OSError as e:
            logger.warning("%s: cannot save the journal position: %s", self.cfg.name, e)

    def _saved(self) -> dict[str, object] | None:
        """The source's saved position, unless it is older than the agent keeps data."""
        saved = self._cursors.get(self._cursor_key)
        saved_at = saved.get("saved_ns") if saved else None
        if not isinstance(saved_at, int) or (
            self._retention is not None
            and self._now() - saved_at > self._retention.total_seconds() * 1e9
        ):
            return None
        return saved

    def _text_start(self, board: probe.Probe | None) -> None:
        """Before a text source starts: map the kernel's lines from its uptime, leave out what it
        printed before, and, on a run's first start, note the time since the last read."""
        first = self._text_boot is None
        saved = self._saved() if first and self.cfg.history == "kept" else None
        if saved is not None and self._dropped_at is None:
            saved_at = saved["saved_ns"]
            assert isinstance(saved_at, int)
            self._dropped_at = saved_at
        reading = self._kernel(board)
        if reading is None:
            self._text_boot = ""
            return
        host_ns, device_ns, boot, oldest = reading
        self._stream.seed(host_ns, device_ns, oldest)
        position = self._stream.position if boot == self._text_boot else None
        if first and saved is not None and saved.get("boot_id") == boot:
            kept = saved.get("position")
            if isinstance(kept, list) and len(kept) == 2:
                at, text = kept
                if isinstance(at, int) and isinstance(text, str):
                    position = (at, text)
        if position is not None:
            self._stream.resume_after(*position)
        elif first and self.cfg.history == "new":
            self._stream.resume_after(device_ns)
        elif first and self._retention is not None:
            self._stream.resume_after(device_ns - int(self._retention.total_seconds() * 1e9))
        self._text_boot = boot

    def _kernel(self, board: probe.Probe | None) -> tuple[int, int, str, str] | None:
        """When, by the host clock, the kernel's uptime was what, its boot, and its buffer's
        oldest line: the probe's, or this computer's on Linux."""
        if board is not None:
            uptime = board.kernel_ns
            if uptime is None or not board.boot_id:
                return None
            return board.host_ns, uptime, board.boot_id, board.kernel_oldest
        if self.cfg.connection != "local" or sys.platform != "linux":
            return None
        try:
            boot = _BOOT_ID.read_text(encoding="ascii").strip().replace("-", "")
        except OSError:
            return None
        return self._now(), time.monotonic_ns(), boot, _oldest_kernel_line()

    def _save_text(self) -> None:
        position = self._stream.position
        try:
            self._cursors.put(
                self._cursor_key,
                {
                    "boot_id": self._text_boot or "",
                    "position": list(position) if position else None,
                    "saved_ns": self._now(),
                },
            )
        except OSError as e:
            logger.warning("%s: cannot save the position: %s", self.cfg.name, e)

    def _note_restart(self, board: probe.Probe) -> None:
        saved = self._cursors.get(self._cursor_key)
        if saved and board.boot_id and saved.get("boot_id") not in ("", None, board.boot_id):
            self._note("warn", "device restarted")

    def _note_gap(self, kind: str) -> tuple[int, int] | None:
        """Note that a gap lost what printed meanwhile; for a command, return when the gap began
        and ended, for its first lines to tell. The journal resumes at its cursor."""
        dropped, self._dropped_at = self._dropped_at, None
        if dropped is None or kind == "journal":
            return None
        if kind == "command":
            return dropped, self._now()
        self._note_missed(dropped, self._now())
        return None

    def _note_missed(self, dropped: int, at: int) -> None:
        self.health.gaps += 1
        seconds = (at - dropped) / 1e9
        self._note(
            "warn", f"Reconnected after {seconds:.0f} s; lines printed meanwhile are missing.", at
        )


def state_of(worker: SourceWorker) -> dict[str, object]:
    """Get State's answer for one source."""
    cfg = worker.cfg
    stream = worker.stream_state()
    return {
        "name": cfg.name,
        "connection": cfg.connection,
        "host": cfg.host,
        "read": cfg.read,
        **asdict(worker.health),
        "long_lines": stream.long_lines,
        "late_names": stream.late_names,
        "errors": stream.errors,
        "signals": [asdict(signal) for signal in stream.signals],
    }
