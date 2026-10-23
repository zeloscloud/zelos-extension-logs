"""Configuration types and loading."""

import json
import os
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft7Validator, ValidationError
from zelos_log_lines import files
from zelos_log_lines.naming import unique_name
from zelos_log_lines.placing import Timing

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "config.schema.json").read_text(encoding="utf-8")
)
# Draft 7, because it is the draft that enforces the per-connection `dependencies`.
_VALIDATOR = Draft7Validator(_SCHEMA)
_ITEM: dict[str, Any] = _SCHEMA["properties"]["sources"]["items"]
_CHOICES: dict[str, list[str]] = {
    key: _ITEM["properties"][key]["enum"] for key in ("connection", "read")
}
# The advanced settings each connection has: SSH all of them, this computer some, the demo none.
_ADVANCED: dict[str, dict[str, Any]] = {
    branch["properties"]["connection"]["enum"][0]: branch.get("else", branch)["properties"]
    .get("advanced", {})
    .get("properties", {})
    for branch in _ITEM["dependencies"]["connection"]["oneOf"]
}
# A replayed file's advanced settings, in place of its connection's.
_REPLAY_ADVANCED: dict[str, Any] = next(
    branch["properties"]["advanced"]["properties"]
    for branch in _ITEM["dependencies"]["read"]["oneOf"]
    if branch["properties"]["read"]["enum"] == ["replay"]
)
#: The years a saved file's timing may name: from the epoch to the last a trace's times hold.
YEARS = range(1970, 2262)
_SETTINGS: dict[str, Any] = _SCHEMA["properties"]["advanced"]["properties"]
# What a missing or blank field asks for.
_ENTER = {
    "host": "enter [user@]host or a Host from your SSH config",
    "path": "enter the file to follow",
    "command": "enter the command to run",
}
# The field each read needs, and its title.
_READ_FIELDS = {
    "file": ("path", "File"),
    "command": ("command", "Command"),
    "replay": ("path", "File"),
}
_KINDS = {"string": "text", "integer": "a whole number", "object": "a group of settings"}

Connection = Literal["ssh", "local", "demo"]
Read = Literal["system", "file", "command", "replay"]


