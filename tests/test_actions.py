"""The actions the Action panel, notebooks and Zelos AI run."""

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from zelos_sdk.actions.types import ActionExecutionError, ValidationError

from tests.conftest import TIME, FakeProcess, Processes, Recording, read_events, run
from zelos_extension_logs import actions, config, keys, probe, remote
from zelos_extension_logs.worker import Cursors, SourceWorker

PROBE = (
    "uid=0\nuser=root\ngroups=root\nsystemd=252\n"
    "boot_id=f07dd35d-996b-4983-98f1-e4347c93e456\nuptime=500.00\nclock=1700000000\nend=1\n"
)


@pytest.fixture(autouse=True)
def no_workers() -> Iterator[None]:
    actions.workers.clear()
    yield
    for worker in actions.workers.values():
        worker.stop()
        if worker.is_alive():
            worker.join(5)
    actions.workers.clear()


@pytest.fixture
def add_worker(trace_file: Recording) -> Callable[..., SourceWorker]:
    """A worker registered as app.run() registers them; given scripted processes it has run."""

    def add(item: dict[str, Any], *scripted: FakeProcess) -> SourceWorker:
        cfg = config.parse({"sources": [item]}).sources[0]
        fakes = Processes(*scripted)
        worker = SourceWorker(cfg, trace_file.source, Cursors(None), None, run=fakes)
        actions.workers[cfg.name] = worker
        if scripted:
            worker.start()
            worker.join(5)
        return worker

    return add


