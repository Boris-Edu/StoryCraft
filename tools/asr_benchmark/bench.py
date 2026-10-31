#!/usr/bin/env python3
"""
ASR engine benchmark for BadWords on macOS.

Compares BadWords' current transcription (faster-whisper on CPU, called through BadWords' own
code) with MLX Whisper (Apple GPU) and Apple's on-device Speech framework, on the same audio.

What it measures, per engine:
  - wall time (incl. model load) and real-time factor (audio seconds per wall second)
  - peak memory of the engine process (and MLX GPU peak where available)
  - word count, filler count (BadWords' DEFAULT_BAD_WORDS list)
  - vs. the BadWords baseline: word difference rate, share of the baseline's fillers the engine
    also produced at the same place, and word-start timing differences (median / p90)

The baseline is not ground truth: "differs from baseline" is not the same as "wrong". The
filler report (fillers.md) lists every filler with what each engine produced around it, for
listening checks.

Usage (from the repo root):
  python3 tools/asr_benchmark/bench.py <media file> [--stream 0] [--start 0] [--duration 0]
      [--lang en] [--engines badwords-large-v3,mlx-large-v3,mlx-large-v3-turbo,apple-speech,apple-dictation]
      [--out .local/bench-runs/<name>]

See README.md for setup (BadWords install, MLX venv, Apple engine build).
"""
import argparse
import difflib
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# BadWords' normalize_audio() filter (engine/audio_extraction.py): applied once here so every
# engine gets identical input.
BADWORDS_FILTER = "highpass=f=80, acompressor=threshold=-15dB:ratio=2:attack=10:release=50"

DEFAULT_ENGINES = ("badwords-large-v3,mlx-large-v3,mlx-large-v3-turbo,"
                   "mlx-large-v3-whole,apple-speech,apple-dictation")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media")
    ap.add_argument("--stream", type=int, default=0, help="audio stream index (ffmpeg 0:a:N)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = whole file")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--engines", default=DEFAULT_ENGINES)
    ap.add_argument("--reference", default="badwords-large-v3")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-warmup", action="store_true", help="skip the short warm-up pass (downloads, caches)")
    ap.add_argument("--badwords-dir", default=os.path.expanduser("~/Library/Application Support/BadWords-dev/BadWords"))
    ap.add_argument("--mlx-python", default=os.path.join(REPO, ".local", "bench-venv", "bin", "python"))
    ap.add_argument("--hf-home", default=os.path.join(REPO, ".local", "bench-cache", "hf"))
    ap.add_argument("--apple-bin", default=os.path.join(HERE, "build", "apple-transcribe"))
    return ap.parse_args()


# ── Audio prep ────────────────────────────────────────────────────────────────

def prepare_audio(args, out_dir, start, duration, name):
    wav = os.path.join(out_dir, name)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if start:
        cmd += ["-ss", str(start)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", args.media, "-map", f"0:a:{args.stream}", "-af", BADWORDS_FILTER,
            "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", wav]
    subprocess.run(cmd, check=True)
    return wav


def audio_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def badwords_python(args):
    return os.path.join(args.badwords_dir, "venv", "bin", "python")


