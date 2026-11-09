#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: edit_preview.py
ROLE: Engine
DESCRIPTION:
Renders a small preview of the edit (kept source ranges, in play order) in the background,
so Play edit runs smoothly inside BadWords without touching Resolve. Every range is rendered
once and cached; changing a cut only re-renders the ranges it touches, and the pieces are
joined without re-encoding.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".mxf")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd, procs=None):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    if procs is not None:
        procs.append(p)
    _, err = p.communicate()
    if procs is not None and p in procs:
        procs.remove(p)
    return p.returncode, err.decode("utf-8", "replace")


class EditPreviewRender:
    """
    Usage:
        job = EditPreviewRender(ffmpeg, source_path, [(start_s, end_s), ...], cache_root)
        job.start()                      # background thread
        job.progress, job.done, job.error, job.output, job.timeline
    timeline: [(preview_start_s, source_start_s, source_end_s), ...] in play order, measured
    from the rendered pieces so the mapping doesn't drift.
    """
    HEIGHT = 360
    WORKERS = 3

    def __init__(self, ffmpeg, source, segments, cache_root):
        self.ffmpeg = ffmpeg or "ffmpeg"
        self.source = source
        self.segments = [(round(float(s), 3), round(float(e), 3)) for s, e in segments if e - s > 0.04]
        self.has_video = os.path.splitext(source)[1].lower() in VIDEO_EXTS
        self.ext = ".mov" if self.has_video else ".wav"
        st = os.stat(source)
        src_key = hashlib.sha1(f"{os.path.abspath(source)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
        self.cache_dir = os.path.join(cache_root, "edit_preview", src_key)
        edit_key = hashlib.sha1(json.dumps(self.segments).encode()).hexdigest()[:16]
        self.output = os.path.join(self.cache_dir, f"edit_{edit_key}{self.ext}")
        self.progress = 0.0
        self.done = False
        self.error = None
        self.timeline = []
        self._cancelled = False
        self._procs = []

    # ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def cancel(self):
        self._cancelled = True
        for p in list(self._procs):
            try:
                p.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _piece_path(self, s, e):
        return os.path.join(self.cache_dir, f"seg_{s:.3f}_{e:.3f}{self.ext}")

    def _encoder_args(self):
        if sys.platform == "darwin":
            return ["-c:v", "h264_videotoolbox", "-b:v", "1500k", "-realtime", "1"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]

    def _render_piece(self, s, e):
        out = self._piece_path(s, e)
        if os.path.exists(out):
            return True
        tmp = out + ".part" + self.ext
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{s:.3f}", "-i", self.source, "-t", f"{e - s:.3f}"]
        if self.has_video:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:{self.HEIGHT}",
                    "-pix_fmt", "yuv420p"] + self._encoder_args()
        else:
            cmd += ["-vn"]
        cmd += ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", tmp]
        code, err = _run(cmd, self._procs)
        if code != 0 and self.has_video and sys.platform == "darwin" and not self._cancelled:
            # Hardware encoder unavailable: software fallback
            cmd = [c for c in cmd]
            i = cmd.index("h264_videotoolbox")
            cmd[i - 1:i + 5] = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
            code, err = _run(cmd, self._procs)
        if code != 0:
            if not self._cancelled:
                self.error = err.strip().splitlines()[-1] if err.strip() else f"ffmpeg exit {code}"
            return False
        os.replace(tmp, out)
        return True

    def _durations(self):
        """Real length of each rendered piece (frames round the video), cached next to the pieces."""
        meta_path = os.path.join(self.cache_dir, "durations.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        ffprobe = os.path.join(os.path.dirname(self.ffmpeg), "ffprobe" + (".exe" if sys.platform == "win32" else ""))
        if not os.path.isfile(ffprobe):
            ffprobe = "ffprobe"
        out = []
        for s, e in self.segments:
            name = os.path.basename(self._piece_path(s, e))
            d = cache.get(name)
            if d is None:
                try:
                    p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                        "-of", "default=nw=1:nk=1", self._piece_path(s, e)],
                                       capture_output=True, text=True, creationflags=_NO_WINDOW)
                    d = float(p.stdout.strip())
                except Exception:
                    d = e - s
                cache[name] = d
            out.append(d)
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except Exception:
            pass
        return out

    def _work(self):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            total = sum(e - s for s, e in self.segments) or 1.0
            if not self.segments:
                self.error = "empty edit"
                return
            if not os.path.exists(self.output):
                todo = [(s, e) for s, e in self.segments if not os.path.exists(self._piece_path(s, e))]
                done_s = total - sum(e - s for s, e in todo)
                self.progress = done_s / total
                lock = threading.Lock()

                def one(seg):
                    nonlocal done_s
                    if self._cancelled or self.error:
                        return
                    if self._render_piece(*seg):
                        with lock:
                            done_s += seg[1] - seg[0]
                            self.progress = min(0.99, done_s / total)

                with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
                    list(pool.map(one, list(dict.fromkeys(todo))))
                if self._cancelled or self.error:
                    return
                list_path = self.output + ".txt"
                with open(list_path, "w", encoding="utf-8") as f:
                    for s, e in self.segments:
                        f.write("file '" + self._piece_path(s, e).replace("'", "'\\''") + "'\n")
                tmp = self.output + ".part" + self.ext
                code, err = _run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                                  "-safe", "0", "-i", list_path, "-c", "copy", tmp], self._procs)
                if code != 0:
                    if not self._cancelled:
                        self.error = err.strip().splitlines()[-1] if err.strip() else f"ffmpeg exit {code}"
                    return
                os.replace(tmp, self.output)
            pos = 0.0
            for (s, e), d in zip(self.segments, self._durations()):
                self.timeline.append((pos, s, e))
                pos += d
            self.progress = 1.0
            self.done = True
        except Exception as ex:
            self.error = str(ex)


