#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MODULE: edit_library.py
ROLE: Handler (files only; Resolve is reached through the resolve handler passed in)
DESCRIPTION:
Every edit cut from the same source timeline, kept together:
  <saves>/edits/<project>/<source timeline>/
      source <date>.drt   backup of the source timeline (a new one only when the source changed)
      source.json         {"fingerprint", "file", "saved"} of the latest backup
      edits.json          [{"name", "created", "length_s", "clips", "state"}]
      <edit name>.json    the StoryCraft state of that edit: transcript, marks, speakers, story board
Loading an edit restores that state and opens its timeline in Resolve.
"""

import json
import os
import time


def _safe(name):
    s = "".join(c for c in str(name or "") if c.isalnum() or c in " -_.()").strip()
    return s[:120] or "Untitled"


def folder_for(saves_dir, project, source_timeline):
    return os.path.join(saves_dir, "edits", _safe(project or "No project"), _safe(source_timeline))


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def backup_source(rh, saves_dir, project, source_timeline):
    """Keep a .drt copy of the source timeline; a new one only when its content changed.
    Returns the backup path, or None when Resolve can't export it."""
    if not rh or not source_timeline:
        return None
    folder = folder_for(saves_dir, project, source_timeline)
    meta_path = os.path.join(folder, "source.json")
    meta = _read(meta_path, {})
    try:
        fp = rh.compute_timeline_fingerprint(source_timeline)
    except Exception:
        fp = None
    if fp and meta.get("fingerprint") == fp and os.path.isfile(meta.get("file", "")):
        return meta["file"]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"source {time.strftime('%Y-%m-%d %H.%M.%S')}.drt")
    try:
        ok, _start = rh.export_timeline_drt(source_timeline, path)
    except Exception:
        ok = False
    if not ok or not os.path.isfile(path):
        return None
    _write(meta_path, {"fingerprint": fp, "file": path, "saved": time.time(), "timeline": source_timeline})
    return path


def source_backup(saves_dir, project, source_timeline):
    """(path, saved time) of the latest source backup, or None."""
    meta = _read(os.path.join(folder_for(saves_dir, project, source_timeline), "source.json"), {})
    if meta.get("file") and os.path.isfile(meta["file"]):
        return meta["file"], meta.get("saved") or os.path.getmtime(meta["file"])
    return None


def record_edit(saves_dir, project, source_timeline, edit_name, length_s, packet, board=None, clips=None,
                signature=None):
    """Remember a new edit of this source: its StoryCraft state (packet + story board) and its entry."""
    folder = folder_for(saves_dir, project, source_timeline)
    state = os.path.join(folder, _safe(edit_name) + ".json")
    data = dict(packet or {})
    if board:
        data["story_board"] = board
    _write(state, data)
    index_path = os.path.join(folder, "edits.json")
    edits = [e for e in _read(index_path, []) if e.get("name") != edit_name]
    edits.append({"name": edit_name, "created": time.time(), "length_s": round(float(length_s or 0), 2),
                  "clips": int(clips or 0), "state": os.path.basename(state), "sig": signature})
    _write(index_path, edits)
    return state


def list_edits(saves_dir, project, source_timeline, sort="date"):
    """The edits of this source, newest first ("date") or longest first ("length").
    Each entry gets "state_path"; entries whose state file is gone are left out."""
    folder = folder_for(saves_dir, project, source_timeline)
    out = []
    for e in _read(os.path.join(folder, "edits.json"), []):
        p = os.path.join(folder, e.get("state", ""))
        if e.get("state") and os.path.isfile(p):
            out.append(dict(e, state_path=p))
    key = (lambda e: -float(e.get("length_s") or 0)) if sort == "length" else (lambda e: -float(e.get("created") or 0))
    return sorted(out, key=key)


def forget_edit(saves_dir, project, source_timeline, edit_name):
    """Remove an edit from the list (its timeline in Resolve is not touched)."""
    folder = folder_for(saves_dir, project, source_timeline)
    index_path = os.path.join(folder, "edits.json")
    edits = _read(index_path, [])
    keep = [e for e in edits if e.get("name") != edit_name]
    for e in edits:
        if e.get("name") == edit_name and e.get("state"):
            try:
                os.remove(os.path.join(folder, e["state"]))
            except OSError:
                pass
    _write(index_path, keep)


def find_edit(saves_dir, project, timeline_name):
    """The library entry of an edit timeline, from any source of this project, or None.
    Adds "state_path" and "source" (the source timeline's folder name)."""
    if not timeline_name:
        return None
    root = os.path.join(saves_dir, "edits", _safe(project or "No project"))
    if not os.path.isdir(root):
        return None
    for src in sorted(os.listdir(root)):
        folder = os.path.join(root, src)
        for e in _read(os.path.join(folder, "edits.json"), []):
            if e.get("name") == timeline_name and e.get("state"):
                p = os.path.join(folder, e["state"])
                if os.path.isfile(p):
                    return dict(e, state_path=p, source=src)
    return None
