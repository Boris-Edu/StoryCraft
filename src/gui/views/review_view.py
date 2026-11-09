#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Szymon Wolarz
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""
MODULE: review_view.py
ROLE: GUI View
DESCRIPTION:
Cut-by-cut review of the current story edit. A list of the cuts (and of the clips that repeat each
other) on the left; on the right the selected cut: the words either side of the join and two players
that share one position: A "as recorded" (the outgoing clip running on as it was said) and B "as
edited" (the same lead-in, then the incoming clip). Switching A/B keeps the position, so the join can
be judged against what was really said. The data comes from algorithms.review_cuts, rebuilt here the
same way BadWordsGUI._cut_review builds it.

Written in the classic grey palette: gui/theme.py maps stylesheet colours to the active theme;
painted colours go through theme.qc / theme.color. Brand colours (cyan, violet) pass through.

Public API: collect_review(win); load_ok_keys(win); ReviewView(win, parent=None) with refresh(), stop(),
toggle_play(), skip(direction) and ok_joins().
"""
import atexit
import os
import sys
import re
import shutil
import struct
import subprocess
import tempfile
import wave

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea,
                               QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

import config
from gui import theme
from gui.icons import icon_pixmap

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
except Exception:  # pragma: no cover - QtMultimedia missing from the build
    QAudioOutput = QMediaPlayer = None

S, FS = config.S, config.FS

LEAD_S = 4.0     # lead-in before the cut, shared by A and B
TAIL_S = 3.0     # what follows the cut point
REPEAT_S = 7.0   # how much of each clip the Repeats players play

PAGE_BG = "#1e1e1e"
CARD_BG = "#262626"
CARD_BG_2 = "#222222"
QUIET = "#2a2a2a"
HOVER = "#333333"
LINE = "#303030"
TEXT = "#ffffff"
TEXT_2 = "#d4d4d4"
SECONDARY = "#a3a3a3"
MUTED = "#8a8a8a"
CYAN = "#22d3ee"
VIOLET = "#a855f7"
VIOLET_2 = "#c084fc"
AMBER = "#f59e0b"
AI_VOICE = getattr(config, "AI_VOICE_FG", "#67e8f9")   # synthetic speech, as the transcript shows it
GRADIENT = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22d3ee, stop:1 #a855f7)"
GRADIENT_HOVER = "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #67e8f9, stop:1 #c084fc)"
ON_GRADIENT = "#0b0b14"

# Notes that describe an editing choice rather than a likely fault (shown neutral, not amber)
_CHOICE_NOTES = ("goes back in the recording", "trim inside a moment", "speaker changes")


# ----------------------------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------------------------

def collect_review(win):
    """(clips, joins, repeats) for the current story, built exactly as BadWordsGUI._cut_review builds
    them (story clips from the story, speech level from the recording); None without a story."""
    import algorithms
    from engine import clarity
    board = getattr(win, 'story_board', None)
    sentences = (board.sentences if board is not None and board.sentences else None) or win._load_ai_sentences()
    if not sentences:
        return None
    index = {s["id"]: i for i, s in enumerate(sentences)}
    clips = []
    for c in (getattr(win, '_story', None) or {}).get("clips") or []:
        f, l = index.get(c.get("start_id")), index.get(c.get("end_id"))
        if f is not None and l is not None:
            clips.append({"first": f, "last": l, "start_id": c["start_id"], "end_id": c["end_id"],
                          "cut_start": c["cut_start"], "cut_end": c["cut_end"],
                          "text": " ".join(s["text"] for s in sentences[f:l + 1])})
    if not clips:
        return None
    audio = win._edit_audio()
    level = None
    if audio is not None:
        level = lambda t0, t1: clarity._speech_level(audio[int(max(0.0, t0) * 16000):int(t1 * 16000)])
    joins, repeats = algorithms.review_cuts(clips, sentences, level)
    return clips, joins, repeats


# "Sounds good" ticks, kept with the story: a small JSON next to the story board's file
# (<timeline>.board.json -> <timeline>.review_ok.json), {"version": 1, "ok": ["S002>S009", ...]}.
# A tick belongs to a join, named by its two sentence ids, so it survives reorders that keep that join.
# A voice patch's two joins go by "patch:<id>:in" / "patch:<id>:out".

def ok_key(before_id, after_id):
    """The key of the join between sentence before_id (the outgoing clip's last) and after_id (the incoming's first)."""
    return f"{before_id}>{after_id}"


def ok_path(win):
    """Where the ticks of the open story live, or None (no story board / no open transcript)."""
    board = getattr(win, 'story_board', None)
    try:
        path = board._path() if board is not None else None
    except Exception:
        path = None
    if not path or not path.endswith(".board.json"):
        return None
    return path[:-len(".board.json")] + ".review_ok.json"


def load_ok_keys(win):
    """The joins ticked "Sounds good" in Review for the open story: a set of "S002>S009" keys (the
    sentence id ending the clip before the cut > the one starting the clip after it). Empty when
    nothing is ticked or there is no story. Never raises."""
    path = ok_path(win)
    if not path or not os.path.isfile(path):
        return set()
    try:
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        keys = data.get("ok") if isinstance(data, dict) else data
        return {str(k) for k in keys or [] if isinstance(k, str) and (">" in k or k.startswith("patch:"))}
    except Exception:
        return set()


def save_ok_keys(win, keys):
    """Write the ticks for the open story (atomically). Returns True when written."""
    path = ok_path(win)
    if not path:
        return False
    try:
        import json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "ok": sorted(keys)}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[review] saving the ticks failed: {e}")
        return False


def _mmss(s):
    s = int(round(max(0.0, s)))
    return f"{s // 60}:{s % 60:02d}"


def _fmt_story(s):
    """A time in the story, as the Story page and the dock write it (story_time.fmt_time)."""
    import story_time as st
    return st.fmt_time(s)


def _mmss_tenths(s):
    s = max(0.0, s)
    return f"{int(s) // 60}:{s % 60:04.1f}"


_UNDO_KEY = "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"


def _audio_path(win):
    canvas = getattr(win, 'text_canvas', None)
    words = getattr(canvas, 'words_data', None) or []
    path = words[0].get("meta_audio_path") if words else None
    return path if path and os.path.exists(path) else None


def _ffmpeg(win):
    cmd = getattr(getattr(win, 'engine', None), 'ffmpeg_cmd', None)
    if cmd and (os.path.isabs(cmd) and os.path.exists(cmd) or shutil.which(cmd)):
        return cmd
    return shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


def _pcm_to_mono(raw, width, channels):
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[:len(b) // 3 * 3].reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    if channels > 1:
        x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    return x.astype(np.float32)


def _wav_layout(path):
    """(format, channels, rate, bytes per sample, data offset, data bytes) of a RIFF WAVE file, read from
    its chunks: plain PCM, IEEE float and WAVE_FORMAT_EXTENSIBLE (which Python 3.10's wave rejects)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF WAVE file")
        size = os.fstat(f.fileno()).st_size
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, n = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                body = f.read(n)
                tag, chans, rate, _br, _align, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]   # the sub-format GUID starts with the real tag
                fmt = (tag, chans, rate, bits // 8)
                if n % 2:
                    f.seek(1, 1)
            elif cid == b"data":
                if fmt is None:
                    raise ValueError("data before fmt")
                off = f.tell()
                if n in (0, 0xFFFFFFFF) or off + n > size:   # streamed / truncated header: to the end
                    n = size - off
                return fmt + (off, n)
            else:
                f.seek(n + (n % 2), 1)
    raise ValueError("no audio data")


class AudioReader:
    """Reads stretches of the timeline audio as mono float32 at the file's rate: straight from the WAV
    (seeks, so an hour-long recording costs nothing), or through ffmpeg for anything else."""

    def __init__(self, path, ffmpeg=None):
        self.path, self.ffmpeg = path, ffmpeg
        self.sr, self.duration, self._wav = 48000, None, None
        try:
            tag, ch, sr, width, off, n = _wav_layout(path)
            if tag not in (1, 3) or ch < 1 or width not in (1, 2, 3, 4, 8) or (tag == 3 and width not in (4, 8)):
                raise ValueError(f"unsupported WAV format {tag}/{width}")
            self._wav = (tag, ch, width, off, n // (ch * width))
            self.sr = sr
            self.duration = self._wav[4] / float(sr)
        except Exception:
            self._wav = None
            self.duration = self._probe_duration()

    def _run(self, cmd):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(cmd, capture_output=True, timeout=30, creationflags=flags)

    def _probe_duration(self):
        if not self.ffmpeg:
            return None
        try:
            err = self._run([self.ffmpeg, "-hide_banner", "-nostdin", "-i", self.path]).stderr.decode("utf-8", "replace")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
        except Exception:
            return None

    def read(self, t0, t1):
        t0 = max(0.0, float(t0))
        t1 = float(t1) if self.duration is None else min(float(t1), self.duration)
        if t1 <= t0:
            return np.zeros(0, np.float32)
        if self._wav:
            tag, ch, width, off, frames = self._wav
            a, b = min(int(round(t0 * self.sr)), frames), min(int(round(t1 * self.sr)), frames)
            with open(self.path, "rb") as f:
                f.seek(off + a * ch * width)
                raw = f.read(max(0, b - a) * ch * width)
            if tag == 3:
                x = np.frombuffer(raw[:len(raw) // width * width], dtype="<f4" if width == 4 else "<f8").astype(np.float32)
                return x[:len(x) // ch * ch].reshape(-1, ch).mean(axis=1).astype(np.float32) if ch > 1 else x
            return _pcm_to_mono(raw, width, ch)
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg not found")
        out = self._run([self.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", self.path,
                         "-ac", "1", "-ar", str(self.sr), "-f", "f32le", "-"])
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", "replace")[-300:])
        return np.frombuffer(out.stdout, dtype="<f4").astype(np.float32)


def _edited_run(reader, clips, k, t, length):
    """`length` seconds of the edit as it plays from time t inside clips[k] onwards (into the next clips)."""
    parts, left = [], length
    while left > 1e-3 and k < len(clips):
        end = float(clips[k]["cut_end"])
        take = min(left, max(0.0, end - t))
        if take > 0:
            parts.append(reader.read(t, t + take))
            left -= take
        k += 1
        if k < len(clips):
            t = float(clips[k]["cut_start"])
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def _peaks(x, n=900):
    if len(x) == 0:
        return np.zeros(1, np.float32)
    n = max(1, min(n, len(x)))
    step = len(x) // n
    return np.abs(x[:step * n]).reshape(n, step).max(axis=1)


def _check_range(reader, *times):
    if reader.duration is not None and any(t >= reader.duration - 0.05 for t in times):
        raise ValueError("outside the recording")


def _runs_before(reader, runs, i, length):
    """`length` seconds of the edit as it plays up to the end of runs[i] (earlier runs included)."""
    parts, left, j = [], length, i
    while left > 1e-3 and j >= 0:
        _k, s, e = runs[j]
        take = min(left, max(0.0, e - s))
        if take > 0:
            parts.insert(0, reader.read(e - take, e))
            left -= take
        j -= 1
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def _runs_from(reader, runs, i, length):
    """`length` seconds of the edit as it plays from the start of runs[i] on."""
    parts, left, j = [], length, i
    while left > 1e-3 and j < len(runs):
        _k, s, e = runs[j]
        take = min(left, max(0.0, e - s))
        if take > 0:
            parts.append(reader.read(s, s + take))
            left -= take
        j += 1
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def build_cut_snippets(reader, clips, n, spans=None, offset=0.0):
    """A and B for cut n (between clips[n-1] and clips[n]): dicts with samples, sr, join (s).
    With the story's kept ranges (story_time.spans: (clip, rec_start, rec_end) in play order) and the cut
    offset, B is the edit exactly as Play story and Send to Resolve play it (Clean's cuts inside the clips
    skipped); A is the recording around the same outgoing point. Without them: the clips' raw edges."""
    runs = [(k, a + offset, b + offset) for k, a, b in (spans or []) if b > a]
    outs = [i for i, r in enumerate(runs) if r[0] == n - 1]
    ins = [i for i, r in enumerate(runs) if r[0] == n]
    if outs and ins:
        io, ii = outs[-1], ins[0]
        ce = runs[io][2]
        _check_range(reader, ce, runs[ii][1])
        t0 = max(0.0, ce - LEAD_S)
        a = np.concatenate([reader.read(t0, ce), reader.read(ce, ce + TAIL_S)])
        lead_b = _runs_before(reader, runs, io, LEAD_S)
        b = np.concatenate([lead_b, _runs_from(reader, runs, ii, TAIL_S)])
        return ({"samples": a, "sr": reader.sr, "join": (ce - t0)},
                {"samples": b, "sr": reader.sr, "join": len(lead_b) / float(reader.sr)})
    out_clip, in_clip = clips[n - 1], clips[n]
    ce, cs = float(out_clip["cut_end"]), float(in_clip["cut_start"])
    _check_range(reader, ce, cs)
    t0 = max(0.0, ce - LEAD_S)
    lead = reader.read(t0, ce)
    a = np.concatenate([lead, reader.read(ce, ce + TAIL_S)])
    b = np.concatenate([lead, _edited_run(reader, clips, n, cs, TAIL_S)])
    join = len(lead) / float(reader.sr)
    return ({"samples": a, "sr": reader.sr, "join": join}, {"samples": b, "sr": reader.sr, "join": join})


def build_repeat_snippets(reader, clips, a, b):
    """The opening REPEAT_S seconds of clips a and b (1-based), each as the edit plays it."""
    res = []
    _check_range(reader, float(clips[a - 1]["cut_start"]), float(clips[b - 1]["cut_start"]))
    for k in (a - 1, b - 1):
        x = _edited_run(reader, clips[:k + 1], k, float(clips[k]["cut_start"]), REPEAT_S)
        res.append({"samples": x, "sr": reader.sr, "join": None})
    return tuple(res)


# ---- voice patches in the story: their two joins (recording → AI voice → recording) ------------------

def patch_audio_path(p):
    """The patch as the story plays it: its span-exact preview.wav (the original crossfaded at its edges) when
    there is one, else the bare patch.wav."""
    if "preview" in p:                     # accepted_patches gives it (None: the patch has none)
        prev = p.get("preview")
    else:
        prev = os.path.join(os.path.dirname(p["path"]), "preview.wav")
    return prev if prev and os.path.isfile(prev) else p["path"]


def _resample(x, sr_from, sr_to):
    if int(sr_from) == int(sr_to) or len(x) < 2:
        return x.astype(np.float32)
    n = max(1, int(round(len(x) * float(sr_to) / float(sr_from))))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def load_patch_audio(path, sr, ffmpeg=None):
    """A patch file as mono float32 at sr (the review reader's rate)."""
    r = AudioReader(path, ffmpeg)
    x = r.read(0.0, r.duration if r.duration is not None else 600.0)
    return _resample(x, r.sr, sr)


def _patch_runs_at(runs, a, b):
    """Where the patched span [a, b] sits in the story's runs [(clip, start, end)] (play order, recording s):
    ((runs, i) so _runs_before(.., i) ends at a, (runs, j) so _runs_from(.., j) starts at b), or None when
    a isn't in them. The span ends inside the clip that holds its start (or that clip's last run)."""
    if not runs:
        return None
    ia = before = None
    for i, (k, s, e) in enumerate(runs):
        if s <= a < e:
            ia, before = i, (runs[:i] + [(k, s, a)] + runs[i + 1:], i)
            break
    if ia is None:   # a just ahead of a run (the clip's or a kept range's start): the story up to that run
        for i, (k, s, e) in enumerate(runs):
            if a < s <= a + 0.5:
                ia, before = i, (runs, i - 1)
                break
    if ia is None:
        return None
    k0 = runs[ia][0]
    same = [ia]
    while same[-1] + 1 < len(runs) and runs[same[-1] + 1][0] == k0:
        same.append(same[-1] + 1)
    after = (runs, same[-1] + 1)          # past the clip's last kept range: what the story plays next
    for j in same:
        k, s, e = runs[j]
        if s <= b < e:
            after = (runs[:j] + [(k, b, e)] + runs[j + 1:], j)
            break
        if b < s:                         # b falls where the cut settings take a bit out
            after = (runs, j)
            break
    return before, after


def build_patch_snippets(reader, p, side, patch, runs=None):
    """A and B for a patch's join: side "in" (what comes before → the patch, at its span start a) or "out"
    (the patch → what follows, at b). patch: the patch's samples at reader.sr (patch_audio_path).
    A: the recording as it was around that point. B: how the story sounds now: the story up to a then the
    patch ("in"); the patch's end then the story from b ("out"). runs: the story's kept ranges in recording
    seconds (cut offset applied) as build_cut_snippets takes them; without them (or with a outside them) the
    recording either side of the span."""
    a, b = float(p["a"]), float(p["b"])
    sr = reader.sr
    edge = a if side == "in" else b
    _check_range(reader, edge)
    t0 = max(0.0, edge - LEAD_S)
    A = np.concatenate([reader.read(t0, edge), reader.read(edge, edge + TAIL_S)])
    where = _patch_runs_at(runs, a, b)

    def before_a(length):
        if where is None:
            return reader.read(max(0.0, a - length), a)
        rr, i = where[0]
        return _runs_before(reader, rr, i, length) if i >= 0 else np.zeros(0, np.float32)

    def from_b(length):
        if length <= 1e-3:
            return np.zeros(0, np.float32)
        if where is None:
            return reader.read(b, b + length)
        rr, j = where[1]
        return _runs_from(reader, rr, j, length)

    patch = np.asarray(patch if patch is not None else np.zeros(0), dtype=np.float32)
    if side == "in":
        lead = before_a(LEAD_S)
        after = patch[:int(TAIL_S * sr)]
        after = np.concatenate([after, from_b(TAIL_S - len(after) / float(sr))])   # a short patch: what follows
        B = np.concatenate([lead, after])
    else:
        n = int(LEAD_S * sr)
        lead = patch[-n:] if len(patch) > n else patch
        if len(lead) < n:                                                        # a short patch: what comes before
            lead = np.concatenate([before_a((n - len(lead)) / float(sr)), lead])
        B = np.concatenate([lead, from_b(TAIL_S)])
    return ({"samples": A, "sr": sr, "join": edge - t0},
            {"samples": B, "sr": sr, "join": len(lead) / float(sr)})


def _write_wav(path, x, sr):
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


# ----------------------------------------------------------------------------------------------
# Small widgets
# ----------------------------------------------------------------------------------------------

def _label(text, size, color, weight=400, wrap=False):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(f"color: {color}; font-size: {FS(size)}pt; font-weight: {weight}; background: transparent;"
                      f" border: none; padding: 0;")
    return lbl


def _glyph(kind, color, px):
    """A play / pause glyph (icons.py has none) as a QIcon, drawn at 2x."""
    s = px * 2
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    if kind == "play":
        path = QPainterPath()
        path.moveTo(s * 0.28, s * 0.18)
        path.lineTo(s * 0.84, s * 0.5)
        path.lineTo(s * 0.28, s * 0.82)
        path.closeSubpath()
        p.drawPath(path)
    else:
        r = s * 0.05
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.18, s * 0.18, s * 0.64), r, r)
        p.drawRoundedRect(QRectF(s * 0.58, s * 0.18, s * 0.18, s * 0.64), r, r)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def _quiet_qss(radius=10, pad_v=8, pad_h=14, size=10.5):
    return f"""
        QPushButton {{ background-color: {QUIET}; color: {TEXT_2}; border: 1px solid {LINE};
                       border-radius: {S(radius)}px; padding: {S(pad_v)}px {S(pad_h)}px; font-size: {FS(size)}pt;
                       font-weight: 500; }}
        QPushButton:hover {{ background-color: {HOVER}; color: {TEXT}; }}
        QPushButton:focus {{ border-color: {VIOLET}; }}
        QPushButton:disabled {{ background-color: {CARD_BG}; color: {MUTED}; }}
    """


def _primary_qss(radius=10, pad_v=8, pad_h=16, size=10.5):
    return f"""
        QPushButton {{ background: {GRADIENT}; color: {ON_GRADIENT}; border: 1px solid transparent;
                       border-radius: {S(radius)}px; padding: {S(pad_v)}px {S(pad_h)}px; font-size: {FS(size)}pt;
                       font-weight: 600; }}
        QPushButton:hover {{ background: {GRADIENT_HOVER}; }}
        QPushButton:focus {{ border: 1px solid {TEXT}; }}
        QPushButton:disabled {{ background: {QUIET}; color: {MUTED}; }}
    """


class _ElidedLabel(QLabel):
    """One line that elides to the width it gets (never widens its parent)."""

    def __init__(self, text, size, color, weight=400):
        super().__init__()
        self._full = text or ""
        self.setStyleSheet(f"color: {color}; font-size: {FS(size)}pt; font-weight: {weight}; background: transparent;"
                           f" border: none; padding: 0;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(self._full)

    def setFullText(self, text):
        self._full = text or ""
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        self.setText(QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, max(10, self.width())))
        self.setToolTip(self._full if self.text() != self._full else "")


def _badge(text, warn):
    """A small tag: warn True (amber, worth a listen), False (neutral) or "ai" (AI voice, cyan)."""
    lbl = QLabel(text)
    lbl.full = text
    if warn == "ai":
        fg, bg, line = AI_VOICE, "rgba(103, 232, 249, 0.10)", "rgba(103, 232, 249, 0.40)"
    else:
        fg, bg, line = (AMBER, "rgba(245, 158, 11, 0.12)", "rgba(245, 158, 11, 0.35)") if warn else \
            (SECONDARY, QUIET, LINE)
    lbl.setStyleSheet(f"color: {fg}; background-color: {bg}; border: 1px solid {line}; border-radius: {S(8)}px;"
                      f" padding: {S(2)}px {S(8)}px; font-size: {FS(9)}pt; font-weight: 500;")
    lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return lbl


class _FlowLayout(QLayout):
    """Left-to-right items that wrap onto the next line (the notes badges)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _place(self, rect, dry):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            hint = it.sizeHint()
            w = min(hint.width(), rect.width())
            if x > rect.x() and x + w > rect.right() + 1:
                x, y, line = rect.x(), y + line + self._gap, 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), QSize(w, hint.height())))
            x += w + self._gap
            line = max(line, hint.height())
        return y + line - rect.y()


class _ListCard(QPushButton):
    """A cut (or a pair of repeating clips) in the list: a real, checkable QPushButton."""

    stepped = Signal(int)  # -1 / +1 from the Up / Down keys

    def __init__(self, title, meta, badges, line1, line2, tooltip, parent=None):
        super().__init__(parent)
        self.setObjectName("ReviewCard")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(title)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"""
            QPushButton#ReviewCard {{ background-color: {CARD_BG}; border: 1px solid {LINE};
                                      border-radius: {S(12)}px; text-align: left; padding: 0; }}
            QPushButton#ReviewCard:hover {{ background-color: {QUIET}; border-color: #3a3a3a; }}
            QPushButton#ReviewCard:checked {{ background-color: #2c2c2c; border: 1px solid {VIOLET}; }}
            QPushButton#ReviewCard:focus {{ border: 1px solid {CYAN}; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(14), S(11), S(14), S(12))
        lay.setSpacing(S(6))
        top = QHBoxLayout()
        top.setSpacing(S(8))
        self._title = _ElidedLabel(title, 11, TEXT, 600)   # a patch join's title quotes its words: elided
        top.addWidget(self._title, 1)
        if meta:
            top.addWidget(_label(meta, 9.5, MUTED, 500))
        lay.addLayout(top)
        self._badges, self._more = [], None
        if badges:
            row = QHBoxLayout()
            row.setSpacing(S(6))
            for text, warn in badges:
                b = _badge(text, warn)
                b.setToolTip(text)
                b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                self._badges.append(b)
                row.addWidget(b)
            self._more = _badge("+0", False)
            self._more.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self._more.hide()
            self._more.setToolTip(" · ".join(t for t, _ in badges))
            row.addWidget(self._more)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addWidget(_ElidedLabel(line1, 9.5, SECONDARY))
        lay.addWidget(_ElidedLabel(line2, 9.5, TEXT_2))
        for w in self.findChildren(QLabel):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_badges()

    def _fit_badges(self):
        """Show the badges that fit on one line, then "+N" for the rest; a first badge too wide is elided."""
        if not self._badges:
            return
        m = self.layout().contentsMargins()
        room = self.width() - m.left() - m.right()
        gap = S(6)
        widths = []
        for b in self._badges:
            b.ensurePolished()
            fm = b.fontMetrics()
            extra = b.sizeHint().width() - fm.horizontalAdvance(b.text())
            widths.append(fm.horizontalAdvance(b.full) + extra)
        self._more.ensurePolished()
        more_extra = self._more.sizeHint().width() - self._more.fontMetrics().horizontalAdvance(self._more.text())

        def more_w(rest):
            return self._more.fontMetrics().horizontalAdvance(f"+{rest}") + more_extra + gap if rest else 0
        shown = len(widths)
        while shown > 0 and sum(widths[:shown]) + gap * (shown - 1) + more_w(len(widths) - shown) > room:
            shown -= 1
        elide = shown == 0
        shown = max(shown, 1)
        for i, b in enumerate(self._badges):
            b.setVisible(i < shown)
            text = b.full
            if i == 0 and elide:
                fm = b.fontMetrics()
                avail = room - more_w(len(widths) - 1) - (widths[0] - fm.horizontalAdvance(b.full))
                text = fm.elidedText(b.full, Qt.ElideRight, max(S(30), avail))
            if b.text() != text:
                b.setText(text)
        rest = len(widths) - shown
        self._more.setText(f"+{rest}")
        self._more.setVisible(rest > 0)

    def sizeHint(self):
        h = self.layout().sizeHint().height()
        return QSize(S(260), h)

    def minimumSizeHint(self):
        return self.sizeHint()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down):
            self.stepped.emit(-1 if e.key() == Qt.Key_Up else 1)
            return
        super().keyPressEvent(e)


