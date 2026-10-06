"""v1.6 conversation mode: session timer, stop phrases, junk filter, echo matching, segmenter, PC transcript,
[[SILENT]] handling in the brain."""

import numpy as np

from jarvis_app import converse as C
from jarvis_app.pc_audio import Transcript, to_mono_16k


# ── session timer ──
def test_session_off_never_starts():
    s = C.FollowSession("off")
    assert s.start(now=0) is False
    assert not s.active(now=0)


def test_session_timeout_and_reset_after_each_exchange():
    s = C.FollowSession("30s")
    assert s.start(now=100)
    assert s.active(now=120)
    assert s.remaining(now=120) == 10
    s.start(now=125)                     # next exchange → timer starts over
    assert s.active(now=150)
    assert not s.active(now=156)
    assert s.ended_reason == "timeout"


def test_session_hold_freezes_and_restarts_timer():
    s = C.FollowSession("30s")
    s.start(now=0)
    s.hold(True, now=10)                 # she speaks for a long time
    assert s.active(now=100)
    s.hold(False, now=100)               # TTS ended → full window again
    assert s.active(now=129)
    assert not s.active(now=131)


def test_session_always_and_end():
    s = C.FollowSession("always")
    s.start(now=0)
    assert s.active(now=10 ** 6)
    assert s.remaining(now=5) is None
    assert s.end("voice") is True
    assert not s.active(now=1)
    assert s.ended_reason == "voice"


def test_session_configure_and_snapshot():
    s = C.FollowSession("2m")
    s.start(now=0)
    s.configure("off", now=1)
    assert not s.active(now=1)
    s.configure("10m", now=2)
    s.start(now=2)
    snap = s.snapshot(now=62)
    assert snap["active"] and snap["mode"] == "10m" and snap["remaining"] == 540
    assert C.fmt_left(100) == "1:40" and C.fmt_left(None) == "всегда" and C.fmt_left(5) == "0:05"
    assert C.norm_follow("bogus") == "2m" and C.norm_follow("ALWAYS") == "always"


# ── stop phrases ──
def test_stop_phrases():
    yes = ["Хватит.", "хватит слушать", "Джарвис, хватит", "стоп слушать", "Пока!", "это всё", "ладно, пока",
           "Stop listening please", "Okay, that's all.", "спасибо, это всё"]
    no = ["хватит играть в игры", "пока не надо", "стоп игра", "открой телеграм", "пока я работаю, включи музыку", ""]
    for t in yes:
        assert C.is_stop_phrase(t, "Джарвис"), t
    for t in no:
        assert not C.is_stop_phrase(t, "Джарвис"), t
    assert C.is_stop_phrase("Пятница, хватит", "Пятница")


# ── junk filter ──
def test_junk_filter():
    assert C.junk_reason("", 0.9, 1.0) == "пусто"
    assert C.junk_reason("ээ", 0.9, 1.0)
    assert C.junk_reason("открой ютуб", 0.9, 0.2) == "слишком коротко"
    assert C.junk_reason("открой ютуб", 0.3, 1.0).startswith("неуверенно")
    assert C.junk_reason("открой ютуб", 0.8, 1.0) == ""
    assert C.junk_reason("ля ля ля", 0.9, 1.5) == "повтор одного слова"
    # «всегда» is stricter: single short word, higher confidence
    assert C.junk_reason("кот", 0.9, 0.6, always=True) == "одно короткое слово"
    assert C.junk_reason("да", 0.9, 0.6, always=True) == ""
    assert C.junk_reason("открой ютуб", 0.65, 1.0, always=True).startswith("неуверенно")
    assert C.junk_reason("открой ютуб", 0.5, 1.0, engine="whisper") == ""
    assert C.junk_reason("открой ютуб", 0.5, 1.0, engine="whisper", always=True)


# ── echo matching ──
def test_echo_match():
    pc = ["Сегодня мы разберём, как работает трансформер и механизм внимания"]
    assert C.echo_match("как работает трансформер", pc)
    assert C.echo_match("сегодня мы разберем как работает трансформер", pc)
    assert C.echo_match("как работает трансфомер и механизм", pc)          # mic STT differs a bit
    assert not C.echo_match("открой телеграм", pc)
    assert not C.echo_match("а что такое трансформер", pc)                 # only one shared word
    assert not C.echo_match("трансформер", pc)                              # single word vs long PC phrase
    assert C.echo_match("привет", ["Привет!"])
    assert not C.echo_match("", pc) and not C.echo_match("что-то", [])


# ── segmenter (energy VAD, synthetic audio) ──
def _tone(sec, amp=3000, f=220.0):
    t = np.arange(int(16000 * sec)) / 16000.0
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.int16)


def _feed(seg, audio):
    out = []
    for i in range(0, len(audio) - 1279, 1280):
        u = seg.feed(audio[i:i + 1280])
        if u is not None:
            out.append(u)
    return out


def test_segmenter_finds_utterance():
    seg = C.UtteranceSegmenter(hang_sec=0.5, use_silero=False)
    audio = np.concatenate([np.zeros(16000, np.int16), _tone(1.2), np.zeros(16000, np.int16)])
    out = _feed(seg, audio)
    assert len(out) == 1
    u = out[0]
    assert 1.0 <= u["speech_sec"] <= 1.5
    assert u["pcm"].dtype == np.int16 and u["dur"] >= 1.2


