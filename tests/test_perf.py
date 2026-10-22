"""Journal entries a second a source records, with none lost."""

import json
import re
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import FakeProcess, Processes, Recording, wait_until
from zelos_extension_logs import config
from zelos_extension_logs.worker import Cursors, SourceWorker

pytestmark = pytest.mark.perf

ENTRIES = 50_000
# Many times what journald lets a service log, 10,000 entries per 30 s, on a slow CI runner.
MIN_RATE = 5_000
BOOT = "f07dd35d996b498398f1e4347c93e456"
PROBE = (
    f"uid=0\nuser=root\ngroups=root\nsystemd=255\nboot_id={BOOT}\n"
    "uptime=100.0\nclock=1700000000\nend=1\n"
)
SEQ = re.compile(r"seq=(\d+) ")


def journal(entries: int) -> str:
    return "".join(
        json.dumps(
            {
                "__CURSOR": f"s=1;i={i:x};b={BOOT}",
                "__REALTIME_TIMESTAMP": str(1_700_000_000_000_000 + i * 100),
                "__MONOTONIC_TIMESTAMP": str(100_000_000 + i * 100),
                "_BOOT_ID": BOOT,
                "PRIORITY": "6",
                "SYSLOG_IDENTIFIER": "pump",
                "MESSAGE": f"[2026-10-21 12:00:00.000] [pump] [info] running seq={i} rpm=1450",
            }
        )
        + "\n"
        for i in range(entries)
    )


def test_a_busy_journal_is_recorded_whole(
    trace_file: Recording, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = sys.platform
    monkeypatch.setattr(sys, "platform", "linux")
    (cfg,) = config.parse({"sources": [{"connection": "local", "name": "dev"}]}).sources
    processes = Processes(FakeProcess(PROBE), FakeProcess(journal(ENTRIES), hold_open=True))
    worker = SourceWorker(
        cfg, trace_file.source, Cursors(tmp_path / "cursors.json"), None, run=processes
    )

    started = time.monotonic()
    worker.start()
    try:
        wait_until(lambda: worker.health.lines == ENTRIES, timeout=60)
        rate = ENTRIES / (time.monotonic() - started)
    finally:
        # Stopped the way this computer stops a process.
        monkeypatch.setattr(sys, "platform", platform)
        worker.stop()
        worker.join(5)
        processes.close()

    rows = trace_file.events()["dev/log"]
    assert sorted(int(m[1]) for row in rows if (m := SEQ.search(row["message"]))) == list(
        range(ENTRIES)
    )
    print(f"{rate:,.0f} entries/s")
    assert rate >= MIN_RATE
