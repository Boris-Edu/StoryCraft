#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: assembly_panel.py
ROLE: GUI Activity Panel
DESCRIPTION:
Sidebar activity panel for timeline assembly options, inaudible/typo toggles, cut color dynamic list, and auto-cut presets.
"""

import os
from PySide6.QtCore import Qt, QSize, QObject, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame
)
from PySide6.QtGui import QIcon

import config
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton, ToggleSwitch, StarFavoriteButton
from .script_panel import (wrap_activity_panel, panel_card, panel_margins, heading_css, quiet_button_css,
                           combo_css, disclosure_css, hairline, style_segmented, text_px)


class _SegmentedRestyler(QObject):
    """The colour rows are rebuilt by the main window: give their Keep / Mark / Cut / Bleep choices
    the panel's segmented look (sized from the text) whenever new rows arrive."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.LayoutRequest:
            from gui.widgets.segmented import SegmentedChoice
            for seg in obj.findChildren(SegmentedChoice):
                if not seg.property("_inspector_styled"):
                    seg.setProperty("_inspector_styled", True)
                    style_segmented(seg.buttons.values(), 8.5)
        return False


def build_assembly_panel(win) -> QFrame:
    """Build the Assembly activity panel and bind widgets to main window."""
    p_assembly = QWidget()
    l_assembly = QVBoxLayout(p_assembly)
    panel_margins(l_assembly, 12)

    def _pin_btn(fav_id: str):
        btn = StarFavoriteButton(size=18)
        win._pin_buttons[fav_id] = btn
        return btn
    
    # Colours in this edit: what each colour means, how much of it there is, and
    # whether the next Assemble leaves it out. Rows are filled by win._render_color_legend().
    def _card():
        return panel_card(10)

    def _heading(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(heading_css(12))
        return lbl

    lbl_legend = _heading(win.txt("legend_title"))
    l_assembly.addSpacing(config.S(4))
    l_assembly.addWidget(lbl_legend)
    card_legend, l_card_legend = _card()
    win.legend_layout = QVBoxLayout()
    win.legend_layout.setSpacing(config.S(12))
    l_card_legend.addLayout(win.legend_layout)
    l_assembly.addWidget(card_legend)
    card_legend._seg_restyler = _SegmentedRestyler(card_legend)
    card_legend.installEventFilter(card_legend._seg_restyler)
    win._legend_signature = None
    win._render_color_legend({})
    # What the AI found: one row per kind, with its count, its switches and Restore.
    # Placed above the colours: first what was detected, then what happens to each colour.
    lbl_det = _heading(win.txt("det_title"))
    card_det, l_card_det = _card()
    l_assembly.insertWidget(0, lbl_det)
    l_assembly.insertWidget(1, card_det)
    win.det_counts = {}
    _small_btn = quiet_button_css(9, 4, 10)
    _small_h = max(config.S(28), text_px(9) + 2 * config.S(4) + 2)
    _row_title_css = f"color: #ffffff; font-size: {config.FS(10)}pt; font-weight: 600; background: transparent;"

    def _small(btn):
        btn.setStyleSheet(_small_btn)
        btn.setMinimumHeight(_small_h)
        return btn

    def _det_row(kind, title_key):
        if l_card_det.count():
            l_card_det.addWidget(hairline())   # rows apart, without a box around each
        box = QVBoxLayout()
        box.setSpacing(config.S(6))
        top = QHBoxLayout()
        top.setSpacing(config.S(8))
        title = QLabel(win.txt(title_key))
        title.setWordWrap(True)
        title.setStyleSheet(_row_title_css)
        top.addWidget(title, 1)
        count = QLabel("")
        count.setStyleSheet(f"color: #8a8a8a; font-size: {config.FS(9)}pt; background: transparent;")
        win.det_counts[kind] = count
        box.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(config.S(6))
        bottom.addWidget(count)
        bottom.addStretch()
        restore = QPushButton(win.txt("btn_det_restore"))
        restore.setCursor(Qt.PointingHandCursor)
        restore.setToolTip(win.txt("tt_det_restore"))
        _small(restore)
        restore.clicked.connect(lambda: win._restore_ai_kind(kind))
        bottom.addWidget(restore)
        box.addLayout(bottom)
        return box, top, bottom

    # Fillers: the auto-filler switch (mirrors the Filler Words panel) and a link to the list
    box, top, bottom = _det_row("fillers", "det_fillers")
    tgl_fill = ToggleSwitch()
    src = getattr(win, 'tgl_auto_filler', None)
    if src is not None:
        tgl_fill.setChecked(src.isChecked())
        tgl_fill.toggled.connect(lambda c: src.setChecked(c) if src.isChecked() != c else None)
        src.toggled.connect(lambda c: tgl_fill.setChecked(c) if tgl_fill.isChecked() != c else None)
    tgl_fill.setToolTip(win.txt("tt_det_fillers"))
    top.addWidget(tgl_fill)
    edit_list = QPushButton(win.txt("btn_det_edit_list"))
    edit_list.setCursor(Qt.PointingHandCursor)
    _small(edit_list)
    edit_list.clicked.connect(lambda: win._open_activity("fillers"))
    bottom.insertWidget(2, edit_list)
    l_card_det.addLayout(box)

    # Errors & retakes: run the detector, or restore what it found
    box, top, bottom = _det_row("errors", "det_errors")
    detect = QPushButton(win.txt("btn_det_detect"))
    detect.setCursor(Qt.PointingHandCursor)
    detect.setToolTip(win.txt("tt_det_detect"))
    _small(detect)
    detect.clicked.connect(lambda: win._on_analyze_standalone())
    top.addWidget(detect)
    l_card_det.addLayout(box)

    # Repeated words ("I think I think") and restarts: every abandoned copy, as Retake
    box, top, bottom = _det_row("stammers", "det_stammers")
    det_st = QPushButton(win.txt("btn_det_detect"))
    det_st.setCursor(Qt.PointingHandCursor)
    det_st.setToolTip(win.txt("tt_det_stammers"))
    _small(det_st)
    det_st.clicked.connect(lambda: win._detect_stammers())
    top.addWidget(det_st)
    l_card_det.addLayout(box)

    # Swear words: marked with their own marker, bleeped by default (Keep / Mark / Cut / Bleep in the legend)
    box, top, bottom = _det_row("swears", "det_swears")
    det_sw = QPushButton(win.txt("btn_det_detect"))
    det_sw.setCursor(Qt.PointingHandCursor)
    det_sw.setToolTip(win.txt("tt_det_swears"))
    _small(det_sw)
    det_sw.clicked.connect(lambda: win._detect_swears())
    top.addWidget(det_sw)
    l_card_det.addLayout(box)

    # To voice patch: what the Voice tab lists (endings that sound unfinished, hard to understand,
    # incoherent, tagged by you). Mark them puts the To voice patch marker on those words (one undo
    # step; it is a Keep colour, so the edit doesn't change); Open Voice goes there to patch them.
    l_card_det.addWidget(hairline())
    v_box = QVBoxLayout()
    v_box.setSpacing(config.S(6))
    v_top = QHBoxLayout()
    v_top.setSpacing(config.S(8))
    v_title = QLabel(win.txt("det_voice", "To voice patch"))
    v_title.setWordWrap(True)
    v_title.setStyleSheet(_row_title_css)
    v_title.setToolTip(win.txt("tt_det_voice", "Passages a voice patch could fix: endings that sound unfinished, "
                               "sentences that are hard to understand or incoherent, and words you marked "
                               "To voice patch."))
    v_top.addWidget(v_title, 1)
    v_mark = QPushButton(win.txt("btn_det_voice_mark", "Mark them"))
    v_mark.setCursor(Qt.PointingHandCursor)
    v_mark.setToolTip(win.txt("tt_det_voice_mark", "Puts the To voice patch marker on these words (it keeps "
                              "them in the story and colours nothing in Resolve). Undo takes it off."))
    _small(v_mark)
    v_mark.clicked.connect(lambda: win._mark_voice_candidates())
    win.btn_voice_mark = v_mark
    v_top.addWidget(v_mark)
    v_box.addLayout(v_top)
    v_bottom = QHBoxLayout()
    v_bottom.setSpacing(config.S(6))
    win.det_voice_count = QLabel("")
    win.det_voice_count.setWordWrap(True)
    win.det_voice_count.setStyleSheet(f"color: #8a8a8a; font-size: {config.FS(9)}pt; background: transparent;")
    v_bottom.addWidget(win.det_voice_count, 1)
    v_open = QPushButton(win.txt("btn_det_voice_open", "Open Voice patch"))
    v_open.setCursor(Qt.PointingHandCursor)
    v_open.setToolTip(win.txt("tt_det_voice_open", "Make the patches with VoiceStudio."))
    _small(v_open)
    v_open.clicked.connect(lambda: win.show_voice())
    v_bottom.addWidget(v_open)
    v_box.addLayout(v_bottom)
    l_card_det.addLayout(v_box)
    if hasattr(win, '_refresh_voice_found'):
        win._refresh_voice_found()

    # Speakers: find up to 10 voices, then name them, set their role or leave one out of the edit.
    # Rows are filled by win._refresh_speakers_card(); a passage changes speaker from the transcript
    # (select it, right-click > Speaker) or by clicking a name tag.
    from PySide6.QtWidgets import QComboBox
    l_card_det.addWidget(hairline())
    spk_box = QVBoxLayout()
    spk_box.setSpacing(config.S(8))
    spk_top = QHBoxLayout()
    spk_top.setSpacing(config.S(6))
    spk_title = QLabel(win.txt("spk_title"))
    spk_title.setStyleSheet(_row_title_css)
    spk_top.addWidget(spk_title)
    spk_top.addWidget(win._create_info_icon("tt_spk_title"))
    spk_top.addStretch()
    # How many voices and Detect on their own line: "Detecting… 42%" needs the room
    spk_ctl = QHBoxLayout()
    spk_ctl.setSpacing(config.S(6))
    win.cmb_spk_count = QComboBox()
    win.cmb_spk_count.addItem(win.txt("spk_count_auto"), None)
    for n in range(1, 11):  # 1: a single voice, everyone is Speaker 1 (no detection needed)
        win.cmb_spk_count.addItem(str(n), n)
    win.cmb_spk_count.setToolTip(win.txt("tt_spk_count"))
    _saved_n = (win.engine.load_preferences() or {}).get('speaker_count')
    if _saved_n and _saved_n > 1:
        win.cmb_spk_count.setCurrentIndex(max(0, win.cmb_spk_count.findData(_saved_n)))
    win.cmb_spk_count.currentIndexChanged.connect(
        lambda _i: win.engine.save_preferences({"speaker_count": win.cmb_spk_count.currentData()}))
    win.cmb_spk_count.setCursor(Qt.PointingHandCursor)
    win.cmb_spk_count.setStyleSheet(combo_css(9))
    win.cmb_spk_count.setMinimumHeight(_small_h)
    spk_ctl.addWidget(win.cmb_spk_count)
    spk_ctl.addStretch()
    det_spk = QPushButton(win.txt("btn_det_detect"))
    det_spk.setCursor(Qt.PointingHandCursor)
    det_spk.setToolTip(win.txt("tt_det_speakers"))
    _small(det_spk)
    det_spk.clicked.connect(lambda: win._detect_speakers())
    win.btn_det_speakers = det_spk
    spk_ctl.addWidget(det_spk)
    spk_box.addLayout(spk_top)
    spk_box.addLayout(spk_ctl)
    win.speakers_rows = QVBoxLayout()
    win.speakers_rows.setSpacing(config.S(8))
    spk_box.addLayout(win.speakers_rows)
    l_card_det.addLayout(spk_box)

    # Script differences (typos): show or hide
    box, top, bottom = _det_row("typos", "det_typos")
    win.tgl_show_typos = ToggleSwitch()
    win.tgl_show_typos.setChecked(True)
    win.tgl_show_typos.setToolTip(win.txt("lbl_show_detected_typos"))
    win.tgl_show_typos.toggled.connect(lambda c: (win._on_typos_toggled(c), win._save_top_toggles_prefs()))
    top.addWidget(win.tgl_show_typos)
    pin_show_typos = _pin_btn('show_typos')
    top.addWidget(pin_show_typos)
    pin_show_typos.clicked.connect(lambda checked=False, p=pin_show_typos: win._toggle_favorite('show_typos', win.tgl_show_typos, win.txt("tool_show_typos"), p))
    l_card_det.addLayout(box)

    # No voice (inaudible): mark it (brown clips), and whether (...) shows in the text
    box, top, bottom = _det_row("novoice", "det_novoice")
    win.tgl_mark_inaudible = ToggleSwitch()
    win.tgl_mark_inaudible.setToolTip(win.txt("lbl_mark_inaudible_fragments"))
    win.tgl_mark_inaudible.toggled.connect(lambda c: (win._on_mark_inaudible_toggled(c), win._save_top_toggles_prefs()))
    top.addWidget(win.tgl_mark_inaudible)
    pin_mark_inaud = _pin_btn('mark_inaudible')
    top.addWidget(pin_mark_inaud)
    pin_mark_inaud.clicked.connect(lambda checked=False, p=pin_mark_inaud: win._toggle_favorite('mark_inaudible', win.tgl_mark_inaudible, win.txt("tool_mark_inaudible"), p))
    show_row = QHBoxLayout()
    show_row.setSpacing(config.S(6))
    lbl_show = QLabel(win.txt("lbl_show_inaudible_fragments"))
    lbl_show.setWordWrap(True)
    lbl_show.setStyleSheet(f"color: #a3a3a3; font-size: {config.FS(9)}pt; background: transparent;")
    show_row.addWidget(lbl_show, 1)
    win.tgl_show_inaudible = ToggleSwitch()
    win.tgl_show_inaudible.setChecked(True)
    win.tgl_show_inaudible.toggled.connect(lambda c: (win._on_inaudible_toggled(c), win._save_top_toggles_prefs()))
    show_row.addWidget(win.tgl_show_inaudible)
    pin_show_inaud = _pin_btn('show_inaudible')
    show_row.addWidget(pin_show_inaud)
    pin_show_inaud.clicked.connect(lambda checked=False, p=pin_show_inaud: win._toggle_favorite('show_inaudible', win.tgl_show_inaudible, win.txt("tool_show_inaudible"), p))
    box.addLayout(show_row)
    # Sounds without words: listen to each one, keep or cut it (loud ones are cut by themselves)
    sound_row = QHBoxLayout()
    sound_row.setSpacing(config.S(6))
    review = QPushButton(win.txt("btn_review_sounds"))
    review.setCursor(Qt.PointingHandCursor)
    review.setToolTip(win.txt("tt_review_sounds"))
    _small(review)
    review.clicked.connect(lambda: win._review_next_sound())
    sound_row.addWidget(review)
    win.btn_sound_choice = QPushButton(win.txt("btn_sound_cut"))
    win.btn_sound_choice.setCursor(Qt.PointingHandCursor)
    _small(win.btn_sound_choice)
    win.btn_sound_choice.setEnabled(False)
    win.btn_sound_choice.clicked.connect(lambda: win._toggle_sound_choice())
    sound_row.addWidget(win.btn_sound_choice)
    sound_row.addStretch()
    box.addLayout(sound_row)
    l_card_det.addLayout(box)

    win.color_cut_buttons = {}
    
    l_colors_container = QVBoxLayout()
    l_colors_container.setContentsMargins(config.S(14), config.S(10), config.S(10), config.S(10))
    l_colors_container.setSpacing(config.S(8))

    from gui.utils import get_layout_icon_path

    color_idx = 0
    for color_name, color_hex in config.RESOLVE_COLORS_HEX.items():
        row_color = QHBoxLayout()
        row_color.setSpacing(config.S(8))
        
        localized_color_name = win.txt(f"resolve_color_{color_name.lower()}")
        lbl_color = QLabel(win.txt("lbl_cut_color_fmt").format(hex=color_hex, color=localized_color_name))
        row_color.addWidget(lbl_color)
        row_color.addStretch()
        
        pin_c = _pin_btn(f'cut_{color_name.lower()}')
        
        is_unsupported = color_name.lower() in ["tan", "chocolate", "green", "blue"]
        btn_auto = None
        
        if not is_unsupported:
            btn_auto = QPushButton()
            btn_auto.setFixedSize(config.S(24), config.S(24))
            btn_auto.setCursor(Qt.PointingHandCursor)
            btn_auto.setStyleSheet("background: transparent; border: none;")
            btn_auto.setCheckable(True)
            btn_auto.setToolTip(win.txt("tooltip_auto_cut"))
            
            prefs = win.engine.load_preferences() or {}
            auto_cut_colors = prefs.get('auto_cut_colors', [])
            is_checked = color_name in auto_cut_colors
            btn_auto.setChecked(is_checked)
            
            def _update_auto_icon(checked, b=btn_auto):
                icon_name = "auto-marked.png" if checked else "auto-unmarked.png"
                b.setIcon(QIcon(get_layout_icon_path(icon_name)))
                b.setIconSize(QSize(config.S(20), config.S(20)))
                
            _update_auto_icon(is_checked)
            btn_auto.toggled.connect(lambda checked, b=btn_auto, fn=_update_auto_icon: (fn(checked, b), win._save_auto_cut_prefs()))
            win.color_cut_buttons[color_name] = btn_auto
        
        btn_cut_now = QPushButton()
        btn_cut_now.setFixedSize(config.S(24), config.S(24))
        btn_cut_now.setCursor(Qt.PointingHandCursor)
        btn_cut_now.setStyleSheet("background: transparent; border: none;")
        btn_cut_now.setIcon(QIcon(get_layout_icon_path("cut.png")))
        btn_cut_now.setIconSize(QSize(config.S(20), config.S(20)))
        btn_cut_now.setToolTip(win.txt("tooltip_cut_now"))
        btn_cut_now.clicked.connect(lambda _, c=color_name: win._on_cut_now_clicked(c))
        
        row_color.addWidget(btn_cut_now)
        if btn_auto:
            row_color.addWidget(btn_auto)
        else:
            row_color.addSpacing(config.S(24))  # keeps the icons of every row in the same columns
        row_color.addWidget(pin_c)
        
        l_colors_container.addLayout(row_color)
        
        clean_label = win.txt(f"resolve_color_{color_name.lower()}").replace("<br>", " ")
        pin_c.clicked.connect(lambda _, c=color_name, b=btn_auto, p=pin_c, l=clean_label: win._toggle_favorite(
            f'cut_{c.lower()}', b, l, p
        ))
        
        color_idx += 1
        if color_idx == 3:
            l_colors_container.addWidget(hairline())
        
    btn_tools = QPushButton("▸  " + win.txt("lbl_resolve_tools"))
    btn_tools.setCheckable(True)
    btn_tools.setCursor(Qt.PointingHandCursor)
    btn_tools.setToolTip(win.txt("tt_resolve_tools"))
    btn_tools.setStyleSheet(disclosure_css(9.5))
    btn_tools.setMinimumWidth(config.S(120))  # long translations: cut at the edge, never wider than the panel
    tools_box = QFrame()
    tools_box.setObjectName("PanelCard")
    tools_box.setStyleSheet(card_legend.styleSheet())
    tools_box.setLayout(l_colors_container)
    tools_box.setVisible(False)
    btn_tools.toggled.connect(lambda c: (tools_box.setVisible(c),
                                         btn_tools.setText(("▾  " if c else "▸  ") + win.txt("lbl_resolve_tools"))))
    l_assembly.addWidget(btn_tools)
    l_assembly.addWidget(tools_box)
    l_assembly.addStretch(1)
    
    return wrap_activity_panel(p_assembly, scroll=True)