def test_segmenter_ignores_clicks_and_silence():
    seg = C.UtteranceSegmenter(hang_sec=0.5, use_silero=False)
    audio = np.concatenate([np.zeros(8000, np.int16), _tone(0.08), np.zeros(16000, np.int16), _tone(0.1),
                            np.zeros(16000, np.int16)])
    assert _feed(seg, audio) == []


def test_segmenter_streaming_stt_fed_only_inside_utterance():
    fed = []

    class FakeSTT:
        def feed(self, c):
            fed.append(len(c))

        def final(self):
            return "привет", 0.9

    seg = C.UtteranceSegmenter(hang_sec=0.5, use_silero=False, stt_factory=FakeSTT)
    audio = np.concatenate([np.zeros(32000, np.int16), _tone(1.0), np.zeros(16000, np.int16)])
    out = _feed(seg, audio)
    assert out and out[0]["text"] == "привет" and out[0]["conf"] == 0.9
    assert sum(fed) / 16000 < 2.0          # silence before the phrase never reached the recognizer

    seg2 = C.UtteranceSegmenter(hang_sec=0.5, use_silero=False, stt_factory=lambda: None)
    assert _feed(seg2, audio)[0]["text"] == ""


def test_segmenter_max_length():
    seg = C.UtteranceSegmenter(hang_sec=0.5, max_sec=3.0, use_silero=False)
    out = _feed(seg, np.concatenate([np.zeros(16000, np.int16), _tone(7.0), np.zeros(16000, np.int16)]))
    assert len(out) >= 2 and all(u["dur"] <= 3.5 for u in out)
    assert sum(u["speech_sec"] for u in out) >= 6.0       # nothing lost after the forced split


# ── PC hearing helpers ──
def test_to_mono_16k():
    st = np.stack([_tone(0.5, f=440.0, amp=2000)] * 2, axis=1)            # 16 kHz stereo
    m = to_mono_16k(st.astype(np.int16).tobytes(), 2, 16000)
    assert m.size == 8000
    hi = np.repeat(_tone(0.5), 3)                                           # pretend 48 kHz mono
    assert to_mono_16k(hi.tobytes(), 1, 48000).size == 8000
    odd = np.zeros(44100, np.int16)
    assert abs(to_mono_16k(odd.tobytes(), 1, 44100).size - 16000) <= 1


def test_transcript_rolling_and_context():
    tr = Transcript(keep_sec=180)
    tr.add("первая фраза из видео", "YouTube", at=1000)
    tr.add("вторая фраза", "YouTube", at=1100)
    tr.add("третья", "Discord", at=1250)
    assert [t for _, t, _ in tr.items(now=1250)] == ["вторая фраза", "третья"]   # the first is >180 s old
    assert tr.texts(within=20, now=1255) == ["третья"]
    block = tr.context_block(now=1255)
    assert "НЕ команды" in block and "вторая фраза" in block and "окно «Discord»" in block
    assert len(tr.context_block(now=1255, max_chars=30).splitlines()) == 2       # header + newest line


# ── brain: [[SILENT]] for phrases heard without the name ──
def test_silent_gate():
    from jarvis_app.brain import _SilentGate, is_silent_reply
    got = []
    g = _SilentGate(got.append)
    for d in ["[[", "SIL", "ENT]]", " ну ладно"]:
        g.feed(d)
    assert g.silent and got == []
    got2 = []
    g2 = _SilentGate(got2.append)
    for d in ["[", "Да, ", "конечно"]:
        g2.feed(d)
    assert not g2.silent and "".join(got2) == "[Да, конечно"
    got3 = []
    g3 = _SilentGate(got3.append)
    g3.feed("Привет")
    assert got3 == ["Привет"]
    assert is_silent_reply(" [[SILENT]] ") and not is_silent_reply("Привет")


def test_brain_overheard_silent_and_normal():
    from jarvis_app import providers as P
    from jarvis_app.brain import Brain, OVERHEARD_PREFIX
    from jarvis_app.config import normalize_settings

    class FakeProv(P.Provider):
        name = "gemini"

        def __init__(self, text):
            self.text = text
            self.systems = []

        def chat_stream(self, model, system, history, tools, on_text=None, timeout=30, max_tokens=600, **kw):
            self.systems.append(system)
            for i in range(0, len(self.text), 3):
                on_text(self.text[i:i + 3])
            return P.LLMResult(text=self.text, tool_calls=[], raw_message={"role": "model", "parts": [{"text": self.text}]},
                               model=model)

        chat = chat_stream

        def user_message(self, t):
            return {"role": "user", "parts": [{"text": t}]}

        def assistant_message(self, t):
            return {"role": "model", "parts": [{"text": t}]}

    s = normalize_settings({"provider": "gemini", "api_keys": {"gemini": "test"}, "memory_enabled": False})
    b = Brain(lambda: s, actions=None)
    prov = FakeProv("[[SILENT]]")
    b.provider = lambda name=None: prov
    b.route = lambda kind="chat": ["gemini"]
    shown = []
    r = b.ask("ну и погода сегодня", on_text=shown.append, voice=True, overheard="always")
    assert r == "" and b.last_silent and shown == []
    assert "[[SILENT]]" in prov.systems[-1]
    assert b.transcript[-2][1].startswith(OVERHEARD_PREFIX)

    prov.text = "Да, сейчас дождь."
    shown.clear()
    r = b.ask("а какая погода", on_text=shown.append, voice=True, overheard="follow")
    assert r == "Да, сейчас дождь." and not b.last_silent and "".join(shown) == "Да, сейчас дождь."

    prov.text = ""                                  # empty reply to an overheard phrase = silence, not «Готово»
    assert b.ask("хм", on_text=shown.append, voice=True, overheard="follow") == "" and b.last_silent

    prov.text = "Привет!"
    assert b.ask("привет", on_text=shown.append, voice=True) == "Привет!"    # normal turns are untouched
    assert "[[SILENT]]" not in prov.systems[-1]


