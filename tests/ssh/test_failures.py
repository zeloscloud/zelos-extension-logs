"""A source that cannot connect says why, with the fix."""

import shlex
from collections.abc import Callable
from pathlib import Path

import pytest
from harness import LOOPBACK, Board, Run, free_port, notes, ssh_source

from zelos_extension_logs import actions, keys

pytestmark = pytest.mark.ssh


def failed(run: Run, text: str) -> list[dict[str, object]]:
    """The notices of a source that never connected, once its health names `text`."""
    run.wait(f"a failure about {text}", lambda: text in run.worker().health.message.lower())
    return notes(run.rows())


def test_a_key_the_board_does_not_have_points_to_show_access_key(
    ubuntu: Board, agent: Callable[..., Run], other_key: Path
) -> None:
    run = agent([ssh_source(ubuntu.port, other_key)])
    found = failed(run, "access key")

    assert [row["level"] for row in found] == ["error"]
    assert "Show Access Key" in str(found[0]["message"])


@pytest.mark.parametrize("server", ["ubuntu", "busybox"])
def test_the_show_access_key_line_lets_its_key_in(
    server: str, request: pytest.FixtureRequest, work: Path
) -> None:
    # OpenSSH and Dropbear: a key whose options a server does not know is refused outright.
    board: Board = request.getfixturevalue(server)
    access_key = keys.ensure(work)
    board.sh(keys.install_line(keys.public(access_key)))

    result = actions.test_connection(
        host=f"root@{LOOPBACK}", ssh_port=board.port, key_path=str(access_key)
    )

    assert result["status"] == "success", result


def test_a_port_nothing_listens_on_is_reported_as_refused(
    agent: Callable[..., Run], key: Path
) -> None:
    run = agent([ssh_source(free_port(), key)])
    found = failed(run, "refused the connection")

    assert "refused the connection" in str(found[0]["message"])


def test_a_strict_host_key_check_refuses_a_board_it_does_not_know(
    ubuntu: Board, agent: Callable[..., Run], work: Path
) -> None:
    known_hosts = work / "known_hosts"
    known_hosts.touch()
    options = shlex.join(["-o", f"UserKnownHostsFile={known_hosts}"])
    run = agent([ubuntu.source(host_key_policy="strict", ssh_options=options)])
    found = failed(run, "host key")

    assert "host key" in str(found[0]["message"]).lower()
    assert found[0]["level"] == "error"
