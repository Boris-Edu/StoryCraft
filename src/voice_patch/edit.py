#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: voice_patch/edit.py
ROLE: Voice patches in the edit
DESCRIPTION:
A patch the user accepted (Voice tab, "Use in the edit") goes into the timeline Send to Resolve
builds:
  1. split_ops: before assembly, the patched words become their own piece of the cut list, so in
     the new timeline the original audio of exactly those words is a clip of its own; a patch whose
     length differs from the words it replaces can close the timeline up (or hold the picture) so the
     edit follows the patch ("length": "close" in accepted.json);
  2. place_patches: after assembly, that original audio piece is switched off (the picture stays),
     the patch is laid on a "StoryCraft voice patches" audio track at the same frame, and a marker
     says it is synthetic.
Accepted patches are listed in <voice_patches folder>/accepted.json next to each ending's files.
"""
import json
import os

TRACK_NAME = "StoryCraft voice patches"


# ---- which patches are in the edit --------------------------------------------------------------
# accepted.json: a list; each entry is an id ("499", an ending) or {"id": ..., "length": "keep"|"close"}.
# "length" matters when a patch is noticeably shorter or longer than what it replaces (a rewrite):
#   keep   the edit keeps the original timing: the patch plays from the start of the span, the rest of
#          the original span stays under the picture (its audio off: room tone / silence)
#   close  the edit follows the patch: unused original time after a shorter patch is cut out, and after
#          a longer one the picture under the patch is held (its last frames repeat) so nothing is talked over
LENGTH_KEEP, LENGTH_CLOSE = "keep", "close"
LENGTH_ASK_S = 0.3   # the Voice tab asks keep / close when the patch differs from the span by more than this


def accepted_path(out_dir):
    return os.path.join(out_dir, "accepted.json")


def load_accepted_map(out_dir):
    """{id: {"length": "keep"|"close"}} for the patches in the edit."""
    try:
        with open(accepted_path(out_dir), encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, dict) and x.get("id") is not None:
            out[str(x["id"])] = {"length": x.get("length") if x.get("length") in (LENGTH_KEEP, LENGTH_CLOSE)
                                 else LENGTH_KEEP}
        elif not isinstance(x, dict):
            out[str(x)] = {"length": LENGTH_KEEP}
    return out


def load_accepted(out_dir):
    return set(load_accepted_map(out_dir))


def save_accepted(out_dir, ids):
    """ids: a set / list of ids (all "keep"), or a {id: {"length": ...}} map."""
    os.makedirs(out_dir, exist_ok=True)
    m = ids if isinstance(ids, dict) else {str(i): {"length": LENGTH_KEEP} for i in ids}
    entries = []
    for k in sorted(m):
        length = (m[k] or {}).get("length", LENGTH_KEEP)
        entries.append(str(k) if length == LENGTH_KEEP else {"id": str(k), "length": length})
    with open(accepted_path(out_dir), "w", encoding="utf-8") as f:
        json.dump(entries, f)


def patch_length_s(rep):
    """Seconds of the fitted patch (patch.wav) from its report."""
    if rep.get("patch_s") is not None:
        return float(rep["patch_s"])
    span = rep.get("original_span") or [0, 0]
    return float(span[1]) - float(span[0]) + ((rep.get("fit") or {}).get("length_change_ms", 0) or 0) / 1000.0


def story_coverage(a, b, spans):
    """Share of the patched span [a, b] (recording seconds) the story still plays; spans: [(k, start, end)]
    (story_time.spans) or [(start, end)]."""
    if b <= a:
        return 0.0
    got = 0.0
    for sp in spans or []:
        s, e = (sp[1], sp[2]) if len(sp) == 3 else (sp[0], sp[1])
        got += max(0.0, min(b, e) - max(a, s))
    return min(1.0, got / (b - a))


def in_story(p, spans, need=0.9):
    """A patch plays / is sent only while the story still holds (nearly) all of the words it replaces: after
    the story was trimmed (Reload from Resolve, a Version, a moved block) a half-cut patch would say words
    that are no longer there. No story: everything counts."""
    if not spans:
        return True
    return story_coverage(float(p["a"]), float(p["b"]), spans) >= need


def accepted_signature(out_dir):
    """Changes whenever what plays for the accepted patches changes: the list itself (accepted.json) or a
    patch's take (choose_take rewrites report.json and patch.wav, not accepted.json). None: no list."""
    try:
        sig = [os.path.getmtime(accepted_path(out_dir))]
    except OSError:
        return None
    for end_id in sorted(load_accepted_map(out_dir)):
        d = os.path.join(out_dir, f"ending_{end_id}")
        for name in ("report.json", "patch.wav"):
            try:
                sig.append(os.path.getmtime(os.path.join(d, name)))
            except OSError:
                sig.append(0.0)
    return (out_dir, tuple(sig))


