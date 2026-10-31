# Changelog

All notable changes to this extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the extension uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Needs Zelos 26.0.10 or later.

### Fixed

- An action that fails, such as Test Connection to a device that refuses the login, shows as a failed run with its reason. Before, it showed as a completed run whose result held `status: error`.
- Run Command, Get State and Sample without a source act on the only source. With several sources they fail and name them, instead of acting on the first one, and the action form no longer preselects the first source.

## [0.1.1] - 2026-10-23

### Added

- Record this computer's system log on macOS, the unified log, and on Windows, the System and Application event logs. Each row keeps the entry's level, program and time.

### Changed

- The settings say where a log is and which log it is: Read from (A device over SSH, This computer or Demo device), Address, and Log (System log, A file or A command's output). Earlier lines and Numbers as signals replace History and Printed values as signals. Saved settings keep working.
- Auto-configure adds this computer's system log on every operating system.
- Messages and help call the machine Logs reads over SSH a device.

### Removed

- The "Saved file, replayed" source. `replay` on the command line still replays a saved file.
- The Time setting. Rows always take the device's clock, and a saved Time setting is ignored.

## [0.1.0] - 2026-10-23

### Added

- Stream the system log of Linux boards over SSH, through your own `ssh` client and its config: the journal, else BusyBox or OpenWrt `logread`, else `/var/log/messages` or `/var/log/syslog`.
- Follow a file through rotation, or the output of any command, on a board or on this computer, and a demo device.
- Log every entry with its level, program and message, and the file and line when the program sent them. A level the line prints wins over the journal's priority.
- Record printed values as signals: on for files, commands, replays and the demo device, off for the system log and the kernel's lines unless you turn them on.
- Place journal entries by the board's monotonic clock, for each boot, so a board whose clock reads 1970 still lines up.
- On a first start, send the journal's current boot as far back as Zelos keeps data.
- Resume the journal where it stopped after a dropped connection, a reboot or an agent restart, and a command that prints the kernel's buffer, such as `dmesg -w`, at the last kernel line it recorded.
- Convert a saved log file, plain or compressed with gzip, bzip2, xz or zstd, to a Zelos trace (`.trz`), each row at the time its line printed: `convert` on the command line, or the Convert Log File action, also while the extension is stopped.
- Replay a saved log file into the agent at the pace its lines were printed: `replay` on the command line, or a source that reads a saved file, replayed.
- Give a saved file the time zone, the year and the start time its stamps leave out when you convert it, and the time zone when you replay it.
- List Hosts, Auto-configure, Test Connection, Show Access Key, Convert Log File, Run Command, Get State and Sample actions.
