#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: gui/components/patch_playback.py
ROLE: Voice patches heard in StoryCraft's own player (not only in Resolve)
DESCRIPTION:
When the playhead reaches a patched span [a, b] of the story, the recording pauses at a and the patch plays
instead (its preview.wav when there is one: the span exactly as the story plays it, with the original audio
crossfaded at its edges — voice_patch/core.py; else the bare patch.wav). Then the recording goes on:
  - "close" (the story follows the patch): from b, once the patch is done;
  - "keep" (original timing): from b after b - a seconds — a longer patch goes on over what follows, a
    shorter one leaves the rest of the span silent — as the Resolve timeline will play it (voice_patch/edit.py).
While the patch plays, the playhead, the time readout and the spoken word follow it (virtual_source_t /
story_time_now): the playhead crosses the span in step with the patch instead of freezing.
One story everywhere: the patches play on every page (Clean too) whenever the story plays; only the player's
"Recording" switch (the original recording, raw) and the assembled-audio mode leave them out.
"""
import os
import time

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class PatchPlayback(QObject):
    def __init__(self, ap):
        super().__init__(ap)
        self.ap = ap
        self.player = None
        self.output = None
        self.active = None        # the patch playing now
        self.holding = False      # the recording is paused while the patch plays (it isn't, for the user)
        self._file_len = 0.0      # seconds of the file playing
        self._p0, self._t0, self._rate = 0.0, 0.0, 1.0   # file position at wall time _t0 (smooth clock)
        self._overlap = False     # "keep" + longer patch: the recording already went on under the patch's end
        self._mute_until = None   # "keep" + shorter patch: the recording stays silent up to b
        self._patches, self._sig, self._checked = [], None, 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._step)

    # -- which patches are in the story --------------------------------------------------------
    def patches(self):
        now = time.time()
        if now - self._checked < 1.0:
            return self._patches
        self._checked = now
        win = self.ap.main_window
        try:
            d = win._voice_patch_dir()
        except Exception:
            self._patches = []
            return self._patches
        from voice_patch.edit import accepted_signature
        sig = accepted_signature(d)        # also changes when the take of an inserted patch changes
        if sig is None:
            self._patches, self._sig = [], None
            return self._patches
        if sig != self._sig:
            try:
                from voice_patch.edit import accepted_patches
                self._patches = accepted_patches(d)
            except Exception:
                self._patches = []
            self._sig = sig
        return self._patches

    def _enabled(self):
        ap = self.ap
        return not ap.clean_ops and not getattr(ap, "recording_mode", False)

    def _in_story(self, p):
        try:
            import story_time as st
            from voice_patch.edit import in_story
            return in_story(p, st.spans(self.ap.main_window))
        except Exception:
            segs = self.ap._edit_segments
            return not segs or any(s < p["b"] and e > p["a"] for s, e in segs)

    def find(self, t):
        for p in self.patches():
            if p["a"] - 0.03 <= t < p["b"] - 0.05 and self._in_story(p):
                return p
        return None

    # -- the patch's own clock -----------------------------------------------------------------
    @staticmethod
    def _wav_len(path):
        try:
            import wave
            with wave.open(path, "rb") as w:
                return w.getnframes() / float(w.getframerate())
        except Exception:
            return None

    def _file(self, p):
        """(file to play, its length): the span-exact preview.wav when the patch has one, else patch.wav."""
        prev = p.get("preview") or os.path.join(os.path.dirname(p["path"]), "preview.wav")
        if prev and os.path.isfile(prev):
            return prev, float(p.get("preview_len") or self._wav_len(prev) or p.get("patch_s") or (p["b"] - p["a"]))
        return p["path"], float(self._wav_len(p["path"]) or p.get("patch_s") or (p["b"] - p["a"]))

    def _pos(self):
        """Seconds into the file playing (smooth: wall clock from the last anchor)."""
        if self.active is None:
            return 0.0
        if self.player is not None and self.player.playbackState() != QMediaPlayer.PlayingState:
            return min(self._file_len, self._p0)
        return min(self._file_len, self._p0 + (time.time() - self._t0) * self._rate)

    def _keep(self):
        return (self.active or {}).get("length") == "keep"

    def virtual_source_t(self):
        """Where the playhead is in the recording while the patch plays: it crosses [a, b] in step with it."""
        p = self.active
        if p is None:
            return None
        # the same mapping times the patched words in the transcript (voice_patch/transcript.py), so the
        # spoken word lights in step with the patch
        from voice_patch.transcript import source_time
        return source_time(self._pos(), float(p["a"]), float(p["b"]), self._file_len,
                           "keep" if self._keep() else "close")

    def story_time_now(self):
        """Story time while the patch plays: the span's start in the story + how far into the patch."""
        p = self.active
        if p is None or not self.holding:
            return None
        ap = self.ap
        if getattr(ap, "_story_active", False):          # the player's story clock (the ribbon uses it too)
            T = ap._story_map.to_story(self.virtual_source_t(), ap._story_hint)[0]
            if T is not None:
                return T
        try:
            import story_time as st
            e0 = st.edit_time(self.ap.main_window, float(p["a"]))
        except Exception:
            return None
        if e0 is None:
            return None
        pos = self._pos()
        return e0 + (min(pos, float(p["b"]) - float(p["a"])) if self._keep() else pos)

    def set_rate(self, rate):
        if self.active is not None and self.player is not None:
            self._p0, self._t0 = self._pos(), time.time()
            self.player.setPlaybackRate(rate)
        self._rate = rate

    # -- playing -------------------------------------------------------------------------------
    def _ensure(self):
        if self.player is None:
            self.player = QMediaPlayer(self)
            self.output = QAudioOutput(self)
            self.player.setAudioOutput(self.output)
            self.player.mediaStatusChanged.connect(self._on_status)

    def _start(self, p, t):
        self._ensure()
        path, flen = self._file(p)
        a, b = float(p["a"]), float(p["b"])
        if p.get("length") == "keep":
            offset = max(0.0, t - a)
        else:
            offset = max(0.0, (t - a) / (b - a) * flen) if b > a else 0.0
        self.active, self.holding = p, True
        self._file_len, self._overlap, self._mute_until = flen, False, None
        self._rate = self.ap.player.playbackRate()
        self.ap.player.pause()                       # the recording waits at the span (holding: still "playing")
        self.output.setVolume(self.ap.audio_output.volume())
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPlaybackRate(self._rate)
        self.player.setPosition(int(offset * 1000))
        self.player.play()
        self._p0, self._t0 = offset, time.time()
        self._timer.start()

    def _seek_main(self, t_src):
        ap = self.ap
        t = t_src
        if ap._preview_on:
            # the piece of the smooth preview the patch sits in, then on in PLAY order (blocks can be out of
            # recording order, so the "next" piece by recording time could be the wrong block)
            piece = next(((p0, s, e) for p0, s, e in ap._preview_map if s <= t_src - 0.02 < e), None)
            t = (piece[0] + min(t_src, piece[2]) - piece[1]) if piece else ap._source_to_preview(t_src)
        ap.player.setPosition(int(t * 1000))
        ap._start_pos_s = t
        ap._real_start_time = time.time()

    def _resume_at(self, t_src, muted_until=None):
        """The recording goes on from source time t_src (after the patch)."""
        self.holding = False
        self._mute_until = muted_until
        self.ap.audio_output.setMuted(muted_until is not None)
        self._seek_main(t_src)
        self.ap.player.play()

    def cancel(self):
        """Stop the patch for good (Stop pressed, a seek, another player): nothing resumes afterwards.
        The recording is left where the playhead was, so Play goes on from there."""
        vt = self.virtual_source_t() if self.holding else None
        self.active, self.holding, self._overlap, self._mute_until = None, False, False, None
        self._timer.stop()
        if self.player is not None:
            self.player.stop()
        try:
            self.ap.audio_output.setMuted(False)
            if vt is not None:
                self._seek_main(vt)
        except RuntimeError:
            pass

    def stop(self):
        self.cancel()

    def _step(self):
        """While the patch plays (30 ms): "keep" + longer patch hands back to the recording at b on time."""
        p = self.active
        if p is None:
            self._timer.stop()
            return
        if self.holding and self._keep() and not self._overlap and self._pos() >= float(p["b"]) - float(p["a"]):
            self._overlap = True             # the patch goes on over the start of what follows (as in Resolve)
            self._resume_at(float(p["b"]))

    def tick(self, t):
        """Each playback tick while the recording plays, t = source time."""
        if self._mute_until is not None and t >= self._mute_until - 0.01:
            self._mute_until = None
            self.ap.audio_output.setMuted(False)
        if not self._enabled():
            if self.active is not None:
                self.cancel()
            return
        if self.active is not None or self._mute_until is not None:
            return       # an overlap tail finishing / the silent rest of a "keep" span: not the patch again
        q = self.find(t)
        if q is not None:
            self._start(q, t)

    def _on_status(self, status):
        if status != QMediaPlayer.EndOfMedia or self.active is None:
            return
        p = self.active
        a, b = float(p["a"]), float(p["b"])
        self.active = None
        self._timer.stop()
        if not self.holding:
            return                            # "keep" overlap: the recording is already playing
        if p.get("length") == "keep" and self._file_len < (b - a):
            # shorter patch, original timing: the rest of the span stays silent, then the recording
            self._resume_at(a + self._file_len, muted_until=b)
        else:
            self._resume_at(b)

    def on_main_state(self, playing):
        """The recording was played / paused (by the user or another part of the app)."""
        if self.holding:
            if playing:                       # something started the recording during the patch: it wins
                self.active, self.holding, self._overlap = None, False, False
                self._timer.stop()
                if self.player is not None:
                    self.player.stop()
                self.ap.audio_output.setMuted(False)
            return
        if not playing and self.active is not None:
            self.cancel()                     # paused during a "keep" overlap tail: it stops too
        if not playing and self._mute_until is not None:
            self._mute_until = None
            self.ap.audio_output.setMuted(False)
