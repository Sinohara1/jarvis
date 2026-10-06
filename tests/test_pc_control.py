"""Unit tests: pc_control defaults, tool gating, media/elevated/full desktop (no Windows required)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app.config import normalize_settings, DEFAULT_SETTINGS
from jarvis_app.actions import TOOLS, tools_for_settings
from jarvis_app.pc_power import (
    pc_control_level, tool_allowed, resolve_media_action, resolve_volume_action,
    elevated_path_allowed, TOOL_MIN_LEVEL, SPOTIFY_ALIASES,
    parse_hotkey, command_risk, WINDOW_ACTIONS,
)
from jarvis_app.brain import build_system_prompt, local_model_hint
from jarvis_app import persona

FULL_ONLY = (
    "send_telegram", "run_elevated", "window_control", "type_text", "press_hotkey",
    "run_command", "get_active_window", "lock_workstation", "system_power",
)


def test_pc_control_default():
    assert DEFAULT_SETTINGS.get("pc_control") == "standard"
    s = normalize_settings({})
    assert s["pc_control"] == "standard"
    assert normalize_settings({"pc_control": "SAFE"})["pc_control"] == "safe"
    assert normalize_settings({"pc_control": "nope"})["pc_control"] == "standard"
    assert normalize_settings({"pc_control": "full"})["pc_control"] == "full"


def test_tool_presence():
    names = {t["name"] for t in TOOLS}
    for n in ("open_app", "media_control", "adjust_volume", "send_telegram", "run_elevated",
              "window_control", "type_text", "press_hotkey", "run_command",
              "get_active_window", "lock_workstation", "system_power"):
        assert n in names, n
    assert TOOL_MIN_LEVEL["media_control"] == "standard"
    for n in FULL_ONLY:
        assert TOOL_MIN_LEVEL[n] == "full", n


def test_tools_for_settings_levels():
    safe = {t["name"] for t in tools_for_settings({"pc_control": "safe"})}
    std = {t["name"] for t in tools_for_settings({"pc_control": "standard"})}
    full = {t["name"] for t in tools_for_settings({"pc_control": "full"})}
    assert "open_app" in safe and "adjust_volume" in safe
    assert "media_control" not in safe
    for n in FULL_ONLY:
        assert n not in safe, n
        assert n not in std, n
        assert n in full, n
    assert "media_control" in std
    assert "media_control" in full
    assert tool_allowed("media_control", "standard")
    assert not tool_allowed("send_telegram", "standard")
    assert not tool_allowed("window_control", "standard")
    assert tool_allowed("send_telegram", "full")
    assert tool_allowed("window_control", "full")
    assert tool_allowed("type_text", "full")
    assert tool_allowed("run_command", "full")


def test_media_action_resolution():
    assert resolve_media_action("play") == "play"
    assert resolve_media_action("PLAY") == "play"
    assert resolve_media_action("пауза") == "pause"
    assert resolve_media_action("следующий") == "next"
    assert resolve_media_action("предыдущий") == "previous"
    assert resolve_media_action("garbage") is None
    assert resolve_volume_action("громче") == "up"
    assert resolve_volume_action("mute") == "mute"
    assert "spotify" in SPOTIFY_ALIASES and "спотик" in SPOTIFY_ALIASES


def test_elevated_path_guards():
    ok, err = elevated_path_allowed("")
    assert not ok
    ok, err = elevated_path_allowed("/tmp/nope.exe")
    assert not ok
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "x.exe")
        open(bad, "w").close()
        ok, info = elevated_path_allowed(bad)
        assert not ok  # wrong ext and likely outside home
        ps1 = os.path.join(os.path.expanduser("~"), "_jarvis_test_elevated_tmp.ps1")
        try:
            with open(ps1, "w", encoding="utf-8") as f:
                f.write("# test\n")
            ok, info = elevated_path_allowed(ps1)
            assert ok, info
            assert info.endswith(".ps1")
        finally:
            try:
                os.remove(ps1)
            except OSError:
                pass


def test_hotkey_and_command_risk():
    assert parse_hotkey("ctrl+c") is not None
    assert parse_hotkey("alt+f4") is not None
    assert parse_hotkey("win+d") is not None
    assert parse_hotkey("ctrl+shift+esc") is not None
    assert parse_hotkey("ctrl+alt+del") is None
    assert parse_hotkey("") is None
    assert parse_hotkey("notakey") is None
    assert "close" in WINDOW_ACTIONS and "minimize" in WINDOW_ACTIONS
    assert command_risk("echo hello") == "ok"
    assert command_risk("dir C:\\") == "ok"
    assert command_risk("del /s C:\\temp\\*") == "refuse"
    assert command_risk("format C:") == "refuse"
    assert command_risk("diskpart") == "refuse"
    assert command_risk("Remove-Item -Recurse C:\\foo") == "refuse"
    assert command_risk("taskkill /im notepad.exe") == "confirm"
    assert command_risk("shutdown /s") == "confirm"
    assert command_risk("reg delete HKCU\\Software\\Foo") == "confirm"


def test_companion_preset_and_prompt():
    assert "companion" in persona.PRESETS
    assert "инструмент" in persona.PRESETS["companion"]["prompt"].lower() or "полный" in persona.PRESETS["companion"]["prompt"].lower()
    s = normalize_settings({"pc_control": "standard", "assistant_name": "Джарвис"})
    p = build_system_prompt(s)
    assert "компаньон" in p.lower() or "вместе" in p.lower()
    assert "media_control" in p
    assert "полный" in p.lower()
    s_full = normalize_settings({"pc_control": "full"})
    pf = build_system_prompt(s_full)
    assert "window_control" in pf
    assert "type_text" in pf
    assert "run_command" in pf
    assert "send_telegram" in pf or "Telegram" in pf
    assert "UAC" in pf or "uac" in pf.lower()
    assert "массово" in pf.lower() or "массового" in pf.lower() or "удаления" in pf.lower()
    h = local_model_hint(normalize_settings({"pc_control": "standard"}), "ru")
    assert "media_control" in h
    assert "window_control" not in h
    h2 = local_model_hint(normalize_settings({"pc_control": "safe"}), "ru")
    assert "media_control" not in h2
    h3 = local_model_hint(normalize_settings({"pc_control": "full"}), "ru")
    assert "window_control" in h3
    assert "type_text" in h3
    assert "send_telegram" in h3


def test_pc_control_level_helper():
    assert pc_control_level({}) == "standard"
    assert pc_control_level({"pc_control": "full"}) == "full"


def test_full_tools_off_windows_graceful():
    """On non-Windows, full helpers return ok=False without crashing."""
    from jarvis_app import pc_power as pp
    if pp.IS_WIN:
        return  # skip on real Windows CI if any
    r = pp.window_action("minimize", "x")
    assert r.get("ok") is False
    r = pp.type_text_into("hi")
    assert r.get("ok") is False
    r = pp.press_hotkey("ctrl+c")
    assert r.get("ok") is False
    r = pp.run_shell_command("echo hi")
    assert r.get("ok") is False
    r = pp.lock_workstation()
    assert r.get("ok") is False
    r = pp.system_power_action("sleep")
    assert r.get("ok") is False
    # refuse path works without Windows
    r = pp.run_shell_command("format C:")
    assert r.get("ok") is False and "отказ" in (r.get("error") or "").lower() or "запрещ" in (r.get("error") or "").lower()
    r = pp.run_shell_command("taskkill /im x.exe")
    assert r.get("needs_confirm") is True


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK", name)
            except Exception as e:
                failed += 1
                print("FAIL", name, type(e).__name__, e)
    if failed:
        raise SystemExit(failed)
    print("all ok")
