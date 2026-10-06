"""Optional Whisper STT (v1.5.x): pure helpers, settings and the core routing with a fake engine.
faster-whisper itself is NOT needed for these tests."""
import io
import os
import queue
import sys
import threading
import types
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app import stt_whisper as W  # noqa: E402


def _wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(rate * seconds))
    return buf.getvalue()


# ── pure helpers ──
def test_allowed_languages():
    assert W.allowed_languages("pair", "ru") == ["ru", "en"]
    assert W.allowed_languages("pair", "en") == ["en"]
    assert W.allowed_languages("speech", "uk") == ["uk"]
    assert W.allowed_languages("auto", "ru") == []


def test_choose_language_restricts_to_allowed():
    probs = [("uk", 0.5), ("ru", 0.3), ("en", 0.2)]
    assert W.choose_language(probs, ["ru", "en"], "ru") == "ru"   # Ukrainian guess on Russian speech → ru
    assert W.choose_language(probs, [], "ru") == "uk"
    assert W.choose_language([], ["ru", "en"], "ru") == "ru"
    assert W.choose_language([("en", 0.9), ("ru", 0.1)], ["ru", "en"], "ru") == "en"


def test_hallucinations_and_clean():
    assert W.is_hallucination("Продолжение следует...")
    assert W.is_hallucination("Субтитры сделал DimaTorzok")
    assert W.is_hallucination("Thank you for watching!")
    assert W.is_hallucination("   ")
    assert not W.is_hallucination("Открой телеграм и напиши маме привет")
    assert W.clean_text("  Открой телеграм...  ") == "Открой телеграм"
    assert W.clean_text("— play music on Spotify…") == "play music on Spotify"


def test_prompt_echo():
    p = W.build_prompt("Пятница", ["Nehto", "Мама"], "ru")
    assert p.startswith("Команды ассистенту:") and "Пятница" in p and "Nehto" in p and "Telegram" in p
    assert W.is_prompt_echo("Команды ассистенту: Пятница, Telegram", p)
    assert W.is_prompt_echo("Telegram, Spotify, Discord, YouTube", p)
    assert not W.is_prompt_echo("Telegram", p)            # a one-word command is real
    assert not W.is_prompt_echo("открой Spotify", p)
    assert W.build_prompt("Jarvis", [], "en").startswith("Commands:")


def test_fix_lang_follows_script():
    assert W.fix_lang("Который час?", "en", "ru") == "ru"
    assert W.fix_lang("Котра година?", "en", "uk") == "uk"
    assert W.fix_lang("Write Nehto on Telegram", "ru", "ru") == "en"
    assert W.fix_lang("Включи lo-fi в Spotify", "ru", "ru") == "ru"
    assert W.fix_lang("Write Nehto on Telegram", "en", "ru") == "en"
    assert W.fix_lang("", "ru", "ru") == "ru"


def test_segments_confidence():
    S = types.SimpleNamespace
    hi = [S(text="открой телеграм", avg_logprob=-0.1, no_speech_prob=0.01)]
    lo = [S(text="ммм", avg_logprob=-2.5, no_speech_prob=0.9)]
    assert W.segments_confidence(hi) > 0.85
    assert W.segments_confidence(lo) < 0.05
    assert W.segments_confidence([]) == 0.0


def test_pick_device():
    assert W.pick_device("auto", "NVIDIA GeForce RTX 5070 Ti", True) == ("cuda", "float16")
    assert W.pick_device("auto", "NVIDIA GeForce RTX 5070 Ti", False) == ("cpu", "int8")  # no cuBLAS yet
    assert W.pick_device("cpu", "NVIDIA", True) == ("cpu", "int8")
    assert W.pick_device("cuda", "", True) == ("cpu", "int8")
    assert W.pick_device("weird", "NVIDIA", True) == ("cuda", "float16")


def test_wav_to_float():
    a = W.wav_to_float(_wav(0.5))
    assert a is not None and a.size == 8000 and abs(float(a[0]) - 16 / 32768) < 1e-6
    assert W.wav_to_float(_wav(0.5, rate=44100)) is None


