#!/bin/bash
# Собирает архивы для релиза на рабочий стол:
#   lecture2obsidian.zip          — macOS (с готовыми программами в bin/)
#   lecture2obsidian-windows.zip  — Windows, бета (программы скачает install.ps1)
set -euo pipefail
cd "$(dirname "$0")"
SCRIPTS="process.py exam.py live.py recap.py telegram.py render.py osutil.py recorder.py README.md config.example.json"
tmp="$(mktemp -d)"

pkg="$tmp/mac/lecture2obsidian"
mkdir -p "$pkg/bin" "$pkg/hammerspoon"
cp $SCRIPTS install.sh "$pkg/"
cp hammerspoon/lecture2obsidian.lua "$pkg/hammerspoon/"
cp bin/recorder bin/whisper-cli "$pkg/bin/"
(cd "$tmp/mac" && ditto -c -k --norsrc --noextattr --keepParent lecture2obsidian "$HOME/Desktop/lecture2obsidian.zip")

pkg="$tmp/win/lecture2obsidian"
mkdir -p "$pkg"
cp $SCRIPTS tray.py install.cmd "$pkg/"
# Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI and garbles the Russian text.
printf '\xef\xbb\xbf' > "$pkg/install.ps1"; cat install.ps1 >> "$pkg/install.ps1"
(cd "$tmp/win" && ditto -c -k --norsrc --noextattr --keepParent lecture2obsidian "$HOME/Desktop/lecture2obsidian-windows.zip")

rm -rf "$tmp"
for z in lecture2obsidian.zip lecture2obsidian-windows.zip; do
  echo "Готово: ~/Desktop/$z ($(du -h "$HOME/Desktop/$z" | cut -f1))"
done
