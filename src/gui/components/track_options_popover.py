#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/components/track_options_popover.py
ROLE: Send ▾ → Track options, opened in place
DESCRIPTION:
A small floating panel under the Send ▾ button with the same track options as Clean's inspector
(which audio and video tracks Send to Resolve puts in the timeline). The user stays on the page
they are on. Esc or a click outside closes it.

It holds its own TrackOptionsDrawer, bound to the same setting as the inspector's one
(win._transcription_source['assembly_track_config']): a change here is saved there at once, and the
inspector's drawer reloads it when the panel closes. Nothing is moved out of the inspector.
"""
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QLayout, QVBoxLayout, QApplication

import config

WIDTH = 300


class TrackOptionsPopover(QFrame):
    def __init__(self, win, parent=None):
        super().__init__(parent or win, Qt.Popup | Qt.FramelessWindowHint)
        from gui.components.track_options_drawer import TrackOptionsDrawer
        self.win = win
        self.setObjectName("TrackOptionsPopover")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setStyleSheet(f"""
            QFrame#TrackOptionsPopover {{ background-color: #1a1a1a; border: 1px solid #333333;
                                          border-radius: {config.S(10)}px; }}
            QLabel#TopTitle {{ color: #ffffff; font-size: {config.FS(10)}pt; font-weight: bold;
                               background: transparent; border: none; }}
            QLabel#TopHint {{ color: #a3a3a3; font-size: {config.FS(8.5)}pt; background: transparent; border: none; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(config.S(12), config.S(10), config.S(12), config.S(12))
        lay.setSpacing(config.S(6))
        lay.setSizeConstraint(QLayout.SetFixedSize)   # the panel follows the drawer (custom track lists open/close)
        title = QLabel(win.txt("btn_track_options", "Track options"))
        title.setObjectName("TopTitle")
        hint = QLabel(win.txt("track_options_hint", "The tracks Send to Resolve puts in the timeline."))
        hint.setObjectName("TopHint")
        hint.setWordWrap(True)
        hint.setFixedWidth(config.S(WIDTH))
        lay.addWidget(title)
        lay.addWidget(hint)

        self.drawer = TrackOptionsDrawer(win, getattr(win, "engine", None), parent=self)
        self.drawer.is_expanded = True
        self.drawer.setFixedWidth(config.S(WIDTH))
        # a box of its own here, not a drawer hanging under a button
        self.drawer.scroll_area.setStyleSheet(f"""
            QScrollArea#TrackOptionsScroll {{ background-color: #0f2d1e; border: 1px solid #11703c;
                                              border-radius: {config.S(6)}px; }}
            QScrollArea#TrackOptionsScroll > QWidget > QWidget {{ background: transparent; }}
        """)
        lay.addWidget(self.drawer)
        self._fit()

    # --- size and place ------------------------------------------------------
    def _fit(self):
        """Give the drawer its full height (the custom track lists wrap at the panel's width)."""
        d = self.drawer
        if getattr(d, "_is_animating", False):
            return
        for lst in (d.w_a_cust_list, d.w_v_cust_list):
            if lst.maximumHeight() > 0:
                lst.resizeEvent(None)
        d.inner_frame.layout().activate()
        d.setFixedHeight(d.inner_frame.sizeHint().height() + 2)
        self.layout().activate()
        self.adjustSize()

    def open_under(self, anchor):
        """Show the panel under anchor (its right edge on the anchor's), kept on the screen."""
        self._fit()
        if anchor is not None:
            br = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height()))
            pos = QPoint(br.x() - self.width(), br.y() + config.S(6))
            screen = anchor.screen() or QApplication.primaryScreen()
        else:
            g = self.win.mapToGlobal(QPoint(self.win.width(), 0))
            pos = QPoint(g.x() - self.width() - config.S(14), g.y() + config.S(50))
            screen = self.win.screen() or QApplication.primaryScreen()
        if screen is not None:
            r = screen.availableGeometry()
            top = self.win.window()
            r = r.united(top.geometry()) if top is not None else r   # a window bigger than the screen: stay with it
            pos.setX(max(r.left() + 4, min(pos.x(), r.right() - self.width() - 4)))
            pos.setY(max(r.top() + 4, min(pos.y(), r.bottom() - self.height() - 4)))
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self._fit)   # once laid out at its real width

    # --- closing ---------------------------------------------------------------
    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            ev.accept()
            self.close()
            return
        super().keyPressEvent(ev)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._sync_inspector()

    def _sync_inspector(self):
        """The inspector's drawer shows what was chosen here (same setting, reloaded)."""
        insp = getattr(self.win, "w_track_options", None)
        if insp is None:
            return
        try:
            insp.load_config()
            if insp.is_expanded:
                insp._animate_to_size()
        except Exception as exc:
            print(f"[track options] inspector sync: {exc}")


def open_track_options(win, anchor=None):
    """Open (or re-open) the track options under anchor; returns the panel."""
    old = getattr(win, "_track_options_popover", None)
    if old is not None:
        try:
            old.close()
        except RuntimeError:
            pass
    pop = TrackOptionsPopover(win)
    win._track_options_popover = pop
    pop.destroyed.connect(lambda *_: getattr(win, "_track_options_popover", None) is pop and setattr(win, "_track_options_popover", None))
    pop.open_under(anchor)
    return pop