class TestDataDir:
    def test_from_the_environment(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))

        assert actions.data_dir() == tmp_path

    @pytest.mark.parametrize("value", [None, ""])
    def test_none_without_it(self, monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("ZELOS_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("ZELOS_DATA_DIR", value)

        assert actions.data_dir() is None


class TestListHosts:
    def test_aliases_with_their_details(self, home: Path) -> None:
        (home / ".ssh" / "config").write_text(
            "Host bench-1\n  HostName 10.0.0.5\n  User root\nHost pump\nHost *\n  User x\n",
            encoding="utf-8",
        )

        assert run("list_hosts") == {
            "status": "success",
            "choices": [
                {"value": "bench-1", "label": "bench-1", "detail": "HostName 10.0.0.5 User root"},
                {"value": "pump", "label": "pump", "detail": None},
            ],
        }

    def test_no_config_says_what_to_type(self, home: Path) -> None:
        assert run("list_hosts") == {
            "status": "success",
            "choices": [],
            "message": "No hosts in ~/.ssh/config. Type [user@]host, such as root@192.168.1.20.",
        }

    def test_a_config_with_only_wildcards(self, home: Path) -> None:
        (home / ".ssh" / "config").write_text("Host *\n  User x\n", encoding="utf-8")

        assert run("list_hosts")["choices"] == []

    def test_included_files_count(self, home: Path) -> None:
        (home / ".ssh" / "boards").write_text("Host bench-2\n", encoding="utf-8")
        (home / ".ssh" / "config").write_text("Include boards\n", encoding="utf-8")

        assert [c["value"] for c in run("list_hosts")["choices"]] == ["bench-2"]

    def test_runs_without_the_extension(self) -> None:
        action = actions.list_hosts._action

        assert (action.standalone, action.read_only) == (True, True)


class TestAutoConfig:
    LOCAL = {"connection": "local", "read": "system"}
    # What a new form holds: one SSH source with nothing typed in, and the defaults it fills in.
    BLANK = {"connection": "ssh", "read": "system", "advanced": {"host_key_policy": "auto"}}

    def test_adds_this_computers_system_log(self) -> None:
        result = run("auto_config")

        assert result["config"] == {"sources": [self.LOCAL]}
        assert result["message"] == "Added this computer's system log."
        assert result["status"] == "success"

    @pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
    def test_on_every_platform_beside_what_there_is(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        ssh = {"connection": "ssh", "host": "root@bench-1"}

        result = run("auto_config", config={"sources": [ssh]})

        assert result["config"] == {"sources": [ssh, self.LOCAL]}
        assert result["message"] == "Added this computer's system log."

    def test_does_not_add_it_twice(self) -> None:
        result = run("auto_config", config={"sources": [{"connection": "local"}]})

        assert result["config"] == {"sources": [{"connection": "local"}]}
        assert result["message"] == "Nothing to add."

    def test_a_local_file_is_not_the_system_log(self) -> None:
        local_file = {"connection": "local", "read": "file", "path": "/var/log/x.log"}

        result = run("auto_config", config={"sources": [local_file]})

        assert result["config"]["sources"] == [local_file, self.LOCAL]

    def test_the_blank_source_of_a_new_form_makes_way_for_the_system_log(self) -> None:
        result = run("auto_config", config={"sources": [self.BLANK]})

        assert result["config"] == {"sources": [self.LOCAL]}

    def test_a_source_with_anything_typed_in_is_kept(self) -> None:
        named = {**self.BLANK, "name": "bench"}

        result = run("auto_config", config={"sources": [named]})

        assert result["config"] == {"sources": [named, self.LOCAL]}

    def test_does_not_change_what_it_was_given(self) -> None:
        given = {"sources": [{"connection": "ssh", "host": "h"}]}

        run("auto_config", config=given)

        assert given == {"sources": [{"connection": "ssh", "host": "h"}]}

    @pytest.mark.parametrize("given", [None, {}, {"sources": None}])
    def test_no_configuration_yet(self, given: dict[str, Any] | None) -> None:
        result = run("auto_config", config=given) if given is not None else run("auto_config")

        assert result["config"] == {"sources": [self.LOCAL]}


class TestShowAccessKey:
    def test_makes_the_key_in_the_data_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))

        result = run("show_access_key")

        assert result["status"] == "success"
        assert result["public_key"].startswith("ssh-ed25519 ")
        assert result["install"] == keys.install_line(result["public_key"])
        assert (
            result["message"] == "Run the install line on the device, in any shell you have there."
        )
        assert (tmp_path / "ssh" / "id_ed25519").is_file()

    def test_shows_the_same_key_every_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))

        assert run("show_access_key")["public_key"] == run("show_access_key")["public_key"]

    def test_without_a_data_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ZELOS_DATA_DIR", raising=False)

        assert run("show_access_key") == {
            "status": "error",
            "message": "The extension has no data directory to keep a key in.",
        }

    def test_a_key_that_cannot_be_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))

        def fail(_data_dir: Path | None) -> Path:
            raise keys.KeyFailure("ssh-keygen is not installed on this computer.")

        monkeypatch.setattr(keys, "ensure", fail)

        assert run("show_access_key") == {
            "status": "error",
            "message": "ssh-keygen is not installed on this computer.",
        }


class Capture:
    """Stands in for `remote.run`: records the call and answers, or times out."""

    def __init__(
        self,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
        returncode: int = 0,
        *,
        times_out: bool = False,
        cut: bool = False,
    ) -> None:
        self.cut = cut
        self.stdout = stdout.encode() if isinstance(stdout, str) else stdout
        self.stderr = stderr.encode() if isinstance(stderr, str) else stderr
        self.returncode = returncode
        self.times_out = times_out
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> remote.Finished | None:
        self.calls.append((argv, kwargs))
        if self.times_out:
            return None
        return remote.Finished(self.returncode, self.stdout, self.stderr, self.cut)


