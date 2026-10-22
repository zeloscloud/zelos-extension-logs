"""Commands on a board, through the system ssh client, or on this computer."""

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .config import SourceConfig

# How often a wrapped session's stdin gets a newline on Windows.
_WAKE_S = 0.5
# The keys ssh offers when nothing names one. Naming the access key with -i stops ssh from offering
# these by itself, so they are named too.
_DEFAULT_KEYS = ("id_ed25519", "id_ecdsa", "id_rsa")


class SshMissing(Exception):
    """No ssh client on this computer."""

    def __init__(self) -> None:
        super().__init__(
            "No ssh client is installed on this computer. On Windows, add the OpenSSH Client "
            "optional feature."
        )


def openssh(tool: str) -> str | None:
    """`ssh` or `ssh-keygen`; Windows' own OpenSSH first, because Git's cannot reach its agent."""
    if sys.platform == "win32":
        system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "OpenSSH"
        if (system / f"{tool}.exe").is_file():
            return str(system / f"{tool}.exe")
    return shutil.which(tool)


def merged(command: str) -> str:
    """`command` for sh, with every statement's errors in its output; its `exit` ends only it."""
    return f"( {command}\n) 2>&1"


def wrap(command: str) -> str:
    """`command` for `sh -c` on the board, ended with the ssh session, which exits with the
    command's own status.

    The builtin `read` waits for the channel to close, and either side ending runs `kill 0`, so
    the command never outlives the session, as Dropbear would otherwise let a `tail -F` do. When
    the command ends first, the session ignores its own `kill 0`. `setsid` when sshd did not give
    the session its own group (Tailscale SSH), or `kill 0` would reach other sessions. `/sbin` is on
    PATH because BusyBox keeps `logread` there and a non-root login over Dropbear does not have it.
    """
    session = (
        "PATH=$PATH:/usr/sbin:/sbin; export PATH; "
        "reap() { trap - EXIT TERM INT HUP; kill 0 2>/dev/null; }; "
        "ended() { wait $!; s=$?; trap - EXIT INT HUP; trap '' TERM; kill 0 2>/dev/null; "
        "exit $s; }; "
        "trap reap EXIT TERM INT HUP; trap ended USR1; "
        f"{{ {merged(command)}; s=$?; kill -USR1 $$ 2>/dev/null; exit $s; }} & "
        "while read -r _; do :; done"
    )
    return (
        f"c={shlex.quote(session)}; "
        "if ! kill -0 -$$ 2>/dev/null && command -v setsid >/dev/null 2>&1; "
        'then exec setsid sh -c "$c"; else exec sh -c "$c"; fi'
    )


def ssh_argv(cfg: SourceConfig, remote_command: str, access_key: Path | None) -> list[str]:
    """The ssh command line that runs `remote_command` on the source's host."""
    exe = openssh("ssh")
    if exe is None:
        raise SshMissing
    assert cfg.host is not None
    options = [
        "BatchMode=yes",
        "ConnectTimeout=10",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=3",
    ]
    if cfg.host_key_policy == "auto":
        options += [
            "StrictHostKeyChecking=no",
            f"UserKnownHostsFile={os.devnull}",
            f"GlobalKnownHostsFile={os.devnull}",
        ]
        # Quiets the new-host-key warning; on Windows it would also hide why a connection failed.
        if sys.platform != "win32":
            options.append("LogLevel=ERROR")
    else:
        options.append("StrictHostKeyChecking=yes")
    if sys.platform == "win32":
        # A `Host *` ControlMaster in the user's config breaks Windows' ssh.exe.
        options += ["ControlMaster=no", "ControlPath=none"]
    # Extra options go first: ssh keeps the first value it sees for an option.
    argv = [exe, *cfg.ssh_options, "-T"]
    for option in options:
        argv += ["-o", option]
    if cfg.ssh_port:
        argv += ["-p", str(cfg.ssh_port)]
    if cfg.key_path:
        argv += ["-i", cfg.key_path, "-o", "IdentitiesOnly=yes"]
    elif access_key is not None and access_key.is_file():
        argv += ["-i", str(access_key)]
        home = Path.home() / ".ssh"
        for name in _DEFAULT_KEYS:
            if (home / name).is_file():
                argv += ["-i", str(home / name)]
    return [*argv, "--", cfg.host, remote_command]


def local_argv(command: str) -> list[str] | str:
    """`command` for this computer's shell: sh, or cmd on Windows."""
    if sys.platform == "win32":
        return command
    return ["/bin/sh", "-c", command]


def child_env() -> dict[str, str] | None:
    """The environment of a process this extension starts, or None for the extension's own.

    Windows' OpenSSH reads its settings from PROGRAMDATA and exits 255 without a word when it is
    missing, as it is in the environment a Zelos agent gives its extensions.
    """
    if sys.platform != "win32" or "PROGRAMDATA" in os.environ:
        return None
    from platformdirs.windows import Windows

    return {**os.environ, "PROGRAMDATA": Windows().site_data_dir}


