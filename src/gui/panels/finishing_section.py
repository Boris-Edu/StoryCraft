#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MODULE: finishing_section.py
ROLE: GUI section of the main panel
DESCRIPTION:
"After sending to Resolve": what StoryCraft does to the new timeline once it is built: speech
cleanup (Voice Isolation), captions (in Resolve and/or an SRT file), extra exports for Premiere and
Final Cut, and cameras (by speaker, or Resolve's SmartSwitch). Every choice is a saved preference;
the engine reads them in finishing_options().
"""

import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QFileDialog

import config
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton, ToggleSwitch
from gui.widgets.segmented import SegmentedChoice


def build_finishing_section(win):
    """Returns (header button, body widget). The body starts collapsed."""
    t = win.txt
    prefs = win.engine.load_preferences() or {}

    def save(**kv):
        win.engine.save_preferences(kv)

    head = QPushButton("▸  " + t("fin_title"))
    head.setCursor(Qt.PointingHandCursor)
    head.setToolTip(t("tt_fin_title"))
    head.setStyleSheet(f"""
        QPushButton {{ background: transparent; border: none; color: #9a9a9a; text-align: left;
            padding: {config.S(4)}px 0; font-size: {config.FS(9)}pt; font-weight: bold; }}
        QPushButton:hover {{ color: #ffffff; }}
    """)
    body = QWidget()
    body.setStyleSheet("background: transparent;")
    lay = QVBoxLayout(body)
    lay.setContentsMargins(0, config.S(4), 0, config.S(8))
    lay.setSpacing(config.S(8))
    small = f"color: #a0a0a0; font-size: {config.FS(8.5)}pt; background: transparent;"
    combo_css = f"""
        QComboBox {{ background: #2a2a2a; color: #d0d0d0; border: 1px solid #3a3a3a; border-radius: {config.S(4)}px;
            padding: {config.S(2)}px {config.S(6)}px; font-size: {config.FS(8.5)}pt; }}
        QComboBox QAbstractItemView {{ background: #222222; color: #e6e6e6; selection-background-color: #23a559; }}"""

    def row(label_key, tip_key, widget, extra=None):
        h = QHBoxLayout()
        h.setSpacing(config.S(6))
        lbl = QLabel(t(label_key))
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-size: {config.FS(9)}pt; background: transparent;")
        lbl.setToolTip(t(tip_key))
        widget.setToolTip(t(tip_key))
        h.addWidget(lbl, 1)
        if extra is not None:
            h.addWidget(extra)
        h.addWidget(widget)
        lay.addLayout(h)

    def toggle(key, default=False):
        tg = ToggleSwitch()
        tg.setChecked(bool(prefs.get(key, default)))
        tg.toggled.connect(lambda c, k=key: save(**{k: bool(c)}))
        return tg

    # Keep Resolve in sync: the last sent timeline follows the edit (never a hand-edited one)
    def _sync(c):
        save(resolve_sync=bool(c))
        if c and hasattr(win, '_update_send_state'):
            win._sync_paused = None
            win._update_send_state()
    sync = ToggleSwitch()
    sync.setChecked(bool(prefs.get('resolve_sync')))
    sync.toggled.connect(_sync)
    row("fin_sync", "tt_fin_sync", sync)

    # Speech cleanup: Resolve's Voice Isolation on the dialogue tracks (Studio)
    amount = QComboBox()
    for v in (25, 50, 75, 100):
        amount.addItem(f"{v}%", v)
    cur = int(prefs.get('voice_isolation', 0) or 0)
    amount.setCurrentIndex(max(0, [25, 50, 75, 100].index(cur)) if cur in (25, 50, 75, 100) else 1)
    amount.setStyleSheet(combo_css)
    amount.setEnabled(cur > 0)
    vi = ToggleSwitch()
    vi.setChecked(cur > 0)

    def _vi(checked):
        amount.setEnabled(checked)
        save(voice_isolation=int(amount.currentData()) if checked else 0)
    vi.toggled.connect(_vi)
    amount.currentIndexChanged.connect(lambda _i: save(voice_isolation=int(amount.currentData())) if vi.isChecked() else None)
    row("fin_voice", "tt_fin_voice", vi, amount)

    # Captions
    row("fin_captions_resolve", "tt_fin_captions_resolve", toggle('captions_resolve'))
    row("fin_captions_srt", "tt_fin_captions_srt", toggle('captions_srt'))
    row("fin_captions_names", "tt_fin_captions_names", toggle('captions_names'))

    # Cameras
    cam = SegmentedChoice([("off", t("fin_cam_off")), ("speaker", t("fin_cam_speaker")),
                           ("smartswitch", t("fin_cam_smart"))], prefs.get('camera_mode') or "off", compact=True)
    def _cam(m):
        save(camera_mode=None if m == "off" else m)
        if hasattr(win, '_refresh_speakers_card'):
            win._refresh_speakers_card()  # shows or hides each speaker's camera
    cam.changed.connect(_cam)
    lbl_cam = QLabel(t("fin_cameras"))
    lbl_cam.setStyleSheet(f"font-size: {config.FS(9)}pt; background: transparent;")
    lbl_cam.setToolTip(t("tt_fin_cameras"))
    cam.setToolTip(t("tt_fin_cameras"))
    lay.addWidget(lbl_cam)
    lay.addWidget(cam)

    # Extra exports of the new timeline
    lbl_exp = QLabel(t("fin_exports"))
    lbl_exp.setStyleSheet(f"font-size: {config.FS(9)}pt; background: transparent;")
    lbl_exp.setToolTip(t("tt_fin_exports"))
    lay.addWidget(lbl_exp)
    chosen = set(prefs.get('export_formats', []) or [])
    chips = QHBoxLayout()
    chips.setSpacing(config.S(4))
    chip_css = f"""
        QPushButton {{ background: #2a2a2a; color: #b8b8b8; border: 1px solid #3a3a3a; border-radius: {config.S(10)}px;
            padding: {config.S(2)}px {config.S(8)}px; font-size: {config.FS(8.5)}pt; }}
        QPushButton:checked {{ color: #ffffff; border-color: #23a559; background: #1d2a22; }}"""
    buttons = {}

    def _save_exports():
        save(export_formats=[k for k, b in buttons.items() if b.isChecked()])
    for key, label_key in (("fcp7", "fin_exp_premiere"), ("fcpxml", "fin_exp_fcp"), ("otio", "fin_exp_otio"),
                           ("edl", "fin_exp_edl")):
        b = QPushButton(t(label_key))
        b.setCheckable(True)
        b.setChecked(key in chosen)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(chip_css)
        b.setToolTip(t("tt_" + label_key))
        b.toggled.connect(lambda _c: _save_exports())
        buttons[key] = b
        chips.addWidget(b)
    chips.addStretch()
    lay.addLayout(chips)

    folder = QLabel("")
    folder.setWordWrap(True)
    folder.setStyleSheet(small)

    def _show_folder():
        folder.setText(t("fin_folder").format(path=win.engine.export_folder()))
    _show_folder()
    change = QPushButton(t("fin_folder_change"))
    change.setCursor(Qt.PointingHandCursor)
    change.setStyleSheet(chip_css)

    def _pick():
        start = win.engine.export_folder()
        path = QFileDialog.getExistingDirectory(win, t("fin_folder_change"), start if os.path.isdir(start) else os.path.expanduser("~"))
        if path:
            save(export_folder=path)
            _show_folder()
    change.clicked.connect(_pick)
    fh = QHBoxLayout()
    fh.addWidget(folder, 1)
    fh.addWidget(change, 0, Qt.AlignTop)
    lay.addLayout(fh)

    body.setVisible(False)

    def _toggle():
        body.setVisible(not body.isVisible())
        head.setText(("▾  " if body.isVisible() else "▸  ") + t("fin_title"))
    head.clicked.connect(_toggle)
    win.finishing_body = body
    return head, body
