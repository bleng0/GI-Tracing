"""Step 7: query console and ledger viewer (admin only)."""
import csv
import io
import json
import re
import shutil
import threading
import time

import pytest
from pymongo.errors import ExecutionTimeout, OperationFailure

from gi_trace import doc_store
from gi_trace.run_all import run_pipeline
from gi_trace.web import query
from gi_trace.web.app import create_app
from gi_trace.web.users import UserStore
from test_atlas_store import FakeAtlas
from test_dashboard import PW, login, token


def make_users(path):
    users = UserStore(path)
    for role, pw in PW.items():
        users.create_user(role, role, pw)


@pytest.fixture(scope="module")
def offline_dirs(tmp_path_factory):
    root = tmp_path_factory.mktemp("console")
    mp = pytest.MonkeyPatch()
    mp.setenv("GI_MODE", "offline")
    run_pipeline(root / "data", root / "out", sample="core", log=lambda *_: None)
    mp.undo()
    make_users(root / "users.db")
    return root


@pytest.fixture
def app(offline_dirs):
    return create_app(offline_dirs / "data", offline_dirs / "out", offline_dirs / "users.db", background_reload=False)


@pytest.fixture
def online(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    make_users(tmp_path / "users.db")
    app = create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False)
    return app, fake


def run_form(client, **fields):
    form = {"collection": "certificates", "filter": "{}", "projection": "", "sort": "", "limit": "200",
            "output": "table", **fields}
    return client.post("/query", data={"csrf": token(client, "/query"), **form})


def result_rows(page: str) -> list[list[str]]:
    table = re.search(r'<table class="results">(.*?)</table>', page, re.S)
    if not table:
        return []
    rows = re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S)
    return [re.findall(r"<td>(.*?)</td>", r, re.S) for r in rows[1:]]


def expected_examples(db):
    """What the four saved examples must return, worked out in plain Python."""
    certs = list(db["certificates"].find({}))
    docs = list(db["documents"].find({}))
    return [
        sorted([c["cert_type"], c["batch_id"], c["signer"]] for c in certs if c.get("compliance_status") == "rejected"),
        sorted(([c["batch_id"], str(c["assay"]["thc_pct"]), c["compliance_status"]] for c in certs
                if c.get("cert_type") == "hemp_lab_assay" and c["assay"]["thc_pct"] > 0.3),
               key=lambda r: (-float(r[1]), r[0])),
        sorted([d["doc_id"], d["type"], d["issued_at"], d["issuer"], d["ledger"]] for d in docs
               if d.get("batch_id") == "HEMP-NP-0001"),
        sorted([d["doc_id"], d["batch_id"], d["issuer"], d["ledger_block"]] for d in docs
               if d.get("type") == "export_clearance" and d.get("ledger") == "NP"),
    ]


def example_rows(client, i):
    page = client.get(f"/query?example={i}").get_data(as_text=True)
    form = {name: re.search(rf'<textarea name="{name}"[^>]*>(.*?)</textarea>', page, re.S).group(1)
            for name in ("filter", "projection", "sort")}
    form = {k: v.replace("&#34;", '"').replace("&quot;", '"') for k, v in form.items()}
    collection = re.search(r"<option selected>(\w+)</option>", page).group(1)
    r = run_form(client, collection=collection, **form)
    assert r.status_code == 200, r.get_data(as_text=True)
    return result_rows(r.get_data(as_text=True))


# ------------------------------------------------------------------ the four examples

def test_the_four_examples_return_the_right_rows_online(online):
    app, fake = online
    c = login(app, "admin")
    expected = expected_examples(fake.db)
    assert all(expected), "every example should match something in the core sample"
    for i, rows in enumerate(expected):
        got = example_rows(c, i)
        assert (got if i == 1 else sorted(got)) == rows, query.EXAMPLES[i]["title"]


def test_the_four_examples_return_the_same_rows_offline(app):
    from gi_trace.seed import open_world
    w = app.config["HOLDER"].get()[0]
    expected = expected_examples(w.store)
    c = login(app, "admin")
    for i, rows in enumerate(expected):
        got = example_rows(c, i)
        assert (got if i == 1 else sorted(got)) == rows