def accepted_patches(out_dir):
    """[{end_id, a, b, path, words, length, patch_s, preview, preview_len}] for the accepted patches that have
    a fitted patch. preview: preview.wav (absolute path) when the patch has one, else None: the span exactly
    as the story plays it (patch_s long, laid at a), the original audio at its edges crossfaded into the
    patch; preview_len its length in seconds (None without one)."""
    out = []
    acc = load_accepted_map(out_dir)
    for end_id in sorted(acc):
        d = os.path.join(out_dir, f"ending_{end_id}")
        try:
            with open(os.path.join(d, "report.json"), encoding="utf-8") as f:
                rep = json.load(f)
        except Exception:
            continue
        path = os.path.join(d, "patch.wav")
        span = rep.get("original_span") or []
        if rep.get("result") == "patched" and os.path.isfile(path) and len(span) == 2:
            pv = os.path.join(d, "preview.wav")
            has_pv = bool(rep.get("preview")) and os.path.isfile(pv)
            pv_len = (rep.get("preview") or {}).get("len_s") if has_pv else None
            out.append({"end_id": end_id, "a": float(span[0]), "b": float(span[1]), "path": path,
                        "words": rep.get("replaced_words", ""), "length": acc[end_id]["length"],
                        "take": rep.get("chosen_take"), "new_text": rep.get("new_text") or rep.get("replaced_words", ""),
                        "patch_s": round(patch_length_s(rep), 3),
                        "preview": os.path.abspath(pv) if has_pv else None,
                        "preview_len": round(float(pv_len), 3) if has_pv and pv_len is not None else None})
    return out


# ---- before assembly ----------------------------------------------------------------------------
def _edit_key(o):
    return (o.get("order", 0), o["s"])


def _close_up(pieces, n_frames):
    """Pieces of one patched span (in edit order) cut or held so they last n_frames of the edit."""
    out, left = [], n_frames
    for p in pieces:
        if left <= 0:
            break                                   # unused original time: out of the edit
        n = p["e"] - p["s"]
        out.append(p if n <= left else dict(p, e=p["s"] + left))
        left -= min(n, left)
    if left > 0 and out:
        last = out[-1]
        while left > 0:                             # a longer patch: hold the picture under it
            n = min(left, last["e"] - last["s"])
            if n <= 0:
                break
            out.append(dict(last, s=last["e"] - n, voice_hold=True))
            left -= n
    return out


def split_ops(ops, patches, fps):
    """The cut list with each patched span [a, b] cut out as its own op(s) (same type, same order), tagged
    with the patch's id in "voice_patch". With length "close" the span is then shortened or held to the
    patch's length, so everything after it moves up or back."""
    out = [dict(op) for op in ops]
    for p in patches or []:
        fa, fb = int(round(p["a"] * fps)), int(round(p["b"] * fps))
        if fb <= fa:
            continue
        nxt, inside = [], []
        for op in out:
            s, e = op["s"], op["e"]
            if e <= fa or s >= fb:
                nxt.append(op)
                continue
            for lo, hi, mid in ((s, max(s, fa), False), (max(s, fa), min(e, fb), True), (min(e, fb), e, False)):
                if hi > lo:
                    piece = dict(op, s=lo, e=hi)
                    if mid:
                        piece["voice_patch"] = str(p.get("end_id", ""))
                        inside.append(piece)
                    nxt.append(piece)
        if p.get("length") == LENGTH_CLOSE and inside and p.get("patch_s"):
            want = max(1, int(round(float(p["patch_s"]) * fps)))
            ordered = sorted(inside, key=_edit_key)
            kept = _close_up(ordered, want)
            ins = {id(q) for q in inside}
            merged, done = [], False
            for o in nxt:
                if id(o) in ins:
                    if not done:
                        merged.extend(kept)     # where the span was, in edit order (holds after their piece)
                        done = True
                    continue
                merged.append(o)
            nxt = merged
        for piece in nxt:
            if "start_s" in piece:
                piece["start_s"], piece["end_s"] = piece["s"] / fps, piece["e"] / fps
        out = nxt
    return out


