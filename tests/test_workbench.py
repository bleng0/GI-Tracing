"""Admin Query Workbench: SQL and NoSQL results, role checks, and the sandbox.

Every fixture lives under tmp_path; neither data/ nor out/ is opened here.
FakeAtlas from test_atlas_store stands in for the read-only online store.
"""
import csv
import io
import json
import re
import sqlite3

import bson
import pytest

from gi_trace import doc_store, sql_store
from gi_trace.run_all import run_pipeline
from gi_trace.web import query, sql_workbench, workbench
from gi_trace.web.app import create_app
from gi_trace.web.users import UserStore
from test_atlas_store import FakeAtlas
from test_dashboard import PW, login, token


@pytest.fixture(scope="module")
def dirs(tmp_path_factory):
    root = tmp_path_factory.mktemp("workbench")
    mp = pytest.MonkeyPatch()
    mp.setenv("GI_MODE", "offline")
    run_pipeline(root / "data", root / "out", sample="core", log=lambda *_: None)
    mp.undo()
    users = UserStore(root / "users.db")
    for role, pw in PW.items():
        users.create_user(role, role, pw)
    return root


@pytest.fixture
def app(dirs):
    return create_app(dirs / "data", dirs / "out", dirs / "users.db", background_reload=False)


@pytest.fixture
def online(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    make_users(tmp_path / "users.db")
    return create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False), fake


def make_users(path):
    users = UserStore(path)
    for role, pw in PW.items():
        users.create_user(role, role, pw)


def post(client, **fields):
    form = {"mode": "sql", "code": "SELECT 1", "op": "find", "collection": "certificates",
            "limit": "200", "output": "table", **fields}
    return client.post("/workbench", data={"csrf": token(client, "/workbench"), **form})


