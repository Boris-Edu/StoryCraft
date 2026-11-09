#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: handlers/recent_info.py
ROLE: What Home says about a saved timeline's story (pure: no Qt, safe in a worker thread)
DESCRIPTION:
For a recent timeline on Home: the story it reopens (length and Edit blocks of its board), the voice
patches in that story, and whether it went to Resolve. Read from the files main_window writes:
  saves/ai_story/<project>/<timeline>.board.json      {"sentences": [...], "edit": [[f, l], ...]}
  saves/ai_story/<project>/<timeline>.versions.json   versions; kind "send" = sent to Resolve ("created")
  saves/voice_patches/<project>/<timeline safe>/accepted.json   [id | {"id": ...}, ...]
  preferences "sent_edits"                            {<timeline name or source file>: {"name", "sig", ...}}
Missing or corrupt files never raise: that part is just left out.
"""
import datetime
import json
import os

from handlers import timeline_store


def story_safe(name):
    """The file name main_window._ai_story_map_path uses for a timeline (letters, digits, space, - and _)."""
    return "".join(c for c in os.path.basename(str(name or "")) if c.isalnum() or c in " -_").strip() or "Transcript"


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _blocks(lst):
    out = []
    for b in lst if isinstance(lst, list) else []:
        try:
            out.append((int(b[0]), int(b[1])))
        except Exception:
            pass
    return out


def _duration(sentences, blocks):
    total = 0.0
    for f, l in blocks:
        try:
            a, b = sentences[f], sentences[l]
            s = a.get("cut_start", a.get("src_start"))
            e = b.get("cut_end", b.get("src_end"))
            total += max(0.0, float(e) - float(s))
        except Exception:
            pass
    return total


def _shape(sentences, blocks):
    """The story as sentence ids per block, to tell whether it changed since a version."""
    out = []
    for f, l in blocks:
        try:
            out.append((str(sentences[f].get("id", f)), str(sentences[l].get("id", l))))
        except Exception:
            out.append((str(f), str(l)))
    return out


def _ts(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso)).timestamp()
    except Exception:
        return None


def story_info(saves, project_dir, timeline_name, sent_key=None, sent_edits=None, patch_name=None):
    """{"story": bool, "length_s", "blocks", "patches", "sent": bool, "sent_ts", "changed": bool}.
    project_dir: the saves/timelines/<project> folder (main_window._story_project of a reopened timeline).
    sent_key: the timeline's key in sent_edits (main_window._sent_key: timeline name, else source file).
    patch_name: the name of its voice patch folder (main_window._voice_patch_dir), default timeline_name."""
    info = {"story": False, "length_s": 0.0, "blocks": 0, "patches": 0, "sent": False, "sent_ts": None,
            "changed": False}
    proj = project_dir or "No project"
    base = os.path.join(saves, "ai_story", proj, story_safe(timeline_name))
    board = _load(base + ".board.json")
    sentences, blocks = [], []
    if isinstance(board, dict):
        sentences = board.get("sentences") if isinstance(board.get("sentences"), list) else []
        blocks = [(f, l) for f, l in _blocks(board.get("edit")) if 0 <= f <= l < len(sentences)]
        if blocks:
            info.update(story=True, length_s=_duration(sentences, blocks), blocks=len(blocks))
    acc = _load(os.path.join(saves, "voice_patches", proj, timeline_store._safe((timeline_name if patch_name is None else patch_name) or "Timeline"),
                             "accepted.json"))
    if isinstance(acc, list):
        info["patches"] = sum(1 for x in acc if (isinstance(x, dict) and x.get("id") is not None)
                              or isinstance(x, (str, int)))
    # Sent to Resolve: the newest "send" version (when, and what it held), else the preferences entry
    versions = _load(base + ".versions.json")
    sends = [v for v in versions if isinstance(v, dict) and v.get("kind") == "send"] if isinstance(versions, list) else []
    sends = [(t, v) for t, v in ((_ts(v.get("created")), v) for v in sends) if t is not None]
    if sends:
        t, v = max(sends, key=lambda x: x[0])
        info.update(sent=True, sent_ts=t)
        if info["story"]:
            vs = v.get("sentences") if isinstance(v.get("sentences"), list) else []
            info["changed"] = _shape(vs, _blocks(v.get("edit"))) != _shape(sentences, blocks)
    if not info["sent"] and sent_key and isinstance(sent_edits, dict) and sent_edits.get(sent_key):
        info["sent"] = True
    return info
