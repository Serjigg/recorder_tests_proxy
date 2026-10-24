"""Операции над тестами, общие для CLI и веб-интерфейса."""
import json
import re
import shutil

from .correlate import extract, find_candidates, substitute
from .runner import recheck, run_test

IGNORE_KINDS = ("json_paths", "regex", "headers")


def summary(store, name):
    test = store.load(name)
    out = {"name": name, "steps": len(test["steps"]), "created": test.get("created"),
           "max_time_ms": test.get("max_time_ms"),
           "status": None, "run_at": None, "failed_steps": 0, "failed_time": 0, "failed_response": 0}
    if store.run_path(name).exists():
        run = store.load_run(name)
        out.update(status="pass" if run["passed"] else "fail", run_at=run["run_at"],
                   failed_steps=sum(not s["passed"] for s in run["steps"]),
                   failed_time=sum(bool(s.get("slow")) for s in run["steps"]),
                   failed_response=sum(bool(s.get("diffs") or s.get("error")) for s in run["steps"]))
    return out


def run(store, name, target=None, timeout=30.0, max_time_ms=None):
    result = run_test(store, store.load(name), target, timeout, max_time_ms)
    store.save_run(name, result)
    return result


def accept(store, name, step_ids=None):
    """Фактические ответы последнего прогона -> эталон.
    По умолчанию — шаги, где отличается ответ (медленный, но верный ответ принимать незачем)."""
    test = store.load(name)
    run_ = store.load_run(name)
    actual = {s["id"]: s for s in run_["steps"]}
    # Значения корреляции из прогона возвращаем к записанным, иначе подстановка сломается.
    restore = {new: old for old, new in run_.get("subs", {}).items()}
    updated = []
    for step in test["steps"]:
        r = actual.get(step["id"])
        if r is None or r.get("actual") is None:
            continue
        if (step_ids and step["id"] in step_ids) or (not step_ids and r.get("diffs")):
            step["response"] = json.loads(substitute(json.dumps(r["actual"], ensure_ascii=False), restore))
            updated.append(step["id"])
    if updated:
        store.save(test)
        refresh_run(store, name)  # превышение времени при этом остаётся ошибкой
    return updated


def set_limit(store, name, ms=None, step_id=None):
    """Лимит времени ответа (мс) для теста или шага; None/0 — снять."""
    if ms is not None and (not isinstance(ms, int) or ms < 0):
        raise ValueError("лимит — целое число миллисекунд")
    test = store.load(name)
    target = _ignore_targets(test, [step_id] if step_id else None)[0]
    if ms:
        target["max_time_ms"] = ms
    else:
        target.pop("max_time_ms", None)
    store.save(test)
    return refresh_run(store, name)


def refresh_run(store, name):
    """После смены правил пересчитываем последний прогон по сохранённым ответам."""
    if not store.run_path(name).exists():
        return None
    run_ = recheck(store.config, store.load(name), store.load_run(name))
    store.save_run(name, run_)
    return run_


def _ignore_targets(test, step_ids):
    if not step_ids:
        return [test]
    targets = [s for s in test["steps"] if s["id"] in step_ids]
    if len(targets) != len(set(step_ids)):
        raise ValueError("указан несуществующий шаг")
    return targets


def _check_ignore(kind, pattern):
    if kind not in IGNORE_KINDS:
        raise ValueError(f"неизвестный вид игнора: {kind}")
    if not pattern:
        raise ValueError("пустой шаблон")
    if kind == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"некорректное регулярное выражение: {e}")


def add_ignore(store, name, kind, pattern, step_ids=None):
    _check_ignore(kind, pattern)
    test = store.load(name)
    for t in _ignore_targets(test, step_ids):
        lst = t.setdefault("ignore", {}).setdefault(kind, [])
        if pattern not in lst:
            lst.append(pattern)
    store.save(test)
    return refresh_run(store, name)


def remove_ignore(store, name, kind, pattern, step_id=None):
    test = store.load(name)
    target = _ignore_targets(test, [step_id] if step_id else None)[0]
    lst = target.get("ignore", {}).get(kind, [])
    if pattern in lst:
        lst.remove(pattern)
        store.save(test)
    return refresh_run(store, name)


def candidates(store, name, min_len=6):
    return find_candidates(store.load(name), min_len)


def add_extract(store, name, rules):
    test = store.load(name)
    known = {r.get("value") for r in test["extract"]}
    steps = {s["id"]: s for s in test["steps"]}
    for r in rules:
        if not {"var", "step"} <= r.keys() or r["step"] not in steps:
            raise ValueError("в правиле нужны var и существующий step")
        if not r.get("value"):
            # Записанное значение берём из эталонного ответа шага.
            r["value"] = extract(r, steps[r["step"]]["response"])
            if r["value"] is None:
                raise ValueError(f"в ответе шага {r['step']} значение по правилу не найдено")
        if r["value"] in known:
            other = next((x["var"] for x in test["extract"] if x.get("value") == r["value"]), r["var"])
            raise ValueError(f"значение {r['value']!r} уже коррелируется правилом '{other}'")
        test["extract"].append(r)
        known.add(r["value"])
    store.save(test)


def remove_extract(store, name, index):
    test = store.load(name)
    if not 0 <= index < len(test["extract"]):
        raise ValueError("нет такого правила")
    del test["extract"][index]
    store.save(test)


def remove_steps(store, name, step_ids):
    # id шагов не перенумеровываем: на них ссылаются правила корреляции.
    test = store.load(name)
    test["steps"] = [s for s in test["steps"] if s["id"] not in step_ids]
    test["extract"] = [r for r in test["extract"] if r["step"] not in step_ids]
    store.save(test)
    refresh_run(store, name)


def delete_test(store, name):
    store.check_name(name)
    store.load(name)  # FileNotFoundError, если нет
    shutil.rmtree(store.dir / name)
