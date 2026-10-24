"""Демо-сервер для сквозных тестов: токены, куки, gzip, HTML, бинарь, кириллица."""
import base64
import gzip
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


class State:
    def __init__(self):
        self.tokens, self.orders = set(), {}
        self.order_status = "NEW"
        self.html_title = "Каталог"
        self.page_hits = 0
        self.delay = 0.0  # задержка ответа GET /orders/<id>, с


class Handler(BaseHTTPRequestHandler):
    state: State

    def _send(self, code, body: bytes, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _authed(self):
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
        cookies = dict(p.strip().partition("=")[::2] for p in self.headers.get("Cookie", "").split(";") if p.strip())
        return tok in self.state.tokens and cookies.get("sid") == tok[:6]

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/login":
            # Токен со спецсимволами: + / = должны правильно подставляться и в url-encoded виде.
            tok = base64.b64encode(os.urandom(18) + b"\xfb\xff").decode()
            self.state.tokens.add(tok)
            return self._json(200, {"access_token": tok, "ts": time.time()},
                              [("Set-Cookie", f"sid={tok[:6]}; Path=/"), ("Set-Cookie", "theme=dark; Path=/")])
        if self.path == "/orders":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            oid = uuid.uuid4().hex[:10]
            self.state.orders[oid] = json.loads(raw)
            return self._json(201, {"id": oid, "created": time.time()}, [("Location", f"/orders/{oid}")])
        self._json(404, {})

    def do_GET(self):
        url = urlsplit(self.path)
        st = self.state
        if url.path.startswith("/orders/"):
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            oid = url.path.rsplit("/", 1)[1]
            time.sleep(st.delay)
            if oid not in st.orders:
                return self._json(404, {"error": "not found"})
            return self._json(200, {**st.orders[oid], "id": oid, "status": st.order_status})
        if url.path == "/check":
            ok = parse_qs(url.query).get("t", [""])[0] in st.tokens
            return self._json(200 if ok else 403, {"ok": ok})
        if url.path == "/page":
            st.page_hits += 1  # меняется при каждом запросе, как время или номер сессии
            html = f"<h1>{st.html_title}</h1>\n<p>Запрос №{st.page_hits}</p>\n"
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if url.path == "/gz":
            return self._send(200, gzip.compress(json.dumps({"zipped": True, "n": 1}).encode()),
                              "application/json", [("Content-Encoding", "gzip")])
        if url.path == "/bin":
            return self._send(200, bytes(range(256)), "application/octet-stream")
        if url.path == "/app.js":
            return self._send(200, b"console.log(1)", "application/javascript")
        self._json(404, {"error": "no route"})

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    state = State()
    handler = type("H", (Handler,), {"state": state})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()
    srv.server_close()
