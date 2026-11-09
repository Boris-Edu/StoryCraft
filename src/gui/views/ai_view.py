#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/views/ai_view.py
ROLE: The AI tab (page 6): every AI action in one place
DESCRIPTION:
The AI round trips used to be scattered (Story's AI ▾, Review's Copy review / Import AI edit, Clean's
Restore AI marks). Here they sit together, each with what it does:
  - Claude, connected: the StoryCraft connector (MCP) lets Claude Desktop / Claude Code read the transcript
    and build or adjust the story directly;
  - an AI chat, by hand: copy the transcript (or the cut review), paste it in any AI chat, bring the answer
    back with Import;
  - the AI's marks in Clean: put them all back.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

import config

S, FS = config.S, config.FS


def _label(text, size, color, weight=400, wrap=True):
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setStyleSheet(f"color: {color}; font-size: {FS(size)}pt; font-weight: {weight}; background: transparent;")
    return lb


class AIView(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.t = lambda k, d: win.txt(k, d)
        self.setStyleSheet(f"""
            QFrame#AICard {{ background-color: #1e1e1e; border: 1px solid #2e2e2e; border-radius: {S(14)}px; }}
            QPushButton#Quiet {{ background-color: #2a2a2a; color: #e6e6e6; border: 1px solid #333333; border-radius: {S(9)}px;
                padding: {S(7)}px {S(14)}px; font-size: {FS(10)}pt; }}
            QPushButton#Quiet:hover {{ background-color: #333333; }}
            QPushButton#Primary {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7);
                color: #0b0b14; border: none; border-radius: {S(9)}px; padding: {S(7)}px {S(14)}px;
                font-size: {FS(10)}pt; font-weight: 600; }}
            QScrollArea {{ background: transparent; border: none; }}
            QWidget#AIHost {{ background: transparent; }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(S(20), S(12), S(20), S(12))   # the hub bar lines up with the other pages
        outer.setSpacing(S(12))
        top = QHBoxLayout()
        from gui.widgets.step_track import HubNav
        self.hub_nav = HubNav(win, active="ai")
        top.addWidget(self.hub_nav, 0, Qt.AlignVCenter)
        top.addStretch(1)
        outer.addLayout(top)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("AIHost")
        col = QVBoxLayout(host)
        col.setContentsMargins(0, S(4), S(6), S(8))
        col.setSpacing(S(14))
        col.addWidget(_label(self.t("ai_title", "AI"), 15, "#ffffff", 600))
        col.addWidget(_label(self.t("ai_intro", "Every AI helper in one place. Nothing here changes the story by itself: "
                                    "you bring an answer back, and ⌘Z undoes it."), 10, "#a3a3a3"))

        # 1. Claude, connected (StoryCraft connector): what is set up, and the one-time setup
        self.lbl_mcp = _label("", 9.5, "#a3a3a3")
        self.btn_desktop = self._btn(self.t("ai_connect_desktop", "Connect Claude Desktop"), self._connect_desktop,
                                     primary=True, tip=self.t("tt_ai_connect_desktop",
                                                              "Add StoryCraft to Claude Desktop's settings (a backup is kept)"))
        self.btn_code = self._btn(self.t("ai_copy_code_cmd", "Copy command for Claude Code"), self._copy_code_cmd,
                                  tip=self.t("tt_ai_copy_code_cmd", "Paste it in a terminal once"))
        col.addWidget(self._card(self.t("ai_mcp_title", "Claude, connected"),
                                 self.t("ai_mcp_body2", "Connect Claude once: then, with StoryCraft open, ask Claude in a "
                                        "chat, for example: “Read the transcript and build a 2-minute story about the "
                                        "garden.” It builds the story here and can send it to Resolve."),
                                 [self.btn_desktop, self.btn_code], extra=self.lbl_mcp))
        # 2. An AI chat: the story
        self.btn_copy_story = self._btn(self.t("board_copy_ai", "Copy for AI"), self._copy_story, primary=True,
                                        tip=self.t("tt_board_copy_ai", "Copy the transcript for an AI chat"))
        self.btn_import = self._btn(self.t("board_import_ai", "Import AI story…"), self._import,
                                    tip=self.t("tt_board_import_ai", "Bring back the story the AI wrote"))
        col.addWidget(self._card(self.t("ai_chat_story_title", "Build or adjust the story with an AI chat"),
                                 self.t("ai_chat_story_body", "Copy the transcript, paste it into any AI chat and ask for a "
                                        "story; bring its answer back with Import. The story you had stays in Versions."),
                                 [self.btn_copy_story, self.btn_import]))
        # 3. An AI chat: the cuts
        self.btn_copy_review = self._btn(self.t("review_copy_ai", "Copy review for AI"), self._copy_review,
                                         tip=self.t("review_tt_copy_ai2", "The whole story, cut by cut, as text for an AI chat"))
        self.btn_import2 = self._btn(self.t("board_import_ai", "Import AI story…"), self._import)
        col.addWidget(self._card(self.t("ai_chat_cuts_title", "Check the cuts with an AI chat"),
                                 self.t("ai_chat_cuts_body", "Copy every cut of the story as text: the AI says which ones jar "
                                        "and answers with a corrected story you bring back with Import."),
                                 [self.btn_copy_review, self.btn_import2]))
        # 4. The AI's marks in Clean
        self.btn_restore = self._btn(self.t("btn_restore_ai", "Restore AI marks"), self._restore,
                                     tip=self.t("tt_restore_ai", "Put back every mark the AI made"))
        col.addWidget(self._card(self.t("ai_marks_title", "The AI's marks in Clean"),
                                 self.t("ai_marks_body", "The fillers, retakes and errors the AI found when transcribing. "
                                        "Put them all back here; each kind has its own Restore in Clean (Found)."),
                                 [self.btn_restore]))
        col.addStretch(1)
        sc.setWidget(host)
        outer.addWidget(sc, 1)

    def _card(self, title, body, buttons, extra=None):
        card = QFrame()
        card.setObjectName("AICard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(S(18), S(14), S(18), S(14))
        lay.setSpacing(S(8))
        lay.addWidget(_label(title, 11.5, "#ffffff", 600))
        lay.addWidget(_label(body, 10, "#c4c4c4"))
        if extra is not None:
            lay.addWidget(extra)
        if buttons:
            row = QHBoxLayout()
            row.setSpacing(S(8))
            for b in buttons:
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
        return card

    def _btn(self, text, fn, primary=False, tip=""):
        b = QPushButton(text)
        b.setObjectName("Primary" if primary else "Quiet")
        b.setCursor(Qt.PointingHandCursor)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(lambda _=False: fn())
        return b

    def _install_dir(self):
        try:
            return self.win.engine.os_doc.install_dir
        except Exception:
            import os
            return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def refresh(self):
        import claude_setup as cs
        inst = self._install_dir()
        on = getattr(self.win, "_mcp_bridge", None) is not None
        desk, code = cs.desktop_state(inst), cs.code_state(inst)
        ok, dim, warn = "#86efac", "#a3a3a3", "#fcd34d"
        line = lambda c, s: f'<span style="color:{c}">{s}</span>'
        rows = [line(ok, self.t("ai_mcp_waiting", "● StoryCraft is waiting for Claude.")) if on else
                line(warn, self.t("ai_mcp_down", "○ The connector didn't start: reopen StoryCraft."))]
        desk_txt = {
            "ok": line(ok, self.t("ai_desk_ok", "✓ Claude Desktop is set up (restart it if you just connected it).")),
            "other": line(warn, self.t("ai_desk_other", "! Claude Desktop points to another copy of StoryCraft: Connect fixes it.")),
            "bad_file": line(warn, self.t("ai_desk_bad", "! Claude Desktop's settings file can't be read: fix it by hand.")),
            "no_app": line(dim, self.t("ai_desk_none", "○ Claude Desktop isn't installed.")),
        }.get(desk, line(dim, self.t("ai_desk_missing", "○ Claude Desktop isn't set up yet.")))
        code_txt = {
            "ok": line(ok, self.t("ai_code_ok", "✓ Claude Code is set up.")),
            "other": line(warn, self.t("ai_code_other", "! Claude Code points to another copy of StoryCraft: run the command again.")),
        }.get(code, line(dim, self.t("ai_code_missing", "○ Claude Code isn't set up (optional).")))
        self.lbl_mcp.setTextFormat(Qt.RichText)
        self.lbl_mcp.setText("<br>".join(rows + [desk_txt, code_txt]))
        self.lbl_mcp.setStyleSheet(f"color: {dim}; font-size: {FS(9.5)}pt; background: transparent;")
        self.btn_desktop.setVisible(desk not in ("no_app", "bad_file"))
        self.btn_desktop.setText(self.t("ai_reconnect_desktop", "Connect again") if desk == "ok" else
                                 self.t("ai_connect_desktop", "Connect Claude Desktop"))
        self.btn_desktop.setObjectName("Quiet" if desk == "ok" else "Primary")
        self.btn_desktop.style().unpolish(self.btn_desktop)
        self.btn_desktop.style().polish(self.btn_desktop)

    def _connect_desktop(self):
        import claude_setup as cs
        ok, info = cs.connect_desktop(self._install_dir())
        if ok:
            self._toast(self.t("ai_desk_done", "Claude Desktop is connected. Quit and reopen Claude Desktop to use it."))
        else:
            self._toast(self.t("ai_desk_failed", "Couldn't change Claude Desktop's settings: {err}").replace("{err}", info))
        self.refresh()

    def _copy_code_cmd(self):
        import claude_setup as cs
        QApplication.clipboard().setText(cs.code_command(self._install_dir()))
        self._toast(self.t("ai_code_copied", "Command copied: paste it in a terminal, then start Claude Code."))

    # -- actions -------------------------------------------------------------------------------
    def _toast(self, text):
        toast = getattr(self.win, "toast", None)
        if toast is not None:
            toast.show_text(text, 4000)
        else:
            self.win._show_temporary_status(text)

    def _copy_story(self):
        if hasattr(self.win, "_on_copy_ai_transcript"):
            self.win._on_copy_ai_transcript()
            self._toast(self.t("ai_copied_story", "Transcript copied: paste it into your AI chat."))

    def _import(self):
        if hasattr(self.win, "_on_import_ai_story"):
            self.win._on_import_ai_story()

    def _copy_review(self):
        text = self.win._cut_review() if hasattr(self.win, "_cut_review") else None
        if not text:
            self._toast(self.t("review_build_story_first", "Build a story first"))
            return
        QApplication.clipboard().setText(text)
        self._toast(self.t("msg_cut_review_copied", "Cut review copied ({cuts} cuts).").replace("{cuts}", str(text.count("\nCut "))))

    def _restore(self):
        if hasattr(self.win, "_restore_all_ai_marks"):
            self.win._restore_all_ai_marks()
            self._toast(self.t("ai_restored", "The AI's marks are back ({key} undoes).").replace(
                "{key}", "⌘Z" if __import__("sys").platform == "darwin" else "Ctrl+Z"))
