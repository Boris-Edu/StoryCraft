#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: story_workspace.py
ROLE: GUI View (page 5 of the main stack, "Story" on the workspace rail)
DESCRIPTION:
The story as a column of cards in play order, one per story-board block, with what is not in the
story ("Available", in recording order) on the right. Drag a card to reorder it; hover the gap between
two sentences to split a block there; join two cards that follow each other in the recording; leave out
a block's first or last sentence (or the whole block); drag a passage (or one sentence) from Available
into the column, or add it with + in recording order. Blocks are always sentence ranges of the
recording, so every split and trim is at a sentence boundary.

The data is the story board (gui/panels/story_panel.py): every change is one board.load(...), which
syncs the story to the rest of the app, saves it and records one undo step. The board calls back
through board.story_changed, so this page and the board panel always show the same story.
An empty story shows a start card: Build with AI (the existing Copy for AI / Import AI edit round
trip), the whole interview, or start empty and drag from Available.
Classic palette like the rest of the UI (gui/theme.py maps it).

Public API: StoryWorkspaceView(win, parent=None) with refresh() and stop().
"""
import sys

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QContextMenuEvent, QDrag, QFontMetrics, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

import config
import story_edit
from gui import theme
from gui.icons import icon_pixmap

S, FS = config.S, config.FS
MIME = "application/x-storycraft-story"

PAGE_BG = "#1e1e1e"
CARD = "#262626"
CARD_HOVER = "#2a2a2a"
WELL = "#222222"
LINE = "#303030"
TEXT = "#ffffff"
TEXT_2 = "#d4d4d4"
SECONDARY = "#a3a3a3"
MUTED = "#8a8a8a"
VIOLET = "#a855f7"
VIOLET_2 = "#c084fc"
GRAD = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
_UNDO_KEY = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"


def _mmss(seconds):
    seconds = int(round(max(0.0, float(seconds or 0))))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ----------------------------------------------------------------------------------------------
# The edits (pure: lists of (first, last) sentence ranges in, new lists out)
# ----------------------------------------------------------------------------------------------

def _tuples(blocks):
    return [(int(b[0]), int(b[1])) for b in blocks]


def op_move(edit, k, to):
    """Block k moved to insertion position `to` (counted in the list before the move)."""
    edit = _tuples(edit)
    if not 0 <= k < len(edit):
        return edit
    blk = edit.pop(k)
    if to > k:
        to -= 1
    edit.insert(max(0, min(to, len(edit))), blk)
    return edit


def op_split(edit, k, at):
    """Block k split before sentence `at`: (f, at-1), (at, l), in place."""
    edit = _tuples(edit)
    f, l = edit[k]
    if f < at <= l:
        edit[k:k + 1] = [(f, at - 1), (at, l)]
    return edit


def can_join(edit, k):
    """Blocks k and k+1 are one stretch of the recording (k+1 starts right after k ends)."""
    return 0 <= k < len(edit) - 1 and edit[k + 1][0] == edit[k][1] + 1


def op_join(edit, k):
    edit = _tuples(edit)
    if can_join(edit, k):
        edit[k:k + 2] = [(edit[k][0], edit[k + 1][1])]
    return edit


def op_leave_out(edit, unused, a, b):
    """Sentences a..b out of the story where they are (a block can split in two); what was taken
    out joins Available as its own block, like Remove on the story board."""
    new_edit, removed = [], []
    for f, l in _tuples(edit):
        if l < a or f > b:
            new_edit.append((f, l))
            continue
        new_edit.extend(story_edit._minus((f, l), a, b))
        removed.append((max(f, a), min(l, b)))
    return new_edit, sorted(_tuples(unused) + removed)


def op_insert(edit, unused, a, b, at):
    """Sentences a..b (all in Available) as a new block at position `at`; None if any is in the story."""
    edit = _tuples(edit)
    used = {i for f, l in edit for i in range(f, l + 1)}
    if a > b or any(i in used for i in range(a, b + 1)):
        return None
    pieces = []
    for blk in _tuples(unused):
        pieces.extend(story_edit._minus(blk, a, b))
    edit.insert(max(0, min(at, len(edit))), (a, b))
    return edit, sorted(pieces)


def _coverage(*lists):
    out = []
    for blocks in lists:
        for f, l in blocks:
            out.extend(range(f, l + 1))
    return out


def valid_change(before_edit, before_unused, edit, unused):
    """The board's rules: no sentence in two blocks, first <= last, the same sentences covered."""
    if any(f > l for f, l in list(edit) + list(unused)):
        return False
    cov = _coverage(edit, unused)
    return len(cov) == len(set(cov)) and set(cov) == set(_coverage(before_edit, before_unused))


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------

def _label(text, size, color, weight=400, wrap=False):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(f"color: {color}; font-size: {FS(size)}pt; font-weight: {weight}; background: transparent;"
                      f" border: none; padding: 0;")
    return lbl


def _glyph(kind, color, px):
    """Play / stop glyph (icons.py has none), drawn at 2x."""
    s = px * 2
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    if kind == "play":
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.18)
        path.lineTo(s * 0.84, s * 0.5)
        path.lineTo(s * 0.30, s * 0.82)
        path.closeSubpath()
        p.drawPath(path)
    else:
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.24, s * 0.52, s * 0.52), s * 0.06, s * 0.06)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def _icon(name, color, px):
    return QIcon(icon_pixmap(name, theme.color(color), px))


def _begin_drag(source, payload, look):
    """Start dragging `payload` ("story:k" / "avail:a,b") with a faded picture of `look`."""
    md = QMimeData()
    md.setData(MIME, payload.encode("utf-8"))
    drag = QDrag(source)
    drag.setMimeData(md)
    try:
        pm = look.grab()
        ratio = pm.devicePixelRatio() or 1.0
        if pm.width() / ratio > S(440):
            pm = pm.copy(0, 0, int(S(440) * ratio), pm.height())
        if pm.height() / ratio > S(160):
            pm = pm.copy(0, 0, pm.width(), int(S(160) * ratio))
        faded = QPixmap(pm.size())
        faded.setDevicePixelRatio(ratio)
        faded.fill(Qt.transparent)
        p = QPainter(faded)
        p.setOpacity(0.85)
        p.drawPixmap(0, 0, pm)
        p.end()
        drag.setPixmap(faded)
        drag.setHotSpot(QPoint(S(18), S(14)))
    except Exception:
        pass
    drag.exec(Qt.MoveAction)


def _payload(mime):
    if mime is None or not mime.hasFormat(MIME):
        return None
    try:
        return bytes(mime.data(MIME)).decode("utf-8")
    except Exception:
        return None


class _DragArea(QWidget):
    """A widget that starts a drag after the mouse moves a little with the button down."""

    def __init__(self, payload_fn, look=None, parent=None):
        super().__init__(parent)
        self._payload_fn = payload_fn
        self._look = look
        self._press = None
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press is not None and e.buttons() & Qt.LeftButton
                and (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance()):
            self._press = None
            _begin_drag(self, self._payload_fn(), self._look or self)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press = None
        super().mouseReleaseEvent(e)


class _Dots(QWidget):
    """The drag handle: two columns of three dots."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(S(12), S(18))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hot = False

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc("#8a8a8a" if self.hot else "#5a5a5a"))
        d = S(2.6)
        cy = self.height() / 2
        for x in (S(3), S(8)):
            for dy in (-S(5), 0, S(5)):
                p.drawEllipse(QRectF(x - d / 2, cy + dy - d / 2, d, d))
        p.end()


# ----------------------------------------------------------------------------------------------
# Centre: story cards
# ----------------------------------------------------------------------------------------------

class _Gap(QWidget):
    """Between two sentences of a card: on hover a dashed line and "Split here"; a click splits."""

    def __init__(self, view, k, at, parent=None):
        super().__init__(parent)
        self.view, self.k, self.at = view, k, at
        self._hot = False
        self._font = self.font()
        self._font.setPointSize(FS(8.5))
        self._font.setBold(True)
        self.setFixedHeight(QFontMetrics(self._font).height() + S(4))
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._text = view.t("sw_split", "Split here")
        self.setToolTip(view.t("sw_split_tt", "Split this block in two here"))
        self.setAccessibleName(self._text)

    def enterEvent(self, e):
        self._hot = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hot = False
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.view.split(self.k, self.at)
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        if not self._hot:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        pen = QPen(theme.qc(VIOLET_2), 1.2, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(0, cy), QPointF(self.width(), cy))
        fm = QFontMetrics(self._font)
        ic = S(12)
        w = fm.horizontalAdvance(self._text) + ic + S(22)
        h = self.height()
        r = QRectF(self.width() - w - S(8), cy - h / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc("#2a2140"))
        p.drawRoundedRect(r, h / 2, h / 2)
        p.drawPixmap(int(r.left() + S(8)), int(cy - ic / 2), icon_pixmap("cleanup", theme.color(VIOLET_2), ic))
        p.setPen(theme.qc("#e9d5ff"))
        p.setFont(self._font)
        p.drawText(r.adjusted(ic + S(12), 0, -S(8), 0), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.end()


# -- the mouse on a sentence line (story cards and Available) ---------------------------------------

_MODS = Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier | Qt.MetaModifier


def _alt_only(e):
    """⌥ / Alt alone: from the event, or the live keyboard state when the event has none (as Clean reads it)."""
    mods = e.modifiers() & _MODS
    if mods:
        return mods == Qt.AltModifier
    return (QApplication.keyboardModifiers() & _MODS) == Qt.AltModifier


def _line_press(row, e, label, play):
    """⌥ right-click: the playhead on the word under the mouse; ⌥ click: play(). True when handled."""
    row._menu_eaten = False
    if not _alt_only(e) or e.button() not in (Qt.RightButton, Qt.LeftButton):
        return False
    if e.button() == Qt.RightButton:
        row._menu_eaten = True   # no menu for this click (it follows the press)
        row.view.jump_to_word(row.i, label, label.mapFrom(row, e.position().toPoint()))
    else:
        play()
    e.accept()
    return True


def _line_double(row, e):
    """Double-click: correct the words of the line's sentence (after this event: the line may be rebuilt)."""
    if e.button() != Qt.LeftButton or _alt_only(e):
        return False
    e.accept()
    view, i = row.view, row.i
    QTimer.singleShot(0, lambda: view.correct_words(i))
    return True


def _menu_eaten(row, e):
    eaten, row._menu_eaten = getattr(row, "_menu_eaten", False), False
    if e.reason() == QContextMenuEvent.Reason.Mouse and (eaten or _alt_only(e)):
        e.accept()
        return True
    return False


