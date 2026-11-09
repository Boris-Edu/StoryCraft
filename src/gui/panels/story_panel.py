#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: story_panel.py
ROLE: GUI Activity Panel
DESCRIPTION:
Story board: the interview as blocks of sentences. "Edit" holds the blocks in the order they
play, "Not used" the rest. Drag to reorder or to move between the two lists. The Edit list is
the story that Assemble and Play edit follow (non-linear order allowed).
"""

from gui.theme import qc

from PySide6.QtCore import Qt, QSize, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QListWidget, QListWidgetItem, QAbstractItemView,
    QStyledItemDelegate, QStyle, QMenu
)

import config
from gui.widgets.labels import QLabel
from gui.widgets.buttons import QPushButton
from .script_panel import (wrap_activity_panel, panel_margins, panel_card, heading_css, caption_css, sublabel_css,
                           quiet_button, quiet_button_css, input_css, style_segmented, hairline, ButtonFlow,
                           card_css, text_px)

BLOCK_ROLE = Qt.UserRole + 1
MAX_TEXT_LINES = 3

# Block card geometry (px): the gap between cards, the inner padding, the header-to-text gap
_GAP = 3
_PAD_X = 11
_PAD_Y = 8
_HEAD_GAP = 4


def _decode(value):
    """Blocks are stored as "first,last" text so they survive drag and drop between lists."""
    try:
        f, l = str(value).split(",")
        return int(f), int(l)
    except Exception:
        return None


def _encode(block):
    return f"{int(block[0])},{int(block[1])}"


def _mmss(seconds):
    seconds = int(round(max(0.0, seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class _BlockDelegate(QStyledItemDelegate):
    """Paints a block as a card: drag handle, IDs, clarity and length on top, what is said underneath
    (up to MAX_TEXT_LINES lines)."""

    def __init__(self, board, view):
        super().__init__(view)
        self.board = board
        self.view = view

    def _head_font(self):
        f = QFont(self.view.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.86))
        f.setWeight(QFont.DemiBold)
        return f

    def _text_rect_width(self):
        # Always leave room for the scrollbar: it appears after the heights are measured
        sb = self.view.verticalScrollBar().sizeHint().width()
        inner = self.view.width() - 2 * self.view.frameWidth() - sb
        return max(60, inner - 2 * config.S(_GAP) - 2 * config.S(_PAD_X))

    def _body(self, block):
        return self.board.block_text(block)

    def _head_h(self):
        return QFontMetrics(self._head_font()).height() + config.S(6)

    def sizeHint(self, option, index):
        block = _decode(index.data(BLOCK_ROLE))
        if not block:
            return QSize(0, 0)
        fm = QFontMetrics(self.view.font())
        w = self._text_rect_width()
        body_h = fm.boundingRect(0, 0, w, 10000, Qt.TextWordWrap, self._body(block)).height()
        body_h = min(body_h, fm.lineSpacing() * MAX_TEXT_LINES)
        return QSize(w, body_h + self._head_h() + config.S(_HEAD_GAP) + 2 * config.S(_PAD_Y) + 2 * config.S(_GAP))

    def _pill(self, painter, rect, text, font, fg, bg):
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setFont(font)
        painter.setPen(fg)
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()

    def paint(self, painter, option, index):
        block = _decode(index.data(BLOCK_ROLE))
        if not block:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        g = config.S(_GAP)
        r = QRectF(option.rect).adjusted(g, g, -g, -g)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        playing = self.board.is_playing_block(block)
        accent = qc(config.BTN_BG)
        card = qc("#2a2a2a") if hovered and not playing else qc("#262626")
        if playing:
            # The block that is playing: the card tinted with the accent
            tint = QColor(accent)
            card = QColor(int(card.red() * 0.8 + tint.red() * 0.2), int(card.green() * 0.8 + tint.green() * 0.2),
                          int(card.blue() * 0.8 + tint.blue() * 0.2))
        if selected or playing:
            border = accent
        else:
            border = qc("#3a3a3a") if hovered else qc("#303030")
        painter.setPen(QPen(border, 1.4 if (selected or playing) else 1.0))
        painter.setBrush(card)
        rad = config.S(10)
        painter.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)

        fm = QFontMetrics(self.view.font())
        hf = self._head_font()
        hfm = QFontMetrics(hf)
        pad_x, pad_y = config.S(_PAD_X), config.S(_PAD_Y)
        head = QRectF(r.left() + pad_x, r.top() + pad_y, r.width() - 2 * pad_x, self._head_h())
        # Drag handle: two columns of three dots, quiet until the card is hovered
        painter.setPen(Qt.NoPen)
        painter.setBrush(qc("#6a6a6a") if hovered or selected else qc("#4a4a4a"))
        dot = config.S(2.4)
        cx, cy = head.left() + config.S(1.5), head.center().y()
        for dx in (0, config.S(4.5)):
            for dy in (-config.S(4.5), 0, config.S(4.5)):
                painter.drawEllipse(QRectF(cx + dx - dot / 2, cy + dy - dot / 2, dot, dot))
        painter.setFont(hf)
        label = self.board.block_label(block)
        label_rect = head.adjusted(config.S(13), 0, 0, 0)
        painter.setPen(qc("#a3a3a3"))
        painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, label)
        tag_x = label_rect.left() + hfm.horizontalAdvance(label) + config.S(10)
        cl = self.board.block_clarity(block)
        if cl is not None:
            # Clarity: a small dot and number after the IDs (green clear, amber so-so, red hard to hear)
            dot_col = QColor("#3fb950" if cl >= 80 else ("#d29922" if cl >= 60 else "#f85149"))
            d = config.S(6)
            painter.setPen(Qt.NoPen)
            painter.setBrush(dot_col)
            painter.drawEllipse(QRectF(tag_x, head.center().y() - d / 2, d, d))
            painter.setPen(qc("#8a8a8a"))
            painter.drawText(QRectF(tag_x + d + config.S(4), head.top(), hfm.horizontalAdvance(str(cl)) + 2, head.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, str(cl))
            tag_x += d + config.S(4) + hfm.horizontalAdvance(str(cl)) + config.S(8)
        small = QFont(hf)
        small.setPointSizeF(max(6.0, hf.pointSizeF() * 0.88))
        small.setBold(True)
        sfm = QFontMetrics(small)
        pill_h = head.height() - config.S(2)
        if self.board.block_avoid(block):
            # Tagged "Avoid" by the editor: an orange pill
            tag = self.board.win.txt("board_avoid")
            tr = QRectF(tag_x, head.center().y() - pill_h / 2, sfm.horizontalAdvance(tag) + config.S(12), pill_h)
            self._pill(painter, tr, tag, small, QColor("#ff9d4d"), QColor(235, 110, 0, 60))
        # Length: a pill at the right end of the header
        dur = _mmss(self.board.block_duration(block))
        dw = hfm.horizontalAdvance(dur) + config.S(14)
        dr = QRectF(head.right() - dw, head.center().y() - pill_h / 2, dw, pill_h)
        self._pill(painter, dr, dur, hf, qc("#d4d4d4"), qc("#1e1e1e"))
        if playing:
            # "Playing" tag left of the length, in the accent colour
            tag = self.board.win.txt("board_playing")
            tw = sfm.horizontalAdvance(tag) + config.S(12)
            tr = QRectF(dr.left() - config.S(6) - tw, dr.top(), tw, pill_h)
            soft = QColor(accent)
            soft.setAlpha(60)
            self._pill(painter, tr, tag, small, accent.lighter(135), soft)

        painter.setFont(self.view.font())
        body = QRectF(head.left(), head.bottom() + config.S(_HEAD_GAP), head.width(),
                      r.bottom() - pad_y - head.bottom() - config.S(_HEAD_GAP))
        text = self._body(block)
        lines_h = fm.boundingRect(0, 0, int(body.width()), 10000, Qt.TextWordWrap, text).height()
        if lines_h > body.height() + 1:
            # Too long for the card: cut to the visible lines and end with an ellipsis
            while text and fm.boundingRect(0, 0, int(body.width()), 10000, Qt.TextWordWrap,
                                           text + "…").height() > body.height() + 1:
                text = text[:max(0, int(len(text) * 0.92) - 1)].rstrip()
            text += "…"
        painter.setPen(qc("#e6e6e6") if selected or hovered else qc("#d4d4d4"))
        painter.drawText(body, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text)
        painter.restore()


class _BlockList(QListWidget):
    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setResizeMode(QListWidget.Adjust)
        self.setWordWrap(True)
        self.setSpacing(0)
        self.setItemDelegate(_BlockDelegate(board, self))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        # Hover state for the cards (the delegate brightens the one under the mouse)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        # A recessed well holding the block cards
        self.setStyleSheet(f"""
            QListWidget {{ background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: {config.S(12)}px;
                          padding: {config.S(3)}px; outline: none; }}
            QListWidget::item {{ background: transparent; border: none; }}
            QListWidget::item:selected {{ background: transparent; }}
            QListWidget::item:hover {{ background: transparent; }}
        """)
        self.model().rowsInserted.connect(board.schedule_sync)
        self.model().rowsRemoved.connect(board.schedule_sync)
        self.model().rowsMoved.connect(board.schedule_sync)
        self.itemDoubleClicked.connect(lambda it: board.play_block(_decode(it.data(BLOCK_ROLE))))
        self.customContextMenuRequested.connect(lambda pos: board.show_menu(self, pos))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.doItemsLayout()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self is self.board.list_edit:
            self.board.move_selected(self, self.board.list_unused)
            return
        super().keyPressEvent(event)


class StoryBoard(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.sentences = []
        self._syncing = False
        self._ops_ctx = None  # (ops, fps, offset) for block lengths after the cuts
        self.story_changed = []  # callbacks(kind): "story" after every sync, "lengths" when block lengths change
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(0)
        self._sync_timer.timeout.connect(self._sync_story)

        lay = QVBoxLayout(self)
        panel_margins(lay, 10)

        row = QHBoxLayout()
        title = QLabel(win.txt("board_title"))
        title.setStyleSheet(heading_css(12))
        row.addWidget(title)
        row.addStretch()
        row.addWidget(win._create_info_icon("tt_board"))
        lay.addLayout(row)

        # One card to build the edit: From transcript / From script, then the External AI round trip
        # in the order it is used: Copy for AI -> (AI chat) -> Import AI edit, then Review cuts to
        # refine, then the target length. Buttons share a row only when their labels fit whole.
        from PySide6.QtWidgets import QSizePolicy, QLineEdit
        self.btn_fill = QPushButton(win.txt("board_fill"))
        self.btn_fill.setToolTip(win.txt("tt_board_fill"))
        self.btn_fill.clicked.connect(self.fill_from_transcript)
        self.btn_copy_ai = QPushButton(win.txt("board_copy_ai"))
        self.btn_copy_ai.setToolTip(win.txt("tt_board_copy_ai"))
        self.btn_copy_ai.clicked.connect(lambda: win._on_copy_ai_transcript())
        self.btn_ai = QPushButton(win.txt("board_import_ai"))
        self.btn_ai.setToolTip(win.txt("tt_board_import_ai"))
        self.btn_ai.clicked.connect(lambda: win._on_import_ai_story())
        self.btn_review = QPushButton(win.txt("board_review_cuts"))
        self.btn_review.setToolTip(win.txt("tt_board_review_cuts"))
        self.btn_review.clicked.connect(lambda: win._on_review_cuts())
        self.btn_from_script = QPushButton(win.txt("board_from_script"))
        self.btn_from_script.setToolTip(win.txt("tt_board_from_script"))
        self.btn_from_script.clicked.connect(lambda: win._build_story_from_script())
        for b in (self.btn_fill, self.btn_from_script, self.btn_copy_ai, self.btn_ai, self.btn_review):
            b.setCursor(Qt.PointingHandCursor)
            quiet_button(b, 9, 30, 5, 8)

        build_card, build_lay = panel_card(8)
        build_lay.setContentsMargins(config.S(12), config.S(12), config.S(12), config.S(12))
        build_lay.addWidget(ButtonFlow([self.btn_fill, self.btn_from_script], max_cols=2))
        build_lay.addSpacing(config.S(2))
        ai_head = QHBoxLayout()
        ai_title = QLabel(win.txt("group_external_ai").upper())
        ai_title.setStyleSheet(caption_css())
        ai_head.addWidget(ai_title)
        ai_head.addStretch()
        ai_head.addWidget(win._create_info_icon("tt_group_external_ai"))
        build_lay.addLayout(ai_head)
        build_lay.addWidget(ButtonFlow([self.btn_copy_ai, self.btn_ai, self.btn_review], max_cols=3))
        build_lay.addSpacing(config.S(2))
        build_lay.addWidget(hairline())

        # Target length for the AI (and to compare the edit against)
        row_t = QHBoxLayout()
        row_t.setSpacing(config.S(8))
        lbl_t = QLabel(win.txt("board_target"))
        lbl_t.setWordWrap(True)
        lbl_t.setStyleSheet(f"color: #d4d4d4; font-size: {config.FS(9.5)}pt; background: transparent;")
        row_t.addWidget(lbl_t, 1)
        self.edit_target = QLineEdit()
        self.edit_target.setPlaceholderText("2:00")
        self.edit_target.setToolTip(win.txt("tt_board_target"))
        self.edit_target.setFixedWidth(config.TS(72))
        self.edit_target.setFixedHeight(max(config.S(28), text_px(9.5) + 2 * config.S(4) + 2))
        self.edit_target.setAlignment(Qt.AlignCenter)
        self.edit_target.setStyleSheet(input_css("QLineEdit", 9.5))
        cur = (win.engine.load_preferences() or {}).get('story_target_s')
        if cur:
            self.edit_target.setText(_mmss(cur))
        self.edit_target.editingFinished.connect(self._on_target_changed)
        row_t.addWidget(self.edit_target)
        build_lay.addLayout(row_t)
        lay.addWidget(build_card)

        # EDIT · blocks · length (wraps rather than being cut), with Clear for the edit list
        self.lbl_edit = QLabel("")
        self.lbl_edit.setWordWrap(True)
        self.lbl_edit.setStyleSheet(self._caption_style("#8a8a8a"))
        row_e = QHBoxLayout()
        row_e.setContentsMargins(config.S(2), config.S(4), 0, 0)
        row_e.setSpacing(config.S(6))
        row_e.addWidget(self.lbl_edit, 1)
        self.btn_clear = QPushButton(win.txt("btn_story_clear"))
        self.btn_clear.clicked.connect(self.clear_edit)
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        _clear_h = text_px(8.5) + 2 * config.S(3) + 2
        self.btn_clear.setFixedHeight(_clear_h)
        self.btn_clear.setStyleSheet(f"""
            QPushButton {{ background: #2a2a2a; color: #a3a3a3; border: 1px solid #333333;
                border-radius: {max(1, _clear_h // 2 - 1)}px; padding: 0 {config.S(10)}px; font-size: {config.FS(8.5)}pt; }}
            QPushButton:hover {{ color: #ffffff; background: #333333; }}
            QPushButton:disabled {{ color: #6a6a6a; background: transparent; border-color: #2a2a2a; }}
        """)
        row_e.addWidget(self.btn_clear, 0, Qt.AlignVCenter)
        lay.addLayout(row_e)
        self.lbl_empty = QLabel(win.txt("board_empty"))
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setStyleSheet(sublabel_css(9))
        lay.addWidget(self.lbl_empty)
        self.list_edit = _BlockList(self)
        # Never squeezed below about two cards: the panel scrolls instead when the window is short
        self.list_edit.setMinimumHeight(config.TS(170))
        lay.addWidget(self.list_edit, 3)

        self.lbl_unused = QLabel("")
        self.lbl_unused.setWordWrap(True)
        self.lbl_unused.setContentsMargins(config.S(2), config.S(4), 0, 0)
        self.lbl_unused.setStyleSheet(self._caption_style("#8a8a8a"))
        lay.addWidget(self.lbl_unused)
        self.list_unused = _BlockList(self)
        self.list_unused.setMinimumHeight(config.TS(130))
        lay.addWidget(self.list_unused, 2)

        # Every edit cut from this source timeline: load one to continue from it
        from gui.widgets.segmented import SegmentedChoice
        self.edits_box = QFrame()
        self.edits_box.setObjectName("editsBox")
        self.edits_box.setStyleSheet(card_css("editsBox"))
        eb = QVBoxLayout(self.edits_box)
        eb.setContentsMargins(config.S(12), config.S(10), config.S(12), config.S(12))
        eb.setSpacing(config.S(6))
        eh = QHBoxLayout()
        eh.setSpacing(config.S(6))
        et = QLabel(win.txt("edits_title").upper())
        et.setWordWrap(True)
        et.setStyleSheet(caption_css())
        eh.addWidget(et, 1)
        eh.addWidget(win._create_info_icon("tt_edits_title"), 0, Qt.AlignVCenter)
        eb.addLayout(eh)
        # Sort order under the title (beside it, the title broke into three lines in a narrow panel)
        self.edits_sort = SegmentedChoice([("date", win.txt("edits_newest")), ("length", win.txt("edits_longest"))],
                                          "date", compact=True)
        style_segmented(self.edits_sort.buttons.values(), 8.5)
        self.edits_sort.changed.connect(lambda _m: self.refresh_edits())
        eb.addWidget(self.edits_sort, 0, Qt.AlignLeft)
        self.edits_rows = QVBoxLayout()
        self.edits_rows.setSpacing(config.S(2))
        eb.addLayout(self.edits_rows)
        self.lbl_source_backup = QLabel("")
        self.lbl_source_backup.setWordWrap(True)
        self.lbl_source_backup.setStyleSheet(f"color: #8a8a8a; font-size: {config.FS(8.5)}pt; background: transparent; border: none;")
        eb.addWidget(self.lbl_source_backup)
        lay.addWidget(self.edits_box)

        self._refresh_labels()

    @staticmethod
    def _caption_style(color):
        return (f"color: {color}; font-size: {config.FS(8.5)}pt; font-weight: 600; letter-spacing: 1px;"
                f" background: transparent;")

    # ------------------------------------------------------------------ data
    def block_text(self, block):
        f, l = block
        return " ".join(s["text"] for s in self.sentences[f:l + 1])

    def block_label(self, block):
        f, l = block
        a, b = self.sentences[f]["id"], self.sentences[l]["id"]
        return a if f == l else f"{a}–{b}"

    def block_clip(self, block):
        f, l = block
        clip = {"cut_start": self.sentences[f]["cut_start"], "cut_end": self.sentences[l]["cut_end"],
                "start_id": self.sentences[f]["id"], "end_id": self.sentences[l]["id"]}
        air = self.air_for(block)
        if air:  # pause override at this block's edges (Review: tighter / airier), seconds of air
            clip["air"] = dict(air)
            clip["src_start"] = self.sentences[f].get("src_start", clip["cut_start"])
            clip["src_end"] = self.sentences[l].get("src_end", clip["cut_end"])
        return clip

    # Pause at a cut: seconds of air kept after a block's last word ("tail") / before its first ("lead")
    def air_for(self, block):
        return (getattr(self, "edge_air", None) or {}).get(f"{block[0]}-{block[1]}") or {}

    def set_air(self, block, lead=None, tail=None):
        """Change one block's edges (None = leave as is, 'auto' = back to the default)."""
        air = dict(getattr(self, "edge_air", None) or {})
        key = f"{block[0]}-{block[1]}"
        cur = dict(air.get(key) or {})
        for side, v in (("lead", lead), ("tail", tail)):
            if v == "auto":
                cur.pop(side, None)
            elif v is not None:
                cur[side] = float(v)
        if cur:
            air[key] = cur
        else:
            air.pop(key, None)
        self.edge_air = air
        self._sync_story()

    def block_avoid(self, block):
        return any(s.get("avoid") for s in self.sentences[block[0]:block[1] + 1])

    def block_clarity(self, block):
        vals = [s.get("clarity") for s in self.sentences[block[0]:block[1] + 1] if s.get("clarity") is not None]
        return int(round(sum(vals) / len(vals))) if vals else None

    def block_duration(self, block):
        ctx = self._ops_ctx
        if ctx is None:
            return self.sentences[block[1]]["cut_end"] - self.sentences[block[0]]["cut_start"]
        import algorithms
        ops, fps, offset = ctx
        kept = algorithms.apply_story_order(ops, [self.block_clip(block)], fps, offset)
        return sum(o['e'] - o['s'] for o in kept) / fps

    def is_playing_block(self, block):
        return block == getattr(self, "_playing_block", None)

    def set_playing_time(self, t):
        """Highlight the Edit block that contains source time t (seconds), or none."""
        found = None
        if self.sentences and t is not None:
            offset = float((self.win.engine.load_preferences() or {}).get("offset", 0.133)) if hasattr(self.win, "engine") else 0.133
            for i in range(self.list_edit.count()):
                b = _decode(self.list_edit.item(i).data(BLOCK_ROLE))
                if b and self.sentences[b[0]]["cut_start"] + offset <= t < self.sentences[b[1]]["cut_end"] + offset:
                    found = b
                    if found != getattr(self, "_playing_block", None):
                        self.list_edit.scrollToItem(self.list_edit.item(i))
                    break
        if found != getattr(self, "_playing_block", None):
            self._playing_block = found
            self.list_edit.viewport().update()

    def edit_blocks(self):
        return [_decode(self.list_edit.item(i).data(BLOCK_ROLE)) for i in range(self.list_edit.count())]

    def unused_blocks(self):
        return [_decode(self.list_unused.item(i).data(BLOCK_ROLE)) for i in range(self.list_unused.count())]

    def _make_item(self, block):
        it = QListWidgetItem(f"{block[0]:06d}")  # sort key only; the delegate paints the card
        it.setData(BLOCK_ROLE, _encode(block))
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        return it

    # ------------------------------------------------------------------ loading
    def load(self, sentences, edit_blocks, unused_blocks=None):
        """Show `edit_blocks` (play order) and `unused_blocks`; if unused is None, everything else."""
        self.sentences = list(sentences)
        self._ops_ctx = self.win._story_ops_context() if hasattr(self.win, "_story_ops_context") else None
        if unused_blocks is None:
            import algorithms
            used = {i for f, l in edit_blocks for i in range(f, l + 1)}
            unused_blocks = algorithms.moment_blocks(self.sentences, [i for i in range(len(self.sentences)) if i not in used])
        self._syncing = True
        try:
            self.list_edit.clear()
            self.list_unused.clear()
            for b in edit_blocks:
                self.list_edit.addItem(self._make_item(b))
            for b in unused_blocks:
                self.list_unused.addItem(self._make_item(b))
        finally:
            self._syncing = False
        self._sync_story()

    def fill_from_transcript(self):
        import algorithms
        _, sentences = self.win._build_ai_transcript()
        if not sentences:
            return
        # Parts tagged Avoid start in Not used
        good = [i for i, s in enumerate(sentences) if not s.get("avoid")]
        bad = [i for i, s in enumerate(sentences) if s.get("avoid")]
        self.load(sentences, algorithms.moment_blocks(sentences, good), algorithms.moment_blocks(sentences, bad))

    def load_ai_story(self, sentences, story):
        """Mirror an imported AI edit: its clips become the Edit list, the rest goes to Not used."""
        index = {s["id"]: i for i, s in enumerate(sentences)}
        blocks = []
        for c in story.get("clips", []):
            f, l = index.get(c.get("start_id")), index.get(c.get("end_id"))
            if f is not None and l is not None and f <= l:
                blocks.append((f, l))
        if story.get("keep_source_order"):
            blocks.sort()
        self.load(sentences, blocks)

    def clear_edit(self):
        if not self.sentences:
            return
        import algorithms
        self.load(self.sentences, [], algorithms.moment_blocks(self.sentences))

    # ------------------------------------------------------------------ sync
    def schedule_sync(self, *args):
        if not self._syncing:
            self._sync_timer.start()

    def _undo_snapshot(self):
        return {"sentences": self.sentences, "air": dict(getattr(self, "edge_air", None) or {}),
                "edit": [tuple(b) for b in self.edit_blocks()],
                "unused": [tuple(_decode(self.list_unused.item(i).data(BLOCK_ROLE))) for i in range(self.list_unused.count())]}

    def _undo_restore(self, snap):
        self._undo_restoring = True
        try:
            self.edge_air = dict(snap.get("air") or {})
            self.load(snap["sentences"] or [], list(snap["edit"]), list(snap["unused"]))
            self._sync_story()
        finally:
            self._undo_restoring = False
        self._undo_last = self._undo_snapshot()

    def _record_undo(self):
        """After a change: the board as it was goes on the undo history (not when a timeline opens)."""
        snap = self._undo_snapshot()
        last = getattr(self, '_undo_last', None)
        if last is None:  # nothing restored for this timeline yet: the change started from an empty board
            last = {"sentences": [], "edit": [], "unused": [], "air": {}}
        if (last is not None and not getattr(self, '_undo_restoring', False)
                and (snap["edit"], snap["unused"], len(snap["sentences"] or []), snap.get("air") or {}) !=
                    (last["edit"], last["unused"], len(last["sentences"] or []), last.get("air") or {})
                and getattr(self.win, 'undo_manager', None) is not None):
            self.win.undo_manager.push_state(self._undo_snapshot, self._undo_restore, last)
        self._undo_last = snap

    def _sync_story(self):
        self._syncing = True
        try:
            self.list_unused.sortItems()  # Not used stays in recording order
        finally:
            self._syncing = False
        blocks = self.edit_blocks()
        self.win._set_story_from_board([self.block_clip(b) for b in blocks] if blocks else None)
        self._refresh_labels()
        self._save()
        self._record_undo()
        self._notify("story")

    def _notify(self, kind):
        for fn in list(self.story_changed):
            try:
                fn(kind)
            except Exception as e:
                print(f"[story board] listener failed: {e}")

    # ------------------------------------------------------------------ persistence
    def _path(self):
        if not hasattr(self.win, "_ai_story_map_path"):
            return None
        base = self.win._ai_story_map_path()
        return base[:-5] + ".board.json" if base.endswith(".json") else base + ".board.json"

    def _save(self):
        path = self._path()
        if not path or not self.sentences:
            return
        import json, os
        unused = [_decode(self.list_unused.item(i).data(BLOCK_ROLE)) for i in range(self.list_unused.count())]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "sentences": self.sentences, "air": getattr(self, "edge_air", None) or {},
                           "edit": [list(b) for b in self.edit_blocks()], "unused": [list(b) for b in unused]}, f)
        except Exception:
            pass

    def state(self):
        """The board as saved: sentences, Edit blocks (play order) and Not used blocks."""
        unused = [_decode(self.list_unused.item(i).data(BLOCK_ROLE)) for i in range(self.list_unused.count())]
        return {"version": 1, "sentences": self.sentences,
                "edit": [list(b) for b in self.edit_blocks()], "unused": [list(b) for b in unused]}

    def refresh_edits(self):
        """Rows of the Edits box: name, length, date; the loaded one highlighted."""
        while self.edits_rows.count():
            it = self.edits_rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        entries, backup = self.win._edits_overview(self.edits_sort.value() or "date")
        if not entries:
            empty = QLabel(self.win.txt("edits_empty"))
            empty.setWordWrap(True)
            empty.setStyleSheet(sublabel_css(9))
            self.edits_rows.addWidget(empty)
        current = getattr(self.win, '_current_edit_name', None)
        import time as _t
        for e in entries:
            secs = int(round(e.get("length_s") or 0))
            when = _t.strftime("%d %b %H:%M", _t.localtime(e.get("created") or 0))
            b = QPushButton(f"{e['name']}    {secs // 60}:{secs % 60:02d}    {when}")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(self.win.txt("tt_edits_row"))
            on = e["name"] == current
            b.setStyleSheet(f"""
                QPushButton {{ background: {'rgba(35, 165, 89, 36)' if on else 'transparent'}; color: {'#ffffff' if on else '#d4d4d4'};
                    border: 1px solid {'#2f6b45' if on else 'transparent'}; border-radius: {config.S(8)}px;
                    text-align: left; padding: {config.S(5)}px {config.S(8)}px; font-size: {config.FS(9)}pt; }}
                QPushButton:hover {{ background: #2a2a2a; color: #ffffff; }}""")
            b.clicked.connect(lambda _=False, e=e: self.win._load_edit(e))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, e=e, b=b: self._edit_menu(e, b.mapToGlobal(pos)))
            self.edits_rows.addWidget(b)
        self.lbl_source_backup.setText(backup or "")
        self.lbl_source_backup.setVisible(bool(backup))

    def _edit_menu(self, entry, pos):
        from gui.widgets.menus import context_menu
        m = context_menu(self)
        m.addAction(self.win.txt("edits_load"), lambda: self.win._load_edit(entry))
        m.addAction(self.win.txt("edits_forget"), lambda: self.win._forget_edit(entry))
        m.exec(pos)

    def maybe_restore(self):
        """Bring back the board saved for this timeline, once, when its transcript is loaded.
        A board waiting from a loaded edit (win._pending_board) comes first."""
        pending = getattr(self.win, '_pending_board', None)
        if pending:
            self.win._pending_board = None
            self._restored_for = self._path()
            try:
                sentences = pending["sentences"]
                n = len(sentences)
                ok = lambda b: 0 <= b[0] <= b[1] < n
                self._undo_restoring = True
                self.load(sentences, [tuple(b) for b in pending.get("edit", []) if ok(b)],
                          [tuple(b) for b in pending.get("unused", []) if ok(b)])
                self._sync_story()
            except Exception:
                pass
            finally:
                self._undo_restoring = False
                self._undo_last = self._undo_snapshot()
            self.refresh_edits()
            return
        self.refresh_edits()
        path = self._path()
        if not path or path == getattr(self, "_restored_for", None):
            return
        self._restored_for = path
        if self.sentences:
            return
        import json, os
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sentences = data["sentences"]
            n = len(sentences)
            ok = lambda b: 0 <= b[0] <= b[1] < n
            edit = [tuple(b) for b in data.get("edit", []) if ok(b)]
            unused = [tuple(b) for b in data.get("unused", []) if ok(b)]
            self.edge_air = dict(data.get("air") or {})
            if edit or unused:
                self._undo_restoring = True  # restoring the saved board isn't a change to undo
                try:
                    self.load(sentences, edit, unused)
                finally:
                    self._undo_restoring = False
        except Exception:
            pass
        self._undo_last = self._undo_snapshot()  # the saved board is where undo starts

    def _target_s(self):
        return (self.win.engine.load_preferences() or {}).get('story_target_s') or None

    def _on_target_changed(self):
        """'2:00', '2m', '90s' or '90' -> seconds; empty clears the target."""
        import re
        txt = self.edit_target.text().strip().lower().replace(",", ".")
        secs = None
        m = re.fullmatch(r"(\d+):(\d{1,2})", txt)
        if m:
            secs = int(m.group(1)) * 60 + int(m.group(2))
        elif re.fullmatch(r"\d+(\.\d+)?\s*m(in)?", txt):
            secs = float(re.match(r"[\d.]+", txt).group(0)) * 60
        elif re.fullmatch(r"\d+(\.\d+)?\s*s?", txt):
            secs = float(re.match(r"[\d.]+", txt).group(0))
        prefs = self.win.engine.load_preferences() or {}
        if txt and not secs:  # not a length: keep the previous target
            self.edit_target.setText(_mmss(prefs['story_target_s']) if prefs.get('story_target_s') else "")
            return
        prefs['story_target_s'] = int(round(secs)) if secs else None
        self.win.engine.save_preferences(prefs)
        self.edit_target.setText(_mmss(secs) if secs else "")
        self._refresh_labels()

    def _refresh_labels(self):
        n = self.list_edit.count()
        story = getattr(self.win, "_story", None) or {}
        dur = story.get("duration", 0.0) if n else 0.0
        target = self._target_s()
        text = self.win.txt("board_edit").format(blocks=n, duration=_mmss(dur))
        color = "#8a8a8a"
        if target and n:
            text += " / " + _mmss(target)
            color = "#3fb950" if abs(dur - target) <= 0.1 * target else "#d29922"
        self.lbl_edit.setText(text)
        self.lbl_edit.setStyleSheet(self._caption_style(color))
        self.lbl_unused.setText(self.win.txt("board_unused").format(blocks=self.list_unused.count()))
        self.lbl_empty.setVisible(n == 0)
        self.btn_clear.setEnabled(n > 0)

    # ------------------------------------------------------------------ actions
    def play_block(self, block):
        if not block:
            return
        ap = getattr(self.win, "audio_preview", None)
        if ap is None:
            return
        # the story plays everywhere now (no Play story switch): just go to the block and play
        offset = float((self.win.engine.load_preferences() or {}).get("offset", 0.133))
        ap.seek_source_time(self.sentences[block[0]]["cut_start"] + offset)
        if ap.player.playbackState() != ap.player.PlaybackState.PlayingState:
            ap.toggle_play()

    def move_selected(self, src, dst):
        items = src.selectedItems()
        if not items:
            return
        self._syncing = True
        try:
            for it in items:
                block = _decode(it.data(BLOCK_ROLE))
                src.takeItem(src.row(it))
                dst.addItem(self._make_item(block))
        finally:
            self._syncing = False
        self._sync_story()

    def _split(self, lst, item):
        f, l = _decode(item.data(BLOCK_ROLE))
        row = lst.row(item)
        self._syncing = True
        try:
            lst.takeItem(row)
            for k, i in enumerate(range(f, l + 1)):
                lst.insertItem(row + k, self._make_item((i, i)))
        finally:
            self._syncing = False
        self._sync_story()

    def _join_next(self, lst, item):
        row = lst.row(item)
        nxt = lst.item(row + 1)
        if nxt is None:
            return
        (f, l), (f2, l2) = _decode(item.data(BLOCK_ROLE)), _decode(nxt.data(BLOCK_ROLE))
        if f2 != l + 1:
            return
        self._syncing = True
        try:
            lst.takeItem(row + 1)
            item.setData(BLOCK_ROLE, _encode((f, l2)))
        finally:
            self._syncing = False
        lst.doItemsLayout()
        self._sync_story()

    def show_menu(self, lst, pos):
        item = lst.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            lst.clearSelection()
            item.setSelected(True)
        t = self.win.txt
        from gui.widgets.menus import context_menu
        menu = context_menu(self)
        menu.addAction(t("board_play"), lambda: self.play_block(_decode(item.data(BLOCK_ROLE))))
        if lst is self.list_edit:
            menu.addAction(t("board_remove"), lambda: self.move_selected(self.list_edit, self.list_unused))
            menu.addAction(t("board_to_top"), lambda: self._move_to(item, 0))
            menu.addAction(t("board_to_end"), lambda: self._move_to(item, self.list_edit.count() - 1))
        else:
            menu.addAction(t("board_add"), lambda: self.move_selected(self.list_unused, self.list_edit))
        menu.addSeparator()
        f, l = _decode(item.data(BLOCK_ROLE))
        a_split = menu.addAction(t("board_split"), lambda: self._split(lst, item))
        a_split.setEnabled(l > f)
        nxt = lst.item(lst.row(item) + 1)
        a_join = menu.addAction(t("board_join"), lambda: self._join_next(lst, item))
        a_join.setEnabled(nxt is not None and _decode(nxt.data(BLOCK_ROLE))[0] == l + 1)
        menu.exec(lst.viewport().mapToGlobal(pos))

    def _move_to(self, item, row):
        lst = self.list_edit
        block = _decode(item.data(BLOCK_ROLE))
        self._syncing = True
        try:
            lst.takeItem(lst.row(item))
            lst.insertItem(row, self._make_item(block))
        finally:
            self._syncing = False
        self._sync_story()

    def refresh_lengths(self, ctx):
        """Cut settings changed: recompute block lengths from (ops, fps, offset)."""
        self._ops_ctx = ctx
        if self.sentences:
            self.list_edit.viewport().update()
            self.list_unused.viewport().update()
            self._refresh_labels()
            self._notify("lengths")


def build_story_panel(win) -> QFrame:
    """Build the Story board activity panel and bind it to the main window."""
    win.story_board = StoryBoard(win)
    return wrap_activity_panel(win.story_board, scroll=True)
