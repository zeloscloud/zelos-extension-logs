"""Configuration: defaults, default names and each validation sentence."""

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from zelos_log_lines.placing import Timing

from zelos_extension_logs import config
from zelos_extension_logs.config import ConfigError


def parse_sources(*sources: dict[str, Any], **advanced: Any) -> config.Settings:
    raw: dict[str, Any] = {"sources": list(sources)}
    if advanced:
        raw["advanced"] = advanced
    return config.parse(raw)


def problems(*sources: dict[str, Any], **advanced: Any) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        parse_sources(*sources, **advanced)
    return caught.value.problems


def names(*sources: dict[str, Any]) -> list[str]:
    return [cfg.name for cfg in parse_sources(*sources).sources]


class TestDefaults:
    def test_ssh_system_log(self) -> None:
        (cfg,) = parse_sources({"connection": "ssh", "host": " root@bench-1 "}).sources

        assert cfg == config.SourceConfig(
            name="bench-1",
            connection="ssh",
            read="system",
            host="root@bench-1",
            path=None,
            command=None,
            ssh_port=None,
            key_path=None,
            host_key_policy="auto",
            ssh_options=(),
            history="kept",
            time_source="device",
            values=False,
            timing=None,
        )

    def test_local_file(self) -> None:
        (cfg,) = parse_sources(
            {"connection": "local", "read": "file", "path": "/var/log/acme/run.log"}
        ).sources

        assert (cfg.connection, cfg.read, cfg.host, cfg.path, cfg.command) == (
            "local",
            "file",
            None,
            "/var/log/acme/run.log",
            None,
        )
        assert cfg.history == "kept"
        assert cfg.time_source == "device"
        assert cfg.host_key_policy == "auto"

    def test_command_over_ssh(self) -> None:
        (cfg,) = parse_sources(
            {"connection": "ssh", "host": "bench-1", "read": "command", "command": "dmesg -w"}
        ).sources

        assert (cfg.read, cfg.command, cfg.path) == ("command", "dmesg -w", None)

    def test_the_demo_reads_its_system_log(self) -> None:
        (cfg,) = parse_sources({"connection": "demo"}).sources

        assert (cfg.connection, cfg.read, cfg.path, cfg.name) == ("demo", "system", None, "demo")

    @pytest.mark.parametrize("read", ["file", "command", "replay"])
    def test_the_demo_offers_nothing_but_its_system_log(self, read: str) -> None:
        assert problems({"connection": "demo", "read": read, "path": "/x", "command": "ls"}) == [
            "Source 1, Read: choose one of system."
        ]

    @pytest.mark.parametrize("read", ["file", "replay"])
    def test_a_file_path_is_trimmed(self, read: str) -> None:
        (cfg,) = parse_sources(
            {"connection": "local", "read": read, "path": " /var/log/x.log "}
        ).sources

        assert cfg.path == "/var/log/x.log"

    def test_path_and_command_apply_only_to_their_read(self) -> None:
        (cfg,) = parse_sources(
            {"connection": "ssh", "host": "h", "read": "system", "path": "/x", "command": "ls"}
        ).sources

        assert (cfg.path, cfg.command) == (None, None)

    def test_advanced_ssh_options(self) -> None:
        (cfg,) = parse_sources(
            {
                "connection": "ssh",
                "host": "bench-1",
                "advanced": {
                    "ssh_port": 2222,
                    "key_path": "/keys/bench",
                    "host_key_policy": "strict",
                    "ssh_options": '-J jump -o "ProxyCommand ssh -W %h:%p other"',
                    "history": "new",
                    "time_source": "arrival",
                },
            }
        ).sources

        assert cfg.ssh_port == 2222
        assert cfg.key_path == "/keys/bench"
        assert cfg.host_key_policy == "strict"
        assert cfg.ssh_options == ("-J", "jump", "-o", "ProxyCommand ssh -W %h:%p other")
        assert cfg.history == "new"
        assert cfg.time_source == "arrival"

    def test_empty_key_path_is_no_key(self) -> None:
        (cfg,) = parse_sources(
            {"connection": "ssh", "host": "h", "advanced": {"key_path": ""}}
        ).sources

        assert cfg.key_path is None

    def test_settings_defaults(self) -> None:
        settings = parse_sources({"connection": "demo"})

        assert (settings.prefix, settings.log_level) == ("Logs", "INFO")

    def test_settings_are_read_and_the_prefix_trimmed(self) -> None:
        settings = parse_sources({"connection": "demo"}, prefix="Boards ", log_level="DEBUG")

        assert (settings.prefix, settings.log_level) == ("Boards", "DEBUG")

    def test_examples_in_the_schema_parse(self) -> None:
        schema = json.loads(
            (Path(config.__file__).parent.parent / "config.schema.json").read_text(encoding="utf-8")
        )
        for example in schema["examples"]:
            assert config.parse(example).sources