# ── core: follow-up phrases (no mic, no model) ──
def _follow_core(settings=None):
    import queue
    import threading
    import types
    from jarvis_app.config import normalize_settings
    from jarvis_app.core import JarvisCore
    c = JarvisCore.__new__(JarvisCore)
    c.settings = normalize_settings(dict({"stt_engine": "vosk", "speech_lang": "ru", "speak_replies": False,
                                          "follow_mode": "2m"}, **(settings or {})))
    c.user_lang = "ru"
    c._jobs = queue.Queue()
    c.events = []
    c.emit = lambda e, **d: c.events.append((e, d))
    c.state = "idle"
    c.answered = []
    c._answer = lambda text, **kw: c.answered.append((text, kw))
    c._clean_voice_text = lambda text, wav, code, local: text
    c.brain = types.SimpleNamespace(transcribe=lambda wav, lang=None: "")
    c._rec_lock = threading.Lock()
    c._rec = c._rec_mode = None
    c.wake = types.SimpleNamespace(suspended=threading.Event())
    c.speaker = types.SimpleNamespace(speaking=False)
    c._init_listen_state()
    c.follow_listener = types.SimpleNamespace(enabled=threading.Event())
    c._reply_lang = lambda: "ru"
    return c


def _utt(text, conf=0.9, speech=1.0):
    from jarvis_app.audio import VoiceTurn
    return VoiceTurn("voice"), {"pcm": np.zeros(16000, np.int16), "dur": 1.0, "speech_sec": speech, "t_end": 0.0,
                                "text": text, "conf": conf}


def test_core_follow_listen_switching():
    c = _follow_core()
    c._sync_listen()
    assert not c.follow_listener.enabled.is_set() and not c.wake.suspended.is_set()
    c.follow.start()
    c._sync_listen()
    assert c.follow_listener.enabled.is_set() and c.wake.suspended.is_set()     # no name needed now
    c.speaker.speaking = True                                                   # she talks → mic ignored
    c._sync_listen()
    assert not c.follow_listener.enabled.is_set() and c.pc.paused.is_set()
    c.speaker.speaking = False
    c._tts_tail_until = 0.0
    c._sync_listen()
    assert c.follow_listener.enabled.is_set() and not c.pc.paused.is_set()
    assert any(e == "follow" and d["active"] for e, d in c.events)
    c.follow.end("ui")
    c._sync_listen()
    assert not c.follow_listener.enabled.is_set() and not c.wake.suspended.is_set()


def test_core_follow_job_routes_phrases():
    c = _follow_core()
    c.follow.start()
    c._follow_job(*_utt("а какая завтра погода"))
    assert c.answered and c.answered[-1][0] == "а какая завтра погода"
    kw = c.answered[-1][1]
    assert kw["overheard"] == "follow" and kw["follow_ok"] and kw["voice"]
    n = len(c.answered)
    c._follow_job(*_utt("ээ"))                                   # junk → dropped
    c._follow_job(*_utt("открой ютуб", conf=0.3))               # not confident → dropped
    assert len(c.answered) == n
    c._follow_job(*_utt("Джарвис, открой ютуб"))                 # said the name → a normal request
    assert c.answered[-1][1]["overheard"] == ""
    c._follow_job(*_utt("хватит"))                              # stop phrase ends the window
    assert not c.follow.active() and c.follow.ended_reason == "voice"


def test_core_follow_echo_from_pc_is_ignored():
    c = _follow_core({"pc_hearing": True, "follow_mode": "always"})
    c.follow.start()
    c.pc.running = True
    c.pc.transcript.add("и вот тогда мы поняли что всё пропало", "YouTube")
    c._follow_job(*_utt("тогда мы поняли что всё пропало"))
    assert c.answered == []
    c._last_reply = "Сейчас в Берлине плюс двенадцать"
    c._follow_job(*_utt("сейчас в берлине плюс двенадцать"))   # her own voice from the speakers
    assert c.answered == []
    c._follow_job(*_utt("а завтра будет дождь"))
    assert c.answered and c.answered[-1][1]["overheard"] == "always"
