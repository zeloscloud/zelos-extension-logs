"""Zelos Logs: record a device's or this computer's logs, files and commands in Zelos."""

#: The action namespace and the default trace source. Equals `name` in extension.toml and the
#: name passed to `zelos_sdk.init`; tests/test_packaging.py pins all three.
ACTION_PREFIX = "Logs"

#: Equals `version` in extension.toml and pyproject.toml; tests/test_packaging.py pins it.
__version__ = "0.1.1"
