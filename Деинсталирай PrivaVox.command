#!/bin/zsh
# PrivaVox деинсталатор за macOS — двоен клик.
# Премахва: работещото приложение, Flow.app (от /Applications или ~/Applications),
# ~/Library/Application Support/Flow (код, venv, настройки, лог, речник),
# ~/Library/Logs/Flow.log и login item-а. По избор — и свалените AI модели.
# Ollama остава инсталиран (споделен инструмент).
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_SUPPORT="$HOME/Library/Application Support/Flow"

step() { print -P "\n%F{cyan}==>%f %B$1%b" }
ok()   { print -P "    %F{green}✓%f $1" }
ask() {  # ask "въпрос" "Бутон1" "Бутон2(default)" → печата натиснатия
  /usr/bin/osascript -e "button returned of (display dialog \"$1\" buttons {\"$2\", \"$3\"} default button \"$3\" with title \"PrivaVox — деинсталиране\" with icon caution)" 2>/dev/null || echo "$2"
}

print -P "%B%F{magenta}  PrivaVox — деинсталиране%f%b"

[[ "$(ask 'Да премахна ли PrivaVox от този Mac?' 'Отказ' 'Премахни')" == "Премахни" ]] || { echo "Отказано."; exit 0; }

# избрания модел ПРЕДИ триене на настройките (за опцията по-долу)
MODEL=""
[ -f "$APP_SUPPORT/settings.json" ] && MODEL=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('ollama_model',''))" "$APP_SUPPORT/settings.json" 2>/dev/null || echo "")

step "Спиране на PrivaVox"
pkill -f "python.* -m flow" 2>/dev/null || true
sleep 1
ok "Приложението е спряно"

step "Премахване на Flow.app"
for dest in "/Applications/Flow.app" "$HOME/Applications/Flow.app"; do
  [ -e "$dest" ] && rm -rf "$dest" && ok "премахнато: $dest"
done

step "Премахване на login item-а"
/usr/bin/osascript -e 'tell application "System Events" to delete (every login item whose name is "Flow")' > /dev/null 2>&1 || true
ok "login item премахнат (ако е имало)"

step "Премахване на файловете"
[ -d "$APP_SUPPORT" ] && rm -rf "$APP_SUPPORT" && ok "премахнато: $APP_SUPPORT"
[ -f "$HOME/Library/Logs/Flow.log" ] && rm -f "$HOME/Library/Logs/Flow.log" && ok "логът е премахнат"

# по избор: AI моделите
if [[ "$(ask 'Да премахна ли и свалените AI модели (BgGPT ~2.5 GB + Whisper ~1.6 GB), за да освободя място? Ollama остава инсталиран.' 'Не' 'Да, премахни')" == "Да, премахни" ]]; then
  step "Премахване на AI моделите"
  if [ -n "$MODEL" ] && command -v ollama > /dev/null; then
    ollama rm "$MODEL" 2>/dev/null && ok "BgGPT модел премахнат: $MODEL" || true
  fi
  # само whisper моделите от HF кеша, не целия кеш
  hf="$HOME/.cache/huggingface"
  if [ -d "$hf" ]; then
    find "$hf" -type d \( -iname "*whisper*" -o -iname "*faster-whisper*" \) -maxdepth 3 -exec rm -rf {} + 2>/dev/null || true
    ok "Whisper кешът е премахнат"
  fi
fi

/usr/bin/osascript -e 'display dialog "PrivaVox е премахнат.\n\nOllama остана инсталиран — ако не го ползваш за друго: brew uninstall ollama (или го спри с brew services stop ollama)." buttons {"Готово"} default button "Готово" with title "PrivaVox — деинсталиране"' > /dev/null 2>&1
print -P "\n%F{green}%BГотово!%b%f Този прозорец може да се затвори.\n"
