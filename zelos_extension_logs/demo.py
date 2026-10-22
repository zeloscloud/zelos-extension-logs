"""A demo board whose journal reads like a small controller's.

A pump service logs through spdlog to its standard output, a co-processor's messages are forwarded
to the journal, and the kernel speaks now and then. The board restarts every 90 s, and each boot
starts with its clock at 1970 until its network sets it 10 s later, so the rows show that placement
follows the board's monotonic clock, not its wall clock.
"""

import json
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator

_BOOT_S = 90.0
_CLOCK_SET_S = 10.0
_TICK_S = 0.25


def lines(
    stop: threading.Event, now: Callable[[], int] = time.time_ns
) -> Iterator[tuple[bytes, int]]:
    """Journal JSON lines with the host time each was printed, until `stop` is set."""
    rng = random.Random(7)
    sequence = 0
    while not stop.is_set():
        boot = uuid.uuid4().hex
        booted_ns = now()
        tick = 0
        while not stop.is_set():
            elapsed_ns = now() - booted_ns
            if elapsed_ns >= _BOOT_S * 1e9:
                break
            monotonic_us = elapsed_ns // 1000
            realtime_ns = now() if elapsed_ns >= _CLOCK_SET_S * 1e9 else elapsed_ns
            for program, priority, transport, message in _events(tick, realtime_ns, rng):
                sequence += 1
                entry = {
                    "__CURSOR": f"s=demo;i={sequence:x};b={boot}",
                    "__REALTIME_TIMESTAMP": str(realtime_ns // 1000),
                    "__MONOTONIC_TIMESTAMP": str(monotonic_us),
                    "_BOOT_ID": boot,
                    "PRIORITY": str(priority),
                    "SYSLOG_IDENTIFIER": program,
                    "_TRANSPORT": transport,
                    "MESSAGE": message,
                }
                yield json.dumps(entry).encode() + b"\n", now()
            tick += 1
            stop.wait(_TICK_S)


def _events(tick: int, realtime_ns: int, rng: random.Random) -> list[tuple[str, int, str, str]]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(realtime_ns / 1e9))
    stamp += f".{realtime_ns // 1_000_000 % 1000:03d}"
    events: list[tuple[str, int, str, str]] = []
    if tick == 0:
        events.append(("kernel", 6, "kernel", "Booting Linux on physical CPU 0x0"))
        events.append(("pump", 6, "stdout", f"[{stamp}] [pump] [info] pump service started"))
    if tick % 4 == 0:
        rpm = 1450 + rng.randint(-30, 30)
        flow = 12.0 + rng.uniform(-0.4, 0.4)
        events.append(
            ("pump", 6, "stdout", f"[{stamp}] [pump] [info] running rpm={rpm} flow={flow:.2f}L/min")
        )
    if tick % 20 == 10:
        pressure = 3.4 + rng.uniform(0.0, 0.3)
        events.append(
            ("pump", 6, "stdout", f"[{stamp}] [pump] [warning] pressure high at {pressure:.2f} bar")
        )
    if tick % 8 == 2:
        events.append(("m4", 6, "syslog", f"sensor reading: {rng.randint(0, 1)}"))
    if tick % 60 == 30:
        events.append(("m4", 3, "syslog", "watchdog timeout on the motor loop"))
    if tick % 28 == 14:
        events.append(("kernel", 4, "kernel", "usb 1-1: reset high-speed USB device number 2"))
    return events
