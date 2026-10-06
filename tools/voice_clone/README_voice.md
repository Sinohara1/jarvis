# Свой голос для Jarvis (Piper `.onnx`)

Честный пайплайн:

1. **Запись** (~45–60 минут чистой речи) → папка `wavs/` + `metadata.csv`
2. **Обучение** модели Piper **на другой машине / в Docker с GPU** (или через сервис) → `name.onnx` + `name.onnx.json`
3. **Импорт в Jarvis** → «Голос» → «загрузить свой голос»

> **Запись сама по себе не даёт `.onnx`.** Скрипт `record_voice` только собирает датасет в формате LJSpeech для дальнейшего обучения.

Jarvis кладёт пользовательские голоса сюда:

`%LOCALAPPDATA%\Jarvis\voices\<имя>\`

Нужны оба файла: `имя.onnx` и `имя.onnx.json` (или один `.zip` с ними).

---

## 1. Запись (Windows PowerShell)

```powershell
cd путь\к\jarvis_v15test\tools\voice_clone
.\record_voice.ps1
```

По умолчанию датасет пишется в:

- `%USERPROFILE%\Desktop\JarvisVoiceRecord`, или
- `%LOCALAPPDATA%\Jarvis\voice_record` (если Desktop недоступен)

Параметры:

```powershell
.\record_voice.ps1 -OutDir "$env:USERPROFILE\Desktop\JarvisVoiceRecord"
.\record_voice.ps1 -Start 120          # продолжить / перезаписать с фразы №120
.\record_voice.ps1 -Backend ffmpeg     # если нет sounddevice
```

Или напрямую Python:

```powershell
pip install -r requirements_record.txt
python record_voice.py
```

### Что получается

```
JarvisVoiceRecord/
  wavs/
    00001.wav
    00002.wav
    ...
  metadata.csv          # строки: id|текст  (LJSpeech)
  prompts_ru.txt        # копия списка фраз
```

Формат WAV: **16-bit PCM, mono, 22050 Hz** (обычный для Piper medium).

В сессии:

| Клавиша | Действие        |
|---------|-----------------|
| Enter   | запись / стоп   |
| `r`     | перезаписать    |
| `s`     | пропустить      |
| `b`     | назад           |
| `q`     | сохранить выход |

Прогресс: `N/total` и оценка оставшихся минут. Сессию можно прервать и продолжить позже — скрипт пропускает уже записанные файлы.

В комплекте `prompts_ru.txt` — **~520 коротких разных русских фраз** (хватает примерно на **45–60 минут**, если читать ровно, без спешки).

### Как записывать хорошо

- Тихая комната, без музыки, ТВ и гула вентилятора
- Одинаковое расстояние до микрофона
- Естественный тон (не шёпот и не сцена)
- Если ошибся — `r` и перезаписать фразу
- Лучше меньше, но чище, чем час с шумами и клиппингом

---

## 2. Обучение → `.onnx` (не на этом скрипте)

Нужен **GPU** и окружение **piper / piper-train**. Кратко — см. `train_hint.md`.

Официально:

- [TRAINING.md (rhasspy/piper)](https://github.com/rhasspy/piper/blob/master/TRAINING.md)
- Чекпоинты для fine-tune: [rhasspy/piper-checkpoints](https://huggingface.co/datasets/rhasspy/piper-checkpoints)

Типичный путь:

1. `piper_train.preprocess` (формат `ljspeech`, `--language ru`, `--sample-rate 22050`)
2. `piper_train` с fine-tune от checkpoint (лучше medium, желательно русскоязычный, если есть)
3. `piper_train.export_onnx` → скопировать `config.json` как `name.onnx.json`

Альтернатива: сторонний сервис / студия клонирования голоса, которая отдаёт **совместимый Piper `.onnx` + `.onnx.json`**. Jarvis проверяет `phoneme_id_map` и `audio.sample_rate` и делает тестовый синтез при импорте.

---

## 3. Упаковка и импорт в Jarvis

Когда уже есть обученные файлы:

```powershell
.\pack_for_jarvis.ps1 -Onnx "C:\path\myvoice.onnx"
# или
.\pack_for_jarvis.ps1 -Dir "C:\path\to\folder"
```

Получите `myvoice_jarvis.zip` → в Jarvis: **Голос → загрузить свой голос**.

Можно указать отдельно `.onnx` и `.onnx.json` без zip — Jarvis тоже это умеет.

---

## Частые вопросы

**Почему нельзя «просто нажать кнопку и получить голос» в этой папке?**  
Обучение VITS/Piper — тяжёлая GPU-задача (часы, гигабайты, зависимости). Коробка/скрипт записи намеренно только готовят данные.

**Сколько минут нужно?**  
Практика: **~45–60 мин** чистой речи для fine-tune. Меньше — часто «похоже, но плывёт»; больше — лучше стабильность, если качество записи держится.

**Какой sample rate?**  
Этот пакет пишет **22050 Hz** под типичный Piper medium. Если учите low/high под другой rate — меняйте и запись, и preprocess одинаково.

**Русский язык?**  
Список фраз — русский. В preprocess укажите язык espeak, например `--language ru`.
