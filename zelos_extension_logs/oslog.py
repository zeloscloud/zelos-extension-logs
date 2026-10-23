"""This computer's own log on macOS and Windows: the command that follows it, and its entries.

On macOS that is the unified log, as `log stream` prints it; on Windows, the System and Application
event logs. Both print one JSON object a line, from the next entry on.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from zelos_log_lines.records import Level

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MACOS_LEVELS: dict[object, Level] = {
    "Fault": "critical",
    "Error": "error",
    "Default": "info",
    "Info": "info",
    "Debug": "debug",
}
# The event log's levels; 0, LogAlways, is an information event's.
_WINDOWS_LEVELS: dict[object, Level] = {1: "critical", 2: "error", 3: "warn", 4: "info", 5: "debug"}

# Polls each log for the records after the last one printed, from the newest when it starts. The
# writer is stdout's own, in UTF-8, as a console's encoding is not; a closed stdout ends it.
_EVENTS = """\
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$encoding = New-Object System.Text.UTF8Encoding $false
$out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $encoding)
$out.AutoFlush = $true
$logs = 'System', 'Application'
$after = @{}
foreach ($log in $logs) {
  $newest = Get-WinEvent -LogName $log -MaxEvents 1
  $after[$log] = if ($newest) { $newest.RecordId } else { 0 }
}
while ($true) {
  foreach ($log in $logs) {
    $filter = "*[System[EventRecordID>$($after[$log])]]"
    $events = @(Get-WinEvent -LogName $log -FilterXPath $filter | Sort-Object RecordId)
    foreach ($e in $events) {
      $after[$log] = $e.RecordId
      $message = $e.Message
      if (-not $message) { $message = "Event $($e.Id)" }
      $line = @{
        time = $e.TimeCreated.ToUniversalTime().ToString('o')
        level = [int]$e.Level
        source = $e.ProviderName
        message = $message
      } | ConvertTo-Json -Compress
      try { $out.WriteLine($line) } catch { exit 0 }
    }
  }
  Start-Sleep -Milliseconds 500
}
"""

MACOS = "/usr/bin/log stream --style ndjson"
# -OutputFormat Text: no CLIXML header for PowerShell's own streams in the output.
WINDOWS = (
    "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text "
    f"-EncodedCommand {base64.b64encode(_EVENTS.encode('utf-16-le')).decode('ascii')}"
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One entry of the log: its message, level, program and time."""

    message: str
    level: Level
    program: str
    time_ns: int


def command(platform: str) -> str | None:
    """The command that follows this computer's log, or None where the system log is the
    journal or syslog."""
    return {"darwin": MACOS, "win32": WINDOWS}.get(platform)


def read(platform: str, record: dict[str, object]) -> Entry | None:
    """An entry, or None for a record that is no log entry, such as macOS's activities."""
    return _macos(record) if platform == "darwin" else _windows(record)


def _macos(record: dict[str, object]) -> Entry | None:
    message, stamp, path = (
        record.get("eventMessage"),
        record.get("timestamp"),
        record.get("processImagePath"),
    )
    if record.get("eventType") != "logEvent" or not isinstance(message, str):
        return None
    try:
        # `2026-10-23 12:30:55.030238-0700`
        when = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S.%f%z")
    except ValueError:
        return None
    program = path.rsplit("/", 1)[-1] if isinstance(path, str) else ""
    return Entry(message, _MACOS_LEVELS.get(record.get("messageType"), "info"), program, _ns(when))


def _windows(record: dict[str, object]) -> Entry | None:
    message, stamp, source = record.get("message"), record.get("time"), record.get("source")
    if not isinstance(message, str):
        return None
    try:
        # `2026-10-23T19:36:46.5036950Z`: seven digits, of which Python reads six.
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    text = message.replace("\r\n", "\n").rstrip()
    level = _WINDOWS_LEVELS.get(record.get("level"), "info")
    return Entry(text, level, source if isinstance(source, str) else "", _ns(when))


def _ns(when: datetime) -> int:
    return (when - _EPOCH) // timedelta(microseconds=1) * 1_000
