#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/components/story_status.py
ROLE: One status line for the story, in the hub bar of every work page
DESCRIPTION:
One story, one timeline, everywhere: every page is a lens on the same story, so every page says the same
thing about it, in the same place (right of the hub bar's tabs):

    [Recording (original)]  Story 2:25 · 4 blocks · 1 voice patch · 3 cuts to check · In Resolve ✓

  - Recording (original): only while the player plays the raw recording instead of the story (amber;
    click goes back to the story). Comes from audio_preview.recording_mode / recording_mode_changed.
  - Story m:ss: how long the story plays, voice patches that make room / close up included
    (story_time.story_length); green / amber against the story's target. Click: the target field (on Story it
    takes the focus; elsewhere Story opens with it focused).
  - N blocks: the story board's blocks. Click: Story.
  - N voice patch(es): accepted patches the story still holds (voice_patch.edit.in_story). Click: Voice patch.
  - N cuts to check: the cuts Review flags (only when it can be counted without decoding the recording).
    Click: Review cuts.
  - Resolve: In Resolve ✓ / Send changes / Not sent yet — the Send button's own state
    (QPushButton#WfSend "sync"). Click: the Send ▾ menu (with Send itself on top). While Resolve isn't
    connected (the same check as Clean's Resolve chip): a grey "Resolve offline" whose tooltip says how to
    connect; click tries to connect again.

Short of room the parts go in this order: cuts, voice patches, blocks; the length and the Resolve state
(and the Recording pill) stay. On a page that fits its own row (HubNav.page_decides: Story) these optional
parts are the first to give way, before the page's "Target" word and the tabs' names; the status line then
never turns the bar compact itself. The room is what the hub bar's row has left (never under the corner's
Send / Home / Settings), so the hub bar's own compact mode is unchanged: HubNav.full_width() counts only
the parts that always stay.

Updates: the story board's story_changed, page changes (CornerActions.sync), the Send button's state, the
player's recording mode, and a 2 s poll while a status is on screen (accepted voice patches, target).
"""
import weakref

from PySide6.QtCore import Qt, QEvent, QObject, QPoint, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

import config

MUTED, TEXT, GREEN, AMBER = "#9a9a9a", "#d4d4d4", "#8fd19e", "#e3b341"
ESSENTIAL = ("rec", "length", "resolve")     # always shown when they have something to say
OPTIONAL = ("blocks", "patches", "cuts")     # shown left to right as room allows (cuts go first)
ORDER = ("rec", "length", "blocks", "patches", "cuts", "resolve")

_live = weakref.WeakSet()
_state = {"data": None, "patch_sig": None, "patch_count": None, "patch_spans": None,
          "cuts_key": None, "cuts": None, "hooked": weakref.WeakSet(), "timer": None}


# ---- data ----------------------------------------------------------------------------------------

def _story_clips_key(win):
    clips = (getattr(win, "_story", None) or {}).get("clips") or []
    out = []
    for c in clips:
        try:
            out.append((c.get("start_id"), c.get("end_id"), round(float(c["cut_start"]), 3), round(float(c["cut_end"]), 3)))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def _patch_count(win, spans):
    """Accepted voice patches the story still holds (cached on accepted.json and the story)."""
    try:
        from voice_patch.edit import accepted_signature, accepted_patches, in_story
        d = win._voice_patch_dir()
        sig = accepted_signature(d)
    except Exception:
        return 0
    if sig is None:
        return 0
    key = tuple(spans)
    if _state["patch_sig"] != sig or _state["patch_spans"] != key:
        try:
            n = sum(1 for p in accepted_patches(d) if in_story(p, spans))
        except Exception as exc:
            print(f"[story status] voice patches: {exc}")
            n = 0
        _state.update(patch_sig=sig, patch_spans=key, patch_count=n)
    return _state["patch_count"] or 0


def _review_flagged(j):
    try:
        from gui.views.review_view import ReviewView
        benign = ReviewView._BENIGN
    except Exception:
        benign = ("goes back in the recording", "trim inside a moment (a stumble cut out)")
    return any(n not in benign for n in (j.get("notes") or []))


def _cuts_to_check(win):
    """The cuts Review flags, or None when that can't be known cheaply (Review not built for this story and
    the recording not decoded yet: counting would decode it)."""
    key = _story_clips_key(win)
    if not key:
        return None
    if _state["cuts_key"] == key:
        return _state["cuts"]
    n = None
    rv = getattr(win, "review_view", None)
    rv_clips = getattr(rv, "_clips", None) if rv is not None else None
    if rv_clips and tuple((c.get("start_id"), c.get("end_id"), round(float(c["cut_start"]), 3), round(float(c["cut_end"]), 3))
                          for c in rv_clips) == key:
        n = sum(1 for j in (getattr(rv, "_joins", None) or []) if _review_flagged(j))
    else:
        canvas = getattr(win, "text_canvas", None)
        words = getattr(canvas, "words_data", None)
        audio_path = words[0].get("meta_audio_path") if words else None
        cache = getattr(win, "_clarity_audio", None)
        if audio_path and cache and cache[0] == audio_path:    # decoded already: counting is cheap
            try:
                from gui.views.review_view import collect_review
                data = collect_review(win)
                n = sum(1 for j in data[1] if _review_flagged(j)) if data else 0
            except Exception as exc:
                print(f"[story status] review: {exc}")
                n = None
        else:
            return None                 # not cached: try again once Review (or Clean) has decoded it
    _state.update(cuts_key=key, cuts=n)
    return n


def resolve_connected(win):
    """Is DaVinci Resolve connected: the same cheap check as Clean's Resolve chip (main_window._update_resolve_chip),
    no call into Resolve."""
    engine = getattr(win, "engine", None)
    rh = (getattr(engine, "resolve_handler", None) if engine is not None else None) or getattr(win, "resolve_handler", None)
    try:
        return bool(rh is not None and rh.is_connected())
    except Exception:
        return False


def collect(win):
    """{length, target, blocks, patches, cuts, resolve, rec} for the open story; None without a transcript."""
    canvas = getattr(win, "text_canvas", None)
    if canvas is None or not getattr(canvas, "words_data", None):
        return None
    import story_time
    board = getattr(win, "story_board", None)
    try:
        blocks = len(board.edit_blocks()) if board is not None else 0
    except Exception:
        blocks = 0
    has_story = bool(_story_clips_key(win))
    try:
        length = story_time.story_length(win) if has_story else 0.0
    except Exception:
        length = 0.0
    try:
        target = board._target_s() if board is not None and hasattr(board, "_target_s") else None
    except Exception:
        target = None
    try:
        spans = story_time.spans(win) if has_story else []
    except Exception:
        spans = []
    patches = _patch_count(win, spans)
    cuts = _cuts_to_check(win) if has_story and blocks > 1 else None
    btn = getattr(win, "btn_send_resolve", None)
    resolve = None
    if btn is not None:
        sync = btn.property("sync")
        syncing = bool(getattr(win, "_sync_job", None))
        resolve = "syncing" if syncing else ("synced" if sync == "synced" else "changed" if sync == "changed" else "new")
        if resolve != "syncing" and not resolve_connected(win):
            resolve = "offline"
    ap = getattr(win, "audio_preview", None)
    rec = bool(getattr(ap, "recording_mode", False)) if ap is not None else False
    return {"length": length, "target": target, "blocks": blocks, "has_story": has_story, "patches": patches,
            "cuts": cuts, "resolve": resolve, "rec": rec}


# ---- wiring --------------------------------------------------------------------------------------

class _SendWatch(QObject):
    """The Send button's 'sync' property changed (sent, story moved on): refresh."""

    def __init__(self, btn, win):
        super().__init__(btn)
        self.win = win
        btn.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.DynamicPropertyChange, QEvent.EnabledChange):
            refresh_all(self.win, soon=True)
        return False


