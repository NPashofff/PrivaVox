"""W4 finding 1 — the icon pipeline (scripts/make_icons.py) and its artifacts.

assets/app-icon.png (color), assets/menubar-icon.png (template glyph) and
assets/app-icon.ico are COMMITTED build artifacts. These tests prove:

- the committed bytes are exactly what the generator renders (deterministic
  regeneration — no drift between script and assets)
- the color icon actually differs from the black template glyph (the
  Windows-tray invisibility bug: a template glyph must never be the only icon)
- the ICO container is structurally valid (directory entries, PNG members)
  on BOTH writer paths (Pillow and the manual stdlib one)
- the stdlib PNG encoder round-trips pixel-exactly through Pillow's decoder
"""

from __future__ import annotations

import importlib.util
import io
import struct
from pathlib import Path

import numpy as np
import pytest

Image = pytest.importorskip("PIL.Image", reason="Pillow (W2 dep) not installed")

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

_spec = importlib.util.spec_from_file_location(
    "make_icons", ROOT / "scripts" / "make_icons.py")
make_icons = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_icons)


def test_generator_reproduces_committed_assets(tmp_path):
    """Deterministic pipeline: a fresh build equals the committed bytes.
    (The .ico compare additionally pins the Pillow writer of this venv —
    if Pillow's PNG encoder ever changes, rerun scripts/make_icons.py.)"""
    make_icons.build_all(str(tmp_path))
    for name in ("app-icon.png", "menubar-icon.png", "app-icon.ico"):
        assert (tmp_path / name).read_bytes() == (ASSETS / name).read_bytes(), name


def test_render_is_deterministic_in_process():
    assert (make_icons.png_encode(make_icons.render_app_icon(64))
            == make_icons.png_encode(make_icons.render_app_icon(64)))


def test_app_icon_is_color_and_glyph_is_template():
    app = np.asarray(Image.open(ASSETS / "app-icon.png").convert("RGBA"))
    glyph = np.asarray(Image.open(ASSETS / "menubar-icon.png").convert("RGBA"))
    assert app.shape == (512, 512, 4)
    assert glyph.shape == (36, 36, 4)
    # color histogram: both brand colors present in quantity
    vis = app[app[..., 3] > 128][:, :3].astype(int)
    purple = (np.abs(vis - (0x3C, 0x34, 0x89)).sum(axis=1) <= 12).sum()
    teal = (np.abs(vis - (0x5D, 0xCA, 0xA5)).sum(axis=1) <= 12).sum()
    assert purple > 10_000 and teal > 1_000
    # the glyph is black-only (mac template) → provably NOT the color icon;
    # shipping it as the sole Windows tray icon was the W4 bug
    gvis = glyph[glyph[..., 3] > 8]
    assert len(gvis) > 0 and (gvis[:, :3] == 0).all()


def _check_ico_structure(raw: bytes, expected_sizes: set[int]) -> None:
    reserved, ico_type, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, ico_type) == (0, 1)
    assert count == len(expected_sizes)
    seen, expected_offset = set(), 6 + 16 * count
    for i in range(count):
        w, h, _colors, _rsvd, _planes, bpp, size, off = struct.unpack(
            "<BBBBHHII", raw[6 + 16 * i:6 + 16 * (i + 1)])
        assert bpp in (0, 32)
        assert off == expected_offset          # members packed contiguously
        blob = raw[off:off + size]
        assert len(blob) == size
        assert blob[:8] == b"\x89PNG\r\n\x1a\n"  # PNG-compressed member
        pw, ph = struct.unpack(">II", blob[16:24])
        assert pw == ph == (w or 256) == (h or 256)
        seen.add(pw)
        expected_offset += size
    assert expected_offset == len(raw)
    assert seen == expected_sizes


def test_committed_ico_structure_and_pillow_opens():
    raw = (ASSETS / "app-icon.ico").read_bytes()
    _check_ico_structure(raw, set(make_icons.ICO_SIZES))
    im = Image.open(io.BytesIO(raw))
    assert im.size == (256, 256)
    assert {s for s, _ in im.info["sizes"]} == set(make_icons.ICO_SIZES)


def test_manual_ico_writer_matches_format():
    """The no-Pillow fallback writer must produce the same valid structure."""
    frames = [(s, make_icons.png_encode(make_icons.render_app_icon(s)))
              for s in (16, 32, 256)]
    raw = make_icons.ico_encode_manual(frames)
    _check_ico_structure(raw, {16, 32, 256})
    im = Image.open(io.BytesIO(raw))          # Pillow accepts it...
    assert im.size == (256, 256)
    im.size = (16, 16)
    im.load()                                 # ...including the small frame
    assert im.size == (16, 16)


def test_png_encoder_roundtrips_through_pillow():
    rgba = make_icons.render_menubar_icon(36)
    decoded = np.asarray(Image.open(io.BytesIO(make_icons.png_encode(rgba))))
    assert decoded.shape == rgba.shape
    assert (decoded == rgba).all()
