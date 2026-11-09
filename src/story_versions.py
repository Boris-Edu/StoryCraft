#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: story_versions.py
ROLE: Story versions (pure: no Qt)
DESCRIPTION:
StoryCraft keeps versions of the story board so the user can identify, compare and roll back to the
stories they saved before. A version is a full copy of the board (sentences, Edit blocks in play order,
Not used blocks, edge overrides) with a name, a kind and a date. Versions live next to the board file:
saves/ai_story/<project>/<timeline>.versions.json (one JSON list, oldest first).

Kinds: "manual" (the user saved it, named), "send" (sent to Resolve), "ai" (an AI edit was imported),
"resolve" (reloaded from Resolve), "before_restore" (automatic safety copy made before a restore).
Every manual/send/ai/resolve version is kept; automatic ones (before_restore) are capped at the last 20.

diff(a, b) compares two stories block by block (what changed from a to b): blocks moved, added, removed,
shortened ("trimmed": sentences left out), lengthened ("extended": sentences brought in), split, joined,
and edges changed. Its summary line is English; a UI builds its own translated line from summary_counts().

Public API:
    versions_path(board_path) -> str | None
    state_of(board) -> {"edit", "unused", "air", "sentences"}
    snapshot(board, kind, name="", note="", duration_s=None) -> version dict
    save_version(path, version) -> version
    list_versions(path) -> [version] (newest first)
    get_version(path, vid) -> version | None
    rename_version(path, vid, name) -> bool
    delete_version(path, vid) -> bool   (manual and before_restore only)
    diff(a, b) -> {"summary", "changes", "same"}
    summary_counts(diff) -> {kind: count, "sentences_out": n, "sentences_in": n}
"""
import copy
import datetime
import json
import os
import uuid

KINDS = ("manual", "send", "ai", "resolve", "before_restore")
AUTO_KINDS = ("before_restore",)          # capped
DELETABLE = KINDS   # every version can be deleted (the Versions panel makes it undoable)
AUTO_CAP = 20
CHANGE_KINDS = ("moved", "added", "removed", "trimmed", "extended", "split", "joined", "edge")
EDGE_FIELDS = ("start_t", "end_t", "lead", "tail")
_EXCERPT = 90


# ----------------------------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------------------------

def versions_path(board_path):
    """<timeline>.board.json -> <timeline>.versions.json (same folder)."""
    if not board_path:
        return None
    p = str(board_path)
    if p.endswith(".board.json"):
        p = p[:-len(".board.json")]
    elif p.endswith(".json"):
        p = p[:-len(".json")]
    return p + ".versions.json"


def _blocks(lst):
    out = []
    for b in lst or []:
        try:
            out.append([int(b[0]), int(b[1])])
        except Exception:
            pass
    return out


def state_of(board):
    """The board as a plain state: deep copies, safe to keep."""
    return {"edit": _blocks(board.edit_blocks()),
            "unused": _blocks(board.unused_blocks()),
            "air": copy.deepcopy(dict(getattr(board, "edge_air", None) or {})),
            "sentences": copy.deepcopy(list(getattr(board, "sentences", None) or []))}


def state_duration(state, block_duration=None):
    """Seconds of the Edit blocks: block_duration((f, l)) when given (the board's, after cuts), else raw."""
    sentences = state.get("sentences") or []
    total = 0.0
    for f, l in _blocks(state.get("edit")):
        d = None
        if block_duration is not None:
            try:
                d = float(block_duration((f, l)))
            except Exception:
                d = None
        if d is None:
            try:
                d = float(sentences[l]["cut_end"]) - float(sentences[f]["cut_start"])
            except Exception:
                d = 0.0
        total += max(0.0, d)
    return total


def snapshot(board, kind, name="", note="", duration_s=None):
    """A version of the board as it is now."""
    st = state_of(board)
    if duration_s is None:
        duration_s = state_duration(st, getattr(board, "block_duration", None))
    now = datetime.datetime.now()
    v = {"id": now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
         "name": str(name or ""),
         "kind": kind if kind in KINDS else "manual",
         "created": now.isoformat(timespec="seconds")}
    v.update(st)
    v["duration_s"] = round(float(duration_s or 0.0), 3)
    v["note"] = str(note or "")
    return v


def _read(path):
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [v for v in data if isinstance(v, dict) and v.get("id")] if isinstance(data, list) else []
    except Exception:
        return []


def _write(path, versions):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(versions, f, ensure_ascii=False)
    os.replace(tmp, path)


def _cap(versions):
    autos = [v["id"] for v in versions if v.get("kind") in AUTO_KINDS]
    drop = set(autos[:-AUTO_CAP]) if len(autos) > AUTO_CAP else set()
    return [v for v in versions if v["id"] not in drop]


def save_version(path, version):
    """Append a version (stored oldest first); automatic ones beyond the last 20 are dropped."""
    versions = _read(path)
    versions.append(version)
    _write(path, _cap(versions))
    return version


def list_versions(path):
    """Every version, newest first."""
    versions = _read(path)
    order = {v["id"]: i for i, v in enumerate(versions)}
    return sorted(versions, key=lambda v: (str(v.get("created") or ""), order[v["id"]]), reverse=True)


def get_version(path, vid):
    return next((v for v in _read(path) if v.get("id") == vid), None)


def rename_version(path, vid, name):
    versions = _read(path)
    for v in versions:
        if v.get("id") == vid:
            v["name"] = str(name or "").strip()
            _write(path, versions)
            return True
    return False


def read_all(path):
    """The versions list as stored (for undo)."""
    return _read(path)


def write_all(path, versions):
    _write(path, list(versions or []))


def delete_version(path, vid):
    """Delete one version (any kind)."""
    versions = _read(path)
    keep = [v for v in versions if not (v.get("id") == vid and v.get("kind") in DELETABLE)]
    if len(keep) == len(versions):
        return False
    _write(path, keep)
    return True


def manual_count(path):
    return sum(1 for v in _read(path) if v.get("kind") == "manual")


# ----------------------------------------------------------------------------------------------
# Diff
# ----------------------------------------------------------------------------------------------

def _lis(seq):
    """Indices of one longest strictly increasing subsequence of seq."""
    n = len(seq)
    if not n:
        return set()
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(n), key=lambda k: (best[k], -k))
    out = set()
    while i != -1:
        out.add(i)
        i = prev[i]
    return out


