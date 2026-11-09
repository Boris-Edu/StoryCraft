#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/widgets/mouse_hint.py
ROLE: How the mouse works in the transcript, drawn
DESCRIPTION:
A row of small pictures: a mouse with the button that does it lit up, the key to hold (⇧, ⌥) as a key
cap, and a word. When there isn't room, it folds into one mouse button that opens the same pictures
as a card with full sentences.
"""
import sys

from PySide6.QtCore import Qt, QRectF, QPointF, QSize, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QPainterPath
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout

import config
from gui.theme import qc

ALT_KEY = "⌥" if sys.platform == "darwin" else "Alt"
SHIFT_KEY = "⇧" if sys.platform == "darwin" else "Shift"

# (button lit, key held, drag) for each action
ACTIONS = (
    ("mark", "left", None, False),
    ("clear", "right", None, False),
    ("select", "left", SHIFT_KEY, True),
    ("jump", "right", ALT_KEY, False),
)
DOUBLE = "2×"   # drawn after the mouse when the button is "double" (a double-click)


def draw_mouse(p, rect, lit, color, accent):
    """A mouse seen from above in rect: body, the two buttons, the lit one filled."""
    if lit == "double":
        lit = "left"
    w, h = rect.width(), rect.height()
    body = QRectF(rect.x() + 0.75, rect.y() + 0.75, w - 1.5, h - 1.5)
    path = QPainterPath()
    path.addRoundedRect(body, w * 0.48, w * 0.48)
    split_y = body.y() + body.height() * 0.42
    cx = body.center().x()
    if lit:
        btn = QPainterPath()
        half = QRectF(body.x(), body.y(), body.width() / 2, split_y - body.y()) if lit == "left" else \
            QRectF(cx, body.y(), body.width() / 2, split_y - body.y())
        btn.addRect(half)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawPath(path.intersected(btn))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(color, 1.3))
    p.drawPath(path)
    p.drawLine(QPointF(body.x(), split_y), QPointF(body.right(), split_y))
    p.drawLine(QPointF(cx, body.y()), QPointF(cx, split_y))


def draw_key(p, x, cy, text, font, color):
    """A key cap with text, starting at x, centred on cy. Returns its width."""
    fm = QFontMetrics(font)
    kw = max(fm.height(), fm.horizontalAdvance(text) + config.S(8))
    kh = fm.height() + config.S(2)
    r = QRectF(x, cy - kh / 2, kw, kh)
    p.setPen(QPen(color, 1.1))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(r, config.S(3), config.S(3))
    p.setFont(font)
    p.drawText(r, Qt.AlignCenter, text)
    return kw


def draw_drag(p, x, cy, color):
    """A small ↔ arrow: drag. Returns its width."""
    w = config.S(12)
    p.setPen(QPen(color, 1.2))
    p.drawLine(QPointF(x, cy), QPointF(x + w, cy))
    a = config.S(3)
    for tip, d in ((x, 1), (x + w, -1)):
        p.drawLine(QPointF(tip, cy), QPointF(tip + d * a, cy - a))
        p.drawLine(QPointF(tip, cy), QPointF(tip + d * a, cy + a))
    return w


def action_width(fm, key, drag, label, mouse_w, gap, lit=None):
    w = mouse_w
    if lit == "double":
        w += gap + fm.horizontalAdvance(DOUBLE)
    if key:
        w += max(fm.height(), fm.horizontalAdvance(key) + config.S(8)) + gap
    if drag:
        w += gap + config.S(12)
    return w + gap + fm.horizontalAdvance(label)


def paint_action(p, x, cy, lit, key, drag, label, font, color, accent, mouse_h):
    """One picture + label at x, centred on cy. Returns the x after it."""
    gap = config.S(5)
    mw = mouse_h * 0.68
    if key:
        x += draw_key(p, x, cy, key, font, color) + gap
    draw_mouse(p, QRectF(x, cy - mouse_h / 2, mw, mouse_h), lit, color, accent)
    x += mw
    fm = QFontMetrics(font)
    if lit == "double":
        x += gap
        p.setFont(font)
        p.setPen(color)
        p.drawText(QPointF(x, cy + fm.ascent() / 2 - 1), DOUBLE)
        x += fm.horizontalAdvance(DOUBLE)
    if drag:
        x += gap
        x += draw_drag(p, x, cy, color)
    x += gap
    p.setFont(font)
    p.setPen(color)
    p.drawText(QPointF(x, cy + fm.ascent() / 2 - 1), label)
    return x + fm.horizontalAdvance(label)


class MouseHintCard(QFrame):
    """The pictures with full sentences (opened from the folded hint)."""

    def __init__(self, rows, title, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._rows, self._title = rows, title
        self.setStyleSheet(f"QFrame {{ background: {config.BG_COLOR}; border: 1px solid #3a3a3a; border-radius: {config.S(8)}px; }}")
        f = QFont(self.font())
        f.setPointSizeF(config.FS(10))
        self._font = f
        fm = QFontMetrics(f)
        self._row_h = max(config.S(30), fm.height() + config.S(14))
        self._pic_w = config.S(74)
        w = self._pic_w + max(fm.horizontalAdvance(t) for _, _, _, _, t in rows) + config.S(40)
        self.setFixedSize(max(config.S(300), w), config.S(44) + self._row_h * len(rows) + config.S(10))

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tf = QFont(self._font)
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(QColor(qc(config.FG_COLOR)))
        p.drawText(QRectF(config.S(16), config.S(10), self.width(), config.S(24)), Qt.AlignVCenter, self._title)
        color, accent = QColor(qc("#9a9a9a")), QColor(qc(config.BTN_BG))
        y = config.S(44)
        for _, lit, key, drag, text in self._rows:
            cy = y + self._row_h / 2
            x = config.S(16)
            if key:
                x += draw_key(p, x, cy, key, self._font, color) + config.S(5)
            draw_mouse(p, QRectF(x, cy - config.S(10), config.S(14), config.S(20)), lit, color, accent)
            x += config.S(14)
            if lit == "double":
                p.setFont(self._font)
                p.drawText(QRectF(x + config.S(4), y, self._pic_w, self._row_h), Qt.AlignVCenter, DOUBLE)
            if drag:
                draw_drag(p, x + config.S(5), cy, color)
            p.setFont(self._font)
            p.setPen(QColor(qc(config.FG_COLOR)))
            p.drawText(QRectF(config.S(16) + self._pic_w, y, self.width(), self._row_h), Qt.AlignVCenter, text)
            y += self._row_h
        p.end()


class MouseHint(QWidget):
    """The drawn hint row; folds into one mouse button when there's no room (set_folded)."""
    clicked = Signal()

    def __init__(self, labels, sentences, title, parent=None, actions=None):
        super().__init__(parent)
        self._labels, self._sentences, self._title = labels, sentences, title
        self._actions = tuple(actions) if actions else ACTIONS   # (key, button lit, key held, drag) per action
        self._folded = False
        f = QFont(self.font())
        f.setPointSizeF(config.FS(8.5))
        self._font = f
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("\n".join(sentences[k] for k, *_ in self._actions))

    def _mouse_h(self):
        return QFontMetrics(self._font).height() + config.S(3)

    def full_width(self):
        fm = QFontMetrics(self._font)
        gap = config.S(5)
        mw = self._mouse_h() * 0.68
        sep = config.S(14)
        return int(sum(action_width(fm, key, drag, self._labels[k], mw, gap, lit) for k, lit, key, drag in self._actions)
                   + sep * (len(self._actions) - 1) + config.S(4))

    def folded_width(self):
        return config.S(26)

    def set_folded(self, folded):
        if folded != self._folded:
            self._folded = folded
            self.updateGeometry()
            self.update()

    def sizeHint(self):
        return QSize(self.folded_width() if self._folded else self.full_width(), self._mouse_h() + config.S(8))

    def minimumSizeHint(self):
        # never forces the strip wider: the strip hides the drawn row when it doesn't fit
        return self.sizeHint() if self._folded else QSize(0, self.sizeHint().height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color, accent = QColor(qc("#777777")), QColor(qc(config.BTN_BG))
        cy = self.height() / 2
        mh = self._mouse_h()
        if self._folded:
            hover = self.underMouse()
            draw_mouse(p, QRectF((self.width() - mh * 0.68) / 2, cy - mh / 2, mh * 0.68, mh), None,
                       QColor(qc("#c4c4c4" if hover else "#8a8a8a")), accent)
            p.end()
            return
        x = 0.0
        for i, (k, lit, key, drag) in enumerate(self._actions):
            if i:
                p.setPen(color)
                p.setFont(self._font)
                p.drawText(QRectF(x, 0, config.S(14), self.height()), Qt.AlignCenter, "·")
                x += config.S(14)
            x = paint_action(p, x, cy, lit, key, drag, self._labels[k], self._font, color, accent, mh)
        p.end()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.show_card()
            self.clicked.emit()

    def show_card(self):
        rows = [(k, lit, key, drag, self._sentences[k]) for k, lit, key, drag in self._actions]
        card = MouseHintCard(rows, self._title, self.window())
        pos = self.mapToGlobal(self.rect().bottomRight())
        card.move(pos.x() - card.width(), pos.y() + config.S(6))
        card.show()
        self._card = card
        return card
