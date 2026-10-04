"""`Jarvis.exe --selftest`: silent diagnostics written to jarvis.log (no mic, no audible sound — the output
device is opened and fed zeros; no screenshots sent; vosk/piper models only loaded if present)."""

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

    def step(name, fn, optional=False):
        nonlocal fails
        t = time.monotonic()
        try:
            res = fn()
            log.info("selftest %-10s OK  %.2fs %s", name, time.monotonic() - t, res if res is not None else "")
        except Exception as e:
            if optional:
                log.warning("selftest %-10s WARN %.2fs %s: %s (optional)", name, time.monotonic() - t, type(e).__name__, e)
                return
            fails += 1
            log.error("selftest %-10s FAIL %.2fs %s: %s", name, time.monotonic() - t, type(e).__name__, e)

    def piper_check():
        from . import tts_local
        import piper  # noqa: F401  (bundled engine + espeak-ng data)
        key = s.get("piper_voice") or tts_local.DEFAULT_VOICE
        ed = tts_local.espeak_dir()
        if not tts_local.installed(key):
            return f"engine ok (espeak {'bundled' if ed else 'package'}); voice {key} not downloaded yet"
        eng = tts_local.PiperTTS()
        t0 = time.monotonic()
        eng.load(key)
        load = time.monotonic() - t0
        t0 = time.monotonic()
        pcm, sr = eng.synth("Привет! Это проверка локального голоса.", key, int(s.get("tts_rate", 0)))
        syn = time.monotonic() - t0
        import numpy as np
        import sounddevice as sd
        t0 = time.monotonic()
        with sd.OutputStream(samplerate=sr, channels=1, dtype="int16", latency="low") as out:  # zeros: silent
            out.write(np.zeros(int(sr * 0.2), dtype=np.int16))
        dev = time.monotonic() - t0
        return (f"{key}: load {load:.2f}s, synth {syn:.3f}s for {pcm.size / sr:.1f}s audio "
                f"(RTF {syn / max(0.01, pcm.size / sr):.3f}), output device open+0.2s zeros {dev:.2f}s")

    def vad_check():
        import numpy as np
        from .vad import SileroVAD
        v = SileroVAD()
        t0 = time.monotonic()
        p = max(v.process(np.zeros(1280, dtype=np.int16)) for _ in range(25))
        return f"silero ok, silence prob {p:.3f}, {((time.monotonic() - t0) / 25) * 1000:.2f} ms per 80 ms chunk"

    def stt_check():
        import numpy as np
        from . import stt_local
        path = stt_local.model_path()
        if not path:
            return "vosk model not downloaded yet"
        t0 = time.monotonic()
        st = stt_local.StreamingSTT(stt_local.get_model(path))
        load = time.monotonic() - t0
        for _ in range(10):
            st.feed(np.zeros(1280, dtype=np.int16))
        t0 = time.monotonic()
        text, conf = st.final()
        return f"vosk ok (load {load:.2f}s, final {1000 * (time.monotonic() - t0):.0f} ms, silence → {text!r})"

    def ai_stream():
        from .brain import Brain
        from .config import PROVIDERS
        b = Brain(lambda: s, None)
        prov = b.provider(s["provider"])
        _chat, lite = b.models(s["provider"])
        t0 = time.monotonic()
        first = []
        prov.chat_stream(lite or PROVIDERS[s["provider"]]["lite"], "Отвечай одним коротким предложением.",
                         [prov.user_message("Скажи «готово».")], None,
                         on_text=lambda d: first.append(time.monotonic() - t0) if not first else None, timeout=20,
                         max_tokens=40)
        return f"{lite}: first token {first[0] if first else -1:.2f}s, total {time.monotonic() - t0:.2f}s"

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

    def vosk_check():
        import vosk  # noqa: F401  (libvosk.dll must be bundled)
        from . import namewake
        path = namewake.find_model()
        if path:
            vosk.SetLogLevel(-1)
            vosk.Model(path)
            return f"model ok: {path}"
        return "library ok; model not downloaded yet (downloads on first «по имени»)"

    def ai():
        return Brain(lambda: s, None).test_connection(s.get("provider", "gemini"))

    step("piper", piper_check)
    step("vad", vad_check)
    step("stt", stt_check)
    step("edge-tts", tts, optional=True)  # online fallback engine; flaky by nature
    step("wakeword", wake)
    step("mic", mic)
    step("vosk", vosk_check)
    step("ai-stream", ai_stream, optional=True)
    step("ai", ai, optional=True)  # chat model; the free tier is often exhausted (429) — not a build problem
    log.info("selftest done: %d failure(s)", fails)
    return fails
