# Third-party components / Сторонние компоненты

The MIT license in `LICENSE` covers only the Jarvis source code. These components keep their own licenses.

| Component | Where | License |
|---|---|---|
| [Silero VAD](https://github.com/snakers4/silero-vad) | `models/silero_vad.onnx` | MIT |
| [openWakeWord](https://github.com/dscripka/openWakeWord) models (`hey_jarvis_v0.1`, `embedding_model`, `melspectrogram`) | `models/*.onnx` | CC BY-NC-SA 4.0 — **non-commercial use only** |
| Manrope, Urbanist fonts | `web/fonts/` | SIL Open Font License 1.1 |
| [Piper](https://github.com/OHF-Voice/piper1-gpl) (`piper-tts`) + espeak-ng data | pip dependency, bundled into `Jarvis.exe` | GPL-3.0 |
| Piper voices ([rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)) | downloaded at runtime | per voice: CC0, RHVoice license, CC BY-NC-SA 4.0, … |
| [Vosk](https://alphacephei.com/vosk/) + models | pip dependency / downloaded at runtime | Apache-2.0 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (optional) + Whisper models | optional pip dependency / downloaded at runtime | MIT |
| Other Python dependencies (`requirements.txt`) | pip | their own licenses |

Because the prebuilt `Jarvis.exe` bundles GPL-3.0 Piper/espeak-ng, the exe as a whole is distributed under
GPL-3.0 terms; its complete source is this repository (MIT is GPL-compatible).
Because of the openWakeWord models, the bundled "Hey Jarvis" wake word must not be used commercially.
