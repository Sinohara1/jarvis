"""End-to-end voice latency through the real JarvisCore pipeline, without a microphone or audible sound.

A fake mic feeds edge-tts-generated Russian questions in real time (80 ms chunks + quiet room noise);
the core records (Silero VAD end-of-speech), recognises (local Vosk or Gemini), streams the reply from
the model and speaks it sentence by sentence (Piper or edge-tts). Playback is silent: zeros to the real
output device (--silent, Windows) or no device at all (--null-audio, servers). PC actions are stubbed.

  python tests/voice_latency.py --wavs DIR [--n 4] [--null-audio|--silent] [--stt local|cloud] [--engine piper|edge]
       [--fast 1|0] [--silence-ms 700]
Prints one JSON line per question and the mean breakdown (seconds, from end of speech).
"""
import argparse, json, os, queue, sys, threading, time, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--wavs", required=True)
ap.add_argument("--n", type=int, default=4)
ap.add_argument("--ids", default="2,5,8,1,9,3,0,6,7,4")
ap.add_argument("--null-audio", action="store_true")
ap.add_argument("--silent", action="store_true")
ap.add_argument("--stt", default="")
ap.add_argument("--engine", default="")
ap.add_argument("--fast", default="")
ap.add_argument("--silence-ms", type=int, default=0)
ap.add_argument("--noise", type=float, default=40.0)
ap.add_argument("--pause", type=float, default=4.0, help="seconds between questions (free-tier RPM)")
ap.add_argument("--log", default="", help="write the app log here (INFO)")
ap.add_argument("--speech-lang", default="", help="ru|uk|en|de|pl — the language of the WAVs")
ap.add_argument("--answer-lang", default="", help="auto|ru|uk|en|de|pl")
ap.add_argument("--import-voice", default="", help="import this Piper .onnx/.zip as a custom voice first (deleted after)")
a = ap.parse_args()
if a.log:
    from jarvis_app.config import setup_logging
    setup_logging(a.log)

from jarvis_app import config
from jarvis_app.audio import rms_int16
from jarvis_app.core import JarvisCore


class FakeHub:
    """Stands in for MicHub: plays a WAV into subscribers in real time, then quiet noise."""
    def __init__(self):
        self.pcm = None
        self.speech_end_t = None

    def subscribe(self):
        q = queue.Queue()
        pcm = self.pcm
        def feed():
            rng = np.random.default_rng(0)
            noise = lambda n: rng.normal(0, a.noise, int(n)).astype(np.int16)
            last = max(i for i in range(0, len(pcm), 1280) if rms_int16(pcm[i:i + 1280]) > 300) + 1280
            speech = (pcm[:last].astype(np.float32) + noise(last)).clip(-32768, 32767).astype(np.int16)
            x = np.concatenate([noise(4800), speech, noise(16000 * 5)])
            end_at = 4800 + last
            t0 = time.monotonic()
            self.speech_end_t = None
            for k, i in enumerate(range(0, len(x), 1280)):
                if self.stop:
                    return
                time.sleep(max(0, t0 + (k + 1) * 0.08 - time.monotonic()))  # chunk arrives when it is complete
                q.put(x[i:i + 1280])
                if self.speech_end_t is None and i + 1280 >= end_at:
                    self.speech_end_t = time.monotonic() - (i + 1280 - end_at) / 16000.0
        self.stop = False
        threading.Thread(target=feed, daemon=True).start()
        return 1, q

    def unsubscribe(self, sid):
        self.stop = True

    def close(self):
        pass


class NoActions:
    def execute(self, name, args):
        return {"ok": True, "note": "test run: action not executed"}


events = []
done = threading.Event()
timing = {}

def emit(ev, **d):
    events.append((ev, d))
    if ev == "voice_timing":
        timing.update(d)
        timing["_at"] = time.monotonic()
    if ev == "state" and d.get("state") == "idle" and timing:
        done.set()