class _Wave(QWidget):
    """A snippet's waveform (peaks drawn with QPainter), the shared playhead and the join point."""

    seek = Signal(float)

    def __init__(self, accent, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.peaks = np.zeros(1, np.float32)
        self.duration = 0.0
        self.join = None
        self.pos = 0.0
        self.active = False
        self.after_tint = None   # colour of the part after the join (None: dimmed, "what the edit drops")
        self.cut_label = "cut"
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(S(96))
        self.setCursor(Qt.PointingHandCursor)

    def set_data(self, peaks, duration, join):
        self.peaks, self.duration, self.join = peaks, duration, join
        self.update()

    def set_pos(self, pos):
        if abs(pos - self.pos) > 1e-3:
            self.pos = pos
            self.update()

    def _x_to_t(self, x):
        pad = S(4)
        return max(0.0, min(self.duration, (x - pad) / max(1, self.width() - 2 * pad) * self.duration))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration > 0:
            self.seek.emit(self._x_to_t(e.position().x()))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and self.duration > 0:
            self.seek.emit(self._x_to_t(e.position().x()))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pad = S(4)
        p.translate(pad, 0)
        w, h = self.width() - 2 * pad, self.height()
        top_pad = S(14)
        mid = top_pad + (h - top_pad) / 2.0
        amp = (h - top_pad) / 2.0 - S(2)
        if self.duration <= 0:
            p.setPen(QPen(theme.qc(LINE), 1))
            p.drawLine(QPointF(0, mid), QPointF(w, mid))
            p.end()
            return
        jx = None if self.join is None else self.join / self.duration * w
        if jx is not None:
            # the part after the join: a quiet panel, so A's "what was said" and B's "next clip" read apart
            shade = theme.qc(QUIET)
            shade.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(shade)
            p.drawRoundedRect(QRectF(jx, top_pad, w - jx, h - top_pad), S(6), S(6))
        step = S(3)
        bar = max(1.0, step * 0.62)
        n = max(1, w // step)
        src = self.peaks
        idx = np.linspace(0, len(src), n + 1).astype(int)
        px = self.pos / self.duration * w
        played = theme.qc(self.accent if self.active else SECONDARY)
        idle = theme.qc("#5c5c5c" if self.active else "#4a4a4a")
        after = theme.qc(self.after_tint) if self.after_tint else None
        p.setPen(Qt.NoPen)
        for i in range(n):
            seg = src[idx[i]:max(idx[i] + 1, idx[i + 1])]
            v = float(seg.max()) if len(seg) else 0.0
            bh = max(S(1), (v ** 0.7) * amp)
            x = i * step
            if x + bar / 2 <= px:
                c = played
            elif jx is not None and x >= jx:
                c = QColor(after) if after is not None else QColor(idle)
                if after is not None:
                    c.setAlpha(150)
                else:
                    c.setAlpha(140)
            else:
                c = idle
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x, mid - bh, bar, bh * 2), bar / 2, bar / 2)
        if jx is not None:
            pen = QPen(theme.qc(VIOLET_2), max(1, S(1)), Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(jx, top_pad - S(2)), QPointF(jx, h))
            f = p.font()
            f.setPointSize(max(7, FS(8)))
            p.setFont(f)
            p.setPen(theme.qc(VIOLET_2))
            tw = QFontMetrics(f).horizontalAdvance(self.cut_label)
            p.drawText(QPointF(min(max(0, jx - tw / 2), w - tw), top_pad - S(4)), self.cut_label)
        # playhead
        head = theme.qc(TEXT)
        p.setPen(QPen(head, max(1.5, S(1.5))))
        p.drawLine(QPointF(px, top_pad), QPointF(px, h))
        p.setPen(Qt.NoPen)
        p.setBrush(head)
        p.drawEllipse(QPointF(px, top_pad), S(3), S(3))
        p.end()


