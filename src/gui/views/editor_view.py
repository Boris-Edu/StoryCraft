#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: editor_view.py
ROLE: GUI View
DESCRIPTION:
Primary transcript editing view with scrollable TranscriptionCanvas, side-by-side loading overlay, and audio preview scrubber.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QStackedWidget, QPushButton
)
import sys
import config
from gui.components.transcription_canvas import TranscriptionCanvas
from gui.components.audio_preview import AudioPreviewWidget
from gui.components.waveform_timeline import WaveformTimelineWidget
from gui.widgets.progress_bar import LiquidProgressBar


def _build_workflow_bar(win, parent) -> QWidget:
    """Clean up -> Compose -> Send to Resolve: the three steps of an edit, always in view."""
    bar = QFrame(parent)
    bar.setObjectName("WorkflowBar")
    grad = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"  # the StoryCraft gradient (brand.CYAN -> brand.VIOLET)
    grad_hi = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc)"
    bar.setStyleSheet(f"""
        QFrame#WorkflowBar {{ background-color: {config.BG_COLOR}; border: none; }}
        QFrame#WfTrack {{ background-color: #222222; border: 1px solid #303030; border-radius: {config.S(12)}px; }}
        QPushButton#WfStep {{ background: transparent; border: none; border-radius: {config.S(9)}px; }}
        QPushButton#WfStep:hover {{ background: #2a2a2a; }}
        QPushButton#WfStep[active="true"] {{ background: #333333; }}
        QLabel#WfBadge {{ background: #2e2e2e; color: #9a9a9a; border-radius: {config.S(11)}px;
            font-size: {config.FS(8.5)}pt; font-weight: bold; }}
        QLabel#WfBadge[active="true"] {{ background: {grad}; color: #0b0b14; }}
        QLabel#WfStepText {{ color: #8f8f8f; font-family: 'Outfit'; font-size: {config.FS(10.5)}pt; font-weight: 600;
            background: transparent; }}
        QLabel#WfStepText[active="true"] {{ color: #ffffff; }}
        QPushButton#WfSend {{ background: {grad}; color: #0b0b14; border: none;
            border-top-left-radius: {config.S(10)}px; border-bottom-left-radius: {config.S(10)}px;
            border-top-right-radius: 0px; border-bottom-right-radius: 0px;
            padding: {config.S(9)}px {config.S(20)}px; font-size: {config.FS(10)}pt; font-weight: bold; }}
        QPushButton#WfSend:hover {{ background: {grad_hi}; }}
        QPushButton#WfSend:disabled {{ background: #2a2a2a; color: #666666; }}
        QPushButton#WfSend[sync="synced"] {{ background: #1d2a22; color: #8fd19e; }}
        QPushButton#WfSend[sync="synced"]:hover {{ background: #24352a; color: #b6e6c1; }}
        QPushButton#WfSendMore {{ background: #a855f7; color: #0b0b14; border: none;
            border-left: 1px solid rgba(11, 11, 20, 0.35); border-top-left-radius: 0px; border-bottom-left-radius: 0px;
            border-top-right-radius: {config.S(10)}px; border-bottom-right-radius: {config.S(10)}px;
            padding: {config.S(9)}px 0; font-size: {config.FS(9)}pt; font-weight: bold; }}
        QPushButton#WfSendMore:hover {{ background: #c084fc; }}
        QPushButton#WfSendMore:disabled {{ background: #2a2a2a; color: #666666; }}
        QPushButton#WfGear {{ background: transparent; color: #9a9a9a; border: none; border-radius: {config.S(15)}px;
            font-size: {config.FS(13)}pt; }}
        QPushButton#WfGear:hover {{ color: #ffffff; background: #262626; }}
    """)
    h = QHBoxLayout(bar)
    h.setContentsMargins(config.S(20), config.S(12), config.S(16), config.S(10))
    win.workflow_bar_layout = h   # its right margin keeps the corner's Home/Settings free when the inspector is closed
    h.setSpacing(config.S(8))
    # The story and the tools around it: the same bar on every work page (here, Clean is lit)
    from gui.widgets.step_track import HubNav
    track = HubNav(win, active="cleanup", parent=bar)
    win.hub_nav = track
    win.workflow_steps = {}
    h.addWidget(track)
    h.addStretch(1)
    # No view toggle: Clean shows the story (the player's Recording switch shows the whole recording)
    # Resolve connection chip (filled by win._update_resolve_chip)
    win.lbl_resolve_chip = QLabel("", bar)
    win.lbl_resolve_chip.hide()
    h.addWidget(win.lbl_resolve_chip)
    h.addSpacing(config.S(6))
    win.lbl_workflow_summary = QLabel("", bar)
    win.lbl_workflow_summary.setStyleSheet(f"color: #9a9a9a; font-size: {config.FS(9)}pt; background: transparent;")
    h.addWidget(win.lbl_workflow_summary)
    win.btn_send_resolve = QPushButton(win.txt("btn_assemble"), bar)
    win.btn_send_resolve.setObjectName("WfSend")
    win.btn_send_resolve.setCursor(Qt.PointingHandCursor)
    win.btn_send_resolve.setToolTip(win.txt("tt_send_resolve"))
    win.btn_send_resolve.clicked.connect(lambda: win._on_assemble())
    h.addWidget(win.btn_send_resolve)
    win.btn_send_options = QPushButton("▾", bar)
    win.btn_send_options.setObjectName("WfSendMore")
    win.btn_send_options.setCursor(Qt.PointingHandCursor)
    win.btn_send_options.setToolTip(win.txt("btn_track_options"))
    win.btn_send_options.setFixedWidth(config.S(28))
    win.btn_send_options.clicked.connect(lambda: win._show_track_options())
    send = QHBoxLayout()
    send.setSpacing(0)
    send.addWidget(win.btn_send_resolve)
    send.addWidget(win.btn_send_options)
    h.removeWidget(win.btn_send_resolve)
    h.addLayout(send)
    # Settings lives on the workspace rail (gui/components/workspace_rail.py)
    win._set_workflow_step("cleanup")

    # Narrow editor: the summary goes first, then the Resolve chip; steps and Send keep their size
    # (Send to Resolve moves to the window's top-right corner, next to Home/Settings: gui/components/corner_actions.py)
    win.btn_send_resolve.setMinimumWidth(win.btn_send_resolve.sizeHint().width())
    _orig_resize = bar.resizeEvent

    def _bar_resize(e, _orig=_orig_resize):
        m = h.contentsMargins()
        # short of room: the hub bar's other tools go icon-only before anything overlaps it
        hub_full = track.full_width() if hasattr(track, 'full_width') else track.sizeHint().width()
        base = m.left() + m.right() + h.spacing() * 6 + config.S(16)
        if hasattr(track, 'set_compact'):
            track.set_compact(hub_full + base > e.size().width())
        fixed = track.sizeHint().width() + base
        chip_w = win.lbl_resolve_chip.sizeHint().width() if win.lbl_resolve_chip.text() else 0
        summ_w = win.lbl_workflow_summary.sizeHint().width()
        room = e.size().width() - fixed
        win.lbl_workflow_summary.setVisible(room >= chip_w + summ_w)
        win._wf_chip_room = room >= chip_w  # read by _update_resolve_chip
        if win.lbl_resolve_chip.text():
            win.lbl_resolve_chip.setVisible(win._wf_chip_room)
        _orig(e)
    bar.resizeEvent = _bar_resize
    return bar


