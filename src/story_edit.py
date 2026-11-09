#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: story_edit.py
ROLE: Add a passage to the story / leave it out (from the transcript)
DESCRIPTION:
The story board's blocks are sentence ranges (first, last). A passage the user selects in the transcript
becomes the sentences it touches, (a, b):
  include: the sentences not in the story yet extend the block they touch, else become a new block right
           after the block that comes before them in the recording (existing blocks keep their order);
  exclude: cut out of every Edit block (a block can split in two); those sentences go to Not used.
Not used never holds what is in the Edit list.
"""


def _minus(block, a, b):
    """block (f, l) without sentences a..b: 0, 1 or 2 pieces."""
    f, l = block
    out = []
    if f < a:
        out.append((f, min(l, a - 1)))
    if l > b:
        out.append((max(f, b + 1), l))
    return [p for p in out if p[0] <= p[1]]


def sentences_for_span(sentences, t0, t1):
    """(a, b): the sentences a passage [t0, t1] (seconds of the recording) touches, or None."""
    idx = [i for i, s in enumerate(sentences)
           if float(s.get("src_start", s.get("cut_start", 0))) < t1 - 0.02
           and float(s.get("src_end", s.get("cut_end", 0))) > t0 + 0.02]
    return (min(idx), max(idx)) if idx else None


def include(edit, unused, a, b):
    """New (edit, unused) with sentences a..b in the story. Only the sentences not in it yet are added:
    each missing stretch extends the block it directly follows (or precedes) in the recording, else it
    becomes a new block right after the block that comes before it. Existing blocks keep their place."""
    edit = [tuple(x) for x in edit]
    used = {i for f, l in edit for i in range(f, l + 1)}
    missing = [i for i in range(a, b + 1) if i not in used]
    runs = []
    for i in missing:
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    for r0, r1 in runs:
        prev = next((k for k, (f, l) in enumerate(edit) if l == r0 - 1), None)
        nxt = next((k for k, (f, l) in enumerate(edit) if f == r1 + 1), None)
        if prev is not None:
            edit[prev] = (edit[prev][0], r1)
        elif nxt is not None:
            edit[nxt] = (r0, edit[nxt][1])
        else:
            before = [k for k, (f, l) in enumerate(edit) if l < r0]
            at = (max(before, key=lambda k: edit[k][1]) + 1) if before else 0
            edit.insert(at, (r0, r1))
    new_unused = []
    for blk in unused:
        pieces = [tuple(blk)]
        for r0, r1 in runs:
            pieces = [q for p in pieces for q in _minus(p, r0, r1)]
        new_unused.extend(pieces)
    return edit, sorted(new_unused)


def exclude(edit, unused, a, b):
    """New (edit, unused) with sentences a..b out of the story (they join Not used)."""
    new_edit, removed = [], []
    for blk in edit:
        f, l = blk
        if l < a or f > b:
            new_edit.append(tuple(blk))
            continue
        new_edit.extend(_minus((f, l), a, b))
        removed.append((max(f, a), min(l, b)))
    pool = sorted(set(tuple(x) for x in unused) | set(removed))
    merged = []
    for f, l in pool:   # neighbouring Not used pieces become one block
        if merged and f <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], l))
        else:
            merged.append((f, l))
    return new_edit, merged, bool(removed)
