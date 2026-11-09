#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: story_versions_view.py
ROLE: GUI (the Versions panel of the Story page, and a reusable diff view)
DESCRIPTION:
The versions of the story (story_versions.py) for the open timeline: save the story as a named version,
see every version (newest first) with how it differs from the story now, compare one with the story now,
restore it (the story now is kept first as a "Before restore" version; one undo step, so Cmd+Z takes the
restore back), rename it, delete it (the ones the user saved and the automatic safety copies).
The panel lies over the Story page under its top bar; the Versions button there opens and closes it.

Public API:
    show_story_versions(win) -> VersionsPanel      (win.show_story_versions() calls it)
    auto_version(win, kind, note="", name="") -> version | None   (send / ai / resolve; never raises)
    render_diff(parent, diff_dict, a_label, b_label, win=None) -> QWidget
    summary_line(win, diff_dict) -> str           (translated one-line summary from summary_counts)
    version_label(win, version) -> str
"""
import datetime
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

import config
import story_versions as sv

S, FS = config.S, config.FS
_UNDO_KEY = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"

PAGE_BG = "#1e1e1e"
CARD = "#262626"
WELL = "#222222"
LINE = "#303030"
TEXT = "#ffffff"
TEXT_2 = "#d4d4d4"
MUTED = "#8a8a8a"
CYAN = "#67e8f9"
VIOLET = "#c084fc"
GRAD = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"

# change kind -> (text colour, background)
_KIND_COLORS = {
    "moved": (CYAN, "#10303a"), "edge": (CYAN, "#10303a"),
    "added": ("#86efac", "#14301f"), "extended": ("#86efac", "#14301f"),
    "removed": ("#fca5a5", "#3a1d1d"), "trimmed": ("#fcd34d", "#3a2f10"),
    "split": ("#d8b4fe", "#2a2140"), "joined": ("#d8b4fe", "#2a2140"),
}
# version kind -> (text colour, background)
_VKIND_COLORS = {
    "manual": ("#86efac", "#14301f"), "send": (CYAN, "#10303a"), "resolve": (CYAN, "#10303a"),
    "ai": ("#d8b4fe", "#2a2140"), "before_restore": ("#a3a3a3", "#2a2a2a"),
}


def _find_win(widget):
    w = widget
    while w is not None:
        if hasattr(w, "story_board") and hasattr(w, "lang"):
            return w
        w = w.parentWidget() if hasattr(w, "parentWidget") else None
    for top in QApplication.topLevelWidgets():
        if hasattr(top, "story_board") and hasattr(top, "lang"):
            return top
    return None


def _tr(win):
    return (lambda k, d: win.txt(k, d)) if win is not None else (lambda k, d: d)


def _label(text, size, color, weight=400, wrap=False):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(f"color: {color}; font-size: {FS(size)}pt; font-weight: {weight}; background: transparent;"
                      f" border: none; padding: 0;")
    return lbl


def _pill(text, fg, bg):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {fg}; background-color: {bg}; border-radius: {S(8)}px; padding: {S(1)}px {S(8)}px;"
                      f" font-size: {FS(8.5)}pt; font-weight: 600;")
    lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return lbl


def _mmss(seconds):
    seconds = int(round(max(0.0, float(seconds or 0))))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _ids_text(ids):
    """S012, S013, S014, S020 -> 'S012–S014, S020'."""
    import re
    out, run = [], []

    def flush():
        if run:
            out.append(run[0] if len(run) == 1 else f"{run[0]}–{run[-1]}")
            run.clear()

    prev = None
    for s in ids:
        m = re.match(r"^(.*?)(\d+)$", str(s))
        cur = (m.group(1), int(m.group(2))) if m else None
        if run and not (cur and prev and cur[0] == prev[0] and cur[1] == prev[1] + 1):
            flush()
        run.append(str(s))
        prev = cur
    flush()
    return ", ".join(out)


def kind_label(win, kind):
    t = _tr(win)
    return {"manual": t("ver_kind_manual", "Saved"), "send": t("ver_kind_send", "Sent to Resolve"),
            "ai": t("ver_kind_ai", "AI story"), "resolve": t("ver_kind_resolve", "From Resolve"),
            "before_restore": t("ver_kind_before_restore", "Before restore")}.get(kind, kind)


def version_label(win, v):
    return (v.get("name") or "").strip() or kind_label(win, v.get("kind"))


def _when(v):
    try:
        d = datetime.datetime.fromisoformat(str(v.get("created")))
        return d.strftime("%Y-%m-%d · %H:%M")
    except Exception:
        return str(v.get("created") or "")


def summary_line(win, d):
    """The diff's summary in the UI language, from the counts."""
    t = _tr(win)
    c = sv.summary_counts(d)
    if (d or {}).get("same", not (d or {}).get("changes")):
        return t("ver_no_changes", "No differences")
    parts = []
    for key, k, default in (("moved", "ver_s_moved", "Moved: {n}"), ("added", "ver_s_added", "Added: {n}"),
                            ("removed", "ver_s_removed", "Removed: {n}"),
                            ("sentences_out", "ver_s_out", "Sentences left out: {n}"),
                            ("sentences_in", "ver_s_in", "Sentences brought in: {n}"),
                            ("split", "ver_s_split", "Split: {n}"), ("joined", "ver_s_joined", "Joined: {n}"),
                            ("edge", "ver_s_edge", "Edges changed: {n}")):
        if c.get(key):
            parts.append(t(k, default).format(n=c[key]))
    return " · ".join(parts)


def _change_detail(t, c):
    k = c.get("kind")
    a = (c.get("a_pos") + 1) if c.get("a_pos") is not None else "?"
    b = (c.get("b_pos") + 1) if c.get("b_pos") is not None else "?"
    if k == "moved":
        return t("ver_d_moved", "Was block {a}, now block {b}").format(a=a, b=b)
    if k == "added":
        return t("ver_d_added", "New block {b}").format(b=b)
    if k == "removed":
        return t("ver_d_removed", "Block {a} is no longer in the story").format(a=a)
    if k == "trimmed":
        return t("ver_d_trimmed", "Left out of block {a}").format(a=a)
    if k == "extended":
        return t("ver_d_extended", "Brought into block {b}").format(b=b)
    if k == "split":
        pos = c.get("b_positions") or [c.get("b_pos")]
        return t("ver_d_split", "Block {a} split: now blocks {list}").format(
            a=a, list=", ".join(str(p + 1) for p in pos if p is not None))
    if k == "joined":
        pos = c.get("a_positions") or [c.get("a_pos")]
        return t("ver_d_joined", "Blocks {list} joined into block {b}").format(
            b=b, list=", ".join(str(p + 1) for p in pos if p is not None))
    if k == "edge":
        return t("ver_d_edge", "The edges of block {b} changed").format(b=b)
    return k or ""


def _kind_name(t, k):
    return {"moved": t("ver_k_moved", "Moved"), "added": t("ver_k_added", "Added"),
            "removed": t("ver_k_removed", "Removed"), "trimmed": t("ver_k_trimmed", "Shortened"),
            "extended": t("ver_k_extended", "Lengthened"), "split": t("ver_k_split", "Split"),
            "joined": t("ver_k_joined", "Joined"), "edge": t("ver_k_edge", "Edge")}.get(k, k)


def render_diff(parent, diff_dict, a_label, b_label, win=None):
    """A readable view of story_versions.diff(a, b): a header 'a_label → b_label', the summary, then one row
    per change (what happened, the sentence ids, the words). Scrolls by itself; embed it anywhere."""
    win = win or _find_win(parent)
    t = _tr(win)
    d = diff_dict or {}
    box = QFrame(parent)
    box.setObjectName("DiffView")
    box.setStyleSheet(f"""
        QFrame#DiffView {{ background: transparent; border: none; }}
        QWidget#DiffHost {{ background: transparent; }}
        QFrame#DiffRow {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {S(10)}px; }}
        QScrollArea {{ background: transparent; border: none; }}
    """)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(S(8))
    head = _label(f"{a_label}  →  {b_label}", 11, TEXT, 600, wrap=True)
    head.setObjectName("DiffHead")
    lay.addWidget(head)
    summ = _label(summary_line(win, d), 9.5, VIOLET if d.get("changes") else MUTED, 500, wrap=True)
    summ.setObjectName("DiffSummary")
    lay.addWidget(summ)
    if not d.get("changes"):
        lay.addWidget(_label(t("ver_no_diff", "The two stories are the same."), 9.5, TEXT_2, wrap=True))
        lay.addStretch(1)
        box.rows = []
        return box
    scroll = QScrollArea(box)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    host = QWidget()
    host.setObjectName("DiffHost")
    hl = QVBoxLayout(host)
    hl.setContentsMargins(0, 0, S(8), 0)
    hl.setSpacing(S(6))
    rows = []
    for c in d.get("changes") or []:
        row = QFrame()
        row.setObjectName("DiffRow")
        row.change = c
        rl = QHBoxLayout(row)
        rl.setContentsMargins(S(12), S(9), S(12), S(9))
        rl.setSpacing(S(12))
        fg, bg = _KIND_COLORS.get(c.get("kind"), (TEXT_2, "#2a2a2a"))
        pill = _pill(_kind_name(t, c.get("kind")), fg, bg)
        pill.setMinimumWidth(S(86))
        pill.setAlignment(Qt.AlignCenter)
        rl.addWidget(pill, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(S(3))
        top = QHBoxLayout()
        top.setSpacing(S(10))
        det = _label(_change_detail(t, c), 9.5, TEXT, 600, wrap=True)
        top.addWidget(det, 1)
        ids = _label(_ids_text(c.get("sentences") or []), 8.5, MUTED, 500)
        ids.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(ids, 0, Qt.AlignTop)
        col.addLayout(top)
        if c.get("text"):
            words = _label(f"“{c['text']}”", 9.5, TEXT_2, wrap=True)
            words.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            col.addWidget(words)
        rl.addLayout(col, 1)
        hl.addWidget(row)
        rows.append(row)
    hl.addStretch(1)
    scroll.setWidget(host)
    lay.addWidget(scroll, 1)
    box.rows = rows
    return box


# ----------------------------------------------------------------------------------------------
# Automatic versions
# ----------------------------------------------------------------------------------------------

def _versions_path(win):
    board = getattr(win, "story_board", None)
    if board is None:
        return None
    try:
        return sv.versions_path(board._path())
    except Exception:
        return None


def auto_version(win, kind, note="", name=""):
    """Keep the story now as a version of `kind` ("send", "ai", "resolve", "before_restore"). Nothing when the
    story is empty or the newest version of that kind is already this story. Never raises."""
    try:
        board = getattr(win, "story_board", None)
        path = _versions_path(win)
        if board is None or not path or not board.sentences or not board.edit_blocks():
            return None
        v = sv.snapshot(board, kind, name=name, note=note)
        last = next((x for x in sv.list_versions(path) if x.get("kind") == kind), None)
        if last is not None and kind != "before_restore" and sv.diff(last, v)["same"]:
            return last
        sv.save_version(path, v)
        panel = getattr(win, "_versions_panel", None)
        if panel is not None and panel.isVisible():
            panel.schedule_refresh()
        return v
    except Exception as e:
        try:
            from osdoc import log_error
            log_error(f"Story version ({kind}): {e}")
        except Exception:
            pass
        return None


# ----------------------------------------------------------------------------------------------
# The panel
# ----------------------------------------------------------------------------------------------

class _Follow(QObject):
    """Keeps the panel over the Story page when the page is resized."""
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Resize, QEvent.Show):
            QTimer.singleShot(0, self.panel.place)
        return False


