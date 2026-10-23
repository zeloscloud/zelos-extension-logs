"""The probe script's output, and the command each kind of board gets."""

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_logs import probe
from zelos_extension_logs.probe import NoLog, Probe, Truncated, system_log

S = 1_000_000_000
BOOT = "f07dd35d-996b-4983-98f1-e4347c93e456"
CURSOR = "s=042b6c2a969345428890518ac13848bc;i=9fd;b=f07dd35d996b498398f1e4347c93e456;m=125371c1"

ROOT_SYSTEMD = f"""\
uid=0
user=root
groups=root
systemd=252
boot_id={BOOT}
journal={{"__CURSOR":"{CURSOR}","__REALTIME_TIMESTAMP":"1792606744000000","__MONOTONIC_TIMESTAMP":"795100000"}}
clock=1792606744
clock_ns=1792606744512000000
uptime=795.61
end=1
"""

NON_ROOT = f"""\
uid=1000
user=probe
groups=probe
systemd=252
boot_id={BOOT}
uptime=795.61
clock=1792606744
end=1
"""

BUSYBOX = f"""\
uid=0
user=root
groups=root,bin,daemon,sys,adm,disk,wheel,floppy,dialout,tape,video
systemd=
logread=1
file=/var/log/messages
boot_id={BOOT}
clock=1792606745
clock_ns=1792606745%N
uptime=796.08
end=1
"""


def make(**changes: Any) -> Probe:
    fields: dict[str, Any] = {
        "uid": 0,
        "user": "root",
        "groups": frozenset({"root"}),
        "systemd": None,
        "logread": False,
        "files": (),
        "boot_id": "f07dd35d996b498398f1e4347c93e456",
        "uptime_ns": 795_610_000_000,
        "clock_s": 1_700_000_000,
        "clock_ns": None,
        "journal_gap_ns": None,
        "host_ns": 1_700_000_000_000_000_000,
    }
    return Probe(**{**fields, **changes})


class TestMonotonic:
    """The board's monotonic clock, the one journal entries carry, when the probe answered."""

    CLOCK_NS = 1_792_606_744_512_000_000

    def test_its_wall_clock_less_the_journals_gap(self) -> None:
        # Asleep for 400 s since boot: the uptime counts that, the monotonic clock does not.
        board = make(
            clock_ns=self.CLOCK_NS, journal_gap_ns=self.CLOCK_NS - 500 * S, uptime_ns=900 * S
        )

        assert board.monotonic_ns == 500 * S

    def test_never_more_than_the_uptime(self) -> None:
        # The wall clock stepped forward after the journal's latest entry.
        board = make(
            clock_ns=self.CLOCK_NS, journal_gap_ns=self.CLOCK_NS - 600 * S, uptime_ns=500 * S
        )

        assert board.monotonic_ns == 500 * S

    def test_whole_seconds_round_it_down_where_date_has_no_nanoseconds(self) -> None:
        board = make(
            clock_s=1_792_606_744,
            clock_ns=None,
            journal_gap_ns=1_792_606_744_250_000_000 - 500 * S,
            uptime_ns=None,
        )

        assert board.monotonic_ns == 500 * S - 250_000_000

    def test_unknown_without_the_journals_clocks(self) -> None:
        assert make(clock_ns=self.CLOCK_NS, journal_gap_ns=None).monotonic_ns is None

    def test_the_kernels_clock_is_the_monotonic_clock_less_its_lag(self) -> None:
        board = make(
            clock_ns=self.CLOCK_NS,
            journal_gap_ns=self.CLOCK_NS - 500 * S,
            uptime_ns=500 * S,
            kernel_lag_ns=420_000_000,
        )

        assert board.kernel_ns == 500 * S - 420_000_000

    def test_the_kernels_clock_is_the_uptime_without_a_journal(self) -> None:
        assert make(uptime_ns=500 * S).kernel_ns == 500 * S

    @pytest.mark.parametrize(
        ("source_us", "lag_ns"),
        # Imported from the buffer when journald started at boot, received seconds late.
        [(400_000_000, -2_600_000_000), (3_000_000, None)],
    )
    def test_the_kernels_lag_is_read_from_an_entry_the_kernel_printed_live(
        self, source_us: int, lag_ns: int | None
    ) -> None:
        received_us = source_us - 2_600_000 if lag_ns else source_us + 5_000_000
        kernel = json.dumps(
            {
                "__MONOTONIC_TIMESTAMP": str(received_us),
                "_SOURCE_MONOTONIC_TIMESTAMP": str(source_us),
            }
        )
        board = probe.parse(ROOT_SYSTEMD.replace("end=1", f"kernel={kernel}\nend=1"), 0)

        assert board.kernel_lag_ns == lag_ns

    def test_the_kernels_oldest_line_is_read_as_dmesg_printed_it(self) -> None:
        line = "[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x414fd0b1]"
        board = probe.parse(ROOT_SYSTEMD.replace("end=1", f"dmesg={line}\nend=1"), 0)

        assert (board.kernel_oldest, probe.parse(ROOT_SYSTEMD, 0).kernel_oldest) == (line, "")

    def test_an_entry_that_is_not_json_has_no_gap(self) -> None:
        board = probe.parse(ROOT_SYSTEMD.replace("journal={", "journal=x{"), 0)

        assert board.journal_gap_ns is None


