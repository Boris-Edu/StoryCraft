#!/usr/bin/env python3
"""
Baseline engine: BadWords' own faster-whisper path, called through its real code.

Runs with the Python of a BadWords v4 install and imports that install's modules, then does
what run_analysis_pipeline() does before transcription: normalize the audio, detect silence,
compute sound islands, and call run_whisper() with the per-language verbatim prompt and the
default filler list. Device is CPU (what BadWords uses on every Mac).

Usage: <badwords>/venv/bin/python badwords_fw.py <audio.wav> <out.json>
           --badwords-dir <install dir> [--model large-v3] [--lang en]
"""
import argparse
import json
import os
import resource
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("audio")
ap.add_argument("out")
ap.add_argument("--badwords-dir", required=True)
ap.add_argument("--model", default="large-v3")
ap.add_argument("--lang", default="en")
ap.add_argument("--no-normalize", action="store_true",
                help="input already has BadWords' normalize filter applied (bench.py does this)")
ap.add_argument("--islands-out", default=None,
                help="only compute BadWords' raw sound islands, write them as JSON here, and exit")
args = ap.parse_args()

bw = os.path.abspath(args.badwords_dir)
sys.path.insert(0, bw)
sys.path.insert(1, os.path.join(bw, "libs"))

import config  # noqa: E402  (BadWords modules)
from osdoc import OSDoctor  # noqa: E402
from engine import AudioEngine  # noqa: E402

t0 = time.perf_counter()
eng = AudioEngine(OSDoctor(), None)
prefs = eng.os_doc.get_all_prefs()

norm = args.audio if args.no_normalize else eng.normalize_audio(args.audio)
silence = eng.detect_silence(norm, prefs.get("silence_threshold_db", -42.0), prefs.get("silence_min_dur", 0.2))
duration = eng._get_audio_duration(norm)
islands = eng._compute_sound_islands(silence, duration)
if args.islands_out:
    with open(args.islands_out, "w") as f:
        json.dump([list(map(float, isl)) for isl in islands], f)
    sys.exit(0)
compute = prefs.get("ai_compute_type") or eng._get_optimal_compute_type("cpu")
if compute == "Auto":
    compute = eng._get_optimal_compute_type("cpu")
prompt = config.get_whisper_prompt_for_lang(args.lang, prefs.get("ai_initial_prompt", "").strip())
t_prep = time.perf_counter()

json_path = eng.run_whisper(
    norm, args.model, args.lang, True, "CPU", compute,
    prefs.get("filler_words", config.DEFAULT_BAD_WORDS),
    initial_prompt=prompt,
    islands=islands,
)
t_done = time.perf_counter()
if not json_path:
    sys.exit("run_whisper failed (see BadWords log)")

with open(json_path, encoding="utf-8") as f:
    data = json.load(f)

with open(args.out, "w", encoding="utf-8") as f:
    json.dump({
        "engine": "badwords-faster-whisper",
        "model": args.model,
        "language": data.get("language"),
        "settings": {"device": "cpu", "compute_type": compute, "islands": len(islands), "prompt": prompt},
        # run_whisper loads the model inside its runner, so load time is part of transcribe_s
        "timings": {"prep_s": t_prep - t0, "transcribe_s": t_done - t_prep},
        # run_whisper transcribes in a child process; report its peak RSS too
        "peak_rss_children_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1e6,
        "segments": data.get("segments", []),
    }, f)
