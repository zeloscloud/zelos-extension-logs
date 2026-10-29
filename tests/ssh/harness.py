"""Docker boards, SSH keys and a running agent, for tests that read logs over a real ssh."""

import contextlib
import re
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.conftest import Recording
from zelos_extension_logs import actions, app, config

PREFIX = "logs-it-"
LABEL = "logs-it"
BOARDS_DIR = Path(__file__).parent / "boards"
LOOPBACK = "127.0.0.1"
# The program name every test writes under.
PROGRAM = "acme-app"
# The longest a test waits for a line, a connection or a process; a poll ends as soon as it holds.
WAIT_S = 20.0
RECONNECT_WAIT_S = 40.0


@dataclass(frozen=True)
class Spec:
    dockerfile: str
    build_args: dict[str, str]
    systemd: bool


SPECS = {
    "systemd-openssh": Spec("systemd.Dockerfile", {"BASE": "ubuntu:24.04"}, True),
    "systemd-old": Spec("systemd.Dockerfile", {"BASE": "debian:11", "ARCHIVE": "1"}, True),
    "busybox-dropbear": Spec("busybox-dropbear.Dockerfile", {}, False),
}


def docker(
    *args: str,
    stdin: str | None = None,
    timeout: float = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def docker_available() -> bool:
    try:
        return docker("info", check=False, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def remove_leftovers(*, images: bool) -> None:
    """Remove every container this suite made, and with `images` the images too."""
    found = docker("ps", "-aq", "--filter", f"label={LABEL}", check=False).stdout.split()
    if found:
        docker("rm", "-f", "-v", *found, check=False)
    if images:
        listed = docker("images", "--format", "{{.Repository}}", check=False).stdout.split()
        names = sorted({name for name in listed if name.startswith(PREFIX)})
        if names:
            docker("rmi", "-f", *names, check=False)


def make_key(path: Path) -> Path:
    """An ed25519 key pair at `path` and `path`.pub."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "logs-it", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def free_port() -> int:
    """A port nothing listens on, as far as one can tell."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def closing_port() -> Iterator[int]:
    """A port whose server ends each connection before it greets ssh."""
    with socket.socket() as server:
        server.bind((LOOPBACK, 0))
        server.listen()

        def close_each() -> None:
            with contextlib.suppress(OSError):
                while True:
                    connection, _ = server.accept()
                    # An orderly close: closing with ssh's greeting unread would reset it instead.
                    with connection:
                        connection.shutdown(socket.SHUT_WR)
                        while connection.recv(4096):
                            pass

        threading.Thread(target=close_each, daemon=True).start()
        yield server.getsockname()[1]


@dataclass
class Board:
    """A running container with an SSH server and a system log."""

    name: str
    container: str
    port: int
    key: Path
    systemd: bool

    def sh(self, script: str, user: str = "root", timeout: float = 60) -> str:
        """Run a shell script inside the container, not over ssh."""
        done = docker("exec", "-u", user, self.container, "sh", "-c", script, timeout=timeout)
        return done.stdout

    def ssh(self, command: str, user: str = "root", key: Path | None = None) -> tuple[int, str]:
        done = subprocess.run(
            [
                "ssh",
                *ssh_flags(self.port, key or self.key),
                "-o",
                "ConnectTimeout=5",
                f"{user}@{LOOPBACK}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return done.returncode, done.stdout + done.stderr

    def source(
        self,
        name: str = "bench-1",
        user: str = "root",
        *,
        top: dict[str, Any] | None = None,
        **advanced: Any,
    ) -> dict[str, Any]:
        """A source's config that reaches this board; `top` adds fields beside `host`."""
        return ssh_source(self.port, self.key, name=name, user=user, top=top, **advanced)

    def log(self, tag: str, text: str, *, priority: str | None = None) -> None:
        """One line through `logger`, which BusyBox and util-linux both have."""
        flag = f"-p {priority} " if priority else ""
        self.sh(f"logger {flag}-t {shlex.quote(tag)} {shlex.quote(text)}")

    def log_numbers(
        self, numbers: Iterable[int], token: str, *, tag: str = PROGRAM, via: str = "logger"
    ) -> None:
        """`n=<i> <token>` per number: `logger` writes at notice, `systemd-cat` at warning."""
        listed = " ".join(str(n) for n in numbers)
        line = f'"n=$i {token}"'
        if via == "logger":
            write = f"logger -t {tag} {line}"
        else:
            write = f"echo {line} | systemd-cat -t {tag} -p warning"
        self.sh(f"for i in {listed}; do {write}; done")

    def cut(self) -> None:
        """End every ssh session and refuse new ones: a board that drops off the network.

        The bracket keeps pkill from matching the shell that runs it.
        """
        self.sh(
            "systemctl stop ssh.socket 2>/dev/null; systemctl stop ssh; "
            "pkill -f 'sshd:.*@[n]otty'; true"
        )

    def restore(self) -> None:
        self.sh("systemctl start ssh.socket 2>/dev/null; systemctl start ssh")

    def readers(self, marker: str | None = None) -> list[str]:
        """The command lines of any journalctl, tail or logread on the board, or with `marker`."""
        listing = self.sh("ps -eo args 2>/dev/null || ps")
        found = []
        for line in listing.splitlines():
            words = line.split()
            if (words and Path(words[0]).name in {"journalctl", "tail", "logread"}) or (
                marker and marker in line
            ):
                found.append(line.strip())
        return found


def ssh_flags(port: int, key: Path, *, known_hosts: Path | None = None) -> list[str]:
    return [
        "-F",
        "/dev/null",
        "-i",
        str(key),
        "-p",
        str(port),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={known_hosts or '/dev/null'}",
        "-o",
        "LogLevel=ERROR",
    ]


def ssh_source(
    port: int,
    key: Path,
    *,
    name: str = "bench-1",
    user: str = "root",
    ssh_options: str = "",
    top: dict[str, Any] | None = None,
    **advanced: Any,
) -> dict[str, Any]:
    """A source over ssh to the loopback port, with its key named and no ssh config read."""
    return {
        "name": name,
        "connection": "ssh",
        "host": f"{user}@{LOOPBACK}",
        **(top or {}),
        "advanced": {
            "ssh_port": port,
            "key_path": str(key),
            "host_key_policy": "auto",
            "ssh_options": f"-F /dev/null {ssh_options}".strip(),
            **advanced,
        },
    }


def start_board(name: str, key: Path) -> Board:
    """Build the board's image, run it, install the key, and wait until ssh answers."""
    spec = SPECS[name]
    image = PREFIX + name
    build = ["build", "-q", "-t", image, "--label", LABEL, "-f", str(BOARDS_DIR / spec.dockerfile)]
    for arg, value in spec.build_args.items():
        build += ["--build-arg", f"{arg}={value}"]
    docker(*build, str(BOARDS_DIR), timeout=900)
    container = image
    run = ["run", "-d", "--name", container, "--label", LABEL, "-p", f"{LOOPBACK}::22"]
    if spec.systemd:
        run += [
            "--privileged",
            "--cgroupns=host",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
        ]
    docker(*run, image, *(["/sbin/init"] if spec.systemd else []))
    mapped = docker("port", container, "22/tcp").stdout.split()[0]
    board = Board(name, container, int(mapped.rpartition(":")[2]), key, spec.systemd)
    public = key.with_suffix(".pub").read_text()
    docker(
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        "mkdir -p /root/.ssh && cat > /root/.ssh/authorized_keys "
        "&& chmod 600 /root/.ssh/authorized_keys",
        stdin=public,
    )
    if spec.systemd:
        docker(
            "exec",
            "-i",
            container,
            "sh",
            "-c",
            "cat > /home/tester/.ssh/authorized_keys && chown -R tester:tester /home/tester/.ssh "
            "&& chmod 700 /home/tester/.ssh && chmod 600 /home/tester/.ssh/authorized_keys",
            stdin=public,
        )
    deadline = time.monotonic() + 90
    while True:
        code, out = board.ssh("true")
        if code == 0:
            return board
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name} did not accept ssh: {out}")
        time.sleep(0.5)


class Boards:
    """Boards started on first use, so a run of one test starts one board."""

    def __init__(self, key: Path) -> None:
        self._key = key
        self._started: dict[str, Board] = {}

    def get(self, name: str) -> Board:
        if name not in self._started:
            self._started[name] = start_board(name, self._key)
        return self._started[name]


class Run:
    """`app.run` in a thread against its own trace recording, until `stop`."""

    def __init__(self, sources: list[dict[str, Any]], recording: Recording, data_dir: Path) -> None:
        self.names = [s["name"] for s in sources]
        self.settings = config.parse({"sources": sources})
        self.data_dir = data_dir
        self.recording = recording
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._main, name="logs-it-run", daemon=True)
        self._events: dict[str, list[dict[str, Any]]] | None = None

    def _main(self) -> None:
        try:
            app.run(
                source=self.recording.source,
                settings=self.settings,
                data_dir=self.data_dir,
                stop=self._stop,
            )
        except BaseException as e:
            self._error = e

    def start(self) -> "Run":
        # An earlier run's workers stay listed after it stops; these names must be this run's.
        actions.workers.clear()
        self._thread.start()
        self.wait("the workers to start", lambda: all(n in actions.workers for n in self.names))
        return self

    def worker(self, name: str | None = None) -> Any:
        return actions.workers[name or self.names[0]]

    def wait(self, what: str, condition: Callable[[], object], timeout: float = WAIT_S) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if self._error is not None:
                raise AssertionError(f"app.run failed while waiting for {what}") from self._error
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
            time.sleep(0.1)

    def seen(self, texts: Iterable[str], name: str | None = None) -> bool:
        """True when every text is a whole line the worker has recorded lately."""
        messages = set(self.worker(name).sample())
        return all(text in messages for text in texts)

    def wait_lines(
        self, texts: Iterable[str], name: str | None = None, timeout: float = WAIT_S
    ) -> None:
        wanted = list(texts)
        self.wait(f"lines {wanted[:2]}", lambda: self.seen(wanted, name), timeout)

    def wait_state(self, state: str, name: str | None = None, timeout: float = WAIT_S) -> None:
        self.wait(f"state {state}", lambda: self.worker(name).health.state == state, timeout)

    def live(self, write: Callable[[str], object], token: str, timeout: float = WAIT_S) -> None:
        """Write a marker line until it arrives: from then on the stream sees every new line."""
        deadline = time.monotonic() + timeout
        count = 0
        while time.monotonic() < deadline:
            count += 1
            write(f"live {token} {count}")
            try:
                self.wait_lines([f"live {token} {count}"], timeout=1.0)
                return
            except AssertionError:
                if self._error is not None:
                    raise
        raise AssertionError(f"the stream never showed a marker line within {timeout:.0f} s")

    def stop(self) -> dict[str, list[dict[str, Any]]]:
        """Stop the agent as a SIGTERM does, and read the trace back."""
        if self._events is not None:
            return self._events
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(30)
            assert not self._thread.is_alive(), "app.run did not return after stop"
        if self._error is not None:
            raise self._error
        self._events = self.recording.events()
        return self._events

    def rows(self, name: str | None = None) -> list[dict[str, Any]]:
        return self.stop().get(f"{name or self.names[0]}/log", [])

    def close(self) -> None:
        """Stop without reading, for teardown."""
        self._stop.set()
        self._thread.join(30)
        if self._events is None:
            self.recording.writer.close()


def numbers(rows: list[dict[str, Any]], token: str, program: str | None = PROGRAM) -> list[int]:
    """The `n=<i>` of every row that carries `token`, in the order written."""
    found = []
    for row in rows:
        match = re.fullmatch(rf"n=(\d+) {re.escape(token)}", row["message"])
        if match and (program is None or row["name"] == program):
            found.append(int(match.group(1)))
    return found


def notes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The extension's own notices, as the Logs source writes them."""
    return [row for row in rows if row["name"] == "logs"]
