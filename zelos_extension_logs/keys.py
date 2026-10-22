"""The extension's own SSH key, for boards a headless agent reaches without a person's keys."""

import shlex
import socket
import subprocess
import sys
from pathlib import Path

from .remote import child_env, openssh


class KeyFailure(Exception):
    """The key could not be made; the message says why."""


def path(data_dir: Path | None) -> Path | None:
    """Where the private key lives, or None without a data directory."""
    return data_dir / "ssh" / "id_ed25519" if data_dir is not None else None


def ensure(data_dir: Path | None) -> Path:
    """The private key, made on first use with the system's ssh-keygen."""
    key = path(data_dir)
    if key is None:
        raise KeyFailure("The extension has no data directory to keep a key in.")
    if key.is_file() and key.with_suffix(".pub").is_file():
        return key
    keygen = openssh("ssh-keygen")
    if keygen is None:
        raise KeyFailure("ssh-keygen is not installed on this computer.")
    try:
        key.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            key.parent.chmod(0o700)
        for stale in (key, key.with_suffix(".pub")):
            stale.unlink(missing_ok=True)
    except OSError as e:
        raise KeyFailure(f"Cannot write the key to {key.parent}: {e.strerror or e}.") from e
    comment = f"zelos-logs@{socket.gethostname()}"
    done = subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
    )
    if done.returncode != 0:
        raise KeyFailure(f"ssh-keygen failed: {done.stderr.strip() or done.returncode}.")
    return key


def public(key: Path) -> str:
    """The public key's one line."""
    return key.with_suffix(".pub").read_text(encoding="utf-8").strip()


def install_line(public_key: str) -> str:
    """A shell line that adds the key on a board, run in any session the user already has."""
    return (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"echo {shlex.quote(public_key)} >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
