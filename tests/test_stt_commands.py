"""Spoken English command templates mangled by small Vosk models → canonical intent text."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app.stt_commands import (
    english_score, fix_commands, match_name, name_score, parse_command, pick_bilingual, voice_names,
    wants_english_pass,
)
from jarvis_app.stt_normalize import normalize_brands, normalize_transcript

# The user's report: said «Write Nehto on Telegram, hello Im Jarvice»
SAID_FIXED = "Write Nehto on Telegram, hello I'm Jarvis"
HEARD_EN = "Right next door on Telegram fellow i'm jarvis"        # what the app showed (English model)
HEARD_EN_RAW = "right next door on telegram fellow i'm jarvis"    # raw Vosk-en words
HEARD_RU = "райт нехто он телеграм хелоу айм джарвис"             # same phrase through the Russian model
NAMES = ["Nehto", "Мама", "Nikita", "Sasha"]


def test_exact_user_example_to_intent_text():
    assert normalize_transcript(HEARD_EN, names=["Nehto"]) == SAID_FIXED
    assert normalize_transcript(HEARD_EN_RAW, names=NAMES) == SAID_FIXED
    assert fix_commands(HEARD_EN, names=NAMES) == SAID_FIXED


def test_exact_user_example_parsed_intent():
    cmd = parse_command(HEARD_EN, names=NAMES)
    assert cmd == {"kind": "telegram", "contact": "Nehto", "message": "hello I'm Jarvis", "heard_contact": "next door"}


def test_without_known_names_structure_is_still_fixed():
    # honest limit: an acoustic model can't invent «Nehto» — the verb, app, hello, I'm, Jarvis are fixed
    assert normalize_transcript(HEARD_EN) == "Write next door on Telegram, hello I'm Jarvis"


def test_russian_model_cyrillic_english():
    assert normalize_transcript(HEARD_RU, names=NAMES) == SAID_FIXED
    assert normalize_transcript("Райт нехто он телеграм хеллоу айм джарвис", names=NAMES) == SAID_FIXED
    # no names → contact stays as heard, rest is English
    assert normalize_transcript(HEARD_RU) == "Write нехто on Telegram, hello I'm Jarvis"


def test_cloud_style_punctuated_and_idempotent():
    assert normalize_transcript("Write Nehto on Telegram, hello I'm Jarvis.", names=NAMES) == SAID_FIXED + "."
    once = normalize_transcript(HEARD_EN, names=NAMES)
    assert normalize_transcript(once, names=NAMES) == once
    assert normalize_transcript(normalize_transcript(HEARD_EN)) == normalize_transcript(HEARD_EN)


def test_template_variants():
    assert fix_commands("Hey Jarvis, write Nehto on Telegram: hello", names=NAMES) == "Write Nehto on Telegram, hello"
    assert fix_commands("write on telegram to nehto hello", names=NAMES) == "Write Nehto on Telegram, hello"
    assert fix_commands("jarvis right to mama on telegram i'm home", names=NAMES) == "Write Мама on Telegram, I'm home"
    assert fix_commands("rite sasha on tell a gram yellow how are you", names=NAMES) == \
        "Write Sasha on Telegram, hello how are you"
    assert fix_commands("text nikita on telegram saying i am late", names=NAMES) == "Write Nikita on Telegram, I am late"


def test_wake_name_variants_jarvis_and_airi():
    assert fix_commands("right next door on telegram fellow im jarvice", names=NAMES) == SAID_FIXED
    assert fix_commands("right next door on telegram fellow i'm airy", names=NAMES, assistant_name="Airi") == \
        "Write Nehto on Telegram, hello I'm Airi"
    assert fix_commands("right next door on telegram fellow i'm айри", names=NAMES, assistant_name="Айри") == \
        "Write Nehto on Telegram, hello I'm Airi"
    # outside templates only unmistakable Jarvis misspellings are fixed
    assert fix_commands("i spoke to jarvice yesterday") == "i spoke to Jarvis yesterday"
    assert fix_commands("the air is airy today", assistant_name="Airi") == "the air is airy today"


def test_spotify_templates():
    from jarvis_app.spotify import spotify_play_intent
    for raw in ("play numb on spot if i", "clay numb on spotify", "play numb on spotty fi", "плей numb он спотифай"):
        out = normalize_transcript(raw)
        assert out == "Play numb on Spotify", (raw, out)
        assert spotify_play_intent(out) == "numb"
    assert normalize_transcript("play numb by linkin park on spot a fly") == "Play numb by linkin park on Spotify"


def test_russian_speech_untouched():
    for raw in (
        "который час",
        "открой калькулятор",
        "напиши маме в телеграм я дома",
        "Напиши Нехто в телеграм привет",
        "он написал мне в телеграм",
        "поставь таймер на пять минут",
        "райский сад",
        "включи кино группа крови",
    ):
        assert fix_commands(raw, names=NAMES) == raw, raw
        assert normalize_transcript(raw, names=NAMES) == normalize_brands(raw), raw
        assert not wants_english_pass(raw), raw
    # brand normalization unchanged
    assert normalize_transcript("открой спотт") == "открой Spotify"
    assert normalize_transcript("открой тг") == "открой Telegram"
    assert normalize_transcript("включи нам в спотифае") == normalize_brands("включи нам в спотифае")


def test_no_false_positive_commands():
    assert fix_commands("right now on telegram there is news") == "right now on telegram there is news"
    assert parse_command("light up the room") is None
    assert parse_command("play") is None
    assert parse_command("write something") is None


def test_fuzzy_names_across_scripts():
    assert name_score("next door", "Nehto") > name_score("next door", "Nikita")
    assert match_name("нехто", NAMES) == "Nehto"
    assert match_name("next door", NAMES) == "Nehto"
    assert match_name("mom", ["Мама", "Dima"]) == "Мама"
    assert match_name("zzz", NAMES) is None


def test_bilingual_gate_and_pick():
    assert wants_english_pass(HEARD_RU)
    assert english_score(HEARD_RU) >= 2
    # both readings → merged: contact from the one that snapped to a name, message from English
    assert pick_bilingual(HEARD_RU, HEARD_EN_RAW, 0.8, names=NAMES) == SAID_FIXED
    assert pick_bilingual("райт нехто он телеграм хау ар ю", "right next door on telegram how are you", 0.7,
                          names=NAMES) == "Write Nehto on Telegram, how are you"
    # English pass empty → keep the Russian one
    assert pick_bilingual(HEARD_RU, "", 0.0, names=NAMES) == HEARD_RU
    # English free speech with decent confidence beats a Cyrillic guess
    assert pick_bilingual("райт ит даун хелоу", "write it down hello", 0.8) == "write it down hello"
    assert pick_bilingual("райт ит даун хелоу", "rat it town hello", 0.2) == "райт ит даун хелоу"  # low conf


# Real Vosk small-model outputs for Piper-synthesised English commands (measured, see report).
REAL_RU_EN = [
    ("Крайне мере он тело краям лоу а мчались", "right nato on telegram hello i'm jarvis", SAID_FIXED),
    ("Поить найти он тело глэм хэллоуин чем офис", "right nato on telegram hello and jarvis", SAID_FIXED),
    ("В рай мама он тебя лугано а им", "right mom on telegram i am home", "Write Мама on Telegram, I am home"),
    ("Вайт мама он тело двойным агентом", "right mamo on telegram i am home", "Write Мама on Telegram, I am home"),
    ("Плэй нам он спад фай", "play numb on spotify", "Play numb on Spotify"),
    ("Клещей по в он спар фай", "play shape of you on spotify", "Play shape of you on Spotify"),
]


def test_real_vosk_outputs_bilingual():
    from jarvis_app.stt_commands import english_hint
    for ru, en, want in REAL_RU_EN:
        if "тебя лугано" not in ru:  # «write» → «в рай», app lost: only the low-confidence cloud path helps
            assert english_hint(ru) >= 1, ru
        assert normalize_transcript(pick_bilingual(ru, en, 0.9, names=NAMES), names=NAMES) == want, (ru, en)
    # Russian sentences heard by the Russian model never ask for the English pass
    for ru in ("Он смотрит телевизор", "Он спит не буди его", "Напиши маме в телеграф я дома", "Какая погода завтра",
               "Напиши маме в телеграм я дома", "Наташе маме в телеграмм я дома", "открой телеграмм",
               "Включи музыку в статей фэ", "он тебя любит", "открой грамматику"):
        assert english_hint(ru) == 0, ru
    # English model on Russian speech produces no template → Russian reading is kept
    assert pick_bilingual("Напиши маме в телеграм я дома", "the to show my me to the going up yet armor", 0.83) == \
        "Напиши маме в телеграм я дома"


def test_voice_names_setting():
    assert voice_names({"voice_contacts": ["Nehto", " ", "Мама"]}) == ["Nehto", "Мама"]
    assert voice_names({"voice_contacts": "Nehto, Мама"}) == ["Nehto", "Мама"]
    assert voice_names({}) == []
    from jarvis_app.config import normalize_settings
    s = normalize_settings({"voice_contacts": "Nehto, Мама;Nehto"})
    assert s["voice_contacts"] == ["Nehto", "Мама"] and s["stt_en_pass"] is True


def _fake_core(settings):
    from jarvis_app.config import normalize_settings
    from jarvis_app.core import JarvisCore
    c = JarvisCore.__new__(JarvisCore)
    c.settings = normalize_settings(settings)
    c.user_lang = "ru"
    return c


def test_core_clean_voice_text_with_english_pass(monkeypatch):
    from jarvis_app import stt_local
    calls = []
    monkeypatch.setattr(stt_local, "english_pass", lambda wav, **kw: calls.append(wav) or (HEARD_EN_RAW, 0.82))
    c = _fake_core({"speech_lang": "ru", "voice_contacts": ["Nehto"]})
    out = c._clean_voice_text(HEARD_RU, b"RIFF-fake", "ru", local=True)
    assert out == SAID_FIXED
    assert calls == [b"RIFF-fake"]
    assert c.user_lang == "en"  # he spoke English this time
    # plain Russian: no second pass, text unchanged
    calls.clear()
    c.user_lang = "ru"
    assert c._clean_voice_text("открой спотт", b"x", "ru", local=True) == "открой Spotify"
    assert calls == [] and c.user_lang == "ru"
    # setting off → only the text fixer
    c2 = _fake_core({"speech_lang": "ru", "voice_contacts": ["Nehto"], "stt_en_pass": False})
    assert c2._clean_voice_text(HEARD_RU, b"x", "ru", local=True) == SAID_FIXED
    assert calls == []


def test_core_english_rescue_low_confidence(monkeypatch):
    from jarvis_app import stt_local
    monkeypatch.setattr(stt_local, "english_pass", lambda wav, **kw: (HEARD_EN_RAW, 0.8))
    c = _fake_core({"speech_lang": "ru", "voice_contacts": ["Nehto"]})
    assert c._english_rescue(HEARD_RU, b"x") == SAID_FIXED
    assert c._english_rescue("который час", b"x") == ""
    monkeypatch.setattr(stt_local, "english_pass", lambda wav, **kw: None)  # model not downloaded yet
    assert c._english_rescue(HEARD_RU, b"x") == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
