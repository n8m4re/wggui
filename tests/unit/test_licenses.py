"""Runtime dependencies must stay GPL-3.0-compatible (scripts/check_licenses.py)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location("check_licenses", ROOT / "scripts" / "check_licenses.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_dependencies_are_gpl_compatible(capsys):
    assert _tool().main() == 0, capsys.readouterr().out


def test_dependency_list_follows_lock_file():
    names = {alt[0] for alt in _tool().runtime_dependencies()}
    assert {"cryptography", "segno", "PySide6", "shiboken6"} <= names


def test_spdx_expression_in_legacy_license_field():
    tool = _tool()
    assert tool.SPDX_EXPRESSION.fullmatch("LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only")
    assert not tool.SPDX_EXPRESSION.fullmatch("Copyright (c) 2020, Example Inc. All rights reserved.")
