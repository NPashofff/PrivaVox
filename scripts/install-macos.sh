#!/bin/zsh
# PrivaVox — bootstrap инсталатор за macOS (без Gatekeeper предупреждение).
#
# Пусни в Terminal:
#   curl -fsSL https://github.com/NPashofff/PrivaVox/releases/latest/download/install-macos.sh | zsh
#
# Файлове, свалени с curl и пуснати през zsh, НЕ получават Gatekeeper карантина,
# затова тук няма „Apple could not verify…". Сваля последното издание, разархивира
# и пуска истинския инсталатор (Инсталирай PrivaVox.command).
set -e
URL="https://github.com/NPashofff/PrivaVox/releases/latest/download/PrivaVox-macOS.zip"
TMP="$(mktemp -d)/PrivaVox"
mkdir -p "$TMP"

print -P "%F{cyan}==>%f Сваляне на последната версия…"
curl -fsSL "$URL" -o "$TMP/p.zip"
print -P "%F{cyan}==>%f Разархивиране…"
/usr/bin/unzip -q "$TMP/p.zip" -d "$TMP"
# curl-download-ът е без карантина, но чистим за всеки случай (напр. стар кеш)
xattr -cr "$TMP/PrivaVox-macOS" 2>/dev/null || true

SRC="$TMP/PrivaVox-macOS"
if [ ! -f "$SRC/Инсталирай PrivaVox.command" ]; then
  print -P "%F{red}Грешка:%f инсталаторът липсва в архива."
  exit 1
fi
print -P "%F{cyan}==>%f Стартиране на инсталатора…\n"
exec zsh "$SRC/Инсталирай PrivaVox.command"
