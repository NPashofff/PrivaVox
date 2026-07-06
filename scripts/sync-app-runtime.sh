#!/bin/zsh
# Provision/update the Flow.app runtime under ~/Library/Application Support/Flow.
#
# The app bundle must not read from ~/Documents (TCC-protected → silent EPERM
# for a headless app), so the launcher runs a private copy of the code + venv
# from Application Support. Re-run this script after changing flow/*.py.
# `--print-src` prints the resolved repo root and exits (for checks/debugging).
set -e
SRC="${0:A:h:h}"  # repo root — this script lives in <repo>/scripts/
DST="$HOME/Library/Application Support/Flow"

if [[ "${1:-}" == "--print-src" ]]; then
  print -r -- "$SRC"
  exit 0
fi

mkdir -p "$DST"
rsync -a --delete "$SRC/flow" "$DST/"
cp "$SRC/assets/menubar-icon.png" "$SRC/assets/app-icon.icns" "$DST/" 2>/dev/null || true
# the user's personal dictionary lives in DST; seed it once, never overwrite
[ -f "$DST/dictionary.txt" ] || cp "$SRC/dictionary.txt" "$DST/"

if [ ! -x "$DST/venv/bin/python" ]; then
  uv venv --python 3.12 "$DST/venv"
  rm -f "$DST/.requirements-runtime.txt"  # fresh venv → force a dependency install
fi
# Install the curated runtime manifest, but only when it changed since the last
# sync. mlx-whisper declares torch yet never imports it at runtime (only in its
# unused weight-conversion module) — excluding torch spares a ~2 GB install.
if ! cmp -s "$SRC/requirements-runtime.txt" "$DST/.requirements-runtime.txt"; then
  uv pip install --quiet --python "$DST/venv/bin/python" \
    --excludes =(print -r -- torch) -r "$SRC/requirements-runtime.txt"
  cp "$SRC/requirements-runtime.txt" "$DST/.requirements-runtime.txt"
  rm -f "$DST/.requirements.txt"  # obsolete dev-freeze copy from older syncs
fi

echo "Flow runtime synced to $DST"
