"""Корреляция: значения из ответов, которые клиент потом отправляет в запросах
(токены, id созданных объектов и т.п.). При прогоне такие значения берутся из
нового ответа и подставляются в последующие запросы вместо записанных."""
import json
import re
from urllib.parse import quote

from .jsonpath import format_path, parse_path
from .storage import header


def extract(rule: dict, resp: dict):
    """Достаёт значение по правилу из ответа. None — если не нашлось."""
    src = rule.get("from", "json")
    if src == "json":
        node = resp.get("json")
        for seg in parse_path(rule["path"]):
            try:
                node = node[seg]
            except (KeyError, IndexError, TypeError):
                return None
        if isinstance(node, (dict, list)) or node is None:
            return None
        return str(node).lower() if isinstance(node, bool) else str(node)
    if src == "header":
        text = header(resp["headers"], rule["name"])
    elif src == "regex":
        text = json.dumps(resp["json"], ensure_ascii=False) if "json" in resp else resp.get("body", "")
    else:
        raise ValueError(f"неизвестный источник извлечения: {src}")
    if text is None:
        return None
    if "regex" in rule:
        m = re.search(rule["regex"], text)
        if not m:
            return None
        return m.group(1) if m.groups() else m.group(0)
    return text


def substitute(text: str, subs: dict) -> str:
    for old, new in subs.items():
        text = text.replace(old, new)
        q_old, q_new = quote(old, safe=""), quote(new, safe="")
        if q_old != old:
            text = text.replace(q_old, q_new)
    return text


def _request_text(step) -> str:
    req = step["request"]
    parts = [req["url"]] + [v for _, v in req["headers"]] + [req.get("body", "")]
    return "\n".join(parts)


def _leaves(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, path + (i,))
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        yield path, str(node)


def _var_name(path, step_id):
    keys = [p for p in path if isinstance(p, str)]
    base = re.sub(r"\W+", "_", keys[-1] if keys else "value").strip("_") or "value"
    return f"{base}_{step_id}"


def find_candidates(test: dict, min_len: int = 6) -> list:
    """Значения из JSON-ответов, которых не было в запросах до этого ответа,
    но которые встречаются в запросах после него."""
    steps = test["steps"]
    texts = [_request_text(s) for s in steps]
    known = {r.get("value") for r in test.get("extract", [])}
    found = []
    for i, step in enumerate(steps):
        resp = step["response"]
        if "json" not in resp:
            continue
        for path, value in _leaves(resp["json"]):
            if len(value) < min_len or value in known:
                continue
            forms = {value, quote(value, safe="")}
            if any(f in t for t in texts[: i + 1] for f in forms):
                continue  # клиент знал значение раньше — его сгенерировал не сервер
            used_in = [steps[j]["id"] for j in range(i + 1, len(steps)) if any(f in texts[j] for f in forms)]
            if used_in:
                known.add(value)
                found.append({
                    "var": _var_name(path, step["id"]),
                    "step": step["id"],
                    "from": "json",
                    "path": format_path(path),
                    "value": value,
                    "used_in": used_in,
                })
    return found
