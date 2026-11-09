#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: voice_patch/candidates.py
ROLE: What could be patched, besides the endings that sound unfinished (core.detect)
DESCRIPTION:
Passages of the transcript a voice patch could replace, in three groups. Each is computed from what
StoryCraft already knows, with no audio work of its own:
  hard        Hard to understand: a sentence whose clarity (engine.clarity: Whisper's confidence and
              how strongly the voice comes through) is below HARD_CLARITY, or that touches a sound
              without words (an inaudible fragment inside it or within INAUDIBLE_NEAR_S of it).
  incoherent  Incoherent: a sentence with a restart or a repeated word (algorithms.find_repeats, plus
              words already marked as stammers), a word broken off ("I...", "the—"), or heavy filler
              (at least FILLERS_MIN fillers, or FILLER_SHARE of its words). Restarts and broken words
              count 2, each filler 1; the sentence is listed at INCOHERENT_MIN or more.
  tagged      Tagged by you: runs of words carrying the "To voice patch" marker. Words that are cut
              (Error, Retake...) or silences between two marked words don't break a run.
A passage is replaced as a whole: by default VoiceStudio says its words again cleanly (clean_text:
fillers and abandoned copies dropped), and the user can rewrite them first.

Passage ids ("p<first word id>-<last word id>") name the patch folder (ending_<id>, like the endings,
so voice_patch.edit reads both the same way).
"""
import re

HARD_CLARITY = 55          # clarity below this (0-100) reads as hard to understand
INAUDIBLE_NEAR_S = 0.5     # a sound without words this close to a sentence makes it hard to follow
FILLERS_MIN = 2
FILLER_SHARE = 0.2
INCOHERENT_MIN = 2
MAX_PASSAGE_WORDS = 40     # longer sentences are left alone (a patch that long would be a re-recording)
CUT_STATUSES = {"bad", "repeat", "typo"}
GROUPS = ("endings", "hard", "incoherent", "tagged")

_BROKEN = re.compile(r"(\.\.\.|…|[-–—])[\"')\]]*$")
_END = (".", "?", "!")
_ABBR = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "prof.", "mt.", "vs.", "etc."}


def norm(t):
    return re.sub(r"[^\w']+", "", str(t or "").lower())


def _spoken(w):
    return (w.get("type") == "word" and not w.get("is_inaudible") and w.get("start") is not None
            and w.get("end") is not None)


def _is_inaudible(w):
    return w.get("type") == "inaudible" or bool(w.get("is_inaudible"))


def sentences(words_data):
    """[[raw indices of the spoken words of one sentence], ...] over the whole transcript (cut words included:
    a patch replaces everything that was said there)."""
    out, cur = [], []
    for i, w in enumerate(words_data or []):
        if not _spoken(w):
            continue
        cur.append(i)
        t = str(w.get("text", "")).strip()
        if t.endswith(_END) and not _BROKEN.search(t) and t.lower() not in _ABBR:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def clean_text(words_data, idxs, fillers=(), repeats=()):
    """The passage's words said cleanly: fillers, abandoned copies (restarts, stammers) and broken-off
    words dropped, immediate repeats collapsed, a capital at the start and the sentence's own end."""
    fill = {norm(f) for f in fillers or ()}
    rep = set(repeats or ())
    spoken = [i for i in idxs if str(words_data[i].get("text", "")).strip()]
    dropped_rep = [i for i in spoken if i in rep or words_data[i].get("is_stammer")]
    if len(dropped_rep) * 2 > len(spoken):
        rep = set()     # the whole passage is a retake of something said elsewhere: keep its words
    kept, prev_dropped = [], None
    for i in idxs:
        w = words_data[i]
        t = str(w.get("text", "")).strip()
        if not t:
            continue
        if (rep and (i in rep or w.get("is_stammer"))) or w.get("is_filler") or norm(t) in fill:
            prev_dropped = t
            continue
        if _BROKEN.search(t) and i != idxs[-1]:
            prev_dropped = t
            continue
        if kept and norm(kept[-1]) == norm(t):
            kept[-1] = t
            continue
        if kept and prev_dropped and norm(prev_dropped) == norm(t) and t[:1].isupper() and prev_dropped[:1].islower():
            t = t[:1].lower() + t[1:]   # "for me because Because after" -> "for me because after"
        prev_dropped = None
        kept.append(t)
    if not kept:
        return ""
    text = " ".join(kept)
    text = re.sub(r"\s*(\.\.\.|…)\s*", " ", text).strip()
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = text[:1].upper() + text[1:]
    last = str(words_data[idxs[-1]].get("text", "")).strip()
    if last.endswith(_END) and not text.endswith(_END):
        text = text.rstrip(",;:-–— ") + last[-1]
    return text