class _Side:
    def __init__(self, state, use_ids):
        self.sentences = list(state.get("sentences") or [])
        self.edit = [tuple(b) for b in _blocks(state.get("edit"))]
        self.air = dict(state.get("air") or {})
        self.use_ids = use_ids
        self.blocks = [self.keys(b) for b in self.edit]
        self.sets = [set(k) for k in self.blocks]
        self.by_key = {}
        for i, s in enumerate(self.sentences):
            self.by_key[self._key(i)] = s

    def _key(self, i):
        if self.use_ids:
            return self.sentences[i].get("id")
        return i

    def keys(self, block):
        f, l = block
        if self.use_ids:
            return [self.sentences[i].get("id") for i in range(f, l + 1) if 0 <= i < len(self.sentences)]
        return list(range(f, l + 1))

    def edge(self, block):
        e = self.air.get(f"{block[0]}-{block[1]}") or {}
        out = []
        for k in EDGE_FIELDS:
            v = e.get(k)
            out.append(None if v is None else round(float(v), 3))
        return tuple(out)


def diff(a, b):
    """What changed from story a to story b. a and b: {"edit", "unused", "sentences", "air"} (a version works).

    Returns {"summary": one English line, "same": bool,
             "changes": [{"kind", "a_block", "b_block", "a_pos", "b_pos", "sentences": [ids], "text"}]}
    Blocks are matched by the sentences they share (sentence ids when the two sentence lists differ, indices
    when their ids are the same). Positions are 0-based places in the play order. A block counts as moved
    only when it is out of order relative to the other matched blocks (longest common subsequence), so
    blocks added or removed before it don't make it "moved". split changes also carry "b_blocks"
    (every piece), joined changes "a_blocks" (every joined block).
    """
    a, b = a or {}, b or {}
    ids_a = [s.get("id") for s in (a.get("sentences") or [])]
    ids_b = [s.get("id") for s in (b.get("sentences") or [])]
    use_ids = ids_a != ids_b and all(x is not None for x in ids_a + ids_b)
    A, B = _Side(a, use_ids), _Side(b, use_ids)

    def sid(k):
        if use_ids:
            return str(k)
        s = A.by_key.get(k) or B.by_key.get(k)
        return str((s or {}).get("id") or f"#{k}")

    def excerpt(keys, prefer="b"):
        sides = (B, A) if prefer == "b" else (A, B)
        words = []
        for k in keys:
            s = sides[0].by_key.get(k) or sides[1].by_key.get(k) or {}
            if s.get("text"):
                words.append(str(s["text"]).strip())
        t = " ".join(words)
        return t if len(t) <= _EXCERPT else t[:_EXCERPT - 1].rstrip() + "…"

    def change(kind, ai=None, bj=None, keys=(), prefer="b", **extra):
        c = {"kind": kind,
             "a_block": A.edit[ai] if ai is not None else None,
             "b_block": B.edit[bj] if bj is not None else None,
             "a_pos": ai, "b_pos": bj,
             "sentences": [sid(k) for k in keys],
             "text": excerpt(keys, prefer)}
        c.update(extra)
        return c

    # components of the overlap graph between a's and b's Edit blocks
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(A.edit)):
        find(("a", i))
    for j in range(len(B.edit)):
        find(("b", j))
    for i, sa in enumerate(A.sets):
        for j, sb in enumerate(B.sets):
            if sa & sb:
                parent[find(("a", i))] = find(("b", j))
    comps = {}
    for node in list(parent):
        comps.setdefault(find(node), []).append(node)

    changes, units = [], []   # units: (a_key, b_key, ai, bj, common keys) for the move check
    for nodes in comps.values():
        ai = sorted(n[1] for n in nodes if n[0] == "a")
        bj = sorted(n[1] for n in nodes if n[0] == "b")
        if not bj:
            for i in ai:
                changes.append(change("removed", ai=i, keys=A.blocks[i], prefer="a"))
            continue
        if not ai:
            for j in bj:
                changes.append(change("added", bj=j, keys=B.blocks[j]))
            continue
        if len(ai) == 1 and len(bj) == 1:
            i, j = ai[0], bj[0]
            common = [k for k in B.blocks[j] if k in A.sets[i]]
            units.append(((i, 0), (j, 0), i, j, common))
            if A.sets[i] == B.sets[j] and A.edge(A.edit[i]) != B.edge(B.edit[j]):
                changes.append(change("edge", ai=i, bj=j, keys=B.blocks[j]))
        elif len(ai) == 1:   # one block became several: split
            i = ai[0]
            pos_in = {k: n for n, k in enumerate(A.blocks[i])}
            pieces = sorted(bj, key=lambda j: min(pos_in[k] for k in B.sets[j] if k in pos_in))
            covered = [k for k in A.blocks[i] if any(k in B.sets[j] for j in bj)]
            changes.append(change("split", ai=i, bj=pieces[0], keys=covered,
                                  b_blocks=[B.edit[j] for j in pieces], b_positions=pieces))
            for j in pieces:
                units.append(((i, min(pos_in[k] for k in B.sets[j] if k in pos_in)), (j, 0), i, j,
                              [k for k in B.blocks[j] if k in A.sets[i]]))
        elif len(bj) == 1:   # several blocks became one: joined
            j = bj[0]
            pos_in = {k: n for n, k in enumerate(B.blocks[j])}
            parts = sorted(ai, key=lambda i: min(pos_in[k] for k in A.sets[i] if k in pos_in))
            covered = [k for k in B.blocks[j] if any(k in A.sets[i] for i in ai)]
            changes.append(change("joined", ai=parts[0], bj=j, keys=covered,
                                  a_blocks=[A.edit[i] for i in parts], a_positions=parts))
            for i in parts:
                units.append(((i, 0), (j, min(pos_in[k] for k in A.sets[i] if k in pos_in)), i, j,
                              [k for k in A.blocks[i] if k in B.sets[j]]))
        else:   # tangled: pair by largest overlap, the rest is split from / joined into its best partner
            pairs = sorted(((len(A.sets[i] & B.sets[j]), i, j) for i in ai for j in bj if A.sets[i] & B.sets[j]),
                           key=lambda t: (-t[0], t[1], t[2]))
            used_a, used_b = set(), set()
            for _, i, j in pairs:
                if i in used_a or j in used_b:
                    continue
                used_a.add(i)
                used_b.add(j)
                units.append(((i, 0), (j, 0), i, j, [k for k in B.blocks[j] if k in A.sets[i]]))
            for j in bj:
                if j not in used_b:
                    i = max(ai, key=lambda i: (len(A.sets[i] & B.sets[j]), -i))
                    common = [k for k in B.blocks[j] if k in A.sets[i]]
                    changes.append(change("split", ai=i, bj=j, keys=common, b_blocks=[B.edit[j]], b_positions=[j]))
                    units.append(((i, 1), (j, 0), i, j, common))
            for i in ai:
                if i not in used_a:
                    j = max(bj, key=lambda j: (len(A.sets[i] & B.sets[j]), -j))
                    common = [k for k in A.blocks[i] if k in B.sets[j]]
                    changes.append(change("joined", ai=i, bj=j, keys=common, a_blocks=[A.edit[i]], a_positions=[i]))
                    units.append(((i, 0), (j, 1), i, j, common))
        # sentences that left the story / came into it, inside this component
        in_b = set().union(*(B.sets[j] for j in bj))
        in_a = set().union(*(A.sets[i] for i in ai))
        for i in ai:
            lost = [k for k in A.blocks[i] if k not in in_b]
            if lost:
                j = max(bj, key=lambda j: (len(A.sets[i] & B.sets[j]), -j))
                changes.append(change("trimmed", ai=i, bj=j, keys=lost, prefer="a"))
        for j in bj:
            gained = [k for k in B.blocks[j] if k not in in_a]
            if gained:
                i = max(ai, key=lambda i: (len(A.sets[i] & B.sets[j]), -i))
                changes.append(change("extended", ai=i, bj=j, keys=gained))

    # moved: matched blocks out of order (longest common subsequence of the play order)
    units.sort(key=lambda u: u[0])
    keep = _lis([u[1] for u in units])
    for n, (_, _, i, j, common) in enumerate(units):
        if n not in keep:
            changes.append(change("moved", ai=i, bj=j, keys=common or B.blocks[j]))

    rank = {k: n for n, k in enumerate(CHANGE_KINDS)}
    changes.sort(key=lambda c: (c["b_pos"] if c["b_pos"] is not None else (c["a_pos"] or 0) - 0.5,
                                rank[c["kind"]]))
    out = {"summary": "", "changes": changes, "same": not changes}
    out["summary"] = summary_text(out)
    return out


