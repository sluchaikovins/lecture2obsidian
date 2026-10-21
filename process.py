#!/usr/bin/env python3
"""Audio file → whisper.cpp transcript → Ollama summary → Obsidian note.

Usage: process.py <audio file>   (wav, m4a, mp3, … — whatever afconvert on macOS / ffmpeg on Windows reads)

Photos of the board/slides taken during the lecture are picked up automatically:
screenshots made with fn+F6 while recording, and any image in the photo folders from
config.json (Downloads, Desktop — where AirDrop and Cmd+Shift+4 put them) whose creation
time falls inside the recording.
"""
import array
import base64
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import osutil
from osutil import import_photo, notify, power_note, taken_at, to_wav16k

ROOT = Path(__file__).resolve().parent
# Written by install.sh / install.ps1. L2O_CONFIG points elsewhere (tests use a throwaway config).
CONFIG = json.loads(Path(os.environ.get("L2O_CONFIG", ROOT / "config.json")).read_text(encoding="utf-8"))
VAULT = Path(CONFIG["vault"]).expanduser()
AUDIO_DIR = VAULT / "Audio"
PHOTO_DIR = VAULT / "Фото"
SERVICE_DIRS = {"Audio", "Фото"}  # every other folder in VAULT is a subject
WHISPER = ROOT / "bin" / ("whisper-cli" + osutil.EXE)
WHISPER_MODEL = ROOT / "models" / "ggml-large-v3-turbo.bin"
LANG = CONFIG.get("lang", "ru")
OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = CONFIG.get("model", "qwen3:14b")
# The student's own subjects (config.json → "subjects"). With a list, every lecture lands in one of them
# or in OTHER_SUBJECT, instead of the model inventing a folder like «Ландшафт».
MY_SUBJECTS = [x.strip() for x in CONFIG.get("subjects", []) if x.strip()]
OTHER_SUBJECT = "Разное"
VISION_MODEL = CONFIG.get("vision")  # optional: reads formulas off board photos
PHOTO_SOURCES = [Path(p).expanduser() for p in CONFIG.get("photo_dirs", ["~/Downloads", "~/Desktop"])]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
NUM_CTX = 32768
# Phrases Whisper invents on silence/noise (it was trained on subtitled videos).
HALLUCINATION = re.compile(r"субтитр|редактор субтитров|продолжение следует|спасибо за просмотр|подписывайтесь на канал"
                           r"|dimatorzok|amara\.org", re.I)
# Russian runs ~2 tokens per word, so 11k words + prompt + the answer fit NUM_CTX in one pass.
# One pass matters: the model reads ~230 tok/s but writes ~12 tok/s, and chunking makes it write three texts.
CHUNK_WORDS = 11000  # longer transcripts are summarized part by part, then merged
MARK_EVERY = 30     # seconds between [mm:ss] marks in the transcript

SYSTEM = """Ты — опытный студент-отличник, который пишет конспекты лекций для Obsidian.
Тебе дают автоматическую расшифровку лекции: в ней бывают ошибки распознавания, повторы и слова-паразиты.
Исправляй очевидные ошибки распознавания по смыслу, но НЕ выдумывай факты, которых не было в лекции.
Микрофон стоит у студентов, поэтому в записи бывают их разговоры между собой, шутки, обсуждение не по теме, шум аудитории.
Всё, что не относится к материалу лекции, в конспект НЕ включай. Единственное исключение — организационная информация
от преподавателя (домашнее задание, дедлайны, контрольные, экзамен): её вынеси в отдельный раздел.
Строки вида [ФОТО N, мм:сс] — это то, что было написано на доске или слайде в этот момент лекции (распознано с фото).
Бери оттуда формулы и определения: преподаватель часто пишет их на доске, не проговаривая.
Пиши по-русски, в Markdown. Формулы — в LaTeX ($...$ или $$...$$). Важные определения выделяй **жирным**."""

