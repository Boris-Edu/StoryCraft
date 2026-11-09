#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: audio.py
ROLE: Audio I/O, pitch tracking, pitch shifting (TD-PSOLA), EQ matching and splicing for voice patches
(numpy, scipy and ffmpeg only)
"""
import os
import shutil
import subprocess
import tempfile

import numpy as np

SR = 48000          # working rate: what StoryCraft extracts from Resolve
F0_SR = 16000       # pitch tracking rate
HOP_S = 0.01


def ffmpeg_bin():
    for cand in (shutil.which("ffmpeg"),
                 os.path.expanduser("~/Library/Application Support/StoryCraft/bin/ffmpeg"),
                 "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if cand and os.path.isfile(cand):
            return cand
    raise RuntimeError("ffmpeg not found")


def load(path_or_bytes, sr=SR):
    """Any audio (path or bytes) -> mono float32 at sr."""
    src, tmp = path_or_bytes, None
    if isinstance(path_or_bytes, (bytes, bytearray)):
        tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        tmp.write(path_or_bytes)
        tmp.close()
        src = tmp.name
    try:
        out = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", src, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
    finally:
        if tmp:
            os.unlink(tmp.name)
    return np.frombuffer(out, dtype=np.float32).copy()


def save(path, x, sr=SR):
    """mono float32 -> 24-bit WAV."""
    x = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s24le", path], input=x.tobytes(), check=True)


def resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def stretch(x, ratio, sr=SR):
    """Change duration by ratio (new_len = len * ratio) keeping pitch (ffmpeg atempo)."""
    if abs(ratio - 1.0) < 0.01:
        return x
    tempo = 1.0 / ratio
    filt = []
    while tempo > 2.0:
        filt.append("atempo=2.0")
        tempo /= 2.0
    while tempo < 0.5:
        filt.append("atempo=0.5")
        tempo /= 0.5
    filt.append(f"atempo={tempo:.5f}")
    out = subprocess.run([ffmpeg_bin(), "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                          "-af", ",".join(filt), "-f", "f32le", "-"],
                         input=np.asarray(x, np.float32).tobytes(), capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def db(v):
    return 20 * np.log10(max(v, 1e-9))


# ---- pitch -----------------------------------------------------------------------------------
def f0_track(x, sr=SR, fmin=65.0, fmax=420.0):
    """YIN pitch track. Returns (times, f0 Hz with nan where unvoiced)."""
    y = resample(x, sr, F0_SR)
    w = int(0.04 * F0_SR)
    hop = int(HOP_S * F0_SR)
    tau_min, tau_max = int(F0_SR / fmax), int(F0_SR / fmin)
    n = max(0, 1 + (len(y) - 2 * w) // hop)
    times = np.arange(n) * HOP_S + w / F0_SR
    f0 = np.full(n, np.nan)
    if n == 0:
        return times, f0
    frames = np.lib.stride_tricks.sliding_window_view(y, 2 * w)[::hop][:n]
    energy = np.sqrt(np.mean(frames[:, :w] ** 2, axis=1))
    gate = max(np.percentile(energy, 30) * 1.5, 1e-4)
    nfft = 1 << int(np.ceil(np.log2(3 * w)))
    for i in np.nonzero(energy > gate)[0]:
        fr = frames[i]
        a = fr[:w]
        # difference function d(tau) = sum (a[j] - fr[j+tau])^2 via FFT cross-correlation
        corr = np.fft.irfft(np.fft.rfft(fr, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)[:tau_max + 1]
        sq = np.cumsum(np.concatenate([[0.0], fr ** 2]))
        taus = np.arange(tau_max + 1)
        d = np.sum(a ** 2) + (sq[taus + w] - sq[taus]) - 2 * corr
        d[0] = 0
        cm = d[1:] * np.arange(1, tau_max + 1) / np.maximum(np.cumsum(d[1:]), 1e-12)
        cm = np.concatenate([[1.0], cm])
        cand = np.nonzero(cm[tau_min:tau_max] < 0.15)[0]
        if len(cand):
            t = tau_min + cand[0]
            while t + 1 < tau_max and cm[t + 1] < cm[t]:
                t += 1
        else:
            t = tau_min + int(np.argmin(cm[tau_min:tau_max]))
            if cm[t] > 0.3:
                continue
        if 1 <= t < tau_max:
            s0, s1, s2 = cm[t - 1], cm[t], cm[t + 1]
            den = s0 - 2 * s1 + s2
            t = t + (0.5 * (s0 - s2) / den if abs(den) > 1e-12 else 0.0)
        f0[i] = F0_SR / t
    return times, f0


def f0_track_clip(x, sr=SR):
    """f0_track of a short clip cut tight around speech (a take's words): padded with silence first, so
    the energy gate (relative to the quietest 30 % of frames) doesn't drop the clip's own voiced frames."""
    x = np.asarray(x, dtype=np.float32)
    pad = int((0.25 + 0.25 * len(x) / sr) * sr)
    times, f0 = f0_track(np.concatenate([np.zeros(pad, np.float32), x, np.zeros(pad, np.float32)]), sr)
    times = times - pad / sr
    keep = (times >= 0) & (times <= len(x) / sr)
    return times[keep], f0[keep]


def semitones(f0, ref_hz):
    return 12 * np.log2(f0 / ref_hz)


def ending_contour(times, f0, t0, t1, ref_hz):
    """Pitch movement over [t0, t1] (the last words of a sentence), in semitones re the speaker's median.

    delta  = level of the last third of voiced frames minus the first third (negative = falling)
    final  = level of the last third relative to the speaker's median (finished statements end low)
    """
    m = (times >= t0) & (times <= t1) & ~np.isnan(f0)
    st = semitones(f0[m], ref_hz)
    if len(st) < 6:
        return None
    # drop octave jumps (YIN halving/doubling) relative to the local median
    st = st[np.abs(st - np.median(st)) < 7]
    if len(st) < 6:
        return None
    k = max(2, len(st) // 3)
    head, tail = float(np.mean(st[:k])), float(np.mean(st[-k:]))
    delta, final = tail - head, tail
    if delta <= -1.5 or (final <= -2.0 and delta < 0):
        shape = "falling"
    elif delta >= 1.5:
        shape = "rising"
    else:
        shape = "flat"
    return {"delta_st": round(delta, 2), "final_st": round(final, 2), "shape": shape, "voiced_frames": int(len(st))}


# ---- pitch shift (TD-PSOLA) ------------------------------------------------------------------
def _voiced_segments(times, f0, min_frames=3, max_gap=4):
    """[(frame_from, frame_to)] of voiced stretches (short unvoiced gaps bridged, octave slips dropped)."""
    f = np.array(f0, dtype=float)
    ok = ~np.isnan(f)
    if ok.sum() >= 5:          # YIN halving / doubling: drop frames an octave off the local median
        lf = np.log2(np.where(ok, f, np.nan))
        med = np.array([np.nanmedian(lf[max(0, i - 4): i + 5]) if ok[i] else np.nan for i in range(len(f))])
        ok &= np.abs(lf - med) < 0.4
    segs, i, n = [], 0, len(f)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (ok[j + 1] or (j + 1 + max_gap < n and ok[j + 1: j + 2 + max_gap].any())):
            j += 1
        while not ok[j]:
            j -= 1
        if j - i + 1 >= min_frames:
            segs.append((i, j))
        i = j + 1
    return segs, ok


def pitch_shift(x, semitones, sr=SR, track=None):
    """Formant-preserving pitch shift (TD-PSOLA), same length as x. Pitch marks follow the f0 track (one
    per period, snapped to the waveform's peaks); each grain is two periods of the original under a Hann
    window, laid down again at the new period. Grains keep their own waveform (not resampled), so the
    spectral envelope (formants) stays; unvoiced parts are copied through unchanged."""
    x = np.asarray(x, dtype=np.float32)
    if abs(semitones) < 0.01 or len(x) < int(0.05 * sr):
        return x.copy()
    times, f0 = track if track is not None else f0_track_clip(x, sr)
    segs, ok = _voiced_segments(times, f0)
    if not segs:
        return x.copy()
    scale = 2.0 ** (semitones / 12.0)
    xd = x.astype(np.float64)
    y = xd.copy()
    n_x = len(x)
    for fa, fb in segs:
        tt = times[fa: fb + 1][ok[fa: fb + 1]]
        ff = np.asarray(f0, float)[fa: fb + 1][ok[fa: fb + 1]]
        if len(ff) >= 5:           # steady periods: a 5-frame median (in log f0) irons out single-frame slips
            lf = np.log(ff)
            ff = np.exp(np.array([np.median(lf[max(0, i - 2): i + 3]) for i in range(len(lf))]))
        i0 = max(0, int((tt[0] - 2.5 * HOP_S) * sr))
        i1 = min(n_x - 1, int((tt[-1] + 2.5 * HOP_S) * sr))

        def period(n):
            return sr / float(np.interp(n / sr, tt, ff))
        # analysis marks: one per period, each on the largest sample (same polarity) near where expected
        T0 = period(i0)
        first = x[i0: i0 + int(T0) + 1]
        if len(first) < 2:
            continue
        sign = 1.0 if first.max() >= -first.min() else -1.0
        m = i0 + int(np.argmax(sign * first))
        marks = []
        while m < i1:
            marks.append(m)
            T = period(m)
            c = int(round(m + T))
            r = max(1, int(0.12 * T))
            lo, hi = max(0, c - r), min(n_x, c + r + 1)
            if lo >= hi or c >= i1:
                break
            m = lo + int(np.argmax(sign * xd[lo:hi]))
        if len(marks) < 3:
            continue
        marks = np.asarray(marks)
        out = np.zeros(n_x)
        s = float(marks[0])
        while s <= marks[-1]:
            k = int(np.argmin(np.abs(marks - s)))
            mk = int(marks[k])
            T = int(round(period(mk)))
            win = np.hanning(2 * T + 1)
            g0, g1 = mk - T, mk + T + 1
            gs = int(round(s)) - T
            a0 = max(0, -g0, -gs)
            a1 = min(2 * T + 1, n_x - g0, n_x - gs)
            if a1 > a0:
                out[gs + a0: gs + a1] += xd[g0 + a0: g0 + a1] * win[a0:a1]
            s += period(s) / scale
        # the new voiced part replaces the old one, cross-faded over about a period at both ends
        m0, m1 = int(marks[0]), int(marks[-1])
        ramp = max(2, int(period(m0)))
        mask = np.zeros(n_x)
        mask[m0:m1 + 1] = 1.0
        up = np.linspace(0, 1, ramp)
        mask[m0: m0 + ramp] = up[: len(mask[m0: m0 + ramp])]
        e0 = max(m0, m1 - ramp + 1)
        mask[e0: m1 + 1] = np.minimum(mask[e0: m1 + 1], up[::-1][-len(mask[e0: m1 + 1]):])
        y = y * (1 - mask) + out * mask
    return y.astype(np.float32)


def median_f0(x, sr=SR):
    """Median f0 (Hz) of the voiced frames of x, or None (fewer than 10)."""
    _t, f = f0_track_clip(x, sr)
    v = f[~np.isnan(f)]
    return float(np.median(v)) if len(v) >= 10 else None


# ---- tone (long-term spectrum) matching --------------------------------------------------------
def ltas(x, sr=SR, nperseg=4096):
    """Long-term average spectrum (Welch): (freqs, power)."""
    from scipy.signal import welch
    x = np.asarray(x, dtype=np.float64)
    n = min(nperseg, len(x))
    f, p = welch(x, sr, nperseg=n, noverlap=n // 2, window="hann")
    return f, p


def smooth_fraction_octave(f, p, frac=3):
    """Average power over a 1/frac-octave band around each frequency."""
    c = np.concatenate([[0.0], np.cumsum(p)])
    k = 2.0 ** (1.0 / (2 * frac))
    lo = np.searchsorted(f, f / k, side="left")
    hi = np.searchsorted(f, f * k, side="right")
    hi = np.maximum(hi, lo + 1)
    return (c[hi] - c[lo]) / (hi - lo)


def eq_curve(take, ref, noise=None, sr=SR, max_db=9.0, f_lo=70.0):
    """Correction (freqs, dB) that moves the take's long-term spectrum toward the reference's speech:
    1/3-octave smoothed, the reference's noise floor (room tone) taken out, level-neutral (median over
    150 Hz-5 kHz is 0 dB; the loudness match does the level), clamped to +-max_db. 0 dB (with soft
    edges) where there is nothing reliable to match: below f_lo, where the take has no content (above
    its bandwidth) and where the reference's speech is under its noise floor."""
    n = 4096 if min(len(take), len(ref)) >= 4096 else 1 << int(np.log2(max(256, min(len(take), len(ref)))))
    f, pt = ltas(take, sr, n)
    _f, pr = ltas(ref, sr, n)
    st, sr_raw = smooth_fraction_octave(f, pt), smooth_fraction_octave(f, pr)
    if noise is not None and len(noise) >= n:
        _f, pn = ltas(noise, sr, n)
        sn = smooth_fraction_octave(f, pn)
        sref = np.maximum(sr_raw - sn, 0.05 * sr_raw)
        ref_ok = sr_raw > 2 * sn
    else:
        sref = sr_raw
        ref_ok = sr_raw > sr_raw.max() * 1e-7
    eps = 1e-20
    corr = 10 * np.log10((sref + eps) / (st + eps))
    take_ok = st > st.max() * 10 ** -7.5        # 75 dB under its peak: nothing there (e.g. above a 24 kHz take's band)
    live = take_ok & ref_ok & (f >= f_lo)
    band = (f >= 150) & (f <= 5000) & live
    if band.any():
        corr -= np.median(corr[band])
    corr = np.clip(corr, -max_db, max_db)
    w = np.clip(smooth_fraction_octave(f, live.astype(float), frac=2) * 2 - 1, 0, 1) * live
    return f, corr * w


def apply_eq(x, f, gain_db, sr=SR, numtaps=1025):
    """Linear-phase FIR (firwin2) with the given magnitude curve; same length and timing as x."""
    from scipy.signal import fftconvolve, firwin2
    nyq = sr / 2.0
    ff = np.clip(np.asarray(f, float) / nyq, 0, 1)
    g = 10 ** (np.asarray(gain_db, float) / 20)
    if ff[0] > 0:
        ff, g = np.concatenate([[0.0], ff]), np.concatenate([[g[0]], g])
    if ff[-1] < 1:
        ff, g = np.concatenate([ff, [1.0]]), np.concatenate([g, [g[-1]]])
    h = firwin2(numtaps, ff, g)
    return fftconvolve(np.asarray(x, np.float64), h, mode="same").astype(np.float32)


def eq_match(take, ref, noise=None, sr=SR, max_db=9.0):
    """The take with a gentle EQ that matches the reference's tone. Returns (audio, info)."""
    if len(take) < int(0.2 * sr) or len(ref) < int(0.5 * sr):
        return np.asarray(take, np.float32), None
    f, c = eq_curve(take, ref, noise, sr, max_db)
    out = apply_eq(take, f, c, sr)
    return out, {"max_boost_db": round(float(c.max()), 1), "max_cut_db": round(float(c.min()), 1)}


# ---- melody (pitch contour) comparison ---------------------------------------------------------
def melody_curve(x, t0=None, t1=None, sr=SR, n=48, track=None):
    """The pitch contour of x over [t0, t1] in semitones re its own median, resampled to n points
    over normalised time (unvoiced gaps interpolated), or None when too little is voiced."""
    times, f0 = track if track is not None else f0_track_clip(x, sr)
    t0 = 0.0 if t0 is None else t0
    t1 = len(x) / sr if t1 is None else t1
    m = (times >= t0) & (times <= t1) & ~np.isnan(f0)
    if m.sum() < 8 or t1 - t0 < 0.1:
        return None
    st = 12 * np.log2(f0[m] / np.median(f0[m]))
    tt = times[m]
    keep = np.abs(st - np.median(st)) < 7
    st, tt = st[keep], tt[keep]
    if len(st) < 8:
        return None
    st = st - np.median(st)
    pos = (tt - t0) / (t1 - t0)
    return np.interp(np.linspace(0, 1, n), pos, st)


def melody_similarity(a, b, band=0.15):
    """{'corr', 'dtw_st'}: Pearson correlation of two melody curves and their mean distance (semitones)
    after dynamic time warping within a +-band window."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.std() < 0.15 or b.std() < 0.15:
        corr = 0.0
    else:
        corr = float(np.corrcoef(a, b)[0, 1])
    n, m = len(a), len(b)
    w = max(2, int(band * max(n, m)))
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0.0
    for i in range(1, n + 1):
        j0, j1 = max(1, int(i * m / n) - w), min(m, int(i * m / n) + w)
        for j in range(j0, j1 + 1):
            D[i, j] = abs(a[i - 1] - b[j - 1]) + min(D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
    return {"corr": round(corr, 3), "dtw_st": round(float(D[n, m] / (n + m)), 3)}


# ---- splicing --------------------------------------------------------------------------------
def snap_quiet(x, t, sr=SR, radius=0.03):
    """Move t to the quietest 5 ms within +-radius (cleaner cut point)."""
    a, b = int((t - radius) * sr), int((t + radius) * sr)
    a, b = max(a, 0), min(b, len(x))
    win = int(0.005 * sr)
    if b - a <= win:
        return t
    e = np.convolve(x[a:b] ** 2, np.ones(win), "valid")
    return (a + int(np.argmin(e)) + win // 2) / sr


def room_tone(x, silences, length, sr=SR):
    """Noise bed `length` samples long, looped from the recording's longest quiet stretch."""
    best = None
    for s in silences or []:
        a, b = int(s["s"] * sr) + int(0.05 * sr), int(s["e"] * sr) - int(0.05 * sr)
        if b - a > int(0.25 * sr) and (best is None or b - a > best[1] - best[0]):
            best = (a, min(b, a + 3 * sr))
    if best is None:
        # fall back to the quietest half second in the recording
        win = int(0.5 * sr)
        hop = win // 2
        starts = range(0, max(1, len(x) - win), hop)
        a = min(starts, key=lambda i: rms(x[i:i + win]))
        best = (a, a + win)
    bed = x[best[0]:best[1]]
    if len(bed) < 32:
        return np.zeros(length, np.float32)
    reps = int(np.ceil(length / len(bed))) + 1
    fade = min(int(0.02 * sr), len(bed) // 4)
    out = np.zeros(reps * (len(bed) - fade) + fade, np.float32)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)
    for i in range(reps):
        seg = bed.copy()
        seg[:fade] *= ramp
        seg[-fade:] *= ramp[::-1]
        p = i * (len(bed) - fade)
        out[p:p + len(seg)] += seg
    return out[:length]


def splice(x, a, b, patch, sr=SR, xfade=0.02):
    """Replace x[a:b] (seconds) with patch, equal-power crossfades at both joins."""
    ia, ib = int(a * sr), int(b * sr)
    n = int(xfade * sr)
    n = min(n, len(patch) // 4, ia, len(x) - ib)
    if n < 2:
        return np.concatenate([x[:ia], patch, x[ib:]])
    t = np.linspace(0, np.pi / 2, n, dtype=np.float32)
    fin, fout = np.sin(t), np.cos(t)
    p = patch.copy()
    head = x[ia - n:ia] * fout + p[:n] * fin
    tail = p[-n:] * fout + x[ib:ib + n] * fin
    return np.concatenate([x[:ia - n], head, p[n:-n], tail, x[ib + n:]])


# ---- word timing -------------------------------------------------------------------------------
def envelope(x, sr=SR, hop_s=0.005, win_s=0.01):
    """RMS envelope: (times of the frame centres, rms), one frame every hop_s."""
    x = np.asarray(x, np.float64)
    hop, win = max(1, int(hop_s * sr)), max(2, int(win_s * sr))
    if len(x) < win:
        return np.zeros(0), np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - win + 1, hop)
    e = np.sqrt(np.maximum(c[starts + win] - c[starts], 0.0) / win)
    return (starts + win / 2) / sr, e


def noise_floor(x, sr=SR):
    """The quiet level of x (10th percentile of its 10 ms RMS), at least -70 dBFS."""
    _t, e = envelope(x, sr, 0.01, 0.01)
    return max(float(np.percentile(e, 10)) if len(e) else 0.0, 10 ** (-70 / 20))


def word_onset(x, t, lo, hi, sr=SR, max_move=0.06):
    """Where a word that ASR starts at t really starts: the first rise out of the quietest point just before
    it (Whisper's word starts are often 20-80 ms off). Searched within [lo, hi] (seconds) and never moved
    more than max_move; returns (onset, quiet point before it)."""
    lo, hi = max(0.0, lo, t - 0.15), min(len(x) / sr, hi, t + 0.15)
    if hi - lo < 0.02:
        return t, max(lo, t - 0.005)
    tt, e = envelope(x[int(lo * sr): int(hi * sr)], sr)
    if len(e) < 3:
        return t, max(lo, t - 0.005)
    tt = tt + lo
    before = np.nonzero(tt <= t + 0.01)[0]
    kq = int(before[np.argmin(e[before])]) if len(before) else 0
    peak = float(e[kq:].max())
    if peak <= e[kq] * 2.0:                 # no rise: nothing to refine
        return t, float(tt[kq])
    thr = e[kq] + 0.12 * (peak - e[kq])
    up = np.nonzero(e[kq:] >= thr)[0]
    on = float(tt[kq + int(up[0])]) if len(up) else t
    if abs(on - t) > max_move:
        return t, max(lo, t - 0.005)
    return on, float(tt[kq])


def stretch_map(x, src, dst, sr=SR, frame_s=0.03, tol_s=0.008):
    """Variable time stretch that keeps pitch (WSOLA): the input instant src[i] (seconds) lands at dst[i] in
    the output, linear in between. src and dst rise monotonically from 0; the output is dst[-1] long. Each
    30 ms frame is taken within +-8 ms of where the map says, at the offset that continues the previous
    frame's waveform best (normalised cross-correlation), and overlap-added under a Hann window."""
    from scipy.signal import correlate
    x = np.asarray(x, np.float64)
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    n_out = int(round(dst[-1] * sr))
    if n_out <= 0:
        return np.zeros(0, np.float32)
    if np.allclose(src, dst, atol=0.5 / sr) and n_out == len(x):
        return x.astype(np.float32)
    L = int(frame_s * sr) // 2 * 2
    H, T = L // 2, int(tol_s * sr)
    off = L + T
    xp = np.concatenate([np.zeros(off), x, np.zeros(off + L)])
    win = np.hanning(L + 1)[:L]
    y = np.zeros(n_out + 3 * L)
    ws = np.zeros(n_out + 3 * L)
    c2 = np.concatenate([[0.0], np.cumsum(xp * xp)])
    prev = None
    k = -1
    while k * H < n_out + H:
        o = k * H + L                                   # frame start in y (y has L of lead)
        t_in = float(np.interp((k * H + H) / sr, dst, src))
        nom = int(round(t_in * sr)) - H + off
        nom = min(max(nom, T), len(xp) - L - T - 1)
        if prev is None:
            st = nom
        else:
            ref = xp[prev + H: prev + H + L]
            seg = xp[nom - T: nom + T + L]
            cc = correlate(seg, ref, mode="valid", method="fft")
            idx = np.arange(nom - T, nom - T + len(cc))
            en = np.sqrt(np.maximum(c2[idx + L] - c2[idx], 0.0)) + 1e-9
            st = int(idx[int(np.argmax(cc / en))]) if np.dot(ref, ref) > 1e-12 else nom
        y[o: o + L] += xp[st: st + L] * win
        ws[o: o + L] += win
        prev = st
        k += 1
    y = y[L: L + n_out] / np.maximum(ws[L: L + n_out], 1e-3)
    return y.astype(np.float32)


def join(parts, sr=SR, xfade_s=0.01):
    """Concatenate audio parts with short equal-power crossfades (cuts made in pauses)."""
    parts = [np.asarray(p, np.float32) for p in parts if len(p)]
    if not parts:
        return np.zeros(0, np.float32)
    out = parts[0]
    for p in parts[1:]:
        n = min(int(xfade_s * sr), len(out) // 2, len(p) // 2)
        if n < 2:
            out = np.concatenate([out, p])
            continue
        t = np.linspace(0, np.pi / 2, n, dtype=np.float32)
        out = np.concatenate([out[:-n], out[-n:] * np.cos(t) + p[:n] * np.sin(t), p[n:]])
    return out


def edge_preview(x, a, b, patch, lead_s, sr=SR, head_s=0.03, fade_s=(0.04, 0.08), fade_out_s=0.05):
    """How [a, b] sounds in the story with the patch in it (the patch is laid at a, len(patch) long):
    the first head_s are the recording itself at a, then an equal-power crossfade (40-80 ms, ending just
    before the patch's first word at lead_s, i.e. in the pause before it) into the patch; before the end
    an equal-power crossfade back to the recording, whose samples just before b end the file (so whatever
    follows b in the story joins it without a seam)."""
    p = np.asarray(patch, np.float32)
    L = len(p)
    ia, ib = int(round(a * sr)), int(round(b * sr))
    h = int(head_s * sr)
    fi = int(min(max(lead_s - head_s - 0.005, fade_s[0]), fade_s[1]) * sr)
    fo = int(fade_out_s * sr)
    if 2 * h + fi + fo > L:                          # a very short patch: shrink the edges to fit
        k = L / float(2 * h + fi + fo + 1)
        h, fi, fo = int(h * k), int(fi * k), int(fo * k)

    def rec(i0, n):
        seg = np.zeros(n, np.float32)
        s0, s1 = max(0, i0), min(len(x), i0 + n)
        if s1 > s0:
            seg[s0 - i0: s1 - i0] = x[s0:s1]
        return seg
    y = p.copy()
    head = rec(ia, h + fi)
    tail = rec(ib - h - fo, h + fo)
    y[:h] = head[:h]
    if fi > 1:
        t = np.linspace(0, np.pi / 2, fi, dtype=np.float32)
        y[h: h + fi] = head[h:] * np.cos(t) + p[h: h + fi] * np.sin(t)
    if fo > 1:
        t = np.linspace(0, np.pi / 2, fo, dtype=np.float32)
        y[L - h - fo: L - h] = p[L - h - fo: L - h] * np.cos(t) + tail[:fo] * np.sin(t)
    if h:
        y[L - h:] = tail[fo:]
    return y, {"head_s": round(h / sr, 3), "fade_in_s": round(fi / sr, 3), "fade_out_s": round(fo / sr, 3)}


def save_quick(path, x, sr=SR):
    """mono float32 -> 16-bit WAV with Python's wave module (no ffmpeg: fast enough to cut a preview on a click)."""
    import wave
    pcm = (np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(sr))
        f.writeframes(pcm.tobytes())
    return path
