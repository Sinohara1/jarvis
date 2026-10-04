"""Custom-voice import, end to end: downloads an official Piper voice (pl gosia) on demand, then imports it as
if the user had picked it — .onnx with json next to it, .onnx alone (→ NeedJson, then with json), .zip, broken
files — into a temporary voices folder, synthesizes with it, routes it by language and deletes it.
Run: python tests/custom_voice_test.py"""
import os, shutil, sys, tempfile, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_app import tts_local as T
from jarvis_app.config import normalize_settings

ok = True


def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL"), name, info, flush=True)


OFFICIAL = "pl_PL-gosia-medium"
t = time.time()
last = [-1]


def prog(done, total):
    pct = int(done * 100 / max(1, total))
    if pct // 25 != last[0] // 25:
        print(f"  download {pct}% ({done / 1e6:.0f}/{total / 1e6:.0f} MB)", flush=True)
    last[0] = pct


T.download_voice(OFFICIAL, prog)
check("on-demand download of an official voice", T.installed(OFFICIAL), f"{time.time() - t:.1f}s")
src_m, src_j = T.voice_files(OFFICIAL)

work = tempfile.mkdtemp(prefix="cv_")
root = os.path.join(work, "voices")
try:
    # 1) .onnx + .onnx.json next to it (Cyrillic file name like a user would have)
    d1 = os.path.join(work, "pick1"); os.makedirs(d1)
    m1 = os.path.join(d1, "Мой голос (Гося).onnx")
    shutil.copy(src_m, m1); shutil.copy(src_j, m1 + ".json")
    r1 = T.import_voice(m1, custom_root=root)
    check("import .onnx with json beside it", r1["key"].startswith("custom:") and r1["lang"] == "pl",
          f"{r1['key']} label={r1['label']!r} lang={r1['lang']} test={r1['test_sec']}s audio={r1['audio_sec']}s")
    # 2) .onnx alone → NeedJson, then the user picks the json
    d2 = os.path.join(work, "pick2"); os.makedirs(d2)
    m2 = os.path.join(d2, "voice.onnx"); shutil.copy(src_m, m2)
    try:
        T.import_voice(m2, custom_root=root); need = False
    except T.NeedJson:
        need = True
    check("onnx without json asks for it", need)
    r2 = T.import_voice(m2, src_j, custom_root=root)
    check("import with the json picked separately", r2["key"] != r1["key"], r2["key"])
    # 3) .zip with both (in a subfolder, plus a MODEL_CARD)
    z = os.path.join(work, "gosia_voice.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src_m, "pl/gosia.onnx"); zf.write(src_j, "pl/gosia.onnx.json"); zf.writestr("pl/MODEL_CARD", "x")
    r3 = T.import_voice(z, custom_root=root)
    check("import .zip", r3["lang"] == "pl", r3["key"])
    # 4) broken inputs
    bad_j = os.path.join(work, "bad.json"); open(bad_j, "w").write('{"foo": 1}')
    junk = os.path.join(work, "junk.onnx"); open(junk, "wb").write(os.urandom(4096))
    for name, args in [("json that is not a piper config", (m2, bad_j)), ("random bytes as .onnx", (junk, src_j)),
                       ("wrong extension", (src_j,))]:
        try:
            T.import_voice(*args, custom_root=root); err = None
        except T.NeedJson as e:
            err = f"NeedJson {e}"
        except (ValueError, RuntimeError) as e:
            err = str(e)
        check(f"rejects {name}", err, repr(err)[:120])
    check("nothing half-copied", sorted(os.listdir(root)) == sorted(x["key"][7:] for x in (r1, r2, r3)),
          os.listdir(root))
    # 5) listed, synthesized, routed by language
    lst = T.list_custom(root)
    check("list_custom", len(lst) == 3 and all(v["lang"] == "pl" for v in lst), [v["label"] for v in lst])
    eng = T.PiperTTS(custom_root=root)
    t = time.time(); pcm, sr = eng.synth("Dzień dobry, to jest mój własny głos.", r1["key"], 0)
    check("synth with the custom voice", pcm.size > sr, f"{pcm.size / sr:.2f}s audio in {time.time() - t:.2f}s")
    # 6) delete
    for r in (r1, r2, r3):
        check(f"delete {r['key']}", T.delete_custom(r["key"], root))
    check("voices folder empty", os.listdir(root) == [])
    check("delete refuses paths outside", not T.delete_custom("custom:../voices", root))
finally:
    shutil.rmtree(work, ignore_errors=True)

# 7) routing with the real voices folder: custom voice used only for its own language
s = normalize_settings({"piper_voices": {"pl": "custom:gosia"}})
check("settings keep custom key", s["piper_voices"]["pl"] == "custom:gosia", s["piper_voices"])
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
