"""The ssh command line, the wrapper that ends a remote command with its session, and ssh's
failures in words."""

import contextlib
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SSH, unwrap, wait_until
from zelos_extension_logs import config, remote
from zelos_extension_logs.remote import Failure, SshMissing, explain, ssh_argv

NULL = os.devnull


def cfg(**advanced: Any) -> config.SourceConfig:
    item: dict[str, Any] = {"connection": "ssh", "host": "root@bench-1"}
    if advanced:
        item["advanced"] = advanced
    return config.source(item)


class TestSshArgv:
    def test_option_order_with_the_defaults(
        self, ssh: str, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert ssh_argv(cfg(), "journalctl -f", None) == [
            SSH,
            "-T",
            *("-o", "BatchMode=yes"),
            *("-o", "ConnectTimeout=10"),
            *("-o", "ServerAliveInterval=5"),
            *("-o", "ServerAliveCountMax=3"),
            *("-o", "StrictHostKeyChecking=no"),
            *("-o", f"UserKnownHostsFile={NULL}"),
            *("-o", f"GlobalKnownHostsFile={NULL}"),
            *("-o", "LogLevel=ERROR"),
            "--",
            "root@bench-1",
            "journalctl -f",
        ]

    def test_extra_options_come_first(self, ssh: str, home: Path) -> None:
        argv = ssh_argv(cfg(ssh_options="-J jump -o 'ServerAliveInterval 30'"), "ls", None)

        assert argv[:6] == [SSH, "-J", "jump", "-o", "ServerAliveInterval 30", "-T"]

    def test_strict_host_key_checking(self, ssh: str, home: Path) -> None:
        argv = ssh_argv(cfg(host_key_policy="strict"), "ls", None)

        assert argv[argv.index("-o", argv.index("ServerAliveCountMax=3")) + 1] == (
            "StrictHostKeyChecking=yes"
        )
        assert not any(word.startswith("UserKnownHostsFile") for word in argv)
        assert "StrictHostKeyChecking=no" not in argv
        assert "LogLevel=ERROR" not in argv

    def test_the_port(self, ssh: str, home: Path) -> None:
        argv = ssh_argv(cfg(ssh_port=2222), "ls", None)

        assert argv[argv.index("-p") : argv.index("-p") + 2] == ["-p", "2222"]
        assert argv.index("-p") < argv.index("--")

    def test_no_port_leaves_it_to_the_ssh_config(self, ssh: str, home: Path) -> None:
        assert "-p" not in ssh_argv(cfg(), "ls", None)

    def test_a_key_file_names_only_that_key(self, ssh: str, home: Path, tmp_path: Path) -> None:
        (home / ".ssh" / "id_ed25519").write_text("k", encoding="utf-8")
        access = tmp_path / "access"
        access.write_text("k", encoding="utf-8")

        argv = ssh_argv(cfg(key_path="/keys/bench"), "ls", access)

        assert argv.count("-i") == 1
        assert argv[argv.index("-i") : argv.index("-i") + 4] == [
            "-i",
            "/keys/bench",
            "-o",
            "IdentitiesOnly=yes",
        ]

    def test_the_access_key_and_the_default_keys_you_already_have(
        self, ssh: str, home: Path, tmp_path: Path
    ) -> None:
        access = tmp_path / "ssh" / "id_ed25519"
        access.parent.mkdir()
        access.write_text("k", encoding="utf-8")
        for name in ("id_rsa", "id_ed25519"):
            (home / ".ssh" / name).write_text("k", encoding="utf-8")

        argv = ssh_argv(cfg(), "ls", access)

        identities = [argv[i + 1] for i, word in enumerate(argv) if word == "-i"]
        assert identities == [
            str(access),
            str(home / ".ssh" / "id_ed25519"),
            str(home / ".ssh" / "id_rsa"),
        ]
        assert "IdentitiesOnly=yes" not in argv

    def test_an_access_key_that_does_not_exist_is_not_named(
        self, ssh: str, home: Path, tmp_path: Path
    ) -> None:
        (home / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")

        argv = ssh_argv(cfg(), "ls", tmp_path / "absent")

        assert "-i" not in argv

    def test_no_access_key_leaves_your_own_keys_to_ssh(self, ssh: str, home: Path) -> None:
        (home / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")

        assert "-i" not in ssh_argv(cfg(), "ls", None)

    def test_the_host_follows_a_double_dash(self, ssh: str, home: Path) -> None:
        argv = ssh_argv(cfg(ssh_port=22, key_path="/k"), "dmesg -w", None)

        assert argv[-3:] == ["--", "root@bench-1", "dmesg -w"]

    def test_windows_ignores_a_control_master_in_the_users_config(
        self, ssh: str, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        argv = ssh_argv(cfg(), "ls", None)

        start = argv.index("ControlMaster=no") - 1
        assert argv[start : start + 4] == ["-o", "ControlMaster=no", "-o", "ControlPath=none"]

    def test_windows_keeps_the_reason_a_connection_failed(
        self, ssh: str, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its ssh.exe prints a refused connection only at the default log level."""
        monkeypatch.setattr(sys, "platform", "win32")

        assert not any(option.startswith("LogLevel=") for option in ssh_argv(cfg(), "ls", None))

    def test_other_platforms_leave_it_alone(
        self, ssh: str, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        assert "ControlMaster=no" not in ssh_argv(cfg(), "ls", None)

    def test_no_ssh_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "openssh", lambda tool: None)

        with pytest.raises(SshMissing):
            ssh_argv(cfg(), "ls", None)


class TestOpenssh:
    def test_the_one_on_the_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")

        assert remote.openssh("ssh") == "/opt/bin/ssh"

    def test_none_when_there_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: None)

        assert remote.openssh("ssh") is None

    def test_windows_own_openssh_comes_before_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        system = tmp_path / "System32" / "OpenSSH"
        system.mkdir(parents=True)
        (system / "ssh.exe").write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
        monkeypatch.setattr(shutil, "which", lambda name: "C:/Git/usr/bin/ssh.exe")

        assert remote.openssh("ssh") == str(system / "ssh.exe")

    def test_windows_without_its_own_openssh_uses_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
        monkeypatch.setattr(shutil, "which", lambda name: "C:/Git/usr/bin/ssh.exe")

        assert remote.openssh("ssh") == "C:/Git/usr/bin/ssh.exe"


class TestLocalArgv:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
    def test_runs_in_the_shell(self) -> None:
        assert remote.local_argv("dmesg -w | cat") == ["/bin/sh", "-c", "dmesg -w | cat"]

    def test_windows_hands_the_text_to_its_own_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        assert remote.local_argv("dir") == "dir"


class TestExplain:
    HOST = "bench-1"

    def test_an_unknown_host_key(self) -> None:
        failure = explain(
            "No ED25519 host key is known for bench-1 and you have requested strict checking.\n"
            "Host key verification failed.",
            self.HOST,
        )

        assert failure == Failure(
            "The host key of bench-1 is not in known_hosts. Connect once with ssh to add it, or "
            "set Host key check to Trust any key.",
            True,
        )

    @pytest.mark.parametrize(
        ("host", "known", "remove"),
        [
            ("root@bench-1", "bench-1", "ssh-keygen -R bench-1"),
            ("root@bench-1", "[bench-1]:2222", "ssh-keygen -R '[bench-1]:2222'"),
        ],
    )
    def test_a_changed_host_key_names_the_entry_to_remove(
        self, host: str, known: str, remove: str
    ) -> None:
        failure = explain(
            "@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n@@@@@@@@\n"
            "Offending ED25519 key in /home/u/.ssh/known_hosts:1\n"
            f"Host key for {known} has changed and you have requested strict checking.\n"
            "Host key verification failed.",
            host,
        )

        assert failure == Failure(
            f"The host key of {host} changed. Run {remove}, or set Host key check to Trust any "
            "key.",
            True,
        )

    def test_a_changed_host_key_without_its_name_removes_the_host_without_its_user(self) -> None:
        failure = explain("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "root@bench-1")

        assert "Run ssh-keygen -R bench-1," in failure.message

    @pytest.mark.parametrize(
        "stderr",
        [
            "root@bench-1: Permission denied (publickey,password).",
            "Received disconnect from 10.0.0.2 port 22:2: Too many authentication failures",
            "PERMISSION DENIED (publickey).",
        ],
    )
    def test_refused_login(self, stderr: str) -> None:
        failure = explain(stderr, self.HOST)

        assert failure.lasting is True
        assert failure.message == (
            "bench-1 refused the login. Run Show Access Key and add its line to the board, or "
            "set Key file."
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "ssh: Could not resolve hostname bench-1: Name or service not known",
            "ssh: Could not resolve hostname bench-1: nodename nor servname provided, or not known",
            "ssh: connect to host bench-1: No such host is known.",
        ],
    )
    def test_unresolvable_host(self, stderr: str) -> None:
        failure = explain(stderr, self.HOST)

        assert failure.lasting is False
        assert failure.message == (
            "bench-1 cannot be resolved. Check the name, or use its IP address."
        )

    def test_connection_refused(self) -> None:
        failure = explain("ssh: connect to host bench-1 port 22: Connection refused", self.HOST)

        assert failure.lasting is False
        assert failure.message == (
            "bench-1 refused the connection. Check that an SSH server runs there."
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "ssh: connect to host bench-1 port 22: Operation timed out",
            "ssh: connect to host bench-1 port 22: Connection timed out",
            "ssh: connect to host bench-1 port 22: No route to host",
            "ssh: connect to host bench-1 port 22: Network is unreachable",
            "ssh: connect to host bench-1 port 22: Host is down",
        ],
    )
    def test_no_answer(self, stderr: str) -> None:
        failure = explain(stderr, self.HOST)

        assert failure == Failure(
            "bench-1 did not answer. Check that it is on and reachable.", False
        )

    def test_anything_else_quotes_ssh_last_line(self) -> None:
        failure = explain("debug1: something\n\nkex_exchange_identification: read: reset\n\n", "h")

        assert failure == Failure(
            "ssh to h failed: kex_exchange_identification: read: reset", False
        )

    def test_no_output_at_all(self) -> None:
        assert explain("", "h") == Failure("ssh to h failed.", False)
        assert explain("  \n\n", "h") == Failure("ssh to h failed.", False)

    def test_the_first_family_that_matches_wins(self) -> None:
        failure = explain("Permission denied\nConnection refused", "h")

        assert "refused the login" in failure.message


POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
SHELLS = [s for s in ("/bin/sh", "/bin/dash", shutil.which("bash")) if s and Path(s).exists()]
SHELL_IDS = [Path(s).name for s in SHELLS]


def alive(pid: int) -> bool:
    """True while `pid` runs; a process waiting to be reaped is gone."""
    done = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = done.stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone_within(seconds: float, pid: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


# The ssh daemon runs the command in its own session; `kill 0` in the wrapper must never reach
# pytest's group.
def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Whatever the test left running: the process led its own session, so pid is its group."""
    assert sys.platform != "win32"
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def read_until_eof(proc: subprocess.Popen[bytes], timeout: float = 3.0) -> bytes:
    """What the session printed, read with its stdin still open: closing stdin ends a session."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    data = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                return data
            data += chunk
    raise AssertionError(f"the session did not end in {timeout} s; it printed {data!r}")


def spawn(shell: str, command: str, cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [shell, "-c", remote.wrap(command)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )


def test_the_wrapper_holds_the_command_it_was_given() -> None:
    command = "journalctl -f -o json --all --cursor='s=1;i=2'"

    assert unwrap(remote.wrap(command)) == command
    assert unwrap("dmesg -w") == "dmesg -w"


@POSIX
@pytest.mark.parametrize("shell", SHELLS, ids=SHELL_IDS)
class TestWrap:
    """The wrapped command is text for the board's `sh -c`: only running it checks it."""

    @pytest.fixture
    def pid_file(self, tmp_path: Path) -> Path:
        return tmp_path / "command.pid"

    def long_running(self, pid_file: Path) -> str:
        # The shell records its own pid, then becomes the command: the pid is the command's.
        return f"sh -c 'echo $$ > {shlex.quote(str(pid_file))}; exec sleep 600'"

    def started(self, pid_file: Path) -> int:
        wait_until(lambda: pid_file.exists() and pid_file.read_text().strip())
        return int(pid_file.read_text().strip())

    def test_closing_stdin_ends_the_command_within_two_seconds(
        self, shell: str, tmp_path: Path, pid_file: Path
    ) -> None:
        proc = spawn(shell, self.long_running(pid_file), tmp_path)
        try:
            pid = self.started(pid_file)
            assert alive(pid)
            assert proc.poll() is None

            assert proc.stdin is not None
            proc.stdin.close()

            assert proc.wait(timeout=2) is not None
            assert gone_within(2, pid)
            assert proc.stdout is not None
            assert proc.stdout.read() == b""
        finally:
            kill_group(proc)

    def test_terminating_the_shell_ends_the_command(
        self, shell: str, tmp_path: Path, pid_file: Path
    ) -> None:
        proc = spawn(shell, self.long_running(pid_file), tmp_path)
        try:
            pid = self.started(pid_file)

            proc.terminate()

            assert proc.wait(timeout=2) is not None
            assert gone_within(2, pid)
        finally:
            kill_group(proc)

    def test_a_command_that_finishes_ends_the_session_with_stdin_still_open(
        self, shell: str, tmp_path: Path
    ) -> None:
        proc = spawn(shell, "printf 'one\\ntwo\\n'", tmp_path)
        try:
            assert read_until_eof(proc) == b"one\ntwo\n"
            assert proc.wait(timeout=2) is not None
            assert proc.stdin is not None
            assert not proc.stdin.closed
        finally:
            kill_group(proc)

    def test_a_command_that_fails_ends_the_session_too(self, shell: str, tmp_path: Path) -> None:
        proc = spawn(shell, "sh -c 'echo before; exit 3'", tmp_path)
        try:
            assert read_until_eof(proc) == b"before\n"
            assert proc.wait(timeout=2) is not None
        finally:
            kill_group(proc)

    def test_lines_sent_to_the_session_never_reach_the_command(
        self, shell: str, tmp_path: Path
    ) -> None:
        proc = spawn(shell, "sleep 0.5; cat; echo done", tmp_path)
        try:
            assert proc.stdin is not None
            proc.stdin.write(b"\n\nnot for the command\n")
            proc.stdin.flush()

            assert read_until_eof(proc) == b"done\n"
            assert proc.wait(timeout=2) == 0
        finally:
            kill_group(proc)

    @pytest.mark.parametrize(("command", "status"), [("echo one", 0), ("exit 3", 3)])
    def test_the_session_exits_with_the_commands_own_status(
        self, shell: str, tmp_path: Path, command: str, status: int
    ) -> None:
        proc = spawn(shell, command, tmp_path)
        try:
            read_until_eof(proc)
            assert proc.wait(timeout=2) == status
        finally:
            kill_group(proc)

    def test_what_a_finished_command_left_running_ends_with_the_session(
        self, shell: str, tmp_path: Path, pid_file: Path
    ) -> None:
        proc = spawn(shell, f"sleep 600 & echo $! > {shlex.quote(str(pid_file))}", tmp_path)
        try:
            pid = self.started(pid_file)
            read_until_eof(proc)

            assert proc.wait(timeout=2) == 0
            assert gone_within(2, pid)
        finally:
            kill_group(proc)

    def test_the_command_can_use_a_shell_syntax(self, shell: str, tmp_path: Path) -> None:
        proc = spawn(shell, "echo a; echo b >&2; echo 'c d' | tr a-z A-Z", tmp_path)
        try:
            # Every statement's errors, not only the last one's, come with the output.
            assert read_until_eof(proc) == b"a\nb\nC D\n"
        finally:
            kill_group(proc)

    def test_sbin_is_on_the_path(self, shell: str, tmp_path: Path) -> None:
        proc = spawn(shell, 'echo "$PATH"', tmp_path)
        try:
            assert read_until_eof(proc).decode().strip().endswith(":/usr/sbin:/sbin")
        finally:
            kill_group(proc)

    def test_a_session_that_shares_its_callers_group_moves_out_of_it(
        self, shell: str, tmp_path: Path, pid_file: Path
    ) -> None:
        """A daemon-hosted sshd hands every session the daemon's process group, and `kill 0` would
        reach the other sessions in it. The stand-in `setsid` is the one this machine lacks."""
        stand_in = tmp_path / "bin"
        stand_in.mkdir()
        (stand_in / "setsid.py").write_text(
            "import os, sys\n"
            "if os.getpgrp() == os.getpid():\n"
            "    if os.fork():\n"
            "        os._exit(0)\n"
            "os.setsid()\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n",
            encoding="utf-8",
        )
        setsid = stand_in / "setsid"
        setsid.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "{stand_in}/setsid.py" "$@"\n',
            encoding="utf-8",
        )
        setsid.chmod(0o755)
        sibling_file = tmp_path / "sibling.pid"
        caller = tmp_path / "caller.sh"
        caller.write_text(
            "#!/bin/sh\n"
            f"sleep 600 >/dev/null 2>&1 &\necho $! > {shlex.quote(str(sibling_file))}\n"
            '"$1" -c "$2"\n'
            "exit $?\n",
            encoding="utf-8",
        )
        caller.chmod(0o755)
        proc = subprocess.Popen(
            [str(caller), shell, remote.wrap(self.long_running(pid_file))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": f"{stand_in}:/usr/bin:/bin"},
            start_new_session=True,
        )
        try:
            pid = self.started(pid_file)
            wait_until(lambda: sibling_file.exists() and sibling_file.read_text().strip())
            sibling = int(sibling_file.read_text().strip())

            assert proc.stdin is not None
            proc.stdin.close()

            assert proc.wait(timeout=2) is not None
            assert gone_within(2, pid)
            assert alive(sibling), "the session reaped a process it does not own"
        finally:
            kill_group(proc)


@POSIX
@pytest.mark.parametrize("command", ["exit 3", "test -f /absent/pump.log || exit 1; tail -f x"])
def test_a_command_that_exits_the_shell_ends_the_session(command: str, tmp_path: Path) -> None:
    proc = spawn("/bin/sh", command, tmp_path)
    try:
        read_until_eof(proc, timeout=1)
        assert proc.wait(timeout=1) is not None
    finally:
        kill_group(proc)


@POSIX
class TestPopen:
    def test_the_process_leads_its_own_group(self) -> None:
        assert sys.platform != "win32"
        proc = remote.popen(["sleep", "600"])
        try:
            assert os.getpgid(proc.pid) == proc.pid
            assert os.getpgid(proc.pid) != os.getpgrp()
        finally:
            kill_group(proc)

    def test_passes_the_pipes_through(self) -> None:
        proc = remote.popen(
            ["/bin/sh", "-c", "cat; echo err >&2"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        out, err = proc.communicate(b"hi\n", timeout=5)

        assert (out, err) == (b"hi\n", b"err\n")


class TestRun:
    LINES = "[print(f'{i:05d}', flush=True) for i in range(30000)]"

    def test_a_long_output_keeps_its_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "_KEEP_BYTES", 1000)

        done = remote.run([sys.executable, "-c", self.LINES], timeout=30)

        assert done is not None
        assert done.cut
        assert done.stdout.splitlines()[-1] == b"29999"
        # 180 kB printed; what is kept is the limit and at most one read more.
        assert len(done.stdout) <= 1000 + 65536

    def test_a_short_output_is_whole(self) -> None:
        done = remote.run([sys.executable, "-c", "print('one'); print('two')"], timeout=30)

        assert done is not None
        assert not done.cut
        assert done.stdout.splitlines() == [b"one", b"two"]
        assert done.returncode == 0


class Running:
    """A process for `wake`: running until `end`."""

    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def end(self) -> None:
        self.returncode = 0


class Received:
    """A session's stdin: what was written to it, until it is closed."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to closed file")
        self.data += data
        return len(data)

    def flush(self) -> None:
        pass


class TestWake:
    @pytest.fixture(autouse=True)
    def quick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "_WAKE_S", 0.01)

    def wake(self, process: Running, stdin: Received) -> None:
        remote.wake(process, stdin)  # type: ignore[arg-type]

    def test_on_windows_a_running_session_gets_newlines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        process, stdin = Running(), Received()

        self.wake(process, stdin)

        wait_until(lambda: stdin.data.count(b"\n") >= 3)
        assert set(stdin.data) == {ord("\n")}
        process.end()

    def test_the_newlines_stop_when_the_session_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        process, stdin = Running(), Received()
        self.wake(process, stdin)
        wait_until(lambda: stdin.data)

        process.end()
        time.sleep(0.05)
        sent = stdin.data
        time.sleep(0.1)

        assert stdin.data == sent

    @pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_closed_stdin_stops_them_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        process, stdin = Running(), Received()
        self.wake(process, stdin)
        wait_until(lambda: stdin.data)

        stdin.closed = True
        time.sleep(0.1)

        assert process.poll() is None
        process.end()

    def test_elsewhere_nothing_is_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        process, stdin = Running(), Received()

        self.wake(process, stdin)
        time.sleep(0.1)

        assert stdin.data == b""
        process.end()


class TestChildEnv:
    def test_elsewhere_a_child_inherits_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")

        assert remote.child_env() is None

    def test_on_windows_a_child_inherits_a_programdata_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("PROGRAMDATA", r"E:\Data")

        assert remote.child_env() is None

    @pytest.mark.skipif(sys.platform != "win32", reason="asks Windows for the folder")
    def test_on_windows_openssh_gets_programdata_when_the_agent_leaves_it_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        given = os.environ["PROGRAMDATA"]
        monkeypatch.delenv("PROGRAMDATA")

        env = remote.child_env()

        assert env is not None
        assert Path(env["PROGRAMDATA"]) == Path(given)


@POSIX
class TestTerminate:
    def test_closing_stdin_is_enough_for_a_reader(self) -> None:
        proc = remote.popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        started = time.monotonic()

        remote.terminate(proc)

        assert proc.returncode == 0
        assert time.monotonic() - started < 1

    def test_a_process_that_ignores_stdin_gets_sigterm(self) -> None:
        proc = remote.popen(["sleep", "600"], stdin=subprocess.PIPE)

        remote.terminate(proc, timeout=0.2)

        assert proc.returncode == -signal.SIGTERM

    def test_a_process_that_ignores_sigterm_gets_sigkill(self) -> None:
        assert sys.platform != "win32"
        proc = remote.popen(["/bin/sh", "-c", "trap '' TERM; sleep 600"], stdin=subprocess.PIPE)
        time.sleep(0.3)

        remote.terminate(proc, timeout=0.2)

        assert proc.returncode == -signal.SIGKILL

    def test_what_the_process_started_stops_with_it(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "child.pid"
        proc = remote.popen(
            ["/bin/sh", "-c", f"sleep 600 & echo $! > {shlex.quote(str(pid_file))}; wait"]
        )
        wait_until(lambda: pid_file.exists() and pid_file.read_text().strip())
        child = int(pid_file.read_text().strip())
        assert alive(child)

        remote.terminate(proc, timeout=0.2)

        assert gone_within(2, child)

    def test_a_process_that_already_ended(self) -> None:
        proc = remote.popen(["true"], stdin=subprocess.PIPE)
        proc.wait()

        remote.terminate(proc)

        assert proc.returncode == 0

    def test_a_process_without_a_stdin_pipe(self) -> None:
        proc = remote.popen(["sleep", "600"])

        remote.terminate(proc, timeout=0.2)

        assert proc.returncode == -signal.SIGTERM

    def test_windows_kills_the_process_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        class Stubborn:
            pid = 4321
            stdin = None
            waits = 0

            def wait(self, timeout: float | None = None) -> int:
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("x", timeout or 0)
                return 1

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda argv, **kwargs: calls.append(argv),  # type: ignore[arg-type]
        )

        remote.terminate(Stubborn(), timeout=0.01)  # type: ignore[arg-type]

        assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]]
