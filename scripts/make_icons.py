#!/usr/bin/env python3
"""Deterministic icon pipeline for PrivaVox — renders every project icon.

Replaces the ad-hoc scratchpad generators that produced the original assets.
One source of truth for the brand geometry (measured off the shipping
Flow.icns): a purple squircle (#3C3489) — a rounded rect whose corner radius
is 22.4% of its side, occupying 80% of the canvas — carrying a 7-bar teal
(#5DCAA5) waveform of vertical capsules with height fractions
[0.26, 0.48, 0.72, 0.94, 0.72, 0.48, 0.26] and pitch = 2 × bar width.

Outputs (committed build artifacts in assets/):
  app-icon.png      512 px full-color icon — Windows tray + anything that
                    needs a color raster (the mac Dock keeps app-icon.icns)
  menubar-icon.png  36 px black template glyph (macOS menu bar; alpha-only
                    waveform, same fractions, no squircle)
  app-icon.ico      multi-size Windows icon (16/24/32/48/64/128/256) for the
                    Start Menu shortcut — written via Pillow when importable,
                    otherwise a hand-built ICO container with PNG frames
                    (PNG-compressed entries are valid ICO members on Vista+)

Rendering is pure numpy (supersampled analytic coverage — no random AA) and
the PNG writer is stdlib zlib/struct, so PNG bytes are identical on every
run/machine; the .ico is byte-stable for a given Pillow version (and fully
machine-independent on the manual path).

Usage:  .venv/bin/python scripts/make_icons.py [--out-dir assets]
"""

from __future__ import annotations

import argparse
import io
import os
import struct
import sys
import zlib

import numpy as np

PURPLE = (0x3C, 0x34, 0x89)   # squircle fill
TEAL = (0x5D, 0xCA, 0xA5)     # waveform capsules
FRACTIONS = (0.26, 0.48, 0.72, 0.94, 0.72, 0.48, 0.26)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Geometry ratios measured off the shipping 1024 px Flow.icns render:
SQUIRCLE_OF_CANVAS = 0.80      # squircle side / canvas side
CORNER_OF_SIDE = 0.224         # corner radius / squircle side
PITCH_OF_SIDE = 78.0 / 820.0   # bar pitch / squircle side (bar width = pitch/2)
WAVE_H_OF_SIDE = 0.60          # tallest-fraction reference height / side
# menu bar glyph (36 px template): waveform only, near edge-to-edge
GLYPH_PITCH_OF_CANVAS = (29.0 / 6.0) / 36.0
GLYPH_H_OF_CANVAS = 0.86
# small raster sizes (ICO 16/24/32): pixel-snapped bars, bigger fill — the
# analytic geometry turns into an unreadable teal smudge below ~40 px
SMALL_MAX = 32
SMALL_FILL = 0.92


# ---- analytic shape coverage (supersampled, deterministic) -----------------

def _subgrid(size: int, ss: int) -> tuple[np.ndarray, np.ndarray]:
    c = (np.arange(size * ss, dtype=np.float64) + 0.5) / ss
    return np.meshgrid(c, c)


def _rounded_rect(X: np.ndarray, Y: np.ndarray, cx: float, cy: float,
                  hw: float, hh: float, r: float) -> np.ndarray:
    """Boolean inside-mask of a rounded rectangle (circular corners)."""
    qx = np.abs(X - cx) - (hw - r)
    qy = np.abs(Y - cy) - (hh - r)
    return np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) <= r


def _coverage(mask: np.ndarray, size: int, ss: int) -> np.ndarray:
    return mask.reshape(size, ss, size, ss).mean(axis=(1, 3))


def _waveform_mask(X: np.ndarray, Y: np.ndarray, cx: float, cy: float,
                   pitch: float, h_ref: float, w: float | None = None) -> np.ndarray:
    """Union of the 7 vertical capsules (bar width = pitch/2), centered."""
    if w is None:
        w = pitch / 2.0
    mask = np.zeros(X.shape, dtype=bool)
    for i, f in enumerate(FRACTIONS):
        bx = cx + (i - 3) * pitch
        bh = max(f * h_ref, w)  # a capsule is never shorter than it is wide
        mask |= _rounded_rect(X, Y, bx, cy, w / 2.0, bh / 2.0, w / 2.0)
    return mask


