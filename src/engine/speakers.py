#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#Copyright (c) 2026 Szymon Wolarz
#Licensed under the MIT License. See LICENSE file in the project root for full license information.

"""
MODULE: speakers.py
ROLE: Engine
DESCRIPTION:
Finds the second speaker of an interview (usually the interviewer, often off-mic) sentence by
sentence. Two clues are combined: how quiet the sentence is compared with the main speaker, and
how far its voice is from the main voice (CAM++ speaker embeddings from 3D-Speaker, Apache-2.0,
run with onnxruntime; ~30 MB model downloaded on first use). No PyTorch.
"""

import os
import subprocess
import sys
import urllib.request

import numpy as np

MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx")
MODEL_FILE = "campplus_sv_en_voxceleb_16k.onnx"
SR = 16000
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


HF_REPO = "csukuangfj/speaker-embedding-models"
HF_FILE = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"


def ensure_model(models_dir, progress=None):
    """Path of the speaker model, downloaded once into models_dir/speakers/.
    Tries Hugging Face (same client as the Whisper models), then GitHub with certifi's
    certificates, then curl (system certificates): some Python installs have no CA bundle."""
    folder = os.path.join(models_dir, "speakers")
    path = os.path.join(folder, MODEL_FILE)
    if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
        return path
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".part"
    errors = []
    try:
        from huggingface_hub import hf_hub_download
        got = hf_hub_download(repo_id=HF_REPO, filename=HF_FILE, local_dir=folder)
        os.replace(got, path)
        return path
    except Exception as e:
        errors.append(f"huggingface: {e}")
    try:
        import ssl
        ctx = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        with urllib.request.urlopen(MODEL_URL, timeout=60, context=ctx) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        os.replace(tmp, path)
        return path
    except Exception as e:
        errors.append(f"github: {e}")
    try:
        subprocess.run(["curl", "-sfL", "-o", tmp, MODEL_URL], check=True, creationflags=_NO_WINDOW, timeout=300)
        os.replace(tmp, path)
        return path
    except Exception as e:
        errors.append(f"curl: {e}")
    raise RuntimeError("speaker model download failed: " + " | ".join(errors))


