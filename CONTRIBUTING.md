# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/getting-started/install-cli/) 0.1.12 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

`docs/reference.md` documents every event, signal, setting, action and health message. The
package ships it and the README's pictures in `assets/readme/`, so an installed copy shows
them without GitHub.

## Commands

| Command                | Description                                            |
| ---------------------- | ------------------------------------------------------ |
| `just install`         | Install dependencies and the pre-commit hooks          |
| `just format`          | Format code with ruff                                  |
| `just check`           | Check formatting and lint (ruff), type-check (pyright) |
| `just test`            | Run tests (pytest)                                     |
| `just ci`              | Install from the lockfile, check, test and package     |
| `just run`             | Run the agent runtime against `./config.json`          |
| `just package`         | Package for the Zelos marketplace                      |
| `just release VERSION` | Bump version, check, test, commit, tag                 |
| `just clean`           | Remove build output, caches and packages               |

`just --list` shows every recipe. `just install` installs the pre-commit hooks in
`.pre-commit-config.yaml`.

CI runs `just check` and `just test` on Linux, macOS and Windows for every push and pull request.
The SSH workflow runs the tests in `tests/ssh` against Docker boards: systemd with OpenSSH on two systemd versions, and BusyBox with Dropbear.
On Linux, CI also packages the extension and runs `uv run pytest -m perf`: a source must record all of 50,000 journal entries, at 5,000 entries a second or more.

## SSH tests against Docker boards

The tests in `tests/ssh` build the boards, connect to them over real SSH with a throwaway key, and remove everything they create.
They need Docker and run on Linux and macOS:

```bash
uv run pytest -m ssh tests/ssh
```

## Local run

```bash
echo '{"sources": [{"connection": "demo"}]}' > config.json
just run
```

`just run` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`.

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.logs --config '{"sources": [{"connection": "demo"}]}'
zelos extensions logs local.logs
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.logs`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

Add the release to `CHANGELOG.md` and commit it, then:

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow runs CI on every OS and the SSH board tests first. It checks that the tag
matches the version in `extension.toml` and `pyproject.toml`, checks, tests and packages the
extension, then attaches the archive to a GitHub release.

To list the extension in the Zelos marketplace, once: push the project to a GitHub repository,
submit it at [marketplace.zeloscloud.io](https://marketplace.zeloscloud.io/), and install the
Zelos GitHub App on the repository when asked. After approval, every release you publish reaches
the marketplace. See
[Publish to the Marketplace](https://docs.zeloscloud.io/sdk/how-to/package-extensions/#publish-to-the-marketplace).
