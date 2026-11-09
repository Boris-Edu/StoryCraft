#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: video_preview.py
ROLE: GUI Component
DESCRIPTION:
16:9 Video playback preview container utilizing QVideoWidget.
Features rounded corners, aspect-ratio preservation, and sleek DaVinci Resolve styling.
"""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel, QSizePolicy
from PySide6.QtMultimediaWidgets import QVideoWidget

import config


class VideoPreviewWidget(QFrame):
    """
    Video playback monitor container for talking-head and timeline footage.
    Embedded in the bottom dock alongside the Timeline Preview ribbon.
    """
    def __init__(self, parent: Optional[QFrame] = None):
        super().__init__(parent)
        self.setObjectName("VideoPreviewWidget")
        # Same card shape as the waveform beside it: radius S(11), hairline border
        self.setStyleSheet(f"""
            QFrame#VideoPreviewWidget {{
                background-color: #222222;
                border: 1px solid #303030;
                border-radius: {config.S(11)}px;
            }}
        """)
        self.setMinimumHeight(config.S(90))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        # Inset by the border so the hairline and rounded corners stay visible around the picture
        inset = max(1, config.S(3))
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setStyleSheet(f"background-color: #000000; border-radius: {config.S(8)}px;")
        self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        layout.addWidget(self.video_widget)

        self.lbl_placeholder = QLabel(self)
        self.lbl_placeholder.setAlignment(Qt.AlignCenter)
        self.lbl_placeholder.setWordWrap(True)
        self.lbl_placeholder.setStyleSheet(
            f"color: #8a8a8a; background: transparent; border: none; font-family: '{config.UI_FONT_NAME}';"
            f" font-size: {config.FS(9)}pt; padding: 0px {config.S(16)}px;")
        self.lbl_placeholder.setText("Video Preview")
        self.lbl_placeholder.hide()
        layout.addWidget(self.lbl_placeholder)

    def sizeHint(self) -> QSize:
        h = max(config.S(90), self.height() if self.height() > 0 else config.S(145))
        w = int(h * 16.0 / 9.0)
        return QSize(w, h)

    def show_placeholder(self, show: bool = True, text: str = "Video Preview") -> None:
        if show:
            self.lbl_placeholder.setText(text)
            self.lbl_placeholder.show()
            self.video_widget.hide()
        else:
            self.lbl_placeholder.hide()
            self.video_widget.show()