class TestTestConnection:
    def connect(
        self, monkeypatch: pytest.MonkeyPatch, capture: Capture, host: str = "root@bench-1"
    ) -> Any:
        monkeypatch.setattr(remote, "run", capture)
        return run("test_connection", host=host)

    @pytest.mark.usefixtures("ssh")
    def test_a_board_with_a_journal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = Capture(PROBE)

        result = self.connect(monkeypatch, capture)

        assert result == {
            "status": "success",
            "message": "Connected to root@bench-1 as root; it will read the journal (systemd 252).",
            "reads": "journal",
            "user": "root",
        }
        argv, kwargs = capture.calls[0]
        assert argv[-3:] == ["--", "root@bench-1", probe.SCRIPT]
        assert kwargs["timeout"] == probe.TIMEOUT_S

    @pytest.mark.usefixtures("ssh")
    def test_a_user_who_sees_only_their_own_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            PROBE.replace("uid=0", "uid=1000")
            .replace("user=root", "user=probe")
            .replace("groups=root", "groups=probe")
        )

        result = self.connect(monkeypatch, Capture(output))

        assert result["status"] == "success"
        assert result["message"].endswith(
            " probe is not root and not in systemd-journal, so the journal shows only its own "
            "entries. Where the journal is, run: sudo usermod -aG systemd-journal probe"
        )

    @pytest.mark.usefixtures("ssh")
    def test_busybox_logread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = PROBE.replace("systemd=252\n", "logread=1\n")

        result = self.connect(monkeypatch, Capture(output))

        assert result["reads"] == "logread"
        assert result["message"] == (
            "Connected to root@bench-1 as root; it will read BusyBox's logread."
        )

    @pytest.mark.usefixtures("ssh")
    def test_a_syslog_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = PROBE.replace("systemd=252\n", "file=/var/log/messages\n")

        result = self.connect(monkeypatch, Capture(output))

        assert result["reads"] == "file"
        assert result["message"].endswith("it will read /var/log/messages.")

    @pytest.mark.usefixtures("ssh")
    def test_a_board_with_no_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self.connect(monkeypatch, Capture(PROBE.replace("systemd=252\n", "")))

        assert result == {
            "status": "error",
            "message": "Connected to root@bench-1, but it has no system log: no journal, logread "
            "or readable syslog file. Set Log to A file or A command's output.",
        }

    @pytest.mark.usefixtures("ssh")
    def test_a_refused_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = Capture("", "root@bench-1: Permission denied (publickey).", 255)

        result = self.connect(monkeypatch, capture)

        assert result == {
            "status": "error",
            "message": "root@bench-1 refused the login. Run Show Access Key and add its line to "
            "the device, or set Key file.",
        }

    @pytest.mark.usefixtures("ssh")
    def test_no_answer_in_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = Capture(times_out=True)

        assert self.connect(monkeypatch, capture) == {
            "status": "error",
            "message": "root@bench-1 did not answer in time.",
        }

    def test_no_ssh_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "openssh", lambda tool: None)
        capture = Capture(PROBE)

        assert self.connect(monkeypatch, capture) == {
            "status": "error",
            "message": str(remote.SshMissing()),
        }
        assert capture.calls == []

    @pytest.mark.usefixtures("ssh")
    @pytest.mark.parametrize(
        ("host", "message"),
        [
            ("-oProxyCommand=x", "Address: '-oProxyCommand=x' is not an address."),
            ("-J jump", "Address: '-J jump' is not an address."),
            (" ", "Address: enter the device's address, such as me@my-board."),
            ("", "Address: enter the device's address, such as me@my-board."),
        ],
    )
    def test_a_host_that_is_not_one(
        self, monkeypatch: pytest.MonkeyPatch, host: str, message: str
    ) -> None:
        capture = Capture(PROBE)

        result = self.connect(monkeypatch, capture, host)

        assert result == {"status": "error", "message": message}
        assert capture.calls == []

    @pytest.mark.usefixtures("ssh", "home")
    def test_a_sources_ssh_settings_are_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        capture = Capture(PROBE)
        monkeypatch.setattr(remote, "run", capture)
        key = tmp_path / "bench.key"

        result = run(
            "test_connection",
            host="root@bench-1",
            ssh_port=2222,
            key_path=str(key),
            host_key_policy="strict",
            ssh_options="-J jump",
        )

        assert result["status"] == "success"
        argv = capture.calls[0][0]
        assert argv[1:3] == ["-J", "jump"]
        assert argv[argv.index("-p") + 1] == "2222"
        assert argv[argv.index("-i") + 1] == str(key)
        assert "StrictHostKeyChecking=yes" in argv

    @pytest.mark.usefixtures("ssh")
    def test_extra_ssh_options_with_a_quote_not_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = Capture(PROBE)
        monkeypatch.setattr(remote, "run", capture)

        result = run("test_connection", host="root@bench-1", ssh_options='-o "ProxyCommand x')

        assert result == {
            "status": "error",
            "message": "Extra ssh options: a quote is not closed.",
        }
        assert capture.calls == []

    @pytest.mark.usefixtures("ssh")
    def test_the_access_key_in_the_data_directory_is_offered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
    ) -> None:
        monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))
        key = keys.ensure(tmp_path)
        capture = Capture(PROBE)

        self.connect(monkeypatch, capture)

        argv = capture.calls[0][0]
        assert argv[argv.index("-i") + 1] == str(key)