def make_passage(words_data, idxs, group, reasons, score, fillers=(), repeats=()):
    first, last = words_data[idxs[0]], words_data[idxs[-1]]
    nxt = next((words_data[j] for j in range(idxs[-1] + 1, len(words_data)) if _spoken(words_data[j])), None)
    prv = next((words_data[j] for j in range(idxs[0] - 1, -1, -1) if _spoken(words_data[j])), None)
    return {"id": f"p{first.get('id', idxs[0])}-{last.get('id', idxs[-1])}", "kind": "passage", "group": group,
            "idx": list(idxs), "start": float(first["start"]), "end": float(last["end"]),
            "time": round(float(first["start"]), 2),
            "next_start": float(nxt["start"]) if nxt else None, "prev_end": float(prv["end"]) if prv else None,
            "text": " ".join(str(words_data[i].get("text", "")).strip() for i in idxs),
            "clean": clean_text(words_data, idxs, fillers, repeats),
            "ends_sentence": str(last.get("text", "")).strip().endswith(_END),
            "reasons": reasons, "score": round(float(score), 2)}


def hard_to_understand(words_data, scorer=None, sents=None, fillers=(), repeats=()):
    sents = sents if sents is not None else sentences(words_data)
    inaud = [(float(w.get("start") or 0), float(w.get("end") or 0)) for w in words_data or []
             if _is_inaudible(w) and w.get("start") is not None]
    out = []
    for idxs in sents:
        if len(idxs) > MAX_PASSAGE_WORDS:
            continue
        t0, t1 = float(words_data[idxs[0]]["start"]), float(words_data[idxs[-1]]["end"])
        reasons, score = [], 0.0
        clar = scorer(t0, t1) if scorer else None
        if clar is not None and clar < HARD_CLARITY:
            reasons.append({"code": "clarity", "value": int(clar)})
            score += (HARD_CLARITY - clar) / 10.0
        if any(a < t1 + INAUDIBLE_NEAR_S and b > t0 - INAUDIBLE_NEAR_S for a, b in inaud):
            reasons.append({"code": "inaudible"})
            score += 2.0
        if reasons:
            out.append(make_passage(words_data, idxs, "hard", reasons, score, fillers, repeats))
    return out


def incoherent(words_data, fillers=(), repeats=None, sents=None):
    """repeats: indices from algorithms.find_repeats (computed here when None)."""
    if repeats is None:
        try:
            import algorithms
            repeats = algorithms.find_repeats(words_data)
        except Exception:
            repeats = []
    rep = set(repeats)
    fill = {norm(f) for f in fillers or ()}
    sents = sents if sents is not None else sentences(words_data)
    out = []
    for idxs in sents:
        if len(idxs) > MAX_PASSAGE_WORDS:
            continue
        restarts = [i for i in idxs if i in rep or words_data[i].get("is_stammer")]
        broken = [i for i in idxs[:-1] if _BROKEN.search(str(words_data[i].get("text", "")).strip())]
        n_fill = sum(1 for i in idxs if words_data[i].get("is_filler") or norm(words_data[i].get("text")) in fill)
        # an adjacent repeat the finder missed ("I I", "the the")
        for a, b in zip(idxs, idxs[1:]):
            if a not in rep and norm(words_data[a].get("text")) and \
                    norm(words_data[a].get("text")) == norm(words_data[b].get("text")):
                restarts.append(a)
        reasons, score = [], 0.0
        if restarts:
            first = min(restarts)
            reasons.append({"code": "restart", "words": str(words_data[first].get("text", "")).strip()})
            score += 2.0 * min(2, len(set(restarts)))
        if broken:
            reasons.append({"code": "broken", "words": str(words_data[broken[0]].get("text", "")).strip()})
            score += 2.0
        heavy = n_fill >= FILLERS_MIN or (n_fill and n_fill / max(1, len(idxs)) >= FILLER_SHARE)
        if heavy:
            reasons.append({"code": "fillers", "count": n_fill})
            score += n_fill
        if score >= INCOHERENT_MIN:
            out.append(make_passage(words_data, idxs, "incoherent", reasons, score, fillers, rep))
    return out


