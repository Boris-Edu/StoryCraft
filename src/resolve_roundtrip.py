#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: resolve_roundtrip.py
ROLE: A StoryCraft timeline edited by hand in Resolve, read back into the story
DESCRIPTION:
Send to Resolve flags every clip it lays (FLAG, a small coloured tab on the clip; clip colours stay
free for the marks). Reload reads the sent timeline's main audio track: each flagged clip plays a
stretch of the source media, found again in the source timeline, so it is a stretch of the
transcript's time. Those stretches become story blocks:
  - clips next to each other whose gap only holds words StoryCraft cut anyway (fillers, retakes,
    pauses) are one block, as on the story board;
  - a block is the sentences its kept words belong to; an edge trimmed inside a sentence becomes a
    word edge (start_t / end_t, the same override as Review's ± Word);
  - clips StoryCraft didn't lay (no flag, other media) are left alone and reported.
Native Resolve API only (TimelineItem.GetSourceStartFrame / GetFlagList / AddFlag).
"""

FLAG = "Cyan"
EDGE_SLACK = 0.06      # an edge this close to its sentence's own edge is the sentence's edge


# ---- Resolve side ---------------------------------------------------------------------------------
def _find_timeline(project, name):
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName() == name:
            return tl
    return None


def _src_in(item):
    try:
        v = item.GetSourceStartFrame()
        if v is not None:
            return int(v)
    except Exception:
        pass
    return int(item.GetLeftOffset())


def _media_key(item):
    mpi = item.GetMediaPoolItem()
    if mpi is None:
        return None
    try:
        return mpi.GetUniqueId()
    except Exception:
        try:
            return mpi.GetClipProperty("File Path") or mpi.GetName()
        except Exception:
            return None


def track_clips(timeline, track_type="audio", track=1):
    """[{key, start, duration, src_in, flags}] of one track, start in frames from the timeline's start."""
    t0 = int(timeline.GetStartFrame())
    out = []
    for it in timeline.GetItemListInTrack(track_type, track) or []:
        try:
            flags = list(it.GetFlagList() or [])
        except Exception:
            flags = []
        out.append({"key": _media_key(it), "start": int(it.GetStart()) - t0, "duration": int(it.GetDuration()),
                    "src_in": _src_in(it), "flags": flags, "name": (it.GetName() if hasattr(it, "GetName") else "")})
    return sorted(out, key=lambda c: c["start"])


def flag_timeline(timeline, color=FLAG):
    """Flag every clip of the (just sent) timeline as StoryCraft's. Returns how many were flagged."""
    n = 0
    for tt in ("video", "audio"):
        for ti in range(1, int(timeline.GetTrackCount(tt) or 0) + 1):
            for it in timeline.GetItemListInTrack(tt, ti) or []:
                try:
                    if color not in (it.GetFlagList() or []) and it.AddFlag(color):
                        n += 1
                except Exception:
                    pass
    return n


def read_segments(project, edit_tl_name, source_tl_name, fps, track_type="audio", track=1, source_track=1, flag=FLAG):
    """The edited timeline as stretches of the transcript: ([{t0, t1, rec}], outside, error).
    outside: clips left alone (not flagged by StoryCraft, or media that isn't in the source timeline)."""
    edit_tl = _find_timeline(project, edit_tl_name)
    src_tl = _find_timeline(project, source_tl_name)
    if edit_tl is None:
        return [], [], f"timeline '{edit_tl_name}' not found"
    if src_tl is None:
        return [], [], f"source timeline '{source_tl_name}' not found"
    src = track_clips(src_tl, track_type, source_track)
    segs, outside = [], []
    for c in track_clips(edit_tl, track_type, track):
        if flag and flag not in c["flags"]:
            outside.append(c)
            continue
        m = next((s for s in src if s["key"] == c["key"] and s["src_in"] <= c["src_in"] < s["src_in"] + s["duration"]), None)
        if m is None:
            outside.append(c)
            continue
        t0 = (m["start"] + (c["src_in"] - m["src_in"])) / float(fps)
        segs.append({"t0": t0, "t1": t0 + c["duration"] / float(fps), "rec": c["start"] / float(fps)})
    return segs, outside, None


# ---- story side (pure) ------------------------------------------------------------------------------
def _mid(w):
    return (float(w["start"]) + float(w["end"])) / 2.0


def segments_to_story(segments, sentences, words, is_cut=lambda i: False):
    """Story blocks from the edited timeline's stretches (in play order).
    Returns (edit, air, report): edit [(first, last)], air {"f-l": {start_t?, end_t?}},
    report {"blocks", "merged", "duplicates", "empty"}."""
    spoken = [(i, w) for i, w in enumerate(words) if w.get("type") == "word" and w.get("start") is not None
              and w.get("end") is not None and str(w.get("text", "")).strip()]
    spans = [(float(s.get("src_start", 0)), float(s.get("src_end", 0))) for s in sentences]

    def sent_of(t):
        for k, (a, b) in enumerate(spans):
            if a - 0.02 <= t <= b + 0.02:
                return k
        return None

    # 1. merge neighbours whose gap only holds words StoryCraft cuts anyway
    groups, merged = [], 0
    for seg in segments:
        if groups:
            g = groups[-1]
            if seg["t0"] >= g["t1"] - 0.05:
                gap = [i for i, w in spoken if g["t1"] - 0.01 < _mid(w) < seg["t0"] + 0.01]
                if all(is_cut(i) for i in gap):
                    g["t1"] = max(g["t1"], seg["t1"])
                    merged += 1
                    continue
        groups.append({"t0": seg["t0"], "t1": seg["t1"]})
    # 2. each group: the sentences of its kept words, word edges where it starts/ends inside one
    edit, air, used, dups, empty = [], {}, set(), 0, 0
    for g in groups:
        inside = [(i, w) for i, w in spoken if g["t0"] - 0.01 <= _mid(w) <= g["t1"] + 0.01 and not is_cut(i)]
        ks = [k for k in (sent_of(_mid(w)) for _i, w in inside) if k is not None]
        if not ks:
            empty += 1
            continue
        f, l = min(ks), max(ks)
        if any(k in used for k in range(f, l + 1)):
            dups += 1      # the same words twice in the edit: the story board holds a sentence once
            continue
        used.update(range(f, l + 1))
        edit.append((f, l))
        edge = {}
        first_w, last_w = inside[0][1], inside[-1][1]
        if float(first_w["start"]) > spans[f][0] + EDGE_SLACK:
            edge["start_t"] = round(float(first_w["start"]), 3)
        if float(last_w["end"]) < spans[l][1] - EDGE_SLACK:
            edge["end_t"] = round(float(last_w["end"]), 3)
        if edge:
            air[f"{f}-{l}"] = edge
    return edit, air, {"blocks": len(edit), "merged": merged, "duplicates": dups, "empty": empty}


def unused_for(edit, n_sentences):
    """Everything not in the story, as runs of consecutive sentences (Available / Not used)."""
    used = {k for f, l in edit for k in range(f, l + 1)}
    out, cur = [], None
    for k in range(n_sentences):
        if k in used:
            if cur:
                out.append(tuple(cur))
                cur = None
        elif cur is None:
            cur = [k, k]
        else:
            cur[1] = k
    if cur:
        out.append(tuple(cur))
    return out
