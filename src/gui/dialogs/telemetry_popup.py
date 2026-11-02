#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: telemetry_popup.py
ROLE: GUI Dialog
DESCRIPTION:
Consent dialog for anonymous app analytics.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget, QFrame
)
import config
from gui.components.mixins import FramelessWindowMixin, _BaseDialog, _HAS_QFRAMELESS
from gui.components.titlebar import CustomTitleBar
from gui.widgets.language_selector import _LangPickerDialog
from gui.utils import _app_icon, _txt, _center_on_screen


class TelemetryPopup(FramelessWindowMixin, _BaseDialog):
    """
    Modal dialog asking once for StoryCraft's usage stats.

    Skip condition: if engine.os_doc.get_telemetry_pref("stats_opt_in")
    is not None, caller should not show this dialog.

    On "Yes": saves stats_opt_in = True and sends the ping (engine.send_telemetry_ping).
    On "No thanks" (or Escape / close): saves stats_opt_in = False.
    """

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._lang_picker = None

        # Load current language from preferences
        prefs = engine.load_preferences() or {}
        self._lang = prefs.get("gui_lang", "en")
        if self._lang not in config.TRANS:
            self._lang = "en"

        # --- Window setup ---
        self.frameless_init(is_popup=True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModal)
        self.setWindowIcon(_app_icon())

        # --- Root QSS (window is transparent; styling lives on inner_frame) ---
        # StoryCraft look, same values as Settings: gui/theme.py maps these greys onto the active
        # theme (light ones included); only the brand gradient and its ink text pass through.
        grad = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
        grad_hover = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3ad9f0, stop:1 #b36cf8)"
        btn = f"""
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(10)}pt;
                font-weight: 600;
                border: none;
                min-width: {config.S(120)}px;
                padding: {config.S(8)}px {config.S(18)}px;
                border-radius: {config.S(10)}px;"""
        self.setStyleSheet(f"""
            QDialog, TelemetryPopup {{ background-color: transparent; }}
            QFrame#MainInnerFrame {{
                background-color: #1c1c1c;
                border: 1px solid #2c2c2c;
                border-radius: {config.S(14)}px;
            }}
            QLabel#lbl_title {{
                color: #ededed;
                font-size: {config.FS(15)}pt;
                font-weight: 600;
                font-family: "Outfit";
                background: transparent;
            }}
            QLabel#lbl_msg {{
                color: #b0b0b0;
                font-size: {config.FS(10.5)}pt;
                font-family: "{config.UI_FONT_NAME}";
                background: transparent;
            }}
            QPushButton#btn_lang {{
                color: #8a8a8a;
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(10)}pt;
                font-weight: 600;
                background: transparent;
                border: none;
                padding: {config.S(2)}px {config.S(4)}px;
            }}
            QPushButton#btn_lang:hover {{ color: #ededed; }}
            QPushButton#btn_yes {{{btn}
                background: {grad};
                color: #0b0b14;
            }}
            QPushButton#btn_yes:hover {{ background: {grad_hover}; }}
            QPushButton#btn_no {{{btn}
                background-color: #2c2c2c;
                color: #ededed;
            }}
            QPushButton#btn_no:hover {{ background-color: #353535; }}
        """)

        # --- Outer wrapper ---
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(1, 1, 1, 1)

        self.inner_frame = QFrame(self)
        self.inner_frame.setObjectName("MainInnerFrame")

        main_layout.addWidget(self.inner_frame)

        root_layout = QVBoxLayout(self.inner_frame)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # --- Custom title bar ---
        self._tb = CustomTitleBar(self, self._lang, parent=self.inner_frame)
        if _HAS_QFRAMELESS and getattr(self, '_is_win', False) and hasattr(self, 'setTitleBar'):
            self.setTitleBar(self._tb)
        self._tb.btn_min.hide()
        self._tb.btn_max.hide()
        if hasattr(self._tb, '_lbl_title'):
            self._tb._lbl_title.setText(self._t("stats_title"))
        root_layout.addWidget(self._tb)

        # --- Content area ---
        container = QWidget(self.inner_frame)
        container.setObjectName("container")
        content_layout = QVBoxLayout(container)
        content_layout.setContentsMargins(config.S(20), config.S(15), config.S(20), config.S(20))
        content_layout.setSpacing(0)
        root_layout.addWidget(container)

        # Header row (title + language button)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)

        self._lbl_title = QLabel("", container)
        self._lbl_title.setObjectName("lbl_title")
        header.addWidget(self._lbl_title, 1)

        self._btn_lang = QPushButton("", container)
        self._btn_lang.setObjectName("btn_lang")
        self._btn_lang.setCursor(Qt.PointingHandCursor)
        self._btn_lang.setFocusPolicy(Qt.NoFocus)
        self._btn_lang.clicked.connect(self._show_lang_picker)
        header.addWidget(self._btn_lang)

        content_layout.addLayout(header)
        content_layout.addSpacing(config.S(15))

        # Message label
        self._lbl_msg = QLabel("", container)
        self._lbl_msg.setObjectName("lbl_msg")
        self._lbl_msg.setWordWrap(True)
        self._lbl_msg.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        content_layout.addWidget(self._lbl_msg)
        content_layout.addSpacing(config.S(10))

        content_layout.addSpacing(config.S(10))

        # Buttons row (No | Yes)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch()

        self._btn_no  = QPushButton("", container)
        self._btn_no.setObjectName("btn_no")
        self._btn_no.setCursor(Qt.PointingHandCursor)
        self._btn_no.clicked.connect(self._on_no)
        btn_row.addWidget(self._btn_no)
        btn_row.addSpacing(config.S(10))

        self._btn_yes = QPushButton("", container)
        self._btn_yes.setObjectName("btn_yes")
        self._btn_yes.setCursor(Qt.PointingHandCursor)
        self._btn_yes.clicked.connect(self._on_yes)
        btn_row.addWidget(self._btn_yes)
        btn_row.addStretch()

        content_layout.addLayout(btn_row)

        # --- Populate text and size ---
        self._refresh_texts()

    def _t(self, key: str, **kwargs) -> str:
        return _txt(self._lang, key, **kwargs)

    def _refresh_texts(self):
        """Update all translatable labels and re-size the dialog."""
        self.setWindowTitle(self._t("stats_title"))
        self._lbl_title.setText(self._t("stats_title"))
        self._lbl_msg.setText(self._t("stats_msg"))
        self._btn_yes.setText(self._t("stats_yes"))
        self._btn_no.setText(self._t("stats_no"))
        self._btn_lang.setText(self._lang.upper())
        if hasattr(self, '_tb') and hasattr(self._tb, '_lbl_title'):
            self._tb._lbl_title.setText(self._t("stats_title"))

        DIALOG_W      = config.S(580)
        HORIZ_MARGINS = config.S(15 + 15 + 20 + 20)
        self._lbl_msg.setMaximumWidth(DIALOG_W - HORIZ_MARGINS)

        self.setFixedWidth(DIALOG_W)
        self.adjustSize()
        h = max(self.sizeHint().height(), self.height())
        _center_on_screen(self, DIALOG_W, h)

    def _show_lang_picker(self):
        """Open a floating language picker anchored below the lang button."""
        try:
            if self._lang_picker and self._lang_picker.isVisible():
                self._lang_picker.close()
                return
        except RuntimeError:
            self._lang_picker = None

        self._lang_picker = _LangPickerDialog(self)
        self._lang_picker.lang_selected.connect(self._on_lang_selected)

        btn_global = self._btn_lang.mapToGlobal(
            self._btn_lang.rect().bottomRight()
        )
        picker_w = self._lang_picker.width()
        self._lang_picker.move(btn_global.x() - picker_w, btn_global.y())
        self._lang_picker.show()

    def _on_lang_selected(self, code: str):
        """Handle language selection: persist and refresh UI."""
        if code == self._lang:
            return
        self._lang = code
        self._engine.save_preferences({"gui_lang": code})
        self._refresh_texts()

    def _on_yes(self):
        self._engine.os_doc.set_telemetry_pref("stats_opt_in", True)
        self._engine.send_telemetry_ping("app_started")
        self.accept()

    def _on_no(self):
        self._engine.os_doc.set_telemetry_pref("stats_opt_in", False)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._on_no()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self._on_no()
        super().closeEvent(event)