def test_catalog_and_installed(tmp_path, monkeypatch):
    from jarvis_app import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    assert W.norm_model("LARGE-V3-TURBO") == "large-v3-turbo" and W.norm_model("tiny") == W.DEFAULT_MODEL
    cat = W.catalog()
    assert [c["key"] for c in cat] == ["small", "medium", "large-v3-turbo"]
    assert not any(c["installed"] for c in cat)
    d = W.model_dir("small")
    assert d.startswith(str(tmp_path)) and d.replace("\\", "/").endswith("whisper/models/small")
    os.makedirs(d)
    for f in W.MODELS["small"]["files"]:
        open(os.path.join(d, f), "wb").close()
    assert not W.installed("small")              # model.bin of the wrong size = broken download
    assert W.delete_model("small") and not os.path.isdir(d)
    assert not W.cuda_installed()


def test_engine_not_loaded_returns_empty():
    e = W.WhisperEngine()
    assert e.transcribe(_wav(1.0)) == ("", 0.0, "")
    assert e.info()["loaded"] is False


# ── settings ──
def test_settings_normalize():
    from jarvis_app.config import normalize_settings
    s = normalize_settings({})
    assert (s["stt_engine"], s["whisper_model"], s["whisper_device"], s["whisper_lang"], s["stt_en_model"]) == \
        ("vosk", "large-v3-turbo", "auto", "pair", "small")
    s = normalize_settings({"stt_engine": "Whisper", "whisper_model": "medium", "whisper_device": "cpu",
                            "whisper_lang": "speech", "stt_en_model": "lgraph"})
    assert (s["stt_engine"], s["whisper_model"], s["whisper_device"], s["whisper_lang"], s["stt_en_model"]) == \
        ("whisper", "medium", "cpu", "speech", "lgraph")
    s = normalize_settings({"stt_engine": "x", "whisper_model": "huge", "whisper_device": "tpu", "whisper_lang": "?"})
    assert (s["stt_engine"], s["whisper_model"], s["whisper_device"], s["whisper_lang"]) == \
        ("vosk", "large-v3-turbo", "auto", "pair")


# ── core routing with a fake engine ──
class FakeEngine:
    def __init__(self, result=("Write Nehto on Telegram, hello", 0.9, "en"), ready=True):
        self.result, self._ready, self.calls = result, ready, []
        self.name, self.device, self.model = "large-v3-turbo", "cuda", object()

    def ready(self, name=None):
        return self._ready

    def transcribe(self, wav, **kw):
        self.calls.append(kw)
        return self.result

    def info(self):
        return {"loaded": self._ready, "model": self.name, "device": self.device}


def _core(settings, monkeypatch, engine):
    from jarvis_app.config import normalize_settings
    from jarvis_app.core import JarvisCore
    monkeypatch.setattr(W, "engine", lambda: engine)
    c = JarvisCore.__new__(JarvisCore)
    c.settings = normalize_settings(settings)
    c.user_lang = "ru"
    c._jobs = queue.Queue()
    c.emit = lambda *a, **k: None
    c.state = "idle"
    c.answered = []
    c._answer = lambda text, **kw: c.answered.append(text)
    c.brain = types.SimpleNamespace(transcribe=lambda wav, lang=None: c.answered.append("CLOUD") or "")
    c._rec_lock = threading.Lock()
    c._rec = c._rec_mode = None
    c.wake = None
    c.speaker = types.SimpleNamespace(speaking=False)
    c._init_listen_state()  # v1.6 conversation-mode bookkeeping
    return c


def _run_worker(c, job):
    c._jobs.put(job)
    c._jobs.put(None)
    c._worker()


WH = {"stt_mode": "local", "stt_engine": "whisper", "speech_lang": "ru", "voice_contacts": ["Nehto"]}


def test_worker_uses_whisper_and_its_language(monkeypatch):
    from jarvis_app.audio import VoiceTurn
    eng = FakeEngine()
    c = _core(WH, monkeypatch, eng)
    t = VoiceTurn("voice")
    _run_worker(c, ("voice", (t, _wav(), "")))
    assert t.stt == "whisper" and t.lang == "en"
    assert c.user_lang == "en"
    assert c.answered and "Nehto" in c.answered[0] and "CLOUD" not in c.answered
    kw = eng.calls[0]
    assert kw["lang_mode"] == "pair" and kw["speech_lang"] == "ru" and kw["prompt"] == ""