def _hit_token(label, pos):
    """(index of the word token under pos, tokens) for the text of `label`, laid out as the label lays it
    out (same font, width, wrap and alignment); (None, tokens) when pos is not on the text."""
    import re
    from PySide6.QtGui import Qt as GuiQt, QTextDocument, QTextOption
    text = label.text()
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(label.font())
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WordWrap if label.wordWrap() else QTextOption.NoWrap)
    opt.setAlignment(label.alignment() & Qt.AlignHorizontal_Mask)
    doc.setDefaultTextOption(opt)
    fmt = label.textFormat()
    if fmt == Qt.RichText or (fmt == Qt.AutoText and GuiQt.mightBeRichText(text)):
        doc.setHtml(text)
    else:
        doc.setPlainText(text)
    found = list(re.finditer(r"[^\s  ]+", doc.toPlainText()))
    toks, spans = [f.group() for f in found], [(f.start(), f.end()) for f in found]
    m = label.margin()
    cr = label.contentsRect().adjusted(m, m, -m, -m)
    doc.setTextWidth(max(1, cr.width()))
    h = doc.size().height()
    al = label.alignment()
    top = cr.top() + ((cr.height() - h) / 2 if al & Qt.AlignVCenter else (cr.height() - h if al & Qt.AlignBottom else 0))
    p = QPointF(pos.x() - cr.left(), pos.y() - top)
    slack = S(3)
    if not spans or p.y() < -slack or p.y() > h + slack or p.x() < -slack or p.x() > cr.width() + slack:
        return None, toks
    c = doc.documentLayout().hitTest(p, Qt.FuzzyHit)
    if c < 0:
        return None, toks
    for n, (a, b) in enumerate(spans):
        if c <= b:
            return n, toks
    return len(spans) - 1, toks


def _token_word(toks, texts, n):
    """Which of `texts` (the sentence's transcript words) token n of the line is: matched by spelling
    (the line may lack a cut word or spell one differently), else by its place in the line."""
    import difflib
    import re

    def norm(x):
        return re.sub(r"\W+", "", str(x).lower())
    sm = difflib.SequenceMatcher(None, [norm(x) for x in toks], [norm(x) for x in texts], autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        if a <= n < a + size:
            return b + n - a
    if len(toks) <= 1 or len(texts) <= 1:
        return 0
    return min(len(texts) - 1, int(round(n * (len(texts) - 1) / (len(toks) - 1))))


class _SentRow(QWidget):
    """One sentence of a story card: its id, its words, and (first / last of a longer block) a quiet
    "leave out" button on hover. Right-click for the sentence's menu; ⌥ right-click puts the playhead on a
    word, ⌥ click plays from here, double-click corrects the words."""

    def __init__(self, view, k, i, removable, parent=None):
        super().__init__(parent)
        self.view, self.k, self.i = view, k, i
        s = view.sentences[i]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(S(10))
        sid = _label(s.get("id", ""), 8.5, MUTED, 600)
        sid.setFixedWidth(view.id_width)
        sid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        sid.setContentsMargins(0, S(2), 0, 0)
        lay.addWidget(sid, 0, Qt.AlignTop)
        ai = view.ai_sentence(i)
        self.text = _label(s.get("text", ""), 10.5, "#67e8f9" if ai else TEXT_2, wrap=True)
        if ai:   # synthetic speech (a voice patch in the edit), as in the transcript
            self.text.setToolTip(view.t("ai_voice_legend_tt", "Words in this colour are synthetic speech (a voice "
                                        "patch in the story, made with VoiceStudio)."))
        self.text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.text.setMinimumWidth(S(80))
        lay.addWidget(self.text, 1)
        self.btn_out = None
        if removable:
            b = QPushButton()
            b.setObjectName("RowX")
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(_icon("close", SECONDARY, S(12)))
            b.setFixedSize(S(28), S(22))   # as wide as the header's buttons: the x's line up
            first = i == view.edit[k][0]
            b.setToolTip(view.t("sw_trim_first_tt", "Leave the first sentence out (it goes to Available)") if first
                         else view.t("sw_trim_last_tt", "Leave the last sentence out (it goes to Available)"))
            b.setAccessibleName(b.toolTip())
            b.clicked.connect(lambda: view.leave_out(i, i))
            sp = b.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            b.setSizePolicy(sp)
            b.hide()
            lay.addWidget(b, 0, Qt.AlignTop)
            self.btn_out = b

    def set_live(self, word_idx):
        """The word being spoken lit (white pill, as in Clean) and this line marked; None: back to normal."""
        if not hasattr(self, "_base_text"):
            self._base_text = self.text.text()
            self._base_style = self.text.styleSheet()
        self._live_word = word_idx
        if word_idx is None:
            self.text.setTextFormat(Qt.AutoText)
            self.text.setText(self._base_text)
            self.setStyleSheet("")
            # Back to a see-through row: left styled, it paints the app's default dark background (a band
            # on every line the playhead has passed)
            self.setAttribute(Qt.WA_StyledBackground, False)
            self.setObjectName("")
            self.layout().setContentsMargins(0, 0, 0, 0)
            return
        import html as _h
        canvas = getattr(self.view.win, "text_canvas", None)
        words = getattr(canvas, "words_data", None) or []
        parts = []
        for j in self.view.line_words(self.i):
            t = _h.escape(str(words[j].get("text", "")).strip())
            parts.append(f"<span style='background-color:#ffffff; color:#1a1a1a;'>&nbsp;{t}&nbsp;</span>" if j == word_idx else t)
        if parts:
            self.text.setTextFormat(Qt.RichText)
            self.text.setText(" ".join(parts))
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.layout().setContentsMargins(S(8), S(2), 0, S(2))   # room for the line marker
        self.setObjectName("SentLive")
        self.setStyleSheet(f"QWidget#SentLive {{ background-color: #1f1a2c; border-left: 3px solid #a855f7;"
                           f" border-radius: {S(4)}px; }}")

    def enterEvent(self, e):
        if self.btn_out is not None:
            self.btn_out.show()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.btn_out is not None:
            self.btn_out.hide()
        super().leaveEvent(e)

    # ⌥ right-click a word: playhead there · ⌥ click: play from here · double-click: correct the words
    def mousePressEvent(self, e):
        if _line_press(self, e, self.text, lambda: self.view.play_from(self.k, self.i)):
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if _line_double(self, e):
            return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e):
        if _menu_eaten(self, e):
            return
        # the line the menu is about stays lit while it is open
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("SentRowMenu")
        self.setStyleSheet(f"QWidget#SentRowMenu {{ background-color: #2a2140; border-radius: {S(6)}px; }}")
        self.view.sentence_menu(self.k, self.i, e.globalPos())
        try:   # the row may be gone already (the story was rebuilt)
            if self.view._live_row is self:
                self.set_live(getattr(self, "_live_word", None))
            else:   # see-through again, as set_live(None) leaves it (left styled, it paints a dark band)
                self.setStyleSheet("")
                self.setAttribute(Qt.WA_StyledBackground, False)
                self.setObjectName("")
        except RuntimeError:
            pass


