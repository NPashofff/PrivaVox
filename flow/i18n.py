"""Minimal bilingual (BG/EN) switch for PrivaVox UI strings.

Stdlib-only, no imports from ``flow`` — the whole point is that this module
can be imported and used from anywhere (config, both shells, tests) without
dragging in the pipeline stack.

Usage: wrap a literal with ``t("българ текст", "english text")``. The module
global ``_LANG`` defaults to ``"bg"``, so with no ``set_language`` call every
``t(...)`` returns its FIRST argument — existing Bulgarian output and every
test asserting a Bulgarian string stays byte-identical.

Both app shells call ``set_language(config.ui_language)`` right after loading
settings, before any menu/HUD text is built.
"""

from __future__ import annotations

# Module global, default Bulgarian. Any code path that never calls
# set_language() therefore behaves exactly as before this module existed.
_LANG = "bg"


def set_language(lang: str) -> None:
    """Set the active UI language to ``"bg"`` or ``"en"``.

    Anything else is ignored — the current language stays unchanged.
    """
    global _LANG
    if lang in ("bg", "en"):
        _LANG = lang


def get_language() -> str:
    """Return the active UI language ("bg" or "en")."""
    return _LANG


def t(bg: str, en: str) -> str:
    """Return ``bg`` when the active language is Bulgarian, else ``en``."""
    return bg if _LANG == "bg" else en
