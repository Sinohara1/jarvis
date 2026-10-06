#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guided LJSpeech-style recording for a custom Piper voice (Jarvis).

Records 16-bit mono WAV @ 22050 Hz. Does NOT train or export .onnx —
only prepares wavs/ + metadata.csv for piper-train (or a third-party service).

Windows / PowerShell friendly. Resume-safe.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

SAMPLE_RATE = 22050
CHANNELS = 1
SAMPWIDTH = 2  # 16-bit
AVG_SEC_PER_UTT = 6.5  # for ETA (speech + pause)


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _default_out() -> Path:
    desktop = Path.home() / "Desktop" / "JarvisVoiceRecord"
    if desktop.parent.is_dir():
        return desktop
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Jarvis" / "voice_record"


def load_prompts(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        t = " ".join(raw.split()).strip()
        if t and not t.startswith("#"):
            lines.append(t)
    if not lines:
        raise SystemExit(f"пустой список фраз: {path}")
    return lines


def ensure_dataset(root: Path) -> tuple[Path, Path]:
    wavs = root / "wavs"
    wavs.mkdir(parents=True, exist_ok=True)
    meta = root / "metadata.csv"
    if not meta.exists():
        meta.write_text("", encoding="utf-8")
    return wavs, meta


def read_metadata(meta: Path) -> dict[str, str]:
    """id -> text (LJSpeech: id|text or id|text|norm)."""
    out: dict[str, str] = {}
    if not meta.exists() or meta.stat().st_size == 0:
        return out
    with meta.open("r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f, delimiter="|"):
            if not row:
                continue
            uid = row[0].strip()
            text = (row[1] if len(row) > 1 else "").strip()
            if uid:
                out[uid] = text
    return out


def write_metadata(meta: Path, rows: list[tuple[str, str]]) -> None:
    tmp = meta.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="|", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for uid, text in rows:
            w.writerow([uid, text])
    tmp.replace(meta)


def uid_for(i: int) -> str:
    return f"{i:05d}"


def wav_path(wavs: Path, uid: str) -> Path:
    return wavs / f"{uid}.wav"


def write_wav_int16(path: Path, pcm: bytes, sr: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(sr)
        wf.writeframes(pcm)


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def record_sounddevice(seconds_hint: float | None = None) -> bytes:
    """Press Enter to start, Enter to stop. Returns raw int16 mono PCM."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError(
            "нужен пакет sounddevice (pip install sounddevice). "
            "Или установи ffmpeg и перезапусти с --backend ffmpeg."
        ) from e

    print("  [Enter] — начать запись…", flush=True)
    input()
    print("  ● Запись… [Enter] — стоп", flush=True)
    chunks: list = []
    stop = {"v": False}

    def _cb(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"  ! {status}", flush=True)
        chunks.append(indata.copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=_cb,
    )
    stream.start()
    t0 = time.monotonic()
    try:
        input()
    finally:
        stream.stop()
        stream.close()
    dur = time.monotonic() - t0
    if not chunks:
        raise RuntimeError("пустая запись — проверь микрофон")
    audio = np.concatenate(chunks, axis=0)
    if audio.ndim > 1:
        audio = audio[:, 0]
    # drop trailing ~80 ms often filled with Enter-key noise
    trim = int(SAMPLE_RATE * 0.05)
    if audio.size > trim * 3:
        audio = audio[:-trim]
    peak = int(np.max(np.abs(audio))) if audio.size else 0
    print(f"  записано {dur:.1f} с, пик={peak}", flush=True)
    if peak < 80:
        print("  ⚠ очень тихо — ближе к микрофону или громче", flush=True)
    elif peak > 30000:
        print("  ⚠ возможны клиппинги — чуть тише", flush=True)
    return audio.astype("<i2").tobytes()


def record_ffmpeg() -> bytes:
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg не найден в PATH")
    print("  [Enter] — начать запись…", flush=True)
    input()
    print("  ● Запись (ffmpeg)… [Enter] — стоп", flush=True)
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_path = Path(tmp)
    # Windows default mic via dshow is awkward; use ffmpeg default lavfi/pulse/wasapi.
    # Prefer wasapi default on Windows, pulse/alsa elsewhere, else default.
    if sys.platform == "win32":
        # DirectShow default device name varies; wasapi is more reliable on Win10+.
        cmd = [
            "ffmpeg", "-y", "-f", "wasapi", "-i", "default",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
            str(tmp_path),
        ]
    elif sys.platform == "darwin":
        cmd = [
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":default",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
            str(tmp_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-f", "pulse", "-i", "default",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            str(tmp_path),
        ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        input()
    finally:
        if proc.stdin:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    if not tmp_path.is_file() or tmp_path.stat().st_size < 44:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg не записал звук — попробуй --backend sounddevice")
    with wave.open(str(tmp_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            # re-encode via ffmpeg to be safe
            tmp2 = tmp_path.with_suffix(".mono.wav")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(tmp_path),
                    "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
                    str(tmp2),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tmp_path.unlink(missing_ok=True)
            tmp_path = tmp2
            with wave.open(str(tmp_path), "rb") as wf2:
                pcm = wf2.readframes(wf2.getnframes())
                sr = wf2.getframerate()
        else:
            pcm = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
    tmp_path.unlink(missing_ok=True)
    if sr != SAMPLE_RATE:
        raise RuntimeError(f"неожиданная частота {sr}, нужна {SAMPLE_RATE}")
    dur = len(pcm) / (SAMPLE_RATE * SAMPWIDTH)
    print(f"  записано {dur:.1f} с", flush=True)
    return pcm


def pick_backend(name: str) -> str:
    name = (name or "auto").lower()
    if name == "sounddevice":
        return "sounddevice"
    if name == "ffmpeg":
        return "ffmpeg"
    # auto
    try:
        import sounddevice  # noqa: F401
        return "sounddevice"
    except ImportError:
        if has_ffmpeg():
            return "ffmpeg"
        raise SystemExit(
            "Нет backend для записи.\n"
            "  pip install sounddevice\n"
            "  или установи ffmpeg и добавь в PATH."
        )


def record_once(backend: str) -> bytes:
    if backend == "ffmpeg":
        return record_ffmpeg()
    return record_sounddevice()


def clear_screen() -> None:
    os.system("cls" if sys.platform == "win32" else "clear")


def progress_line(done: int, total: int) -> str:
    left = max(0, total - done)
    eta_min = left * AVG_SEC_PER_UTT / 60.0
    pct = 100.0 * done / total if total else 0
    return f"{done}/{total} ({pct:.0f}%) · осталось ~{eta_min:.0f} мин"


def run_session(out_dir: Path, prompts: list[str], backend: str, start_at: int) -> None:
    wavs, meta_path = ensure_dataset(out_dir)
    total = len(prompts)
    done_ids = {
        uid_for(i)
        for i in range(1, total + 1)
        if wav_path(wavs, uid_for(i)).is_file()
    }
    print(f"Папка: {out_dir}")
    print(f"Backend: {backend} · {SAMPLE_RATE} Hz · mono · 16-bit")
    print(f"Фраз: {total} · уже записано: {len(done_ids)}")
    print()
    print("Клавиши после показа фразы:")
    print("  Enter  — записать (ещё Enter — стоп)")
    print("  r      — перезаписать эту же")
    print("  s      — пропустить")
    print("  b      — назад на одну")
    print("  q      — сохранить прогресс и выйти")
    print()
    input("Enter — начать сессию… ")

    idx = max(1, min(start_at, total))
    # default: resume at first missing; explicit --start N jumps there (redo ok)
    if start_at == 1:
        while idx <= total and uid_for(idx) in done_ids:
            idx += 1

    while idx <= total:
        uid = uid_for(idx)
        text = prompts[idx - 1]
        clear_screen()
        done_n = sum(1 for i in range(1, total + 1) if uid_for(i) in done_ids)
        print("=" * 60)
        print(f"  Jarvis · запись голоса · {progress_line(done_n, total)}")
        print("=" * 60)
        print()
        print(f"  [{idx}/{total}]  {uid}.wav")
        print()
        print(f"  « {text} »")
        print()
        if uid in done_ids:
            print("  (уже есть файл — Enter перезапишет, s — дальше)")
        print()
        print("  Enter=записать  r=redo  s=skip  b=назад  q=выход")
        choice = input("  > ").strip().lower()

        if choice in ("q", "й"):
            break
        if choice in ("s", "ы"):
            idx += 1
            continue
        if choice in ("b", "и"):
            idx = max(1, idx - 1)
            continue
        if choice in ("r", "к"):
            pass  # fall through to record
        elif choice not in ("",):
            print("  неизвестная команда")
            time.sleep(0.6)
            continue

        try:
            pcm = record_once(backend)
        except KeyboardInterrupt:
            print("\n  прервано")
            break
        except Exception as e:
            print(f"  ошибка записи: {e}")
            input("  Enter — продолжить… ")
            continue

        if len(pcm) < SAMPLE_RATE * SAMPWIDTH * 0.3:
            print("  слишком коротко (<0.3 с) — не сохраняю")
            input("  Enter… ")
            continue

        dest = wav_path(wavs, uid)
        write_wav_int16(dest, pcm, SAMPLE_RATE)
        done_ids.add(uid)
        # rewrite metadata for all existing wavs in prompt order
        rows = []
        for i, t in enumerate(prompts, start=1):
            u = uid_for(i)
            if wav_path(wavs, u).is_file():
                rows.append((u, t))
        write_metadata(meta_path, rows)
        print(f"  ✓ сохранено {dest.name}")
        time.sleep(0.35)
        idx += 1

    # final metadata rewrite (only existing wavs, prompt order)
    final_rows = []
    for i, text in enumerate(prompts, start=1):
        uid = uid_for(i)
        if wav_path(wavs, uid).is_file():
            final_rows.append((uid, text))
    write_metadata(meta_path, final_rows)

    done_n = len(final_rows)
    clear_screen()
    print("=" * 60)
    print("  Сессия сохранена")
    print("=" * 60)
    print(f"  Папка:     {out_dir}")
    print(f"  Записано:  {done_n}/{total}")
    print(f"  WAV:       {out_dir / 'wavs'}")
    print(f"  Метаданные:{out_dir / 'metadata.csv'}")
    print()
    print("  Это ещё НЕ голос Piper (.onnx).")
    print("  Дальше: обучение (см. train_hint.md) → name.onnx + name.onnx.json")
    print("  Затем в Jarvis: Голос → загрузить свой голос (или pack_for_jarvis.ps1).")
    print()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Запись датасета для кастомного голоса Piper / Jarvis")
    ap.add_argument(
        "-o", "--out",
        type=Path,
        default=None,
        help="папка датасета (по умолчанию Desktop\\JarvisVoiceRecord или %%LOCALAPPDATA%%\\Jarvis\\voice_record)",
    )
    ap.add_argument(
        "-p", "--prompts",
        type=Path,
        default=None,
        help="файл с фразами (по умолчанию prompts_ru.txt рядом со скриптом)",
    )
    ap.add_argument(
        "--backend",
        choices=("auto", "sounddevice", "ffmpeg"),
        default="auto",
    )
    ap.add_argument(
        "--start",
        type=int,
        default=1,
        help="начать с номера фразы (1-based); по умолчанию продолжает с первой незаписанной",
    )
    args = ap.parse_args()

    prompts_path = args.prompts or (_script_dir() / "prompts_ru.txt")
    if not prompts_path.is_file():
        raise SystemExit(f"не найден список фраз: {prompts_path}")
    prompts = load_prompts(prompts_path)
    out_dir = (args.out or _default_out()).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # copy prompts into dataset for reproducibility
    dest_prompts = out_dir / "prompts_ru.txt"
    if not dest_prompts.exists() or dest_prompts.resolve() != prompts_path.resolve():
        try:
            shutil.copy2(prompts_path, dest_prompts)
        except shutil.SameFileError:
            pass
    backend = pick_backend(args.backend)
    run_session(out_dir, prompts, backend, args.start)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nВыход.")
        sys.exit(130)
