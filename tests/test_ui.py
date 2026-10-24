"""Интерфейс в headless Chrome (playwright). Пропускается, если playwright или Chrome недоступны.
Скриншоты: PROXYREC_SHOTS=<папка> python -m pytest tests/test_ui.py"""
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from proxyrec.server import App, make_handler
from proxyrec.storage import Store
from test_e2e import free_port

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


@pytest.fixture
def page():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception as e:
            pytest.skip(f"Chrome недоступен: {e}")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.errors = []
        page.on("console", lambda m: m.type == "error" and page.errors.append(m.text))
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        yield page
        browser.close()


@pytest.fixture
def ui_url(tmp_path):
    app = App(Store(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    if app.recording and app.recording.alive:
        app.recording.stop()
    httpd.shutdown()
    httpd.server_close()


def test_full_ui_flow(page, ui_url, server):
    shots = os.environ.get("PROXYREC_SHOTS")
    shot = (lambda n: page.screenshot(path=str(Path(shots) / f"{n}.png"), full_page=True)) if shots else (lambda n: None)
    idle = lambda: expect(page.locator("body.busy")).to_have_count(0)
    run_btn = lambda: page.get_by_role("button", name="▶ Прогнать", exact=True)
    badge = page.locator(".title-row .badge")
    proxy_port = free_port()

    page.goto(ui_url)
    expect(page.get_by_text("Тесты по записанному трафику")).to_be_visible()
    shot("01_home")

    # --- запись ---
    page.get_by_role("button", name="● Новая запись").first.click()
    page.fill("input[name=name]", "оформление_заказа")
    page.get_by_role("button", name="Reverse-прокси").click()
    assert page.input_value("input[name=name]") == "оформление_заказа"   # имя не теряется при смене режима
    page.get_by_role("button", name="HTTP-прокси").click()
    page.fill("input[name=port]", str(proxy_port))
    shot("02_record_form")
    page.get_by_role("button", name="● Начать запись").click()
    expect(page.get_by_text("Идёт запись: оформление_заказа")).to_be_visible()
    expect(page.locator(".test-item")).to_have_count(1)

    c = requests.Session()
    c.trust_env = False
    c.proxies = {"http": f"http://127.0.0.1:{proxy_port}"}
    tok = c.post(f"{server.url}/login", json={"user": "иван"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    oid = c.post(f"{server.url}/orders", json={"item": "книга"}, headers=h).json()["id"]
    c.get(f"{server.url}/orders/{oid}", headers=h)
    c.get(f"{server.url}/page")
    c.get(f"{server.url}/bin")
    expect(page.locator(".step")).to_have_count(5)
    expect(page.locator(".rec-chip")).to_contain_text("(5)")
    shot("03_recording")

    page.get_by_role("button", name="■ Остановить запись").click()
    expect(page.locator(".tab.active")).to_contain_text("Корреляция")   # сразу показывает кандидатов
    expect(page.locator(".cand")).to_have_count(2)
    shot("04_candidates")
    page.get_by_role("button", name="Добавить выбранные").click()
    expect(page.locator(".tab.active .count")).to_have_text("2")

    # --- прогон и разбор отличий ---
    page.locator(".tab", has_text="Шаги").click()
    run_btn().click()
    expect(badge).to_have_text("FAIL")
    expect(page.locator(".diffs")).to_contain_text("$.ts")
    shot("05_run_fail")

    page.locator(".diff-line", has_text="$.ts").get_by_role("button", name="игнорировать").click()
    expect(page.locator("#igPattern")).to_have_value("ts")
    page.select_option("#igScope", "")
    shot("06_ignore_dialog")
    page.locator("#igOk").click()
    expect(page.locator('.step[data-id="1"] .icon')).to_have_class("icon pass")   # пересчитано без прогона

    page.locator('.step[data-id="2"]').click()
    page.locator(".diff-line", has_text="$.created").get_by_role("button", name="игнорировать").click()
    page.locator("#igOk").click()
    expect(page.locator('.step[data-id="2"] .icon')).to_have_class("icon pass")
    page.locator('.step[data-id="4"]').click()
    expect(page.locator(".textdiff .del")).to_be_visible()
    page.get_by_role("button", name="игнор по regex").click()
    page.fill("#igPattern", r"№\d+")
    page.keyboard.press("Enter")                                          # Enter = «Добавить»
    expect(badge).to_have_text("PASS")

    page.locator(".tab", has_text="Игнор").click()
    rows = page.locator("table.list tr").count()
    page.get_by_role("button", name="+ Добавить").click()
    page.fill("#igPattern", "should.not.be.added")
    page.keyboard.press("Escape")                                         # Esc не добавляет правило
    page.wait_for_timeout(300)
    assert page.locator("table.list tr").count() == rows
    shot("07_ignore_tab")

    page.locator(".tab", has_text="Шаги").click()
    run_btn().click()
    idle()
    expect(badge).to_have_text("PASS")
    shot("08_pass")

    # --- сервер изменился: смотрим и принимаем ---
    server.order_status = "PAID"
    page.get_by_role("button", name="▶ Прогнать все").click()
    idle()
    expect(badge).to_have_text("FAIL")
    expect(page.locator(".diffs")).to_contain_text('"NEW" -> "PAID"')
    expect(page.locator(".test-item .badge.fail")).to_have_text("≠1")
    shot("09_changed")
    page.get_by_role("button", name="Принять фактический ответ").click()
    expect(badge).to_have_text("PASS")
    run_btn().click()
    idle()
    expect(badge).to_have_text("PASS")

    # --- ручная корреляция ---
    page.locator(".tab", has_text="Корреляция").click()
    form = page.locator("#extractForm")
    form.locator("input[name=var]").fill("loc")
    form.locator("select[name=step]").select_option("2")
    form.locator("select[name=from]").select_option("header")
    form.locator("input[name=what]").fill("Location")
    form.locator("input[name=regex]").fill(r"/orders/(\w+)")
    form.locator("button[type=submit]").click()
    expect(page.locator(".toast.err", has_text="уже коррелируется правилом 'id_2'")).to_be_visible()
    form.locator("input[name=var]").fill("tok6")
    form.locator("select[name=step]").select_option("1")
    form.locator("select[name=from]").select_option("regex")
    form.locator("input[name=what]").fill("")
    form.locator("input[name=regex]").fill(r'"access_token": "(.{6})')
    form.locator("button[type=submit]").click()
    expect(page.locator(".tab.active .count")).to_have_text("3")
    shot("10_corr")

    # --- удаление шага, перезагрузка, узкий экран, удаление теста ---
    page.locator(".tab", has_text="Шаги").click()
    page.locator('.step[data-id="5"]').click()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Удалить шаг").click()
    expect(page.locator(".step")).to_have_count(4)
    page.reload()
    expect(page.locator("h1")).to_have_text("оформление_заказа")
    page.set_viewport_size({"width": 700, "height": 900})
    expect(page.locator(".test-item")).to_be_visible()
    shot("11_narrow")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Удалить", exact=True).click()
    expect(page.get_by_text("Тесты по записанному трафику")).to_be_visible()

    # --- занятый порт ---
    page.get_by_role("button", name="● Новая запись").first.click()
    page.fill("input[name=name]", "x")
    page.fill("input[name=port]", ui_url.rsplit(":", 1)[1].strip("/"))
    page.get_by_role("button", name="● Начать запись").click()
    expect(page.locator(".toast.err", has_text="занят")).to_be_visible()

    # в консоли — только ожидаемые 400 (дубликат правила, занятый порт)
    assert [e for e in page.errors if "status of 400" not in e] == []


def test_response_time_ui(page, ui_url, server):
    shots = os.environ.get("PROXYREC_SHOTS")
    shot = (lambda n: page.screenshot(path=str(Path(shots) / f"{n}.png"), full_page=True)) if shots else (lambda n: None)
    idle = lambda: expect(page.locator("body.busy")).to_have_count(0)
    badge = page.locator(".title-row .badge")
    proxy_port = free_port()

    page.goto(ui_url)
    page.get_by_role("button", name="● Новая запись").first.click()
    page.fill("input[name=name]", "время")
    page.fill("input[name=port]", str(proxy_port))
    page.get_by_role("button", name="● Начать запись").click()
    expect(page.get_by_text("Идёт запись: время")).to_be_visible()
    c = requests.Session()
    c.trust_env = False
    c.proxies = {"http": f"http://127.0.0.1:{proxy_port}"}
    tok = c.post(f"{server.url}/login", json={}).json()["access_token"]
    oid = c.post(f"{server.url}/orders", json={"a": 1}, headers={"Authorization": f"Bearer {tok}"}).json()["id"]
    c.get(f"{server.url}/orders/{oid}", headers={"Authorization": f"Bearer {tok}"})
    expect(page.locator(".step")).to_have_count(3)
    page.get_by_role("button", name="■ Остановить запись").click()
    expect(page.locator(".cand")).to_have_count(2)
    page.get_by_role("button", name="Добавить выбранные").click()
    idle()
    page.locator(".tab", has_text="Шаги").click()
    expect(page.locator('.step[data-id="1"] .ms')).to_contain_text("мс")      # время записи в списке

    # медленный, но правильный ответ: при лимите прогона падает только по времени
    page.request.post(ui_url + f"api/tests/{'время'}/ignore", data={"kind": "json_paths", "pattern": "**.ts"})
    page.request.post(ui_url + f"api/tests/{'время'}/ignore", data={"kind": "json_paths", "pattern": "**.created"})
    server.delay = 0.4
    page.fill("#maxTime", "300")
    page.get_by_role("button", name="▶ Прогнать", exact=True).click()
    idle()
    expect(badge).to_have_text("FAIL")
    expect(page.locator(".reason.slow")).to_contain_text("превышено время: 1 шаг")
    expect(page.locator(".reason.fail")).to_have_count(0)
    expect(page.locator(".step.active")).to_have_attribute("data-id", "3")
    expect(page.locator(".diffs.slow")).to_contain_text("больше лимита 300 мс (задан при прогоне)")
    expect(page.locator('.step[data-id="3"] .ms.t-bad')).to_be_visible()
    expect(page.locator(".test-item .badge.slow")).to_have_text("⏱1")
    expect(page.get_by_role("button", name="Принять все упавшие")).to_have_count(0)   # принимать нечего
    shot("20_slow")

    # лимит шага 1000 мс: пересчёт без прогона -> PASS
    page.locator(".diffs.slow").get_by_role("button", name="Изменить лимит").click()
    expect(page.locator("#limScope")).to_have_value("3")
    page.fill("#limMs", "1000")
    shot("21_limit_dialog")
    page.keyboard.press("Enter")
    idle()
    expect(badge).to_have_text("PASS")
    expect(page.locator(".timing")).to_contain_text("1000 мс (лимит шага)")

    # и время, и ответ
    server.order_status = "PAID"
    page.locator(".timing").get_by_role("button", name="Изменить лимит шага").click()
    page.fill("#limMs", "100")
    page.locator("#limitDlg .btn.primary").click()
    idle()
    page.get_by_role("button", name="▶ Прогнать", exact=True).click()
    idle()
    expect(page.locator(".reason.slow")).to_be_visible()
    expect(page.locator(".reason.fail")).to_contain_text("ответ отличается: 1 шаг")
    expect(page.locator(".diffs")).to_have_count(2)
    shot("22_both")
    page.get_by_role("button", name="Принять фактический ответ").click()
    idle()
    expect(page.locator(".reason.fail")).to_have_count(0)                     # ответ принят,
    expect(page.locator(".reason.slow")).to_be_visible()                     # время — всё ещё ошибка

    # лимит теста через ссылку в подзаголовке, снятие лимита шага
    page.locator(".sub a[data-action=limit-edit]").click()
    expect(page.locator("#limScope")).to_have_value("")
    page.fill("#limMs", "5000")
    page.locator("#limitDlg .btn.primary").click()
    idle()
    expect(page.locator(".sub")).to_contain_text("лимит времени теста: 5000 мс")
    page.locator(".timing").get_by_role("button", name="Изменить лимит шага").click()
    page.fill("#limMs", "")
    page.locator("#limitDlg .btn.primary").click()
    idle()
    expect(badge).to_have_text("PASS")
    expect(page.locator(".timing")).to_contain_text("5000 мс (лимит теста)")
    assert [e for e in page.errors if "status of 400" not in e] == []
