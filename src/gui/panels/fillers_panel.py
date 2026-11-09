#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: fillers_panel.py
ROLE: GUI Activity Panel
DESCRIPTION:
Sidebar activity panel for inline filler words editing, word count, and automatic marking.
"""

from PySide6.QtCore import Qt, QObject, QEvent, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QFrame
)
import config
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton, ToggleSwitch, ReloadButton
from .script_panel import (wrap_activity_panel, panel_card, panel_header, panel_margins, input_css,
                           primary_button_css, hairline, text_px)


class _RefitOnResize(QObject):
    """The word list is as tall as its text (set by the main window when the text changes). Its
    height was measured before the panel had its width, which left one line showing: measure it
    again whenever the width changes, with the same rule."""

    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit
        self._w = None

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize and obj.width() != self._w:
            self._w = obj.width()
            QTimer.singleShot(0, self._fit)
        return False

    def _fit(self):
        te = self._edit
        h = int(te.document().size().height() + QFontMetrics(te.font()).lineSpacing() + 10)
        te.setFixedHeight(min(h, 250))


def build_fillers_panel(win) -> QFrame:
    """Build the Filler Words activity panel and bind widgets to main window."""
    p_fillers = QWidget()
    l_fillers = QVBoxLayout(p_fillers)
    panel_margins(l_fillers)
    panel_header(l_fillers, win.txt("tool_filler_words"))

    prefs = win.engine.load_preferences() or {}
    fillers = prefs.get('filler_words', config.DEFAULT_BAD_WORDS)

    card, l_card = panel_card(10)
    l_fillers.addWidget(card)

    win.txt_fillers = QTextEdit()
    win.txt_fillers.setAcceptRichText(False)
    win.txt_fillers.setStyleSheet(input_css("QTextEdit", 9.5) + f"""
        QTextEdit {{ padding: {config.S(5)}px; color: #e6e6e6; }}""")
    win.txt_fillers.setText(", ".join(fillers))
    l_card.addWidget(win.txt_fillers)

    filler_tools_layout = QHBoxLayout()
    filler_tools_layout.setContentsMargins(0, 0, 0, 0)
    filler_tools_layout.setSpacing(config.S(6))

    win.lbl_filler_count = QLabel(win.txt("lbl_words"))
    win.lbl_filler_count.setStyleSheet(f"color: #8a8a8a; font-size: {config.FS(9)}pt;")
    filler_tools_layout.addWidget(win.lbl_filler_count)
    filler_tools_layout.addStretch()

    win.btn_reset_fillers = ReloadButton(size=28)
    win.btn_reset_fillers.setToolTip(win.txt("tt_revert_to_default"))
    win.btn_reset_fillers.clicked.connect(win._on_reset_inline_fillers)
    filler_tools_layout.addWidget(win.btn_reset_fillers, 0, Qt.AlignVCenter)

    win.btn_save_fillers = QPushButton(win.txt("btn_save"))
    win.btn_save_fillers.setCursor(Qt.PointingHandCursor)
    win.btn_save_fillers.setStyleSheet(primary_button_css(9.5, 6, 16))
    win.btn_save_fillers.setMinimumHeight(max(config.S(32), text_px(9.5, True) + 2 * config.S(6)))
    win.btn_save_fillers.clicked.connect(win._on_save_inline_fillers)
    filler_tools_layout.addWidget(win.btn_save_fillers)
    l_card.addLayout(filler_tools_layout)

    win.txt_fillers.textChanged.connect(win._on_fillers_text_changed)
    win._on_fillers_text_changed()
    win.txt_fillers._refit = _RefitOnResize(win.txt_fillers)
    win.txt_fillers.installEventFilter(win.txt_fillers._refit)

    l_card.addWidget(hairline())
    row_auto_filler = QHBoxLayout()
    row_auto_filler.setSpacing(config.S(6))
    lbl_auto = QLabel(win.txt("lbl_mark_filler_words_automat"))
    lbl_auto.setWordWrap(True)   # long translations wrap instead of pushing the switch out of the panel
    lbl_auto.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9.5)}pt; background: transparent;")
    row_auto_filler.addWidget(lbl_auto, 1)
    info_auto_filler = win._create_info_icon("tt_mark_filler_words")
    row_auto_filler.addWidget(info_auto_filler, 0, Qt.AlignVCenter)
    row_auto_filler.addSpacing(config.S(4))
    win.tgl_auto_filler = ToggleSwitch()
    win.tgl_auto_filler.setChecked(True)
    row_auto_filler.addWidget(win.tgl_auto_filler, 0, Qt.AlignVCenter)
    l_card.addLayout(row_auto_filler)

    l_fillers.addStretch(1)

    return wrap_activity_panel(p_fillers, scroll=True)
