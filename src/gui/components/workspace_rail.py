#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: workspace_rail.py
ROLE: GUI Component
DESCRIPTION:
The narrow icon rail on the left edge of the window: Home, Story (the story block by block), the two
steps of an edit (Clean up, Compose), Review, Voice and Settings. It shows where you are (Home or set-up form, or the step open in the
editor) and takes you there in one click. Clean up and Compose need an open transcript.
Classic palette like the rest of the UI (gui/theme.py maps it); the active item uses the brand violet.
"""
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QPushButton, QVBoxLayout, QWidget, QLabel

import config
from gui.icons import icon_pixmap

# The story is the hub: Clean, Review cuts and Voice patches are detours that come back to it
TOOLS = ("cleanup", "review", "voice")
ITEMS = (("home", "home_back", None), ("story", "insp_story", "Story"), ("cleanup", "wf_clean", "Clean"),
         ("review", "board_review_cuts", None), ("voice", "voice_title", "Voice patch"))


class _RailButton(QPushButton):
    def __init__(self, key, tip, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("RailBtn")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(config.S(44), config.S(44))
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self._needs_pixmap_rebuild = False  # gui/theme.set_theme raises it on a theme switch
        self._build_icon()

    def _build_icon(self):
        from gui.theme import color  # painted icons: greys follow the theme by hand, the violet stays
        px = config.S(20)
        # the story (the hub) is violet; the tools around it (Clean, Review cuts, Voice patches) are cyan
        on = "#67e8f9" if self.key in TOOLS else "#c084fc"
        idle, hover, off = color("#a3a3a3"), color("#ffffff"), color("#555555")
        icon = QIcon()
        for mode, state, col in ((QIcon.Normal, QIcon.Off, idle), (QIcon.Normal, QIcon.On, on),
                                 (QIcon.Active, QIcon.Off, hover), (QIcon.Active, QIcon.On, on),
                                 (QIcon.Disabled, QIcon.Off, off), (QIcon.Disabled, QIcon.On, off)):
            icon.addPixmap(icon_pixmap(self.key, col, px), mode, state)
        self.setIcon(icon)
        self.setIconSize(QSize(px, px))

    def paintEvent(self, e):
        if self._needs_pixmap_rebuild:
            self._needs_pixmap_rebuild = False
            self._build_icon()
        super().paintEvent(e)


class WorkspaceRail(QFrame):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("WorkspaceRail")
        self.setFixedWidth(config.S(60))
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QFrame#WorkspaceRail {{ background-color: #1a1a1a; border: none; border-right: 1px solid #2a2a2a; }}
            QPushButton#RailBtn {{ background: transparent; border: none; border-radius: {config.S(10)}px; }}
            QPushButton#RailBtn:hover {{ background-color: #2a2a2a; }}
            QPushButton#RailBtn:checked {{ background-color: #2a2140; }}
            QPushButton#RailBtn[tool="true"]:checked {{ background-color: #10303a; }}
            QFrame#RailSep {{ background-color: #2e2e2e; border: none; }}
            QPushButton#RailBtn:disabled {{ background: transparent; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, config.S(12), 0, config.S(12))
        lay.setSpacing(config.S(6))
        lay.setAlignment(Qt.AlignHCenter)

        mark = QLabel(self)
        mark.setFixedSize(config.S(34), config.S(34))
        mark.setPixmap(self._mark_pixmap(config.S(34)))
        mark.setToolTip(config.APP_NAME)
        lay.addWidget(mark, 0, Qt.AlignHCenter)
        lay.addSpacing(config.S(12))

        self.buttons = {}
        for key, txt_key, default in ITEMS:
            if key == TOOLS[0]:      # a thin line: the story above, the tools around it below
                sep = QFrame(self)
                sep.setObjectName("RailSep")
                sep.setFixedSize(config.S(24), 1)
                lay.addSpacing(config.S(4))
                lay.addWidget(sep, 0, Qt.AlignHCenter)
                lay.addSpacing(config.S(4))
            b = _RailButton(key, win.txt(txt_key, default).split("  ", 1)[-1], self)
            b.setProperty("tool", "true" if key in TOOLS else "false")
            b.clicked.connect(lambda _=False, k=key: self._go(k))
            lay.addWidget(b, 0, Qt.AlignHCenter)
            self.buttons[key] = b
        lay.addStretch(1)
        s = _RailButton("settings", win.txt("tool_settings"), self)
        s.setCheckable(False)
        s.clicked.connect(lambda: win._on_settings())
        lay.addWidget(s, 0, Qt.AlignHCenter)
        self.btn_settings = s

    @staticmethod
    def _mark_pixmap(px):
        from gui import brand
        ratio = 2
        pm = QPixmap(px * ratio, px * ratio)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        try:
            brand.draw_mark(p, QRectF(0, 0, px * ratio, px * ratio))
        except Exception:
            pass
        p.end()
        pm.setDevicePixelRatio(ratio)
        return pm

    def _go(self, key):
        win = self.win
        if key == "home":
            win.show_home()
        elif key == "cleanup" and hasattr(win, 'show_clean'):
            win.show_clean()
        elif key in ("cleanup", "compose"):   # compose: the older story step (no rail button any more)
            if win._stack.currentIndex() != 2:
                win.go_to_page(2)
            win._on_workflow_step(key)
        elif key == "review":
            win.show_review()
        elif key == "voice":
            win.show_voice()
        elif key == "story":
            win.show_story()
        self.sync()

    def sync(self):
        """Light up where the user is; Clean up and Compose only with a transcript open."""
        win = self.win
        canvas = getattr(win, 'text_canvas', None)
        has_transcript = bool(canvas is not None and getattr(canvas, 'words_data', None))
        page = win._stack.currentIndex() if hasattr(win, '_stack') else 0
        if page == 0:
            active = "home"
        elif page == 2:
            active = "cleanup"
        elif page == 3:
            active = "review"
        elif page == 4:
            active = "voice"
        elif page == 5:
            active = "story"
        else:
            active = None
        for key, b in self.buttons.items():
            if key not in ("home", "voice"):  # Voice shows the VoiceStudio connection even before a timeline
                b.setEnabled(has_transcript)
            b.setChecked(key == active)
