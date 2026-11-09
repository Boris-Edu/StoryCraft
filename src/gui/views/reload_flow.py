#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/views/reload_flow.py
ROLE: Reload from Resolve (the sent timeline, edited by hand, back into the story)
DESCRIPTION:
Reads the timeline StoryCraft last sent for this transcript (resolve_roundtrip), turns it into a story
and compares it with the story now (story_versions.diff, shown with render_diff):
  - only Resolve changed: Use the Resolve version / Keep it as a version only;
  - both changed since the last Send: Sync Resolve (send the story now as a new timeline), Sync
    StoryCraft (take the Resolve edit), Transcribe as a new story (that timeline, from scratch).
Taking the Resolve edit keeps the story it replaces as a version, saves the Resolve one as a version
("From Resolve"), and is one undo step.
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

import config


def _t(win, key, default):
    return win.txt(key, default)


def _note(win, text, ms=8000):
    sv = getattr(win, 'story_view', None)
    if sv is not None and win._stack.currentIndex() == 5 and hasattr(sv, 'note'):
        sv.note(text, ms)
    else:
        win._show_temporary_status(text, ms)


def _msg(win, text):
    from gui.components.dialogs import CustomMsgBox
    CustomMsgBox(win, _t(win, "rt_title", "Reload from Resolve"), text, win.txt("btn_ok", "OK")).exec()


def read_resolve_story(win):
    """(resolve_state, info) or (None, error text)."""
    import resolve_roundtrip as RT
    rh = getattr(win.engine, 'resolve_handler', None) or getattr(win, 'resolve_handler', None)
    if rh is None or getattr(rh, 'backend', None) != 'native' or getattr(rh, 'project', None) is None:
        return None, _t(win, "rt_need_native", "Reloading needs the direct connection to DaVinci Resolve (Studio, "
                                                "External scripting: Local) with this project open.")
    board = getattr(win, 'story_board', None)
    canvas = getattr(win, 'text_canvas', None)
    if board is None or not getattr(board, 'sentences', None) or canvas is None or not canvas.words_data:
        return None, _t(win, "rt_no_story", "Open the transcript and its story first.")
    prefs = win.engine.load_preferences() or {}
    sent = (prefs.get('sent_edits') or {}).get(win._sent_key()) or {}
    edit_name = sent.get('name')
    if not edit_name:
        return None, _t(win, "rt_nothing_sent", "Nothing was sent to Resolve from this transcript yet: Send to "
                                                 "Resolve first, edit that timeline, then reload it.")
    src = getattr(win, '_transcription_source', None) or {}
    source_tl = src.get('timeline_name')
    track = (src.get('track_indices') or [1])[0]
    fps = float(getattr(rh, 'fps', None) or 24.0)
    segs, outside, err = RT.read_segments(rh.project, edit_name, source_tl, fps, source_track=track)
    legacy = False
    if not err and not segs and outside:
        # sent before StoryCraft flagged its clips: read every clip of the source's media
        segs, outside, err = RT.read_segments(rh.project, edit_name, source_tl, fps, source_track=track, flag=None)
        legacy = True
    if err:
        return None, _t(win, "rt_read_failed", "Couldn't read the timeline from Resolve: {err}").format(err=err)
    words = canvas.words_data
    preview = getattr(canvas, '_cut_preview', None) or {}
    is_cut = lambda i: preview.get(words[i].get('id')) == 'cut' or bool(words[i].get('is_assembled_cut'))
    edit, air, rep = RT.segments_to_story(segs, board.sentences, words, is_cut)
    if not edit:
        return None, _t(win, "rt_empty", "No StoryCraft clips were found in “{name}”.").format(name=edit_name)
    cur_air = dict(getattr(board, 'edge_air', None) or {})
    for f, l in edit:                       # Review's pause choices stay on blocks that are still there
        key = f"{f}-{l}"
        keep = {k: v for k, v in (cur_air.get(key) or {}).items() if k in ("lead", "tail")}
        if keep:
            air[key] = dict(keep, **air.get(key, {}))
    state = {"edit": [tuple(b) for b in edit], "unused": RT.unused_for(edit, len(board.sentences)),
             "air": air, "sentences": board.sentences}
    info = {"name": edit_name, "outside": outside, "report": rep, "legacy": legacy,
            "resolve_changed": win._timeline_fp(edit_name) != sent.get('fp'),
            "story_changed": getattr(win, '_edit_signature', None) != sent.get('sig')}
    return state, info


