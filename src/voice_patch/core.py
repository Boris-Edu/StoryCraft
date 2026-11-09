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
             --mode convert  you record the words with the intonation you want; VoiceStudio says them
                             in the speaker's voice (3 takes) and the take whose melody and pace
                             are closest to your recording is kept
  batch    detect + patch the N worst endings (tts mode) and write an A/B listening page
  doctor   check the VoiceStudio connection, active engine, model licence and watermark

Everything is written to an output folder (originals, patched versions, takes, report.json and
index.html). Nothing in Resolve or in StoryCraft's saves is changed.

Run with StoryCraft's Python (numpy, scipy, faster-whisper, requests are already installed):
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py doctor
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py detect "<saves>/timelines/<project>/<timeline>.json"
  "<install>/venv/bin/python3" tools/voicepatch/voicepatch.py batch  "<timeline>.json" --out ~/Desktop/voicepatch
"""
import difflib
import html
import json
import os
import re
import sys
import tempfile
import time

import numpy as np

from . import audio as A
from .client import VoiceStudioClient, VoiceStudioError  # noqa: F401  (re-exported)

STORYCRAFT_HOME = os.path.expanduser("~/Library/Application Support/StoryCraft")
MODELS_DIR = None  # set by the app (<install>/models); else STORYCRAFT_HOME/models
VOICESTUDIO_URL = "https://github.com/debpalash/VoiceStudio"
MIN_WINDOW_S = 0.45      # pitch is judged over at least this much of the sentence's end
MAX_PATCH_WORDS = 3
STRETCH_RANGE = (0.8, 1.25)
PASSAGE_STRETCH_RANGE = (0.92, 1.08)  # a passage said cleanly or rewritten keeps its natural pace

# ---- delivery: how the patch is said (the Voice patch page's Delivery controls) -------------------
# ending  fall: a finished statement (reference ends falling, prompt ends "."), rise: a rising ending
#         (reference ends rising, prompt ends "?"), asis: like the recording (reference around the target)
# pitch_st  semitones on top of matching the speaker's pitch around the patch (-2 .. +2, step 0.5)
# pace    VoiceStudio's speed: slower 0.88, natural 1.0, faster 1.14
# sound   room: tone (EQ) + loudness + room tone matched to the recording; clean: loudness only
DELIVERY_DEFAULT = {"ending": "fall", "pitch_st": 0.0, "pace": "natural", "sound": "room"}
PACE_SPEED = {"slower": 0.88, "natural": 1.0, "faster": 1.14}
PITCH_MATCH_MAX_ST = 3.0      # never move a take further than this to match the speaker
PITCH_MATCH_MIN_ST = 0.3      # closer than this is left alone
EQ_MAX_DB = 9.0
SHAPE_OF = {"fall": "falling", "rise": "rising"}


def norm_delivery(d=None):
    """A complete, valid delivery dict (unknown or missing values -> the defaults)."""
    out = dict(DELIVERY_DEFAULT)
    d = d or {}
    if d.get("ending") in ("fall", "rise", "asis"):
        out["ending"] = d["ending"]
    try:
        out["pitch_st"] = float(min(2.0, max(-2.0, round(float(d.get("pitch_st") or 0.0) * 2) / 2)))
    except (TypeError, ValueError):
        pass
    if d.get("pace") in PACE_SPEED:
        out["pace"] = d["pace"]
    if d.get("sound") in ("room", "clean"):
        out["sound"] = d["sound"]
    return out


def delivery_speed(d):
    return PACE_SPEED[norm_delivery(d)["pace"]]


def delivery_is_default(d):
    return norm_delivery(d) == DELIVERY_DEFAULT


def delivery_summary(d):
    """Short English summary of what differs from the defaults ("ending rises · +1 st · faster · clean")."""
    d = norm_delivery(d)
    parts = []
    if d["ending"] != "fall":
        parts.append({"rise": "ending rises", "asis": "ending as recorded"}[d["ending"]])
    if d["pitch_st"]:
        parts.append(f"{d['pitch_st']:+g} st")
    if d["pace"] != "natural":
        parts.append(d["pace"])
    if d["sound"] != "room":
        parts.append("clean")
    return " · ".join(parts)


class PatchError(RuntimeError):
    pass


# ---- timeline ----------------------------------------------------------------------------------
def _sane_silences(silences, words):
    """The saved silence analysis, unless it's broken (seen: one range over the whole recording): when it
    covers most words, the gaps between the words are used instead (room tone must never be speech)."""
    import bisect
    sil = [s for s in (silences or []) if isinstance(s, dict) and "s" in s and "e" in s]
    if sil and words:
        rs = sorted((float(s["s"]), float(s["e"])) for s in sil)
        starts = [a for a, _b in rs]
        inside = 0
        for w in words:
            m = (w["start"] + w["end"]) / 2
            k = bisect.bisect_right(starts, m) - 1
            if k >= 0 and rs[k][1] >= m:
                inside += 1
        if inside <= 0.5 * len(words):
            return sil
    elif sil:
        return sil
    out = []
    for a, b in zip(words, words[1:]):
        if b["start"] - a["end"] >= 0.2:
            out.append({"s": a["end"], "e": b["start"]})
    return out


def load_timeline(path):
    d = json.load(open(path))
    raw = d.get("words_data") or []
    words = [dict(w, start=float(w["start"]), end=float(w["end"]), text=str(w.get("text", "")).strip())
             for w in raw if w.get("type") == "word" and w.get("status") not in ("bad", "inaudible")]
    first = next((w for w in raw if w.get("meta_audio_path")), {})
    return {
        "name": (d.get("transcription_source") or {}).get("timeline_name") or os.path.basename(path),
        "words": words,
        "audio_path": first.get("meta_audio_path"),
        "silences": _sane_silences(first.get("meta_global_silence"), words),
        "language": first.get("meta_language") or None,
    }


def timeline_from_words(words_data, name):
    """The same dict as load_timeline, from the open transcript (main window's words_data)."""
    raw = words_data or []
    words = [dict(w, start=float(w["start"]), end=float(w["end"]), text=str(w.get("text", "")).strip())
             for w in raw if w.get("type") == "word" and w.get("status") not in ("bad", "inaudible")
             and w.get("start") is not None and w.get("end") is not None]
    first = next((w for w in raw if w.get("meta_audio_path")), {})
    return {"name": name or "Timeline", "words": words, "audio_path": first.get("meta_audio_path"),
            "silences": _sane_silences(first.get("meta_global_silence"), words),
            "language": first.get("meta_language") or None}


def norm(t):
    return re.sub(r"[^\w']+", "", t.lower())


ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "prof.", "mt.", "vs.", "etc."}


def sentences(words):
    """[(start_idx, end_idx)] over words; a sentence ends on . ? or ! (not on "Mr." and the like)"""
    out, s = [], 0
    for i, w in enumerate(words):
        if w["text"].endswith((".", "?", "!")) and w["text"].lower() not in ABBREVIATIONS:
            out.append((s, i))
            s = i + 1
    return out


def ending_window(words, s, e):
    """Indices of the last words of sentence (s, e) spanning >= MIN_WINDOW_S, at most MAX_PATCH_WORDS."""
    i = e
    while i > s and (e - i + 1) < MAX_PATCH_WORDS and words[e]["end"] - words[i]["start"] < MIN_WINDOW_S:
        i -= 1
    return i, e


