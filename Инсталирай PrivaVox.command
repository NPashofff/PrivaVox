#!/bin/zsh
# Flow installer — double-click to set everything up.
#
# Idempotent: safe to re-run; existing pieces are detected and skipped.
# Needs: internet. Installs/uses: Homebrew packages (ollama, uv), two Python
# venvs (.venv for the repo from requirements.txt; the app runtime via
# scripts/sync-app-runtime.sh from requirements-runtime.txt), the chosen
# BgGPT model, the Whisper STT model, Flow.app.
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

FLOW_SRC="$(cd "$(dirname "$0")" && pwd)"
APP_SUPPORT="$HOME/Library/Application Support/Flow"

step()  { print -P "\n%F{cyan}==>%f %B$1%b" }
ok()    { print -P "    %F{green}✓%f $1" }
# Един AppleScript диалог за целия инсталатор (заглавие: „PrivaVox — инсталация").
#   dialog [--caution] "съобщение" "Бутон1" ["Бутон2" ...]
# Последният бутон е default. Печата натиснатия бутон; exit кодът е на osascript.
dialog() {
  local icon=""
  if [[ "$1" == "--caution" ]]; then icon=" with icon caution"; shift; fi
  local msg="$1"; shift
  local btns="" b
  for b in "$@"; do btns+="${btns:+, }\"$b\""; done
  /usr/bin/osascript -e "button returned of (display dialog \"$msg\" buttons {$btns} default button \"${argv[-1]}\" with title \"PrivaVox — инсталация\"$icon)"
}
fail_dialog() {
  dialog --caution "$1" "OK" > /dev/null 2>&1
  exit 1
}

print -P "%B%F{magenta}"
print "  PrivaVox — локална диктовка (EN/BG)"
print "  Инсталаторът ще подготви всичко; ще те попита само за AI модела."
print -P "%f%b"

# --- 0. Процесор (Apple Silicon) ----------------------------------------------
step "Проверка на процесора"
if [[ "$(uname -m)" != "arm64" ]]; then
  fail_dialog "PrivaVox изисква Mac с Apple Silicon (M-серия процесор).\n\nТози Mac е с Intel процесор, а AI библиотеките (MLX) нямат Intel версии."
fi
ok "Apple Silicon"

# --- 1. Homebrew ------------------------------------------------------------
step "Проверка за Homebrew"
if ! command -v brew > /dev/null; then
  fail_dialog "Липсва Homebrew (мениджър на пакети).\n\nИнсталирай го от https://brew.sh (една команда в Terminal), после пусни инсталатора отново."
fi
ok "Homebrew е наличен"

# --- 2. Ollama --------------------------------------------------------------
step "Ollama (локален LLM сървър)"
if ! command -v ollama > /dev/null; then
  echo "    инсталирам ollama през brew..."
  brew install ollama
fi
if ! curl -s --max-time 2 http://localhost:11434/api/version > /dev/null; then
  if brew list ollama > /dev/null 2>&1; then
    echo "    стартирам ollama като услуга..."
    brew services start ollama || true
  else
    # ollama е инсталиран извън brew (DMG приложението) — пускаме самото app
    echo "    стартирам приложението Ollama..."
    open -a Ollama > /dev/null 2>&1 || true
  fi
  for i in {1..15}; do
    curl -s --max-time 2 http://localhost:11434/api/version > /dev/null && break
    sleep 1
  done
fi
curl -s --max-time 2 http://localhost:11434/api/version > /dev/null || fail_dialog "Ollama не тръгна. Пусни ръчно: brew services start ollama — и опитай пак."
ok "Ollama върви"

# --- 3. uv + Python среда ----------------------------------------------------
step "Python среда (uv venv + зависимости)"
if ! command -v uv > /dev/null; then
  echo "    инсталирам uv през brew..."
  brew install uv
fi
if [ ! -x "$FLOW_SRC/.venv/bin/python" ]; then
  (cd "$FLOW_SRC" && uv venv --python 3.12 .venv)
fi
(cd "$FLOW_SRC" && uv pip install --quiet --python .venv/bin/python -r requirements.txt)
ok "Python средата е готова"

