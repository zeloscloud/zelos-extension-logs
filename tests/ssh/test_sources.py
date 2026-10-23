"""A file and a command read over ssh, on a GNU board and a BusyBox board."""

import time
import uuid
from collections.abc import Callable

import pytest
from harness import LOOPBACK, WAIT_S, Board, Boards, Run, notes, numbers

from tests.conftest import run as run_action

pytestmark = pytest.mark.ssh

# A line every 0.2 s whose stamp advances 0.1 s: the device clock, not the arrival, sets the time.
TICKS = (
    "sh -c 'i=0; while true; do i=$((i+1)); t=$((1000000 + i*100000)); "
    'printf "[%6d.%06d] acme: tick %d\\n" $((t/1000000)) $((t%1000000)) $i; sleep 0.2; done\''
)


@pytest.mark.parametrize("name", ["systemd-openssh", "busybox-dropbear"])
def test_a_followed_file_survives_rotation_by_rename_and_truncate(
    boards: Boards, agent: Callable[..., Run], name: str
) -> None:
    board = boards.get(name)
    tok = uuid.uuid4().hex[:8]
    path = f"/tmp/acme-{tok}.log"
    board.sh(f": > {path}")

    def append(*lines: str) -> None:
        board.sh("; ".join(f'echo "{line}" >> {path}' for line in lines))

    run = agent([board.source(top={"read": "file", "path": path})])
    run.live(append, tok)
    append(*(f"n={i} {tok}" for i in range(5)))
    run.wait_lines([f"n=4 {tok}"])

    # tail takes a moment to notice a rotation: markers go in until one shows it follows again.
    board.sh(f"mv {path} {path}.1 && : > {path}")
    run.live(append, f"{tok}-renamed")
    append(*(f"n={i} {tok}" for i in range(5, 10)))
    run.wait_lines([f"n=9 {tok}"])

    board.sh(f": > {path}")
    run.live(append, f"{tok}-truncated")
    append(*(f"n={i} {tok}" for i in range(10, 15)))
    run.wait_lines([f"n=14 {tok}"])

    rows = run.rows()
    assert sorted(numbers(rows, tok, program=None)) == list(range(15))
    # tail says when the file was replaced or truncated; those words are not the file's.
    assert [r["message"] for r in rows if r["message"].startswith("tail:")] == []


@pytest.mark.parametrize("name", ["systemd-openssh", "busybox-dropbear"])
def test_a_file_not_there_yet_is_noted_then_followed_once_it_appears(
    boards: Boards, agent: Callable[..., Run], name: str
) -> None:
    board = boards.get(name)
    tok = uuid.uuid4().hex[:8]
    path = f"/tmp/acme-{tok}.log"
    note = f"No file {path} yet. Its lines are read once it appears."

    run = agent([board.source(top={"read": "file", "path": path})])
    run.wait("the note", lambda: run.worker().health.message == note)
    board.sh(f'echo "n=0 {tok}" >> {path}')
    run.wait_lines([f"n=0 {tok}"])

    rows = run.rows()
    assert numbers(rows, tok, program=None) == [0]
    assert [r["message"] for r in notes(rows)] == [f"[logs] {note}"]


def test_a_command_places_its_lines_by_the_device_uptime(
    ubuntu: Board, agent: Callable[..., Run]
) -> None:
    run = agent([ubuntu.source(top={"read": "command", "command": TICKS})])
    run.wait_lines([f"acme: tick {i}" for i in range(1, 11)])
    rows = [row for row in run.rows() if row["message"].startswith("acme: tick ")]
    times = [row["@time_s"] for row in rows]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]

    assert [row["message"] for row in rows[:10]] == [f"acme: tick {i}" for i in range(1, 11)]
    assert gaps[:9] == pytest.approx([0.1] * 9, abs=0.04)


@pytest.mark.parametrize("name", ["systemd-openssh", "busybox-dropbear"])
def test_a_command_that_finishes_runs_once_and_ends_the_source(
    boards: Boards, agent: Callable[..., Run], name: str
) -> None:
    tok = uuid.uuid4().hex[:8]
    run = agent([boards.get(name).source(top={"read": "command", "command": f"echo {tok}"})])
    run.wait_state("ended")
    worker = run.worker()

    assert [row["message"] for row in run.rows()].count(tok) == 1
    assert worker.health.reconnects == 0


@pytest.mark.parametrize("name", ["systemd-openssh", "busybox-dropbear"])
def test_a_command_that_fails_reports_its_own_exit_status(
    boards: Boards, agent: Callable[..., Run], name: str
) -> None:
    run = agent([boards.get(name).source(top={"read": "command", "command": "exit 3"})])
    message = f"root@{LOOPBACK}: the log ended (exit 3)."

    run.wait(message, lambda: run.worker().health.message == message)


@pytest.mark.parametrize("name", ["systemd-openssh", "busybox-dropbear"])
def test_run_command_ends_what_it_started_on_the_board_at_its_timeout(
    boards: Boards, agent: Callable[..., Run], name: str
) -> None:
    board = boards.get(name)
    tok = uuid.uuid4().hex[:8]
    path = f"/tmp/acme-{tok}.log"
    board.sh(f": > {path}")
    agent([board.source()])

    def tails() -> list[str]:
        return [line for line in board.readers(path) if path in line]

    result = run_action("run_command", command=f"tail -f {path}", timeout_s=1.0)
    deadline = time.monotonic() + WAIT_S
    while tails() and time.monotonic() < deadline:
        time.sleep(0.2)

    assert result == {"status": "error", "message": "The command did not finish in 1 s."}
    assert tails() == []