class TestConvertLogFile:
    # Zephyr lines that print only the device's uptime: a file of them ends at its last change.
    TEXT = "[00:00:01.000,000] <inf> dcdc: rail=13.6V\n[00:00:03.000,000] <wrn> bms: cell low\n"
    CHANGED_S = 1_792_600_000

    def saved(self, directory: Path) -> Path:
        path = directory / "app.log"
        path.write_text(self.TEXT, encoding="utf-8")
        os.utime(path, (self.CHANGED_S, self.CHANGED_S))
        return path

    def test_writes_the_trace_beside_the_file(self, tmp_path: Path) -> None:
        path = self.saved(tmp_path)

        assert run("convert_log_file", input_file=str(path)) == {
            "status": "success",
            "input_file": str(path),
            "output_file": str(tmp_path / "app.trz"),
            "lines": 2,
        }
        rows = read_events(tmp_path / "app.trz")["app/log"]
        assert [row[TIME] for row in rows] == [self.CHANGED_S - 2.0, float(self.CHANGED_S)]

    def test_its_options(self, tmp_path: Path) -> None:
        path = self.saved(tmp_path)
        (tmp_path / "run.trz").write_bytes(b"an earlier trace")

        result = run(
            "convert_log_file",
            input_file=str(path),
            output_file=str(tmp_path / "run"),
            overwrite=True,
            time_zone="Europe/Berlin",
            year=2025,
            start_time="2026-10-21T14:03:01+02:00",
        )

        assert result["output_file"] == str(tmp_path / "run.trz")
        rows = read_events(tmp_path / "run.trz")["app/log"]
        assert [row[TIME] for row in rows] == [1_792_584_181.0, 1_792_584_183.0]

    def test_an_existing_output_is_kept(self, tmp_path: Path) -> None:
        path = self.saved(tmp_path)
        (tmp_path / "app.trz").write_bytes(b"an earlier trace")

        with pytest.raises(ActionExecutionError, match="The output exists: "):
            run("convert_log_file", input_file=str(path))

        assert (tmp_path / "app.trz").read_bytes() == b"an earlier trace"

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ActionExecutionError, match="No such file or directory"):
            run("convert_log_file", input_file=str(tmp_path / "gone.log"))

    @pytest.mark.parametrize(
        ("field", "value", "problem"),
        [
            ("time_zone", "Mars/Base", "'Mars/Base' is not an IANA time zone"),
            ("start_time", "yesterday", "'yesterday' is not an ISO 8601 time with an offset"),
        ],
    )
    def test_timing_it_cannot_read(
        self, tmp_path: Path, field: str, value: str, problem: str
    ) -> None:
        path = self.saved(tmp_path)

        with pytest.raises(ActionExecutionError, match=problem):
            run("convert_log_file", input_file=str(path), **{field: value})

        assert not (tmp_path / "app.trz").exists()

    def test_a_year_out_of_range(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            run("convert_log_file", input_file=str(self.saved(tmp_path)), year=26)

    def test_its_fields(self) -> None:
        action = actions.convert_log_file._action
        schema = action.to_schema()
        ui = action.to_ui_schema()

        assert list(schema["properties"]) == [
            "input_file",
            "output_file",
            "overwrite",
            "time_zone",
            "year",
            "start_time",
        ]
        assert schema["required"] == ["input_file"]
        assert schema["properties"]["year"]["minimum"] == 1970
        assert schema["properties"]["year"]["maximum"] == 2261
        assert ui["input_file"]["ui:widget"] == "FilePathPickerWidget"
        assert ui["output_file"]["ui:widget"] == "FilePathPickerWidget"
        assert ui["overwrite"]["ui:widget"] == "ToggleWidget"

    def test_runs_without_the_extension_for_up_to_half_an_hour(self) -> None:
        action = actions.convert_log_file._action

        assert (action.standalone, action.read_only, action.timeout) == (True, False, 1800.0)


def python(code: str) -> str:
    """A command line that runs `code` in this Python, in sh and in cmd alike."""
    return f'"{sys.executable}" -c "{code}"'


class TestRunCommand:
    def test_no_workers(self) -> None:
        assert run("run_command", command="echo hi") == {
            "status": "error",
            "message": "No sources are running. Start the extension.",
        }

    def test_an_unknown_name_names_the_known_ones(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev-2"})
        add_worker({"connection": "demo", "name": "demo-1"})

        assert actions.run_command("nope", command="echo hi") == {
            "status": "error",
            "message": "No source named 'nope'. Sources: demo-1, dev-2.",
        }

    def test_the_sdk_refuses_an_unknown_name_before_the_action_runs(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        with pytest.raises(ValidationError):
            run("run_command", source="nope", command="echo hi")

    def test_the_demo_runs_no_commands(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "demo"})

        assert run("run_command", command="echo hi") == {
            "status": "error",
            "message": "The demo device runs no commands.",
        }

    def test_a_local_worker_runs_echo_hi(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "dev"})

        assert run("run_command", command="echo hi") == {
            "status": "success",
            "exit_code": 0,
            "output": ["hi"],
            "truncated": False,
        }

    def test_the_first_source_is_the_default_and_a_name_picks_another(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "demo", "name": "demo-1"})
        add_worker({"connection": "local", "name": "dev"})

        assert (
            run("run_command", command="echo hi")["message"] == "The demo device runs no commands."
        )
        assert run("run_command", source="dev", command="echo hi")["output"] == ["hi"]

    def test_the_exit_code_and_what_was_printed(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        line = "echo out& exit 3" if sys.platform == "win32" else "echo out; exit 3"

        result = run("run_command", command=line)

        assert result["status"] == "success"
        assert result["exit_code"] == 3
        assert result["output"] == ["out"]

    def test_what_a_command_prints_to_stderr_is_in_the_output(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        result = run("run_command", command="echo err>&2")

        assert result["output"] == ["err"]

    def test_stderr_of_every_statement_is_in_the_output(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        line = "echo err>&2& echo out" if sys.platform == "win32" else "echo err >&2; echo out"

        result = run("run_command", command=line)

        assert sorted(result["output"]) == ["err", "out"]

    def test_a_missing_command_is_the_shells_exit_code_not_an_error(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        result = run("run_command", command="no-such-command-for-bench")

        if sys.platform == "win32":
            assert result["exit_code"] == 1
            assert "is not recognized" in result["output"][0]
        else:
            assert result["exit_code"] == 127
            assert "not found" in result["output"][0]

    def test_output_is_cut_to_the_last_two_thousand_lines(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        result = run("run_command", command=python("print(*range(1, 2501), sep=chr(10))"))

        assert len(result["output"]) == 2000
        assert result["output"][0] == "501"
        assert result["output"][-1] == "2500"
        assert result["truncated"] is True

    def test_output_cut_by_its_length_starts_at_a_whole_line(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})
        # What run keeps of a long output starts wherever a read ended, mid-line.
        monkeypatch.setattr(remote, "run", Capture("998\n00999\n01000\n", cut=True))

        result = run("run_command", command="seq 1000")

        assert result["output"] == ["00999", "01000"]
        assert result["truncated"] is True

    def test_bytes_that_are_not_text_are_replaced(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        line = python("import sys; sys.stdout.buffer.write(bytes([97, 255, 98, 10]))")

        assert run("run_command", command=line)["output"] == ["a�b"]

    def test_a_command_that_runs_too_long(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "dev"})

        result = run("run_command", command=python("import time; time.sleep(30)"), timeout_s=1.0)

        assert result == {"status": "error", "message": "The command did not finish in 1 s."}

    @pytest.mark.skipif(sys.platform == "win32", reason="pgrep")
    def test_a_command_that_runs_too_long_leaves_nothing_running(
        self, add_worker: Callable[..., SourceWorker]
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})
        marker = f"sleep 30.{os.getpid()}"

        run("run_command", command=f"{marker}; echo done", timeout_s=1.0)
        running = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        subprocess.run(["pkill", "-f", marker], check=False)

        assert running.stdout == ""

    @pytest.mark.parametrize("timeout_s", [0.5, 0.0, 121.0, -1.0])
    def test_a_timeout_out_of_range(
        self, add_worker: Callable[..., SourceWorker], timeout_s: float
    ) -> None:
        add_worker({"connection": "local", "name": "dev"})

        # The SDK's own range check comes first when run as the agent runs it.
        assert actions.run_command(command="echo hi", timeout_s=timeout_s) == {
            "status": "error",
            "message": "timeout_s must be 1.0 to 120.0.",
        }
        with pytest.raises(ValidationError):
            run("run_command", command="echo hi", timeout_s=timeout_s)

    @pytest.mark.usefixtures("ssh")
    def test_over_ssh_the_command_runs_on_the_sources_host(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "ssh", "host": "root@bench-1", "name": "bench-1"})
        capture = Capture("Linux bench-1\n")
        monkeypatch.setattr(remote, "run", capture)

        result = run("run_command", command="uname -a", timeout_s=7.0)

        assert result == {
            "status": "success",
            "exit_code": 0,
            "output": ["Linux bench-1"],
            "truncated": False,
        }
        argv, kwargs = capture.calls[0]
        assert argv[-3:] == ["--", "root@bench-1", remote.wrap("uname -a")]
        assert kwargs == {"timeout": 7.0, "hold_stdin": True, "stderr": subprocess.PIPE}

    @pytest.mark.usefixtures("ssh")
    def test_over_ssh_a_failed_connection_is_explained(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "ssh", "host": "root@bench-1", "name": "bench-1"})
        monkeypatch.setattr(
            remote,
            "run",
            Capture("", "ssh: connect to host bench-1: Connection refused", 255),
        )

        assert run("run_command", command="uname") == {
            "status": "error",
            "message": "root@bench-1 refused the connection. Check that an SSH server runs there.",
        }

    @pytest.mark.usefixtures("ssh")
    def test_over_ssh_the_commands_own_255_is_taken_for_a_connection_failure(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "ssh", "host": "root@bench-1", "name": "bench-1"})
        monkeypatch.setattr(remote, "run", Capture("", "", 255))

        assert run("run_command", command="exit 255")["status"] == "error"

    def test_over_ssh_with_no_client(
        self, add_worker: Callable[..., SourceWorker], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_worker({"connection": "ssh", "host": "root@bench-1", "name": "bench-1"})
        monkeypatch.setattr(remote, "openssh", lambda tool: None)

        assert run("run_command", command="uname") == {
            "status": "error",
            "message": str(remote.SshMissing()),
        }

    def test_is_not_read_only(self) -> None:
        action = actions.run_command._action

        assert action.read_only is False
        assert action.timeout == 125.0


class TestGetState:
    def test_no_workers(self) -> None:
        assert run("get_state") == {
            "status": "error",
            "message": "No sources are running. Start the extension.",
        }

    def test_an_unknown_name(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "dev"})

        assert actions.get_state("nope") == {
            "status": "error",
            "message": "No source named 'nope'. Sources: dev.",
        }

    def test_a_workers_fields(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker(
            {"connection": "local", "read": "command", "command": "tail", "name": "dev"},
            FakeProcess("one\ntwo\n"),
        )

        result = run("get_state", source="dev")

        assert result["status"] == "success"
        assert result["name"] == "dev"
        assert result["connection"] == "local"
        assert result["read"] == "command"
        assert result["host"] is None
        assert result["state"] == "ended"
        assert result["reading"] == "command"
        assert result["lines"] == 2
        assert (
            result["message"] == "The command finished. It runs again when the extension restarts."
        )
        assert set(result) == {
            "status", "name", "connection", "host", "read", "state", "reading", "message",
            "lines", "reconnects", "gaps", "boot_id", "last_line_ns", "long_lines", "late_names",
            "errors", "signals",
        }  # fmt: skip

    def test_the_first_source_is_the_default(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "first"})
        add_worker({"connection": "demo", "name": "second"})

        assert run("get_state")["name"] == "first"
        assert run("get_state", source="second")["name"] == "second"

    def test_is_read_only_and_needs_the_running_extension(self) -> None:
        action = actions.get_state._action

        assert (action.read_only, action.standalone) == (True, False)


