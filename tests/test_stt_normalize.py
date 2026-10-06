"""Unit tests for post-STT English brand-name normalization under Russian speech."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app.stt_normalize import (
    normalize_transcript,
    canonical_app_name,
    brand_alias_targets,
    ALIAS_TO_SEARCH,
)
from jarvis_app.actions import APP_ALIASES, score_name


def test_spotify_mangled_phrases():
    for raw in (
        "открой спотт",
        "Открой споти",
        "открой спотифай",
        "опро спотт",
        "запусти спотти",
    ):
        out = normalize_transcript(raw)
        assert "Spotify" in out, (raw, out)
        assert "спотт" not in out.lower() or "Spotify" in out


def test_common_brands():
    cases = {
        "открой телеграм": "Telegram",
        "открой тг": "Telegram",
        "открой дискорд": "Discord",
        "открой стим": "Steam",
        "открой хром": "Google Chrome",
        "открой гугл хром": "Google Chrome",
        "открой оперы": "Opera",
        "открой ваштсап": "WhatsApp",
        "открой ватсап": "WhatsApp",
        "открой эдж": "Microsoft Edge",
        "открой фаерфокс": "Firefox",
        "открой вскод": "Visual Studio Code",
        "открой обс": "OBS Studio",
        "открой ворд": "Word",
    }
    for raw, brand in cases.items():
        out = normalize_transcript(raw)
        assert brand in out, (raw, out, brand)


def test_russian_only_unchanged():
    for raw in (
        "который час",
        "поставь таймер на пять минут",
        "что на экране",
        "открой калькулятор",
        "найди файл физика",
    ):
        assert normalize_transcript(raw) == raw


def test_idempotent_and_latin():
    assert normalize_transcript("открой Spotify") == "открой Spotify"
    assert "Spotify" in normalize_transcript("открой Spotify")
    again = normalize_transcript(normalize_transcript("открой спотт"))
    assert again == normalize_transcript("открой спотт")


def test_canonical_app_name():
    assert canonical_app_name("спотт") == "Spotify"
    assert canonical_app_name("спотифай") == "Spotify"
    assert canonical_app_name("Telegram") == "Telegram"
    assert canonical_app_name("неизвестное") == "неизвестное"


def test_aliases_merged_into_app_aliases():
    targets = brand_alias_targets()
    assert targets["спотт"] == "spotify"
    assert APP_ALIASES.get("спотт") == "spotify"
    assert APP_ALIASES.get("ваштсап") == "whatsapp"
    # existing system aliases still win / remain
    assert APP_ALIASES["калькулятор"] == "calc.exe"


def test_open_app_target_via_alias_and_score():
    # Same path «открой Spotify» would take after normalize → search "spotify"
    q = ALIAS_TO_SEARCH["спотт"]
    assert q == "spotify"
    assert score_name(q, "Spotify") >= 0.88
    assert score_name("telegram", "Telegram Desktop") >= 0.88
    # resolve_app without a full Windows app index: alias / exe fallback
    from jarvis_app.actions import Actions, AppIndex
    a = Actions.__new__(Actions)
    a.core = None
    a.apps = AppIndex()
    # empty index → path fallback to spotify.exe for Latin query after canonicalize
    r = a.resolve_app("спотт")
    assert r is not None
    kind, disp, target = r
    assert kind in ("path", "exe", "lnk", "uwp")
    assert "spotify" in target.lower() or "spotify" in disp.lower() or disp == "Spotify"


def test_word_boundaries_no_false_positive():
    # «опрос» must not become «откройс»; bare Russian without brand stays
    assert normalize_transcript("опрос населения") == "опрос населения"
    assert "Telegram" not in normalize_transcript("стратегия")


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} tests passed")
