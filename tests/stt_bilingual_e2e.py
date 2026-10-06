"""Manual E2E (not a unit test): Piper-synthesised EN/RU phrases → Vosk ru (+en pass) → core cleanup.
Needs vosk + piper-tts and model dirs (vosk-model-small-ru-0.22, vosk_ml/vosk-model-small-en-us-0.15, piper/, piper_ml/)."""
import io, wave, subprocess, sys, time
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from piper import PiperVoice
from jarvis_app import stt_local
from jarvis_app.config import normalize_settings
from jarvis_app.core import JarvisCore
from jarvis_app.stt_commands import english_hint
B = sys.argv[1] if len(sys.argv) > 1 else "/workspace/jarvis_bench"  # dir with the vosk + piper models
P = {"ru": B + "/vosk-model-small-ru-0.22", "en": B + "/vosk_ml/vosk-model-small-en-us-0.15"}
stt_local.model_path = lambda code="ru": P.get(code)
ru = stt_local.get_model(P["ru"]); stt_local.get_model(P["en"])
def core(names):
    c = JarvisCore.__new__(JarvisCore); c.settings = normalize_settings({"speech_lang": "ru", "voice_contacts": names}); c.user_lang = "ru"; return c
voices = {}
def synth(voice, text):
    v = voices.get(voice) or voices.setdefault(voice, PiperVoice.load(voice))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        v.synthesize_wav(text, w)
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "-", "-ar", "16000", "-ac", "1", "-f", "wav", "-"],
                          input=buf.getvalue(), capture_output=True).stdout
EN = [B + "/piper_ml/en_US-ryan-medium.onnx", B + "/piper_ml/en_US-lessac-medium.onnx"]
RU = [B + "/piper/ru_RU-dmitri-medium.onnx", B + "/piper/ru_RU-irina-medium.onnx"]
cases = [(v, t) for v in EN for t in ("Write Nehto on Telegram, hello I'm Jarvis", "Write Mama on Telegram, I am home",
                                     "Play Numb on Spotify", "Play Believer on Spotify")] + \
        [(v, t) for v in RU for t in ("Напиши маме в телеграм, я дома", "Открой спотифай", "Он смотрит телевизор",
                                     "Включи музыку в спотифае", "Какая погода завтра", "Он спит, не буди его")]
for voice, text in cases:
    wav = synth(voice, text)
    rt, rc = stt_local.transcribe_wav(ru, wav)
    c = core(["Nehto", "Mama", "Nikita"])
    t0 = time.monotonic()
    out = c._clean_voice_text(rt, wav, "ru", local=True) if stt_local.acceptable(rt, rc) else (c._english_rescue(rt, wav) or "<cloud>")
    ms = 1000 * (time.monotonic() - t0)
    print(f"{voice.split('/')[-1][:12]:12} SAID {text!r}\n   ru {rt!r} {rc:.2f} hint={english_hint(rt)}\n   -> {out!r}  lang={c.user_lang} {ms:.0f}ms")
