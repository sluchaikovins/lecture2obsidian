#!/usr/bin/env python3
"""«Что я пропустил»: a short recap of the last minutes of the lecture being recorded.

Usage: recap.py <recording.wav> <minutes> [--auto] [--telegram]

The recap is written as an HTML page to <recording>.recap.html, which Hammerspoon shows in a small floating
window. With --telegram it is sent to the Telegram bot as pictures instead.

The transcript comes from <recording>.live.jsonl (written by live.py as it listens). If that is missing or
behind — live answers switched off, say — the last minutes are transcribed here. --auto is the periodic
recap from Hammerspoon: it stays quiet when there was hardly any speech.
"""
import html
import datetime as dt
import json
import re
import sys
import tempfile
import wave
from pathlib import Path

import osutil
import telegram
from process import (HALLUCINATION, LANG, LLM_MODEL, NUM_CTX, ROOT, WHISPER, WHISPER_MODEL, ensure_ollama, log,
                     notify, ollama_chat)

BYTES_PER_SEC = 16000 * 2
MIN_WORDS_AUTO = 120  # an automatic recap of less speech than this isn't worth a message

SYSTEM = """Студент на лекции отвлёкся. По расшифровке последних минут (с ошибками распознавания — исправляй по
смыслу, но ничего не выдумывай) напиши коротко, что он пропустил. Включай только то, что говорил преподаватель.
Реплики студентов между собой («ты домашку сделал?», про другие предметы, шутки) и организационный шум — не включай.
Формат — Markdown, строго эти разделы (пустые не пиши):

## О чём говорили
<3-6 коротких тезисов>

## Что записать
<то, что преподаватель диктовал или велел записать: определения, имена, даты, формулы, списки — максимально близко к
сказанному>

## Важно
<задания, дедлайны, «это будет на зачёте», вопросы к аудитории>

Формулы — обычным текстом (без LaTeX), например: (a+b)/2, x → ∞. Только по-русски."""


def audio_start(wav: Path) -> int:
    with open(wav, "rb") as f:
        head = f.read(16384)
    return head.find(b"data") + 8


def recorded_seconds(wav: Path) -> float:
    return (wav.stat().st_size - audio_start(wav)) / BYTES_PER_SEC


def from_live(wav: Path, since: float):
    path = wav.with_suffix(".live.jsonl")
    if not path.exists():
        return []
    segs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [s for s in segs if s[0] >= since]


def transcribe_tail(wav: Path, since: float, until: float):
    start = audio_start(wav)
    with open(wav, "rb") as f:
        f.seek(start + int(since * BYTES_PER_SEC) // 2 * 2)
        pcm = f.read(int((until - since) * BYTES_PER_SEC) // 2 * 2)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tail.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        out = osutil.run([WHISPER, "-m", str(WHISPER_MODEL), "-l", LANG, "-mc", "0", "-nt", "-np", "-f", str(path)],
                             capture_output=True, text=True, check=True).stdout
    # -nt prints everything as one line: filter Whisper's inventions sentence by sentence, not line by line,
    # or one "Продолжение следует…" in the silence at the end drops the whole transcript.
    sentences = re.findall(r"[^.!?…]+[.!?…]*", out)
    return [x.strip() for x in sentences if x.strip() and not HALLUCINATION.search(x)]


def recording_start(wav: Path) -> dt.datetime:
    m = re.match(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", wav.stem)
    return dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S") if m else dt.datetime.now()


WINDOW_CSS = """<style>
body { font-family: -apple-system, 'Helvetica Neue'; font-size: 15px; color: #1f1d1a; background: #fcfaf5;
       line-height: 1.4; margin: 14px 16px; }
h2 { font-size: 17px; color: #7a3e12; margin: 14px 0 4px 0; }
ul { padding-left: 18px; margin: 4px 0; } li { margin: 3px 0; }
.meta { color: #8a8378; font-size: 13px; margin: 0 0 4px 0; }
.wait { color: #8a8378; font-size: 15px; margin-top: 40px; text-align: center; }
</style>"""


def page(meta: str, body_html: str) -> str:
    return (f"<html><head><meta charset='utf-8'>{WINDOW_CSS}</head><body>"
            f"<p class='meta'>{html.escape(meta)}</p>{body_html}</body></html>")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    wav, minutes = Path(sys.argv[1]).resolve(), float(sys.argv[2])
    auto, to_telegram = "--auto" in sys.argv, "--telegram" in sys.argv
    out = wav.with_suffix(".recap.html")
    if to_telegram and not telegram.enabled():
        notify("Что я пропустил", "Telegram не подключён: 🎓 → «Подключить Telegram…»")
        return
    now = recorded_seconds(wav)
    since = max(0.0, now - minutes * 60)
    begin = recording_start(wav)
    period = f"{begin + dt.timedelta(seconds=since):%H:%M}–{begin + dt.timedelta(seconds=now):%H:%M}"

    segs = from_live(wav, since)
    # Live text lags ~10 s behind; if it covers less than ~2/3 of the period, it's stale — transcribe instead.
    covered = segs and segs[-1][1] - max(since, segs[0][0]) >= (now - since) * 0.66
    if covered:
        lines = [s[2] for s in segs]
    else:
        log(f"recap: transcribing last {minutes:g} min")
        lines = transcribe_tail(wav, since, now)
    text = " ".join(lines)
    words = len(text.split())
    if auto and words < MIN_WORDS_AUTO:
        log(f"recap: only {words} words in the last {minutes:g} min, skipped")
        return
    if not words:
        out.write_text(page(period, "<p class='wait'>За это время ничего не слышно 🤫</p>"), encoding="utf-8")
        out.with_suffix(".md").write_text(f"{period}\n\nЗа это время ничего не слышно 🤫", encoding="utf-8")
        return

    ensure_ollama()
    md = ollama_chat(LLM_MODEL, [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": f"Расшифровка последних {minutes:g} минут:\n<<<\n{text}\n>>>"}],
                     num_ctx=NUM_CTX, keep_alive="20m", num_predict=900)
    md = re.sub(r"<think>.*?</think>", "", md, flags=re.S).strip()

    kind = "🕐 Конспект части" if auto else "👀 Что я пропустил"
    if to_telegram:
        n = telegram.send_markdown(md, f"{kind} · {period}", f"{kind} · {period}")
        log(f"recap: sent {n} picture(s) for {period} ({words} words)")
    else:
        out.write_text(page(f"{kind} · {period}", telegram.md_to_html(md)), encoding="utf-8")
        # The Windows tray app draws plain markdown in its own window.
        out.with_suffix(".md").write_text(f"{kind} · {period}\n\n{md}", encoding="utf-8")
        log(f"recap: {period} ({words} words) → window")


if __name__ == "__main__":
    sys.stdout = open(ROOT / "l2o.log", "a", buffering=1)  # started by Hammerspoon without a redirect
    try:
        main()
    except Exception as e:
        log(f"recap: ERROR {e}")
        if len(sys.argv) > 1:
            Path(sys.argv[1]).with_suffix(".recap.html").write_text(
                page("Ошибка", f"<p class='wait'>Не получилось: {html.escape(str(e))}</p>"), encoding="utf-8")
