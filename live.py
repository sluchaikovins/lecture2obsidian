#!/usr/bin/env python3
"""Live helper while a lecture is being recorded: when the lecturer asks the audience a question,
an answer pops up as a notification. Started and stopped by Hammerspoon together with the recording.

Usage: live.py <recording.wav that is still being written>

The last WINDOW_SECS of the growing WAV are transcribed back to back, about once a second (overlapping
windows, so phrases aren't cut and recent text gets refined). As soon as a sentence ends with "?", the LLM
decides whether it's a question to the audience and writes a one-line answer. Speed matters more than
precision here: a student would answer within a few seconds, so some rhetorical questions get answered too.
Questions and answers are appended to <recording>.qa.jsonl, which process.py puts into the lecture note.
"""
import array
import difflib
import json
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from process import (HALLUCINATION, LANG, LLM_MODEL, NUM_CTX, ROOT, WHISPER, WHISPER_MODEL, ensure_ollama, log, mmss,
                     notify, ollama_chat)

STEP_SECS = 1           # minimum time between looks at the recording (each look takes ~1 s of Whisper)
WINDOW_SECS = 10        # how much recent audio each look transcribes
CONTEXT_CHARS = 400     # transcript before the question: enough for the topic, short enough to read fast
BYTES_PER_SEC = 16000 * 2
SILENCE_RMS = 150       # 16-bit samples; below this the window is room noise and Whisper would invent text
# "Ну, кто скажет?" / "Так?" only push the previous question; on their own there's nothing to answer.
NUDGE = re.compile(r"^(ну|но|а|и|так|итак)?[\s,]*(кто|кто-нибудь)?\s*(скажет|знает|ответит|помнит|смелый|первый|понятно|"
                   r"ясно|верно|так|да|правильно|согласны)?[\s,]*(ну|а|же|нет)?\??$|^(\w+,?\s)?кто (думает|считает|согласен|за)\b", re.I)
SEGMENT = re.compile(r"^\[(\d+):(\d+):([\d.]+) --> (\d+):(\d+):([\d.]+)\]\s*(.*)$")
FOREIGN = re.compile(r"[　-鿿가-힯]")  # CJK leaking into Qwen's Russian

SYSTEM = """Ты подсказываешь студенту на лекции в реальном времени. Тебе дают тему (кусок расшифровки)
и ВОПРОС, который только что прозвучал. Если это вопрос по материалу, на который можно ответить, — сразу напиши
ответ, который студент скажет вслух: несколько слов или одно короткое предложение (до 70 символов), только по-русски,
без вступлений.
Напиши НЕТ, если это организационный вопрос («всех слышно?», «вопросы есть?», «где староста?»), болтовня студентов
(«ты домашку сделал?») или вопрос без учебного смысла. Подгоняющие вопросы («ну, кто скажет?», «а?», «так?») относятся
к предыдущему вопросу — отвечай на него. Если сомневаешься, риторический ли вопрос, — всё равно отвечай по сути."""


def audio_start(wav: Path) -> int:
    """Byte offset of the samples: AVAudioRecorder pads the header with JUNK/FLLR chunks."""
    with open(wav, "rb") as f:
        head = f.read(16384)
    i = head.find(b"data")
    if i < 0:
        raise ValueError("no data chunk yet")
    return i + 8


def read_tail(wav: Path, start: int, secs: float):
    """(pcm of the last `secs` seconds, absolute start time of that pcm, audio length so far)."""
    total = wav.stat().st_size - start
    total -= total % 2
    begin = max(0, total - int(secs * BYTES_PER_SEC))
    begin -= begin % 2
    with open(wav, "rb") as f:
        f.seek(start + begin)
        pcm = f.read(total - begin)
    return pcm, begin / BYTES_PER_SEC, total / BYTES_PER_SEC


