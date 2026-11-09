#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/components/corner_actions.py
ROLE: Send to Resolve ▾, Home and Settings, top-right of the window
DESCRIPTION:
The side rail is gone (the hub bar at the top of every work page already switches between Story,
Clean, Review cuts and Voice patch). What only the rail had — Home and Settings — sits here, in the
window's top-right corner, on the same line as the hub bar. It floats over the content; the pages keep
that corner free (Story's Available panel and the inspector start one line lower).
"""
from PySide6.QtCore import Qt, QEvent, QObject, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

import config
from gui.icons import icon_pixmap

TOP = 11          # same line as the hub bar (pages start their top row 11-12 px down)
RIGHT = 14


def reserved_height():
    """How far down the pages keep the top-right corner free."""
    return config.S(TOP) + config.S(36) + config.S(6)


class CornerActions(QFrame):
    def __init__(self, win, host):
        super().__init__(host)
        self.win, self.host = win, host
        self.setObjectName("CornerActions")
        self.setAttribute(Qt.WA_StyledBackground, True)
        r = config.S(10)
        self.setStyleSheet(f"""
            QFrame#CornerActions {{ background-color: #1e1e1e; border: 1px solid #303030; border-radius: {config.S(12)}px; }}
            QPushButton#CornerBtn {{ background: transparent; border: none; border-radius: {r}px; }}
            QPushButton#CornerBtn:hover {{ background-color: #2a2a2a; }}
            QPushButton#CornerBtn:checked {{ background-color: #2a2140; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(config.S(3), config.S(3), config.S(3), config.S(3))
        lay.setSpacing(config.S(2))
        self.btn_home = self._button("home", win.txt("home_back", "Home"), win.show_home)
        self.btn_settings = self._button("settings", win.txt("tool_settings", "Settings"), win._on_settings)
        self.btn_home.setCheckable(True)
        # Send to Resolve is the last step of every page around the story: it lives here, not in Clean only.
        # The editor's own buttons are moved (not copied): their states ("In Resolve", "Send changes") follow.
        grad = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
        self.setStyleSheet(self.styleSheet() + f"""
            QPushButton#WfSend {{ background: {grad}; color: #0b0b14; border: none;
                border-top-left-radius: {config.S(9)}px; border-bottom-left-radius: {config.S(9)}px;
                border-top-right-radius: 0px; border-bottom-right-radius: 0px;
                padding: {config.S(6)}px {config.S(16)}px; font-size: {config.FS(10)}pt; font-weight: bold; }}
            QPushButton#WfSend:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc); }}
            QPushButton#WfSend:disabled {{ background: #2a2a2a; color: #666666; }}
            QPushButton#WfSend[sync="synced"] {{ background: #1d2a22; color: #8fd19e; }}
            QPushButton#WfSend[sync="synced"]:hover {{ background: #24352a; color: #b6e6c1; }}
            QPushButton#WfSendMore {{ background: #a855f7; color: #0b0b14; border: none;
                border-left: 1px solid rgba(11, 11, 20, 0.35); border-top-left-radius: 0px; border-bottom-left-radius: 0px;
                border-top-right-radius: {config.S(9)}px; border-bottom-right-radius: {config.S(9)}px;
                padding: {config.S(6)}px 0; font-size: {config.FS(9)}pt; font-weight: bold; }}
            QPushButton#WfSendMore:hover {{ background: #c084fc; }}
            QPushButton#WfSendMore:disabled {{ background: #2a2a2a; color: #666666; }}
            QFrame#CornerSep {{ background-color: #343434; border: none; }}
        """)
        self.send_box = None
        send, more = getattr(win, "btn_send_resolve", None), getattr(win, "btn_send_options", None)
        if send is not None and more is not None:
            from PySide6.QtWidgets import QWidget
            self.send_box = QWidget(self)
            sl = QHBoxLayout(self.send_box)
            sl.setContentsMargins(0, 0, 0, 0)
            sl.setSpacing(0)
            for b in (send, more):
                old = b.parentWidget().layout() if b.parentWidget() is not None else None
                b.setParent(self.send_box)
                sl.addWidget(b)
            send.setFixedHeight(config.S(30))
            more.setFixedHeight(config.S(30))
            # ▾: the rest of the round trip (track options, reload what was edited in Resolve)
            try:
                more.clicked.disconnect()
            except Exception:
                pass
            more.setToolTip(win.txt("tt_send_more", "Track options · Reload from Resolve"))
            more.clicked.connect(self._send_menu)
            lay.addWidget(self.send_box)
            sep = QFrame(self)
            sep.setObjectName("CornerSep")
            sep.setFixedSize(1, config.S(20))
            lay.addSpacing(config.S(4))
            lay.addWidget(sep, 0, Qt.AlignVCenter)
            lay.addSpacing(config.S(2))
        lay.addWidget(self.btn_home)
        lay.addWidget(self.btn_settings)
        self.adjustSize()
        host.installEventFilter(self)
        self.place()

    def _button(self, icon, tip, fn):
        b = QPushButton(self)
        b.setObjectName("CornerBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.setFixedSize(config.S(32), config.S(30))
        px = config.S(18)
        ic = QIcon()
        ic.addPixmap(icon_pixmap(icon, "#a3a3a3", px), QIcon.Normal, QIcon.Off)
        ic.addPixmap(icon_pixmap(icon, "#ffffff", px), QIcon.Active, QIcon.Off)
        ic.addPixmap(icon_pixmap(icon, "#c084fc", px), QIcon.Normal, QIcon.On)
        b.setIcon(ic)
        b.setIconSize(QSize(px, px))
        b.clicked.connect(lambda _=False: fn())
        return b

    def place(self):
        self.adjustSize()
        self.move(self.host.width() - self.width() - config.S(RIGHT), config.S(TOP))
        self.raise_()

    def _send_menu(self):
        self.send_menu()

    def send_menu(self, anchor=None, with_send=False):
        """The Send ▾ menu: track options, reload from Resolve. with_send (from the story status's Resolve
        part): Send itself on top, as the button says it now ("Send to Resolve", "Send changes")."""
        from gui.widgets.menus import context_menu
        win = self.win
        m = context_menu(self)
        send = getattr(win, "btn_send_resolve", None)
        if with_send and send is not None:
            label = send.text().replace("●", "").replace("✓", "").strip() or win.txt("btn_assemble", "Send to Resolve")
            if send.property("sync") == "synced":
                label = win.txt("st_send_again", "Send to Resolve again")
            a = m.addAction(label, send.click)
            a.setEnabled(send.isEnabled())
            m.addSeparator()
        m.addAction(win.txt("btn_track_options", "Track options"), self._track_options)
        if hasattr(win, "reload_from_resolve"):
            m.addAction(win.txt("rt_button", "Reload from Resolve…"), win.reload_from_resolve)
        more = anchor or getattr(win, "btn_send_options", None)
        self._menu_anchor = more
        m.exec(more.mapToGlobal(more.rect().bottomLeft()) if more is not None else self.mapToGlobal(self.rect().bottomLeft()))

    def _track_options(self):
        """Track options open in place, under the Send ▾ that was clicked: the page stays as it is.
        Same setting as the track options in Clean's inspector (see track_options_popover)."""
        from gui.components.track_options_popover import open_track_options
        anchor = getattr(self, "_menu_anchor", None) or getattr(self.win, "btn_send_options", None)
        try:
            if anchor is not None and not anchor.isVisible():
                anchor = getattr(self.win, "btn_send_options", None)
            return open_track_options(self.win, anchor if anchor is not None and anchor.isVisible() else None)
        except RuntimeError:          # the anchor was deleted meanwhile
            return open_track_options(self.win, None)

    def sync(self, page):
        self.btn_home.setChecked(page == 0)
        if self.send_box is not None:
            canvas = getattr(self.win, "text_canvas", None)
            work = page in (2, 3, 4, 5, 6) and bool(getattr(canvas, "words_data", None))
            if self.send_box.isVisible() != work:
                self.send_box.setVisible(work)
                self.place()
        self.raise_()
        # the story's status line (hub bar) follows the page: what it says and the room the corner leaves
        try:
            from gui.components.story_status import refresh_all
            refresh_all(self.win, soon=True)
        except Exception as exc:
            print(f"[corner] story status: {exc}")

    def eventFilter(self, obj, ev):
        if obj is self.host and ev.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self.place()
        return False


class _VisibilityWatch(QObject):
    """Calls fn when a widget is shown or hidden."""

    def __init__(self, widget, fn):
        super().__init__(widget)
        self.fn = fn
        widget.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Show, QEvent.Hide):
            self.fn()
        return False


def watch_visibility(widget, fn):
    return _VisibilityWatch(widget, fn)
