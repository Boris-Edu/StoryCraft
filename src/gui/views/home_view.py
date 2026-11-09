#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: home_view.py
ROLE: GUI View
DESCRIPTION:
StoryCraft's Home: what to start from (the open Resolve timeline, a file, the last edit), a few
secondary actions, and the recent timelines saved by timeline_store (opened with one click).
Choosing "From Resolve" or "Open a file" shows the existing set-up form (welcome_view) in that
mode; the form has a "Home" link back. Written in the classic palette like the rest of the UI:
gui/theme.py maps the greys and the green to the active theme; brand colours pass through.
"""
import glob
import json
import os
import re
import threading

from PySide6.QtCore import QObject, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

import config
from gui.views.welcome_view import CARD_BG, LABEL_FG, MUTED_FG, SURFACE_HOVER, WelcomeBrandingWidget

ROW_HOVER = "#2e2e2e"
LINE = "#333333"
MAX_RECENTS = 8

from gui.icons import icon_pixmap  # noqa: E402  (shared with the workspace rail)


def _label(text, size, color, weight=400, wrap=False):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(f"color: {color}; font-size: {config.FS(size)}pt; font-weight: {weight};"
                      f" background: transparent; padding: 0;")
    return lbl


class _ElidedLabel(QLabel):
    """One line that ends in "…" when the column is too narrow (the full text is its tooltip)."""

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        r = self.contentsRect()
        p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, self.fontMetrics().elidedText(self.text(), Qt.ElideRight, r.width()))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


def _when_short(ts, txt):
    """'today 14:10', 'yesterday 09:05' or '2026-11-02 14:10'."""
    import time
    lt, now = time.localtime(ts), time.time()
    hm = time.strftime("%H:%M", lt)
    day = time.strftime("%Y-%m-%d", lt)
    if day == time.strftime("%Y-%m-%d", time.localtime(now)):
        return txt("home_when_today", "today {time}").format(time=hm)
    if day == time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        return txt("home_when_yesterday", "yesterday {time}").format(time=hm)
    return f"{day} {hm}"


def story_line(info, txt):
    """'Story 2:25 · 4 blocks · 1 voice patch · Sent today 14:10', or 'No story yet'."""
    if not info:
        return ""
    parts = []
    if info.get("story"):
        n = int(info.get("blocks") or 0)
        parts.append(txt("home_story_len", "Story {len}").format(len=_fmt_len(info.get("length_s") or 0.0)))
        parts.append(txt("st_blocks_one", "1 block") if n == 1 else txt("st_blocks", "{n} blocks").format(n=n))
    else:
        parts.append(txt("home_story_none", "No story yet"))
    p = int(info.get("patches") or 0)
    if p:
        parts.append(txt("st_patch_one", "1 voice patch") if p == 1 else txt("st_patches", "{n} voice patches").format(n=p))
    if info.get("sent_ts"):
        when = _when_short(info["sent_ts"], txt)
        parts.append(txt("home_story_sent_changed", "Sent {when}, changed since").format(when=when) if info.get("changed")
                     else txt("home_story_sent", "Sent {when}").format(when=when))
    elif info.get("sent"):
        parts.append(txt("home_story_in_resolve", "Sent to Resolve"))
    return " · ".join(parts)


class _Clickable(QFrame):
    """A frame that acts as one button: click, Enter or Space runs the action."""

    def __init__(self, on_click, accessible, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(accessible)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()) and self.isEnabled():
            self._on_click()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self.isEnabled():
            self._on_click()
            return
        super().keyPressEvent(e)


class StartCard(_Clickable):
    """One of the three ways to start: icon tile, title, description, a line of context, arrow."""

    def __init__(self, icon, tint, tile, title, desc, on_click, parent=None):
        super().__init__(on_click, title, parent)
        self.setObjectName("HomeCard")
        self.setMinimumHeight(config.S(176))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setStyleSheet(f"""
            QFrame#HomeCard {{ background-color: {CARD_BG}; border: 1px solid {LINE}; border-radius: {config.S(16)}px; }}
            QFrame#HomeCard:hover {{ background-color: #2b2b2b; border-color: #4a4a4a; }}
            QFrame#HomeCard:focus {{ border-color: #a855f7; }}
            QFrame#HomeCard:disabled {{ background-color: #232323; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(config.S(22), config.S(20), config.S(22), config.S(18))
        lay.setSpacing(config.S(12))
        tile_w = QLabel()
        tile_w.setFixedSize(config.S(44), config.S(44))
        tile_w.setAlignment(Qt.AlignCenter)
        tile_w.setPixmap(icon_pixmap(icon, tint, config.S(22)))
        tile_w.setStyleSheet(f"background-color: {tile}; border-radius: {config.S(12)}px;")
        lay.addWidget(tile_w)
        text = QVBoxLayout()
        text.setSpacing(config.S(4))
        self.title = _label(title, 12, "#ffffff", 600, wrap=True)
        self.desc = _label(desc, 9.5, LABEL_FG, wrap=True)
        text.addWidget(self.title)
        text.addWidget(self.desc)
        lay.addLayout(text)
        lay.addStretch(1)
        foot = QHBoxLayout()
        foot.setSpacing(config.S(8))
        self.meta = _ElidedLabel("")          # one line ending in "…" when long (full text in the tooltip)
        self.meta.setStyleSheet(_label("", 9, MUTED_FG).styleSheet())
        self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        arrow = QLabel()
        arrow.setPixmap(icon_pixmap("arrow", "#8a8a8a", config.S(18)))
        foot.addWidget(self.meta, 1)
        foot.addWidget(arrow)
        lay.addLayout(foot)

    def set_meta(self, text):
        self.meta.setText(text)
        self.meta.setToolTip(text)


class SmallCard(_Clickable):
    def __init__(self, title, desc, on_click, parent=None):
        super().__init__(on_click, title, parent)
        self.setObjectName("HomeSmall")
        self.setMinimumHeight(config.S(64))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"""
            QFrame#HomeSmall {{ background-color: {CARD_BG}; border: 1px solid {LINE}; border-radius: {config.S(14)}px; }}
            QFrame#HomeSmall:hover {{ background-color: #2b2b2b; border-color: #4a4a4a; }}
            QFrame#HomeSmall:focus {{ border-color: #a855f7; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(config.S(16), config.S(12), config.S(16), config.S(12))
        lay.setSpacing(config.S(2))
        lay.addWidget(_label(title, 10, "#ffffff", 600))
        lay.addWidget(_label(desc, 9, LABEL_FG, wrap=True))


class RecentRow(_Clickable):
    COLS = (2.6, 1.6, 0.8, 0.9, 1.1)

    MAX_CHIPS = 6

    def __init__(self, item, on_open, txt, parent=None, on_fresh=None):
        """on_open(tl_name or None): None opens the project where it was left, a name opens that edit.
        on_fresh(): Start fresh (its ⋯ menu, or right-click)."""
        super().__init__(lambda: on_open(None), item["name"], parent)
        self._txt, self._on_fresh = txt, on_fresh
        self.setObjectName("HomeRow")
        self.setMinimumHeight(config.S(50))
        self.setStyleSheet(f"""
            QFrame#HomeRow {{ background: transparent; border: none; border-top: 1px solid {LINE}; }}
            QFrame#HomeRow:hover {{ background-color: {ROW_HOVER}; }}
            QFrame#HomeRow:focus {{ background-color: {ROW_HOVER}; }}
            QPushButton#HomeChip {{ background-color: #2a2a2a; color: #d4d4d4; border: 1px solid #3a3a3a;
                border-radius: {config.S(9)}px; padding: {config.S(2)}px {config.S(10)}px; font-size: {config.FS(8.5)}pt; }}
            QPushButton#HomeChip:hover {{ background-color: #2a2140; color: #ffffff; border-color: #6b4fa8; }}
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(config.S(18), config.S(10), config.S(18), config.S(10))
        row.setSpacing(config.S(12))
        name_col = QVBoxLayout()
        name_col.setContentsMargins(0, 0, 0, 0)
        name_col.setSpacing(config.S(6))
        name_box = QHBoxLayout()
        name_box.setSpacing(config.S(8))
        name = _label(item["name"], 10, "#ffffff", 500)
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name.setToolTip(item["path"])
        name_box.addWidget(name, 1)
        if item.get("is_edit"):
            b = _label(txt("home_badge_edit"), 8, "#d8b4fe", 500)
            b.setStyleSheet(b.styleSheet() + f" background-color: #2a2140; border-radius: {config.S(8)}px;"
                                              f" padding: {config.S(1)}px {config.S(8)}px;")
            name_box.addWidget(b, 0)
        head = QVBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(config.S(2))
        head.addLayout(name_box)
        name_col.addLayout(head)
        # The story this row reopens: its length, blocks, voice patches and whether it went to Resolve
        line = story_line(item.get("story"), txt)
        if line:
            self.story = _ElidedLabel(line)
            self.story.setObjectName("HomeStoryLine")
            self.story.setStyleSheet(f"color: {MUTED_FG}; font-size: {config.FS(8.5)}pt; background: transparent; padding: 0;")
            self.story.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.story.setToolTip(line)
            head.addWidget(self.story)
        # The project's edits: each chip opens that edit inside the project
        edits = item.get("edits") or []
        if edits:
            # Chips keep their natural width and wrap, four to a line
            chips = QGridLayout()
            chips.setHorizontalSpacing(config.S(6))
            chips.setVerticalSpacing(config.S(6))
            shown = edits if len(edits) <= self.MAX_CHIPS else [edits[0]] + edits[-(self.MAX_CHIPS - 1):]
            widgets = []
            for e in shown:
                label = txt("titlebar_original") if e["original"] else e["label"]
                c = QPushButton(label)
                c.setObjectName("HomeChip")
                c.setCursor(Qt.PointingHandCursor)
                # The chips are timelines in Resolve ("… StoryCraft Edit 2"): the label is their name
                tip = txt("home_tt_chip_original") if e["original"] else txt("home_tt_chip")
                tip = tip.format(name=e["tl"]) if "{name}" in tip else e["tl"]
                sl = story_line(e.get("story"), txt)
                c.setToolTip(f"{tip}\n{sl}" if sl else tip)
                c.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                c.clicked.connect(lambda _=False, tl=e["tl"]: on_open(tl))
                widgets.append(c)
            if len(edits) > len(shown):
                widgets.append(_label(f"+{len(edits) - len(shown)}", 8.5, MUTED_FG))
            per_line = 4
            for i, w in enumerate(widgets):
                chips.addWidget(w, i // per_line, i % per_line)
            chips.setColumnStretch(per_line, 1)
            name_col.addLayout(chips)
        name_wrap = QWidget()
        name_wrap.setLayout(name_col)
        self.cells = [name_wrap,
                      _label(item["project"], 9.5, LABEL_FG),
                      _label(item["length"], 9.5, "#d4d4d4"),
                      _label(item["words"], 9.5, "#d4d4d4"),
                      _label(item["saved"], 9.5, LABEL_FG)]
        for w, stretch in zip(self.cells, self.COLS):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            row.addWidget(w, int(stretch * 10))
        if on_fresh is not None:
            more = QPushButton("⋯")
            more.setObjectName("HomeMore")
            more.setCursor(Qt.PointingHandCursor)
            more.setFixedSize(config.S(28), config.S(24))
            more.setToolTip(txt("home_more_tt", "More: start fresh with this timeline"))
            more.setStyleSheet(f"QPushButton#HomeMore {{ background: transparent; color: {LABEL_FG}; border: none;"
                               f" border-radius: {config.S(6)}px; font-size: {config.FS(11)}pt; }}"
                               f" QPushButton#HomeMore:hover {{ background-color: #2a2a2a; color: #ffffff; }}")
            more.clicked.connect(lambda _=False, b=more: self._menu(b.mapToGlobal(b.rect().bottomLeft())))
            row.addWidget(more, 0, Qt.AlignVCenter)

    def _menu(self, gpos):
        from gui.widgets.menus import context_menu
        m = context_menu(self)
        m.addAction(self._txt("home_fresh", "Start fresh…"), self._on_fresh)
        m.exec(gpos)

    def contextMenuEvent(self, e):
        if self._on_fresh is not None:
            self._menu(e.globalPos())

    def set_compact(self, compact):
        for i in (1, 3):  # project and words go first on a narrow window
            self.cells[i].setVisible(not compact)


class _RecentLoader(QObject):
    loaded = Signal(list)


def _fmt_len(seconds):
    s = int(round(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


_EDIT_RE = re.compile(r"^(.*?)\s+(?:StoryCraft|BadWords) Edit (\d+)$")


def _edit_label(tl_name):
    m = _EDIT_RE.match(tl_name or "")
    return f"Edit {m.group(2)}" if m else (tl_name or "")


def load_recents(saves_dir, limit=MAX_RECENTS, scan=40, sent_edits=None):
    """Newest saved projects: [{path, name, project, project_dir, length, words, saved, is_edit, edits, story}].
    story (and each edit's): handlers.recent_info.story_info, the story the row reopens; sent_edits is
    the preferences map of what went to Resolve (read-only).
    An edit timeline whose project is saved (its source's transcript holds it as a chapter, or the
    edits library has it) is not listed on its own: it shows as a chip ("Edit 2") on its source's row,
    and the row moves up when one of its edits was saved more recently."""
    from handlers import edit_library, recent_info, timeline_store
    paths = [p for p in glob.glob(os.path.join(saves_dir, "timelines", "*", "*.json")) if ".before " not in p]
    paths.sort(key=os.path.getmtime, reverse=True)
    cache = {}

    def _read(p):
        if p not in cache:
            try:
                with open(p, encoding="utf-8") as f:
                    cache[p] = json.load(f)
            except Exception:
                cache[p] = None
        return cache[p]

    def _name(p, d):
        return ((d or {}).get("transcription_source") or {}).get("timeline_name") or os.path.splitext(os.path.basename(p))[0]

    rows, newest = {}, {}
    projects_of = {}   # timeline name -> project folders saving one by that name
    for p in paths:
        projects_of.setdefault(os.path.splitext(os.path.basename(p))[0], set()).add(os.path.basename(os.path.dirname(p)))
    for p in paths[:scan]:
        d = _read(p)
        if d is None:
            continue
        name, proj_dir = _name(p, d), os.path.basename(os.path.dirname(p))
        target = p
        m = _EDIT_RE.match(name)
        if m:
            src = m.group(1)
            src_path = timeline_store.path_for(saves_dir, proj_dir, timeline_store._safe(src))
            sd = _read(src_path) if os.path.isfile(src_path) else None
            in_chapters = any(ch.get("tl_name") == name for ch in ((sd or {}).get("chapters") or []))
            if sd is not None and (in_chapters or edit_library.find_edit(saves_dir, proj_dir, name)):
                target = src_path  # shown on its source's row
        newest[target] = max(newest.get(target, 0.0), os.path.getmtime(p))
        if target not in rows:
            rows[target] = proj_dir
    out = []
    for p in sorted(rows, key=lambda k: newest[k], reverse=True)[:limit]:
        d, proj_dir = _read(p), rows[p]
        words = d.get("words_data") or []
        end = max((float(w.get("end", 0.0)) for w in words), default=0.0)
        n = sum(1 for w in words if w.get("type") == "word")
        name = _name(p, d)
        # Its edits: chapters of this transcript (older edits), then the edits library (newer ones)
        edits, seen = [], set()
        chapters = d.get("chapters") or []
        if len(chapters) > 1:
            for ch in chapters:
                tl = ch.get("tl_name")
                if tl and tl not in seen:
                    seen.add(tl)
                    edits.append({"label": ch.get("name") or _edit_label(tl), "tl": tl, "original": tl == name})
        try:
            library = edit_library.list_edits(saves_dir, proj_dir, name)
        except Exception:
            library = []
        for e in sorted(library, key=lambda e: e.get("created") or 0):
            tl = e.get("name")
            if tl and tl not in seen:
                seen.add(tl)
                edits.append({"label": _edit_label(tl), "tl": tl, "original": False})
        if edits and not any(e["original"] for e in edits):
            edits.insert(0, {"label": "", "tl": name, "original": True})
        # The story each one reopens (main_window: _ai_story_map_path, _voice_patch_dir, _sent_key)
        src = d.get("transcription_source") or {}
        key = src.get("timeline_name") or src.get("source_file") or ""

        # sent_edits is keyed by timeline name only ("Timeline 1" in two projects shares one key): an entry
        # counts only when no other project saves a timeline by that name (else the versions file decides)
        sent_here = {k: v for k, v in (sent_edits or {}).items()
                     if len(projects_of.get(timeline_store._safe(k), {proj_dir})) == 1}

        def _story(tl, key, patch_name=None):
            try:
                return recent_info.story_info(saves_dir, proj_dir, tl, sent_key=key, sent_edits=sent_here,
                                              patch_name=patch_name)
            except Exception:
                return None
        story = _story(key or "Transcript", key, src.get("timeline_name") or "Timeline")
        for e in edits:
            e["story"] = story if e["tl"] == name else _story(e["tl"], e["tl"])
        out.append({"path": p, "name": name, "project": "" if proj_dir == "_files" else proj_dir, "project_dir": proj_dir,
                    "length": _fmt_len(end), "words": f"{n:,}", "saved": timeline_store.when_text(newest[p]),
                    "is_edit": bool(_EDIT_RE.match(name)), "edits": edits, "story": story})
    return out


class HomeView(QWidget):
    """Page 0's first screen (the set-up form is the second, see main_window.home_stack)."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._recents = []
        self._rows = []
        self._cols = 0
        self._loader = _RecentLoader()
        self._loader.loaded.connect(self._show_recents)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                             " QScrollArea > QWidget > QWidget { background: transparent; }")
        outer.addWidget(scroll)
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        scroll.setWidget(page)
        center = QHBoxLayout(page)
        center.setContentsMargins(config.S(24), config.S(28), config.S(24), config.S(40))
        self.col = QWidget()
        self.col.setMaximumWidth(config.S(1100))
        center.addStretch(1)
        center.addWidget(self.col, 100)
        center.addStretch(1)

        col = QVBoxLayout(self.col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(config.S(26))

        self.brand = WelcomeBrandingWidget(self.col)
        col.addWidget(self.brand, 0, Qt.AlignHCenter)
        # A transcript is open: the way back to it (Home isn't a one-way door; reopening reloads everything)
        self.btn_back_open = QPushButton("")
        self.btn_back_open.setObjectName("BackOpen")
        self.btn_back_open.setCursor(Qt.PointingHandCursor)
        self.btn_back_open.setStyleSheet(f"QPushButton#BackOpen {{ background-color: #241f33; color: #e9d5ff; border: 1px solid #4c3a75;"
                                         f" border-radius: {config.S(12)}px; padding: {config.S(10)}px {config.S(18)}px;"
                                         f" font-size: {config.FS(10.5)}pt; font-weight: 600; text-align: left; }}"
                                         f" QPushButton#BackOpen:hover {{ background-color: #2e2642; color: #ffffff; }}")
        self.btn_back_open.clicked.connect(lambda: win.show_story())
        self.btn_back_open.hide()
        col.addWidget(self.btn_back_open)

        t = win.txt
        self.card_resolve = StartCard("resolve", "#22d3ee", "#12313a", t("home_card_resolve_title"),
                                      t("home_card_resolve_desc"), lambda: self._start("resolve"))
        self.card_file = StartCard("file", "#c084fc", "#2a1e45", t("home_card_file_title"),
                                   t("home_card_file_desc"), lambda: self._start("file"))
        self.card_file.set_meta(t("home_card_file_meta"))
        self.card_continue = StartCard("continue", "#93c5fd", "#1c2840", t("home_card_continue_title"),
                                       t("home_card_continue_desc"), self._continue_last)
        self.cards_box = QWidget()
        self.cards_grid = QGridLayout(self.cards_box)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(config.S(16))
        col.addWidget(self.cards_box)

        # (silence detection without a transcript is an option of the start form now, not a way in here)
        # (Settings is in the window's top-right corner: no second Settings card here)
        self.small = [SmallCard(t("home_more_import"), t("home_more_import_desc"), self._import_file)]
        self.small_box = QWidget()
        self.small_grid = QGridLayout(self.small_box)
        self.small_grid.setContentsMargins(0, 0, 0, 0)
        self.small_grid.setSpacing(config.S(12))
        col.addWidget(self.small_box)

        # Recent timelines
        rec = QVBoxLayout()
        rec.setSpacing(config.S(10))
        rec.addWidget(_label(t("home_recent"), 11, "#ffffff", 600))
        self.table = QFrame()
        self.table.setObjectName("HomeTable")
        self.table.setStyleSheet(f"QFrame#HomeTable {{ background-color: #222222; border: 1px solid {LINE};"
                                 f" border-radius: {config.S(14)}px; }}")
        self.table_lay = QVBoxLayout(self.table)
        self.table_lay.setContentsMargins(0, 0, 0, config.S(4))
        self.table_lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(config.S(18), config.S(10), config.S(18), config.S(10))
        head.setSpacing(config.S(12))
        self.head_cells = []
        for key, stretch in zip(("home_col_timeline", "home_col_project", "home_col_length", "home_col_words",
                                 "home_col_saved"), RecentRow.COLS):
            h = _label(t(key).upper(), 8, MUTED_FG, 600)
            h.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            head.addWidget(h, int(stretch * 10))
            self.head_cells.append(h)
        self.table_lay.addLayout(head)
        self.empty = _label(t("home_recent_empty"), 9.5, MUTED_FG)
        self.empty.setContentsMargins(config.S(18), config.S(14), config.S(18), config.S(14))
        self.table_lay.addWidget(self.empty)
        rec.addWidget(self.table)
        col.addLayout(rec)
        col.addStretch(1)

        self._relayout(force=True)

    # ── look ─────────────────────────────────────────────────────────────
    def paintEvent(self, event):
        """The start screen's faint cyan / violet light behind the logo."""
        from gui import brand
        p = QPainter(self)
        cx = self.width() / 2
        cy = config.S(90)
        for dx, dy, colr, alpha, reach in ((-config.S(170), config.S(40), brand.CYAN, 22, config.S(460)),
                                           (config.S(190), config.S(120), brand.VIOLET, 26, config.S(520))):
            g = QRadialGradient(QPointF(cx + dx, cy + dy), reach)
            c0 = QColor(colr)
            c0.setAlpha(alpha)
            c1 = QColor(colr)
            c1.setAlpha(0)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            p.fillRect(self.rect(), g)
        p.end()
        super().paintEvent(event)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self, force=False):
        w = self.width() or config.S(1200)
        cols = 3 if w >= config.S(1000) else 1
        if cols == self._cols and not force:
            return
        self._cols = cols
        for card in (self.card_resolve, self.card_file, self.card_continue):
            card.setMinimumHeight(config.S(176) if cols == 3 else config.S(132))
        for grid, items in ((self.cards_grid, [self.card_resolve, self.card_file, self.card_continue]),
                            (self.small_grid, self.small)):
            for it in items:
                grid.removeWidget(it)
            for i, it in enumerate(items):
                grid.addWidget(it, i // cols, i % cols)
        compact = cols == 1
        for i in (1, 3):
            self.head_cells[i].setVisible(not compact)
        for r in self._rows:
            r.set_compact(compact)

    # ── data ─────────────────────────────────────────────────────────────
    def refresh(self):
        """Resolve status line and recent timelines (read off the UI thread)."""
        t = self.win.txt
        rh = getattr(self.win.engine, "resolve_handler", None)
        try:
            connected = bool(rh and rh.is_connected())
        except Exception:
            connected = False
        if connected:
            try:
                proj = self.win.engine._get_resolve_project_name() or ""
            except Exception:
                proj = ""
            tl = getattr(getattr(self.win, "combo_tl_0", None), "text", lambda: "")() or ""
            self.card_resolve.set_meta(" · ".join(x for x in (tl, proj) if x) or t("home_resolve_connected"))
        else:
            self.card_resolve.set_meta(t("home_resolve_off"))
        saves = self.win.engine.os_doc.get_saves_folder()
        engine = self.win.engine

        def _load():
            try:
                sent = (engine.load_preferences() or {}).get("sent_edits") or {}
            except Exception:
                sent = {}
            self._loader.loaded.emit(load_recents(saves, sent_edits=sent))
        threading.Thread(target=_load, daemon=True).start()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self._update_back_open()

    def _update_back_open(self):
        win = self.win
        canvas = getattr(win, "text_canvas", None)
        if not getattr(canvas, "words_data", None):
            self.btn_back_open.hide()
            return
        src = getattr(win, "_transcription_source", None) or {}
        name = (getattr(win, "_current_edit_name", None) or src.get("timeline_name")
                or os.path.basename(src.get("source_file") or "") or "")
        self.btn_back_open.setText("←  " + win.txt("home_back_open", "Back to “{name}”").format(name=name))
        self.btn_back_open.setToolTip(win.txt("tt_home_back_open", "The transcript you have open, where you left it"))
        self.btn_back_open.show()

    def _show_recents(self, items):
        self._recents = items
        for r in self._rows:
            self.table_lay.removeWidget(r)
            r.deleteLater()
        self._rows = []
        t = self.win.txt
        for it in items:
            row = RecentRow(it, lambda tl, it=it: self._open_item(it, tl), t,
                            on_fresh=lambda it=it: self.win.start_fresh(it["project_dir"], it["name"]))
            row.set_compact(self._cols == 1)
            self.table_lay.addWidget(row)
            self._rows.append(row)
        self.empty.setVisible(not items)
        if items:
            last = items[0]
            label = f"{last['name']}, {last['project']}" if last.get("project") else last["name"]
            self.card_continue.set_meta(t("home_card_continue_meta").format(name=label, when=last["saved"]))
            self.card_continue.setEnabled(True)
        else:
            self.card_continue.set_meta(t("home_card_continue_none"))
            self.card_continue.setEnabled(False)

    # ── actions ──────────────────────────────────────────────────────────
    def _start(self, mode):
        """Show the set-up form: 'resolve' or 'file' transcription, or 'silence' detection."""
        win = self.win
        page = getattr(win, "welcome_page", None)
        if page is not None:
            try:
                page.switch_workspace_instant(1 if mode == "silence" else 0)
                if mode in ("resolve", "file"):
                    page._apply_source_mode(mode, animated=False)
            except Exception as e:
                from osdoc import log_warn
                log_warn(f"Home: could not prepare the set-up form ({e})")
        win.show_setup_form()

    def _open_item(self, item, tl=None):
        """The project where it was left (tl None), or one of its edits (Original included)."""
        saves = self.win.engine.os_doc.get_saves_folder()
        if tl is None:
            self.win.open_saved_timeline(saves, item["project_dir"], item["name"], fallback_path=item["path"])
        else:
            self.win.open_saved_timeline(saves, item["project_dir"], tl, fallback_path=item["path"],
                                         chapter_tl=tl)

    def _continue_last(self):
        if self._recents:
            self._open_item(self._recents[0])

    def _import_file(self):
        self.win._on_import_project()

    def _open_settings(self):
        if hasattr(self.win, "_on_settings"):
            self.win._on_settings()


def home_back_button(win, parent):
    """The set-up form's way back to Home: a quiet link in its top-left corner."""
    b = QPushButton("  " + win.txt("home_back"), parent)
    b.setObjectName("HomeBack")
    b.setIcon(icon_pixmap("back", "#a3a3a3", config.S(16)))
    b.setIconSize(QSize(config.S(16), config.S(16)))
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(f"""
        QPushButton#HomeBack {{ background: transparent; color: {LABEL_FG}; border: none; border-radius: {config.S(8)}px;
            font-size: {config.FS(9.5)}pt; font-weight: 500; padding: {config.S(6)}px {config.S(10)}px; }}
        QPushButton#HomeBack:hover {{ background-color: {SURFACE_HOVER}; color: #ffffff; }}
    """)
    b.adjustSize()
    b.move(config.S(16), config.S(14))
    b.clicked.connect(win.show_home)
    b.raise_()
    return b
