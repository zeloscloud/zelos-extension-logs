"""The extension's own ssh key: made once with the system's ssh-keygen, and the line that installs
it on a board."""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from zelos_extension_logs import keys
from zelos_extension_logs.keys import KeyFailure

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and shell")


class TestPath:
    def test_under_the_data_directory(self, tmp_path: Path) -> None:
        assert keys.path(tmp_path) == tmp_path / "ssh" / "id_ed25519"

    def test_no_data_directory(self) -> None:
        assert keys.path(None) is None


@needs_keygen
class TestEnsure:
    def test_makes_an_ed25519_key_pair(self, tmp_path: Path) -> None:
        key = keys.ensure(tmp_path)

        assert key == tmp_path / "ssh" / "id_ed25519"
        assert key.is_file()
        assert key.with_suffix(".pub").is_file()
        line = keys.public(key)
        assert line.startswith("ssh-ed25519 ")
        assert line.endswith(f" zelos-logs@{socket.gethostname()}")
        assert "\n" not in line

    def test_the_public_half_belongs_to_the_private_half(self, tmp_path: Path) -> None:
        key = keys.ensure(tmp_path)

        derived = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, check=True
        ).stdout.split()

        assert derived[:2] == keys.public(key).split()[:2]

    def test_has_no_passphrase(self, tmp_path: Path) -> None:
        key = keys.ensure(tmp_path)

        done = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(key)], capture_output=True, check=False
        )

        assert done.returncode == 0

    @posix
    def test_the_directory_and_key_are_private(self, tmp_path: Path) -> None:
        key = keys.ensure(tmp_path)

        assert key.parent.stat().st_mode & 0o777 == 0o700
        assert key.stat().st_mode & 0o077 == 0

    def test_made_once_and_reused(self, tmp_path: Path) -> None:
        first = keys.ensure(tmp_path)
        private, public = first.read_bytes(), first.with_suffix(".pub").read_bytes()
        stamp = first.stat().st_mtime_ns

        second = keys.ensure(tmp_path)

        assert second == first
        assert second.read_bytes() == private
        assert second.with_suffix(".pub").read_bytes() == public
        assert second.stat().st_mtime_ns == stamp

    def test_a_private_key_without_its_public_half_is_made_again(self, tmp_path: Path) -> None:
        key = keys.ensure(tmp_path)
        before = key.read_bytes()
        key.with_suffix(".pub").unlink()

        keys.ensure(tmp_path)

        assert key.with_suffix(".pub").is_file()
        assert key.read_bytes() != before

    def test_a_directory_that_does_not_exist_yet(self, tmp_path: Path) -> None:
        keys.ensure(tmp_path / "data" / "nested")

        assert (tmp_path / "data" / "nested" / "ssh" / "id_ed25519").is_file()

    def test_no_data_directory_is_an_error(self) -> None:
        with pytest.raises(KeyFailure) as caught:
            keys.ensure(None)

        assert str(caught.value) == "The extension has no data directory to keep a key in."

    def test_no_ssh_keygen_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keys, "openssh", lambda tool: None)

        with pytest.raises(KeyFailure) as caught:
            keys.ensure(tmp_path)

        assert str(caught.value) == "ssh-keygen is not installed on this computer."
        assert not (tmp_path / "ssh" / "id_ed25519").exists()

    @posix
    def test_a_failing_ssh_keygen_says_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "ssh-keygen"
        fake.write_text("#!/bin/sh\necho 'no entropy' >&2\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setattr(keys, "openssh", lambda tool: str(fake))

        with pytest.raises(KeyFailure) as caught:
            keys.ensure(tmp_path / "data")

        assert str(caught.value) == "ssh-keygen failed: no entropy."

    @posix
    def test_a_failing_ssh_keygen_without_words_gives_its_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keys, "openssh", lambda tool: "/usr/bin/false")

        with pytest.raises(KeyFailure) as caught:
            keys.ensure(tmp_path / "data")

        assert str(caught.value) == "ssh-keygen failed: 1."

    def test_a_data_directory_that_cannot_be_written_is_a_key_failure(self, tmp_path: Path) -> None:
        blocker = tmp_path / "data"
        blocker.write_text("x", encoding="utf-8")

        with pytest.raises(KeyFailure) as caught:
            keys.ensure(blocker)

        assert str(caught.value).startswith(f"Cannot write the key to {blocker / 'ssh'}: ")


KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPump0123456789 zelos-logs@bench-1"


class TestInstallLine:
    def test_content(self) -> None:
        line = keys.install_line(KEY)

        assert line == (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo '{KEY}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        )

    @posix
    def test_run_on_a_board_it_appends_the_key_with_private_permissions(
        self, tmp_path: Path
    ) -> None:
        board_home = tmp_path / "board"
        board_home.mkdir()
        env = {"HOME": str(board_home), "PATH": os.environ["PATH"]}

        for _ in range(2):
            subprocess.run(["/bin/sh", "-c", keys.install_line(KEY)], env=env, check=True)

        authorized = board_home / ".ssh" / "authorized_keys"
        assert authorized.read_text(encoding="utf-8").splitlines() == [KEY, KEY]
        assert (board_home / ".ssh").stat().st_mode & 0o777 == 0o700
        assert authorized.stat().st_mode & 0o777 == 0o600

    @posix
    def test_run_on_a_board_a_hostile_comment_stays_text(self, tmp_path: Path) -> None:
        board_home = tmp_path / "board"
        board_home.mkdir()
        hostile = "ssh-ed25519 AAAA c'; touch ~/pwned; echo '"
        env = {"HOME": str(board_home), "PATH": os.environ["PATH"]}

        subprocess.run(["/bin/sh", "-c", keys.install_line(hostile)], env=env, check=True)

        assert not (board_home / "pwned").exists()
        authorized = board_home / ".ssh" / "authorized_keys"
        assert authorized.read_text(encoding="utf-8") == hostile + "\n"


class TestPublic:
    def test_is_the_one_line_without_its_line_end(self, tmp_path: Path) -> None:
        key = tmp_path / "id_ed25519"
        key.with_suffix(".pub").write_text(f"{KEY}\n\n", encoding="utf-8")

        assert keys.public(key) == KEY