# ----------------------------------------------------------------------
# The story's own clock
# ----------------------------------------------------------------------
class StoryMap:
    """
    One clock for the story as it plays (and as Send to Resolve builds it): the kept source ranges in play
    order, laid end to end, with the voice patches in it at their real length.

        m = StoryMap([(start_s, end_s), ...], patches=[(a, b, new_len, tag), ...])
        m.length                       seconds the story lasts
        m.runs                         [(story_start, story_end, src_start, src_end, tag)], play order;
                                       tag None = the recording, else the patch's tag (it plays instead of
                                       [src_start, src_end]: a "close" patch at its own length new_len, a
                                       "keep" patch at the span's length)
        m.to_story(t, hint) -> (T, i)  story time of source time t (None outside the story); i = run index,
                                       pass it back as hint so a moment shared by two runs stays in the one
                                       playing
        m.to_source(T) -> (t, i)       source time at story time T (inside a patch: where the playhead is
                                       in its span, as the player shows it)
        m.snap(t)                      the nearest moment of the story to source time t (source time)
        m.piece_bounds()               story times where the story jumps to another part of the recording
    Pure Python (bisect), so it stays quick for long interviews (thousands of runs).
    """

    def __init__(self, segments, patches=()):
        import bisect
        self._bisect = bisect
        segs = [(float(s), float(e)) for s, e in (segments or []) if float(e) - float(s) > 1e-3]
        todo = sorted(((float(a), float(b), float(n), tag) for a, b, n, tag in (patches or []) if b > a),
                      key=lambda p: p[0])
        placed = set()
        runs, pos = [], 0.0
        for s, e in segs:
            cur = s
            for k, (a, b, n, tag) in enumerate(todo):
                if k in placed or not (max(a, s) < min(b, e) - 1e-3):
                    continue
                ps, pe = max(a, s, cur), min(b, e)
                if pe <= ps:
                    continue
                placed.add(k)
                if ps > cur:
                    runs.append((pos, pos + ps - cur, cur, ps, None))
                    pos += ps - cur
                ln = max(0.0, n * (pe - ps) / (b - a))
                runs.append((pos, pos + ln, ps, pe, tag))
                pos += ln
                cur = pe
            if e > cur + 1e-4:
                runs.append((pos, pos + e - cur, cur, e, None))
                pos += e - cur
        self.runs = runs
        self.length = pos
        self.segments = segs
        self._starts = [r[0] for r in runs]
        by_src = sorted(range(len(runs)), key=lambda i: runs[i][2])
        self._src_order = by_src
        self._src_starts = [runs[i][2] for i in by_src]

    def __bool__(self):
        return bool(self.runs)

    @staticmethod
    def _within(r, t, lead=0.03, tail=0.0):
        return r[2] - lead <= t < r[3] + tail

    def _story_of(self, r, t):
        e0, e1, s, e, tag = r
        t = min(max(t, s), e)
        if tag is None or e <= s:
            return e0 + (t - s)
        return e0 + (t - s) / (e - s) * (e1 - e0)

    def to_story(self, t, hint=None):
        if t is None or not self.runs:
            return None, None
        t = float(t)
        runs = self.runs
        if hint is not None and 0 <= hint < len(runs):
            # the run playing, or the one after it in play order (a frame past a boundary)
            if self._within(runs[hint], t, 0.03, 0.02):
                return self._story_of(runs[hint], t), hint
            if hint + 1 < len(runs) and self._within(runs[hint + 1], t, 0.03, 0.0):
                return self._story_of(runs[hint + 1], t), hint + 1
        j = self._bisect.bisect_right(self._src_starts, t + 0.03) - 1
        for lead in (0.0, 0.03):                    # a run holding t itself first, then frame rounding
            for jj in (j, j - 1):
                if 0 <= jj < len(self._src_order):
                    i = self._src_order[jj]
                    if self._within(runs[i], t, lead, 0.0):
                        return self._story_of(runs[i], t), i
        return None, None

    def to_source(self, T):
        if not self.runs:
            return None, None
        T = max(0.0, min(float(T), self.length))
        i = max(0, self._bisect.bisect_right(self._starts, T) - 1)
        while i + 1 < len(self.runs) and self.runs[i][1] <= T and self.runs[i + 1][0] <= T:
            i += 1                                  # skip empty runs
        e0, e1, s, e, tag = self.runs[i]
        if tag is None or e1 <= e0:
            return min(e, s + (T - e0)), i
        return s + (T - e0) / (e1 - e0) * (e - s), i

    def snap(self, t):
        """The story moment nearest to source time t (t itself when it is in the story)."""
        if not self.runs:
            return t
        best = None
        for e0, e1, s, e, tag in self.runs:
            if s - 0.03 <= t < e:
                return t
            d, p = (s - t, s) if t < s else (t - e, max(s, e - 0.05))
            if best is None or d < best[0] - 1e-6:
                best = (d, p)
        return best[1]

    def piece_bounds(self):
        """Story times where a new piece of the recording starts (a jump in the source)."""
        out = []
        prev = None
        for e0, e1, s, e, tag in self.runs:
            if prev is not None and abs(s - prev) > 0.02:
                out.append(e0)
            prev = e
        return out