def summary_counts(d):
    """Counts per change kind, plus the sentences left out (trimmed) and brought in (extended)."""
    counts = {k: 0 for k in CHANGE_KINDS}
    counts["sentences_out"] = counts["sentences_in"] = 0
    for c in (d or {}).get("changes") or []:
        k = c.get("kind")
        if k in counts:
            counts[k] += 1
        if k == "trimmed":
            counts["sentences_out"] += len(c.get("sentences") or [])
        elif k == "extended":
            counts["sentences_in"] += len(c.get("sentences") or [])
    return counts


def summary_text(d):
    """One short English line, e.g. '2 blocks moved · 1 block added · 3 sentences left out · 1 edge changed'."""
    c = summary_counts(d)
    if not any(c[k] for k in CHANGE_KINDS):
        return "same"

    def n(count, one, many):
        return f"{count} {one if count == 1 else many}"

    parts = []
    if c["moved"]:
        parts.append(n(c["moved"], "block moved", "blocks moved"))
    if c["added"]:
        parts.append(n(c["added"], "block added", "blocks added"))
    if c["removed"]:
        parts.append(n(c["removed"], "block removed", "blocks removed"))
    if c["sentences_out"]:
        parts.append(n(c["sentences_out"], "sentence left out", "sentences left out"))
    if c["sentences_in"]:
        parts.append(n(c["sentences_in"], "sentence brought in", "sentences brought in"))
    if c["split"]:
        parts.append(n(c["split"], "block split", "blocks split"))
    if c["joined"]:
        parts.append(n(c["joined"], "join", "joins"))
    if c["edge"]:
        parts.append(n(c["edge"], "edge changed", "edges changed"))
    return " · ".join(parts)
