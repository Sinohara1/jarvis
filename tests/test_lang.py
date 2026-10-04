"""Offline tests for v1.4 languages + custom voices. Run: python tests/test_lang.py (or pytest)"""
import os, sys, tempfile, time as _t, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from jarvis_app import lang as L, tts_local as T
from jarvis_app.config import normalize_settings


def test_detect():
    cases = {"Почему небо голубое?": "ru", "Чому небо блакитне?": "uk", "Котра година?": "uk",
             "Why is the sky blue?": "en", "Warum ist der Himmel blau?": "de", "Wie spät ist es?": "de",
             "Dlaczego niebo jest niebieskie?": "pl", "Która jest godzina?": "pl", "Otwieram YouTube.": "pl",
             "Öffne YouTube.": "de", "Opening YouTube.": "en", "Відкриваю YouTube.": "uk", "Открываю YouTube.": "ru"}
    for t, want in cases.items():
        assert L.detect(t) == want, (t, L.detect(t))
    assert L.detect("Готово.", prefer="uk") == "uk" and L.detect("Готово.") == "ru"  # ambiguous → preference
    assert L.detect("12:30", prefer="de") == "de" and L.detect("") == "ru"
    assert L.detect("YouTube", prefer="pl") == "pl"


def test_reply_lang_and_rules():
    s = {"answer_lang": "auto", "speech_lang": "de"}
    assert L.reply_lang(s, None) == "de" and L.reply_lang(s, "pl") == "pl"
    assert L.reply_lang({"answer_lang": "uk"}, "en") == "uk"
    assert L.reply_lang({"answer_lang": "zz"}, "en") == "ru"  # junk → Russian
    assert "на том же языке" in L.prompt_rule(s)
    assert "по-английски" in L.prompt_rule({"answer_lang": "en"})
    assert "по-польски" in L.prompt_rule(s, "pl", proactive=True)
    r = L.turn_rule({"answer_lang": "en", "user_name": "Вова"}, "en")
    assert "по-английски" in r and "Vova" in r
    assert "Vova" not in L.turn_rule({"answer_lang": "uk", "user_name": "Вова"}, "uk")
    assert "Vova" not in L.turn_rule({"answer_lang": "auto", "user_name": "Вова"}, "ru")
    assert L.translit("Вова") == "Vova" and L.translit("Щука Юля") == "Shchuka Yulya"


def test_localized_phrases():
    assert L.ack("open_url", "ru") == "Открываю." and L.ack("open_url", "de") and L.ack("open_url", "pl")
    assert L.ack("no_such_tool", "en") is None
    assert L.minutes(1, "uk") == "1 хвилина" and L.minutes(3, "uk") == "3 хвилини"
    assert L.minutes(5, "uk") == "5 хвилин" and L.minutes(22, "pl") == "22 minuty"
    assert L.minutes(25, "pl").endswith("minut") and L.minutes(1, "en") == "1 minute"
    for c in L.CODES:
        for key in ("break", "back", "done", "remind", "ok", "did"):
            assert "{" not in L.text(key, c, task="x", mins=L.minutes(5, c), today="1 h", text="x")
        assert L.preview_text(c, "Айри", "Вова")
    assert "Vova" in L.preview_text("en", "Айри", "Вова") and "Вова" in L.preview_text("uk", "Айри", "Вова")
    assert L.nudge("ru", 1, "x", "", "") is None  # Russian keeps the character presets
    n = L.nudge("en", 2, "", "", "")
    assert n and "{" not in n and not n.startswith(",")
    assert L.edge_voice({"voice": "ru-RU-SvetlanaNeural"}, "de") == "de-DE-KatjaNeural"
    assert L.edge_voice({"voice": "ru-RU-DmitryNeural"}, "ru") == "ru-RU-DmitryNeural"
    assert [p["code"] for p in L.payload()] == list(L.CODES) and all(p["vosk_mb"] for p in L.payload())


def test_settings_v14():
    s = normalize_settings({})
    assert s["answer_lang"] == "ru" and s["speech_lang"] == "ru" and s["piper_voices"] == {}
    s = normalize_settings({"answer_lang": "AUTO", "speech_lang": "auto", "piper_voices":
                            {"en": "en_US-lessac-medium", "de": "custom:mein_ding", "xx": "a", "pl": "../../evil"}})
    assert s["answer_lang"] == "auto" and s["speech_lang"] == "ru"  # speech needs a concrete language
    assert s["piper_voices"] == {"en": "en_US-lessac-medium", "de": "custom:mein_ding"}


