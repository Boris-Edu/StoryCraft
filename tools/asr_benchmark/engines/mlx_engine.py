#!/usr/bin/env python3
"""
MLX Whisper engine (Apple GPU) for the ASR benchmark.

Runs in its own venv with `mlx-whisper` installed. Uses the same decoding settings as
BadWords' faster-whisper runner (temperature 0, no conditioning on previous text, verbatim
initial prompt, word timestamps) so the comparison isolates the engine, not the settings.

Writes the BadWords JSON contract:
  {"segments": [{"start", "end", "text", "words": [{"word", "start", "end", "probability"}]}], ...}

Usage: mlx_engine.py <audio.wav> <out.json> --model <hf-repo> [--lang en] [--prompt "..."]
"""
import argparse
import json
import time

ap = argparse.ArgumentParser()
ap.add_argument("audio")
ap.add_argument("out")
ap.add_argument("--model", required=True, help="e.g. mlx-community/whisper-large-v3-mlx")
ap.add_argument("--lang", default=None)
ap.add_argument("--prompt", default="")
args = ap.parse_args()

t0 = time.perf_counter()
import mlx.core as mx
import mlx_whisper
from mlx_whisper.load_models import load_model

# Load explicitly so model load and transcription can be timed separately
# (mlx_whisper.transcribe reuses the cached model for the same repo).
load_model(args.model)
t_load = time.perf_counter()

result = mlx_whisper.transcribe(
    args.audio,
    path_or_hf_repo=args.model,
    language=args.lang,
    initial_prompt=args.prompt or None,
    condition_on_previous_text=False,
    temperature=0.0,
    no_speech_threshold=0.6,
    logprob_threshold=-1.0,
    compression_ratio_threshold=2.4,
    word_timestamps=True,
    verbose=None,
)
t_done = time.perf_counter()

segments = []
for s in result.get("segments", []):
    segments.append({
        "start": s["start"],
        "end": s["end"],
        "text": s["text"],
        "words": [{"word": w["word"], "start": w["start"], "end": w["end"],
                   "probability": w.get("probability")} for w in s.get("words", [])],
    })

try:
    peak_gpu = mx.get_peak_memory()
except AttributeError:
    peak_gpu = mx.metal.get_peak_memory()

with open(args.out, "w", encoding="utf-8") as f:
    json.dump({
        "engine": "mlx-whisper",
        "model": args.model,
        "language": result.get("language"),
        "timings": {"load_s": t_load - t0, "transcribe_s": t_done - t_load},
        "peak_gpu_mb": peak_gpu / 1e6,
        "segments": segments,
    }, f)