class Analysis:
    """The timeline's audio with a pitch track, loaded once."""

    def __init__(self, tl):
        self.tl = tl
        if not tl["audio_path"] or not os.path.isfile(tl["audio_path"]):
            raise PatchError(f"The timeline's audio isn't on disk any more ({tl['audio_path']}). Open the timeline "
                             "in StoryCraft once so it extracts the audio again.")
        self.x = A.load(tl["audio_path"])
        self.times, self.f0 = A.f0_track(self.x)

    def local_median(self, t0, t1):
        m = (self.times >= t0) & (self.times <= t1) & ~np.isnan(self.f0)
        v = self.f0[m]
        return float(np.median(v)) if len(v) >= 10 else None

    def contour(self, words, s, e):
        a, b = ending_window(words, s, e)
        ref = self.local_median(words[s]["start"] - 10, words[e]["end"])
        if not ref:
            return None, (a, b)
        return A.ending_contour(self.times, self.f0, words[a]["start"], words[b]["end"], ref), (a, b)


def detect(an, include_questions=False):
    words = an.tl["words"]
    rows = []
    for s, e in sentences(words):
        text = " ".join(w["text"] for w in words[s:e + 1])
        if words[e]["text"].endswith("?") and not include_questions:
            continue
        c, (a, b) = an.contour(words, s, e)
        if not c:
            continue
        nxt = words[e + 1]["start"] if e + 1 < len(words) else None
        # Unfinished: the pitch rises over the last words, or stays level while sitting above the
        # speaker's usual pitch. A level ending that is already low reads as finished.
        score = c["delta_st"] + 0.5 * max(c["final_st"], 0.0)
        unfinished = c["delta_st"] >= 0.8 or (c["delta_st"] > -1.0 and c["final_st"] >= 1.0)
        rows.append({"end_id": words[e]["id"], "end_idx": e, "start_idx": s, "patch_from_idx": a,
                     "patch_from_id": words[a]["id"], "start_t": round(words[s]["start"], 2),
                     "time": round(words[e]["end"], 2), "pause_after": round(nxt - words[e]["end"], 2) if nxt else None,
                     "sentence": text, "patch_words": " ".join(w["text"] for w in words[a:b + 1]), **c,
                     "score": round(score, 2), "unfinished": unfinished})
    return rows


# ---- alignment ---------------------------------------------------------------------------------
_ASR = {}
_ASR_LOCK = __import__("threading").RLock()   # one model, used by the patch job and the words reader in turn


def asr(model_name):
    with _ASR_LOCK:
        if model_name not in _ASR:
            from faster_whisper import WhisperModel
            root = MODELS_DIR or os.path.join(STORYCRAFT_HOME, "models")
            _ASR[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8",
                                            download_root=root if os.path.isdir(root) else None,
                                            local_files_only=os.path.isdir(root))
        return _ASR[model_name]


def transcribe_words(x, language, model_name, beam_size=5):
    y = A.resample(x, A.SR, 16000)
    with _ASR_LOCK:
        segs, _ = asr(model_name).transcribe(y, language=language, word_timestamps=True, beam_size=beam_size,
                                             vad_filter=False, condition_on_previous_text=False)
        return [{"text": w.word.strip(), "start": w.start, "end": w.end} for s in segs for w in (s.words or [])]


# ---- the patch's own words (the transcript follows its rhythm: voice_patch/transcript.py) ------------
CONTEXT_S = (1.5, 1.0)   # the recording read with the patch (before, after): Whisper needs the sentence around it


def recording_context(x, a, b, sr=A.SR):
    """(before, after): the recording just before a and just after b (x: the whole recording)."""
    ia, ib = int(round(a * sr)), int(round(b * sr))
    return (np.asarray(x[max(0, ia - int(CONTEXT_S[0] * sr)): max(0, ia)], np.float32),
            np.asarray(x[ib: ib + int(CONTEXT_S[1] * sr)], np.float32))


