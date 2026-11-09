"""Edit time: where a moment of the recording falls in the story as it plays.

Recording time is a position in the raw recording (the transcript's seconds). Edit time is the
position in the story: 0:00 is the start of the story, the clips follow in play order, and what the
cut settings take out inside a clip (silences, fillers) doesn't count when the story board has
measured it (its kept ranges); otherwise each clip counts from its cut_start to its cut_end.

    edit_time(win, t)      -> seconds into the story, or None when t isn't in the story
    story_length(win)      -> seconds the story lasts (0.0 without a story)
    clip_starts(win)       -> edit time at which each clip starts, in play order
    fmt_time(seconds)      -> "m:ss" (or "h:mm:ss")

Voice patches put in the story with "Make room" / "Cut the extra time" (length "close") change how long
the story lasts: the story is longer (or shorter) by the patch's extra time from the patch's end on.
"""
import os

_cache = {"key": None, "ctx": None, "spans": []}


def fmt_time(seconds):
    """'m:ss', or 'h:mm:ss' from an hour on; rounded to the second, never negative."""
    try:
        s = int(round(max(0.0, float(seconds or 0.0))))
    except (TypeError, ValueError):
        s = 0
    h, m, s = s // 3600, (s // 60) % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clips(win):
    out = []
    for c in (getattr(win, "_story", None) or {}).get("clips") or []:
        try:
            a, b = float(c["cut_start"]), float(c["cut_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            out.append((a, b))
    return out


def _ops_ctx(win):
    board = getattr(win, "story_board", None)
    ctx = getattr(board, "_ops_ctx", None) if board is not None else None
    if not ctx or len(ctx) != 3 or not ctx[0]:
        return None
    return ctx


def spans(win):
    """The story as played: [(clip_index, rec_start, rec_end), ...] in play order (seconds of the
    recording). Cached until the story or the board's cut measure changes."""
    clips = _clips(win)
    if not clips:
        return []
    ctx = _ops_ctx(win)
    key = tuple(clips)
    if _cache["key"] == key and _cache["ctx"] is ctx:
        return _cache["spans"]
    out = []
    if ctx is not None:
        try:
            import algorithms
            ops, fps, offset = ctx
            fps = float(fps or 24.0)
            story = [{"cut_start": a, "cut_end": b} for a, b in clips]
            kept = algorithms.apply_story_order(ops, story, fps, offset=float(offset))
            kept.sort(key=lambda o: (o.get("order", 0), o["s"]))
            out = [(int(o.get("order", 0)), o["s"] / fps - float(offset), o["e"] / fps - float(offset)) for o in kept]
        except Exception as exc:
            print(f"[story time] kept ranges failed: {exc}")
            out = []
    if not out:
        out = [(k, a, b) for k, (a, b) in enumerate(clips)]
    _cache.update(key=key, ctx=ctx, spans=out)
    return out


_pcache = {"sig": None, "deltas": []}


def patch_deltas(win):
    """[(rec_start, rec_end, extra_s)] of the voice patches in the story that change its length
    (length "close": the story follows the patch). Cached until accepted.json changes."""
    try:
        from voice_patch.edit import accepted_signature
        d = win._voice_patch_dir()
        sig = accepted_signature(d)
    except Exception:
        return []
    if sig is None:
        return []
    if _pcache["sig"] != sig:
        out = []
        try:
            from voice_patch.edit import accepted_patches
            for p in accepted_patches(d):
                if p.get("length") == "close" and p.get("patch_s"):
                    extra = float(p["patch_s"]) - (float(p["b"]) - float(p["a"]))
                    if abs(extra) > 0.01:
                        out.append((float(p["a"]), float(p["b"]), extra))
        except Exception as exc:
            print(f"[story time] voice patches: {exc}")
        _pcache.update(sig=sig, deltas=out)
    return _pcache["deltas"]


def _patch_play_index(sp, a, b):
    """Play-order index of the span holding the patch's end (where its extra time comes in), or None."""
    last = None
    for i, (_k, s, e) in enumerate(sp):
        if s < b and e >= b - 0.05 and e > a:
            return i
        if s < b and e > a:
            last = i                          # its end cut away by the cut settings: the last span it touches
    return last


def _shifts(win, sp):
    """{play index: extra seconds coming in at the end of that span}."""
    out = {}
    from voice_patch.edit import story_coverage
    for a, b, extra in patch_deltas(win):
        if story_coverage(a, b, sp) < 0.9:
            continue                          # the story no longer holds the words it replaces
        i = _patch_play_index(sp, a, b)
        if i is not None:
            out[i] = out.get(i, 0.0) + extra
    return out


def _shift_points(win, sp):
    """{play index: [(patch end, extra seconds)]}: where inside a span each patch's extra time comes in."""
    out = {}
    from voice_patch.edit import story_coverage
    for a, b, extra in patch_deltas(win):
        if story_coverage(a, b, sp) < 0.9:
            continue
        i = _patch_play_index(sp, a, b)
        if i is not None:
            out.setdefault(i, []).append((b, extra))
    return out


def story_length(win):
    sp = spans(win)
    return float(sum(max(0.0, b - a) for _k, a, b in sp) + sum(_shifts(win, sp).values()))


def clip_starts(win):
    """Edit time at which each story clip starts (play order); a clip with nothing kept starts where
    the next one would."""
    sp = spans(win)
    n = len(_clips(win))
    shift = _shifts(win, sp)
    starts, pos = [None] * n, 0.0
    for i, (k, a, b) in enumerate(sp):
        if 0 <= k < n and starts[k] is None:
            starts[k] = pos
        pos += max(0.0, b - a) + shift.get(i, 0.0)
    nxt = pos
    for k in range(n - 1, -1, -1):
        if starts[k] is None:
            starts[k] = nxt
        nxt = starts[k]
    return starts


def edit_time(win, t_recording):
    """Seconds into the story of recording time t, or None when t isn't in the story.
    Inside a clip but in a part the cut settings take out: where the story picks up again."""
    if t_recording is None:
        return None
    try:
        t = float(t_recording)
    except (TypeError, ValueError):
        return None
    clips = _clips(win)
    if not clips:
        return None
    k_in = next((k for k, (a, b) in enumerate(clips) if a <= t <= b), None)
    if k_in is None:
        return None
    pos, after = 0.0, None
    sp = spans(win)
    shift = _shifts(win, sp)
    points = _shift_points(win, sp)
    for i, (k, a, b) in enumerate(sp):       # a clip's kept ranges come in recording order
        if k == k_in:
            if a <= t <= b:
                # a patch earlier in this span already added its extra time (as the player's clock does)
                return pos + (t - a) + sum(e for pb, e in points.get(i, []) if t >= pb - 0.01)
            if t < a:
                return pos           # in a bit the cut settings take out: the story picks up here
            after = pos + (b - a) + shift.get(i, 0.0)
        pos += max(0.0, b - a) + shift.get(i, 0.0)
    if after is not None:
        return after
    starts = clip_starts(win)
    return starts[k_in] if k_in < len(starts) else None
