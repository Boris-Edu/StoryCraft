#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MODULE: finishing.py
ROLE: Logic (no GUI, no Resolve)
DESCRIPTION:
What happens to an edit after its cuts are known: where each kept word lands on the new timeline,
captions (SRT) of the edit, swear words, the ranges to bleep, and who is on screen when (cameras).
The kept ops are laid end to end in ('order', 's') order, exactly as every assembler places them;
an op's frames are source frames that already include the cut offset.
"""

import re

# ── Where things land on the new timeline ────────────────────────────────────────────────────

def layout(ops):
    """[(op, first frame of the op on the new timeline, counted from the timeline start)]."""
    placed, acc = [], 0
    for op in sorted(ops or [], key=lambda o: (o.get('order', 0), o['s'])):
        placed.append((op, acc))
        acc += op['e'] - op['s']
    return placed


def _spoken(w):
    return not (w.get("type") in ("silence", "inaudible") or w.get("is_inaudible")
                or not (w.get("text") or "").strip())


def placed_words(words, ops, fps, offset=0.133):
    """Every spoken word the edit keeps, in timeline order:
    [(word index, start s, end s on the new timeline, op)]. A word is kept when its middle is."""
    import bisect
    out = []
    # (middle frame, word index, start s, end s), sorted by middle: each op takes a slice by bisection
    # instead of testing every word (an hour of transcript × hundreds of clips took seconds)
    spoken = []
    for i, w in enumerate(words or []):
        if _spoken(w):
            st, en = float(w.get("start", 0)), float(w.get("end", 0))
            spoken.append((((st + en) / 2 + offset) * fps, i, st, en))
    spoken.sort()
    mids = [m for m, _i, _s, _e in spoken]
    for op, at in layout(ops):
        lo, hi = bisect.bisect_left(mids, op['s']), bisect.bisect_left(mids, op['e'])
        for _m, i, st, en in sorted(spoken[lo:hi], key=lambda x: x[1]):
            f0 = max(op['s'], (st + offset) * fps)
            f1 = min(op['e'], (en + offset) * fps)
            out.append((i, (at + f0 - op['s']) / fps, (at + max(f1, f0) - op['s']) / fps, op))
    return out


# ── Captions ─────────────────────────────────────────────────────────────────────────────────

def _srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _two_lines(text, max_chars):
    if len(text) <= max_chars:
        return text
    words = text.split()
    best, best_d = None, None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        d = abs(len(a) - len(b))
        if best_d is None or d < best_d:
            best, best_d = (a, b), d
    return "\n".join(best) if best else text


_ABBREV = {"dr.", "mr.", "mrs.", "ms.", "st.", "prof.", "mt.", "jr.", "sr.", "vs.", "etc.", "e.g.", "i.e.",
           "m.", "mme.", "mlle.", "hr.", "fr.", "sra.", "sr.", "dott.", "sig."}


def caption_cues(words, ops, fps, offset=0.133, speakers=None, names=False,
                 max_chars=42, max_dur=6.0, max_gap=0.8):
    """[(start s, end s, text)] on the new timeline. A caption ends at a speaker change, a pause,
    two full lines, max_dur seconds, or a sentence end once it has some length."""
    names_by = {s["id"]: s["name"] for s in (speakers or [])}
    cues, cur = [], None
    for i, t0, t1, _op in placed_words(words, ops, fps, offset):
        w = words[i]
        tok = (w.get("text") or "").strip()
        spk = w.get("spk")
        if cur is not None:
            new_text = cur["text"] + " " + tok
            if (spk != cur["spk"] or t0 - cur["end"] > max_gap or len(new_text) > 2 * max_chars
                    or t1 - cur["start"] > max_dur):
                cues.append(cur)
                cur = None
            else:
                cur["text"], cur["end"] = new_text, t1
        if cur is None:
            cur = {"start": t0, "end": t1, "text": tok, "spk": spk}
        if tok[-1:] in ".?!" and tok.lower() not in _ABBREV and len(cur["text"]) >= max_chars * 0.6:
            cues.append(cur)
            cur = None
    if cur is not None:
        cues.append(cur)
    out = []
    for k, c in enumerate(cues):
        end = c["end"]
        if k + 1 < len(cues):
            end = min(max(end, c["start"] + 0.7), cues[k + 1]["start"])  # readable, never overlapping
        text = c["text"]
        if names and c["spk"] in names_by and (k == 0 or cues[k - 1]["spk"] != c["spk"]):
            text = f"{names_by[c['spk']].upper()}: {text}"
        out.append((c["start"], end, _two_lines(text, max_chars)))
    return out


def build_srt(words, ops, fps, offset=0.133, speakers=None, names=False, max_chars=42):
    """SubRip captions of the edit (times from the start of the new timeline)."""
    blocks = []
    for n, (a, b, text) in enumerate(caption_cues(words, ops, fps, offset, speakers, names, max_chars), 1):
        blocks.append(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n")
    return "\n".join(blocks)


# ── Swear words ──────────────────────────────────────────────────────────────────────────────
# Whole words, after lower-casing and stripping punctuation. A trailing * matches any ending.
SWEARS = {
    "en": "fuck* motherfuck* shit shitty bullshit bitch* bastard* asshole* arsehole* dick dickhead cunt* "
          "piss pissed crap damn goddamn wanker* twat* bollocks prick*",
    "fr": "merde putain bordel connard* connasse* salope* enculé* encule* foutre bite couilles chier "
          "emmerde* fdp pute*",
    "es": "mierda joder jodido* puta* puto* cabrón cabron coño cono gilipollas pendejo* chingar* "
          "chingada* carajo hostia",
    "de": "scheiße scheisse scheiß scheiss fick* arschloch* arsch wichser* hurensohn* fotze* verdammt* "
          "kacke miststück",
    "it": "cazzo* merda stronzo* stronza* vaffanculo puttana* minchia coglione* porca fottuto*",
    "pt": "merda porra caralho foda* fodido* puta* cacete buceta viado*",
    "nl": "kut* klootzak* shit godverdomme* tering* kanker* lul hoer* eikel*",
    "pl": "kurwa* kurw* chuj* huj* pierdol* jebać jeba* jebany* skurwysyn* pizda* gówno",
    "ru": "блять* бля сука* хуй* пизд* ебать* ебан* мудак* говно* дерьмо",
    "uk": "блять* бля сука* хуй* пизд* їбати* єбан* мудак* лайно* гівно",
}


def _swear_matcher(lang, extra=None):
    words = SWEARS.get((lang or "en")[:2].lower(), SWEARS["en"]).split() + list(extra or [])
    exact = {x for x in words if not x.endswith("*")}
    prefixes = tuple(x[:-1] for x in words if x.endswith("*"))

    def match(token):
        t = re.sub(r"[^\w']", "", (token or "").lower()).strip("'")
        return bool(t) and (t in exact or (prefixes and t.startswith(prefixes)))
    return match


def find_swears(words, lang="en", extra=None):
    """Indices of the swear words in the transcript (spoken words only)."""
    match = _swear_matcher(lang, extra)
    return [i for i, w in enumerate(words or []) if _spoken(w) and match(w.get("text"))]


# ── Ranges on the new timeline ───────────────────────────────────────────────────────────────

def bleep_ranges(ops, fps):
    """[(first frame, end frame)] on the new timeline of the ops to bleep (op['bleep'])."""
    return [(at, at + op['e'] - op['s']) for op, at in layout(ops) if op.get('bleep')]


def speaker_ranges(words, ops, fps, offset=0.133):
    """[(first frame, end frame, speaker id or None)] per op on the new timeline: who speaks most
    of it (by duration). Ops without words keep the previous speaker, so a pause doesn't switch camera."""
    per_op = {}
    for i, t0, t1, op in placed_words(words, ops, fps, offset):
        spk = words[i].get("spk")
        if spk:
            d = per_op.setdefault(id(op), {})
            d[spk] = d.get(spk, 0.0) + (t1 - t0)
    out, prev = [], None
    for op, at in layout(ops):
        d = per_op.get(id(op))
        spk = max(d, key=d.get) if d else prev
        out.append((at, at + op['e'] - op['s'], spk))
        prev = spk
    return out
