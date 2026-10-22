"""Stopping the agent ends what it started on the board."""

import time
import uuid
from collections.abc import Callable

import pytest
from harness import PROGRAM, WAIT_S, Board, Boards, Run

pytestmark = pytest.mark.ssh

CASES = {
    "journal": ("systemd-openssh", "journalctl"),
    "file": ("systemd-openssh", "tail"),
    "file-busybox": ("busybox-dropbear", "tail"),
    "logread": ("busybox-dropbear", "logread"),
    "command": ("systemd-openssh", "while true"),
}


def settle(board: Board, marker: str, *, running: bool) -> list[str]:
    """The reader processes once they are, or are not, on the board; polled, not slept."""
    deadline = time.monotonic() + WAIT_S
    found = board.readers(marker)
    while bool(found) != running and time.monotonic() < deadline:
        time.sleep(0.2)
        found = board.readers(marker)
    return found


@pytest.mark.parametrize("case", CASES)
def test_no_reader_is_left_on_the_board_after_stop(
    boards: Boards, agent: Callable[..., Run], case: str
) -> None:
    name, reader = CASES[case]
    board = boards.get(name)
    marker = f"acme-{uuid.uuid4().hex[:8]}"
    path = f"/tmp/{marker}.log"
    top: dict[str, str] = {}
    if case.startswith("file"):
        board.sh(f": > {path}")
        top = {"read": "file", "path": path}
    elif case == "command":
        top = {
            "read": "command",
            "command": f"sh -c 'while true; do echo {marker}; sleep 0.2; done'",
        }
    assert not settle(board, marker, running=False), "a reader from an earlier test is still there"

    run = agent([board.source(top=top)])
    if case.startswith("file"):
        run.live(lambda text: board.sh(f'echo "{text}" >> {path}'), marker)
    elif case == "command":
        run.wait_lines([marker])
    else:
        run.live(lambda text: board.log(PROGRAM, text), marker)
    running = settle(board, marker, running=True)
    assert any(reader in line for line in running), running

    run.stop()

    assert settle(board, marker, running=False) == []
