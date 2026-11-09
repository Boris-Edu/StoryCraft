#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: voice_patch/transcript.py
ROLE: The transcript shows the patches that are in the edit
DESCRIPTION:
A patch in the edit (accepted.json) is synthetic speech: its words in the transcript carry
"voice_patch": <id> (the canvas draws them in the AI-voice colour). When the patch says other words (a
rewrite), the transcript shows the new words in the same word slots (lined up by time with word_fix;
the number of words never changes, so word ids — which name the passage folders — stay put). The words that were there are kept in the patch folder (transcript_before.json) and come back
when the patch leaves the edit. sync() makes the transcript match the accepted set, so undo/redo of
"Use in the edit" (which restores the set) brings the words along.
The patched words follow the patch's own rhythm: the file that plays for it (preview.wav, else patch.wav) is
transcribed with word timestamps (report "patch_words", seconds in that file — voice_patch/core.py) and each
word's time is carried into recording time with the player's own mapping (source_time: the playhead crosses
[a, b] in step with the patch), so the spoken word lights in step with what is heard. Without patch_words
(a patch made before this) the words keep the recording's timing until they are read.
"""
import copy
import difflib
import json
import os
import re

import word_fix


# ---- the file that plays for a patch and the player's clock ------------------------------------------
def source_time(pos, a, b, file_len, length):
    """Recording time the playhead shows at pos seconds into the patch's playing file (the player's mapping,
    gui/components/patch_playback.py): "keep" plays at the recording's pace and holds at b; "close" (the story
    follows the patch) crosses [a, b] in step with the whole file."""
    pos = max(0.0, float(pos))
    if length == "keep":
        return a + min(pos, b - a)
    return a + (b - a) * (min(1.0, pos / file_len) if file_len > 0 else 1.0)


def _wav_len(path):
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def playing_file(d, rep):
    """("preview"|"patch", path, length in seconds) of the file the player plays for this patch (the same
    choice and length as PatchPlayback._file), or None without a fitted patch."""
    a, b = (float(x) for x in (rep.get("original_span") or [0, 0])[:2]) if len(rep.get("original_span") or []) == 2 \
        else (0.0, 0.0)
    from voice_patch.edit import patch_length_s
    try:
        patch_s = round(patch_length_s(rep), 3)
    except Exception:
        patch_s = None
    pv = os.path.join(d, "preview.wav")
    if os.path.isfile(pv):
        pv_len = (rep.get("preview") or {}).get("len_s") if rep.get("preview") else None
        pv_len = round(float(pv_len), 3) if pv_len is not None else None
        return "preview", pv, float(pv_len or _wav_len(pv) or patch_s or (b - a))
    path = os.path.join(d, "patch.wav")
    if not os.path.isfile(path):
        return None
    return "patch", path, float(_wav_len(path) or patch_s or (b - a))


def _length_of(out_dir, pid):
    try:
        from voice_patch.edit import load_accepted_map
        return (load_accepted_map(out_dir).get(str(pid)) or {}).get("length") or "close"
    except Exception:
        return "close"


def _norm(t):
    return re.sub(r"[^\w']+", "", str(t or "").lower())


def _split_window(texts, s, e):
    """Times for words said one after another in [s, e], shared by their length."""
    weights = [len(_norm(t)) + 1 for t in texts]
    tot = float(sum(weights)) or 1.0
    out, t = [], s
    for wgt in weights:
        t1 = t + (e - s) * wgt / tot
        out.append((t, t1))
        t = t1
    return out


def align_times(targets, heard, file_len=None):
    """(start, end) in the patch file for each target word, from what the ASR heard (lined up by text; words
    it missed share the time between their neighbours, or with the file's start / end). None when nothing
    was heard."""
    heard = [h for h in heard or [] if _norm(h.get("text"))]
    if not targets or not heard:
        return None
    tn, hn = [_norm(t) for t in targets], [_norm(h["text"]) for h in heard]
    out = [None] * len(targets)
    ops = difflib.SequenceMatcher(None, tn, hn, autojunk=False).get_opcodes()
    for tag, i0, i1, j0, j1 in ops:
        if tag == "equal":
            for k in range(i1 - i0):
                out[i0 + k] = (float(heard[j0 + k]["start"]), float(heard[j0 + k]["end"]))
        elif i1 > i0 and j1 > j0:          # said differently: the target words share what was heard there
            out[i0:i1] = _split_window(targets[i0:i1], float(heard[j0]["start"]), float(heard[j1 - 1]["end"]))
    for i in range(len(out)):              # missed words: the gap between the neighbours
        if out[i] is None:
            j = i
            while j < len(out) and out[j] is None:
                j += 1
            s = out[i - 1][1] if i > 0 else (0.0 if file_len else float(heard[0]["start"]))
            e = out[j][0] if j < len(out) else max(float(heard[-1]["end"]), float(file_len or 0.0))
            out[i:j] = _split_window(targets[i:j], s, max(s, e))
    return out


def _slot_groups(old_texts, new_texts):
    """For each word slot, the indices of the new words it shows (lined up by text: a slot whose word is still
    said keeps it; the others share the new words in order; a slot with none is left blank)."""
    groups = [[] for _ in old_texts]
    if not old_texts:
        return groups
    on, nn = [_norm(t) for t in old_texts], [_norm(t) for t in new_texts]
    ops = difflib.SequenceMatcher(None, on, nn, autojunk=False).get_opcodes()
    pending = []                           # new words before any slot: they go to the first slot
    for tag, i0, i1, j0, j1 in ops:
        n_old, new = i1 - i0, list(range(j0, j1))
        if tag == "equal":
            for k in range(n_old):
                groups[i0 + k].append(j0 + k)
        elif n_old and new:
            if len(new) >= n_old:          # n_old consecutive groups, as even as possible
                for k in range(n_old):
                    groups[i0 + k].extend(new[k * len(new) // n_old:(k + 1) * len(new) // n_old])
            else:                           # fewer words than slots: spread over them, the rest blank
                for k, j in enumerate(new):
                    groups[i0 + (k * n_old) // len(new)].append(j)
        elif new:                          # inserted words: with the slot before them
            if i0 > 0:
                groups[i0 - 1].extend(new)
            else:
                pending.extend(new)
    if pending:
        groups[0][:0] = pending
    return groups


def _chain(items, lo, hi):
    """items: [[start, end, min_len, pause]] in order (start/end None: unknown): monotonic times inside
    [lo, hi], each at least min_len long, as close to the wanted times as that allows. An unknown word sits
    just after what comes before it; an unknown pause fills the gap up to the next word."""
    t = lo
    for k, it in enumerate(items):
        if it[0] is None:
            it[0] = t
        if it[1] is None:
            nxt = next((x[0] for x in items[k + 1:] if x[0] is not None), None) if it[3] else None
            it[1] = max(it[0], nxt) if nxt is not None else it[0]
        it[0], it[1] = min(max(it[0], lo), hi), min(max(it[1], lo), hi)
        t = it[1]
    cur = lo
    for it in items:                        # forward: no overlap, no zero length
        it[0] = max(it[0], cur)
        it[1] = max(it[1], it[0] + it[2])
        cur = it[1]
    cur = hi
    for it in reversed(items):              # backward: back inside hi (squeezing the end when needed)
        it[1] = min(it[1], cur)
        it[0] = min(it[0], it[1] - it[2])
        cur = it[0]
    return items


def timing_sig(out_dir, pid, rep=None):
    """What the patched words' times follow (file, length mode, take, file length, words), or None when the
    patch has no words of its own for the file that plays (the words keep the recording's times)."""
    rep = rep if rep is not None else _report(out_dir, pid)
    if not rep or not isinstance(rep.get("patch_words"), list) or not rep["patch_words"] \
            or len(rep.get("original_span") or []) != 2:
        return None
    pf = playing_file(os.path.join(out_dir, f"ending_{pid}"), rep)
    if pf is None or rep.get("patch_words_file", "preview") != pf[0]:
        return None
    return f"{pf[0]}:{_length_of(out_dir, pid)}:{rep.get('chosen_take')}:{round(pf[2], 3)}:{len(rep['patch_words'])}"


def needs_words(out_dir, pid):
    """True when patch pid is fitted but has no words of its own for the file that plays (made before they
    were read, or its take changed to one without them): voice_view reads them in the background."""
    rep = _report(out_dir, pid)
    if not rep or rep.get("result") != "patched" or len(rep.get("original_span") or []) != 2:
        return False
    pf = playing_file(os.path.join(out_dir, f"ending_{pid}"), rep)
    return pf is not None and timing_sig(out_dir, pid, rep) is None


def _timed_segment(new_seg, rep, out_dir, pid, text, lo, hi):
    """new_seg's words set from the patch's own words (report patch_words) at their place in the recording.
    Returns the timing signature, or None (no patch_words for the file that plays: nothing changed)."""
    sig = timing_sig(out_dir, pid, rep)
    if sig is None:
        return None
    pf = playing_file(os.path.join(out_dir, f"ending_{pid}"), rep)
    a, b = (float(x) for x in rep["original_span"])
    length, flen = _length_of(out_dir, pid), pf[2]
    slots = [i for i, w in enumerate(new_seg) if w.get("type") == "word"]
    if not slots or b <= a:
        return None
    old_texts = [str(new_seg[k].get("text", "")) for k in slots]
    targets = text.split() if text else old_texts
    times = align_times(targets, rep["patch_words"], flen)
    if times is None:
        return None
    groups = _slot_groups(old_texts, targets) if text else [[k] for k in range(len(slots))]
    lo, hi = max(a, lo), min(b, hi)
    if hi - lo < 0.05:
        lo, hi = a, b
    min_w = min(0.03, (hi - lo) / (2.0 * len(new_seg) + 1))
    slot_of = {i: n for n, i in enumerate(slots)}
    items = []
    for i, w in enumerate(new_seg):
        if i not in slot_of:                 # a pause between the words: the gap the patch leaves there
            items.append([None, None, min_w / 10.0, True])
            continue
        g = groups[slot_of[i]]
        if text:
            if g:
                w["text"] = " ".join(targets[j] for j in g)
                w.pop("voice_patch_blank", None)
            else:
                w["text"] = ""
                w["voice_patch_blank"] = True
        if g:
            items.append([source_time(min(times[j][0] for j in g), a, b, flen, length),
                          source_time(max(times[j][1] for j in g), a, b, flen, length), min_w, False])
        else:
            items.append([None, None, min_w, False])
    _chain(items, lo, hi)
    for w, it in zip(new_seg, items):
        w["start"], w["end"] = round(it[0], 4), round(it[1], 4)
    return sig


def _report(out_dir, pid):
    try:
        with open(os.path.join(out_dir, f"ending_{pid}", "report.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _patch_text(rep):
    """The words the patch says (None = the same words as the recording)."""
    if rep.get("kind") == "passage" and rep.get("rewrite") and rep.get("new_text"):
        return str(rep["new_text"]).strip()
    return None


def _run_of(words, pid):
    idx = [i for i, w in enumerate(words) if w.get("voice_patch") == pid]
    return (min(idx), max(idx)) if idx else None


def _span_range(words, a, b):
    """First..last index of the transcript words whose middle lies in [a, b] (silences between kept)."""
    idx = [i for i, w in enumerate(words) if w.get("type") == "word"
           and a - 0.02 <= (float(w.get("start", 0)) + float(w.get("end", 0))) / 2.0 <= b + 0.02]
    return (min(idx), max(idx)) if idx else None


def _renumber(words):
    for i, w in enumerate(words):
        w["id"] = i
    return words


def revert(words, out_dir, pid):
    """The words that were there before patch pid (or the patch flags dropped). Returns (words, changed)."""
    rng = _run_of(words, pid)
    if rng is None:
        return words, False
    first, last = rng
    path = os.path.join(out_dir, f"ending_{pid}", "transcript_before.json")
    try:
        with open(path, encoding="utf-8") as f:
            before = json.load(f)
    except Exception:
        before = None
    if before is None:
        seg = [{k: v for k, v in w.items() if not str(k).startswith("_")} for w in words[first:last + 1]]
        for w in seg:
            w.pop("voice_patch", None)
            w.pop("voice_patch_text", None)
            w.pop("voice_patch_timing", None)
        before = seg
    out = words[:first] + before + words[last + 1:]
    for k in ("meta_audio_path", "meta_global_silence", "meta_language", "meta_speakers"):
        if k in words[0] and k not in out[0]:
            out[0][k] = words[0][k]
    return _renumber(out), True


def apply(words, out_dir, pid, clear_status=None):
    """Patch pid shown in the transcript. clear_status: a word status the patch makes moot (the To voice
    patch marker). Returns (words, changed)."""
    rep = _report(out_dir, pid)
    if not rep or rep.get("result") != "patched" or len(rep.get("original_span") or []) != 2:
        return words, False
    text = _patch_text(rep)
    sig = timing_sig(out_dir, pid, rep)
    run = _run_of(words, pid)
    if run is not None:
        first, last = run
        if all(w.get("voice_patch_text") == text and w.get("voice_patch_timing") == sig
               for w in words[first:last + 1] if w.get("voice_patch") == pid):
            return words, False          # already shown as it is
        words, _ = revert(words, out_dir, pid)
    a, b = (float(x) for x in rep["original_span"])
    rng = _span_range(words, a, b)
    if rng is None:
        return words, False
    first, last = rng
    segment = words[first:last + 1]
    path = os.path.join(out_dir, f"ending_{pid}", "transcript_before.json")
    with open(path, "w", encoding="utf-8") as f:   # without the canvas's layout keys (_rect…)
        json.dump([{k: v for k, v in w.items() if not str(k).startswith("_")} for w in segment], f)
    # fresh copies without the canvas's cached layout (_display_text, _rect…): it lays them out again
    new_seg = [{k: v for k, v in w.items() if not str(k).startswith("_")} for w in segment]
    slots = [i for i, w in enumerate(segment) if w.get("type") == "word"]
    # the words around the span bound the patched words (no overlap with the recording's words)
    prev_end = next((float(w.get("end", 0)) for w in reversed(words[:first]) if w.get("type") == "word"), a)
    next_start = next((float(w.get("start", 0)) for w in words[last + 1:] if w.get("type") == "word"), b)
    sig = _timed_segment(new_seg, rep, out_dir, pid, text, prev_end, next_start) if sig else None
    if sig is None and text and slots and text.split() != word_fix.spoken_text(segment).split():
        # (no words of the patch's own yet) The new words go into the SAME word slots (word ids stay put:
        # other patches are named after them): lined up by time with word_fix, a slot can hold several new
        # words or none (then it's hidden)
        timed = [w for w in word_fix.correct_passage(segment, text) if w.get("type") != "silence"]
        got = {k: [] for k in slots}
        for nw in timed:
            mid = (float(nw["start"]) + float(nw["end"])) / 2.0
            k = min(slots, key=lambda j: 0.0 if float(segment[j]["start"]) <= mid <= float(segment[j]["end"])
                    else min(abs(mid - float(segment[j]["start"])), abs(mid - float(segment[j]["end"]))))
            got[k].append(str(nw["text"]))
        for k in slots:
            if got[k]:
                new_seg[k]["text"] = " ".join(got[k])
                new_seg[k].pop("voice_patch_blank", None)
            else:
                new_seg[k]["text"] = ""
                new_seg[k]["voice_patch_blank"] = True
    for w in new_seg:
        if w.get("type") == "silence":
            continue
        w["voice_patch"] = pid
        w["voice_patch_text"] = text
        if sig is not None:
            w["voice_patch_timing"] = sig      # what the times follow: a new take / length mode re-times them
        else:
            w.pop("voice_patch_timing", None)
        if clear_status and w.get("status") == clear_status:
            w["status"] = None
            w["manual_status"] = None
    out = words[:first] + new_seg + words[last + 1:]
    for k in ("meta_audio_path", "meta_global_silence", "meta_language", "meta_speakers"):
        if k in words[0] and k not in out[0]:
            out[0][k] = words[0][k]
    return _renumber(out), True


def sync(words, out_dir, accepted_ids, clear_status=None):
    """The transcript showing exactly the accepted patches. Returns (words, changed)."""
    changed = False
    shown = {w.get("voice_patch") for w in words if w.get("voice_patch")}
    for pid in sorted(shown - set(accepted_ids)):
        words, c = revert(words, out_dir, pid)
        changed |= c
    for pid in sorted(accepted_ids):
        words, c = apply(words, out_dir, pid, clear_status)
        changed |= c
    return words, changed


def refresh_board_text(sentences, words):
    """The story board's sentence texts from the transcript (by time): a rewrite shows in the story too.
    Returns True when any text changed."""
    changed = False
    spoken = [w for w in words if w.get("type") == "word"]
    for s in sentences or []:
        a, b = float(s.get("src_start", 0)), float(s.get("src_end", 0))
        txt = " ".join(str(w.get("text", "")).strip() for w in spoken if str(w.get("text", "")).strip()
                       if a - 0.02 <= (float(w.get("start", 0)) + float(w.get("end", 0))) / 2.0 <= b + 0.02).strip()
        if txt and txt != s.get("text"):
            s["text"] = txt
            changed = True
    return changed
