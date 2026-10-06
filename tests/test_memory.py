"""Unit tests: configurable companion memory (jarvis_app/memory.py + brain/actions/config wiring).

No network, no Ollama: a capturing fake provider sees the system prompt and tool list.
Run: python tests/test_memory.py   (or python -m pytest -q tests/test_memory.py)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_app import memory as M
from jarvis_app import providers as P
from jarvis_app.actions import TOOLS, Actions
from jarvis_app.brain import Brain, needs_tools
from jarvis_app.config import DEFAULT_SETTINGS, normalize_settings


WORDS = ["кофе", "чай", "пиццу", "суши", "футбол", "шахматы", "джаз", "рок", "горы", "море", "кошек", "собак",
         "зиму", "лето", "книги", "аниме", "сериалы", "велосипед", "бег", "плавание"]


def _store(settings: dict | None = None, path: str | None = None) -> M.MemoryStore:
    s = normalize_settings(settings or {})
    path = path or os.path.join(tempfile.mkdtemp(prefix="jmem_"), "memory.json")
    return M.MemoryStore(path=path, get_settings=lambda: s)


# ─── parsing / guards ────────────────────────────────────────────────────────

def test_explicit_fact_positive():
    cases = {
        "Запомни, что я люблю кофе без сахара": "Я люблю кофе без сахара",
        "Джарвис, запомни: мою сестру зовут Аня": "Мою сестру зовут Аня",
        "запомни пожалуйста что у меня кот Барсик.": "У меня кот Барсик",
        "Пятница, запомни, что дедлайн проекта 20 октября": "Дедлайн проекта 20 октября",
        "запиши в память что я учусь в 10 классе": "Я учусь в 10 классе",
        "remember that I prefer dark mode": "I prefer dark mode",
    }
    for src, want in cases.items():
        assert M.explicit_fact(src) == want, (src, M.explicit_fact(src))


def test_explicit_fact_negative():
    for src in ("запомни это", "ты запомнил?", "Помоги запомнить формулу", "напомни через 5 минут выпить воды",
                "как мне запомнить таблицу умножения", "открой телеграм", "", "запомни"):
        assert M.explicit_fact(src) == "", src


def test_looks_secret():
    for t in ("мой пароль от почты qwerty123", "API key sk-abcdefghijklmnop", "карта 4111 1111 1111 1111",
              "IBAN DE89 3704 0044 0532 0130 00", "токен бота 123456789:AAEhBOweik9ai2o2J1PsGx8uVB-nGTOkVrU",
              "пин-код 1234", "AIzaSyA1234567890abcdefghijklmnopqrs"):
        assert M.looks_secret(t), t
    for t in ("я люблю кофе", "Сестру зовут Аня, ей 15 лет", "Учусь в 10 классе", "телефон мамы +49 151 23456789",
              "Дедлайн проекта 20 октября 2026"):
        assert not M.looks_secret(t), t


def test_guess_category_and_norm():
    assert M.guess_category("Я люблю кофе без сахара") == "preference"
    assert M.guess_category("Мою сестру зовут Аня") == "people"
    assert M.guess_category("Делаю проект сайта для школы") == "project"
    assert M.guess_category("Я живу в Берлине") == "user"
    assert M.norm_category("Люди") == "people" and M.norm_category("???") == "other"


# ─── store ───────────────────────────────────────────────────────────────────

def test_dedupe_updates_restated_fact_but_keeps_distinct_ones():
    st = _store()
    st.add("Мне 15 лет")
    assert st.add("Мне 16 лет")["updated"] and [f["text"] for f in st.all()] == ["Мне 16 лет"]
    st.add("Сестру зовут Аня")
    assert st.add("Сестру зовут Аня Петрова")["updated"]
    st.add("Работаю над проектом сайта")
    assert not st.add("Работаю над проектом бота").get("updated")
    assert st.count() == 4


def test_add_persist_reload_and_dedupe():
    st = _store()
    r = st.add("Я люблю кофе без сахара", source="explicit")
    assert r["ok"] and r["category"] == "preference"
    assert st.add("я люблю кофе без сахара.")["updated"]  # same fact → update, not a duplicate
    assert st.count() == 1
    with open(st.path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 1 and data["facts"][0]["text"] == "Я люблю кофе без сахара"
    again = M.MemoryStore(path=st.path, get_settings=lambda: {})
    assert [f["text"] for f in again.all()] == ["Я люблю кофе без сахара"]


def test_secret_is_refused_and_not_saved():
    st = _store()
    r = st.add("мой пароль от стима hunter22")
    assert not r["ok"] and "секрет" in r["error"]
    assert st.count() == 0 and not os.path.exists(st.path)


def test_max_facts_evicts_oldest_unpinned():
    st = _store({"memory_max_facts": 10})
    st.add("Самый первый важный факт", pinned=True)
    for i, w in enumerate(WORDS[:12]):
        st.add(f"Люблю {w}")
        time.sleep(0.001)
    facts = st.all()
    assert len(facts) == 10
    assert any(f["text"] == "Самый первый важный факт" for f in facts)  # pinned survives
    assert not any(f["text"] in ("Люблю " + WORDS[0], "Люблю " + WORDS[1]) for f in facts)  # oldest went first


def test_enforce_limit_after_setting_change():
    s = normalize_settings({"memory_max_facts": 50})
    st = M.MemoryStore(path=os.path.join(tempfile.mkdtemp(), "m.json"), get_settings=lambda: s)
    for w in WORDS[:15]:
        st.add(f"Люблю {w}")
    s["memory_max_facts"] = 10
    assert st.enforce_limit() == 5 and st.count() == 10


def test_update_delete_forget_clear():
    st = _store()
    a = st.add("Сестру зовут Аня")["id"]
    st.add("Я учусь в 10 классе")
    st.add("Люблю пиццу с ананасами")
    assert st.update(a, pinned=True)["ok"] and st.all()[0]["pinned"]
    assert not st.update(a, text="пароль 12345")["ok"]
    r = st.forget("ананасы пицца")
    assert r["ok"] and "пицц" in r["forgotten"].lower()
    assert not st.forget("марсоход")["ok"]
    assert st.delete(a)["ok"] and not st.delete(a)["ok"]
    assert st.clear()["removed"] == 1 and st.count() == 0


def test_forget_ambiguous_asks_to_clarify():
    st = _store()
    st.add("Проект сайта на React", "project")
    st.add("Проект бота на Python", "project")
    r = st.forget("проект")
    assert not r["ok"] and len(r["candidates"]) == 2 and st.count() == 2


def test_relevance_and_prompt_block():
    st = _store({"memory_inject": 2})
    st.add("Сестру зовут Аня", "people")
    st.add("Готовлюсь к ЕГЭ по физике", "project")
    st.add("Люблю кофе без сахара", "preference")
    top = st.relevant("что подарить сестре на день рождения?")
    assert top[0]["text"] == "Сестру зовут Аня" and len(top) == 2
    block = st.prompt_block("помоги с физикой")
    assert "Готовлюсь к ЕГЭ по физике" in block and block.count("\n- ") == 2
    assert "долгая память" in block


def test_prompt_block_respects_switches_and_budget():
    assert _store({"memory_enabled": False}).prompt_block("x") == ""
    st0 = _store({"memory_inject": 0})
    st0.add("Люблю кофе без сахара")
    assert st0.prompt_block("кофе") == ""
    st = _store({"memory_inject": 30, "memory_max_facts": 100})
    for w in WORDS:
        st.add(f"Очень длинный и подробный факт про {w}, " + "с массой деталей и пояснений " * 4)
    block = st.prompt_block("")
    assert 0 < block.count("\n- ") < 20 and len(block) < M.PROMPT_BUDGET + 400


def test_corrupted_file_starts_empty():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "memory.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    st = M.MemoryStore(path=p, get_settings=lambda: {})
    assert st.count() == 0
    assert st.add("Люблю чай")["ok"] and st.count() == 1
    with open(p + ".bad", encoding="utf-8") as f:  # the broken file is kept, not overwritten
        assert f.read() == "{not json"


def test_manual_edit_of_file_is_picked_up():
    st = _store()
    st.add("Люблю чай")
    with open(st.path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "facts": [{"text": "Люблю кофе", "category": "preference"}]}, f, ensure_ascii=False)
    os.utime(st.path, (time.time() + 5, time.time() + 5))
    assert [f["text"] for f in st.all()] == ["Люблю кофе"]


def test_on_change_and_payload():
    seen = []
    st = M.MemoryStore(path=os.path.join(tempfile.mkdtemp(), "m.json"), get_settings=lambda: {},
                       on_change=lambda what, n: seen.append((what, n)))
    st.add("Люблю чай")
    st.clear()
    assert seen == [("add", 1), ("clear", 0)]
    p = st.payload()
    assert p["ok"] and {c["key"] for c in p["categories"]} == set(M.CATEGORIES)


def test_parse_extracted():
    raw = 'Вот: {"facts": [{"text": "Учится в 10 классе", "category": "user"}, {"text": "пароль 1234"}, "Любит кофе"]}'
    out = M.parse_extracted(raw)
    assert [f["text"] for f in out] == ["Учится в 10 классе", "Любит кофе"]
    assert M.parse_extracted("ничего") == [] and M.parse_extracted('{"facts": []}') == []


# ─── settings ────────────────────────────────────────────────────────────────

def test_settings_defaults_and_clamps():
    s = normalize_settings({})
    assert s["memory_enabled"] is True and s["memory_explicit"] is True and s["memory_auto_extract"] is False
    assert s["memory_max_facts"] == 100 and s["memory_inject"] == 8
    s2 = normalize_settings({"memory_max_facts": 99999, "memory_inject": -3, "memory_enabled": 0})
    assert s2["memory_max_facts"] == 500 and s2["memory_inject"] == 0 and s2["memory_enabled"] is False
    assert "memory_enabled" in DEFAULT_SETTINGS


# ─── brain / actions wiring ──────────────────────────────────────────────────

class CapturingProv(P.Provider):
    name = "gemini"

    def __init__(self, calls=None):
        self.systems: list[str] = []
        self.tools: list = []
        self.calls = list(calls or [])

    def user_message(self, text, image_jpeg=None):
        return {"role": "user", "content": text}

    def assistant_message(self, text):
        return {"role": "assistant", "content": text}

    def tool_result_messages(self, calls, results):
        return [{"role": "tool", "content": json.dumps(r, ensure_ascii=False)} for r in results]

    def chat_stream(self, model, system, messages, tools=None, *, on_text, timeout=40.0, temperature=0.7,
                    max_tokens=2048):
        return self.chat(model, system, messages, tools)

    def chat(self, model, system, messages, tools=None, *, timeout=40.0, temperature=0.7, max_tokens=2048):
        self.systems.append(system or "")
        self.tools.append(tools)
        if self.calls:
            c = self.calls.pop(0)
            return P.LLMResult(text="", tool_calls=[c], raw_message={"role": "assistant", "content": ""})
        return P.LLMResult(text="Запомнил.", tool_calls=[], raw_message={"role": "assistant", "content": "Запомнил."})


class FakeCore:
    def __init__(self, settings, memory):
        self.settings = settings
        self.memory = memory
        self.addons = None


def _brain(settings: dict, calls=None):
    s = normalize_settings(dict({"provider": "gemini", "ai_route": "cloud"}, **settings))
    mem = M.MemoryStore(path=os.path.join(tempfile.mkdtemp(), "memory.json"), get_settings=lambda: s)
    core = FakeCore(s, mem)
    acts = Actions.__new__(Actions)  # no app-index thread
    acts.core = core
    b = Brain(lambda: s, acts, lambda: "")
    b.memory = mem
    cap = CapturingProv(calls)
    b.provider = lambda name=None: cap
    b.models = lambda name=None: ("m", "m")
    return b, cap, mem, s


def test_brain_injects_memory_and_declares_tools():
    b, cap, mem, s = _brain({})
    mem.add("Сестру зовут Аня", "people")
    b.ask("что подарить сестре?")
    assert "Сестру зовут Аня" in cap.systems[-1]
    names = {t["name"] for t in cap.tools[-1]}
    assert {"remember_fact", "forget_fact", "recall_memory"} <= names
    assert "remember_fact" not in {t["name"] for t in TOOLS}  # static list untouched


def test_brain_memory_off_means_no_block_and_no_tools():
    b, cap, mem, s = _brain({"memory_enabled": False})
    mem.add("Сестру зовут Аня", "people")
    b.ask("что подарить сестре?")
    assert "Сестру зовут Аня" not in cap.systems[-1]
    assert not {"remember_fact", "recall_memory"} & {t["name"] for t in cap.tools[-1]}
    assert b.actions.execute("remember_fact", {"text": "Люблю чай"})["ok"] is False
    assert mem.count() == 1


def test_explicit_remember_saved_without_model_and_noted():
    b, cap, mem, s = _brain({})
    b.ask("Запомни, что я люблю кофе без сахара")
    assert [f["text"] for f in mem.all()] == ["Я люблю кофе без сахара"]
    assert "Только что по просьбе пользователя сохранено" in cap.systems[-1]
    assert b._mem_note == ""  # note lives for one turn only
    b.ask("а который час?")
    assert "Только что по просьбе" not in cap.systems[-1]


def test_explicit_switch_off_leaves_it_to_the_model():
    b, cap, mem, s = _brain({"memory_explicit": False})
    b.ask("Запомни, что я люблю кофе без сахара")
    assert mem.count() == 0


def test_explicit_secret_is_refused_and_model_told():
    b, cap, mem, s = _brain({})
    b.ask("запомни, что мой пароль от почты qwerty12345")
    assert mem.count() == 0 and "не сохранено" in cap.systems[-1]


def test_model_tool_calls_remember_and_recall():
    calls = [P.ToolCall(id="1", name="remember_fact", args={"text": "Учится в 10 классе", "category": "user"}),
             P.ToolCall(id="2", name="recall_memory", args={"query": "класс"})]
    b, cap, mem, s = _brain({}, calls=calls)
    b.ask("я учусь в десятом классе, кстати")
    assert [f["text"] for f in mem.all()] == ["Учится в 10 классе"]
    assert mem.all()[0]["source"] == "model"


def test_memory_tool_logging_has_no_fact_text():
    rec: list[str] = []

    class H(logging.Handler):
        def emit(self, r):
            rec.append(r.getMessage())

    lg = logging.getLogger("jarvis")
    h = H()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        b, cap, mem, s = _brain({})
        b.actions.execute("remember_fact", {"text": "Секретный проект Феникс в гараже"})
        b.actions.execute("recall_memory", {"query": "Феникс"})
        b.actions.execute("forget_fact", {"query": "Феникс гараж"})
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    joined = "\n".join(rec)
    assert "remember_fact" in joined and "Феникс" not in joined


def test_needs_tools_routes_memory_phrases():
    assert needs_tools("запомни что я люблю кофе")
    assert needs_tools("забудь про мою сестру пожалуйста")


def test_auto_extract_worker_saves_facts():
    b, cap, mem, s = _brain({"memory_auto_extract": True})
    b.complete = lambda system, messages, temperature=0.9: '{"facts": [{"text": "Готовится к ЕГЭ по физике", "category": "project"}]}'
    b._extracting = True
    b._extract_worker(["я готовлюсь к егэ по физике"], delay=0)
    assert [f["text"] for f in mem.all()] == ["Готовится к ЕГЭ по физике"]
    assert mem.all()[0]["source"] == "auto" and b._extracting is False


def test_auto_extract_triggers_every_n_turns_only_when_enabled():
    started = []
    for enabled in (False, True):
        b, cap, mem, s = _brain({"memory_auto_extract": enabled})
        b._extract_worker = lambda lines, delay=12.0, _st=started: _st.append(lines)
        for i in range(M.EXTRACT_EVERY):
            b.ask(f"обычная реплика номер {i}")
        time.sleep(0.05)
    assert len(started) == 1 and started[0][-1].startswith("обычная реплика")


def test_brain_without_memory_store_is_unchanged():
    s = normalize_settings({"provider": "gemini", "ai_route": "cloud"})
    b = Brain(lambda: s, None, lambda: "")
    cap = CapturingProv()
    b.provider = lambda name=None: cap
    b.models = lambda name=None: ("m", "m")
    b.ask("Запомни, что я люблю кофе")
    assert "долгая память" not in cap.systems[-1]
    assert not {"remember_fact"} & {t["name"] for t in cap.tools[-1]}


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
