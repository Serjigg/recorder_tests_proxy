"""Сквозные тесты: настоящий mitmproxy в отдельном процессе + демо-сервер."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest
import requests

from proxyrec.cli import main

PROJECT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recording:
    def __init__(self, cwd, name, *args):
        self.port = free_port()
        env = dict(os.environ, PYTHONPATH=str(PROJECT), PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "proxyrec", "record", name, "--port", str(self.port), *args],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8")
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(self.proc.stdout.read())
                time.sleep(0.2)
        else:
            raise RuntimeError("прокси не запустился")
        self.client = requests.Session()
        self.client.trust_env = False
        self.client.proxies = {"http": f"http://127.0.0.1:{self.port}"}

    def stop(self):
        self.proc.stdin.write("\n")
        self.proc.stdin.flush()
        out, _ = self.proc.communicate(timeout=20)
        return out


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def run(*args):
        code = 0
        try:
            main(list(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if isinstance(e.code, str):
                print(e.code)
        out = capsys.readouterr()
        return code, out.out + out.err
    return run


def load(tmp_path, name):
    return json.loads((tmp_path / "recordings" / name / "test.json").read_text(encoding="utf-8"))


def record_scenario(tmp_path, server, name):
    rec = Recording(tmp_path, name)
    c, u = rec.client, server.url
    tok = c.post(f"{u}/login", json={"user": "иван"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    oid = c.post(f"{u}/orders", json={"item": "книга", "qty": 2}, headers=h).json()["id"]
    assert c.get(f"{u}/orders/{oid}", headers=h).json()["status"] == "NEW"
    assert c.get(f"{u}/check?t={quote(tok, safe='')}").json() == {"ok": True}
    c.get(f"{u}/page")
    assert c.get(f"{u}/gz").json() == {"zipped": True, "n": 1}
    assert c.get(f"{u}/bin").content == bytes(range(256))
    c.get(f"{u}/app.js")  # статику не пишем
    out = rec.stop()
    assert "Записано шагов: 7" in out, out
    return tok, oid


def test_full_cycle(tmp_path, server, cli):
    tok, oid = record_scenario(tmp_path, server, "заказы")
    test = load(tmp_path, "заказы")
    steps = test["steps"]
    assert [s["request"]["url"].split("/", 3)[3] for s in steps] == [
        "login", "orders", f"orders/{oid}", f"check?t={quote(tok, safe='')}", "page", "gz", "bin"]
    assert json.loads(steps[1]["request"]["body"]) == {"item": "книга", "qty": 2}
    assert steps[2]["response"]["json"]["item"] == "книга"
    assert steps[5]["response"]["json"] == {"zipped": True, "n": 1}       # gzip распакован
    assert "Каталог" in steps[4]["response"]["body"]
    assert "body_b64" in steps[6]["response"]

    # Без корреляции новый токен не попадёт в запросы -> 401/403.
    code, out = cli("run")
    assert code == 1 and "FAIL" in out and "status: 201 -> 401" in out

    code, out = cli("correlate", "заказы", "--apply")
    assert code == 0 and "access_token_1" in out and "id_2" in out, out
    rules = {r["path"]: r for r in load(tmp_path, "заказы")["extract"]}
    assert rules["$.access_token"]["used_in"] == [2, 3, 4]
    assert rules["$.id"]["used_in"] == [3]

    code, out = cli("run")
    assert code == 1
    assert "$.ts" in out and "$.created" in out and "Запрос №" in out
    assert "status: 201 -> 401" not in out and "-> 403" not in out    # корреляция и куки работают

    cli("ignore", "заказы", "**.ts")
    cli("ignore", "заказы", "created", "--step", "2")
    cli("ignore", "заказы", r"№\d+", "--regex")
    code, out = cli("run", "заказы")
    assert code == 0 and "PASS  заказы (шагов: 7," in out, out

    # Поведение сервера изменилось.
    server.order_status = "PAID"
    server.html_title = "Товары"
    code, out = cli("run")
    assert code == 1
    assert '$.status: "NEW" -> "PAID"' in out
    assert "-<h1>Каталог</h1>" in out and "+<h1>Товары</h1>" in out

    code, out = cli("diff", "заказы")
    assert "шаг 3" in out and "шаг 5" in out

    code, out = cli("accept", "заказы", "--step", "3")
    assert "обновлены шаги [3]" in out
    code, out = cli("run")
    assert code == 1 and "шаг 3" not in out and "шаг 5" in out

    code, out = cli("accept", "заказы")
    assert "обновлены шаги [5]" in out
    code, out = cli("run")
    assert code == 0, out
    code, out = cli("run")   # повторно: эталон с исходными значениями корреляции не ломается
    assert code == 0, out

    test = load(tmp_path, "заказы")
    assert test["steps"][2]["response"]["json"]["id"] == oid
    assert test["steps"][2]["response"]["json"]["status"] == "PAID"

    code, out = cli("list")
    assert "заказы" in out and "PASS" in out
    code, out = cli("show", "заказы")
    assert "[7] GET" in out and "корреляция id_2" in out


def test_target_override_and_server_down(tmp_path, server, cli):
    rec = Recording(tmp_path, "t")
    rec.client.get(f"{server.url}/gz")
    rec.stop()
    port = server.url.rsplit(":", 1)[1]
    code, out = cli("run", "--target", f"http://localhost:{port}")
    assert code == 0, out
    code, out = cli("run", "--target", f"http://127.0.0.1:{free_port()}", "--timeout", "3")
    assert code == 1 and "ошибка запроса" in out and f"127.0.0.1:{port}" not in out


def test_reverse_mode(tmp_path, server, cli):
    rec = Recording(tmp_path, "rev", "--reverse", server.url)
    r = rec.client.__class__()
    r.trust_env = False
    assert r.get(f"http://127.0.0.1:{rec.port}/gz").json()["zipped"] is True
    rec.stop()
    steps = load(tmp_path, "rev")["steps"]
    assert len(steps) == 1 and steps[0]["request"]["url"] == f"{server.url}/gz"
    assert cli("run")[0] == 0


def test_host_filter_and_keep_static(tmp_path, server, cli):
    rec = Recording(tmp_path, "f1", "--host", "example.com")
    assert rec.client.get(f"{server.url}/gz").status_code == 200   # трафик проходит, но не пишется
    assert "Записано шагов: 0" in rec.stop()

    rec = Recording(tmp_path, "f2", "--host", "127.0.0.1", "--keep-static")
    rec.client.get(f"{server.url}/app.js")
    assert "Записано шагов: 1" in rec.stop()


def test_killed_recorder_keeps_steps(tmp_path, server):
    rec = Recording(tmp_path, "k")
    rec.client.get(f"{server.url}/gz")
    rec.client.get(f"{server.url}/page")
    rec.proc.kill()
    rec.proc.wait()
    assert len(load(tmp_path, "k")["steps"]) == 2


def test_cli_errors(tmp_path, server, cli):
    assert cli("run")[0] == 1                                         # нет тестов
    code, out = cli("record", "a/b")
    assert code == 1 and "недопустимое имя" in out
    rec = Recording(tmp_path, "x")
    rec.client.get(f"{server.url}/gz")
    rec.stop()
    code, out = cli("record", "x")
    assert code == 1 and "уже существует" in out
    code, out = cli("diff", "x")
    assert code == 1 and "нет результатов прогона" in out
    code, out = cli("run", "nope")
    assert code == 1 and "не найден" in out
    code, out = cli("ignore", "x", "a", "--step", "99")
    assert code == 1 and "несуществующий шаг" in out
    cli("run")
    code, out = cli("accept", "x", "x", "--step", "1")
    assert code == 1 and "только для одного теста" in out
    code, out = cli("accept", "x")
    assert "нечего обновлять" in out


def test_response_time_limits(tmp_path, server, cli):
    record_scenario(tmp_path, server, "t")
    cli("correlate", "t", "--apply")
    cli("ignore", "t", "**.ts")
    cli("ignore", "t", "**.created")
    cli("ignore", "t", r"№\d+", "--regex")
    test = load(tmp_path, "t")
    assert all(isinstance(s["elapsed_ms"], int) for s in test["steps"])   # время записи сохранено
    code, out = cli("show", "t")
    assert "при записи" in out

    server.delay = 0.4
    code, out = cli("run")
    assert code == 0                                       # лимита нет — медленно, но успешно

    # лимит на прогон: падает только медленный шаг 3, причина — время
    code, out = cli("run", "--max-time", "300")
    assert code == 1
    assert "упало 1 из 7 — по времени: 1;" in out, out
    assert "шаг 3:" in out and "> лимита 300 мс" in out and "ответ отличается" not in out
    run = json.loads((tmp_path / "recordings" / "t" / "last_run.json").read_text(encoding="utf-8"))
    s3 = run["steps"][2]
    assert s3["slow"] and s3["diffs"] == [] and s3["elapsed_ms"] >= 400 and s3["limit_ms"] == 300

    # лимит теста важнее параметра прогона, лимит шага важнее лимита теста
    cli("limit", "t", "1000")
    assert cli("run", "--max-time", "300")[0] == 0
    code, out = cli("limit", "t", "200", "--step", "3")
    assert "пересчитан: FAIL" in out                      # пересчёт по сохранённому времени
    assert cli("run")[0] == 1

    # время и ответ одновременно
    server.order_status = "PAID"
    code, out = cli("run")
    assert "по времени: 1, по ответу: 1" in out and "> лимита 200 мс" in out and '"NEW" -> "PAID"' in out
    # accept меняет эталон, но превышение времени остаётся ошибкой
    code, out = cli("accept", "t")
    assert "обновлены шаги [3]" in out
    run = json.loads((tmp_path / "recordings" / "t" / "last_run.json").read_text(encoding="utf-8"))
    assert run["steps"][2]["diffs"] == [] and run["steps"][2]["slow"] and not run["passed"]
    # медленный шаг без отличий accept по умолчанию не трогает
    assert "нечего обновлять" in cli("accept", "t")[1]

    code, out = cli("limit", "t", "off", "--step", "3")
    assert "снят" in out and "пересчитан: PASS" in out    # остаётся лимит теста 1000
    cli("limit", "t", "off")
    assert "лимит" not in cli("show", "t")[1].split("\n")[0]


def test_config_limit_and_request_error(tmp_path, server, cli):
    rec = Recording(tmp_path, "c")
    rec.client.get(f"{server.url}/gz")
    rec.stop()
    (tmp_path / "proxyrec.json").write_text('{"max_time_ms": 1}', encoding="utf-8")
    server.delay = 0
    code, out = cli("run")   # 1 мс почти наверняка превышен даже локально
    assert code == 1 and "по времени: 1" in out
    code, out = cli("run", "--target", f"http://127.0.0.1:{free_port()}", "--timeout", "2")
    assert "по ответу: 1" in out and "ошибка запроса" in out
