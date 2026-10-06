"""Portable mode (v1.5.2): portable.txt next to Jarvis.exe -> data in <exe folder>\\data.
Run: python -m pytest -q tests/test_portable.py (or python tests/test_portable.py)"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import config
from jarvis_app.config import data_dir, portable_data_dir, _exe_dir, PORTABLE_MARKER

WIN_ENV = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}


def test_no_marker_uses_localappdata():
    with tempfile.TemporaryDirectory() as d:
        assert portable_data_dir(d) is None
        got = data_dir(platform="win32", environ=WIN_ENV, home=r"C:\Users\u", exe_dir=d)
        assert got == os.path.join(WIN_ENV["LOCALAPPDATA"], "Jarvis")


def test_marker_switches_to_exe_data():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, PORTABLE_MARKER), "w", encoding="utf-8").write("portable")
        want = os.path.join(d, "data")
        assert portable_data_dir(d) == want
        assert data_dir(platform="win32", environ=WIN_ENV, home=r"C:\Users\u", exe_dir=d) == want
        assert data_dir(platform="linux", environ={}, home="/home/u", exe_dir=d) == want
        # data folder is not created just by resolving the path
        assert not os.path.exists(want)
        # frozen=False (running from source) ignores the marker
        assert data_dir(platform="win32", environ=WIN_ENV, home=r"C:\Users\u", exe_dir=d,
                        frozen=False) == os.path.join(WIN_ENV["LOCALAPPDATA"], "Jarvis")


def test_data_folder_alone_is_not_a_marker():
    """Deleting portable.txt must switch back to %LOCALAPPDATA% even if a data folder is left."""
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "data"))
        assert portable_data_dir(d) is None
        # a directory named portable.txt is not the marker either
        os.makedirs(os.path.join(d, PORTABLE_MARKER))
        assert portable_data_dir(d) is None


def test_exe_dir_detection():
    assert _exe_dir(frozen=False) is None
    exe = os.path.join(tempfile.gettempdir(), "Jarvis_clean", "Jarvis.exe")
    assert _exe_dir(frozen=True, executable=exe) == os.path.dirname(os.path.abspath(exe))


def test_source_run_is_not_portable():
    # tests run from source (not frozen): module-level paths come from the normal per-user folder
    assert config.PORTABLE is False
    assert config.SETTINGS_PATH == os.path.join(config.DATA_DIR, "settings.json")


def test_all_paths_under_data_dir():
    from jarvis_app import tts_local, namewake, remote, webui, stt_whisper
    root = os.path.abspath(config.DATA_DIR)
    for p in (config.SETTINGS_PATH, config.STATS_PATH, config.DISTRACT_PATH, config.LOG_PATH,
              config.MEMORY_PATH, config.TMP_DIR, tts_local.PIPER_DIR, tts_local.CUSTOM_DIR,
              namewake.USER_MODELS_DIR, remote.PHONE_FILE, webui.CHAT_PATH,
              stt_whisper.model_dir("small"), stt_whisper.cuda_dir()):
        assert os.path.abspath(p).startswith(root + os.sep), p


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print("ok", k)
