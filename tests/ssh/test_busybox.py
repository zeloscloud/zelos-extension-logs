"""A BusyBox board over Dropbear, read through logread."""

import uuid
from collections.abc import Callable

import pytest
from harness import PROGRAM, Board, Run

pytestmark = pytest.mark.ssh


def test_a_logread_warning_keeps_its_program_and_level(
    busybox: Board, agent: Callable[..., Run]
) -> None:
    tok = uuid.uuid4().hex[:8]
    run = agent([busybox.source()])
    run.live(lambda text: busybox.log(PROGRAM, text), tok)
    busybox.log(PROGRAM, f"x-{tok}", priority="user.warn")
    run.wait_lines([f"x-{tok}"])
    reading = run.worker().health.reading
    rows = [row for row in run.rows() if row["message"] == f"x-{tok}"]

    assert reading == "logread"
    assert [(row["level"], row["name"]) for row in rows] == [("warn", PROGRAM)]
