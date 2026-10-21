#!/usr/bin/env python3
"""Lecture → Obsidian for Windows: a tray icon with hotkeys (what the Hammerspoon module does on macOS).

F5 — start/stop recording        Shift+F5 — cancel the recording (asks first)
F6 — screenshot into the notes   F7 — «что я пропустил»: recap of the last 10 minutes in a small window

The logic (App) knows nothing about the GUI toolkit; TkUI draws windows with tkinter, the tray icon is
pystray, hotkeys are pynput. Everything heavy runs in the same Python scripts as on macOS.
"""
import datetime as dt
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import osutil

ROOT = Path(__file__).resolve().parent
REC_DIR = ROOT / "recordings"
LOG = ROOT / "l2o.log"
PY = sys.executable
RECAP_MINUTES = 10
SIDE_FILES = (".qa.jsonl", ".live.jsonl", ".recap.html", ".recap.md", ".transcript.json", ".lock", ".stop")


def config_path() -> Path:
    return Path(os.environ.get("L2O_CONFIG", ROOT / "config.json"))


def config():
    return json.loads(config_path().read_text(encoding="utf-8"))


def save_config(cfg):
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


class App:
    """Recording, helpers and processing. `ui` draws; every ui call happens on the GUI thread."""

    def __init__(self, ui):
        self.ui = ui
        REC_DIR.mkdir(exist_ok=True)
        self.rec = self.rec_file = self.rec_start = self.listener = None
        self.jobs, self.shots, self.active = 0, 0, set()
        self.dead_since = self.warned_at = None

    # ---------- background processes ----------

    def spawn(self, script, *args, on_done=None, capture=False):
        """Run one of our scripts; `on_done(returncode, output)` comes back on the GUI thread."""
        cmd = [PY, str(ROOT / script), *map(str, args)]
        if capture:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **osutil.HIDDEN)
        else:
            log = open(LOG, "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=log, stderr=log, **osutil.HIDDEN)

        def wait():
            if capture:
                out = proc.communicate()[0]
            else:
                proc.wait()  # the job is done only when the script exits
                out = ""
            if on_done:
                self.ui.later(lambda: on_done(proc.returncode, out or ""))
        threading.Thread(target=wait, daemon=True).start()
        return proc

    def run_job(self, script, *args):
        self.jobs += 1
        self.refresh()

        def done(code, out):
            self.jobs -= 1
            self.refresh()
        self.spawn(script, *args, on_done=done)

    def process(self, wav: Path):
        if wav in self.active:
            return
        self.active.add(wav)
        self.jobs += 1
        self.refresh()

        def done(code, out):
            self.active.discard(wav)
            self.jobs -= 1
            self.refresh()
        self.spawn("process.py", wav, on_done=done)

    # ---------- state shown in the tray ----------

    def title(self) -> str:
        if self.rec:
            mark = "Микрофон пропал! " if self.mic_dead() else "Запись "
            t = mark + fmt(time.time() - self.rec_start) + (f" · фото {self.shots}" if self.shots else "")
        else:
            t = "Лекция → Obsidian"
        return t + (f" · делаю конспект ({self.jobs})" if self.jobs else "")

    def state(self) -> str:
        if self.rec:
            return "dead" if self.mic_dead() else "rec"
        return "busy" if self.jobs else "idle"

    def refresh(self):
        self.ui.refresh(self.title(), self.state())

    # ---------- recording ----------

    def toggle(self):
        self.stop() if self.rec else self.start()

    def start(self):
        self.rec_file = REC_DIR / (dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".wav")
        self.rec = subprocess.Popen([PY, str(ROOT / "recorder.py"), str(self.rec_file)],
                                    stderr=subprocess.PIPE, text=True, **osutil.HIDDEN)
        self.rec_start, self.shots = time.time(), 0
        self.dead_since = self.warned_at = None
        threading.Thread(target=self._watch_recorder, args=(self.rec,), daemon=True).start()
        if config().get("live_answers", True):
            self.listener = subprocess.Popen([PY, str(ROOT / "live.py"), str(self.rec_file)], **osutil.HIDDEN)
        self.ui.alert("🔴 Запись лекции началась")
        self.refresh()

    def _watch_recorder(self, proc):
        err = proc.communicate()[1]
        if proc.returncode not in (0, None) and self.rec is proc:  # died on its own: no mic, access denied…
            def failed():
                self.rec = None
                self.ui.alert(f"Запись не удалась: {(err or '').strip()[-200:]}", 8)
                self.refresh()
            self.ui.later(failed)

    def stop(self, cancel=False):
        if not self.rec:
            return
        proc, wav, elapsed = self.rec, self.rec_file, time.time() - self.rec_start
        self.rec = None
        if self.listener:
            self.listener.terminate()
            self.listener = None
        wav.with_suffix(".stop").touch()  # recorder.py finishes the WAV and exits

        def finished():
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.ui.later(lambda: self._stopped(wav, elapsed, cancel))
        threading.Thread(target=finished, daemon=True).start()
        self.refresh()

    def _stopped(self, wav, elapsed, cancel):
        if cancel or elapsed < 3:
            self.discard(wav)
            self.ui.alert("🗑 Запись отменена и удалена" if cancel else "Запись отменена (слишком короткая)")
        else:
            self.ui.alert(f"⏹ Запись остановлена ({fmt(elapsed)}). Делаю конспект…")
            self.process(wav)
        self.refresh()

    def cancel(self):
        if not self.rec:
            self.ui.alert("Сейчас ничего не записывается")
        elif self.ui.confirm("Отменить запись?",
                             f"Запись ({fmt(time.time() - self.rec_start)}) будет удалена, конспекта не будет."):
            self.stop(cancel=True)

    @staticmethod
    def discard(wav: Path):
        wav.unlink(missing_ok=True)
        for ext in SIDE_FILES:
            wav.with_suffix(ext).unlink(missing_ok=True)
        photos = wav.with_suffix(".photos")
        if photos.is_dir():
            for p in photos.iterdir():
                p.unlink()
            photos.rmdir()

    # ---------- helpers during the lecture ----------

    def screenshot(self):
        if not self.rec:
            self.ui.alert("Скриншот работает во время записи (F5)")
            return
        from PIL import ImageGrab
        folder = self.rec_file.with_suffix(".photos")
        folder.mkdir(exist_ok=True)
        ImageGrab.grab(all_screens=True).save(folder / (dt.datetime.now().strftime("%H-%M-%S") + ".png"))
        self.shots += 1
        self.ui.alert("📷 Экран добавлен в конспект")
        self.refresh()

    def recap(self):
        if not self.rec:
            self.ui.alert("«Что я пропустил» работает во время записи (F5)")
            return
        md_file = self.rec_file.with_suffix(".recap.md")
        md_file.unlink(missing_ok=True)
        self.ui.show_recap(f"⏳ Собираю, что было за последние {RECAP_MINUTES} минут…")

        def done(code, out):
            if md_file.exists():
                self.ui.show_recap(md_file.read_text(encoding="utf-8"))
            else:
                page = self.rec_file.with_suffix(".recap.html")
                text = re.sub(r"<[^>]+>", " ", page.read_text(encoding="utf-8")) if page.exists() else "Не получилось — см. лог"
                self.ui.show_recap(re.sub(r"\s+", " ", text).strip())
        self.spawn("recap.py", self.rec_file, RECAP_MINUTES, on_done=done)

    def mic_dead(self) -> bool:
        return bool(self.dead_since and time.time() - self.dead_since >= 20)

    def watchdog(self):
        """Called every few seconds: warn when the recording has been exact zeros for 20 s."""
        if not self.rec or not self.rec_file.exists():
            return
        with open(self.rec_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size < 44 + 32000:
                return
            f.seek(size - 32000)
            dead = not f.read(32000).strip(b"\0")
        if dead:
            self.dead_since = self.dead_since or time.time()
            if self.mic_dead() and (not self.warned_at or time.time() - self.warned_at >= 120):
                self.warned_at = time.time()
                self.ui.alert("🎙❌ Микрофон пропал — запись идёт, но звука нет!\n"
                              "Если подключили наушники — остановите и начните запись заново (F5)", 8)
                self.ui.notify("🎙❌ Микрофон пропал", "Запись идёт, но звука нет")
        else:
            if self.warned_at:
                self.ui.alert("🎙✅ Звук снова пишется")
            self.dead_since = self.warned_at = None
        self.refresh()

    # ---------- menu actions ----------

    def vault(self) -> Path:
        return Path(config()["vault"]).expanduser()

    def subjects(self):
        v = self.vault()
        if not v.is_dir():
            return []
        return sorted(d.name for d in v.iterdir() if d.is_dir() and d.name not in ("Audio", "Фото")
                      and not d.name.startswith("."))

    def exam(self, subject):
        self.ui.alert(f"📝 Готовлю материалы к экзамену: {subject} (несколько минут)")
        self.run_job("exam.py", subject)

    def unfinished(self):
        return [p for p in sorted(REC_DIR.glob("*.wav"))
                if p != (self.rec_file if self.rec else None) and p not in self.active and not osutil.recorder_alive(p)]

    def finish_unfinished(self):
        for p in self.unfinished():
            self.process(p)

    def toggle_live(self):
        cfg = config()
        cfg["live_answers"] = not cfg.get("live_answers", True)
        save_config(cfg)
        self.ui.alert("❓ Подсказки включены (со следующей записи)" if cfg["live_answers"] else "Подсказки выключены")

    def edit_subjects(self):
        cfg = config()
        text = self.ui.ask("Мои предметы", "Предметы через запятую — каждая лекция попадёт в один из них:",
                           ", ".join(cfg.get("subjects", [])))
        if text is not None:
            cfg["subjects"] = [x.strip() for x in re.split(r"[,;\n]", text) if x.strip()]
            save_config(cfg)
            self.ui.alert(f"📚 Предметов: {len(cfg['subjects'])}")

    def connect_telegram(self):
        cfg = config()
        token = self.ui.ask("Telegram-бот", "1) @BotFather → /newbot → токен.  2) Откройте бота, нажмите Start.\n"
                                            "3) Вставьте токен:", (cfg.get("telegram") or {}).get("token", ""))
        if not token:
            return
        cfg["telegram"] = {"token": token.strip(), "chat_id": ""}
        save_config(cfg)
        self.spawn("telegram.py", "setup", capture=True,
                   on_done=lambda code, out: self.ui.alert("✅ Telegram подключён" if code == 0 else f"Не вышло: {out.strip()[-200:]}", 6))

    def open_folder(self):
        os.startfile(self.vault()) if osutil.IS_WIN else subprocess.run(["open", self.vault()])

    def open_log(self):
        os.startfile(LOG) if osutil.IS_WIN else subprocess.run(["open", LOG])

    def startup(self):
        """Recordings left over from a crash get another try."""
        for p in self.unfinished():
            self.process(p)
        self.refresh()


# ---------------- GUI (Windows) ----------------

class TkUI:
    COLORS = {"idle": (120, 120, 120), "rec": (220, 40, 40), "dead": (240, 160, 0), "busy": (40, 120, 220)}

    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.calls = queue.Queue()
        self.recap_win = None
        self.icon = None
        self.root.after(100, self._pump)

    def later(self, fn):
        """Run fn on the GUI thread (callable from any thread)."""
        self.calls.put(fn)

    def _pump(self):
        while not self.calls.empty():
            try:
                self.calls.get_nowait()()
            except Exception as e:
                print(f"[tray] {e}", file=sys.stderr)
        self.root.after(100, self._pump)

    # tray icon
    def image(self, state):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=self.COLORS[state] + (255,))
        d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        return img

    def refresh(self, title, state):
        if self.icon:
            self.icon.title = title[:120]
            self.icon.icon = self.image(state)

    def notify(self, title, text):
        if self.icon:
            self.icon.notify(text, title)

    # small windows
    def alert(self, text, seconds=3):
        tk = self.tk
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-alpha", 0.92)
        tk.Label(w, text=text, bg="#222", fg="white", font=("Segoe UI", 14), padx=24, pady=14, justify="center").pack()
        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_width()) // 2
        y = w.winfo_screenheight() // 3
        w.geometry(f"+{x}+{y}")
        self._no_activate(w)
        w.after(int(seconds * 1000), w.destroy)

    def confirm(self, title, text) -> bool:
        from tkinter import messagebox
        top = self._front()
        try:
            return messagebox.askyesno(title, text, icon="warning", parent=top)
        finally:
            top.destroy()

    def ask(self, title, text, default=""):
        from tkinter import simpledialog
        top = self._front()
        try:
            return simpledialog.askstring(title, text, initialvalue=default, parent=top)
        finally:
            top.destroy()

    def _front(self):
        top = self.tk.Toplevel(self.root)
        top.withdraw()
        top.attributes("-topmost", True)
        return top

    def _no_activate(self, w):
        """Windows: show without taking the keyboard from the app the student types in."""
        if not osutil.IS_WIN:
            return
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def show_recap(self, md):
        tk = self.tk
        if not self.recap_win or not self.recap_win.winfo_exists():
            w = tk.Toplevel(self.root)
            w.title("👀 Что я пропустил")
            w.attributes("-topmost", True)
            width, height = 440, 540
            w.geometry(f"{width}x{height}+{w.winfo_screenwidth() - width - 16}+16")
            text = tk.Text(w, wrap="word", bg="#fcfaf5", fg="#1f1d1a", relief="flat", padx=14, pady=10,
                           font=("Segoe UI", 11), spacing1=2, spacing3=2)
            text.pack(fill="both", expand=True)
            text.tag_configure("meta", foreground="#8a8378", font=("Segoe UI", 9))
            text.tag_configure("h", foreground="#7a3e12", font=("Segoe UI", 13, "bold"), spacing1=10)
            text.tag_configure("b", font=("Segoe UI", 11, "bold"))
            w.text = text
            self.recap_win = w
            self._no_activate(w)
        text = self.recap_win.text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for i, line in enumerate(md.splitlines()):
            s = line.strip()
            if i == 0 and not s.startswith("#"):
                text.insert("end", s + "\n", "meta")
            elif s.startswith("#"):
                text.insert("end", s.lstrip("# ") + "\n", "h")
            else:
                s = re.sub(r"^[-*+]\s+", "•  ", s)
                for k, part in enumerate(re.split(r"\*\*(.+?)\*\*", s)):
                    text.insert("end", part, "b" if k % 2 else ())
                text.insert("end", "\n")
        text.configure(state="disabled")
        self.recap_win.deiconify()

    def run(self, app):
        import pystray
        from pynput import keyboard
        i = pystray.MenuItem
        M = pystray.Menu

        def act(fn):
            return lambda icon, item: self.later(fn)

        def exam_menu():
            subjects = app.subjects()
            if not subjects:
                return [i("Пока нет лекций", None, enabled=False)]
            return [i(s, act(lambda s=s: app.exam(s))) for s in subjects]

        menu = M(
            i(lambda item: "Остановить запись (F5)" if app.rec else "Начать запись (F5)", act(app.toggle), default=True),
            i("Скриншот экрана (F6)", act(app.screenshot), enabled=lambda item: bool(app.rec)),
            i("👀 Что я пропустил (F7)", act(app.recap), enabled=lambda item: bool(app.rec)),
            i("🗑 Отменить запись (Shift+F5)", act(app.cancel), enabled=lambda item: bool(app.rec)),
            i("❓ Подсказки на вопросы преподавателя", act(app.toggle_live),
              checked=lambda item: config().get("live_answers", True)),
            M.SEPARATOR,
            i("📨 Подключить Telegram…", act(app.connect_telegram)),
            i("📚 Мои предметы…", act(app.edit_subjects)),
            i("📝 Подготовка к экзамену", M(exam_menu)),
            M.SEPARATOR,
            i(lambda item: f"🔁 Доделать незаконченные записи ({len(app.unfinished())})", act(app.finish_unfinished),
              enabled=lambda item: bool(app.unfinished())),
            i("Открыть папку конспектов", act(app.open_folder)),
            i("Показать лог", act(app.open_log)),
            M.SEPARATOR,
            i("Выход", act(self.quit)),
        )
        self.app = app
        self.icon = pystray.Icon("lecture2obsidian", self.image("idle"), "Лекция → Obsidian", menu)
        self.icon.run_detached()

        hotkeys = keyboard.GlobalHotKeys({
            "<f5>": lambda: self.later(app.toggle),
            "<shift>+<f5>": lambda: self.later(app.cancel),
            "<f6>": lambda: self.later(app.screenshot),
            "<f7>": lambda: self.later(app.recap),
        })
        hotkeys.daemon = True
        hotkeys.start()

        def tick():
            app.watchdog() if app.rec and int(time.time()) % 5 == 0 else app.refresh()
            self.root.after(1000, tick)
        self.root.after(1000, tick)
        app.startup()
        self.root.mainloop()

    def quit(self):
        if self.app.rec and not self.confirm("Выйти?", "Идёт запись — она будет остановлена и обработана."):
            return
        self.app.stop()
        self.root.after(2000, self._exit)

    def _exit(self):
        if self.icon:
            self.icon.stop()
        self.root.destroy()


def single_instance():
    """A second copy (autostart + manual start) would fight over hotkeys."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 47835))
    except OSError:
        sys.exit(0)
    return s


if __name__ == "__main__":
    _lock = single_instance()
    if osutil.IS_WIN:
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)  # pythonw has no console
    ui = TkUI()
    ui.run(App(ui))