class TestValues:
    """`values` auto is off for the system log and on for files and commands."""

    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            ({"connection": "ssh", "host": "h"}, False),
            ({"connection": "ssh", "host": "h", "read": "file", "path": "/x"}, True),
            ({"connection": "ssh", "host": "h", "read": "command", "command": "ls"}, True),
            ({"connection": "local", "read": "system"}, False),
            ({"connection": "local", "read": "file", "path": "/x"}, True),
            ({"connection": "local", "read": "command", "command": "ls"}, True),
            ({"connection": "local", "read": "replay", "path": "/x"}, True),
        ],
    )
    def test_auto(self, item: dict[str, Any], expected: bool) -> None:
        (cfg,) = parse_sources({**item, "advanced": {"values": "auto"}}).sources

        assert cfg.values is expected

    def test_the_demo_prints_values_as_signals(self) -> None:
        (cfg,) = parse_sources({"connection": "demo"}).sources

        assert cfg.values is True

    @pytest.mark.parametrize("read", ["system", "file", "command"])
    def test_on_and_off_override_the_read(self, read: str) -> None:
        extra = {"system": {}, "file": {"path": "/x"}, "command": {"command": "ls"}}[read]
        for setting, expected in (("on", True), ("off", False)):
            (cfg,) = parse_sources(
                {
                    "connection": "ssh",
                    "host": "h",
                    "read": read,
                    **extra,
                    "advanced": {"values": setting},
                }
            ).sources

            assert cfg.values is expected


class TestDefaultNames:
    def test_host_name_is_made_safe(self) -> None:
        assert names({"connection": "ssh", "host": "root@bench.local"}) == ["bench_local"]

    def test_host_without_a_user(self) -> None:
        assert names({"connection": "ssh", "host": "bench-1"}) == ["bench-1"]

    def test_the_last_at_sign_splits_the_user(self) -> None:
        assert names({"connection": "ssh", "host": "a@b@bench-2"}) == ["bench-2"]

    def test_file_name_is_the_host_and_the_file_stem(self) -> None:
        item = {"connection": "ssh", "host": "root@bench.local", "read": "file"}

        assert names({**item, "path": "/var/log/acme/run.log"}) == ["bench_local run"]

    def test_windows_path_stem(self) -> None:
        item = {"connection": "local", "read": "file", "path": "C:\\logs\\pump.txt"}

        assert names(item) == ["local pump"]

    @pytest.mark.parametrize("read", ["file", "replay"])
    @pytest.mark.parametrize(
        ("path", "name"),
        [
            ("/var/log/syslog.2.gz", "local syslog_2"),
            ("/var/log/app.log.gz", "local app"),
            ("/var/log/app.log.1", "local app_log_1"),
        ],
    )
    def test_a_file_is_named_as_its_converted_trace_names_it(
        self, read: str, path: str, name: str
    ) -> None:
        assert names({"connection": "local", "read": read, "path": path}) == [name]

    def test_command_name_is_the_host_and_the_program(self) -> None:
        item = {"connection": "local", "read": "command", "command": "/usr/bin/adb logcat -v time"}

        assert names(item) == ["local adb"]

    def test_local_system_and_demo(self) -> None:
        assert names({"connection": "local"}, {"connection": "demo"}) == ["local", "demo"]

    def test_duplicates_get_a_numbered_suffix(self) -> None:
        item = {"connection": "ssh", "host": "root@bench.local"}

        assert names(item, item, item) == ["bench_local", "bench_local_2", "bench_local_3"]

    def test_a_default_never_takes_a_name_you_set(self) -> None:
        explicit = {"connection": "ssh", "host": "root@acme-app", "name": "Pump"}

        assert names(explicit, {"connection": "ssh", "host": "root@pump"}) == ["Pump", "pump_2"]

    def test_a_name_you_set_is_trimmed_and_kept(self) -> None:
        assert names({"connection": "ssh", "host": "h", "name": "  bench-1 run "}) == [
            "bench-1 run"
        ]

    def test_long_names_fit_with_room_for_a_program(self) -> None:
        (name,) = names({"connection": "ssh", "host": "a" * 200})

        assert 0 < len(name.encode()) <= 64

    @given(count=st.integers(min_value=1, max_value=12), host=st.text(max_size=30))
    def test_defaults_are_unique_ignoring_case(self, count: int, host: str) -> None:
        item = {"connection": "ssh", "host": f"root@h{host.strip()}".replace(" ", "")}
        try:
            settings = parse_sources(*[item] * count)
        except ConfigError:
            return

        folded = [cfg.name.casefold() for cfg in settings.sources]
        assert len(set(folded)) == count
        assert all(folded)


