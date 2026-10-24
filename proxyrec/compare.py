"""Сравнение записанного (эталонного) ответа с фактическим.
Игнор JSON-пути (синтаксис — в jsonpath.py) игнорирует и всё его поддерево."""
import difflib
import json
import re

from .correlate import substitute
from .jsonpath import format_path, is_ignored, parse_path
from .storage import header

MAX_TEXT_DIFF_LINES = 40


def _short(value, limit=80) -> str:
    s = json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 3] + "..."


class Comparer:
    def __init__(self, ignore: dict, compare_headers, subs=None):
        # subs: значения корреляции (старое -> новое), в эталоне заменяются перед сравнением
        self.subs = subs or {}
        self.paths = [parse_path(p) for p in ignore.get("json_paths", [])]
        self.regexes = [re.compile(r) for r in ignore.get("regex", [])]
        ignored_headers = {h.lower() for h in ignore.get("headers", [])}
        self.headers = [h for h in compare_headers if h.lower() not in ignored_headers]

    def mask(self, s: str) -> str:
        for r in self.regexes:
            s = r.sub("<*>", s)
        return s

    def compare(self, expected: dict, actual: dict) -> list:
        diffs = []
        if expected["status"] != actual["status"]:
            diffs.append(f"status: {expected['status']} -> {actual['status']}")
        for name in self.headers:
            e, a = header(expected["headers"], name), header(actual["headers"], name)
            if e != a:
                diffs.append(f"header {name}: {e!r} -> {a!r}")
        diffs.extend(self.compare_body(expected, actual))
        return diffs

    def compare_body(self, expected, actual) -> list:
        if "json" in expected and "json" in actual:
            out = []
            self._json_diff(expected["json"], actual["json"], (), out)
            return out
        if "body_b64" in expected or "body_b64" in actual:
            if expected.get("body_b64") != actual.get("body_b64"):
                return ["body: бинарное содержимое отличается"]
            return []
        e = self.mask(substitute(_as_text(expected), self.subs))
        a = self.mask(_as_text(actual))
        if e == a:
            return []
        lines = list(difflib.unified_diff(e.splitlines(), a.splitlines(), "expected", "actual", lineterm="", n=1))
        if len(lines) > MAX_TEXT_DIFF_LINES:
            lines = lines[:MAX_TEXT_DIFF_LINES] + [f"... ещё {len(lines) - MAX_TEXT_DIFF_LINES} строк"]
        return ["body (текст отличается):"] + ["  " + l for l in lines]

    def _json_diff(self, exp, act, path, out):
        if is_ignored(path, self.paths):
            return
        where = format_path(path)
        if isinstance(exp, dict) and isinstance(act, dict):
            for k in list(exp) + [k for k in act if k not in exp]:
                sub = path + (k,)
                if k not in act:
                    if not is_ignored(sub, self.paths):
                        out.append(f"{format_path(sub)}: пропало (было {_short(exp[k])})")
                elif k not in exp:
                    if not is_ignored(sub, self.paths):
                        out.append(f"{format_path(sub)}: появилось {_short(act[k])}")
                else:
                    self._json_diff(exp[k], act[k], sub, out)
        elif isinstance(exp, list) and isinstance(act, list):
            if len(exp) != len(act):
                out.append(f"{where}: длина массива {len(exp)} -> {len(act)}")
            for i in range(min(len(exp), len(act))):
                self._json_diff(exp[i], act[i], path + (i,), out)
        elif isinstance(exp, str) and isinstance(act, str):
            if self.mask(substitute(exp, self.subs)) != self.mask(act):
                out.append(f"{where}: {_short(exp)} -> {_short(act)}")
        elif self.subs.get(str(exp), str(exp)) == str(act) and str(exp) in self.subs:
            return
        elif type(exp) is not type(act) or exp != act:
            out.append(f"{where}: {_short(exp)} -> {_short(act)}")


def _as_text(resp) -> str:
    if "json" in resp:
        return json.dumps(resp["json"], ensure_ascii=False, indent=2)
    return resp.get("body", "")
