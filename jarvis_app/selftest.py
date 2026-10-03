"""`Jarvis.exe --selftest`: silent diagnostics written to jarvis.log (no mic, no sound, no screenshots sent)."""

from __future__ import annotations

import logging
import os
import tempfile
import time

log = logging.getLogger("jarvis")


def run() -> int:
    from . import config, audio, wakeword
    from .brain import Brain
    s = config.load_settings()
    fails = 0

    def step(name, fn):
        nonlocal fails
        t = time.monotonic()
        try:
            res = fn()
            log.info("selftest %-10s OK  %.1fs %s", name, time.monotonic() - t, res if res is not None else "")
        except Exception as e:
            fails += 1
            log.error("selftest %-10s FAIL %.1fs %s: %s", name, time.monotonic() - t, type(e).__name__, e)

    def tts():
        p = os.path.join(tempfile.gettempdir(), "jarvis_selftest.mp3")
        audio.synth_to_file("Проверка.", s.get("voice", "ru-RU-DmitryNeural"), 0, p)
        n = os.path.getsize(p)
        if os.name == "nt":
            m = audio._MCI()
            m.cmd(f'open "{p}" type mpegvideo alias jselftest')
            m.cmd("close jselftest")
        os.remove(p)
        return f"{n} bytes"

    def wake():
        import numpy as np
        w = wakeword.WakeWordModel()
        return round(float(max(w.process(np.zeros(1280, dtype=np.int16)) for _ in range(20))), 4)

    def mic():
        import sounddevice as sd
        return sd.query_devices(kind="input")["name"]

    def ai():
        return Brain(lambda: s, None).test_connection(s.get("provider", "gemini"))

    step("tts", tts)
    step("wakeword", wake)
    step("mic", mic)
    step("ai", ai)
    log.info("selftest done: %d failure(s)", fails)
    return fails
