"""Silent checks: synth mp3, open it with MCI (same commands as Speaker) at volume 0, no audible output; load wake-word model."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import audio, wakeword
import numpy as np
p = os.path.join(tempfile.gettempdir(), "jarvis_mci_test.mp3")
audio.synth_to_file("Тест.", "ru-RU-DmitryNeural", 0, p)
m = audio._MCI()
m.cmd(f'open "{p}" type mpegvideo alias jtest')
print("mci length ms:", m.cmd("status jtest length"))
m.cmd("setaudio jtest volume to 0")
m.cmd("close jtest"); os.remove(p)
w = wakeword.WakeWordModel()
s = max(w.process(np.zeros(1280, dtype=np.int16)) for _ in range(30))
print("wakeword model ok, silence score", round(float(s), 4))
