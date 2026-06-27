"""HARNESS: auto-verify opt-in gate.

The coding-agent harness can auto-queue a ``run_tests`` call after every
``edit_file``/``write_file`` so the model sees fresh test output before
its next edit. Off by default — opt-in via either:

  - The global ``agent_auto_verify`` setting (default False).
  - A per-workspace ``<workspace>/.devspace/auto-verify.json`` file
    containing ``{"enabled": <bool>}``. The per-workspace override wins
    over the global setting (so a single project can have auto-verify
    on without flipping it app-wide).

This test pins the gate behaviour without driving the whole agent loop.
"""
import json
import os
import tempfile

import pytest

from src.agent_harness import should_auto_verify


def _setting(value):
    """Build a setting_reader stub that returns ``value`` for any key."""
    return lambda key, default=None: value


def test_off_by_default_with_no_workspace():
    assert should_auto_verify(None, setting_reader=_setting(False)) is False


def test_off_by_default_with_workspace():
    with tempfile.TemporaryDirectory() as td:
        assert should_auto_verify(td, setting_reader=_setting(False)) is False


def test_global_setting_on_returns_true():
    assert should_auto_verify(None, setting_reader=_setting(True)) is True


def test_global_setting_on_with_workspace_returns_true():
    with tempfile.TemporaryDirectory() as td:
        assert should_auto_verify(td, setting_reader=_setting(True)) is True


def test_per_workspace_override_disables_global_on(tmp_path):
    (tmp_path / ".devspace").mkdir()
    (tmp_path / ".devspace" / "auto-verify.json").write_text(
        json.dumps({"enabled": False}), encoding="utf-8"
    )
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(True)) is False


def test_per_workspace_override_enables_global_off(tmp_path):
    (tmp_path / ".devspace").mkdir()
    (tmp_path / ".devspace" / "auto-verify.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(False)) is True


def test_missing_override_file_falls_back_to_global(tmp_path):
    # No .devspace/auto-verify.json -> just use the global setting.
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(True)) is True
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(False)) is False


def test_malformed_json_falls_back_to_global(tmp_path):
    (tmp_path / ".devspace").mkdir()
    (tmp_path / ".devspace" / "auto-verify.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    # Bad file should not crash; global wins.
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(True)) is True


def test_non_dict_json_falls_back_to_global(tmp_path):
    (tmp_path / ".devspace").mkdir()
    (tmp_path / ".devspace" / "auto-verify.json").write_text(
        json.dumps([1, 2, 3]), encoding="utf-8"  # a list, not a dict
    )
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(False)) is False


def test_missing_enabled_key_falls_back_to_global(tmp_path):
    (tmp_path / ".devspace").mkdir()
    (tmp_path / ".devspace" / "auto-verify.json").write_text(
        json.dumps({"other_key": True}), encoding="utf-8"
    )
    # No "enabled" key in override -> global wins.
    assert should_auto_verify(str(tmp_path), setting_reader=_setting(True)) is True


def test_setting_reader_exception_treated_as_off():
    def _boom(*_a, **_kw):
        raise RuntimeError("settings unavailable")
    assert should_auto_verify(None, setting_reader=_boom) is False


def test_setting_reader_returns_non_bool_coerces():
    # Defensive: non-bool settings must still coerce cleanly.
    assert should_auto_verify(None, setting_reader=_setting(1)) is True
    assert should_auto_verify(None, setting_reader=_setting(0)) is False
    assert should_auto_verify(None, setting_reader=_setting("yes")) is True
