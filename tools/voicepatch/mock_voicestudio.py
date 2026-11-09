#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MODULE: mock_voicestudio.py
ROLE: Test stub, NOT VoiceStudio. Answers the handful of VoiceStudio API routes voicepatch.py uses,
so the pipeline (alignment, pitch check, splice, report) can be tested on a machine without
VoiceStudio. /generate speaks the text with the macOS `say` voice and ignores the reference, so
the output does NOT sound like the speaker. /convert returns the uploaded take unchanged.

  python3 tools/voicepatch/mock_voicestudio.py --port 3999
  voicepatch.py --url http://127.0.0.1:3999 batch <timeline.json>
"""
import argparse
import email.parser
import email.policy
import json
import os
import subprocess
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = tempfile.mkdtemp(prefix="mock-voicestudio-")


def multipart(handler):
    n = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(n)
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + handler.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename():
            files[name] = part.get_payload(decode=True)
        else:
            fields[name] = part.get_payload(decode=True).decode()
    return fields, files


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", headers=None):
        data = json.dumps(body).encode() if ctype == "application/json" else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "mock": True})
        if self.path == "/engines/tts":
            return self._send(200, {"active": "mock-say", "active_model": "mock/say",
                                    "backends": [{"id": "mock-say", "name": "Mock (macOS say)",
                                                  "supports_cloning": True}]})
        if self.path == "/models":
            return self._send(200, [{"repo_id": "mock/say", "license_info": {"license": "test stub, no model"}}])
        if self.path == "/watermark/status":
            return self._send(200, {"invisible_enabled": True, "audioseal_available": False})
        if self.path.startswith("/audio/"):
            p = os.path.join(OUT, os.path.basename(self.path))
            if os.path.isfile(p):
                return self._send(200, open(p, "rb").read(), "audio/wav")
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        fields, files = multipart(self)
        if self.path == "/generate":
            aid = uuid.uuid4().hex[:12]
            aiff, wav = os.path.join(OUT, aid + ".aiff"), os.path.join(OUT, aid + ".wav")
            subprocess.run(["say", "-v", "Samantha", "-o", aiff, fields.get("text", "")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", aiff, "-ar", "24000", "-ac", "1", wav], check=True)
            return self._send(200, open(wav, "rb").read(), "audio/wav",
                              {"X-Audio-Id": aid, "X-Seed": fields.get("seed", "")})
        if self.path == "/profiles":
            return self._send(200, {"id": "mock-" + uuid.uuid4().hex[:8], "name": fields.get("name")})
        if self.path == "/convert":
            aid = uuid.uuid4().hex[:12]
            open(os.path.join(OUT, aid + ".wav"), "wb").write(files["audio"])
            return self._send(200, {"audio_url": f"/audio/{aid}.wav", "id": aid, "text": ""})
        self._send(404, {"detail": "not found"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3999)
    a = ap.parse_args()
    print(f"mock VoiceStudio (test stub) on http://127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
