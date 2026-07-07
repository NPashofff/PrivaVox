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
# Двуезичен помощник: arg1 = български, arg2 = английски. Избира по $LANG (bg|en).
t() { if [[ "$LANG" == "en" ]]; then print -r -- "$2"; else print -r -- "$1"; fi }
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

# --- 0. Процесор (Apple Silicon) ----------------------------------------------
step "$(t 'Проверка на процесора' 'Checking the processor')"
if [[ "$(uname -m)" != "arm64" ]]; then
  fail_dialog "$(t 'PrivaVox изисква Mac с Apple Silicon (M-серия процесор).\n\nТози Mac е с Intel процесор, а AI библиотеките (MLX) нямат Intel версии.' 'PrivaVox requires a Mac with Apple Silicon (M-series chip).\n\nThis Mac has an Intel processor, and the AI libraries (MLX) have no Intel builds.')"
fi
ok "$(t 'Apple Silicon' 'Apple Silicon')"

# --- 0.5. Избор на език / Language selection ----------------------------------
# ВИНАГИ питаме; без предварителен избор. Задава $LANG=bg|en за целия инсталатор.
choice=$(/usr/bin/osascript -e 'button returned of (display dialog "Изберете език / Choose language" buttons {"Български","English"} default button "Български" with title "PrivaVox")')
case "$choice" in English) LANG=en ;; *) LANG=bg ;; esac

print -P "%B%F{magenta}"
print -r -- "$(t '  PrivaVox — локална диктовка (EN/BG)' '  PrivaVox — local dictation (EN/BG)')"
print -r -- "$(t '  Инсталаторът ще подготви всичко; ще те попита само за AI модела.' '  The installer will set up everything; it will only ask you about the AI model.')"
print -P "%f%b"

# --- 1. Homebrew ------------------------------------------------------------
step "$(t 'Проверка за Homebrew' 'Checking for Homebrew')"
if ! command -v brew > /dev/null; then
  fail_dialog "$(t 'Липсва Homebrew (мениджър на пакети).\n\nИнсталирай го от https://brew.sh (една команда в Terminal), после пусни инсталатора отново.' 'Homebrew (package manager) is missing.\n\nInstall it from https://brew.sh (one command in Terminal), then run the installer again.')"
fi
ok "$(t 'Homebrew е наличен' 'Homebrew is available')"

# --- 2. Ollama --------------------------------------------------------------
step "$(t 'Ollama (локален LLM сървър)' 'Ollama (local LLM server)')"
if ! command -v ollama > /dev/null; then
  echo "    $(t 'инсталирам ollama през brew...' 'installing ollama via brew...')"
  brew install ollama
fi
if ! curl -s --max-time 2 http://localhost:11434/api/version > /dev/null; then
  if brew list ollama > /dev/null 2>&1; then
    echo "    $(t 'стартирам ollama като услуга...' 'starting ollama as a service...')"
    brew services start ollama || true
  else
    # ollama е инсталиран извън brew (DMG приложението) — пускаме самото app
    echo "    $(t 'стартирам приложението Ollama...' 'starting the Ollama app...')"
    open -a Ollama > /dev/null 2>&1 || true
  fi
  for i in {1..15}; do
    curl -s --max-time 2 http://localhost:11434/api/version > /dev/null && break
    sleep 1
  done
fi
curl -s --max-time 2 http://localhost:11434/api/version > /dev/null || fail_dialog "$(t 'Ollama не тръгна. Пусни ръчно: brew services start ollama — и опитай пак.' 'Ollama did not start. Run manually: brew services start ollama — then try again.')"
ok "$(t 'Ollama върви' 'Ollama is running')"

# --- 3. uv + Python среда ----------------------------------------------------
step "$(t 'Python среда (uv venv + зависимости)' 'Python environment (uv venv + dependencies)')"
if ! command -v uv > /dev/null; then
  echo "    $(t 'инсталирам uv през brew...' 'installing uv via brew...')"
  brew install uv
fi
if [ ! -x "$FLOW_SRC/.venv/bin/python" ]; then
  (cd "$FLOW_SRC" && uv venv --python 3.12 .venv)
fi
(cd "$FLOW_SRC" && uv pip install --quiet --python .venv/bin/python -r requirements.txt)
ok "$(t 'Python средата е готова' 'The Python environment is ready')"

# --- 4. Избор на AI модел -----------------------------------------------------
step "$(t 'Избор на AI модел за изчистване на текста' 'Choosing the AI model for text cleanup')"
# какво вече е в Ollama (за да не теглим излишно и за избор на собствен модел)
INSTALLED=$(ollama ls 2>/dev/null | awk 'NR>1 && $1!="" {print $1}')
have_model() {  # точен таг, „име" ≡ „име:latest" в двете посоки
  print -r -- "$INSTALLED" | awk -v m="$1" 'BEGIN{if(m!~/:/)m=m":latest"} {t=$1; if(t!~/:/)t=t":latest"; if(t==m)f=1} END{exit !f}'
}

