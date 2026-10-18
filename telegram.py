#!/usr/bin/env python3
"""Sends lecture notes to Telegram as pictures: markdown → HTML → PNG (bin/render) → sendMediaGroup.

Config (config.json): "telegram": {"token": "<from @BotFather>", "chat_id": "<filled in by `telegram.py setup`>"}

Usage: telegram.py setup        — finds your chat with the bot (send the bot any message first)
       telegram.py test         — sends a test picture
"""
import html
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RENDER = ROOT / "bin" / "render"
PART_CHARS = 1400  # text per picture: about one phone screen at a readable size

CSS = """<style>
body { font-family: -apple-system, 'Helvetica Neue'; font-size: 17px; color: #1f1d1a; line-height: 1.35; }
h1 { font-size: 24px; margin: 0 0 6px 0; }
h2 { font-size: 21px; color: #7a3e12; margin: 14px 0 6px 0; }
h3 { font-size: 18px; color: #2d5a87; margin: 12px 0 4px 0; }
p, li { margin: 3px 0; }
.meta { color: #8a8378; font-size: 14px; }
table { border-collapse: collapse; } td, th { border: 1px solid #d6cfc2; padding: 4px 6px; font-size: 15px; }
</style>"""


def config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def enabled() -> bool:
    tg = config().get("telegram") or {}
    return bool(tg.get("token") and tg.get("chat_id"))