def _hook(win):
    """Connect once to what changes the story's status (all optional: parts of the app land separately)."""
    hooked = _state["hooked"]
    board = getattr(win, "story_board", None)
    if board is not None and board not in hooked:
        if not hasattr(board, "story_changed"):
            board.story_changed = []
        board.story_changed.append(lambda _kind="story", w=weakref.ref(win): (w() is not None and refresh_all(w(), soon=True)))
        hooked.add(board)
    btn = getattr(win, "btn_send_resolve", None)
    if btn is not None and btn not in hooked:
        _SendWatch(btn, win)
        hooked.add(btn)
    ap = getattr(win, "audio_preview", None)
    sig = getattr(ap, "recording_mode_changed", None) if ap is not None else None
    if sig is not None and ap not in hooked:
        try:
            sig.connect(lambda *_a, w=weakref.ref(win): (w() is not None and refresh_all(w())))
            hooked.add(ap)
        except Exception:
            pass
    if _state["timer"] is None:
        t = QTimer()
        t.setInterval(2000)
        t.timeout.connect(lambda w=weakref.ref(win): (w() is not None and _poll(w())))
        _state["timer"] = t


def _poll(win):
    if not any(s.isVisible() for s in list(_live)):
        _state["timer"].stop()
        return
    refresh_all(win)


