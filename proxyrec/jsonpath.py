"""JSON-пути: data.items[0].id, data.items[*].id, *.id, **.timestamp
  *    — любой один ключ или индекс
  [*]  — любой индекс массива
  **   — любое число уровней (в т.ч. ноль)
"""
import re

_TOKEN_RE = re.compile(r"\[(\d+|\*)\]|([^.\[\]]+)")


def parse_path(pattern: str):
    pattern = pattern.strip()
    if pattern.startswith("$"):
        pattern = pattern[1:]
    tokens = []
    for m in _TOKEN_RE.finditer(pattern):
        idx, key = m.groups()
        if idx is not None:
            tokens.append("*" if idx == "*" else int(idx))
        else:
            tokens.append(key)
    return tokens


def format_path(path) -> str:
    out = "$"
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else f".{seg}"
    return out


def _match(path, pat) -> bool:
    if not pat:
        return not path
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_match(path[i:], rest) for i in range(len(path) + 1))
    if not path:
        return False
    if head == "*" or head == path[0]:
        return _match(path[1:], rest)
    return False


def is_ignored(path, patterns) -> bool:
    return any(_match(path[:k], pat) for pat in patterns for k in range(len(path) + 1))
