"""Markdown → phone-friendly PNG with Pillow (replaces the macOS-only Swift renderer).

Handles what the notes use: ##/### headings, bullets and numbered lists (one nesting level), paragraphs,
**bold**, *italic*, tables and flashcards (Q::A). Drawn at 2x for sharp text on phones.
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SCALE = 2
WIDTH = 720 * SCALE
PAD = 36 * SCALE
PAPER, INK, MUTED = (252, 250, 245), (31, 29, 26), (138, 131, 120)
H2, H3, RULE = (122, 62, 18), (45, 90, 135), (214, 207, 194)
SIZES = {"meta": 14, "h1": 24, "h2": 21, "h3": 18, "p": 17, "li": 17, "td": 15}

FONT_FILES = {
    "regular": ["/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf",
             "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "italic": ["/System/Library/Fonts/Supplemental/Arial Italic.ttf", "C:/Windows/Fonts/segoeuii.ttf",
               "C:/Windows/Fonts/ariali.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"],
}
_fonts = {}


def font(style: str, size: int):
    key = (style, size)
    if key not in _fonts:
        path = next((p for p in FONT_FILES[style] if Path(p).exists()), None)
        _fonts[key] = ImageFont.truetype(path, size * SCALE) if path else ImageFont.load_default(size * SCALE)
    return _fonts[key]


# ---------- markdown → blocks of styled runs ----------

NO_GLYPH = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]")  # emoji: system text fonts don't have them


def plain_math(s: str) -> str:
    """LaTeX doesn't render here; turn the common bits into readable text."""
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


def runs(text: str):
    """[(text, style)] with style regular/bold/italic."""
    text = NO_GLYPH.sub("", plain_math(text))
    text = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text).replace("`", "")
    out = []
    for part in re.split(r"(\*\*.+?\*\*|(?<![*\w])\*(?!\s).+?\*(?!\w))", text):
        if not part:
            continue
        if part.startswith("**"):
            out.append((part[2:-2], "bold"))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], "italic"))
        else:
            out.append((part, "regular"))
    return out


def blocks(md: str):
    out, table = [], []
    for line in md.splitlines() + [""]:
        s = line.strip()
        if s.startswith("|"):
            if not re.fullmatch(r"[|:\-\s]+", s):
                table.append([c.strip() for c in s.strip("|").split("|")])
            continue
        if table:
            out.append(("table", table))
            table = []
        if not s or s.startswith("#flashcards") or s.startswith("![[") or s.startswith(">"):
            continue
        m = re.match(r"(#{1,3})\s+(.*)", s)
        if m:
            out.append((f"h{len(m.group(1))}", runs(m.group(2))))
            continue
        m = re.match(r"([-*+]|\d+[.)])\s+(.*)", s)
        if m:
            marker = "•" if not m.group(1)[0].isdigit() else m.group(1)
            out.append(("li", runs(m.group(2)), 1 if line.startswith("  ") else 0, marker))
            continue
        if "::" in s:  # flashcard
            q, _, a = s.partition("::")
            s = f"**{q.strip()}** — {a.strip()}"
        out.append(("p", runs(s)))
    return out


# ---------- layout ----------

def wrap(spans, size: int, width: int):
    """Lines of [(text, style)] that fit `width` pixels."""
    words = []
    for text, style in spans:
        if text[:1].isspace() and words and not words[-1][0].endswith(" "):
            words[-1] = (words[-1][0] + " ", words[-1][1])  # "**до вещи** (в …": keep the space after bold
        for w in re.findall(r"\S+\s*", text):
            words.append((w, style))
    lines, line, x = [], [], 0
    for w, style in words:
        wlen = font(style, size).getlength(w.rstrip())
        if line and x + wlen > width:
            lines.append(line)
            line, x = [], 0
        line.append((w, style))
        x += font(style, size).getlength(w)
    if line:
        lines.append(line)
    return lines or [[("", "regular")]]


def layout(md: str, header: str):
    """[(op, …)] drawing ops and the total height."""
    ops, y = [], PAD
    lh = lambda size: int(size * SCALE * 1.38)  # line height
    ops.append(("text", PAD, y, [(NO_GLYPH.sub("", header).strip(), "regular")], SIZES["meta"], MUTED))
    y += lh(SIZES["meta"]) + 6 * SCALE
    inner = WIDTH - 2 * PAD
    for b in blocks(md):
        kind = b[0]
        if kind in ("h1", "h2", "h3"):
            size, color = SIZES[kind], {"h1": INK, "h2": H2, "h3": H3}[kind]
            y += (14 if kind != "h1" else 0) * SCALE
            spans = [(t, "bold") for t, _ in b[1]]
            for line in wrap(spans, size, inner):
                ops.append(("text", PAD, y, line, size, color))
                y += lh(size)
            y += 2 * SCALE
        elif kind in ("p", "li"):
            size = SIZES[kind]
            x0 = PAD
            if kind == "li":
                x0 = PAD + (22 + 26 * b[2]) * SCALE
                ops.append(("text", x0 - 20 * SCALE, y, [(b[3], "regular")], size, INK))
            for line in wrap(b[1], size, WIDTH - PAD - x0):
                ops.append(("text", x0, y, line, size, INK))
                y += lh(size)
            y += 3 * SCALE
        elif kind == "table":
            rows, size = b[1], SIZES["td"]
            ncol = max(len(r) for r in rows)
            rows = [r + [""] * (ncol - len(r)) for r in rows]
            longest = [max(len(r[c]) for r in rows) or 1 for c in range(ncol)]
            total = sum(longest)
            colw = [max(90 * SCALE, int(inner * l / total)) for l in longest]
            colw[-1] = inner - sum(colw[:-1])
            cell_pad = 6 * SCALE
            y += 4 * SCALE
            for ri, row in enumerate(rows):
                cells = [wrap([(t, "bold" if ri == 0 else s) for t, s in runs(cell)], size, colw[c] - 2 * cell_pad)
                         for c, cell in enumerate(row)]
                height = max(len(c) for c in cells) * lh(size) + 2 * cell_pad
                x = PAD
                for c, lines in enumerate(cells):
                    ops.append(("rect", x, y, colw[c], height))
                    for k, line in enumerate(lines):
                        ops.append(("text", x + cell_pad, y + cell_pad + k * lh(size), line, size, INK))
                    x += colw[c]
                y += height
            y += 6 * SCALE
    return ops, y + PAD


def render_png(md: str, header: str, out: Path):
    ops, height = layout(md, header)
    img = Image.new("RGB", (WIDTH, height), PAPER)
    d = ImageDraw.Draw(img)
    for op in ops:
        if op[0] == "rect":
            _, x, y, w, h = op
            d.rectangle([x, y, x + w, y + h], outline=RULE, width=SCALE)
            continue
        _, x, y, line, size, color = op
        for text, style in line:
            d.text((x, y), text, font=font(style, size), fill=color)
            x += font(style, size).getlength(text)
    img.save(out, "PNG", optimize=True)
    return out


if __name__ == "__main__":
    import sys
    md = Path(sys.argv[1]).read_text(encoding="utf-8")
    print(render_png(md, sys.argv[3] if len(sys.argv) > 3 else "", Path(sys.argv[2])))
