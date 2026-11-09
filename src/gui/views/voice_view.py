#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: voice_view.py
ROLE: GUI View (page 4 of the main stack, "Voice" on the workspace rail)
DESCRIPTION:
Everything StoryCraft does with VoiceStudio, in one place: the connection (engine, model licence,
watermark) and what in the open timeline could be patched, in four groups: endings that sound
unfinished (voice_patch.core.detect: their last words are replaced), and passages that are hard to
understand, incoherent, or tagged by the user with the "To voice patch" marker
(voice_patch.candidates: the whole passage is replaced, by default its words said cleanly, or the
words the user types). Any candidate can be heard at once (A, cut from the timeline's audio with a
second around it); a patch is made in the speaker's voice (VoiceStudio, best of 3 takes; Delivery steers
its ending, pitch, pace and sound) or steered by the user's own recording (the take whose melody and pace
are closest to it), compared (A on the left, every take on the right: the
one the user picks is the patch), put in the edit (one undo step; when a
rewrite changes the length the user chooses to close the edit up or keep the timing) and saved.
VoiceStudio is a separate app (AGPL-3.0) reached over its local API (voice_patch.client); none of its
code is here. Results are kept per timeline under <saves>/voice_patches/. Classic palette like the
rest of the UI (gui/theme.py maps it).
"""
import copy
import html as _h
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import QSize, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import config

LINE = "#303030"
CARD = "#262626"
GRAD = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
GROUP_ORDER = ("tagged", "endings", "hard", "incoherent")   # what the user marked first
PREVIEW_PAD_S = 1.0      # A plays the passage with this much of the recording around it


def _lbl(text, size=9.5, color="#d4d4d4", weight=400, wrap=True):
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lb.setStyleSheet(f"color: {color}; font-size: {config.FS(size)}pt; font-weight: {weight}; background: transparent;")
    return lb


def _badge(text, fg, bg):
    b = QLabel(text)
    b.setStyleSheet(f"color: {fg}; background-color: {bg}; border-radius: {config.S(9)}px; font-size: {config.FS(8.5)}pt;"
                    f" font-weight: 600; padding: {config.S(2)}px {config.S(9)}px;")
    return b


def _card(name):
    f = QFrame()
    f.setObjectName(name)
    f.setStyleSheet(f"QFrame#{name} {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {config.S(14)}px; }}")
    return f


def _set_icon(btn, name, primary=False):
    """A stroke icon (gui/icons.py) on a button: dark on the gradient Primary, light grey on the quiet ones,
    dimmed when disabled; the checked Primary (In the story) gets its green."""
    from gui import theme
    from gui.icons import icon_pixmap
    px = config.S(15)
    normal, on = ("#0b0b14", "#a7f3d0") if primary else ("#a3a3a3", "#ffffff")
    ic = QIcon()
    for col, mode, state in ((normal, QIcon.Normal, QIcon.Off), (normal, QIcon.Active, QIcon.Off),
                             ("#666666", QIcon.Disabled, QIcon.Off), (on, QIcon.Normal, QIcon.On),
                             (on, QIcon.Active, QIcon.On), ("#666666", QIcon.Disabled, QIcon.On)):
        ic.addPixmap(icon_pixmap(name, theme.color(col), px), mode, state)
    btn.setIcon(ic)
    btn.setIconSize(QSize(px, px))


def _fmt_t(s):
    s = max(0.0, float(s or 0))
    return f"{int(s // 60)}:{s % 60:05.2f}"


def _short(text, n=80):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def row_key(row):
    """The id a candidate's files and accepted.json entry go by: the ending's last word id, or the passage id."""
    return str(row["id"]) if row.get("kind") == "passage" else str(row["end_id"])


class _RowButton(QPushButton):
    """A list row: its lines are elided to the width it gets (never wider than the list)."""

    def __init__(self, text, pad=24, modes=(), suffix=""):
        super().__init__(text + suffix)
        self._full = text
        self._suffix = suffix    # kept whole after the (elided) first line, e.g. a group's count
        self._pad = pad
        self._modes = modes      # per line: Qt.ElideRight (default) or Qt.ElideLeft (keep the line's end)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(config.S(120))

    def full_text(self):
        return self._full + self._suffix

    def resizeEvent(self, e):
        super().resizeEvent(e)
        fm = self.fontMetrics()
        room = max(config.S(40), self.width() - config.S(self._pad))
        lines = self._full.split("\n")
        modes = list(self._modes) + [Qt.ElideRight] * len(lines)
        out = []
        for i, line in enumerate(lines):
            sfx = self._suffix if i == 0 else ""
            out.append(fm.elidedText(line, modes[i], max(config.S(20), room - fm.horizontalAdvance(sfx))) + sfx)
        self.setText("\n".join(out))


class _Job(QThread):
    """Runs fn(progress) off the UI thread; progress(text) is safe to call from fn."""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn(self.progress.emit))
        except Exception as e:  # shown to the user as is: the messages are written for people
            self.failed.emit(str(e))


class VoiceView(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.t = lambda k, d: win.txt(k, d)
        self._job = None
        self._words_reader = None    # the background read of patches' own words (_read_patch_words)
        self._words_failed = set()   # patches whose words couldn't be read (not tried again this session)
        self._words_timer = QTimer(self)
        self._words_timer.setInterval(250)
        self._words_timer.timeout.connect(self._words_poll)
        self._an = None          # voice_patch.core.Analysis of the open transcript (cached per audio file)
        self._audio16 = None     # the same recording at 16 kHz (clarity)
        self._rows = []          # core.detect rows (every statement ending)
        self._groups = {g: [] for g in GROUP_ORDER}
        self._endings_measured = False
        self._collapsed = set()
        self._sel = None
        self._show_all = False
        self._whole = False      # False: only what lies inside the story (when there is one)
        self._conn = {}
        self._player = None
        self.setObjectName("VoiceView")
        self.setStyleSheet(f"""
            QWidget#VoiceView {{ background-color: {config.BG_COLOR}; }}
            QPushButton#Quiet {{ background-color: #2a2a2a; color: #ffffff; border: 1px solid #333333; border-radius: {config.S(9)}px;
                padding: {config.S(7)}px {config.S(14)}px; font-size: {config.FS(9.5)}pt; }}
            QPushButton#Quiet:hover {{ background-color: #333333; }}
            QPushButton#Quiet:checked {{ background-color: #2a2140; color: #ffffff; border-color: #6b4fa8; }}
            QPushButton#Quiet:disabled {{ color: #666666; }}
            QPushButton#Small {{ background-color: #2a2a2a; color: #d4d4d4; border: 1px solid #333333; border-radius: {config.S(7)}px;
                padding: {config.S(3)}px {config.S(10)}px; font-size: {config.FS(8.5)}pt; }}
            QPushButton#Small:hover {{ background-color: #333333; color: #ffffff; }}
            QPushButton#Small:disabled {{ color: #666666; }}
            QPushButton#Primary {{ background: {GRAD}; color: #0b0b14; border: none; border-radius: {config.S(10)}px;
                padding: {config.S(9)}px {config.S(18)}px; font-size: {config.FS(10)}pt; font-weight: 600; }}
            QPushButton#Primary:disabled {{ background: #2a2a2a; color: #666666; }}
            QPushButton#Primary:checked {{ background: #0f2e24; color: #a7f3d0; border: 1px solid #1f5c47; }}
            QPushButton#Ending {{ text-align: left; background-color: {CARD}; color: #ffffff; border: 1px solid {LINE};
                border-radius: {config.S(12)}px; padding: {config.S(10)}px {config.S(12)}px; font-size: {config.FS(9.5)}pt; }}
            QPushButton#Ending:hover {{ border-color: #4a4a4a; }}
            QPushButton#Ending:checked {{ border-color: #6b4fa8; background-color: #2a2140; }}
            QPushButton#Group {{ text-align: left; background: transparent; color: #d4d4d4; border: none;
                border-top: 1px solid {LINE}; padding: {config.S(8)}px {config.S(2)}px {config.S(4)}px {config.S(2)}px;
                font-size: {config.FS(9.5)}pt; font-weight: 600; }}
            QPushButton#Group:hover {{ color: #ffffff; }}
            QPlainTextEdit#Words {{ background-color: #1e1e1e; color: #ffffff; border: 1px solid {LINE};
                border-radius: {config.S(9)}px; padding: {config.S(6)}px {config.S(8)}px; font-size: {config.FS(10)}pt; }}
            QPlainTextEdit#Words:focus {{ border-color: #6b4fa8; }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(config.S(20), config.S(12), config.S(24), config.S(16))   # the hub bar lines up with the other pages
        outer.setSpacing(config.S(14))

        # ── title + connection ──────────────────────────────────────────────
        head = QHBoxLayout()
        from gui.widgets.step_track import HubNav
        self.hub_nav = HubNav(self.win, active="voice")
        self.btn_back_story = self.hub_nav.buttons["story"]      # "back to the story" is the bar's Story
        head.addWidget(self.hub_nav, 0, Qt.AlignVCenter)
        head.addSpacing(config.S(12))
        head.addWidget(_badge(self.t("voice_preview", "Beta"), "#d8b4fe", "#2a2140"))
        head.addStretch(1)
        outer.addLayout(head)
        outer.addWidget(_lbl(self.t("voice_intro2", "Find what sounds wrong: endings that sound unfinished, passages "
                                    "that are hard to understand or incoherent, words you marked. Replace them with a "
                                    "version in the speaker's own voice, made by VoiceStudio."), 9.5, "#a3a3a3"))

        self.conn_card = _card("VoiceConn")
        cl = QVBoxLayout(self.conn_card)
        cl.setContentsMargins(config.S(16), config.S(12), config.S(16), config.S(12))
        cl.setSpacing(config.S(8))
        # Two lines so nothing is cut on a narrow window: status, then licence, watermark and actions
        row = QHBoxLayout()
        row.setSpacing(config.S(10))
        self.dot = QLabel("●")
        self.conn_text = _lbl("", 10, "#ffffff", 600, wrap=False)
        self.engine_text = _lbl("", 9.5, "#a3a3a3", wrap=False)
        self.engine_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lic_badge = _badge("", "#ffffff", "#2a2a2a")
        self.wm_text = _lbl("", 9, "#8a8a8a", wrap=False)
        for w in (self.dot, self.conn_text):
            row.addWidget(w)
        row.addWidget(self.engine_text, 1)
        cl.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(config.S(10))
        row.addWidget(self.lic_badge)
        row.addWidget(self.wm_text)
        row.addStretch(1)
        self.btn_check = QPushButton(self.t("voice_check", "Check again"))
        self.btn_check.setObjectName("Quiet")
        self.btn_check.clicked.connect(self.check_connection)
        self.btn_open_vs = QPushButton(self.t("voice_open_vs", "Open VoiceStudio"))
        self.btn_open_vs.setObjectName("Quiet")
        self.btn_open_vs.clicked.connect(self._open_voicestudio)
        row.addWidget(self.btn_check)
        row.addWidget(self.btn_open_vs)
        cl.addLayout(row)
        self.conn_help = _lbl("", 9, "#a3a3a3")
        cl.addWidget(self.conn_help)
        outer.addWidget(self.conn_card)

        # ── body: candidates | selected candidate ───────────────────────────
        body = QHBoxLayout()
        body.setSpacing(config.S(16))
        left = QVBoxLayout()
        left.setSpacing(config.S(8))
        left.addWidget(_lbl(self.t("voice_candidates", "What could be patched"), 11, "#ffffff", 600))
        lh = QHBoxLayout()      # the filters on their own row: the list column is narrow
        lh.setSpacing(config.S(6))
        # Two separate filters: where to look (the story, or the whole interview) and which endings
        # (only the ones that sound unfinished, or all of them)
        self.btn_whole = QPushButton(self.t("voice_whole_interview", "Whole interview"))
        self.btn_whole.setObjectName("Quiet")
        self.btn_whole.setCheckable(True)
        self.btn_whole.setToolTip(self.t("voice_whole_interview_tt", "Also list what lies outside the story. "
                                         "Off: only what the story uses."))
        self.btn_whole.toggled.connect(self._toggle_whole)
        self.btn_whole.hide()     # shown when there is a story to narrow the list to
        lh.addWidget(self.btn_whole)
        self.btn_all = QPushButton(self.t("voice_all_endings", "All endings"))
        self.btn_all.setObjectName("Quiet")
        self.btn_all.setCheckable(True)
        self.btn_all.setToolTip(self.t("voice_all_endings_tt", "Also list the sentences that end as finished."))
        self.btn_all.toggled.connect(self._toggle_all)
        lh.addWidget(self.btn_all)
        lh.addStretch(1)
        left.addLayout(lh)
        from PySide6.QtWidgets import QLineEdit
        from PySide6.QtCore import QTimer as _QT
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(self.t("voice_search_ph", "Search the transcript…"))
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setToolTip(self.t("voice_search_tt", "Find the words you want to patch: every sentence that "
                                          "contains them can be patched."))
        self.search_box.setStyleSheet(f"QLineEdit {{ background: #1e1e1e; color: #e6e6e6; border: 1px solid #333333; "
                                      f"border-radius: {config.S(8)}px; padding: {config.S(6)}px {config.S(10)}px; "
                                      f"font-size: {config.FS(10)}pt; }} QLineEdit:focus {{ border-color: {config.BTN_BG}; }}")
        self._query = ""
        self._search_timer = _QT(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_box.textChanged.connect(lambda _t: self._search_timer.start())
        left.addWidget(self.search_box)
        self.list_status = _lbl("", 9, "#8a8a8a")
        left.addWidget(self.list_status)
        self.btn_get_audio = QPushButton(self.t("review_get_audio", "Get the recording"))
        self.btn_get_audio.setObjectName("Quiet")
        self.btn_get_audio.setToolTip(self.t("review_get_audio_tt", "Rebuilds the timeline's audio from DaVinci Resolve: "
                                             "this project must be open there."))
        self.btn_get_audio.clicked.connect(self._get_audio)
        self.btn_get_audio.hide()
        left.addWidget(self.btn_get_audio, 0, Qt.AlignLeft)
        from gui.widgets.progress_bar import LiquidProgressBar
        self.audio_progress = LiquidProgressBar(self)
        self.audio_progress.setFixedHeight(config.S(6))
        self.audio_progress.hide()
        left.addWidget(self.audio_progress)
        self.list_box = QWidget()
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(0, 0, config.S(6), 0)
        self.list_lay.setSpacing(config.S(6))
        self.list_lay.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
        sc.setWidget(self.list_box)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # rows elide their text instead
        self.list_scroll = sc
        left.addWidget(sc, 1)
        lw = QWidget()
        lw.setLayout(left)
        lw.setMinimumWidth(config.TS(300))
        lw.setMaximumWidth(config.TS(400))
        body.addWidget(lw, 1)          # wider on a big window (300-400), the detail takes the rest

        self.detail = _card("VoiceDetail")
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(config.S(20), config.S(18), config.S(20), config.S(18))
        dl.setSpacing(config.S(12))
        self.d_sentence = _lbl("", 13, "#ffffff", 500)
        self.d_sentence.setTextFormat(Qt.RichText)
        self.d_facts = _lbl("", 9.5, "#a3a3a3")
        dl.addWidget(self.d_sentence)
        dl.addWidget(self.d_facts)

        # A passage is replaced as a whole: the words VoiceStudio says, editable (a rewrite)
        self.words_box = QWidget()
        wl = QVBoxLayout(self.words_box)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(config.S(6))
        wh = QHBoxLayout()
        wh.setSpacing(config.S(8))
        self.words_title = _lbl(self.t("voice_edit_words", "Edit the words"), 9.5, "#ffffff", 600, wrap=False)
        self.words_title.setToolTip(self.t("voice_edit_words_tt", "By default VoiceStudio says the same words cleanly "
                                           "(without fillers and restarts). Change them to rewrite the passage."))
        wh.addWidget(self.words_title)
        self.words_note = _lbl("", 9, "#c084fc", wrap=False)
        self.words_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        wh.addWidget(self.words_note, 1)
        self.btn_words_reset = QPushButton(self.t("voice_reset", "Reset"))
        self.btn_words_reset.setObjectName("Small")
        self.btn_words_reset.setToolTip(self.t("voice_reset_tt", "Back to the passage's own words, said cleanly."))
        self.btn_words_reset.clicked.connect(self._reset_words)
        wh.addWidget(self.btn_words_reset)
        wl.addLayout(wh)
        self.words_edit = QPlainTextEdit()
        self.words_edit.setObjectName("Words")
        self.words_edit.setTabChangesFocus(True)
        fm = self.words_edit.fontMetrics()
        self.words_edit.setFixedHeight(int(fm.lineSpacing() * 2.6) + config.S(20))
        self.words_edit.textChanged.connect(self._words_changed)
        wl.addWidget(self.words_edit)
        dl.addWidget(self.words_box)

        acts = QHBoxLayout()
        acts.setSpacing(config.S(8))
        self.btn_make = QPushButton(self.t("voice_make", "Generate patch"))
        self.btn_make.setObjectName("Primary")
        _set_icon(self.btn_make, "ai", primary=True)
        self.btn_make.clicked.connect(lambda: self._make("tts"))
        self.btn_rec = QPushButton(self.t("voice_use_recording", "Use my recording…"))
        self.btn_rec.setObjectName("Quiet")
        _set_icon(self.btn_rec, "voice")
        self.btn_rec.setToolTip(self.t("voice_use_recording_tt", "Say it the way you want: StoryCraft uses your words "
                                        "and picks the take whose melody and pace are closest to yours."))
        self.btn_rec.clicked.connect(lambda: self._make("convert"))
        self.btn_rewrite = QPushButton(self.t("voice_rewrite_sentence", "Rewrite the sentence…"))
        self.btn_rewrite.setObjectName("Quiet")
        self.btn_rewrite.setToolTip(self.t("voice_rewrite_sentence_tt", "Change the sentence's words: it opens as a "
                                           "passage you can rewrite entirely, then patch."))
        self.btn_rewrite.clicked.connect(self._rewrite_sentence)
        acts.addWidget(self.btn_make)
        acts.addWidget(self.btn_rec)
        acts.addWidget(self.btn_rewrite)
        acts.addStretch(1)
        self._build_delivery(dl)      # Delivery ▾ above the buttons it steers
        dl.addLayout(acts)
        self.d_progress = _lbl("", 9.5, "#c084fc")
        dl.addWidget(self.d_progress)

        players = QHBoxLayout()
        players.setSpacing(config.S(12))
        self._players_lay = players
        self.pa = self._player_card("A", self.t("voice_original", "Original"))
        acol = QVBoxLayout()
        acol.setSpacing(config.S(6))
        self._a_spacer = _lbl(" ", 10, "#ffffff", 600, wrap=False)   # A's card level with the first take, under B's title
        acol.addWidget(self._a_spacer)
        acol.addWidget(self.pa["card"])
        acol.addStretch(1)
        players.addLayout(acol, 1)
        # B: every take VoiceStudio made, one row each with its own play button (StoryCraft's pick has a
        # star); the selected take is the patch Use in the story, Save patch and Show in Finder act on
        self.takes_col = QWidget()
        self.takes_col.setObjectName("TakesCol")
        self.takes_col.setAttribute(Qt.WA_StyledBackground, True)
        self.takes_col.setStyleSheet(
            f"QWidget#TakesCol {{ background: transparent; }}"
            f" QPushButton#TakeRow {{ text-align: left; background-color: {CARD}; border: 1px solid {LINE};"
            f" border-radius: {config.S(14)}px; padding: 0px; }}"
            f" QPushButton#TakeRow:hover {{ border-color: #4a4a4a; }}"
            f" QPushButton#TakeRow:checked {{ border-color: #6b4fa8; background-color: #2a2140; }}"
            f" QPushButton#TakeRow:disabled {{ background-color: #1e1e1e; border-color: #2a2a2a; }}")
        tc = QVBoxLayout(self.takes_col)
        tc.setContentsMargins(0, 0, 0, 0)
        tc.setSpacing(config.S(6))
        th = QHBoxLayout()
        th.setSpacing(config.S(8))
        self.takes_title = _lbl(f"B · {self.t('voice_patched', 'Patched')}", 10, "#ffffff", 600, wrap=False)
        self.takes_hint = _lbl(self.t("voice_takes_hint", "Click a take to use it as the patch"), 8.5, "#8a8a8a", wrap=False)
        self.takes_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        th.addWidget(self.takes_title)
        th.addWidget(self.takes_hint, 1)
        tc.addLayout(th)
        self._pb_empty = self._player_card("B", self.t("voice_patched", "Patched"))   # before there is a patch
        tc.addWidget(self._pb_empty["card"])
        self.takes_lay = QVBoxLayout()
        self.takes_lay.setContentsMargins(0, 0, 0, 0)
        self.takes_lay.setSpacing(config.S(6))
        tc.addLayout(self.takes_lay)
        players.addWidget(self.takes_col, 1, Qt.AlignTop)
        self.pb = self._pb_empty      # B: the selected take's row (the empty card while there is none)
        self._take_rows = []
        self._making = None           # {"key", "total", "k", "fitting"} while VoiceStudio makes the takes
        dl.addLayout(players)
        self.d_result = _lbl("", 9.5, "#d4d4d4")
        dl.addWidget(self.d_result)
        res_acts = QHBoxLayout()
        self.btn_use = QPushButton(self.t("voice_use", "Insert patch"))
        self.btn_use.setObjectName("Primary")
        self.btn_use.setCheckable(True)
        _set_icon(self.btn_use, "insert", primary=True)
        self.btn_use.setToolTip(self.t("voice_use_tt", "Send to Resolve then lays this patch on its own audio track at "
                                       "the right frame and mutes the original words under it (the picture stays)."))
        # The label first: _toggle_use may uncheck the button again (length dialog cancelled)
        self.btn_use.toggled.connect(lambda on: self.btn_use.setText(
            self.t("voice_in_edit_btn", "In the story ✓") if on else self.t("voice_use", "Insert patch")))
        self.btn_use.toggled.connect(self._toggle_use)
        res_acts.addWidget(self.btn_use)
        self.btn_save = QPushButton(self.t("voice_save", "Save patch…"))
        self.btn_save.setObjectName("Quiet")
        _set_icon(self.btn_save, "download")
        self.btn_save.clicked.connect(self._save_patch)
        self.btn_reveal = QPushButton(self.t("voice_reveal_mac", "Show in Finder") if sys.platform == "darwin"
                                      else self.t("voice_reveal_files", "Show files"))
        self.btn_reveal.setObjectName("Quiet")
        _set_icon(self.btn_reveal, "folder")
        self.btn_reveal.clicked.connect(self._reveal)
        res_acts.addWidget(self.btn_save)
        res_acts.addWidget(self.btn_reveal)
        res_acts.addStretch(1)
        dl.addLayout(res_acts)
        dl.addStretch(1)
        dl.addWidget(_lbl(self.t("voice_consent", "Patches are synthetic speech. Use them only with the speaker's "
                                 "permission, and say so where your audience would expect to know."), 8.5, "#8a8a8a"))
        # The detail scrolls when the window is short (a passage adds the words box)
        dsc = QScrollArea()
        dsc.setWidgetResizable(True)
        dsc.setFrameShape(QFrame.NoFrame)
        dsc.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }")
        dsc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        dsc.setWidget(self.detail)
        body.addWidget(dsc, 3)
        outer.addLayout(body, 1)
        self._show_detail(None)

    # ── Delivery: how the patch is said (voice_patch.core.norm_delivery) ─────
    def _build_delivery(self, dl):
        """Delivery ▾ (closed by default) and its four choices: Ending, Pitch, Pace, Sound. The choices are
        kept for the session (on the main window), not per patch; the defaults make the patch as before."""
        from gui.panels.script_panel import style_segmented
        from gui.widgets.segmented import SegmentedChoice
        from voice_patch.core import DELIVERY_DEFAULT, norm_delivery
        self._delivery = norm_delivery(getattr(self.win, "_voice_delivery", None) or DELIVERY_DEFAULT)
        head = QHBoxLayout()
        head.setSpacing(config.S(8))
        self.btn_delivery = QPushButton()
        self.btn_delivery.setObjectName("Quiet")
        self.btn_delivery.setCheckable(True)
        _set_icon(self.btn_delivery, "settings")
        self.btn_delivery.setToolTip(self.t("voice_delivery_tt", "How the patch is said: how it ends, its pitch, "
                                            "its pace and its sound."))
        head.addWidget(self.btn_delivery)
        self.dl_summary = _lbl("", 9, "#c084fc", wrap=False)
        self.dl_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        head.addWidget(self.dl_summary, 1)
        dl.addLayout(head)

        self.dl_panel = QFrame()
        self.dl_panel.setObjectName("DeliveryPanel")
        self.dl_panel.setStyleSheet(f"QFrame#DeliveryPanel {{ background-color: #1e1e1e; border: 1px solid {LINE};"
                                    f" border-radius: {config.S(10)}px; }}")
        g = QGridLayout(self.dl_panel)
        g.setContentsMargins(config.S(12), config.S(10), config.S(12), config.S(10))
        g.setHorizontalSpacing(config.S(12))
        g.setVerticalSpacing(config.S(8))
        self._dl_seg = {}

        def row(r, key, title, tip, widget):
            lb = _lbl(title, 9.5, "#d4d4d4", 600, wrap=False)
            lb.setToolTip(tip)
            widget.setToolTip(tip)
            g.addWidget(lb, r, 0, Qt.AlignVCenter)
            g.addWidget(widget, r, 1, Qt.AlignLeft | Qt.AlignVCenter)

        def seg(key, choices):
            w = SegmentedChoice(choices, self._delivery[key])
            style_segmented(w.buttons.values(), 9)
            w.changed.connect(lambda v, k=key: self._set_delivery(k, v))
            self._dl_seg[key] = w
            return w
        row(0, "ending", self.t("voice_dl_ending", "Ending"),
            self.t("voice_dl_ending_tt", "Falls: a finished statement. Rises: a question, or a thought that goes on. "
                   "As recorded: like the speaker said it here."),
            seg("ending", [("fall", self.t("voice_dl_falls", "Falls")), ("rise", self.t("voice_dl_rises", "Rises")),
                           ("asis", self.t("voice_dl_asis", "As recorded"))]))
        # Pitch: matched to the speaker around the patch, then lower / higher by half a semitone
        pw = QWidget()
        pl = QHBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        self.btn_pitch_down = QPushButton("−")
        self.btn_pitch_down.setToolTip(self.t("voice_dl_lower", "Lower"))
        self.btn_pitch_val = QPushButton()
        self.btn_pitch_up = QPushButton("+")
        self.btn_pitch_up.setToolTip(self.t("voice_dl_higher", "Higher"))
        for b in (self.btn_pitch_down, self.btn_pitch_val, self.btn_pitch_up):
            b.setCursor(Qt.PointingHandCursor)
            pl.addWidget(b)
        style_segmented([self.btn_pitch_down, self.btn_pitch_val, self.btn_pitch_up], 9)
        fm = self.btn_pitch_val.fontMetrics()
        self.btn_pitch_val.setMinimumWidth(fm.horizontalAdvance(self.t("voice_dl_match", "Match the speaker")) + config.S(28))
        self.btn_pitch_down.clicked.connect(lambda: self._set_delivery("pitch_st", self._delivery["pitch_st"] - 0.5))
        self.btn_pitch_up.clicked.connect(lambda: self._set_delivery("pitch_st", self._delivery["pitch_st"] + 0.5))
        self.btn_pitch_val.clicked.connect(lambda: self._set_delivery("pitch_st", 0.0))
        row(1, "pitch", self.t("voice_dl_pitch", "Pitch"),
            self.t("voice_dl_pitch_tt", "StoryCraft tunes the patch to the speaker's pitch around it. − and + make "
                   "it lower or higher, half a semitone at a time."), pw)
        row(2, "pace", self.t("voice_dl_pace", "Pace"),
            self.t("voice_dl_pace_tt", "How fast VoiceStudio says the words. Slower or faster patches keep their pace "
                   "when they are fitted."),
            seg("pace", [("slower", self.t("voice_dl_slower", "Slower")), ("natural", self.t("voice_dl_natural", "Natural")),
                         ("faster", self.t("voice_dl_faster", "Faster"))]))
        row(3, "sound", self.t("voice_dl_sound", "Sound"),
            self.t("voice_dl_sound_tt", "Match the room: the patch gets the recording's tone and room noise. "
                   "Clean: the voice alone, at the same loudness."),
            seg("sound", [("room", self.t("voice_dl_room", "Match the room")), ("clean", self.t("voice_dl_clean", "Clean"))]))
        g.setColumnStretch(2, 1)
        dl.addWidget(self.dl_panel)
        self.btn_delivery.setChecked(bool(getattr(self.win, "_voice_delivery_open", False)))
        self.dl_panel.setVisible(self.btn_delivery.isChecked())
        self.btn_delivery.toggled.connect(self._toggle_delivery)
        self._sync_delivery()

    def _toggle_delivery(self, on):
        self.win._voice_delivery_open = bool(on)
        self.dl_panel.setVisible(on)
        self._sync_delivery()

    def _set_delivery(self, key, value):
        from voice_patch.core import norm_delivery
        self._delivery = norm_delivery(dict(self._delivery, **{key: value}))
        self.win._voice_delivery = dict(self._delivery)     # the session's choice (every patch, every visit)
        self._sync_delivery()

    def _sync_delivery(self):
        d = self._delivery
        arrow = "▴" if self.btn_delivery.isChecked() else "▾"
        self.btn_delivery.setText(f"{self.t('voice_delivery', 'Delivery')} {arrow}")
        for k, w in self._dl_seg.items():
            w.set_value(d[k])
        st = d["pitch_st"]
        self.btn_pitch_val.setText(self.t("voice_dl_match", "Match the speaker") if not st else
                                   self.t("voice_dl_st", "{n} st").format(n=f"{st:+g}"))
        self.btn_pitch_down.setEnabled(st > -2.0)
        self.btn_pitch_up.setEnabled(st < 2.0)
        self.dl_summary.setText(self._delivery_text(d))

    def _delivery_text(self, d, report=False):
        """'Ending rises · +1 st · faster · clean' for what differs from the defaults ('' when nothing does).
        report=True: d is a report's delivery (a patch made from the user's recording took its melody there)."""
        from voice_patch.core import norm_delivery
        n = norm_delivery(d)
        parts = []
        if report and (d or {}).get("melody_from") == "recording":
            parts.append(self.t("voice_dl_sum_rec", "melody from your recording"))
        elif n["ending"] != "fall":
            parts.append({"rise": self.t("voice_dl_sum_rise", "ending rises"),
                          "asis": self.t("voice_dl_sum_asis", "ending as recorded")}[n["ending"]])
        if n["pitch_st"]:
            parts.append(self.t("voice_dl_st", "{n} st").format(n=f"{n['pitch_st']:+g}"))
        if n["pace"] != "natural":
            parts.append({"slower": self.t("voice_dl_sum_slower", "slower"),
                          "faster": self.t("voice_dl_sum_faster", "faster")}[n["pace"]])
        if n["sound"] != "room":
            parts.append(self.t("voice_dl_sum_clean", "clean"))
        text = " · ".join(parts)
        return text[:1].upper() + text[1:]

    def resizeEvent(self, e):
        super().resizeEvent(e)
        from PySide6.QtWidgets import QBoxLayout
        # A and B side by side when there is room, one above the other when not
        narrow = self.detail.width() < config.S(560)
        self._players_lay.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self._sync_a_spacer()

    def _sync_a_spacer(self):
        side_by_side = self._players_lay.direction() == self._players_lay.Direction.LeftToRight
        self._a_spacer.setVisible(side_by_side and not self.takes_title.isHidden())

    # ── players ─────────────────────────────────────────────────────────────
    def _player_card(self, tag, title):
        card = _card("VoicePlayer" + tag)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(config.S(14), config.S(10), config.S(14), config.S(10))
        btn = self._play_button()
        btn.setToolTip(title)
        col = QVBoxLayout()
        col.setSpacing(0)
        name = _lbl(f"{tag} · {title}", 10, "#ffffff", 600, wrap=False)
        sub = _lbl("", 9, "#a3a3a3", wrap=False)
        col.addWidget(name)
        col.addWidget(sub)
        lay.addWidget(btn)
        lay.addLayout(col, 1)
        p = {"card": card, "btn": btn, "sub": sub, "name": name, "path": None}
        btn.clicked.connect(lambda: self._play(p))
        return p

    @staticmethod
    def _play_button():
        btn = QPushButton("▶")
        btn.setFixedSize(config.S(38), config.S(38))
        btn.setStyleSheet(f"QPushButton {{ background-color: #ffffff; color: #1e1e1e; border: none; border-radius: {config.S(19) - 0.5}px;"
                          f" font-size: {config.FS(10)}pt; }} QPushButton:disabled {{ background-color: #333333; color: #666666; }}")
        return btn

    def _take_row(self, s):
        """One take on the right: a selectable card (violet when it is the patch) with its own play button,
        its length and StoryCraft's star. s: a spec from _take_specs."""
        row = QPushButton()
        row.setObjectName("TakeRow")
        row.setCheckable(True)
        row.setChecked(bool(s.get("chosen")))
        row.setEnabled(bool(s.get("ready")))
        row.setCursor(Qt.PointingHandCursor)
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(config.S(14), config.S(8), config.S(14), config.S(8))
        lay.setSpacing(config.S(10))
        btn = self._play_button()
        btn.setToolTip(self.t("voice_take_play_tt", "Play this take"))
        col = QVBoxLayout()
        col.setSpacing(0)
        name = _lbl(self.t("voice_take", "Take {n}").format(n=s["n"]), 10, "#ffffff" if s.get("ready") else "#8a8a8a", 600, wrap=False)
        sub = _lbl(s.get("sub", ""), 9, "#a3a3a3", wrap=False)
        sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(name)
        col.addWidget(sub)
        lay.addWidget(btn)
        lay.addLayout(col, 1)
        labels = [name, sub]
        if s.get("best"):
            star = _lbl(self.t("voice_take_best", "★ StoryCraft's pick"), 8.5, "#d8b4fe", 600, wrap=False)
            lay.addWidget(star, 0, Qt.AlignVCenter)
            labels.append(star)
        for lb in labels:          # a click on the text selects the take (the row gets it)
            lb.setTextInteractionFlags(Qt.NoTextInteraction)
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.setMinimumHeight(config.S(38) + config.S(16) + 2)
        if s.get("tip"):
            row.setToolTip(s["tip"])
        p = {"card": row, "btn": btn, "sub": sub, "name": name, "path": s.get("path"), "take": s["n"]}
        btn.clicked.connect(lambda: self._play(p))
        if s.get("ready"):
            row.clicked.connect(lambda _=False, n=s["n"]: self._choose_take(n))
        return p

    def _players(self):
        return [self.pa, self._pb_empty] + list(self._take_rows)

    def _ensure_player(self):
        if self._player is None:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._player = QMediaPlayer(self)
            self._audio_out = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_out)
            self._player.playbackStateChanged.connect(lambda _s: self._sync_play_buttons())
        return self._player

    def _play(self, p):
        if not p["path"] or not os.path.isfile(p["path"]):
            return
        pl = self._ensure_player()
        from PySide6.QtMultimedia import QMediaPlayer
        same = getattr(self, "_playing", None) is p and getattr(self, "_playing_path", None) == p["path"]
        if same and pl.playbackState() == QMediaPlayer.PlayingState:
            pl.pause()
            return
        if not same:
            pl.setSource(QUrl.fromLocalFile(p["path"]))
            self._playing, self._playing_path = p, p["path"]
        ap = getattr(self.win, "audio_preview", None)
        if ap is not None and hasattr(ap, "pause_for_other_player"):
            ap.pause_for_other_player()        # the story stops: never two audios at once
        pl.play()

    def _sync_play_buttons(self):
        from PySide6.QtMultimedia import QMediaPlayer
        playing = self._player is not None and self._player.playbackState() == QMediaPlayer.PlayingState
        for p in self._players():
            p["btn"].setText("❚❚" if playing and getattr(self, "_playing", None) is p else "▶")

    def stop(self):
        if self._player is not None:
            self._player.stop()

    def toggle_play(self):
        """Space on this page: play / pause the selected side (the one played last for this
        candidate, A otherwise; the selected take when only B can play)."""
        p = getattr(self, "_playing", None)
        if not any(p is q for q in self._players()) or not p["path"] or getattr(self, "_playing_path", None) != p["path"]:
            p = self.pa if self.pa["path"] else self.pb
        self._play(p)

    SKIP_MS = 2000

    def skip(self, direction):
        """← / → on this page: back or on in what A or a take is playing."""
        pl = self._player
        if pl is None or pl.source().isEmpty():
            return
        pos = pl.position() + (self.SKIP_MS if direction > 0 else -self.SKIP_MS)
        dur = pl.duration()
        pl.setPosition(max(0, min(pos, dur - 50) if dur > 0 else pos))

    # ── connection ──────────────────────────────────────────────────────────
    def check_connection(self):
        self.conn_text.setText(self.t("voice_checking", "Checking VoiceStudio…"))

        def work(_progress):
            from voice_patch.client import VoiceStudioClient
            vs = VoiceStudioClient()
            vs.health()
            eng = vs.tts_engine()
            return {"ok": True, "base": vs.base, "engine": eng, "licence": vs.model_licence(eng.get("model")) or {},
                    "watermark": vs.watermark_status() or {}}
        job = _Job(work, self)
        job.done.connect(self._show_connection)
        job.failed.connect(lambda msg: self._show_connection({"ok": False, "error": msg}))
        job.start()
        self._conn_job = job

    def _show_connection(self, c):
        from gui.theme import qc
        self._conn = c
        ok = c.get("ok")
        self.dot.setStyleSheet(f"color: {qc('#23a559').name() if ok else '#ed4245'}; font-size: {config.FS(11)}pt;")
        if not ok:
            self.conn_text.setText(self.t("voice_not_connected", "VoiceStudio isn't running"))
            self.engine_text.setText("")
            self.lic_badge.hide()
            self.wm_text.setText("")
            self.conn_help.setText(self.t("voice_setup", "1. Install VoiceStudio (free, voicestudio.sh) and open it.  "
                                          "2. In its Model Manager, choose a voice engine that can clone (CosyVoice 3 for "
                                          "paid work).  3. Leave it open, then press Check again."))
            self.conn_help.show()
        else:
            eng, lic, wm = c["engine"], c.get("licence") or {}, c.get("watermark") or {}
            self.conn_text.setText(self.t("voice_connected", "VoiceStudio connected"))
            self.engine_text.setText(f"{eng.get('name') or eng.get('id')} · {eng.get('model') or ''}")
            cat = str(lic.get("license_category") or lic.get("review_status") or "").lower()
            text = str(lic.get("license") or "")
            if "noncommercial" in cat or "by-nc" in text.lower():
                self.lic_badge.setText(self.t("voice_lic_nc", "Non-commercial"))
                self.lic_badge.setStyleSheet(self.lic_badge.styleSheet().split("color:")[0] +
                                             f"color: #fecaca; background-color: #3a1720; border-radius: {config.S(9)}px;"
                                             f" font-size: {config.FS(8.5)}pt; font-weight: 600; padding: {config.S(2)}px {config.S(9)}px;")
                self.conn_help.setText(self.t("voice_lic_nc_help", "This model's licence doesn't allow paid work. For client "
                                              "projects, pick CosyVoice 3 in VoiceStudio's Model Manager."))
                self.conn_help.show()
            else:
                self.lic_badge.setText(text.split(";")[0][:28] or self.t("voice_lic_check", "Check licence"))
                self.lic_badge.setStyleSheet(f"color: #a7f3d0; background-color: #0f2e24; border-radius: {config.S(9)}px;"
                                             f" font-size: {config.FS(8.5)}pt; font-weight: 600; padding: {config.S(2)}px {config.S(9)}px;")
                self.conn_help.hide()
            self.lic_badge.setToolTip(text)
            self.lic_badge.show()
            on = wm.get("invisible_enabled") and wm.get("audioseal_available", True)
            self.wm_text.setText(self.t("voice_wm_on", "Watermark on") if on else self.t("voice_wm_off", "Watermark off"))
            self.wm_text.setToolTip(self.t("voice_wm_tt", "VoiceStudio's invisible watermark lets anyone check later that "
                                           "the audio is synthetic. Turn it on in VoiceStudio's settings."))
        self._update_buttons()

    def _open_voicestudio(self):
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "VoiceStudio"])
        QTimer.singleShot(6000, self.check_connection)

    # ── candidates ──────────────────────────────────────────────────────────
    def _words(self):
        canvas = getattr(self.win, "text_canvas", None)
        return getattr(canvas, "words_data", None) or []

    def _timeline(self):
        from voice_patch import core
        snap = getattr(self.win, "_transcription_source", None) or {}
        return core.timeline_from_words(self._words(), snap.get("timeline_name") or "Timeline")

    def _out_dir(self):
        return self.win._voice_patch_dir()  # the same folder Send to Resolve reads the accepted patches from

    def refresh(self):
        """Connection, then the open transcript's candidates (endings re-measured when the audio changed)."""
        self.check_connection()
        self._sync_transcript(self._accepted_map())   # patches put in the edit before the transcript showed them
        from voice_patch import core
        core.MODELS_DIR = os.path.join(self.win.engine.os_doc.install_dir, "models")
        tl = self._timeline()
        if not tl["words"]:
            self._an, self._rows, self._audio16 = None, [], None
            self._update_groups(measured=False)
            self._fill_list()
            self.list_status.setText(self.t("voice_no_transcript", "Open a timeline first: the endings come from its transcript."))
            return
        if not tl["audio_path"] or not os.path.isfile(tl["audio_path"]):
            self._an, self._rows, self._audio16 = None, [], None
            self._update_groups(measured=False)
            self._fill_list()
            self.list_status.setText(self.t("voice_no_audio", "The recording of this timeline isn't on disk any more "
                                            "(temporary files are cleared). Get it back to measure the endings."))
            self.btn_get_audio.show()
            return
        self.btn_get_audio.hide()
        if self._an is not None and self._an.tl.get("audio_path") == tl["audio_path"] and self._endings_measured:
            self._an.tl = tl
            self._update_groups(measured=True)
            self._fill_list()
            return
        self._update_groups(measured=False)
        self._fill_list()
        self.list_status.setText(self.t("voice_scanning", "Listening to the sentence endings…"))

        def work(_progress):
            an = core.Analysis(tl)
            return an, core.detect(an)
        self._run(work, self._scanned)

    def _get_audio(self):
        ap = getattr(self.win, "audio_preview", None)
        if ap is None or not hasattr(ap, "_on_fetch_missing_audio"):
            return
        self.btn_get_audio.setEnabled(False)
        self.list_status.setText(self.t("review_getting_audio", "Getting the recording… this can take a minute."))
        self.audio_progress.show()
        self.audio_progress.set_value(-1)
        ap._on_fetch_missing_audio()

        def _wait():
            if getattr(ap, "_fetching_audio", False):
                QTimer.singleShot(400, _wait)
                return
            self.audio_progress.hide()
            self.btn_get_audio.setEnabled(True)
            self.refresh()
        QTimer.singleShot(500, _wait)

    def _scanned(self, result):
        self._an, self._rows = result
        self._audio16 = self._an.x[::3] if self._an is not None else None   # 48 kHz -> 16 kHz (levels only)
        self._update_groups(measured=True)
        self._fill_list()
        if self._sel is not None and self.pa["path"] is None:
            self._show_detail(self._sel)     # the recording is loaded now: A can play the passage

    def _update_groups(self, measured):
        """The four groups, from the main window (the Found row in Clean counts the same lists)."""
        self._endings_measured = measured
        try:
            g = self.win._voice_candidates(endings=self._rows if measured else None, audio=self._audio16)
        except Exception as e:
            from osdoc import log_error
            log_error(f"voice candidates: {e}")
            g = {}
        self._groups = {k: list(g.get(k) or []) for k in GROUP_ORDER}
        if self._sel is not None:   # keep the selection on the refreshed copy of the same candidate
            k = row_key(self._sel)
            self._sel = next((c for c in self._all_candidates() if row_key(c) == k), self._sel)
        try:
            self.win._refresh_voice_found()
        except Exception:
            pass

    def _all_candidates(self):
        for g in GROUP_ORDER:
            yield from self._shown(g)

    def _shown(self, group):
        if group == "endings":
            if self._show_all:
                return [dict(r, group="endings", kind="ending") for r in sorted(self._scope(self._rows), key=lambda r: r["time"])]
            return sorted(self._scope(self._groups.get("endings")), key=lambda r: -r["score"])
        return sorted(self._scope(self._groups.get(group)), key=lambda c: c["time"])

    def _toggle_all(self, on):
        self._show_all = on
        self._fill_list()

    # ── the story or the whole interview ───────────────────────────────────
    def _story_spans(self):
        """The story's clips as (start, end) seconds of the recording; [] without a story."""
        out = []
        for c in (getattr(self.win, "_story", None) or {}).get("clips") or []:
            try:
                out.append((float(c["cut_start"]), float(c["cut_end"])))
            except (KeyError, TypeError, ValueError):
                pass
        return out

    @staticmethod
    def _row_span(row):
        if row.get("kind") == "passage" or "start_t" not in row:
            return float(row.get("start", row.get("time", 0.0))), float(row.get("end", row.get("time", 0.0)))
        return float(row["start_t"]), float(row["time"])

    def in_story(self, row, spans=None):
        """Does the candidate's time range overlap a story clip (more than a breath at a clip's edge)?"""
        spans = self._story_spans() if spans is None else spans
        a, b = self._row_span(row)
        need = min(0.2, max(0.0, b - a) / 2.0)
        return any(min(b, ce) - max(a, cs) > need or (cs <= a and b <= ce) for cs, ce in spans)

    def _when(self, row, t=None):
        """(shown, tooltip) for a candidate's time: its place in the story ("0:12 in the story") when it is
        in the story, else in the recording ("1:36 in the recording"); the tooltip says the other one."""
        import story_time as st
        if t is None:
            t = row["start"] if row.get("kind") == "passage" and "start" in row else row.get("time", 0.0)
        t = float(t or 0.0)
        rec = self.t("time_in_recording", "{time} in the recording").format(time=st.fmt_time(t))
        e = st.edit_time(self.win, t)
        if e is None and self.in_story(row):
            # the candidate overlaps the story but this moment sits just outside a clip: its nearest edge
            a, b = self._row_span(row)
            for cs, ce in self._story_spans():
                if min(b, ce) > max(a, cs):
                    e = st.edit_time(self.win, min(max(t, cs), ce))
                    if e is not None:
                        break
        if e is None:
            return rec, self.t("time_not_in_story", "Not in the story")
        return self.t("time_in_story", "{time} in the story").format(time=st.fmt_time(e)), rec

    def _scope(self, rows):
        """rows narrowed to the story, unless Whole interview is on or there is no story."""
        rows = list(rows or [])
        spans = [] if self._whole else self._story_spans()
        if not spans:
            return rows
        return [r for r in rows if self.in_story(r, spans)]

    def _shown_unscoped(self, group):
        if group == "endings" and self._show_all:
            return list(self._rows)
        return list(self._groups.get(group) or [])

    def _toggle_whole(self, on):
        self._whole = on
        if not on and self._sel is not None and not self._query and not self._scope([self._sel]):
            self._sel = None            # it left the list: the first one in the story instead
            self._show_detail(None)
        self._fill_list()

    def _apply_search(self):
        q = self.search_box.text().strip()
        if q == self._query:
            return
        self._query = q
        self._sel = None
        self._fill_list()

    def _matches_query(self, row):
        import re as _re
        q = [_re.sub(r"[^\w']+", "", x.lower()) for x in self._query.split()]
        q = " ".join(x for x in q if x)
        text = row.get("text") if row.get("kind") == "passage" else row.get("sentence", "")
        hay = " ".join(_re.sub(r"[^\w']+", "", x.lower()) for x in str(text or "").split())
        return bool(q) and q in hay

    def _search_rows(self):
        """Sentences of the transcript with the searched words (cached per query and transcript)."""
        from voice_patch import candidates as VC
        words = self._words() or []
        key = (self._query, id(words), len(words))
        if getattr(self, '_search_cache', (None,))[0] != key:
            prefs = self.win.engine.load_preferences() or {}
            fillers = prefs.get('filler_words', config.DEFAULT_BAD_WORDS)
            try:
                rows = VC.search(words, self._query, fillers)
            except Exception:
                rows = []
            self._search_cache = (key, rows)
        return self._search_cache[1]

    def _toggle_group(self, group):
        (self._collapsed.discard if group in self._collapsed else self._collapsed.add)(group)
        self._fill_list()

    def _group_title(self, g):
        return {"endings": self.t("voice_endings", "Endings that sound unfinished"),
                "hard": self.t("voice_g_hard", "Hard to understand"),
                "incoherent": self.t("voice_g_incoherent", "Incoherent"),
                "tagged": self.t("voice_g_tagged", "Tagged by you"),
                "search": self.t("voice_g_search", "Found in the transcript")}[g]

    def _reason_text(self, row):
        if row.get("kind") != "passage":
            shape = {"rising": self.t("voice_rising", "rising"), "flat": self.t("voice_level", "level"),
                     "falling": self.t("voice_falling", "falling")}.get(row["shape"], row["shape"])
            return f"{shape} {row['delta_st']:+.1f} st"
        out = []
        for r in row.get("reasons") or []:
            c = r.get("code")
            if c == "clarity":
                out.append(self.t("voice_r_clarity", "clarity {value} of 100").format(value=r.get("value", 0)))
            elif c == "inaudible":
                out.append(self.t("voice_r_inaudible", "next to a sound without words"))
            elif c == "restart":
                out.append(self.t("voice_r_restart", "restart at “{words}”").format(words=r.get("words", "")))
            elif c == "broken":
                out.append(self.t("voice_r_broken", "broken off at “{words}”").format(words=r.get("words", "")))
            elif c == "fillers":
                out.append(self.t("voice_r_fillers", "{count} fillers").format(count=r.get("count", 0)))
            elif c == "tagged":
                out.append(self.t("voice_r_tagged", "marked by you"))
            elif c == "search":
                out.append(self.t("voice_r_search", "contains “{words}”").format(words=r.get("words", "")))
        return ", ".join(out)

    def select_passage_ids(self, ids):
        """Open the Tagged passage holding these words (Send to voice patch from the transcript)."""
        self._pending_ids = set(ids or ())
        if self._query:
            self.search_box.blockSignals(True)
            self.search_box.clear()
            self.search_box.blockSignals(False)
            self._query = ""
        self._collapsed.discard("tagged")
        self._fill_list()

    def _resolve_pending(self):
        pend = getattr(self, '_pending_ids', None)
        if not pend:
            return False
        for r in self._groups.get("tagged") or []:
            if set(r.get("idx") or ()) & pend:
                self._pending_ids = None
                self._sel = r
                if not self._whole and self._story_spans() and not self.in_story(r):
                    self._whole = True                 # words outside the story: list the whole interview
                    self.btn_whole.blockSignals(True)
                    self.btn_whole.setChecked(True)
                    self.btn_whole.blockSignals(False)
                return True
        return False

    def _fill_list(self):
        picked = self._resolve_pending()
        while self.list_lay.count() > 1:
            it = self.list_lay.takeAt(0)
            if it.widget():
                it.widget().hide()          # gone at once, not when the deletion runs
                it.widget().deleteLater()
        self.btn_whole.setVisible(bool(self._story_spans()))
        if self._rows:
            rows = self._scope(self._rows)
            n_bad = sum(1 for r in rows if r["unfinished"])
            self.list_status.setText(self.t("voice_found_plain", "{bad} of {all} sentences end as if unfinished").format(
                bad=n_bad, all=len(rows)))
        accepted = self._accepted()
        first = None
        searching = bool(self._query)
        groups = (("search",) + GROUP_ORDER) if searching else GROUP_ORDER
        shown_idx = set()
        if searching:
            n_hits = len(self._search_rows())
            self.list_status.setText(self.t("voice_search_found", "{count} sentences with “{q}”").format(
                count=n_hits, q=self._query))
        for g in groups:
            if g == "search":
                rows = self._search_rows()
                n = len(rows)
            else:
                rows = self._shown(g)
                if searching:
                    rows = [r for r in rows if self._matches_query(r)]
                    if not rows:
                        continue          # searching: groups without a match stay out of the way
                    for r in rows:
                        if r.get("kind") == "passage":
                            shown_idx.add(tuple(r.get("idx") or ()))
                n = len(rows) if searching else len(self._scope(self._groups.get(g)))
            open_ = g not in self._collapsed
            hdr = _RowButton(f"{'▾' if open_ else '▸'}  {self._group_title(g)}", pad=8, suffix=f"  ·  {n}")
            hdr.setObjectName("Group")
            hdr.setToolTip(self._group_title(g))
            hdr.setCursor(Qt.PointingHandCursor)
            hdr.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            hdr.clicked.connect(lambda _=False, g=g: self._toggle_group(g))
            self.list_lay.insertWidget(self.list_lay.count() - 1, hdr)
            if not open_:
                continue
            if not rows:
                if g == "endings" and not self._endings_measured:
                    empty = self.t("voice_empty_endings", "Measured from the recording once it is on disk.")
                elif g == "tagged":
                    empty = self.t("voice_empty_tagged", "Mark words To voice patch in Clean and they show here.")
                elif not searching and len(self._shown_unscoped(g)) > 0:
                    empty = self.t("voice_empty_story", "Nothing in the story ({count} in the whole interview).").format(
                        count=len(self._shown_unscoped(g)))
                else:
                    empty = self.t("voice_empty", "Nothing found.")
                lb = _lbl(empty, 9, "#8a8a8a")
                lb.setContentsMargins(config.S(4), 0, 0, config.S(4))
                self.list_lay.insertWidget(self.list_lay.count() - 1, lb)
                continue
            for r in rows:
                k = row_key(r)
                # the state first: the reason may be elided in a narrow list, "patch used" must not
                done = os.path.isfile(os.path.join(self._out_dir(), f"ending_{k}", "patched.wav"))
                mark = ("  ·  " + self.t("voice_patch_used", "patch used")) if k in accepted else ("  ·  ✓" if done else "")
                if r.get("kind") == "passage":
                    second, mode = f"“{_short(r['text'], 160)}”", Qt.ElideRight
                else:
                    second, mode = r["sentence"][-160:], Qt.ElideLeft   # an ending: its last words matter
                when, when_tt = self._when(r)
                b = _RowButton(f"{when}{mark}  ·  {self._reason_text(r)}\n{second}", pad=30,
                               modes=(Qt.ElideRight, mode))
                b.setToolTip(when_tt)
                b.setObjectName("Ending")
                b.setCheckable(True)
                b.setProperty("key", f"{g}:{k}")
                b.setChecked(self._sel is not None and self._sel.get("group", "endings") == g and row_key(self._sel) == k)
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                b.clicked.connect(lambda _=False, r=r: self._select(r))
                self.list_lay.insertWidget(self.list_lay.count() - 1, b)
                first = first or r
        if picked:
            self._select(self._sel)
        elif self._sel is None and first is not None:
            self._select(first)

    def _select(self, row):
        self._sel = row
        want = f"{row.get('group', 'endings')}:{row_key(row)}"
        for i in range(self.list_lay.count() - 1):
            w = self.list_lay.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.isCheckable():
                w.setChecked(w.property("key") == want)
        self._show_detail(row)

    # ── one candidate ───────────────────────────────────────────────────────
    def _ending_dir(self, row):
        return os.path.join(self._out_dir(), f"ending_{row_key(row)}")

    def _report(self, row):
        try:
            with open(os.path.join(self._ending_dir(row), "report.json"), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _passage_html(self, row):
        """The passage highlighted, with a few words of what is said around it."""
        words = self._words()
        idx = row.get("idx") or []
        if not idx or idx[-1] >= len(words):
            return f"<span style='background-color:#2a2140; color:#e9d5ff;'>&nbsp;{_h.escape(row['text'])}&nbsp;</span>"

        def spoken(i):
            w = words[i]
            return w.get("type") == "word" and not w.get("is_inaudible")
        before = [i for i in range(idx[0] - 1, -1, -1) if spoken(i)][:8][::-1]
        after = [i for i in range(idx[-1] + 1, len(words)) if spoken(i)][:8]
        txt = lambda ids: " ".join(str(words[i].get("text", "")).strip() for i in ids)
        pre = ("…" if before and before[0] > 0 else "") + txt(before)
        post = txt(after) + ("…" if after else "")
        return (f"<span style='color:#a3a3a3;'>{_h.escape(pre)}</span> "
                f"<span style='background-color:#2a2140; color:#e9d5ff;'>&nbsp;{_h.escape(row['text'])}&nbsp;</span> "
                f"<span style='color:#a3a3a3;'>{_h.escape(post)}</span>")

    def _preview_span(self, row):
        """Seconds of the recording A plays before any patch: the passage (an ending: its sentence) with
        PREVIEW_PAD_S around it."""
        if row.get("kind") == "passage":
            t0, t1 = row["start"], row["end"]
        else:
            t0 = row.get("start_t")
            if t0 is None and self._an is not None:
                ws = self._an.tl["words"]
                t0 = ws[row["start_idx"]]["start"] if 0 <= row.get("start_idx", -1) < len(ws) else row["time"] - 3.0
            t0, t1 = float(t0 if t0 is not None else row["time"] - 3.0), float(row["time"])
        return max(0.0, t0 - PREVIEW_PAD_S), t1 + PREVIEW_PAD_S

    def _preview_path(self, row):
        """A WAV of the passage as recorded, cut from the timeline's audio (temp folder), or None."""
        an = self._an
        if an is None or getattr(an, "x", None) is None:
            return None
        from voice_patch import audio as A
        t0, t1 = self._preview_span(row)
        seg = an.x[int(t0 * A.SR): int(t1 * A.SR)]
        if len(seg) < int(0.1 * A.SR):
            return None
        folder = os.path.join(tempfile.gettempdir(), "StoryCraft voice preview")
        try:
            os.makedirs(folder, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", f"{row_key(row)}_{t0:.2f}")
            return A.save_quick(os.path.join(folder, safe + ".wav"), seg, A.SR)
        except Exception as e:
            from osdoc import log_error
            log_error(f"voice preview: {e}")
            return None

    def _show_detail(self, row):
        self.stop()
        self.d_progress.setText("")
        if row is None:
            self.d_sentence.setText(self.t("voice_pick_any", "Pick something on the left to hear it and patch it."))
            self.d_facts.setText("")
            self.words_box.hide()
            self.btn_rewrite.hide()
            for p in (self.pa, self._pb_empty):
                p["path"] = None
                p["sub"].setText("")
            self.d_result.setText("")
            self._fill_takes(None, None)
            self._update_buttons()
            return
        passage = row.get("kind") == "passage"
        rep = self._report(row)
        d = self._ending_dir(row)
        if passage:
            self.d_sentence.setText(self._passage_html(row))
            when, when_tt = self._when(row, row["start"])
            self.d_facts.setToolTip(when_tt)
            self.d_facts.setText(self.t("voice_p_facts", "At {time} · {dur} s · {why}").format(
                time=when, dur=f"{row['end'] - row['start']:.1f}", why=self._reason_text(row)))
            self._loading_words = True
            self.words_edit.setPlainText((rep or {}).get("new_text") or row.get("clean") or row.get("text") or "")
            self._loading_words = False
            self._words_changed()
            self.words_box.show()
            self.btn_rewrite.hide()
            self.btn_make.setToolTip(self.t("voice_make_p_tt", "VoiceStudio says these words in the speaker's voice "
                                            "(best of 3 takes); StoryCraft keeps the take that says them most clearly."))
        else:
            self.words_box.hide()
            self.btn_rewrite.show()
            sent, tail = row["sentence"], row["patch_words"]
            head = sent[: max(0, len(sent) - len(tail))] if sent.endswith(tail) else sent + " "
            self.d_sentence.setText(f"{_h.escape(head)}<span style='background-color:#2a2140; color:#e9d5ff;'>"
                                    f"&nbsp;{_h.escape(tail)}&nbsp;</span>")
            shape = {"rising": self.t("voice_rising", "rising"), "flat": self.t("voice_level", "level"),
                     "falling": self.t("voice_falling", "falling")}.get(row["shape"], row["shape"])
            when, when_tt = self._when(row, row["time"])
            self.d_facts.setToolTip(when_tt)
            self.d_facts.setText(self.t("voice_facts", "At {time} · pitch over the last words: {shape} ({delta:+.1f} semitones), "
                                        "ending {final:+.1f} from the speaker's usual pitch").format(
                time=when, shape=shape, delta=row["delta_st"], final=row["final_st"]))
            self.btn_make.setToolTip(self.t("voice_make_tt", "VoiceStudio speaks the sentence in the speaker's voice with a full stop; "
                                            "StoryCraft keeps the take that ends most like a finished sentence."))
        # A: as recorded, right away (the patch's own copy once it exists); B: every take, after the patch
        orig = os.path.join(d, "original.wav")
        self.pa["path"] = orig if os.path.isfile(orig) else self._preview_path(row)
        self.pa["sub"].setText(self.t("voice_as_recorded", "As recorded") if self.pa["path"] else "")
        self._pb_empty["path"] = None
        self._pb_empty["sub"].setText(self.t("voice_after_the_patch", "After the patch"))
        self._show_result(row, rep)
        self._fill_takes(rep, d)
        self._update_buttons()

    def _show_result(self, row, rep):
        """The line under the takes: what the selected take does (its length, where it goes)."""
        if rep and rep.get("result") == "patched":
            af, fit = rep.get("after") or {}, rep.get("fit") or {}
            a, b = rep.get("original_span") or [0, 0]
            if row.get("kind") == "passage":
                from voice_patch.edit import patch_length_s
                new = patch_length_s(rep)
                text = self.t("voice_p_result", "{new} s in place of {old} s ({diff} s) · place the patch "
                              "at {at} on a track above the interview").format(
                    new=f"{new:.1f}", old=f"{b - a:.1f}", diff=f"{new - (b - a):+.1f}", at=_fmt_t(a))
            else:
                text = self.t("voice_result", "Pitch at the end: {before:+.1f} → {after:+.1f} semitones · length "
                              "{ms:+d} ms{stretched} · place the patch at {at} on a track above the interview").format(
                    before=row["delta_st"], after=af.get("delta_st", 0.0), ms=fit.get("length_change_ms", 0),
                    stretched=self.t("voice_stretched", ", stretched to fit") if fit.get("stretched") else "",
                    at=_fmt_t(a))
            if sum(1 for t in rep.get("takes") or [] if t.get("ready")) > 1:   # say which take the line is about
                text = self.t("voice_take", "Take {n}").format(n=rep.get("chosen_take", 1)) + " · " + text
            dlv = rep.get("delivery") or {}
            how = self._delivery_text(dlv, report=True) if dlv else ""
            if how:                    # the Delivery it was made with, when not the defaults
                text += " · " + how
            if dlv.get("reference_fallback") and dlv.get("melody_from") != "recording":
                text += "\n" + ({"rise": self.t("voice_dl_no_rise_ref", "No rising sentence nearby to copy the melody "
                                                "from: the nearest sentence was used, so the rise may be weak."),
                                 "fall": self.t("voice_dl_no_fall_ref", "No falling sentence nearby to copy the melody "
                                                "from: the nearest sentence was used.")}.get(dlv.get("reference_wanted"), ""))
            self.d_result.setText(text.rstrip())
        elif rep:
            self.d_result.setText(self.t("voice_failed", "No take said the words clearly. Try again, or use your recording."))
        else:
            self.d_result.setText("")

    def _making_this(self, row):
        m = self._making
        return bool(m) and row is not None and m["key"] == row_key(row) and self._job is not None and self._job.isRunning()

    def _take_specs(self, rep, d):
        """[{n, ready, chosen, best, path, sub, tip}] for the takes on the right: every take of the patch (the
        ones that didn't say the words greyed out), the takes being made, or [] (no patch yet)."""
        m = self._making if self._making_this(self._sel) else None
        if m:
            out = []
            for n in range(1, m["total"] + 1):
                if m["fitting"]:
                    sub = self.t("voice_take_checking", "Checking…")
                elif n < m["k"]:
                    sub = self.t("voice_take_made", "Made")
                elif n == m["k"]:
                    sub = self.t("voice_take_making", "Being made…")
                else:
                    sub = self.t("voice_take_waiting", "Waiting")
                out.append({"n": n, "ready": False, "sub": sub})
            return out
        if not rep or rep.get("result") != "patched" or not d:
            return []
        chosen = rep.get("chosen_take") or 1
        span = rep.get("original_span") or [0, 0]
        old = float(span[1]) - float(span[0]) if len(span) == 2 else 0.0
        takes = [t for t in rep.get("takes") or [] if t.get("take") is not None]
        if not any(t.get("ready") for t in takes):     # a patch from before every take was fitted: its own take
            from voice_patch.edit import patch_length_s
            takes = [{"take": chosen, "ready": True, "patch_s": patch_length_s(rep)}]
        many = len(takes) > 1
        out = []
        for t in takes:
            n, ready = t["take"], bool(t.get("ready"))
            path = None
            if ready:
                own, cur = os.path.join(d, f"take_{n}_patched.wav"), os.path.join(d, "patched.wav")
                path = own if os.path.isfile(own) else (cur if n == chosen and os.path.isfile(cur) else None)
            ps = t.get("patch_s")
            if ready and ps is not None:
                sub = f"{float(ps):.1f} s, {float(ps) - old:+.1f} s"
            elif ready:
                sub = ""
            else:
                sub = self.t("voice_take_failed", "This take didn't say the words clearly")
            tip = (self.t("voice_take_tt", "Hear this take as B and use it as the patch") if ready
                   else sub + (f": {t.get('error')}" if t.get("error") else ""))
            best = many and n == rep.get("best_take")
            out.append({"n": n, "ready": ready, "chosen": ready and n == chosen, "best": best, "path": path, "sub": sub,
                        "tip": tip})
        return out

    def _fill_takes(self, rep, d):
        """B on the right: Take 1 · Take 2 · Take 3, one row each; the selected one is the patch."""
        while self.takes_lay.count():
            it = self.takes_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        if any(getattr(self, "_playing", None) is p for p in self._take_rows):
            self.stop()                 # its row is gone
            self._playing = None
        self._take_rows = []
        specs = self._take_specs(rep, d)
        self._pb_empty["card"].setVisible(not specs)
        self.takes_title.setVisible(bool(specs))
        self.takes_hint.setVisible(sum(1 for s in specs if s.get("ready")) > 1)
        self._sync_a_spacer()
        self.pb = self._pb_empty
        for s in specs:
            p = self._take_row(s)
            self.takes_lay.addWidget(p["card"])
            self._take_rows.append(p)
            if s.get("chosen"):
                self.pb = p
        self._sync_play_buttons()

    def _on_make_progress(self, text):
        """VoiceStudio's steps ("take 2 of 3", "fitting the takes"): the take rows show where it is."""
        m = self._making
        if not m:
            return
        hit = re.search(r"take (\d+) of (\d+)", text or "")
        if hit:
            m["k"], m["total"] = int(hit.group(1)), int(hit.group(2))
        elif "fitting the takes" in (text or ""):
            m["fitting"] = True
        if self._making_this(self._sel):
            self._fill_takes(None, None)

    def _choose_take(self, n):
        """Take n becomes the patch: its row turns violet, the line below and the buttons follow it."""
        row = self._sel
        if row is None or self._making_this(row):
            return
        d = self._ending_dir(row)
        rep = self._report(row) or {}
        if rep.get("chosen_take") != n:
            from voice_patch import core
            try:
                rep = core.choose_take(d, n)
            except Exception as exc:
                self.d_progress.setText(str(exc))
                n = rep.get("chosen_take")
            else:
                if row_key(row) in self._accepted():   # in the story: its words follow the new take's rhythm
                    self._sync_transcript(self._accepted_map())
        for p in self._take_rows:
            p["card"].setChecked(p["take"] == n)
            if p["take"] == n:
                self.pb = p
        self._show_result(row, rep)
        self._update_buttons()

    def _reset_words(self):
        if self._sel is not None and self._sel.get("kind") == "passage":
            self.words_edit.setPlainText(self._sel.get("clean") or self._sel.get("text") or "")

    def _words_changed(self):
        if getattr(self, "_loading_words", False) or self._sel is None or self._sel.get("kind") != "passage":
            return
        from voice_patch.core import _word_list
        txt = self.words_edit.toPlainText()
        changed = _word_list(txt) != _word_list(self._sel.get("clean") or self._sel.get("text"))
        self.words_note.setText(self.t("voice_rewrite_note", "Rewritten: the patch says your words.") if changed else "")
        self.btn_words_reset.setEnabled(changed)

    # ── in the edit ─────────────────────────────────────────────────────────
    def _accepted(self):
        """Ids of the patches in the edit."""
        return set(self._accepted_map())

    def _accepted_map(self):
        """{id: {"length": "keep"|"close"}} of the patches in the edit."""
        from voice_patch.edit import load_accepted_map
        return load_accepted_map(self._out_dir())

    def _set_accepted(self, m):
        """m: {id: {"length": ...}} (or a set of ids, all keeping their length)."""
        from voice_patch.edit import save_accepted
        save_accepted(self._out_dir(), copy.deepcopy(m))
        self._sync_transcript(m)
        self._syncing_use = True
        self.btn_use.setChecked(self._sel is not None and row_key(self._sel) in m)
        self._syncing_use = False
        self._fill_list()

    def _sync_transcript(self, accepted):
        """The transcript shows the patches in the edit: their words flagged AI voice, a rewrite's new words in
        place (the old ones come back when the patch leaves the edit)."""
        from voice_patch import transcript as VT
        canvas = getattr(self.win, 'text_canvas', None)
        if canvas is None or not canvas.words_data:
            return
        self._read_patch_words(accepted)
        try:   # the To voice patch mark stays: the passage keeps its place in Tagged by you
            new, changed = VT.sync(canvas.words_data, self._out_dir(), set(accepted), None)
        except Exception as exc:
            print(f"[voice] transcript sync failed: {exc}")
            return
        if not changed:
            return
        canvas.load_data(new)
        sv = getattr(self.win, 'story_view', None)
        if sv is not None:
            try:
                sv.refresh(force=True)   # the story cards show AI voice too
            except Exception as exc:
                print(f"[voice] story view refresh: {exc}")
        for fn in ('_refresh_cut_preview', '_refresh_detection_counts'):
            if hasattr(self.win, fn):
                getattr(self.win, fn)()
        board = getattr(self.win, 'story_board', None)
        if board is not None and getattr(board, 'sentences', None) and VT.refresh_board_text(board.sentences, new):
            board._sync_story()     # saves the board; the story cards show the new words

    # ── the patches' own words (the transcript follows the patch's rhythm) ────
    def _asr_model(self):
        """Whisper finds the patch's words: the best faster-whisper model already installed, or None."""
        from voice_patch import core
        try:
            core.MODELS_DIR = core.MODELS_DIR or os.path.join(self.win.engine.os_doc.install_dir, "models")
        except Exception:
            pass
        models = core.MODELS_DIR or ""
        return next((m for m in ("large-v3", "large-v3-turbo", "medium", "small", "base", "tiny")
                     if os.path.isdir(os.path.join(models, f"models--Systran--faster-whisper-{m}"))), None)

    def _read_patch_words(self, accepted):
        """Patches in the story made before their words were read (or switched to a take without them): what
        their playing file says is read in the background (ASR, a few seconds of audio each); then the
        transcript is synced again so the patched words light in step with the patch. No undo step."""
        if self._words_reader is not None:
            return                                  # reading: the poll looks again when it's done
        from voice_patch import transcript as VT
        out = self._out_dir()
        try:
            todo = [str(k) for k in sorted(accepted) if str(k) not in self._words_failed and VT.needs_words(out, k)]
        except Exception as exc:
            print(f"[voice] patch words check: {exc}")
            return
        if not todo:
            return
        model = self._asr_model()
        if model is None:
            return                                  # no Whisper yet: the words keep the recording's times
        words = self._words() or [{}]
        lang = words[0].get("meta_language") or None
        audio = words[0].get("meta_audio_path") or None    # the recording around the span: Whisper's context
        state = {"todo": todo, "out": out, "results": None}

        def work():
            from voice_patch import core
            res = []
            for k in todo:
                d = os.path.join(out, f"ending_{k}")
                try:
                    res.append((k, d, core.read_patch_words(d, lang, model, audio_path=audio)))
                except Exception as exc:
                    res.append((k, d, exc))
            state["results"] = res

        import threading
        state["thread"] = threading.Thread(target=work, name="patch-words", daemon=True)
        self._words_reader = state
        if not self.d_progress.text():
            self._words_note = self.t("voice_words_reading", "Reading the patch's words so the transcript "
                                      "follows its rhythm…")
            self.d_progress.setText(self._words_note)
        state["thread"].start()
        self._words_timer.start()

    def _words_poll(self):
        st = self._words_reader
        if st is None:
            self._words_timer.stop()
            return
        if st["thread"].is_alive() or st["results"] is None:
            return
        self._words_timer.stop()
        self._words_reader = None
        from voice_patch import core
        for k, d, r in st["results"]:
            if isinstance(r, Exception) or r is None or not r[2]:
                print(f"[voice] patch {k}: words not read: {r if r is not None else 'no patch file'}")
                self._words_failed.add(k)
                continue
            try:
                core.store_patch_words(d, *r)
            except Exception as exc:
                print(f"[voice] patch {k}: words not saved: {exc}")
                self._words_failed.add(k)
        if getattr(self, "_words_note", None) and self.d_progress.text() == self._words_note:
            self.d_progress.setText("")
        self._words_note = None
        if st["out"] == self._out_dir():
            self._sync_transcript(self._accepted_map())   # the new times (and any patch still waiting)

    def _rewrite_sentence(self):
        """An ending: rewrite its whole sentence (it becomes a Tagged passage with Edit the words)."""
        from voice_patch import candidates as VC
        row = self._sel
        words = self._words() or []
        if row is None or row.get("kind") == "passage" or not words:
            return
        try:
            idx = set(VC.candidate_indices(words, row))
        except Exception:
            idx = set()
        sent = next((s for s in VC.sentences(words) if idx & set(s)), None)
        if sent and hasattr(self.win, '_selection_voice_patch'):
            self.win._selection_voice_patch(sent)

    def _length_diff(self, row):
        """Seconds the fitted patch is longer (+) or shorter (-) than what it replaces, or 0."""
        rep = self._report(row) or {}
        span = rep.get("original_span") or []
        if rep.get("result") != "patched" or len(span) != 2:
            return 0.0
        from voice_patch.edit import patch_length_s
        return patch_length_s(rep) - (float(span[1]) - float(span[0]))

    def _ask_length(self, diff):
        """Cut the extra time / Make room ("close"), Keep the length ("keep"), or None (cancelled)."""
        from gui.dialogs.msgbox import CustomMsgBox
        d = f"{abs(diff):.1f}"
        if diff < 0:
            title = self.t("voice_len_title_short", "The patch is shorter")
            msg = self.t("voice_len_short", "The new words take {d} s less than the words they replace.\n\n"
                         "Cut the extra time: the story closes up after the patch.\n"
                         "Keep the length: the original picture runs on under room tone.").format(d=d)
            yes = self.t("voice_len_cut", "Cut the extra time")
        else:
            title = self.t("voice_len_title_long", "The patch is longer")
            msg = self.t("voice_len_long", "The new words take {d} s more than the words they replace.\n\n"
                         "Make room: the picture holds under the patch (its last frames repeat; cover them with "
                         "B-roll) and the rest of the story moves later.\n"
                         "Keep the length: the patch plays over the start of what follows.").format(d=d)
            yes = self.t("voice_len_room", "Make room")
        box = CustomMsgBox(self, title, msg, yes, self.t("voice_len_keep", "Keep the length"),
                           self.win.txt("btn_cancel", "Cancel"))
        r = box.exec()
        return {1: "close", 0: "keep"}.get(r)

    def _toggle_use(self, on):
        if getattr(self, "_syncing_use", False) or self._sel is None:
            return
        from voice_patch.edit import LENGTH_ASK_S
        before = self._accepted_map()
        m = copy.deepcopy(before)
        k = row_key(self._sel)
        length = "keep"
        if on:
            diff = self._length_diff(self._sel)
            if abs(diff) > LENGTH_ASK_S:
                length = self._ask_length(diff)
                if length is None:           # cancelled: not in the edit
                    self._syncing_use = True
                    self.btn_use.setChecked(False)
                    self._syncing_use = False
                    return
            m[k] = {"length": length}
        else:
            m.pop(k, None)
        self._set_accepted(m)
        um = getattr(self.win, "undo_manager", None)
        if um is not None:  # Cmd+Z takes the patch back out of the edit (or puts it back)
            um.push_state(self._accepted_map, self._set_accepted, before)
        if not on:
            self.d_progress.setText(self.t("voice_use_off", "Taken out of the story."))
        elif length == "close":
            self.d_progress.setText(self.t("voice_use_on_close", "In the story: Send to Resolve places it, and the story "
                                           "follows the patch's length.") )
        else:
            self.d_progress.setText(self.t("voice_use_on", "In the story: Send to Resolve places it."))
        if on and self._patch_in_story(k):
            self.win.show_review_patch(k)   # straight to its two joins on Review cuts, "Patch in" selected

    def _patch_in_story(self, k):
        """Is patch k (just inserted) fitted and played by the story? Not with an empty story."""
        if not hasattr(self.win, "show_review_patch"):
            return False
        try:
            import story_time as st
            from voice_patch.edit import accepted_patches, in_story
            spans = st.spans(self.win)
            if not spans:
                return False
            return any(str(p["end_id"]) == str(k) and in_story(p, spans) for p in accepted_patches(self._out_dir()))
        except Exception as exc:
            print(f"[voice] patch check: {exc}")
            return False

    def _update_buttons(self):
        busy = self._job is not None and self._job.isRunning()
        ok = bool(self._conn.get("ok"))
        has = self._sel is not None and self._an is not None
        self.btn_make.setEnabled(ok and has and not busy)
        self.btn_rec.setEnabled(ok and has and not busy)
        for p in self._players():
            p["btn"].setEnabled(bool(p["path"]))
        patched = self.pb is not self._pb_empty   # a take is selected (none while takes are being made)
        self.btn_use.setEnabled(patched)
        self._syncing_use = True
        self.btn_use.setChecked(patched and self._sel is not None and row_key(self._sel) in self._accepted())
        self._syncing_use = False
        self.btn_save.setEnabled(patched)
        self.btn_reveal.setEnabled(patched)

    # ── making a patch ──────────────────────────────────────────────────────
    def _make(self, mode):
        row, an = self._sel, self._an
        if row is None or an is None:
            return
        text = None
        if row.get("kind") == "passage":
            from voice_patch.core import _word_list
            text = " ".join(self.words_edit.toPlainText().split())
            if not _word_list(text):
                self.d_progress.setText(self.t("voice_no_words", "Type the words the patch should say."))
                return
        take = None
        if mode == "convert":
            take, _ = QFileDialog.getOpenFileName(self, self.t("voice_use_recording", "Use my recording…"), os.path.expanduser("~"),
                                                  "Audio (*.wav *.m4a *.mp3 *.aif *.aiff *.caf *.flac *.mp4 *.mov)")
            if not take:
                return
        from voice_patch import core
        from voice_patch.client import VoiceStudioClient
        out = self._out_dir()
        asr_model = self._asr_model()
        if asr_model is None:
            self.d_progress.setText(self.t("voice_no_whisper", "A Whisper model is needed to check the takes: transcribe "
                                           "any timeline once with Whisper (it downloads the model), then try again."))
            return
        self.stop()
        rows = self._rows
        delivery = dict(self._delivery)      # Delivery as chosen now (the defaults while it was never changed)

        def work(progress):
            os.makedirs(out, exist_ok=True)
            cache_path = os.path.join(out, "profiles.json")
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}
            rep = core.patch_one(an, rows, row, VoiceStudioClient(), out, mode=mode, take_path=take, takes=3,
                                 asr_model=asr_model, profile_cache=cache, text=text, delivery=delivery,
                                 progress=lambda s: progress(self.t("voice_working", "VoiceStudio is working: {step}…").format(step=s)))
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            return rep
        self.d_progress.setText(self.t("voice_starting", "Sending to VoiceStudio…"))
        # the take rows say which take is being made, from VoiceStudio's steps (a recording: 3 takes as well,
        # the one closest to it is picked)
        self._making = {"key": row_key(row), "total": 3, "k": 1, "fitting": False}

        def _finished():
            self._making = None
            if self._sel is not None and row_key(self._sel) == row_key(row):   # failed or done: the takes as they are
                self._fill_takes(self._report(self._sel), self._ending_dir(self._sel))
                self._update_buttons()
        self._run(work, lambda _rep: (setattr(self, "_making", None), self.d_progress.setText(""),
                                      self._sync_transcript(self._accepted_map()), self._show_detail(row), self._fill_list()))
        self._job.progress.connect(self._on_make_progress)
        self._job.finished.connect(_finished)
        self._fill_takes(None, None)
        self._update_buttons()

    def _run(self, fn, on_done):
        job = _Job(fn, self)
        job.progress.connect(self.d_progress.setText)
        job.done.connect(on_done)
        job.failed.connect(lambda msg: self.d_progress.setText(msg))
        job.finished.connect(self._update_buttons)
        self._job = job
        job.start()
        self._update_buttons()

    def _take_file(self, row, n):
        """The selected take's file to save / show: its span-exact preview (take_N_preview.wav, seamless
        edges) when it has one, else its bare patch (take_N_patch.wav); without takes preview.wav / patch.wav."""
        d = self._ending_dir(row)
        names = ([f"take_{n}_preview.wav", f"take_{n}_patch.wav"] if n else []) + ["preview.wav", "patch.wav"]
        return next((os.path.join(d, x) for x in names if os.path.isfile(os.path.join(d, x))), None)

    def _save_patch(self):
        row = self._sel
        if row is None:
            return
        if not os.path.isfile(os.path.join(self._ending_dir(row), "patch.wav")):
            return
        src = self._take_file(row, self.pb.get("take"))
        if not src:
            return
        n = self.pb.get("take") if len(self._take_rows) > 1 else None    # the selected take (patch.wav is its copy)
        name = None
        try:   # the same name Send gives it in Resolve: its sentence id(s), its words, its take
            from voice_patch.edit import media_name
            rep = self._report(row) or {}
            span = rep.get("original_span") or []
            if len(span) == 2:
                info = {"a": span[0], "b": span[1], "end_id": row_key(row), "take": n or rep.get("chosen_take"),
                        "new_text": rep.get("new_text") or rep.get("replaced_words", "")}
                name = media_name(dict(info, label=self.win._patch_label(info))) + ".wav"
        except Exception:
            name = None
        if not name:
            name = (f"{self._timeline()['name']} - patch at {_fmt_t(row['time']).replace(':', '.')}"
                    f"{f' - take {n}' if n else ''}.wav")
        dst, _ = QFileDialog.getSaveFileName(self, self.t("voice_save", "Save patch…"), os.path.join(os.path.expanduser("~/Desktop"), name),
                                             "WAV (*.wav)")
        if dst:
            shutil.copyfile(src, dst)

    def _reveal(self):
        if self._sel is None:
            return
        d = self._ending_dir(self._sel)
        f = self._take_file(self._sel, self.pb.get("take"))   # the selected take's file, selected in the folder
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", f] if f else ["open", d])
        elif f and sys.platform.startswith("win"):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(f)}"')
        elif sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", d])

    def showEvent(self, e):
        super().showEvent(e)
        if not e.spontaneous() and self._collapsed != set(GROUP_ORDER):
            # every visit starts with What could be patched folded: the user opens the group they want
            self._collapsed = set(GROUP_ORDER)
            self._fill_list()

    def hideEvent(self, e):
        self.stop()
        super().hideEvent(e)
