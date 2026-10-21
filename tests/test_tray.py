"""The Windows tray app's logic (tray.App) with a fake UI and stand-in scripts: no mic, no models, no GUI."""
import json
import os
import queue
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_RECORDER = textwrap.dedent('''
    import struct, sys, time, math
    from pathlib import Path
    out = Path(sys.argv[1]); stop = out.with_suffix(".stop"); lock = out.with_suffix(".lock")
    lock.write_text("1")
    silent = "silent" in out.read_text() if out.exists() else False
    with open(out, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", 0))
        while not stop.exists():
            sec = b"\\0" * 32000 if Path(str(out) + ".mute").exists() else \\
                  b"".join(struct.pack("<h", int(2000 * math.sin(i / 8))) for i in range(16000))
            f.write(sec); f.flush(); time.sleep(0.2)
    lock.unlink(); stop.unlink()
''')
FAKE_SCRIPT = textwrap.dedent('''
    import sys, json
    from pathlib import Path
    root = Path(__file__).parent
    with open(root / "calls.jsonl", "a") as f:
        f.write(json.dumps([Path(__file__).name] + sys.argv[1:]) + "\\n")
    if Path(__file__).name == "recap.py":
        Path(sys.argv[1]).with_suffix(".recap.md").write_text("👀 · 10:00–10:10\\n\\n## О чём говорили\\n- **тест**")
''')


class FakeUI:
    def __init__(self):
        self.calls, self.alerts, self.recaps, self.titles = queue.Queue(), [], [], []
        self.answer = True

    def later(self, fn):
        self.calls.put(fn)

    def pump(self, seconds=0.0):
        end = time.time() + seconds
        while True:
            while not self.calls.empty():
                self.calls.get()()
            if time.time() >= end:
                return
            time.sleep(0.05)

    def refresh(self, title, state):
        self.titles.append((title, state))

    def alert(self, text, seconds=3):
        self.alerts.append(text)

    def notify(self, title, text):
        self.alerts.append(title)

    def confirm(self, title, text):
        return self.answer

    def ask(self, title, text, default=""):
        return "Программирование, История"

    def show_recap(self, md):
        self.recaps.append(md)


class TrayApp(unittest.TestCase):
    def setUp(self):
        import tray
        self.tray = tray
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "recorder.py").write_text(FAKE_RECORDER)
        for name in ("live.py", "process.py", "recap.py", "exam.py"):
            (self.dir / name).write_text(FAKE_SCRIPT)
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({"vault": str(self.dir / "vault"), "live_answers": True}))
        os.environ["L2O_CONFIG"] = str(cfg)
        tray.ROOT, tray.REC_DIR, tray.LOG = self.dir, self.dir / "recordings", self.dir / "l2o.log"
        self.ui = FakeUI()
        self.app = tray.App(self.ui)

    def calls(self):
        f = self.dir / "calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.ui.pump(0.1)
            if cond():
                return True
        return False

    def test_record_then_stop_makes_notes(self):
        self.app.toggle()
        wav = self.app.rec_file
        self.assertTrue(self.wait_for(lambda: wav.exists() and wav.stat().st_size > 64000))
        time.sleep(3.1)  # longer than the "too short" threshold
        self.app.toggle()
        self.assertTrue(self.wait_for(lambda: any(c[0] == "process.py" for c in self.calls())))
        self.assertEqual([c for c in self.calls() if c[0] == "process.py"][0][1], str(wav))
        self.assertTrue(any(c[0] == "live.py" for c in self.calls()))
        self.assertFalse(wav.with_suffix(".lock").exists())

    def test_cancel_deletes_everything(self):
        self.app.toggle()
        wav = self.app.rec_file
        self.assertTrue(self.wait_for(lambda: wav.exists()))
        wav.with_suffix(".qa.jsonl").write_text("{}")
        self.app.cancel()
        self.assertTrue(self.wait_for(lambda: not wav.exists()))
        self.assertFalse(wav.with_suffix(".qa.jsonl").exists())
        self.assertFalse(any(c[0] == "process.py" for c in self.calls()))
        self.assertIn("🗑 Запись отменена и удалена", self.ui.alerts)

    def test_cancel_can_be_declined(self):
        self.app.toggle()
        self.ui.answer = False
        self.app.cancel()
        self.assertIsNotNone(self.app.rec)
        self.app.stop(cancel=True)
        self.wait_for(lambda: self.app.rec is None and not self.app.rec_file.exists())

    def test_recap_shows_in_window(self):
        self.app.toggle()
        self.assertTrue(self.wait_for(lambda: self.app.rec_file.exists()))
        self.app.recap()
        self.assertIn("Собираю", self.ui.recaps[0])
        self.assertTrue(self.wait_for(lambda: len(self.ui.recaps) == 2))
        self.assertIn("О чём говорили", self.ui.recaps[1])
        self.app.stop(cancel=True)
        self.wait_for(lambda: not self.app.rec_file.exists())

    def test_watchdog_warns_when_mic_goes_silent(self):
        self.app.toggle()
        wav = self.app.rec_file
        self.assertTrue(self.wait_for(lambda: wav.exists() and wav.stat().st_size > 64000))
        Path(str(wav) + ".mute").touch()
        time.sleep(1)
        self.app.watchdog()
        self.assertIsNotNone(self.app.dead_since)
        self.app.dead_since -= 25  # as if 25 s passed
        self.app.watchdog()
        self.assertEqual(self.app.state(), "dead")
        self.assertIn("🎙❌ Микрофон пропал", self.ui.alerts)
        Path(str(wav) + ".mute").unlink()
        time.sleep(1)
        self.app.watchdog()
        self.assertEqual(self.app.state(), "rec")
        self.app.stop(cancel=True)
        self.wait_for(lambda: not wav.exists())

    def test_leftover_recording_is_processed_on_startup(self):
        self.tray.REC_DIR.mkdir(exist_ok=True)
        old = self.tray.REC_DIR / "2026-10-01_10-00-00.wav"
        old.write_bytes(b"RIFF")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.app.startup()
        self.assertTrue(self.wait_for(lambda: any(c[0] == "process.py" for c in self.calls())))

    def test_subjects_are_saved(self):
        self.app.edit_subjects()
        self.assertEqual(self.tray.config()["subjects"], ["Программирование", "История"])


if __name__ == "__main__":
    unittest.main()
