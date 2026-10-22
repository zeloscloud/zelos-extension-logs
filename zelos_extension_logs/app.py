"""The extension's life cycle: one SourceWorker per configured source until SIGTERM or SIGINT.

Given a `source`, run() writes there and never connects to the agent; given a `stop` event, it
installs no signal handlers.
"""

import logging
import signal
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from . import ACTION_PREFIX, actions, config, keys, probe
from .config import ConfigError, Settings
from .worker import Cursors, Runner, SourceWorker

logger = logging.getLogger(__name__)


def run(
    source: zelos_sdk.TraceSource | None = None,
    *,
    settings: Settings | None = None,
    data_dir: Path | None = None,
    runner: Runner | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Run the extension until `stop` is set, or until SIGTERM or SIGINT."""
    if settings is None:
        try:
            settings = config.load()
        except ConfigError as e:
            # The supervisor does not restart the extension: these lines are the user's feedback.
            for problem in e.problems:
                logger.error("%s", problem)
            sys.exit(1)
    retention = probe.HISTORY
    if source is None:
        # Name the global source before init() does; init_global_source is idempotent.
        source = zelos_sdk.init_global_source(settings.prefix)
        zelos_sdk.init(name=ACTION_PREFIX, actions=True)
        retention = agent_retention()
    if stop is None:
        stop = stop_on_signals()
    data_dir = data_dir or actions.data_dir()

    handler = TraceLoggingHandler(source)
    handler.setLevel(settings.log_level)
    # The SDK logs every failed action, which the caller already gets as the action's error.
    handler.addFilter(lambda record: not record.name.startswith("zelos_sdk.actions"))
    root = logging.getLogger()
    root.addHandler(handler)
    logging.getLogger(__package__).setLevel(settings.log_level)

    cursors = Cursors(data_dir / "cursors.json" if data_dir else None)
    access_key = keys.path(data_dir)
    actions.workers.clear()
    try:
        for cfg in settings.sources:
            worker = SourceWorker(cfg, source, cursors, access_key, run=runner, retention=retention)
            worker.start()
            actions.workers[cfg.name] = worker
        # A timed wait: an untimed Event.wait() may not wake for Ctrl-C on Windows.
        while not stop.wait(0.5):
            pass
    finally:
        workers = list(actions.workers.values())
        # Every source at once, so the agent's grace period also covers the flush; a process that
        # outlives its SIGTERM is killed halfway.
        for kill in (False, True):
            for worker in workers:
                worker.stop(kill=kill)
            deadline = time.monotonic() + 0.5
            for worker in workers:
                worker.join(max(0.0, deadline - time.monotonic()))
        source.flush()
        root.removeHandler(handler)


def agent_retention() -> timedelta | None:
    """How long the agent keeps data, None for no limit; probe.HISTORY when it cannot say."""
    try:
        with zelos_sdk.Agent() as agent:
            settings = agent.info().settings
    except zelos_sdk.AgentError:
        settings = None
    if settings is None:
        logger.warning("The agent did not say how long it keeps data: history goes back an hour.")
        return probe.HISTORY
    return settings.data_retention


def stop_on_signals() -> threading.Event:
    """An event that SIGTERM and SIGINT set."""
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    return stop
