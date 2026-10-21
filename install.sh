#!/bin/bash
# Лекция → Obsidian: установщик. Запуск: ./install.sh
# Всё ставится в домашнюю папку, пароль администратора не нужен.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/lecture2obsidian"
APPS="$HOME/Applications"
WHISPER_PATH="ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
WHISPER_SHA1="4af2b29d7ec73d781377bfd1758ca957a807e941"
REPO="sluchaikovins/lecture2obsidian"  # GitHub repo whose releases carry the prebuilt binaries (bin/ isn't in git)
SCRIPTS="process.py exam.py live.py recap.py telegram.py render.py osutil.py recorder.py README.md config.example.json"
BINARIES="recorder whisper-cli"  # bin/recorder: the old Swift recorder, kept as a fallback

bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  ✅ %s\n" "$*"; }
die()  { printf "\n❌ %s\n" "$*" >&2; exit 1; }

# Finds an app in /Applications or ~/Applications; prints its path.
find_app() {
  for d in /Applications "$APPS"; do [ -d "$d/$1.app" ] && { echo "$d/$1.app"; return; }; done
  return 1
}

NET_HINT="Похоже, интернет или провайдер не пускает на этот сайт. Что можно сделать:
   • проверить интернет и просто запустить установщик ещё раз — скачанное не пропадёт;
   • подключиться к другой сети (например, раздать интернет с телефона) или включить VPN;
   • сменить DNS на 1.1.1.1: Настройки → Сеть → Wi-Fi → Подробнее → DNS."

# True if the host answers at all (any HTTP status counts).
reachable() { curl -s -o /dev/null --max-time 15 -I "$1"; }

# Downloads a zip or dmg with an .app inside into ~/Applications; tries each URL in turn.
install_app() {
  local name="$1" url="" tmp; shift
  tmp="$(mktemp -d)"
  for u in "$@"; do
    if curl -L --fail --progress-bar -o "$tmp/pkg" "$u"; then url="$u"; break; fi
    echo "  Не получилось с $(echo "$u" | cut -d/ -f3), пробую другой источник…"
  done
  [ -n "$url" ] || die "Не удалось скачать $name.
$NET_HINT"
  mkdir -p "$APPS"
  if [[ "$url" == *.dmg ]]; then
    local mnt
    mnt="$(hdiutil attach -nobrowse -readonly "$tmp/pkg" | grep -o '/Volumes/.*' | head -1)"
    ditto "$mnt/$name.app" "$APPS/$name.app"
    hdiutil detach "$mnt" -quiet
  else
    ditto -xk "$tmp/pkg" "$APPS/"
  fi
  rm -rf "$tmp"
}

bold "🎓 Лекция → Obsidian: установка"

# ---------- 0. Проверки ----------
[ "$(uname -m)" = "arm64" ] || die "Нужен Mac на Apple Silicon (M1 и новее)."
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 13 ] || die "Нужна macOS 13 или новее."
ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
free_gb=$(( $(df -k "$HOME" | tail -1 | awk '{print $4}') / 1048576 ))
[ "$free_gb" -ge 20 ] || die "Нужно минимум 20 ГБ свободного места (сейчас $free_gb ГБ)."
ok "Mac: $(sysctl -n machdep.cpu.brand_string), ${ram_gb} ГБ памяти, macOS $(sw_vers -productVersion)"

# Check the sites we need before downloading gigabytes. Models only come from the Ollama registry.
reachable https://github.com || die "Не открывается github.com.
$NET_HINT"
reachable https://registry.ollama.ai/v2/ || die "Не открывается registry.ollama.ai — оттуда скачиваются модели для конспектов.
$NET_HINT"
reachable https://huggingface.co || reachable https://hf-mirror.com || die "Не открывается huggingface.co — оттуда скачивается модель распознавания речи.
$NET_HINT"
ok "Интернет: все нужные сайты открываются"