class TestValidation:
    def test_no_sources(self) -> None:
        for raw in ({}, {"sources": []}, {"sources": None}, None, []):
            with pytest.raises(ConfigError) as caught:
                config.parse(raw)

            assert caught.value.problems == ["Add at least one source."]

    def test_missing_host(self) -> None:
        assert problems({"connection": "ssh"}) == [
            "Source 1, Host: enter [user@]host or a Host from your SSH config."
        ]

    def test_blank_host(self) -> None:
        assert problems({"connection": "ssh", "host": "   "}) == [
            "Source 1, Host: enter [user@]host or a Host from your SSH config."
        ]

    @pytest.mark.parametrize("host", ["-oProxyCommand=x", "  -J bastion", "a b", "root@bench 1"])
    def test_host_that_ssh_would_read_as_an_option_or_as_two_words(self, host: str) -> None:
        assert problems({"connection": "ssh", "host": host}) == [
            f"Source 1, Host: '{host}' is not a host name."
        ]

    def test_missing_file_path(self) -> None:
        assert problems({"connection": "local", "read": "file"}) == [
            "Source 1, File: enter the file to follow."
        ]
        assert problems({"connection": "local", "read": "file", "path": "  "}) == [
            "Source 1, File: enter the file to follow."
        ]

    def test_missing_command(self) -> None:
        assert problems({"connection": "local", "read": "command"}) == [
            "Source 1, Command: enter the command to run."
        ]
        assert problems({"connection": "local", "read": "command", "command": " "}) == [
            "Source 1, Command: enter the command to run."
        ]

    def test_unclosed_quote_in_ssh_options(self) -> None:
        item = {"connection": "ssh", "host": "h", "advanced": {"ssh_options": '-o "ProxyCommand'}}

        assert problems(item) == ["Source 1, Extra ssh options: a quote is not closed."]

    def test_bad_connection(self) -> None:
        assert problems({"connection": "serial"}) == [
            "Source 1, Connection: choose one of ssh, local, demo."
        ]
        assert problems({}) == ["Source 1, Connection: choose one of ssh, local, demo."]

    def test_bad_read(self) -> None:
        assert problems({"connection": "local", "read": "tail"}) == [
            "Source 1, Read: choose one of system, file, command, replay."
        ]

    def test_source_that_is_not_an_object(self) -> None:
        assert problems("bench-1") == [  # type: ignore[arg-type]
            "Source 1: must be a group of settings."
        ]

    def test_sources_that_is_not_a_list(self) -> None:
        with pytest.raises(ConfigError) as caught:
            config.parse({"sources": {"connection": "demo"}})

        assert caught.value.problems == ["Sources must be a list."]

    def test_each_source_is_named_by_its_position(self) -> None:
        assert problems(
            {"connection": "demo"},
            {"connection": "ssh"},
            {"connection": "local", "read": "command"},
        ) == [
            "Source 2, Host: enter [user@]host or a Host from your SSH config.",
            "Source 3, Command: enter the command to run.",
        ]

    def test_name_with_characters_the_trace_cannot_hold(self) -> None:
        assert problems({"connection": "ssh", "host": "h", "name": "a/b"}) == [
            "Source 1, Name: use letters, digits, spaces, _ and - only."
        ]

    def test_prefix_must_hold_a_letter_or_digit(self) -> None:
        assert problems({"connection": "demo"}, prefix="  ") == [
            "Advanced (all sources), Prefix: must not be empty."
        ]

    def test_names_that_differ_only_in_case_are_rejected(self) -> None:
        assert problems(
            {"connection": "ssh", "host": "a", "name": "Pump"},
            {"connection": "ssh", "host": "b", "name": "pump"},
        ) == ["Source 2: name 'pump' is already used by source 1."]

    def test_the_same_name_twice_is_rejected(self) -> None:
        assert problems(
            {"connection": "local", "name": "bench-1"},
            {"connection": "demo"},
            {"connection": "local", "name": "bench-1"},
        ) == ["Source 3: name 'bench-1' is already used by source 1."]

    @pytest.mark.parametrize(
        ("item", "problem"),
        [
            (
                {"connection": "ssh", "host": "h", "advanced": {"ssh_options": 5}},
                "Source 1, Extra ssh options: must be text.",
            ),
            (
                {"connection": "ssh", "host": "h", "advanced": 5},
                "Source 1, Advanced: must be a group of settings.",
            ),
            (
                {"connection": "ssh", "host": "h", "advanced": {"ssh_port": 70000}},
                "Source 1, SSH port: must be from 1 to 65535.",
            ),
            (
                {"connection": "ssh", "host": "h", "advanced": {"host_key_policy": "maybe"}},
                "Source 1, Host key check: choose one of auto, strict.",
            ),
            ({"connection": "ssh", "host": 5}, "Source 1, Host: must be text."),
        ],
    )
    def test_a_rejected_value_is_named_by_its_field(
        self, item: dict[str, Any], problem: str
    ) -> None:
        assert problems(item) == [problem]

    def test_a_setting_the_connection_does_not_have_is_ignored(self) -> None:
        (cfg,) = parse_sources(
            {"connection": "local", "host": 5, "advanced": {"ssh_options": 5, "ssh_port": 0}}
        ).sources

        assert (cfg.host, cfg.ssh_options, cfg.ssh_port) == (None, (), None)

    def test_an_unknown_setting_for_all_sources(self) -> None:
        assert problems({"connection": "demo"}, verbose=1) == [
            "Advanced (all sources): unknown setting verbose."
        ]


