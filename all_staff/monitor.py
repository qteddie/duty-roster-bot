"""Live training monitor for ``train-lines``.

``python -m all_staff.cli train-lines --monitor`` starts it next to the training;
``python -m all_staff.cli monitor --run models/.../recognizer.json`` watches a run
started elsewhere (another terminal, ``nohup``). The page polls ``/api/status``,
which simply returns the JSON the trainer rewrites every ~1.5 s, and its stop
button creates ``<output>.stop``: training ends after the current step and keeps
the best checkpoint.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("monitor_page.html")
DEFAULT_PORT = 8778


def make_handler(run: Path):
    class Handler(BaseHTTPRequestHandler):
        def send(self, payload: bytes, content_type="application/json; charset=utf-8", status=200):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(payload)

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                return self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            if route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT); self.end_headers(); return
            if route == "/api/status":
                try:
                    payload = json.loads(run.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    payload = {"status": "waiting", "detail": "等待訓練開始寫入進度"}
                except json.JSONDecodeError:
                    payload = {"status": "waiting", "detail": "讀取中"}
                payload["run_file"] = str(run)
                payload["stop_requested"] = run.with_suffix(".stop").exists()
                return self.send(json.dumps(payload, ensure_ascii=False).encode())
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/stop":
                return self.send_error(HTTPStatus.NOT_FOUND)
            run.with_suffix(".stop").touch()
            return self.send(b'{"ok":true}')

        def log_message(self, fmt, *args):
            pass

    return Handler


def start(run: Path, host="127.0.0.1", port=DEFAULT_PORT, open_browser=True, background=True):
    """Serve the monitor. With ``background=True`` returns the server (daemon thread)."""
    server = ThreadingHTTPServer((host, port), make_handler(run))
    url = f"http://{host}:{port}"
    print(f"訓練監控：{url}（進度檔 {run}）", flush=True)
    if open_browser:
        webbrowser.open(url)
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return server
