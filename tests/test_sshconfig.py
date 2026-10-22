"""The Host picker's aliases, read from an SSH config."""

from pathlib import Path

import pytest

from zelos_extension_logs.sshconfig import Host, hosts


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def aliases(config: Path) -> list[str]:
    return [host.alias for host in hosts(config)]


class TestAliases:
    def test_in_file_order_with_user_and_host_name(self, home: Path) -> None:
        config = write(
            home / ".ssh" / "config",
            """\
# the bench
Host bench-1
    HostName 10.0.0.5
    User root

Host acme-app
    User deploy
    HostName acme.example.org
    Port 2222

Host pump
""",
        )

        assert hosts(config) == [
            Host("bench-1", "HostName 10.0.0.5 User root"),
            Host("acme-app", "User deploy HostName acme.example.org"),
            Host("pump", ""),
        ]

    def test_several_aliases_on_one_line_share_the_block(self, home: Path) -> None:
        config = write(home / "c", "Host bench-1 bench-2\n  HostName 10.0.0.5\n")

        assert hosts(config) == [
            Host("bench-1", "HostName 10.0.0.5"),
            Host("bench-2", "HostName 10.0.0.5"),
        ]

    def test_wildcards_and_negations_are_skipped(self, home: Path) -> None:
        config = write(
            home / "c",
            "Host *\n  User root\nHost *.local ?ench !skip\nHost bench-1 *.lab\n  User pi\n",
        )

        assert hosts(config) == [Host("bench-1", "User pi")]

    def test_options_under_a_wildcard_do_not_reach_an_alias_before_it(self, home: Path) -> None:
        config = write(home / "c", "Host bench-1\nHost *\n  User root\n  HostName x\n")

        assert hosts(config) == [Host("bench-1", "")]

    def test_match_blocks_end_the_host_above_them(self, home: Path) -> None:
        config = write(
            home / "c",
            "Host bench-1\n  User root\nMatch host *.lab\n  User other\n  HostName other\n"
            "Host pump\n  HostName 10.0.0.9\n",
        )

        assert hosts(config) == [Host("bench-1", "User root"), Host("pump", "HostName 10.0.0.9")]

    def test_keywords_ignore_case_and_may_use_an_equals_sign(self, home: Path) -> None:
        config = write(home / "c", "HOST=bench-1\n  hostname = 10.0.0.5\n  USER root\n")

        assert hosts(config) == [Host("bench-1", "HostName 10.0.0.5 User root")]

    def test_the_first_value_of_an_option_wins_as_in_ssh(self, home: Path) -> None:
        config = write(home / "c", "Host bench-1\n  User first\n  User second\n")

        assert hosts(config) == [Host("bench-1", "User first")]

    def test_a_host_listed_twice_appears_once_with_both_blocks(self, home: Path) -> None:
        config = write(
            home / "c", "Host bench-1\n  User root\nHost pump\nHost bench-1\n  HostName h\n"
        )

        assert hosts(config) == [Host("bench-1", "User root HostName h"), Host("pump", "")]

    def test_comments_and_blank_lines(self, home: Path) -> None:
        config = write(home / "c", "\n\n# Host hidden\n   # Host hidden-too\nHost shown\n")

        assert aliases(config) == ["shown"]

    def test_a_file_that_is_not_there(self, home: Path) -> None:
        assert hosts(home / "absent") == []

    def test_a_directory_in_place_of_the_file(self, home: Path) -> None:
        assert hosts(home) == []

    def test_bytes_that_are_not_text_do_not_stop_the_read(self, home: Path) -> None:
        config = home / "c"
        config.write_bytes(b"Host bench-\xe9\nHost pump\n")

        assert aliases(config)[-1] == "pump"

    def test_a_tab_separates_a_keyword_from_its_argument(self, home: Path) -> None:
        config = write(home / "c", "Host\tbench-1\n\tHostName\t10.0.0.5\n")

        assert hosts(config) == [Host("bench-1", "HostName 10.0.0.5")]