def edit_position(ops, t, fps):
    """Frame (from the timeline's start) where transcript time t plays in the edit, or None when cut.
    Give it the final ops (after split_ops): spans closed up or held move what follows them."""
    f = int(round(t * fps))
    pos = 0
    for op in sorted(ops, key=_edit_key):
        if op["s"] <= f < op["e"] and not op.get("voice_hold"):
            return pos + (f - op["s"])
        pos += op["e"] - op["s"]
    return None


def patch_ranges(ops, end_id):
    """[(start, end)] edit frames of the ops split_ops tagged for this patch (held frames included)."""
    pos, out = 0, []
    for op in sorted(ops, key=_edit_key):
        n = op["e"] - op["s"]
        if op.get("voice_patch") == str(end_id):
            out.append((pos, pos + n))
        pos += n
    return out


# ---- the patch's media file: named after its place in the story, kept for good ---------------------
MEDIA_DIR = "media"
BIN_NAME = "Voice patches"


def _safe_name(text, limit=70):
    t = "".join(c if (c.isalnum() or c in " -_.,'·()&") else " " for c in str(text or ""))
    t = " ".join(t.split())
    return (t[:limit].rstrip(" .,") + "…") if len(t) > limit else t


def _md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_name(p):
    """'S011 · and then we opened the shop… (voice patch, take 2)' — what the bin and Finder show."""
    label = p.get("label") or f"Patch {p.get('end_id', '')}"
    words = " ".join(str(p.get("new_text") or p.get("words") or "").split()[:8])
    take = p.get("take")
    tail = f"(voice patch, take {take})" if take else "(voice patch)"
    return _safe_name(f"{label} · {words}" if words else label, 80) + " " + tail


def media_copy(p):
    """A fixed copy of the patch's audio (its seamless preview.wav, else patch.wav) in <patches>/media, named
    after its place in the story. Never overwritten: the same audio reuses its file; another take or a new
    generation gets a new one (" v2"…), so timelines already sent keep their sound and Resolve finds the files
    again when the project is reopened. Returns the path."""
    src = p.get("preview") if p.get("preview") and os.path.isfile(p["preview"]) else p["path"]
    out_dir = os.path.dirname(os.path.dirname(src))          # <patches>/ending_<id>/x.wav -> <patches>
    folder = os.path.join(out_dir, MEDIA_DIR)
    os.makedirs(folder, exist_ok=True)
    base, want = media_name(p), _md5(src)
    for k in range(1, 100):
        dest = os.path.join(folder, base + (f" v{k}" if k > 1 else "") + ".wav")
        if not os.path.exists(dest):
            import shutil
            shutil.copyfile(src, dest)
            return dest
        if _md5(dest) == want:
            return dest
    return src


def _bin(mp, parent, name):
    for sub in parent.GetSubFolderList() or []:
        if sub.GetName() == name:
            return sub
    return mp.AddSubFolder(parent, name)


def patch_bin(mp):
    """Media Pool: StoryCraft › Voice patches (created when missing)."""
    try:
        import config
        top = config.RESOLVE_BIN
    except Exception:
        top = "StoryCraft"
    sc = _bin(mp, mp.GetRootFolder(), top)
    return _bin(mp, sc, BIN_NAME) if sc is not None else None


