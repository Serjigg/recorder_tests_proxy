"""Прогон теста: повторяем записанные запросы и сравниваем ответы с эталоном."""
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from .compare import Comparer
from .correlate import extract, substitute
from .storage import merge_ignore, now_iso, pack_body, unpack_body

SKIP_REQUEST_HEADERS = {"content-length", "host", "connection", "proxy-connection", "keep-alive",
                        "transfer-encoding", "te", "upgrade", "proxy-authorization"}


def _retarget(url, target):
    if not target:
        return url
    t, u = urlsplit(target), urlsplit(url)
    return urlunsplit((t.scheme, t.netloc, u.path, u.query, u.fragment))


def _apply_cookies(value, jar):
    parts = []
    for item in value.split(";"):
        name, eq, val = item.strip().partition("=")
        if eq and name in jar:
            val = jar[name]
        parts.append(f"{name}{eq}{val}")
    return "; ".join(parts)


def _build_request(req, subs, jar, target):
    url = _retarget(substitute(req["url"], subs), target)
    headers = {}
    for k, v in req["headers"]:
        if k.lower() in SKIP_REQUEST_HEADERS:
            continue
        v = substitute(v, subs)
        if k.lower() == "cookie":
            v = _apply_cookies(v, jar)
        headers[k] = f"{headers[k]}, {v}" if k in headers else v
    if "body_b64" in req:
        body = unpack_body(req)
    else:
        body = substitute(req.get("body", ""), subs).encode("utf-8")
    return url, headers, body or None


def _response_dict(r: requests.Response):
    return {
        "status": r.status_code,
        "headers": [[k, v] for k, v in r.raw.headers.items()],
        **pack_body(r.content, try_json=True),
    }


def check_step(cfg, test, step, actual, subs):
    """Извлекает значения корреляции в subs и сравнивает ответ с эталоном. Возвращает отличия."""
    diffs = []
    for rule in test.get("extract", []):
        if rule["step"] != step["id"]:
            continue
        new = extract(rule, actual)
        if new is None:
            diffs.append(f"корреляция '{rule['var']}': значение не найдено в ответе")
        elif new != rule["value"]:
            subs[rule["value"]] = new
    comparer = Comparer(merge_ignore(cfg.get("ignore"), test.get("ignore"), step.get("ignore")),
                        cfg.get("compare_headers", []), subs)
    return diffs + comparer.compare(step["response"], actual)


def time_limit(cfg, test, step, run_limit=None):
    """Лимит времени ответа, мс: шаг > тест > параметр прогона > proxyrec.json. None — без лимита."""
    for v in (step.get("max_time_ms"), test.get("max_time_ms"), run_limit, cfg.get("max_time_ms")):
        if v:
            return v
    return None


def evaluate(cfg, test, step, res, subs, run_limit=None):
    """Заполняет в результате шага: diffs (ответ), limit_ms/slow (время), passed."""
    if res.get("error"):
        res.update(diffs=[], slow=False, limit_ms=None, passed=False)
        return res
    diffs = check_step(cfg, test, step, res["actual"], subs)
    limit = time_limit(cfg, test, step, run_limit)
    elapsed = res.get("elapsed_ms")
    slow = bool(limit and elapsed is not None and elapsed > limit)
    res.update(diffs=diffs, limit_ms=limit, slow=slow, passed=not diffs and not slow)
    return res


def recheck(cfg, test, run):
    """Пересчитать прогон по текущим правилам и лимитам, не отправляя запросы заново."""
    steps = {s["id"]: s for s in test["steps"]}
    run["steps"] = [r for r in run["steps"] if r["id"] in steps]
    subs = dict(run.get("subs", {}))
    for res in run["steps"]:
        if res.get("actual") is not None or res.get("error"):
            evaluate(cfg, test, steps[res["id"]], res, subs, run.get("max_time_ms"))
    run["passed"] = all(r["passed"] for r in run["steps"])
    return run


def run_test(store, test, target=None, timeout=30.0, max_time_ms=None):
    cfg = store.config
    session = requests.Session()
    session.trust_env = False  # системный прокси не нужен
    subs, jar, results = {}, {}, []

    for step in test["steps"]:
        req = step["request"]
        url, headers, body = _build_request(req, subs, jar, target)
        res = {"id": step["id"], "method": req["method"], "url": url}
        results.append(res)
        started = time.perf_counter()
        try:
            # stream=False: тело читается внутри вызова, в замер входит и его загрузка.
            r = session.request(req["method"], url, headers=headers, data=body,
                                allow_redirects=False, timeout=timeout)
        except requests.RequestException as e:
            res.update(error=f"ошибка запроса: {e}", actual=None,
                       elapsed_ms=round((time.perf_counter() - started) * 1000))
            evaluate(cfg, test, step, res, subs, max_time_ms)
            continue
        res["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        session.cookies.clear()  # куки ведём сами в jar
        for c in r.raw.headers.getlist("Set-Cookie"):
            name, _, val = c.split(";", 1)[0].partition("=")
            jar[name.strip()] = val.strip()

        res["actual"] = _response_dict(r)
        evaluate(cfg, test, step, res, subs, max_time_ms)

    return {
        "test": test["name"],
        "run_at": now_iso(),
        "target": target,
        "max_time_ms": max_time_ms,
        "passed": all(r["passed"] for r in results),
        "subs": subs,
        "steps": results,
    }