def refresh_all(win, soon=False):
    """Recompute the story's status and show it in every hub bar on screen."""
    if soon:
        if not getattr(win, "_story_status_pending", False):
            win._story_status_pending = True

            def _go(w=weakref.ref(win)):
                x = w()
                if x is not None:
                    x._story_status_pending = False
                    refresh_all(x)
            QTimer.singleShot(0, _go)
        return
    shown = [s for s in list(_live) if s.win is win and s.isVisible()]
    if not shown:
        _state["data"] = None
        return
    _hook(win)
    if not _state["timer"].isActive():
        _state["timer"].start()
    try:
        data = collect(win)
    except Exception as exc:
        print(f"[story status] {exc}")
        data = None
    _state["data"] = data
    for s in shown:
        s.show_data(data)


def invalidate_cuts():
    _state["cuts_key"] = None


# ---- the widget ----------------------------------------------------------------------------------

def _find_layout(lay, w):
    """The layout (inside lay) that holds widget w directly."""
    if lay is None:
        return None
    for i in range(lay.count()):
        it = lay.itemAt(i)
        if it.widget() is w:
            return lay
        sub = it.layout()
        if sub is not None:
            found = _find_layout(sub, w)
            if found is not None:
                return found
    return None


class StoryStatus(QWidget):
    def __init__(self, win, hub, parent=None):
        super().__init__(parent)
        self.win, self.hub = win, hub
        self.setObjectName("StoryStatus")
        self.setAttribute(Qt.WA_StyledBackground, True)
        r = config.S(8)
        self.setStyleSheet(f"""
            QWidget#StoryStatus {{ background: transparent; }}
            QPushButton#StPart {{ background: transparent; color: {TEXT}; border: 1px solid transparent; border-radius: {r}px;
                padding: {config.S(3)}px {config.S(6)}px; font-size: {config.FS(9.5)}pt; font-weight: 500; }}
            QPushButton#StPart:hover {{ background: #2a2a2a; color: #ffffff; }}
            QPushButton#StPart[tone="muted"] {{ color: {MUTED}; }}
            QPushButton#StPart[tone="green"] {{ color: {GREEN}; }}
            QPushButton#StPart[tone="amber"] {{ color: {AMBER}; }}
            QPushButton#StPart[tone="strong"] {{ color: #ffffff; font-weight: 600; }}
            QPushButton#StPart[tone="rec"] {{ background: #3a2a0c; color: #fbbf24; border-color: #a16207; font-weight: 600; }}
            QPushButton#StPart[tone="rec"]:hover {{ background: #4a360f; color: #fde68a; }}
            QLabel#StDot {{ color: #5a5a5a; background: transparent; font-size: {config.FS(9.5)}pt; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.parts, self.dots = {}, {}
        go = {"rec": self._recording_off, "length": self._length_clicked,
              "blocks": lambda: self._show("show_story"), "patches": lambda: self._show("show_voice"),
              "cuts": lambda: self._show("show_review"), "resolve": self._resolve_menu}
        for k in ORDER:
            if k not in ("rec", "length"):
                d = QLabel("·", self)
                d.setObjectName("StDot")
                d.hide()
                self.dots[k] = d
                lay.addWidget(d, 0, Qt.AlignVCenter)
            b = QPushButton(self)
            b.setObjectName("StPart")
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(config.S(26))
            b.clicked.connect(lambda _=False, fn=go[k]: fn())
            b.hide()
            self.parts[k] = b
            lay.addWidget(b, 0, Qt.AlignVCenter)
            if k == "rec":
                lay.addSpacing(config.S(6))
        self._has = {k: False for k in ORDER}     # the part has something to say
        self._room_host = None
        self._fit_pending = False
        self._story_shown = False
        self._resolve_offline = False
        _live.add(self)

    # -- clicks --
    def _show(self, fn):
        panel = getattr(self.win, "_versions_panel", None)
        if panel is not None and not panel.isHidden():
            panel.close_panel()
        if hasattr(self.win, fn):
            getattr(self.win, fn)()

    def _page(self):
        """The page whose hub bar holds this status."""
        page = self.hub.parentWidget()
        while page is not None and getattr(page, "step_track", None) is not self.hub:
            page = page.parentWidget()
        return page

    def _length_clicked(self):
        """The story's length: set its target. On Story the target field takes the focus; elsewhere Story opens
        with it focused (without a story yet, Story just opens)."""
        sv = getattr(self.win, "story_view", None)
        on_story = sv is not None and self._page() is sv
        if not on_story:
            self._show("show_story")
        else:
            panel = getattr(self.win, "_versions_panel", None)
            if panel is not None and not panel.isHidden():
                panel.close_panel()
        field = getattr(sv, "edit_target", None) if sv is not None else None
        if field is None or not getattr(self, "_story_shown", False):
            return

        def _focus(w=weakref.ref(field)):
            f = w()
            if f is not None and f.isVisible():
                f.setFocus(Qt.MouseFocusReason)
                f.selectAll()
        if on_story:
            _focus()
        else:
            QTimer.singleShot(0, _focus)

    def _recording_off(self):
        ap = getattr(self.win, "audio_preview", None)
        if ap is not None and hasattr(ap, "set_recording_mode"):
            ap.set_recording_mode(False)
        elif ap is not None:
            ap.recording_mode = False
        refresh_all(self.win)

    def _resolve_menu(self):
        if getattr(self, "_resolve_offline", False) and hasattr(self.win, "_refresh_davinci_connection"):
            self.win._refresh_davinci_connection()      # offline: try to connect again (no menu to send with)
            QTimer.singleShot(1500, lambda w=weakref.ref(self.win): (w() is not None and refresh_all(w())))
            return
        corner = getattr(self.win, "corner_actions", None)
        if corner is not None and hasattr(corner, "send_menu"):
            corner.send_menu(anchor=self.parts["resolve"], with_send=True)

    # -- text --
    def _t(self, key, default, **kw):
        try:
            return self.win.txt(key, default, **kw)
        except Exception:
            return default.format(**kw) if kw else default

    def _set(self, k, text, tone, tip):
        b = self.parts[k]
        if b.text() != text:
            b.setText(text)
        if b.property("tone") != tone:
            b.setProperty("tone", tone)
            b.style().unpolish(b)
            b.style().polish(b)
        b.setToolTip(tip)
        b.setAccessibleName(text)

    def show_data(self, d):
        import story_time
        has = {k: False for k in ORDER}
        if d:
            if d["rec"]:
                has["rec"] = True
                self._set("rec", self._t("st_recording", "Recording (original)"), "rec",
                          self._t("st_tt_recording", "The player plays the raw recording, not the story. "
                                                     "Click to play the story again"))
            if d["has_story"]:
                has["length"] = True
                dur = story_time.fmt_time(d["length"])
                tone, tip = "strong", self._t("st_tt_length_target", "How long the story plays, voice patches "
                                                                     "included. Click: set its target length")
                if d["target"]:
                    t = float(d["target"])
                    tone = "green" if abs(d["length"] - t) <= 0.1 * t else "amber"
                    tip += " · " + self._t("st_tt_target", "target {target}", target=story_time.fmt_time(t))
                self._set("length", self._t("st_length", "Story {duration}", duration=dur), tone, tip)
                n = d["blocks"]
                if n:
                    has["blocks"] = True
                    self._set("blocks", self._t("st_blocks_one", "1 block") if n == 1 else
                              self._t("st_blocks", "{n} blocks", n=n), "muted",
                              self._t("st_tt_blocks", "The story's blocks. Click: arrange them on the Story page"))
            else:
                has["length"] = True
                self._set("length", self._t("st_no_story", "No story yet"), "muted",
                          self._t("st_tt_no_story", "Nothing is in the story yet. Click: build it on the Story page"))
            n = d["patches"]
            if n:
                has["patches"] = True
                self._set("patches", self._t("st_patch_one", "1 voice patch") if n == 1 else
                          self._t("st_patches", "{n} voice patches", n=n), "muted",
                          self._t("st_tt_patches", "Voice patches playing in the story. Click: Voice patch"))
            n = d["cuts"]
            if n:
                has["cuts"] = True
                self._set("cuts", self._t("st_cut_one", "1 cut to check") if n == 1 else
                          self._t("st_cuts", "{n} cuts to check", n=n), "amber",
                          self._t("st_tt_cuts", "Cuts the review flags as worth a listen. Click: Review cuts"))
            res = d["resolve"]
            self._resolve_offline = res == "offline"
            if res and d["has_story"]:
                has["resolve"] = True
                if res == "offline":
                    self._set("resolve", self._t("st_resolve_offline", "Resolve offline"), "muted",
                              self._t("st_tt_resolve_offline", "DaVinci Resolve isn't connected. Open Resolve Studio "
                                      "with a project and set Preferences > System > General > External scripting "
                                      "to Local; in the free Resolve, run Workspace > Scripts > StoryCraft Bridge. "
                                      "Click: connect again"))
                elif res == "synced":
                    self._set("resolve", self._t("st_in_resolve", "In Resolve ✓"), "green",
                              self._t("st_tt_in_resolve", "Resolve has this exact story. Click: Send options"))
                elif res == "changed":
                    self._set("resolve", self._t("st_changed", "Changed since sent"), "amber",
                              self._t("st_tt_changed", "The story changed since it was sent to Resolve. "
                                                       "Click: send it again, or reload what was edited in Resolve"))
                elif res == "syncing":
                    self._set("resolve", self._t("st_syncing", "Sending to Resolve…"), "muted",
                              self._t("st_tt_changed", "The story changed since it was sent to Resolve. "
                                                       "Click: send it again, or reload what was edited in Resolve"))
                else:
                    self._set("resolve", self._t("st_not_sent", "Not sent yet"), "muted",
                              self._t("st_tt_not_sent", "This story hasn't been sent to Resolve yet. Click: send it"))
        self._has = has
        self._story_shown = bool(d and d["has_story"])
        self._quiet_page_bits(bool(d))
        self.fit()

    # -- room --
    def _quiet_page_bits(self, on):
        """One status everywhere: the pages' own "N blocks · m:ss" lines say less (no voice patches, no
        Resolve) and disagree with it; they step aside while the status shows."""
        if not on:
            return
        page = self._page()
        meta = getattr(page, "lbl_meta", None) if page is not None else None
        if meta is not None and not meta.isHidden():
            meta.hide()
        summ = getattr(self.win, "lbl_workflow_summary", None)    # Clean's top bar
        if summ is not None and getattr(self.win, "hub_nav", None) is self.hub and summ.maximumWidth() != 0:
            summ.setMaximumWidth(0)

    def _part_w(self, k):
        w = self.parts[k].sizeHint().width()
        if k in self.dots:
            w += self.dots[k].sizeHint().width()
        return w

    def essential_width(self):
        """Width of the parts that always stay (what HubNav.full_width counts)."""
        ks = [k for k in ESSENTIAL if self._has.get(k)]
        if not ks:
            return 0
        return sum(self._part_w(k) for k in ks) + (config.S(6) if "rec" in ks else 0)

    def extras_width(self):
        return sum(self._part_w(k) for k in OPTIONAL if self._has.get(k) and not self.parts[k].isHidden())

    def _reserve(self):
        """What the page counts on its row besides what its layout says (the pages' own meta lines step aside
        while the status shows: they take no room)."""
        chip = getattr(self.win, "lbl_resolve_chip", None)   # Clean: Resolve connected / offline comes first
        if chip is not None and getattr(self.win, "hub_nav", None) is self.hub and chip.text() and chip.isHidden():
            return chip.sizeHint().width() + config.S(8)
        return 0

    def _room(self):
        """Pixels the hub bar's row can still give the optional parts (never under the top-right corner)."""
        host = self.hub.parentWidget()
        lay = _find_layout(host.layout() if host is not None else None, self.hub)
        if lay is None or not self.hub.isVisible():
            return None
        g = lay.geometry()
        if g.width() > 0:
            self._row_rect = (lay, g)
        elif getattr(self, "_row_rect", (None,))[0] is lay:
            g = self._row_rect[1]      # invalidated just now (our own size change): the rect it had
        else:
            return None
        if self._room_host is not host:
            self._room_host = host
            host.installEventFilter(self)
        right = g.right()
        corner = getattr(self.win, "corner_actions", None)
        if corner is not None and corner.isVisible():
            ctl = host.mapFromGlobal(corner.mapToGlobal(QPoint(0, 0)))
            if ctl.y() < g.bottom() and ctl.y() + corner.height() > g.top():
                right = min(right, ctl.x() - config.S(12))
        used = lay.sizeHint().width() - self.extras_width()
        return right - g.left() + 1 - used - self._reserve() - config.S(8)

    def fit(self):
        """Show the optional parts that fit (blocks, then voice patches, then cuts)."""
        self._room()                   # remembers the row's rect before the hints are dropped (that clears it)
        self._fresh_hints()
        room = self._room()
        hub = self.hub
        if getattr(hub, "page_decides", False):
            # the page fits its own row (Story: the "Target" word goes before the tab names); never decide here
            if getattr(hub, "_self_compact", False):
                hub.set_self_compact(False)
                self._fresh_hints()
                room = self._room()
        elif room is not None:
            # not even length + Resolve fit: the bar's tools go icon-only (pages that don't do it themselves)
            if room < 0 and not getattr(hub, "_compact", False) and hasattr(hub, "set_self_compact"):
                hub.set_self_compact(True)
                self._fresh_hints()
                room = self._room()
            elif getattr(hub, "_self_compact", False) and room - hub.labels_delta() >= config.S(16):
                hub.set_self_compact(False)
                self._fresh_hints()
                room = self._room()
        room = room or 0
        show = set(k for k in ESSENTIAL if self._has.get(k))
        for k in OPTIONAL:
            if not self._has.get(k):
                continue
            w = self._part_w(k)
            if w <= room:
                show.add(k)
                room -= w
            else:
                room = -1           # keep the order: nothing after a part that didn't fit
        self._apply(show)

    def _apply(self, show):
        first = True
        dirty = False
        for k in ORDER:
            on = k in show
            if self.parts[k].isHidden() == on:
                self.parts[k].setVisible(on)
                dirty = True
            if k in self.dots:
                d_on = on and not first and k != "rec"
                if self.dots[k].isHidden() == d_on:
                    self.dots[k].setVisible(d_on)
            if on and k != "rec":
                first = False
        if dirty:
            self._fresh_hints()
        self.hub._status_changed()

    def has_any(self):
        return any(not b.isHidden() for b in self.parts.values())

    def _fresh_hints(self):
        """Drop the cached size hints up to the hub bar's row (they're read right away by _room)."""
        self.layout().invalidate()
        self.updateGeometry()
        self.hub.layout().invalidate()
        self.hub.updateGeometry()

    def fit_soon(self):
        if self._fit_pending:
            return
        self._fit_pending = True

        def _go(w=weakref.ref(self)):
            s = w()
            if s is not None:
                s._fit_pending = False
                if s.isVisible():
                    s.fit()
        QTimer.singleShot(0, _go)

    # -- events --
    def eventFilter(self, obj, ev):
        if obj is self._room_host and ev.type() == QEvent.Resize:
            self.fit_soon()
        return False

    def showEvent(self, e):
        super().showEvent(e)
        refresh_all(self.win, soon=True)
        t = _state.get("timer")
        if t is not None and not t.isActive():
            t.start()