class TestParse:
    def test_root_with_systemd(self) -> None:
        board = probe.parse(ROOT_SYSTEMD, 42)

        assert board == Probe(
            uid=0,
            user="root",
            groups=frozenset({"root"}),
            systemd=252,
            logread=False,
            files=(),
            boot_id="f07dd35d996b498398f1e4347c93e456",
            uptime_ns=795_610_000_000,
            clock_s=1792606744,
            clock_ns=1792606744_512_000_000,
            journal_gap_ns=(1792606744_000_000 - 795_100_000) * 1000,
            host_ns=42,
        )
        assert not board.partial_journal

    def test_non_root_outside_the_journal_groups_sees_a_partial_journal(self) -> None:
        board = probe.parse(NON_ROOT, 0)

        assert (board.uid, board.user, board.systemd) == (1000, "probe", 252)
        assert board.partial_journal is True

    @pytest.mark.parametrize("group", ["systemd-journal", "adm", "wheel", "root"])
    def test_a_journal_reading_group_sees_everything(self, group: str) -> None:
        board = probe.parse(NON_ROOT.replace("groups=probe", f"groups=probe,{group}"), 0)

        assert board.partial_journal is False

    def test_busybox(self) -> None:
        board = probe.parse(BUSYBOX, 0)

        assert board.systemd is None
        assert board.logread is True
        assert board.files == ("/var/log/messages",)
        assert board.uptime_ns == 796_080_000_000
        assert board.clock_s == 1792606745
        # BusyBox prints `date +%N` as is.
        assert board.clock_ns is None
        assert board.journal_gap_ns is None
        assert "adm" in board.groups

    def test_a_clock_with_an_empty_fraction_is_not_misread(self) -> None:
        """BusyBox's `date +%s.%N` prints `1792606745.`; the script asks for whole seconds, but a
        trailing dot must never turn into another number."""
        board = probe.parse(BUSYBOX.replace("clock=1792606745", "clock=1792606745."), 0)

        assert board.clock_s in (None, 1792606745)

    def test_uptime_with_an_empty_fraction(self) -> None:
        board = probe.parse(BUSYBOX.replace("uptime=796.08", "uptime=796."), 0)

        assert board.uptime_ns == 796_000_000_000

    def test_uptime_keeps_nanosecond_precision_and_cuts_beyond_it(self) -> None:
        board = probe.parse(ROOT_SYSTEMD.replace("795.61", "1.123456789999"), 0)

        assert board.uptime_ns == 1_123_456_789

    def test_every_file_is_listed_in_order(self) -> None:
        output = BUSYBOX.replace(
            "file=/var/log/messages\n", "file=/var/log/messages\nfile=/var/log/syslog\n"
        )

        assert probe.parse(output, 0).files == ("/var/log/messages", "/var/log/syslog")

    def test_a_missing_end_is_truncated(self) -> None:
        with pytest.raises(Truncated):
            probe.parse(ROOT_SYSTEMD.removesuffix("end=1\n"), 0)

    def test_an_empty_output_is_truncated(self) -> None:
        with pytest.raises(Truncated):
            probe.parse("", 0)

    def test_an_end_that_is_not_one_is_truncated(self) -> None:
        with pytest.raises(Truncated):
            probe.parse("uid=0\nend=0\n", 0)

    def test_an_empty_answer_is_unknown_not_zero(self) -> None:
        board = probe.parse(
            "uid=\nuser=\ngroups=\nsystemd=\njournal=\nclock=\nclock_ns=\nuptime=\nend=1\n", 0
        )

        assert board.uid is None
        assert board.systemd is None
        assert board.uptime_ns is None
        assert board.clock_s is None
        assert board.clock_ns is None
        assert board.journal_gap_ns is None
        assert board.groups == frozenset()
        assert board.boot_id == ""

    def test_unknown_uid_is_a_partial_journal(self) -> None:
        assert probe.parse("end=1\n", 0).partial_journal is True

    def test_noise_around_the_answers_is_skipped(self) -> None:
        output = "Welcome to bench-1\nno equals sign here\n" + ROOT_SYSTEMD + "logout\n"

        assert probe.parse(output, 0).systemd == 252

    def test_windows_line_ends(self) -> None:
        board = probe.parse(ROOT_SYSTEMD.replace("\n", "\r\n"), 0)

        assert (board.uid, board.systemd, board.clock_s) == (0, 252, 1792606744)

    @given(st.text(), st.integers())
    def test_arbitrary_text_is_only_ever_truncated(self, output: str, host_ns: int) -> None:
        with contextlib.suppress(Truncated):
            probe.parse(output, host_ns)

    @given(
        st.lists(
            st.tuples(
                st.sampled_from(
                    [
                        "uid",
                        "user",
                        "groups",
                        "systemd",
                        "logread",
                        "file",
                        "boot_id",
                        "uptime",
                        "clock",
                    ]
                ),
                st.text(max_size=20),
            ).map(lambda pair: f"{pair[0]}={pair[1]}".replace("\n", " ")),
            max_size=12,
        )
    )
    def test_arbitrary_answers_that_end_never_raise(self, lines: list[str]) -> None:
        board = probe.parse("\n".join([*lines, "end=1"]), 0)

        assert isinstance(board.partial_journal, bool)