def table_rows(page):
    block = re.search(r'<table class="results wb-table">(.*?)</table>', page, re.S)
    if not block:
        return []
    return [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", block.group(1), re.S)[1:]]


# ------------------------------------------------------------------ the page

def test_admin_page_has_a_split_ide_and_external_script(app):
    page = login(app, "admin").get("/workbench").get_data(as_text=True)
    assert '<textarea id="wb-code" name="code"' in page
    assert 'id="wb-gutter"' in page and 'id="wb-highlight"' in page
    assert 'data-mode="nosql"' in page and 'id="wb-collection-wrap"' in page
    assert 'src="/static/workbench.js"' in page and "<script>" not in page
    assert "Recent batches" in page and "Certificate counts" in page
    assert "Schema" in page and "Results" in page


def test_workbench_is_in_the_admin_menu_only(app):
    admin = re.search(r"<nav>(.*?)</nav>", login(app, "admin").get("/shipments").get_data(as_text=True), re.S).group(1)
    other = re.search(r"<nav>(.*?)</nav>", login(app, "employee").get("/shipments").get_data(as_text=True), re.S).group(1)
    assert 'href="/workbench"' in admin and 'href="/workbench"' not in other


@pytest.mark.parametrize("role", ["employee", "delivery"])
def test_other_roles_get_403_for_get_and_post(app, role):
    client = login(app, role)
    assert client.get("/workbench").status_code == 403
    r = post(client)
    assert r.status_code == 403 and "Your role cannot open this page" in r.get_data(as_text=True)


def test_logged_out_visitor_is_sent_to_login(app):
    client = app.test_client()
    r = client.get("/workbench")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    # Logged-out users have no workbench token; take the one from /login and
    # confirm the auth redirect still happens for an otherwise valid POST.
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/login").get_data(as_text=True)).group(1)
    r = client.post("/workbench", data={"csrf": csrf, "mode": "sql", "code": "SELECT 1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


@pytest.mark.parametrize("csrf", ["", "forged"])
def test_posts_without_a_valid_csrf_token_are_refused(app, csrf):
    client = login(app, "admin")
    form = {"mode": "sql", "code": "SELECT 1", "csrf": csrf}
    assert client.post("/workbench", data=form).status_code == 400


def test_get_never_runs_a_query(app, monkeypatch):
    monkeypatch.setattr(workbench, "run", lambda *a, **k: pytest.fail("ran on GET"))
    assert login(app, "admin").get("/workbench?code=DROP+TABLE+batches").status_code == 200


# ------------------------------------------------------------------ SQL results

def test_sql_select_returns_rows_count_and_timing(app):
    r = post(login(app, "admin"), code="SELECT batch_id, product FROM batches ORDER BY batch_id")
    assert r.status_code == 200, r.get_data(as_text=True)
    page = r.get_data(as_text=True)
    assert re.search(r"3 rows · \d+ ms", page)
    assert table_rows(page)[0] == ["CHAP-PK-0001", "Peshawari chappal"]


def test_sql_join_works_offline(app):
    r = post(login(app, "admin"), code="SELECT b.batch_id, c.jurisdiction FROM batches b "
                                        "JOIN cooperatives c ON c.coop_id = b.coop_id LIMIT 2")
    assert r.status_code == 200 and len(table_rows(r.get_data(as_text=True))) == 2


def test_sql_schema_lists_only_prototype_tables(dirs):
    schema = workbench.schema(dirs / "data")
    names = [t["name"] for t in schema]
    assert "batches" in names and "users" not in names
    assert "batch_id" in next(t for t in schema if t["name"] == "batches")["columns"]


def test_sql_row_cap_is_200(tmp_path):
    path = tmp_path / "gi_trace.db"
    conn = sqlite3.connect(path)
    conn.executescript(sql_store.SCHEMA)
    conn.executemany("INSERT INTO cooperatives VALUES (?, ?, ?, ?)",
                     [(f"c{i}", f"Coop {i}", "NP", "GI") for i in range(250)])
    conn.commit()
    conn.close()
    columns, rows, truncated = sql_workbench.run(path, "SELECT coop_id FROM cooperatives")
    assert len(rows) == 200 and truncated and columns == ["coop_id"]


def test_sql_progress_handler_stops_a_long_query(tmp_path, monkeypatch):
    path = tmp_path / "gi_trace.db"
    conn = sqlite3.connect(path)
    conn.executescript(sql_store.SCHEMA)
    conn.execute("INSERT INTO cooperatives VALUES ('c1', 'a', 'NP', 'x')")
    conn.execute("INSERT INTO cooperatives VALUES ('c2', 'b', 'NP', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(sql_workbench, "TIMEOUT_SECONDS", -1)
    monkeypatch.setattr(sql_workbench, "PROGRESS_STEPS", 1)
    with pytest.raises(sql_workbench.WorkbenchError, match="time limit"):
        sql_workbench.run(path, "SELECT a.coop_id FROM cooperatives a, cooperatives b, "
                                "cooperatives c, cooperatives d, cooperatives e")


# ------------------------------------------------------------------ SQL sandbox

SQL_ATTACKS = [
    "DROP TABLE batches", "INSERT INTO batches VALUES ('x', 'c', 'a', 'p', 'h', 1, 'd')",
    "UPDATE batches SET product = 'x'", "DELETE FROM batches", "ALTER TABLE batches ADD COLUMN x TEXT",
    "CREATE TABLE evil (x TEXT)", "ATTACH DATABASE ':memory:' AS other", "DETACH DATABASE other",
    "PRAGMA table_info(batches)", "VACUUM", "REINDEX", "REPLACE INTO batches VALUES ('x')",
    "SELECT 1; DROP TABLE batches", "SELECT * FROM users", "SELECT * FROM users.db",
    "SELECT * FROM sqlite_master", "SELECT load_extension('evil.dll')", "DELETE FROM compliance_view",
    "WITH x AS (DELETE FROM batches RETURNING *) SELECT * FROM x",
]


@pytest.mark.parametrize("sql", SQL_ATTACKS)
def test_sql_attacks_are_refused_by_the_page(app, sql):
    r = post(login(app, "admin"), code=sql)
    assert r.status_code == 400, sql
    page = r.get_data(as_text=True)
    assert "Query refused" in page and "results wb-table" not in page
    assert sql.split()[0] in page  # the editor keeps what was typed


@pytest.mark.parametrize("sql", SQL_ATTACKS)
def test_sql_attacks_are_refused_by_the_module(dirs, sql):
    with pytest.raises(sql_workbench.WorkbenchError):
        sql_workbench.run(dirs / "data" / "gi_trace.db", sql)


def test_sql_attacks_do_not_change_either_database(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for name in ("gi_trace.db", "users.db"):
        conn = sqlite3.connect(data / name)
        conn.executescript(sql_store.SCHEMA if name == "gi_trace.db" else
                           "CREATE TABLE users (name TEXT, pw TEXT)")
        if name == "gi_trace.db":
            conn.execute("INSERT INTO batches VALUES ('b', 'c', 'a', 'p', 'h', 1, 'd')")
        conn.commit()
        conn.close()
    before = [(p.name, p.read_bytes()) for p in sorted(data.iterdir())]
    for sql in ("DROP TABLE batches", "INSERT INTO batches VALUES ('x', 'c', 'a', 'p', 'h', 1, 'd')",
                "UPDATE batches SET product = 'x'", "ATTACH DATABASE 'users.db' AS u"):
        with pytest.raises(sql_workbench.WorkbenchError):
            sql_workbench.run(data / "gi_trace.db", sql)
    assert [(p.name, p.read_bytes()) for p in sorted(data.iterdir())] == before


def test_sql_connection_is_read_only_with_an_authorizer(dirs, monkeypatch):
    connects, authorizers = [], []
    real_connect = sqlite3.connect
    real_authorizer = sql_workbench._authorizer

    def spy_connect(*args, **kwargs):
        connects.append((args, kwargs))
        return real_connect(*args, **kwargs)

    def spy_authorizer(*args):
        authorizers.append(args)
        return real_authorizer(*args)

    monkeypatch.setattr(sql_workbench.sqlite3, "connect", spy_connect)
    monkeypatch.setattr(sql_workbench, "_authorizer", spy_authorizer)
    sql_workbench.run(dirs / "data" / "gi_trace.db", "SELECT batch_id FROM batches")
    assert any("mode=ro" in str(args[0]) and kwargs.get("uri") for args, kwargs in connects)
    assert authorizers
    assert sql_workbench._authorizer(sqlite3.SQLITE_READ, "users", "pw", "main", None) == sqlite3.SQLITE_DENY
    assert sql_workbench._authorizer(sqlite3.SQLITE_READ, "batches", "batch_id", "main", None) == sqlite3.SQLITE_OK
    assert sql_workbench._authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == sqlite3.SQLITE_OK
    assert sql_workbench._authorizer(sqlite3.SQLITE_INSERT, "batches", None, "main", None) == sqlite3.SQLITE_DENY


# ------------------------------------------------------------------ NoSQL results

def test_nosql_find_returns_rows(app):
    r = post(login(app, "admin"), mode="nosql", op="find", collection="certificates",
             code='{"compliance_status": "rejected"}')
    assert r.status_code == 200, r.get_data(as_text=True)
    page = r.get_data(as_text=True)
    assert "Query refused" not in page and table_rows(page)


def test_nosql_find_on_offline_ledgers(app):
    client = login(app, "admin")
    blocks = post(client, mode="nosql", op="find", collection="ledger_blocks", code='{"jurisdiction": "NP"}')
    heads = post(client, mode="nosql", op="find", collection="ledger_heads", code="{}")
    assert blocks.status_code == 200 and table_rows(blocks.get_data(as_text=True))
    assert heads.status_code == 200 and table_rows(heads.get_data(as_text=True))


def test_nosql_aggregate_groups_documents(app):
    r = post(login(app, "admin"), mode="nosql", op="aggregate", collection="certificates",
             code='[{ "$group": { "_id": "$cert_type", "count": { "$sum": 1 } } }]')
    assert r.status_code == 200, r.get_data(as_text=True)
    rows = table_rows(r.get_data(as_text=True))
    assert rows and any("count" in page for page in re.findall(r"<th>(.*?)</th>", r.get_data(as_text=True)))


def test_nosql_aggregate_has_a_200_row_cap(tmp_path):
    q = query.parse_workbench({"op": "aggregate", "collection": "certificates",
                               "code": '[{ "$project": { "n": 1 } }]', "limit": "200"})
    assert q.pipeline[-1] == {"$limit": 200}
    docs = [{"n": i} for i in range(250)]
    rows = query.run_isolated(docs, q, op=q.op, pipeline=q.pipeline, collection=q.collection)
    assert len(rows) == 200


def test_nosql_find_limit_caps_rows(app):
    r = post(login(app, "admin"), mode="nosql", collection="documents", code="{}", limit="3")
    assert r.status_code == 200 and len(table_rows(r.get_data(as_text=True))) == 3


def test_nosql_online_uses_the_read_only_store(online):
    app, fake = online
    client = login(app, "admin")
    r = post(client, mode="nosql", op="aggregate", collection="ledger_blocks",
             code='[{ "$match": { "jurisdiction": "NP" } }]')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert table_rows(r.get_data(as_text=True))


def test_aggregate_online_gets_a_time_limit():
    class Recorder:
        def aggregate(self, pipeline, **kwargs):
            self.calls = (pipeline, kwargs)
            return [{"n": 1}]

    recorder = Recorder()
    q = query.parse_workbench({"op": "aggregate", "collection": "certificates",
                               "code": '[{ "$count": "n" }]', "limit": "10"})
    assert query._run_aggregate_atlas(recorder, q) == [{"n": 1}]
    assert recorder.calls[1]["maxTimeMS"] == query.MAX_TIME_MS
    assert recorder.calls[0][-1] == {"$limit": 10}


# ------------------------------------------------------------------ NoSQL sandbox

NOSQL_ATTACKS = [
    '{"$where": "sleep(100000)"}', '{"$WHERE": "1"}', '{"$expr": {"$function": {}}}',
    '{"$expr": {"$accumulator": {}}}', '{"$out": "evil"}', '{"$merge": "evil"}',
    '{ "a": { "$in": ["$where"] } }',
]

NOSQL_PIPELINE_ATTACKS = [
    '[{ "$out": "evil" }]', '[{ "$merge": { "into": "users" } }]',
    '[{ "$where": "1" }]', '[{ "$function": {} }]', '[{ "$accumulator": {} }]',
    '[{ "$lookup": { "from": "users" } }]', '[{ "$unionWith": "users" }]',
    '[{ "$graphLookup": {} }]', '[{ "$drop": "certificates" }]',
]


@pytest.mark.parametrize("code", NOSQL_ATTACKS + NOSQL_PIPELINE_ATTACKS)
def test_nosql_attacks_are_refused_by_the_page(app, monkeypatch, code):
    monkeypatch.setattr(query, "run_workbench", lambda *a, **k: pytest.fail("reached the database"))
    op = "aggregate" if code.lstrip().startswith("[") else "find"
    r = post(login(app, "admin"), mode="nosql", op=op, code=code)
    assert r.status_code == 400, code
    assert "Query refused" in r.get_data(as_text=True)


@pytest.mark.parametrize("code", NOSQL_PIPELINE_ATTACKS)
def test_nosql_pipeline_attacks_are_refused_by_the_parser(code):
    with pytest.raises(query.QueryError, match="not allowed|not a read-only stage"):
        query.parse_workbench({"op": "aggregate", "collection": "certificates", "code": code})


@pytest.mark.parametrize("fields, message", [
    ({"collection": "users"}, "choose one of"),
    ({"op": "update"}, "choose find or aggregate"),
    ({"op": "aggregate", "code": "{}"}, "non-empty JSON array"),
    ({"op": "aggregate", "code": '[{"$match": {}}, {"$skip": 1}]' * 21}, "at most 20"),
    ({"op": "aggregate", "code": '[{"$match": {}}, {"$comment": "x"}]'}, "read-only stage"),
    ({"code": "not json"}, "not valid JSON"),
    ({"limit": "0"}, "Limit"), ({"limit": "201"}, "Limit"), ({"limit": "2OO"}, "Limit"),
])
def test_bad_workbench_forms_are_refused(app, fields, message):
    r = post(login(app, "admin"), mode="nosql", **fields)
    assert r.status_code == 400 and message in r.get_data(as_text=True)


def test_users_db_is_not_reachable_in_either_mode(app):
    client = login(app, "admin")
    sql = post(client, code="SELECT * FROM users")
    nosql = post(client, mode="nosql", collection="users", code="{}")
    assert sql.status_code == 400 and "dashboard accounts" in sql.get_data(as_text=True)
    assert nosql.status_code == 400 and "choose one of" in nosql.get_data(as_text=True)


def test_worker_runs_an_aggregate_in_its_own_process():
    import subprocess
    import sys
    request = bson.encode({"docs": [{"jurisdiction": "NP"}, {"jurisdiction": "IN"}],
                           "filter": {}, "projection": None, "sort": [], "limit": 200,
                           "op": "aggregate", "collection": "ledger_blocks",
                           "pipeline": [{"$group": {"_id": "$jurisdiction", "n": {"$sum": 1}}}]})
    r = subprocess.run([sys.executable, "-m", "gi_trace.web.query_worker"], input=request,
                       capture_output=True, cwd=query.ROOT, timeout=60)
    assert r.stdout[:1] == b"R"
    reply = bson.decode(r.stdout[1:])
    assert sorted((row["_id"], row["n"]) for row in reply["rows"]) == [("IN", 1), ("NP", 1)]


# ------------------------------------------------------------------ exports and view

def test_csv_export_neutralises_formulas(online):
    app, fake = online
    fake.db["certificates"].insert_one({"signer": "@me", "batch_id": '=HYPERLINK("http://evil","x")'})
    r = post(login(app, "admin"), mode="nosql", collection="certificates",
             code='{"signer": "@me"}', output="csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert r.headers["Content-Disposition"] == 'attachment; filename="workbench.csv"'
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("\ufeff"))))
    assert "'=HYPERLINK" in rows[1][0] or any("'=HYPERLINK" in cell for cell in rows[1])


def test_json_export_is_valid_json(app):
    r = post(login(app, "admin"), code="SELECT batch_id FROM batches ORDER BY batch_id", output="json")
    assert r.status_code == 200 and r.mimetype == "application/json"
    assert r.headers["Content-Disposition"] == 'attachment; filename="workbench.json"'
    payload = json.loads(r.get_data(as_text=True))
    assert len(payload) == 3 and payload[0]["batch_id"] == "CHAP-PK-0001"


def test_json_view_is_rendered_and_toggled_by_external_script(app):
    page = post(login(app, "admin"), code="SELECT batch_id FROM batches").get_data(as_text=True)
    assert 'class="wb-json-view" id="wb-json-view" hidden' in page
    assert "CHAP-PK-0001" in page and "&#34;batch_id&#34;" in page
    js = app.test_client().get("/static/workbench.js").get_data(as_text=True)
    assert "dataset.view" in js and "aria-pressed" in js
    # Regression guard: the highlighter used to return only the unmatched tail.
    assert "return out + escapeHtml(raw.slice(last))" in js
