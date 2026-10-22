"""The hosts named in an SSH config, for the Host picker."""

from dataclasses import dataclass
from pathlib import Path

# Include may nest; a loop of includes must still end.
_MAX_DEPTH = 8
# The options the picker shows beside an alias, as ssh_config spells them.
_SHOWN = {"hostname": "HostName", "user": "User"}


@dataclass(frozen=True, slots=True)
class Host:
    alias: str
    detail: str


def hosts(config: Path) -> list[Host]:
    """Each `Host` alias without a pattern, with its user and host name, in file order."""
    found: dict[str, dict[str, str]] = {}
    _read(config, found, 0)
    return [
        Host(alias, " ".join(f"{k} {v}" for k, v in options.items()))
        for alias, options in found.items()
    ]


def _read(path: Path, found: dict[str, dict[str, str]], depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.replace("=", " ", 1).replace("\t", " ").partition(" ")
        key, value = key.lower(), value.strip()
        if key == "include":
            for pattern in value.split():
                # Relative includes are under ~/.ssh, as ssh reads them.
                base = Path(pattern).expanduser()
                if not base.is_absolute():
                    base = Path.home() / ".ssh" / base
                # A pattern in the last part, as `Include config.d/*` uses.
                for match in sorted(base.parent.glob(base.name)):
                    _read(match, found, depth + 1)
        elif key == "host":
            current = [a for a in value.split() if not any(c in a for c in "*?!")]
            for alias in current:
                found.setdefault(alias, {})
        elif key == "match":
            current = []
        elif key in _SHOWN and current:
            for alias in current:
                found[alias].setdefault(_SHOWN[key], value)