def run_script(shell: str, path: str) -> str:
    done = subprocess.run(
        [shell, "-c", probe.SCRIPT],
        capture_output=True,
        text=True,
        env={"PATH": path},
        timeout=20,
        check=True,
    )
    return done.stdout


SHELLS = [s for s in ("/bin/sh", "/bin/dash", shutil.which("bash")) if s and Path(s).exists()]


@pytest.mark.skipif(os.name == "nt", reason="the script runs in a POSIX shell")
class TestScript:
    """The probe is one line of sh that runs on the board; only running it checks it."""

    @pytest.fixture
    def tools(self, tmp_path: Path) -> Path:
        """Only the tools the probe calls, so this computer's own journalctl never answers it."""
        directory = tmp_path / "tools"
        directory.mkdir()
        for name in ("cat", "cut", "date", "id", "sed", "tr"):
            found = shutil.which(name)
            assert found is not None
            (directory / name).symlink_to(found)
        return directory

    @pytest.mark.parametrize("shell", SHELLS)
    def test_runs_under_every_posix_shell(self, shell: str, tools: Path) -> None:
        assert sys.platform != "win32"
        before = time.time()
        output = run_script(shell, str(tools))

        board = probe.parse(output, 0)
        assert board.uid == os.getuid()
        assert board.clock_s is not None
        assert before - 5 <= board.clock_s <= time.time() + 5
        assert board.systemd is None
        assert board.logread is False

    @pytest.fixture
    def bin(self, tmp_path: Path) -> Path:
        directory = tmp_path / "bin"
        directory.mkdir()
        return directory

    def fake(self, bin: Path, name: str, body: str) -> None:
        path = bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    @pytest.mark.parametrize("shell", SHELLS)
    def test_reads_the_systemd_version(self, bin: Path, shell: str) -> None:
        self.fake(bin, "journalctl", 'echo "systemd 255 (255.4-1)"; echo "+PAM +AUDIT"')

        board = probe.parse(run_script(shell, f"{bin}:/usr/bin:/bin"), 0)

        assert board.systemd == 255

    def test_a_journalctl_that_prints_no_version_is_no_journal(self, bin: Path) -> None:
        self.fake(bin, "journalctl", "exit 1")

        assert probe.parse(run_script("/bin/sh", f"{bin}:/usr/bin:/bin"), 0).systemd is None

    def test_logread_is_tried_not_just_found(self, bin: Path) -> None:
        self.fake(bin, "logread", 'echo "logread: can\'t find syslogd buffer" >&2; exit 1')

        assert probe.parse(run_script("/bin/sh", f"{bin}:/usr/bin:/bin"), 0).logread is False

    def test_a_working_logread_is_found(self, bin: Path) -> None:
        self.fake(bin, "logread", "exit 0")

        assert probe.parse(run_script("/bin/sh", f"{bin}:/usr/bin:/bin"), 0).logread is True

    def test_ends_with_the_end_marker(self) -> None:
        assert run_script("/bin/sh", "/usr/bin:/bin").endswith("end=1\n")


