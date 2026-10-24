"""Командная строка: record / list / show / correlate / run / diff / accept / ignore / limit / ui."""
import argparse
import sys

from . import ops
from .storage import Store

MAX_DIFFS_SHORT = 10


def _ms(v):
    return "—" if v is None else f"{v} мс"


def _print_step_diffs(step, limit=None):
    print(f"    шаг {step['id']}: {step['method']} {step['url']}  ({_ms(step.get('elapsed_ms'))})")
    if step.get("error"):
        print(f"      {step['error']}")
    if step.get("slow"):
        print(f"      время отклика: {step['elapsed_ms']} мс > лимита {step['limit_ms']} мс")
    diffs = step.get("diffs", [])
    if diffs:
        print("      ответ отличается:")
    shown = diffs if limit is None else diffs[:limit]
    for d in shown:
        print(f"        {d}")
    if len(shown) < len(diffs):
        print(f"        ... ещё {len(diffs) - len(shown)} (подробно: diff {step.get('test', '')})")


def _fail_reasons(steps):
    slow = sum(bool(s.get("slow")) for s in steps)
    resp = sum(bool(s.get("diffs") or s.get("error")) for s in steps)
    parts = ([f"по времени: {slow}"] if slow else []) + ([f"по ответу: {resp}"] if resp else [])
    return ", ".join(parts)


def cmd_record(store, a):
    from .recorder import record
    store.check_name(a.name)
    if store.exists(a.name) and not a.overwrite:
        sys.exit(f"тест '{a.name}' уже существует, используйте --overwrite")
    record(store, a.name, a.port, a.listen_host, a.reverse, a.host, a.keep_static)
    print(f"Дальше: python -m proxyrec correlate {a.name}  (найти токены/id для подстановки)")


def cmd_list(store, a):
    for name in store.names():
        s = ops.summary(store, name)
        status = {"pass": "PASS", "fail": "FAIL", None: ""}[s["status"]]
        print(f"{name:30} шагов: {s['steps']:3}  {status}")


def cmd_show(store, a):
    test = store.load(a.name)
    if test.get("max_time_ms"):
        print(f"лимит времени ответа: {test['max_time_ms']} мс")
    for s in test["steps"]:
        limit = f", лимит {s['max_time_ms']} мс" if s.get("max_time_ms") else ""
        print(f"[{s['id']}] {s['request']['method']} {s['request']['url']} -> {s['response']['status']}"
              f"  (при записи {_ms(s.get('elapsed_ms'))}{limit})")
    for r in test.get("extract", []):
        print(f"корреляция {r['var']}: шаг {r['step']} {r.get('from', 'json')} {r.get('path', r.get('name', ''))}")


def cmd_correlate(store, a):
    found = ops.candidates(store, a.name, a.min_len)
    if not found:
        print("Кандидатов для корреляции не найдено.")
        return
    for c in found:
        print(f"{c['var']}: шаг {c['step']} {c['path']} = {c['value'][:60]!r} -> используется в шагах {c['used_in']}")
    if a.apply:
        ops.add_extract(store, a.name, found)
        print(f"Добавлено правил: {len(found)}")
    else:
        print("\nЧтобы сохранить правила, повторите с --apply (лишние потом можно удалить из test.json).")


def cmd_run(store, a):
    names = a.names or store.names()
    if not names:
        sys.exit("нет тестов")
    failed = 0
    for name in names:
        run = ops.run(store, name, a.target, a.timeout, a.max_time)
        steps = run["steps"]
        total = sum(s.get("elapsed_ms") or 0 for s in steps)
        if run["passed"]:
            print(f"PASS  {name} (шагов: {len(steps)}, {total} мс)")
            continue
        failed += 1
        bad = [s for s in steps if not s["passed"]]
        print(f"FAIL  {name} (упало {len(bad)} из {len(steps)} — {_fail_reasons(bad)}; {total} мс)")
        for s in bad:
            s["test"] = name
            _print_step_diffs(s, None if a.verbose else MAX_DIFFS_SHORT)
    print(f"\nИтого: {len(names) - failed} прошло, {failed} упало")
    if failed:
        print("Если новый ответ верный: python -m proxyrec accept <имя>; "
              "если допустимо дольше: python -m proxyrec limit <имя> <мс>")
        sys.exit(1)


def cmd_diff(store, a):
    run = store.load_run(a.name)
    print(f"Прогон {run['run_at']}: {'PASS' if run['passed'] else 'FAIL'}")
    for s in run["steps"]:
        if not s["passed"]:
            _print_step_diffs(s)


