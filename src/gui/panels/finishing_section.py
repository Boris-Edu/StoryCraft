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
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QFileDialog, QFrame

import config
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton, ToggleSwitch
from gui.widgets.segmented import SegmentedChoice
from .script_panel import (disclosure_css, combo_css, card_css, chip_css, quiet_button, style_segmented,
                           sublabel_css, hairline, text_px)


def build_finishing_section(win):
    """Returns (header button, body widget). The body starts collapsed."""
    t = win.txt
    prefs = win.engine.load_preferences() or {}

    def save(**kv):
        win.engine.save_preferences(kv)

    head = QPushButton("▸  " + t("fin_title"))
    head.setCursor(Qt.PointingHandCursor)
    head.setToolTip(t("tt_fin_title"))
    head.setStyleSheet(disclosure_css(9.5, compact=True))
    head.setMinimumWidth(config.S(120))  # long translations: cut at the edge, never wider than the panel
    # The options open as a card under their header
    body = QFrame()
    body.setObjectName("FinishingCard")
    body.setStyleSheet(card_css("FinishingCard"))
    lay = QVBoxLayout(body)
    lay.setContentsMargins(config.S(14), config.S(12), config.S(14), config.S(14))
    lay.setSpacing(config.S(10))
    small = sublabel_css(9)
    _combo_css = combo_css(9)
    _ctl_h = max(config.S(28), text_px(9) + 2 * config.S(4) + 2)

    def row(label_key, tip_key, widget, extra=None):
        h = QHBoxLayout()
        h.setSpacing(config.S(8))
        lbl = QLabel(t(label_key))
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9.5)}pt; background: transparent;")
        lbl.setToolTip(t(tip_key))
        widget.setToolTip(t(tip_key))
        h.addWidget(lbl, 1)
        if extra is not None:
            h.addWidget(extra, 0, Qt.AlignVCenter)
        h.addWidget(widget, 0, Qt.AlignVCenter)
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
    amount.setStyleSheet(_combo_css)
    amount.setMinimumHeight(_ctl_h)
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
    lay.addWidget(hairline())
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
    style_segmented(cam.buttons.values(), 9, stretch=True)
    lay.addWidget(hairline())
    lbl_cam = QLabel(t("fin_cameras"))
    lbl_cam.setWordWrap(True)
    lbl_cam.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9.5)}pt; background: transparent;")
    lbl_cam.setToolTip(t("tt_fin_cameras"))
    cam.setToolTip(t("tt_fin_cameras"))
    lay.addWidget(lbl_cam)
    lay.addWidget(cam)

    # Extra exports of the new timeline
    lay.addWidget(hairline())
    lbl_exp = QLabel(t("fin_exports"))
    lbl_exp.setWordWrap(True)
    lbl_exp.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9.5)}pt; background: transparent;")
    lbl_exp.setToolTip(t("tt_fin_exports"))
    lay.addWidget(lbl_exp)
    chosen = set(prefs.get('export_formats', []) or [])
    from .script_panel import ButtonFlow
    _chip_h = text_px(8.5) + 2 * config.S(5)
    _chip_css = chip_css(_chip_h, 8.5)
    buttons = {}

    def _save_exports():
        save(export_formats=[k for k, b in buttons.items() if b.isChecked()])
    for key, label_key in (("fcp7", "fin_exp_premiere"), ("fcpxml", "fin_exp_fcp"), ("otio", "fin_exp_otio"),
                           ("edl", "fin_exp_edl")):
        b = QPushButton(t(label_key))
        b.setCheckable(True)
        b.setChecked(key in chosen)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(_chip_css)
        b.setFixedHeight(_chip_h)
        b.setToolTip(t("tt_" + label_key))
        b.toggled.connect(lambda _c: _save_exports())
        buttons[key] = b
    # Pills two or four to a row, whichever fits (four in a row overflowed a narrow panel)
    lay.addWidget(ButtonFlow(list(buttons.values()), max_cols=4, spacing=6))

    folder = QLabel("")
    folder.setWordWrap(True)
    folder.setStyleSheet(small)

    def _show_folder():
        folder.setText(t("fin_folder").format(path=win.engine.export_folder()))
    _show_folder()
    change = QPushButton(t("fin_folder_change"))
    change.setCursor(Qt.PointingHandCursor)
    quiet_button(change, 9, 28, 4, 10)

    def _pick():
        start = win.engine.export_folder()
        path = QFileDialog.getExistingDirectory(win, t("fin_folder_change"), start if os.path.isdir(start) else os.path.expanduser("~"))
        if path:
            save(export_folder=path)
            _show_folder()
    change.clicked.connect(_pick)
    fh = QHBoxLayout()
    fh.setSpacing(config.S(8))
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
