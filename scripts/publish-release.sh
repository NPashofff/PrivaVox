#!/bin/zsh
# Publish a PrivaVox release: platform zips + GitHub release on NPashofff/PrivaVox.
#
#   scripts/publish-release.sh v0.2.0
#
# Builds two bundles from the CURRENT git HEAD (never the dirty tree):
#   PrivaVox-macOS.zip    — tree minus Windows installer & private journals
#   PrivaVox-Windows.zip  — tree minus mac installer/bundle & private journals
# and creates/updates the GitHub release with them. The README's
# releases/latest/download/... links always resolve to the newest release.
#
# PRIVATE-BY-DESIGN exclusions (never published): STATUS.md, PLAN.md, docs/
# work journals. Check `git log` messages before the FIRST public push — the
# public repo starts from a clean orphan snapshot (see repo setup notes).
set -e
VERSION="${1:?usage: publish-release.sh vX.Y.Z}"
REPO="NPashofff/PrivaVox"
SRC="${0:A:h:h}"
STAGE="$(mktemp -d)/PrivaVox"
trap 'rm -rf "${STAGE:h}"' EXIT

PRIVATE=(STATUS.md PLAN.md docs "Публикувай PrivaVox.command")

mkdir -p "$STAGE"
git -C "$SRC" archive HEAD | tar -x -C "$STAGE"
for p in "${PRIVATE[@]}"; do rm -rf "$STAGE/$p"; done

BUILD="${STAGE:h}"
(cd "$BUILD" && cp -R PrivaVox PrivaVox-macOS && rm -rf \
    "PrivaVox-macOS/Install-PrivaVox.ps1" \
    "PrivaVox-macOS/Install-PrivaVox.bat" \
    "PrivaVox-macOS/Uninstall-PrivaVox.ps1" \
    "PrivaVox-macOS/Uninstall-PrivaVox.bat" \
    "PrivaVox-macOS/requirements-runtime-win.txt" && \
  zip -qry PrivaVox-macOS.zip PrivaVox-macOS)
(cd "$BUILD" && cp -R PrivaVox PrivaVox-Windows && rm -rf \
    "PrivaVox-Windows/Инсталирай PrivaVox.command" \
    "PrivaVox-Windows/Деинсталирай PrivaVox.command" \
    "PrivaVox-Windows/Flow.app" \
    "PrivaVox-Windows/Flow.command" \
    "PrivaVox-Windows/requirements-runtime.txt" \
    "PrivaVox-Windows/scripts/sync-app-runtime.sh" 2>/dev/null; \
  zip -qry PrivaVox-Windows.zip PrivaVox-Windows)

# bootstrap installers uploaded as loose assets (the curl|zsh / irm|iex paths
# that skip Gatekeeper / SmartScreen) — served at releases/latest/download/…
BOOT_MAC="$SRC/scripts/install-macos.sh"
BOOT_WIN="$SRC/scripts/install-windows.ps1"

print -P "%F{cyan}==>%f bundles:"
ls -lh "$BUILD"/PrivaVox-*.zip

ASSETS=("$BUILD/PrivaVox-macOS.zip" "$BUILD/PrivaVox-Windows.zip" "$BOOT_MAC" "$BOOT_WIN")
if gh release view "$VERSION" --repo "$REPO" > /dev/null 2>&1; then
  gh release upload "$VERSION" --repo "$REPO" --clobber "${ASSETS[@]}"
else
  gh release create "$VERSION" --repo "$REPO" --latest \
    --title "PrivaVox $VERSION" --generate-notes "${ASSETS[@]}"
fi
print -P "%F{green}✓%f release $VERSION: https://github.com/$REPO/releases/tag/$VERSION"