# локализирани фрагменти за етикетите (без ASCII кавички — влизат в AppleScript списък)
L_AVAIL="$(t 'вече наличен' 'already installed')"
L_DOWNLOAD="$(t 'за сваляне' 'to download')"
L_YOURS="$(t 'твой наличен модел' 'your installed model')"

# градим списъка за диалога + карта етикет→таг
typeset -A LABEL2TAG
CHOICES=()
add_choice() {  # таг  базов-етикет  размер-за-сваляне
  local tag="$1" base="$2" size="$3" label
  if have_model "$tag"; then label="$base  —  ✓ $L_AVAIL"; else label="$base  —  $size $L_DOWNLOAD"; fi
  CHOICES+=("$label"); LABEL2TAG[$label]="$tag"
}
add_choice "todorov/bggpt:latest" "$(t 'BgGPT 4B — ПРЕПОРЪЧИТЕЛЕН (баланс качество/скорост)' 'BgGPT 4B — RECOMMENDED (balance of quality/speed)')" "2.5 GB"
add_choice "todorov/bggpt:Gemma-3-12B-IT-Q4_K_M" "$(t 'BgGPT 12B — по-качествен, ~3x по-бавен' 'BgGPT 12B — higher quality, ~3x slower')" "7.3 GB"
add_choice "hf.co/INSAIT-Institute/BgGPT-Gemma-2-2.6B-IT-v1.0-GGUF:Q4_K_M" "$(t 'BgGPT 2.6B — най-лек' 'BgGPT 2.6B — lightest')" "1.7 GB"
# другите вече инсталирани модели (не-BgGPT) — избор без сваляне
for t in ${(f)INSTALLED}; do
  case "${t:l}" in
    *bggpt*) ;;  # BgGPT-ите вече са горе
    *) local l="$t  —  ✓ $L_YOURS"; CHOICES+=("$l"); LABEL2TAG[$l]="$t" ;;
  esac
done
CUSTOM_LABEL="$(t 'Друг модел… (въведи име на Ollama модел ръчно)' 'Other model… (enter an Ollama model name manually)')"
CHOICES+=("$CUSTOM_LABEL")
REC_LABEL="${CHOICES[1]}"

# AppleScript списък-литерал от CHOICES (екранираме кавичките)
aslist=""
for it in "${CHOICES[@]}"; do aslist+="\"${it//\"/\\\"}\", "; done
aslist="${aslist%, }"
recEsc="${REC_LABEL//\"/\\\"}"
AS_TITLE="$(t 'PrivaVox — избор на AI модел' 'PrivaVox — choose AI model')"
AS_PROMPT="$(t 'Кой AI модел да ползва PrivaVox за изчистване на текста? Отбелязаните като вече наличен не се теглят. Може да смениш и по-късно от менюто.' 'Which AI model should PrivaVox use for text cleanup? Those marked already installed are not downloaded. You can change it later from the menu.')"
AS_OK="$(t 'Избери' 'Select')"
AS_CANCEL="$(t 'Отказ' 'Cancel')"
CHOICE=$(/usr/bin/osascript <<OSA
set sel to choose from list {$aslist} with title "$AS_TITLE" with prompt "$AS_PROMPT" default items {"$recEsc"} OK button name "$AS_OK" cancel button name "$AS_CANCEL"
if sel is false then return "__cancel__"
return item 1 of sel
OSA
)
[[ "$CHOICE" == "__cancel__" ]] && { echo "$(t 'Отказано от потребителя.' 'Cancelled by the user.')"; exit 0; }

if [[ "$CHOICE" == "$CUSTOM_LABEL" ]]; then
  CM_MSG="$(t 'Име на Ollama модел (напр. llama3.1:8b, qwen2.5:7b, todorov/bggpt:latest).\n\nЩе бъде свален, ако още го няма. Виж ollama.com/library за наличните.' 'Ollama model name (e.g. llama3.1:8b, qwen2.5:7b, todorov/bggpt:latest).\n\nIt will be downloaded if not present yet. See ollama.com/library for what is available.')"
  CM_TITLE="$(t 'PrivaVox — собствен AI модел' 'PrivaVox — custom AI model')"
  CM_CANCEL="$(t 'Отказ' 'Cancel')"
  MODEL=$(/usr/bin/osascript -e "text returned of (display dialog \"$CM_MSG\" default answer \"\" with title \"$CM_TITLE\" buttons {\"$CM_CANCEL\",\"OK\"} default button \"OK\")" 2>/dev/null || echo "")
  [[ -z "$MODEL" ]] && { echo "$(t 'Отказано.' 'Cancelled.')"; exit 0; }