class ReloadDialog(QDialog):
    """The difference and the choices (both changed: three ways; only Resolve: use it or keep it)."""

    def __init__(self, win, diff_dict, info, parent=None):
        super().__init__(parent or win)
        self.win, self.choice = win, None
        self.setWindowTitle(_t(win, "rt_title", "Reload from Resolve"))
        self.setModal(True)
        self.setMinimumWidth(config.S(620))
        self.setStyleSheet(f"""
            QDialog {{ background-color: {config.BG_COLOR}; }}
            QLabel {{ color: #d4d4d4; font-size: {config.FS(10)}pt; background: transparent; }}
            QLabel#Title {{ color: #ffffff; font-size: {config.FS(14)}pt; font-weight: 600; }}
            QPushButton {{ background: #262626; color: #e6e6e6; border: 1px solid #333333; border-radius: {config.S(8)}px;
                padding: {config.S(7)}px {config.S(14)}px; font-size: {config.FS(10)}pt; }}
            QPushButton:hover {{ background: #2e2e2e; color: #ffffff; }}
            QPushButton#Primary {{ background: {config.BTN_BG}; color: #ffffff; border: none; font-weight: 600; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(config.S(20), config.S(18), config.S(20), config.S(16))
        lay.setSpacing(config.S(10))
        both = info["resolve_changed"] and info["story_changed"]
        title = QLabel(_t(win, "rt_both_title", "The story changed in both places") if both else
                       _t(win, "rt_resolve_title", "“{name}” was edited in Resolve").format(name=info["name"]))
        title.setObjectName("Title")
        lay.addWidget(title)
        intro = (_t(win, "rt_both_intro", "Since the last Send, “{name}” was edited in Resolve and the story was changed "
                    "in StoryCraft. Below: what the Resolve edit changes compared with the story now.")
                 if both else _t(win, "rt_resolve_intro", "Below: what the Resolve edit changes compared with the story now. "
                                 "The story now stays in Versions either way."))
        lb = QLabel(intro.format(name=info["name"]))
        lb.setWordWrap(True)
        lay.addWidget(lb)
        from gui.views.story_versions_view import render_diff
        view = render_diff(self, diff_dict, _t(win, "rt_label_now", "The story now"),
                           _t(win, "rt_label_resolve", "Resolve"), win=win)
        view.setMinimumHeight(config.S(220))
        lay.addWidget(view, 1)
        notes = []
        if info["outside"]:
            notes.append(_t(win, "rt_outside", "{n} clip(s) StoryCraft didn't lay (B-roll, other media) stay in Resolve "
                            "as they are.").format(n=len(info["outside"])))
        if info["report"].get("duplicates"):
            notes.append(_t(win, "rt_dups", "Words used twice in the timeline are in the story once."))
        if info["legacy"]:
            notes.append(_t(win, "rt_legacy", "This timeline was sent before StoryCraft flagged its clips: every clip "
                            "of the interview was read."))
        if notes:
            nl = QLabel(" ".join(notes))
            nl.setWordWrap(True)
            nl.setStyleSheet(f"color: #a3a3a3; font-size: {config.FS(9)}pt;")
            lay.addWidget(nl)
        row = QHBoxLayout()
        row.setSpacing(config.S(8))
        cancel = QPushButton(win.txt("btn_cancel", "Cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addStretch(1)
        if both:
            acts = (("transcribe", _t(win, "rt_transcribe", "Transcribe as a new story"),
                     _t(win, "rt_tt_transcribe", "Start a new transcript from the edited timeline")),
                    ("sync_resolve", _t(win, "rt_sync_resolve", "Sync Resolve"),
                     _t(win, "rt_tt_sync_resolve", "Send the story now to Resolve as a new timeline")),
                    ("use", _t(win, "rt_sync_storycraft", "Sync StoryCraft"),
                     _t(win, "rt_tt_sync_storycraft", "Take the Resolve edit as the story (the story now stays in Versions)")))
        else:
            acts = (("keep", _t(win, "rt_keep_version", "Keep it as a version only"),
                     _t(win, "rt_tt_keep_version", "Save the Resolve edit in Versions without changing the story")),
                    ("use", _t(win, "rt_use_resolve", "Use the Resolve version"),
                     _t(win, "rt_tt_use_resolve", "Take the Resolve edit as the story (the story now stays in Versions)")))
        self.buttons = {}
        for k, text, tip in acts:
            b = QPushButton(text)
            b.setToolTip(tip)
            if k == "use":
                b.setObjectName("Primary")
            b.clicked.connect(lambda _=False, k=k: self._pick(k))
            self.buttons[k] = b
            row.addWidget(b)
        lay.addLayout(row)
        self.resize(config.S(720), config.S(560))

    def _pick(self, k):
        self.choice = k
        self.accept()


def run_reload(win, ask=None):
    """Reload from Resolve. ask: test hook replacing the dialog (returns a choice key or None)."""
    import story_versions as SV
    state, info = read_resolve_story(win)
    if state is None:
        _msg(win, info)
        return None
    board = win.story_board
    d = SV.diff(SV.state_of(board), state)
    if d.get("same"):
        _note(win, _t(win, "rt_same", "Resolve has the same story as StoryCraft."))
        return "same"
    if ask is not None:
        choice = ask(d, info)
    else:
        dlg = ReloadDialog(win, d, info)
        choice = dlg.choice if dlg.exec() else None
    if choice in (None, "cancel"):
        return None
    if choice == "use":
        path = SV.versions_path(board._path()) if hasattr(board, '_path') else None
        if path:
            SV.save_version(path, SV.snapshot(board, "before_restore", note=_t(
                win, "rt_before_note", "Before reloading “{name}” from Resolve").format(name=info["name"])))
        board.edge_air = dict(state["air"])
        board.load(board.sentences, state["edit"], state["unused"])
        if hasattr(win, '_story_version_auto'):
            win._story_version_auto("resolve", note=info["name"])
        # StoryCraft and the timeline now tell the same story: Send says "In Resolve" again
        QTimer.singleShot(900, lambda: win._remember_sent(info["name"]))
        import sys
        key = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"
        _note(win, _t(win, "rt_used", "The story now follows “{name}” ({key} undoes; the previous story is in "
                      "Versions)").format(name=info["name"], key=key))
    elif choice == "keep":
        path = SV.versions_path(board._path())
        v = SV.snapshot(board, "resolve", note=info["name"])
        v.update({"edit": [list(b) for b in state["edit"]], "unused": [list(b) for b in state["unused"]],
                  "air": state["air"]})
        try:
            v["duration_s"] = round(float(SV.state_duration(state, getattr(board, "block_duration", None)) or 0), 3)
        except Exception:
            pass
        SV.save_version(path, v)
        _note(win, _t(win, "rt_kept", "The Resolve edit is in Versions; the story didn't change."))
    panel = getattr(win, "_versions_panel", None)
    if panel is not None and choice in ("use", "keep"):
        try:
            panel.refresh()          # the new version shows in the list at once
        except Exception:
            pass
    if choice == "sync_resolve":
        win._on_assemble()
    elif choice == "transcribe":
        rh = getattr(win.engine, 'resolve_handler', None)
        try:
            rh.set_current_timeline(info["name"])
        except Exception:
            pass
        if hasattr(win, 'show_setup_form'):
            win.show_setup_form()
        win._show_temporary_status(_t(win, "rt_transcribe_hint", "“{name}” is the current timeline in Resolve: start the "
                                      "transcription to make it a new story.").format(name=info["name"]), 10000)
    return choice