class TestSystemLog:
    def test_journal_history_starts_one_hour_before_the_boards_clock(self) -> None:
        log = system_log(make(systemd=252, clock_s=1_700_000_000), None, "kept")

        assert log.kind == "journal"
        assert log.resumes_at is None
        assert log.command.endswith("-b --no-tail --since=@1699996400")

    def test_history_uses_the_boards_clock_not_this_computers(self) -> None:
        board = make(systemd=252, clock_s=1_000_000_000, host_ns=1_700_000_000_000_000_000)

        assert system_log(board, None, "kept").command.endswith("--since=@999996400")

    def test_history_of_a_board_whose_clock_reads_1970_does_not_go_negative(self) -> None:
        # systemd 247 takes --since=@0 for no --since, and -f then sends only 10 entries.
        assert system_log(make(systemd=247, clock_s=120), None, "kept").command.endswith(
            "-b --no-tail --since=@0"
        )

    def test_history_goes_back_as_far_as_the_agent_keeps_data(self) -> None:
        board = make(systemd=252, clock_s=1_700_000_000)

        log = system_log(board, None, "kept", timedelta(hours=6))

        assert log.command.endswith("-b --no-tail --since=@1699978400")

    def test_an_agent_that_keeps_everything_gets_the_whole_boot(self) -> None:
        log = system_log(make(systemd=252, clock_s=None), None, "kept", None)

        assert log.command.endswith("-b --no-tail")

    def test_new_lines_only(self) -> None:
        log = system_log(make(systemd=252), None, "new")

        assert log.command.endswith(" -n 0")
        assert "--since" not in log.command

    def test_unknown_board_clock_falls_back_to_new_lines(self) -> None:
        log = system_log(make(systemd=252, clock_s=None), None, "kept")

        assert log.command.endswith(" -n 0")

    @pytest.mark.parametrize("systemd", [100, 219, 235])
    def test_output_fields_are_left_out_below_236(self, systemd: int) -> None:
        assert "--output-fields" not in system_log(make(systemd=systemd), None, "new").command

    @pytest.mark.parametrize("systemd", [236, 245, 257])
    def test_output_fields_from_236(self, systemd: int) -> None:
        command = system_log(make(systemd=systemd), None, "new").command

        assert "--output-fields=MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_COMM,_SYSTEMD_UNIT," in command
        assert "CODE_FILE,CODE_LINE,_TRANSPORT" in command

    def test_cursor_resumes_at_the_cursor_and_drops_that_entry(self) -> None:
        log = system_log(make(systemd=252), CURSOR, "kept")

        assert log.kind == "journal"
        assert log.resumes_at == CURSOR
        assert f"--cursor={shlex.quote(CURSOR)}" in log.command
        assert shlex.split(log.command)[-1] == f"--cursor={CURSOR}"
        assert "--after-cursor" not in log.command
        assert "--since" not in log.command
        assert "-n 0" not in log.command

    def test_a_cursor_is_quoted_for_the_shell(self) -> None:
        nasty = "s=a;i=1;b=2'; rm -rf / #"

        words = shlex.split(system_log(make(systemd=252), nasty, "kept").command)

        assert words[-1] == f"--cursor={nasty}"
        assert "rm" not in words

    def test_the_cursor_wins_over_history(self) -> None:
        log = system_log(make(systemd=252, clock_s=1_700_000_000), CURSOR, "kept")

        assert "--since" not in log.command

    def test_the_journal_is_followed_as_json_with_every_field(self) -> None:
        command = system_log(make(systemd=252), None, "new").command

        assert command.startswith("journalctl -f -o json --all ")

    def test_logread(self) -> None:
        log = system_log(make(logread=True, files=("/var/log/messages",)), None, "kept")

        assert (log.kind, log.command, log.resumes_at) == ("logread", "logread -f", None)

    def test_the_journal_wins_over_logread_and_files(self) -> None:
        log = system_log(make(systemd=252, logread=True, files=("/var/log/syslog",)), None, "new")

        assert log.kind == "journal"

    def test_file(self) -> None:
        log = system_log(make(files=("/var/log/messages", "/var/log/syslog")), None, "kept")

        assert (log.kind, log.command) == ("file", "tail -n 0 -F /var/log/messages")

    def test_a_file_name_is_quoted(self) -> None:
        log = system_log(make(files=("/var/log/my log",)), None, "kept")

        assert log.command == "tail -n 0 -F '/var/log/my log'"

    @pytest.mark.parametrize("history", ["kept", "new"])
    def test_a_board_with_no_log(self, history: Literal["kept", "new"]) -> None:
        with pytest.raises(NoLog):
            system_log(make(), CURSOR, history)
