#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: icons.py
ROLE: GUI helper
DESCRIPTION:
StoryCraft's stroke icons (24 px grid, drawn from inline SVG at 2x) for Home, the workspace rail
and anywhere a crisp single-colour icon is needed. Colours are passed in: callers map them through
gui.theme when they follow the theme.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICONS = {
    # AI: a sparkle
    "ai": '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.7 1.8 1.8.7-1.8.7L19 20l-.7-1.8-1.8-.7 1.8-.7z"/>',
    "resolve": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M8 5v5M14 10v9"/>',
    "file": '<path d="M12 15V4M7 9l5-5 5 5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    "continue": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "back": '<path d="M15 6l-6 6 6 6"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z"/>',
    "cleanup": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.6 7.6 20 19M8.6 16.4 20 5"/>',
    "compose": '<rect x="3" y="4" width="18" height="4" rx="1"/><rect x="3" y="10" width="12" height="4" rx="1"/>'
               '<rect x="3" y="16" width="15" height="4" rx="1"/>',
    "review": '<path d="M3 12h3l2-6 3 12 3-9 2 3h5"/>',
    "story": '<rect x="4" y="3" width="16" height="6" rx="1.5"/><rect x="4" y="11" width="16" height="6" rx="1.5"/>'
             '<path d="M7 21h10"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "voice": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    # Voice patch page: Insert patch (an arrow into a track), Save patch (download), Show in Finder (folder)
    "insert": '<rect x="3" y="15" width="18" height="5" rx="1.5"/><path d="M12 3v8.5M8.5 8 12 11.5 15.5 8"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "settings": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
                '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
}


def icon_pixmap(name, color, px, stroke=1.75):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="{w}" '
           'stroke-linecap="round" stroke-linejoin="round">{b}</svg>').format(c=color, w=stroke, b=ICONS[name])
    ratio = 2
    pm = QPixmap(int(px * ratio), int(px * ratio))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    QSvgRenderer(svg.encode("utf-8")).render(p)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm
