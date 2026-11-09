#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: waveform_timeline.py
ROLE: GUI Component
DESCRIPTION:
Interactive waveform timeline preview ribbon widget with real-time playhead.
Inspired by DaVinci Resolve / Fairlight interface aesthetics.
Features pre-rendered QPixmap waveform caching for stable 60 FPS scrubbing,
cut range hatching (solid vs hatched), High-DPI scaling, and bidirectional playhead synchronization.

Two views (the player decides, gui/components/audio_preview.py):
  - the story (set_story_view): the story's audio laid end to end in play order, built from the recording's
    peaks (no extra decoding), block boundaries marked, voice patches as cyan stretches at the length they
    play; the time axis, the playhead and clicks are STORY time;
  - the recording (set_recording_view): the raw recording with its cut regions, in recording time.
"""

from __future__ import annotations

from gui.theme import qc

import os
from typing import Optional, List, Dict, Any, Tuple

import numpy as np
from PySide6.QtCore import Qt, QRectF, QPointF, QLineF, Signal, QThread
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QPainterPath,
    QPixmap,
    QFont,
    QLinearGradient,
    QPolygonF,
)
from PySide6.QtWidgets import QWidget, QSizePolicy

import config
from engine.waveform import WaveformPeaks, WaveformExtractor
from osdoc import log_info, log_error


# DaVinci Resolve solid timeline clip colors (matching DaVinci Edit / Fairlight page)
RESOLVE_CLIP_COLORS = {
    "normal": qc("#2f614d"),       # DaVinci Fairlight Green
    "bad": qc("#8c3450"),          # DaVinci Violet / Pink (Errors / Bad Takes)
    "red": qc("#8c3450"),
    "violet": qc("#8c3450"),
    "repeat": qc("#2b5278"),       # DaVinci Navy / Blue (Retakes)
    "blue": qc("#2b5278"),
    "navy": qc("#2b5278"),
    "typo": qc("#356844"),         # DaVinci Olive (Typo)
    "green": qc("#356844"),
    "olive": qc("#356844"),
    "silence_mark": qc("#8a7154"), # DaVinci Tan (Silence marked)
    "tan": qc("#8a7154"),
    "silence": qc("#8a7154"),
    "inaudible": qc("#5e3518"),    # DaVinci Chocolate (Inaudible)
    "chocolate": qc("#5e3518"),
}
CUT_VOID_COLOR = QColor("#0e0e0e")


class WaveformWorker(QThread):
    """Background worker thread to extract waveform peaks without freezing the Qt UI."""
    peaks_ready = Signal(object, str)  # WaveformPeaks, audio_path
    error = Signal(str)

    def __init__(
        self,
        audio_path: str,
        points_per_second: int = 400,
        cache_dir: Optional[str] = None,
        ffmpeg_cmd: str = "ffmpeg",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.audio_path = audio_path
        self.points_per_second = points_per_second
        self.cache_dir = cache_dir
        self.ffmpeg_cmd = ffmpeg_cmd

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            peaks = WaveformExtractor.extract_from_file(
                self.audio_path,
                points_per_second=self.points_per_second,
                cache_dir=self.cache_dir,
                ffmpeg_cmd=self.ffmpeg_cmd,
            )
            if self.isInterruptionRequested():
                return
            if peaks is not None and peaks.num_points > 0:
                self.peaks_ready.emit(peaks, self.audio_path)
            else:
                self.error.emit(f"Failed to generate waveform for {self.audio_path}")
        except Exception as e:
            if not self.isInterruptionRequested():
                log_error(f"WaveformWorker error: {e}")
                self.error.emit(str(e))


class WaveformTimelineWidget(QWidget):
    """
    DaVinci Resolve / Fairlight styled timeline waveform preview ribbon.
    Pre-renders the waveform and cut regions to QPixmap for zero-latency 60 FPS playhead movement.
    """
    seek_requested = Signal(float, bool)  # Emits (target_timestamp_s, sync_resolve)
    scrub_started = Signal()
    scrub_finished = Signal()
    cut_mark_clicked = Signal(int)  # a story cut's marker (set_cut_marks) was clicked: its number

    def __init__(self, parent: Optional[QWidget] = None, main_window: Optional[QWidget] = None):
        super().__init__(parent)
        self.main_window = main_window

        # Card metrics: inner padding, corner radius, and the height of the header pills
        self._PAD = config.S(10)
        self._CARD_R = config.S(11)
        self._PILL_H = config.S(22)

        # State
        self._peaks: Optional[WaveformPeaks] = None
        self._audio_path: Optional[str] = None
        self._clip_name: str = "A1: Timeline Audio"
        self._duration: float = 0.0          # length of what is shown (the story, or the recording)
        self._src_duration: float = 0.0      # length of the recording
        self._playhead_time: float = 0.0
        # The story view (set_story_view): None = the recording
        self._story_runs = None              # [(story_s, story_e, src_s, src_e, tag)]
        self._story_blocks: List[Tuple[float, int]] = []  # (story time, cut number): where blocks 2.. start
        self._story_label: str = ""
        self._story_peaks: Optional[WaveformPeaks] = None
        self._story_segments: List[Dict[str, Any]] = []
        self._is_scrubbing: bool = False
        self._hover_x: Optional[float] = None
        self._worker: Optional[WaveformWorker] = None

        # Zoom & Viewport Pan State
        self._zoom_level: float = 1.0  # 1.0 = Fit all, up to 120.0 = deep zoom
        self._view_start_time: float = 0.0

        # Cuts & Marking Ranges for Visual Design System (Solid vs Hatched)
        self._words_data: Optional[List[Dict[str, Any]]] = None
        self._cut_ranges: List[Tuple[float, float, str]] = []  # (start_s, end_s, type)
        self._timeline_segments: List[Dict[str, Any]] = []

        # Pre-rendered cache
        self._waveform_pixmap: Optional[QPixmap] = None
        self._needs_pixmap_rebuild: bool = True

        # Horizontal ScrollBar for panning when zoomed in
        from PySide6.QtWidgets import QScrollBar, QPushButton, QHBoxLayout
        self.scrollbar = QScrollBar(Qt.Horizontal, self)
        self.scrollbar.setObjectName("TimelineScrollBar")
        self.scrollbar.setStyleSheet(f"""
            QScrollBar:horizontal {{
                height: {config.S(7)}px;
                background: #1a1a1a;
                margin: 0px;
                border: none;
                border-radius: {config.S(2)}px;
            }}
            QScrollBar::handle:horizontal {{
                background: #3a3a3a;
                min-width: {config.S(24)}px;
                border-radius: {config.S(2)}px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: #4a4a4a;
            }}
            QScrollBar::handle:horizontal:pressed {{
                background: #2ecc71;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
                height: 0px;
                background: transparent;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)
        self.scrollbar.setRange(0, 1000)
        self.scrollbar.setValue(0)
        self.scrollbar.hide()
        self.scrollbar.valueChanged.connect(self._on_scrollbar_changed)

        # Zoom Controls Widget ([-] [Fit] [+]) at top-right corner of track lane
        self.zoom_ctrl_widget = QWidget(self)
        self.zoom_ctrl_widget.setObjectName("ZoomCtrlWidget")
        zoom_layout = QHBoxLayout(self.zoom_ctrl_widget)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(config.S(4))

        # Small pills, the same family as the clip chip painted on the left of the header row
        pill_h = self._PILL_H
        btn_style = f"""
            QPushButton {{
                background-color: #2a2a2a;
                border: 1px solid #303030;
                border-radius: {pill_h // 2 - 1}px;
                color: #d4d4d4;
                font-family: "{config.UI_FONT_NAME}";
                font-size: {config.FS(8)}pt;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton:hover {{
                background-color: #333333;
                border-color: #3a3a3a;
                color: #ffffff;
            }}
            QPushButton:pressed {{
                background-color: rgba(46, 204, 113, 0.25);
            }}
        """
        self.btn_zoom_out = QPushButton("−", self.zoom_ctrl_widget)
        self.btn_zoom_out.setToolTip(self.main_window.txt("tt_zoom_out", "Zoom Out") if self.main_window else "Zoom Out")
        self.btn_zoom_out.setStyleSheet(btn_style)
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        self.btn_zoom_out.setFixedSize(config.S(26), pill_h)

        self.btn_zoom_fit = QPushButton("Fit", self.zoom_ctrl_widget)
        self.btn_zoom_fit.setToolTip(self.main_window.txt("tt_zoom_fit", "Fit Timeline") if self.main_window else "Fit Timeline")
        self.btn_zoom_fit.setStyleSheet(btn_style)
        self.btn_zoom_fit.clicked.connect(self.zoom_fit)
        self.btn_zoom_fit.setFixedSize(config.S(38), pill_h)

        self.btn_zoom_in = QPushButton("+", self.zoom_ctrl_widget)
        self.btn_zoom_in.setToolTip(self.main_window.txt("tt_zoom_in", "Zoom In (or Ctrl + Wheel)") if self.main_window else "Zoom In")
        self.btn_zoom_in.setStyleSheet(btn_style)
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        self.btn_zoom_in.setFixedSize(config.S(26), pill_h)

        zoom_layout.addWidget(self.btn_zoom_out)
        zoom_layout.addWidget(self.btn_zoom_fit)
        zoom_layout.addWidget(self.btn_zoom_in)

        # Widget styling & sizing
        self.setObjectName("WaveformTimelineWidget")
        self.setMinimumHeight(config.S(105))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def set_waveform_data(
        self,
        peaks: Optional[WaveformPeaks],
        clip_name: Optional[str] = None,
        audio_path: Optional[str] = None,
    ) -> None:
        """Sets pre-computed waveform peak data and refreshes the display."""
        self._peaks = peaks
        if audio_path is not None:
            self._audio_path = audio_path
        if clip_name:
            self._clip_name = clip_name
        if peaks is not None:
            self._src_duration = peaks.duration
            self._story_peaks = None
            if self._story_runs is None:
                self._duration = peaks.duration
        self._zoom_level = 1.0
        self._view_start_time = 0.0
        self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    def load_from_audio(
        self,
        audio_path: str,
        clip_name: Optional[str] = None,
        duration: Optional[float] = None,
        track_name: Optional[str] = None,
    ) -> None:
        """
        Loads waveform from audio file in background without blocking the UI.
        If the file has already been loaded, avoids duplicate processing.
        """
        if not audio_path or not os.path.isfile(audio_path):
            return

        effective_name = clip_name or track_name
        if effective_name:
            self._clip_name = effective_name
        if duration and duration > 0:
            self._src_duration = duration
            if self._story_runs is None:
                self._duration = duration

        if self._audio_path == audio_path and self._peaks is not None and self._peaks.num_points > 0:
            return  # Already loaded

        self._audio_path = audio_path

        # Determine temp cache folder
        cache_dir = None
        if self.main_window and hasattr(self.main_window, "engine") and hasattr(self.main_window.engine, "os_doc"):
            cache_dir = self.main_window.engine.os_doc.get_temp_folder()

        ffmpeg_cmd = "ffmpeg"
        if self.main_window and hasattr(self.main_window, "engine"):
            ffmpeg_cmd = getattr(self.main_window.engine, "ffmpeg_cmd", "ffmpeg")

        # Safely detach any previous background worker without killing thread
        if self._worker is not None and self._worker.isRunning():
            try:
                self._worker.peaks_ready.disconnect()
            except Exception:
                pass
            try:
                self._worker.error.disconnect()
            except Exception:
                pass
            self._worker.requestInterruption()

        self._worker = WaveformWorker(
            audio_path=audio_path,
            points_per_second=400,
            cache_dir=cache_dir,
            ffmpeg_cmd=ffmpeg_cmd,
            parent=self,
        )
        self._worker.peaks_ready.connect(self._on_peaks_ready)
        self._worker.error.connect(self._on_peaks_error)
        self._worker.start()

    def set_cut_marks(self, marks) -> None:
        """[(source time s, cut number)]: where the story cuts, drawn as small flags; clicking one emits
        cut_mark_clicked(n) (the main window opens that cut in Review)."""
        self._cut_marks = list(marks or [])
        self.update()

    # -- The story view ---------------------------------------------------------------------------
    @property
    def story_view(self) -> bool:
        """True while the ribbon shows the story (story time), False for the recording."""
        return self._story_runs is not None

    def set_story_view(self, runs, length: float, block_starts=(), label: str = "") -> None:
        """Show the story: runs = StoryMap.runs (play order), length = the story's length (s), block_starts =
        [(story time, cut number)] where blocks 2.. start (cut k sits before block k, as in Review), label =
        the chip text. Cheap to call again with the same data (nothing is rebuilt)."""
        runs = [tuple(r) for r in (runs or [])]
        blocks = sorted((float(t), int(k)) for t, k in (block_starts or []) if 0.0 < float(t) < float(length))
        was_story = self._story_runs is not None
        same = was_story and runs == self._story_runs and abs(length - self._duration) < 1e-6
        if same and blocks == self._story_blocks and label == self._story_label:
            return
        self._story_blocks = blocks
        self._story_label = label or ""
        if not same:
            self._story_runs = runs
            self._story_peaks = None
            self._duration = max(0.0, float(length))
            self._story_segments = self._build_story_segments(runs)
            if not was_story:
                self._zoom_level = 1.0
                self._view_start_time = 0.0
            self._view_start_time = max(0.0, min(self._view_start_time, max(0.0, self._duration - self.visible_duration())))
            self._playhead_time = min(self._playhead_time, self._duration)
            self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    def set_recording_view(self, label: Optional[str] = None) -> None:
        """Show the raw recording (recording time), as Clean always did."""
        if self._story_runs is None and (label is None or label == self._story_label):
            return
        was_story = self._story_runs is not None
        self._story_runs = None
        self._story_peaks = None
        self._story_segments = []
        self._story_blocks = []
        self._story_label = label or ""
        self._duration = self._src_duration
        if was_story:
            self._zoom_level = 1.0
            self._view_start_time = 0.0
            self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    PATCH_COLOR = "#22d3ee"   # voice patches: cyan, as on the Story page

    def _build_story_segments(self, runs):
        out = []
        for e0, e1, _s, _e, tag in runs:
            if e1 - e0 <= 1e-4:
                continue
            out.append({"start_s": e0, "end_s": e1, "type": "patch" if tag is not None else "normal",
                        "is_cut": False, "color": RESOLVE_CLIP_COLORS["normal"]})
        return out

    def _ensure_story_peaks(self) -> Optional[WaveformPeaks]:
        """The story's waveform: the recording's peaks of each run, end to end (a patch's span stretched to
        the length it plays). Built once per story with numpy slicing: instant even for long interviews."""
        if self._story_peaks is not None or self._story_runs is None:
            return self._story_peaks
        src = self._peaks
        if src is None or src.num_points <= 0 or self._duration <= 0:
            return None
        pps = int(src.points_per_second or 400)
        n_src = src.num_points
        mins, maxs, rmss = [], [], []
        done = 0
        for e0, e1, s, e, _tag in self._story_runs:
            n = int(round(e1 * pps)) - done
            if n <= 0:
                continue
            done += n
            i0 = max(0, min(n_src - 1, int(s * pps)))
            i1 = max(i0 + 1, min(n_src, int(round(e * pps))))
            if i1 - i0 == n:
                idx = slice(i0, i1)
            else:
                idx = np.minimum(n_src - 1, i0 + (np.arange(n) * (i1 - i0) // max(1, n)))
            mins.append(src.peaks_min[idx])
            maxs.append(src.peaks_max[idx])
            rmss.append(src.peaks_rms[idx] if len(src.peaks_rms) == n_src else src.peaks_max[idx])
        if not mins:
            return None
        self._story_peaks = WaveformPeaks(
            peaks_min=np.concatenate(mins).astype(np.float32, copy=False),
            peaks_max=np.concatenate(maxs).astype(np.float32, copy=False),
            peaks_rms=np.concatenate(rmss).astype(np.float32, copy=False),
            duration=float(self._duration),
            sample_rate=getattr(src, "sample_rate", 16000),
            points_per_second=pps,
        )
        return self._story_peaks

    def _story_cut_marks(self):
        """[(story time, cut number)] at the block boundaries: the same cuts Review opens."""
        return list(self._story_blocks)

    def _cut_mark_at(self, x, y):
        marks = self._story_cut_marks() if self._story_runs is not None else getattr(self, '_cut_marks', None)
        if not marks or self._duration <= 0:
            return None
        clip_rect = self._get_clip_rect()
        if y > clip_rect.y() + config.S(18):
            return None
        vis_dur = self.visible_duration()
        best = None
        for t, n in marks:
            mx = clip_rect.x() + ((t - self._view_start_time) / vis_dur) * clip_rect.width()
            d = abs(mx - x)
            if d <= config.S(7) and (best is None or d < best[0]):
                best = (d, n)
        return best[1] if best else None

    def set_words_data(self, words_data: Optional[List[Dict[str, Any]]]) -> None:
        """
        Passes transcript words to calculate visual cut ranges (Solid vs Hatched).
        """
        self._words_data = words_data or []
        self._recalculate_cut_ranges()
        self._needs_pixmap_rebuild = True
        self.update()

    def _recalculate_cut_ranges(self) -> None:
        """Extracts solid clip segments and cut ranges directly from engine calculation."""
        self._cut_ranges.clear()
        self._timeline_segments = []

        total_dur = self._src_duration if self._src_duration > 0 else 0.0

        if not self._words_data or not self.main_window or not hasattr(self.main_window, "engine"):
            if total_dur > 0:
                self._timeline_segments = [{
                    "start_s": 0.0,
                    "end_s": total_dur,
                    "type": "normal",
                    "is_cut": False,
                    "color": RESOLVE_CLIP_COLORS["normal"],
                }]
            return

        prefs = self.main_window.engine.load_preferences() or {}
        if hasattr(self.main_window, 'tgl_silence_cut'):
            prefs['silence_cut'] = self.main_window.tgl_silence_cut.isChecked()
        if hasattr(self.main_window, 'tgl_silence_mark'):
            prefs['silence_mark'] = self.main_window.tgl_silence_mark.isChecked()
        if hasattr(self.main_window, 'tgl_show_inaudible'):
            prefs['show_inaudible'] = self.main_window.tgl_show_inaudible.isChecked()
        if hasattr(self.main_window, 'tgl_mark_inaudible'):
            prefs['mark_inaudible'] = self.main_window.tgl_mark_inaudible.isChecked()
        if hasattr(self.main_window, 'tgl_show_typos'):
            prefs['show_typos'] = self.main_window.tgl_show_typos.isChecked()
        if hasattr(self.main_window, 'color_cut_buttons'):
            auto_cut = [c_name for c_name, btn in self.main_window.color_cut_buttons.items() if btn.isChecked()]
            prefs['auto_cut_colors'] = auto_cut

        fps = 60.0
        if hasattr(self.main_window.engine, 'resolve_handler') and self.main_window.engine.resolve_handler:
            fps = getattr(self.main_window.engine.resolve_handler, 'fps', 60.0) or 60.0

        surviving_ops, cut_ops = self.main_window.engine.calculate_timeline_structure_with_cuts(
            self._words_data, fps, prefs
        )
        if hasattr(self.main_window, '_update_silence_stats'):
            self.main_window._update_silence_stats(surviving_ops, cut_ops, total_dur)
        if hasattr(self.main_window, '_update_color_legend'):
            self.main_window._update_color_legend(surviving_ops, cut_ops)
        if hasattr(self.main_window, '_update_text_cut_preview'):
            self.main_window._update_text_cut_preview(surviving_ops)
        if hasattr(self.main_window, '_update_edit_playlist'):
            self.main_window._update_edit_playlist(surviving_ops, fps)

        raw_segments = []
        for op in surviving_ops:
            s_s = max(0.0, float(op.get('start_s', 0.0)))
            e_s = float(op.get('end_s', 0.0))
            if total_dur > 0:
                e_s = min(total_dur, e_s)
            if e_s <= s_s:
                continue
            op_type = (op.get('type') or 'normal').lower()
            col = RESOLVE_CLIP_COLORS.get(op_type, RESOLVE_CLIP_COLORS["normal"])
            raw_segments.append({
                "start_s": s_s,
                "end_s": e_s,
                "type": op_type,
                "is_cut": False,
                "color": col,
            })
            self._cut_ranges.append((s_s, e_s, f"{op_type}_solid"))

        for op in cut_ops:
            s_s = max(0.0, float(op.get('start_s', 0.0)))
            e_s = float(op.get('end_s', 0.0))
            if total_dur > 0:
                e_s = min(total_dur, e_s)
            if e_s <= s_s:
                continue
            cut_type = str(op.get('cut_type', 'silence_cut')).lower()
            raw_segments.append({
                "start_s": s_s,
                "end_s": e_s,
                "type": cut_type,
                "is_cut": True,
                "color": CUT_VOID_COLOR,
            })
            self._cut_ranges.append((s_s, e_s, cut_type))

        raw_segments.sort(key=lambda s: s["start_s"])

        # Fill any gaps with 'normal' clips so the entire timeline has a solid continuous base
        cur_pos = 0.0
        filled = []
        for seg in raw_segments:
            if seg["start_s"] > cur_pos + 0.001:
                filled.append({
                    "start_s": cur_pos,
                    "end_s": seg["start_s"],
                    "type": "normal",
                    "is_cut": False,
                    "color": RESOLVE_CLIP_COLORS["normal"],
                })
            filled.append(seg)
            cur_pos = max(cur_pos, seg["end_s"])

        if total_dur > cur_pos + 0.001:
            filled.append({
                "start_s": cur_pos,
                "end_s": total_dur,
                "type": "normal",
                "is_cut": False,
                "color": RESOLVE_CLIP_COLORS["normal"],
            })

        self._timeline_segments = filled

    def set_playhead_time(self, seconds: float) -> None:
        """
        Updates the playhead position in seconds and requests an ultra-fast repaint.
        Called on playback tick (60 FPS) without rebuilding the waveform pixmap!
        """
        seconds = max(0.0, seconds)
        if self._duration > 0:
            seconds = min(self._duration, seconds)

        if abs(self._playhead_time - seconds) > 0.001:
            self._playhead_time = seconds

            # Auto-scroll if keep centered is checked or playhead leaves viewport during playback
            if self._duration > 0 and self._zoom_level > 1.01:
                vis_dur = self.visible_duration()
                t0 = self._view_start_time
                t1 = t0 + vis_dur

                keep_centered = False
                if self.main_window and hasattr(self.main_window, 'audio_preview') and self.main_window.audio_preview:
                    keep_centered = getattr(self.main_window.audio_preview.tgl_centered, 'isChecked', lambda: False)()

                if keep_centered:
                    if seconds < t0 + vis_dur * 0.2 or seconds > t0 + vis_dur * 0.8:
                        self.scroll_to_time(seconds - vis_dur * 0.5)
                else:
                    if seconds > t1 or seconds < t0:
                        self.scroll_to_time(seconds - vis_dur * 0.1)

            self.update()

    def set_clip_info(self, clip_name: str, duration: Optional[float] = None) -> None:
        """Updates clip label or duration badge."""
        self._clip_name = clip_name
        if duration is not None and duration > 0:
            self._src_duration = duration
            if self._story_runs is None:
                self._duration = duration
        self._needs_pixmap_rebuild = True
        self.update()

    # Alias for backward compatibility
    set_track_info = set_clip_info

    def clear(self) -> None:
        """Resets waveform data and playhead state."""
        self._peaks = None
        self._audio_path = None
        self._duration = 0.0
        self._src_duration = 0.0
        self._story_peaks = None
        self._playhead_time = 0.0
        self._words_data = None
        self._cut_ranges.clear()
        self._zoom_level = 1.0
        self._view_start_time = 0.0
        self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    # -------------------------------------------------------------------------
    # Zoom & Viewport Pan API
    # -------------------------------------------------------------------------

    def visible_duration(self) -> float:
        """Returns the time duration currently visible in the timeline viewport."""
        if self._duration <= 0.0:
            return 1.0
        return max(0.2, self._duration / max(1.0, self._zoom_level))

    def set_zoom(self, zoom: float, center_time: Optional[float] = None) -> None:
        """
        Sets zoom level and maintains relative cursor position in viewport.
        Allows zooming in so deeply that individual peaks/nodes can be seen (down to 0.2s visible).
        """
        if self._duration <= 0:
            self._zoom_level = 1.0
            self._view_start_time = 0.0
            self._sync_scrollbar()
            self._needs_pixmap_rebuild = True
            self.update()
            return

        max_zoom = max(1.0, self._duration / 0.2)
        new_zoom = max(1.0, min(max_zoom, zoom))

        old_vis_dur = self.visible_duration()
        new_vis_dur = max(0.2, self._duration / new_zoom)

        if center_time is None:
            center_time = self._playhead_time

        ratio = (center_time - self._view_start_time) / max(0.001, old_vis_dur)
        ratio = max(0.0, min(1.0, ratio))
        self._view_start_time = center_time - ratio * new_vis_dur

        max_start = max(0.0, self._duration - new_vis_dur)
        self._view_start_time = max(0.0, min(max_start, self._view_start_time))
        self._zoom_level = new_zoom

        self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom_level * 1.5, center_time=self._playhead_time)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom_level / 1.5, center_time=self._playhead_time)

    def zoom_fit(self) -> None:
        self.set_zoom(1.0, center_time=0.0)

    def scroll_to_time(self, t: float) -> None:
        vis_dur = self.visible_duration()
        max_start = max(0.0, self._duration - vis_dur)
        self._view_start_time = max(0.0, min(max_start, t))
        self._sync_scrollbar()
        self._needs_pixmap_rebuild = True
        self.update()

    def _sync_scrollbar(self) -> None:
        if not hasattr(self, 'scrollbar'):
            return
        vis_dur = self.visible_duration()
        max_start = max(0.0, self._duration - vis_dur)
        if max_start <= 0.001 or self._zoom_level <= 1.01:
            self.scrollbar.setEnabled(False)
            self.scrollbar.blockSignals(True)
            self.scrollbar.setPageStep(1000)
            self.scrollbar.setValue(0)
            self.scrollbar.blockSignals(False)
            self.scrollbar.hide()
        else:
            self.scrollbar.setEnabled(True)
            self.scrollbar.show()
            page_ratio = min(1.0, vis_dur / max(0.001, self._duration))
            page_step = int(page_ratio * 1000)
            val = int((self._view_start_time / max_start) * (1000 - page_step))
            self.scrollbar.blockSignals(True)
            self.scrollbar.setPageStep(page_step)
            self.scrollbar.setValue(max(0, min(1000 - page_step, val)))
            self.scrollbar.blockSignals(False)

    def _on_scrollbar_changed(self, value: int) -> None:
        vis_dur = self.visible_duration()
        max_start = max(0.0, self._duration - vis_dur)
        if max_start > 0:
            max_val = max(1, 1000 - self.scrollbar.pageStep())
            pct = max(0.0, min(1.0, value / float(max_val)))
            self._view_start_time = pct * max_start
            self._needs_pixmap_rebuild = True
            self.update()

    # -------------------------------------------------------------------------
    # Worker callbacks
    # -------------------------------------------------------------------------

    def _on_peaks_ready(self, peaks: WaveformPeaks, audio_path: str) -> None:
        if self._audio_path == audio_path:
            self._peaks = peaks
            self._src_duration = peaks.duration
            self._story_peaks = None
            if self._story_runs is None:
                self._duration = peaks.duration
            self._recalculate_cut_ranges()
            self._zoom_level = 1.0
            self._view_start_time = 0.0
            self._sync_scrollbar()
            self._needs_pixmap_rebuild = True
            self.update()

    def _on_peaks_error(self, err_msg: str) -> None:
        log_info(f"WaveformTimelineWidget: peak extraction notice: {err_msg}")
        self._needs_pixmap_rebuild = True
        self.update()

    # -------------------------------------------------------------------------
    # Pre-rendered Waveform Cache (QPixmap)
    # -------------------------------------------------------------------------

    def _header_top(self) -> int:
        """Top of the header pills inside the card."""
        return self._PAD - config.S(2)

    def _header_h(self) -> int:
        """Height of the card's header row (clip chip + zoom pills) including the gap below it."""
        return self._header_top() + self._PILL_H + config.S(8)

    def _get_clip_rect(self) -> QRectF:
        """Calculates the Fairlight clip ribbon rectangle inside the widget with breathing room."""
        m_x = float(self._PAD)
        m_y_top = float(self._header_h())  # Header row: clip chip (left) + zoom pills (right)
        m_y_bot = float(self._PAD)
        sb_h = float(config.S(7) + config.S(6)) if (hasattr(self, 'scrollbar') and self.scrollbar.isVisible()) else 0.0

        w = max(10.0, self.width() - 2 * m_x)
        h = max(10.0, self.height() - m_y_top - m_y_bot - sb_h)
        return QRectF(m_x, m_y_top, w, h)

    def _rebuild_waveform_pixmap(self) -> None:
        """
        Pre-renders the Fairlight clip background, waveform geometry, cut hatching,
        and bottom clip badge into a QPixmap.
        Executes only on resize or data changes, keeping paintEvent < 0.2ms!
        """
        w = max(1, self.width())
        h = max(1, self.height())
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0

        pixmap = QPixmap(int(w * dpr), int(h * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        clip_rect = self._get_clip_rect()
        rx = clip_rect.x()
        ry = clip_rect.y()
        rw = clip_rect.width()
        rh = clip_rect.height()
        radius = float(config.S(8))

        from gui import theme as _theme
        modern = _theme.current() != "classic"

        # 1. The card: rounded, hairline border, on the dock surface
        card_rect = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        card_path = QPainterPath()
        card_path.addRoundedRect(card_rect, float(self._CARD_R), float(self._CARD_R))
        painter.fillPath(card_path, qc("#222222"))
        painter.setPen(QPen(qc("#303030"), 1.0))
        painter.drawPath(card_path)

        # 2. Fairlight Audio Clip Ribbon
        clip_path = QPainterPath()
        clip_path.addRoundedRect(clip_rect, radius, radius)

        painter.save()
        painter.setClipPath(clip_path)

        vis_dur = self.visible_duration()
        t_start = self._view_start_time
        t_end = t_start + vis_dur

        # 3. Draw Solid Timeline Segments (DaVinci Resolve Style Solid Colors)
        # Classic keeps Resolve's Fairlight green; the other themes put plain clips on a recessed track
        plain_clip = qc("#1a1a1a") if modern else RESOLVE_CLIP_COLORS["normal"]
        story = self._story_runs is not None
        segments = self._story_segments if story else self._timeline_segments
        peaks = self._ensure_story_peaks() if story else self._peaks
        patch_bg = qc("#0f3d47")
        if not segments:
            # Default normal Fairlight clip
            painter.fillRect(clip_rect, plain_clip)
        else:
            for seg in segments:
                if seg["end_s"] <= t_start or seg["start_s"] >= t_end:
                    continue
                s0 = max(t_start, seg["start_s"])
                s1 = min(t_end, seg["end_s"])
                x0 = rx + ((s0 - t_start) / vis_dur) * rw
                x1 = rx + ((s1 - t_start) / vis_dur) * rw
                seg_w = max(1.0, x1 - x0)
                seg_rect = QRectF(x0, ry, seg_w, rh)

                if seg["is_cut"]:
                    painter.fillRect(seg_rect, qc(CUT_VOID_COLOR.name()))
                    painter.fillRect(seg_rect, QBrush(qc(22, 22, 22), Qt.BDiagPattern))
                elif seg.get("type") == "patch":
                    painter.fillRect(seg_rect, patch_bg)
                    continue                     # one patch is one piece: no seams inside it
                else:
                    seg_col = plain_clip if (modern and seg.get("type") == "normal") else seg["color"]
                    painter.fillRect(seg_rect, seg_col)
                    # Subtle top highlight
                    painter.setPen(QPen(seg_col.lighter(112), 1.0))
                    painter.drawLine(QPointF(x0, ry), QPointF(x1, ry))
                    # Subtle bottom shadow
                    painter.setPen(QPen(seg_col.darker(118), 1.0))
                    painter.drawLine(QPointF(x0, ry + rh), QPointF(x1, ry + rh))

                # Vertical divider seam at cut point (the story marks its block boundaries instead)
                if not story:
                    painter.setPen(QPen(qc(16, 16, 16), 1.0))
                    painter.drawLine(QPointF(x0, ry), QPointF(x0, ry + rh))
                    painter.drawLine(QPointF(x1, ry), QPointF(x1, ry + rh))

        # 4. Subtle horizontal audio midline and dB guide lines
        mid_y = ry + rh / 2.0
        half_h = (rh / 2.0) * 0.88

        # 0dB midline (subtle dark line matching DaVinci Fairlight Screenshot 3)
        midline_pen = QPen(qc(15, 35, 25, 120), 1.0, Qt.SolidLine)
        painter.setPen(midline_pen)
        painter.drawLine(QPointF(rx, mid_y), QPointF(rx + rw, mid_y))

        # 5. Draw Waveform Silhouette (1:1 with DaVinci Resolve Screenshot 3)
        if peaks is not None and peaks.num_points > 0 and self._duration > 0:
            num_cols = max(1, int(rw))

            # Smooth power scaling preserving quiet speech while preventing clipping
            def scale_amp(v: float) -> float:
                if abs(v) < 1e-4:
                    return 0.0
                return float(np.sign(v) * (abs(v) ** 0.62))

            top_pts = []
            bot_pts = []

            for col in range(num_cols):
                t0 = t_start + (col / float(num_cols)) * vis_dur
                t1 = t_start + ((col + 1) / float(num_cols)) * vis_dur

                p_min, p_max, _, _ = peaks.get_visible_peaks(t0, t1)
                if len(p_max) > 0:
                    v_max = float(np.max(p_max))
                    v_min = float(np.min(p_min))
                else:
                    v_max = 0.0
                    v_min = 0.0

                if abs(v_max) < 0.002 and abs(v_min) < 0.002:
                    s_max = 0.003
                    s_min = -0.003
                else:
                    s_max = scale_amp(v_max)
                    s_min = scale_amp(v_min)

                x_val = rx + col + 0.5
                y_top = mid_y - (s_max * half_h)
                y_bot = mid_y - (s_min * half_h)

                if y_bot - y_top < 1.0:
                    y_top = mid_y - 0.5
                    y_bot = mid_y + 0.5

                top_pts.append(QPointF(x_val, y_top))
                bot_pts.append(QPointF(x_val, y_bot))

            if top_pts:
                wf_poly = QPolygonF()
                for pt in top_pts:
                    wf_poly.append(pt)
                for pt in reversed(bot_pts):
                    wf_poly.append(pt)

                # Solid filled continuous silhouette: pale mint (Classic) or the theme accent
                wave_col = qc(config.BTN_BG).lighter(118) if modern else qc("#f0fcf6")
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(wave_col))
                painter.drawPolygon(wf_poly)

                # Clean razor center trace across silence/quiet portions
                painter.setPen(QPen(wave_col, 1.0))
                painter.drawLine(QPointF(rx, mid_y), QPointF(rx + rw, mid_y))

            # The story: voice patches as cyan stretches (a bar on top), block boundaries as seams
            if story:
                cyan = qc(self.PATCH_COLOR)
                for seg in segments:
                    if seg.get("type") != "patch" or seg["end_s"] <= t_start or seg["start_s"] >= t_end:
                        continue
                    x0 = rx + ((max(t_start, seg["start_s"]) - t_start) / vis_dur) * rw
                    x1 = rx + ((min(t_end, seg["end_s"]) - t_start) / vis_dur) * rw
                    tint = QColor(cyan)
                    tint.setAlpha(60)
                    painter.fillRect(QRectF(x0, ry, max(1.0, x1 - x0), rh), tint)
                    painter.fillRect(QRectF(x0, ry, max(1.0, x1 - x0), float(config.S(3))), cyan)
                seam = QPen(qc("#9a9a9a") if modern else qc("#cfe9db"), 1.0)
                seam.setColor(QColor(seam.color().red(), seam.color().green(), seam.color().blue(), 150))
                painter.setPen(seam)
                for bt, _k in self._story_blocks:
                    if t_start <= bt <= t_end:
                        bx = rx + ((bt - t_start) / vis_dur) * rw
                        painter.drawLine(QPointF(bx, ry), QPointF(bx, ry + rh))

            # Mask cut voids & diagonal hatching over the waveform in cut segments
            for seg in segments:
                if not seg["is_cut"]:
                    continue
                if seg["end_s"] <= t_start or seg["start_s"] >= t_end:
                    continue
                s0 = max(t_start, seg["start_s"])
                s1 = min(t_end, seg["end_s"])
                x0 = rx + ((s0 - t_start) / vis_dur) * rw
                x1 = rx + ((s1 - t_start) / vis_dur) * rw
                seg_w = max(1.0, x1 - x0)
                seg_rect = QRectF(x0, ry, seg_w, rh)

                painter.fillRect(seg_rect, qc(14, 14, 14, 235))
                painter.fillRect(seg_rect, QBrush(qc(0, 0, 0, 210), Qt.BDiagPattern))
                painter.setPen(QPen(qc(10, 10, 10, 255), 1.0))
                painter.drawLine(QPointF(x0, ry), QPointF(x0, ry + rh))
                painter.drawLine(QPointF(x1, ry), QPointF(x1, ry + rh))

            painter.restore()
        else:
            # Subtle waiting text
            painter.setPen(qc("#8a8a8a") if modern else qc("#4e735b"))
            placeholder_font = QFont(_theme.ui_font() or config.UI_FONT_NAME, config.FS(9))
            painter.setFont(placeholder_font)
            txt = self.main_window.txt("lbl_timeline_ready", "A1: Ready for timeline audio") if self.main_window and hasattr(self.main_window, "txt") else "A1: Ready for timeline audio"
            painter.drawText(clip_rect, Qt.AlignCenter, txt)
            painter.restore()

        # Ribbon outer border: a hairline (Classic keeps Fairlight's dark green edge)
        border_pen = QPen(qc("#303030") if modern else qc("#1b3b2c"), 1.0)
        painter.setPen(border_pen)
        painter.drawPath(clip_path)

        # 6. Clip Name Chip: a small pill in the card's header row, left of the zoom pills
        badge_font = QFont(_theme.ui_font() or config.UI_FONT_NAME, config.FS(8))
        badge_font.setWeight(QFont.DemiBold)
        painter.setFont(badge_font)

        fm = painter.fontMetrics()
        pill_h = float(self._PILL_H)
        dot_d = float(config.S(6))
        badge_pad_x = float(config.S(10))
        dot_gap = float(config.S(7))
        zoom_w = self.zoom_ctrl_widget.width() if hasattr(self, "zoom_ctrl_widget") else config.S(100)
        max_txt_w = max(0, int(w - 2 * self._PAD - zoom_w - config.S(12) - 2 * badge_pad_x - dot_d - dot_gap))
        chip = self._story_label or self._clip_name
        clip_label = fm.elidedText(chip, Qt.ElideRight, max_txt_w)
        txt_w = fm.horizontalAdvance(clip_label)

        badge_rect = QRectF(
            float(self._PAD),
            float(self._header_top()),
            badge_pad_x * 2 + dot_d + dot_gap + txt_w,
            pill_h,
        )
        badge_path = QPainterPath()
        badge_r = pill_h / 2.0
        badge_path.addRoundedRect(badge_rect.adjusted(0.5, 0.5, -0.5, -0.5), badge_r, badge_r)
        painter.fillPath(badge_path, qc("#2a2a2a"))
        painter.setPen(QPen(qc("#303030"), 1.0))
        painter.drawPath(badge_path)

        # Dot in the waveform's colour ties the chip to the track
        dot_col = qc(config.BTN_BG).lighter(118) if modern else RESOLVE_CLIP_COLORS["normal"].lighter(150)
        if self._story_runs is None and self._story_label:
            dot_col = qc("#f59e0b")               # the Recording switch is on: the chip says so in amber
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(dot_col))
        painter.drawEllipse(QRectF(badge_rect.x() + badge_pad_x, badge_rect.center().y() - dot_d / 2.0, dot_d, dot_d))
        painter.setBrush(Qt.NoBrush)

        painter.setPen(qc("#d4d4d4"))
        text_rect = QRectF(badge_rect.x() + badge_pad_x + dot_d + dot_gap, badge_rect.y(), txt_w + 1.0, pill_h)
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, clip_label)

        painter.end()

        self._waveform_pixmap = pixmap
        self._needs_pixmap_rebuild = False

    # -------------------------------------------------------------------------
    # Qt Event Handlers
    # -------------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        sb_h = config.S(7)
        m_x = self._PAD
        if hasattr(self, 'scrollbar'):
            self.scrollbar.setGeometry(
                m_x,
                self.height() - sb_h - self._PAD,
                max(10, self.width() - 2 * m_x),
                sb_h,
            )
            self._sync_scrollbar()
        if hasattr(self, 'zoom_ctrl_widget'):
            # Header row, right: sized to the three pills
            ctrl = self.zoom_ctrl_widget
            ctrl_w = max(ctrl.sizeHint().width(), ctrl.layout().sizeHint().width() if ctrl.layout() else 0)
            ctrl_h = self._PILL_H
            ctrl.setGeometry(
                self.width() - ctrl_w - self._PAD,
                self._header_top(),
                ctrl_w,
                ctrl_h,
            )
        self._needs_pixmap_rebuild = True

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if abs(delta) < 1:
            delta = event.angleDelta().x()
        if delta == 0 or self._duration <= 0:
            return

        clip_rect = self._get_clip_rect()
        pos_x = event.position().x()

        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            # Zoom centered at cursor position
            rx = clip_rect.x()
            rw = clip_rect.width()
            vis_dur = self.visible_duration()
            cursor_ratio = max(0.0, min(1.0, (pos_x - rx) / max(1.0, rw)))
            t_cursor = self._view_start_time + cursor_ratio * vis_dur

            factor = 1.3 if delta > 0 else (1.0 / 1.3)
            self.set_zoom(self._zoom_level * factor, center_time=t_cursor)
        else:
            # Horizontal Pan
            vis_dur = self.visible_duration()
            pan_s = (vis_dur * 0.15) * (-1 if delta > 0 else 1)
            self.scroll_to_time(self._view_start_time + pan_s)

    def paintEvent(self, event) -> None:
        if self._needs_pixmap_rebuild or self._waveform_pixmap is None:
            self._rebuild_waveform_pixmap()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        # 1. Blit pre-rendered waveform pixmap (ultra-fast, ~0.05ms)
        if self._waveform_pixmap is not None:
            painter.drawPixmap(0, 0, self._waveform_pixmap)

        clip_rect = self._get_clip_rect()
        rx = clip_rect.x()
        ry = clip_rect.y()
        rw = clip_rect.width()
        rh = clip_rect.height()

        # 1b. Story cuts: a small violet flag at each, clickable (opens the cut in Review)
        marks = self._story_cut_marks() if self._story_runs is not None else getattr(self, '_cut_marks', None)
        if marks and self._duration > 0:
            vis_dur = self.visible_duration()
            fh = float(config.S(12))
            for t, n in marks:
                if not (self._view_start_time <= t <= self._view_start_time + vis_dur):
                    continue
                mx = rx + ((t - self._view_start_time) / vis_dur) * rw
                painter.setPen(QPen(QColor(168, 85, 247, 170), 1.0))
                painter.drawLine(QPointF(mx, ry), QPointF(mx, ry + rh))
                flag = QPainterPath()
                flag.moveTo(mx, ry + 1)
                flag.lineTo(mx + fh * 0.9, ry + 1 + fh / 2)
                flag.lineTo(mx, ry + 1 + fh)
                flag.closeSubpath()
                painter.fillPath(flag, QColor(168, 85, 247, 230))

        # 2. Draw Red Playhead (matching Screenshot 1: vertical red line + top red circular pin marker)
        if self._duration > 0:
            vis_dur = self.visible_duration()
            t_start = self._view_start_time
            t_end = t_start + vis_dur

            if t_start <= self._playhead_time <= t_end:
                pct = (self._playhead_time - t_start) / vis_dur
                playhead_x = rx + (pct * rw)

                # Vertical red line through the clip
                line_pen = QPen(qc("#ff3b30"), config.S(1.8))
                painter.setPen(line_pen)
                painter.drawLine(QPointF(playhead_x, ry), QPointF(playhead_x, ry + rh))

                # Top red circular marker (radius ~4.5px, matching Screenshot 1!)
                marker_radius = float(config.S(4.5))
                painter.setPen(QPen(qc("#c0392b"), 1.0))
                painter.setBrush(QBrush(qc("#ff3b30")))
                painter.drawEllipse(QPointF(playhead_x, ry + marker_radius), marker_radius, marker_radius)

            # Optional scrubbing time tooltip
            if self._is_scrubbing or self._hover_x is not None:
                hover_t = self._playhead_time if self._is_scrubbing else (
                    max(0.0, min(self._duration, t_start + ((self._hover_x - rx) / rw) * vis_dur))
                    if self._hover_x is not None else self._playhead_time
                )
                hm, hs = divmod(int(hover_t), 60)
                hms = int((hover_t - int(hover_t)) * 100)
                tc_str = f"{hm:02d}:{hs:02d}.{hms:02d}"

                tc_font = QFont("JetBrains Mono", config.FS(7.5))
                if not tc_font.exactMatch():
                    tc_font = QFont("Consolas", config.FS(7.5))
                painter.setFont(tc_font)
                fm = painter.fontMetrics()
                box_w = fm.horizontalAdvance(tc_str) + config.S(8)
                box_h = fm.height() + config.S(2)

                tip_pct = (hover_t - t_start) / vis_dur
                tip_center_x = rx + (tip_pct * rw)
                tip_x = max(rx, min(rx + rw - box_w, tip_center_x - box_w / 2.0))
                tip_y = max(ry + 2, ry + rh - box_h - float(config.S(4)))
                tip_rect = QRectF(tip_x, tip_y, box_w, box_h)

                tip_path = QPainterPath()
                tip_path.addRoundedRect(tip_rect, config.S(3), config.S(3))
                painter.fillPath(tip_path, qc(0, 0, 0, 210))
                painter.setPen(QPen(qc("#ff3b30"), 1.0))
                painter.drawPath(tip_path)

                painter.setPen(qc("#ffffff"))
                painter.drawText(tip_rect, Qt.AlignCenter, tc_str)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            n = self._cut_mark_at(event.position().x(), event.position().y())
            if n is not None:
                self.cut_mark_clicked.emit(n)
                return
            clip_rect = self._get_clip_rect()
            if clip_rect.contains(event.position()) and self._duration > 0:
                self._is_scrubbing = True
                self.scrub_started.emit()
                is_jump = bool(event.modifiers() & (Qt.ControlModifier | Qt.AltModifier))
                self._update_from_mouse_x(event.position().x(), sync_resolve=is_jump)

    def mouseMoveEvent(self, event) -> None:
        clip_rect = self._get_clip_rect()
        if clip_rect.contains(event.position()):
            self._hover_x = event.position().x()
            if self._is_scrubbing and self._duration > 0:
                self._update_from_mouse_x(event.position().x(), sync_resolve=False)
            else:
                self.update()
        else:
            if self._hover_x is not None:
                self._hover_x = None
                self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._is_scrubbing:
            self._is_scrubbing = False
            self.scrub_finished.emit()
            clip_rect = self._get_clip_rect()
            if self._duration > 0:
                is_jump = bool(event.modifiers() & (Qt.ControlModifier | Qt.AltModifier))
                self._update_from_mouse_x(event.position().x(), sync_resolve=is_jump)
            self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if self._hover_x is not None:
            self._hover_x = None
            self.update()

    def _update_from_mouse_x(self, mouse_x: float, sync_resolve: bool = False) -> None:
        clip_rect = self._get_clip_rect()
        rx = clip_rect.x()
        rw = clip_rect.width()
        if rw <= 0 or self._duration <= 0:
            return

        vis_dur = self.visible_duration()
        pct = max(0.0, min(1.0, (mouse_x - rx) / rw))
        target_t = self._view_start_time + (pct * vis_dur)
        target_t = max(0.0, min(self._duration, target_t))
        self._playhead_time = target_t
        self.update()

        # Local immediate seek (60 FPS playback / video frame update). The player maps story time back to
        # the recording, and moves Resolve's playhead too on Ctrl/Alt + click (with the recording's time)
        self.seek_requested.emit(target_t, sync_resolve)

