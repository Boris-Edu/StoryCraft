#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/widgets/step_track.py
ROLE: The story and the tools around it (top-left of every work page)
DESCRIPTION:
Not a sequence: the story is the hub (violet), Clean · Review cuts · Voice patch are tools around it
(cyan), none numbered. The same bar sits in the same place on the Story page, in Clean, in Review and
in Voice patches, with the page's own entry lit, so the user always sees where they are and that the
story is one click away. Icons are the rail's.
Right of the tabs, the story's status (gui/components/story_status.py): the same line on every page — how
long the story plays, its blocks, voice patches, cuts to check, whether Resolve has it, and an amber
"Recording (original)" while the player plays the raw recording instead of the story.
"""
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

import config
from gui.icons import icon_pixmap

VIOLET, CYAN = "#c084fc", "#67e8f9"

# (key, icon, text key, default, tooltip key, tooltip default)
HUB = ("story", "story", "hub_story", "Story", "tt_hub_story", "The story: arrange its blocks, add or leave out passages")
VERSIONS = ("versions", "continue", "hub_versions", "Versions", "tt_hub_versions",
            "The stories you kept: compare them, go back to one, reload an edit from Resolve")
STORY_SIDE = (HUB, VERSIONS)      # violet: the story and its history
TOOLS = (("cleanup", "cleanup", "hub_clean", "Clean", "tt_hub_clean", "Mark fillers, errors and retakes on the story's text"),
         ("review", "review", "hub_review", "Review cuts", "tt_hub_review", "Hear and fix every cut of the story"),
         ("voice", "voice", "hub_voice", "Voice patch", "tt_hub_voice", "Patch what sounds wrong with VoiceStudio"),
         ("ai", "ai", "hub_ai", "AI", "tt_hub_ai", "Every AI helper in one place: Claude connected, AI chats, the AI's marks"))


class HubNav(QFrame):
    def __init__(self, win, active="story", parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("HubNav")
        r = config.S(9)
        self.setStyleSheet(f"""
            QFrame#HubNav {{ background-color: #1e1e1e; border: 1px solid #303030; border-radius: {config.S(12)}px; }}
            QFrame#HubSep {{ background-color: #343434; border: none; }}
            QFrame#HubStatusSep {{ background: transparent; border: none; border-left: 1px solid #343434;
                margin-left: {config.S(4)}px; margin-right: {config.S(2)}px; }}
            QPushButton#HubBtn {{ background: transparent; color: #9a9a9a; border: 1px solid transparent; border-radius: {r}px;
                padding: {config.S(5)}px {config.S(10)}px {config.S(5)}px {config.S(8)}px; font-family: 'Outfit';
                font-size: {config.FS(10.5)}pt; font-weight: 600; text-align: left; }}
            QPushButton#HubBtn:hover {{ background: #2a2a2a; color: #ffffff; }}
            QPushButton#HubBtn[hub="true"][active="true"] {{ background: #2a2140; color: #ffffff; border-color: #6d28d9; }}
            QPushButton#HubBtn[hub="false"][active="true"] {{ background: #10303a; color: #ffffff; border-color: #0e7490; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(config.S(3), config.S(3), config.S(3), config.S(3))
        lay.setSpacing(config.S(2))
        self.buttons = {}
        self.page_decides = False          # True: the page alone turns compact on / off (see set_compact)
        self.on_essentials_changed = None  # that page's refit, called when the status's kept parts resize
        for n, (key, icon, tk, td, ttk, ttd) in enumerate(STORY_SIDE + TOOLS):
            if n == len(STORY_SIDE):
                sep = QFrame(self)
                sep.setObjectName("HubSep")
                sep.setFixedSize(1, config.S(20))
                lay.addSpacing(config.S(4))
                lay.addWidget(sep, 0, Qt.AlignVCenter)
                lay.addSpacing(config.S(4))
            b = QPushButton(win.txt(tk, td), self)
            b.setObjectName("HubBtn")
            b.setProperty("hub", "true" if key in ("story", "versions") else "false")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(win.txt(ttk, ttd))
            b.setMinimumHeight(config.S(32))
            b.setIconSize(QSize(config.S(16), config.S(16)))
            b.clicked.connect(lambda _=False, k=key: self._go(k))
            self.buttons[key] = b
            lay.addWidget(b)
        # The story's status, the same on every page (its parts drop as room runs out; see story_status.py)
        self.status_sep = QFrame(self)
        self.status_sep.setObjectName("HubStatusSep")   # its gaps are its own margins: nothing left when hidden
        self.status_sep.setFixedSize(config.S(4) + 1 + config.S(2), config.S(20))
        lay.addWidget(self.status_sep, 0, Qt.AlignVCenter)
        self.status_sep.hide()
        self.status = None
        try:
            from gui.components.story_status import StoryStatus
            self.status = StoryStatus(win, self, self)
            lay.addWidget(self.status, 0, Qt.AlignVCenter)
        except Exception as exc:          # the bar works without its status line
            print(f"[hub] story status unavailable: {exc}")
        self.set_active(active)

    def _go(self, key):
        panel = getattr(self.win, "_versions_panel", None)
        if key != "versions" and panel is not None and not panel.isHidden():
            panel.close_panel()          # Story (or a tool) leaves Versions
        fn = {"story": "show_story", "versions": "show_story_versions", "cleanup": "show_clean",
              "review": "show_review", "voice": "show_voice", "ai": "show_ai"}[key]
        if hasattr(self.win, fn):
            getattr(self.win, fn)()

    def set_compact(self, on):
        """Short of room: the tools that aren't lit show their icon only (their name is in the tooltip).
        Pages that measure their top row call this; on the others the status line asks (set_self_compact).
        A page that sets page_decides = True (Story) is the only one to decide: the status line never asks there."""
        self._page_compact = bool(on)
        self._update_compact()

    def set_self_compact(self, on):
        """The row can't hold the bar and the status line's length + Resolve state (pages that don't measure)."""
        self._self_compact = bool(on)
        self._update_compact()

    def labels_delta(self):
        """Pixels the bar gains when the tools show their names again (0 when they already do)."""
        if not getattr(self, '_compact', False):
            return 0
        return self.full_width() - (self.sizeHint().width() - self._status_extra())

    def _update_compact(self):
        on = getattr(self, '_page_compact', False) or getattr(self, '_self_compact', False)
        if getattr(self, '_compact', False) == on:
            return
        self._compact = on
        self._apply_labels()
        if self.status is not None:
            self.status.fit_soon()

    def _status_extra(self):
        """The status parts beyond length + Resolve: they only take room the row has left, never count here."""
        return self.status.extras_width() if self.status is not None else 0

    def _width_as(self, compact):
        """The bar's width with (compact=False) or without the tools' names; the status's kept parts counted."""
        was = getattr(self, '_compact', False)
        if was == compact:
            return self.sizeHint().width() - self._status_extra()
        self._compact = compact
        self._apply_labels()
        w = self.sizeHint().width() - self._status_extra()
        self._compact = was
        self._apply_labels()
        return w

    def full_width(self):
        return self._width_as(False)

    def compact_width(self):
        return self._width_as(True)

    def _status_changed(self):
        """The status line changed what it shows: its separator follows, and the bar's size."""
        on = self.status is not None and self.status.has_any()
        if self.status_sep.isHidden() == on:
            self.status_sep.setVisible(on)
            self.layout().invalidate()
            self.updateGeometry()
        self.setMinimumWidth(self.sizeHint().width())
        # A page that fits its own row (page_decides) hears when the parts that always stay change width
        # (e.g. "Not sent yet" -> "Resolve offline"): its "Target" word / the tab names may fit again, or not
        ew = (self.status.essential_width(), on) if self.status is not None else (0, on)
        if ew != getattr(self, '_last_essentials', None):
            self._last_essentials = ew
            cb = getattr(self, 'on_essentials_changed', None)
            if cb is not None:
                QTimer.singleShot(0, cb)

    def refresh_status(self):
        if self.status is not None:
            from gui.components.story_status import refresh_all
            refresh_all(self.win, soon=True)

    def _apply_labels(self):
        for k, b in self.buttons.items():
            name = getattr(b, '_label', None) or b.text()
            b._label = name
            show = not getattr(self, '_compact', False) or k in ("story", getattr(self, 'active', None))
            b.setText(name if show else "")
        self.setMinimumWidth(self.sizeHint().width())   # never squeezed: compact mode is how it gives room

    def set_active(self, key):
        self.active = key
        for k, b in self.buttons.items():
            on = k == key
            b.setProperty("active", "true" if on else "false")
            col = (VIOLET if k in ("story", "versions") else CYAN) if on else "#9a9a9a"
            icon = next(t[1] for t in STORY_SIDE + TOOLS if t[0] == k)
            b.setIcon(icon_pixmap(icon, col, config.S(16)))
            b.style().unpolish(b)
            b.style().polish(b)
        self._apply_labels()


# Kept for callers of the earlier "1 Story · 2 Clean" track
StepTrack = HubNav
