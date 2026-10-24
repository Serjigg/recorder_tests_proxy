"""API веб-интерфейса: настоящий HTTP-сервер в потоке, без браузера."""
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest
import requests

from proxyrec.server import App, make_handler
from proxyrec.storage import Store
from test_e2e import free_port


@pytest.fixture
def ui(tmp_path):
    app = App(Store(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    s = requests.Session()
    s.trust_env = False

    def call(method, path, body=None, status=200):
        r = s.request(method, base + path, json=body)
        assert r.status_code == status, (r.status_code, r.text)
        return r.json()
    call.base, call.app = base, app
    yield call
    if app.recording and app.recording.alive:
        app.recording.stop()
    httpd.shutdown()
    httpd.server_close()


def test_page_served(ui):
    r = requests.get(ui.base + "/", proxies={"http": None})
    assert r.status_code == 200 and "<title>proxyrec</title>" in r.text


def test_record_run_ignore_accept_via_api(ui, server):
    port = free_port()
    rec = ui("POST", "/api/record/start", {"name": "тест", "port": port})
    assert rec["active"] and str(port) in rec["title"]
    ui("POST", "/api/record/start", {"name": "другой", "port": free_port()}, status=409)

    c = requests.Session()
    c.trust_env = False
    c.proxies = {"http": f"http://127.0.0.1:{port}"}
    tok = c.post(f"{server.url}/login", json={}).json()["access_token"]
    c.get(f"{server.url}/check?t={quote(tok, safe='')}")
    assert len(ui("GET", "/api/record")["steps"]) == 2
    assert len(ui("GET", f"/api/tests/{quote('тест')}")["test"]["steps"]) == 2   # видно во время записи

    stopped = ui("POST", "/api/record/stop")
    assert stopped["name"] == "тест" and len(stopped["steps"]) == 2
    ui("POST", "/api/record/stop", status=409)

    T = f"/api/tests/{quote('тест')}"
    cands = ui("GET", T + "/candidates")
    assert [c["path"] for c in cands] == ["$.access_token"]
    ui("POST", T + "/extract", {"rules": cands})
    ui("POST", T + "/extract", {"rules": cands}, status=400)          # дубликат

    d = ui("POST", T + "/run", {})
    assert not d["run"]["passed"] and any("$.ts" in x for x in d["run"]["steps"][0]["diffs"])
    d = ui("POST", T + "/ignore", {"kind": "json_paths", "pattern": "ts", "steps": [1]})
    assert d["run"]["passed"]                                          # пересчитано без повторного прогона
    assert d["test"]["steps"][0]["ignore"]["json_paths"] == ["ts"]
    d = ui("POST", T + "/ignore/remove", {"kind": "json_paths", "pattern": "ts", "step": 1})
    assert not d["run"]["passed"]
    ui("POST", T + "/ignore", {"kind": "regex", "pattern": "([", "steps": None}, status=400)
    ui("POST", T + "/ignore", {"kind": "json_paths", "pattern": "x", "steps": [99]}, status=400)

    d = ui("POST", T + "/accept", {"steps": [1]})
    assert d["updated"] == [1] and d["run"]["passed"]
    assert not ui("POST", T + "/run", {})["run"]["passed"]           # ts меняется всегда
    ui("POST", T + "/ignore", {"kind": "json_paths", "pattern": "**.ts"})
    assert ui("POST", T + "/run", {})["run"]["passed"]

    d = ui("POST", T + "/extract", {"rules": [{"var": "t2", "step": 1, "from": "header", "name": "X-Nope"}]}, status=400)
    assert "не найдено" in d["error"]

    d = ui("POST", T + "/steps/remove", {"ids": [2]})
    assert [s["id"] for s in d["test"]["steps"]] == [1] and [s["id"] for s in d["run"]["steps"]] == [1]

    state = ui("GET", "/api/state")
    assert state["tests"][0]["name"] == "тест" and state["tests"][0]["status"] == "pass"
    ui("DELETE", T)
    assert ui("GET", "/api/state")["tests"] == []
    ui("GET", T, status=404)


def test_api_errors(ui):
    ui("GET", "/api/tests/nope", status=404)
    ui("GET", "/api/tests/..%2F..%2Fetc", status=400)                  # имя проверяется
    ui("POST", "/api/record/start", {"name": "a b"}, status=400)
    ui("GET", "/api/unknown", status=404)
    busy = free_port()
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", busy))
        s.listen()
        d = ui("POST", "/api/record/start", {"name": "x", "port": busy}, status=400)
    assert "занят" in d["error"]
    assert not ui("GET", "/api/record")["active"]


def test_limit_api(ui, server):
    port = free_port()
    ui("POST", "/api/record/start", {"name": "lim", "port": port})
    c = requests.Session()
    c.trust_env = False
    c.proxies = {"http": f"http://127.0.0.1:{port}"}
    c.get(f"{server.url}/gz")
    ui("POST", "/api/record/stop")
    d = ui("POST", "/api/tests/lim/run", {"max_time_ms": "1"})
    s = d["run"]["steps"][0]
    assert d["run"]["max_time_ms"] == 1 and s["limit_ms"] == 1 and isinstance(s["elapsed_ms"], int)
    d = ui("POST", "/api/tests/lim/limit", {"ms": 60000})
    assert d["test"]["max_time_ms"] == 60000 and d["run"]["passed"]
    d = ui("POST", "/api/tests/lim/limit", {"ms": 1, "step": 1})
    assert d["test"]["steps"][0]["max_time_ms"] == 1 and not d["run"]["passed"]
    d = ui("POST", "/api/tests/lim/limit", {"ms": None, "step": 1})
    assert "max_time_ms" not in d["test"]["steps"][0] and d["run"]["passed"]
    ui("POST", "/api/tests/lim/limit", {"ms": "abc"}, status=400)
    ui("POST", "/api/tests/lim/limit", {"ms": -5}, status=400)
    ui("POST", "/api/tests/lim/limit", {"ms": 10, "step": 9}, status=400)
    assert ui("GET", "/api/state")["tests"][0]["max_time_ms"] == 60000
