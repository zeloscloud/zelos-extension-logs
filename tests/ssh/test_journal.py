"""The systemd journal over ssh, on a current board and on systemd 247."""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from harness import (
    PROGRAM,
    RECONNECT_WAIT_S,
    Board,
    Run,
    notes,
    numbers,
)

pytestmark = pytest.mark.ssh


def token() -> str:
    return uuid.uuid4().hex[:8]


def test_numbered_lines_arrive_once_with_program_and_level(
    systemd_board: Board, agent: Callable[..., Run]
) -> None:
    tok = token()
    run = agent([systemd_board.source()])
    run.live(lambda text: systemd_board.log(PROGRAM, text), tok)
    systemd_board.log_numbers(range(0, 10), tok, via="logger")
    systemd_board.log_numbers(range(10, 20), tok, via="systemd-cat")
    run.wait_lines([f"n=19 {tok}"])
    rows = [row for row in run.rows() if tok in row["message"] and row["message"].startswith("n=")]

    assert sorted(numbers(rows, tok)) == list(range(20))
    by_number = {int(row["message"].split()[0][2:]): row for row in rows}
    assert {by_number[i]["level"] for i in range(10)} == {"info"}
    assert {by_number[i]["level"] for i in range(10, 20)} == {"warn"}
    assert {row["name"] for row in rows} == {PROGRAM}


def test_a_spdlog_line_through_a_service_keeps_its_level_and_message(
    systemd_board: Board, agent: Callable[..., Run]
) -> None:
    run = agent([systemd_board.source()])
    run.live(lambda text: systemd_board.log(PROGRAM, text), token())
    systemd_board.sh(
        "systemd-run --collect --unit=acme-app sh -c "
        "'echo \"[2026-10-21 14:12:01.123] [acme] [warning] pressure high\"'"
    )
    run.wait_lines(["pressure high"])
    rows = [row for row in run.rows() if row["message"] == "pressure high"]

    assert [(row["level"], row["message"]) for row in rows] == [("warn", "pressure high")]


def test_history_before_the_start_arrives_when_kept_and_not_when_new(
    ubuntu: Board, agent: Callable[..., Run]
) -> None:
    tok = token()
    ubuntu.log_numbers(range(5), tok)
    kept = agent([ubuntu.source(name="kept")])
    kept.wait_lines([f"n=4 {tok}"])
    assert sorted(numbers(kept.rows(), tok)) == list(range(5))

    fresh = agent([ubuntu.source(name="fresh", history="new")])
    fresh.live(lambda text: ubuntu.log(PROGRAM, text), tok)
    ubuntu.log_numbers(range(5, 8), tok)
    fresh.wait_lines([f"n=7 {tok}"])
    assert sorted(numbers(fresh.rows(), tok)) == [5, 6, 7]


def test_a_cut_connection_resumes_without_a_lost_or_repeated_line(
    systemd_board: Board, agent: Callable[..., Run]
) -> None:
    tok = token()
    board = systemd_board
    run = agent([board.source()])
    run.live(lambda text: board.log(PROGRAM, text), tok)
    board.log_numbers(range(0, 5), tok)
    run.wait_lines([f"n=4 {tok}"])

    board.cut()
    try:
        run.wait("the cut to be seen", lambda: run.worker().health.state != "streaming")
        board.log_numbers(range(5, 10), tok)
        board.log_numbers(range(10, 15), tok, via="systemd-cat")
    finally:
        board.restore()
    run.wait_lines([f"n=14 {tok}"], timeout=RECONNECT_WAIT_S)
    board.log_numbers(range(15, 20), tok)
    run.wait_lines([f"n=19 {tok}"])

    assert run.worker().health.reconnects >= 1
    assert sorted(numbers(run.rows(), tok)) == list(range(20))


def test_an_agent_restart_resumes_from_the_saved_position(
    systemd_board: Board, agent: Callable[..., Run], work: Path
) -> None:
    tok = token()
    board = systemd_board
    first = agent([board.source()], data_dir=work / "kept")
    first.live(lambda text: board.log(PROGRAM, text), tok)
    board.log_numbers(range(0, 5), tok)
    first.wait_lines([f"n=4 {tok}"])
    first_rows = first.rows()

    board.log_numbers(range(5, 10), tok)
    second = agent([board.source()], data_dir=work / "kept")
    second.wait_lines([f"n=9 {tok}"])
    board.log_numbers(range(10, 12), tok)
    second.wait_lines([f"n=11 {tok}"])
    second_rows = second.rows()

    assert sorted(numbers(first_rows, tok) + numbers(second_rows, tok)) == list(range(12))


def test_a_user_outside_systemd_journal_is_told_how_to_join_it(
    ubuntu: Board, agent: Callable[..., Run]
) -> None:
    run = agent([ubuntu.source(user="tester")])
    # The note is written after the probe and before the journal starts.
    run.wait("the journal to start", lambda: run.worker().health.reading == "journal")
    found = notes(run.rows())

    assert any("usermod -aG systemd-journal tester" in row["message"] for row in found)


def test_a_vacuumed_cursor_resumes_at_the_first_line_left(
    systemd_board: Board, agent: Callable[..., Run], work: Path
) -> None:
    """systemd 245 to 252 skip one more line after a cursor they can no longer find."""
    tok = token()
    board = systemd_board
    first = agent([board.source()], data_dir=work / "kept")
    first.live(lambda text: board.log(PROGRAM, text), tok)
    board.log_numbers(range(0, 5), tok)
    first.wait_lines([f"n=4 {tok}"])
    first_rows = first.rows()

    # The files holding the saved position and the next two lines go; the active file stays.
    board.sh("journalctl --rotate")
    board.log_numbers(range(5, 7), tok)
    board.sh("journalctl --rotate")
    board.log_numbers(range(7, 9), tok)
    board.sh("sleep 2; journalctl --vacuum-time=1s")
    # journald's own first line in the file that is left: the line systemd 247 would skip.
    first_left = board.sh("journalctl -o cat --no-pager | head -n 1").strip()
    assert first_left

    second = agent([board.source()], data_dir=work / "kept")
    second.wait_lines([f"n=8 {tok}"])
    board.log_numbers(range(9, 11), tok)
    second.wait_lines([f"n=10 {tok}"])

    assert sorted(numbers(first_rows, tok)) == list(range(5))
    second_rows = second.rows()
    assert sorted(numbers(second_rows, tok)) == [7, 8, 9, 10]
    assert [row["message"] for row in second_rows].count(first_left) == 1
