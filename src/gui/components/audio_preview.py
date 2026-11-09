#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: audio_preview.py
ROLE: GUI Component
DESCRIPTION:
Unified Media Preview & Playback Dock for BadWords v4.0.
Integrates:
- Control Bar (Timecode, Playhead centering, Playback controls, Speed, Volume, Collapse tab)
- Video Preview Widget (16:9 QVideoWidget for talking-head footage)
- Timeline Preview Widget (Fairlight green clip ribbon with waveform, hatched cuts, and playhead)
- Full modularity: toggle video preview, toggle timeline preview, compact audio-only bar, or collapse all.

ONE STORY, ONE TIMELINE, EVERYWHERE: on every page the player plays the story (its blocks in story order,
the cut parts skipped, voice patches swapped in: exactly what Send to Resolve builds), the ribbon below shows
the story and the time readout is story time. With no story yet it plays the recording and says so quietly.
The "Play original" switch (off by default; called Recording in the code) plays the original recording instead (raw: no cuts, no patches),
under a banner saying so; it turns itself off when the page changes or a timeline opens.
    recording_mode / set_recording_mode(on) / recording_mode_changed(bool)
    seek_source_time(t): a moment of the recording -> there in the story (outside it: the nearest story moment)
    seek_story_time(T), story_time_now(), story_length()
"""

from __future__ import annotations

from gui.theme import qc

import os
import time
import subprocess
import sys
import re
import threading
from typing import Optional, List, Dict, Any

from PySide6.QtCore import Qt, QTimer, QUrl, QRectF, QPointF, Signal, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QPolygonF,
    QPixmap,
    QFont,
    QFontMetrics,
)
from PySide6.QtWidgets import (
    QFrame,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

import config
from gui.widgets.buttons import ToggleSwitch, AnimatedPlayerButton, AudioToggleTab, SpeedDropdown
from gui.widgets.sliders import JumpSlider
from gui.components.video_preview import VideoPreviewWidget
from gui.components.waveform_timeline import WaveformTimelineWidget
from osdoc import log_info, log_error


class PreviewToggleBtn(QPushButton):
    """Clean minimalist toggle icon button for Timeline and Video preview modes."""
    def __init__(self, mode: str = "timeline", parent: Optional[QWidget] = None, label: Optional[str] = None):
        super().__init__(parent)
        self.mode = mode
        self.label = label   # a word after the icon (the Video toggle), so it reads as a button
        self.setCheckable(True)
        self.setChecked(True)
        size = config.S(26)
        if label:
            f = self.font()
            f.setPointSizeF(config.FS(9))
            f.setWeight(QFont.DemiBold)
            self.setFont(f)
            self.setFixedSize(size + QFontMetrics(f).horizontalAdvance(label) + config.S(9), size)
        else:
            self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        # One segment of the view toggle group (the group's frame draws the pill around both)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                border-radius: {size // 2 - 1}px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background-color: #333333;
            }}
            QPushButton:checked {{
                background-color: #303030;
            }}
            QPushButton:checked:hover {{
                background-color: #3a3a3a;
            }}
        """)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = qc("#23a559") if self.isChecked() else qc("#8a8a8a")  # the classic accent: maps to the theme accent exactly
        h = float(self.height())
        w = h if self.label else float(self.width())   # the icon keeps its square on the left
        if self.label:
            painter.setPen(qc("#ffffff") if self.isChecked() else qc("#b8b8b8"))
            painter.setFont(self.font())
            painter.drawText(QRectF(w + config.S(1), 0.0, float(self.width()) - w - config.S(1), h),
                             int(Qt.AlignLeft | Qt.AlignVCenter), self.label)

        if self.mode == "timeline":
            # Waveform bars icon
            painter.setPen(QPen(color, float(config.S(1.8)), Qt.SolidLine, Qt.RoundCap))
            xs = [w * 0.26, w * 0.42, w * 0.58, w * 0.74]
            heights = [h * 0.30, h * 0.60, h * 0.42, h * 0.65]
            for x, bar_h in zip(xs, heights):
                painter.drawLine(QPointF(x, (h - bar_h) / 2.0), QPointF(x, (h + bar_h) / 2.0))
        elif self.mode == "video":
            # Filmstrip / 16:9 frame with center play triangle
            painter.setPen(QPen(color, float(config.S(1.4)), Qt.SolidLine, Qt.RoundCap))
            rect = QRectF(w * 0.18, h * 0.25, w * 0.64, h * 0.50)
            painter.drawRoundedRect(rect, config.S(2), config.S(2))
            # Center play triangle
            tri = QPolygonF([
                QPointF(w * 0.43, h * 0.36),
                QPointF(w * 0.43, h * 0.64),
                QPointF(w * 0.63, h * 0.50),
            ])
            painter.setBrush(QBrush(color))
            painter.drawPolygon(tri)


class _PlayStoryCompat:
    """Stands in for the removed "Play story" button (code that still asks it): checked = the story plays."""
    def __init__(self, ap):
        self._ap = ap

    def isChecked(self):
        return bool(self._ap.edit_mode)

    def setChecked(self, on):
        if on:
            self._ap.set_recording_mode(False)

    def isEnabled(self):
        return self._ap._has_story()

    def setEnabled(self, _on):
        pass

    def blockSignals(self, _b):
        return False

    def isVisible(self):
        return False


