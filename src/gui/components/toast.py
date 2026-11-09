#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/components/toast.py
ROLE: A short message over the bottom of the work area, whatever page is shown
DESCRIPTION:
Feedback that must reach the user wherever they are: what ⌘Z just undid (the undo history is shared by
every page), the timeline Send to Resolve made, … One line, fades after a few seconds, never blocks.
"""
from PySide6.QtCore import Qt, QTimer, QEvent, QObject
from PySide6.QtWidgets import QLabel

import config


class Toast(QLabel):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setObjectName("Toast")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"QLabel#Toast {{ background-color: #2a2140; color: #e9d5ff; border: 1px solid #6d28d9;"
                           f" border-radius: {config.S(14)}px; padding: {config.S(7)}px {config.S(16)}px;"
                           f" font-size: {config.FS(9.5)}pt; font-weight: 500; }}")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        host.installEventFilter(self)

    def show_text(self, text, ms=5000):
        self.setText(text)
        self.setMaximumWidth(max(config.S(240), int(self.host.width() * 0.6)))
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(ms)

    def _place(self):
        self.move(int((self.host.width() - self.width()) / 2), self.host.height() - self.height() - config.S(18))

    def eventFilter(self, obj, ev):
        if obj is self.host and ev.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False
