# train_hint — высокий уровень (обучение Piper не здесь)

Этот файл **не обучает** модель на машине Jarvis. Только ориентиры, чтобы не потеряться после записи.

Официально: https://github.com/rhasspy/piper/blob/master/TRAINING.md  
Чекпоинты: https://huggingface.co/datasets/rhasspy/piper-checkpoints

## Что у вас уже есть после `record_voice`

```
JarvisVoiceRecord/
  wavs/*.wav          # 16-bit mono 22050 Hz
  metadata.csv        # id|текст
```

Это формат **LJSpeech**, который ждёт `piper_train.preprocess`.

## Типичные шаги (Linux + NVIDIA GPU / Docker)

1. Поставить окружение по TRAINING.md (часто Python 3.10, espeak-ng, pytorch, piper-train). Docker с GPU предпочтительнее «голого» Windows.
2. Preprocess (пример):

```bash
python3 -m piper_train.preprocess \
  --language ru \
  --input-dir /data/JarvisVoiceRecord \
  --output-dir /data/training \
  --dataset-format ljspeech \
  --single-speaker \
  --sample-rate 22050
```

3. Fine-tune от checkpoint той же «качества»/sample rate (medium ≈ 22050), например:

```bash
python3 -m piper_train \
  --dataset-dir /data/training \
  --accelerator gpu \
  --devices 1 \
  --batch-size 32 \
  --max_epochs 10000 \
  --resume_from_checkpoint /path/to/something.ckpt \
  --checkpoint-epochs 1 \
  --precision 32
```

Подберите batch-size под VRAM. Для русского предпочтителен русскоязычный checkpoint, если найдёте совместимый; иначе fine-tune с другого языка того же quality тоже практикуют (см. обсуждения/дока Piper).

4. Экспорт:

```bash
python3 -m piper_train.export_onnx /path/to/epochXXXX.ckpt /path/to/name.onnx
cp /data/training/config.json /path/to/name.onnx.json
```

5. Упаковка для Jarvis:

```powershell
.\pack_for_jarvis.ps1 -Onnx C:\path\name.onnx -Json C:\path\name.onnx.json
```

Импорт: Jarvis → **Голос** → **загрузить свой голос**.

## Чего здесь нет

- Нет готового Docker-образа «под ключ» в этом репозитории
- Нет обещания, что обучение займёт N часов — зависит от GPU и объёма данных
- Нет автоматического клонирования «с пяти минут» — для Piper нужен нормальный датасет

Если нужен быстрый результат без своего GPU — ищите сервис, который отдаёт именно **Piper onnx + json**, совместимый с `phoneme_id_map` / `sample_rate` (Jarvis это проверяет при импорте).
