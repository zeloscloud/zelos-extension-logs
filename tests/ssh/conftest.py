"""Docker boards for the ssh tests, started once per session and removed at its end."""

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from harness import PREFIX, Board, Boards, Run, docker_available, make_key, remove_leftovers

from tests.conftest import Recording, recording

SYSTEMD = ["systemd-openssh", "systemd-old"]


@pytest.fixture(scope="session")
def session_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=PREFIX) as tmp:
        yield Path(tmp)


@pytest.fixture(scope="session")
def key(session_dir: Path) -> Path:
    """The key every board accepts."""
    return make_key(session_dir / "id_ed25519")


@pytest.fixture(scope="session")
def other_key(session_dir: Path) -> Path:
    """A key no board has."""
    return make_key(session_dir / "id_other")


@pytest.fixture(scope="session")
def boards(key: Path) -> Iterator[Boards]:
    if not docker_available():
        pytest.skip("Docker is not running")
    # Containers from a run that was killed before its teardown.
    remove_leftovers(images=False)
    try:
        yield Boards(key)
    finally:
        # Images stay when asked, to make a rerun faster.
        remove_leftovers(images=not os.environ.get("LOGS_IT_KEEP_IMAGES"))


@pytest.fixture(params=SYSTEMD)
def systemd_board(request: pytest.FixtureRequest, boards: Boards) -> Board:
    return boards.get(request.param)


@pytest.fixture
def ubuntu(boards: Boards) -> Board:
    return boards.get("systemd-openssh")


@pytest.fixture
def busybox(boards: Boards) -> Board:
    return boards.get("busybox-dropbear")


@pytest.fixture
def work() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=PREFIX) as tmp:
        yield Path(tmp)


@pytest.fixture
def agent(work: Path, trace_file: Recording) -> Iterator[Callable[..., Run]]:
    """Start an agent on sources; every one still running is stopped at the end.

    The first agent writes to `trace_file`, a later one to a recording of its own.
    """
    started: list[Run] = []

    def make(sources: list[dict[str, Any]], data_dir: Path | None = None) -> Run:
        trace = recording(work / f"trace-{len(started)}.trz") if started else trace_file
        run = Run(sources, trace, data_dir or work / "data")
        started.append(run)
        return run.start()

    yield make
    for run in started:
        run.close()