class TestReplay:
    LOCAL = {"connection": "local", "read": "replay", "path": "/var/log/app.log"}

    def test_a_saved_file_on_this_computer(self) -> None:
        (cfg,) = parse_sources(self.LOCAL).sources

        assert (cfg.connection, cfg.read, cfg.path, cfg.command, cfg.name) == (
            "local",
            "replay",
            "/var/log/app.log",
            None,
            "local app",
        )
        assert cfg.timing == Timing()

    def test_its_timing(self) -> None:
        (cfg,) = parse_sources(
            {
                **self.LOCAL,
                "advanced": {
                    "time_zone": " Europe/Berlin ",
                    "year": 2025,
                    "start_time": "2026-10-21T14:03:01+02:00",
                    "values": "off",
                },
            }
        ).sources

        assert cfg.timing == Timing(ZoneInfo("Europe/Berlin"), 2025, 1_792_584_181_000_000_000)
        assert cfg.values is False

    def test_blank_timing_is_left_to_the_file(self) -> None:
        (cfg,) = parse_sources(
            {**self.LOCAL, "advanced": {"time_zone": "", "start_time": "  "}}
        ).sources

        assert cfg.timing == Timing()

    def test_timing_applies_to_a_replay_only(self) -> None:
        (cfg,) = parse_sources(
            {
                "connection": "local",
                "read": "file",
                "path": "/var/log/app.log",
                "advanced": {"time_zone": "Mars/Base"},
            }
        ).sources

        assert cfg.timing is None

    def test_the_live_settings_are_not_a_replays(self) -> None:
        live = {"history": "new", "time_source": "arrival"}
        (cfg,) = parse_sources({**self.LOCAL, "advanced": live}).sources

        assert (cfg.history, cfg.time_source) == ("kept", "device")

    @pytest.mark.parametrize("read", [None, "system", "file"])
    def test_the_live_settings_are_checked_for_every_other_read(self, read: str | None) -> None:
        item = {"connection": "local", "path": "/var/log/app.log", "advanced": {"history": "all"}}
        if read:
            item["read"] = read

        assert problems(item) == ["Source 1, History: choose one of kept, new."]

    def test_the_file_is_required(self) -> None:
        for item in (
            {"connection": "local", "read": "replay"},
            {**self.LOCAL, "path": ""},
            {**self.LOCAL, "path": "  "},
        ):
            assert problems(item) == ["Source 1, File: enter the file to replay."]

    def test_on_this_computer_only(self) -> None:
        item = {"connection": "ssh", "host": "root@bench-1"}

        assert problems({**item, "read": "replay", "path": "/var/log/app.log"}) == [
            "Source 1, Read: choose one of system, file, command."
        ]

    @pytest.mark.parametrize(
        ("advanced", "problem"),
        [
            (
                {"time_zone": "Mars/Base"},
                "Source 1, Time zone: 'Mars/Base' is not an IANA time zone, such as Europe/Berlin.",
            ),
            (
                {"start_time": "2026-10-21 14:03"},
                "Source 1, Start time: '2026-10-21 14:03' is not an ISO 8601 time with an "
                "offset, such as 2026-10-21T14:03:01+02:00.",
            ),
            (
                {"start_time": "3026-10-21T14:03:01Z"},
                "Source 1, Start time: '3026-10-21T14:03:01Z' is not from 1970 to 2261.",
            ),
            ({"year": 26}, "Source 1, Year: must be from 1970 to 2261."),
            ({"year": "2025"}, "Source 1, Year: must be a whole number."),
        ],
    )
    def test_timing_it_cannot_read(self, advanced: dict[str, Any], problem: str) -> None:
        assert problems({**self.LOCAL, "advanced": advanced}) == [problem]


class TestLoad:
    def test_reads_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"sources": [{"connection": "demo"}]}), encoding="utf-8")

        assert [cfg.name for cfg in config.load(path).sources] == ["demo"]

    def test_default_path_comes_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "elsewhere.json"
        path.write_text(json.dumps({"sources": [{"connection": "local"}]}), encoding="utf-8")
        monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))

        assert [cfg.name for cfg in config.load().sources] == ["local"]

    def test_missing_file_asks_for_a_source(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            config.load(tmp_path / "absent.json")

        assert caught.value.problems == ["Add at least one source."]

    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ConfigError) as caught:
            config.load(path)

        assert caught.value.problems[0].startswith("The configuration is not valid JSON: ")

    def test_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_bytes(b"\xff\xfe\x00{")

        with pytest.raises(ConfigError) as caught:
            config.load(path)

        assert caught.value.problems == [f"The configuration file {path} is not UTF-8 text."]

    def test_unreadable_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            config.load(tmp_path)

        assert caught.value.problems[0].startswith(
            f"The configuration file {tmp_path} cannot be read: "
        )

    def test_the_error_joins_its_problems(self) -> None:
        error = ConfigError(["one", "two"])

        assert str(error) == "one; two"