def cmd_accept(store, a):
    if a.step and len(a.names) != 1:
        sys.exit("--step можно указывать только для одного теста")
    for name in a.names:
        updated = ops.accept(store, name, a.step)
        print(f"{name}: обновлены шаги {updated}" if updated else f"{name}: нечего обновлять")


def cmd_ignore(store, a):
    kind = "regex" if a.regex else "headers" if a.header else "json_paths"
    run = ops.add_ignore(store, a.name, kind, a.pattern, a.step)
    where = f"шаги {a.step}" if a.step else "весь тест"
    print(f"{a.name}: игнор {kind} {a.pattern!r} ({where})")
    if run:
        print(f"Последний прогон пересчитан по новым правилам: {'PASS' if run['passed'] else 'FAIL'}")


def cmd_limit(store, a):
    ms = None if a.ms.lower() in ("off", "0", "нет") else int(a.ms)
    run = ops.set_limit(store, a.name, ms, a.step)
    where = f"шаг {a.step}" if a.step else "весь тест"
    print(f"{a.name}: лимит времени ответа {_ms(ms) if ms else 'снят'} ({where})")
    if run:
        print(f"Последний прогон пересчитан: {'PASS' if run['passed'] else 'FAIL'}")


def cmd_ui(store, a):
    from .server import serve
    serve(store, a.port, not a.no_browser)


def build_parser():
    p = argparse.ArgumentParser(prog="proxyrec", description="Запись HTTP-трафика в тесты и их прогон")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="записать новый тест")
    r.add_argument("name")
    r.add_argument("--port", type=int, default=8888)
    r.add_argument("--listen-host", default="127.0.0.1", help="0.0.0.0 — принимать с других машин")
    r.add_argument("--reverse", metavar="URL", help="режим reverse-прокси на указанный сервер")
    r.add_argument("--host", action="append", default=[], help="записывать только этот хост (можно несколько)")
    r.add_argument("--keep-static", action="store_true", help="не отбрасывать js/css/картинки")
    r.add_argument("--overwrite", action="store_true")
    r.set_defaults(func=cmd_record)

    sub.add_parser("list", help="список тестов").set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="шаги теста")
    s.add_argument("name")
    s.set_defaults(func=cmd_show)

    c = sub.add_parser("correlate", help="найти значения из ответов, используемые в следующих запросах")
    c.add_argument("name")
    c.add_argument("--min-len", type=int, default=6)
    c.add_argument("--apply", action="store_true", help="сохранить найденные правила в тест")
    c.set_defaults(func=cmd_correlate)

    r = sub.add_parser("run", help="прогнать тесты (по умолчанию все)")
    r.add_argument("names", nargs="*")
    r.add_argument("--target", metavar="URL", help="подменить схему и хост, например http://staging:8080")
    r.add_argument("--timeout", type=float, default=30.0, help="таймаут запроса, с")
    r.add_argument("--max-time", type=int, metavar="MS",
                   help="приемлемое время ответа, мс (для шагов и тестов без своего лимита)")
    r.add_argument("-v", "--verbose", action="store_true", help="показывать все отличия")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("diff", help="отличия из последнего прогона")
    d.add_argument("name")
    d.set_defaults(func=cmd_diff)

    a = sub.add_parser("accept", help="принять фактические ответы последнего прогона как новый эталон")
    a.add_argument("names", nargs="+")
    a.add_argument("--step", type=int, action="append", help="только эти шаги (по умолчанию все упавшие)")
    a.set_defaults(func=cmd_accept)

    i = sub.add_parser("ignore", help="добавить правило игнора")
    i.add_argument("name")
    i.add_argument("pattern", help="JSON-путь (data.items[*].id, **.timestamp), regex или имя заголовка")
    g = i.add_mutually_exclusive_group()
    g.add_argument("--regex", action="store_true", help="pattern — регулярное выражение, маскирует совпадения")
    g.add_argument("--header", action="store_true", help="pattern — имя заголовка ответа")
    i.add_argument("--step", type=int, action="append", help="только для этих шагов")
    i.set_defaults(func=cmd_ignore)

    lm = sub.add_parser("limit", help="приемлемое время ответа для теста или шага")
    lm.add_argument("name")
    lm.add_argument("ms", help="миллисекунды или off")
    lm.add_argument("--step", type=int, help="только для этого шага")
    lm.set_defaults(func=cmd_limit)

    u = sub.add_parser("ui", help="веб-интерфейс")
    u.add_argument("--port", type=int, default=8000)
    u.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    u.set_defaults(func=cmd_ui)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        args.func(Store("."), args)
    except (FileNotFoundError, ValueError, OSError) as e:
        sys.exit(f"ошибка: {e}")
