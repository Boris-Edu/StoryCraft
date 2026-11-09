#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: voicepatch.py
ROLE: Test bench: fix sentence endings that sound unfinished, using a separately installed VoiceStudio
DESCRIPTION:
When an edit ends a sentence where the speaker originally kept talking, the last words often keep
a rising or flat pitch and the sentence sounds unfinished. This tool:
  detect   measures the pitch movement over the last words of every statement in a StoryCraft
           timeline and lists the endings that don't fall
  patch    replaces the last 1-3 words of one ending with a version in the speaker's own voice:
             --mode tts      VoiceStudio clones the voice from nearby finished sentences and speaks
                             the sentence with a full stop; the best of N takes is kept
             --mode convert  you record the words with the right intonation; VoiceStudio converts
                             your take into the speaker's voice, keeping your delivery
  batch    detect + patch the N worst endings (tts mode) and write an A/B listening page
  doctor   check the VoiceStudio connection, active engine, model licence and watermark

Everything is written to an output folder (originals, patched versions, takes, report.json and
index.html). Nothing in Resolve or in StoryCraft's saves is changed.

Run with StoryCraft's Python (numpy, scipy, faster-whisper, requests are already installed):
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py doctor
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py detect "<saves>/timelines/<project>/<timeline>.json"
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py batch  "<timeline>.json" --out ~/Desktop/voicepatch
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

# The code lives in the app (src/voice_patch); this is its command line
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
from voice_patch.core import *  # noqa: E402,F401,F403
from voice_patch.core import PatchError, VoiceStudioClient, VoiceStudioError  # noqa: E402


# ---- commands ----------------------------------------------------------------------------------
def cmd_doctor(args):
    vs = VoiceStudioClient(args.url)
    try:
        vs.health()
    except VoiceStudioError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ VoiceStudio answers at {vs.base}")
    eng = vs.tts_engine()
    print(f"  engine: {eng['name']} ({eng['id']}), model: {eng['model']}, cloning: {eng['supports_cloning']}")
    lic = vs.model_licence(eng["model"])
    print(f"  model licence: {json.dumps(lic) if lic else 'not listed by VoiceStudio — check the Model Manager'}")
    text = json.dumps(lic or {}).lower()
    if any(s in text for s in ("by-nc", "non-commercial", "noncommercial")):
        print("  ⚠ this model's licence is non-commercial: don't use its output in paid work. "
              "CosyVoice 3 (Apache-2.0 model card) is the engine to pick for commercial projects.")
    wm = vs.watermark_status()
    if wm:
        print(f"  invisible watermark: {'on' if wm.get('invisible_enabled') else 'off'}"
              f"{'' if wm.get('audioseal_available') else ' (AudioSeal not installed, so nothing is embedded)'}")
    if eng["supports_cloning"] is False:
        print("  ✗ the active engine can't clone a voice: pick a cloning engine in VoiceStudio's Model Manager")
        return 1
    return 0


def cmd_detect(args):
    an = Analysis(load_timeline(args.timeline))
    rows = detect(an, args.questions)
    bad = sorted([r for r in rows if r["unfinished"]], key=lambda r: -r["score"])
    print(f"{an.tl['name']}: {len(rows)} statement endings measured, {len(bad)} don't fall")
    print_rows(bad if not args.all else sorted(rows, key=lambda r: r["time"]), args.limit)
    if args.json:
        json.dump(rows, open(args.json, "w"), indent=2)
    return 0


def _run_patches(args, picks):
    an = Analysis(load_timeline(args.timeline))
    rows = detect(an, True)
    by_id = {str(r["end_id"]): r for r in rows}
    vs = VoiceStudioClient(args.url)
    vs.health()
    os.makedirs(args.out, exist_ok=True)
    cache_path = os.path.join(args.out, "profiles.json")
    cache = json.load(open(cache_path)) if os.path.isfile(cache_path) else {}
    reports = []
    for pid in picks(rows):
        row = by_id.get(str(pid))
        if not row:
            print(f"  [{pid}] not a sentence ending in this timeline")
            continue
        t = time.time()
        print(f"  [{pid}] …{row['sentence'][-60:]}")
        rep = patch_one(an, rows, row, vs, args.out, mode=args.mode, take_path=getattr(args, "take", None),
                        takes=args.takes, asr_model=args.asr_model, vs_language=args.vs_language, profile_cache=cache)
        af = rep.get("after") or {}
        print(f"        {rep['result']}: {row['shape']} Δ{row['delta_st']:+.1f} → {af.get('shape', '?')} "
              f"Δ{af.get('delta_st', float('nan')):+.1f} st  ({time.time() - t:.0f} s)")
        reports.append(rep)
    json.dump(cache, open(cache_path, "w"), indent=2)
    write_index(args.out, reports, an.tl["name"])
    print(f"Listen: {os.path.join(os.path.abspath(args.out), 'index.html')}")
    return 0


def cmd_patch(args):
    return _run_patches(args, lambda rows: [args.end])


def cmd_batch(args):
    def pick(rows):
        bad = sorted([r for r in rows if r["unfinished"] and not r["sentence"].endswith("?")],
                     key=lambda r: -r["score"])
        return [r["end_id"] for r in bad[: args.count]]
    return _run_patches(args, pick)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("DESCRIPTION:")[0].strip())
    p.add_argument("--url", help="VoiceStudio API (default http://127.0.0.1:3900 or $VOICESTUDIO_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    d = sub.add_parser("detect")
    d.add_argument("timeline")
    d.add_argument("--all", action="store_true", help="list every ending, in time order")
    d.add_argument("--questions", action="store_true", help="include questions")
    d.add_argument("--limit", type=int, default=40)
    d.add_argument("--json")
    for name in ("patch", "batch"):
        q = sub.add_parser(name)
        q.add_argument("timeline")
        q.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "storycraft-voicepatch"))
        q.add_argument("--takes", type=int, default=3)
        q.add_argument("--asr-model", default="large-v3")
        q.add_argument("--vs-language", help="language name for VoiceStudio (default: its auto-detect)")
        if name == "patch":
            q.add_argument("--end", required=True, help="end_id from detect")
            q.add_argument("--mode", choices=("tts", "convert"), default="tts")
            q.add_argument("--take", help="convert mode: your recording of the words")
        else:
            q.add_argument("--count", type=int, default=5)
            q.set_defaults(mode="tts")
    args = p.parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "detect": cmd_detect, "patch": cmd_patch, "batch": cmd_batch}[args.cmd](args)
    except VoiceStudioError as e:
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