# ---------- 1. Command Line Tools (нужен python3) ----------
bold "1/6 Инструменты разработчика Apple"
if ! xcode-select -p >/dev/null 2>&1; then
  echo "  Откроется окно установки — нажмите «Установить» и дождитесь окончания (5–15 минут)."
  xcode-select --install >/dev/null 2>&1 || true
  until xcode-select -p >/dev/null 2>&1; do sleep 10; done
fi
ok "Готово"

# ---------- 2. Файлы программы ----------
bold "2/6 Копирую программу в ~/lecture2obsidian"
mkdir -p "$DEST"/{bin,models,recordings,hammerspoon}
if [ ! -f "$SRC/bin/whisper-cli" ]; then
  # Installing from a git clone: the compiled helpers come from the latest release.
  echo "  Скачиваю готовые программы из релиза на GitHub…"
  rel="$(mktemp -d)"
  curl -L --fail --progress-bar -o "$rel/l2o.zip" "https://github.com/$REPO/releases/latest/download/lecture2obsidian.zip" \
    || die "Не удалось скачать релиз с GitHub ($REPO)."
  ditto -xk "$rel/l2o.zip" "$rel"
  mkdir -p "$SRC/bin"
  for b in $BINARIES; do cp "$rel/lecture2obsidian/bin/$b" "$SRC/bin/"; done
  rm -rf "$rel"
fi
if [ "$SRC" != "$DEST" ]; then
  for f in $SCRIPTS install.sh; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"; done
  cp "$SRC/hammerspoon/lecture2obsidian.lua" "$DEST/hammerspoon/"
  for b in $BINARIES; do cp "$SRC/bin/$b" "$DEST/bin/"; done
fi
chmod +x "$DEST/bin/"* "$DEST/install.sh"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
ok "Готово"

echo "  Окружение Python и библиотеки (запись звука, картинки)…"
[ -x "$DEST/.venv/bin/python3" ] || /usr/bin/python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/python3" -m pip install -q --upgrade pip
"$DEST/.venv/bin/python3" -m pip install -q sounddevice pillow || die "Не удалось поставить библиотеки Python."
ok "Готово"

# ---------- 3. Приложения ----------
bold "3/6 Приложения: Obsidian, Ollama, Hammerspoon"
if OBSIDIAN="$(find_app Obsidian)"; then ok "Obsidian уже есть"; else
  echo "  Скачиваю Obsidian…"
  url="$(curl -s "https://api.github.com/repos/obsidianmd/obsidian-releases/releases?per_page=20" \
        | grep -o 'https://[^"]*/Obsidian-[0-9.]*\.dmg' | head -1)"
  [ -n "$url" ] || die "Не нашёл Obsidian для Mac на GitHub."
  install_app Obsidian "$url"; OBSIDIAN="$APPS/Obsidian.app"; ok "Obsidian установлен"
fi
if OLLAMA_APP="$(find_app Ollama)"; then ok "Ollama уже есть"; else
  echo "  Скачиваю Ollama…"
  install_app Ollama "https://github.com/ollama/ollama/releases/latest/download/Ollama-darwin.zip" \
    "https://ollama.com/download/Ollama-darwin.zip"; OLLAMA_APP="$APPS/Ollama.app"; ok "Ollama установлена"
