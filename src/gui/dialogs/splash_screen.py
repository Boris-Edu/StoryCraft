#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: splash_screen.py
ROLE: GUI Dialog
DESCRIPTION:
Splash screen loading window displayed during app initialization.
Draws a themed card (mark, app name, tagline, accent glow, progress line) with a
"LOADING..." status; the app name comes from config.APP_NAME.
"""

from gui.theme import qc

import os
import time
from PySide6.QtCore import Qt, QTimer, QObject
from PySide6.QtWidgets import QDialog, QVBoxLayout, QFrame, QLabel, QApplication, QGraphicsBlurEffect
from PySide6.QtGui import QColor, QFont, QPixmap, QPainter, QImage

import config
from gui.components.mixins import FramelessWindowMixin, _BaseDialog
from gui.utils import _app_icon, _center_on_screen, _txt, get_layout_icon_path, get_layout_dir


def _find_splash_path() -> str:
    """Find splashscreen.png in assets/layout across development and installed environments."""
    candidates = [
        get_layout_icon_path("splashscreen.png"),
        os.path.join(get_layout_dir(), "splashscreen.png"),
        # Dev repo layout: <repo_root>/assets/layout/splashscreen.png
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets", "layout", "splashscreen.png")),
        # Production install layout: <install_dir>/assets/layout/splashscreen.png
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "layout", "splashscreen.png")),
        # Legacy unpacked fallback
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "layout", "splashscreen.png")),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


class SplashInnerFrame(QFrame):
    """The splash card, drawn in code so it follows the colour theme: mark, name, tagline,
    a soft accent glow and an indeterminate progress line."""

    def __init__(self, splash_path: str, W: int, H: int, parent=None):
        super().__init__(parent)
        self.setObjectName("MainInnerFrame")
        self.setFixedSize(W, H)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._t0 = time.monotonic()
        self._phase = 0.0  # progress line position, 0..1
        self._t = 0.0      # animated mark clock (s)

    def advance(self):
        self.update()

    def paint_now(self):
        """Paint at once, without the event loop (used while the UI thread builds the main window)."""
        self.repaint()
        _flush_screen()

    def _tick_clock(self):
        # Follows the clock, not the number of frames: after a busy moment the animation is where
        # it should be instead of resuming from where it stopped
        self._t = time.monotonic() - self._t0
        self._phase = (self._t * 0.75) % 1.0

    def _layout(self):
        from PySide6.QtCore import QRectF
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pad = config.S(28)
        tile = QRectF(pad, pad, config.S(52), config.S(52))
        track = QRectF(pad, r.bottom() - pad + config.S(2), r.width() - 2 * pad, config.S(3))
        return r, pad, tile, track

    def _static_layer(self):
        """Everything that doesn't move (card, light, border, name, tagline, line track, version),
        drawn once: a frame then only paints the mark and the moving segment."""
        from PySide6.QtCore import QRectF, QPointF
        from PySide6.QtGui import QRadialGradient, QPen, QPainterPath
        from gui import brand
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr)
        if getattr(self, '_static', None) is not None and self._static[0] == key:
            return self._static[1]
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r, pad, tile, track = self._layout()
        radius = config.S(14)

        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setClipPath(path)
        p.fillRect(r, QColor("#0b0b14"))
        # Brand light: cyan from the upper left, violet from the lower right
        for corner, col, alpha, reach in ((QPointF(r.left(), r.top()), brand.CYAN, 55, 0.65),
                                          (QPointF(r.right(), r.bottom()), brand.VIOLET, 95, 0.85)):
            glow = QRadialGradient(corner, r.width() * reach)
            c0 = QColor(col); c0.setAlpha(alpha)
            c1 = QColor(col); c1.setAlpha(0)
            glow.setColorAt(0.0, c0)
            glow.setColorAt(1.0, c1)
            p.fillRect(r, glow)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, radius, radius)

        # Name and tagline (gui/brand.py)
        name_px = config.S(34)
        name_rect = QRectF(pad, tile.bottom() + config.S(14), r.width() - 2 * pad, config.S(40))
        brand.draw_wordmark(p, pad, name_rect.top() + name_px * 0.92, name_px)
        tag_font = QFont(self.font())
        tag_font.setPointSizeF(config.FS(9.5))
        p.setFont(tag_font)
        p.setPen(QColor("#a1a3bd"))
        p.drawText(QRectF(pad, name_rect.bottom() + config.S(2), r.width() - 2 * pad, config.S(20)),
                   Qt.AlignLeft | Qt.AlignVCenter, _txt("en", "splash_tagline"))

        # Progress line track
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)

        # Version, bottom right above the line
        small = QFont(self.font())
        small.setPointSizeF(config.FS(8))
        p.setFont(small)
        p.setPen(QColor("#6b6e8a"))
        p.drawText(QRectF(pad, track.top() - config.S(22), track.width(), config.S(16)),
                   Qt.AlignRight | Qt.AlignVCenter, f"v{config.VERSION}")
        p.end()
        self._static = (key, pm)
        return pm

    def paintEvent(self, event):
        from PySide6.QtCore import QRectF
        from gui import brand
        self._tick_clock()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(0, 0, self._static_layer())
        r, pad, tile, track = self._layout()

        # The animated mark
        brand.draw_mark_animated(p, tile, self._t)

        # Indeterminate progress segment moving along the track
        seg_w = track.width() * 0.28
        x = track.left() - seg_w + (track.width() + seg_w) * self._phase
        seg = QRectF(max(track.left(), x), track.top(), min(track.right(), x + seg_w) - max(track.left(), x), track.height())
        if seg.width() > 0:
            p.setPen(Qt.NoPen)
            p.setBrush(brand.gradient(seg.left(), seg.right()))
            p.drawRoundedRect(seg, track.height() / 2, track.height() / 2)
        p.end()


_CA_FLUSH = None


def _flush_screen():
    """macOS: a repaint outside the event loop waits in Core Animation's open transaction until the
    loop runs again; commit it now ([CATransaction flush]) so the frame reaches the screen."""
    global _CA_FLUSH
    import sys
    if sys.platform != "darwin":
        return
    if _CA_FLUSH is None:
        _CA_FLUSH = False
        try:
            import ctypes, ctypes.util
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            send = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p))
            cls, sel = objc.objc_getClass(b"CATransaction"), objc.sel_registerName(b"flush")
            if cls and sel:
                _CA_FLUSH = lambda: send(cls, sel)
        except Exception:
            _CA_FLUSH = False
    if _CA_FLUSH:
        try:
            _CA_FLUSH()
        except Exception:
            pass


class _BuildPulse(QObject):
    """While the main window is built (UI thread busy, no event loop), Qt still dispatches events to the
    widgets being created; on those, repaint the splash every ~33 ms so it keeps moving."""

    def __init__(self, frame):
        super().__init__()
        self._frame = frame
        self._last = 0.0
        self._busy = False

    def eventFilter(self, obj, event):
        now = time.monotonic()
        if not self._busy and now - self._last >= 0.033:
            self._busy = True
            self._last = now
            try:
                self._frame.paint_now()
            except Exception:
                pass
            finally:
                self._busy = False
        return False


class SplashScreen(FramelessWindowMixin, _BaseDialog):
    """
    Frameless, dark loading window displayed while engine/api are initializing.
    Shows animated "LOADING..." label (0-3 cycling dots at 300 ms).
    Closed by main.py once initialization completes and the main window is rendered.
    """

    def txt(self, key: str, **kwargs) -> str:
        return _txt("en", key, **kwargs)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.frameless_init(is_popup=True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        W, H = config.S(440), config.S(250)
        self.setFixedSize(W + config.S(24), H + config.S(24))

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(config.S(12), config.S(12), config.S(12), config.S(12))

        splash_file = _find_splash_path()
        self.inner_frame = SplashInnerFrame(splash_file, W, H, self)

        # The card's shadow is drawn by this window from a cached image (paintEvent): a shadow effect
        # on the card would blur it again on every animation frame
        self._shadow = None

        main_layout.addWidget(self.inner_frame)

        content_layout = QVBoxLayout(self.inner_frame)
        # Status text sits bottom-left, just above the progress line
        content_layout.setContentsMargins(config.S(28), 0, config.S(28), config.S(30))
        content_layout.addStretch()

        self._lbl_loading = QLabel(self.txt("lbl_loading").upper(), self.inner_frame)
        self._lbl_loading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._lbl_loading.setStyleSheet(f"""
            QLabel {{
                color: #a0a0a0;
                font-size: {config.FS(8)}pt;
                font-weight: 500;
                letter-spacing: 1.5px;
                background: transparent;
            }}
        """)
        content_layout.addWidget(self._lbl_loading)

        self.setStyleSheet("QDialog { background-color: transparent; }")

        # Icon & centering
        self.setWindowIcon(_app_icon())
        _center_on_screen(self, W, H)

        # Dot animation (calm rhythm: 300ms cycle)
        self._dot_count = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(300)
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self.inner_frame.advance)
        self._progress_timer.start(16)

    def _animate(self):
        dots = "." * (self._dot_count % 4)
        self._lbl_loading.setText(f"LOADING{dots}")
        self._dot_count += 1

    def set_status(self, text: str):
        self._timer.stop()
        # A sentence reads better without the wide spacing of the LOADING label
        self._lbl_loading.setStyleSheet(self._lbl_loading.styleSheet().replace("letter-spacing: 1.5px;", "letter-spacing: 0px;"))
        self._lbl_loading.setText(text)
        QApplication.processEvents()

    def _shadow_image(self):
        """The card's soft shadow (blur 24, black 190), rendered once per size."""
        from PySide6.QtCore import QRectF
        from PySide6.QtWidgets import QGraphicsScene, QGraphicsPixmapItem
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr)
        if self._shadow is not None and self._shadow[0] == key:
            return self._shadow[1]
        card = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        card.fill(Qt.transparent)
        p = QPainter(card)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(qc(0, 0, 0, 190))
        g = QRectF(self.inner_frame.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(QRectF(g.x() * dpr, g.y() * dpr, g.width() * dpr, g.height() * dpr),
                          config.S(14) * dpr, config.S(14) * dpr)
        p.end()
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(card)
        blur = QGraphicsBlurEffect()  # the blurred shape alone: the shadow without the shape on top
        blur.setBlurRadius(config.S(24))  # as the former shadow effect: not scaled by the pixel ratio
        blur.setBlurHints(QGraphicsBlurEffect.QualityHint)
        item.setGraphicsEffect(blur)
        scene.addItem(item)
        out = QPixmap(card.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        scene.render(p, QRectF(out.rect()), QRectF(card.rect()))
        p.end()
        out.setDevicePixelRatio(dpr)
        self._shadow = (key, out)
        return out

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.drawPixmap(0, 0, self._shadow_image())
        p.end()

    def start_build_pulse(self):
        """Keep the splash animated while the UI thread is busy building the main window."""
        if getattr(self, '_pulse', None) is None:
            self._pulse = _BuildPulse(self.inner_frame)
            QApplication.instance().installEventFilter(self._pulse)

    def stop_build_pulse(self):
        pulse = getattr(self, '_pulse', None)
        if pulse is not None:
            QApplication.instance().removeEventFilter(pulse)
            self._pulse = None

    def closeEvent(self, event):
        self.stop_build_pulse()
        self._timer.stop()
        self._progress_timer.stop()
        super().closeEvent(event)
