"""Global push-to-talk hotkey via the `keyboard` package.

Hold the combo → on_down(); release → on_up(held_seconds). Auto-repeat while
held is ignored. Works without admin rights (not inside elevated windows).
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("jarvis")


def validate_hotkey(combo: str) -> bool:
    try:
        import keyboard
        keyboard.parse_hotkey(combo)
        return bool(combo.strip())
    except Exception:
        return False


class Hotkey:
    def __init__(self, combo: str, on_down, on_up) -> None:
        self.combo = combo
        self.on_down = on_down
        self.on_up = on_up
        self._handle = None
        self._active = False
        self._lock = threading.Lock()
        self.error: str | None = None

    def start(self) -> bool:
        try:
            import keyboard
            self._handle = keyboard.add_hotkey(self.combo, self._pressed, suppress=False,
                                               trigger_on_release=False)
            log.info("hotkey registered: %s", self.combo)
            return True
        except Exception as e:
            self.error = str(e)
            log.error("hotkey register failed (%s): %s", self.combo, e)
            return False

    def stop(self) -> None:
        if self._handle is not None:
            try:
                import keyboard
                keyboard.remove_hotkey(self._handle)
            except Exception:
                pass
            self._handle = None

    def _pressed(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
        t0 = time.monotonic()
        try:
            self.on_down()
        except Exception:
            log.exception("hotkey down handler")
        threading.Thread(target=self._wait_release, args=(t0,), name="hotkey-hold", daemon=True).start()

    def _wait_release(self, t0: float) -> None:
        import keyboard
        main_key = self.combo.split("+")[-1].strip()
        try:
            while keyboard.is_pressed(main_key):
                time.sleep(0.03)
                if time.monotonic() - t0 > 120:
                    break
        except Exception:
            pass
        held = time.monotonic() - t0
        with self._lock:
            self._active = False
        try:
            self.on_up(held)
        except Exception:
            log.exception("hotkey up handler")