class _VersionCard(QFrame):
    def __init__(self, panel, v, d):
        super().__init__()
        self.panel, self.v, self.diff = panel, v, d
        t = panel.t
        self.setObjectName("VerCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(14), S(11), S(14), S(11))
        lay.setSpacing(S(5))
        top = QHBoxLayout()
        top.setSpacing(S(8))
        self.lbl_name = _label(version_label(panel.win, v), 10.5, TEXT, 600)
        self.lbl_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_name.setToolTip(version_label(panel.win, v))
        self.edit_name = QLineEdit(v.get("name") or "")
        self.edit_name.setObjectName("Search")
        self.edit_name.setPlaceholderText(kind_label(panel.win, v.get("kind")))
        self.edit_name.hide()
        self.edit_name.returnPressed.connect(self._commit_rename)
        top.addWidget(self.lbl_name, 1)
        top.addWidget(self.edit_name, 1)
        fg, bg = _VKIND_COLORS.get(v.get("kind"), (TEXT_2, "#2a2a2a"))
        self.badge = _pill(kind_label(panel.win, v.get("kind")), fg, bg)
        top.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.badge.setVisible(bool((v.get("name") or "").strip()))   # unnamed: the title is already the kind
        lay.addLayout(top)
        n = len(v.get("edit") or [])
        meta = f"{_when(v)}   ·   " + t("sw_meta", "{blocks} blocks · {duration}").format(
            blocks=n, duration=_mmss(v.get("duration_s")))
        self.lbl_meta = _label(meta, 9, MUTED, 500, wrap=True)
        self.lbl_meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.lbl_meta)
        if (v.get("note") or "").strip():
            note = _label(v["note"].strip(), 9, MUTED, wrap=True)
            note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(note)
        same = d.get("same")
        self.lbl_summary = _label(t("ver_same_now", "Same as now") if same else
                                  t("ver_vs_now", "Compared with now: {summary}").format(summary=summary_line(panel.win, d)),
                                  9, "#86efac" if same else VIOLET, 500, wrap=True)
        self.lbl_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.lbl_summary)
        from gui.widgets.layouts import FlowLayout   # the buttons wrap when a translation is long
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        self._btn_row = FlowLayout(row, 0, S(6), S(6))
        lay.addWidget(row)
        self.btn_compare = self._btn(t("ver_compare", "Compare"), t("ver_tt_compare", "See what differs from the story now"),
                                     lambda: panel.compare(self.v))
        self.btn_restore = self._btn(t("ver_restore", "Restore"),
                                     t("ver_tt_restore", "Make this version the story (the story now is kept as a version; {undo} undoes it)").format(undo=_UNDO_KEY),
                                     lambda: panel.restore(self.v))
        self.btn_restore.setEnabled(not same)
        self.btn_rename = self._btn(t("ver_rename", "Rename"), t("ver_tt_rename", "Give this version a name"),
                                    self._start_rename)
        self.btn_delete = self._btn(t("ver_delete", "Delete"), t("ver_tt_delete", "Delete this version"),
                                    lambda: panel.delete(self.v))
        self.btn_delete.setVisible(True)
        self.set_selected(False)

    def _btn(self, text, tip, fn):
        b = QPushButton(text)
        b.setObjectName("Small")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(lambda _=False: fn())
        self._btn_row.addWidget(b)
        return b

    def set_selected(self, on):
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def _start_rename(self):
        self.lbl_name.hide()
        self.badge.hide()
        self.edit_name.setText(self.v.get("name") or version_label(self.panel.win, self.v))
        self.edit_name.show()
        self.edit_name.setFocus()
        self.edit_name.selectAll()
        self.panel.reveal(self)
        self.btn_rename.setText(self.panel.t("ver_rename_ok", "Save name"))
        self.btn_rename.clicked.disconnect()
        self.btn_rename.clicked.connect(lambda _=False: self._commit_rename())

    def _commit_rename(self):
        self.panel.rename(self.v, self.edit_name.text())

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and self.edit_name.isVisible():
            self.panel.schedule_refresh()
            return
        super().keyPressEvent(ev)


