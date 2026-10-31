"""What `zelos extensions package` and the marketplace read, checked before a tag is pushed."""

import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from zelos_extension_logs import ACTION_PREFIX, actions

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))
SCHEMA = json.loads((REPO_ROOT / MANIFEST["config"]["schema"]).read_text(encoding="utf-8"))


def test_the_name_is_the_action_prefix() -> None:
    assert MANIFEST["name"] == ACTION_PREFIX == "Logs"


def test_config_schema_is_valid_and_its_examples_validate() -> None:
    """Draft 7, because it is the draft that enforces the per-connection `dependencies`."""
    Draft7Validator.check_schema(SCHEMA)
    validator = Draft7Validator(SCHEMA)
    for example in SCHEMA["examples"]:
        validator.validate(example)


def test_the_schema_names_actions_that_exist() -> None:
    """The form's Auto-configure button and Host picker run these by their full names."""
    named = {
        SCHEMA["ui:options"]["autoconfig"],
        SCHEMA["properties"]["sources"]["items"]["dependencies"]["connection"]["oneOf"][0][
            "properties"
        ]["host"]["ui:options"]["action"],
    }

    assert named == {f"{ACTION_PREFIX}/auto_config", f"{ACTION_PREFIX}/list_hosts"}
    for full in named:
        function = getattr(actions, full.removeprefix(f"{ACTION_PREFIX}/"))
        assert function._action.standalone is True


def test_a_branch_keeps_its_fields_labels() -> None:
    """The form keeps one label list per field for every branch: a branch's own list relabels the
    field everywhere, and a branch's choices must start the field's for the labels to fit."""
    item = SCHEMA["properties"]["sources"]["items"]
    for key, dependency in item["dependencies"].items():
        for branch in dependency["oneOf"]:
            for name, field in branch["properties"].items():
                if name == key:
                    continue
                assert "ui:enumNames" not in field, (key, name)
                if "enum" in field:
                    choices = item["properties"][name]["enum"]
                    assert field["enum"] == choices[: len(field["enum"])], (key, name)


def _advanced(node: object) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every per-source Advanced setting, in every branch that shows it."""
    if isinstance(node, dict):
        advanced = node.get("advanced")
        if isinstance(advanced, dict) and "properties" in advanced:
            yield from advanced["properties"].items()
        for value in node.values():
            yield from _advanced(value)
    elif isinstance(node, list):
        for value in node:
            yield from _advanced(value)


def test_a_setting_reads_the_same_wherever_it_shows() -> None:
    """Each branch writes its own copy of a shared setting; the form shows whichever applies."""
    copies: dict[str, list[dict[str, Any]]] = {}
    for name, field in _advanced(SCHEMA["properties"]["sources"]["items"]):
        copies.setdefault(name, []).append(field)

    assert {name for name, fields in copies.items() if len(fields) > 1} >= {"history", "values"}
    for name, fields in copies.items():
        assert all(field == fields[0] for field in fields), name


def _defaults(node: object, name: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every field in the schema that has a default, in every branch, with its name."""
    if isinstance(node, dict):
        if "default" in node:
            yield name, node
        for key, value in node.items():
            yield from _defaults(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _defaults(value, name)


def test_every_default_in_the_schema_is_allowed_by_it() -> None:
    checked = set()
    for name, field in _defaults(SCHEMA):
        # The form starts with no sources; a config needs one, which Auto-configure adds.
        if name != "sources":
            Draft7Validator(field).validate(field["default"])
            checked.add(name)

    assert checked >= {"connection", "read", "host_key_policy", "history"}
    assert checked >= {"values", "advanced", "prefix", "log_level"}


def test_every_picture_the_readme_shows_is_packaged() -> None:
    """An installed copy shows the README's pictures from the package, not from GitHub."""
    readme = (REPO_ROOT / MANIFEST["readme"]).read_text(encoding="utf-8")
    pictures = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", readme)
    paths = MANIFEST["package"]["paths"]

    assert pictures
    for picture in pictures:
        assert any(picture == path or picture.startswith(f"{path}/") for path in paths), picture


def test_importing_every_module_starts_nothing() -> None:
    """Packaging and standalone action runs import the entry module; it must not start the app."""
    script = (
        "import importlib, pkgutil, threading\n"
        "import main, zelos_extension_logs as package\n"
        "for module in pkgutil.iter_modules(package.__path__):\n"
        "    importlib.import_module(f'{package.__name__}.{module.name}')\n"
        "assert threading.active_count() == 1, threading.enumerate()\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, check=True, timeout=60)
