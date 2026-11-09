#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: handlers/fresh_start.py
ROLE: Start fresh with a timeline: its saved StoryCraft work goes to the archive
DESCRIPTION:
Everything StoryCraft keeps for a Resolve timeline comes back by itself when that timeline is opened or
transcribed again (the transcript, the story board, its Versions, the voice patches in it). "Start fresh"
moves all of it out of the way, into saves/archive/<project>/<timeline> <date>/ (same layout inside, so
nothing is lost and it can be put back by hand):
  timelines/<project>/<timeline>.json (+ its ".before …" copies)
  ai_story/<project>/<timeline>.json / .board.json / .versions.json (+ the old project-less copies)
  voice_patches/<project>/<timeline>/  — except media/: those files are used by timelines already in Resolve
  edits/<project>/<source>/<timeline>.json — when the timeline is an edit StoryCraft sent (its saved state)
Nothing in Resolve changes.
"""
import glob
import os
import shutil
import time

from handlers import timeline_store


def _story_safe(name):
    """The file name main_window._ai_story_map_path uses for a timeline."""
    return "".join(c for c in os.path.basename(str(name)) if c.isalnum() or c in " -_").strip() or "Transcript"


def stores(saves, project_folder, timeline):
    """Existing paths (files or folders) that hold this timeline's saved work."""
    proj, tsafe, ssafe = project_folder or "No project", timeline_store._safe(timeline), _story_safe(timeline)
    out = []
    tdir = os.path.join(saves, "timelines", proj)
    out.append(os.path.join(tdir, tsafe + ".json"))
    out += glob.glob(os.path.join(glob.escape(tdir), glob.escape(tsafe) + ".before *.json"))
    for root in (os.path.join(saves, "ai_story", proj), os.path.join(saves, "ai_story")):
        for ext in (".json", ".board.json", ".versions.json"):
            out.append(os.path.join(root, ssafe + ext))
    try:   # an edit StoryCraft sent to Resolve: its saved state in the edits library would open instead
        from handlers import edit_library
        entry = edit_library.find_edit(saves, proj, timeline)
        if entry and entry.get("state_path"):
            out.append(entry["state_path"])
    except Exception:
        pass
    vdir = os.path.join(saves, "voice_patches", proj, tsafe)
    if os.path.isdir(vdir):
        out += [os.path.join(vdir, n) for n in os.listdir(vdir) if n != "media"]
    return [p for p in dict.fromkeys(out) if os.path.exists(p)]


def summary(saves, project_folder, timeline):
    """{"transcript": bool, "story": bool, "versions": int, "patches": int, "paths": [...]}."""
    paths = stores(saves, project_folder, timeline)
    names = [os.path.basename(p) for p in paths]
    versions = 0
    for p in paths:
        if p.endswith(".versions.json"):
            try:
                import json
                with open(p, encoding="utf-8") as f:
                    versions += len(json.load(f) or [])
            except Exception:
                pass
    return {"transcript": any(p.startswith(os.path.join(saves, "timelines")) for p in paths),
            "story": any(n.endswith(".board.json") for n in names), "versions": versions,
            "patches": sum(1 for n in names if n.startswith("ending_")), "paths": paths}


def archive(saves, project_folder, timeline):
    """Move this timeline's saved work to saves/archive/…; returns (archive folder or None, items moved)."""
    paths = stores(saves, project_folder, timeline)
    if not paths:
        return None, 0
    stamp = time.strftime("%Y-%m-%d %H.%M.%S")
    dest_root = os.path.join(saves, "archive", project_folder or "No project",
                             f"{timeline_store._safe(timeline)} {stamp}")
    moved = 0
    for p in paths:
        rel = os.path.relpath(p, saves)
        dst = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(p, dst)
            moved += 1
        except Exception:
            continue
    return dest_root, moved