def import_patch(mp, path):
    """The Media Pool item for path in StoryCraft › Voice patches: the one already there, else imported there."""
    try:
        folder = patch_bin(mp)
    except Exception:
        folder = None          # no bin access (older Resolve / scripting limits): import where Resolve puts it
    if folder is not None:
        for clip in folder.GetClipList() or []:
            try:
                if os.path.normcase(clip.GetClipProperty("File Path") or "") == os.path.normcase(path):
                    return clip
            except Exception:
                continue
    prev = None
    try:
        if folder is not None:
            try:
                prev = mp.GetCurrentFolder()
                mp.SetCurrentFolder(folder)
            except Exception:
                prev = None
        items = mp.ImportMedia([path]) or []
    finally:
        if prev is not None:
            try:
                mp.SetCurrentFolder(prev)
            except Exception:
                pass
    return items[0] if items else None


# ---- after assembly (DaVinci Resolve scripting API) ---------------------------------------------
def place_patches(project, timeline, ops, patches, fps, log=lambda m: None):
    """Lay the patches into the new timeline. Returns (placed, problems)."""
    placed, problems = 0, []
    if not patches:
        return 0, []
    mp = project.GetMediaPool()
    start = int(timeline.GetStartFrame())
    if not timeline.AddTrack("audio", "mono"):
        timeline.AddTrack("audio")
    track = int(timeline.GetTrackCount("audio"))
    try:
        timeline.SetTrackName("audio", track, TRACK_NAME)
    except Exception:
        pass
    for p in patches:
        ranges = patch_ranges(ops, p.get("end_id"))
        if ranges:
            rel, rel_end = ranges[0][0], ranges[-1][1]
        else:  # ops from before the pieces were tagged: the span as it was
            rel = edit_position(ops, p["a"] + 0.001, fps)
            if rel is not None:
                ranges = [(rel, rel + int(round((p["b"] - p["a"]) * fps)))]
                rel_end = ranges[0][1]
        if rel is None:
            problems.append(f"{p['words']}: not in the edit any more")
            continue
        rec = start + rel
        patch_len = int(round(float(p.get("patch_s") or (p["b"] - p["a"])) * fps))
        # preview.wav covers exactly what the story plays from a (the recording at its edges, crossfaded into
        # the patch), so over the muted original it joins seamlessly; older patches only have patch.wav
        try:
            src = media_copy(p)          # named after its sentence, kept in the StoryCraft folder for good
        except Exception as e:
            log(f"voice patch media copy failed ({e}); using the take's own file")
            src = p.get("preview") if p.get("preview") and os.path.isfile(p["preview"]) else p["path"]
        item = import_patch(mp, src)     # Media Pool: StoryCraft › Voice patches (reused when already there)
        if item is None:
            problems.append(f"{p['words']}: Resolve couldn't import the patch")
            continue
        added = mp.AppendToTimeline([{"mediaPoolItem": item, "mediaType": 2, "trackIndex": track,
                                      "recordFrame": rec}]) or []
        if not added:
            problems.append(f"{p['words']}: Resolve didn't place the patch")
            continue
        # The original words' audio (their own clips, see split_ops) is switched off; the picture stays
        muted = 0
        for ti in range(1, track):
            for it in timeline.GetItemListInTrack("audio", ti) or []:
                st, en = int(it.GetStart()) - start, int(it.GetEnd()) - start
                if any(abs(st - r0) <= 1 and abs(en - r1) <= 2 for r0, r1 in ranges):
                    if it.SetClipEnabled(False):
                        muted += 1
        if not muted:
            problems.append(f"{p['words']}: the original audio couldn't be switched off")
        note = f"Synthetic voice (VoiceStudio) replacing: {p['words']}"
        if any(o.get("voice_hold") and o.get("voice_patch") == str(p.get("end_id")) for o in ops):
            note += " (the picture holds under the longer patch: cover it with B-roll)"
        timeline.AddMarker(rel, "Purple", "Voice patch", note, max(1, patch_len, rel_end - rel))
        placed += 1
        log(f"voice patch placed at {rec}: {p['words']} (original audio off on {muted} clip(s))")
    return placed, problems