class TestInclude:
    def test_a_relative_include_is_under_the_ssh_directory(self, home: Path) -> None:
        write(home / ".ssh" / "boards.conf", "Host bench-1\n  User root\n")
        config = write(home / ".ssh" / "config", "Include boards.conf\nHost pump\n")

        assert hosts(config) == [Host("bench-1", "User root"), Host("pump", "")]

    def test_a_relative_include_is_not_relative_to_the_including_file(self, home: Path) -> None:
        write(home / "elsewhere" / "boards.conf", "Host bench-1\n")
        config = write(home / "elsewhere" / "config", "Include boards.conf\n")

        assert hosts(config) == []

    def test_an_absolute_include(self, home: Path, tmp_path: Path) -> None:
        other = write(tmp_path / "shared" / "boards.conf", "Host bench-2\n")
        config = write(home / ".ssh" / "config", f"Include {other}\n")

        assert aliases(config) == ["bench-2"]

    def test_a_home_relative_include(self, home: Path) -> None:
        write(home / "boards" / "extra.conf", "Host bench-3\n")
        config = write(home / ".ssh" / "config", "Include ~/boards/extra.conf\n")

        assert aliases(config) == ["bench-3"]

    def test_a_pattern_includes_each_match_in_name_order(self, home: Path) -> None:
        write(home / ".ssh" / "config.d" / "b.conf", "Host b-board\n")
        write(home / ".ssh" / "config.d" / "a.conf", "Host a-board\n")
        write(home / ".ssh" / "config.d" / "note.txt", "Host not-a-board\n")
        config = write(home / ".ssh" / "config", "Include config.d/*.conf\nHost last\n")

        assert aliases(config) == ["a-board", "b-board", "last"]

    def test_several_paths_on_one_line(self, home: Path) -> None:
        write(home / ".ssh" / "one.conf", "Host one\n")
        write(home / ".ssh" / "two.conf", "Host two\n")
        config = write(home / ".ssh" / "config", "Include one.conf two.conf\n")

        assert aliases(config) == ["one", "two"]

    def test_an_include_that_is_not_there(self, home: Path) -> None:
        config = write(home / ".ssh" / "config", "Include nothing.conf\nHost pump\n")

        assert aliases(config) == ["pump"]

    def test_includes_nest(self, home: Path) -> None:
        write(home / ".ssh" / "c.conf", "Host deep\n")
        write(home / ".ssh" / "b.conf", "Include c.conf\nHost middle\n")
        config = write(home / ".ssh" / "config", "Include b.conf\nHost top\n")

        assert aliases(config) == ["deep", "middle", "top"]

    def test_a_file_that_includes_itself_ends(self, home: Path) -> None:
        config = write(home / ".ssh" / "config", "Include config\nHost bench-1\n  User root\n")

        assert hosts(config) == [Host("bench-1", "User root")]

    def test_two_files_that_include_each_other_end(self, home: Path) -> None:
        write(home / ".ssh" / "a.conf", "Include b.conf\nHost a\n")
        write(home / ".ssh" / "b.conf", "Include a.conf\nHost b\n")
        config = write(home / ".ssh" / "config", "Include a.conf\n")

        assert sorted(aliases(config)) == ["a", "b"]

    def test_a_loop_reads_each_file_a_bounded_number_of_times(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[Path] = []
        original = Path.read_text

        def counting(self: Path, *args: object, **kwargs: object) -> str:
            reads.append(self)
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", counting)
        config = write(home / ".ssh" / "config", "Include config\nInclude config\n")

        hosts(config)

        # Two includes per file, nesting at most 8 deep, would otherwise be 2**N reads.
        assert len(reads) < 2000

    def test_a_chain_longer_than_the_depth_limit_is_cut(self, home: Path) -> None:
        for n in range(12):
            write(home / ".ssh" / f"c{n}.conf", f"Include c{n + 1}.conf\nHost h{n}\n")
        config = write(home / ".ssh" / "config", "Include c0.conf\n")

        found = aliases(config)

        assert "h0" in found
        assert "h11" not in found
