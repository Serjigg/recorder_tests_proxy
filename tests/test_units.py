from proxyrec.compare import Comparer
from proxyrec.correlate import extract, find_candidates, substitute
from proxyrec.jsonpath import format_path, is_ignored, parse_path
from proxyrec.storage import pack_body, unpack_body

NO_IGNORE = {"json_paths": [], "regex": [], "headers": []}


def resp(status=200, headers=None, **body):
    return {"status": status, "headers": headers or [["Content-Type", "application/json"]], **body}


def diff(exp, act, ignore=None, headers=("content-type",), subs=None):
    return Comparer({**NO_IGNORE, **(ignore or {})}, list(headers), subs).compare(exp, act)


# --- jsonpath ---

def test_parse_and_format_roundtrip():
    assert parse_path("$.a.b[0].c") == ["a", "b", 0, "c"]
    assert parse_path("a[*].b") == ["a", "*", "b"]
    assert format_path(("a", 0, "b")) == "$.a[0].b"
    assert format_path(()) == "$"


def test_ignore_patterns():
    pats = lambda *p: [parse_path(x) for x in p]
    assert is_ignored(("a", "b"), pats("a.b"))
    assert is_ignored(("a", "b", "c", 3), pats("a.b"))            # поддерево
    assert not is_ignored(("a", "bb"), pats("a.b"))
    assert is_ignored(("x", 5, "id"), pats("x[*].id"))
    assert is_ignored(("deep", "er", 1, "ts"), pats("**.ts"))
    assert is_ignored(("ts",), pats("**.ts"))                     # ** = ноль уровней
    assert is_ignored(("k", "id"), pats("*.id"))
    assert not is_ignored(("k", "z", "id"), pats("*.id"))
    assert not is_ignored(("a", 1), pats("a[0]"))


# --- body packing ---

def test_pack_body_kinds():
    assert pack_body(b"") == {"body": ""}
    assert pack_body(b'{"a":1}', try_json=True) == {"json": {"a": 1}}
    assert pack_body(b'{"a":1}') == {"body": '{"a":1}'}            # запросы храним как есть
    assert pack_body(b"{broken", try_json=True) == {"body": "{broken"}
    assert pack_body("привет".encode()) == {"body": "привет"}
    raw = bytes(range(256))
    packed = pack_body(raw, try_json=True)
    assert "body_b64" in packed and unpack_body(packed) == raw


# --- compare ---

def test_equal_responses():
    assert diff(resp(json={"a": [1, {"b": "x"}]}), resp(json={"a": [1, {"b": "x"}]})) == []


def test_status_and_header_diff():
    d = diff(resp(200, json={}), resp(500, [["Content-Type", "text/html"]], json={}))
    assert "status: 200 -> 500" in d
    assert any(x.startswith("header content-type") for x in d)
    assert diff(resp(json={}), resp(headers=[["Content-Type", "text/html"]], json={}),
                ignore={"headers": ["Content-Type"]}) == []


def test_json_diff_details():
    d = diff(resp(json={"a": 1, "b": [1, 2], "c": "x", "gone": 1}),
             resp(json={"a": 2, "b": [1], "c": "x", "new": 1}))
    assert "$.a: 1 -> 2" in d
    assert "$.b: длина массива 2 -> 1" in d
    assert any(x.startswith("$.gone: пропало") for x in d)
    assert any(x.startswith("$.new: появилось") for x in d)


def test_json_type_strictness():
    assert diff(resp(json={"a": 1}), resp(json={"a": True})) != []
    assert diff(resp(json={"a": 1}), resp(json={"a": "1"})) != []
    assert diff(resp(json={"a": None}), resp(json={"a": 0})) != []


def test_json_ignore_and_regex():
    exp = resp(json={"items": [{"id": 1, "at": "2024-01-01T10:00"}], "msg": "req 123 done"})
    act = resp(json={"items": [{"id": 2, "at": "2025-05-05T11:11"}], "msg": "req 999 done"})
    assert len(diff(exp, act)) == 3
    assert diff(exp, act, ignore={"json_paths": ["items[*].id", "**.at"], "regex": [r"\d{3}"]}) == []