class _Card(QFrame):
    """A story block: drag handle, number, sentence ids, length, play and remove; its sentences below."""

    def __init__(self, view, k, block, parent=None):
        super().__init__(parent)
        self.view, self.k, self.block = view, k, block
        self.setObjectName("StoryCard")
        f, l = block
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(10), S(8), S(12), S(10))
        lay.setSpacing(0)

        self.head = _DragArea(lambda: f"story:{self.k}", self, self)
        self.head.setObjectName("CardHead")
        self.head.setToolTip(view.t("sw_drag_tt", "Drag to move this block"))
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(0, 0, 0, S(6))
        hl.setSpacing(S(8))
        self.dots = _Dots(self.head)
        hl.addWidget(self.dots, 0, Qt.AlignVCenter)
        num = _label(str(k + 1), 9, VIOLET_2, 700)
        hl.addWidget(num, 0, Qt.AlignVCenter)
        s = view.sentences
        ids = s[f]["id"] if f == l else f"{s[f]['id']}–{s[l]['id']}"
        hl.addWidget(_label(ids, 9, SECONDARY, 600), 0, Qt.AlignVCenter)
        self.dur = QLabel(_mmss(view.block_duration(block)))
        self.dur.setObjectName("DurPill")
        hl.addWidget(self.dur, 0, Qt.AlignVCenter)
        # where the block starts in the story (edit time); the recording time in its tooltip
        self.at = _label("", 8.5, MUTED)
        hl.addWidget(self.at, 0, Qt.AlignVCenter)
        self.set_start(view.block_start(k))
        hl.addStretch(1)
        self.btn_play = QPushButton()
        self.btn_play.setObjectName("IconBtn")
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setFixedSize(S(28), S(26))
        self.btn_play.clicked.connect(lambda: view.play_card(self.k))
        hl.addWidget(self.btn_play, 0, Qt.AlignVCenter)
        self.btn_remove = QPushButton()
        self.btn_remove.setObjectName("IconBtn")
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.setFixedSize(S(28), S(26))
        self.btn_remove.setIcon(_icon("close", SECONDARY, S(13)))
        self.btn_remove.setToolTip(view.t("sw_remove_tt", "Remove from the story (its sentences go to Available)"))
        self.btn_remove.setAccessibleName(self.btn_remove.toolTip())
        self.btn_remove.clicked.connect(lambda: view.remove(self.k))
        hl.addWidget(self.btn_remove, 0, Qt.AlignVCenter)
        lay.addWidget(self.head)

        self.rows = []
        for i in range(f, l + 1):
            if i > f:
                lay.addWidget(_Gap(view, k, i, self))
            row = _SentRow(view, k, i, removable=(l > f and i in (f, l)), parent=self)
            lay.addWidget(row)
            self.rows.append(row)
        self.set_playing(False)

    def set_start(self, start):
        import story_time as st
        view, f = self.view, self.block[0]
        self.at.setText(view.t("sw_at", "at {time}").format(time=st.fmt_time(start)))
        s = view.sentences[f]
        rec = st.fmt_time(s.get("src_start", s.get("cut_start", 0)))
        self.at.setToolTip(view.t("time_in_recording", "{time} in the recording").format(time=rec))

    def add_marks(self, texts):
        """Versions > Compare: what changed in this block since that version (pills in its header)."""
        hl = self.head.layout()
        at = hl.indexOf(self.at) + 1
        for n, txt in enumerate(texts):
            p = QLabel(txt)
            p.setStyleSheet(f"QLabel {{ background-color: #3a2f12; color: #fcd34d; border: 1px solid #a16207;"
                            f" border-radius: {S(8)}px; padding: {S(1)}px {S(8)}px; font-size: {FS(8.5)}pt;"
                            f" font-weight: 600; }}")
            hl.insertWidget(at + n, p, 0, Qt.AlignVCenter)
        self.setProperty("changed", "true")
        self.setStyleSheet(self.styleSheet() + f" QFrame#StoryCard {{ border: 1px solid #a16207; }}")

    def set_playing(self, on):
        self.btn_play.setIcon(_glyph("stop" if on else "play", theme.color(VIOLET_2 if on else TEXT_2), S(14)))
        self.btn_play.setToolTip(self.view.t("sw_stop_tt", "Stop") if on else
                                 self.view.t("sw_play_tt", "Play this block from the recording"))
        self.btn_play.setAccessibleName(self.btn_play.toolTip())
        self.setProperty("playing", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def enterEvent(self, e):
        self.dots.hot = True
        self.dots.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.dots.hot = False
        self.dots.update()
        super().leaveEvent(e)


class _Joint(QWidget):
    """Between cards k and k+1: the cut (opens Review on it) and, when they follow each other in the
    recording, Join."""

    def __init__(self, view, k, joinable, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(S(4), S(4), S(4), S(4))
        lay.setSpacing(S(8))
        lay.addStretch(1)
        ok = False
        try:   # Review's "Sounds good" (saved with the story): a ✓ on the chip
            a_id = view.sentences[view.edit[k][1]].get("id")
            b_id = view.sentences[view.edit[k + 1][0]].get("id")
            ok = f"{a_id}>{b_id}" in getattr(view, "_ok_keys", set())
        except Exception:
            pass
        self.btn_cut = QPushButton(("✓ " if ok else "") + view.t("sw_cut", "cut {n}").format(n=k + 1))
        self.btn_cut.setObjectName("Chip")
        self.btn_cut.setIcon(_icon("cleanup", SECONDARY, S(12)))
        self.btn_cut.setCursor(Qt.PointingHandCursor)
        self.btn_cut.setToolTip(view.t("sw_cut_ok_tt", "Checked in Review: sounds good") if ok else
                                view.t("sw_cut_tt", "Hear and judge this cut in Review"))
        self.btn_cut.clicked.connect(lambda: view.open_cut(k + 1))
        lay.addWidget(self.btn_cut)
        self.btn_join = None
        if joinable:
            self.btn_join = QPushButton(view.t("sw_join", "Join"))
            self.btn_join.setObjectName("ChipJoin")
            self.btn_join.setCursor(Qt.PointingHandCursor)
            self.btn_join.setToolTip(view.t("sw_join_tt", "Join these two blocks: they follow each other in the recording"))
            self.btn_join.clicked.connect(lambda: view.join(k))
            lay.addWidget(self.btn_join)
        lay.addStretch(1)


class _StoryColumn(QWidget):
    """The cards and the joints between them; takes drops (a card to move, a passage from Available)."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setAcceptDrops(True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(S(2), S(2), S(10), S(16))
        self.lay.setSpacing(0)
        self.cards = []
        self.indicator = QFrame(self)
        self.indicator.setObjectName("DropLine")
        self.indicator.setFixedHeight(S(3))
        self.indicator.hide()
        self.placeholder = None

    def drop_index(self, y):
        """Where a drop at y (column coordinates) lands: before the first card whose middle is below y."""
        for k, c in enumerate(self.cards):
            g = c.geometry()
            if y < g.top() + g.height() / 2:
                return k
        return len(self.cards)

    def _indicator_y(self, k):
        if not self.cards:
            return S(8)
        if k <= 0:
            return max(0, self.cards[0].geometry().top() - S(4))
        if k >= len(self.cards):
            return self.cards[-1].geometry().bottom() + S(4)
        return self.cards[k - 1].geometry().bottom() + S(3)   # above the joint's chips, not over them

    def show_indicator(self, k):
        y = self._indicator_y(k)
        self.indicator.setGeometry(S(6), int(y - S(1.5)), max(10, self.width() - S(22)), S(3))
        self.indicator.show()
        self.indicator.raise_()

    def dragEnterEvent(self, e):
        if _payload(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if not _payload(e.mimeData()):
            e.ignore()
            return
        y = int(e.position().y())
        self.show_indicator(self.drop_index(y))
        self.view.autoscroll(self, y)
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.indicator.hide()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self.indicator.hide()
        p = _payload(e.mimeData())
        if not p:
            e.ignore()
            return
        k = self.drop_index(int(e.position().y()))
        e.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.view.drop_payload(p, k))   # after the drag has ended


# ----------------------------------------------------------------------------------------------
# Right: Available
# ----------------------------------------------------------------------------------------------

class _AvailSent(_DragArea):
    """A sentence in Available: drag it (alone) into the story; right-click for its menu."""

    def __init__(self, view, i, state, parent=None):
        super().__init__(lambda: f"avail:{i},{i}", None, parent)
        self.view, self.i = view, i
        self.setToolTip(view.t("sw_drag_sentence_tt", "Drag this sentence into the story"))
        s = view.sentences[i]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, S(2), 0, S(2))
        lay.setSpacing(S(8))
        sid = _label(s.get("id", ""), 8, MUTED, 600)
        sid.setFixedWidth(view.id_width_small)
        sid.setContentsMargins(0, S(2), 0, 0)
        lay.addWidget(sid, 0, Qt.AlignTop)
        color = TEXT if state == "hit" else (MUTED if state == "dim" else "#c8c8c8")
        txt = _label(s.get("text", ""), 9.5, color, 400, wrap=True)
        txt.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        txt.setMinimumWidth(S(60))
        lay.addWidget(txt, 1)
        self.text = txt
        if state == "hit":
            self.setObjectName("AvailHit")

    # the same mouse as a story line (⌥ click plays this sentence)
    def mousePressEvent(self, e):
        if _line_press(self, e, self.text, lambda: self.view._play_span(None, self.i, self.i)):
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if _line_double(self, e):
            return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e):
        if _menu_eaten(self, e):
            return
        self.view.avail_menu(self.i, e.globalPos())


class _AvailBlock(QFrame):
    """A Not used block: ids, where it is in the recording and its length, + to add it; its sentences."""

    def __init__(self, view, block, query, parent=None):
        super().__init__(parent)
        self.setObjectName("AvailCard")
        f, l = block
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(10), S(6), S(8), S(8))
        lay.setSpacing(S(2))
        head = _DragArea(lambda: f"avail:{f},{l}", self, self)
        head.setToolTip(view.t("sw_drag_block_tt", "Drag into the story"))
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(S(6))
        dots = _Dots(head)
        hl.addWidget(dots, 0, Qt.AlignVCenter)
        s = view.sentences
        ids = s[f]["id"] if f == l else f"{s[f]['id']}–{s[l]['id']}"
        hl.addWidget(_label(ids, 8.5, SECONDARY, 600), 0, Qt.AlignVCenter)
        at = view.t("sw_at", "at {time}").format(time=_mmss(s[f].get("src_start", s[f].get("cut_start", 0))))
        meta = _label(f"{at} · {_mmss(view.block_duration(block))}", 8.5, MUTED)
        meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        meta.setToolTip(view.t("time_in_recording", "{time} in the recording").format(   # not in the story yet
            time=_mmss(s[f].get("src_start", s[f].get("cut_start", 0)))))
        hl.addWidget(meta, 1, Qt.AlignVCenter)
        self.btn_add = QPushButton()
        self.btn_add.setObjectName("AddBtn")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.setFixedSize(S(26), S(24))
        self.btn_add.setIcon(_icon("plus", TEXT, S(13)))
        self.btn_add.setToolTip(view.t("sw_add_tt", "Add to the story, in recording order"))
        self.btn_add.setAccessibleName(self.btn_add.toolTip())
        self.btn_add.clicked.connect(lambda: view.add(f, l))
        hl.addWidget(self.btn_add, 0, Qt.AlignVCenter)
        lay.addWidget(head)
        q = (query or "").strip().lower()
        for i in range(f, l + 1):
            state = None
            if q:
                state = "hit" if view.sentence_matches(i, q) else "dim"
            lay.addWidget(_AvailSent(view, i, state, self))


class _AvailPanel(QFrame):
    """The Available column; a card dropped here leaves the story."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setObjectName("AvailPanel")
        self.setAcceptDrops(True)
        self.setProperty("droptarget", False)

    def _set_hot(self, on):
        self.setProperty("droptarget", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        p = _payload(e.mimeData())
        if p and p.startswith("story:"):
            self._set_hot(True)
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        p = _payload(e.mimeData())
        if p and p.startswith("story:"):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_hot(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_hot(False)
        p = _payload(e.mimeData())
        if not p or not p.startswith("story:"):
            e.ignore()
            return
        e.acceptProposedAction()
        QTimer.singleShot(0, lambda: self.view.drop_payload(p, None))


# ----------------------------------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------------------------------

class StoryWorkspaceView(QWidget):
    """The story, block by block. refresh() redraws from the story board; stop() stops playback."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.sentences, self.edit, self.unused = [], [], []
        self._built = None           # (sentences signature, edit) the column was built for
        self._built_avail = None     # (sentences signature, unused, query) Available was built for
        self._fallback = (None, [])  # transcript sentences while the board has none: (key, sentences)
        self._dismissed_for = None   # "Start empty" chosen for this board (its save path)
        self._dirty = True
        self._playing = None         # card index being played
        self._compare_v = None       # a version shown against the story (Versions > Compare): marks on the cards
        self._live_row = None        # the sentence row of the word being spoken
        self._play_range = None
        self._play_started = 0.0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(lambda: self._build_avail(force=True))
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(80)
        self._play_timer.timeout.connect(self._play_tick)
        self.setObjectName("StoryWorkspace")
        self.setAttribute(Qt.WA_StyledBackground, True)
        fm_id = QFontMetrics(self.font())
        f = self.font()
        f.setPointSize(FS(8.5))
        f.setBold(True)
        self.id_width = QFontMetrics(f).horizontalAdvance("S0000") + S(4)
        f.setPointSize(FS(8))
        self.id_width_small = QFontMetrics(f).horizontalAdvance("S0000") + S(2)
        del fm_id
        self._style()
        self._build()
        board = getattr(win, "story_board", None)
        if board is not None:
            if not hasattr(board, "story_changed"):
                board.story_changed = []
            board.story_changed.append(self._on_board_changed)

    def t(self, key, default, **kw):
        txt = getattr(self.win, "txt", None)
        s = txt(key, default) if txt else default
        if kw:
            try:
                return s.format(**kw)
            except Exception:
                return s
        return s

    # -- layout ------------------------------------------------------------------------------

    def _style(self):
        self.setStyleSheet(f"""
            QWidget#StoryWorkspace {{ background-color: {PAGE_BG}; }}
            QScrollArea {{ background: transparent; border: none; }}
            QWidget#ColumnHost, QWidget#AvailHost {{ background: transparent; }}
            QFrame#StoryCard {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {S(12)}px; }}
            QFrame#StoryCard:hover {{ border-color: #3d3d3d; }}
            QFrame#StoryCard[playing="true"] {{ border: 1px solid {VIOLET}; background-color: #2a2433; }}
            QWidget#CardHead {{ background: transparent; }}
            QLabel#DurPill {{ background-color: #1e1e1e; color: {TEXT_2}; border-radius: {S(8)}px;
                padding: {S(1)}px {S(8)}px; font-size: {FS(8.5)}pt; font-weight: 600; }}
            QPushButton#IconBtn {{ background: transparent; border: 1px solid transparent; border-radius: {S(7)}px; }}
            QPushButton#IconBtn:hover {{ background-color: #333333; border-color: #3a3a3a; }}
            QPushButton#RowX {{ background: transparent; border: none; border-radius: {S(6)}px; }}
            QPushButton#RowX:hover {{ background-color: #3a2a2a; }}
            QPushButton#Chip {{ background-color: transparent; color: {MUTED}; border: 1px solid {LINE};
                border-radius: {S(10)}px; padding: {S(2)}px {S(10)}px; font-size: {FS(8.5)}pt; }}
            QPushButton#Chip:hover {{ color: {TEXT}; border-color: #4a4a4a; background-color: #2a2a2a; }}
            QPushButton#ChipJoin {{ background-color: #2a2140; color: #e9d5ff; border: 1px solid #4c3a75;
                border-radius: {S(10)}px; padding: {S(2)}px {S(12)}px; font-size: {FS(8.5)}pt; font-weight: 600; }}
            QPushButton#ChipJoin:hover {{ background-color: #3a2c5c; color: {TEXT}; }}
            QFrame#DropLine {{ background-color: {VIOLET_2}; border: none; border-radius: {S(1)}px; }}
            QFrame#AvailPanel {{ background-color: {WELL}; border: 1px solid {LINE}; border-radius: {S(16)}px; }}
            QFrame#AvailPanel[droptarget="true"] {{ border: 1px dashed {VIOLET_2}; }}
            QFrame#AvailCard {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {S(10)}px; }}
            QFrame#AvailCard:hover {{ border-color: #3d3d3d; }}
            QWidget#AvailHit {{ background-color: #2a2140; border-radius: {S(5)}px; }}
            QPushButton#AddBtn {{ background-color: #2a2a2a; border: 1px solid #333333; border-radius: {S(7)}px; }}
            QPushButton#AddBtn:hover {{ background-color: #1f3d2c; border-color: #2f6b45; }}
            QLineEdit#Search {{ background-color: #1e1e1e; color: {TEXT}; border: 1px solid {LINE};
                border-radius: {S(9)}px; padding: {S(5)}px {S(10)}px; font-size: {FS(9.5)}pt; }}
            QLineEdit#Search:focus {{ border-color: #6b4fa8; }}
            QPushButton#Quiet {{ background-color: #2a2a2a; color: {TEXT}; border: 1px solid #333333; border-radius: {S(9)}px;
                padding: {S(6)}px {S(12)}px; font-size: {FS(9.5)}pt; }}
            QPushButton#Quiet:hover {{ background-color: #333333; }}
            QPushButton#Quiet:disabled {{ color: #666666; }}
            QPushButton#Quiet[narrow="true"] {{ padding: {S(6)}px {S(4)}px; }}
            QPushButton#Quiet[narrow="true"]::menu-indicator {{ image: none; width: 0px; }}
            QPushButton#Primary {{ background: {GRAD}; color: #0b0b14; border: none; border-radius: {S(9)}px;
                padding: {S(7)}px {S(14)}px; font-size: {FS(9.5)}pt; font-weight: 600; }}
            QPushButton#Primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc); }}
            QFrame#StartCard {{ background-color: {CARD}; border: 1px solid {LINE}; border-radius: {S(16)}px; }}
            QFrame#StartOption {{ background-color: #222222; border: 1px solid {LINE}; border-radius: {S(12)}px; }}
            QFrame#EmptyDrop {{ background: transparent; border: 1px dashed #3a3a3a; border-radius: {S(12)}px; }}
        """)

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(S(20), S(12), S(20), S(16))   # top: same as the editor bar (the step track lines up)
        outer.setSpacing(S(18))

        # ── centre ─────────────────────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(S(12))
        top = QHBoxLayout()
        top.setSpacing(S(8))
        # The story and the tools around it: the same bar, same place, on every work page (it names the page)
        from gui.widgets.step_track import HubNav
        self.step_track = HubNav(self.win, active="story")
        self.step_track.page_decides = True                       # this page fits its top row (_fit_top_row)
        self.step_track.on_essentials_changed = self._fit_top_row  # the status's length / Resolve parts resized
        top.addWidget(self.step_track, 0, Qt.AlignVCenter)
        self.lbl_title = _label(self.t("insp_story", "Story"), 15, TEXT, 600)   # kept for callers; not shown
        self.lbl_title.hide()
        self.lbl_meta = _label("", 9.5, MUTED, 500)
        top.addWidget(self.lbl_meta, 0, Qt.AlignVCenter)
        # The story's target length (it was only in the old story panel): '2:00', '2m', '90s'
        self.edit_target = QLineEdit()
        self.edit_target.setObjectName("TargetEdit")
        self.edit_target.setPlaceholderText("2:00")
        self.edit_target.setToolTip(self.t("tt_board_target", "Target length of the story (2:00, 2m, 90s)"))
        self.edit_target.setFixedWidth(S(60))
        self.edit_target.setAlignment(Qt.AlignCenter)
        self.edit_target.setStyleSheet(f"QLineEdit#TargetEdit {{ background: #1e1e1e; color: {TEXT}; border: 1px solid #333333;"
                                       f" border-radius: {S(8)}px; padding: {S(3)}px {S(6)}px; font-size: {FS(9.5)}pt; }}"
                                       f" QLineEdit#TargetEdit:focus {{ border-color: #6b4fa8; }}")
        board = getattr(self.win, "story_board", None)
        if board is not None and hasattr(board, "edit_target"):
            self.edit_target.setText(board.edit_target.text())
        self.edit_target.editingFinished.connect(self._target_changed)
        # the row's own spacing (S(8)) separates bar, word and field: no fixed gaps that stay when the word goes
        self.lbl_target = _label(self.t("sw_target_label", "Target"), 9.5, MUTED, 500)
        self.lbl_target.setContentsMargins(S(4), 0, 0, 0)
        top.addWidget(self.lbl_target, 0, Qt.AlignVCenter)
        top.addWidget(self.edit_target, 0, Qt.AlignVCenter)

        top.addStretch(1)
        self.btn_copy_ai = QPushButton(self.t("board_copy_ai", "Copy for AI"))
        self.btn_copy_ai.setToolTip(self.t("tt_board_copy_ai", "Copy the transcript for an AI chat"))
        self.btn_copy_ai.clicked.connect(self._copy_for_ai)
        self.btn_import_ai = QPushButton(self.t("board_import_ai", "Import AI story…"))
        self.btn_import_ai.setToolTip(self.t("tt_board_import_ai", "Bring back the story the AI wrote"))
        self.btn_import_ai.clicked.connect(self._import_ai)
        # Versions and Review cuts are the hub bar's own tabs (step_track.HubNav): the page repeats neither
        self.btn_review = self.btn_versions = None
        # One "AI ▾" menu for the AI round trip (Copy for AI · Import AI edit…): the top bar keeps its room
        self.btn_ai = QPushButton(self.t("sw_ai_menu", "AI"))   # Qt draws the menu arrow
        self.btn_ai.setToolTip(self.t("sw_ai_menu_tt", "Build or adjust the story with an AI chat: copy the "
                                      "transcript for it, then import the story it writes"))
        from gui.widgets.menus import context_menu
        self._ai_menu = context_menu(self)
        self._ai_menu.addAction(self.btn_copy_ai.text(), self._copy_for_ai)
        self._ai_menu.addAction(self.btn_import_ai.text(), self._import_ai)
        self.btn_ai.setMenu(self._ai_menu)
        # ⋯: the rest of what the old story panel offered (build from the script, clear)
        self.btn_more = QPushButton("⋯")
        self.btn_more.setToolTip(self.t("sw_more_tt", "More: build the story from the script, clear the story"))
        self._more_menu = context_menu(self)
        self._more_menu.addAction(self.t("sw_from_script", "Build from the script…"), self._from_script)
        self._more_menu.addAction(self.t("sw_clear_story", "Clear the story"), self._clear_story)
        self.btn_more.setMenu(self._more_menu)
        self.btn_more.setProperty("narrow", "true")    # just "⋯": no menu arrow, no wide padding
        self.btn_more.setFixedWidth(S(32))
        for b in (self.btn_copy_ai, self.btn_import_ai, self.btn_ai, self.btn_more):
            b.setObjectName("Quiet")
            b.setCursor(Qt.PointingHandCursor)
            top.addWidget(b, 0, Qt.AlignVCenter)
        self.btn_copy_ai.hide()  # both in the AI ▾ menu (kept as objects for callers)
        self.btn_ai.hide()       # the AI tab holds every AI helper now
        self.btn_import_ai.hide()
        left.addLayout(top)
        # Versions > Compare: what differs from that version, said once above the story (the cards carry marks)
        self.compare_bar = QFrame()
        self.compare_bar.setObjectName("CompareBar")
        self.compare_bar.setStyleSheet(f"QFrame#CompareBar {{ background-color: #241f33; border: 1px solid #4c3a75;"
                                       f" border-radius: {S(12)}px; }}")
        cbl = QVBoxLayout(self.compare_bar)
        cbl.setContentsMargins(S(14), S(10), S(12), S(10))
        cbl.setSpacing(S(6))
        cbh = QHBoxLayout()
        cbh.setSpacing(S(8))
        self.lbl_compare = _label("", 10, TEXT, 600, wrap=True)
        self.lbl_compare.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        cbh.addWidget(self.lbl_compare, 1)
        self.btn_compare_restore = QPushButton(self.t("ver_restore_this", "Restore this version"))
        self.btn_compare_stop = QPushButton(self.t("sw_compare_stop", "Stop comparing"))
        for b in (self.btn_compare_restore, self.btn_compare_stop):
            b.setObjectName("Quiet")
            b.setCursor(Qt.PointingHandCursor)
            cbh.addWidget(b, 0, Qt.AlignTop)
        self.btn_compare_restore.clicked.connect(self._restore_compared)
        self.btn_compare_stop.clicked.connect(self.stop_compare)
        cbl.addLayout(cbh)
        self.compare_removed = QVBoxLayout()
        self.compare_removed.setSpacing(S(4))
        cbl.addLayout(self.compare_removed)
        self.compare_bar.hide()
        left.addWidget(self.compare_bar)

        self._center = QStackedLayout()
        self._center.addWidget(self._build_start())
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.column = _StoryColumn(self)
        self.column.setObjectName("ColumnHost")
        self.scroll.setWidget(self.column)
        self._center.addWidget(self.scroll)
        left.addLayout(self._center, 1)
        left.addLayout(self._build_hint())
        outer.addLayout(left, 1)

        # ── right: Available ─────────────────────────────────────────────────
        self.avail = _AvailPanel(self)
        al = QVBoxLayout(self.avail)
        al.setContentsMargins(S(12), S(14), S(6), S(10))
        al.setSpacing(S(8))
        ah = QVBoxLayout()
        ah.setContentsMargins(S(2), 0, S(6), 0)
        ah.setSpacing(S(2))
        ah.addWidget(_label(self.t("sw_available", "Available"), 12, TEXT, 600))
        self.lbl_avail = _label("", 8.5, MUTED, 500, wrap=True)
        ah.addWidget(self.lbl_avail)
        al.addLayout(ah)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText(self.t("sw_search", "Search what was said…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        sw = QHBoxLayout()
        sw.setContentsMargins(0, 0, S(6), 0)
        sw.addWidget(self.search)
        al.addLayout(sw)
        self.avail_scroll = QScrollArea()
        self.avail_scroll.setWidgetResizable(True)
        self.avail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.avail_scroll.setFrameShape(QFrame.NoFrame)
        self.avail_host = QWidget()
        self.avail_host.setObjectName("AvailHost")
        self.avail_lay = QVBoxLayout(self.avail_host)
        self.avail_lay.setContentsMargins(0, 0, S(8), S(8))
        self.avail_lay.setSpacing(S(6))
        self.avail_scroll.setWidget(self.avail_host)
        al.addWidget(self.avail_scroll, 1)
        # the window's top-right corner holds Home / Settings: Available starts one line lower
        from gui.components.corner_actions import reserved_height
        avail_col = QVBoxLayout()
        avail_col.setContentsMargins(0, 0, 0, 0)
        avail_col.addSpacing(max(0, reserved_height() - S(12)))
        avail_col.addWidget(self.avail, 1)
        outer.addLayout(avail_col)

    def _build_start(self):
        page = QWidget()
        page.setObjectName("StartPage")
        page.setStyleSheet("QWidget#StartPage { background: transparent; }")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, S(8), 0, 0)
        box = QFrame()
        box.setObjectName("StartCard")
        box.setMaximumWidth(S(640))
        lay = QVBoxLayout(box)
        lay.setContentsMargins(S(26), S(24), S(26), S(24))
        lay.setSpacing(S(10))
        icon = QLabel()
        icon.setPixmap(icon_pixmap("story", theme.color(VIOLET_2), S(28)))
        icon.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(icon)
        lay.addWidget(_label(self.t("sw_start_title", "Start the story"), 15, TEXT, 600))
        lay.addWidget(_label(self.t("sw_start_body", "Pick a way in. Afterwards you can move, split, join and trim "
                                    "the blocks here, and add back anything from Available."), 10, SECONDARY, wrap=True))
        lay.addSpacing(S(4))

        def option(title, body, buttons):
            f = QFrame()
            f.setObjectName("StartOption")
            ol = QVBoxLayout(f)
            ol.setContentsMargins(S(16), S(12), S(16), S(14))
            ol.setSpacing(S(6))
            ol.addWidget(_label(title, 11, TEXT, 600))
            ol.addWidget(_label(body, 9.5, SECONDARY, wrap=True))
            row = QHBoxLayout()
            row.setSpacing(S(8))
            for b in buttons:
                b.setCursor(Qt.PointingHandCursor)
                row.addWidget(b)
            row.addStretch(1)
            ol.addSpacing(S(2))
            ol.addLayout(row)
            lay.addWidget(f)

        b_copy = QPushButton(self.t("board_copy_ai", "Copy for AI"))
        b_copy.setObjectName("Primary")
        b_copy.clicked.connect(self._copy_for_ai)
        b_imp = QPushButton(self.t("board_import_ai", "Import AI story…"))
        b_imp.setObjectName("Quiet")
        b_imp.clicked.connect(self._import_ai)
        self.btn_start_copy, self.btn_start_import = b_copy, b_imp
        option(self.t("sw_start_ai", "Build with AI"),
               self.t("sw_start_ai_body", "Copy the transcript, paste it into an AI chat and ask for a story; then "
                      "bring its answer back with Import AI story."), [b_copy, b_imp])
        b_whole = QPushButton(self.t("sw_start_whole", "Start from the whole interview"))
        b_whole.setObjectName("Quiet")
        b_whole.clicked.connect(self.start_whole)
        self.btn_start_whole = b_whole
        option(self.t("sw_start_whole_title", "The whole interview"),
               self.t("sw_start_whole_body", "Every passage in recorded order (parts tagged Avoid stay in Available). "
                      "Then take out what you don't need."), [b_whole])
        b_empty = QPushButton(self.t("sw_start_empty", "Start empty"))
        b_empty.setObjectName("Quiet")
        b_empty.clicked.connect(self.start_empty)
        self.btn_start_empty = b_empty
        option(self.t("sw_start_empty_title", "Pick passages yourself"),
               self.t("sw_start_empty_body", "Start with nothing and drag the passages you want from Available on the "
                      "right, or add them with +."), [b_empty])
        row = QHBoxLayout()
        row.addWidget(box, 10)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        wrap = QScrollArea()
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setWidget(page)
        return wrap

    def _build_hint(self):
        """How the mouse works on the lines, drawn as in Clean (gui/widgets/mouse_hint.py): a quiet row under
        the story; one mouse button that opens the same card when the row has no room."""
        from gui.widgets.mouse_hint import ALT_KEY, MouseHint
        key = "Option" if sys.platform == "darwin" else "Alt"
        labels = (
            {"jump": self.t("hint_m_jump", "playhead here"), "correct": self.t("sw_hint_correct", "correct the words"),
             "play": self.t("sw_hint_play", "play from here"), "menu": self.t("sw_hint_menu", "menu")},
            {"jump": self.t("hint_m_jump_full", "{key} + right-click a word to put the playhead there").format(key=key),
             "correct": self.t("sw_hint_correct_full", "Double-click a line to correct its words"),
             "play": self.t("sw_hint_play_full", "{key} + click a line to play from that sentence").format(key=key),
             "menu": self.t("sw_hint_menu_full", "Right-click a line for its menu")},
            self.t("sw_hint_title", "The mouse on the story's lines"))
        actions = (("jump", "right", ALT_KEY, False), ("correct", "double", None, False),
                   ("play", "left", ALT_KEY, False), ("menu", "right", None, False))
        self.mouse_hint = MouseHint(*labels, parent=self, actions=actions)
        self.mouse_hint_btn = MouseHint(*labels, parent=self, actions=actions)
        self.mouse_hint_btn.set_folded(True)
        self.mouse_hint_btn.hide()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.mouse_hint)
        row.addWidget(self.mouse_hint_btn)
        row.addStretch(1)
        return row

    def _fit_hint(self):
        """The drawn row where it fits under the story, else the folded mouse button."""
        m = self.layout().contentsMargins()
        room = self.width() - m.left() - m.right() - (self.avail.maximumWidth() + S(18) if not self.avail.isHidden() else 0)
        full = room >= self.mouse_hint.full_width()
        self.mouse_hint.setVisible(full)
        self.mouse_hint_btn.setVisible(not full)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # the Available panel gives a little room to the top row (the hub bar's tab names come first)
        self.avail.setFixedWidth(max(S(250), min(S(320), int(self.width() * 0.22))))
        self._fit_top_row()
        self._fit_hint()

    def _fit_top_row(self):
        """The top row (hub bar + its status line, Target, ⋯) left of the Available panel. Short of room, what
        gives way, in this order: the status line's optional parts (blocks, voice patches, cuts: story_status.fit
        only fills the room left over), then the "Target" word (the field and its tooltip stay), and last the hub
        bar's tab names (icon-only). This page decides the last two; the status line doesn't (hub.page_decides)."""
        hub = getattr(self, "step_track", None)
        if hub is None or not hasattr(hub, "set_compact") or getattr(self, "avail", None) is None:
            return
        m = self.layout().contentsMargins()
        avail_w = self.avail.maximumWidth() if self.avail.maximumWidth() < 16777215 else self.avail.width()
        room = self.width() - m.left() - m.right() - (avail_w + S(18) if not self.avail.isHidden() else 0)
        hub_w = hub.full_width()            # tab names + the status line's length and Resolve state
        gap = S(8)                          # the row's spacing before each widget shown

        def _w(x):   # the width it really takes (size hints overstate fixed-width fields and short labels)
            if isinstance(x, QLabel):
                return x.fontMetrics().horizontalAdvance(x.text()) + S(4)
            return max(x.minimumWidth(), min(x.sizeHint().width(), x.maximumWidth()))
        rest = sum(_w(x) + gap for x in (self.lbl_meta, self.edit_target, self.btn_copy_ai, self.btn_import_ai,
                                         self.btn_ai, self.btn_more) if not x.isHidden())
        word = _w(self.lbl_target) + self.lbl_target.contentsMargins().left() + gap
        if hub_w + rest + word <= room:
            compact, keep_word = False, True
        elif hub_w + rest <= room:
            compact, keep_word = False, False
        else:                               # icon-only tabs; the room they free goes to the word first
            compact = True
            keep_word = hub.compact_width() + rest + word <= room
        if self.lbl_target.isHidden() == keep_word:
            self.lbl_target.setVisible(keep_word)
        hub.set_compact(compact)
        st = getattr(hub, "status", None)
        if st is not None:
            st.fit_soon()                   # the optional parts take what's left

    # -- data ----------------------------------------------------------------------------------

    def _board(self):
        return getattr(self.win, "story_board", None)

    def _transcript_sentences(self):
        """The transcript's sentences while the board has none yet (cached per transcript)."""
        canvas = getattr(self.win, "text_canvas", None)
        words = getattr(canvas, "words_data", None)
        if not words:
            return []
        key = (id(words), len(words))
        if self._fallback[0] != key:
            try:
                _, sentences = self.win._build_ai_transcript()
            except Exception as exc:
                print(f"[story] transcript unavailable: {exc}")
                sentences = []
            self._fallback = (key, list(sentences or []))
        return self._fallback[1]

    def _model(self):
        """(sentences, edit, unused) as the board has them; with an empty board, the transcript, all Available."""
        board = self._board()
        if board is not None and board.sentences:
            edit, unused = _tuples(board.edit_blocks()), _tuples(board.unused_blocks())
            # Available is everything not in the story, even if the saved board forgot some sentences
            have = {k for f, l in edit + unused for k in range(f, l + 1)}
            missing = [k for k in range(len(board.sentences)) if k not in have]
            if missing:
                import algorithms
                unused = sorted(unused + _tuples(algorithms.moment_blocks(board.sentences, missing)))
            return list(board.sentences), edit, unused
        sentences = self._transcript_sentences()
        if not sentences:
            return [], [], []
        import algorithms
        return sentences, [], _tuples(algorithms.moment_blocks(sentences))

    @staticmethod
    def _sig(sentences):
        if not sentences:
            return None
        return (len(sentences), sentences[0].get("id"), sentences[-1].get("id"),
                sentences[0].get("src_start"), sentences[-1].get("src_end"))

    def block_duration(self, block):
        s = self.sentences
        extra = 0.0
        try:
            extra = self._patch_extra(float(s[block[0]].get("cut_start", s[block[0]].get("src_start", 0))),
                                      float(s[block[1]].get("cut_end", s[block[1]].get("src_end", 0))))
        except (IndexError, TypeError, ValueError):
            pass
        board = self._board()
        if board is not None and board.sentences and len(board.sentences) == len(self.sentences):
            try:
                return float(board.block_duration(tuple(block))) + extra
            except Exception:
                pass
        return float(s[block[1]].get("cut_end", 0)) - float(s[block[0]].get("cut_start", 0)) + extra

    def _patch_extra(self, a0=None, b0=None):
        """Seconds the voice patches in the story add (or take away) with "Make room" / "Cut the extra time":
        all of them, or those inside recording range a0..b0 (one block)."""
        try:
            import story_time as st
            sp = st.spans(self.win)
            total = 0.0
            for a, b, extra in st.patch_deltas(self.win):
                if st._patch_play_index(sp, a, b) is None:
                    continue                      # not in the story
                if a0 is None or (a >= a0 - 0.05 and b <= b0 + 0.05):
                    total += extra
            return total
        except Exception:
            return 0.0

    def _measure_starts(self):
        """Edit time at which each block of the story starts: the lengths of the blocks before it."""
        starts, pos = [], 0.0
        for blk in self.edit:
            starts.append(pos)
            pos += max(0.0, self.block_duration(blk))
        self._starts = starts

    def block_start(self, k):
        starts = getattr(self, "_starts", None) or []
        return starts[k] if 0 <= k < len(starts) else 0.0

    def sentence_matches(self, i, q):
        s = self.sentences[i]
        return q in str(s.get("text", "")).lower() or q == str(s.get("id", "")).lower()

    def _board_key(self):
        board = self._board()
        try:
            return board._path() if board is not None else None
        except Exception:
            return None

    # -- public --------------------------------------------------------------------------------

    def ai_sentence(self, i):
        """True when sentence i holds words of a voice patch in the edit (cached per rebuild)."""
        if getattr(self, '_ai_set', None) is None:
            canvas = getattr(self.win, 'text_canvas', None)
            words = [w for w in (getattr(canvas, 'words_data', None) or []) if w.get('voice_patch')]
            ai = set()
            if words:
                mids = [(float(w.get('start', 0)) + float(w.get('end', 0))) / 2.0 for w in words]
                for k, sent in enumerate(self.sentences or []):
                    a, b = float(sent.get('src_start', 0)) - 0.02, float(sent.get('src_end', 0)) + 0.02
                    if any(a <= m <= b for m in mids):
                        ai.add(k)
            self._ai_set = ai
        return i in self._ai_set

    # -- target length, script, clear (from the old story panel) -------------------------------------
    def _target_changed(self):
        board = self._board()
        if board is None or not hasattr(board, "edit_target"):
            return
        board.edit_target.setText(self.edit_target.text())
        board._on_target_changed()                 # parses '2:00' / '2m' / '90s', saves, keeps the old one if invalid
        self.edit_target.setText(board.edit_target.text())
        self._update_header()

    def _from_script(self):
        if hasattr(self.win, "_build_story_from_script"):
            self.win._build_story_from_script()

    def _clear_story(self):
        """Start fresh: no blocks, and the voice patches taken out of the story too (their new words leave the
        transcript; the patches stay made, in Voice patch). One undo step brings both back."""
        board = self._board()
        win = self.win
        patches = win._voice_patch_map() if hasattr(win, "_voice_patch_map") else {}
        if board is None or (not board.edit_blocks() and not patches):
            return
        import copy, sys as _sys
        before = {"board": board._undo_snapshot(), "patches": copy.deepcopy(patches)}
        board._undo_restoring = True          # one undo step for the whole clear (pushed below)
        try:
            board.clear_edit()
        finally:
            board._undo_restoring = False
        if patches:
            win._set_voice_patches({})
        um = getattr(win, "undo_manager", None)
        if um is not None:
            def _now():
                return {"board": board._undo_snapshot(), "patches": win._voice_patch_map()}

            def _apply(st):
                board._undo_restore(st["board"])
                win._set_voice_patches(st["patches"])
            um.push_state(_now, _apply, before)
        key = "⌘Z" if _sys.platform == "darwin" else "Ctrl+Z"
        if patches:
            self.note(self.t("sw_cleared_patches", "Story cleared, voice patches taken out ({key} undoes)").format(key=key))
        else:
            self.note(self.t("sw_cleared", "Story cleared ({key} undoes)").format(key=key))

    # -- Versions > Compare: marks on the story itself ----------------------------------------------
    def start_compare(self, v):
        self._compare_v = v
        self.refresh(force=True)

    def stop_compare(self):
        if self._compare_v is None:
            return
        self._compare_v = None
        panel = getattr(self.win, "_versions_panel", None)
        if panel is not None and hasattr(panel, "_clear_selection"):
            panel._clear_selection()
        self.refresh(force=True)

    def _restore_compared(self):
        panel = getattr(self.win, "_versions_panel", None)
        v = self._compare_v
        if panel is not None and v is not None:
            self._compare_v = None
            panel.restore(v)
            self.refresh(force=True)

    def _compare_diff(self):
        if self._compare_v is None:
            return None
        import story_versions as SVV
        board = self._board()
        if board is None:
            return None
        try:
            return SVV.diff(self._compare_v, SVV.state_of(board))
        except Exception as exc:
            print(f"[story] compare failed: {exc}")
            return None

    def _compare_marks(self, d):
        """{current block: [change, ...]} and the removed ones (not in the story now)."""
        marks, removed = {}, []
        for c in (d or {}).get("changes", []):
            blocks = [tuple(b) for b in (c.get("b_blocks") or [])] or ([tuple(c["b_block"])] if c.get("b_block") else [])
            if c.get("kind") == "removed" or not blocks:
                removed.append(c)
            for b in blocks:
                marks.setdefault(b, []).append(c)
        return marks, removed

    def _mark_text(self, c):
        k = c.get("kind")
        if k == "moved":
            return self.t("sw_mark_moved", "Moved · was {n}").format(n=(c.get("a_pos") or 0) + 1)
        return {"added": self.t("sw_mark_added", "New"), "trimmed": self.t("sw_mark_trimmed", "Shorter"),
                "extended": self.t("sw_mark_extended", "Longer"), "split": self.t("sw_mark_split", "Split"),
                "joined": self.t("sw_mark_joined", "Joined"), "edge": self.t("sw_mark_edge", "Edge moved")}.get(k, k)

    def _update_compare_bar(self, d, removed):
        while self.compare_removed.count():
            it = self.compare_removed.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if self._compare_v is None or d is None:
            self.compare_bar.hide()
            return
        from gui.views.story_versions_view import version_label, summary_line
        name = version_label(self.win, self._compare_v)
        if d.get("same"):
            text = self.t("sw_compare_same", "Comparing with “{name}”: the story is the same.").format(name=name)
        else:
            text = self.t("sw_compare_with", "Comparing with “{name}”: {summary}. The marks on the blocks show where.").format(
                name=name, summary=summary_line(self.win, d))
        self.lbl_compare.setText(text)
        self.btn_compare_restore.setVisible(not d.get("same"))
        for c in removed:
            ids = c.get("sentences") or []
            span = (ids[0] + ("–" + ids[-1] if len(ids) > 1 else "")) if ids else ""
            lb = _label(self.t("sw_compare_removed", "Not in the story now: {ids} “{text}”").format(
                ids=span, text=c.get("text", "")), 9, "#fca5a5", wrap=True)
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.compare_removed.addWidget(lb)
        self.compare_bar.show()

    # -- the word being spoken (as in Clean) ---------------------------------------------------------
    def set_playing_word(self, word_idx, t):
        """Light the spoken word in its sentence and mark that sentence's line (follows Keep centered)."""
        row = None
        if word_idx is not None and word_idx >= 0 and t is not None:
            for card in getattr(self.column, "cards", []):
                for r in card.rows:
                    s = self.sentences[r.i] if 0 <= r.i < len(self.sentences) else {}
                    if float(s.get("src_start", 0)) - 0.05 <= t <= float(s.get("src_end", 0)) + 0.05:
                        row = r
                        break
                if row is not None:
                    break
        if self._live_row is not None and self._live_row is not row:
            try:
                self._live_row.set_live(None)
            except RuntimeError:
                pass
        self._live_row = row
        if row is None:
            return
        row.set_live(word_idx)
        ap = getattr(self.win, "audio_preview", None)
        tgl = getattr(ap, "tgl_centered", None)
        if tgl is not None and tgl.isChecked():
            self.scroll.ensureWidgetVisible(row, 0, max(S(40), self.scroll.viewport().height() // 3))

    def refresh(self, force=False):
        """Redraw from the story board (only what changed, unless force)."""
        self._ai_set = None
        self._dirty = False
        self.sentences, self.edit, self.unused = self._model()
        if self._playing is not None and self._playing >= len(self.edit):
            self.stop()
        self._update_header()
        show_start = bool(self.sentences) and not self.edit and self._dismissed_for != self._board_key()
        self._center.setCurrentIndex(0 if show_start else 1)
        self._build_column(force)
        self._build_avail(force)

    def stop(self):
        """Stop a block this page started playing."""
        if self._playing is None:
            return
        self._play_timer.stop()
        ap = getattr(self.win, "audio_preview", None)
        pp = getattr(ap, "patch_playback", None)
        if pp is not None and (pp.active is not None or pp.holding):
            pp.cancel()                        # a voice patch finishing: it stops too, nothing resumes
        try:
            if ap is not None and ap.player.playbackState() == ap.player.PlaybackState.PlayingState:
                ap.toggle_play()
        except Exception:
            pass
        self._set_playing(None)

    def _on_board_changed(self, kind="story"):
        if kind == "lengths":
            if self.isVisible():
                self._update_lengths()
            return
        if self.isVisible():
            self._refresh_timer.start()
        else:
            self._dirty = True

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self.refresh()
        else:
            try:
                self._update_lengths()   # a voice patch put in / taken out elsewhere changes the lengths
            except Exception as exc:
                print(f"[story] lengths: {exc}")

    def hideEvent(self, e):
        self.stop()
        super().hideEvent(e)

    # -- drawing -------------------------------------------------------------------------------

    def _update_header(self):
        n = len(self.edit)
        story = getattr(self.win, "_story", None) or {}
        dur = float(story.get("duration") or 0.0) if n else 0.0
        if n:
            dur += self._patch_extra()   # voice patches that make room / close up change the story's length
        board = self._board()
        target = None
        try:
            target = board._target_s() if board is not None else None
        except Exception:
            pass
        text = self.t("sw_meta", "{blocks} blocks · {duration}").format(blocks=n, duration=_mmss(dur))
        color = MUTED
        if target and n:
            text += " · " + self.t("sw_target", "target {target}").format(target=_mmss(target))
            color = "#3fb950" if abs(dur - target) <= 0.1 * target else "#d29922"
        self.lbl_meta.setText(text)
        self.lbl_meta.setStyleSheet(f"color: {color}; font-size: {FS(9.5)}pt; font-weight: 500; background: transparent;"
                                    f" border: none; padding: 0;")
        self._fit_top_row()

    def _update_lengths(self):
        self._update_header()
        self._measure_starts()
        for c in self.column.cards:
            c.dur.setText(_mmss(self.block_duration(c.block)))
            c.set_start(self.block_start(c.k))

    @staticmethod
    def _clear(lay):
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif it.layout() is not None:
                StoryWorkspaceView._clear(it.layout())

    def _build_column(self, force=False):
        d = self._compare_diff()
        marks, removed = self._compare_marks(d)
        self._update_compare_bar(d, removed)
        try:
            from gui.views.review_view import load_ok_keys
            self._ok_keys = load_ok_keys(self.win)
        except Exception:
            self._ok_keys = set()
        key = (self._sig(self.sentences), tuple(self.edit), tuple(sorted(self._ok_keys)),
               (self._compare_v or {}).get("id"), (d or {}).get("summary"))
        if not force and key == self._built:
            return
        self._live_row = None
        self._built = key
        self._measure_starts()
        vbar = self.scroll.verticalScrollBar()
        pos = vbar.value()
        col = self.column
        col.setUpdatesEnabled(False)
        try:
            self._clear(col.lay)
            col.cards = []
            for k, blk in enumerate(self.edit):
                if k > 0:
                    col.lay.addWidget(_Joint(self, k - 1, can_join(self.edit, k - 1), col))
                card = _Card(self, k, blk, col)
                card.set_playing(k == self._playing)
                if marks.get(tuple(blk)):
                    card.add_marks([self._mark_text(c) for c in marks[tuple(blk)]])
                col.lay.addWidget(card)
                col.cards.append(card)
            if not self.edit:
                ph = QFrame(col)
                ph.setObjectName("EmptyDrop")
                pl = QVBoxLayout(ph)
                pl.setContentsMargins(S(20), S(28), S(20), S(28))
                lbl = _label(self.t("sw_drop_here", "Drag passages here from Available, or add them with +."),
                             10.5, MUTED, wrap=True)
                lbl.setAlignment(Qt.AlignCenter)
                pl.addWidget(lbl)
                col.lay.addWidget(ph)
            col.lay.addStretch(1)
        finally:
            col.setUpdatesEnabled(True)
        QTimer.singleShot(0, lambda: vbar.setValue(min(pos, vbar.maximum())))

    def _build_avail(self, force=False):
        query = self.search.text().strip().lower()
        key = (self._sig(self.sentences), tuple(self.unused), query)
        if not force and key == self._built_avail:
            return
        self._built_avail = key
        vbar = self.avail_scroll.verticalScrollBar()
        pos = vbar.value()
        self.avail_host.setUpdatesEnabled(False)
        try:
            self._clear(self.avail_lay)
            shown = 0
            for blk in self.unused:
                if query and not any(self.sentence_matches(i, query) for i in range(blk[0], blk[1] + 1)):
                    continue
                self.avail_lay.addWidget(_AvailBlock(self, blk, query, self.avail_host))
                shown += 1
            if not shown:
                msg = (self.t("sw_no_match", "Nothing in Available matches “{query}”.").format(query=self.search.text().strip())
                       if query else self.t("sw_avail_empty", "Everything is in the story."))
                lbl = _label(msg, 9.5, MUTED, wrap=True)
                lbl.setContentsMargins(S(4), S(6), S(4), 0)
                self.avail_lay.addWidget(lbl)
            self.avail_lay.addStretch(1)
        finally:
            self.avail_host.setUpdatesEnabled(True)
        n = len(self.unused)
        if query:
            self.lbl_avail.setText(self.t("sw_avail_found", "{shown} of {blocks} passages").format(shown=shown, blocks=n))
        else:
            self.lbl_avail.setText(self.t("sw_avail_count", "{blocks} passages not in the story · drag or +").format(blocks=n))
        self.avail_shown = shown
        QTimer.singleShot(0, lambda: vbar.setValue(min(pos, vbar.maximum())))

    def autoscroll(self, column, y):
        """While dragging near the top or bottom of the story, scroll it."""
        vp_y = column.mapTo(self.scroll.viewport(), QPoint(0, y)).y()
        h = self.scroll.viewport().height()
        edge = S(40)
        bar = self.scroll.verticalScrollBar()
        if vp_y < edge:
            bar.setValue(bar.value() - S(18))
        elif vp_y > h - edge:
            bar.setValue(bar.value() + S(18))

    # -- changes -------------------------------------------------------------------------------

    def _commit(self, edit, unused):
        """One change of the story: one board.load (syncs the story, saves it, one undo step)."""
        board = self._board()
        sentences, e0, u0 = self._model()
        if board is None or not sentences:
            return False
        edit, unused = _tuples(edit), sorted(_tuples(unused))
        if (edit, unused) == (e0, sorted(u0)):
            return False
        if not valid_change(e0, u0, edit, unused):
            print(f"[story] change refused (breaks the board's rules): {edit} / {unused}")
            return False
        self.stop()
        board.load(sentences, edit, unused)
        self.refresh()
        return True

    def move(self, k, to):
        return self._commit(op_move(self.edit, k, to), self.unused)

    def split(self, k, at):
        return self._commit(op_split(self.edit, k, at), self.unused)

    def join(self, k):
        return self._commit(op_join(self.edit, k), self.unused)

    def leave_out(self, a, b):
        e, u = op_leave_out(self.edit, self.unused, a, b)
        return self._commit(e, u)

    def remove_sentence(self, i):
        """Right-click > Remove this sentence: out of the story where it is (a sentence inside a block cuts it
        in two), into Available; says what happened and shows it there."""
        k = next((n for n, (f, l) in enumerate(self.edit) if f <= i <= l), None)
        if k is None:
            return False
        f, l = self.edit[k]
        sid = lambda j: self.sentences[j].get("id", str(j))
        if not self.leave_out(i, i):
            return False
        if f < i < l:
            msg = self.t("sw_note_removed_cut", "{id} moved to Available · cut added between {a} and {b}").format(
                id=sid(i), a=sid(i - 1), b=sid(i + 1))
        else:
            msg = self.t("sw_note_removed", "{id} moved to Available").format(id=sid(i))
        self.note(msg)
        self.flash_available(i)
        return True

    def note(self, text, ms=7000):
        """A short message next to the story's length (the editor's status line isn't on this page)."""
        lbl = getattr(self, 'lbl_note', None)
        if lbl is None:   # a floating pill over the bottom of the story column (the top bar has no room)
            lbl = self.lbl_note = QLabel(self)
            lbl.setStyleSheet(f"QLabel {{ background-color: #2a2140; color: #e9d5ff; border: 1px solid #6d28d9;"
                              f" border-radius: {S(14)}px; padding: {S(6)}px {S(14)}px; font-size: {FS(9.5)}pt;"
                              f" font-weight: 500; }}")
        lbl.setText(text)
        lbl.adjustSize()
        area = self.scroll.geometry()
        host = self.scroll.parentWidget()
        tl = host.mapTo(self, area.topLeft()) if host is not None and host is not self else area.topLeft()
        lbl.move(int(tl.x() + (area.width() - lbl.width()) / 2), int(tl.y() + area.height() - lbl.height() - S(14)))
        lbl.raise_()
        lbl.show()
        t = getattr(self, '_note_timer', None)
        if t is None:
            from PySide6.QtCore import QTimer
            t = self._note_timer = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda: self.lbl_note.hide())
        t.start(ms)

    def flash_available(self, i):
        """Scroll Available to sentence i and light it for a moment."""
        row = next((w for w in self.avail_host.findChildren(_AvailSent) if w.i == i), None)
        if row is None:
            return
        self.avail_scroll.ensureWidgetVisible(row, 0, S(40))
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setObjectName("AvailFlash")
        row.setStyleSheet(f"QWidget#AvailFlash {{ background-color: #2a2140; border-radius: {S(6)}px; }}")
        from PySide6.QtCore import QTimer

        def _off():
            try:
                row.setStyleSheet("")
            except RuntimeError:
                pass
        QTimer.singleShot(2200, _off)

    def remove(self, k):
        if 0 <= k < len(self.edit):
            return self.leave_out(*self.edit[k])
        return False

    def add(self, a, b):
        """+ in Available: into the story in recording order (story_edit.include)."""
        e, u = story_edit.include(self.edit, self.unused, a, b)
        return self._commit(e, u)

    def insert(self, a, b, at):
        res = op_insert(self.edit, self.unused, a, b, at)
        return self._commit(*res) if res else False

    def drop_payload(self, payload, at):
        """A drop: "story:k" moves card k to position `at` (None: out of the story); "avail:a,b" adds
        those sentences as a new card at `at` (None: in recording order)."""
        self.sentences, self.edit, self.unused = self._model()
        try:
            kind, val = payload.split(":", 1)
            if kind == "story":
                k = int(val)
                if not 0 <= k < len(self.edit):
                    return False
                return self.remove(k) if at is None else self.move(k, at)
            if kind == "avail":
                a, b = (int(v) for v in val.split(","))
                return self.add(a, b) if at is None else self.insert(a, b, at)
        except Exception as exc:
            print(f"[story] drop ignored: {exc}")
        return False

    # -- start card ----------------------------------------------------------------------------

    def start_whole(self):
        board = self._board()
        if board is None:
            return
        board.fill_from_transcript()   # one undo step (story board), Cmd+Z takes it back
        self.refresh()

    def start_empty(self):
        self._dismissed_for = self._board_key()
        self.refresh()

    def _copy_for_ai(self):
        self.win._on_copy_ai_transcript()

    def _import_ai(self):
        self.win._on_import_ai_story()

    def open_cut(self, n):
        self.win.open_review_cut(n)

    # -- menus, playback, voice patch ----------------------------------------------------------

    def sentence_menu(self, k, i, gpos):
        from gui.widgets.menus import context_menu
        if not 0 <= k < len(self.edit):
            return
        f, l = self.edit[k]
        m = context_menu(self)
        m.addAction(self.t("sw_m_remove", "Remove this sentence"), lambda: self.remove_sentence(i))
        m.addAction(self.t("board_play", "Play from here"), lambda: self._play_span(k, i, l))
        if i > f:
            m.addAction(self.t("sw_m_split", "Split before this sentence"), lambda: self.split(k, i))
        m.addSeparator()
        m.addAction(self.t("menu_sel_correct", "Correct the words…"), lambda: self.correct_words(i))
        m.addAction(self.t("menu_sel_voice_patch", "Send to voice patch"), lambda: self.voice_patch(i))
        m.exec(gpos)

    def avail_menu(self, i, gpos):
        from gui.widgets.menus import context_menu
        m = context_menu(self)
        m.addAction(self.t("sw_m_add", "Add to the story"), lambda: self.add(i, i))
        m.addAction(self.t("sw_m_play_sentence", "Play this sentence"), lambda: self._play_span(None, i, i))
        m.addSeparator()
        m.addAction(self.t("menu_sel_correct", "Correct the words…"), lambda: self.correct_words(i))
        m.addAction(self.t("menu_sel_voice_patch", "Send to voice patch"), lambda: self.voice_patch(i))
        m.exec(gpos)

    def sentence_word_ids(self, i):
        """Word indices of sentence i: words whose middle lies inside the sentence (seconds of the recording)."""
        canvas = getattr(self.win, "text_canvas", None)
        words = getattr(canvas, "words_data", None) or []
        s = self.sentences[i]
        t0 = float(s.get("src_start", s.get("cut_start", 0)))
        t1 = float(s.get("src_end", s.get("cut_end", 0)))
        out = []
        for n, w in enumerate(words):
            if w.get("type") != "word" or w.get("start") is None or w.get("end") is None:
                continue
            mid = (float(w["start"]) + float(w["end"])) / 2
            if t0 <= mid <= t1:
                out.append(n)
        return out

    def line_words(self, i):
        """Word indices of the words a line shows for sentence i (the transcript's words inside it, in order)."""
        canvas = getattr(self.win, "text_canvas", None)
        words = getattr(canvas, "words_data", None) or []
        if not 0 <= i < len(self.sentences):
            return []
        s = self.sentences[i]
        a, b = float(s.get("src_start", 0)), float(s.get("src_end", 0))
        out = []
        for j, w in enumerate(words):
            if w.get("type") != "word" or w.get("voice_patch_blank") or not str(w.get("text", "")).strip():
                continue
            m = (float(w.get("start", 0)) + float(w.get("end", 0))) / 2.0
            if m < a - 0.02:
                continue
            if m > b + 0.02:
                break
            out.append(j)
        return out

    def jump_to_word(self, i, label, pos):
        """⌥ right-click on a line: the playhead on the word under the mouse (pos: in `label`), as Clean does;
        the sentence's start when no word is hit."""
        if not 0 <= i < len(self.sentences):
            return
        canvas = getattr(self.win, "text_canvas", None)
        words = getattr(canvas, "words_data", None) or []
        ids = self.line_words(i)
        idx = None
        if ids:
            try:
                n, toks = _hit_token(label, pos)
                if n is not None:
                    idx = ids[_token_word(toks, [words[j].get("text", "") for j in ids], n)]
            except Exception as exc:
                print(f"[story] word hit failed: {exc}")
        if idx is not None:
            w = words[idx]
            t = float(w.get("anchor_start", w.get("start", 0.0)))
        else:
            s = self.sentences[i]
            t = float(s.get("src_start", s.get("cut_start", 0)))
        t = max(0.0, t)
        if self._playing is not None:   # a block this page started: the player goes on from the new spot
            self._play_timer.stop()
            self._set_playing(None)
        ap = getattr(self.win, "audio_preview", None)
        if ap is not None:
            try:
                ap.seek_source_time(t, force_word_idx=idx)
            except Exception as exc:
                print(f"[story] seek failed: {exc}")
        jump = getattr(self.win, "_jump_playhead", None)
        if jump is not None:
            QTimer.singleShot(0, lambda: jump(t))

    def play_from(self, k, i):
        """⌥ click on a story line: play from that sentence to the end of its block (the menu's Play from here)."""
        if 0 <= k < len(self.edit):
            self._play_span(k, i, self.edit[k][1])

    def correct_words(self, i):
        """Correct the words of sentence i (the transcript's Correct the words… dialog); the line then shows them."""
        fix = getattr(self.win, "_selection_correct", None)
        canvas = getattr(self.win, "text_canvas", None)
        if fix is None or canvas is None or not 0 <= i < len(self.sentences):
            return
        ids = self.sentence_word_ids(i)
        if not ids:
            return
        before = canvas.words_data
        fix(ids)
        if canvas.words_data is not before:   # applied (a new word list): not cancelled
            self._retext(i)

    def _retext(self, i):
        """Sentence i's text from the corrected transcript, on the board too (its blocks stay; no undo step)."""
        board = self._board()
        if board is None or not board.sentences or not 0 <= i < len(board.sentences):
            self.refresh(force=True)   # the transcript's own sentences: rebuilt for the new word list
            return
        s = board.sentences[i]
        a, b = float(s.get("src_start", 0)), float(s.get("src_end", 0))
        text = None
        try:   # the transcript's sentence over the same stretch: its words as the board writes them
            _, fresh = self.win._build_ai_transcript()
            for f in fresh or []:
                if abs(float(f.get("src_start", -1)) - a) < 0.05 and abs(float(f.get("src_end", -1)) - b) < 0.05:
                    text = f.get("text")
                    break
        except Exception:
            pass
        if not text:
            canvas = getattr(self.win, "text_canvas", None)
            words = getattr(canvas, "words_data", None) or []
            text = " ".join(str(words[j].get("text", "")).strip() for j in self.line_words(i))
        if text and text != s.get("text"):
            new = list(board.sentences)
            new[i] = dict(s, text=text)
            board.load(new, _tuples(board.edit_blocks()), _tuples(board.unused_blocks()))
        self.refresh(force=True)

    def retext_all(self):
        """After an undo/redo of a word correction: every story sentence takes its text back from the transcript
        (same stretch of the recording); blocks untouched, no undo step (the board ignores text-only changes)."""
        board = self._board()
        if board is None or not board.sentences:
            return
        try:
            _, fresh = self.win._build_ai_transcript()
        except Exception:
            return
        by_span = {(round(float(f.get("src_start", -1)), 2), round(float(f.get("src_end", -1)), 2)): f.get("text")
                   for f in fresh or []}
        new, changed = [], False
        for s in board.sentences:
            t = by_span.get((round(float(s.get("src_start", 0)), 2), round(float(s.get("src_end", 0)), 2)))
            if t and t != s.get("text"):
                s, changed = dict(s, text=t), True
            new.append(s)
        if changed:
            board.load(new, _tuples(board.edit_blocks()), _tuples(board.unused_blocks()))
            self.refresh(force=True)

    def voice_patch(self, i):
        ids = self.sentence_word_ids(i)
        if ids:
            self.win._selection_voice_patch(ids)

    def play_card(self, k):
        if self._playing == k:
            self.stop()
            return
        if 0 <= k < len(self.edit):
            f, l = self.edit[k]
            self._play_span(k, f, l)

    def _play_span(self, k, i, j):
        """Play sentences i..j from the recording (k: the card it belongs to, for its button)."""
        ap = getattr(self.win, "audio_preview", None)
        if ap is None or not self.sentences:
            return
        s = self.sentences
        t0 = float(s[i].get("src_start", s[i].get("cut_start", 0)))
        t1 = float(s[j].get("src_end", s[j].get("cut_end", 0)))
        self._play_timer.stop()
        try:
            ap.seek_source_time(max(0.0, t0 - 0.05))
            if ap.player.playbackState() != ap.player.PlaybackState.PlayingState:
                ap.toggle_play()
        except Exception as exc:
            print(f"[story] play failed: {exc}")
            return
        import time
        self._play_range = (t0, t1)
        self._play_started = time.time()
        self._set_playing(k if k is not None else -1)
        self._play_timer.start()

    def _source_now(self, ap):
        try:
            audio_t = max(0.0, ap._get_audio_t())
            if getattr(ap, "_preview_on", False):
                return ap._preview_to_source(audio_t)
            return ap._audio_to_original_time(audio_t) if ap.clean_ops else audio_t
        except Exception:
            return None

    def _play_tick(self):
        import time
        ap = getattr(self.win, "audio_preview", None)
        if ap is None or self._play_range is None:
            self._set_playing(None)
            self._play_timer.stop()
            return
        playing = ap.player.playbackState() == ap.player.PlaybackState.PlayingState
        if not playing and getattr(getattr(ap, "patch_playback", None), "holding", False):
            return   # the recording waits while a longer voice patch finishes: still playing this block
        if not playing:
            if time.time() - self._play_started > 1.0:  # paused elsewhere, or reached the end
                self._play_timer.stop()
                self._set_playing(None)
            return
        t = self._source_now(ap)
        t0, t1 = self._play_range
        if t is not None and time.time() - self._play_started > 0.4 and (t > t1 + 0.15 or t < t0 - 1.0):
            self.stop()

    def _set_playing(self, k):
        old, self._playing = self._playing, k
        if k is None:
            self._play_range = None
        for c in self.column.cards:
            if c.k in (old, k):
                c.set_playing(c.k == k)