def rms(pcm: bytes) -> float:
    samples = array.array("h", pcm)
    if not samples:
        return 0.0
    picked = samples[::max(1, len(samples) // 20000)]
    return (sum(s * s for s in picked) / len(picked)) ** 0.5


def transcribe_window(pcm: bytes, offset: float):
    """[(abs_start, abs_end, text), …] for the window."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "window.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        # Greedy decoding and a 15 s encoder window (-ac 768) instead of 30 s: ~30% faster on these windows.
        out = subprocess.run([str(WHISPER), "-m", str(WHISPER_MODEL), "-l", LANG, "-np", "-bs", "1", "-ac", "768",
                              "-f", str(path)],
                             capture_output=True, text=True, check=True).stdout
    segs = []
    for line in out.splitlines():
        m = SEGMENT.match(line.strip())
        if m and m.group(7).strip() and not HALLUCINATION.search(m.group(7)):
            h1, m1, s1, h2, m2, s2 = (float(x) for x in m.groups()[:6])
            segs.append((offset + h1 * 3600 + m1 * 60 + s1, offset + h2 * 3600 + m2 * 60 + s2, m.group(7).strip()))
    return segs


def answer(context: str, question: str):
    """One-line answer, or None if it isn't a question worth answering."""
    reply = ollama_chat(LLM_MODEL, [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Тема:\n{context or '(начало лекции)'}\n\nВОПРОС: {question}"},
    ], num_ctx=NUM_CTX, keep_alive="20m", num_predict=60)  # same num_ctx as process.py: no model reload after
    a = FOREIGN.sub("", reply.strip().splitlines()[0] if reply.strip() else "").strip()
    a = re.sub(r"^(ОТВЕТ|Ответ)\s*:\s*", "", a)
    # Only a bare "НЕТ" is a refusal: "Нет, производная в нуле не существует" is an answer.
    # A bare "нет" (any case) is the model declining; a real "no" answer comes with its reason.
    return None if not a or re.fullmatch(r"\W*нет\W*", a, re.I) or "риторическ" in a.lower() else a


def recorder_alive(wav: Path) -> bool:
    return subprocess.run(["pgrep", "-f", f"bin/recorder {wav}"], capture_output=True).returncode == 0


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    wav = Path(sys.argv[1]).resolve()
    qa_file = wav.with_suffix(".qa.jsonl")
    # Text that no longer changes goes here, so recap.py ("что я пропустил") needn't run Whisper itself.
    text_file = wav.with_suffix(".live.jsonl")
    written_until = 0.0
    sys.stdout = open(ROOT / "l2o.log", "a", buffering=1)  # Hammerspoon starts us without a log redirect
    log(f"live: listening to {wav.name}")
    ensure_ollama()

    start, looked = None, 0.0
    segments = []   # rolling transcript: (abs_start, abs_end, text)
    judged = []     # (abs_end, text) of question sentences already decided
    asked = []      # question texts already notified
    # The recorder creates the file a moment after it starts (and after the mic permission prompt).
    for _ in range(60):
        if wav.exists():
            break
        time.sleep(0.5)
    while wav.exists() and recorder_alive(wav):
        time.sleep(max(0.0, STEP_SECS - (time.time() - looked)))
        looked = time.time()
        try:
            start = start or audio_start(wav)
            pcm, offset, now = read_tail(wav, start, WINDOW_SECS)
            if rms(pcm) < SILENCE_RMS:
                continue
            fresh = transcribe_window(pcm, offset)
        except Exception as e:  # one bad window must not stop the helper for the rest of the lecture
            log(f"live: {e}")
            continue
        # The window re-covers recent audio with more context: its text replaces what we had from there on.
        # The first second is skipped, because a segment cut by the window edge is garbled.
        cut = offset + 1.0 if offset > 0 else 0.0
        segments = [s for s in segments if s[0] < cut] + [s for s in fresh if s[0] >= cut]
        segments = segments[-200:]
        settled = [x for x in segments if written_until <= x[0] < cut]
        if settled:
            with open(text_file, "a", encoding="utf-8") as f:
                for x in settled:
                    f.write(json.dumps(x, ensure_ascii=False) + "\n")
            written_until = settled[-1][0] + 0.001

        for i, (s_start, s_end, text) in enumerate(segments):
            if "?" not in text:
                continue
            if now - s_end < 0.5:
                continue  # still being said: Whisper may have cut it mid-sentence
            # The question is the longest "?" sentence ("Кто знает, чему равна …? Ну, кто скажет?" → the first).
            q = max((x.strip(" -—,") for x in re.findall(r"[^.!?…]*\?", text)), key=len)
            if q[:1].islower() and i > 0 and text.lstrip(" -—,").startswith(q):
                # Started in the previous segment: "… как вы думаете, существует" + "ли производная …?"
                q = (re.split(r"[.!?…]", segments[i - 1][2])[-1].strip() + " " + q).strip()
            if len(q) < 8 or NUDGE.match(q) or any(difflib.SequenceMatcher(None, t, q).ratio() > 0.75 for e, t in judged):
                continue
            judged.append((s_end, q))
            context = " ".join(t for a, b, t in segments if b <= s_start)[-CONTEXT_CHARS:]
            try:
                a = answer(context, q)
            except Exception as e:
                log(f"live: {e}")
                continue
            if not a or any(difflib.SequenceMatcher(None, q.lower(), old.lower()).ratio() > 0.6 for old in asked):
                log(f"live: skipped «{q}»")
                continue
            asked.append(q)
            lag = (wav.stat().st_size - start) / BYTES_PER_SEC - s_end
            log(f"live: [{mmss(s_end)}] {q} → {a} (через {lag:.0f} с)")
            notify(f"❓ {q}", a)
            with open(qa_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": s_start, "q": q, "a": a}, ensure_ascii=False) + "\n")
    log(f"live: stopped ({wav.name})")


if __name__ == "__main__":
    main()
