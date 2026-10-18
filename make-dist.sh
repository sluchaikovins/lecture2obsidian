#!/bin/bash
# Собирает архив для раздачи друзьям: ~/Desktop/lecture2obsidian.zip
set -euo pipefail
cd "$(dirname "$0")"
tmp="$(mktemp -d)"; pkg="$tmp/lecture2obsidian"
mkdir -p "$pkg/bin" "$pkg/hammerspoon"
cp install.sh process.py exam.py live.py recap.py telegram.py README.md config.example.json "$pkg/"
cp hammerspoon/lecture2obsidian.lua "$pkg/hammerspoon/"
cp bin/recorder bin/whisper-cli bin/render "$pkg/bin/"
(cd "$tmp" && ditto -c -k --norsrc --noextattr --keepParent lecture2obsidian "$HOME/Desktop/lecture2obsidian.zip")
rm -rf "$tmp"
echo "Готово: ~/Desktop/lecture2obsidian.zip ($(du -h "$HOME/Desktop/lecture2obsidian.zip" | cut -f1))"