def test_worker_low_confidence_falls_back_to_vosk_text(monkeypatch):
    from jarvis_app.audio import VoiceTurn
    c = _core(WH, monkeypatch, FakeEngine(result=("эм", 0.1, "ru")))
    t = VoiceTurn("voice")
    t.fallback = "открой спотт"
    _run_worker(c, ("voice", (t, _wav(), "")))
    assert t.stt == "local" and c.answered == ["открой Spotify"]


def test_worker_whisper_empty_no_fallback_goes_to_cloud(monkeypatch):
    from jarvis_app.audio import VoiceTurn
    c = _core(WH, monkeypatch, FakeEngine(result=("", 0.0, "")))
    _run_worker(c, ("voice", (VoiceTurn("voice"), _wav(), "")))
    assert c.answered == ["CLOUD"]  # cloud returned "" → «Не расслышал», nothing answered


def test_vosk_engine_does_not_touch_whisper(monkeypatch):
    from jarvis_app.audio import VoiceTurn
    eng = FakeEngine()
    c = _core(dict(WH, stt_engine="vosk"), monkeypatch, eng)
    t = VoiceTurn("voice")
    t.stt = "local"
    _run_worker(c, ("voice", (t, _wav(), "который час")))
    assert eng.calls == [] and c.answered == ["который час"]


def test_record_thread_defers_to_whisper_keeping_vosk_fallback(monkeypatch):
    c = _core(WH, monkeypatch, FakeEngine())
    rec = types.SimpleNamespace(run=lambda: _wav(), speech_detected=True, t_speech_end=None, t_end=None,
                                stt=object(), text="открой телеграм", conf=0.95, cancelled=False)
    import jarvis_app.core as core_mod
    monkeypatch.setattr(core_mod, "beep", lambda *a: None)
    c._record_thread(rec, "auto")
    _kind, (turn, wav, text) = c._jobs.get_nowait()
    assert text == "" and turn.fallback == "открой телеграм" and wav


def test_record_thread_vosk_engine_unchanged(monkeypatch):
    c = _core(dict(WH, stt_engine="vosk"), monkeypatch, FakeEngine())
    rec = types.SimpleNamespace(run=lambda: _wav(), speech_detected=True, t_speech_end=None, t_end=None,
                                stt=object(), text="открой телеграм", conf=0.95, cancelled=False)
    import jarvis_app.core as core_mod
    monkeypatch.setattr(core_mod, "beep", lambda *a: None)
    c._record_thread(rec, "auto")
    _kind, (turn, wav, text) = c._jobs.get_nowait()
    assert text == "открой телеграм" and turn.stt == "local"


def test_whisper_info_without_package(monkeypatch):
    c = _core(WH, monkeypatch, FakeEngine(ready=False))
    c._whisper_dl, c._whisper_err = None, None
    monkeypatch.setattr(W, "available", lambda: False)
    monkeypatch.setattr(W, "package_present", lambda: False)
    monkeypatch.setattr(W, "nvidia_gpu", lambda probe=True: "")
    info = c.whisper_info()
    assert info["engine"] == "whisper" and info["available"] is False
    assert [m["key"] for m in info["catalog"]] == ["small", "medium", "large-v3-turbo"]
    assert c.download_whisper("small") is False and "сборку" in c._whisper_err


# ── Vosk: «Точная» English model for the bilingual pass ──
def test_english_model_lgraph_falls_back_to_small(monkeypatch):
    from jarvis_app import namewake, stt_local
    loaded = []
    monkeypatch.setattr(namewake, "find_model", lambda name: None)          # lgraph not downloaded
    monkeypatch.setattr(stt_local, "model_path", lambda code: "/m/small-en")
    monkeypatch.setattr(stt_local, "get_model", lambda path: loaded.append(path) or "MODEL")
    assert stt_local.english_model(allow_download=False, variant="lgraph") == "MODEL"
    assert loaded == ["/m/small-en"]
    monkeypatch.setattr(namewake, "find_model", lambda name: "/m/" + name)
    loaded.clear()
    assert stt_local.english_model(allow_download=False, variant="lgraph") == "MODEL"
    assert loaded == ["/m/vosk-model-en-us-0.22-lgraph"]
    loaded.clear()
    stt_local.english_model(allow_download=False, variant="bogus")           # unknown → small
    assert loaded == ["/m/small-en"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
