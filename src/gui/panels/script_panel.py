#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: script_panel.py
ROLE: GUI Activity Panel
DESCRIPTION:
Sidebar activity panel for pasting, importing, and comparing script text with audio transcription.
"""

from gui.theme import qc

from PySide6.QtCore import Qt, QVariantAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTextEdit, QPushButton, QFrame, QScrollArea, QLabel,
    QSizePolicy
)
from PySide6.QtGui import QColor, QFont, QFontMetrics

import config


# ── The inspector's look, shared by every panel in it ──
# Written in the classic greys (gui/theme.py moves them onto the active theme): panel surface
# #1a1a1a, cards #262626 with a #303030 hairline, quiet surfaces #2a2a2a, hover #333333.
# The brand gradient (cyan -> violet) is kept for the one main action of a panel.
BRAND_GRADIENT = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
BRAND_GRADIENT_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc)"


def text_px(pt, bold=False):
    """Line height in px of the UI font at FS(pt): boxes sized from it never clip their text."""
    try:
        from gui.theme import ui_font
        family = ui_font() or config.UI_FONT_NAME
    except Exception:
        family = config.UI_FONT_NAME
    f = QFont(family)
    f.setPointSize(config.FS(pt))
    f.setBold(bold)
    return QFontMetrics(f).height()


def panel_header(layout, title, subtitle=None):
    """Panel title (and a one-line explanation under it), the same in every side panel."""
    lbl = QLabel(title)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: #ffffff; font-size: {config.FS(12)}pt; font-weight: 600; background: transparent;")
    layout.addWidget(lbl)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: #a3a3a3; font-size: {config.FS(9)}pt; background: transparent;")
        layout.addWidget(sub)
    return lbl


def panel_margins(layout, spacing=12):
    """Cards line up with the inspector's tab bar above them (it already sits inside the panel's margins)."""
    layout.setContentsMargins(config.S(1), config.S(6), config.S(3), config.S(14))
    layout.setSpacing(config.S(spacing))


def heading_css(pt=12):
    return f"color: #ffffff; font-size: {config.FS(pt)}pt; font-weight: 600; background: transparent; border: none;"


def caption_css():
    """Small group title over a list or a few controls ("EDIT · 11 blocks", "PAUSE DETECTION")."""
    return (f"color: #8a8a8a; font-size: {config.FS(8)}pt; font-weight: 600; letter-spacing: 1px;"
            f" background: transparent; border: none;")


def sublabel_css(pt=9):
    return f"color: #a3a3a3; font-size: {config.FS(pt)}pt; background: transparent; border: none;"


def card_css(name="PanelCard", radius=12):
    return (f"QFrame#{name} {{ background: #262626; border: 1px solid #303030;"
            f" border-radius: {config.S(radius)}px; }}")


def panel_card(spacing=10):
    """A rounded card grouping related controls; returns (frame, its layout)."""
    card = QFrame()
    card.setObjectName("PanelCard")
    card.setStyleSheet(card_css())
    lay = QVBoxLayout(card)
    lay.setContentsMargins(config.S(14), config.S(13), config.S(14), config.S(14))
    lay.setSpacing(config.S(spacing))
    return card, lay


def hairline():
    """A 1 px divider between rows inside a card."""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet("background-color: #303030; border: none; margin: 0px;")
    return line


def quiet_button_css(pt=9.5, pad_y=6, pad_x=12):
    """The everyday button: a quiet surface that lights up on hover."""
    return f"""
        QPushButton {{ background: #2a2a2a; color: #d4d4d4; border: 1px solid #333333; border-radius: {config.S(9)}px;
            padding: {config.S(pad_y)}px {config.S(pad_x)}px; font-size: {config.FS(pt)}pt; font-weight: 500; }}
        QPushButton:hover {{ background: #333333; color: #ffffff; border-color: #3a3a3a; }}
        QPushButton:pressed {{ background: #2a2a2a; }}
        QPushButton:disabled {{ background: #222222; color: #6a6a6a; border-color: #2a2a2a; }}"""


def quiet_button(btn, pt=9.5, min_h=32, pad_y=6, pad_x=12):
    btn.setStyleSheet(quiet_button_css(pt, pad_y, pad_x))
    btn.setMinimumHeight(max(config.S(min_h), text_px(pt) + 2 * config.S(pad_y) + 2))
    return btn


def primary_button_css(pt=10, pad_y=7, pad_x=14):
    """The one main action of a panel: the brand gradient with ink text."""
    return f"""
        QPushButton {{ background: {BRAND_GRADIENT}; color: #0b0b14; border: none; border-radius: {config.S(9)}px;
            padding: {config.S(pad_y)}px {config.S(pad_x)}px; font-size: {config.FS(pt)}pt; font-weight: 600; }}
        QPushButton:hover {{ background: {BRAND_GRADIENT_HOVER}; }}
        QPushButton:disabled {{ background: #2a2a2a; color: #6a6a6a; }}"""


def link_button_css(pt=9):
    """A text-only action (no surface): secondary text that turns white on hover."""
    return f"""
        QPushButton {{ background: transparent; border: none; color: #a3a3a3; text-align: left;
            padding: {config.S(4)}px {config.S(2)}px; font-size: {config.FS(pt)}pt; font-weight: 500; }}
        QPushButton:hover {{ color: #ffffff; }}
        QPushButton:disabled {{ color: #6a6a6a; }}"""


def disclosure_css(pt=9.5, compact=False):
    """The ▸/▾ header of a section that folds open: a full-width quiet row."""
    pad_y = config.S(5) if compact else config.S(8)
    return f"""
        QPushButton {{ background: #262626; border: 1px solid #303030; border-radius: {config.S(10)}px; color: #d4d4d4;
            text-align: left; padding: {pad_y}px {config.S(12)}px; font-size: {config.FS(pt)}pt; font-weight: 600; }}
        QPushButton:hover {{ background: #2a2a2a; color: #ffffff; }}
        QPushButton:checked {{ background: #262626; }}"""


def input_css(cls="QLineEdit", pt=9.5):
    # No selection-background-color here: the theme reads an accent "background" in the block as the
    # field's own and would turn its text into the accent's text colour (white on a light theme)
    return f"""
        {cls} {{ background: #1e1e1e; color: #ffffff; border: 1px solid #333333; border-radius: {config.S(8)}px;
            padding: {config.S(3)}px {config.S(8)}px; font-family: "{config.UI_FONT_NAME}"; font-size: {config.FS(pt)}pt; }}
        {cls}:focus {{ border: 1px solid #23a559; }}"""


def combo_css(pt=9):
    return f"""
        QComboBox {{ background: #2a2a2a; color: #d4d4d4; border: 1px solid #333333; border-radius: {config.S(8)}px;
            padding: {config.S(3)}px {config.S(8)}px; font-size: {config.FS(pt)}pt; }}
        QComboBox:hover {{ background: #333333; color: #ffffff; }}
        QComboBox:disabled {{ color: #6a6a6a; background: #222222; }}
        QComboBox QAbstractItemView {{ background: #222222; color: #e6e6e6; selection-background-color: #23a559; }}"""


def style_segmented(buttons, pt=9, stretch=False):
    """Joined buttons for mutually exclusive choices (Keep | Mark | Cut): the chosen one filled with
    the accent. Sized from the text, so a larger text size never clips the labels."""
    buttons = list(buttons)
    h = max(config.S(28), text_px(pt, bold=True) + 2 * config.S(5) + 2)
    r = config.S(8)
    for i, b in enumerate(buttons):
        r_l = r if i == 0 else 0
        r_r = r if i == len(buttons) - 1 else 0
        b.setFixedHeight(h)
        if stretch:
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        b.setStyleSheet(f"""
            QPushButton {{ background: #1e1e1e; color: #a3a3a3; border: 1px solid #333333;
                {'border-left-width: 0px;' if i else ''}
                border-top-left-radius: {r_l}px; border-bottom-left-radius: {r_l}px;
                border-top-right-radius: {r_r}px; border-bottom-right-radius: {r_r}px;
                padding: 0 {config.S(10)}px; font-family: '{config.UI_FONT_NAME}'; font-size: {config.FS(pt)}pt;
                font-weight: 600; }}
            QPushButton:hover {{ color: #ffffff; background: #262626; }}
            QPushButton:checked {{ background: #23a559; color: #ffffff; border-color: #23a559; }}
            QPushButton:disabled {{ color: #6a6a6a; }}
            QPushButton:checked:disabled {{ background: #34483b; color: #a3a3a3; border-color: #34483b; }}
        """)
    return h


def chip_css(h, pt=8.5):
    """A pill-shaped toggle (radius = half its height, so Qt keeps the rounding)."""
    r = max(1, h // 2 - 1)
    return f"""
        QPushButton {{ background: #2a2a2a; color: #a3a3a3; border: 1px solid #333333; border-radius: {r}px;
            padding: 0 {config.S(10)}px; font-size: {config.FS(pt)}pt; font-weight: 500; }}
        QPushButton:hover {{ color: #ffffff; background: #333333; }}
        QPushButton:checked {{ color: #ffffff; border-color: #23a559; background: rgba(35, 165, 89, 46); }}"""


class ButtonFlow(QWidget):
    """Buttons side by side in as many equal columns as their labels fit (up to max_cols), else fewer,
    down to one per row: long translations stay whole in a narrow panel."""

    def __init__(self, buttons, max_cols=2, spacing=6, parent=None):
        super().__init__(parent)
        self._buttons = list(buttons)
        self._max = max(1, min(max_cols, len(self._buttons)))
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(config.S(spacing))
        for b in self._buttons:
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._cols = None
        self._place(self._max)

    def _fits(self, cols, width):
        need = max(b.sizeHint().width() for b in self._buttons)
        return cols * need + (cols - 1) * self._grid.spacing() <= width

    def _place(self, cols):
        if cols == self._cols:
            return
        self._cols = cols
        for b in self._buttons:
            self._grid.removeWidget(b)
        n = len(self._buttons)
        for i, b in enumerate(self._buttons):
            # The last button of a short row takes the rest of it
            span = cols - (i % cols) if i == n - 1 else 1
            self._grid.addWidget(b, i // cols, i % cols, 1, span)
        for c in range(self._max):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = event.size().width()
        self._place(next((c for c in range(self._max, 0, -1) if self._fits(c, width)), 1))


def wrap_activity_panel(widget: QWidget, scroll: bool = False) -> QFrame:
    """Wrap any panel widget into a styled ActivityPanel frame container.
    scroll: the content keeps its natural size and scrolls when the window is short
    (instead of being squeezed until labels overlap)."""
    container = QFrame()
    container.setObjectName("ActivityPanel")
    container.setAttribute(Qt.WA_StyledBackground, True)

    container.setStyleSheet(f"""
        QFrame#ActivityPanel {{
            background-color: #1a1a1a;
            border-radius: 0px;
            margin: 0px;
            border: none;
        }}
        /* Force all generic children to be transparent so the panel surface shows through */
        QFrame#ActivityPanel QWidget {{
            background-color: transparent;
        }}
        /* Restore specific background for input fields so they don't blend in */
        QFrame#ActivityPanel QTextEdit,
        QFrame#ActivityPanel QDoubleSpinBox,
        QFrame#ActivityPanel QLineEdit {{
            background-color: #1e1e1e;
            border: 1px solid #333333;
            border-radius: {config.S(8)}px;
            color: #ffffff;
            font-family: "{config.UI_FONT_NAME}";
            font-size: {config.FS(9.5)}pt;
        }}
        QFrame#ActivityPanel QPushButton {{
            background-color: #2a2a2a;
            border: 1px solid #333333;
            border-radius: {config.S(9)}px;
            padding: {config.S(6)}px {config.S(12)}px;
            color: #d4d4d4;
            font-family: "{config.UI_FONT_NAME}";
            font-size: {config.FS(9.5)}pt;
        }}
        QFrame#ActivityPanel QPushButton:hover {{ background-color: #333333; border-color: #3a3a3a; color: #ffffff; }}
        QFrame#ActivityPanel QPushButton:disabled {{ background-color: #222222; border-color: #2a2a2a; color: #6a6a6a; }}
    """)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    if scroll:
        area = QScrollArea()
        area.setObjectName("ActivityScroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setStyleSheet("QScrollArea#ActivityScroll { background: transparent; border: none; }")
        area.setWidget(widget)
        layout.addWidget(area)
    else:
        layout.addWidget(widget)
    return container


def build_script_panel(win) -> QFrame:
    """Build the Script Analysis activity panel and bind widgets to main window."""
    p_script_analysis = QWidget()
    l_script_analysis = QVBoxLayout(p_script_analysis)
    panel_margins(l_script_analysis)
    panel_header(l_script_analysis, win.txt("lbl_script_title"), win.txt("sub_script"))

    card_script, l_card_script = panel_card(10)
    win.text_script = QTextEdit()
    win.text_script.setAcceptRichText(False)
    win.text_script.setPlaceholderText(win.txt("ph_paste_script_here"))
    win.text_script.setMinimumHeight(config.TS(110))
    win.text_script.setStyleSheet(input_css("QTextEdit", 9.5) + f"""
        QTextEdit {{ padding: {config.S(6)}px; color: #e6e6e6; }}""")
    l_card_script.addWidget(win.text_script, 1)
    l_script_analysis.addWidget(card_script, 1)

    win.btn_import_script = QPushButton(win.txt("btn_import_script"))
    win.btn_import_script.setCursor(Qt.CursorShape.PointingHandCursor)
    win.btn_clear_script = QPushButton(win.txt("btn_clear"))
    win.btn_clear_script.setCursor(Qt.CursorShape.PointingHandCursor)
    for b in (win.btn_import_script, win.btn_clear_script):
        quiet_button(b, pad_x=8)
    l_card_script.addWidget(ButtonFlow([win.btn_import_script, win.btn_clear_script], max_cols=2))

    # What to do with it: compare (primary), side by side, or find errors without a script
    win.script_actions_card, l_actions = panel_card(8)
    l_script_analysis.addWidget(win.script_actions_card)

    win.btn_analyze_standalone = QPushButton(win.txt("btn_script_errors"))
    win.btn_analyze_standalone.setToolTip(win.txt("tt_script_errors"))
    win.btn_analyze_standalone.setCursor(Qt.CursorShape.PointingHandCursor)
    win.btn_analyze_standalone.setStyleSheet(link_button_css(9) + """
        QPushButton { text-decoration: underline; }""")

    win.btn_analyze_compare = QPushButton(win.txt("btn_script_compare"))
    win.btn_analyze_compare.setCursor(Qt.CursorShape.PointingHandCursor)
    win.btn_analyze_compare.setMinimumHeight(max(config.S(36), text_px(10, True) + 2 * config.S(8)))
    win.btn_analyze_compare.setStyleSheet(primary_button_css(10))
    l_actions.addWidget(win.btn_analyze_compare)

    win.btn_side_by_side_compare = QPushButton(win.txt("btn_script_sbs"))
    win.btn_side_by_side_compare.setCursor(Qt.CursorShape.PointingHandCursor)
    quiet_button(win.btn_side_by_side_compare)
    win.btn_side_by_side_compare.setEnabled(False)
    l_actions.addWidget(win.btn_side_by_side_compare)
    l_actions.addWidget(win.btn_analyze_standalone, 0, Qt.AlignHCenter)

    win.btn_exit_sbs_text = QPushButton(win.txt("btn_return_normal"))
    win.btn_exit_sbs_text.setCursor(Qt.CursorShape.PointingHandCursor)
    quiet_button(win.btn_exit_sbs_text)
    win.btn_exit_sbs_text.clicked.connect(win._exit_side_by_side)
    win.btn_exit_sbs_text.hide()
    l_script_analysis.addWidget(win.btn_exit_sbs_text)

    win._analyze_color_anim = QVariantAnimation(win)
    win._analyze_color_anim.setDuration(250)
    win._analyze_color_anim.setEasingCurve(QEasingCurve.InOutQuad)

    def update_btn_style(color):
        # While it fades in or out: a plain colour; once there, the brand gradient (or quiet when off)
        style = (f"QPushButton {{ background-color: {color.name()}; border: none; border-radius: {config.S(9)}px;"
                 f" color: #0b0b14; font-weight: 600; font-size: {config.FS(10)}pt;"
                 f" padding: {config.S(7)}px {config.S(14)}px; }}"
                 f" QPushButton:disabled {{ color: #6a6a6a; }}")
        win.btn_analyze_compare.setStyleSheet(style)

    def finish_btn_style():
        win.btn_analyze_compare.setStyleSheet(primary_button_css(10))

    win._analyze_color_anim.valueChanged.connect(update_btn_style)
    win._analyze_color_anim.finished.connect(finish_btn_style)

    def update_compare_btn():
        has_text = bool(win.text_script.toPlainText().strip())
        if getattr(win, '_analyze_last_state', None) == has_text:
            return
        win._analyze_last_state = has_text

        win.btn_analyze_compare.setEnabled(has_text)
        win.btn_side_by_side_compare.setEnabled(has_text)

        start_color = QColor("#2a2a2a") if has_text else QColor("#22d3ee")
        end_color = QColor("#22d3ee") if has_text else QColor("#2a2a2a")  # setStyleSheet themes it

        win._analyze_color_anim.stop()
        win._analyze_color_anim.setStartValue(start_color)
        win._analyze_color_anim.setEndValue(end_color)
        win._analyze_color_anim.start()

    win._update_compare_btn = update_compare_btn
    win.text_script.textChanged.connect(win._update_compare_btn)
    win._update_compare_btn()

    return wrap_activity_panel(p_script_analysis)