def test_an_unknown_example_is_404(app):
    c = login(app, "admin")
    for bad in ("4", "-1", "x", "99999999999999999999", "²", "٣", "１"):
        assert c.get(f"/query?example={bad}").status_code == 404


# ------------------------------------------------------------------ banned operators

BANNED_FORMS = [
    {"filter": '{"$where": "sleep(100000)"}'},
    {"filter": '{"$WHERE": "1"}'},
    {"filter": '{" $where ": "1"}'},
    {"filter": '{"$and": [{"a": 1}, {"$or": [{"$where": "1"}]}]}'},
    {"filter": '{"$expr": {"$function": {"body": "function(){return true}", "args": [], "lang": "js"}}}'},
    {"filter": '{"$expr": {"$eq": [{"$accumulator": {}}, 1]}}'},
    {"filter": '{"a": {"$elemMatch": {"$expr": {"$function": {}}}}}'},
    {"filter": '{"a": "$where"}'},
    {"filter": '{"a": ["x", ["y", "$Function"]]}'},
    {"filter": '{"$expr": {"$eq": ["$out.x", 1]}}'},
    {"filter": '{"$out": "evil"}'},
    {"filter": '{"$merge": {"into": "evil"}}'},
    {"projection": '{"x": {"$function": {"body": "f", "args": [], "lang": "js"}}}'},
    {"projection": '{"$where": 1}'},
    {"sort": '{"$where": 1}'},
    {"sort": '{"$merge": 1}'},
]


@pytest.mark.parametrize("fields", BANNED_FORMS)
def test_banned_operators_are_refused_before_the_database(app, monkeypatch, fields):
    monkeypatch.setattr(query, "run", lambda *a, **k: pytest.fail("the query reached the database"))
    r = run_form(login(app, "admin"), **fields)
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "is not allowed" in page and "results" not in page


@pytest.mark.parametrize("op", query.BANNED)
def test_every_banned_operator_at_depth(op):
    nested = {"$and": [{"$expr": {"$eq": [{op: {}}, 1]}}]}
    for _ in range(10):
        nested = {"$or": [nested]}
    with pytest.raises(query.QueryError, match="not allowed"):
        query.parse({"collection": "documents", "filter": json.dumps(nested)})
    with pytest.raises(query.QueryError, match="not allowed"):
        query.parse({"collection": "documents", "filter": json.dumps({"a": {"$in": [op]}})})


# ------------------------------------------------------------------ hostile input

@pytest.mark.parametrize("fields, message", [
    ({"limit": "201"}, "Limit"), ({"limit": "0"}, "Limit"), ({"limit": "-1"}, "Limit"),
    ({"limit": "1e3"}, "Limit"), ({"limit": "99999999999999999999999"}, "Limit"), ({"limit": "2OO"}, "Limit"),
    ({"limit": "２００"}, "Limit"),
    ({"collection": "users"}, "choose one of"), ({"collection": "ledger_blocks"}, "choose one of"),
    ({"filter": '{"a": NaN}'}, "NaN is not allowed"), ({"filter": '{"a": -Infinity}'}, "not allowed"),
    ({"filter": '{"a": 1, "a": {"$gt": 0}}'}, "given twice"),
    ({"filter": '{"a": 123456789012345678901234567890}'}, "too large"),
    ({"filter": "[]"}, "must be a JSON object"), ({"filter": '"x"'}, "must be a JSON object"),
    ({"filter": "{'a': 1}"}, "not valid JSON"), ({"filter": '{"a": 1'}, "not valid JSON"),
    ({"filter": '{"a":' * 40 + "1" + "}" * 40}, "deeper than"),
    ({"filter": "[" * 100000 + "]" * 100000}, "longer than"),
    ({"filter": '{"a": "' + "x" * 20001 + '"}'}, "longer than"),
    ({"sort": '{"batch_id": "1"}'}, "must be 1"), ({"sort": '{"batch_id": true}'}, "must be 1"),
    ({"sort": '{"batch_id": 2}'}, "must be 1"), ({"sort": '{"batch_id": 1.0}'}, "must be 1"),
    ({"sort": json.dumps({f"f{i}": 1 for i in range(6)})}, "at most 5"),
    ({"projection": "[1]"}, "must be a JSON object"),
])
def test_hostile_input_is_refused_with_a_message(app, fields, message):
    r = run_form(login(app, "admin"), **fields)
    assert r.status_code == 400 and message in r.get_data(as_text=True)


