#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: voicestudio_client.py
ROLE: Client for a separately installed VoiceStudio (https://github.com/debpalash/VoiceStudio)
DESCRIPTION:
StoryCraft never bundles or imports VoiceStudio code. It talks to the VoiceStudio app the user
installed, over VoiceStudio's documented local HTTP API (loopback, port 3900 by default; no key
needed from the same machine). VoiceStudio is AGPL-3.0 and stays a separate program; the voice
models it downloads carry their own licences, which this client reads back and reports.

Endpoints used (VoiceStudio backend/api/routers):
  GET  /health                    liveness
  GET  /engines/tts               active engine, its model, cloning support
  GET  /models                    model catalogue rows with license_info
  GET  /watermark/status          whether generated audio carries the invisible watermark
  POST /generate                  text -> WAV, cloned from ref_audio (+ ref_text); speed works when cloning
  POST /profiles                  save a reference clip as a voice profile
  POST /convert                   speech -> speech in a profile's voice. It transcribes the recording and
                                  says the text again: the recording's timing and intonation are not kept
"""
import os

import requests

DEFAULT_URL = "http://127.0.0.1:3900"


class VoiceStudioError(RuntimeError):
    pass


class VoiceStudioClient:
    def __init__(self, base_url=None, api_key=None, timeout=600):
        self.base = (base_url or os.environ.get("VOICESTUDIO_URL") or DEFAULT_URL).rstrip("/")
        key = api_key or os.environ.get("VOICESTUDIO_API_KEY") or os.environ.get("OMNIVOICE_API_KEY")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.timeout = timeout

    # ---- plumbing -------------------------------------------------------------------------
    def _req(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        try:
            r = requests.request(method, self.base + path, headers=self.headers, **kw)
        except requests.ConnectionError:
            raise VoiceStudioError(
                f"VoiceStudio isn't reachable at {self.base}. Start the VoiceStudio app (or its Docker "
                "container) and try again.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise VoiceStudioError(f"VoiceStudio {method} {path} failed ({r.status_code}): {detail}")
        return r

    def _json(self, method, path, **kw):
        return self._req(method, path, **kw).json()

    # ---- status ---------------------------------------------------------------------------
    def health(self):
        r = self._req("GET", "/health", timeout=5)
        return r.status_code == 200

    def tts_engine(self):
        """{'id', 'model', 'supports_cloning', 'name'} for the active text-to-speech engine."""
        d = self._json("GET", "/engines/tts", timeout=15)
        active = d.get("active")
        row = next((b for b in d.get("backends", []) if b.get("id") == active), {})
        return {"id": active, "model": d.get("active_model"), "name": row.get("name") or row.get("label") or active,
                "supports_cloning": row.get("supports_cloning"), "row": row}

    def model_licence(self, model_id):
        """license_info of the catalogue row for model_id, or None when VoiceStudio doesn't list it."""
        if not model_id:
            return None
        try:
            d = self._json("GET", "/models", timeout=30)
        except VoiceStudioError:
            return None
        rows = d if isinstance(d, list) else d.get("models") or d.get("data") or []
        for row in rows:
            ids = {row.get("repo_id"), row.get("id"), row.get("model_id")}
            if model_id in ids:
                return row.get("license_info") or {k: row.get(k) for k in ("license", "commercial_use") if k in row}
        return None

    def watermark_status(self):
        try:
            return self._json("GET", "/watermark/status", timeout=10)
        except VoiceStudioError:
            return None

    # ---- generation -----------------------------------------------------------------------
    def generate(self, text, ref_audio_path, ref_text="", language=None, seed=None, engine=None, speed=None):
        """Clone the voice in ref_audio_path and speak text. Returns (wav_bytes, info dict).

        speed: VoiceStudio's pace (e.g. 0.88 slower, 1.14 faster); sent only when given and not 1.0.

        effect_preset=raw: no broadcast mastering, so the take can be matched to the recording.
        postprocess_output=False: VoiceStudio keeps the take's edges instead of trimming silences.
        """
        data = {"text": text, "effect_preset": "raw", "postprocess_output": "false", "wav_bits": "16",
                "max_chunk_chars": "0"}
        if ref_text:
            data["ref_text"] = ref_text
        if language:
            data["language"] = language
        if seed is not None:
            data["seed"] = str(seed)
        if engine:
            data["engine"] = engine
        if speed is not None and abs(float(speed) - 1.0) > 1e-6:
            data["speed"] = f"{float(speed):.3f}"
        with open(ref_audio_path, "rb") as f:
            r = self._req("POST", "/generate", data=data, files={"ref_audio": ("reference.wav", f, "audio/wav")})
        info = {k: r.headers.get(h) for k, h in (("audio_id", "X-Audio-Id"), ("seed", "X-Seed"),
                                                  ("duration", "X-Audio-Duration"), ("gen_time", "X-Gen-Time"))}
        return r.content, info

    def create_profile(self, name, ref_audio_path, ref_text="", language="Auto"):
        with open(ref_audio_path, "rb") as f:
            d = self._json("POST", "/profiles", data={"name": name, "ref_text": ref_text, "language": language,
                                                      "kind": "clone"},
                           files={"ref_audio": ("reference.wav", f, "audio/wav")})
        pid = d.get("id") or (d.get("profile") or {}).get("id")
        if not pid:
            raise VoiceStudioError(f"VoiceStudio created the profile but returned no id: {d}")
        return pid

    def convert(self, audio_path, profile_id, match_duration=True):
        """Speech -> speech in the profile's voice. VoiceStudio's /convert takes only audio, profile_id
        and match_duration: it transcribes the recording and re-synthesises the text, so the recording's
        timing and intonation are lost (StoryCraft's "use my recording" therefore generates takes and
        keeps the one closest to the recording instead). Returns (wav_bytes, info dict)."""
        with open(audio_path, "rb") as f:
            d = self._json("POST", "/convert",
                           data={"profile_id": profile_id, "match_duration": "true" if match_duration else "false"},
                           files={"audio": (os.path.basename(audio_path), f, "audio/wav")})
        url = d.get("audio_url")
        if not url:
            raise VoiceStudioError(f"VoiceStudio converted the take but returned no audio_url: {d}")
        wav = self._req("GET", url if url.startswith("/") else "/" + url).content
        return wav, d