FINAL_PROMPT = """Составь конспект лекции строго в таком формате:

TITLE: <короткое название темы лекции, до 60 символов, без кавычек>
SUBJECT: <учебный предмет. {subjects}>
TAGS: <3-6 тегов через запятую, строчными, слова через дефис>
RELATED: <названия лекций из списка ниже, которые связаны по теме с этой (до 3, через « ; »), или «нет»>

## Кратко
<3-5 предложений: о чём лекция и главные выводы>

## Конспект
<структурированный конспект: не меньше {sections} разделов ### по темам лекции в порядке изложения, внутри — подробные тезисы,
определения, формулы, примеры и пояснения преподавателя. Не сжимай: по конспекту должно быть можно готовиться без записи.{photo_hint}>

## Ключевые понятия
<список учебных терминов из материала лекции: - **термин** — определение>

## Вопросы для самопроверки
<5 вопросов нумерованным списком>

## Карточки
<8-15 карточек для запоминания, каждая на отдельной строке: сам вопрос, затем ::, затем короткий ответ в одну строку.
Только учебный материал — никаких карточек про ДЗ, даты и следующие темы. Без нумерации и маркеров списка. Например:
Что такое производная функции?::Предел отношения приращения функции к приращению аргумента при $\Delta x \to 0$
Чему равна производная $\sin x$?::$\cos x$>

## Организационное
<ОБЯЗАТЕЛЬНО, если преподаватель упоминал: домашнее задание, дедлайны, контрольные, коллоквиумы, экзамен, тему следующего занятия — списком.
Если ничего такого не было — этот раздел не пиши>

Прошлые лекции (для RELATED):
{previous}

{kind}:
<<<
{text}
>>>"""

PHOTO_HINT = (" Если в расшифровке есть строки [ФОТО N, …], вставь в подходящем месте конспекта маркер ФОТО_N"
              " на отдельной строке — туда встанет сама фотография")

CHUNK_PROMPT = """Это часть {i} из {n} расшифровки лекции.
Выпиши подробные тезисы этой части: все определения, формулы, примеры, аргументы и выводы — в том порядке, в котором они звучат.
Организационную информацию (ДЗ, дедлайны, контрольные) тоже выпиши, отдельным пунктом.
Строки [ФОТО N, …] сохрани как есть, на своих местах. Без вступлений, только тезисы в Markdown.

<<<
{text}
>>>"""

VISION_PROMPT = """Это фото доски или слайда с лекции. Перепиши всё учебное содержимое: текст, определения, формулы (в LaTeX $...$),
схемы опиши словами кратко. Только содержимое, без вступлений.
Если на фото нет учебного материала (люди, аудитория, размытое фото) — ответь одним словом: НЕТ"""


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def mmss(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60:02d}:{secs % 60:02d}"


def wav_seconds(wav_path: Path) -> int:
    """Length of a 16 kHz mono 16-bit WAV (what to_wav16k makes), from the data size. Header-agnostic:
    works for WAVE_FORMAT_EXTENSIBLE and padded headers that the wave module rejects."""
    with open(wav_path, "rb") as f:
        start = f.read(16384).find(b"data") + 8
    return int((wav_path.stat().st_size - start) / 32000)