def test_voice_for_lang():
    male, fem = {"piper_voice": "ru_RU-dmitri-medium"}, {"piper_voice": "ru_RU-irina-medium"}
    for c in ("uk", "en", "de", "pl"):
        a, b = T.voice_for_lang(male, c), T.voice_for_lang(fem, c)
        assert T.voice_lang(a) == c and T.voice_lang(b) == c and a != b, (c, a, b)
        assert T.PIPER_VOICES[a]["gender"] == "м" and T.PIPER_VOICES[b]["gender"] == "ж"
    assert T.voice_for_lang(male, "ru") == "ru_RU-dmitri-medium"
    assert T.voice_for_lang({"piper_voices": {"en": "en_US-amy-medium"}}, "en") == "en_US-amy-medium"
    assert T.voice_for_lang({"piper_voices": {"en": "de_DE-thorsten-medium"}}, "en") != "de_DE-thorsten-medium"
    # uk voices are speakers of one shared file
    ks = [k for k, v in T.PIPER_VOICES.items() if v["lang"] == "uk"]
    assert len({T.file_key(k) for k in ks}) == 1 and len({T.speaker_of(k) for k in ks}) == len(ks)
    # custom voice: used for its own language only, and only while installed
    orig = T.voice_lang, T.installed
    try:
        T.voice_lang = lambda k: "de" if k == "custom:mine" else orig[0](k)
        T.installed = lambda k, *a, **kw: k == "custom:mine" or orig[1](k, *a, **kw)
        assert T.voice_for_lang({"piper_voices": {"de": "custom:mine"}}, "de") == "custom:mine"
        assert T.voice_for_lang({"piper_voices": {"en": "custom:mine"}}, "en") != "custom:mine"
        assert T.voice_for_lang({"piper_voice": "custom:mine"}, "ru") == "ru_RU-dmitri-medium"
        T.installed = lambda k, *a, **kw: False if k == "custom:mine" else orig[1](k, *a, **kw)
        assert T.voice_for_lang({"piper_voices": {"de": "custom:mine"}}, "de") != "custom:mine"
    finally:
        T.voice_lang, T.installed = orig


def test_custom_voice_files():
    assert T.lang_from_config({"language": {"code": "uk_UA"}})[0] == "uk"
    assert T.lang_from_config({"espeak": {"voice": "de"}})[0] == "de"
    assert T._slug("Мой голос (Гося).onnx") == "moy_golos_gosya" and T._slug("!!!.zip") == "voice"
    with tempfile.TemporaryDirectory() as d:
        m = os.path.join(d, "a.onnx"); open(m, "wb").write(b"x")
        try:
            T.import_voice(m, custom_root=os.path.join(d, "v")); assert False
        except T.NeedJson:
            pass
        open(m + ".json", "w").write("{not json")
        assert T.find_json_for(m) == m + ".json"
        for args in [(m,), (os.path.join(d, "nope.onnx"),), (m + ".json",)]:
            try:
                T.import_voice(*args, custom_root=os.path.join(d, "v")); assert False, args
            except T.NeedJson:
                assert False, args
            except (ValueError, RuntimeError):
                pass
        z = os.path.join(d, "two.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a.onnx", "1"); zf.writestr("b.onnx", "2")
        try:
            T.import_voice(z, custom_root=os.path.join(d, "v")); assert False
        except ValueError as e:
            assert "несколько" in str(e)
        assert not os.path.exists(os.path.join(d, "v")) or os.listdir(os.path.join(d, "v")) == []
        # delete only inside the voices folder
        root = os.path.join(d, "v"); os.makedirs(os.path.join(root, "ok"), exist_ok=True)
        assert not T.delete_custom("custom:..", root) and not T.delete_custom("en_US-ryan-medium", root)
        assert T.delete_custom("custom:ok", root) and os.listdir(root) == []


def test_speaker_language_routing():
    from jarvis_app.audio import Speaker, VoiceTurn

    class FakePiper:
        def __init__(self): self.said = []
        def ready(self, key): return key != "pl_PL-darkman-medium"  # pl voice "not downloaded yet"
        def synth(self, text, key, rate):
            self.said.append((text, key))
            return np.zeros(2205, dtype=np.int16), 22050

    fp, missing = FakePiper(), []
    settings = {"tts_engine": "piper", "piper_voice": "ru_RU-dmitri-medium", "tts_rate": 0, "tts_volume": 0,
                "voice": "ru-RU-DmitryNeural", "piper_voices": {}}
    sp = Speaker(lambda: settings, piper=fp, lang_pref=lambda: "en", on_voice_missing=missing.append)
    sp.null_output = True
    assert sp.piper_key(settings, "de") == "de_DE-thorsten-medium" and sp.engine_for(settings, "de") == "piper"
    st = sp.open_stream(VoiceTurn("voice"))
    st.lang = "de"
    st.feed("Hallo, das ist ein Test.")
    st.feed("12:30.")  # no letters → the stream's language
    st.end()
    for _ in range(200):
        if sp.idle: break
        _t.sleep(0.02)
    assert fp.said == [("Hallo, das ist ein Test.", "de_DE-thorsten-medium"), ("12:30.", "de_DE-thorsten-medium")]
    assert sp.last_voice == "de_DE-thorsten-medium"
    fp.said.clear()
    sp.say("Привет.", lang="ru")
    for _ in range(200):
        if sp.idle and fp.said: break
        _t.sleep(0.02)
    assert fp.said == [("Привет.", "ru_RU-dmitri-medium")]
    assert sp.engine_for(settings, "pl") == "edge"  # missing voice → edge with a Polish voice meanwhile


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
