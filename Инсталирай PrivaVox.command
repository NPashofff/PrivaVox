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
# какво вече е в Ollama (за да не теглим излишно и за избор на собствен модел)
INSTALLED=$(ollama ls 2>/dev/null | awk 'NR>1 && $1!="" {print $1}')
have_model() {  # точен таг, „име" ≡ „име:latest" в двете посоки
  print -r -- "$INSTALLED" | awk -v m="$1" 'BEGIN{if(m!~/:/)m=m":latest"} {t=$1; if(t!~/:/)t=t":latest"; if(t==m)f=1} END{exit !f}'
}

# градим списъка за диалога + карта етикет→таг
typeset -A LABEL2TAG
CHOICES=()
add_choice() {  # таг  базов-етикет  размер-за-сваляне
  local tag="$1" base="$2" size="$3" label
  if have_model "$tag"; then label="$base  —  ✓ вече наличен"; else label="$base  —  $size за сваляне"; fi
  CHOICES+=("$label"); LABEL2TAG[$label]="$tag"
}
add_choice "todorov/bggpt:latest" "BgGPT 4B — ПРЕПОРЪЧИТЕЛЕН (баланс качество/скорост)" "2.5 GB"
add_choice "todorov/bggpt:Gemma-3-12B-IT-Q4_K_M" "BgGPT 12B — по-качествен, ~3x по-бавен" "7.3 GB"
add_choice "hf.co/INSAIT-Institute/BgGPT-Gemma-2-2.6B-IT-v1.0-GGUF:Q4_K_M" "BgGPT 2.6B — най-лек" "1.7 GB"
# другите вече инсталирани модели (не-BgGPT) — избор без сваляне
for t in ${(f)INSTALLED}; do
  case "${t:l}" in
    *bggpt*) ;;  # BgGPT-ите вече са горе
    *) local l="$t  —  ✓ твой наличен модел"; CHOICES+=("$l"); LABEL2TAG[$l]="$t" ;;
  esac
done
CUSTOM_LABEL="Друг модел… (въведи име на Ollama модел ръчно)"
CHOICES+=("$CUSTOM_LABEL")
REC_LABEL="${CHOICES[1]}"

# AppleScript списък-литерал от CHOICES (екранираме кавичките)
aslist=""
for it in "${CHOICES[@]}"; do aslist+="\"${it//\"/\\\"}\", "; done
aslist="${aslist%, }"
recEsc="${REC_LABEL//\"/\\\"}"
CHOICE=$(/usr/bin/osascript <<OSA
set sel to choose from list {$aslist} with title "PrivaVox — избор на AI модел" with prompt "Кой AI модел да ползва PrivaVox за изчистване на текста? Отбелязаните като вече наличен не се теглят. Може да смениш и по-късно от менюто." default items {"$recEsc"} OK button name "Избери" cancel button name "Отказ"
if sel is false then return "__cancel__"
return item 1 of sel
OSA
)
[[ "$CHOICE" == "__cancel__" ]] && { echo "Отказано от потребителя."; exit 0; }

if [[ "$CHOICE" == "$CUSTOM_LABEL" ]]; then
  MODEL=$(/usr/bin/osascript -e 'text returned of (display dialog "Име на Ollama модел (напр. llama3.1:8b, qwen2.5:7b, todorov/bggpt:latest).\n\nЩе бъде свален, ако още го няма. Виж ollama.com/library за наличните." default answer "" with title "PrivaVox — собствен AI модел" buttons {"Отказ","OK"} default button "OK")' 2>/dev/null || echo "")
  [[ -z "$MODEL" ]] && { echo "Отказано."; exit 0; }
else
  MODEL="${LABEL2TAG[$CHOICE]}"
fi

if have_model "$MODEL"; then
  ok "Моделът $MODEL вече е наличен (няма да се тегли)"
else
  echo "    свалям $MODEL (прогресът е по-долу)..."
  ollama pull "$MODEL" || fail_dialog "Свалянето на $MODEL не успя. Провери името/интернет връзката и пусни инсталатора отново."
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
# Свали Gatekeeper карантината от копието — иначе първото пускане удря
# „Apple could not verify…" (приложението не е нотаризирано). Кодът е локален.
xattr -dr com.apple.quarantine "$APP_DEST" 2>/dev/null || true
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