class AudioPreviewWidget(QFrame):
    """
    Unified bottom media dock in BadWords v4.0.
    Houses the control bar, video monitor, and waveform preview ribbon.
    """
    recording_mode_changed = Signal(bool)   # the Recording switch: True = the original recording plays

    def __init__(self, parent_widget: Optional[QWidget], main_window: Optional[QWidget]):
        super().__init__(parent_widget)
        self.main_window = main_window
        self.is_collapsed = False
        self._anim = None

        # Modularity flags with preference persistence
        prefs = {}
        if self.main_window and hasattr(self.main_window, "engine"):
            prefs = self.main_window.engine.load_preferences() or {}
        self.show_timeline = bool(prefs.get("show_timeline_preview", True))
        # The video pane: the user's choice (True/False), or None = automatic (shown when there is a video)
        pv = prefs.get("preview_show_video")
        self._video_pref = pv if isinstance(pv, bool) else None
        self._has_video = False
        self.show_video = self._video_wanted()
        self._time_tip = None

        if os.name == "posix":
            os.environ["PULSE_PROP_application.name"] = "BadWordsApp"
            os.environ["PULSE_PROP_media.role"] = "production"

        if self.main_window and hasattr(self.main_window, "scroll_area"):
            vbar = self.main_window.scroll_area.verticalScrollBar()
            vbar.actionTriggered.connect(self._on_user_scroll)

        # Shared metrics of the transport row: one control height, pills rounded to just under half of it
        # (a radius beyond half the height makes Qt drop the rounding)
        ctrl_h = config.S(30)
        pill_r = ctrl_h // 2 - 1
        self._ctrl_h = ctrl_h
        # Slider knobs: round, groove S(3) + S(4) either side. The radius is written with a decimal on
        # purpose: theme.py rounds small integer radii up, which would square the knob off
        knob = config.S(3) + 2 * config.S(4)
        knob_r = f"{(knob - 1) / 2:.1f}"  # Qt drops the rounding at exactly half

        self.setObjectName("AudioPreview")
        self.setStyleSheet(f"""
            QFrame#AudioPreview {{
                background-color: #1a1a1a;
                border: none;
                border-top: 1px solid #303030;
            }}
            QWidget#AudioContent {{
                background: transparent;
                border: none;
            }}
            QFrame#CenteredPill, QFrame#ViewToggleGroup {{
                background-color: #262626;
                border: 1px solid #303030;
                border-radius: {pill_r}px;
            }}
            QWidget#TabContainer {{
                background: transparent;
                border: none;
            }}
            QWidget#AudioControls {{
                background: transparent;
            }}
            QWidget#MediaContainer {{
                background: transparent;
            }}
            QWidget {{
                background: transparent;
            }}
            QPushButton {{
                background: transparent;
                border: none;
                color: #b0b0b0;
                font-weight: 600;
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(10)}pt;
                padding: {config.S(6)}px {config.S(10)}px;
                border-radius: {config.S(6)}px;
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.08);
                color: #ffffff;
            }}
            QPushButton:pressed {{
                background: rgba(255, 255, 255, 0.06);
            }}
            QPushButton#PlayBtn {{
                background-color: {config.BTN_BG};
                color: #ffffff;
                border-radius: {config.S(16)}px;
                font-size: {config.FS(11)}pt;
            }}
            QPushButton#PlayBtn:hover {{
                background-color: {config.BTN_ACTIVE};
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: #b0b0b0;
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(9.5)}pt;
            }}
            QComboBox {{
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: {config.S(6)}px;
                color: #d0d0d0;
                padding: {config.S(4)}px {config.S(10)}px;
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(9)}pt;
            }}
            QComboBox:hover {{
                border: 1px solid rgba(255, 255, 255, 0.15);
            }}
            QComboBox::drop-down {{
                border: none;
                width: 0px;
            }}
            QSlider::groove:horizontal {{
                height: {config.S(3)}px;
                background: #303030;
                border-radius: 1px;
            }}
            QSlider::sub-page:horizontal {{
                background: #a3a3a3;
                border-radius: 1px;
            }}
            QSlider::handle:horizontal {{
                background: #d4d4d4;
                width: {knob}px;
                margin: -{config.S(4)}px 0px;
                border-radius: {knob_r}px;
                border: 0px solid transparent;
            }}
            QSlider::handle:horizontal:hover {{
                background: #ffffff;
            }}
            QWidget#SeekBarContainer {{
                background: transparent;
                border: none;
            }}
            QSlider#SeekSlider {{
                margin: 0px;
                padding: 0px;
                border: none;
                background: transparent;
            }}
            QSlider#SeekSlider::groove:horizontal {{
                height: {config.S(3)}px;
                background: rgba(255, 255, 255, 0.10);
                border-radius: 0px;
                margin: 0px;
            }}
            QSlider#SeekSlider::sub-page:horizontal {{
                background: #1ed760;
                border-radius: 0px;
                margin: 0px;
            }}
            QSlider#SeekSlider::handle:horizontal {{
                background: #ffffff;
                width: {knob}px;
                height: {knob}px;
                margin: -{config.S(4)}px 0px;
                border-radius: {knob_r}px;
                border: 0px solid transparent;
            }}
            QSlider#SeekSlider::handle:horizontal:hover {{
                background: #ffffff;
                border: 1px solid #1ed760;
            }}
            QLabel#TimecodeLabel {{
                font-family: 'JetBrains Mono', 'SF Mono', 'Menlo', 'Consolas', monospace;
                font-size: {config.FS(9.5)}pt;
                font-weight: 600;
                color: #ffffff;
                letter-spacing: 0.5px;
                background-color: #262626;
                border: 1px solid #303030;
                border-radius: {pill_r}px;
                padding: 0px {config.S(12)}px;
            }}
            QLabel#CenteredLabel {{
                color: #d4d4d4;
                font-size: {config.FS(9)}pt;
            }}
            QLabel#StatusLabel {{
                color: #8a8a8a;
                font-size: {config.FS(9)}pt;
            }}
        """)

        dock_layout = QVBoxLayout(self)
        # One full-width surface under a hairline (QFrame#AudioPreview's top border)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)

        # 1. Main Collapsible Content Container
        self.content_widget = QWidget()
        self.content_widget.setObjectName("AudioContent")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(config.S(14), config.S(4), config.S(14), config.S(12))
        self.content_layout.setSpacing(0)
        self.content_widget.setAttribute(Qt.WA_StyledBackground, True)
        dock_layout.addWidget(self.content_widget)

        # 2. Floating Toggle Tab (wysepka)
        self.toggle_tab = AudioToggleTab(parent_widget)
        self.toggle_tab.setToolTip(self.main_window.txt("tooltip_toggle_audio_preview") if self.main_window else "Toggle Preview")
        self.toggle_tab.clicked.connect(self.toggle_collapse)

        if parent_widget:
            parent_widget.installEventFilter(self)

        # 2b. The Recording switch is on: say so above the player, plainly (amber, like a warning)
        _tb = self.main_window.txt if self.main_window else (lambda k, d=None: d or k)
        self.lbl_recording_banner = QLabel(_tb("player_recording_banner", "Playing the original recording — not the story. Turn off Play original to hear the story."))
        self.lbl_recording_banner.setObjectName("RecordingBanner")
        self.lbl_recording_banner.setAlignment(Qt.AlignCenter)
        self.lbl_recording_banner.setWordWrap(True)
        self.lbl_recording_banner.setStyleSheet(f"""
            QLabel#RecordingBanner {{ background: rgba(245, 158, 11, 0.16); color: #fbbf24;
                border: 1px solid rgba(245, 158, 11, 0.55); border-radius: {config.S(8)}px;
                font-weight: 600; font-size: {config.FS(9.5)}pt; padding: {config.S(5)}px {config.S(12)}px; }}""")
        self.lbl_recording_banner.hide()
        self.content_layout.addWidget(self.lbl_recording_banner)

        # 3. Status Label (for fetching / missing audio)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("StatusLabel")
        self.lbl_status.setFixedHeight(config.S(60))
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setOpenExternalLinks(False)
        self.lbl_status.setTextFormat(Qt.RichText)
        self.lbl_status.linkActivated.connect(self._on_fetch_missing_audio)
        self.lbl_status.hide()
        self.content_layout.addWidget(self.lbl_status)

        # 4. Top Seek Progress Slider (spans between left and right side panels)
        self.seek_bar_container = QWidget()
        self.seek_bar_container.setObjectName("SeekBarContainer")
        self.seek_bar_container.setFixedHeight(knob)
        seek_h_layout = QHBoxLayout(self.seek_bar_container)
        seek_h_layout.setContentsMargins(0, 0, 0, 0)
        seek_h_layout.setSpacing(0)

        self.seek_spacer_left = QWidget()
        self.seek_spacer_left.setStyleSheet("background: transparent;")
        self.seek_spacer_right = QWidget()
        self.seek_spacer_right.setStyleSheet("background: transparent;")

        self.slider_seek = JumpSlider(Qt.Horizontal)
        self.slider_seek.setObjectName("SeekSlider")
        self.slider_seek.setRange(0, 1000)
        self.slider_seek.setValue(0)
        self.slider_seek.setFixedHeight(knob)
        self.slider_seek.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._seek_dragging = False
        self.slider_seek.sliderPressed.connect(self._on_seek_pressed)
        self.slider_seek.sliderReleased.connect(self._on_seek_released)
        self.slider_seek.sliderMoved.connect(self._on_seek_moved)

        seek_h_layout.addWidget(self.seek_spacer_left)
        seek_h_layout.addWidget(self.slider_seek, 1)
        seek_h_layout.addWidget(self.seek_spacer_right)

        self.content_layout.addWidget(self.seek_bar_container)

        # 5. Controls Row (Timecode | Buttons | Playback | Volume)
        self.controls_widget = QWidget()
        self.controls_widget.setObjectName("AudioControls")
        # Tall enough for the control height plus breathing room; a minimum, so larger text never clips
        self.controls_widget.setMinimumHeight(ctrl_h + config.S(18))
        controls_layout = QHBoxLayout(self.controls_widget)
        controls_layout.setContentsMargins(0, config.S(8), 0, config.S(10))
        controls_layout.setSpacing(config.S(12))

        # --- Left Section: Timecode + Centered + Modularity Toggles ---
        left_widget = QWidget()
        left_layout = QHBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(config.S(8))

        self.lbl_timecode = QLabel("01:00:00:00")
        self.lbl_timecode.setObjectName("TimecodeLabel")
        self.lbl_timecode.setAlignment(Qt.AlignCenter)
        self.lbl_timecode.setFixedHeight(ctrl_h)
        self.lbl_timecode.setText("0:00")
        left_layout.addWidget(self.lbl_timecode, 0, Qt.AlignVCenter)

        # Keep centered: switch + label in one quiet pill
        centered_pill = QFrame()
        centered_pill.setObjectName("CenteredPill")
        centered_pill.setFixedHeight(ctrl_h)
        centered_layout = QHBoxLayout(centered_pill)
        centered_layout.setContentsMargins(config.S(6), 0, config.S(12), 0)
        centered_layout.setSpacing(config.S(8))

        self.tgl_centered = ToggleSwitch(parent=self)
        self.tgl_centered.toggled.connect(self._on_tgl_centered_changed)
        centered_layout.addWidget(self.tgl_centered, 0, Qt.AlignVCenter)

        self.lbl_centered = QLabel(self.main_window.txt("msg_keep_centered") if self.main_window else "Keep centered")
        self.lbl_centered.setObjectName("CenteredLabel")
        centered_layout.addWidget(self.lbl_centered, 0, Qt.AlignVCenter)
        left_layout.addWidget(centered_pill, 0, Qt.AlignVCenter)

        # Modularity Buttons: Waveform Timeline & Video Preview Toggles, as one segmented pill
        view_group = QFrame()
        view_group.setObjectName("ViewToggleGroup")
        view_group.setFixedHeight(ctrl_h)
        view_layout = QHBoxLayout(view_group)
        view_pad = max(1, (ctrl_h - config.S(26)) // 2 - 1)
        view_layout.setContentsMargins(view_pad, 0, view_pad, 0)
        view_layout.setSpacing(config.S(2))

        self.btn_toggle_timeline = PreviewToggleBtn("timeline", parent=self)
        self.btn_toggle_timeline.setChecked(self.show_timeline)
        self.btn_toggle_timeline.setToolTip(self.main_window.txt("tt_toggle_timeline", "Toggle Timeline Preview") if self.main_window else "Toggle Timeline")
        self.btn_toggle_timeline.clicked.connect(self.toggle_timeline_preview)
        view_layout.addWidget(self.btn_toggle_timeline, 0, Qt.AlignVCenter)

        _tv = self.main_window.txt if self.main_window else (lambda k, d=None: d or k)
        self.btn_toggle_video = PreviewToggleBtn("video", parent=self, label=_tv("preview_video_btn", "Video"))
        self.btn_toggle_video.setChecked(self.show_video)
        self.btn_toggle_video.clicked.connect(self.toggle_video_preview)
        self._update_video_button()
        view_layout.addWidget(self.btn_toggle_video, 0, Qt.AlignVCenter)
        left_layout.addWidget(view_group, 0, Qt.AlignVCenter)

        # Recording: hear the original recording instead of the story (off by default; turns itself off
        # when the page changes or a timeline opens). Story playback needs no button: it is always on.
        _t = self.main_window.txt if self.main_window else (lambda k, d=None: d or k)
        self.recording_pill = QFrame()
        self.recording_pill.setObjectName("CenteredPill")
        self.recording_pill.setFixedHeight(ctrl_h)
        self.recording_pill.setToolTip(_t("tt_player_recording", "Hear the original recording instead of the story (turns off when you change page)"))
        rec_layout = QHBoxLayout(self.recording_pill)
        rec_layout.setContentsMargins(config.S(6), 0, config.S(12), 0)
        rec_layout.setSpacing(config.S(8))
        self.tgl_recording = ToggleSwitch(parent=self)
        self.tgl_recording.toggled.connect(self.set_recording_mode)
        rec_layout.addWidget(self.tgl_recording, 0, Qt.AlignVCenter)
        self.lbl_recording = QLabel(_t("player_recording", "Play original"))
        self.lbl_recording.setObjectName("CenteredLabel")
        rec_layout.addWidget(self.lbl_recording, 0, Qt.AlignVCenter)
        left_layout.addWidget(self.recording_pill, 0, Qt.AlignVCenter)
        # No story yet: the player plays the recording, said quietly
        self.lbl_play_note = QLabel(_t("player_no_story", "No story yet · playing the recording"))
        self.lbl_play_note.setObjectName("StatusLabel")
        self.lbl_play_note.hide()
        left_layout.addWidget(self.lbl_play_note, 0, Qt.AlignVCenter)
        self.btn_play_edit = _PlayStoryCompat(self)

        left_layout.addStretch()

        # --- Center Section: Playback Controls ---
        center_widget = QWidget()
        center_layout = QHBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(config.S(6))
        center_layout.setAlignment(Qt.AlignCenter)

        skip_size = config.S(34)
        skip_style = f"""
            QPushButton {{ background: transparent; border: none; padding: 0px; border-radius: {skip_size // 2 - 1}px; }}
            QPushButton:hover {{ background: #2a2a2a; }}
            QPushButton:pressed {{ background: #333333; }}
        """
        self.btn_prev = AnimatedPlayerButton("player-backward.png", button_size=skip_size, icon_size=config.S(15))
        self.btn_prev.setStyleSheet(skip_style)
        # Round, filled play button with a dark glyph (light-theme icons and fill swap with the theme)
        play_size = config.S(40)
        play_icon = config.S(17)
        self.btn_play = AnimatedPlayerButton("player-play.png", button_size=play_size, icon_size=play_icon, is_circle=True)
        self.btn_play.setObjectName("PlayBtn")
        self.btn_play.setStyleSheet("QPushButton#PlayBtn { background: transparent; border: none; outline: none; }")
        self.btn_next = AnimatedPlayerButton("player-forward.png", button_size=skip_size, icon_size=config.S(15))
        self.btn_next.setStyleSheet(skip_style)

        center_layout.addWidget(self.btn_prev, 0, Qt.AlignVCenter)
        center_layout.addSpacing(config.S(2))
        center_layout.addWidget(self.btn_play, 0, Qt.AlignVCenter)
        center_layout.addSpacing(config.S(2))
        center_layout.addWidget(self.btn_next, 0, Qt.AlignVCenter)

        # Playback speed: next to Play, where it is seen (Shift+. / Shift+, change it too)
        speed_h = config.S(26)
        self.cb_speed = SpeedDropdown()
        self.cb_speed.addItems(list(self.SPEEDS_LABELS))
        self.cb_speed.setFixedSize(config.S(56), speed_h)
        self.cb_speed.setToolTip(_t("tt_playback_speed"))
        self.cb_speed.setStyleSheet(f"""
            QPushButton {{ background: #262626; border: 1px solid #303030; border-radius: {speed_h // 2 - 1}px;
                color: #d4d4d4; font-weight: 600; font-size: {config.FS(8.5)}pt; padding: 0px {config.S(6)}px; }}
            QPushButton:hover {{ color: #ffffff; background: #333333; border-color: #3a3a3a; }}""")
        center_layout.addSpacing(config.S(8))
        center_layout.addWidget(self.cb_speed, 0, Qt.AlignVCenter)

        # --- Right Section: Speed + Volume ---
        right_widget = QWidget()
        right_layout = QHBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(config.S(8))
        right_layout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.lbl_vol_icon = QLabel()
        self.lbl_vol_icon.setFixedSize(config.S(18), config.S(18))
        self.lbl_vol_icon.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.lbl_vol_icon, 0, Qt.AlignVCenter)

        self.slider_vol = JumpSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(100)
        self.slider_vol.setFixedSize(config.S(84), ctrl_h)
        right_layout.addWidget(self.slider_vol, 0, Qt.AlignVCenter)

        controls_layout.addWidget(left_widget, 2, Qt.AlignLeft | Qt.AlignVCenter)
        controls_layout.addWidget(center_widget, 1, Qt.AlignCenter)
        controls_layout.addWidget(right_widget, 2, Qt.AlignRight | Qt.AlignVCenter)

        self.content_layout.addWidget(self.controls_widget)

        # 6. Bottom Media Container: Video Preview + Timeline Preview (matching Screenshot 1!)
        self.media_container = QWidget()
        self.media_container.setObjectName("MediaContainer")
        self.media_layout = QHBoxLayout(self.media_container)
        self.media_layout.setContentsMargins(0, 0, 0, 0)
        self.media_layout.setSpacing(config.S(12))

        # Left: 16:9 Video Monitor
        self.video_preview_widget = VideoPreviewWidget(self.media_container)
        self.media_layout.addWidget(self.video_preview_widget, 0)

        # Right: Waveform Timeline Preview Ribbon
        self.waveform_timeline = WaveformTimelineWidget(self.media_container, main_window)
        self.waveform_timeline.seek_requested.connect(self._on_waveform_seek_requested)
        self.media_layout.addWidget(self.waveform_timeline, 1)

        self.content_layout.addWidget(self.media_container)

        # 7. Media Player Engine
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(1.0)
        self.player.setVideoOutput(self.video_preview_widget.video_widget)

        # Signal Connections
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_prev.clicked.connect(self.skip_backward)
        self.btn_next.clicked.connect(self.skip_forward)
        self.slider_vol.valueChanged.connect(self._on_volume_changed)
        self.cb_speed.currentTextChanged.connect(self.change_speed)
        try:  # the last speed used
            saved = float((self.main_window.engine.load_preferences() or {}).get('playback_speed') or 1.0)
        except Exception:
            saved = 1.0
        self.cb_speed.setCurrentText(self._speed_label(saved if saved in self.SPEEDS else 1.0))
        self._on_volume_changed(100)

        from gui.vsync import get_refresh_interval_ms
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(get_refresh_interval_ms())
        self.update_timer.timeout.connect(self.sync_playback)

        self.player.playbackStateChanged.connect(self.on_state_changed)

        # State Variables
        self.current_word_idx = -1
        self.last_jumped_ts = -1.0
        self.original_audio_path = None
        self._source_audio_path = None
        self._source_video_path = None
        self.clean_ops = None

        # The story: kept source ranges (seconds) in play order, and the one playing now
        from engine.edit_preview import StoryMap
        self.recording_mode = False
        self._story_active = False      # the story plays (there is one, Recording is off): see _apply_play_mode
        self._story_map = StoryMap([])  # the story's own clock (story time <-> source time, patches included)
        self._story_hint = None
        self._story_t = None            # story time at the last tick
        self._story_sig = None
        self._seg_ver = 0
        self._edit_segments = []
        self._edit_idx = -1
        # Smooth Play edit: a rendered preview of the edit replaces the jumping source once ready
        self._preview_job = None
        self._preview_on = False
        self._preview_map = []        # [(preview_start_s, source_start_s, source_end_s), ...]
        self._pending_switch = None   # (position_s, keep_playing) applied once the new source loads
        self._preview_debounce = QTimer(self)
        self._preview_debounce.setSingleShot(True)
        self._preview_debounce.setInterval(800)
        self._preview_debounce.timeout.connect(self._start_preview_render)
        self._preview_poll = QTimer(self)
        self._preview_poll.setInterval(200)
        self._preview_poll.timeout.connect(self._poll_preview_render)
        self.player.mediaStatusChanged.connect(self._on_media_status_for_switch)
        # While paused, nothing else re-syncs once a seek lands
        self.player.positionChanged.connect(self._on_position_changed_paused)
        # Voice patches in the story play here too (gui/components/patch_playback.py)
        from gui.components.patch_playback import PatchPlayback
        self.patch_playback = PatchPlayback(self)
        # The story changes in many places (board, cut settings, voice patches): look once a second
        self._story_watch = QTimer(self)
        self._story_watch.setInterval(1000)
        self._story_watch.timeout.connect(self._refresh_story)
        self._story_watch.start()
        # The Recording switch turns itself off when the page changes
        stack = getattr(self.main_window, "_stack", None)
        if stack is not None and hasattr(stack, "currentChanged"):
            stack.currentChanged.connect(lambda _i: self.set_recording_mode(False))

        self.hide()

    # -------------------------------------------------------------------------
    # Modularity & Toggles (Timeline, Video, Controls, Collapse)
    # -------------------------------------------------------------------------

    def toggle_timeline_preview(self) -> None:
        """Toggles visibility of the Waveform Timeline ribbon."""
        self.show_timeline = self.btn_toggle_timeline.isChecked()
        self._apply_modularity_state()
        self._save_modularity_prefs()

    def toggle_video_preview(self) -> None:
        """Shows or hides the video pane; the choice is remembered (preview_show_video)."""
        self._video_pref = self.btn_toggle_video.isChecked()
        self.show_video = self._video_wanted()
        self._apply_modularity_state()
        self._save_modularity_prefs()

    def _video_wanted(self) -> bool:
        """The user's choice; automatic (None): shown only when the timeline has a video of its own."""
        return self._video_pref if self._video_pref is not None else bool(self._has_video)

    def _set_has_video(self, has_video: bool) -> None:
        self._has_video = bool(has_video)
        wanted = self._video_wanted()
        if wanted != self.show_video:
            self.show_video = wanted
            self._apply_modularity_state()
        else:
            self._update_video_button()

    def _update_video_button(self) -> None:
        btn = getattr(self, "btn_toggle_video", None)
        if btn is None:
            return
        _t = self.main_window.txt if self.main_window else (lambda k, d=None: d or k)
        if btn.isChecked() != self.show_video:
            btn.setChecked(self.show_video)
        if self.show_video:
            tip = _t("tt_video_hide", "Hide the video (the waveform takes the room)")
        elif self._has_video:
            tip = _t("tt_video_show", "Show the video")
        else:
            tip = _t("tt_video_none", "Show the video area (this timeline has no video of its own: audio only)")
        btn.setToolTip(tip)
        btn.setAccessibleName(_t("preview_video_btn", "Video"))
        btn.update()

    def _save_modularity_prefs(self) -> None:
        if self.main_window and hasattr(self.main_window, "engine"):
            prefs = self.main_window.engine.load_preferences() or {}
            prefs["show_timeline_preview"] = self.show_timeline
            prefs["preview_show_video"] = self._video_pref
            self.main_window.engine.save_preferences(prefs)

    MEDIA_H = 235        # the waveform / video height (unscaled) on a roomy window
    MEDIA_H_MIN = 96     # never squeezed below this

    def _media_height(self) -> int:
        """Height of the waveform and video. Below 900 px of window height the whole expanded dock keeps to
        about 28% of the window, so the page above keeps most of a small screen."""
        full = config.S(self.MEDIA_H)
        win = self.main_window
        wh = win.height() if win is not None and hasattr(win, "height") else 0
        if not wh or wh >= 900:
            return full
        m = self.content_layout.contentsMargins()
        mm = self.media_layout.contentsMargins()
        chrome = (m.top() + m.bottom() + self.seek_bar_container.height() + mm.top() + mm.bottom()
                  + max(self.controls_widget.minimumHeight(), self.controls_widget.sizeHint().height()))
        return max(config.S(self.MEDIA_H_MIN), min(full, int(wh * 0.28) - chrome))

    def _apply_modularity_state(self) -> None:
        """Updates visibility of video, timeline, and media container based on toggle states."""
        self.waveform_timeline.setVisible(self.show_timeline)
        self.video_preview_widget.setVisible(self.show_video)
        self._update_video_button()

        has_media = self.show_timeline or self.show_video
        self.media_container.setVisible(has_media)

        # Resize video preview to maintain 16:9 aspect ratio relative to container
        target_h = self._media_height()
        self._applied_media_h = target_h
        self.video_preview_widget.setFixedHeight(target_h)
        self.video_preview_widget.setFixedWidth(int(target_h * 16.0 / 9.0))
        self.waveform_timeline.setFixedHeight(target_h)

        if not getattr(self, "is_collapsed", False):
            self.content_widget.setMaximumHeight(16777215)
            self.content_widget.setMinimumHeight(0)
            self.updateGeometry()

        self.update_tab_position()

    def update_tab_position(self) -> None:
        """Centers the floating toggle tab directly above the Play button."""
        if not hasattr(self, "toggle_tab") or not self.toggle_tab:
            return
        parent = self.parentWidget()
        if not parent or not self.isVisible():
            if hasattr(self, "toggle_tab") and self.toggle_tab:
                self.toggle_tab.hide()
            return

        tw = self.toggle_tab.width()
        th = self.toggle_tab.height()

        if hasattr(self, "btn_play") and self.btn_play.isVisible():
            center_pt = self.btn_play.mapTo(parent, self.btn_play.rect().center())
            target_x = center_pt.x() - (tw // 2)
        else:
            target_x = (parent.width() - tw) // 2

        target_y = self.y() - th
        self.toggle_tab.move(target_x, target_y)
        if not self.toggle_tab.isVisible():
            self.toggle_tab.show()
            self.toggle_tab.raise_()

    def eventFilter(self, watched, event):
        if watched == self.parentWidget() and event.type() == QEvent.Resize:
            self.update_tab_position()
            # the window got taller or shorter: the dock's height follows (small windows: ~28%)
            if getattr(self, "_applied_media_h", None) is not None and self._media_height() != self._applied_media_h:
                self._apply_modularity_state()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_tab_position()
        self.update_seek_margins()

    def showEvent(self, event):
        super().showEvent(event)
        self.update_tab_position()
        self.update_seek_margins()

    def update_seek_margins(self) -> None:
        """
        Dynamically adjusts left and right spacers so that slider_seek aligns
        between the left activity panel and right activity panel (matching the central editor width).
        """
        if not self.main_window or not hasattr(self, 'seek_spacer_left') or not hasattr(self, 'seek_spacer_right'):
            return
        try:
            stack = getattr(self.main_window, "_stack", None)
            if stack and stack.isVisible() and self.isVisible():
                p_tl = stack.mapToGlobal(QPointF(0, 0).toPoint())
                p_tr = stack.mapToGlobal(QPointF(stack.width(), 0).toPoint())

                # Measured in the seek row's own coordinates, so the dock's side padding is accounted for
                row = self.seek_bar_container
                row_tl = row.mapFromGlobal(p_tl)
                row_tr = row.mapFromGlobal(p_tr)

                w_left = max(0, int(row_tl.x()))
                w_right = max(0, int(row.width() - row_tr.x()))

                if self.seek_spacer_left.width() != w_left:
                    self.seek_spacer_left.setFixedWidth(w_left)
                if self.seek_spacer_right.width() != w_right:
                    self.seek_spacer_right.setFixedWidth(w_right)
            else:
                sb_l = getattr(self.main_window, "_sidebar_left", None)
                p_l = getattr(self.main_window, "_panel_left", None)
                sb_r = getattr(self.main_window, "_sidebar_right", None)
                p_r = getattr(self.main_window, "_panel_right", None)

                w_left = (sb_l.width() if sb_l and sb_l.isVisible() else 0) + (p_l.width() if p_l and p_l.isVisible() else 0)
                w_right = (sb_r.width() if sb_r and sb_r.isVisible() else 0) + (p_r.width() if p_r and p_r.isVisible() else 0)

                # The seek row sits inside the dock's side padding
                pad_l = self.content_layout.contentsMargins().left()
                pad_r = self.content_layout.contentsMargins().right()
                self.seek_spacer_left.setFixedWidth(max(0, w_left - pad_l))
                self.seek_spacer_right.setFixedWidth(max(0, w_right - pad_r))
        except Exception:
            pass

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "toggle_tab") and self.toggle_tab:
            self.toggle_tab.hide()

    def is_preview_active(self) -> bool:
        return self.isVisible() and not getattr(self, "is_collapsed", False)

    def _get_expanded_target_height(self) -> int:
        m = self.content_layout.contentsMargins()
        h = (m.top() + m.bottom() + self.seek_bar_container.height()
             + max(self.controls_widget.minimumHeight(), self.controls_widget.sizeHint().height()))
        if self.show_timeline or self.show_video:
            mm = self.media_layout.contentsMargins()
            h += self._media_height() + mm.top() + mm.bottom()
        if not self.lbl_recording_banner.isHidden():
            h += self.lbl_recording_banner.sizeHint().height()
        return max(50, h)

    def toggle_collapse(self) -> None:
        """Smoothly collapses or expands the entire preview dock."""
        if getattr(self, "_anim", None) and self._anim.state() == QPropertyAnimation.Running:
            self._anim.stop()

        target_collapsed = not self.is_collapsed
        self.is_collapsed = target_collapsed
        self.toggle_tab.set_collapsed(target_collapsed)

        target_h = self._get_expanded_target_height()

        if target_collapsed:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            self.update_timer.stop()
            self._clear_highlights()

            start_h = self.content_widget.height()
            if start_h <= 0:
                start_h = target_h
            self._anim = QPropertyAnimation(self.content_widget, b"maximumHeight")
            self._anim.setDuration(300)
            self._anim.setEasingCurve(QEasingCurve.InOutCubic)
            self._anim.setStartValue(start_h)
            self._anim.setEndValue(0)
            self._anim.valueChanged.connect(lambda _v: self.update_tab_position())

            def on_collapse_finished():
                if self.is_collapsed:
                    self.content_widget.hide()
                    self.content_widget.setMaximumHeight(16777215)
                self.update_tab_position()

            self._anim.finished.connect(on_collapse_finished)
            self._anim.start()
        else:
            self.content_widget.show()
            self.content_widget.setMaximumHeight(0)
            self.current_word_idx = -1
            self.sync_playback()

            self._anim = QPropertyAnimation(self.content_widget, b"maximumHeight")
            self._anim.setDuration(300)
            self._anim.setEasingCurve(QEasingCurve.InOutCubic)
            self._anim.setStartValue(0)
            self._anim.setEndValue(target_h)
            self._anim.valueChanged.connect(lambda _v: self.update_tab_position())

            def on_expand_finished():
                if not self.is_collapsed:
                    self.content_widget.setMaximumHeight(16777215)
                    self.content_widget.setMinimumHeight(0)
                self.update_tab_position()

            self._anim.finished.connect(on_expand_finished)
            self._anim.start()

    # -------------------------------------------------------------------------
    # Audio & Video Availability
    # -------------------------------------------------------------------------

    def _show_missing_audio_ui(self, is_loading=False, failed=False):
        if is_loading:
            loading_txt = self.main_window.txt("msg_audio_preview_preparing") if self.main_window else "Preparing preview..."
            self.lbl_status.setText(f"<span style='color: {qc('#a3a3a3').name()};'>{loading_txt}</span>")
        elif failed:
            failed_txt = self.main_window.txt("msg_audio_preview_failed") if self.main_window else "Failed to get audio. Click to retry."
            html = f"<a href='fetch_audio' style='color: #e74c3c; text-decoration: none;'>{failed_txt}</a>"
            self.lbl_status.setText(html)
        else:
            prefix = self.main_window.txt("msg_audio_preview_missing") if self.main_window else "Audio not found."
            link_txt = self.main_window.txt("msg_audio_preview_get_now") if self.main_window else "Get preview audio"
            html = f"{prefix} <a href='fetch_audio' style='color: {qc('#23a559').name()}; text-decoration: underline;'>{link_txt}</a>"
            self.lbl_status.setText(html)
            # The preview loads by itself: fetch the audio once per transcript (the link stays for a retry)
            snap = getattr(self.main_window, "_transcription_source", None) or {}
            canvas = getattr(self.main_window, "text_canvas", None)
            words = getattr(canvas, "words_data", None) or []
            key = (snap.get("timeline_name") or snap.get("source_file") or "",
                   words[0].get("meta_audio_path") if words else None, len(words))
            if words and getattr(self, "_auto_fetch_key", None) != key:
                self._auto_fetch_key = key
                from PySide6.QtCore import QTimer as _QT
                _QT.singleShot(300, self._on_fetch_missing_audio)

        self.lbl_status.show()
        self.controls_widget.hide()
        self.slider_seek.hide()
        self.media_container.hide()
        if getattr(self, "is_collapsed", False):
            self.content_widget.hide()
        else:
            self.content_widget.show()
        self.show()

    def _on_fetch_missing_audio(self, url=None):
        if getattr(self, "_fetching_audio", False):
            return
        self._fetching_audio = True
        self._show_missing_audio_ui(is_loading=True)

        from PySide6.QtCore import QThread, Signal

        class FetchAudioWorker(QThread):
            finished = Signal(str)
            def __init__(self, main_win):
                super().__init__()
                self.main_win = main_win
            def run(self):
                try:
                    settings = {}
                    snap = getattr(self.main_win, "_transcription_source", None) or {}
                    tl_name = snap.get("timeline_name")
                    if not tl_name and hasattr(self.main_win, "combo_tl_0") and self.main_win.combo_tl_0:
                        tl_name = self.main_win.combo_tl_0.text()
                    if tl_name:
                        settings["timeline_name"] = tl_name

                    track_indices = snap.get("track_indices")
                    if not track_indices and hasattr(self.main_win, "get_selected_track_indices"):
                        track_indices = self.main_win.get_selected_track_indices()
                    if track_indices:
                        settings["track_indices"] = track_indices

                    source_files = snap.get("source_files") or []
                    if not source_files:
                        canvas = getattr(self.main_win, "text_canvas", None)
                        if canvas and getattr(canvas, "words_data", None):
                            audio_p = canvas.words_data[0].get("meta_audio_path")
                            if audio_p:
                                source_files = [audio_p]
                    if source_files:
                        settings["source_files"] = source_files

                    wav_path = self.main_win.engine.prepare_preview_audio(settings)
                    self.finished.emit(wav_path or "")
                except Exception as e:
                    log_error(f"FetchAudioWorker error: {e}")
                    self.finished.emit("")

        self._fetch_thread = FetchAudioWorker(self.main_window)

        def _on_fetch_done(wav_path):
            self._fetching_audio = False
            if wav_path and os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                canvas = getattr(self.main_window, "text_canvas", None)
                if canvas and getattr(canvas, "words_data", None):
                    for w in canvas.words_data:
                        w["meta_audio_path"] = wav_path
                self.check_audio_availability()
            else:
                self._show_missing_audio_ui(is_loading=False, failed=True)

        self._fetch_thread.finished.connect(_on_fetch_done)
        self._fetch_thread.start()

    def check_audio_availability(self) -> None:
        """Discovers audio & video media and configures the player and timeline ribbon."""
        canvas = getattr(self.main_window, "text_canvas", None)
        words = getattr(canvas, "words_data", None) if canvas else None
        audio_path = words[0].get("meta_audio_path") if (words and len(words) > 0) else None

        if not audio_path and self.main_window and hasattr(self.main_window, "engine"):
            audio_path = getattr(self.main_window.engine, "current_analysis_audio_path", None) or getattr(self.main_window.engine, "last_audio_path", None)

        if not audio_path or not os.path.exists(audio_path):
            if not words:
                self.hide()
                return
            self._show_missing_audio_ui(is_loading=getattr(self, "_fetching_audio", False))
            return

        # Restore UI controls
        self.lbl_status.hide()
        self.controls_widget.show()
        self.slider_seek.show()
        self._apply_modularity_state()

        if getattr(self, "is_collapsed", False):
            self.content_widget.hide()
        else:
            self.content_widget.show()
        self.show()

        # Discover source video file (e.g. from DaVinci timeline or project snapshot)
        snap = getattr(self.main_window, "_transcription_source", None) or {}
        source_files = list(snap.get("source_files") or [])
        video_file = None

        for sf in source_files:
            if sf and os.path.isfile(sf):
                ext = os.path.splitext(sf)[1].lower()
                if ext in (".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm"):
                    video_file = sf
                    break

        # Fallback 1: Query resolve_handler for timeline source files if snap was empty or missing video
        if not video_file and self.main_window and getattr(self.main_window, "resolve_handler", None):
            tl_name = snap.get("timeline_name")
            try:
                resolved_files = self.main_window.resolve_handler.get_timeline_source_files(tl_name)
                for sf in (resolved_files or []):
                    if sf and os.path.isfile(sf):
                        ext = os.path.splitext(sf)[1].lower()
                        if ext in (".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm"):
                            video_file = sf
                            source_files.append(sf)
                            break
                if source_files:
                    snap["source_files"] = source_files
                    self.main_window._transcription_source = snap
            except Exception as _e:
                pass

        # Fallback 2: Check adjacent directory of audio_path for matching video footage
        if not video_file and audio_path:
            audio_dir = os.path.dirname(audio_path)
            if os.path.isdir(audio_dir):
                try:
                    for fname in os.listdir(audio_dir):
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in (".mov", ".mp4", ".m4v", ".mkv", ".webm"):
                            cand = os.path.join(audio_dir, fname)
                            if os.path.isfile(cand) and os.path.getsize(cand) > 1024 * 1024:
                                video_file = cand
                                break
                except Exception:
                    pass

        # The player shows the video only when that file IS the timeline: one source clip, same length.
        # A timeline cut from several clips (e.g. an assembled edit) plays its own rendered audio instead;
        # otherwise clicks past the first clip's end were clamped and the sound wasn't the edit.
        if video_file and not self._video_matches_timeline(video_file, audio_path, source_files):
            video_file = None
            self._video_note = "multi"
        else:
            self._video_note = None

        clip_name = os.path.basename(video_file) if video_file else (
            f"A1: {snap.get('timeline_name', 'Timeline Audio')}" if snap.get("timeline_name") else os.path.basename(audio_path)
        )

        # Set media player source
        target_source = video_file if (video_file and self.clean_ops is None) else audio_path
        if self._source_audio_path != target_source:
            # Another recording: a smooth preview of the old one means nothing now
            self._cancel_preview_render()
            self._preview_on = False
            self._preview_map = []
            self._pending_switch = None
            self._story_active = False
            self._source_audio_path = target_source
            self.original_audio_path = target_source
            self.player.setSource(QUrl.fromLocalFile(target_source))
            self.clean_ops = None
            self.current_word_idx = -1
            self.last_jumped_ts = -1.0
        # No video: the pane stays out of the way unless the user asked for it (the Video button)
        self._set_has_video(bool(video_file))
        if video_file:
            self.video_preview_widget.show_placeholder(False)
        else:
            note = (self.main_window.txt("video_multi_clip") if self.main_window else "Several clips: audio only") if getattr(self, "_video_note", None) == "multi" else "Audio Only"
            self.video_preview_widget.show_placeholder(True, note)

        # Configure Waveform Timeline Ribbon
        self.waveform_timeline.load_from_audio(audio_path, clip_name=clip_name)
        self.waveform_timeline.set_words_data(words)
        # A timeline opened: the story plays (the Recording switch turns itself off)
        self.set_recording_mode(False)
        self._refresh_story(force=True)

    def _video_matches_timeline(self, video_file: str, audio_path: str, source_files) -> bool:
        """True when the video can stand in for the timeline: a single source clip as long as the timeline audio."""
        videos = {os.path.normcase(f) for f in (source_files or []) if f and os.path.splitext(f)[1].lower() in
                  (".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm")}
        if len(videos) > 1:
            return False
        cache = getattr(self, "_duration_cache", None)
        if cache is None:
            cache = self._duration_cache = {}
        engine = getattr(self.main_window, "engine", None)
        def _dur(p):
            if p not in cache:
                try:
                    cache[p] = float(engine._get_audio_duration(p)) if engine else None
                except Exception:
                    cache[p] = None
            return cache[p]
        dv, da = _dur(video_file), _dur(audio_path)
        if not dv or not da:
            return len(videos) == 1
        return abs(dv - da) <= 1.5

    def load_assembled_audio(self, assembled_audio_path: str, clean_ops: List[Dict[str, Any]]) -> None:
        """Switches playback to the newly assembled timeline audio."""
        if os.path.exists(assembled_audio_path):
            self._cancel_preview_render()
            self._preview_on = False
            self._preview_map = []
            self._pending_switch = None
            self.original_audio_path = assembled_audio_path
            self.clean_ops = clean_ops
            self.player.setSource(QUrl.fromLocalFile(assembled_audio_path))
            self.lbl_status.hide()
            self.controls_widget.show()
            self.slider_seek.show()
            self._has_video = False          # the assembled audio has no picture
            self.show_video = self._video_wanted()
            self._apply_modularity_state()

            if getattr(self, "is_collapsed", False):
                self.content_widget.hide()
            else:
                self.content_widget.show()

            self.slider_seek.setValue(0)
            self.current_word_idx = -1
            self.last_jumped_ts = -1.0
            self._clear_highlights()
            self.show()

            self.waveform_timeline.load_from_audio(assembled_audio_path, clip_name="A1: Assembled Timeline")
            self.waveform_timeline.set_words_data([])
            self.recording_mode = False
            self._refresh_story(force=True)

    # -------------------------------------------------------------------------
    # Playback Controls & Sync
    # -------------------------------------------------------------------------

    def set_position_ms(self, ms: int, force_word_idx: Optional[int] = None) -> None:
        pp = getattr(self, "patch_playback", None)
        if pp is not None and (pp.active is not None or pp.holding):
            was_holding = pp.holding
            pp.cancel()
            if was_holding:                    # the recording was only waiting for the patch: go on from the new spot
                self.player.setPosition(ms)
                self._start_pos_s = ms / 1000.0
                self._real_start_time = time.time()
                self.player.play()
                return
        self.player.setPosition(ms)
        self._pending_seek = (ms / 1000.0, time.time())
        self._start_pos_s = ms / 1000.0
        self._real_start_time = time.time()

        if force_word_idx is not None and force_word_idx >= 0:
            self._force_highlight_idx = force_word_idx
            self._force_highlight_until = time.time() + 0.3

        self.sync_playback()   # the playhead, the readout and the word follow (in story time while it plays)

    # -------------------------------------------------------------------------
    # The story plays everywhere (and the Recording switch)
    # -------------------------------------------------------------------------

    @property
    def edit_mode(self) -> bool:
        """True while the story plays (kept for code that asked the old Play story button)."""
        return self._story_active

    @edit_mode.setter
    def edit_mode(self, _value) -> None:
        pass   # there is no Play story switch any more: the story plays whenever there is one

    def _has_story(self) -> bool:
        story = getattr(self.main_window, "_story", None) if self.main_window else None
        return bool(story and story.get("clips"))

    def _story_on(self) -> bool:
        return bool(self._edit_segments and self._has_story() and not self.recording_mode and not self.clean_ops)

    def set_recording_mode(self, on: bool) -> None:
        """The Recording switch: True = the original recording plays (raw: no cuts, no patches), False = the story."""
        on = bool(on) and self._has_story() and not self.clean_ops
        if on == self.recording_mode:
            if self.tgl_recording.isChecked() != on:
                self.tgl_recording.blockSignals(True)
                self.tgl_recording.setChecked(on, animated=False)
                self.tgl_recording.blockSignals(False)
            return
        self.recording_mode = on
        if self.tgl_recording.isChecked() != on:
            self.tgl_recording.blockSignals(True)
            self.tgl_recording.setChecked(on, animated=False)
            self.tgl_recording.blockSignals(False)
        pp = getattr(self, "patch_playback", None)
        if pp is not None and on:
            pp.cancel()
        self._refresh_story(force=True)
        self.recording_mode_changed.emit(on)

    def set_edit_segments(self, segments) -> None:
        """segments: [(start_s, end_s), ...] of the source, in the order the story plays them."""
        segments = list(segments or [])
        if segments == self._edit_segments:
            self._refresh_story()
            return
        self._edit_segments = segments
        self._edit_idx = -1
        self._seg_ver += 1
        if self._story_active and self._story_on():
            # The story changed: jump in the source again until the new smooth preview is ready
            if self._preview_on:
                self._leave_preview()
            self._preview_debounce.start()
        self._refresh_story(force=True)

    def _story_patches(self):
        """[(a, b, length in the story, tag)] of the voice patches the story plays."""
        pp = getattr(self, "patch_playback", None)
        if pp is None:
            return []
        out = []
        try:
            for p in pp.patches():
                if not pp._in_story(p):
                    continue
                a, b = float(p["a"]), float(p["b"])
                if p.get("length") == "close":
                    n = float(p.get("patch_s") or pp._file(p)[1] or (b - a))
                else:
                    n = b - a
                out.append((a, b, n, str(p.get("end_id", "patch"))))
        except Exception as exc:
            log_error(f"story map: voice patches: {exc}")
        return out

    def _refresh_story(self, force: bool = False) -> None:
        """Rebuild the story's clock and the ribbon when the story, its patches or the mode changed."""
        pp = getattr(self, "patch_playback", None)
        try:
            pp_sig = (pp.patches() and pp._sig) if pp is not None else None
        except Exception:
            pp_sig = None
        try:
            import story_time as st
            spans_id = id(st._cache.get("spans")) if self._has_story() else None
        except Exception:
            spans_id = None
        sig = (self._seg_ver, self._has_story(), pp_sig, self.recording_mode, self.clean_ops is None, spans_id)
        if not force and sig == self._story_sig:
            return
        self._story_sig = sig
        from engine.edit_preview import StoryMap
        if self._edit_segments and self._has_story():
            self._story_map = StoryMap(self._edit_segments, self._story_patches())
        else:
            self._story_map = StoryMap([])
        self._story_hint = None
        self._apply_play_mode()
        self._update_story_view()

    def _apply_play_mode(self) -> None:
        """Switch between the story and the recording (or keep what plays), keeping the spot heard."""
        on = self._story_on()
        if on == self._story_active:
            return
        self._story_active = on
        self._edit_idx = -1
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        if on:
            if self._preview_on:      # a smooth preview from before: the story map may have changed
                self._leave_preview()
            src_t = self._get_audio_t()
            if self._story_map.to_story(src_t)[0] is None:
                # not in the story: from its start (paused) / the nearest story moment (playing)
                t = self._story_map.to_source(0.0)[0] if not playing else self._story_map.snap(src_t)
                if t is not None:
                    self._seek_player_to_source(t)
            self._start_preview_render()
        else:
            self._cancel_preview_render()
            if self._preview_on:
                self._leave_preview()     # back to the recording at the same moment

    def _update_story_view(self) -> None:
        """The ribbon, the banner, the switch and the quiet note follow the mode."""
        _t = self.main_window.txt if self.main_window else (lambda k, d=None: d or k)
        has_story = self._has_story() and bool(self._edit_segments) and not self.clean_ops
        wt = self.waveform_timeline
        if self._story_active:
            m = self._story_map
            blocks = []
            try:
                import story_time as st
                starts = st.clip_starts(self.main_window)
                blocks = [(t, k) for k, t in enumerate(starts) if k > 0 and t is not None]
            except Exception:
                pass
            import story_time as st
            label = _t("timeline_chip_story", "Story · {length}").format(length=st.fmt_time(m.length))
            wt.set_story_view(m.runs, m.length, blocks, label)
        elif self.recording_mode:
            wt.set_recording_view(_t("timeline_chip_recording", "Recording · {name}").format(name=wt._clip_name))
        else:
            wt.set_recording_view("")
        self.lbl_recording_banner.setVisible(self.recording_mode)
        self.recording_pill.setEnabled(has_story)
        self.lbl_recording.setEnabled(has_story)
        self.recording_pill.setVisible(has_story or self.clean_ops is None)
        no_story = not has_story and not self.clean_ops
        self.lbl_play_note.setVisible(no_story and bool(self._source_audio_path))
        self.recording_pill.setToolTip(_t("tt_player_recording", "Hear the original recording instead of the story (turns off when you change page)")
                                       if has_story else _t("tt_player_recording_no_story", "There's no story yet: the player plays the recording."))
        self.sync_playback()

    # --- The story's clock ---------------------------------------------------

    def story_length(self) -> float:
        return float(self._story_map.length) if self._story_active else 0.0

    def story_time_now(self):
        """Story time of the playhead (None when the story isn't playing)."""
        if not self._story_active:
            return None
        return self._story_time_of(self._current_source_t())

    def _story_time_of(self, src_t: float):
        T, i = self._story_map.to_story(src_t, self._story_hint)
        if T is None:
            return None
        self._story_hint = i
        return T

    def _current_source_t(self) -> float:
        """The recording's time at the playhead, whatever plays (the smooth preview, a voice patch...)."""
        pp = getattr(self, "patch_playback", None)
        vt = pp.virtual_source_t() if pp is not None and pp.holding else None
        if vt is not None:
            return vt
        audio_t = max(0.0, self._get_audio_t())
        if self.clean_ops:
            return self._audio_to_original_time(audio_t)
        return self._preview_to_source(audio_t) if self._preview_on else audio_t

    def _seek_player_to_source(self, t: float, force_word_idx: Optional[int] = None) -> None:
        """Put the player at source time t (in the smooth preview when it plays)."""
        pos = self._source_to_preview(t) if self._preview_on else t
        if self._pending_switch is not None:          # a source switch is landing: it goes there instead
            self._pending_switch[0] = max(0.0, pos)
            self._start_pos_s = pos
            self._pending_seek = (pos, time.time())
            self.sync_playback()
            return
        self._edit_idx = -1
        self.set_position_ms(int(round(pos * 1000)), force_word_idx=force_word_idx)

    def seek_story_time(self, T: float, force_word_idx: Optional[int] = None) -> None:
        """Seek to story time T (the ribbon, the seek bar, skip). Without a story: the recording's time."""
        if not self._story_active:
            self.set_position_ms(int(max(0.0, T) * 1000), force_word_idx=force_word_idx)
            return
        t, i = self._story_map.to_source(T)
        if t is None:
            return
        self._story_hint = i
        self._seek_player_to_source(t, force_word_idx)

    # --- Smooth preview -----------------------------------------------------

    def _update_play_edit_label(self, pct=None) -> None:
        pass   # the Play story button is gone; the smooth preview renders quietly

    def _cancel_preview_render(self) -> None:
        self._preview_debounce.stop()
        self._preview_poll.stop()
        if self._preview_job is not None:
            self._preview_job.cancel()
            self._preview_job = None

    def _start_preview_render(self) -> None:
        self._cancel_preview_render()
        source = self._source_audio_path
        if not (self._story_active and self._edit_segments and source and os.path.exists(source)) or self.clean_ops:
            return
        try:
            from engine.edit_preview import EditPreviewRender
            engine = getattr(self.main_window, "engine", None)
            ffmpeg = getattr(engine, "ffmpeg_cmd", None) or "ffmpeg"
            cache_root = engine.os_doc.get_temp_folder() if engine is not None else os.path.dirname(source)
            self._preview_job = EditPreviewRender(ffmpeg, source, self._edit_segments, cache_root)
            self._preview_job.start()
            self._preview_poll.start()
        except Exception as e:
            log_error(f"Story playback: preview render could not start: {e}")
            self._preview_job = None

    def _poll_preview_render(self) -> None:
        job = self._preview_job
        if job is None:
            self._preview_poll.stop()
            return
        if job.error:
            log_error(f"Story playback: preview render failed ({job.error}); jumping in the source instead")
            self._cancel_preview_render()
            return
        if not job.done:
            return
        self._preview_poll.stop()
        self._preview_job = None
        if self._story_active and not self.clean_ops and os.path.exists(job.output):
            self._enter_preview(job.output, job.timeline)

    def _enter_preview(self, path: str, timeline) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        src_t = self._get_audio_t()
        self._preview_map = list(timeline)
        self._preview_on = True
        log_info(f"Play edit: smooth preview ready ({len(timeline)} pieces)")
        self._switch_source(path, self._source_to_preview(src_t), playing)

    def _leave_preview(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        src_t = self._preview_to_source(self._get_audio_t())
        self._preview_on = False
        self._preview_map = []
        self._edit_idx = -1
        if self._source_audio_path:
            self._switch_source(self._source_audio_path, src_t, playing)

    def _switch_source(self, path: str, pos_s: float, keep_playing: bool) -> None:
        self.player.stop()  # let the old source shut down before the new one opens
        # Applied once the NEW source reports LoadingMedia -> LoadedMedia (stop() emits the old one's status)
        self._pending_switch = [max(0.0, pos_s), keep_playing, False]
        self.player.setSource(QUrl.fromLocalFile(path))

    def _on_media_status_for_switch(self, status) -> None:
        if self._pending_switch is None:
            return
        if status == QMediaPlayer.LoadingMedia:
            self._pending_switch[2] = True
            return
        if self._pending_switch[2] and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            pos_s, keep_playing, _ = self._pending_switch
            self._pending_switch = None
            self._edit_seek(pos_s)
            if keep_playing:
                self.player.play()
                self._start_pos_s = pos_s
                self._real_start_time = time.time()

    def _preview_to_source(self, t: float) -> float:
        for p0, s, e in self._preview_map:
            if t < p0 + (e - s):
                return s + max(0.0, t - p0)
        return self._preview_map[-1][2] if self._preview_map else t

    def _source_to_preview(self, t: float) -> float:
        for p0, s, e in self._preview_map:
            if s <= t < e:
                return p0 + (t - s)
        later = [(s, p0) for p0, s, e in self._preview_map if s > t]
        if later:
            return min(later)[1]
        if self._preview_map:
            p0, s, e = self._preview_map[-1]
            return p0 + (e - s)
        return t

    def seek_source_time(self, t: float, force_word_idx: Optional[int] = None) -> None:
        """Seek to a time of the source recording, whatever the player is showing right now.
        While the story plays: there in the story; a moment the story doesn't hold goes to the nearest moment
        it does (never a silent switch to the recording). Recording on / no story: the recording itself."""
        if self.clean_ops:
            self.set_position_ms(int(self._original_to_audio_time(t) * 1000), force_word_idx=force_word_idx)
            return
        if self._story_active:
            t = self._story_map.snap(float(t))
            self._story_hint = self._story_map.to_story(t)[1]
            self._seek_player_to_source(t, force_word_idx)
            return
        self._seek_player_to_source(max(0.0, float(t)), force_word_idx)

    def _offset_s(self) -> float:
        """The cut offset (prefs "offset"), read at most once a second (the readout asks every frame)."""
        now = time.time()
        if now - getattr(self, "_offset_at", 0.0) > 1.0:
            self._offset_at = now
            try:
                self._offset_val = float((self.main_window.engine.load_preferences() or {}).get("offset", 0.133))
            except Exception:
                self._offset_val = getattr(self, "_offset_val", 0.133)
        return getattr(self, "_offset_val", 0.133)

    def pause_for_other_player(self) -> None:
        """Another player (Review cuts, Voice patch) starts: the story stops so the two don't play together."""
        pp = getattr(self, "patch_playback", None)
        if pp is not None:
            pp.cancel()
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def _locate_edit_segment(self, t: float, inside_only: bool = False):
        for i, (s, e) in enumerate(self._edit_segments):
            if s - 0.03 <= t < e:      # frame rounding moves a segment's start by up to ~0.02 s
                return i
        if inside_only:
            return None
        later = [(s, i) for i, (s, e) in enumerate(self._edit_segments) if s > t]
        return min(later)[1] if later else None

    def _edit_seek(self, t: float) -> None:
        self.player.setPosition(int(round(t * 1000)))
        self._start_pos_s = t
        self._real_start_time = time.time()

    def _follow_edit(self) -> None:
        """Called every playback tick: jump over removed parts and on to the next clip."""
        segs = self._edit_segments
        t = self._get_audio_t()
        i = self._edit_idx
        if not (0 <= i < len(segs)) or t < segs[i][0] - 0.25 or t > segs[i][1] + 0.25:
            # Just started, or the user moved the playhead: pick up from there
            i = self._locate_edit_segment(t)
            if i is None:
                self.player.pause()
                self._edit_idx = -1
                return
            self._edit_idx = i
            if t < segs[i][0]:
                self._edit_seek(segs[i][0])
            return
        if t >= segs[i][1] - 0.02:
            if i + 1 >= len(segs):
                self.player.pause()
                self._edit_idx = -1
                return
            self._edit_idx = i + 1
            self._edit_seek(segs[i + 1][0])

    def _get_audio_t(self) -> float:
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            pos = self.player.position() / 1000.0
            # A seek in a video file lands a moment later: until then the player still reports the old
            # position, which would pull the playhead back from where the user clicked
            pending = getattr(self, "_pending_seek", None)
            if pending:
                target, issued = pending
                if abs(pos - target) > 0.05 and time.time() - issued < 2.0:
                    return target
                self._pending_seek = None
            return pos
        return getattr(self, "_start_pos_s", 0.0) + (time.time() - getattr(self, "_real_start_time", 0.0)) * self.player.playbackRate()

    def _on_position_changed_paused(self, _ms) -> None:
        if self.player.playbackState() != QMediaPlayer.PlayingState and not getattr(self.waveform_timeline, "_is_scrubbing", False):
            self.sync_playback()

    def toggle_play(self) -> None:
        pp = getattr(self, "patch_playback", None)
        if pp is not None and pp.holding:      # a longer voice patch is finishing: Stop stops it, nothing resumes
            pp.cancel()
            self.on_state_changed(self.player.playbackState())
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self._story_active and not self._preview_on:
                # Play plays the story: from its start when the playhead isn't in it or the story is over
                src_t = self._get_audio_t()
                T = self._story_map.to_story(src_t, self._story_hint)[0]
                if T is None or T >= self._story_map.length - 0.05:
                    self.seek_story_time(0.0)
            elif self._story_active and self._preview_on:
                dur = self.player.duration() / 1000.0
                if dur > 0 and self._get_audio_t() >= dur - 0.05:
                    self.seek_story_time(0.0)
            self._start_pos_s = self._get_audio_t()  # the clicked spot, even if the seek hasn't landed yet
            self._real_start_time = time.time()
            self.player.play()
            self.update_timer.start()

    def on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        pp = getattr(self, "patch_playback", None)
        if pp is not None:
            pp.on_main_state(state == QMediaPlayer.PlayingState)
            if pp.holding:                     # paused only while the patch finishes: still playing for the user
                self.btn_play.update_icon("player-stop.png")
                self.update_timer.start()
                return
        if state == QMediaPlayer.PlayingState:
            self.btn_play.update_icon("player-stop.png")
            self.update_timer.start()
            vol = self.slider_vol.value()
            self.audio_output.setVolume(vol / 100.0)
            self._force_system_volume(vol)
        else:
            self.btn_play.update_icon("player-play.png")
            self.update_timer.stop()
            if state == QMediaPlayer.StoppedState:
                self._clear_highlights()
                self.waveform_timeline.set_playhead_time(0.0)

    def _skip(self, delta: float) -> None:
        if self._story_active:
            T = self.story_time_now()
            if T is None:
                T = self._story_t or 0.0
            self.seek_story_time(max(0.0, min(self._story_map.length, T + delta)))
            return
        curr_t = self._get_audio_t()
        self.set_position_ms(int(max(0.0, curr_t + delta) * 1000))

    def skip_forward(self) -> None:
        self._skip(3.0)

    def skip_backward(self) -> None:
        self._skip(-3.0)

    def _on_volume_changed(self, v: int) -> None:
        self.audio_output.setVolume(v / 100.0)
        self._force_system_volume(v)
        from gui.utils import get_layout_icon_path

        if v > 70:
            path = get_layout_icon_path("volume-max.png")
        elif v >= 40:
            path = get_layout_icon_path("volume-mid.png")
        else:
            path = get_layout_icon_path("volume-min.png")

        pixmap = QPixmap(path)
        if not pixmap.isNull():
            dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            s = config.S(18)
            pixmap = pixmap.scaled(int(s * dpr), int(s * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        self.lbl_vol_icon.setPixmap(pixmap)

    SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
    SPEEDS_LABELS = tuple(f"{v:g}×" for v in SPEEDS)

    @classmethod
    def _speed_label(cls, rate):
        return f"{rate:g}×"

    def change_speed(self, text: str) -> None:
        try:
            new_rate = float(str(text).rstrip("x×").strip())
        except ValueError:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self._start_pos_s = self._get_audio_t()
            self._real_start_time = time.time()
        self.player.setPlaybackRate(new_rate)
        pp = getattr(self, "patch_playback", None)
        if pp is not None:
            pp.set_rate(new_rate)                  # a patch playing now follows the new speed
        try:
            self.player.setPitchCompensation(True)  # voices keep their pitch when faster
        except Exception:
            pass
        try:
            self.main_window.engine.save_preferences({"playback_speed": new_rate})
        except Exception:
            pass

    def step_speed(self, direction: int) -> None:
        """One step faster (+1) or slower (-1), with a short note in the status line."""
        cur = self.player.playbackRate() or 1.0
        idx = min(range(len(self.SPEEDS)), key=lambda i: abs(self.SPEEDS[i] - cur))
        idx = max(0, min(len(self.SPEEDS) - 1, idx + (1 if direction > 0 else -1)))
        self.cb_speed.setCurrentText(self.SPEEDS_LABELS[idx])
        if hasattr(self.main_window, "_show_temporary_status"):
            self.main_window._show_temporary_status(self.main_window.txt("msg_playback_speed").format(
                speed=self.SPEEDS_LABELS[idx]), 1500)

    def _on_user_scroll(self, action: int) -> None:
        if getattr(self, "tgl_centered", None) and self.tgl_centered.isChecked():
            self.tgl_centered.setChecked(False)

    def _on_tgl_centered_changed(self, state: bool) -> None:
        if state:
            self._center_current_word()

    def _center_current_word(self) -> None:
        if getattr(self, "current_word_idx", -1) == -1:
            return
        canvas = getattr(self.main_window, "text_canvas", None)
        if not canvas or not getattr(canvas, "words_data", None):
            return

        words = canvas.words_data
        if 0 <= self.current_word_idx < len(words):
            w = words[self.current_word_idx]
            if "_rect" in w:
                rect = w["_rect"]
                scroll_area = getattr(self.main_window, "scroll_area", None)
                if scroll_area:
                    scroll_area.ensureVisible(rect.x(), rect.y(), 50, 50)
                    vp_h = scroll_area.viewport().height()
                    vbar = scroll_area.verticalScrollBar()
                    target_y = rect.center().y()
                    new_val = int(target_y - vp_h / 2)
                    vbar.setValue(max(vbar.minimum(), min(new_val, vbar.maximum())))

    def _on_seek_pressed(self) -> None:
        self._seek_dragging = True

    def _on_seek_released(self) -> None:
        from PySide6.QtGui import QGuiApplication
        self._seek_dragging = False
        if self._story_active and self._story_map.length > 0:
            T = self.slider_seek.value() / 1000.0 * self._story_map.length
            self.seek_story_time(T)
            mods = QGuiApplication.queryKeyboardModifiers()
            if mods & (Qt.ControlModifier | Qt.AltModifier):
                t_src = self._story_map.to_source(T)[0]
                if t_src is not None and self.main_window and hasattr(self.main_window, "_jump_playhead"):
                    self.main_window._jump_playhead(t_src)
            return
        dur = self.player.duration()
        if dur > 0:
            ms = int(self.slider_seek.value() / 1000.0 * dur)
            self.set_position_ms(ms)
            mods = QGuiApplication.queryKeyboardModifiers()
            if mods & (Qt.ControlModifier | Qt.AltModifier):
                t_s = ms / 1000.0
                if self.main_window and hasattr(self.main_window, "_jump_playhead"):
                    self.main_window._jump_playhead(t_s)

    def _on_seek_moved(self, value: int) -> None:
        if self._story_active and self._story_map.length > 0:
            T = value / 1000.0 * self._story_map.length
            self._show_time(self._story_map.to_source(T)[0] or 0.0, T)
            return
        dur = self.player.duration()
        if dur > 0:
            t = (value / 1000.0) * (dur / 1000.0)
            if self.clean_ops:
                t = self._audio_to_original_time(t)
            elif self._preview_on:
                t = self._preview_to_source(t)
            self._show_time(t)

    def _on_waveform_seek_requested(self, timestamp_s: float, sync_resolve: bool = False) -> None:
        """Receives playhead click/scrub from WaveformTimelineWidget for 60 FPS local playback and video sync."""
        if self.clean_ops:
            # The ribbon shows the assembled audio: the click is already in its time, no conversion
            self.set_position_ms(int(timestamp_s * 1000))
            src_t = timestamp_s
        elif self.waveform_timeline.story_view and self._story_active:
            # The ribbon shows the story: the click is story time
            self.seek_story_time(timestamp_s)
            src_t = self._story_map.to_source(timestamp_s)[0]
        else:
            self.seek_source_time(timestamp_s)
            src_t = timestamp_s
        if sync_resolve and src_t is not None and self.main_window and hasattr(self.main_window, "_jump_playhead"):
            self.main_window._jump_playhead(src_t)

    def _show_time(self, orig_t: float, story_t: Optional[float] = None) -> None:
        """The time readout: story time while the story plays ("1:12 / 2:25": where / how long, on every page,
        voice patches at their real length); the recording's time with Recording on or no story yet. The
        tooltip adds the other one and the Resolve timecode."""
        import story_time as st
        win = self.main_window
        _t = win.txt if win else (lambda k, d=None: d or k)
        rec_t = orig_t - self._offset_s()   # the story counts recording time + offset (story_panel), every mode
        e = None
        if self._story_active:
            e = story_t if story_t is not None else (self._story_t if self._story_t is not None else 0.0)
        rec = _t("time_in_recording", "{time} in the recording").format(time=st.fmt_time(rec_t))
        tc = _t("preview_tt_timecode", "Timecode {tc}").format(tc=self._format_timecode(orig_t))
        if e is not None:
            length = self._story_map.length
            text = f"{st.fmt_time(e)} / {st.fmt_time(length)}"
            tip = "\n".join((_t("time_in_story_of", "{time} in the story (of {length})").format(
                time=st.fmt_time(e), length=st.fmt_time(length)), rec, tc))
        else:
            text = st.fmt_time(rec_t)
            lines = [rec]
            if self.recording_mode:
                lines.append(_t("player_recording_banner", "Playing the original recording — not the story. Turn off Play original to hear the story."))
            elif not self.clean_ops and not self._has_story():
                lines.append(_t("player_no_story", "No story yet · playing the recording"))
            tip = "\n".join(lines + [tc])
        lbl = self.lbl_timecode
        if lbl.text() != text:
            lbl.setText(text)
        if tip != self._time_tip:
            self._time_tip = tip
            lbl.setToolTip(tip)
        if not getattr(self, "_time_w_set", False) and lbl.isVisible():
            self._time_w_set = True   # wide enough for "0:00 / 0:00": the row doesn't shift as it changes
            lbl.setMinimumWidth(lbl.fontMetrics().horizontalAdvance("00:00 / 00:00") + config.S(24) + 2)

    def _format_timecode(self, seconds: float) -> str:
        """Formats seconds to SMPTE timecode (HH:MM:SS:FF) matching DaVinci Resolve."""
        fps = 24.0
        start_frame = 0
        if self.main_window and hasattr(self.main_window, "resolve_handler") and self.main_window.resolve_handler:
            fps = float(self.main_window.resolve_handler.fps or 24.0)
            try:
                start_frame = int(self.main_window.resolve_handler.get_timeline_start_frame() or 0)
            except Exception:
                start_frame = 0

        # Fallback start frame 01:00:00:00 (standard DaVinci Resolve timeline start)
        if start_frame <= 0 and fps > 0:
            start_frame = int(3600 * fps)

        curr_frame = start_frame + int(round(seconds * fps))
        int_fps = max(1, int(round(fps)))
        ff = curr_frame % int_fps
        total_s = curr_frame // int_fps
        ss = total_s % 60
        mm = (total_s // 60) % 60
        hh = total_s // 3600
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    def _audio_to_original_time(self, audio_t: float) -> float:
        if not self.clean_ops:
            return audio_t
        fps = getattr(self.main_window.resolve_handler, "fps", 24.0) if self.main_window else 24.0
        audio_frames = audio_t * fps
        current_audio_f = 0
        for op in self.clean_ops:
            dur = op["e"] - op["s"]
            if current_audio_f <= audio_frames < current_audio_f + dur:
                return (op["s"] + (audio_frames - current_audio_f)) / fps
            current_audio_f += dur
        if self.clean_ops:
            return self.clean_ops[-1]["e"] / fps
        return audio_t

    def _original_to_audio_time(self, orig_t: float) -> float:
        if not self.clean_ops:
            return orig_t
        fps = getattr(self.main_window.resolve_handler, "fps", 24.0) if self.main_window else 24.0
        orig_frames = orig_t * fps
        current_audio_f = 0
        for op in self.clean_ops:
            if orig_frames < op["s"]:
                break
            if op["s"] <= orig_frames <= op["e"]:
                return (current_audio_f + (orig_frames - op["s"])) / fps
            current_audio_f += (op["e"] - op["s"])
        return current_audio_f / fps

    def sync_playback(self) -> None:
        """60 FPS synchronization tick for playhead, waveform, video, and words."""
        if getattr(self, "is_collapsed", False):
            self._clear_highlights()
            return
        playing = self.player.playbackState() == QMediaPlayer.PlayingState

        # Voice patches first: a longer patch holds the recording before the story jumps to the next clip
        if playing and not self.clean_ops:
            try:
                at = max(0.0, self._get_audio_t())
                self.patch_playback.tick(self._preview_to_source(at) if self._preview_on else at)
            except Exception as exc:
                log_error(f"patch playback: {exc}")

        if self._story_active and self._edit_segments and not self._preview_on and playing:
            self._follow_edit()

        audio_t = max(0.0, self._get_audio_t())
        orig_t = self._audio_to_original_time(audio_t) if self.clean_ops else audio_t
        if self._preview_on:
            orig_t = self._preview_to_source(audio_t)
        pp = getattr(self, "patch_playback", None)
        vt = pp.virtual_source_t() if pp is not None and pp.holding else None
        if vt is not None:
            # a voice patch plays (the recording waits at its span): the playhead crosses the span with it
            orig_t = vt
            audio_t = self._source_to_preview(vt) if self._preview_on else vt

        # Story time: one clock for the readout, the seek bar and the ribbon
        story_t = self._story_time_of(orig_t) if self._story_active else None
        if story_t is not None:
            self._story_t = story_t

        dur = self.player.duration() / 1000.0
        self._show_time(orig_t, story_t)

        # Update seek slider position
        if not self._seek_dragging:
            if self._story_active and self._story_map.length > 0:
                if story_t is not None:
                    self.slider_seek.setValue(int(story_t / self._story_map.length * 1000))
            elif dur > 0:
                self.slider_seek.setValue(int(audio_t / dur * 1000))

        # The ribbon's playhead: story time on the story, the recording's time on the recording
        wt = self.waveform_timeline
        if wt.story_view:
            if story_t is not None and not getattr(wt, "_is_scrubbing", False):
                wt.set_playhead_time(story_t)
        else:
            wt.set_playhead_time(orig_t if self._preview_on else audio_t)

        canvas = getattr(self.main_window, "text_canvas", None)
        if not canvas or not getattr(canvas, "words_data", None):
            return

        # Story board: highlight the block that's playing (a few times a second is plenty)
        now = time.time()
        if not self.clean_ops and now - getattr(self, "_board_tick", 0.0) > 0.2:
            self._board_tick = now
            board = getattr(self.main_window, "story_board", None)
            if board is not None:
                board.set_playing_time(orig_t if (self.player.playbackState() == QMediaPlayer.PlayingState or vt is not None) else None)

        # Word highlighting
        found_idx = -1
        if time.time() < getattr(self, "_force_highlight_until", 0.0):
            found_idx = getattr(self, "_force_highlight_idx", -1)
        else:
            offset_s = 0.0
            if self.clean_ops and hasattr(self.main_window, "engine"):
                prefs = self.main_window.engine.load_preferences() or {}
                offset_s = prefs.get("offset", 0.133)

            match_t = orig_t + 0.15 - offset_s
            # Edited view: only words the edit keeps can light up (never one that was cut)
            allowed = canvas.edit_ids() if hasattr(canvas, 'edit_ids') else None
            for i, w_item in enumerate(canvas.words_data):
                start_t = w_item.get("start", 0)
                if start_t > match_t:
                    break
                if allowed is None or i in allowed:
                    found_idx = i

        if found_idx >= 0 and canvas.words_data[found_idx].get("type") == "silence":
            pass
        elif found_idx != getattr(self, "current_word_idx", -1):
            self._clear_highlights()
            self.current_word_idx = found_idx
            if found_idx >= 0:
                w = canvas.words_data[found_idx]
                w["_audio_active"] = True
                canvas.update()

        if getattr(self, "tgl_centered", None) and self.tgl_centered.isChecked():
            self._center_current_word()

        # The Story page shows the word being spoken too (as Clean does)
        if found_idx != getattr(self, "_story_word_idx", -2):
            self._story_word_idx = found_idx
            sv = getattr(self.main_window, "story_view", None)
            if sv is not None and hasattr(sv, "set_playing_word") and self.main_window._stack.currentIndex() == 5:
                try:
                    sv.set_playing_word(found_idx, orig_t)
                except Exception as exc:
                    print(f"[preview] story word: {exc}")

    def _clear_highlights(self) -> None:
        canvas = getattr(self.main_window, "text_canvas", None)
        if canvas and canvas.words_data:
            for w in canvas.words_data:
                w.pop("_audio_active", None)
            canvas.update()

    def _force_system_volume(self, v: int) -> None:
        if os.name != "posix":
            return
        def _task():
            try:
                time.sleep(0.1)
                script_name = os.path.basename(sys.argv[0])
                out = subprocess.check_output(["pactl", "list", "sink-inputs"], text=True)
                current_id = None
                for line in out.splitlines():
                    if "Sink Input" in line or "odpływ wejścia" in line:
                        m = re.search(r"(?:#|^)(\d+)\.?", line)
                        if m:
                            current_id = m.group(1)
                    if script_name in line and current_id:
                        subprocess.call(["pactl", "set-sink-input-volume", current_id, f"{v}%"])
            except Exception:
                pass
        threading.Thread(target=_task, daemon=True).start()
