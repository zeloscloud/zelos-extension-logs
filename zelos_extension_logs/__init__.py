"""Zelos Logs: stream logs from Linux boards, files and commands into a Zelos trace."""

#: The action namespace and the default trace source. Equals `name` in extension.toml and the
#: name passed to `zelos_sdk.init`; tests/test_packaging.py pins all three.
ACTION_PREFIX = "Logs"

#: Equals `version` in extension.toml and pyproject.toml; tests/test_packaging.py pins it.
__version__ = "0.1.0"
