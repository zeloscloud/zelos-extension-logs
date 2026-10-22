# Contributing

## Prerequisites

- [Zelos CLI](https://docs.zeloscloud.io/latest/getting-started/install-cli/) 0.1.10 or later
- Python 3.11
- [uv](https://docs.astral.sh/uv/) 0.9.5 or later
- [just](https://github.com/casey/just)

The Zelos guide to [developing extensions](https://docs.zeloscloud.io/latest/sdk/how-to/develop-extensions/) covers the SDK, and [Package and Publish Extensions](https://docs.zeloscloud.io/latest/sdk/how-to/package-extensions/) covers the archive and the marketplace.

## Commands

| Command        | Description                                              |
| -------------- | -------------------------------------------------------- |
| `just install` | Install dependencies and the pre-commit hooks            |
| `just fmt`     | Format and fix lint with ruff                            |
| `just check`   | Format check, lint, pyright and pytest: the merge gate   |
| `just test`    | Run tests                                                |
| `just dev`     | Run the extension against `./config.json`                |
| `just package` | Package for the Zelos marketplace                        |
| `just clean`   | Remove build artifacts                                   |

CI runs `just check` on Linux, macOS and Windows for every pull request and every push to `main`.
The SSH workflow runs the tests in `tests/ssh` against Docker boards: systemd with OpenSSH on two systemd versions, and BusyBox with Dropbear.
On Linux, CI also runs `uv run pytest -m perf`: a source must record all of 50,000 journal entries, at 5,000 entries a second or more.

## SSH tests against Docker boards

The tests in `tests/ssh` build the boards, connect to them over real SSH with a throwaway key, and remove everything they create.
They need Docker and run on Linux and macOS:

```bash
uv run pytest -m ssh tests/ssh
```

## Local run

```bash
echo '{"sources": [{"connection": "demo"}]}' > config.json
just dev
```

`just dev` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`. To run it under the agent instead:

```bash
zelos extensions install-local .
zelos extensions start local.logs --config '{"sources": [{"connection": "demo"}]}'
zelos extensions logs local.logs
```

## Releasing

```bash
zelos extensions bump X.Y.Z   # writes extension.toml and pyproject.toml
# Set __version__ in zelos_extension_logs/__init__.py to X.Y.Z, and add the release to CHANGELOG.md.
uv lock
just check
git commit -am "Release vX.Y.Z" && git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push --follow-tags
```

The tag runs `.github/workflows/release.yml`:

1. It runs CI on every OS and the SSH board tests; both must pass.
2. It checks that the tag matches the version in `extension.toml` and `pyproject.toml`.
3. It runs `just package`, and checks that the archive's `actions.json` lists the five actions that run while the extension is stopped.
4. It creates a GitHub release with `logs-X.Y.Z.tar.gz` attached.

The Zelos marketplace picks up each new release.