def _build_mark_strip(win, parent) -> QWidget:
    """Marker chips (keys 1-9), how marking works, and Restore AI marks."""
    strip = QFrame(parent)
    strip.setObjectName("MarkStrip")
    strip.setStyleSheet(f"""
        QFrame#MarkStrip {{ background-color: {config.BG_COLOR}; border: none; }}
        QPushButton#MarkChip {{ background: #232323; color: #c4c4c4; border: 1px solid #303030;
            border-radius: {config.S(15)}px; padding: {config.S(5)}px {config.S(13)}px; font-size: {config.FS(9.5)}pt; }}
        QPushButton#MarkChip:hover {{ color: #ffffff; background: #2b2b2b; }}
        QPushButton#MarkChip:checked {{ color: #ffffff; background: #2e2e2e; border-color: {config.BTN_BG}; font-weight: bold; }}
        QPushButton#MarkChip:disabled {{ color: #555555; }}
        QPushButton#RestoreAI {{ background: transparent; color: #a8a8a8; border: none;
            border-radius: {config.S(13)}px; padding: {config.S(3)}px {config.S(10)}px; font-size: {config.FS(9)}pt; }}
        QPushButton#RestoreAI:hover {{ color: #ffffff; background: #262626; }}
    """)
    v = QVBoxLayout(strip)          # row 1: chips; row 2: how the mouse works, drawn
    v.setContentsMargins(config.S(20), config.S(2), config.S(16), config.S(10))
    v.setSpacing(config.S(4))
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(config.S(8))
    v.addLayout(h)
    lbl = QLabel(win.txt("mark_with"), strip)
    lbl.setStyleSheet(f"color: #8a8a8a; font-size: {config.FS(9.5)}pt; background: transparent;")
    h.addWidget(lbl)
    chips = QHBoxLayout()
    chips.setSpacing(config.S(6))
    win.mark_strip_chips = chips
    h.addLayout(chips)
    h.addStretch(1)
    from gui.widgets.mouse_hint import MouseHint
    t = win.txt
    labels = (
        {"mark": t("hint_m_mark", "marks"), "clear": t("hint_m_clear", "clears"),
         "select": t("hint_m_select", "selects"), "jump": t("hint_m_jump", "playhead here")},
        {"mark": t("hint_m_mark_full", "Click a word to mark it (drag to mark more)"),
         "clear": t("hint_m_clear_full", "Right-click a word to clear its mark (drag to clear more)"),
         "select": t("hint_m_select_full", "Shift-drag to select a passage, then right-click it for more"),
         "jump": t("hint_m_jump_full", "{key} + right-click a word to put the playhead there").format(
             key="Option" if sys.platform == "darwin" else "Alt")},
        t("hint_m_title", "The mouse in the transcript"))
    hint = MouseHint(*labels, parent=strip)          # the drawn row, under the chips
    hint_btn = MouseHint(*labels, parent=strip)      # folded: one mouse button in the chip row (opens the card)
    hint_btn.set_folded(True)
    win.mouse_hint, win.mouse_hint_btn = hint, hint_btn
    h.addWidget(hint_btn)
    row2 = QHBoxLayout()
    row2.setContentsMargins(0, 0, 0, 0)
    row2.addWidget(hint)
    row2.addStretch(1)
    # Compose > Original with a story: what the grey means
    legend = QLabel(f"<span style='color:{config.BTN_BG}; font-weight:900'>▍</span> "
                    f"{t('story_legend_in', 'In the story')}  ·  "
                    f"<span style='color:#5f5f5f'>{t('story_legend_out', 'Grey: left out of the story')}</span>", strip)
    legend.setStyleSheet(f"color: #a8a8a8; font-size: {config.FS(8.5)}pt; background: transparent;")
    legend.setToolTip(t("story_legend_tt", "Shift-drag a passage and right-click it to add it to the story or leave it out."))
    legend.hide()
    win.story_legend = legend
    row2.addWidget(legend)
    # Clean without a story: the whole recording, and where the story comes from
    story_hint = QLabel(f"<a href='story' style='color:#9a9a9a; text-decoration:none'>"
                        f"{t('clean_no_story_hint', 'Build a story on the Story page; Clean then shows just the story.')}</a>", strip)
    story_hint.setStyleSheet(f"font-size: {config.FS(8.5)}pt; background: transparent;")
    story_hint.setCursor(Qt.PointingHandCursor)
    story_hint.linkActivated.connect(lambda _h: win.show_story() if hasattr(win, 'show_story') else None)
    story_hint.hide()
    win.clean_story_hint = story_hint
    row2.addWidget(story_hint)
    # Clean on the story: one quiet line on where the whole recording went (the player's Play original
    # switch). Dimmed; ✕ or turning the switch on puts it away for the session (main_window._apply_clean_view)
    rec_hint = QWidget(strip)
    rec_hint.setStyleSheet("background: transparent;")
    rh = QHBoxLayout(rec_hint)
    rh.setContentsMargins(0, 0, 0, 0)
    rh.setSpacing(config.S(4))
    switch_name = t('player_recording', 'Play original')
    rec_lbl = QLabel(t('clean_rec_hint', 'Clean shows your story. To see or hear the whole recording, '
                                         'turn on {switch} in the player.').replace('{switch}', switch_name), rec_hint)
    rec_lbl.setStyleSheet(f"color: #7a7a7a; font-size: {config.FS(8.5)}pt; background: transparent;")
    rec_lbl.setMinimumWidth(1)                   # one line; a narrow window clips it rather than widening
    rec_lbl.setToolTip(rec_lbl.text())
    rec_x = QPushButton("✕", rec_hint)
    rec_x.setObjectName("CleanRecHintClose")
    rec_x.setCursor(Qt.PointingHandCursor)
    rec_x.setToolTip(t('clean_rec_hint_close', 'Hide this tip'))
    rec_x.setFlat(True)
    rec_x.setStyleSheet(f"QPushButton#CleanRecHintClose {{ color: #6a6a6a; background: transparent; border: none;"
                        f" font-size: {config.FS(8)}pt; padding: 0 {config.S(4)}px; }}"
                        f" QPushButton#CleanRecHintClose:hover {{ color: #c8c8c8; }}")

    def _close_rec_hint():
        win._clean_rec_hint_off = True
        rec_hint.hide()
    rec_x.clicked.connect(_close_rec_hint)
    rh.addWidget(rec_lbl, 0)
    rh.addWidget(rec_x, 0)
    rh.addStretch(1)
    rec_hint.hide()
    win.clean_rec_hint, win.clean_rec_hint_label = rec_hint, rec_lbl
    ai = QLabel(f"<span style='color:#67e8f9; text-decoration: underline dotted;'>"
                f"{t('ai_voice_legend', 'Voice patch')}</span>", strip)
    ai.setStyleSheet(f"font-size: {config.FS(8.5)}pt; background: transparent; padding-left: {config.S(10)}px;")
    ai.setToolTip(t("ai_voice_legend_tt", "Words in this colour are synthetic speech (a voice patch in the story, "
                                          "made with VoiceStudio)."))
    ai.hide()
    win.ai_voice_legend = ai
    row2.addWidget(ai)
    v.addLayout(row2)
    row3 = QHBoxLayout()                         # the Play original tip: a line of its own while shown
    row3.setContentsMargins(0, 0, 0, 0)
    row3.addWidget(rec_hint, 1)
    v.addLayout(row3)
    lbl.setToolTip(hint.toolTip())
    btn = QPushButton(win.txt("btn_restore_ai"), strip)
    btn.setObjectName("RestoreAI")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setToolTip(win.txt("tt_restore_ai"))
    btn.clicked.connect(lambda: win._restore_all_ai_marks())
    h.addWidget(btn)
    btn.hide()   # Restore AI marks lives in the AI tab now (each Found kind keeps its own Restore)

    # The chips keep their full labels: the hint (also the "Mark with" tooltip) gives way first,
    # then the "Mark with" label, then Restore AI marks shrinks to its icon
    full_restore = btn.text()
    _orig_resize = strip.resizeEvent

    def _strip_resize(e, _orig=_orig_resize):
        chips_w = sum(chips.itemAt(i).widget().sizeHint().width() + chips.spacing()
                      for i in range(chips.count()) if chips.itemAt(i).widget())
        margins = h.spacing() * 4 + v.contentsMargins().left() + v.contentsMargins().right()
        btn.setText(full_restore)
        restore_w = 0 if btn.isHidden() else btn.sizeHint().width()
        w = e.size().width()
        base = chips_w + margins
        # the drawn row needs its width; without it, the mouse button in the chip row opens the same card
        room = w - v.contentsMargins().left() - v.contentsMargins().right()
        full = room >= hint.full_width()
        hint.setVisible(full)
        hint_btn.setVisible(not full)
        extra = 0 if full else hint_btn.folded_width() + h.spacing()
        lbl.setVisible(w >= base + lbl.sizeHint().width() + restore_w + extra)
        if w < base + restore_w + extra:
            btn.setText("\u21ba")  # ↺, the full name stays in the tooltip
        _orig(e)
    strip.resizeEvent = _strip_resize
    if hasattr(win, '_rebuild_mark_strip'):
        win._rebuild_mark_strip()
    return strip


