# Logs

Record logs in Zelos, on the same timeline as everything else you measure: a device's or this computer's system log, any log file, or any command's output.

- 🔌 **Just an address**: if `ssh` reaches the device, so does Logs. Nothing to install on it.
- ⏱️ **The device's clock**: rows line up with your data, even if its clock reads 1970.
- 🧭 **The right level**: a service's `[warning]` is a warning, not the journal's info.
- 🔎 **Every line, searchable**: by level, by program and by message.
- 📈 **Numbers as signals**: `rpm=1450` in a line can plot as `rpm`.
- 🔁 **Picks up where it stopped**: the journal resumes after a drop or a reboot.
- 🗂️ **Any log**: a file, a command's output, or a saved log to convert to a trace.

[![Read from set to Demo device, then Save & Start. The demo's log, dragged onto the layout, fills a Log panel line by line, a warning and an error among them, and the pump's rpm, dragged below, plots live.](assets/readme/quickstart.avif)](assets/readme/quickstart.mp4)

## Works with

| | |
|---|---|
| **A device over SSH** | Its system log on Linux: the journal (`journalctl`), else BusyBox or OpenWrt `logread`, else `/var/log/messages` or `/var/log/syslog`. Or any file, followed through rotation, or any command that keeps printing lines, such as `dmesg -w`. |
| **This computer** | Its system log: the journal or syslog on Linux, the unified log on macOS, the event log on Windows. Or any file or command, such as `adb logcat` or `docker logs -f`. |
| **Saved logs** | Plain, gzip, bzip2, xz or zstd log files, converted to Zelos traces |
| **Log formats** | The journal, syslog, the Linux kernel, Android logcat, and common logging libraries for C and C++, Rust, Go, Python and Java. [zelos-log-lines](https://pypi.org/project/zelos-log-lines/) lists every format. |

Logs runs on Linux, macOS and Windows, with Zelos 26.0.10 or later.

## Get started

1. Install **Logs** from Extensions and open its settings.
2. Set **Address** to your device, such as `me@my-board`, or pick one from your SSH config. To record this computer instead, set **Read from** to **This computer**.
3. Press **Save & Start**. The log appears under **Logs** in the signal tree. Drag its `log` onto the layout to read it in a Log panel.

![The demo device in Zelos: each row of the Log panel names the program that printed it, the pump, the co-processor or the kernel, with a watchdog error and two warnings among them, and the pump's rpm plots live below.](assets/readme/live.png)

- **If `ssh me@my-board` works** in a terminal on this computer, it works here. **Test Connection** says which log it will read on a device, or what to fix.
- **No access yet?** Run **Show Access Key** and paste its line into any shell on the device, even a serial console.
- **Log in as a user of your own, not root**, and add it to `systemd-journal` to read the whole journal. Run Command and command sources run as that user.
- **No device?** Set **Read from** to **This computer** to record this computer's own system log, or to **Demo device**: a small controller whose pump logs its speed and flow, and which restarts every 90 s with its clock at 1970.

## Actions

Actions show up under **Logs** in the sidebar's **Actions**. Double-click one to add it to the layout.

- **Test Connection** connects to a device and says what it will read, or what to fix.
- **Show Access Key** gives the line that lets this computer into a device.
- **Run Command** runs a shell command where a source reads, on its device or this computer, and returns its output.
- **Convert Log File** converts a saved log to a Zelos trace (`.trz`), even while Logs is stopped.

## When something is wrong

| You see | Do this |
|---|---|
| `<address> refused the login` | Run **Show Access Key** and paste its line into a shell on the device, or set **Key file**. |
| `The host key of <address> changed` | A reflashed device gets a new key. Run the `ssh-keygen -R` line the message gives, or set **Host key check** to **Trust any key**. |
| `<user> is not root and not in systemd-journal` | The journal shows that user only its own entries. Where the journal is, run `sudo usermod -aG systemd-journal <user>`. |
| `<address> did not answer` | Check that the device is on and reachable from this computer. Logs keeps trying, up to once a minute. |
| `No system log found` | There is no journal, `logread` or readable syslog file. Set **Log** to **A file** or **A command's output**. |

## From a script

The same actions run from Python or the command line:

```python
from zelos_sdk import connect

with connect() as agent:
    result = agent.actions.execute("Logs/sample", {"source": "bench-1", "lines": 20})
    print(result.value)
```

```bash
zelos actions execute Logs/convert_log_file --params '{"input_file": "/data/bench-1/syslog.2.gz"}'
```

## Reference

Every event and signal, how a device and a saved file are read, every setting, every action and every health message are in [docs/reference.md](docs/reference.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