class VersionsPanel(QFrame):
    """Over the Story page, under its top bar: save, list, compare, restore, rename, delete."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.win = view.win
        self.t = lambda k, d: self.win.txt(k, d)
        self.cards = []
        self.diff_view = None
        self._compare_id = None
        self.setObjectName("VerPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QFrame#VerPanel {{ background-color: {PAGE_BG}; }}
            QFrame#VerList {{ background-color: {WELL}; border: 1px solid {LINE}; border-radius: {S(16)}px; }}
            QFrame#VerCompare {{ background-color: {WELL}; border: 1px solid {LINE}; border-radius: {S(16)}px; }}
            QFrame#VerCard {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {S(12)}px; }}
            QFrame#VerCard:hover {{ border-color: #3d3d3d; }}
            QFrame#VerCard[selected="true"] {{ border: 1px solid {VIOLET}; background-color: #2a2433; }}
            QFrame#SaveRow {{ background-color: {CARD}; border: 1px solid #4c3a75; border-radius: {S(12)}px; }}
            QWidget#VerHost {{ background: transparent; }}
            QScrollArea {{ background: transparent; border: none; }}
            QLineEdit#Search {{ background-color: #1e1e1e; color: {TEXT}; border: 1px solid {LINE};
                border-radius: {S(9)}px; padding: {S(5)}px {S(10)}px; font-size: {FS(9.5)}pt; }}
            QLineEdit#Search:focus {{ border-color: #6b4fa8; }}
            QPushButton#Quiet {{ background-color: #2a2a2a; color: {TEXT}; border: 1px solid #333333; border-radius: {S(9)}px;
                padding: {S(6)}px {S(12)}px; font-size: {FS(9.5)}pt; }}
            QPushButton#Quiet:hover {{ background-color: #333333; }}
            QPushButton#Small {{ background-color: #2a2a2a; color: {TEXT_2}; border: 1px solid #333333; border-radius: {S(7)}px;
                padding: {S(3)}px {S(10)}px; font-size: {FS(8.5)}pt; }}
            QPushButton#Small:hover {{ background-color: #333333; color: {TEXT}; }}
            QPushButton#Small:disabled {{ color: #5a5a5a; border-color: #2c2c2c; }}
            QPushButton#Primary {{ background: {GRAD}; color: #0b0b14; border: none; border-radius: {S(9)}px;
                padding: {S(7)}px {S(14)}px; font-size: {FS(9.5)}pt; font-weight: 600; }}
            QPushButton#Primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc); }}
            QPushButton#Primary:disabled {{ background: #2a2a2a; color: #666666; }}
        """)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh)
        self._build()
        view.installEventFilter(_Follow(self))
        board = getattr(self.win, "story_board", None)
        if board is not None:
            board.story_changed.append(self._on_story_changed)

    # -- layout ------------------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(S(12))
        head = QHBoxLayout()
        head.setSpacing(S(10))
        tl = QVBoxLayout()
        tl.setSpacing(S(2))
        self.lbl_title = _label(self.t("ver_title", "Story versions"), 14, TEXT, 600)
        tl.addWidget(self.lbl_title)
        self.lbl_sub = _label(self.t("ver_sub", "The stories you kept for this timeline. Restore one to go back to it;"
                                              " {undo} undoes a restore.").format(undo=_UNDO_KEY), 9.5, MUTED, wrap=True)
        self.lbl_sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tl.addWidget(self.lbl_sub)
        head.addLayout(tl, 1)
        self.btn_save = QPushButton(self.t("ver_save", "Save this version…"))
        self.btn_save.setObjectName("Primary")
        self.btn_save.setToolTip(self.t("ver_tt_save", "Keep the story as it is now, with a name"))
        self.btn_save.clicked.connect(lambda _=False: self.start_save())
        self.btn_reload = QPushButton(self.t("rt_button", "Reload from Resolve…"))
        self.btn_reload.setObjectName("Quiet")
        self.btn_reload.setToolTip(self.t("rt_button_tt", "Bring back the timeline you sent, after editing it by hand in "
                                          "Resolve: it becomes a version you can compare and use"))
        self.btn_reload.clicked.connect(lambda _=False: self.win.reload_from_resolve() if hasattr(self.win, 'reload_from_resolve') else None)
        self.btn_close = QPushButton(self.t("ver_close", "Close"))
        self.btn_close.setObjectName("Quiet")
        self.btn_close.setToolTip(self.t("ver_tt_close", "Back to the story (Esc)"))
        self.btn_close.clicked.connect(lambda _=False: self.close_panel())
        self.btn_delete_all = QPushButton(self.t("ver_delete_all", "Delete all…"))
        self.btn_delete_all.setObjectName("Quiet")
        self.btn_delete_all.setToolTip(self.t("ver_tt_delete_all", "Delete every version of this timeline ({undo} undoes)").format(undo=_UNDO_KEY))
        self.btn_delete_all.clicked.connect(lambda _=False: self.delete_all())
        for b in (self.btn_reload, self.btn_save, self.btn_close, self.btn_delete_all):
            b.setCursor(Qt.PointingHandCursor)
        self.btn_close.setText("✕")
        self.btn_close.setFixedWidth(S(34))
        head.addWidget(self.btn_close, 0, Qt.AlignTop)
        outer.addLayout(head)
        # the right-hand column is narrow: the two actions on their own row, Save first
        acts = QHBoxLayout()
        acts.setSpacing(S(8))
        acts.addWidget(self.btn_save, 1)
        acts.addWidget(self.btn_delete_all, 0)
        acts.addWidget(self.btn_reload, 1)
        self.btn_reload.hide()   # Reload from Resolve sits with Send to Resolve (its ▾), the round trip in one place
        outer.addLayout(acts)

        # the name row of "Save this version…"
        self.save_row = QFrame()
        self.save_row.setObjectName("SaveRow")
        sr = QHBoxLayout(self.save_row)
        sr.setContentsMargins(S(14), S(9), S(10), S(9))
        sr.setSpacing(S(8))
        sr.addWidget(_label(self.t("ver_name", "Name"), 9.5, TEXT_2, 500))
        self.edit_save = QLineEdit()
        self.edit_save.setObjectName("Search")
        self.edit_save.returnPressed.connect(lambda: self.commit_save())
        sr.addWidget(self.edit_save, 1)
        self.btn_save_ok = QPushButton(self.t("ver_save_ok", "Save"))
        self.btn_save_ok.setObjectName("Primary")
        self.btn_save_ok.clicked.connect(lambda _=False: self.commit_save())
        self.btn_save_cancel = QPushButton(self.t("btn_cancel", "Cancel"))
        self.btn_save_cancel.setObjectName("Quiet")
        self.btn_save_cancel.clicked.connect(lambda: self.save_row.hide())
        for b in (self.btn_save_ok, self.btn_save_cancel):
            b.setCursor(Qt.PointingHandCursor)
            sr.addWidget(b)
        self.save_row.hide()
        outer.addWidget(self.save_row)

        body = QHBoxLayout()
        body.setSpacing(S(16))
        self.list_frame = QFrame()
        self.list_frame.setObjectName("VerList")
        ll = QVBoxLayout(self.list_frame)
        ll.setContentsMargins(S(12), S(12), S(6), S(10))
        ll.setSpacing(S(8))
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_scroll.setFrameShape(QFrame.NoFrame)
        self.list_host = QWidget()
        self.list_host.setObjectName("VerHost")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, S(8), S(4))
        self.list_lay.setSpacing(S(8))
        self.list_scroll.setWidget(self.list_host)
        ll.addWidget(self.list_scroll, 1)
        body.addWidget(self.list_frame, 1)

        self.compare_frame = QFrame()
        self.compare_frame.setObjectName("VerCompare")
        cl = QVBoxLayout(self.compare_frame)
        cl.setContentsMargins(S(16), S(14), S(10), S(12))
        cl.setSpacing(S(10))
        ch = QHBoxLayout()
        ch.setSpacing(S(8))
        self.lbl_compare = _label(self.t("ver_compare_title", "Compare"), 12, TEXT, 600)
        ch.addWidget(self.lbl_compare, 1)
        self.btn_compare_restore = QPushButton(self.t("ver_restore_this", "Restore this version"))
        self.btn_compare_restore.setObjectName("Quiet")
        self.btn_compare_restore.setCursor(Qt.PointingHandCursor)
        self.btn_compare_restore.clicked.connect(lambda _=False: self._restore_compared())
        self.btn_compare_restore.hide()
        ch.addWidget(self.btn_compare_restore, 0, Qt.AlignVCenter)
        cl.addLayout(ch)
        self.compare_host = QVBoxLayout()
        self.compare_host.setContentsMargins(0, 0, S(6), 0)
        cl.addLayout(self.compare_host, 1)
        self.lbl_compare_empty = _label(self.t("ver_compare_hint", "Press Compare on a version to see how it differs"
                                                                   " from the story now."), 9.5, MUTED, wrap=True)
        self.compare_host.addWidget(self.lbl_compare_empty)
        self.compare_host.addStretch(1)
        body.addWidget(self.compare_frame, 1)
        self.compare_frame.hide()     # Compare shows on the story itself (marks on the blocks, removed parts above)
        outer.addLayout(body, 1)

    def place(self):
        """The right-hand column of the Story page (where Available is): the story stays in view."""
        v = self.view
        av = getattr(v, "avail", None)
        if av is None:
            return
        g = av.geometry()
        if av.parentWidget() is not v and av.parentWidget() is not None:
            tl = av.parentWidget().mapTo(v, g.topLeft())
            g.moveTopLeft(tl)
        self.setGeometry(g)
        self.raise_()

    # -- data --------------------------------------------------------------------------------
    def _path(self):
        return _versions_path(self.win)

    def _board(self):
        return getattr(self.win, "story_board", None)

    def _current(self):
        b = self._board()
        return sv.state_of(b) if b is not None else {"edit": [], "unused": [], "air": {}, "sentences": []}

    def _on_story_changed(self, kind="story"):
        if kind == "story" and self.isVisible():
            self.schedule_refresh()

    def schedule_refresh(self):
        self._refresh_timer.start()

    def refresh(self):
        self._refresh_timer.stop()
        board = self._board()
        path = self._path()
        versions = sv.list_versions(path) if path else []
        cur = self._current()
        self.btn_save.setEnabled(bool(board is not None and board.sentences and board.edit_blocks()))
        self.btn_delete_all.setVisible(bool(versions))
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self.cards = []
        if not versions:
            empty = _label(self.t("ver_empty", "No versions yet. “Save this version…” keeps the story as it is now; "
                                               "StoryCraft also keeps one each time you send the story to Resolve "
                                               "or import an AI story."), 9.5, MUTED, wrap=True)
            empty.setObjectName("VerEmpty")
            self.list_lay.addWidget(empty)
        for v in versions:
            card = _VersionCard(self, v, sv.diff(v, cur))
            card.set_selected(v.get("id") == self._compare_id)
            self.list_lay.addWidget(card)
            self.cards.append(card)
        self.list_lay.addStretch(1)
        if self._compare_id:
            v = next((x for x in versions if x.get("id") == self._compare_id), None)
            if v is not None:
                self._show_compare(v, cur)
            else:
                self._clear_compare()

    # -- actions -----------------------------------------------------------------------------
    def open(self):
        pass                    # docked over Available (right-hand column); the story stays visible
        if hasattr(self.view, "_fit_versions_button"):
            self.view._fit_versions_button()
        self.refresh()
        self.show()
        self.raise_()
        self.place()
        QTimer.singleShot(0, self.place)
        hub = getattr(self.view, "step_track", None)
        if hub is not None:
            hub.set_active("versions")   # Versions is its own tab in the hub bar

    def close_panel(self):
        self.save_row.hide()
        self.hide()
        if hasattr(self.view, "stop_compare"):
            self.view.stop_compare()
        hub = getattr(self.view, "step_track", None)
        if hub is not None:
            hub.set_active("story")
        avail = getattr(self.view, "avail", None)
        if avail is not None:
            avail.show()
        if hasattr(self.view, "_fit_versions_button"):
            self.view._fit_versions_button()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            if self.save_row.isVisible():
                self.save_row.hide()
            else:
                self.close_panel()
            return
        super().keyPressEvent(ev)

    def start_save(self):
        n = sv.manual_count(self._path()) + 1 if self._path() else 1
        self.edit_save.setText(self.t("ver_default_name", "Version {n}").format(n=n))
        self.save_row.show()
        self.edit_save.setFocus()
        self.edit_save.selectAll()

    def commit_save(self, name=None):
        board, path = self._board(), self._path()
        if board is None or not path or not board.sentences:
            return None
        name = (name if name is not None else self.edit_save.text()).strip()
        if not name:
            name = self.t("ver_default_name", "Version {n}").format(n=sv.manual_count(path) + 1)
        v = sv.save_version(path, sv.snapshot(board, "manual", name=name))
        self.save_row.hide()
        self.refresh()
        self._note(self.t("ver_saved_note", "Saved “{name}”").format(name=name))
        return v

    def compare(self, v):
        """Compare: the story itself shows where it differs (marks on the blocks, removed parts above it)."""
        self._compare_id = v.get("id")
        for c in self.cards:
            c.set_selected(c.v.get("id") == self._compare_id)
            if c.v.get("id") == self._compare_id:
                self.reveal(c)
        self.last_diff = sv.diff(v, self._current())
        if hasattr(self.view, "start_compare"):
            self.view.start_compare(v)

    def _clear_selection(self):
        self._compare_id = None
        for c in self.cards:
            c.set_selected(False)

    def reveal(self, card):
        """Scroll the list so the card can be seen (after the layout has settled)."""
        def go():
            if card.isVisible():
                half = card.height() // 2
                self.list_scroll.ensureVisible(0, card.y() + half, 0, half + S(8))
                self.list_scroll.horizontalScrollBar().setValue(0)
        QTimer.singleShot(0, go)

    def _clear_compare(self):
        self._compare_id = None
        if self.diff_view is not None:
            self.diff_view.setParent(None)
            self.diff_view.deleteLater()
            self.diff_view = None
        self.lbl_compare.setText(self.t("ver_compare_title", "Compare"))
        self.lbl_compare_empty.show()
        self.btn_compare_restore.hide()

    def _show_compare(self, v, cur):
        d = sv.diff(v, cur)
        if self.diff_view is not None:
            self.diff_view.setParent(None)
            self.diff_view.deleteLater()
        self.lbl_compare_empty.hide()
        self.lbl_compare.setText(self.t("ver_compare_title", "Compare"))
        self.diff_view = render_diff(self.compare_frame, d, version_label(self.win, v),
                                     self.t("ver_now", "The story now"), win=self.win)
        self.compare_host.insertWidget(0, self.diff_view, 1)
        self.btn_compare_restore.setVisible(not d.get("same"))
        self.last_diff = d

    def _restore_compared(self):
        v = sv.get_version(self._path(), self._compare_id) if self._compare_id else None
        if v is not None:
            self.restore(v)

    def restore(self, v):
        """The story now is kept as 'Before restore', then the version becomes the story (one undo step)."""
        board, path = self._board(), self._path()
        if board is None or not path:
            return False
        sentences = list(v.get("sentences") or [])
        n = len(sentences)
        if not n:
            return False
        if sv.diff(sv.state_of(board), v)["same"]:
            return False
        if board.sentences and board.edit_blocks():
            sv.save_version(path, sv.snapshot(board, "before_restore",
                                              note=self.t("ver_before_note", "Before restoring “{name}”").format(
                                                  name=version_label(self.win, v))))
        ok = lambda b: 0 <= int(b[0]) <= int(b[1]) < n
        edit = [(int(b[0]), int(b[1])) for b in v.get("edit") or [] if ok(b)]
        unused = [(int(b[0]), int(b[1])) for b in v.get("unused") or [] if ok(b)]
        board.edge_air = dict(v.get("air") or {})
        board.load(sentences, edit, unused)
        story = getattr(self.win, "story_view", None)
        if story is not None and hasattr(story, "retext_all"):
            try:
                story.retext_all()   # the words as corrected since: the version keeps its blocks, not old texts
            except Exception as exc:
                print(f"[versions] retext: {exc}")
        self.refresh()
        self._note(self.t("ver_restored_note", "Restored “{name}” · {undo} undoes it").format(
            name=version_label(self.win, v), undo=_UNDO_KEY))
        return True

    def rename(self, v, name):
        path = self._path()
        if path:
            sv.rename_version(path, v.get("id"), (name or "").strip())
        self.refresh()

    def _push_undo(self, path, before):
        """⌘Z puts the versions list back as it was (and redo takes it away again)."""
        um = getattr(self.win, "undo_manager", None)
        if um is None:
            return
        def _now():
            return sv.read_all(path)

        def _apply(lst):
            sv.write_all(path, lst)
            self.refresh()
        um.push_state(_now, _apply, before)

    def delete(self, v, confirm=False):
        """Delete one version, any kind; no question asked: {undo} brings it back."""
        path = self._path()
        if not path:
            return False
        if confirm:
            from PySide6.QtWidgets import QDialog
            from gui.dialogs.msgbox import CustomMsgBox
            box = CustomMsgBox(self.win, self.t("ver_delete_title", "Delete version"),
                               self.t("ver_delete_msg2", "Delete “{name}”? {undo} brings it back.").format(
                                   name=version_label(self.win, v), undo=_UNDO_KEY),
                               self.t("ver_delete", "Delete"), self.t("btn_cancel", "Cancel"))
            if box.exec() != QDialog.Accepted:
                return False
        before = sv.read_all(path)
        done = sv.delete_version(path, v.get("id"))
        if done:
            self._push_undo(path, before)
            if v.get("id") == self._compare_id:
                self._clear_compare()
            self._note(self.t("ver_deleted_note", "Deleted “{name}” · {undo} undoes it").format(
                name=version_label(self.win, v), undo=_UNDO_KEY))
        self.refresh()
        return done

    def delete_all(self, confirm=True):
        """Delete every version of this timeline (asks once; {undo} brings them all back)."""
        path = self._path()
        before = sv.read_all(path) if path else []
        if not before:
            return False
        if confirm:
            from PySide6.QtWidgets import QDialog
            from gui.dialogs.msgbox import CustomMsgBox
            box = CustomMsgBox(self.win, self.t("ver_delete_all_title", "Delete all versions"),
                               self.t("ver_delete_all_msg", "Delete all {n} versions of this timeline? The story "
                                      "itself stays as it is; {undo} brings them back.").format(n=len(before), undo=_UNDO_KEY),
                               self.t("ver_delete_all_ok", "Delete all"), self.t("btn_cancel", "Cancel"))
            if box.exec() != QDialog.Accepted:
                return False
        sv.write_all(path, [])
        self._push_undo(path, before)
        if self._compare_id:
            self._clear_compare()
        self._note(self.t("ver_deleted_all_note", "All versions deleted · {undo} undoes it").format(undo=_UNDO_KEY))
        self.refresh()
        return True

    def _note(self, text):
        try:
            self.view.note(text)
            lbl = getattr(self.view, "lbl_note", None)
            if lbl is not None:
                lbl.raise_()
        except Exception:
            pass


def show_story_versions(win):
    """Open the Story page with the Versions panel (toggles it when it is already open)."""
    view = getattr(win, "story_view", None)
    if view is None or win._stack.currentIndex() != 5:
        win.show_story()
        view = getattr(win, "story_view", None)
    if view is None:
        return None
    panel = getattr(win, "_versions_panel", None)
    if panel is None or panel.view is not view:
        panel = win._versions_panel = VersionsPanel(view)
    panel.open()
    return panel