def tagged(words_data, status, fillers=(), repeats=()):
    """Runs of words carrying status (custom_<Color> of the To voice patch marker)."""
    if not status:
        return []
    out, run, pending = [], [], []
    for i, w in enumerate(words_data or []):
        if w.get("status") == status and _spoken(w):
            run.extend(pending)
            pending = []
            run.append(i)
        elif run and (w.get("type") == "silence" or w.get("status") in CUT_STATUSES or _is_inaudible(w)):
            if _spoken(w):
                pending.append(i)
        elif run:
            out.append(run)
            run, pending = [], []
    if run:
        out.append(run)
    return [make_passage(words_data, r, "tagged", [{"code": "tagged"}], 1.0, fillers, repeats) for r in out]


def find_all(words_data, scorer=None, fillers=(), voice_status=None, endings=None, repeats=None):
    """{group: [candidate, ...]} for the four groups. endings: core.detect rows (only the unfinished ones
    are kept), or None when they haven't been measured."""
    if repeats is None:
        try:
            import algorithms
            repeats = algorithms.find_repeats(words_data)
        except Exception:
            repeats = []
    sents = sentences(words_data)
    return {"endings": [dict(r, group="endings", kind="ending") for r in endings or [] if r.get("unfinished")],
            "hard": hard_to_understand(words_data, scorer, sents, fillers, repeats),
            "incoherent": incoherent(words_data, fillers, repeats, sents),
            "tagged": tagged(words_data, voice_status, fillers, repeats)}


def ending_indices(words_data, row, ids=None):
    """Raw indices of an ending's patched words (core rows index the filtered word list; ids match).
    ids: {word id: raw index}, built here when not given."""
    if ids is None:
        ids = {w.get("id"): i for i, w in enumerate(words_data or [])}
    lo, hi = row.get("patch_from_id"), row.get("end_id")
    if hi not in ids:
        return []
    if lo is None or lo not in ids:
        return [ids[hi]]
    return [i for i in range(ids[lo], ids[hi] + 1) if _spoken(words_data[i])]


def candidate_indices(words_data, cand, ids=None):
    return ending_indices(words_data, cand, ids) if cand.get("kind") == "ending" else list(cand.get("idx") or [])


def count_unique(words_data, groups):
    """How many separate passages the groups name together (overlapping ones count once)."""
    ids = {w.get("id"): i for i, w in enumerate(words_data or [])} if groups.get("endings") else None
    spans = []
    for g in GROUPS:
        for c in groups.get(g) or []:
            ix = candidate_indices(words_data, c, ids)
            if ix:
                spans.append((min(ix), max(ix)))
    spans.sort()
    n, last = 0, -1
    for a, b in spans:
        if a > last:
            n += 1
        last = max(last, b)
    return n


def search(words_data, query, fillers=(), repeats=(), limit=60):
    """Sentences that contain `query` (whole words in order, the last one may be partial), as passages to
    patch (group "search"). Cut words count: a patch replaces everything said there."""
    q = [norm(x) for x in str(query or "").split() if norm(x)]
    if not q:
        return []
    out = []
    for idxs in sentences(words_data):
        toks = [norm(words_data[i].get("text")) for i in idxs]
        hit = None
        for s in range(len(toks) - len(q) + 1):
            if all(toks[s + j] == q[j] for j in range(len(q) - 1)) and q[-1] in toks[s + len(q) - 1]:
                hit = " ".join(str(words_data[idxs[s + j]].get("text", "")).strip() for j in range(len(q)))
                hit = hit.strip(".,;:!?…\"'“”‘’ ")
                break
        if hit is not None:
            out.append(make_passage(words_data, idxs, "search", [{"code": "search", "words": hit}], 0.0,
                                    fillers, repeats))
            if len(out) >= limit:
                break
    return out
