"""Checks for the parts that broke on real lectures. No mic, no models: run with
    .venv/bin/python3 -m unittest discover tests
"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
_cfg = Path(_tmp) / "config.json"
_cfg.write_text(json.dumps({"vault": _tmp + "/vault", "model": "x", "lang": "ru", "ollama": "",
                            "subjects": ["Программирование", "Матанализ", "Философия", "История"]}), encoding="utf-8")
os.environ["L2O_CONFIG"] = str(_cfg)

import osutil  # noqa: E402
import process  # noqa: E402
import render  # noqa: E402
import telegram  # noqa: E402


def wav(path: Path, pcm: bytes, padded=False):
    """A 16 kHz mono WAV; padded=True mimics AVAudioRecorder's header with a FLLR chunk."""
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    pad = b"FLLR" + struct.pack("<I", 4040) + b"\0" * 4040 if padded else b""
    body = fmt + pad + b"data" + struct.pack("<I", len(pcm)) + pcm
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
    return path


def tone(seconds: float, amp=3000) -> bytes:
    import math
    n = int(16000 * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(i / 8))) for i in range(n))


class WhisperOutput(unittest.TestCase):
    def test_loops_are_cut_to_two(self):
        segs = [(0, "a"), (1, "Леонардо да Винчи."), (2, "Леонардо да Винчи."), (3, "Леонардо да Винчи."),
                (4, "Леонардо да Винчи."), (5, "b")]
        self.assertEqual([t for _, t in process.drop_repeats(segs)],
                         ["a", "Леонардо да Винчи.", "Леонардо да Винчи.", "b"])

    def test_hallucination_phrases(self):
        self.assertTrue(process.HALLUCINATION.search("Субтитры делал DimaTorzok"))
        self.assertFalse(process.HALLUCINATION.search("Производная синуса равна косинусу."))


class Silence(unittest.TestCase):
    def test_dead_mic_stretch_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            w = wav(Path(d) / "a.wav", tone(10) + b"\0" * 32000 * 30 + tone(5), padded=True)
            self.assertEqual(process.dead_spans(w), [(10, 40)])
            self.assertEqual(process.wav_seconds(w), 45)

    def test_short_pause_is_not_a_dropout(self):
        with tempfile.TemporaryDirectory() as d:
            w = wav(Path(d) / "a.wav", tone(5) + b"\0" * 32000 * 5 + tone(5))
            self.assertEqual(process.dead_spans(w), [])

    def test_quality_line(self):
        self.assertIn("47:09–2:21:22", process.quality_note(8503, [(2829, 8482)], 3279))
        self.assertIn("[!info]", process.quality_note(4224, [], 5414))
        self.assertIn("мало речи", process.quality_note(6000, [], 1000))


class Subjects(unittest.TestCase):
    def test_snaps_to_the_students_list(self):
        for answer, expected in [("Программирование на Python", "Программирование"), ("Матан", "Матанализ"),
                                 ("Математический анализ", "Матанализ"), ("Ландшафт", "Разное"),
                                 ("философия", "Философия")]:
            self.assertEqual(process.pick_subject(answer), expected, answer)


class Pictures(unittest.TestCase):
    def test_space_after_bold_survives(self):
        lines = render.wrap(render.runs("**до вещи** (в уме), **в вещи** и после"), 17, 10_000)
        text = "".join(t for t, _ in lines[0])
        self.assertIn("до вещи (в уме)", text)
        self.assertIn("в вещи и после", text)

    def test_tables_are_not_split(self):
        md = "## A\n" + "\n".join(f"- пункт {i} " + "слово " * 20 for i in range(12)) + \
             "\n| Позиция | Суть |\n|---|---|\n| Реализм | общее до вещей |\n| Номинализм | общее — имена |"
        parts = telegram.split_parts(md, limit=600)
        table_parts = [p for p in parts if "| Реализм" in p or "| Позиция" in p]
        self.assertEqual(len(table_parts), 1)
        self.assertIn("| Позиция", table_parts[0])

    def test_renders_a_png(self):
        with tempfile.TemporaryDirectory() as d:
            out = render.render_png("## Кратко\n- **Пантеизм** — $\\frac{a}{b} \\to \\infty$\n| a | b |\n|---|---|\n| 1 | 2 |",
                                    "👀 тест", Path(d) / "p.png")
            self.assertGreater(out.stat().st_size, 1000)

    def test_latex_becomes_text(self):
        self.assertEqual(render.plain_math("$\\frac{a}{b} \\to \\infty$"), "(a)/(b) → ∞")


class Recorder(unittest.TestCase):
    def test_alive_follows_lock_and_growth(self):
        with tempfile.TemporaryDirectory() as d:
            w = wav(Path(d) / "r.wav", tone(1))
            self.assertTrue(osutil.recorder_alive(w))           # just written
            os.utime(w, (time.time() - 30, time.time() - 30))
            self.assertFalse(osutil.recorder_alive(w))          # stale, no lock
            w.with_suffix(".lock").write_text("1")
            self.assertTrue(osutil.recorder_alive(w))           # lock: a minute of grace
            os.utime(w, (time.time() - 90, time.time() - 90))
            self.assertFalse(osutil.recorder_alive(w))          # stale lock from a crash

    def test_header_written_by_recorder_is_readable(self):
        import recorder
        h = recorder.wav_header(32000 * 3)
        self.assertEqual(len(h), 44)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.wav"
            p.write_bytes(h + tone(3))
            self.assertEqual(process.wav_seconds(p), 3)
            import wave
            with wave.open(str(p)) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 48000))


if __name__ == "__main__":
    unittest.main()