core = JarvisCore(emit)
s = dict(core.settings)
if a.stt: s["stt_mode"] = a.stt
if a.engine: s["tts_engine"] = a.engine
if a.fast: s["fast_replies"] = a.fast == "1"
if a.silence_ms: s["vad_silence_ms"] = a.silence_ms
if a.speech_lang: s["speech_lang"] = a.speech_lang
if a.answer_lang: s["answer_lang"] = a.answer_lang
s["speak_replies"] = True
core.settings = config.normalize_settings(s)  # not saved to disk
hub = FakeHub()
core.mic = hub
core.brain.actions = NoActions()
core.speaker.silent = a.silent
core.speaker.null_output = a.null_audio
threading.Thread(target=core._worker, daemon=True).start()
t0 = time.monotonic()
core._prepare_voice()
print(f"prepare: {time.monotonic() - t0:.1f}s; engine={core.speaker.engine_for(core.settings)} stt={core.settings['stt_mode']} "
      f"fast={core.settings['fast_replies']} silence={core.settings['vad_silence_ms']}ms", flush=True)
from jarvis_app import lang as L, stt_local
imported = None
if a.import_voice:
    import jarvis_app.core as _core_mod
    _core_mod.save_settings = lambda *_a, **_k: None  # test settings must not land in settings.json
    res = core.import_voice(a.import_voice)
    imported = (res.get("voice") or {}).get("key")
    print("import:", {k: res[k] for k in ("ok", "lang_label") if k in res}, imported, res.get("error", ""), flush=True)
for _ in range(200):  # wait for the piper voice / vosk model download + load
    voice_ok = (core.settings["tts_engine"] != "piper" or core.speaker.engine_for(core.settings, core._reply_lang()) == "piper"
                or core._piper_err)
    stt_ok = core.settings["stt_mode"] != "local" or stt_local.is_loaded(L.speech_lang(core.settings)) or core._stt_err
    if voice_ok and stt_ok:
        break
    time.sleep(1)
print(f"ready: voice={core.speaker.piper_key(core.settings, core._reply_lang())} "
      f"stt={stt_local.model_path(L.speech_lang(core.settings))}", flush=True)

texts = open(os.path.join(a.wavs, "text.txt"), encoding="utf-8").read().splitlines()
rows = []
for i in [int(x) for x in a.ids.split(",")][:a.n]:
    w = wave.open(os.path.join(a.wavs, f"q{i}.wav"))
    hub.pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    timing.clear(); done.clear(); events.clear()
    core.toggle_listen()
    ok = done.wait(60)
    heard = next((d["text"] for e, d in events if e == "chat" and d.get("role") == "user"), "")
    reply = next((d["text"] for e, d in events if e == "chat" and d.get("role") == "jarvis"), "")
    err = next((d["text"] for e, d in events if e == "chat" and d.get("role") == "system"), "")
    # first_audio from the core is measured from the VAD's last voiced chunk; also report vs the true end of speech
    at = timing.pop("_at", None)
    r = dict(q=texts[i], heard=heard, reply=reply[:90], error=err, ok=ok, **timing)
    r["reply_lang"] = L.detect(reply) if reply else None
    r["voice"] = core.speaker.last_voice
    # the core measures from the VAD's last voiced chunk; this is from the true end of the WAV's speech
    r["true_first_audio"] = round(at - hub.speech_end_t, 3) if at and hub.speech_end_t else None
    rows.append(r)
    print(json.dumps(r, ensure_ascii=False), flush=True)
    time.sleep(a.pause)

keys = ["endpoint", "stt", "llm_first", "first_sentence", "tts", "play", "first_audio", "true_first_audio"]
if imported:
    print("delete:", core.delete_voice(imported), flush=True)
good = [r for r in rows if r.get("first_audio") is not None]
if good:
    print("MEAN", json.dumps({k: round(sum(r[k] or 0 for r in good) / len(good), 3) for k in keys}), f"({len(good)}/{len(rows)} ok)")
else:
    print("MEAN none ok")