def badwords_config_value(args, expr):
    """Evaluates an expression against the BadWords install's `config` module (prompt, filler list)."""
    code = f"import sys, json; sys.path.insert(0, {args.badwords_dir!r}); import config; print(json.dumps({expr}))"
    out = subprocess.run([badwords_python(args), "-c", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


# ── Engines ───────────────────────────────────────────────────────────────────

def compute_islands(args, wav, out_dir):
    """BadWords' raw sound islands for this audio (silence detection with the install's settings)."""
    path = os.path.join(out_dir, "islands.json")
    subprocess.run([badwords_python(args), os.path.join(HERE, "engines", "badwords_fw.py"), wav, os.devnull,
                    "--badwords-dir", args.badwords_dir, "--no-normalize", "--islands-out", path],
                   check=True, capture_output=True)
    return path


def engine_command(name, args, wav, out_json, prompt, islands_path=None):
    locale = {"en": "en-US"}.get(args.lang, args.lang)
    env = dict(os.environ)
    if name.startswith("badwords-"):
        model = name[len("badwords-"):]
        cmd = [badwords_python(args), os.path.join(HERE, "engines", "badwords_fw.py"), wav, out_json,
               "--badwords-dir", args.badwords_dir, "--model", model, "--lang", args.lang, "--no-normalize"]
    elif name.startswith("mlx-"):
        # mlx-<model>        : BadWords' sound-island clusters, prompt per cluster (like-for-like)
        # mlx-<model>-whole  : one transcribe() over the whole file
        whole = name.endswith("-whole")
        repo = {"large-v3": "mlx-community/whisper-large-v3-mlx",
                "large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}[name[4:].removesuffix("-whole")]
        cmd = [args.mlx_python, os.path.join(HERE, "engines", "mlx_engine.py"), wav, out_json,
               "--model", repo, "--lang", args.lang, "--prompt", prompt]
        if not whole:
            cmd += ["--islands", islands_path]
        env["HF_HOME"] = args.hf_home
    elif name in ("apple-speech", "apple-dictation"):
        cmd = [args.apple_bin, wav, out_json, "--module", name.split("-")[1], "--locale", locale]
    else:
        raise SystemExit(f"unknown engine: {name}")
    return cmd, env


def run_engine(name, args, wav, out_json, prompt, log_path, islands_path=None):
    cmd, env = engine_command(name, args, wav, out_json, prompt, islands_path)
    with open(log_path, "w") as log:
        t0 = time.perf_counter()
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        _, status, ru = os.wait4(p.pid, 0)
        wall = time.perf_counter() - t0
    code = os.waitstatus_to_exitcode(status)
    peak_mb = ru.ru_maxrss / 1e6 if sys.platform == "darwin" else ru.ru_maxrss / 1e3
    return {"exit_code": code, "wall_s": wall, "peak_rss_mb": peak_mb}


# ── Metrics ───────────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def norm(word):
    return _PUNCT.sub("", word.lower()).strip("'")


def load_words(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    words = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            t = norm(w.get("word", ""))
            if t:
                words.append({"t": t, "raw": w.get("word", "").strip(), "start": float(w.get("start", 0.0)),
                              "end": float(w.get("end", 0.0)), "p": w.get("probability")})
    return data, words


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def compare(ref, cand, fillers):
    """Aligns candidate words to reference words; returns agreement metrics."""
    rt, ct = [w["t"] for w in ref], [w["t"] for w in cand]
    sm = difflib.SequenceMatcher(None, rt, ct, autojunk=False)
    dts, matched_ref = [], set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched_ref.add(i1 + k)
                dts.append(abs(cand[j1 + k]["start"] - ref[i1 + k]["start"]))
    ref_fill = [i for i, w in enumerate(ref) if w["t"] in fillers]
    dts.sort()
    return {
        "diff_rate_vs_ref": edit_distance(rt, ct) / max(1, len(rt)),
        "ref_fillers_matched": sum(1 for i in ref_fill if i in matched_ref),
        "ref_fillers_total": len(ref_fill),
        "start_diff_median_ms": 1000 * statistics.median(dts) if dts else None,
        "start_diff_p90_ms": 1000 * dts[int(0.9 * (len(dts) - 1))] if dts else None,
    }


def filler_report(results, ref_name, fillers, window=0.6):
    """For every filler any engine produced, shows what each engine has within ±window seconds."""
    events = []
    for name, r in results.items():
        for w in r.get("_words", []):
            if w["t"] in fillers:
                events.append((w["start"], name, w["raw"]))
    events.sort()
    # merge events from different engines that are about the same moment
    groups = []
    for t, name, raw in events:
        if groups and t - groups[-1]["t"] <= window:
            groups[-1]["hits"].setdefault(name, raw)
        else:
            groups.append({"t": t, "hits": {name: raw}})
    names = list(results.keys())
    lines = ["| time | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for g in groups:
        row = []
        for name in names:
            if name in g["hits"]:
                row.append(f"**{g['hits'][name]}**")
            else:
                near = [w["raw"] for w in results[name].get("_words", []) if abs(w["start"] - g["t"]) <= window]
                row.append(" ".join(near[:3]) or "—")
        mm, ss = divmod(g["t"], 60)
        lines.append(f"| {int(mm)}:{ss:05.2f} | " + " | ".join(row) + " |")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    base = os.path.splitext(os.path.basename(args.media))[0]
    out_dir = args.out or os.path.join(REPO, ".local", "bench-runs", f"{base}_{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)

    print(f"Preparing audio from {args.media} (stream {args.stream}) ...")
    wav = prepare_audio(args, out_dir, args.start, args.duration, "audio.wav")
    dur = audio_duration(wav)
    prompt = badwords_config_value(args, f"config.get_whisper_prompt_for_lang({args.lang!r}, '')")
    fillers = {norm(f) for f in badwords_config_value(args, "config.DEFAULT_BAD_WORDS") if norm(f) and " " not in f}
    islands_path = compute_islands(args, wav, out_dir)
    print(f"Audio: {dur:.1f}s | engines: {', '.join(engines)}")

    if not args.no_warmup:
        warm = prepare_audio(args, out_dir, args.start, 15, "warmup.wav")
        warm_islands = compute_islands(args, warm, os.path.join(out_dir, "logs"))
        for name in engines:
            print(f"  warm-up {name} ...", flush=True)
            run_engine(name, args, warm, os.path.join(out_dir, f"warmup_{name}.json"), prompt,
                       os.path.join(out_dir, "logs", f"warmup_{name}.log"), warm_islands)

    results = {}
    for name in engines:
        print(f"  run {name} ...", flush=True)
        out_json = os.path.join(out_dir, f"{name}.json")
        r = run_engine(name, args, wav, out_json, prompt, os.path.join(out_dir, "logs", f"{name}.log"), islands_path)
        if r["exit_code"] != 0 or not os.path.isfile(out_json):
            print(f"    FAILED (exit {r['exit_code']}), see logs/{name}.log")
            results[name] = r
            continue
        data, words = load_words(out_json)
        r.update({
            "rtf": dur / r["wall_s"],
            "engine_timings": data.get("timings", {}),
            "peak_gpu_mb": data.get("peak_gpu_mb"),
            "peak_rss_mb": max(r["peak_rss_mb"], data.get("peak_rss_children_mb") or 0),
            "words": len(words),
            "fillers": sum(1 for w in words if w["t"] in fillers),
            "with_confidence": sum(1 for w in words if w["p"] is not None) / max(1, len(words)),
            "_words": words,
        })
        with open(os.path.join(out_dir, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(" ".join(w["raw"] for w in words) + "\n")
        print(f"    {r['wall_s']:.1f}s wall, {r['rtf']:.1f}x realtime, {r['words']} words, {r['fillers']} fillers")
        results[name] = r

    ref = results.get(args.reference)
    if ref and "_words" in ref:
        for name, r in results.items():
            if "_words" in r and name != args.reference:
                r.update(compare(ref["_words"], r["_words"], fillers))

    # ── Report ──
    hdr = ("| engine | wall | × realtime | peak RAM | words | fillers | baseline fillers also found "
           "| word diff vs baseline | start Δ median / p90 |")
    rows = [hdr, "|---|---|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        if "_words" not in r:
            rows.append(f"| {name} | failed (exit {r['exit_code']}) | | | | | | | |")
            continue
        ram = f"{r['peak_rss_mb']:.0f} MB" + (f" (+GPU {r['peak_gpu_mb']:.0f} MB)" if r.get("peak_gpu_mb") else "")
        if name.startswith("apple-"):
            ram = "n/a (macOS service)"  # recognition runs in a system daemon, not in our process
        if name == args.reference:
            match, diff, dt = "(baseline)", "—", "—"
        else:
            match = f"{r['ref_fillers_matched']}/{r['ref_fillers_total']}" if "ref_fillers_total" in r else "—"
            diff = f"{100 * r['diff_rate_vs_ref']:.1f}%" if "diff_rate_vs_ref" in r else "—"
            dt = (f"{r['start_diff_median_ms']:.0f} / {r['start_diff_p90_ms']:.0f} ms"
                  if r.get("start_diff_median_ms") is not None else "—")
        rows.append(f"| {name} | {r['wall_s']:.1f}s | {r['rtf']:.1f}× | {ram} | {r['words']} | {r['fillers']} "
                    f"| {match} | {diff} | {dt} |")
    table = "\n".join(rows)

    with open(os.path.join(out_dir, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write(f"# ASR benchmark: {os.path.basename(args.media)}\n\n")
        f.write(f"- Audio: {dur:.1f}s, stream {args.stream}, start {args.start}s, language {args.lang}\n")
        f.write(f"- Baseline: `{args.reference}` (not ground truth)\n")
        f.write(f"- Machine: {subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True).stdout.strip()}, "
                f"macOS {subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()}\n\n")
        f.write(table + "\n")
    with open(os.path.join(out_dir, "fillers.md"), "w", encoding="utf-8") as f:
        f.write("# Fillers per engine\n\nBold = the engine produced a filler there; "
                "otherwise the words it has at that moment.\n\n")
        f.write(filler_report({k: v for k, v in results.items() if "_words" in v}, args.reference, fillers) + "\n")
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "_words"} for k, v in results.items()},
                  f, indent=2)

    print("\n" + table)
    print(f"\nFull results: {out_dir}")


if __name__ == "__main__":
    main()