fi
if HS_APP="$(find_app Hammerspoon)"; then ok "Hammerspoon уже есть"; else
  echo "  Скачиваю Hammerspoon…"
  url="$(curl -s https://api.github.com/repos/Hammerspoon/hammerspoon/releases/latest \
        | grep -o 'https://[^"]*Hammerspoon-[^"]*\.zip' | head -1)"
  [ -n "$url" ] || die "Не нашёл Hammerspoon на GitHub."
  install_app Hammerspoon "$url"; HS_APP="$APPS/Hammerspoon.app"; ok "Hammerspoon установлен"
fi
OLLAMA="$OLLAMA_APP/Contents/Resources/ollama"

# ---------- 4. Модель распознавания речи ----------
bold "4/6 Модель распознавания речи Whisper (1.6 ГБ)"
MODEL="$DEST/models/ggml-large-v3-turbo.bin"
if [ "$(shasum -a 1 "$MODEL" 2>/dev/null | cut -d' ' -f1)" != "$WHISPER_SHA1" ]; then
  for _ in 1 2 3 4 5; do
    host="huggingface.co"; reachable "https://$host" || host="hf-mirror.com"
    curl -L --fail -C - --progress-bar -o "$MODEL" "https://$host/$WHISPER_PATH" && break
    echo "  Связь оборвалась, продолжаю…"; sleep 3
  done
  [ "$(shasum -a 1 "$MODEL" | cut -d' ' -f1)" = "$WHISPER_SHA1" ] || die "Модель Whisper скачалась с ошибкой, запустите установщик ещё раз."
fi
ok "Готово"

# ---------- 5. Модель для конспектов ----------
if   [ "$ram_gb" -ge 24 ]; then LLM="qwen3:14b"; VISION="qwen3-vl:8b"; size="15 ГБ"
elif [ "$ram_gb" -ge 16 ]; then LLM="qwen3:8b";  VISION="qwen3-vl:4b"; size="8 ГБ"
else                            LLM="qwen3:4b";  VISION="";            size="2.5 ГБ"; fi
bold "5/6 Модели для конспектов и фото доски: $LLM ${VISION:+и $VISION} ($size)"
if ! curl -s http://127.0.0.1:11434/api/version >/dev/null; then
  nohup "$OLLAMA" serve >/dev/null 2>&1 &
  until curl -s http://127.0.0.1:11434/api/version >/dev/null; do sleep 1; done
fi
for m in $LLM $VISION; do
  until "$OLLAMA" pull "$m"; do echo "  Связь оборвалась, продолжаю…"; sleep 3; done
done
ok "Готово"

# ---------- 6. Хранилище Obsidian и хоткей ----------
bold "6/6 Хранилище Obsidian и хоткей F5"
OBS_JSON="$HOME/Library/Application Support/obsidian/obsidian.json"
VAULT="$(/usr/bin/python3 - "$OBS_JSON" <<'EOF'
import json, os, sys
try:
    vaults = [v["path"] for v in json.load(open(sys.argv[1]))["vaults"].values() if os.path.isdir(v["path"])]
except Exception:
    vaults = []
if len(vaults) == 1:
    print(vaults[0])
elif len(vaults) > 1:
    tty = open("/dev/tty", "r+")
    tty.write("  У вас несколько хранилищ Obsidian. Куда складывать лекции?\n")
    for i, v in enumerate(vaults, 1):
        tty.write(f"    {i}) {v}\n")
    tty.write("  Номер: "); tty.flush()
    try:
        print(vaults[int(tty.readline()) - 1])
    except Exception:
        print(vaults[0])
EOF
)"
if [ -z "$VAULT" ]; then
  # Хранилищ нет — создаём ~/Obsidian и регистрируем его, чтобы Obsidian сразу его открыл.
  VAULT="$HOME/Obsidian"
  mkdir -p "$VAULT/.obsidian" "$(dirname "$OBS_JSON")"
  if ! pgrep -x Obsidian >/dev/null; then
    /usr/bin/python3 - "$OBS_JSON" "$VAULT" <<'EOF'
import json, os, sys, time, secrets
path, vault = sys.argv[1], sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) else {}
data.setdefault("vaults", {})[secrets.token_hex(8)] = {"path": vault, "ts": int(time.time() * 1000), "open": True}
json.dump(data, open(path, "w"))
EOF
  fi
fi
LECTURES="$VAULT/Лекции"
mkdir -p "$LECTURES/Audio"
ok "Лекции будут сохраняться в: $LECTURES"

