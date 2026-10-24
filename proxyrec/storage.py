"""Хранение тестов на диске: recordings/<имя>/test.json и recordings/<имя>/last_run.json."""
import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path

CONFIG_FILE = "proxyrec.json"
DEFAULT_CONFIG = {
    "tests_dir": "recordings",
    # Какие заголовки ответа сравнивать (остальные игнорируются).
    "compare_headers": ["content-type"],
    # Лимит времени ответа по умолчанию, мс (null — без лимита).
    "max_time_ms": None,
    # Глобальные правила игнора, действуют на все тесты.
    "ignore": {"json_paths": [], "regex": [], "headers": []},
}
NAME_RE = re.compile(r"^[\w.-]+$")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def pack_body(raw: bytes, try_json: bool = False) -> dict:
    """Тело -> поле для JSON-файла: json (разобранный), body (текст) или body_b64 (бинарь)."""
    if not raw:
        return {"body": ""}
    if try_json and raw.lstrip()[:1] in (b"{", b"["):
        try:
            return {"json": json.loads(raw)}
        except ValueError:
            pass
    try:
        return {"body": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"body_b64": base64.b64encode(raw).decode("ascii")}


def unpack_body(d: dict) -> bytes:
    if "json" in d:
        return json.dumps(d["json"], ensure_ascii=False).encode("utf-8")
    if "body_b64" in d:
        return base64.b64decode(d["body_b64"])
    return d.get("body", "").encode("utf-8")


def header(headers, name, default=None):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return default


def merge_ignore(*parts) -> dict:
    out = {"json_paths": [], "regex": [], "headers": []}
    for p in parts:
        for key in out:
            out[key].extend((p or {}).get(key, []))
    return out


class Store:
    def __init__(self, root="."):
        self.root = Path(root)
        self.config = dict(DEFAULT_CONFIG)
        cfg_path = self.root / CONFIG_FILE
        if cfg_path.exists():
            self.config.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        self.dir = self.root / self.config["tests_dir"]

    @staticmethod
    def check_name(name):
        if not NAME_RE.match(name):
            raise ValueError(f"недопустимое имя теста: {name!r} (буквы, цифры, _ . -)")

    def test_path(self, name):
        return self.dir / name / "test.json"

    def run_path(self, name):
        return self.dir / name / "last_run.json"

    def exists(self, name):
        return self.test_path(name).exists()

    def names(self):
        if not self.dir.exists():
            return []
        return sorted(p.parent.name for p in self.dir.glob("*/test.json"))

    def load(self, name):
        path = self.test_path(name)
        if not path.exists():
            raise FileNotFoundError(f"тест '{name}' не найден")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, test):
        self._write(self.test_path(test["name"]), test)

    def load_run(self, name):
        path = self.run_path(name)
        if not path.exists():
            raise FileNotFoundError(f"у теста '{name}' нет результатов прогона, сначала выполните run")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_run(self, name, run):
        self._write(self.run_path(name), run)

    @staticmethod
    def _write(path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # В Windows замена падает, если файл в этот момент кто-то читает: пробуем ещё раз.
        for attempt in range(20):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)


def new_test(name):
    return {
        "name": name,
        "created": now_iso(),
        "ignore": {"json_paths": [], "regex": [], "headers": []},
        "extract": [],
        "steps": [],
    }