# --- 4. Избор на AI модел -----------------------------------------------------
step "Избор на AI модел за изчистване на текста"
REC="BgGPT 4B  —  2.5 GB  —  ПРЕПОРЪЧИТЕЛЕН: най-добър баланс качество/скорост"
CHOICE=$(/usr/bin/osascript <<OSA
set models to {"$REC", "BgGPT 12B  —  7.3 GB  —  по-тежък и ~3x по-бавен (за диктовка не е по-добър)", "BgGPT 2.6B  —  1.7 GB  —  най-лек, за по-слаби машини (по-просто чистене)"}
set sel to choose from list models with title "PrivaVox — избор на AI модел" with prompt "Кой AI модел да инсталирам? (може да добавиш/смениш и по-късно от Dock менюто на Flow)" default items {"$REC"} OK button name "Инсталирай" cancel button name "Отказ"
if sel is false then return "cancel"
return item 1 of sel
OSA
)
case "$CHOICE" in
  cancel) echo "Отказано от потребителя."; exit 0 ;;
  *12B*)  MODEL="todorov/bggpt:Gemma-3-12B-IT-Q4_K_M" ;;
  *2.6B*) MODEL="hf.co/INSAIT-Institute/BgGPT-Gemma-2-2.6B-IT-v1.0-GGUF:Q4_K_M" ;;
  *)      MODEL="todorov/bggpt:latest" ;;
esac
# точен таг: първата колона на `ollama ls` == $MODEL („име" ≡ „име:latest" в двете посоки)
if ollama ls 2>/dev/null | awk -v m="$MODEL" 'BEGIN{if (m !~ /:/) m=m":latest"} NR>1{t=$1; if (t !~ /:/) t=t":latest"; if (t==m) f=1} END{exit !f}'; then
  ok "Моделът $MODEL вече е изтеглен"
else
  echo "    свалям $MODEL (прогресът е по-долу)..."
  ollama pull "$MODEL"
fi
ok "AI модел: $MODEL"

# --- 5. Whisper STT модел ------------------------------------------------------
step "Whisper STT модел (~1.6 GB, еднократно)"
(cd "$FLOW_SRC" && .venv/bin/python - <<'PY'
from flow.config import FlowConfig
from flow import stt
secs = stt.warm_up(FlowConfig())
print(f"    Whisper готов ({secs:.1f}s)")
PY
)
ok "Whisper е наличен и зареден"

# --- 6. Работна среда на приложението -----------------------------------------
step "Инсталиране на работната среда (~/Library/Application Support/Flow)"
"$FLOW_SRC/scripts/sync-app-runtime.sh" > /dev/null
# запази избрания модел в настройките, без да пипаш език и др.
FLOW_MODEL="$MODEL" "$APP_SUPPORT/venv/bin/python" - <<'PY'
import json, os
path = os.path.expanduser("~/Library/Application Support/Flow/settings.json")
s = {}
try:
    with open(path) as f: s = json.load(f)
except Exception: pass
s["ollama_model"] = os.environ["FLOW_MODEL"]
with open(path, "w") as f: json.dump(s, f)
print("    settings.json: модел записан")
PY
ok "Работната среда е готова"

# --- 7. Копие в Applications ----------------------------------------------------
step "Инсталиране на Flow.app"
if [ -w "/Applications" ]; then APP_DEST="/Applications/Flow.app"; else APP_DEST="$HOME/Applications/Flow.app"; mkdir -p "$HOME/Applications"; fi
if [ "$FLOW_SRC/Flow.app" != "$APP_DEST" ]; then
  rm -rf "$APP_DEST"
  cp -R "$FLOW_SRC/Flow.app" "$APP_DEST"
fi
ok "Flow.app → $APP_DEST"

# --- 8. Автостарт при login (по избор) ------------------------------------------
BTN=$(dialog "Да стартира ли PrivaVox автоматично при включване на Mac-а?" "Не" "Да" 2>/dev/null || echo "Не")
if [[ "$BTN" == "Да" ]]; then
  /usr/bin/osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP_DEST\", hidden:false}" > /dev/null 2>&1 \
    && ok "Добавен в Login Items" \
    || echo "    (не успях — добави ръчно: System Settings → General → Login Items)"
fi

# --- 9. Старт --------------------------------------------------------------------
step "Стартиране на Flow"
dialog "Инсталацията завърши! Пускам PrivaVox.\n\nmacOS ще поиска няколко разрешения (Accessibility, Input Monitoring, Микрофон) — натискай Allow / включи Flow в списъка. Flow ще продължи сам след всяко.\n\nПолзване: задръж дясната ⌥ (Option), говори, пусни я." "Пусни PrivaVox" > /dev/null 2>&1
open "$APP_DEST"
ok "Flow е стартиран — виж иконата в Dock-а и HUD-а долу в центъра"
print -P "\n%F{green}%BГотово!%b%f Този прозорец може да се затвори.\n"