def load_span(path, t0, t1, sr=A.SR):
    """Seconds t0..t1 of an audio file (only that part is decoded), mono float32 at sr."""
    import subprocess
    t0 = max(0.0, float(t0))
    if t1 <= t0:
        return np.zeros(0, np.float32)
    out = subprocess.run([A.ffmpeg_bin(), "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", path,
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def patch_words(x, language, model_name, before=None, after=None):
    """[{text, start, end}] (seconds in x) of what the fitted patch says, for the transcript's timing.
    before / after: the recording around the span, read with it (a patch is often under a second: alone,
    Whisper mishears it and its times drift; in its sentence it hears it as the story does). Only the words
    that lie in x are kept. The words themselves are known (the take was checked): the fast greedy search
    is enough for their times."""
    pre = np.zeros(0, np.float32) if before is None else np.asarray(before, np.float32)
    post = np.zeros(0, np.float32) if after is None else np.asarray(after, np.float32)
    t0, n = len(pre) / A.SR, len(x) / A.SR
    out = []
    for w in transcribe_words(np.concatenate([pre, np.asarray(x, np.float32), post]), language, model_name,
                              beam_size=1):
        if not norm(w["text"]) or w["end"] is None or w["start"] is None:
            continue
        s, e = float(w["start"]) - t0, float(w["end"]) - t0
        if not 0.0 <= (s + e) / 2.0 <= n:
            continue                       # a word of the recording around the patch
        s, e = round(min(max(s, 0.0), n), 3), round(min(max(e, 0.0), n), 3)
        out.append({"text": w["text"], "start": s, "end": max(e, s + 0.01)})
    return out


def read_patch_words(d, language, model_name, report=None, audio_path=None):
    """What the file that plays for the patch in folder d says, with times: (take, "preview"|"patch", words,
    file mtime), or None (no fitted patch). audio_path: the recording (read around the span for context).
    Slow (ASR): run it off the UI thread; store_patch_words saves it."""
    from .transcript import playing_file
    if report is None:
        with open(os.path.join(d, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    pf = playing_file(d, report)
    if pf is None:
        return None
    mtime = os.path.getmtime(pf[1])
    before = after = None
    span = report.get("original_span") or []
    if audio_path and os.path.isfile(audio_path) and len(span) == 2:
        try:
            a, b = float(span[0]), float(span[1])
            before, after = load_span(audio_path, a - CONTEXT_S[0], a), load_span(audio_path, b, b + CONTEXT_S[1])
        except Exception as exc:
            print(f"[voice patch] recording around the patch not read: {exc}")
    return report.get("chosen_take"), pf[0], patch_words(A.load(pf[1]), language, model_name, before, after), mtime


def store_patch_words(d, take, kind, words, mtime):
    """Save words read by read_patch_words in the report (and its take's entry), unless the patch changed
    meanwhile (another take chosen, a new patch made). Returns True when saved."""
    from .transcript import playing_file
    try:
        with open(os.path.join(d, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except Exception:
        return False
    pf = playing_file(d, report)
    if pf is None or pf[0] != kind or report.get("chosen_take") != take or os.path.getmtime(pf[1]) != mtime:
        return False
    report["patch_words"], report["patch_words_file"] = words, kind
    for t in report.get("takes") or []:
        if t.get("take") == take and t.get("ready"):
            t["patch_words"], t["patch_words_file"] = words, kind
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return True


def _find_run(take_words, target):
    """(index of the first take word, mean similarity, per-word similarities) of the run of take words that
    says the target words one to one, searching from the end; None when no run is close enough."""
    tgt = [norm(t) for t in target]
    got = [norm(w["text"]) for w in take_words]
    n = len(tgt)
    best = None
    for i in range(len(got) - n, -1, -1):
        sims = [difflib.SequenceMatcher(None, got[i + k], tgt[k]).ratio() for k in range(n)]
        score = float(np.mean(sims))
        if best is None or score > best[0]:
            best = (score, i, sims)
        if score >= 0.95:
            break
    if not best or best[0] < 0.7:
        return None
    return best[1], best[0], best[2]


def find_words(take_words, target):
    """Locate the target words (list of strings) in a take's words, searching from the end."""
    run = _find_run(take_words, target)
    if run is None:
        return None
    i, score, _sims = run
    return take_words[i]["start"], take_words[i + len(target) - 1]["end"], round(float(score), 2)


def decay_end(x, t, limit, sr=A.SR):
    """Extend t forward while the signal is still decaying above the floor (Whisper ends words early)."""
    win = int(0.01 * sr)
    floor = max(A.rms(x[: int(0.2 * sr)]), 10 ** (-50 / 20))
    end = t
    while end + 0.01 < limit:
        seg = x[int(end * sr): int(end * sr) + win]
        if len(seg) < win or A.rms(seg) < floor * 2.5:
            break
        end += 0.01
    return end


# ---- word timing -------------------------------------------------------------------------------
# A patch is laid at the start of its span a, so its first word must start exactly where the original's
# did. An ending (the same words) then follows the original word by word: each word (with the gap before
# it) is stretched so its onset lands on the original's, within +-25 % per piece. A passage or a rewrite
# keeps the take's own rhythm, but pauses longer than the speaker's usual pause are shortened (never
# lengthened). The take is cut at its first target word (a filler or breath before it is left out) and at
# the decay of its last.
WORD_STRETCH_RANGE = (0.75, 1.25)
LEAD_S = 0.09            # the span starts this far before its first word when the pause allows (room for
                         # the crossfade from the recording into the patch)
PRE_ROLL_S = 0.04        # at most this much of the take before its first word is kept
PAUSE_GAP_MIN_S = 0.12   # a gap between transcript words counts as a pause from this length
PAUSE_RANGE = (0.15, 0.6)
PAUSE_DEFAULT_S = 0.3
FILLERS = {"uh", "um", "er", "erm", "ah", "eh", "hmm", "mm", "mhm", "uhm"}


def typical_pause(words):
    """The speaker's usual pause (seconds): median of the transcript's gaps between words that are pauses
    (>= PAUSE_GAP_MIN_S, under 2.5 s), kept within PAUSE_RANGE."""
    gaps = [float(b["start"]) - float(a["end"]) for a, b in zip(words, words[1:])]
    gaps = [g for g in gaps if PAUSE_GAP_MIN_S <= g <= 2.5]
    if not gaps:
        return PAUSE_DEFAULT_S
    return float(min(max(np.median(gaps), PAUSE_RANGE[0]), PAUSE_RANGE[1]))


def span_start(x, first_start, prev_end=None):
    """Where a span starts: up to LEAD_S before its first word, in the pause before it (never into the word
    before); with no pause, the quiet point just before the word as before."""
    lo = first_start - LEAD_S
    if prev_end is not None:
        lo = max(lo, float(prev_end) + 0.01)
    hi = first_start - 0.01
    if hi - lo < 0.015:
        a = A.snap_quiet(x, first_start - 0.01)
        return max(a, float(prev_end) + 0.005) if prev_end is not None else a
    return min(max(A.snap_quiet(x, lo + 0.01, radius=0.01), lo), hi)


def _onsets(x, ws, lo0):
    """Refined onsets (seconds, absolute) of the words ws ({start, end}) in x; the first is searched from
    lo0, each next one from the middle of the word before it; they always rise."""
    out = []
    for j, w in enumerate(ws):
        s, e = float(w["start"]), float(w["end"])
        lo = lo0 if j == 0 else (float(ws[j - 1]["start"]) + float(ws[j - 1]["end"])) / 2
        o, _q = A.word_onset(x, s, lo, max(s + 0.03, (s + e) / 2))
        if out and o < out[-1] + 0.02:
            o = max(s, out[-1] + 0.02)
        out.append(o)
    return out


def _take_cut(take, tw, i0, i1):
    """Cut points [s, e] of take words i0..i1 and their timing relative to s: the cut starts at a quiet
    point just before the first word's real onset (at most 60 ms before it: a filler, a breath or a cut
    syllable before it stays out) and ends at the decay of the last word (never into a word after it)."""
    lo0 = (float(tw[i0 - 1]["start"]) + float(tw[i0 - 1]["end"])) / 2 if i0 > 0 else 0.0
    on = _onsets(take, tw[i0: i1 + 1], lo0)
    _o, q = A.word_onset(take, float(tw[i0]["start"]), lo0, max(float(tw[i0]["start"]) + 0.03, on[0] + 0.01))
    s = q if q < on[0] - 0.002 else on[0] - 0.01
    s = max(0.0, s, on[0] - 0.06)
    total = len(take) / A.SR
    last_end = float(tw[i1]["end"])
    nxt = float(tw[i1 + 1]["start"]) if i1 + 1 < len(tw) else total
    e = decay_end(take, last_end, min(total, last_end + 0.3, max(nxt, last_end + 0.01)))
    e = max(e, on[-1] + 0.05)
    tm = {"onsets": [round(o - s, 4) for o in on], "ends": [round(float(w["end"]) - s, 4) for w in tw[i0: i1 + 1]],
          "end": round(e - s, 4)}
    return s, e, tm


def _shorten_pauses(x, on, max_pause, sr=A.SR):
    """Pauses between the words (the quiet run before each next onset) longer than max_pause cut down to
    it, from their middle, crossfaded. Returns (audio, new onsets, [[before_s, after_s], ...])."""
    _t, env = A.envelope(x, sr)
    thr = max(A.noise_floor(x, sr) * 4, (float(env.max()) if len(env) else 0.0) * 0.01, 10 ** (-60 / 20))
    hop = 0.005
    xf = 0.01
    parts, cuts, new_on = [], [], [on[0]]
    pos, shift = 0.0, 0.0
    for k in range(len(on) - 1):
        p1 = on[k + 1]
        p0 = p1 - 0.005
        floor_t = on[k] + 0.05
        while p0 - hop > floor_t:      # walk back through the quiet before the next word
            i = int((p0 - hop) / hop)
            if i < 0 or i >= len(env) or env[i] >= thr:
                break
            p0 -= hop
        gap = p1 - p0
        if gap > max_pause + 0.03:
            c0, c1 = p0 + max_pause / 2, p1 - max_pause / 2 - xf
            parts.append(x[int(pos * sr): int(c0 * sr)])
            pos = c1
            shift += c1 - c0 + xf
            cuts.append([round(gap, 3), round(max_pause, 3)])
        new_on.append(p1 - shift)
    parts.append(x[int(pos * sr):])
    return A.join(parts, sr, xf), new_on, cuts


def _time_fit(patch, tm, plan, sr=A.SR):
    """The take's cut (patch, with its word timing tm) laid out on the span's clock: its first word at
    plan["lead"] seconds (the original's first onset after a); then
      kind "words"    each next onset on plan["dst"] (the original's onsets, the last item the span's end),
                      piecewise WSOLA within WORD_STRETCH_RANGE per piece; falls back to "uniform" when a
                      word wasn't matched one to one
      kind "passage"  pauses longer than plan["max_pause"] shortened, the rest as said
      plan["uniform"] then one stretch of everything after the first onset toward plan["target_s"] when the
                      ratio lies within plan["range"] (an unmatched ending, a passage said cleanly, convert)
    Returns (audio, info)."""
    lead = max(0.0, float(plan["lead"]))
    on = [float(o) for o in tm["onsets"]]
    pre = on[0]
    keep = min(pre, lead, PRE_ROLL_S)
    cut = pre - keep
    x = np.asarray(patch[int(round(cut * sr)):], np.float32).copy()
    on = [o - cut for o in on]
    end = min(float(tm["end"]) - cut, len(x) / sr)
    x = x[: int(round(end * sr))]
    nk = int(keep * sr)
    if nk > 1:                                      # the kept bit of quiet before the word fades in
        x[:nk] *= np.sin(np.linspace(0, np.pi / 2, nk)).astype(np.float32)
    pad = lead - keep
    info = {"lead_s": round(lead, 3), "pre_roll_s": round(keep, 3)}
    kind = plan.get("kind", "uniform")
    sims = tm.get("sims") or []
    if kind == "words" and not (len(plan.get("dst") or []) == len(on) + 1 and sims and min(sims) >= 0.6):
        kind, info["fallback"] = "uniform", "a word wasn't matched one to one"
    ratio = None
    if kind == "words":
        src = on + [end]
        want = [float(d) - pad for d in plan["dst"]]
        got, clamped = [keep], 0
        for i in range(1, len(src)):
            ds, dw = src[i] - src[i - 1], want[i] - got[-1]
            r = dw / ds if ds > 0.005 else 1.0
            rc = min(max(r, WORD_STRETCH_RANGE[0]), WORD_STRETCH_RANGE[1])
            clamped += int(abs(rc - r) > 1e-6)
            got.append(got[-1] + ds * rc)
        x = A.stretch_map(x, [0.0] + src, [0.0] + got, sr)
        info.update({"method": "words", "clamped": clamped,
                     "onset_err_ms": [int(round((g - w) * 1000)) for g, w in zip(got[:-1], want[:-1])],
                     "end_err_ms": int(round((got[-1] - want[-1]) * 1000)),
                     "piece_ratios": [round((got[i] - got[i - 1]) / max(src[i] - src[i - 1], 1e-6), 3)
                                      for i in range(1, len(src))]})
    else:
        if kind == "passage":
            x, on, cuts = _shorten_pauses(x, on, float(plan["max_pause"]), sr)
            info.update({"max_pause_s": round(float(plan["max_pause"]), 3), "pauses_cut": cuts})
        if plan.get("uniform") and plan.get("target_s"):
            body = len(x) / sr - keep
            r = (float(plan["target_s"]) - lead) / body if body > 0.05 else None
            lo, hi = plan.get("range") or STRETCH_RANGE
            if r is not None and lo <= r <= hi and abs(r - 1.0) >= 0.01:
                x = A.stretch_map(x, [0.0, keep, keep + body], [0.0, keep, keep + body * r], sr)
                ratio = round(r, 3)
        info.update({"method": kind, "ratio": ratio})
    info["stretched"] = kind == "words" or ratio is not None
    y = np.concatenate([np.zeros(int(round(pad * sr)), np.float32), np.asarray(x, np.float32)])
    return y, info


def ending_plan(an, row, a, b, speed=1.0, mode="tts", target_s=None):
    """How an ending's take is timed: word by word onto the original's onsets (at another pace the gaps
    scaled by 1/speed); with the user's recording (convert) one stretch toward its length."""
    words = an.tl["words"]
    ws = words[row["patch_from_idx"]: row["end_idx"] + 1]
    O = [o - a for o in _onsets(an.x, ws, a + 0.003)]
    lead = O[0]
    dst = [lead + (o - lead) / speed for o in O] + [lead + ((b - a) - lead) / speed]
    return {"kind": "uniform" if mode == "convert" else "words", "lead": lead, "dst": dst, "uniform": True,
            "range": STRETCH_RANGE, "target_s": target_s if target_s is not None else (b - a) / speed}


def passage_plan(an, cand, a, b, rewrite=False, user=None, target_s=None):
    """How a passage's take is timed: its first word on the original's first onset, long pauses shortened
    to the speaker's usual pause; said cleanly (same words) or after the user's recording also one gentle
    stretch toward the target length (PASSAGE_STRETCH_RANGE); a rewrite keeps its own length."""
    s = float(cand["start"])
    lead = A.word_onset(an.x, s, a + 0.003, s + 0.08)[0] - a
    return {"kind": "passage", "lead": lead, "max_pause": typical_pause(an.tl["words"]),
            "uniform": bool(user) or not rewrite, "range": PASSAGE_STRETCH_RANGE,
            "target_s": target_s if target_s is not None else (b - a)}


# ---- reference ---------------------------------------------------------------------------------
def pick_reference(an, rows, target_row, min_s=5.0, max_s=14.0, shape="fall", info=None):
    """6-14 s of the same timeline for VoiceStudio to clone the voice from; the clone copies the
    reference's delivery, so its last sentence steers the take's ending:
      fall  the nearest stretch ending on a sentence whose pitch falls (pushes the take toward finality)
      rise  the nearest stretch ending on a rising sentence (questions included)
      asis  the passage around the target itself (no shape preference): the take sounds like the recording
    When no stretch has the wanted ending, the nearest usable one is taken; info (a dict, optional)
    gets {"wanted", "used", "fallback", "end_idx"}."""
    words = an.tl["words"]
    tgt_s, tgt_e = target_row["start_idx"], target_row["end_idx"]

    def start_for(e, overlap_ok=False):
        i = e
        while i > 0 and words[e]["end"] - words[i - 1]["start"] <= max_s:
            i -= 1
        if words[e]["end"] - words[i]["start"] < min_s:
            return None
        if not overlap_ok and i <= tgt_e and e >= tgt_s:      # overlaps the sentence being fixed
            return None
        return i

    def clip(i, e, used):
        if info is not None:
            info.update({"wanted": shape, "used": used, "fallback": used != shape, "end_idx": e})
        t0, t1 = words[i]["start"] - 0.05, words[e]["end"] + 0.15
        return A.SR, an.x[int(t0 * A.SR): int(t1 * A.SR)], " ".join(w["text"] for w in words[i:e + 1]), (t0, t1)

    ends = sorted({e for _s, e in sentences(words)} | {r["end_idx"] for r in rows}, key=lambda e: abs(e - tgt_e))
    if shape == "asis":
        for e in [tgt_e] + [e for e in ends if e != tgt_e]:
            i = start_for(e, overlap_ok=True)
            if i is not None:
                return clip(i, e, "asis")
    else:
        pool = rows
        if shape == "rise":      # rising endings are mostly questions, which detect leaves out by default
            try:
                pool = detect(an, include_questions=True)
            except Exception:
                pool = rows
        want = SHAPE_OF.get(shape, "falling")
        cands = sorted((r for r in pool if r.get("shape") == want and r["end_idx"] != tgt_e),
                       key=lambda r: abs(r["end_idx"] - tgt_e))
        for r in cands:
            i = start_for(r["end_idx"])
            if i is not None:
                return clip(i, r["end_idx"], shape)
        for e in ends:           # nothing ends that way: the nearest stretch that doesn't overlap
            i = start_for(e)
            if i is not None:
                return clip(i, e, "nearest")
    raise PatchError("No usable voice sample: the timeline has no 5 s stretch of speech to clone the voice from.")


# ---- patching ----------------------------------------------------------------------------------
def original_span(an, row):
    words = an.tl["words"]
    a_idx, e = row["patch_from_idx"], row["end_idx"]
    nxt = words[e + 1]["start"] - 0.02 if e + 1 < len(words) else len(an.x) / A.SR
    a = span_start(an.x, words[a_idx]["start"], words[a_idx - 1]["end"] if a_idx > 0 else None)
    b = decay_end(an.x, words[e]["end"], min(nxt, words[e]["end"] + 0.25))
    return a, b


def prepare_patch(take, take_words, target, orig_len, language_hint=None):
    """Cut the target words out of a take; returns (patch, info) or (None, reason). info["_tm"] is the
    words' timing in the cut (for _time_fit; not written to the report)."""
    run = _find_run(take_words, target)
    if run is None:
        heard = " ".join(w["text"] for w in take_words)
        return None, f"target words not found in the take (heard: {heard!r})"
    i0, match, sims = run
    s, e, tm = _take_cut(take, take_words, i0, i0 + len(target) - 1)
    tm["sims"] = [round(float(v), 2) for v in sims]
    patch = take[int(s * A.SR): int(e * A.SR)]
    if len(patch) < int(0.1 * A.SR):
        return None, "patch too short"
    t, f = A.f0_track(take)
    vm = f[~np.isnan(f)]
    contour = A.ending_contour(t, f, s, e, float(np.median(vm))) if len(vm) > 10 else None
    ratio = orig_len / (len(patch) / A.SR)
    return patch, {"match": round(float(match), 2), "take_span": [round(s, 3), round(e, 3)], "contour": contour,
                   "duration_ratio": round(ratio, 3), "_tm": tm}


def shape_score(contour, want="fall", orig_delta=None):
    """How well a take's ending has the wanted shape (higher is better)."""
    c = contour or {"delta_st": 0.0, "shape": "flat"}
    if want == "rise":
        return c["delta_st"] + (3 if c["shape"] == "rising" else 0)
    if want == "asis":
        return -1.5 * abs(c["delta_st"] - orig_delta) if orig_delta is not None else 0.0
    return -c["delta_st"] + (3 if c["shape"] == "falling" else 0)


def score_take(info, want="fall", speed=1.0, orig_delta=None):
    """An ending's take: the wanted ending shape, a length close to the original's at the chosen pace
    (original length / speed), the words said. score_take(info) is the original rule (falls, natural pace)."""
    dur = 4 * abs(np.log(info["duration_ratio"] / speed))
    return shape_score(info["contour"], want, orig_delta) - dur + 2 * info["match"]


def score_convert(info, user):
    """A take in "use my recording" mode: its melody and length closest to the user's recording."""
    m = info.get("melody") or {"corr": 0.0, "dtw_st": 3.0}
    patch_s = info["take_span"][1] - info["take_span"][0]
    return (2 * m["corr"] - 0.5 * m["dtw_st"] - 4 * abs(np.log(user["duration_s"] / max(patch_s, 0.05)))
            + 2 * info["match"])


def pitch_match(an, patch, a, b, offset_st=0.0):
    """Semitones to move the take so it sits at the speaker's pitch around [a, b] (voiced frames of the
    recording 4 s either side), clamped to +-PITCH_MATCH_MAX_ST, 0 under PITCH_MATCH_MIN_ST; plus the
    user's offset. Returns (shift_st, info)."""
    spk = an.local_median(a - 4.0, b + 4.0)
    take = A.median_f0(patch)
    match = 0.0
    if spk and take:
        match = float(np.clip(12 * np.log2(spk / take), -PITCH_MATCH_MAX_ST, PITCH_MATCH_MAX_ST))
        if abs(match) < PITCH_MATCH_MIN_ST:
            match = 0.0
    total = round(match + float(offset_st or 0.0), 2)
    return total, {"speaker_hz": round(spk, 1) if spk else None, "take_hz": round(take, 1) if take else None,
                   "match_st": round(match, 2), "offset_st": float(offset_st or 0.0), "shift_st": total}


def tone_reference(an, a, b):
    """(speech, noise) the take's tone is matched to: the original span and 1.5 s either side, and a room
    tone bed of the same length (its noise floor is taken out of the comparison)."""
    ref = an.x[int(max(0.0, a - 1.5) * A.SR): int((b + 1.5) * A.SR)]
    return ref, A.room_tone(an.x, an.tl["silences"], len(ref))


def fit_and_splice(an, row, patch, a, b, stretch_range=STRETCH_RANGE, target_s=None, sound="room", tone_ref=None,
                   timed=False):
    """Fit a take into [a, b]: stretched toward target_s (default the original length; at another pace
    original / speed, so the pace isn't undone) when within stretch_range, tone matched (sound="room":
    EQ toward the recording + room tone under it; "clean": neither), loudness matched, spliced.
    timed=True: the patch is already laid out on the span's clock (_time_fit): no stretch and no splice
    (the caller builds the preview); out is None."""
    orig = an.x[int(a * A.SR): int(b * A.SR)]
    target = (b - a) if target_s is None else target_s
    ratio = target / (len(patch) / A.SR)
    stretched = (not timed) and stretch_range[0] <= ratio <= stretch_range[1]
    if stretched:
        patch = A.stretch(patch, ratio)
    eq = None
    if sound == "room":
        ref, noise = tone_ref or tone_reference(an, a, b)
        patch, eq = A.eq_match(patch, ref, noise, max_db=EQ_MAX_DB)
    gain = A.rms(orig) / max(A.rms(patch), 1e-6)
    patch = patch * min(gain, 8.0)
    if sound == "room":
        patch = patch + A.room_tone(an.x, an.tl["silences"], len(patch)) * 0.8
    out = None if timed else A.splice(an.x, a, b, patch.astype(np.float32))
    return out, {"stretched": stretched, "gain_db": round(A.db(gain), 1),
                 "length_change_ms": int(round((len(patch) / A.SR - (b - a)) * 1000)),
                 "patch_s": round(len(patch) / A.SR, 3), "eq": eq, "room_tone": sound == "room"}, patch


def measure_after(out, a, new_b):
    t, f = A.f0_track(out[int(max(0, a - 10) * A.SR): int((new_b + 0.5) * A.SR)])
    off = max(0, a - 10)
    vm = f[~np.isnan(f)]
    if len(vm) < 10:
        return None
    return A.ending_contour(t + off, f, a, new_b, float(np.median(vm)))


def provenance(vs):
    try:
        eng = vs.tts_engine()
        lic = vs.model_licence(eng["model"])
    except VoiceStudioError:
        eng, lic = {}, None
    return {"app": "VoiceStudio", "url": VOICESTUDIO_URL, "api": vs.base, "engine": eng.get("id"),
            "model": eng.get("model"), "model_licence": lic, "watermark": vs.watermark_status()}


def ending_prompt(words, row, ending="fall"):
    """What VoiceStudio says for an ending: its sentence, ending with "." (fall), "?" (rise) or as
    transcribed (asis); a long sentence keeps its last 20 words."""
    sent = re.sub(r"\s+", " ", " ".join(w["text"] for w in words[row["start_idx"]: row["end_idx"] + 1])).strip()
    if ending != "asis":
        sent = re.sub(r"[,;:\-–—…\s]*$", "", sent.rstrip(".!?")) + ("?" if ending == "rise" else ".")
    if len(sent.split()) > 30:                       # keep the prompt to the end of the thought
        sent = " ".join(sent.split()[-20:])
    return sent


def user_target(take_path, language, asr_model, target=None, text=None):
    """The user's own recording as the melody target ("use my recording"): the part that says the target
    words (an ending's last words) or all the words (a passage), its length, pitch curve and ending.
    Returns {"duration_s", "curve", "contour", "want", "heard", "file"}."""
    x = A.load(take_path)
    if len(x) < int(0.2 * A.SR):
        raise PatchError("The recording is too short.")
    tw = transcribe_words(x, language, asr_model)
    heard = [w for w in tw if norm(w["text"])]
    loc = find_words(tw, target) if target else None
    if loc:
        s, e = loc[0], loc[1]
    elif heard:
        s, e = heard[0]["start"], heard[-1]["end"]
    else:                            # nothing recognised: the voiced part of the whole file
        t, f = A.f0_track(x)
        v = t[~np.isnan(f)]
        if len(v) < 10:
            raise PatchError("No speech was heard in the recording.")
        s, e = float(v[0]) - 0.03, float(v[-1]) + 0.03
    s = max(0.0, s - 0.03)
    e = decay_end(x, e, min(len(x) / A.SR, e + 0.3))
    track = A.f0_track(x)
    vm = track[1][~np.isnan(track[1])]
    ref = float(np.median(vm)) if len(vm) > 10 else None
    c0 = s if target else max(s, e - 0.6)
    contour = A.ending_contour(track[0], track[1], c0, e, ref) if ref else None
    shape = (contour or {}).get("shape")
    return {"duration_s": round(e - s, 3), "curve": A.melody_curve(x, s, e, track=track), "contour": contour,
            "want": {"falling": "fall", "rising": "rise"}.get(shape, "asis"),
            "heard": " ".join(w["text"] for w in heard), "file": os.path.basename(take_path), "span": [s, e]}


def _take_melody(take, pinfo, user):
    """pinfo["melody"]: how the patch part of a take compares with the user's recording."""
    s, e = pinfo["take_span"]
    curve = A.melody_curve(take, s, e)
    if curve is None or user.get("curve") is None:
        pinfo["melody"] = {"corr": 0.0, "dtw_st": 3.0}
    else:
        pinfo["melody"] = A.melody_similarity(curve, user["curve"])
    pinfo["user_ratio"] = round(user["duration_s"] / max(e - s, 0.05), 3)


def _report_delivery(dl, speed, ref_info, user):
    out = dict(dl, speed=speed, summary=delivery_summary(dl))
    if ref_info:
        out["reference_wanted"] = ref_info.get("wanted")
        out["reference_used"] = ref_info.get("used")
        out["reference_fallback"] = bool(ref_info.get("fallback"))
    if user:
        out["melody_from"] = "recording"
        out["ending_from_recording"] = user["want"]
    return out


def patch_one(an, rows, row, vs, out_dir, mode="tts", take_path=None, takes=3, asr_model="large-v3",
              vs_language=None, profile_cache=None, progress=None, text=None, delivery=None):
    """progress(text): optional, called before each slow step (the app shows it).
    row: an ending (core.detect: its last 1-3 words are replaced) or a passage (candidates.make_passage:
    the whole span is replaced by `text`, by default its words said cleanly).
    delivery: how the patch is said (DELIVERY_DEFAULT when None; see norm_delivery).
    mode="convert": take_path is the user's recording of the words. VoiceStudio's /convert only transcribes
    a recording and says the text again (the delivery is lost), so the takes are generated from the words
    instead and the one whose melody and length are closest to the recording is kept."""
    dl = norm_delivery(delivery)
    if row.get("kind") == "passage":
        return patch_passage(an, rows, row, vs, out_dir, text=text, mode=mode, take_path=take_path, takes=takes,
                             asr_model=asr_model, vs_language=vs_language, profile_cache=profile_cache,
                             progress=progress, delivery=dl)
    say = progress or (lambda _t: None)
    words = an.tl["words"]
    target = [w["text"] for w in words[row["patch_from_idx"]: row["end_idx"] + 1]]
    a, b = original_span(an, row)
    d = os.path.join(out_dir, f"ending_{row['end_id']}")
    os.makedirs(d, exist_ok=True)
    lang = an.tl["language"]
    user = None
    if mode == "convert":
        if not take_path:
            raise PatchError("A recording of the words is needed for this mode.")
        say("listening to your recording")
        user = user_target(take_path, lang, asr_model, target=target)
    want = user["want"] if user else dl["ending"]
    ref_info = {}
    sr, ref, ref_text, ref_span = pick_reference(an, rows, row, shape=want, info=ref_info)
    ref_path = os.path.join(d, "reference.wav")
    A.save(ref_path, ref)

    speed = delivery_speed(dl)
    sent = ending_prompt(words, row, want)
    candidates = []
    for k in range(takes):
        seed = 1000 + k * 7919
        say(f"take {k + 1} of {takes}")
        wav, info = vs.generate(sent, ref_path, ref_text=ref_text, language=vs_language, seed=seed,
                                speed=speed if speed != 1.0 else None)
        take = A.load(wav)
        A.save(os.path.join(d, f"take_{k + 1}.wav"), take)
        tw = transcribe_words(take, lang, asr_model)
        patch, pinfo = prepare_patch(take, tw, target, b - a)
        tm = pinfo.pop("_tm", None) if patch is not None else None
        if patch is not None and user:
            _take_melody(take, pinfo, user)
        candidates.append({"take": k + 1, "seed": info.get("seed") or seed, "patch": patch, "tm": tm,
                           "info": pinfo if patch is not None else {"error": pinfo}})

    dl_rep = _report_delivery(dl, speed, ref_info, user)
    good = [c for c in candidates if c["patch"] is not None]
    report = {"timeline": an.tl["name"], "end_id": row["end_id"], "sentence": row["sentence"],
              "replaced_words": " ".join(target), "original_span": [round(a, 3), round(b, 3)],
              "before": {k: row[k] for k in ("delta_st", "final_st", "shape")}, "mode": mode, "prompt": sent,
              "delivery": dl_rep,
              "reference": {"span": [round(v, 2) for v in ref_span], "text": ref_text, "ending": ref_info.get("used")},
              "takes": [{"take": c["take"], "seed": c.get("seed"), "delivery": dl_rep["summary"], **c["info"]}
                        for c in candidates],
              "generated_with": provenance(vs)}
    if user:
        report["recording"] = {k: user[k] for k in ("file", "duration_s", "contour", "want", "heard")}
    ctx0 = max(0.0, words[row["start_idx"]]["start"] - 1.0)
    A.save(os.path.join(d, "original.wav"), an.x[int(ctx0 * A.SR): int((b + 1.5) * A.SR)])
    if not good:
        report["result"] = "failed: no take contained the words"
        json.dump(report, open(os.path.join(d, "report.json"), "w"), indent=2)
        return report
    say("fitting the takes")
    if user:
        best = max(good, key=lambda c: score_convert(c["info"], user))
        target_s = user["duration_s"]
    else:
        best = max(good, key=lambda c: score_take(c["info"], want, speed, row.get("delta_st")))
        target_s = (b - a) / speed
    plan = ending_plan(an, row, a, b, speed, mode, target_s)
    _fit_takes(an, row, good, a, b, ctx0, d, report, STRETCH_RANGE, measure=True, delivery=dl, target_s=target_s,
               plan=plan, words_of=lambda y: patch_words(y, lang, asr_model, *recording_context(an.x, a, b)))
    report["result"] = "patched"
    report["best_take"] = best["take"]   # StoryCraft's pick (the user can choose another)
    return choose_take(d, best["take"], report=report)


# ---- passages (hard to understand, incoherent, tagged by the user) ---------------------------------
def passage_span(an, cand):
    """[a, b] seconds of the recording the passage's patch replaces: from just before its first word to
    the end of its last word's decay, never into the words around it."""
    end_of_audio = len(an.x) / A.SR
    a = span_start(an.x, float(cand["start"]), cand.get("prev_end"))
    limit = (cand["next_start"] - 0.02) if cand.get("next_start") is not None else end_of_audio
    b = decay_end(an.x, cand["end"], min(limit, cand["end"] + 0.25, end_of_audio))
    return max(0.0, a), max(b, cand["end"])


def passage_target_row(an, cand):
    """start_idx / end_idx over the timeline's word list (core indexes), for pick_reference."""
    words = an.tl["words"]
    inside = [i for i, w in enumerate(words) if w["end"] > cand["start"] - 0.01 and w["start"] < cand["end"] + 0.01]
    if not inside:
        k = min(range(len(words)), key=lambda i: abs(words[i]["start"] - cand["start"])) if words else 0
        inside = [k]
    return {"start_idx": inside[0], "end_idx": inside[-1]}


def _word_list(text):
    return [norm(t) for t in str(text or "").split() if norm(t)]


def prepare_whole(take, take_words, text, orig_len):
    """The whole take, from its first word to the decay of its last, when it says `text`; (patch, info)
    or (None, reason)."""
    heard = [w for w in take_words if norm(w["text"])]
    want = _word_list(text)
    if not heard or not want:
        return None, "nothing was heard in the take"
    sm = difflib.SequenceMatcher(None, [norm(w["text"]) for w in heard], want)
    match = sm.ratio()
    if match < 0.6:
        return None, f"the take didn't say the words (heard: {' '.join(w['text'] for w in heard)!r})"
    # words the take adds at its edges (a filler, an extra word: the text has nothing there) are left out
    i0, i1 = 0, len(heard) - 1
    ops = sm.get_opcodes()
    if ops and ops[0][0] == "delete":
        i0 = ops[0][2]
    if ops and ops[-1][0] == "delete":
        i1 = ops[-1][1] - 1
    while i0 < i1 and norm(heard[i0]["text"]) in FILLERS and norm(heard[i0]["text"]) not in want[:1]:
        i0 += 1
    while i1 > i0 and norm(heard[i1]["text"]) in FILLERS and norm(heard[i1]["text"]) not in want[-1:]:
        i1 -= 1
    if i1 < i0:
        i0, i1 = 0, len(heard) - 1
    s, e, tm = _take_cut(take, heard, i0, i1)
    patch = take[int(s * A.SR): int(e * A.SR)]
    if len(patch) < int(0.1 * A.SR):
        return None, "patch too short"
    t, f = A.f0_track(take)          # the take's ending (the last 0.6 s of its words), for the Ending choice
    vm = f[~np.isnan(f)]
    contour = (A.ending_contour(t, f, max(s, heard[i1]["end"] - 0.6), e, float(np.median(vm)))
               if len(vm) > 10 else None)
    info = {"match": round(float(match), 2), "take_span": [round(s, 3), round(e, 3)], "contour": contour,
            "duration_ratio": round(orig_len / (len(patch) / A.SR), 3), "_tm": tm}
    if i0 > 0 or i1 < len(heard) - 1:
        info["left_out"] = " ".join(w["text"] for w in heard[:i0] + heard[i1 + 1:])
    return patch, info


def passage_prompt(cand, text, ending="fall"):
    """What VoiceStudio says: the words as given, ending like the original passage (a full stop only
    where the passage finished its sentence); ending="rise" ends them with "?", "asis" leaves them as typed."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if ending == "asis":
        return t
    if ending == "rise":
        return re.sub(r"[.,;:!?…\-–—\s]*$", "", t) + "?"
    if cand.get("ends_sentence"):
        if not t.endswith((".", "?", "!")):
            t = re.sub(r"[,;:\-–—…\s]*$", "", t) + "."
    else:
        t = re.sub(r"[.,;:!?…\s]*$", "", t)
    return t


def passage_contour(an, cand):
    """The original passage's ending (its last 0.6 s), for Ending: as recorded."""
    ref = an.local_median(cand["start"] - 10, cand["end"])
    if not ref:
        return None
    return A.ending_contour(an.times, an.f0, max(cand["start"], cand["end"] - 0.6), cand["end"], ref)


def patch_passage(an, rows, cand, vs, out_dir, text=None, mode="tts", take_path=None, takes=3,
                  asr_model="large-v3", vs_language=None, profile_cache=None, progress=None, delivery=None):
    """Replace a whole passage: VoiceStudio says `text` (default: the passage's words said cleanly) in the
    speaker's voice, best of `takes`; with the user's recording (mode="convert") the take whose melody and
    length are closest to it. Writes the same files and report keys as an ending (original_span,
    replaced_words, result, patch.wav), so voice_patch.edit treats both alike; the report adds new_text,
    rewrite and patch_s."""
    dl = norm_delivery(delivery)
    say = progress or (lambda _t: None)
    text = (text or cand.get("clean") or cand.get("text") or "").strip()
    if not _word_list(text):
        raise PatchError("There are no words to say: type the words the patch should say.")
    a, b = passage_span(an, cand)
    d = os.path.join(out_dir, f"ending_{cand['id']}")
    os.makedirs(d, exist_ok=True)
    lang = an.tl["language"]
    user = None
    if mode == "convert":
        if not take_path:
            raise PatchError("A recording of the words is needed for this mode.")
        say("listening to your recording")
        user = user_target(take_path, lang, asr_model, text=text)
    want = user["want"] if user else dl["ending"]
    ref_info = {}
    sr, ref, ref_text, ref_span = pick_reference(an, rows, passage_target_row(an, cand), shape=want, info=ref_info)
    ref_path = os.path.join(d, "reference.wav")
    A.save(ref_path, ref)
    rewrite = _word_list(text) != _word_list(cand.get("clean") or cand.get("text"))
    speed = delivery_speed(dl)
    prompt = passage_prompt(cand, text, want)
    candidates = []
    for k in range(takes):
        seed = 1000 + k * 7919
        say(f"take {k + 1} of {takes}")
        wav, info = vs.generate(prompt, ref_path, ref_text=ref_text, language=vs_language, seed=seed,
                                speed=speed if speed != 1.0 else None)
        take = A.load(wav)
        A.save(os.path.join(d, f"take_{k + 1}.wav"), take)
        patch, pinfo = prepare_whole(take, transcribe_words(take, lang, asr_model), text, b - a)
        tm = pinfo.pop("_tm", None) if patch is not None else None
        if patch is not None and user:
            _take_melody(take, pinfo, user)
        candidates.append({"take": k + 1, "seed": info.get("seed") or seed, "patch": patch, "tm": tm,
                           "info": pinfo if patch is not None else {"error": pinfo}})

    dl_rep = _report_delivery(dl, speed, ref_info, user)
    good = [c for c in candidates if c["patch"] is not None]
    report = {"timeline": an.tl["name"], "end_id": cand["id"], "kind": "passage", "group": cand.get("group"),
              "sentence": cand.get("text", ""), "replaced_words": cand.get("text", ""), "new_text": text,
              "rewrite": rewrite, "original_span": [round(a, 3), round(b, 3)], "before": {}, "mode": mode,
              "prompt": prompt, "delivery": dl_rep,
              "reference": {"span": [round(v, 2) for v in ref_span], "text": ref_text, "ending": ref_info.get("used")},
              "takes": [{"take": c["take"], "seed": c.get("seed"), "delivery": dl_rep["summary"], **c["info"]}
                        for c in candidates],
              "generated_with": provenance(vs)}
    if user:
        report["recording"] = {k: user[k] for k in ("file", "duration_s", "contour", "want", "heard")}
    ctx0 = max(0.0, cand["start"] - 1.0)
    A.save(os.path.join(d, "original.wav"), an.x[int(ctx0 * A.SR): int((b + 1.5) * A.SR)])
    if not good:
        report["result"] = "failed: no take said the words"
        json.dump(report, open(os.path.join(d, "report.json"), "w"), indent=2)
        return report
    say("fitting the takes")
    orig_c = passage_contour(an, cand) if want == "asis" else None

    def score(c):
        i = c["info"]
        if user:
            return score_convert(i, user)
        s = 2 * i["match"] - (0.0 if rewrite else 2 * abs(np.log(i["duration_ratio"] / speed)))
        if want != "fall":       # Falls keeps the original rule: the clearest take
            s += shape_score(i.get("contour"), want, (orig_c or {}).get("delta_st"))
        return s
    best = max(good, key=score)
    target_s = user["duration_s"] if user else (b - a) / speed
    plan = passage_plan(an, cand, a, b, rewrite=rewrite, user=user, target_s=target_s)
    _fit_takes(an, cand, good, a, b, ctx0, d, report, PASSAGE_STRETCH_RANGE, measure=False, delivery=dl,
               target_s=target_s, plan=plan,
               words_of=lambda y: patch_words(y, lang, asr_model, *recording_context(an.x, a, b)))
    report["result"] = "patched"
    report["best_take"] = best["take"]   # StoryCraft's pick (the user can choose another)
    return choose_take(d, best["take"], report=report)


# ---- every usable take is fitted, so the user can pick another one -------------------------------
def _fit_takes(an, row, good, a, b, ctx0, d, report, stretch_range, measure, delivery=None, target_s=None,
               plan=None, words_of=None):
    """For each take that said the words: take_<n>_patch.wav (fitted, ready for Resolve), take_<n>_preview.wav
    (the span exactly as the story plays it: the recording at its edges, crossfaded into the patch) and
    take_<n>_patched.wav (in context); its fit goes in report["takes"]. Each take is first moved to the
    speaker's pitch (plus the delivery's offset, TD-PSOLA), then laid out on the span's clock (_time_fit:
    word timing, plan from ending_plan / passage_plan) and fitted (fit_and_splice).
    words_of(audio): the words the take's file that plays says, with times (patch_words): the transcript
    follows the patch's rhythm with them (voice_patch/transcript.py); None leaves them out."""
    dl = norm_delivery(delivery)
    by_take = {t["take"]: t for t in report["takes"]}
    tone_ref = tone_reference(an, a, b) if dl["sound"] == "room" else None
    for c in good:
        n = c["take"]
        A.save(os.path.join(d, f"take_{n}_words.wav"), c["patch"])
        shift, pinfo = pitch_match(an, c["patch"], a, b, dl["pitch_st"])
        words_audio = A.pitch_shift(c["patch"], shift) if abs(shift) >= 0.05 else c["patch"]
        extra = {}
        if plan is not None and c.get("tm"):
            timed, tinfo = _time_fit(words_audio, c["tm"], dict(plan, range=plan.get("range") or stretch_range))
            _o, fit, fitted = fit_and_splice(an, row, timed, a, b, stretch_range=stretch_range, target_s=target_s,
                                             sound=dl["sound"], tone_ref=tone_ref, timed=True)
            fit["stretched"] = bool(tinfo.get("stretched"))
            fit["timing"] = tinfo
            preview, pv = A.edge_preview(an.x, a, b, fitted, plan["lead"])
            fit["edges"] = pv
            A.save(os.path.join(d, f"take_{n}_preview.wav"), preview)
            extra["preview"] = {"file": f"take_{n}_preview.wav", "len_s": round(len(preview) / A.SR, 3)}
            ia, ib = int(round(a * A.SR)), int(round(b * A.SR))
            out = np.concatenate([an.x[:ia], preview, an.x[ib:]])
        else:
            out, fit, fitted = fit_and_splice(an, row, words_audio, a, b, stretch_range=stretch_range,
                                              target_s=target_s, sound=dl["sound"], tone_ref=tone_ref)
        fit["pitch"] = pinfo
        A.save(os.path.join(d, f"take_{n}_patch.wav"), fitted)
        new_b = b + fit["length_change_ms"] / 1000
        A.save(os.path.join(d, f"take_{n}_patched.wav"), out[int(ctx0 * A.SR): int((new_b + 1.5) * A.SR)])
        if words_of is not None:          # what plays: the preview when there is one (the player's choice)
            try:
                extra["patch_words"] = words_of(preview if "preview" in extra else fitted)
                extra["patch_words_file"] = "preview" if "preview" in extra else "patch"
            except Exception as exc:      # the patch still works: its words keep the recording's times
                print(f"[voice patch] take {n}: words not read: {exc}")
        by_take[n].update({"ready": True, "fit": fit, "patch_s": fit["patch_s"],
                           "after": measure_after(out, a, new_b) if measure else None, **extra})


def choose_take(d, n, report=None):
    """Make take n the patch (patch.wav, preview.wav, patched.wav, take_words.wav and the report). Returns the
    report. A take without a preview (made before previews existed) leaves no preview.wav behind."""
    import shutil
    if report is None:
        with open(os.path.join(d, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    t = next((x for x in report.get("takes") or [] if x.get("take") == n and x.get("ready")), None)
    if t is None:
        raise PatchError(f"Take {n} can't be used.")
    for src, dst in (("patch", "patch.wav"), ("patched", "patched.wav"), ("words", "take_words.wav")):
        shutil.copyfile(os.path.join(d, f"take_{n}_{src}.wav"), os.path.join(d, dst))
    pv_src = os.path.join(d, f"take_{n}_preview.wav")
    if t.get("preview") and os.path.isfile(pv_src):
        shutil.copyfile(pv_src, os.path.join(d, "preview.wav"))
        report["preview"] = {"file": "preview.wav", "len_s": t["preview"]["len_s"]}
    else:
        report.pop("preview", None)
        try:
            os.remove(os.path.join(d, "preview.wav"))
        except OSError:
            pass
    report.update({"chosen_take": n, "fit": t["fit"], "patch_s": t["patch_s"], "after": t.get("after")})
    if t.get("patch_words"):              # the take's own words with their times (the transcript's timing)
        report["patch_words"], report["patch_words_file"] = t["patch_words"], t.get("patch_words_file", "patch")
    else:                                 # not read yet (an older take): voice_view reads them
        report.pop("patch_words", None)
        report.pop("patch_words_file", None)
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


# ---- output ------------------------------------------------------------------------------------
def write_index(out_dir, reports, tl_name):
    def lic_text(p):
        li = (p or {}).get("model_licence") or {}
        if isinstance(li, dict):
            return li.get("license") or li.get("label") or json.dumps(li)[:120]
        return str(li)
    gw = next((r["generated_with"] for r in reports if r.get("generated_with")), {})
    rows = []
    for r in reports:
        d = f"ending_{r['end_id']}"
        b, af = r.get("before") or {}, r.get("after") or {}
        rows.append(f"""<section><h2>{html.escape(r['sentence'])}</h2>
<p class="meta">Replaced: <b>{html.escape(r['replaced_words'])}</b> · mode {r['mode']} · {html.escape(r['result'])}
· pitch before {b.get('shape','?')} ({b.get('delta_st','?')} st) → after {af.get('shape','?')} ({af.get('delta_st','?')} st)
{('· length ' + format(r['fit']['length_change_ms'], '+d') + ' ms') if r.get('fit') else ''}
{('· delivery: ' + html.escape((r.get('delivery') or {}).get('summary') or '')) if (r.get('delivery') or {}).get('summary') else ''}</p>
<div class="ab"><label>Original<audio controls preload="none" src="{d}/original.wav"></audio></label>
{f'<label>Patched<audio controls preload="none" src="{d}/patched.wav"></audio></label>' if r['result'] == 'patched' else ''}</div></section>""")
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice patch test</title>
<style>:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#f2f2f7;--mut:#a1a1a6;--line:#2c2c2e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px}}h2{{font-size:16px;font-weight:600;margin:0 0 4px}}section{{border-top:1px solid var(--line);padding:16px 0}}
.meta,footer{{color:var(--mut);font-size:13px}}.ab{{display:flex;flex-wrap:wrap;gap:16px}}label{{display:grid;gap:4px;font-size:13px}}
audio{{width:min(380px,100%)}}</style>
<h1>Voice patch test · {html.escape(tl_name)}</h1>
<p class="meta">Listen to each pair. Patched audio is synthetic: it is not what the speaker recorded.</p>
{''.join(rows)}
<footer><p>Synthetic voice generated with <a href="{VOICESTUDIO_URL}">VoiceStudio</a> (AGPL-3.0, separate app)
· engine {html.escape(str(gw.get('engine')))} · model {html.escape(str(gw.get('model')))}
· model licence: {html.escape(lic_text(gw))}.</p></footer>"""
    open(os.path.join(out_dir, "index.html"), "w").write(page)


def print_rows(rows, limit):
    for r in rows[:limit]:
        flag = "UNFINISHED" if r["unfinished"] else "ok"
        print(f"  [{r['end_id']:>5}] {r['time']:>8.2f}s  {r['shape']:<7} Δ{r['delta_st']:+5.1f} st  "
              f"end {r['final_st']:+5.1f} st  {flag:<10} …{r['sentence'][-70:]}")
