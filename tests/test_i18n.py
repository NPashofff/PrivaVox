"""Bilingual (BG/EN) scaffold: the flow.i18n switch + the ui_language
settings round-trip through BOTH app shells.

flow.i18n keeps a module-global _LANG (default "bg"); no strings are wrapped
in t() yet, so with the default every t(bg, en) must return bg — that is what
keeps all existing Bulgarian output byte-identical. These tests pin the tiny
API and the ui_language plumbing without touching that guarantee (each test
restores _LANG so import order can't leak state into the rest of the suite).
"""

from __future__ import annotations

import json

import pytest

import flow.i18n as i18n
from flow.config import FlowConfig

flow_app = pytest.importorskip(
    "flow.app", reason="mac shell deps (rumps/AppKit) not installed")
win_shell = pytest.importorskip(
    "flow.platform_win32.shell", reason="pystray/Pillow not installed")


@pytest.fixture(autouse=True)
def _restore_language():
    """Never let one test's set_language() leak into another (or the suite)."""
    saved = i18n.get_language()
    try:
        yield
    finally:
        i18n.set_language("bg" if saved not in ("bg", "en") else saved)


# --------------------------------------------------------------------------
# the tiny i18n API
# --------------------------------------------------------------------------

def test_default_language_is_bulgarian():
    assert i18n.get_language() == "bg"
    assert i18n._LANG == "bg"


def test_t_returns_bg_by_default():
    # the byte-identical guarantee: with the default, t(...) is its first arg
    assert i18n.t("българ", "english") == "българ"


def test_t_returns_en_after_switch():
    i18n.set_language("en")
    assert i18n.get_language() == "en"
    assert i18n.t("българ", "english") == "english"


def test_set_language_ignores_unknown_values():
    i18n.set_language("en")
    i18n.set_language("xx")            # ignored → stays previous
    assert i18n.get_language() == "en"
    assert i18n.t("българ", "english") == "english"
    i18n.set_language("")             # ignored too
    assert i18n.get_language() == "en"


# --------------------------------------------------------------------------
# ui_language round-trips through both shells' load/save
# --------------------------------------------------------------------------

def test_ui_language_default_on_config():
    assert FlowConfig().ui_language == "bg"


def test_mac_shell_ui_language_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = FlowConfig()
    config.ui_language = "en"
    flow_app.save_settings(config, str(path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["ui_language"] == "en"
    fresh = FlowConfig()
    flow_app.load_settings(fresh, str(path))
    assert fresh.ui_language == "en"


def test_mac_shell_ui_language_invalid_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ui_language": "fr"}), encoding="utf-8")
    config = FlowConfig()
    flow_app.load_settings(config, str(path))
    assert config.ui_language == "bg"        # bad value ignored → default


def test_win32_shell_ui_language_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = FlowConfig()
    config.ui_language = "en"
    win_shell.save_settings(config, str(path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["ui_language"] == "en"
    fresh = FlowConfig()
    win_shell.load_settings(fresh, str(path))
    assert fresh.ui_language == "en"


def test_win32_shell_ui_language_invalid_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ui_language": "fr"}), encoding="utf-8")
    config = FlowConfig()
    win_shell.load_settings(config, str(path))
    assert config.ui_language == "bg"        # bad value ignored → default