def render_app_icon(size: int) -> np.ndarray:
    """Full-color RGBA (H, W, 4) uint8: purple squircle + teal waveform."""
    ss = 8 if size <= 128 else 4
    X, Y = _subgrid(size, ss)
    c = size / 2.0
    if size <= SMALL_MAX:
        # pixel-snapped small-icon cut: whole-pixel pitch/width, odd widths
        # centered on pixel centers, so the bars stay crisp at 16-32 px
        side = SMALL_FILL * size
        pitch = max(2, round(size / 8))
        w = max(1, (pitch + 1) // 2)
        cx = c + (0.5 if w % 2 == 1 else 0.0)
        bars = _waveform_mask(X, Y, cx, c, float(pitch),
                              WAVE_H_OF_SIDE * side, float(w))
    else:
        side = SQUIRCLE_OF_CANVAS * size
        bars = _waveform_mask(X, Y, c, c, PITCH_OF_SIDE * side,
                              WAVE_H_OF_SIDE * side)
    sq = _rounded_rect(X, Y, c, c, side / 2.0, side / 2.0, CORNER_OF_SIDE * side)
    sq_cov = _coverage(sq, size, ss)
    bar_cov = np.minimum(_coverage(bars & sq, size, ss), sq_cov)

    rgba = np.zeros((size, size, 4), dtype=np.float64)
    for ch in range(3):  # premultiplied composite, then back to straight alpha
        rgba[..., ch] = PURPLE[ch] * (sq_cov - bar_cov) + TEAL[ch] * bar_cov
    np.divide(rgba[..., :3], sq_cov[..., None], out=rgba[..., :3],
              where=sq_cov[..., None] > 0)
    rgba[..., 3] = sq_cov * 255.0
    return np.clip(np.round(rgba), 0, 255).astype(np.uint8)


def render_menubar_icon(size: int = 36) -> np.ndarray:
    """Black template glyph RGBA (H, W, 4) uint8: the waveform alone."""
    ss = 8
    X, Y = _subgrid(size, ss)
    c = size / 2.0
    bars = _waveform_mask(X, Y, c, c, GLYPH_PITCH_OF_CANVAS * size,
                          GLYPH_H_OF_CANVAS * size)
    cov = _coverage(bars, size, ss)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 3] = np.clip(np.round(cov * 255.0), 0, 255).astype(np.uint8)
    return rgba


# ---- stdlib PNG writer (no Pillow needed) -----------------------------------

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_encode(rgba: np.ndarray) -> bytes:
    """Minimal RGBA-8 PNG: filter 0 scanlines, zlib level 9 — deterministic."""
    if rgba.dtype != np.uint8 or rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("png_encode expects (H, W, 4) uint8")
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 9))
            + _png_chunk(b"IEND", b""))


# ---- ICO container -----------------------------------------------------------

def ico_encode_manual(frames: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR + ICONDIRENTRYs + PNG blobs. PNG members are valid on Vista+
    (this is exactly what Pillow's ICO plugin emits too)."""
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, blobs = [], []
    for size, png in sorted(frames):
        b = 0 if size >= 256 else size  # 0 encodes 256 in the BYTE fields
        entries.append(struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32,
                                   len(png), offset))
        blobs.append(png)
        offset += len(png)
    return header + b"".join(entries) + b"".join(blobs)


def ico_encode_pillow(frames: list[tuple[int, bytes]]) -> bytes:
    """The preferred writer when Pillow is importable: identical frames are
    handed over explicitly (append_images), so nothing gets rescaled."""
    from PIL import Image

    imgs = {size: Image.open(io.BytesIO(png)) for size, png in frames}
    sizes = sorted(imgs)
    base = imgs[sizes[-1]]
    buf = io.BytesIO()
    base.save(buf, format="ICO", sizes=[(s, s) for s in sizes],
              append_images=[imgs[s] for s in sizes[:-1]])
    return buf.getvalue()


def ico_encode(frames: list[tuple[int, bytes]]) -> tuple[bytes, str]:
    try:
        return ico_encode_pillow(frames), "pillow"
    except ImportError:
        return ico_encode_manual(frames), "manual"


# ---- entry point --------------------------------------------------------------

def build_all(out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    written: dict[str, str] = {}

    app_png = png_encode(render_app_icon(512))
    written["app-icon.png"] = "512 px color"
    with open(os.path.join(out_dir, "app-icon.png"), "wb") as f:
        f.write(app_png)

    glyph_png = png_encode(render_menubar_icon(36))
    written["menubar-icon.png"] = "36 px template glyph"
    with open(os.path.join(out_dir, "menubar-icon.png"), "wb") as f:
        f.write(glyph_png)

    frames = [(s, png_encode(render_app_icon(s))) for s in ICO_SIZES]
    ico, writer = ico_encode(frames)
    written["app-icon.ico"] = f"{len(ICO_SIZES)} sizes via {writer}"
    with open(os.path.join(out_dir, "app-icon.ico"), "wb") as f:
        f.write(ico)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_out = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "assets")
    parser.add_argument("--out-dir", default=default_out,
                        help="output directory (default: <repo>/assets)")
    args = parser.parse_args(argv)
    for name, what in build_all(args.out_dir).items():
        path = os.path.join(args.out_dir, name)
        print(f"  {name:18s} {what:24s} {os.path.getsize(path):7d} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