def api(method: str, fields=None, files=None):
    token = config()["telegram"]["token"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (fields or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for name, path in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{Path(path).name}\"\r\n"
                     f"Content-Type: image/png\r\n\r\n").encode() + Path(path).read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(fields or {}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        reply = json.load(r)
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram: {reply.get('description')}")
    return reply["result"]


# ---------- markdown → pictures ----------

def plain_math(s: str) -> str:
    """LaTeX doesn't render in a picture; turn the common bits into readable text."""
    def conv(m):
        t = m.group(1)
        for a, b in [(r"\to", "→"), (r"\infty", "∞"), (r"\cdot", "·"), (r"\times", "×"), (r"\le", "≤"), (r"\ge", "≥"),
                     (r"\ne", "≠"), (r"\pm", "±"), (r"\Delta", "Δ"), (r"\alpha", "α"), (r"\beta", "β"), (r"\pi", "π"),
                     (r"\sqrt", "√"), (r"\int", "∫"), (r"\sum", "Σ"), (r"\lim", "lim"), (r"\sin", "sin"), (r"\cos", "cos"),
                     (r"\ln", "ln"), (r"\left", ""), (r"\right", ""), (r"\,", " "), ("\\", "")]:
            t = t.replace(a, b)
        t = re.sub(r"frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
        return t.replace("{", "").replace("}", "")
    s = re.sub(r"\$\$(.+?)\$\$", conv, s, flags=re.S)
    return re.sub(r"\$([^$\n]+)\$", conv, s)


def inline(s: str) -> str:
    s = html.escape(plain_math(s), quote=False)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)\*(?!\w)", r"<i>\1</i>", s)
    return re.sub(r"`([^`]+)`", r"\1", s)


def md_to_html(md: str) -> str:
    out, in_list, table = [], None, []

    def close():
        nonlocal in_list, table
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if table:
            rows = ["<tr>" + "".join(f"<td><b>{inline(c)}</b></td>" for c in table[0]) + "</tr>"]
            rows += ["<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in table[1:]]
            out.append("<table>" + "".join(rows) + "</table>")
            table = []

    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#flashcards") or s.startswith("![["):
            close()
            continue
        if s.startswith("|"):
            if not re.fullmatch(r"[|:\-\s]+", s):
                table.append([c.strip() for c in s.strip("|").split("|")])
            continue
        if table:
            close()
        m = re.match(r"(#{1,3})\s+(.*)", s)
        if m:
            close()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
            continue
        m = re.match(r"([-*+]|\d+[.)])\s+(.*)", s)
        if m:
            kind = "ol" if m.group(1)[0].isdigit() else "ul"
            if in_list != kind:
                close()
                out.append(f"<{kind}>")
                in_list = kind
            indent = "&nbsp;" * 4 if line.startswith("  ") else ""  # nested bullet
            out.append(f"<li>{indent}{inline(m.group(2))}</li>")
            continue
        close()
        if "::" in s:  # flashcard "Q::A"
            q, _, a = s.partition("::")
            s = f"**{q.strip()}** — {a.strip()}"
        out.append(f"<p>{inline(s)}</p>")
    close()
    return "\n".join(out)


def split_parts(md: str, limit: int = PART_CHARS):
    """Cut at headings first, then at paragraph breaks, so each picture is one readable chunk."""
    blocks = re.split(r"\n(?=#{2,3} )", md.strip())
    parts, cur = [], ""
    for b in blocks:
        # Never inside a table: a table cut in two loses its header row.
        pieces = [b] if len(b) <= limit else re.split(r"\n(?=[-*+] |\d+[.)] |[^\s|])", b)
        for p in pieces:
            if cur and len(cur) + len(p) > limit:
                parts.append(cur.strip())
                cur = ""
            cur += "\n" + p
    if cur.strip():
        parts.append(cur.strip())
    return parts


def render(md_parts, header: str, tmp: Path):
    """PNG paths, one per part; every picture carries the header so it makes sense on its own."""
    paths = []
    for i, part in enumerate(md_parts, 1):
        page = f"{i}/{len(md_parts)}" if len(md_parts) > 1 else ""
        doc = (f"<html><head><meta charset='utf-8'>{CSS}</head><body>"
               f"<p class='meta'>{html.escape(header)} {page}</p>{md_to_html(part)}</body></html>")
        src, png = tmp / f"p{i}.html", tmp / f"p{i}.png"
        src.write_text(doc, encoding="utf-8")
        subprocess.run([str(RENDER), str(src), str(png)], check=True)
        paths.append(png)
    return paths


def send_markdown(md: str, header: str, caption: str = ""):
    """Renders `md` into pictures and sends them as albums (Telegram allows 10 per album)."""
    chat = config()["telegram"]["chat_id"]
    with tempfile.TemporaryDirectory() as t:
        pngs = render(split_parts(md), header, Path(t))
        for start in range(0, len(pngs), 10):
            group = pngs[start:start + 10]
            if len(group) == 1:
                api("sendPhoto", {"chat_id": chat, "caption": caption[:1024]}, {"photo": group[0]})
            else:
                media = [{"type": "photo", "media": f"attach://p{k}"} for k in range(len(group))]
                if start == 0 and caption:
                    media[0]["caption"] = caption[:1024]
                api("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)},
                    {f"p{k}": p for k, p in enumerate(group)})
    return len(pngs)


def setup():
    cfg = config()
    token = (cfg.get("telegram") or {}).get("token")
    if not token:
        sys.exit("Сначала впишите токен бота в config.json → telegram.token")
    updates = api("getUpdates")
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in updates if "message" in u}
    if not chats:
        sys.exit("Бот пока не получил ни одного сообщения — откройте его в Telegram, нажмите Start и запустите снова")
    chat_id, name = list(chats.items())[-1]
    cfg["telegram"]["chat_id"] = str(chat_id)
    (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    api("sendMessage", {"chat_id": chat_id, "text": f"✅ Готово, {name}! Сюда будут приходить конспекты лекций."})
    print(f"chat_id = {chat_id} ({name})")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["test"]:
        n = send_markdown("## Проверка\n- Картинки конспектов будут выглядеть **вот так**.\n- Формула: $\\frac{a}{b} \\to \\infty$",
                          "🎓 Лекция → Obsidian", "Тестовая картинка")
        print(f"sent {n} picture(s)")
    else:
        sys.exit(__doc__)