def build_editor_view(win) -> QWidget:
    """Build Page 2 of the main stack: Interactive Transcript Editor screen."""
    page = QWidget()
    page.setObjectName("page_editor")
    page.setStyleSheet(f"QWidget#page_editor {{ background-color: {config.BG_COLOR}; }}")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    
    win.scroll_area = QScrollArea(page)
    win.scroll_area.setWidgetResizable(True)
    win.scroll_area.setFrameShape(QFrame.NoFrame)
    win.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    win.scroll_area.setStyleSheet(f"QScrollArea {{ background-color: {config.BG_COLOR}; border: none; }}")
    
    win.text_canvas = TranscriptionCanvas(main_window=win)
    win.scroll_area.setWidget(win.text_canvas)

    normal_editor_page = QWidget()
    normal_editor_layout = QVBoxLayout(normal_editor_page)
    normal_editor_layout.setContentsMargins(0, 0, 0, 0)
    normal_editor_layout.setSpacing(0)
    normal_editor_layout.addWidget(win.scroll_area)

    win.sbs_loading_page = QWidget()
    win.sbs_loading_page.setStyleSheet(f"background-color: {config.BG_COLOR};")
    ol_layout = QVBoxLayout(win.sbs_loading_page)
    ol_layout.setAlignment(Qt.AlignCenter)
    
    lbl = QLabel(win.txt("lbl_just_a_second"))
    lbl.setStyleSheet(f"color: {config.NOTE_COL}; font-size: {config.FS(13)}pt; font-family: '{config.UI_FONT_NAME}'; background: transparent;")
    lbl.setAlignment(Qt.AlignCenter)
    ol_layout.addWidget(lbl)
    
    ol_layout.addSpacing(config.S(15))
    
    win.sbs_loading_bar = LiquidProgressBar(win.sbs_loading_page)
    win.sbs_loading_bar.setFixedWidth(config.S(400))
    ol_layout.addWidget(win.sbs_loading_bar, 0, Qt.AlignCenter)

    win.editor_view_stack = QStackedWidget(page)
    win.editor_view_stack.setStyleSheet("background: transparent;")
    win.editor_view_stack.addWidget(normal_editor_page)
    win.editor_view_stack.addWidget(win.sbs_loading_page)
    layout.addWidget(_build_workflow_bar(win, page))
    layout.addWidget(_build_mark_strip(win, page))
    layout.addWidget(win.editor_view_stack)
    
    return page