class _PlayerCard(QFrame):
    """One side (A or B): letter, title, line of context, a play button and the waveform."""

    activate = Signal()          # the card (not its button) was clicked
    play_clicked = Signal()
    seek = Signal(float)

    def __init__(self, letter, accent, parent=None):
        super().__init__(parent)
        self.letter, self.accent = letter, accent
        self.setObjectName("PlayerCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(16), S(14), S(16), S(14))
        lay.setSpacing(S(10))
        top = QHBoxLayout()
        top.setSpacing(S(10))
        self.badge = QLabel(letter)
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setFixedSize(S(28), S(28))
        top.addWidget(self.badge, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(S(1))
        self.title = _label("", 11.5, TEXT, 600)
        self.desc = _ElidedLabel("", 9.5, MUTED)
        texts.addWidget(self.title)
        texts.addWidget(self.desc)
        top.addLayout(texts, 1)
        self.btn = QPushButton()
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setFixedSize(S(36), S(36))
        self.btn.setIconSize(QSize(S(14), S(14)))
        self.btn.clicked.connect(self.play_clicked)
        top.addWidget(self.btn, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        self.wave = _Wave(accent)
        self.wave.seek.connect(self.seek)
        lay.addWidget(self.wave)
        self.set_state(False, False)

    def set_texts(self, title, desc, tooltip, accessible):
        self.title.setText(title)
        self.desc.setFullText(desc)
        self.btn.setToolTip(tooltip)
        self.btn.setAccessibleName(accessible)

    def set_state(self, active, playing):
        self.wave.active = active
        self.wave.update()
        border = self.accent if active else LINE
        self.setStyleSheet(f"QFrame#PlayerCard {{ background-color: {CARD_BG}; border: 1px solid {border};"
                           f" border-radius: {S(16)}px; }}")
        if active:
            self.badge.setStyleSheet(f"background-color: {self.accent}; color: {ON_GRADIENT}; border-radius: {S(14)}px;"
                                     f" font-size: {FS(10.5)}pt; font-weight: 700;")
        else:
            self.badge.setStyleSheet(f"background-color: transparent; color: {self.accent};"
                                     f" border: 1px solid {self.accent}; border-radius: {S(14)}px;"
                                     f" font-size: {FS(10.5)}pt; font-weight: 700;")
        if active:
            self.btn.setStyleSheet(_primary_qss(radius=17, pad_v=0, pad_h=0))
            self.btn.setIcon(_glyph("pause" if playing else "play", ON_GRADIENT, S(14)))
        else:
            self.btn.setStyleSheet(_quiet_qss(radius=17, pad_v=0, pad_h=0))
            self.btn.setIcon(_glyph("play", theme.color(TEXT_2), S(14)))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.activate.emit()
        super().mousePressEvent(e)


class _Fact(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(S(3))
        self.caption = _label("", 9, MUTED, 500)
        self.value = _label("", 11.5, TEXT, 600)
        lay.addWidget(self.caption)
        lay.addWidget(self.value)

    def set(self, caption, value):
        self.caption.setText(caption)
        self.value.setText(value)


# ----------------------------------------------------------------------------------------------
# The screen
# ----------------------------------------------------------------------------------------------

class ReviewView(QWidget):
    """Cut-by-cut review of the story edit. refresh() re-collects the data; stop() stops playback."""

    composeRequested = Signal()   # kept for the window's old wiring; "Go to the story" calls win.show_story()

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("ReviewView")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"QWidget#ReviewView {{ background-color: {PAGE_BG}; }}")
        self._clips, self._joins, self._repeats = [], [], []
        self._mode = "cuts"
        self._show_all = False
        self._ok = set()                 # "Sounds good" ticks, as "S002>S009" keys (kept with the story)
        self._patches = []               # the voice patches' joins ("Patch in" / "Patch out"), see _collect_patches
        self._join_t = {}                # cut n -> its place in the story (orders the patch joins among the cuts)
        self._patch_audio = {}           # (file, mtime, sr) -> samples
        self._index = {"cuts": 0, "repeats": 0}
        self._cards = []
        self._snips = (None, None)
        self._side = 0
        self._pos = 0.0                  # the shared position (seconds)
        self._playing = False
        self._pending = {}               # player -> position (ms) to apply once it has loaded
        self._reader = None
        self._audio_error = None
        self._tmp = tempfile.mkdtemp(prefix="storycraft-review-")
        _tmp = self._tmp
        atexit.register(shutil.rmtree, _tmp, True)
        self.destroyed.connect(lambda *_: shutil.rmtree(_tmp, True))
        self._serial = 0
        self._players, self._outputs = [], []
        if QMediaPlayer is not None:
            for i in range(2):
                out = QAudioOutput(self)
                out.setVolume(1.0)
                pl = QMediaPlayer(self)
                pl.setAudioOutput(out)
                pl.mediaStatusChanged.connect(lambda st, i=i: self._on_status(i, st))
                pl.playbackStateChanged.connect(lambda st, i=i: self._on_playback(i, st))
                self._players.append(pl)
                self._outputs.append(out)
        self._tick = QTimer(self)
        self._tick.setInterval(30)
        self._tick.timeout.connect(self._on_tick)
        self._build()
        for key, fn in (("A", lambda: self._switch(0)), ("B", lambda: self._switch(1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

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

    def _build(self):
        # The story and the tools around it, in the same place as on every work page
        from gui.widgets.step_track import HubNav
        root = QVBoxLayout(self)
        root.setContentsMargins(S(20), S(12), S(20), 0)
        root.setSpacing(S(2))
        top = QHBoxLayout()
        self.hub_nav = HubNav(self.win, active="review")
        top.addWidget(self.hub_nav)
        top.addStretch(1)
        root.addLayout(top)
        self._stack = QStackedLayout()
        root.addLayout(self._stack, 1)
        self._stack.addWidget(self._build_empty())
        self._stack.addWidget(self._build_content())

    def _build_empty(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(page)
        outer.addStretch(1)
        box = QFrame()
        box.setObjectName("EmptyCard")
        box.setMaximumWidth(S(460))
        box.setStyleSheet(f"QFrame#EmptyCard {{ background-color: {CARD_BG}; border: 1px solid {LINE};"
                          f" border-radius: {S(16)}px; }}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(S(32), S(30), S(32), S(30))
        lay.setSpacing(S(10))
        icon = QLabel()
        icon.setPixmap(icon_pixmap("story", theme.color(VIOLET_2), S(30)))
        icon.setStyleSheet("background: transparent; border: none;")
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)
        title = _label(self.t("review_empty_title", "No story to review yet"), 15, TEXT, 600)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        body = _label(self.t("review_empty_body", "Build a story on the Story page first. Every cut between its clips"
                             " shows up here, to hear and judge."), 10.5, SECONDARY, wrap=True)
        body.setAlignment(Qt.AlignCenter)
        lay.addWidget(body)
        lay.addSpacing(S(6))
        self.btn_go_story = QPushButton(self.t("review_go_compose", "Go to the story"))
        self.btn_go_story.setCursor(Qt.PointingHandCursor)
        self.btn_go_story.setToolTip(self.t("review_tt_go_compose", "Open the Story page to build it"))
        self.btn_go_story.setStyleSheet(_primary_qss())
        self.btn_go_story.clicked.connect(self._go_story)
        self.btn_compose = self.btn_go_story   # old name, for callers that still use it
        lay.addWidget(self.btn_go_story, 0, Qt.AlignCenter)
        lay.addSpacing(S(10))
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {LINE}; border: none;")
        lay.addWidget(sep)
        lay.addSpacing(S(6))
        self.btn_whole = QPushButton(self.t("review_use_whole", "Use the whole interview"))
        self.btn_whole.setCursor(Qt.PointingHandCursor)
        self.btn_whole.setStyleSheet(_quiet_qss())
        self.btn_whole.clicked.connect(self._use_whole_interview)
        lay.addWidget(self.btn_whole, 0, Qt.AlignCenter)
        why = _label(self.t("review_use_whole_why", "Makes the whole interview the story, in its recorded order. "
                            "Nothing is moved or left out, so the only cuts to review are the ones made in Clean "
                            "(fillers, retakes, long pauses). If nothing was cleaned, the clips simply follow each "
                            "other and there is little to hear. For story cuts, build a story on the Story page."),
                     9.5, MUTED, wrap=True)
        why.setAlignment(Qt.AlignCenter)
        lay.addWidget(why)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box, 10)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        # scrolls on a short window (the preview dock below takes room): the card is never squeezed
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                             " QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(page)
        return scroll

    def _build_content(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        row = QHBoxLayout(page)
        row.setContentsMargins(S(20), S(18), S(20), S(18))
        row.setSpacing(S(20))

        # left: header + list
        self._left = QFrame()
        self._left.setObjectName("ReviewLeft")
        self._left.setStyleSheet(f"QFrame#ReviewLeft {{ background-color: {CARD_BG_2}; border: 1px solid {LINE};"
                                 f" border-radius: {S(16)}px; }}")
        left = QVBoxLayout(self._left)
        left.setContentsMargins(S(14), S(16), S(14), S(14))
        left.setSpacing(S(12))
        self.btn_back_story = self.hub_nav.buttons["story"]      # "back to the story" is the bar's Story
        left.addWidget(_label(self.t("review_title", "Review"), 15, TEXT, 600))
        # The AI round trip covers the whole edit, so it lives here once, not under every cut
        ai = QVBoxLayout()  # stacked: the list column is narrow
        ai.setSpacing(S(6))
        self.btn_copy = QPushButton(self.t("review_copy_ai", "Copy review for AI"))
        self.btn_copy.setToolTip(self.t("review_tt_copy_ai2", "The whole story, cut by cut, as text for an AI chat: it "
                                        "answers with a corrected story you bring back with Import AI story"))
        self.btn_import_ai = QPushButton(self.t("review_import_ai", "Import AI story…"))
        self.btn_import_ai.setToolTip(self.t("review_tt_import_ai", "Bring back the corrected story the AI wrote"))
        for b in (self.btn_copy, self.btn_import_ai):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_quiet_qss())
            ai.addWidget(b)
        self.btn_copy.clicked.connect(self._copy_for_ai)
        self.btn_import_ai.clicked.connect(self._import_ai_edit)
        left.addLayout(ai)
        self.btn_copy.hide()        # the AI tab holds every AI helper now (Copy review for AI, Import)
        self.btn_import_ai.hide()
        seg = QFrame()
        seg.setObjectName("ReviewSeg")
        seg.setStyleSheet(f"QFrame#ReviewSeg {{ background-color: {PAGE_BG}; border: 1px solid {LINE};"
                          f" border-radius: {S(10)}px; }}")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(S(3), S(3), S(3), S(3))
        sl.setSpacing(S(3))
        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)
        self.btn_cuts, self.btn_repeats = QPushButton(), QPushButton()
        for b, mode, tip in ((self.btn_cuts, "cuts", self.t("review_tt_tab_cuts", "Every join of the story, in play order")),
                             (self.btn_repeats, "repeats", self.t("review_tt_tab_repeats", "Clips that say much the same thing"))):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {SECONDARY}; border: 1px solid transparent;
                               border-radius: {S(8)}px; padding: {S(6)}px {S(6)}px; font-size: {FS(10)}pt;
                               font-weight: 500; }}
                QPushButton:hover {{ color: {TEXT}; }}
                QPushButton:checked {{ background-color: {HOVER}; color: {TEXT}; font-weight: 600; }}
                QPushButton:focus {{ border-color: {VIOLET}; }}
            """)
            b.clicked.connect(lambda _=False, m=mode: self._set_mode(m))
            self._seg_group.addButton(b)
            sl.addWidget(b, 1)
        self.btn_cuts.setChecked(True)
        left.addWidget(seg)
        # What's worth a listen, counted apart: the flagged cuts (the Story status line's "cuts to check")
        # and the voice patches' joins (always listed)
        self.lbl_check = _label("", 9.5, AMBER, 500, wrap=True)
        self.lbl_check.hide()
        left.addWidget(self.lbl_check)
        self.btn_all = QPushButton()
        self.btn_all.setCheckable(True)
        self.btn_all.setCursor(Qt.PointingHandCursor)
        self.btn_all.setToolTip(self.t("review_tt_show_all", "Only flagged cuts are listed. Any cut also opens by "
                                       "clicking it in the transcript or on the waveform."))
        self.btn_all.setStyleSheet(_quiet_qss() + f" QPushButton:checked {{ background-color: {HOVER}; color: {TEXT}; }}")
        self.btn_all.toggled.connect(self._toggle_all)
        self.btn_all.hide()
        left.addWidget(self.btn_all)
        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFrameShape(QFrame.NoFrame)
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_lay = QVBoxLayout(self._list_host)
        self._list_lay.setContentsMargins(0, 0, S(4), 0)
        self._list_lay.setSpacing(S(8))
        self._list_lay.addStretch(1)
        self._list_scroll.setWidget(self._list_host)
        self._list_scroll.viewport().setStyleSheet("background: transparent;")
        left.addWidget(self._list_scroll, 1)
        self._card_group = QButtonGroup(self)
        self._card_group.setExclusive(True)
        row.addWidget(self._left, 0)

        # right: the selected cut
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(host)
        hl.setContentsMargins(0, 0, S(4), 0)
        detail = QWidget()
        detail.setMaximumWidth(S(1040))
        hl.addWidget(detail, 1)
        scroll.setWidget(host)
        row.addWidget(scroll, 1)
        self._detail_widget = detail

        d = QVBoxLayout(detail)
        d.setContentsMargins(S(4), S(2), 0, 0)
        d.setSpacing(S(16))
        # Opened from Voice patch's Insert patch: one line saying why we're here, and the way back
        self._patch_note = QFrame()
        self._patch_note.setObjectName("ReviewPatchNote")
        self._patch_note.setStyleSheet(f"QFrame#ReviewPatchNote {{ background-color: rgba(103, 232, 249, 0.08);"
                                       f" border: 1px solid rgba(103, 232, 249, 0.35); border-radius: {S(12)}px; }}")
        pn = QHBoxLayout(self._patch_note)
        pn.setContentsMargins(S(16), S(8), S(10), S(8))
        pn.setSpacing(S(10))
        self.lbl_patch_note = _label(self.t("review_patch_note", "Check the joins of the patch you inserted."), 10, AI_VOICE, 500, wrap=True)
        pn.addWidget(self.lbl_patch_note, 1)
        self.btn_patch_back = QPushButton(self.t("review_patch_back", "Back to Voice patch to pick another take"))
        self.btn_patch_back.setCursor(Qt.PointingHandCursor)
        self.btn_patch_back.setStyleSheet(_quiet_qss(pad_v=5, pad_h=12))
        self.btn_patch_back.clicked.connect(self._back_to_voice)
        pn.addWidget(self.btn_patch_back, 0, Qt.AlignVCenter)
        self._patch_note.hide()
        d.addWidget(self._patch_note)
        # The recording is one file for every cut: when it's missing, one banner for all of them
        self._audio_banner = QFrame()
        self._audio_banner.setObjectName("ReviewAudioBanner")
        self._audio_banner.setStyleSheet(f"QFrame#ReviewAudioBanner {{ background-color: #2a2113; border: 1px solid #5c4214;"
                                         f" border-radius: {S(12)}px; }}")
        bl = QVBoxLayout(self._audio_banner)
        bl.setContentsMargins(S(16), S(12), S(16), S(12))
        bl.setSpacing(S(8))
        brow = QHBoxLayout()
        self.lbl_banner = _label(self.t("review_audio_missing", "The recording isn't available, so the cuts can't be played."),
                                 10, AMBER, wrap=True)
        brow.addWidget(self.lbl_banner, 1)
        self.btn_get_audio = QPushButton(self.t("review_get_audios", "Get the recordings"))
        self.btn_get_audio.setToolTip(self.t("review_get_audio_tt", "Rebuilds the timeline's audio from DaVinci Resolve: "
                                             "this project must be open there."))
        self.btn_get_audio.setCursor(Qt.PointingHandCursor)
        self.btn_get_audio.setStyleSheet(_primary_qss())
        self.btn_get_audio.clicked.connect(self._get_audio)
        brow.addWidget(self.btn_get_audio, 0, Qt.AlignVCenter)
        bl.addLayout(brow)
        from gui.widgets.progress_bar import LiquidProgressBar
        self.audio_progress = LiquidProgressBar(self._audio_banner)
        self.audio_progress.setFixedHeight(S(6))
        self.audio_progress.hide()
        bl.addWidget(self.audio_progress)
        self._audio_banner.hide()
        d.addWidget(self._audio_banner)
        head = QHBoxLayout()
        head.setSpacing(S(12))
        self.lbl_heading = _label("", 19, TEXT, 600)
        self.lbl_sub = _label("", 10.5, MUTED, 500)
        head.addWidget(self.lbl_heading, 0, Qt.AlignBottom)
        head.addWidget(self.lbl_sub, 0, Qt.AlignBottom)
        head.addStretch(1)
        d.addLayout(head)

        quote = QFrame()
        quote.setObjectName("ReviewQuote")
        quote.setStyleSheet(f"QFrame#ReviewQuote {{ background-color: {CARD_BG_2}; border: 1px solid {LINE};"
                            f" border-radius: {S(14)}px; }}")
        ql = QVBoxLayout(quote)
        ql.setContentsMargins(S(20), S(16), S(20), S(16))
        self.lbl_quote = QLabel()
        self.lbl_quote.setWordWrap(True)
        self.lbl_quote.setTextFormat(Qt.RichText)
        self.lbl_quote.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_quote.setStyleSheet(f"background: transparent; border: none; font-size: {FS(13)}pt; color: {TEXT_2};")
        ql.addWidget(self.lbl_quote)
        d.addWidget(quote)

        players = QHBoxLayout()
        players.setSpacing(S(14))
        self.card_a = _PlayerCard("A", CYAN)
        self.card_b = _PlayerCard("B", VIOLET)
        self.card_b.wave.after_tint = VIOLET_2
        for i, c in enumerate((self.card_a, self.card_b)):
            c.activate.connect(lambda i=i: self._switch(i, keep_playing=True))
            c.play_clicked.connect(lambda i=i: self._on_card_play(i))
            c.seek.connect(lambda t, i=i: self._seek(t, side=i))
            players.addWidget(c, 1)
        d.addLayout(players)

        transport = QHBoxLayout()
        transport.setSpacing(S(10))
        self.btn_play = QPushButton()
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setToolTip(self.t("review_tt_play", "Play or pause the selected side (Space)"))
        self.btn_play.setStyleSheet(_primary_qss())
        self.btn_play.setIconSize(QSize(S(14), S(14)))
        self.btn_play.clicked.connect(self.toggle_play)
        transport.addWidget(self.btn_play)
        self.btn_restart = QPushButton(self.t("review_from_start", "From the start"))
        self.btn_restart.setCursor(Qt.PointingHandCursor)
        self.btn_restart.setToolTip(self.t("review_tt_from_start", "Back to the start of the snippet"))
        self.btn_restart.setStyleSheet(_quiet_qss())
        self.btn_restart.clicked.connect(lambda: self._seek(0.0))
        transport.addWidget(self.btn_restart)
        self.lbl_time = _label("0:00.0 / 0:00.0", 10.5, SECONDARY, 500)
        self.lbl_time.setMinimumWidth(S(110))
        transport.addSpacing(S(6))
        transport.addWidget(self.lbl_time)
        self.lbl_hint = _label(self.t("review_switch_hint", "Press A or B to switch: the position is kept"),
                               9.5, MUTED, wrap=True)
        self.lbl_hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_hint.setMinimumWidth(S(60))
        transport.addWidget(self.lbl_hint, 1)
        d.addLayout(transport)
        self.lbl_audio = _label("", 10, AMBER, wrap=True)
        self.lbl_audio.hide()
        d.addWidget(self.lbl_audio)

        facts = QFrame()
        facts.setObjectName("ReviewFacts")
        facts.setStyleSheet(f"QFrame#ReviewFacts {{ background-color: {CARD_BG}; border: 1px solid {LINE};"
                            f" border-radius: {S(14)}px; }}")
        fl = QVBoxLayout(facts)
        fl.setContentsMargins(S(20), S(14), S(20), S(16))
        fl.setSpacing(S(14))
        frow = QHBoxLayout()
        frow.setSpacing(S(28))
        self.fact1, self.fact2, self.fact3 = _Fact(), _Fact(), _Fact()
        for f in (self.fact1, self.fact2, self.fact3):
            frow.addWidget(f)
        frow.addStretch(1)
        fl.addLayout(frow)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {LINE}; border: none;")
        fl.addWidget(sep)
        nrow = QHBoxLayout()
        nrow.setSpacing(S(8))
        self.lbl_notes_caption = _label(self.t("review_fact_notes", "Notes"), 9, MUTED, 500)
        nrow.addWidget(self.lbl_notes_caption, 0, Qt.AlignTop)
        nrow.addSpacing(S(6))
        self._notes_host = QWidget()
        self._notes_host.setStyleSheet("background: transparent;")
        self._notes_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._notes_lay = _FlowLayout(self._notes_host, spacing=S(6))
        nrow.addWidget(self._notes_host, 1)
        fl.addLayout(nrow)
        d.addWidget(facts)
        d.addWidget(self._build_edit_card())

        actions = QHBoxLayout()
        actions.setSpacing(S(10))
        self.btn_prev = QPushButton()
        self.btn_next = QPushButton()
        for b, icon in ((self.btn_prev, "back"), (self.btn_next, "arrow")):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_quiet_qss())
            b.setIcon(QIcon(icon_pixmap(icon, theme.color(TEXT_2), S(16))))
            b.setIconSize(QSize(S(16), S(16)))
        self.btn_next.setLayoutDirection(Qt.RightToLeft)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        actions.addWidget(self.btn_prev)
        actions.addWidget(self.btn_next)
        actions.addStretch(1)
        d.addLayout(actions)
        d.addStretch(1)

        self._no_items = _label("", 10.5, SECONDARY, wrap=True)
        self._no_items.setAlignment(Qt.AlignCenter)
        self._no_items.setContentsMargins(S(8), S(24), S(8), S(8))
        self._update_play_ui()
        return page

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_left"):
            self._left.setFixedWidth(max(S(270), min(S(360), int(self.width() * 0.27))))

    # -- public --------------------------------------------------------------------------------

    def refresh(self):
        """Re-collect the review; keep the selected cut (and pair) when it still exists."""
        self.stop()
        try:
            data = collect_review(self.win)
        except Exception as exc:  # a broken story shouldn't take the screen down
            print(f"[review] collect failed: {exc}")
            data = None
        if not data:
            self._clips, self._joins, self._repeats = [], [], []
            self._patches, self._join_t = [], {}
            self._stack.setCurrentIndex(0)
            self._release_media()
            return
        self._clips, self._joins, self._repeats = data
        self._ok = load_ok_keys(self.win)
        self._patches = self._collect_patches()
        self._join_t = {j["n"]: self._edit_time(j["n"]) for j in self._joins} if self._patches else {}
        path = _audio_path(self.win)
        if self._reader is None or self._reader.path != path:
            self._reader = AudioReader(path, _ffmpeg(self.win)) if path else None
        self._audio_error = None if self._reader else self.t(
            "review_audio_missing", "The recording isn't available, so the cuts can't be played.")
        if self._reader and not self._players:
            self._audio_error = self.t("review_audio_unavailable", "Audio playback isn't available on this system.")
        self._update_tab_labels()
        self.btn_repeats.setText(self.t("review_tab_repeats", "Repeats · {n}", n=len(self._repeats)))
        if self._mode == "repeats" and not self._repeats and self._joins:
            self._mode = "cuts"
            self.btn_cuts.setChecked(True)
        self._stack.setCurrentIndex(1)
        self._rebuild_list()

    def _get_audio(self):
        ap = getattr(self.win, 'audio_preview', None)
        if ap is None or not hasattr(ap, '_on_fetch_missing_audio'):
            return
        self.btn_get_audio.setEnabled(False)
        self.lbl_banner.setText(self.t("review_getting_audio", "Getting the recording… this can take a minute."))
        self.audio_progress.show()
        self.audio_progress.set_value(-1)  # the rebuild doesn't report a percentage: a moving bar
        ap._on_fetch_missing_audio()

        def _wait():
            if getattr(ap, '_fetching_audio', False):
                QTimer.singleShot(400, _wait)
                return
            self.audio_progress.hide()
            self.btn_get_audio.setEnabled(True)
            self.lbl_banner.setText(self.t("review_audio_missing", "The recording isn't available, so the cuts can't be played."))
            self.refresh()
            if self._reader is None:  # still nothing: say what to try
                self.lbl_banner.setText(self.t("review_get_failed", "The recording couldn't be rebuilt. Open this project in "
                                               "DaVinci Resolve, or check that the original media files are still in place, "
                                               "then try again."))
        QTimer.singleShot(400, _wait)

    def stop(self):
        """Pause playback (the position is kept)."""
        for pl in self._players:
            pl.pause()
        self._playing = False
        self._tick.stop()
        self._update_play_ui()

    def toggle_play(self):
        pl = self._active_player()
        if pl is None:
            return
        if self._playing:
            self.stop()
            return
        dur = self._duration()
        if dur and self._pos >= dur - 0.05:
            self._pos = 0.0
        self._seek_player(pl, int(self._pos * 1000))
        ap = getattr(self.win, "audio_preview", None)
        if ap is not None and hasattr(ap, "pause_for_other_player"):
            ap.pause_for_other_player()        # never two audios at once
        pl.play()
        self._playing = True
        self._tick.start()
        self._update_play_ui()

    SKIP_S = 1.0   # ← / →: the snippets are a few seconds long, so a small step

    def skip(self, direction):
        """← / → (the window's skip shortcuts on this page): move the shared position back or on."""
        pl = self._active_player()
        if pl is None:
            return
        if self._playing and pl not in self._pending:
            self._pos = pl.position() / 1000.0
        self._seek(self._pos + (self.SKIP_S if direction > 0 else -self.SKIP_S))

    def select(self, index, mode=None):
        """Select item `index` (0-based) of the cuts or the repeats list."""
        if mode and mode != self._mode:
            self._set_mode(mode)
        items = self._items()
        if not items:
            return
        index = max(0, min(len(items) - 1, index))
        self._index[self._mode] = index
        for i, c in enumerate(self._cards):
            c.setChecked(i == index)
        if 0 <= index < len(self._cards):
            self._list_scroll.ensureWidgetVisible(self._cards[index], 0, S(12))
        self._show_item()

    # -- list ----------------------------------------------------------------------------------

    # Only the cuts worth a listen are listed (what the review flags), unless the user asks for all;
    # any other cut opens from the transcript or the waveform (open_cut)
    _BENIGN = ("goes back in the recording", "trim inside a moment (a stumble cut out)")

    def _flagged(self, j):
        return any(n not in self._BENIGN for n in (j.get("notes") or []))

    def _cut_list(self):
        if getattr(self, "_show_all", False):
            joins = self._joins
        else:
            flagged = [j for j in self._joins if self._flagged(j)]
            joins = flagged if flagged else self._joins
        if not self._patches:
            return joins
        # the voice patches' joins are always listed (always worth a check), in story order among the cuts
        rank = {"out": 0, "in": 2}
        items = [((self._join_t.get(j["n"], 0.0), 1), j) for j in joins] + \
                [((p["t"], rank[p["side"]]), p) for p in self._patches]
        return [it for _key, it in sorted(items, key=lambda x: x[0])]

    @staticmethod
    def _is_patch(item):
        return isinstance(item, dict) and item.get("kind") == "patch"

    def _collect_patches(self):
        """The joins of the voice patches the story plays (Voice patch's Insert patch): a "Patch in" (what
        comes before → the patch, at its span start a) and a "Patch out" (the patch → what follows, at b) for
        each patch still in the story, with their place in it and the words either side."""
        try:
            import story_time as st
            from voice_patch.edit import accepted_patches, in_story
            spans = st.spans(self.win)
            d = self.win._voice_patch_dir() if hasattr(self.win, "_voice_patch_dir") else None
            if not spans or not d or not os.path.isdir(d):
                return []
            pats = [p for p in accepted_patches(d) if in_story(p, spans)]
        except Exception as exc:
            print(f"[review] voice patches: {exc}")
            return []
        if not pats:
            return []
        import bisect
        sw = self._spoken_words()
        starts = [w[0] for w in sw]
        play = []                          # the story's words, in play order
        for _k, s, e in spans:
            for w in sw[bisect.bisect_left(starts, s - 1.0):]:
                if w[0] >= e:
                    break
                if s <= (w[0] + w[1]) / 2.0 < e:
                    play.append(w)
        out = []
        for p in pats:
            a, b = float(p["a"]), float(p["b"])
            ix = [i for i, w in enumerate(play) if a - 0.02 <= (w[0] + w[1]) / 2.0 <= b + 0.02]
            if ix:
                before = " ".join(w[2] for w in play[max(0, ix[0] - 10):ix[0]])
                after = " ".join(w[2] for w in play[ix[-1] + 1:ix[-1] + 11])
            else:
                before = " ".join([w[2] for w in sw if w[1] <= a + 0.02][-10:]) if sw else ""
                after = " ".join([w[2] for w in sw if w[0] >= b - 0.02][:10]) if sw else ""
            try:
                t_in = st.edit_time(self.win, a)
            except Exception:
                t_in = None
            if t_in is None:
                t_in = 0.0
            length = float(p["patch_s"]) if p.get("length") == "close" and p.get("patch_s") else b - a
            words = " ".join(str(p.get("words") or "").split())
            for side in ("in", "out"):
                out.append({"kind": "patch", "side": side, "end_id": str(p["end_id"]), "patch": p, "words": words,
                            "before": before, "after": after, "t": t_in if side == "in" else t_in + length})
        return out

    def open_cut(self, n):
        """Select cut n (1-based join number), showing all cuts if it isn't a flagged one."""
        if self._mode != "cuts":
            self._mode = "cuts"
            self.btn_cuts.setChecked(True)
        if not any(j.get("n") == n for j in self._cut_list()):
            self._show_all = True
            self.btn_all.blockSignals(True)
            self.btn_all.setChecked(True)
            self.btn_all.blockSignals(False)
            self._update_tab_labels()
            self._rebuild_list()
        idx = next((i for i, j in enumerate(self._cut_list()) if j.get("n") == n), None)
        if idx is not None:
            self.select(idx, "cuts")

    def _toggle_all(self, on):
        self._show_all = on
        self._index["cuts"] = 0
        self._update_tab_labels()
        self._rebuild_list()
        if self._cut_list():
            self.select(0, "cuts")

    def flagged_counts(self):
        """What Review asks to check, counted apart: {"cuts": the flagged cuts (what the Story status line
        calls "cuts to check"), "patch_joins": the voice patches' joins (Patch in / Patch out, always listed)}."""
        return {"cuts": sum(1 for j in (self._joins or []) if self._flagged(j)),
                "patch_joins": len(self._patches or [])}

    def check_summary(self, counts=None):
        """"1 cut · 2 patch joins to check" (each part only when there is some), "" with nothing to check."""
        c = counts or self.flagged_counts()
        parts = []
        if c["cuts"]:
            parts.append(self.t("review_check_cut_one", "1 cut") if c["cuts"] == 1 else
                         self.t("review_check_cuts", "{n} cuts", n=c["cuts"]))
        if c["patch_joins"]:
            parts.append(self.t("review_check_join_one", "1 patch join") if c["patch_joins"] == 1 else
                         self.t("review_check_joins", "{n} patch joins", n=c["patch_joins"]))
        return self.t("review_check_line", "{what} to check", what=" · ".join(parts)) if parts else ""

    def _update_tab_labels(self):
        c = self.flagged_counts()
        flagged = c["cuts"]
        # the tab counts the story's cuts; what's to check (flagged cuts, patch joins) is said apart below it
        self.btn_cuts.setText(self.t("review_tab_cuts", "Cuts · {n}", n=len(self._joins)))
        summary = self.check_summary(c)
        self.lbl_check.setText(summary)
        self.lbl_check.setVisible(bool(summary))
        self.btn_all.setText(self.t("review_show_all", "Show all {n} cuts", n=len(self._joins)))
        self.btn_all.setVisible(bool(flagged) and flagged < len(self._joins))

    def _items(self):
        return self._cut_list() if self._mode == "cuts" else self._repeats

    def _set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        (self.btn_cuts if mode == "cuts" else self.btn_repeats).setChecked(True)
        self._rebuild_list()

    def _note_text(self, note):
        n = note.lower()
        m = re.match(r"loudness (drops|rises) (\d+)", n)
        if m:
            key, default = (("review_note_quieter", "Loudness drops {db} dB") if m.group(1) == "drops"
                            else ("review_note_louder", "Loudness rises {db} dB"))
            return self.t(key, default, db=m.group(2))
        for prefix, key, default in (("ends mid-thought", "review_note_ends_mid", "Ends mid-thought"),
                                     ("starts mid-thought", "review_note_starts_mid", "Starts mid-thought"),
                                     ("goes back in the recording", "review_note_goes_back", "Goes back in the recording"),
                                     ("trim inside a moment", "review_note_trim", "Trim inside a moment"),
                                     ("speaker changes", "review_note_speaker", "Speaker changes")):
            if n.startswith(prefix):
                return self.t(key, default)
        return note

    def _badges(self, join, card=False):
        """The notes as (text, warn) badges. The list card leaves out "trim inside a moment": it is an
        editing choice, not something to check, and the selected cut's Notes already say it."""
        return [(self._note_text(n), not n.lower().startswith(_CHOICE_NOTES)) for n in join["notes"]
                if not (card and n.lower().startswith("trim inside a moment"))]

    def _edit_time(self, n):
        """Where clip n + 1 starts in the story (the cut after clip n), counted like the Story page and the
        dock (story_time: what the cut settings remove inside the clips doesn't count). Review's own clip
        lengths only when its clips aren't the story's (a review of an edit given as text)."""
        try:
            import story_time as st
            story = [(float(c["cut_start"]), float(c["cut_end"])) for c in
                     (getattr(self.win, "_story", None) or {}).get("clips") or []]
            mine = [(float(c["cut_start"]), float(c["cut_end"])) for c in self._clips]
            if story and story == mine:
                if n < len(mine):
                    e = st.edit_time(self.win, mine[n][0])
                    starts = st.clip_starts(self.win)
                    # edit_time finds the first clip holding that moment: with a clip ending exactly where
                    # clip n + 1 starts (or used twice), take clip n + 1's own start instead
                    if starts and n < len(starts) and (e is None or abs(e - starts[n]) > 0.5):
                        e = starts[n]
                    if e is not None:
                        return e
                else:
                    return st.story_length(self.win)
        except Exception as exc:
            print(f"[review] story time failed: {exc}")
        return sum(max(0.0, float(c["cut_end"]) - float(c["cut_start"])) for c in self._clips[:n])

    def _jump_text(self, jump):
        if not jump:
            return self.t("review_jump_none", "Continues straight on")
        return self.t("review_jump_seconds", "{value} s", value=f"{jump:+.0f}")

    def _rebuild_list(self):
        for c in self._cards:
            self._card_group.removeButton(c)
            c.setParent(None)
            c.deleteLater()
        self._cards = []
        self._no_items.setParent(None)
        items = self._items()
        if self._mode == "cuts":
            for j in items:
                if self._is_patch(j):
                    self._cards.append(self._patch_card(j))
                    continue
                title = self.t("review_cut_title", "Cut {n} · {time}", n=j["n"], time=_fmt_story(self._edit_time(j["n"])))
                if self._ok_key(j) in self._ok:
                    title += "  ✓"
                meta = "" if not j["jump"] else f"{j['jump']:+.0f}s"
                notes = self._badges(j)
                tip = f"{j['before_id']} → {j['after_id']}" + ("\n" + " · ".join(t for t, _ in notes) if notes else "")
                self._cards.append(_ListCard(title, meta, self._badges(j, card=True), j["before"], j["after"], tip))
        else:
            for a, b, share in items:
                title = self.t("review_repeat_title", "Clips {a} and {b}", a=a, b=b)
                meta = self.t("review_repeat_share", "{pct}% shared words", pct=int(share * 100))
                ca, cb = self._clips[a - 1], self._clips[b - 1]
                tip = f"{ca['start_id']} · {cb['start_id']}"
                self._cards.append(_ListCard(title, meta, [], ca["text"], cb["text"], tip))
        for i, c in enumerate(self._cards):
            c.clicked.connect(lambda _=False, i=i: self.select(i))
            c.stepped.connect(self._step_focus)
            self._card_group.addButton(c)
            self._list_lay.insertWidget(self._list_lay.count() - 1, c)
        self._detail_widget.setVisible(bool(items))
        if not items:
            self._patch_note.hide()
            self._no_items.setText(self.t("review_no_cuts", "The story has a single clip: there are no cuts to review.")
                                   if self._mode == "cuts" else
                                   self.t("review_no_repeats", "No clips repeat each other."))
            self._list_lay.insertWidget(0, self._no_items)
            self._snips = (None, None)
            self._release_media()
            return
        idx = self._index[self._mode]
        self.select(idx if 0 <= idx < len(items) else 0)

    def _step(self, d):
        items = self._items()
        if items:
            self.select(max(0, min(len(items) - 1, self._index[self._mode] + d)))

    def _step_focus(self, d):
        self._step(d)
        i = self._index[self._mode]
        if 0 <= i < len(self._cards):
            self._cards[i].setFocus(Qt.TabFocusReason)

    # -- detail --------------------------------------------------------------------------------

    def _quote_html(self, before, after):
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        mark = theme.color(VIOLET_2)
        return (f"<span style='color:{theme.color(SECONDARY)}'>{esc(before)}</span>"
                f"<span style='color:{mark}; font-weight:700'>&nbsp;&nbsp;┃&nbsp;&nbsp;</span>"
                f"<span style='color:{theme.color(TEXT)}'>{esc(after)}</span>")

    def _clear_notes(self):
        while self._notes_lay.count():
            w = self._notes_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    # -- voice patches: their two joins -----------------------------------------------------------

    @staticmethod
    def _short(text, n, tail=False):
        w = (text or "").split()
        if len(w) <= n:
            return " ".join(w)
        return ("…" + " ".join(w[-n:])) if tail else (" ".join(w[:n]) + "…")

    def _patch_title(self, item):
        if item["side"] == "in":
            return self.t("review_patch_in", "Patch in") + f" · “{self._short(item['words'], 4)}”"
        return self.t("review_patch_out", "Patch out") + f" · “{self._short(item['words'], 4, tail=True)}”"

    def _patch_card(self, item):
        title = self._patch_title(item)
        if self._ok_key(item) in self._ok:
            title += "  ✓"
        p = item["patch"]
        if item["side"] == "in":
            line1, line2 = self._short(item["before"], 10, tail=True), item["words"]
        else:
            line1, line2 = item["words"], self._short(item["after"], 10)
        tip = (item["words"] + "\n" + self.t("time_in_recording", "{time} in the recording",
                                             time=f"{_mmss(float(p['a']))} → {_mmss(float(p['b']))}"))
        return _ListCard(title, _fmt_story(item["t"]), [(self.t("review_patch_tag", "AI voice"), "ai")], line1, line2, tip)

    def _show_patch(self, item):
        p = item["patch"]
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ai = f"<span style='color:{theme.color(AI_VOICE)}'>{esc(item['words'])}</span>"
        bar = f"<span style='color:{theme.color(VIOLET_2)}; font-weight:700'>&nbsp;&nbsp;┃&nbsp;&nbsp;</span>"
        if item["side"] == "in":
            self.lbl_heading.setText(self.t("review_patch_in", "Patch in"))
            self.lbl_quote.setText(f"<span style='color:{theme.color(SECONDARY)}'>{esc(self._short(item['before'], 24, tail=True))}"
                                   f"</span>{bar}{ai}")
        else:
            self.lbl_heading.setText(self.t("review_patch_out", "Patch out"))
            self.lbl_quote.setText(f"{ai}{bar}<span style='color:{theme.color(TEXT)}'>{esc(self._short(item['after'], 24))}</span>")
        self.lbl_sub.setText(self.t("review_patch_tag", "AI voice"))
        self.card_a.set_texts(self.t("review_player_a", "As recorded"),
                              self.t("review_patch_a_desc", "The original words, as they were said"),
                              self.t("review_tt_listen_a", "Listen to A, as recorded (key A)"), "A")
        self.card_b.set_texts(self.t("review_player_b", "In the story"),
                              self.t("review_patch_b_desc", "The join with the AI voice, as the story plays it"),
                              self.t("review_tt_listen_b", "Listen to B, in the story (key B)"), "B")
        a, b = float(p["a"]), float(p["b"])
        secs = lambda x: self.t("review_patch_secs", "{s} s", s=f"{x:.1f}")
        self.fact1.set(self.t("review_patch_fact_len", "Patch"), secs(float(p.get("patch_s") or (b - a))))
        self.fact1.setToolTip("")
        self.fact2.set(self.t("review_fact_position", "In the story"), _fmt_story(item["t"]))
        self.fact2.setToolTip(self.t("time_in_recording", "{time} in the recording", time=_mmss(a if item["side"] == "in" else b)))
        self.fact3.set(self.t("review_patch_fact_orig", "Original words"), secs(b - a))
        self.fact3.setToolTip(f"{_mmss(a)} → {_mmss(b)}")
        self.lbl_notes_caption.setText(self.t("review_fact_tip", "Tip"))
        self._notes_lay.addWidget(_label(self.t("review_patch_tip", "Listen for a jump in loudness, tone or timing where the "
                                                "AI voice meets the recording."), 10, TEXT_2))
        self.lbl_hint.setText(self.t("review_switch_hint", "Press A or B to switch: the position is kept"))

    def _story_runs(self):
        """The story's kept ranges in recording seconds with the cut offset (as build_cut_snippets takes them),
        or None when the review isn't of the story."""
        try:
            import story_time as st
            story = [(float(c["cut_start"]), float(c["cut_end"])) for c in
                     (getattr(self.win, "_story", None) or {}).get("clips") or []]
            mine = [(float(c["cut_start"]), float(c["cut_end"])) for c in self._clips]
            if not story or story != mine:
                return None
            off = float((self.win.engine.load_preferences() or {}).get("offset", 0.133))
            return [(k, a + off, b + off) for k, a, b in st.spans(self.win) if b > a] or None
        except Exception:
            return None

    def _patch_snippets(self, item):
        p = item["patch"]
        path = patch_audio_path(p)
        sr = self._reader.sr
        try:
            key = (path, os.path.getmtime(path), sr)
        except OSError:
            key = (path, None, sr)
        x = self._patch_audio.get(key)
        if x is None:
            x = load_patch_audio(path, sr, _ffmpeg(self.win))
            self._patch_audio = {key: x}            # one patch at a time is plenty
        return build_patch_snippets(self._reader, p, item["side"], x, self._story_runs())

    def focus_patch(self, end_id, note=True):
        """Select the "Patch in" join of patch end_id (Voice patch's Insert patch lands here) and say why in a
        one-line note with the way back. False when the story doesn't play that patch."""
        if self._stack.currentIndex() != 1:
            return False
        if self._mode != "cuts":
            self._set_mode("cuts")
        idx = next((i for i, it in enumerate(self._cut_list())
                    if self._is_patch(it) and it["end_id"] == str(end_id) and it["side"] == "in"), None)
        if idx is None:
            self._patch_note.hide()
            return False
        self._patch_note.setVisible(bool(note))
        self.select(idx, "cuts")
        return True

    def _back_to_voice(self):
        self.stop()
        if hasattr(self.win, "show_voice"):
            self.win.show_voice()

    def _show_item(self):
        was_playing = self._playing
        self.stop()
        items = self._items()
        i = self._index[self._mode]
        if not items or not (0 <= i < len(items)):
            return
        self._update_edit_card()
        self._clear_notes()
        cut = self._mode == "cuts"
        total = len(items)
        self.btn_prev.setText(self.t("review_prev_cut", "Previous cut") if cut else self.t("review_prev_pair", "Previous pair"))
        self.btn_next.setText(self.t("review_next_cut", "Next cut") if cut else self.t("review_next_pair", "Next pair"))
        self.btn_prev.setToolTip(self.t("review_tt_prev", "Go to the previous one in the list (Up)"))
        self.btn_next.setToolTip(self.t("review_tt_next", "Go to the next one in the list (Down)"))
        self.btn_prev.setEnabled(i > 0)
        self.btn_next.setEnabled(i < total - 1)
        if cut and self._is_patch(items[i]):
            self._show_patch(items[i])
        elif cut:
            j = items[i]
            self.lbl_heading.setText(self.t("review_cut_heading", "Cut {n} of {total}", n=j["n"],
                                    total=sum(1 for it in items if not self._is_patch(it))))
            self.lbl_sub.setText(f"{j['before_id']}  →  {j['after_id']}")
            self.lbl_quote.setText(self._quote_html(j["before"], j["after"]))
            self.card_a.set_texts(self.t("review_player_a", "As recorded"),
                                  self.t("review_player_a_desc", "What was actually said next"),
                                  self.t("review_tt_listen_a", "Listen to A, as recorded (key A)"), "A")
            self.card_b.set_texts(self.t("review_player_b", "In the story"),
                                  self.t("review_player_b_desc", "The join, as the story plays it"),
                                  self.t("review_tt_listen_b", "Listen to B, in the story (key B)"), "B")
            self.fact1.set(self.t("review_fact_jump", "Jump in the recording"), self._jump_text(j["jump"]))
            self.fact2.set(self.t("review_fact_position", "In the story"), _fmt_story(self._edit_time(j["n"])))
            n = j["n"]   # the cut joins clip n and clip n + 1: where that is in the recording
            if 0 < n < len(self._clips):
                rec = f"{_mmss(float(self._clips[n - 1]['cut_end']))} → {_mmss(float(self._clips[n]['cut_start']))}"
                self.fact2.setToolTip(self.t("time_in_recording", "{time} in the recording", time=rec))
            else:
                self.fact2.setToolTip("")
            self.fact3.set(self.t("review_fact_clips", "Clips"), f"{j['n']} → {j['n'] + 1}")
            self.fact3.setToolTip("")
            self.lbl_notes_caption.setText(self.t("review_fact_notes", "Notes"))
            badges = self._badges(j)
            if badges:
                for text, warn in badges:
                    self._notes_lay.addWidget(_badge(text, warn))
            else:
                self._notes_lay.addWidget(_label(self.t("review_no_notes", "Nothing flagged"), 10, SECONDARY))
            self.lbl_hint.setText(self.t("review_switch_hint", "Press A or B to switch: the position is kept"))
        else:
            a, b, share = items[i]
            ca, cb = self._clips[a - 1], self._clips[b - 1]
            la = self.t("review_clip_label", "Clip {n}", n=a)
            lb = self.t("review_clip_label", "Clip {n}", n=b)
            self.lbl_heading.setText(self.t("review_repeat_title", "Clips {a} and {b}", a=a, b=b))
            self.lbl_sub.setText(self.t("review_repeat_share", "{pct}% shared words", pct=int(share * 100)))
            esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            short = lambda s, n=45: " ".join(s.split()[:n]) + ("…" if len(s.split()) > n else "")
            self.lbl_quote.setText(
                f"<span style='color:{theme.color(CYAN)}; font-weight:600'>{esc(la)}</span>"
                f"<span style='color:{theme.color(TEXT_2)}'>&nbsp;&nbsp;{esc(short(ca['text']))}</span><br><br>"
                f"<span style='color:{theme.color(VIOLET_2)}; font-weight:600'>{esc(lb)}</span>"
                f"<span style='color:{theme.color(TEXT_2)}'>&nbsp;&nbsp;{esc(short(cb['text']))}</span>")
            dur = lambda c: float(c["cut_end"]) - float(c["cut_start"])
            self.card_a.set_texts(la, self.t("review_clip_opening", "Its first seconds · {time} long",
                                             time=_mmss(dur(ca))), self.t("review_tt_listen_a_clip", "Listen to A (key A)"), "A")
            self.card_b.set_texts(lb, self.t("review_clip_opening", "Its first seconds · {time} long",
                                             time=_mmss(dur(cb))), self.t("review_tt_listen_b_clip", "Listen to B (key B)"), "B")
            self.fact1.set(self.t("review_fact_shared", "Shared words"), f"{int(share * 100)}%")
            self.fact2.set(la, f"{ca['start_id']} · {_mmss(dur(ca))}")
            self.fact3.set(lb, f"{cb['start_id']} · {_mmss(dur(cb))}")
            for fact, n, c in ((self.fact2, a, ca), (self.fact3, b, cb)):   # where each clip starts
                fact.setToolTip(self.t("time_in_story", "{time} in the story", time=_fmt_story(self._edit_time(n - 1))) + "\n"
                                + self.t("time_in_recording", "{time} in the recording", time=_mmss(float(c["cut_start"]))))
            self.lbl_notes_caption.setText(self.t("review_fact_tip", "Tip"))
            hint = _label(self.t("review_repeat_hint", "They say much the same thing. Listen to both and keep the"
                                 " stronger one."), 10, TEXT_2)
            self._notes_lay.addWidget(hint)
            self.lbl_hint.setText(self.t("review_switch_hint", "Press A or B to switch: the position is kept"))
        self._load_snippets()
        if was_playing:
            self.toggle_play()

    # -- audio ---------------------------------------------------------------------------------

    def _release_media(self):
        for pl in self._players:
            pl.stop()
            pl.setSource(QUrl())
        self._pending.clear()

    def _load_snippets(self):
        self._pos = 0.0
        self._snips = (None, None)
        err = self._audio_error
        if self._reader is not None and err is None:
            try:
                i = self._index[self._mode]
                item = self._cut_list()[i] if self._mode == "cuts" else None
                if self._is_patch(item):
                    self._snips = self._patch_snippets(item)
                elif self._mode == "cuts":
                    spans, off = None, 0.0
                    try:   # the edit as Play story / Send play it: kept ranges + the cut offset
                        import story_time as st
                        story = [(float(c["cut_start"]), float(c["cut_end"])) for c in
                                 (getattr(self.win, "_story", None) or {}).get("clips") or []]
                        mine = [(float(c["cut_start"]), float(c["cut_end"])) for c in self._clips]
                        if story and story == mine:   # the review is of the story (not an edit given as text)
                            spans = st.spans(self.win)
                            off = float((self.win.engine.load_preferences() or {}).get("offset", 0.133))
                    except Exception:
                        spans, off = None, 0.0
                    self._snips = build_cut_snippets(self._reader, self._clips, self._cut_list()[i]["n"],
                                                     spans=spans, offset=off)
                else:
                    a, b, _ = self._repeats[i]
                    self._snips = build_repeat_snippets(self._reader, self._clips, a, b)
            except Exception as exc:
                print(f"[review] snippet failed: {exc}")
                if isinstance(exc, ValueError) and "outside" in str(exc):
                    err = self.t("review_audio_outside", "This part lies past the end of the recording ({end}), so it"
                                 " can't be played.", end=_mmss(self._reader.duration or 0))
                else:
                    err = self.t("review_audio_failed", "This part of the recording couldn't be read, so it can't be played.")
                self._snips = (None, None)
        missing = self._reader is None and bool(err)
        self._audio_banner.setVisible(missing)          # the whole recording: one banner at the top
        self.lbl_audio.setText("" if missing else (err or ""))
        self.lbl_audio.setVisible(bool(err) and not missing)  # this cut only (past the end, unreadable)
        ok = all(s is not None and len(s["samples"]) for s in self._snips)
        if not ok and not err:
            err = self.t("review_audio_failed", "This part of the recording couldn't be read, so it can't be played.")
            self.lbl_audio.setText(err)
            self.lbl_audio.show()
        for w in (self.btn_play, self.btn_restart, self.card_a.btn, self.card_b.btn):
            w.setEnabled(ok)
        if not ok:
            self._release_media()
            for c in (self.card_a, self.card_b):
                c.wave.set_data(np.zeros(1, np.float32), 0.0, None)
            self._update_play_ui()
            return
        norm = max(max(float(_peaks(s["samples"]).max()) for s in self._snips), 1e-4)
        self._serial += 1
        for k, (s, c) in enumerate(zip(self._snips, (self.card_a, self.card_b))):
            s["peaks"] = _peaks(s["samples"]) / norm
            s["duration"] = len(s["samples"]) / float(s["sr"])
            s["path"] = os.path.join(self._tmp, f"{self._mode}-{self._index[self._mode]}-{'ab'[k]}-{self._serial}.wav")
            _write_wav(s["path"], s["samples"], s["sr"])
            c.wave.cut_label = self.t("review_marker_cut", "cut")
            c.wave.set_data(s["peaks"], s["duration"], s["join"])
            c.wave.set_pos(0.0)
        self._prune_tmp()
        if self._players:
            for pl, s in zip(self._players, self._snips):
                pl.stop()
                pl.setSource(QUrl.fromLocalFile(s["path"]))
            self._pending.clear()
        self._update_play_ui()

    def _prune_tmp(self):
        keep = {s["path"] for s in self._snips if s}
        try:
            for name in os.listdir(self._tmp):
                p = os.path.join(self._tmp, name)
                if p not in keep and not name.endswith(f"-{self._serial - 1}.wav"):
                    os.remove(p)
        except OSError:
            pass

    def _active_player(self):
        return self._players[self._side] if self._players and self._snips[0] is not None else None

    def _duration(self):
        s = self._snips[self._side] if self._snips[0] is not None else None
        return s.get("duration", 0.0) if s else 0.0

    def _seek_player(self, pl, ms):
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                  QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.EndOfMedia)
        if pl.mediaStatus() in loaded:
            pl.setPosition(ms)
            self._pending.pop(pl, None)
        else:
            self._pending[pl] = ms

    def _on_status(self, i, status):
        pl = self._players[i]
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and pl in self._pending:
            pl.setPosition(self._pending.pop(pl))
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and i == self._side and self._playing:
            self._pos = self._duration()
            self._playing = False
            self._tick.stop()
            self._sync_waves()
            self._update_play_ui()

    def _on_playback(self, i, state):
        if i == self._side and state != QMediaPlayer.PlaybackState.PlayingState and self._playing \
                and self._players[i].mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self._playing = False
            self._tick.stop()
            self._update_play_ui()

    def _on_tick(self):
        pl = self._active_player()
        if pl is None or not self._playing:
            return
        if pl in self._pending:
            return
        self._pos = min(self._duration(), pl.position() / 1000.0)
        self._sync_waves()

    def _sync_waves(self):
        for c in (self.card_a, self.card_b):
            c.wave.set_pos(min(self._pos, c.wave.duration))
        self.lbl_time.setText(f"{_mmss_tenths(self._pos)} / {_mmss_tenths(self._duration())}")

    def _switch(self, side, keep_playing=True):
        """Make A (0) or B (1) the side that plays, at the same position; keeps playing if it was."""
        if self._snips[0] is None:
            return
        if side == self._side:
            self._update_play_ui()
            return
        was = self._playing
        cur = self._active_player()
        if cur is not None and was and cur not in self._pending:
            self._pos = cur.position() / 1000.0
        for pl in self._players:
            pl.pause()
        self._side = side
        self._pos = min(self._pos, self._duration())
        nxt = self._active_player()
        if nxt is not None:
            self._seek_player(nxt, int(self._pos * 1000))
            if was and keep_playing:
                nxt.play()
            else:
                self._playing = False
                self._tick.stop()
        self._sync_waves()
        self._update_play_ui()

    def _on_card_play(self, side):
        if side != self._side:
            self._switch(side)
            if not self._playing:
                self.toggle_play()
        else:
            self.toggle_play()

    def _seek(self, t, side=None):
        if self._snips[0] is None:
            return
        if side is not None and side != self._side:
            self._switch(side)
        self._pos = max(0.0, min(t, self._duration()))
        pl = self._active_player()
        if pl is not None:
            self._seek_player(pl, int(self._pos * 1000))
        self._sync_waves()

    def _update_play_ui(self):
        if not hasattr(self, "btn_play"):
            return
        self.card_a.set_state(self._side == 0, self._playing and self._side == 0)
        self.card_b.set_state(self._side == 1, self._playing and self._side == 1)
        self.btn_play.setText(self.t("review_pause", "Pause") if self._playing else self.t("review_play", "Play"))
        self.btn_play.setIcon(_glyph("pause" if self._playing else "play", ON_GRADIENT, S(14)))
        self._sync_waves()

    # -- actions -------------------------------------------------------------------------------

    # -- changing the edit from here (the story's blocks; one undo step each, Cmd+Z) -----------

    def _build_edit_card(self):
        card = QFrame()
        card.setObjectName("ReviewEdit")
        card.setStyleSheet(f"QFrame#ReviewEdit {{ background-color: {CARD_BG}; border: 1px solid {LINE};"
                           f" border-radius: {S(14)}px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(S(20), S(14), S(20), S(16))
        lay.setSpacing(S(10))
        top = QHBoxLayout()
        self.lbl_change_title = _label(self.t("review_change_title", "Change this cut"), 11, TEXT, 600)
        top.addWidget(self.lbl_change_title)
        top.addStretch(1)
        self.btn_open_voice = QPushButton(self.t("review_patch_open_voice", "Open in Voice patch"))
        self.btn_open_voice.setCursor(Qt.PointingHandCursor)
        self.btn_open_voice.setToolTip(self.t("review_tt_patch_open_voice", "Pick another take of this patch, or take it out of the story"))
        self.btn_open_voice.setStyleSheet(_quiet_qss())
        self.btn_open_voice.clicked.connect(self._back_to_voice)
        self.btn_open_voice.hide()
        top.addWidget(self.btn_open_voice)
        self.btn_ok = QPushButton(self.t("review_sounds_good", "Sounds good"))
        self.btn_ok.setCheckable(True)
        self.btn_ok.setCursor(Qt.PointingHandCursor)
        self.btn_ok.setToolTip(self.t("review_tt_sounds_good", "Tick this cut off; the list shows which ones you've checked"))
        self.btn_ok.setStyleSheet(_quiet_qss() + f" QPushButton:checked {{ background-color: #0f2e24; color: #a7f3d0;"
                                  f" border-color: #1f5c47; }}")
        self.btn_ok.toggled.connect(self._toggle_ok)
        top.addWidget(self.btn_ok)
        lay.addLayout(top)
        self._edit_cut_box = QWidget()
        grid = QGridLayout(self._edit_cut_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(S(8))
        grid.setVerticalSpacing(S(8))
        self._edit_btns = {}
        rows = (("before", self.t("review_side_before", "Before the cut")),
                ("after", self.t("review_side_after", "After the cut")))
        acts = (("more", self.t("review_more_sentence", "+ Sentence")), ("less", self.t("review_less_sentence", "− Sentence")),
                ("wmore", self.t("review_more_word", "+ Word")), ("wless", self.t("review_less_word", "− Word")),
                ("drop", self.t("review_drop_clip", "Drop clip")))
        tips = {("before", "more"): self.t("review_tt_before_more", "Keep the next sentence too, so the clip before the cut ends later"),
                ("before", "less"): self.t("review_tt_before_less", "End the clip before the cut one sentence earlier"),
                ("before", "drop"): self.t("review_tt_before_drop", "Take the clip before the cut out of the story"),
                ("after", "more"): self.t("review_tt_after_more", "Start the clip after the cut one sentence earlier"),
                ("after", "less"): self.t("review_tt_after_less", "Start the clip after the cut one sentence later"),
                ("after", "drop"): self.t("review_tt_after_drop", "Take the clip after the cut out of the story"),
                ("before", "wmore"): self.t("review_tt_before_wmore", "Keep one more word at the end of the clip before the cut"),
                ("before", "wless"): self.t("review_tt_before_wless", "Drop the last word of the clip before the cut (a hesitation)"),
                ("after", "wmore"): self.t("review_tt_after_wmore", "Start the clip after the cut one word earlier"),
                ("after", "wless"): self.t("review_tt_after_wless", "Drop the first word of the clip after the cut (a hesitation)")}
        for r, (side, name) in enumerate(rows):
            grid.addWidget(_label(name, 9.5, SECONDARY, 500), r, 0)
            for c, (act, text) in enumerate(acts, start=1):
                b = QPushButton(text)
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet(_quiet_qss())
                b.setToolTip(tips[(side, act)])
                b.clicked.connect(lambda _=False, s_=side, a_=act: self._edit_cut(s_, a_))
                grid.addWidget(b, r, c)
                self._edit_btns[(side, act)] = b
        grid.setColumnStretch(6, 1)
        lay.addWidget(self._edit_cut_box)
        # The pause at the cut: three versions to hear (the B player plays the chosen one)
        self._pause_box = QWidget()
        pr0 = QHBoxLayout(self._pause_box)
        pr0.setContentsMargins(0, S(4), 0, 0)
        pr0.setSpacing(S(8))
        pr0.addWidget(_label(self.t("review_pause_cut", "Pause at the cut"), 9.5, SECONDARY, 500))
        self._pause_btns = {}
        for key, text, tip in (("tight", self.t("review_pause_tight", "Tighter"),
                                self.t("review_tt_pause_tight", "Almost no pause: the next words follow straight on")),
                               ("auto", self.t("review_pause_auto", "As now"),
                                self.t("review_tt_pause_auto", "StoryCraft's usual breath at a cut")),
                               ("airy", self.t("review_pause_airy", "Airier"),
                                self.t("review_tt_pause_airy", "A longer pause, room to breathe"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(_quiet_qss() + f" QPushButton:checked {{ background-color: {HOVER}; color: {TEXT};"
                            f" border-color: {VIOLET}; }}")
            b.clicked.connect(lambda _=False, k=key: self._set_pause(k))
            pr0.addWidget(b)
            self._pause_btns[key] = b
        self.lbl_pause = _label("", 9.5, MUTED, 500)
        pr0.addWidget(self.lbl_pause)
        pr0.addStretch(1)
        lay.addWidget(self._pause_box)
        self._edit_pair_box = QWidget()
        pr = QHBoxLayout(self._edit_pair_box)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(S(8))
        self.btn_keep_a = QPushButton(self.t("review_keep_a", "Keep A, drop B"))
        self.btn_keep_b = QPushButton(self.t("review_keep_b", "Keep B, drop A"))
        for b, keep in ((self.btn_keep_a, 0), (self.btn_keep_b, 1)):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_quiet_qss())
            b.clicked.connect(lambda _=False, k=keep: self._keep_one(k))
            pr.addWidget(b)
        pr.addStretch(1)
        lay.addWidget(self._edit_pair_box)
        self.lbl_edit_note = _label(self.t("review_edit_note", "Changes go to the story. {shortcut} undoes them.", shortcut=_UNDO_KEY),
                                    9, MUTED, wrap=True)
        lay.addWidget(self.lbl_edit_note)
        return card

    def _board_blocks(self):
        """The story board's Edit blocks as lists, or None when they don't match the review's clips."""
        board = getattr(self.win, 'story_board', None)
        if board is None or not board.sentences:
            return None
        blocks = [list(b) for b in board.edit_blocks()]
        return blocks if len(blocks) == len(self._clips) else None

    def _apply_blocks(self, blocks, status):
        import algorithms
        board = self.win.story_board
        sentences = board.sentences
        used = {i for f, l in blocks for i in range(f, l + 1)}
        # Not used: what no block covers, kept in its moments (as the board shows them)
        spans = sorted(algorithms.moment_ranges(sentences).values()) or [(0, len(sentences) - 1)]
        unused = []
        for f, l in spans:
            run = None
            for i in range(f, l + 1):
                if i in used:
                    if run:
                        unused.append(tuple(run))
                    run = None
                else:
                    run = [i, i] if run is None else [run[0], i]
            if run:
                unused.append(tuple(run))
        board.load(sentences, [tuple(b) for b in blocks], unused)
        board._sync_story()  # updates the story, saves the board, records the undo step
        self.refresh()
        if hasattr(self.win, '_show_temporary_status'):
            self.win._show_temporary_status(status)

    # ---- ± Word: a clip edge moved by one word of the recording (hesitations at a cut) ----------------
    def _spoken_words(self):
        words = getattr(getattr(self.win, 'text_canvas', None), 'words_data', None) or []
        return sorted((float(w["start"]), float(w["end"]), str(w.get("text", "")).strip())
                      for w in words if w.get("type") == "word" and w.get("start") is not None
                      and w.get("end") is not None and str(w.get("text", "")).strip())

    def _word_edges(self, k):
        """For the cut between blocks k-1 and k: {(side, act): (new_time, word)} of the possible ± Word moves."""
        board = getattr(self.win, 'story_board', None)
        blocks = self._board_blocks()
        if board is None or blocks is None or not (0 < k < len(blocks)) or not board.sentences:
            return {}
        sent = board.sentences
        sw = self._spoken_words()
        if not sw:
            return {}
        spans = [(float(c["cut_start"]), float(c["cut_end"])) for c in (self._clips or [])]
        taken = lambda t: any(a - 0.01 <= t <= b + 0.01 for j, (a, b) in enumerate(spans) if j not in (k - 1, k))
        out = {}
        (f0, l0), (f1, l1) = blocks[k - 1], blocks[k]
        air0, air1 = board.air_for((f0, l0)), board.air_for((f1, l1))
        start0 = float(air0.get("start_t", sent[f0].get("src_start", 0)))
        end0 = float(air0.get("end_t", sent[l0].get("src_end", 0)))
        start1 = float(air1.get("start_t", sent[f1].get("src_start", 0)))
        end1 = float(air1.get("end_t", sent[l1].get("src_end", 0)))
        # before the cut: its last word
        inside0 = [j for j, (a, b, _t) in enumerate(sw) if a >= start0 - 0.02 and b <= end0 + 0.02]
        if inside0:
            j = inside0[-1]
            if len(inside0) > 1:
                out[("before", "wless")] = (sw[j - 1][1], sw[j][2])
            if j + 1 < len(sw) and not taken((sw[j + 1][0] + sw[j + 1][1]) / 2):
                out[("before", "wmore")] = (sw[j + 1][1], sw[j + 1][2])
        inside1 = [j for j, (a, b, _t) in enumerate(sw) if a >= start1 - 0.02 and b <= end1 + 0.02]
        if inside1:
            j = inside1[0]
            if len(inside1) > 1:
                out[("after", "wless")] = (sw[j + 1][0], sw[j][2])
            if j - 1 >= 0 and not taken((sw[j - 1][0] + sw[j - 1][1]) / 2):
                out[("after", "wmore")] = (sw[j - 1][0], sw[j - 1][2])
        return out

    def _edit_word(self, side, act):
        board = getattr(self.win, 'story_board', None)
        blocks = self._board_blocks()
        cuts = self._cut_list()
        i = self._index.get("cuts", 0)
        if board is None or blocks is None or not (0 <= i < len(cuts)) or "n" not in cuts[i]:
            return
        k = cuts[i]["n"]
        move = self._word_edges(k).get((side, act))
        if move is None:
            return
        t, word = move
        blk = tuple(blocks[k - 1] if side == "before" else blocks[k])
        key = f"{blk[0]}-{blk[1]}"
        sent = board.sentences
        natural = float(sent[blk[1]]["src_end"]) if side == "before" else float(sent[blk[0]]["src_start"])
        air = dict(getattr(board, "edge_air", None) or {})
        cur = dict(air.get(key) or {})
        field = "end_t" if side == "before" else "start_t"
        if abs(t - natural) < 0.005:
            cur.pop(field, None)          # back to the sentence's own edge
        else:
            cur[field] = round(t, 3)
        if cur:
            air[key] = cur
        else:
            air.pop(key, None)
        board.edge_air = air
        board._sync_story()               # one undo step (the board records its edge settings)
        self.refresh()
        msg = {"wless": self.t("review_done_wless", "“{w}” left out: listen to B again"),
               "wmore": self.t("review_done_wmore", "“{w}” added: listen to B again")}[act]
        self.win._show_temporary_status(msg.format(w=word))

    def _update_word_tips(self, join):
        moves = self._word_edges(join["n"]) if join else {}
        names = {("before", "wless"): self.t("review_tt_w_drop_end", "Drop “{w}” at the end of the clip before the cut"),
                 ("before", "wmore"): self.t("review_tt_w_add_end", "Add “{w}” at the end of the clip before the cut"),
                 ("after", "wless"): self.t("review_tt_w_drop_start", "Drop “{w}” at the start of the clip after the cut"),
                 ("after", "wmore"): self.t("review_tt_w_add_start", "Add “{w}” at the start of the clip after the cut")}
        for key, tpl in names.items():
            b = self._edit_btns.get(key)
            if b is None:
                continue
            m = moves.get(key)
            b.setEnabled(m is not None and b.isEnabled())
            if m is not None:
                b.setToolTip(tpl.format(w=m[1]))

    def _edit_cut(self, side, act):
        if act in ("wmore", "wless"):
            return self._edit_word(side, act)
        blocks = self._board_blocks()
        i = self._index.get("cuts", 0)
        cuts = self._cut_list()
        if blocks is None or not (0 <= i < len(cuts)) or "n" not in cuts[i]:
            return
        k = cuts[i]["n"]                  # the cut between clips k-1 and k (0-based blocks)
        out, inc = blocks[k - 1], blocks[k]
        n = len(self.win.story_board.sentences)
        others = lambda keep: {j for bi, (f, l) in enumerate(blocks) if bi not in keep for j in range(f, l + 1)}
        if act == "drop":
            del blocks[k - 1 if side == "before" else k]
            self._apply_blocks(blocks, self.t("review_done_drop", "Clip taken out of the story"))
            return
        if side == "before":
            if act == "more" and out[1] + 1 < n and out[1] + 1 not in others({k - 1}):
                out[1] += 1
            elif act == "less" and out[1] > out[0]:
                out[1] -= 1
            else:
                return
        else:
            if act == "more" and inc[0] - 1 >= 0 and inc[0] - 1 not in others({k}):
                inc[0] -= 1
            elif act == "less" and inc[0] < inc[1]:
                inc[0] += 1
            else:
                return
        self._apply_blocks(blocks, self.t("review_done_change", "Cut changed: listen again"))

    _PAUSES = {"tight": (0.04, 0.06), "airy": (0.30, 0.45)}   # (lead of the clip after, tail of the clip before)

    def _set_pause(self, key):
        blocks = self._board_blocks()
        cuts = self._cut_list()
        i = self._index.get("cuts", 0)
        if blocks is None or not (0 <= i < len(cuts)) or "n" not in cuts[i]:
            return
        k = cuts[i]["n"]
        board = self.win.story_board
        out, inc = tuple(blocks[k - 1]), tuple(blocks[k])
        lead, tail = ("auto", "auto") if key == "auto" else self._PAUSES[key]
        # two edges, one undo step: the first is set quietly, set_air on the second syncs and records it
        air = dict(getattr(board, "edge_air", None) or {})
        okey = f"{out[0]}-{out[1]}"
        cur = dict(air.get(okey) or {})
        if tail == "auto":
            cur.pop("tail", None)
        else:
            cur["tail"] = tail
        if cur:
            air[okey] = cur
        else:
            air.pop(okey, None)
        board.edge_air = air
        board.set_air(inc, lead=lead)
        self.refresh()
        self.win._show_temporary_status(self.t("review_done_pause", "Pause changed: listen to B again"))

    def _update_pause_row(self, join):
        board = getattr(self.win, 'story_board', None)
        blocks = self._board_blocks()
        if join is None or blocks is None or board is None:
            self._pause_box.setVisible(False)
            return
        self._pause_box.setVisible(True)
        k = join["n"]
        out, inc = tuple(blocks[k - 1]), tuple(blocks[k])
        tail = board.air_for(out).get("tail")
        lead = board.air_for(inc).get("lead")
        cur = "auto"
        for key, (l_, t_) in self._PAUSES.items():
            if tail == t_ and lead == l_:
                cur = key
        for key, b in self._pause_btns.items():
            b.setChecked(key == cur)
        try:
            a, b2 = self._clips[k - 1], self._clips[k]
            sent = board.sentences
            e_out = board.air_for(out).get("end_t", sent[a["last"]].get("src_end", a["cut_end"]))
            s_in = board.air_for(inc).get("start_t", sent[b2["first"]].get("src_start", b2["cut_start"]))
            gap = (float(a["cut_end"]) - float(e_out)) + (float(s_in) - float(b2["cut_start"]))
            self.lbl_pause.setText(self.t("review_pause_now", "{s:.2f} s of pause now", s=max(0.0, gap)))
        except Exception:
            self.lbl_pause.setText("")

    def _keep_one(self, keep):
        blocks = self._board_blocks()
        i = self._index.get("repeats", 0)
        if blocks is None or not (0 <= i < len(self._repeats)):
            return
        a, b, _ = self._repeats[i]          # clip numbers, 1-based
        del blocks[(b if keep == 0 else a) - 1]
        self._apply_blocks(blocks, self.t("review_done_drop", "Clip taken out of the story"))

    # "Sounds good" ticks live with the story (see load_ok_keys): keyed by the join's two sentence ids
    def _ok_key(self, j):
        if self._is_patch(j):
            return f"patch:{j['end_id']}:{j['side']}"
        return ok_key(j.get("before_id"), j.get("after_id"))

    def ok_joins(self):
        """The ticked joins, as "S002>S009" keys (the outgoing clip's last sentence > the incoming's first), and
        a voice patch's joins as "patch:<id>:in" / "patch:<id>:out"."""
        return set(self._ok)

    def _toggle_ok(self, on):
        i = self._index.get("cuts", 0)
        cuts = self._cut_list()
        if self._mode != "cuts" or not (0 <= i < len(cuts)):
            return
        key = self._ok_key(cuts[i])
        if (key in self._ok) == bool(on):
            return
        if on:
            self._ok.add(key)
        else:
            self._ok.discard(key)
        save_ok_keys(self.win, self._ok)
        if getattr(self, "_syncing_ok", False):
            return
        self._rebuild_list()

    def _update_edit_card(self):
        cut = self._mode == "cuts"
        self._edit_cut_box.setVisible(cut)
        self._pause_box.setVisible(cut)
        self._edit_pair_box.setVisible(not cut)
        self.btn_ok.setVisible(cut)
        editable = self._board_blocks() is not None
        for b in list(self._edit_btns.values()) + [self.btn_keep_a, self.btn_keep_b]:
            b.setEnabled(editable)
        if not editable:
            self.lbl_edit_note.setText(self.t("review_edit_unavailable", "This review doesn't match the story, so it "
                                              "can't be changed from here. Open the Story page to edit it."))
        else:
            self.lbl_edit_note.setText(self.t("review_edit_note", "Changes go to the story. {shortcut} undoes them.", shortcut=_UNDO_KEY))
        self.lbl_change_title.setText(self.t("review_change_title", "Change this cut"))
        self.btn_open_voice.hide()
        if cut:
            i = self._index.get("cuts", 0)
            self._syncing_ok = True
            cuts = self._cut_list()
            item = cuts[i] if 0 <= i < len(cuts) else None
            self.btn_ok.setChecked(item is not None and self._ok_key(item) in self._ok)
            if self._is_patch(item):
                # a patch's join changes on Voice patch (another take, or out of the story), not here
                self._edit_cut_box.setVisible(False)
                self._update_pause_row(None)
                self.lbl_change_title.setText(self.t("review_patch_join_title", "This patch join"))
                self.btn_open_voice.show()
                self.lbl_edit_note.setText(self.t("review_patch_edit_note", "To change it, pick another take on Voice patch."))
            else:
                self._update_pause_row(item)
                self._update_word_tips(item)
            self._syncing_ok = False

    def _use_whole_interview(self):
        board = getattr(self.win, 'story_board', None)
        if board is None:
            return
        board.fill_from_transcript()   # one undo step (story board), Cmd+Z takes it back
        self.refresh()

    def _import_ai_edit(self):
        if hasattr(self.win, '_on_import_ai_story'):
            self.win._on_import_ai_story()
            self.refresh()

    def _copy_for_ai(self):
        text = self.win._cut_review() if hasattr(self.win, "_cut_review") else None
        status = getattr(self.win, "_show_temporary_status", None)
        if not text:
            if status:
                status(self.t("review_build_story_first", "Build a story first"))
            return
        QApplication.clipboard().setText(text)
        if status:
            status(self.t("msg_cut_review_copied", "Cut review copied ({cuts} cuts).", cuts=text.count("\nCut ")))

    def _go_story(self):
        """The empty state's "Go to the story": the Story page (page 5)."""
        if hasattr(self.win, "show_story"):
            self.win.show_story()

    _go_compose = _go_story   # old name

    # -- events --------------------------------------------------------------------------------

    def keyPressEvent(self, e):
        # A / B / Space reach here when the focused child (a button, the list) doesn't use them; the
        # A / B shortcuts cover the rest when the window is active
        if self._stack.currentIndex() == 1 and not e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier):
            if e.key() == Qt.Key_Space:
                self.toggle_play()
                return
            if e.key() in (Qt.Key_A, Qt.Key_B):
                self._switch(0 if e.key() == Qt.Key_A else 1)
                return
        super().keyPressEvent(e)

    def hideEvent(self, e):
        self.stop()
        self._patch_note.hide()   # the Insert patch note belongs to that one visit
        super().hideEvent(e)
