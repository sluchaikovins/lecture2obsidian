#!/usr/bin/env python3
"""Records the default microphone to a 16 kHz mono WAV until stopped. Works on macOS and Windows.

Usage: recorder.py <output.wav>

Stops on SIGINT/SIGTERM or when <output>.stop appears (Windows has no SIGINT for other processes).
While recording, <output>.lock holds the pid; it disappears on a clean stop.

The file grows as audio comes in (live.py, recap.py and the mic watchdog read it while it's being
written) and the WAV header is updated every few seconds, so even a hard kill leaves a valid file
that is at most a few seconds short.
"""
import os
import queue
import signal
import struct
import sys
import time
from pathlib import Path

import sounddevice as sd

RATE, CHANNELS, WIDTH = 16000, 1, 2
HEADER_EVERY = 5.0  # seconds between WAV header updates


def wav_header(data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, RATE, RATE * CHANNELS * WIDTH, CHANNELS * WIDTH, WIDTH * 8)
            + b"data" + struct.pack("<I", data_bytes))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    stop_file, lock_file = out.with_suffix(".stop"), out.with_suffix(".lock")
    stop_file.unlink(missing_ok=True)

    stopping = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.append(True))

    chunks = queue.Queue()

    def on_audio(indata, frames, t, status):
        chunks.put(bytes(indata))

    try:
        stream = sd.RawInputStream(samplerate=RATE, channels=CHANNELS, dtype="int16", callback=on_audio,
                                   blocksize=RATE // 10)
    except Exception as e:  # no mic / access denied
        sys.stderr.write(f"cannot open microphone: {e}\n")
        sys.exit(1)

    written = 0
    with open(out, "wb") as f:
        f.write(wav_header(0))
        f.flush()
        lock_file.write_text(str(os.getpid()))
        print(f"recording → {out}", flush=True)
        last_header = time.time()
        with stream:
            while not stopping and not stop_file.exists():
                try:
                    data = chunks.get(timeout=0.2)
                except queue.Empty:
                    continue
                f.write(data)
                written += len(data)
                f.flush()
                if time.time() - last_header > HEADER_EVERY:
                    # Keep the sizes in the header current, then return to the end.
                    f.seek(0)
                    f.write(wav_header(written))
                    f.seek(0, os.SEEK_END)
                    f.flush()
                    last_header = time.time()
        while not chunks.empty():  # what arrived while closing
            data = chunks.get_nowait()
            f.write(data)
            written += len(data)
        f.seek(0)
        f.write(wav_header(written))
    lock_file.unlink(missing_ok=True)
    stop_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