def load_audio(ffmpeg, path):
    """Any audio/video file -> mono float32 at 16 kHz (through ffmpeg)."""
    cmd = [ffmpeg or "ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


def fbank(x, n_mels=80):
    """Kaldi-style log mel filter bank (25 ms / 10 ms, povey window, pre-emphasis 0.97), mean-normalised."""
    x = x.astype(np.float64) * 32768.0
    flen, shift, nfft = 400, 160, 512
    if len(x) < flen:
        x = np.pad(x, (0, flen - len(x)))
    n = 1 + (len(x) - flen) // shift
    idx = np.arange(flen)[None, :] + shift * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] - 0.97 * fr[:, :1], fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(flen) / (flen - 1))) ** 0.85
    spec = np.abs(np.fft.rfft(fr * win, nfft)) ** 2
    mel = lambda f: 1127.0 * np.log(1 + f / 700.0)
    pts = np.linspace(mel(20.0), mel(SR / 2), n_mels + 2)
    fbins = mel(np.arange(nfft // 2 + 1) * SR / nfft)
    W = np.zeros((n_mels, nfft // 2 + 1))
    for m in range(n_mels):
        lo, c, hi = pts[m], pts[m + 1], pts[m + 2]
        W[m] = np.maximum(0, np.minimum((fbins - lo) / (c - lo), (hi - fbins) / (hi - c)))
    feats = np.log(np.maximum(spec @ W.T, np.finfo(float).eps))
    return (feats - feats.mean(axis=0)).astype(np.float32)


def sentences(words):
    """Group spoken words into sentences: [(first index, last index), ...]."""
    out, first = [], None
    for i, w in enumerate(words):
        if w.get("type") in ("silence", "inaudible") or w.get("is_inaudible") or not (w.get("text") or "").strip():
            continue
        if first is None:
            first = i
        last = i
        if (w.get("text") or "").strip()[-1:] in ".?!" :
            out.append((first, last))
            first = None
    if first is not None:
        out.append((first, last))
    return out


def _robust_z(v):
    med = np.median(v)
    mad = np.median(np.abs(v - med)) * 1.4826 + 1e-9
    return (v - med) / mad


def find_second_speaker(audio, words, model_path, threshold=6.0, cancel=None):
    """Word indices said by the second speaker, plus how many sentences were looked at.
    A sentence counts when it is clearly quieter than the main speaker and/or in a different voice."""
    import onnxruntime as ort
    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    sents = sentences(words)
    if len(sents) < 4:
        return [], len(sents)
    levels, embs = [], []
    for a, b in sents:
        if cancel and cancel():
            return [], 0
        t0, t1 = float(words[a].get("start", 0.0)), float(words[b].get("end", 0.0))
        seg = audio[int(max(0.0, t0) * SR):int(t1 * SR)]
        if len(seg) >= 800:
            fr = seg[:len(seg) // 800 * 800].reshape(-1, 800)
            e = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
            levels.append(20 * np.log10(np.quantile(e, 0.9)))
        else:
            levels.append(-90.0)
        if t1 - t0 >= 0.6:
            pad = audio[int(max(0.0, t0 - 0.05) * SR):int((t1 + 0.05) * SR)]
            v = sess.run(None, {"x": fbank(pad)[None]})[0][0]
            embs.append(v / (np.linalg.norm(v) + 1e-9))
        else:
            embs.append(None)
    E = np.stack([e for e in embs if e is not None])
    cen = E.mean(axis=0)
    cen /= np.linalg.norm(cen)
    for _ in range(3):  # the main voice: centroid of the 70% most typical sentences
        s = E @ cen
        cen = E[s >= np.quantile(s, 0.3)].mean(axis=0)
        cen /= np.linalg.norm(cen)
    sims = np.array([float(e @ cen) if e is not None else np.nan for e in embs])
    sims = np.where(np.isnan(sims), np.nanmedian(sims), sims)
    score = -_robust_z(np.array(levels)) - _robust_z(sims)
    picked = []
    for (a, b), sc in zip(sents, score):
        if sc > threshold:
            picked.extend(i for i in range(a, b + 1)
                          if not (words[i].get("type") in ("silence", "inaudible") or words[i].get("is_inaudible")))
    return picked, len(sents)


# ── Several speakers (up to 10) ─────────────────────────────────────────────────────────────────

def _is_spoken(w):
    return not (w.get("type") in ("silence", "inaudible") or w.get("is_inaudible")
                or not (w.get("text") or "").strip())


def _sentence_embeddings(audio, words, sess, sents, cancel=None, progress=None):
    """Unit CAM++ embedding per sentence (None when shorter than 0.6 s), or None if cancelled."""
    embs = []
    for k, (a, b) in enumerate(sents):
        if cancel and cancel():
            return None
        t0, t1 = float(words[a].get("start", 0.0)), float(words[b].get("end", 0.0))
        if t1 - t0 >= 0.6:
            pad = audio[int(max(0.0, t0 - 0.05) * SR):int((t1 + 0.05) * SR)]
            v = sess.run(None, {"x": fbank(pad)[None]})[0][0]
            embs.append(v / (np.linalg.norm(v) + 1e-9))
        else:
            embs.append(None)
        if progress and k % 20 == 0:
            progress(0.9 * k / max(1, len(sents)))
    return embs


def cluster_voices(E, durations, n_speakers=None, max_speakers=10, threshold=0.75):
    """Average-linkage agglomerative clustering of unit embeddings E (n x d) on cosine distance.
    Automatic (n_speakers=None): merge while the closest clusters are nearer than `threshold`
    (0.75: a single voice in a noisy field recording stays one speaker; tuned on real recordings),
    then keep merging down to max_speakers. Tiny clusters (< 2 sentences and < 3 s) are absorbed
    by the nearest larger one. Returns a cluster index per row (0..k-1, arbitrary order)."""
    n = len(E)
    if n == 0:
        return np.zeros(0, dtype=int)
    D = 1.0 - np.clip(E @ E.T, -1.0, 1.0)
    np.fill_diagonal(D, np.inf)
    size = np.ones(n)
    alive = np.ones(n, dtype=bool)
    label = np.arange(n)
    target = max(1, min(n, n_speakers)) if n_speakers else 1
    k = n
    while k > target:
        i, j = divmod(int(np.argmin(D)), n)
        if not n_speakers and D[i, j] > threshold and k <= max_speakers:
            break
        # Lance-Williams update for average linkage: merge j into i
        row = (size[i] * D[i] + size[j] * D[j]) / (size[i] + size[j])
        D[i, :] = row
        D[:, i] = row
        D[i, i] = np.inf
        D[j, :] = np.inf
        D[:, j] = np.inf
        size[i] += size[j]
        alive[j] = False
        label[label == j] = i
        k -= 1

    durations = np.asarray(durations, dtype=float)
    roots = list(np.unique(label))
    if len(roots) > 1 and not n_speakers:
        cents = {r: E[label == r].mean(axis=0) for r in roots}
        def small(r):
            m = label == r
            return m.sum() < 2 and durations[m].sum() < 3.0
        for r in [r for r in roots if small(r)]:
            others = [o for o in roots if o != r and not small(o) and (label == o).any()]
            if not others:
                continue
            c = cents[r] / (np.linalg.norm(cents[r]) + 1e-9)
            best = max(others, key=lambda o: float(c @ (cents[o] / (np.linalg.norm(cents[o]) + 1e-9))))
            label[label == r] = best
    _, out = np.unique(label, return_inverse=True)
    return out


WIN_S, HOP_S, MERGE_DIST = 1.5, 0.75, 0.3


def _windows(words, sents):
    """1.5 s analysis windows (0.75 s apart) over every sentence; a shorter sentence gets one window
    of its own length (at least 0.8 s). Returns [(t0, t1, full)], full = a whole 1.5 s window."""
    out = []
    for a, b in sents:
        s0, s1 = float(words[a].get("start", 0.0)), float(words[b].get("end", 0.0))
        if s1 - s0 <= WIN_S:
            c, h = (s0 + s1) / 2, max(0.8, s1 - s0) / 2
            out.append((max(0.0, c - h), c + h, False))
            continue
        t = s0
        while True:
            e = min(t + WIN_S, s1)
            if e - t < WIN_S:
                t, e = s1 - WIN_S, s1
            out.append((t, e, True))
            if e >= s1:
                break
            t += HOP_S
    return out


def _voice_centres(W, full, seeds, n_speakers=None):
    """k-means on the full windows, started from the sentence clusters' centres. Voices with
    almost no windows are dropped, and (automatic count) voices closer than MERGE_DIST are merged."""
    WF = W[full] if full.any() else W
    C = seeds
    for _ in range(15):
        wl = (WF @ C.T).argmax(1)
        keep = [j for j in range(len(C)) if n_speakers or (wl == j).sum() >= max(3, 0.02 * len(WF))]
        keep = [j for j in keep if (wl == j).any()] or [0]
        C2 = np.stack([WF[wl == j].mean(0) if (wl == j).any() else C[j] for j in keep])
        C2 /= np.linalg.norm(C2, axis=1, keepdims=True)
        if len(C2) == len(C) and np.allclose(C2, C):
            break
        C = C2
    if not n_speakers and len(C) > 2:
        # A small "voice" lying between two others is a blend (crosstalk, phone artefacts), not a
        # person: under 10% of the windows and clearly closer to both than they are to each other.
        while len(C) > 2:
            wl = (WF @ C.T).argmax(1)
            share = np.bincount(wl, minlength=len(C)) / max(1, len(wl))
            j = int(share.argmin())
            others = [x for x in range(len(C)) if x != j]
            D = 1.0 - C @ C.T
            a, b = max(((x, y) for x in others for y in others if x < y), key=lambda p: D[p[0], p[1]])
            if share[j] < 0.10 and D[j, a] < 0.9 * D[a, b] and D[j, b] < 0.9 * D[a, b]:
                C = C[others]
                for _ in range(10):  # its windows go to the real voices
                    wl = (WF @ C.T).argmax(1)
                    C2 = np.stack([WF[wl == x].mean(0) if (wl == x).any() else C[x] for x in range(len(C))])
                    C2 /= np.linalg.norm(C2, axis=1, keepdims=True)
                    if np.allclose(C2, C):
                        break
                    C = C2
            else:
                break
    if not n_speakers and len(C) > 1:
        D = 1.0 - C @ C.T
        group = list(range(len(C)))
        for i in range(len(C)):
            for j in range(i + 1, len(C)):
                if D[i, j] < MERGE_DIST:
                    gi, gj = group[i], group[j]
                    group = [gi if g == gj else g for g in group]
        ids = sorted(set(group))
        C = np.stack([C[[k for k in range(len(C)) if group[k] == g]].mean(0) for g in ids])
        C /= np.linalg.norm(C, axis=1, keepdims=True)
    return C


SWITCH_IN, SWITCH_OUT = 1.0, 0.15  # cost of a change of voice inside a sentence / between sentences


def _viterbi(sim, sent_of, pen_in=SWITCH_IN, pen_out=SWITCH_OUT):
    """Most likely voice per window: the windows' similarity to each voice, minus a cost for each
    change of voice (dear inside a sentence, cheap between sentences), so one odd window doesn't flip
    a turn while a real reply still does. sim: (windows x voices); sent_of: sentence index per window."""
    n, k = sim.shape
    score = sim[0].astype(float).copy()
    back = np.zeros((n, k), dtype=np.int32)
    for t in range(1, n):
        pen = pen_in if sent_of[t] == sent_of[t - 1] else pen_out
        best, arg = score.max(), int(score.argmax())
        switch = best - pen
        stay = score >= switch
        back[t] = np.where(stay, np.arange(k), arg)
        score = np.where(stay, score, switch) + sim[t]
    path = np.zeros(n, dtype=np.int32)
    path[-1] = int(score.argmax())
    for t in range(n - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path


def _smooth_in_sentences(words, sents, labels):
    """Inside a sentence, a fragment of fewer than 3 words and under 0.8 s takes its longer
    neighbour's speaker (window edges, not a real change of voice)."""
    for a, b in sents:
        while True:
            runs = []
            for i in range(a, b + 1):
                if labels[i] is None:
                    continue
                if runs and labels[runs[-1][-1]] == labels[i]:
                    runs[-1].append(i)
                else:
                    runs.append([i])
            short = [k for k, r in enumerate(runs) if len(runs) > 1 and len(r) < 3
                     and float(words[r[-1]].get("end", 0)) - float(words[r[0]].get("start", 0)) < 0.8]
            if not short:
                break
            k = short[0]
            nb = [runs[j] for j in (k - 1, k + 1) if 0 <= j < len(runs)]
            to = labels[max(nb, key=len)[0]]
            for i in runs[k]:
                labels[i] = to


def find_speakers(audio, words, model_path, n_speakers=None, max_speakers=10, threshold=0.75,
                  cancel=None, progress=None, _embed=None):
    """Who speaks each word. Returns (labels, info): labels has one entry per word, a speaker
    number 1..k (1 = talks most) or None for silence/inaudible/empty words;
    info = {"count": k, "talk_s": {1: seconds, ...}, "sentences": n}.
    1. Whole sentences are clustered by voice: how many voices, roughly who says what.
    2. Then 1.5 s windows decide word by word, so a sentence where both people speak
       ("Yes. But you said...") is split between them."""
    sents = sentences(words)
    none = [None] * len(words)
    if not sents:
        return none, {"count": 0, "talk_s": {}, "sentences": 0}
    sess = None
    if _embed is not None:  # tests
        embs = _embed(sents)
    else:
        import onnxruntime as ort
        sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        half = (lambda f: progress(0.45 * f)) if progress else None
        embs = _sentence_embeddings(audio, words, sess, sents, cancel, half)
    if embs is None:
        return none, {"count": 0, "talk_s": {}, "sentences": len(sents)}

    dur = [max(0.0, float(words[b].get("end", 0.0)) - float(words[a].get("start", 0.0))) for a, b in sents]
    have = [k for k, e in enumerate(embs) if e is not None]
    sent_lab = [0] * len(sents)
    lab = None
    if len(have) >= 2:
        lab = cluster_voices(np.stack([embs[k] for k in have]), [dur[k] for k in have],
                             n_speakers, max_speakers, threshold)
        for k, l in zip(have, lab):
            sent_lab[k] = int(l)
        # Too short to embed: the nearest embedded sentence in time (the previous one on a tie)
        for k in range(len(sents)):
            if embs[k] is None:
                prev = next((h for h in reversed(have) if h < k), None)
                nxt = next((h for h in have if h > k), None)
                if prev is None or (nxt is not None and
                                    float(words[sents[nxt][0]].get("start", 0)) - float(words[sents[k][1]].get("end", 0))
                                    < float(words[sents[k][0]].get("start", 0)) - float(words[sents[prev][1]].get("end", 0))):
                    sent_lab[k] = sent_lab[nxt]
                else:
                    sent_lab[k] = sent_lab[prev]

    labels = list(none)
    for (a, b), l in zip(sents, sent_lab):
        for i in range(a, b + 1):
            if _is_spoken(words[i]):
                labels[i] = l

    # 2. Word by word, from 1.5 s windows
    if sess is not None and lab is not None and lab.max() >= 1:
        wins = _windows(words, sents)
        W = []
        for n, (t0, t1, _f) in enumerate(wins):
            if cancel and cancel():
                return none, {"count": 0, "talk_s": {}, "sentences": len(sents)}
            seg = audio[int(max(0.0, t0) * SR):int(t1 * SR)]
            v = sess.run(None, {"x": fbank(seg)[None]})[0][0]
            W.append(v / (np.linalg.norm(v) + 1e-9))
            if progress and n % 40 == 0:
                progress(0.45 + 0.5 * n / max(1, len(wins)))
        W = np.stack(W)
        full = np.array([f for _a, _b, f in wins])
        M = np.stack([embs[k] for k in have])
        long_s = np.array([dur[k] >= WIN_S for k in have])
        seeds = np.stack([M[(lab == j) & long_s].mean(0) if ((lab == j) & long_s).any() else M[lab == j].mean(0)
                          for j in range(int(lab.max()) + 1)])
        seeds /= np.linalg.norm(seeds, axis=1, keepdims=True)
        C = _voice_centres(W, full, seeds, n_speakers)
        sim = W @ C.T
        starts = np.array([w[0] for w in wins])
        ends = np.array([w[1] for w in wins])
        mids = (starts + ends) / 2
        # which sentence each window belongs to (windows are made sentence by sentence, in order)
        sent_of, k = [], 0
        for a, b, _f in wins:
            m = (a + b) / 2
            while k + 1 < len(sents) and float(words[sents[k][1]].get("end", 0)) < m - 1.0:
                k += 1
            sent_of.append(k)
        path = _viterbi(sim, sent_of)
        onehot = np.eye(len(C))[path]
        for i, w in enumerate(words):
            if labels[i] is None:
                continue
            m = (float(w.get("start", 0)) + float(w.get("end", 0))) / 2
            cov = np.where((starts <= m) & (ends >= m))[0]
            if len(cov) == 0:
                cov = [int(np.argmin(np.abs(mids - m)))]
            labels[i] = int(onehot[cov].sum(0).argmax())
        _smooth_in_sentences(words, sents, labels)

    talk = {}
    for i, l in enumerate(labels):
        if l is not None:
            talk[l] = talk.get(l, 0.0) + max(0.0, float(words[i].get("end", 0)) - float(words[i].get("start", 0)))
    order = sorted(talk, key=lambda l: -talk[l])
    renum = {l: k + 1 for k, l in enumerate(order)}
    labels = [renum[l] if l is not None else None for l in labels]
    if progress:
        progress(1.0)
    return labels, {"count": len(order), "talk_s": {renum[l]: round(talk[l], 1) for l in order},
                    "sentences": len(sents)}
