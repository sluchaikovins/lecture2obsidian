"""Everything that differs between macOS and Windows, behind one small interface.

macOS uses the tools that ship with it (afconvert, sips, mdls, osascript, caffeinate, pmset).
Windows uses ffmpeg (downloaded by install.ps1 into bin/), Pillow and the Win32 API through ctypes.
"""
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
ROOT = Path(__file__).resolve().parent
EXE = ".exe" if IS_WIN else ""
HIDDEN = {"creationflags": 0x08000000} if IS_WIN else {}  # CREATE_NO_WINDOW: no console flashing up


def short_path(arg):
    """Windows: a path with non-Latin letters (C:\\Users\\Сева\\…) as its 8.3 short form, which old C programs
    like whisper-cli can open. A file that doesn't exist yet keeps its name, the folder is shortened."""
    a = str(arg)
    if not IS_WIN or a.isascii() or not (os.path.sep in a or "/" in a):
        return a
    import ctypes
    p = Path(a)
    target, tail = (p, "") if p.exists() else (p.parent, p.name)
    if not target.exists():
        return a
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(str(target), buf, 1024):
        return os.path.join(buf.value, tail) if tail else buf.value
    return a


def run(args, **kw):
    return subprocess.run([short_path(a) for a in args], **HIDDEN, **kw)


def tool(name: str) -> str:
    """A helper program: our bin/ first, then PATH."""
    local = ROOT / "bin" / (name + EXE)
    return str(local) if local.exists() else (shutil.which(name) or name)


# ---------- notifications ----------

def notify(title: str, text: str):
    if IS_MAC:
        run(["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
             "-e", "end run", "--", title, text], check=False)
    elif IS_WIN:
        # A tray balloon, which Windows 10/11 shows as a toast. No extra modules needed.
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
              "$n.ShowBalloonTip(8000, $env:L2O_T, $env:L2O_X, 'Info'); Start-Sleep 9; $n.Dispose()")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         env=dict(os.environ, L2O_T=title, L2O_X=text), **HIDDEN)


# ---------- power ----------

def keep_awake():
    """Don't let the computer sleep while this process works (it can take many minutes of GPU time)."""
    if IS_MAC:
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    elif IS_WIN:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def power_note():
    """The model writes ~3x slower on battery or a weak charger; say so up front."""
    if IS_MAC:
        batt = run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
        if "Battery Power" in batt:
            return "мак на батарее — конспект будет делаться в 2–3 раза дольше. Поставьте на зарядку"
        watts = re.search(r"Wattage = (\d+)W", run(["pmset", "-g", "ac"], capture_output=True, text=True).stdout)
        if watts and int(watts.group(1)) < 20:
            return (f"зарядка слабая ({watts.group(1)} Вт) — конспект будет делаться в разы дольше. "
                    f"Нужна зарядка от мака (от 30 Вт)")
    elif IS_WIN:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = Status()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.ACLineStatus == 0:
            return "ноутбук на батарее — конспект будет делаться заметно дольше. Подключите зарядку"
    return None


# ---------- audio ----------

def to_wav16k(src: Path, dst: Path):
    """Any audio → 16 kHz mono 16-bit WAV (what Whisper wants)."""
    if IS_MAC:
        run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst], check=True)
    else:
        run([tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
            check=True)


def compress_audio(wav: Path, dst_stem: Path) -> Path:
    """A small copy for the vault: AAC on macOS (afconvert), MP3 on Windows (ffmpeg). Returns the file made."""
    if IS_MAC:
        dst = dst_stem.with_suffix(".m4a")
        run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", wav, dst], check=True)
    else:
        dst = dst_stem.with_suffix(".mp3")
        run([tool("ffmpeg"), "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", "-b:a", "32k", dst], check=True)
    return dst


# ---------- photos ----------

def taken_at(path: Path) -> dt.datetime:
    """When a photo was taken: EXIF capture time if there is one, else when the file appeared."""
    if IS_MAC:
        out = run(["mdls", "-raw", "-name", "kMDItemContentCreationDate", path], capture_output=True, text=True).stdout
        try:
            return dt.datetime.strptime(out.strip(), "%Y-%m-%d %H:%M:%S %z").astimezone().replace(tzinfo=None)
        except ValueError:
            return dt.datetime.fromtimestamp(path.stat().st_birthtime)
    try:
        from PIL import Image
        exif = Image.open(path).getexif()
        stamp = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)  # DateTimeOriginal, DateTime
        if stamp:
            return dt.datetime.strptime(stamp, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return dt.datetime.fromtimestamp(path.stat().st_ctime)  # creation time on Windows


def import_photo(src: Path, dst: Path):
    """Copy a photo into the vault as a JPEG no bigger than 2000 px."""
    if IS_MAC:
        run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", "-Z", "2000", src, "--out", dst],
            check=True, stdout=subprocess.DEVNULL)
        return
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    img.thumbnail((2000, 2000))
    img.save(dst, "JPEG", quality=80)


# ---------- programs and places ----------

def ollama_candidates():
    if IS_MAC:
        return [d / "Ollama.app/Contents/Resources/ollama" for d in (Path("/Applications"), Path.home() / "Applications")] \
            + [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama")]
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    return [local / "Programs/Ollama/ollama.exe", Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Ollama/ollama.exe"]


def obsidian_config() -> Path:
    if IS_MAC:
        return Path.home() / "Library/Application Support/obsidian/obsidian.json"
    return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "obsidian/obsidian.json"


def recorder_alive(wav: Path) -> bool:
    """Is someone still recording into `wav`? The recorder keeps a .lock while it runs and the file grows.
    (The old macOS Swift recorder has no lock: then the file growing is the only sign.)"""
    if not wav.exists():
        return False
    idle = time.time() - wav.stat().st_mtime
    # With a lock, allow a minute of no growth (a stalled audio device); a crashed recorder's stale lock
    # stops counting after that.
    return idle < 60 if wav.with_suffix(".lock").exists() else idle < 15