else
  MODEL="${LABEL2TAG[$CHOICE]}"
fi

if have_model "$MODEL"; then
  ok "$(t "Моделът $MODEL вече е наличен (няма да се тегли)" "Model $MODEL is already installed (will not be downloaded)")"
else
  echo "    $(t "свалям $MODEL (прогресът е по-долу)..." "downloading $MODEL (progress below)...")"
  ollama pull "$MODEL" || fail_dialog "$(t "Свалянето на $MODEL не успя. Провери името/интернет връзката и пусни инсталатора отново." "Downloading $MODEL failed. Check the name/internet connection and run the installer again.")"
fi
ok "$(t "AI модел: $MODEL" "AI model: $MODEL")"

# --- 5. Whisper STT модел ------------------------------------------------------
step "$(t 'Whisper STT модел (~1.6 GB, еднократно)' 'Whisper STT model (~1.6 GB, one-time)')"
(cd "$FLOW_SRC" && .venv/bin/python - <<'PY'
from flow.config import FlowConfig
from flow import stt
secs = stt.warm_up(FlowConfig())
print(f"    Whisper готов ({secs:.1f}s)")
PY
)
ok "$(t 'Whisper е наличен и зареден' 'Whisper is present and loaded')"

# --- 6. Работна среда на приложението -----------------------------------------
step "$(t 'Инсталиране на работната среда (~/Library/Application Support/Flow)' 'Installing the app runtime (~/Library/Application Support/Flow)')"
"$FLOW_SRC/scripts/sync-app-runtime.sh" > /dev/null
# запази избрания модел + UI езика в настройките, без да губиш другите ключове.
export FLOW_UILANG="$LANG"
FLOW_MODEL="$MODEL" "$APP_SUPPORT/venv/bin/python" - <<'PY'
import json, os
path = os.path.expanduser("~/Library/Application Support/Flow/settings.json")
s = {}
try:
    with open(path) as f: s = json.load(f)
except Exception: pass
s["ollama_model"] = os.environ["FLOW_MODEL"]
s["ui_language"] = os.environ["FLOW_UILANG"]
with open(path, "w") as f: json.dump(s, f)
print("    settings.json: модел записан")
PY
ok "$(t 'Работната среда е готова' 'The app runtime is ready')"

# --- 7. Копие в Applications ----------------------------------------------------
step "$(t 'Инсталиране на Flow.app' 'Installing Flow.app')"
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
AUTO_YES="$(t 'Да' 'Yes')"
AUTO_NO="$(t 'Не' 'No')"
BTN=$(dialog "$(t 'Да стартира ли PrivaVox автоматично при включване на Mac-а?' 'Should PrivaVox start automatically when the Mac turns on?')" "$AUTO_NO" "$AUTO_YES" 2>/dev/null || echo "$AUTO_NO")
if [[ "$BTN" == "$AUTO_YES" ]]; then
  /usr/bin/osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP_DEST\", hidden:false}" > /dev/null 2>&1 \
    && ok "$(t 'Добавен в Login Items' 'Added to Login Items')" \
    || echo "    $(t '(не успях — добави ръчно: System Settings → General → Login Items)' '(failed — add manually: System Settings → General → Login Items)')"
fi

# --- 9. Старт --------------------------------------------------------------------
step "$(t 'Стартиране на Flow' 'Starting Flow')"
dialog "$(t 'Инсталацията завърши! Пускам PrivaVox.\n\nmacOS ще поиска няколко разрешения (Accessibility, Input Monitoring, Микрофон) — натискай Allow / включи Flow в списъка. Flow ще продължи сам след всяко.\n\nПолзване: задръж дясната ⌥ (Option), говори, пусни я.' 'Installation complete! Launching PrivaVox.\n\nmacOS will ask for a few permissions (Accessibility, Input Monitoring, Microphone) — click Allow / enable Flow in the list. Flow will continue on its own after each.\n\nUsage: hold the right ⌥ (Option), speak, release it.')" "$(t 'Пусни PrivaVox' 'Launch PrivaVox')" > /dev/null 2>&1
open "$APP_DEST"
ok "$(t 'Flow е стартиран — виж иконата в Dock-а и HUD-а долу в центъра' 'Flow has started — see the Dock icon and the HUD at the bottom center')"
print -P "\n%F{green}%B$(t 'Готово!' 'Done!')%b%f $(t 'Този прозорец може да се затвори.' 'You can close this window.')\n"