# Existing settings win (re-running the installer = update); new settings get defaults.
/usr/bin/python3 - "$DEST/config.json" "$LECTURES" "$LLM" "$OLLAMA" "$VISION" <<'EOF'
import json, os, sys
path, vault, model, ollama, vision = sys.argv[1:6]
cfg = json.load(open(path)) if os.path.exists(path) else {}
defaults = {"vault": vault, "model": model, "lang": "ru", "ollama": ollama,
            "vision": vision or None, "photo_dirs": ["~/Downloads", "~/Desktop"]}
for k, v in defaults.items():
    cfg.setdefault(k, v)
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
EOF
LECTURES="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["vault"])' "$DEST/config.json")"

# Свои предметы: лекции раскладываются только по ним, модель не придумывает папки.
if ! /usr/bin/python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("subjects") else 1)' "$DEST/config.json"; then
  echo
  echo "  По каким предметам будете записывать лекции? Через запятую, например:"
  echo "  Программирование, Матанализ, Физика, История"
  printf "  Предметы (Enter — пропустить, можно задать потом в меню 🎓): "
  read -r subjects_line < /dev/tty || subjects_line=""
  /usr/bin/python3 - "$DEST/config.json" "$subjects_line" <<'EOF'
import json, re, sys
path, line = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
cfg["subjects"] = [x.strip() for x in re.split(r"[,;]", line) if x.strip()]
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
EOF
fi
VAULT="$(dirname "$LECTURES")"

# Карточки: плагин Spaced Repetition прямо в хранилище.
PLUGIN="$VAULT/.obsidian/plugins/obsidian-spaced-repetition"
if [ ! -f "$PLUGIN/main.js" ]; then
  mkdir -p "$PLUGIN"
  rel="$(curl -s https://api.github.com/repos/st3v3nmw/obsidian-spaced-repetition/releases/latest)"
  for f in main.js manifest.json styles.css; do
    u="$(echo "$rel" | grep -o "https://[^\"]*/$f" | head -1)"
    [ -n "$u" ] && curl -sL --fail -o "$PLUGIN/$f" "$u"
  done
fi
/usr/bin/python3 - "$VAULT/.obsidian/community-plugins.json" <<'EOF'
import json, os, sys
path = sys.argv[1]
plugins = json.load(open(path)) if os.path.exists(path) else []
if "obsidian-spaced-repetition" not in plugins:
    plugins.append("obsidian-spaced-repetition")
json.dump(plugins, open(path, "w"))
EOF
ok "Плагин карточек Spaced Repetition установлен"

mkdir -p "$HOME/.hammerspoon"
LINE='dofile(os.getenv("HOME") .. "/lecture2obsidian/hammerspoon/lecture2obsidian.lua")'
grep -qF "$LINE" "$HOME/.hammerspoon/init.lua" 2>/dev/null || echo "$LINE" >> "$HOME/.hammerspoon/init.lua"
pkill -x Hammerspoon 2>/dev/null || true; sleep 1
open "$HS_APP"
ok "Hammerspoon запущен"

bold "🎉 Готово! Осталось 3 шага:"
cat <<EOF

  1. Разрешите Hammerspoon «Универсальный доступ» — сейчас откроются настройки,
     включите там переключатель у Hammerspoon.
  2. Нажмите F5 (или fn+F5), скажите пару фраз, нажмите F5 ещё раз.
     macOS спросит доступ к микрофону — разрешите.
     Через минуту придёт уведомление, а конспект появится в Obsidian в папке «Лекции».
  3. В Obsidian: Настройки → Сторонние плагины → «Включить сторонние плагины»,
     чтобы заработали карточки для повторения (Spaced Repetition).

  Во время записи F6 (fn+F6) — скриншот слайда в конспект (macOS спросит доступ
  к записи экрана). Фото доски с айфона скиньте по AirDrop — они сами попадут в конспект.
  Подготовка к экзамену: 🎓 в строке меню → «📝 Подготовка к экзамену» → предмет.

  В строке меню: 🎓 — ждёт, 🔴 — идёт запись, ⏳ — делается конспект.
EOF
sleep 2
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