def popen(argv: list[str] | str, **kwargs: object) -> subprocess.Popen[bytes]:
    """Start a process in its own group, so stopping it stops what it started."""
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        shell = isinstance(argv, str)
        return subprocess.Popen(argv, shell=shell, creationflags=flags, env=child_env(), **kwargs)  # type: ignore[call-overload]
    return subprocess.Popen(argv, start_new_session=True, **kwargs)  # type: ignore[call-overload]


def wake(process: subprocess.Popen[bytes], stdin: IO[bytes]) -> None:
    """On Windows, write a newline to a wrapped ssh session's `stdin` every half second while it
    runs; elsewhere, nothing.

    Windows' ssh.exe can hang closing its idle stdin when a session ends, until a byte arrives.
    The session reads and drops whole lines, and the command never sees them.
    """
    if sys.platform != "win32":
        return

    def beat() -> None:
        while process.poll() is None:
            try:
                stdin.write(b"\n")
                stdin.flush()
            except (OSError, ValueError):
                return
            time.sleep(_WAKE_S)

    threading.Thread(target=beat, name="ssh-stdin", daemon=True).start()


def stop(process: subprocess.Popen[bytes], *, kill: bool = False) -> None:
    """Close stdin, which ends a wrapped remote command, and signal the process and its group to
    stop, or with `kill` to die; returns without waiting."""
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)


def terminate(process: subprocess.Popen[bytes], timeout: float = 1.0) -> None:
    """Close stdin, which ends a wrapped remote command, then stop the process and its group, then
    kill them, each step after the one before has waited `timeout`."""
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()
    try:
        process.wait(timeout)
        return
    except subprocess.TimeoutExpired:
        stop(process)
    try:
        process.wait(timeout)
        return
    except subprocess.TimeoutExpired:
        stop(process, kill=True)
    process.wait()


def run(
    argv: list[str] | str,
    timeout: float,
    *,
    hold_stdin: bool = False,
    stderr: int = subprocess.PIPE,
) -> subprocess.CompletedProcess[bytes] | None:
    """Run `argv` to its end, or stop it and what it started after `timeout` and return None.

    With `hold_stdin`, its stdin stays open until then, as a wrapped remote command needs, and
    `wake` keeps it moving; without, it reads nothing. `stderr` is Popen's: STDOUT puts a local
    command's errors in its output.
    """
    with contextlib.ExitStack() as held:
        stdin = subprocess.DEVNULL
        session: IO[bytes] | None = None
        if hold_stdin:
            stdin, end = os.pipe()
            held.callback(os.close, stdin)
            session = held.enter_context(os.fdopen(end, "wb", buffering=0))
        process = popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr)
        if session is not None:
            wake(process, session)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            held.close()
            terminate(process, timeout=0.5)
            process.communicate()
            return None
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a connection ended, in one sentence with the fix."""

    message: str
    # Retrying soon cannot help: the user has to change something first.
    lasting: bool


# What ssh prints, the sentence it becomes, and whether it lasts; the first match wins.
_FAILURES: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (
        ("remote host identification has changed",),
        "The host key of {host} changed. Run {remove}, or set Host key check to Trust the "
        "board's key.",
        True,
    ),
    (
        ("host key verification failed",),
        "The host key of {host} is not in known_hosts. Connect once with ssh to add it, or set "
        "Host key check to Trust the board's key.",
        True,
    ),
    (
        ("permission denied", "too many authentication failures"),
        "{host} refused the login. Run Show Access Key and add its line to the board, or set "
        "Key file.",
        True,
    ),
    (
        (
            "could not resolve hostname",
            "name or service not known",
            "nodename nor servname",
            "no such host",
        ),
        "{host} cannot be resolved. Check the name, or use its IP address.",
        False,
    ),
    (
        ("connection refused",),
        "{host} refused the connection. Check that an SSH server runs there.",
        False,
    ),
    (
        ("timed out", "no route to host", "network is unreachable", "host is down"),
        "{host} did not answer. Check that it is on and reachable.",
        False,
    ),
)
# The name ssh keeps the key under, with the port when it is not 22: `[bench-1]:2222`.
_CHANGED = re.compile(r"Host key for (\S+) has changed")


def explain(stderr: str, host: str) -> Failure:
    """What ssh's stderr says went wrong."""
    text = stderr.lower()
    for phrases, message, lasting in _FAILURES:
        if any(phrase in text for phrase in phrases):
            m = _CHANGED.search(stderr)
            known = m[1] if m else host.rpartition("@")[2]
            remove = f"ssh-keygen -R {shlex.quote(known)}"
            return Failure(message.format(host=host, remove=remove), lasting)
    last = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    return Failure(f"ssh to {host} failed: {last}" if last else f"ssh to {host} failed.", False)