class ConfigError(Exception):
    """The configuration is invalid; one human sentence per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One configured log, with every default applied."""

    name: str
    connection: Connection
    read: Read
    host: str | None
    path: str | None
    command: str | None
    ssh_port: int | None
    key_path: str | None
    host_key_policy: Literal["auto", "strict"]
    ssh_options: tuple[str, ...]
    history: Literal["kept", "new"]
    time_source: Literal["device", "arrival"]
    values: bool
    # What a replayed file's stamps leave out; None for every other read.
    timing: Timing | None


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole configuration."""

    sources: tuple[SourceConfig, ...]
    prefix: str
    log_level: str


def load(path: Path | None = None) -> Settings:
    """Read, validate and default the configuration."""
    path = path or Path(os.environ.get("ZELOS_CONFIG_PATH", "config.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except OSError as e:
        raise ConfigError(
            [f"The configuration file {path} cannot be read: {e.strerror or e}."]
        ) from e
    except UnicodeDecodeError as e:
        raise ConfigError([f"The configuration file {path} is not UTF-8 text."]) from e
    except json.JSONDecodeError as e:
        raise ConfigError([f"The configuration is not valid JSON: {e}."]) from e
    return parse(raw)


def parse(raw: object) -> Settings:
    """Validate and default a configuration already read."""
    if not isinstance(raw, dict) or not raw.get("sources"):
        raise ConfigError(["Add at least one source."])
    problems = _problems(raw)
    if problems:
        raise ConfigError(problems)

    sources = [source(item) for item in raw["sources"]]
    seen: dict[str, int] = {}
    for index, cfg in enumerate(sources, 1):
        # The trace store ignores case: two names that differ only in case leave it empty.
        key = cfg.name.casefold()
        if key and seen.setdefault(key, index) != index:
            problems.append(
                f"Source {index}: name '{cfg.name}' is already used by source {seen[key]}."
            )
    if problems:
        raise ConfigError(problems)

    advanced = {name: schema["default"] for name, schema in _SETTINGS.items()}
    advanced.update(raw.get("advanced", {}))
    return Settings(
        sources=tuple(_with_default_names(sources)),
        prefix=advanced["prefix"].strip(" "),
        log_level=advanced["log_level"],
    )


def source(item: dict[str, Any]) -> SourceConfig:
    """One source from a valid config item."""
    connection: Connection = item["connection"]
    read: Read = item.get("read", "system")
    advanced = {name: schema.get("default") for name, schema in _ADVANCED["ssh"].items()}
    known = _REPLAY_ADVANCED.keys() if read == "replay" else _ADVANCED[connection].keys()
    given = item.get("advanced")
    if isinstance(given, dict):
        advanced.update((k, v) for k, v in given.items() if k in known)
    values = advanced["values"]
    return SourceConfig(
        name=(item.get("name") or "").strip(" "),
        connection=connection,
        read=read,
        host=((item.get("host") or "").strip() or None) if connection == "ssh" else None,
        path=item["path"].strip() if read in ("file", "replay") else None,
        command=item.get("command") if read == "command" else None,
        ssh_port=advanced.get("ssh_port"),
        key_path=advanced.get("key_path") or None,
        host_key_policy=advanced["host_key_policy"],
        ssh_options=tuple(shlex.split(advanced.get("ssh_options") or "")),
        history=advanced["history"],
        time_source=advanced["time_source"],
        # The demo prints values to show them; it has no Advanced settings.
        values=connection == "demo" or (read != "system" if values == "auto" else values == "on"),
        # A replay starts now, so a start is moot; the year comes from the file's last change.
        timing=Timing(zone(advanced.get("time_zone"))) if read == "replay" else None,
    )


def host_problem(host: object) -> str | None:
    """Why `host` cannot be given to ssh, or None."""
    if not isinstance(host, str) or not host.strip():
        return _ENTER["host"]
    # A leading dash would reach ssh as an option.
    if host.strip().startswith("-") or any(c.isspace() for c in host.strip()):
        return f"'{host}' is not a host name"
    return None


def zone(name: str | None) -> tzinfo | None:
    """The IANA time zone `name`, or None, this computer's, when blank; raises ValueError, in
    words, for a name it does not know."""
    if not name or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"'{name}' is not an IANA time zone, such as Europe/Berlin") from None


def start_ns(text: str | None) -> int | None:
    """The ISO 8601 time `text`, which must give its offset, in nanoseconds since the epoch, or
    None when blank; raises ValueError, in words, for a time it cannot read."""
    if not text or not text.strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise ValueError(
            f"'{text}' is not an ISO 8601 time with an offset, such as 2026-10-21T14:03:01+02:00"
        )
    if moment.astimezone(UTC).year not in YEARS:
        raise ValueError(f"'{text}' is not from {YEARS[0]} to {YEARS[-1]}")
    return (moment - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1_000


def _problems(raw: dict[str, Any]) -> list[str]:
    """One sentence per mistake, naming the source and the field."""
    items = raw.get("sources")
    if not isinstance(items, list):
        return ["Sources must be a list."]
    found: dict[tuple[Any, ...], str] = {}
    for error in _VALIDATOR.iter_errors(raw):
        for leaf in _leaves(error):
            key, problem = _sentence(leaf)
            # A value of the wrong type also fails the enum, which says more.
            if key not in found or leaf.validator == "enum":
                found[key] = problem
    flagged = {key[1] for key in found if key[0] == "sources" and len(key) > 1}
    for index, item in enumerate(items):
        if index not in flagged:
            found.update(_meaning(index, item))
    return [found[key] for key in sorted(found, key=_position)]


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    """The errors worth reporting: for a failed `connection` or `read` branch, the chosen one's."""
    if error.validator != "oneOf":
        yield error
        return
    key = str(error.schema_path[-2])
    instance: Any = error.instance
    # An unknown choice already has its own error from the item's enum.
    if instance.get(key) in _CHOICES[key]:
        branch = _CHOICES[key].index(instance[key])
        yield from (e for e in error.context if e.relative_schema_path[0] == branch)


