"""Browser mode (python main.py --browser): serves the same frontend with a local JSON API.

Handy for developing the interface in any browser; only binds to 127.0.0.1.
"""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).with_name("web")


def serve(api, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.split("?")[0].lstrip("/") or "index.html"
            path = (WEB / name).resolve()
            if not path.is_relative_to(WEB.resolve()) or not path.is_file():
                self.send_error(404)
                return
            self._send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

        def do_POST(self):
            method = self.path.removeprefix("/api/")
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self.send_error(403)
                return
            if method.startswith("_") or not callable(getattr(api, method, None)):
                self.send_error(404)
                return
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
            try:
                body, status = {"result": getattr(api, method)(*args)}, 200
            except Exception as exc:
                body, status = {"error": str(exc)}, 500
            self._send(json.dumps(body, ensure_ascii=False, default=str).encode(), "application/json", status)

        def _send(self, data, kind, status=200):
            self.send_response(status)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith(("text", "application/j")) else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dokumenten-Sortierer im Browser: http://127.0.0.1:{port}")
    return server