def test_nesting_deeper_than_python_allows_is_refused_not_a_crash():
    text = "[" * 9000 + "]" * 9000
    with pytest.raises(query.QueryError):
        query.parse_json('{"a": ' + text + "}", "Filter")


def test_database_errors_become_messages_offline(app):
    c = login(app, "admin")
    for flt in ('{"$nosuchop": 1}', '{"a": {"$nosuchop": 1}}'):
        r = run_form(c, filter=flt)
        assert r.status_code == 400 and "refused" in r.get_data(as_text=True), flt


def test_get_never_runs_a_query_and_post_needs_csrf(app, monkeypatch):
    monkeypatch.setattr(query, "run", lambda *a, **k: pytest.fail("ran"))
    c = login(app, "admin")
    assert c.get("/query?example=0&filter=%7B%7D").status_code == 200
    assert c.post("/query", data={"collection": "certificates", "filter": "{}"}).status_code == 400


def test_values_are_escaped_in_the_table(online):
    app, fake = online
    fake.db["certificates"].insert_one({"batch_id": "<script>alert(1)</script>", "cert_type": "x"})
    page = run_form(login(app, "admin"), filter='{"cert_type": "x"}').get_data(as_text=True)
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ------------------------------------------------------------------ time limit and limit

class Recorder:
    """A pymongo-like cursor that records how it was used."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def find(self, *a):
        self.calls.append(("find",) + a)
        return self

    def sort(self, s):
        self.calls.append(("sort", s))
        return self

    def limit(self, n):
        self.calls.append(("limit", n))
        return self

    def max_time_ms(self, ms):
        self.calls.append(("max_time_ms", ms))
        return self

    def __iter__(self):
        if self.fail:
            raise self.fail
        return iter([{"a": 1}])


def test_atlas_gets_the_limit_and_max_time_ms():
    rec = Recorder()
    q = query.parse({"collection": "documents", "filter": '{"a": 1}', "sort": '{"a": -1}', "limit": "200"})
    assert query._run_atlas(rec, q) == [{"a": 1}]
    assert ("limit", 200) in rec.calls and ("max_time_ms", 10_000) in rec.calls and ("sort", [("a", -1)]) in rec.calls


@pytest.mark.parametrize("error, message", [
    (ExecutionTimeout("operation exceeded time limit", 50), "stopped the query after 10 s"),
    (OperationFailure("unknown operator: $foo", 2, {"errmsg": "unknown operator: $foo"}), "unknown operator: $foo"),
    (KeyError("assay"), "the query failed: KeyError"),
])
def test_atlas_errors_become_messages(error, message):
    q = query.parse({"collection": "documents", "filter": "{}"})
    with pytest.raises(query.QueryError, match=re.escape(message)):
        query._run_atlas(Recorder(fail=error), q)


def test_atlas_connection_loss_is_not_swallowed():
    from pymongo.errors import ServerSelectionTimeoutError
    q = query.parse({"collection": "documents", "filter": "{}"})
    with pytest.raises(ServerSelectionTimeoutError):
        query._run_atlas(Recorder(fail=ServerSelectionTimeoutError("down")), q)


SLOW = '{"lab_file_sha256": {"$regex": "^(\\\\w+)+X$"}}'  # catastrophic backtracking on 64 hex digits


def test_a_slow_offline_query_is_stopped_and_other_pages_keep_working(app, monkeypatch):
    monkeypatch.setattr(query, "MAX_TIME_MS", 1500)
    admin = login(app, "admin")
    other = login(app, "employee")
    done = {}

    def slow():
        t = time.perf_counter()
        r = run_form(admin, filter=SLOW)
        done.update(status=r.status_code, page=r.get_data(as_text=True), took=time.perf_counter() - t)

    th = threading.Thread(target=slow)
    th.start()
    time.sleep(0.3)
    t = time.perf_counter()
    assert other.get("/batches").status_code == 200 and time.perf_counter() - t < 1.0
    th.join(60)
    assert done["status"] == 400 and "stopped after 1.5 s (time limit)" in done["page"]
    assert done["took"] < 30


def test_limit_caps_the_rows_offline(app):
    r = run_form(login(app, "admin"), collection="documents", limit="3")
    assert r.status_code == 200 and len(result_rows(r.get_data(as_text=True))) == 3


# ------------------------------------------------------------------ CSV

def test_csv_cells_cannot_start_a_formula():
    assert [query.csv_cell(v) for v in ["=1+1", "+1", "-1+2", "@SUM(A1)", "\t=1", "\r=1", "ok", "", None]] == \
           ["'=1+1", "'+1", "'-1+2", "'@SUM(A1)", "'\t=1", "'\r=1", "ok", "", ""]
    assert [query.csv_cell(v) for v in [" =1+1", "\n=1+1", " \r\n@x", "\uff1d1+1", "\uff0bx", "a=b", " ok"]] == \
           ["' =1+1", "'\n=1+1", "' \r\n@x", "'\uff1d1+1", "'\uff0bx", "a=b", " ok"]
    assert [query.csv_cell(v) for v in [-5, -0.5, True, ["=x"], {"=a": 1}]] == ["-5", "-0.5", "true", '["=x"]', '{"=a": 1}']


def test_csv_download_online_neutralises_formulas(online):
    app, fake = online
    fake.db["certificates"].insert_one({"batch_id": '=HYPERLINK("http://evil","x")', "cert_type": "+cmd",
                                        "signer": "@me", "operator_code": "-2+3", "weight": -4, "=col": "y"})
    r = run_form(login(app, "admin"), filter='{"signer": "@me"}', projection='{"_id": 0}', output="csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert r.headers["Content-Disposition"] == 'attachment; filename="certificates.csv"'
    text = r.get_data(as_text=True)
    assert text.startswith("﻿")
    header, row = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    cells = dict(zip(header, row))
    assert cells["batch_id"] == "'=HYPERLINK(\"http://evil\",\"x\")" and cells["cert_type"] == "'+cmd"
    assert cells["signer"] == "'@me" and cells["operator_code"] == "'-2+3" and cells["weight"] == "-4"
    assert "'=col" in header
    for cell in header + row:
        assert not cell.startswith(("=", "+", "@", "\t", "\r")) and (not cell.startswith("-") or cell == "-4")


def test_csv_matches_the_table(app):
    c = login(app, "admin")
    table = result_rows(run_form(c, collection="documents", projection='{"_id": 0, "doc_id": 1, "type": 1}',
                                 sort='{"doc_id": 1}').get_data(as_text=True))
    text = run_form(c, collection="documents", projection='{"_id": 0, "doc_id": 1, "type": 1}', sort='{"doc_id": 1}',
                    output="csv").get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == ["doc_id", "type"] and rows[1:] == table


# ------------------------------------------------------------------ who can use it

def test_only_admins_reach_the_console_and_ledger(app):
    for role in ("employee", "delivery"):
        c = login(app, role)
        assert c.get("/query").status_code == 403 and c.get("/ledger").status_code == 403
        r = c.post("/query", data={"csrf": token(c, "/shipments"), "collection": "certificates", "filter": "{}"})
        assert r.status_code == 403
    anon = app.test_client()
    assert anon.get("/query").status_code == 302 and anon.get("/ledger?j=NP").status_code == 302


# ------------------------------------------------------------------ ledger viewer

def ledger_page(client, j, page=None):
    url = f"/ledger?j={j}" + (f"&page={page}" if page else "")
    r = client.get(url)
    assert r.status_code == 200
    return r.get_data(as_text=True)


@pytest.mark.parametrize("j", ["NP", "IN", "PK"])
def test_clean_chains_are_valid_offline(app, j):
    page = ledger_page(login(app, "admin"), j)
    assert "<h2>Chain valid</h2>" in page and "NOT valid" not in page


def test_clean_chains_are_valid_online(online):
    app, _ = online
    c = login(app, "admin")
    for j in ("NP", "IN", "PK"):
        assert "<h2>Chain valid</h2>" in ledger_page(c, j)


@pytest.fixture
def copy_app(offline_dirs, tmp_path):
    """A copy of the offline world (never the real data/), opened by a dashboard."""
    for name in ("data", "out"):
        shutil.copytree(offline_dirs / name, tmp_path / name)
    shutil.copy(offline_dirs / "users.db", tmp_path / "users.db")
    app = create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False)
    assert app.config["HOLDER"].get()[0] is not None
    return app, tmp_path / "data" / "ledgers"


def edit_ledger(path, change):
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data["blocks"])
    path.write_text(json.dumps(data), encoding="utf-8")


def test_an_edited_block_is_flagged(copy_app, offline_dirs):
    app, ledgers = copy_app
    original = (offline_dirs / "data" / "ledgers" / "NP.json").read_bytes()
    c = login(app, "admin")
    assert "<h2>Chain valid</h2>" in ledger_page(c, "NP")

    def change(blocks):
        blocks[1]["payload"]["batch_id"] = "HEMP-NP-9999"
    edit_ledger(ledgers / "NP.json", change)
    page = ledger_page(c, "NP")
    assert "Chain NOT valid" in page and "block 1: payload does not match its chameleon digest" in page
    assert re.search(r'<tr class="rejected"><td class="num">1 ✗</td>', page)
    assert "<h2>Chain valid</h2>" in ledger_page(c, "IN")  # the other ledgers are untouched
    assert (offline_dirs / "data" / "ledgers" / "NP.json").read_bytes() == original  # only the copy changed


def test_an_edited_timestamp_breaks_the_signature_and_the_next_link(copy_app):
    app, ledgers = copy_app

    def change(blocks):
        blocks[2]["timestamp"] = "2020-01-01T00:00:00+00:00"
    edit_ledger(ledgers / "PK.json", change)
    page = ledger_page(login(app, "admin"), "PK")
    assert "block 2: bad or unauthorised signature" in page and "block 3: broken link" in page


def test_a_removed_last_block_is_caught_by_the_checkpoint(copy_app):
    app, ledgers = copy_app
    edit_ledger(ledgers / "NP.json", lambda blocks: blocks.pop())
    page = ledger_page(login(app, "admin"), "NP")
    assert "Chain NOT valid" in page and "blocks were removed" in page and str(ledgers) not in page


@pytest.mark.parametrize("change", [
    lambda b: b[0].update(ch_r="zz"), lambda b: b[0].pop("signature"), lambda b: b[0].update(index="0"),
    lambda b: b.__setitem__(0, "not a block"), lambda b: b[0].update(payload=[1, 2]),
])
def test_malformed_blocks_are_problems_not_errors(copy_app, change):
    app, ledgers = copy_app
    edit_ledger(ledgers / "IN.json", change)
    page = ledger_page(login(app, "admin"), "IN")
    assert "Chain NOT valid" in page


def test_an_unreadable_ledger_file_is_a_problem(copy_app):
    app, ledgers = copy_app
    (ledgers / "NP.json").write_text("{", encoding="utf-8")
    page = ledger_page(login(app, "admin"), "NP")
    assert "the NP ledger file cannot be read" in page and str(ledgers) not in page


def test_an_edited_block_in_atlas_is_flagged(online):
    app, fake = online
    c = login(app, "admin")
    assert "<h2>Chain valid</h2>" in ledger_page(c, "NP")
    fake.db["ledger_blocks"].update_one({"jurisdiction": "NP", "index": 1}, {"$set": {"payload.batch_id": "X"}})
    page = ledger_page(c, "NP")
    assert "block 1: payload does not match its chameleon digest" in page


def test_ledger_paging_and_bad_parameters(app):
    c = login(app, "admin")
    assert c.get("/ledger?j=XX").status_code == 404
    assert c.get("/ledger?j=").status_code == 404
    for page in ("0", "-1", "abc", "999999999", "1" * 50, "²", "٣"):
        assert c.get(f"/ledger?j=NP&page={page}").status_code == 200
    assert "Page 1 of 1" in ledger_page(c, "NP")


# ------------------------------------------------------------------ review fixes

def test_a_ledger_file_that_cannot_be_opened_is_a_problem_not_a_500(copy_app):
    app, ledgers = copy_app
    (ledgers / "NP.json").replace(ledgers / "NP.moved")
    (ledgers / "NP.json").mkdir()  # reading it raises an OSError (IsADirectoryError / PermissionError on Windows)
    r = login(app, "admin").get("/ledger?j=NP")
    assert r.status_code == 200 and "the NP ledger file cannot be read" in r.get_data(as_text=True)


def test_an_atlas_read_error_is_a_problem_not_a_500(online, monkeypatch):
    from gi_trace.ledger_store import AtlasLedgerStore
    app, fake = online
    c = login(app, "admin")

    def refuse(self, *a, **k):
        raise OperationFailure("not authorized", 13)
    monkeypatch.setattr(type(fake.db["ledger_blocks"]), "find", refuse)
    r = c.get("/ledger?j=NP")
    assert r.status_code == 200 and "Atlas refused to read the NP ledger: OperationFailure" in r.get_data(as_text=True)


def test_blocks_after_an_unreadable_one_are_still_shown(copy_app):
    app, ledgers = copy_app

    def change(blocks):
        blocks[0]["ch_r"] = "zz"
        blocks[3]["payload"]["edited"] = "<b>here</b>"
    edit_ledger(ledgers / "NP.json", change)
    page = ledger_page(login(app, "admin"), "NP")
    assert "block 0: cannot be read" in page and "No blocks." not in page
    assert "&lt;b&gt;here&lt;/b&gt;" in page  # later blocks appear as stored, escaped
    assert len(re.findall(r'<tr class="rejected">', page)) == len(json.loads((ledgers / "NP.json").read_text())["blocks"])


def test_a_checkpoint_naming_a_block_marks_that_row(copy_app):
    app, ledgers = copy_app
    head = ledgers / "NP.head.json"
    data = json.loads(head.read_text())
    edit_ledger(ledgers / "NP.json", lambda blocks: blocks[-1].update(timestamp="2020-01-01T00:00:00+00:00"))
    page = ledger_page(login(app, "admin"), "NP")
    last = data["index"]
    assert f"block {last} is not the one its signed checkpoint names" in page
    assert re.search(rf'<tr class="rejected"><td class="num">{last} ✗</td>', page)


def test_the_worker_marks_a_failure_before_loading(tmp_path):
    import subprocess
    import sys
    from bson import decode
    r = subprocess.run([sys.executable, "-m", "gi_trace.web.query_worker"], input=b"not bson",
                       capture_output=True, cwd=query.ROOT, timeout=60)
    assert r.stdout[:1] == b"E" and "error" in decode(r.stdout[1:])


def test_a_timed_out_worker_is_killed(monkeypatch):
    import subprocess
    started = []
    real = subprocess.Popen

    def spy(*a, **k):
        started.append(real(*a, **k))
        return started[-1]
    monkeypatch.setattr(query.subprocess, "Popen", spy)
    docs = [{"lab_file_sha256": "ab" * 32}]
    q = query.parse({"collection": "certificates", "filter": SLOW})
    with pytest.raises(query.QueryError, match="time limit"):
        query.run_isolated(docs, q, seconds=1)
    assert started and started[0].poll() is not None  # the process has ended
    assert query._slots._value == query.OFFLINE_SLOTS  # every slot released