def loudness(wav_path: Path) -> float:
    """RMS of the 16-bit samples (sampled, so a 2-hour file takes a fraction of a second)."""
    with open(wav_path, "rb") as f:
        head = f.read(16384)
        start = head.find(b"data") + 8
        size = wav_path.stat().st_size - start
        total, count = 0.0, 0
        for k in range(200):  # 200 one-second probes spread over the recording
            f.seek(start + (size * k // 200) // 2 * 2)
            samples = array.array("h", f.read(32000))
            total += sum(x * x for x in samples[::8])
            count += len(samples[::8])
    return (total / count) ** 0.5 if count else 0.0


def recording_start(src: Path, seconds: int) -> dt.datetime:
    # Our recorder names files by start time; for anything else assume the file was saved when it ended.
    m = re.match(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", src.stem)
    if m:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
    return dt.datetime.fromtimestamp(src.stat().st_mtime) - dt.timedelta(seconds=seconds)


def dead_spans(wav_path: Path, min_secs: int = 20):
    """Stretches of digital silence (exact zeros): the mic stopped delivering sound — lid closed, headphones
    connected, Mac asleep. Whisper invents text on them ("Чек, чек, чек…"), so their segments are dropped."""
    spans, run_start, sec = [], None, 0
    with open(wav_path, "rb") as f:
        start = f.read(16384).find(b"data") + 8
        f.seek(start)
        while True:
            chunk = f.read(32000)  # one second
            if not chunk:
                break
            dead = not chunk.strip(b"\0")
            if dead and run_start is None:
                run_start = sec
            elif not dead and run_start is not None:
                if sec - run_start >= min_secs:
                    spans.append((run_start, sec))
                run_start = None
            sec += 1
    if run_start is not None and sec - run_start >= min_secs:
        spans.append((run_start, sec))
    return spans


def quality_note(seconds: int, spans, words: int) -> str:
    """One honest line at the top of the note: how much of the lecture actually made it in."""
    lost = sum(b - a for a, b in spans)
    heard = max(1, seconds - lost)
    rate = words / (heard / 60)
    line = f"Расшифровано {mmss(heard)} из {mmss(seconds)}, {rate:.0f} слов/мин"
    if spans:
        where = ", ".join(f"{mmss(a)}–{mmss(b)}" for a, b in spans)
        return (f"> [!warning] Микрофон не записывал звук: {where}\n"
                f"> Этой части лекции в конспекте нет (закрыта крышка, подключились наушники или мак уснул). {line}.")
    if rate < 35:
        return (f"> [!warning] Подозрительно мало речи: {line}\n"
                f"> Возможно, микрофон был далеко от лектора — сверьте конспект с записью.")
    return f"> [!info] {line}"


def transcribe(wav_path: Path):
    """Returns [(start_seconds, text), …]."""
    out_base = wav_path.with_suffix("")
    osutil.run(
        [WHISPER, "-m", WHISPER_MODEL, "-l", LANG, "-f", wav_path,
         # -mc 0: no conditioning on earlier text — otherwise Whisper falls into loops and repeats one
         # phrase for minutes ("Леонардо да Винчи" ×1000), losing the lecture under it.
         "-mc", "0", "-oj", "-of", out_base, "-np"],
        check=True, stdout=subprocess.DEVNULL,
    )
    segments = json.loads(out_base.with_suffix(".json").read_text(encoding="utf-8"))["transcription"]
    return drop_repeats([(s["offsets"]["from"] / 1000, s["text"].strip()) for s in segments
                         if s["text"].strip() and not HALLUCINATION.search(s["text"])])


def drop_repeats(segments):
    """Backstop for Whisper loops: a line repeated back to back is kept at most twice."""
    out = []
    for seg in segments:
        if len(out) >= 2 and seg[1] == out[-1][1] == out[-2][1]:
            continue
        out.append(seg)
    return out


def find_photos(session_dir: Path, start: dt.datetime, seconds: int):
    """Returns [(offset_seconds, path), …] sorted by time."""
    end = start + dt.timedelta(seconds=seconds)
    found = {}
    if session_dir.is_dir():
        for p in session_dir.iterdir():
            if p.suffix.lower() in IMAGE_EXT:
                found[p] = taken_at(p)
    for d in PHOTO_SOURCES:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix.lower() in IMAGE_EXT and p not in found:
                t = taken_at(p)
                if start - dt.timedelta(minutes=1) <= t <= end + dt.timedelta(minutes=1):
                    found[p] = t
    return sorted((max(0.0, min((t - start).total_seconds(), seconds)), p) for p, t in found.items())


def read_photo(path: Path):
    """Board/slide content as text, or None if the photo has nothing to read."""
    content = ollama_chat(VISION_MODEL, [{"role": "user", "content": VISION_PROMPT,
                                          "images": [base64.b64encode(path.read_bytes()).decode()]}],
                          num_ctx=8192, keep_alive="2m", num_predict=1500, repeat_penalty=1.1)
    return None if re.fullmatch(r"\W*НЕТ\W*", content, re.I) or not content else content


# ---------- LLM ----------

def ollama_bin() -> Path:
    # Ollama.app offers to move itself to /Applications on first launch, so the configured path can go stale.
    candidates = [Path(CONFIG.get("ollama", "")).expanduser()] + osutil.ollama_candidates()
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("ollama")
    if found:
        return Path(found)
    raise RuntimeError("не нашёл Ollama — запустите install.sh ещё раз")


def ensure_ollama():
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=3)
    except Exception:
        log("starting ollama serve")
        # Flash attention + 8-bit KV cache: ~2 GB less memory at a 32k context, same speed.
        env = dict(os.environ, OLLAMA_FLASH_ATTENTION="1", OLLAMA_KV_CACHE_TYPE="q8_0")
        subprocess.Popen([str(ollama_bin()), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env, **osutil.HIDDEN)
        for _ in range(30):
            try:
                urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=3)
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError("Ollama не запустилась")


def ollama_chat(model, messages, num_ctx=NUM_CTX, keep_alive="5m", num_predict=-1, repeat_penalty=1.0) -> str:
    body = json.dumps({
        "model": model, "messages": messages, "stream": True, "think": False, "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx, "temperature": 0.3, "num_predict": num_predict, "repeat_penalty": repeat_penalty},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    # Streamed, so the timeout is per token rather than per answer; retried because a Mac that
    # fell asleep mid-answer comes back with a dead connection.
    for attempt in range(3):
        try:
            parts, started = [], time.time()
            with urllib.request.urlopen(req, timeout=900) as r:
                for line in r:
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])
                    parts.append(msg.get("message", {}).get("content", ""))
                    if msg.get("done"):
                        log(f"  {model}: read {msg.get('prompt_eval_count', 0)} tok, wrote {msg.get('eval_count', 0)} tok"
                            f" in {mmss(time.time() - started)}")
            break
        except (OSError, ValueError) as e:
            if attempt == 2:
                raise
            log(f"  {model}: {e} — retrying")
            ensure_ollama()
    return re.sub(r"<think>.*?</think>", "", "".join(parts), flags=re.S).strip()


def unload(model):
    body = json.dumps({"model": model, "keep_alive": 0}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass


def chat(prompt: str, system: str = SYSTEM) -> str:
    # Capped: a model stuck repeating itself would otherwise write until the context runs out.
    return ollama_chat(LLM_MODEL, [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                       num_predict=6000)


def summarize(transcript: str, subjects, previous, has_photos) -> str:
    if MY_SUBJECTS:
        subj = (f"Выбери СТРОГО одно название из списка, слово в слово: {', '.join(MY_SUBJECTS)}. "
                f"Если лекция явно не относится ни к одному из них — напиши «{OTHER_SUBJECT}»")
    elif subjects:
        subj = (f"Уже есть предметы: {', '.join(subjects)}. Если лекция по теме действительно относится к одному из них — "
                f"напиши его точно так же. Если ни один не подходит по теме — НЕ подгоняй, а дай новое короткое название "
                f"с большой буквы")
    else:
        subj = "Короткое название с большой буквы, например «Матанализ», «История», «Физика»"
    # A single pass tends to compress a long lecture into a handful of bullets; tell it how much to keep.
    sections = max(3, min(12, len(transcript.split()) // 900))
    fmt = dict(sections=sections, subjects=subj, previous="\n".join(previous) or "(пока нет)",
               photo_hint=PHOTO_HINT if has_photos else "")
    words = transcript.split()
    if len(words) <= CHUNK_WORDS:
        return chat(FINAL_PROMPT.format(kind="Расшифровка лекции", text=transcript, **fmt))
    chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
    notes = []
    for i, chunk in enumerate(chunks, 1):
        log(f"summarizing part {i}/{len(chunks)}")
        notes.append(chat(CHUNK_PROMPT.format(i=i, n=len(chunks), text=chunk)))
    return chat(FINAL_PROMPT.format(kind="Подробные тезисы лекции по частям", text="\n\n".join(notes), **fmt))


def pick_subject(answer: str) -> str:
    """Snap the model's SUBJECT to the student's list: exact, then case-insensitive, then fuzzy, else OTHER_SUBJECT."""
    if not MY_SUBJECTS:
        return answer
    options = {x.lower(): x for x in MY_SUBJECTS}
    key = answer.strip().lower()
    if key in options:
        return options[key]
    for low, name in options.items():  # "Программирование на Python" → "Программирование"
        if low in key or key in low and len(key) >= 4:
            return name
    close = difflib.get_close_matches(key, list(options), n=1, cutoff=0.6)
    return options[close[0]] if close else OTHER_SUBJECT


def fix_math(text: str) -> str:
    # Obsidian doesn't render inline math with inner padding: "$ x $" → "$x$"
    return re.sub(r"(?<![$\\])\$(?!\$)([^$\n]+?)\$(?!\$)", lambda m: "$" + m.group(1).strip() + "$", text)


def parse_summary(summary: str):
    meta = {"TITLE": "Лекция", "SUBJECT": "Разное", "TAGS": "", "RELATED": ""}
    body_lines = []
    for line in summary.splitlines():
        m = re.match(r"^\**(TITLE|SUBJECT|TAGS|RELATED)\**:\s*(.*)", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"«»*')
        else:
            body_lines.append(line.rstrip())  # models like trailing "  " line breaks; they break heading matches
    tags = [re.sub(r"[^\w-]", "", x.strip().lower().replace(" ", "-")) for x in meta["TAGS"].split(",")]
    related = [x.strip().strip('"«»[]') for x in re.split(r"\s*;\s*", meta["RELATED"])]
    related = [x for x in related if x and x.lower() not in ("нет", "-")]
    return meta["TITLE"], safe_filename(meta["SUBJECT"]) or "Разное", [t for t in tags if t], related, \
        fix_math("\n".join(body_lines).strip())


def clean_cards(body: str) -> str:
    """Keep the ## Карточки section to plain `Q::A` lines the Spaced Repetition plugin understands."""
    def fix(m):
        lines = []
        for l in m.group(2).splitlines():
            l = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", l).strip()
            q, _, a = l.replace("**", "").partition("::")
            if q.strip() and a.strip() and not re.fullmatch(r"вопрос", q.strip(), re.I):
                lines.append(f"{q.strip()}::{a.strip()}")
        return m.group(1) + "\n".join(lines) + "\n\n"
    return re.sub(r"(## Карточки\n)(.*?)(?=\n## |\Z)", fix, body + "\n", flags=re.S).strip()


def place_photos(body: str, photos) -> str:
    """Swap ФОТО_N markers for the images; photos the model didn't place go to the end of the summary."""
    placed = set()

    def embed(m):
        n = int(m.group(1))
        if 1 <= n <= len(photos) and n not in placed:
            placed.add(n)
            return f"![[Фото/{photos[n - 1]}]]"
        return ""
    body = re.sub(r"^[ \t>*_\-\[(]*ФОТО[_ ]?(\d+)[\])*_ \t]*$", embed, body, flags=re.M)
    body = re.sub(r"\[?ФОТО[_ ](\d+)\]?", embed, body)  # markers the model put inline
    body = re.sub(r"[*_]+(!\[\[Фото/[^\]]+\]\])[*_]+", r"\1", body)
    rest = [f"![[Фото/{name}]]" for i, name in enumerate(photos, 1) if i not in placed]
    if rest:
        section = "## Фото с доски\n" + "\n".join(rest) + "\n\n"
        body = body.replace("## Ключевые понятия", section + "## Ключевые понятия", 1) \
            if "## Ключевые понятия" in body else body + "\n\n" + section
    return body


def safe_filename(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "", s).strip()[:80]


def lecture_notes(subject_dir: Path):
    """Lecture notes of a subject, oldest first (names start with the date)."""
    return sorted(p for p in subject_dir.glob("*.md") if re.match(r"\d{4}-\d{2}-\d{2}", p.name))


def title_of(note: Path) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2} ", "", note.stem)


def subject_index(subject: str) -> Path:
    index = VAULT / subject / f"{subject}.md"
    if not index.exists():
        index.write_text(
            f"---\ntags: [предмет]\n---\n\n# {subject}\n\n"
            f"Все лекции по предмету — в «Обратных ссылках» внизу и на графе.\n\n"
            f"Карточки для повторения: колода `flashcards/{slug(subject)}` в плагине Spaced Repetition.\n",
            encoding="utf-8")
    return index


def slug(s: str) -> str:
    return re.sub(r"[^\w-]", "", s.strip().lower().replace(" ", "-"))


# ---------- main flow ----------

def process(src: Path):
    session_dir = src.with_suffix(".photos")
    for d in (AUDIO_DIR, PHOTO_DIR):
        d.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "lecture.wav"
        log(f"converting {src.name}")
        to_wav16k(src, wav)
        seconds = wav_seconds(wav)
        # Mic access denied or the wrong input: the file is silence, Whisper would invent a "lecture" on
        # some random topic and the model would dutifully summarize it.
        if seconds > 30 and loudness(wav) < 30:
            raise RuntimeError("в записи тишина — проверьте доступ к микрофону: Настройки → Конфиденциальность → "
                               "Микрофон → Hammerspoon, и что выбран нужный микрофон")
        started = recording_start(src, seconds)
        stamp = f"{started:%Y-%m-%d %H-%M}"
        spans = dead_spans(wav)
        if spans:
            log("no sound: " + ", ".join(f"{mmss(a)}–{mmss(b)}" for a, b in spans))

        # The transcript is kept next to the recording, so a retry after a failed summary skips Whisper.
        cache = src.with_suffix(".transcript.json")
        if cache.exists():
            log("using saved transcript")
            segments = [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        else:
            log(f"transcribing ({mmss(seconds)})")
            segments = drop_repeats([tuple(x) for x in transcribe(wav)])
            if segments and src.parent == ROOT / "recordings":
                cache.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        segments = [x for x in segments if not any(a - 1 <= x[0] < b for a, b in spans)]
        if not segments:
            raise RuntimeError("пустая расшифровка — возможно, микрофон ничего не записал")

        audio_name = osutil.compress_audio(wav, AUDIO_DIR / stamp).name  # .m4a on macOS, .mp3 on Windows

    ensure_ollama()

    # Photos: copy into the vault and, if a vision model is set, read what's on the board.
    photos, photo_lines = [], []
    for offset, path in find_photos(session_dir, started, seconds):
        n = len(photos) + 1
        name = f"{stamp} {n}.jpg"
        import_photo(path, PHOTO_DIR / name)
        text = None
        if VISION_MODEL:
            log(f"reading photo {n} ({mmss(offset)}) with {VISION_MODEL}")
            try:
                text = read_photo(PHOTO_DIR / name)
                if text is None and path.parent != session_dir:
                    # Swept up from Downloads/Desktop and has nothing to study on it: not a board photo.
                    log(f"photo {n}: no lecture content, skipped ({path.name})")
                    (PHOTO_DIR / name).unlink()
                    continue
            except Exception as e:
                log(f"photo {n}: vision failed: {e}")
        photos.append(name)
        photo_lines.append((offset, f"[ФОТО {n}, {mmss(offset)}] " + (text or "(фото без распознанного текста)")))
    if VISION_MODEL and photos:
        unload(VISION_MODEL)  # free memory for the summary model

    # Transcript with time marks and photo contents woven in where they were taken.
    lines, next_mark = [], 0.0
    events = sorted([(t, 0, txt) for t, txt in segments] + [(t, 1, txt) for t, txt in photo_lines])
    for t, is_photo, text in events:
        if is_photo:
            lines.append(text)
            continue
        if t >= next_mark:
            text = f"[{mmss(t)}] {text}"
            next_mark = t + MARK_EVERY
        lines.append(text)
    transcript = "\n".join(lines)

    subjects = sorted(d.name for d in VAULT.iterdir() if d.is_dir() and d.name not in SERVICE_DIRS
                      and not d.name.startswith("."))
    previous = [f"- {title_of(n)} ({s})" for s in subjects for n in lecture_notes(VAULT / s)][-60:]

    log(f"summarizing with {LLM_MODEL} ({len(transcript.split())} words, {len(photos)} photos)")
    title, subject, tags, related, body = parse_summary(summarize(transcript, subjects, previous, bool(photos)))
    subject = pick_subject(subject)
    body = place_photos(clean_cards(body), photos)

    subject_dir = VAULT / subject
    subject_dir.mkdir(exist_ok=True)
    subject_index(subject)
    earlier = [n for n in lecture_notes(subject_dir) if n.name < stamp]
    number = len(earlier) + 1

    all_notes = {title_of(n).lower(): n.stem for s in subjects for n in lecture_notes(VAULT / s)}
    links = []
    for r in related:
        stem = all_notes.get(safe_filename(r).lower())
        if stem and stem not in links:
            links.append(stem)
    nav = f"**Предмет:** [[{subject}]] · лекция №{number}"
    if earlier:
        nav += f" · ← [[{earlier[-1].stem}|предыдущая]]"
    qa_file = src.with_suffix(".qa.jsonl")
    qa = [json.loads(l) for l in qa_file.read_text(encoding="utf-8").splitlines() if l.strip()] if qa_file.exists() else []
    qa_md = ("## Вопросы на лекции\n" + "\n".join(f"- `{mmss(x['t'])}` **{x['q']}** — {x['a']}" for x in qa)
             + "\n\n") if qa else ""
    related_md = ("## Связанные лекции\n" + "\n".join(f"- [[{s}]]" for s in links) + "\n\n") if links else ""
    if "## Карточки" in body:
        body = body.replace("## Карточки\n", f"## Карточки\n#flashcards/{slug(subject)}\n\n", 1)

    note = subject_dir / f"{stamp} {safe_filename(title) or 'Лекция'}.md"
    quoted = "\n".join("> " + l for l in transcript.splitlines())
    tag_list = ", ".join(["lecture", slug(subject)] + tags)
    note.write_text(
        f"---\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"subject: \"[[{subject}]]\"\n"
        f"lecture: {number}\n"
        f"date: {started:%Y-%m-%dT%H:%M}\n"
        f"duration: \"{mmss(seconds)}\"\n"
        f"tags: [{tag_list}]\n"
        f"audio: \"[[Audio/{audio_name}]]\"\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{nav}\n\n"
        f"{quality_note(seconds, spans, sum(len(t.split()) for _, t in segments))}\n\n"
        f"{body}\n\n"
        f"{qa_md}"
        f"{related_md}"
        f"## Запись\n![[Audio/{audio_name}]]\n\n"
        f"> [!quote]- Полная расшифровка\n{quoted}\n",
        encoding="utf-8",
    )
    log(f"saved {note}")
    send_to_telegram(note, subject, title, started)
    return note, f"{subject}: {title}"


def send_to_telegram(note: Path, subject: str, title: str, started: dt.datetime):
    """The finished notes as pictures, if a Telegram bot is set up (CONFIG telegram_full_note: false turns it off)."""
    try:
        import telegram
        if not telegram.enabled() or CONFIG.get("telegram_full_note") is False:
            return
        text = note.read_text(encoding="utf-8")
        body = text.split("\n## Кратко", 1)[-1].split("\n## Запись")[0]
        n = telegram.send_markdown("## Кратко" + body, f"{subject} · {started:%d.%m} · {title}",
                                   f"📚 {subject}: {title}")
        log(f"telegram: sent {n} picture(s)")
    except Exception as e:  # the note is saved either way; Telegram is a bonus
        log(f"telegram: {e}")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    # Keep the Mac awake until this process exits: a lecture takes minutes of GPU work.
    osutil.keep_awake()
    src = Path(sys.argv[1]).expanduser().resolve()
    slow = power_note()
    if slow:
        log(f"power: {slow}")
        notify("⚡ Конспект будет медленным", slow)
    try:
        note, title = process(src)
    except Exception as e:
        log(f"ERROR: {e}")
        notify("Лекция: ошибка", f"{e}. Аудио сохранено: {src.name}")
        sys.exit(1)
    notify("Конспект готов ✅", title)
    if src.parent == ROOT / "recordings":
        src.unlink()  # the vault keeps the compressed copy
        shutil.rmtree(src.with_suffix(".photos"), ignore_errors=True)
        src.with_suffix(".transcript.json").unlink(missing_ok=True)
        src.with_suffix(".qa.jsonl").unlink(missing_ok=True)
        src.with_suffix(".live.jsonl").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
