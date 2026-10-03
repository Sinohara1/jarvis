import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import actions
a = actions.Actions.__new__(actions.Actions); a.core = None; a.apps = actions.AppIndex(); a.apps.build()
for q in ("калькулятор", "телеграм", "хром", "блокнот", "дискорд", "стим", "вскод", "браузер", "ютуб", "майнкрафт", "обсидиан", "фотошоп", "эдж", "яндекс", "спотифай", "Visual Studio Code", "opera gx"):
    print(q, "->", a.resolve_app(q))
names = list(a.apps.shortcuts) + list(a.apps.uwp)
print([n for n in names if any(k in n.lower() for k in ("chrome", "code", "edge", "yandex", "opera", "firefox", "brave", "editor", "minecraft", "browser"))])
