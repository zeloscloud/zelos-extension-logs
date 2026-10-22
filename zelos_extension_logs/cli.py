"""The command line: run the extension, or convert or replay a saved log file."""

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import zelos_sdk
from zelos_log_lines import files
from zelos_log_lines.naming import safe_name
from zelos_log_lines.placing import Timing
from zelos_log_lines.stream import LogStream

from . import ACTION_PREFIX, app, config
from .worker import NOTES


def main(argv: Sequence[str] | None = None) -> int:
    """Run what `argv` asks for, the extension when it names no command; returns the exit code."""
    parser, convert = _parsers()
    args, rest = parser.parse_known_args(argv)
    # Files may follow an option: `convert a.log --zone UTC b.log`.
    if rest and args.command == "convert" and not any(r.startswith("-") for r in rest):
        args.files += [Path(r) for r in rest]
    elif rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command is None:
        # UTC with milliseconds, matching the SDK's own lines in the same extension.log.
        logging.Formatter.converter = time.gmtime
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        app.run()
        return 0
    timing = Timing(args.zone, args.year, args.start)
    name = safe_name(args.name) if args.name else None
    if args.command == "replay":
        return _replay(args.file, name, timing)
    if args.output is not None and len(args.files) > 1:
        convert.error(f"-o names one trace, but {len(args.files)} files were given")
    return _convert(args.files, args.output, name, timing, args.force)


def _convert(
    paths: list[Path], output: Path | None, name: str | None, timing: Timing, force: bool
) -> int:
    failed = False
    for path in paths:
        try:
            written, lines = files.convert(
                path, output, source=ACTION_PREFIX, name=name, timing=timing, overwrite=force
            )
            print(f"{path} -> {written} ({_lines(lines)})")
        except Exception as e:
            failed = True
            hint = " Add -f to overwrite it." if isinstance(e, FileExistsError) else ""
            print(f"{path}: {_reason(e)}.{hint}", file=sys.stderr)
    return 1 if failed else 0


def _replay(path: Path, name: str | None, timing: Timing) -> int:
    if not path.is_file():
        print(f"{path}: No such file.", file=sys.stderr)
        return 1
    source = zelos_sdk.init_global_source(ACTION_PREFIX)
    zelos_sdk.init(name=ACTION_PREFIX)
    stream = LogStream(source, name or files.name_for(path), notes=NOTES)
    try:
        lines = files.replay(path, stream, app.stop_on_signals(), timing=timing)
    except Exception as e:
        print(f"{path}: {_reason(e)}.", file=sys.stderr)
        return 1
    finally:
        stream.close()
        source.flush()
    print(f"{path}: {_lines(lines)} replayed")
    return 0


def _parsers() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """The command line, and its `convert` command, which reports its own usage errors."""
    parser = argparse.ArgumentParser(
        description="Record logs in Zelos. With no command, run the extension with its config."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--name", help="the log's name in the trace (default: the file's name)")
    common.add_argument(
        "--zone",
        type=_option(config.zone),
        help="the IANA time zone of stamps that print none, such as Europe/Berlin "
        "(default: this computer's)",
    )
    common.add_argument(
        "--year",
        type=_year,
        help="the year of the last stamp that prints none (default: the file's last change)",
    )
    common.add_argument(
        "--start",
        type=_option(config.start_ns),
        metavar="TIME",
        help="when the first line printed, for a file that prints only uptime: ISO 8601 with an "
        "offset, such as 2026-10-21T14:03:01+02:00 (default: the file ends at its last change)",
    )
    commands = parser.add_subparsers(dest="command")
    convert = commands.add_parser(
        "convert",
        parents=[common],
        help="convert saved log files to Zelos traces",
        description="Write each saved log file to a Zelos trace (.trz) beside it, each row at the "
        "time its line printed. Needs no agent.",
    )
    convert.add_argument(
        "files", nargs="+", type=Path, metavar="FILE", help="plain, gzip, bzip2, xz or zstd"
    )
    convert.add_argument(
        "-o", "--output", type=Path, help="the trace to write, for one file (default: FILE.trz)"
    )
    convert.add_argument("-f", "--force", action="store_true", help="overwrite a trace that exists")
    replay = commands.add_parser(
        "replay",
        parents=[common],
        help="replay a saved log file into the agent",
        description="Write a saved log file into the agent at the pace its lines were printed, "
        "the first one now. Ctrl-C stops it.",
    )
    replay.add_argument("file", type=Path, metavar="FILE", help="plain, gzip, bzip2, xz or zstd")
    return parser, convert


def _option(read: Callable[[str], object]) -> Callable[[str], object]:
    """`read` as an option's type: its ValueError is the option's error."""

    def parse(text: str) -> object:
        try:
            return read(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None

    return parse


def _year(text: str) -> int:
    if text.isdecimal() and int(text) in config.YEARS:
        return int(text)
    raise argparse.ArgumentTypeError(f"must be from {config.YEARS[0]} to {config.YEARS[-1]}")


def _reason(e: Exception) -> object:
    return e.strerror if isinstance(e, OSError) and e.strerror else e


def _lines(count: int) -> str:
    return f"{count:,} line{'' if count == 1 else 's'}"