class TestSample:
    def lines(self, add_worker: Callable[..., SourceWorker], count: int) -> None:
        text = "".join(f"line {n}\n" for n in range(count))
        add_worker(
            {"connection": "local", "read": "command", "command": "tail", "name": "dev"},
            FakeProcess(text),
        )

    def test_the_last_fifty_by_default(self, add_worker: Callable[..., SourceWorker]) -> None:
        self.lines(add_worker, 120)

        result = run("sample")

        assert result["status"] == "success"
        assert len(result["lines"]) == 50
        assert result["lines"][0] == "line 70"
        assert result["lines"][-1] == "line 119"

    def test_a_number_of_lines(self, add_worker: Callable[..., SourceWorker]) -> None:
        self.lines(add_worker, 20)

        assert run("sample", lines=3)["lines"] == ["line 17", "line 18", "line 19"]

    def test_fewer_lines_than_asked(self, add_worker: Callable[..., SourceWorker]) -> None:
        self.lines(add_worker, 2)

        assert run("sample", lines=10)["lines"] == ["line 0", "line 1"]

    def test_a_worker_with_nothing_yet(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "dev"})

        assert run("sample") == {"status": "success", "lines": []}

    def test_no_workers(self) -> None:
        assert run("sample")["message"] == "No sources are running. Start the extension."

    def test_an_unknown_name(self, add_worker: Callable[..., SourceWorker]) -> None:
        add_worker({"connection": "local", "name": "dev"})

        assert actions.sample("nope") == {
            "status": "error",
            "message": "No source named 'nope'. Sources: dev.",
        }

    @pytest.mark.parametrize("lines", [0, -1, 1001])
    def test_a_count_out_of_range(self, lines: int) -> None:
        assert actions.sample(lines=lines) == {
            "status": "error",
            "message": "lines must be 1 to 1000.",
        }
        with pytest.raises(ValidationError):
            run("sample", lines=lines)


