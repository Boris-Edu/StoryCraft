#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: main_workspace_panel.py
ROLE: GUI Activity Panel
DESCRIPTION:
Primary sidebar workspace panel for markers, duration stats, favorites, and timeline assemble trigger.
"""

import os
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QFrame
)
from PySide6.QtGui import QIcon

from gui.widgets.layouts import MainPanelWidget
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton, AssembleSplitButton
from gui.components.track_options_drawer import TrackOptionsDrawer
from .script_panel import (wrap_activity_panel, panel_card, panel_margins, heading_css, sublabel_css,
                           link_button_css, quiet_button, disclosure_css, caption_css)


def build_main_workspace_panel(win) -> QFrame:
    """Build the Main Workspace activity panel and bind widgets to main window."""
    import config
    p_main = MainPanelWidget()
    l_main = QVBoxLayout(p_main.layer1)
    panel_margins(l_main, 10)

    # Top Section (Markers)
    row_marking_title = QHBoxLayout()
    lbl_markers = QLabel(win.txt("lbl_marking_mode"))
    lbl_markers.setStyleSheet(heading_css(12))
    row_marking_title.addWidget(lbl_markers)
    row_marking_title.addStretch()
    win.btn_clear_transcript = QPushButton()
    win.btn_clear_transcript.setFixedSize(config.S(28), config.S(28))
    win.btn_clear_transcript.setToolTip("")
    win.btn_clear_transcript.setCursor(Qt.CursorShape.PointingHandCursor)
    
    from gui.utils import get_layout_icon_path
    win.btn_clear_transcript.setIcon(QIcon(get_layout_icon_path("clean.png")))
    win.btn_clear_transcript.setIconSize(QSize(config.S(18), config.S(18)))
    win.btn_clear_transcript.setStyleSheet(f"""
        QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: {config.S(8)}px;
            padding: {config.S(2)}px; }}
        QPushButton:hover {{ background-color: #2a2a2a; border-color: #333333; }}
    """)
    win.btn_clear_transcript.clicked.connect(win._on_clear_transcript)
    row_marking_title.addWidget(win.btn_clear_transcript)
    l_main.addLayout(row_marking_title)
    lbl_markers_sub = QLabel(win.txt("sub_markers"))
    lbl_markers_sub.setWordWrap(True)
    lbl_markers_sub.setStyleSheet(sublabel_css(9))
    l_main.addWidget(lbl_markers_sub)

    card_markers, l_card_markers = panel_card(6)
    l_card_markers.setContentsMargins(config.S(12), config.S(8), config.S(12), config.S(6))
    win.markers_layout = QVBoxLayout()
    win.markers_layout.setSpacing(config.S(4))
    l_card_markers.addLayout(win.markers_layout)
    l_main.addWidget(card_markers)
    
    win.btn_add_custom_marker = QPushButton(win.txt("lbl_add_custom_marker"))
    win.btn_add_custom_marker.setCursor(Qt.CursorShape.PointingHandCursor)
    win.btn_add_custom_marker.setStyleSheet(link_button_css(9.5) + f"""
        QPushButton {{ font-family: '{config.UI_FONT_NAME}'; padding: {config.S(4)}px {config.S(2)}px; }}""")
    win.btn_add_custom_marker.clicked.connect(win._on_add_custom_marker)
    l_card_markers.addWidget(win.btn_add_custom_marker)
    
    l_main.addStretch(1)
    
    l_layer2 = QVBoxLayout(p_main.layer2)
    l_layer2.setContentsMargins(config.S(1), config.S(6), config.S(3), config.S(12))
    l_layer2.setSpacing(config.S(6))
    l_layer2.setAlignment(Qt.AlignBottom)
    
    win.lbl_analysis_duration = QLabel("")
    win.lbl_analysis_duration.setWordWrap(True)
    win.lbl_analysis_duration.setStyleSheet(sublabel_css(9))
    win.lbl_analysis_duration.setAlignment(Qt.AlignCenter)
    win.lbl_analysis_duration.setVisible(False)
    l_layer2.addWidget(win.lbl_analysis_duration)
    
    win.lbl_pinned_favorites = QLabel(win.txt("lbl_pinned_favorites"))
    win.lbl_pinned_favorites.setStyleSheet(caption_css())
    win.lbl_pinned_favorites.setVisible(False)
    l_layer2.addWidget(win.lbl_pinned_favorites)
    
    win.layout_favorites = QVBoxLayout()
    win.layout_favorites.setSpacing(config.S(10))
    l_layer2.addLayout(win.layout_favorites)
    
    layout_assemble_group = QVBoxLayout()
    layout_assemble_group.setContentsMargins(0, 0, 0, 0)
    layout_assemble_group.setSpacing(config.S(6))

    # Story mode (issue #8): visible while an imported AI edit drives the next Assemble
    win.story_banner = QFrame()
    win.story_banner.setObjectName("StoryBanner")
    win.story_banner.setStyleSheet(f"""
        QFrame#StoryBanner {{ background-color: rgba(35, 165, 89, 30); border: 1px solid #2f6b45;
                              border-radius: {config.S(12)}px; }}
    """)
    l_story = QHBoxLayout(win.story_banner)
    l_story.setContentsMargins(config.S(12), config.S(8), config.S(10), config.S(8))
    l_story.setSpacing(config.S(10))
    win.lbl_story = QLabel("")
    win.lbl_story.setWordWrap(True)
    win.lbl_story.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9)}pt; background: transparent; border: none;")
    win.btn_story_clear = QPushButton(win.txt("btn_story_clear"))
    win.btn_story_clear.setCursor(Qt.PointingHandCursor)
    win.btn_story_clear.clicked.connect(win._clear_story)
    quiet_button(win.btn_story_clear, 9, 28, 4, 10)
    l_story.addWidget(win.lbl_story, 1)
    l_story.addWidget(win.btn_story_clear, 0, Qt.AlignVCenter)
    win.story_banner.setVisible(False)
    layout_assemble_group.addWidget(win.story_banner)

    # Send to Resolve lives in the workflow bar above the transcript. This split button stays as the
    # object other code locks/unlocks, hidden; its track options open from a plain toggle instead.
    win.btn_assemble = AssembleSplitButton(win.txt("btn_assemble"), win)
    win.btn_assemble.setVisible(False)
    layout_assemble_group.addWidget(win.btn_assemble)

    win.btn_track_options = QPushButton("▸  " + win.txt("btn_track_options"))
    win.btn_track_options.setCursor(Qt.PointingHandCursor)
    win.btn_track_options.setStyleSheet(disclosure_css(9.5, compact=True))
    win.btn_track_options.setMinimumWidth(config.S(120))  # long translations: cut at the edge, never wider than the panel
    layout_assemble_group.addWidget(win.btn_track_options)

    win.w_track_options = TrackOptionsDrawer(win, win.engine)
    layout_assemble_group.addWidget(win.w_track_options)

    # After sending: speech cleanup, captions, cameras, extra exports
    from .finishing_section import build_finishing_section
    fin_head, fin_body = build_finishing_section(win)
    layout_assemble_group.addWidget(fin_head)
    layout_assemble_group.addWidget(fin_body)
    QTimer.singleShot(60, win.w_track_options.warmup)

    def _toggle_track_options():
        win.w_track_options.toggle_expand()
        arrow = "▾  " if win.w_track_options.is_expanded else "▸  "
        win.btn_track_options.setText(arrow + win.txt("btn_track_options"))
        p_main.resizeEvent(None)
    win._toggle_track_options = _toggle_track_options
    win.btn_track_options.clicked.connect(_toggle_track_options)
    win.btn_assemble.toggleDrawerClicked.connect(_toggle_track_options)

    l_layer2.addLayout(layout_assemble_group)
    
    win._build_marker_radio_buttons()
    win.p_main = p_main
    
    return wrap_activity_panel(p_main, scroll=True)  # MainPanelWidget reserves room for both layers
