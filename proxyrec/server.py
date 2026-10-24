"""Веб-интерфейс: JSON API + одна HTML-страница (proxyrec/ui/index.html)."""
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import ops

UI_FILE = Path(__file__).parent / "ui" / "index.html"


def _int_or_none(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"ожидалось целое число миллисекунд, получено {v!r}")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, store):
        self.store = store
        self.recording = None  # RecordingSession
        self.lock = threading.Lock()

    # --- запись ---

    def record_status(self):
        s = self.recording
        if s is None:
            return {"active": False}
        title, hint = s.describe()
        return {
            "active": s.alive, "name": s.name, "title": title, "hint": hint,
            "steps": [{"id": st["id"], "method": st["request"]["method"], "url": st["request"]["url"],
                       "status": st["response"]["status"]} for st in list(s.test["steps"])],
        }

    def record_start(self, body):
        from .recorder import RecordingSession
        name = (body.get("name") or "").strip()
        self.store.check_name(name)
        with self.lock:
            if self.recording and self.recording.alive:
                raise ApiError(409, f"уже идёт запись '{self.recording.name}'")
            if self.store.exists(name) and not body.get("overwrite"):
                raise ApiError(409, f"тест '{name}' уже существует")
            hosts = [h.strip() for h in re.split(r"[,\s]+", body.get("hosts") or "") if h.strip()]
            session = RecordingSession(self.store, name, int(body.get("port") or 8888),
                                       body.get("listen_host") or "127.0.0.1",
                                       (body.get("reverse") or "").strip() or None, hosts,
                                       bool(body.get("keep_static")))
            session.start()
            self.recording = session
        return self.record_status()

    def record_stop(self):
        with self.lock:
            if not self.recording:
                raise ApiError(409, "запись не идёт")
            self.recording.stop()
            status = self.record_status()
            self.recording = None
        return status

    # --- маршруты ---

    def routes(self):
        T = r"/api/tests/(?P<name>[^/]+)"
        return [
            ("GET", r"/api/state", lambda m, q, b: {
                "tests": [ops.summary(self.store, n) for n in self.store.names()],
                "recording": self.record_status()}),
            ("GET", r"/api/record", lambda m, q, b: self.record_status()),
            ("POST", r"/api/record/start", lambda m, q, b: self.record_start(b)),
            ("POST", r"/api/record/stop", lambda m, q, b: self.record_stop()),
            ("POST", r"/api/run", lambda m, q, b: [
                ops.summary(self.store, n) for n in self._run_many(b)]),
            ("GET", T, lambda m, q, b: self._test(m["name"])),
            ("DELETE", T, lambda m, q, b: ops.delete_test(self.store, m["name"])),
            ("POST", T + "/run", lambda m, q, b: (
                ops.run(self.store, m["name"], b.get("target") or None, float(b.get("timeout") or 30),
                        _int_or_none(b.get("max_time_ms"))),
                self._test(m["name"]))[1]),
            ("POST", T + "/accept", lambda m, q, b: {
                "updated": ops.accept(self.store, m["name"], b.get("steps")), **self._test(m["name"])}),
            ("POST", T + "/ignore", lambda m, q, b: (
                ops.add_ignore(self.store, m["name"], b.get("kind"), b.get("pattern"), b.get("steps")),
                self._test(m["name"]))[1]),
            ("POST", T + "/limit", lambda m, q, b: (
                ops.set_limit(self.store, m["name"], _int_or_none(b.get("ms")), b.get("step")),
                self._test(m["name"]))[1]),
            ("POST", T + "/ignore/remove", lambda m, q, b: (
                ops.remove_ignore(self.store, m["name"], b.get("kind"), b.get("pattern"), b.get("step")),
                self._test(m["name"]))[1]),
            ("GET", T + "/candidates", lambda m, q, b: ops.candidates(
                self.store, m["name"], int(q.get("min_len", ["6"])[0]))),
            ("POST", T + "/extract", lambda m, q, b: (
                ops.add_extract(self.store, m["name"], b.get("rules") or []), self._test(m["name"]))[1]),
            ("POST", T + "/extract/remove", lambda m, q, b: (
                ops.remove_extract(self.store, m["name"], int(b.get("index", -1))), self._test(m["name"]))[1]),
            ("POST", T + "/steps/remove", lambda m, q, b: (
                ops.remove_steps(self.store, m["name"], b.get("ids") or []), self._test(m["name"]))[1]),
        ]

    def _run_many(self, body):
        names = body.get("names") or self.store.names()
        for n in names:
            ops.run(self.store, n, body.get("target") or None, float(body.get("timeout") or 30),
                    _int_or_none(body.get("max_time_ms")))
        return names

    def _test(self, name):
        if self.recording and self.recording.alive and self.recording.name == name:
            test = self.recording.test
        else:
            test = self.store.load(name)
        run = self.store.load_run(name) if self.store.run_path(name).exists() else None
        return {"test": test, "run": run, "config": self.store.config}


def make_handler(app):
    routes = [(meth, re.compile(f"^{pat}$"), fn) for meth, pat, fn in app.routes()]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body: bytes, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _handle(self, method):
            url = urlsplit(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            for meth, rx, fn in routes:
                m = rx.match(url.path)
                if m and meth == method:
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                        params = {k: unquote(v) for k, v in m.groupdict().items()}
                        if "name" in params:
                            app.store.check_name(params["name"])
                        result = fn(params, parse_qs(url.query), body)
                        return self._json(200, {"ok": True} if result is None else result)
                    except ApiError as e:
                        return self._json(e.status, {"error": str(e)})
                    except FileNotFoundError as e:
                        return self._json(404, {"error": str(e)})
                    except (ValueError, OSError, KeyError, TypeError) as e:
                        return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

        def log_message(self, *a):
            pass

    return Handler


def serve(store, port=8000, open_browser=True):
    app = App(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print(f"Интерфейс: {url}  (Ctrl+C — выход)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.recording and app.recording.alive:
            app.recording.stop()
        httpd.server_close()