def _sentence(error: ValidationError) -> tuple[tuple[Any, ...], str]:
    """The field `error` is about, and the problem in words."""
    path: list[Any] = list(error.absolute_path)
    schema: Any = error.schema
    instance: Any = error.instance
    if error.validator == "required":
        required: Any = error.validator_value
        field = next(name for name in required if name not in instance)
        path.append(field)
        schema = schema.get("properties", {}).get(field, {})
        instance = None
    field = path[-1] if path else ""
    blank = instance is None or (isinstance(instance, str) and not instance.strip())
    if field in _ENTER and blank:
        # Inside a `read` branch, the parent error's instance is the source.
        item: Any = error.parent.instance if error.parent is not None else {}
        wording = _enter(field, item.get("read") if isinstance(item, dict) else None)
    elif "enum" in schema:
        wording = f"choose one of {', '.join(schema['enum'])}"
    elif error.validator == "type":
        wording = f"must be {_KINDS[schema['type']]}"
    elif error.validator in ("minimum", "maximum"):
        wording = f"must be from {schema['minimum']} to {schema['maximum']}"
    elif error.validator in ("minLength", "pattern") and blank:
        wording = "must not be empty"
    elif error.validator == "pattern":
        wording = "use letters, digits, spaces, _ and - only"
    elif error.validator == "maxLength":
        wording = f"must be at most {schema['maxLength']} characters"
    elif error.validator == "additionalProperties":
        given: Any = error.instance
        wording = f"unknown setting {', '.join(sorted(set(given) - set(schema['properties'])))}"
    else:
        wording = error.message
    if path[:1] == ["sources"] and len(path) > 1:
        where, rest = f"Source {path[1] + 1}", path[2:]
    else:
        where, rest = _SCHEMA["properties"]["advanced"]["title"], path[1:]
    title = schema.get("title") if rest else None
    return tuple(path), f"{where}{f', {title}' if title else ''}: {wording}."


def _meaning(index: int, item: dict[str, Any]) -> Iterator[tuple[tuple[Any, ...], str]]:
    """What the schema cannot say about a source it accepts."""
    where = f"Source {index + 1}"
    connection, read = item["connection"], item.get("read", "system")
    if connection == "ssh":
        if problem := host_problem(item["host"]):
            yield ("sources", index, "host"), f"{where}, Host: {problem}."
        try:
            shlex.split(item.get("advanced", {}).get("ssh_options", ""))
        except ValueError:
            yield (
                ("sources", index, "advanced"),
                f"{where}, Extra ssh options: a quote is not closed.",
            )
    if read in _READ_FIELDS:
        field, title = _READ_FIELDS[read]
        if not item[field].strip():
            yield ("sources", index, field), f"{where}, {title}: {_enter(field, read)}."
    if read == "replay":
        try:
            zone(item.get("advanced", {}).get("time_zone"))
        except ValueError as e:
            yield (
                ("sources", index, "advanced", "time_zone"),
                f"{where}, {_REPLAY_ADVANCED['time_zone']['title']}: {e}.",
            )


def _enter(field: str, read: object) -> str:
    return "enter the file to replay" if (field, read) == ("path", "replay") else _ENTER[field]


def _position(key: tuple[Any, ...]) -> float:
    return key[1] if key[0] == "sources" and len(key) > 1 else float("inf")


def _with_default_names(sources: list[SourceConfig]) -> list[SourceConfig]:
    used = {cfg.name for cfg in sources if cfg.name}
    named: list[SourceConfig] = []
    for cfg in sources:
        if not cfg.name:
            # 64 bytes, as a Name you set: `<name>/<program>` keeps room for a program.
            cfg = replace(cfg, name=unique_name(default_name(cfg), used, max_bytes=64))
        named.append(cfg)
    return named


def default_name(cfg: SourceConfig) -> str:
    """The name a source gets from its config alone, before it is made unique."""
    if cfg.connection == "demo":
        return "demo"
    base = "local" if cfg.connection == "local" else _host_name(cfg.host or "")
    if cfg.read in ("file", "replay") and cfg.path:
        return f"{base} {files.name_for(Path(cfg.path.replace(chr(92), '/')))}"
    if cfg.read == "command" and cfg.command:
        words = cfg.command.split()
        return f"{base} {PurePosixPath(words[0].replace(chr(92), '/')).stem}" if words else base
    return base


def _host_name(host: str) -> str:
    # `root@bench.local` → `bench.local`; dots are path separators in the trace.
    return host.rpartition("@")[2].replace(".", "_")
