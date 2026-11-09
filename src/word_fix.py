#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: word_fix.py
ROLE: Correct a transcript passage by hand
DESCRIPTION:
The user types what is actually said over a selected passage. The typed words are lined up with the
old ones (difflib on the bare words): a word that didn't change keeps its own timing; new words share
the time of the words they replace, in proportion to their length. Every moment of the passage stays
covered by a word, so the cuts built from the transcript don't change: only the text does.
"""
import re
from difflib import SequenceMatcher

_MIN_WORD_S = 0.06   # a new word squeezed into a gap gets at least this much time


def _bare(text):
    return re.sub(r"[^\w']", "", str(text or "").lower())


def spoken_text(words):
    """The passage as the user reads it (silence markers left out)."""
    return " ".join(str(w.get("text", "")).strip() for w in words if w.get("type") != "silence").strip()


def _new_word(template, text, start, end):
    w = {k: v for k, v in template.items()
         if k in ("type", "spk", "seg_start", "seg_end", "status", "manual_status", "is_auto")}
    w.update({"type": "word", "text": text, "start": round(start, 3), "end": round(end, 3),
              "anchor_start": round(start, 3), "prob": 1.0, "is_filler": False, "ai_mark": None,
              "selected": False, "is_segment_start": False, "user_text": True})
    return w


def _spread(tokens, t0, t1, template, silences=()):
    """tokens over [t0, t1], each in proportion to its length, back to back. Silences inside [t0, t1]
    stay free: the words go on the speech around them (a stretch of speech that would get no word is
    joined to the one before it, covering that silence)."""
    chunks, t = [], t0
    for s0, s1 in sorted(silences):
        if s0 > t + 0.001 and s1 < t1 - 0.001:
            chunks.append([t, s0])
            t = s1
    chunks.append([t, t1])
    weights = [len(_bare(tok)) + 1 for tok in tokens]
    total_w = float(sum(weights)) or 1.0
    speech = sum(b - a for a, b in chunks) or 1.0
    # each token goes to the chunk holding the middle of its share of the speech time
    edges, acc = [], 0.0
    for a, b in chunks:
        acc += (b - a) / speech
        edges.append(acc)
    groups = [[] for _ in chunks]
    cum = 0.0
    for tok, wgt in zip(tokens, weights):
        mid = (cum + wgt / 2.0) / total_w
        cum += wgt
        groups[next((i for i, e in enumerate(edges) if mid <= e + 1e-9), len(chunks) - 1)].append(tok)
    merged = []
    for (a, b), g in zip(chunks, groups):
        if not g and merged:
            merged[-1][1] = b          # no word here: the chunk before covers it
        elif merged and not merged[-1][2]:
            merged[-1][1], merged[-1][2] = b, g
        else:
            merged.append([a, b, g])
    out = []
    for a, b, g in merged:
        if not g:
            continue
        wsum = float(sum(len(_bare(x)) + 1 for x in g)) or 1.0
        t = a
        for tok in g:
            t_end = t + (b - a) * (len(_bare(tok)) + 1) / wsum
            out.append(_new_word(template, tok, t, t_end))
            t = t_end
        out[-1]["end"] = round(b, 3)
    return out


def correct_passage(segment, new_text):
    """segment: the word dicts of the selected passage (silences included), in order. Returns the new
    list to put in its place. Raises ValueError when new_text has no words or the passage has none."""
    tokens = str(new_text or "").split()
    if not tokens:
        raise ValueError("no words")
    spoken = [w for w in segment if w.get("type") != "silence"]
    if not spoken:
        raise ValueError("no spoken words in the passage")
    seg_t0, seg_t1 = float(spoken[0].get("start", 0.0)), float(spoken[-1].get("end", 0.0))

    sm = SequenceMatcher(None, [_bare(w.get("text")) for w in spoken], [_bare(t) for t in tokens], autojunk=False)
    # anchors: typed token j keeps old word i (same bare word)
    anchor = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                anchor[j1 + k] = i1 + k

    out = []
    j = 0
    while j < len(tokens):
        if j in anchor:
            w = dict(spoken[anchor[j]])
            w["text"] = tokens[j]
            if w["text"] != spoken[anchor[j]].get("text"):
                w["user_text"] = True
            out.append(w)
            j += 1
            continue
        # a run of new tokens: the time between the previous kept word and the next one
        k = j
        while k < len(tokens) and k not in anchor:
            k += 1
        run = tokens[j:k]
        prev_old = anchor[j - 1] if j > 0 else None
        next_old = anchor[k] if k < len(tokens) else None
        t0 = float(spoken[prev_old]["end"]) if prev_old is not None else seg_t0
        t1 = float(spoken[next_old]["start"]) if next_old is not None else seg_t1
        first_replaced = (prev_old + 1) if prev_old is not None else 0
        template = spoken[min(first_replaced, len(spoken) - 1)]
        if t1 - t0 < _MIN_WORD_S * len(run):
            # no room (a word inserted between two that touch): share the previous kept word's time,
            # or the next one's when the run starts the passage
            if out:
                host = out.pop()
                run = [host["text"]] + run
                t0, template = float(host["start"]), host
            elif next_old is not None:
                run = run + [tokens[k]]
                t1 = float(spoken[next_old]["end"])
                k += 1
        sil = [(float(x["start"]), float(x["end"])) for x in segment if x.get("type") == "silence"]
        out.extend(_spread(run, t0, max(t1, t0 + 0.001), template, sil))   # never past the passage
        j = k

    # Old words dropped with nothing typed in their place: the word before takes their time
    for a, b in zip(out, out[1:]):
        if float(b["start"]) - float(a["end"]) > 0.001:
            gap_words = [w for w in spoken if float(w["start"]) >= float(a["end"]) - 0.001
                         and float(w["end"]) <= float(b["start"]) + 0.001]
            if gap_words:
                a["end"] = b["start"]
    if out:
        out[0]["start"] = min(float(out[0]["start"]), seg_t0)
        out[-1]["end"] = max(float(out[-1]["end"]), seg_t1)
        out[0]["is_segment_start"] = bool(spoken[0].get("is_segment_start"))

    # Silences of the passage stay where no word now is
    for s in (w for w in segment if w.get("type") == "silence"):
        s0, s1 = float(s.get("start", 0)), float(s.get("end", 0))
        if not any(float(w["start"]) < s1 - 0.001 and float(w["end"]) > s0 + 0.001 for w in out):
            out.append(dict(s))
    out.sort(key=lambda w: (float(w.get("start", 0)), 0 if w.get("type") != "silence" else 1))
    return out