def test_the_source_field_defaults_to_the_first_source(
    add_worker: Callable[..., SourceWorker],
) -> None:
    assert actions.get_state._action.to_schema()["properties"]["source"]["default"] is None
    add_worker({"connection": "local", "name": "first"})
    add_worker({"connection": "demo", "name": "second"})

    schema = actions.get_state._action.to_schema()["properties"]["source"]

    assert schema["default"] == "first"
    assert schema["enum"] == ["first", "second"]


def test_which_actions_run_without_the_extension() -> None:
    standalone = {name for name, function in _registered().items() if function._action.standalone}

    assert standalone == {
        "list_hosts",
        "auto_config",
        "test_connection",
        "show_access_key",
        "convert_log_file",
    }


def test_every_action_has_its_flags_decided() -> None:
    """Pins each action's flags, so a new action or a changed flag is a deliberate choice."""
    flags = {
        name: (function._action.read_only, function._action.hidden)
        for name, function in _registered().items()
    }

    assert flags == {
        "list_hosts": (True, True),
        "auto_config": (True, True),
        "test_connection": (False, False),
        "show_access_key": (False, False),
        "convert_log_file": (False, False),
        "run_command": (False, False),
        "get_state": (True, True),
        "sample": (True, True),
    }


def _registered() -> dict[str, Any]:
    return {name: obj for name, obj in vars(actions).items() if hasattr(obj, "_action")}