def test_ignore_added_and_missing_keys():
    assert diff(resp(json={"a": 1, "ts": 1}), resp(json={"a": 1, "rid": 2}),
                ignore={"json_paths": ["ts", "rid"]}) == []


def test_text_and_binary_bodies():
    th = [["Content-Type", "text/html"]]
    d = diff(resp(headers=th, body="<p>1</p>\n<b>x</b>"), resp(headers=th, body="<p>1</p>\n<b>y</b>"))
    assert d[0].startswith("body") and any("-<b>x</b>" in l for l in d) and any("+<b>y</b>" in l for l in d)
    assert diff(resp(headers=th, body="time 10:00"), resp(headers=th, body="time 11:30"),
                ignore={"regex": [r"\d\d:\d\d"]}) == []
    assert diff(resp(body_b64="AAE="), resp(body_b64="AAI=")) == ["body: бинарное содержимое отличается"]
    assert diff(resp(json={"a": 1}), resp(body="not json")) != []


def test_long_text_diff_truncated():
    th = [["Content-Type", "text/plain"]]
    d = diff(resp(headers=th, body="\n".join(map(str, range(200)))),
             resp(headers=th, body="\n".join(str(i * 2) for i in range(200))))
    assert len(d) <= 43 and "ещё" in d[-1]


def test_correlated_values_not_reported():
    subs = {"old-id-1": "new-id-9", "12345": "67890"}
    exp = resp(json={"id": "old-id-1", "link": "/o/old-id-1", "num": 12345})
    act = resp(json={"id": "new-id-9", "link": "/o/new-id-9", "num": 67890})
    assert diff(exp, act, subs=subs) == []
    assert diff(exp, act) != []


# --- correlate ---

def step(i, url, body="", headers=None, rjson=None):
    return {"id": i,
            "request": {"method": "POST", "url": url, "headers": headers or [], "body": body},
            "response": {"status": 200, "headers": [], **({"json": rjson} if rjson is not None else {"body": ""})}}


def test_find_candidates():
    tok = "a+b/c=TOKEN"
    test = {"extract": [], "steps": [
        step(1, "http://h/login", '{"user":"username1"}', rjson={"token": tok, "user": "username1", "n": 1234567}),
        step(2, "http://h/x?t=a%2Bb%2Fc%3DTOKEN", rjson={"order": {"id": 1234567}}),
        step(3, "http://h/y/1234567", headers=[["Authorization", "Bearer " + tok]]),
    ]}
    found = {c["path"]: c for c in find_candidates(test)}
    assert found["$.token"]["used_in"] == [2, 3]                  # в т.ч. url-encoded
    assert "$.user" not in found                                  # клиент знал значение раньше
    assert found["$.n"]["used_in"] == [3]
    test["extract"] = list(found.values())
    assert find_candidates(test) == []                            # повторно не предлагает


def test_extract_variants():
    r = {"status": 200, "headers": [["Location", "/orders/abc42"]], "json": {"a": [{"b": True}], "n": 5}}
    assert extract({"from": "json", "path": "$.a[0].b"}, r) == "true"
    assert extract({"from": "json", "path": "n"}, r) == "5"
    assert extract({"from": "json", "path": "missing.x"}, r) is None
    assert extract({"from": "json", "path": "a"}, r) is None
    assert extract({"from": "header", "name": "location", "regex": r"/orders/(\w+)"}, r) == "abc42"
    assert extract({"from": "header", "name": "X-None"}, r) is None
    html = {"status": 200, "headers": [], "body": '<input name="csrf" value="zz9">'}
    assert extract({"from": "regex", "regex": r'value="(\w+)"'}, html) == "zz9"


def test_substitute_plain_and_encoded():
    s = substitute("t=a%2Bb&h=a+b", {"a+b": "c/d"})
    assert s == "t=c%2Fd&h=c/d"
