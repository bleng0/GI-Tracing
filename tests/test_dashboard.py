"""Step 6: dashboard shell and pages, online and offline."""
import os
import re
import time

import pytest

from gi_trace import doc_store
from gi_trace.docgen.qr import qr_payload
from gi_trace.run_all import run_pipeline
from gi_trace.web import data as webdata
from gi_trace.web.app import PAGES, create_app
from gi_trace.web.users import UserStore
from test_atlas_store import FakeAtlas

PW = {"admin": "Admin-Pass-123", "employee": "Employee-Pass-123", "delivery": "Delivery-Pass-123"}
STAFF_ONLY = {"/overview", "/batches", "/documents", "/batches/HEMP-NP-0001",
              "/documents/hemp_lab_assay:HEMP-NP-0001/download"}
ADMIN_ONLY = {"/audit", "/users", "/query", "/ledger"}
EVERYONE = {"/shipments", "/verify"}


@pytest.fixture(scope="module")
def world_dirs(tmp_path_factory):
    root = tmp_path_factory.mktemp("dash")
    mp = pytest.MonkeyPatch()
    mp.setenv("GI_MODE", "offline")
    run_pipeline(root / "data", root / "out", sample="core", log=lambda *_: None)
    mp.undo()
    users = UserStore(root / "users.db")
    for role, pw in PW.items():
        users.create_user(role, role, pw)
    return root


@pytest.fixture
def app(world_dirs):
    return create_app(world_dirs / "data", world_dirs / "out", world_dirs / "users.db", background_reload=False)


def token(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def login(app, role):
    c = app.test_client()
    r = c.post("/login", data={"username": role, "password": PW[role], "csrf": token(c)})
    assert r.status_code == 302, r.get_data(as_text=True)
    return c


# ------------------------------------------------------------------ access

def test_logged_out_visitors_go_to_login(app):
    c = app.test_client()
    for path in STAFF_ONLY | ADMIN_ONLY | EVERYONE:
        r = c.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/login"), path


@pytest.mark.parametrize("role", ["admin", "employee", "delivery"])
def test_each_role_sees_only_its_pages(app, role):
    c = login(app, role)
    allowed = EVERYONE | (STAFF_ONLY if role != "delivery" else set()) | (ADMIN_ONLY if role == "admin" else set())
    for path in STAFF_ONLY | ADMIN_ONLY | EVERYONE:
        assert c.get(path).status_code == (200 if path in allowed else 403), (role, path)
    menu = set(re.findall(r'<nav>(.*?)</nav>', c.get("/shipments").get_data(as_text=True), re.S)[0].split('href="')[1:])
    menu = {m.split('"')[0] for m in menu}
    assert menu == {p for p in allowed if p.count("/") == 1}


def test_home_sends_each_role_to_its_first_page(app):
    assert login(app, "admin").get("/").headers["Location"].endswith("/overview")
    assert login(app, "delivery").get("/").headers["Location"].endswith("/shipments")


def test_page_table_and_routes_agree(app):
    rules = {r.endpoint for r in app.url_map.iter_rules()}
    assert {e for e, _, _ in PAGES} <= rules


# ------------------------------------------------------------------ login, CSRF, cookies, headers

def test_wrong_password_gets_one_generic_message(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": "nope-nope", "csrf": token(c)})
    assert r.status_code == 401 and "Wrong username or password" in r.get_data(as_text=True)
    r = c.post("/login", data={"username": "ghost", "password": "nope-nope", "csrf": token(c)})
    assert r.status_code == 401 and "Wrong username or password" in r.get_data(as_text=True)


@pytest.mark.parametrize("csrf", [None, "", "forged"])
def test_posts_without_the_csrf_token_are_refused(app, csrf):
    c = app.test_client()
    c.get("/login")
    form = {"username": "admin", "password": PW["admin"]}
    if csrf is not None:
        form["csrf"] = csrf
    assert c.post("/login", data=form).status_code == 400
    logged_in = login(app, "admin")
    assert logged_in.post("/logout", data={} if csrf is None else {"csrf": csrf}).status_code == 400
    assert logged_in.get("/overview").status_code == 200  # still signed in


def test_cookie_and_security_headers(app):
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": PW["admin"], "csrf": token(c)})
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    h = c.get("/overview").headers
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"
    assert h["Cache-Control"] == "no-store"


def test_sign_out_ends_the_session(app):
    c = login(app, "employee")
    page = c.get("/overview").get_data(as_text=True)
    t = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert c.post("/logout", data={"csrf": t}).status_code == 302
    assert c.get("/overview").status_code == 302


def test_a_new_login_gets_a_new_csrf_token(app):
    c = app.test_client()
    before = token(c)
    c.post("/login", data={"username": "admin", "password": PW["admin"], "csrf": before})
    assert token(c, "/overview") != before


# ------------------------------------------------------------------ pages and data

def test_offline_banner_and_counts(app, world_dirs):
    c = login(app, "admin")
    page = c.get("/overview").get_data(as_text=True)
    assert "OFFLINE MODE: local data from the last offline run" in page
    w = app.config["HOLDER"].get()[0]
    ships = webdata.shipments(w)
    assert re.search(rf'<span class="num">{len(ships)}</span> shipments', page)
    assert re.search(r'<span class="num">3</span> batches', page)
    assert "OFFLINE MODE" in app.test_client().get("/login").get_data(as_text=True)  # every page, even sign-in


def test_employee_never_receives_prices(app):
    admin = login(app, "admin").get("/overview").get_data(as_text=True)
    employee = login(app, "employee").get("/overview").get_data(as_text=True)
    assert "Order value" in admin and "Order value" not in employee
    values = re.findall(r'<td class="num">([\d,]+\.\d\d)</td>', admin)
    assert values and not any(v in employee for v in values)


def test_batches_shipments_documents_pages(app):
    c = login(app, "employee")
    batches = c.get("/batches").get_data(as_text=True)
    assert "HEMP-NP-0002" in batches and "rejected" in batches
    detail = c.get("/batches/HEMP-NP-0001").get_data(as_text=True)
    assert "hemp_lab_assay" in detail and "signature" not in detail
    assert c.get("/batches/NOPE").status_code == 404
    shipments = login(app, "delivery").get("/shipments").get_data(as_text=True)
    assert "COMMITTED" in shipments and "unit_price" not in shipments
    docs = c.get("/documents").get_data(as_text=True)
    assert "hemp_lab_assay:HEMP-NP-0001" in docs and "twin" not in docs


def test_download_serves_only_the_registered_pdf(app, world_dirs):
    c = login(app, "employee")
    r = c.get("/documents/hemp_lab_assay:HEMP-NP-0001/download")
    path = world_dirs / "out" / "certificates" / "hemp_lab_assay__HEMP-NP-0001.pdf"
    assert r.status_code == 200 and r.data == path.read_bytes() and r.mimetype == "application/pdf"
    assert c.get("/documents/nope/download").status_code == 404
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"%tampered")
        assert c.get("/documents/hemp_lab_assay:HEMP-NP-0001/download").status_code == 409
    finally:
        path.write_bytes(original)


def test_verify_pdf_and_qr(app, world_dirs):
    import io
    c = login(app, "delivery")
    pdf = (world_dirs / "out" / "certificates" / "hemp_lab_assay__HEMP-NP-0001.pdf").read_bytes()

    def post(**fields):
        t = re.search(r'name="csrf" value="([^"]+)"', c.get("/verify").get_data(as_text=True)).group(1)
        return c.post("/verify", data={"csrf": t, **fields}, content_type="multipart/form-data").get_data(as_text=True)

    assert "<h2>VALID: hemp_lab_assay:HEMP-NP-0001" in post(pdf=(io.BytesIO(pdf), "hemp_lab_assay__HEMP-NP-0001.pdf"))
    assert "<h2>TAMPERED" in post(pdf=(io.BytesIO(pdf + b"%one extra byte"), "hemp_lab_assay__HEMP-NP-0001.pdf"))
    w = app.config["HOLDER"].get()[0]
    rec = w.store.get_collection("documents").find_one({"doc_id": "hemp_lab_assay:HEMP-NP-0001"})
    assert "<h2>VALID" in post(qr=rec["qr_payload"])
    forged = rec["qr_payload"].replace(rec["ledger_block"], "0" * 64)
    assert "<h2>TAMPERED" in post(qr=forged)
    anchor = w.ledgers["NP"].find("BATCH_ANCHOR", batch_id="HEMP-NP-0001")[-1]
    batch_qr = qr_payload(batch_id="HEMP-NP-0001", gi="x", ledger="NP", anchor_block=anchor.hash)
    assert "<h2>VALID: HEMP-NP-0001" in post(qr=batch_qr)
    assert "<h2>UNKNOWN" in post(qr="hello")
    assert "Choose a PDF" in post()


def test_admin_pages(app):
    c = login(app, "admin")
    audit = c.get("/audit").get_data(as_text=True)
    assert "login_ok" in audit and "Admin-Pass-123" not in audit
    assert "delivery" in c.get("/users").get_data(as_text=True)


# ------------------------------------------------------------------ reloading and no data

def test_reopens_after_a_new_run_and_shows_no_data_when_broken(app, world_dirs):
    c = login(app, "admin")
    holder = app.config["HOLDER"]
    first = holder.get()[0]
    manifest = world_dirs / "data" / "world.json"
    original = manifest.read_text()
    try:
        manifest.write_text(original.replace('"complete": true', '"complete": false'))
        os.utime(manifest, ns=(time.time_ns(), time.time_ns() + 10**9))
        r = c.get("/overview")
        assert r.status_code == 503 and "NO DATA" in r.get_data(as_text=True)
        assert c.get("/audit").status_code == 200  # pages that do not need the world still work
    finally:
        manifest.write_text(original)
        os.utime(manifest, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
    assert c.get("/overview").status_code == 200 and holder.get()[0] is not first


# ------------------------------------------------------------------ online and the offline copy

def test_online_then_offline_shows_the_same_counts_with_the_snapshot_time(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    summary = run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    users = UserStore(tmp_path / "users.db")
    users.create_user("admin", "admin", PW["admin"])

    def overview_numbers(app):
        c = login(app, "admin")
        page = c.get("/overview").get_data(as_text=True)
        return page, re.findall(r'<span class="num">(\d+)</span>', page)

    online_page, online = overview_numbers(create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False))
    assert "OFFLINE MODE" not in online_page
    monkeypatch.setenv("GI_MODE", "auto")  # Wi-Fi off: Atlas unreachable
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: (_ for _ in ()).throw(
        __import__("pymongo").errors.ServerSelectionTimeoutError("timed out")))
    offline_page, offline = overview_numbers(create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False))
    assert f"OFFLINE MODE: data as of {summary['snapshot_at']}" in offline_page
    assert offline == online


# ------------------------------------------------------------------ review fixes

class Down:
    """A collection whose server has stopped answering."""
    def __getattr__(self, name):
        def fail(*a, **k):
            from pymongo.errors import ServerSelectionTimeoutError
            raise ServerSelectionTimeoutError("timed out")
        return fail


@pytest.fixture
def online_app(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    summary = run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    UserStore(tmp_path / "users.db").create_user("admin", "admin", PW["admin"])
    app = create_app(tmp_path / "data", tmp_path / "out", tmp_path / "users.db", background_reload=False)
    return app, fake, summary, monkeypatch


def test_atlas_dropping_mid_session_switches_to_the_offline_copy_and_back(online_app):
    app, fake, summary, monkeypatch = online_app
    c = login(app, "admin")
    assert "OFFLINE MODE" not in c.get("/overview").get_data(as_text=True)
    monkeypatch.setattr(FakeAtlas, "get_collection", lambda self, name: Down())  # Wi-Fi off
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: (_ for _ in ()).throw(
        __import__("pymongo").errors.ServerSelectionTimeoutError("timed out")))
    r = c.get("/overview")
    assert r.status_code == 302
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert f"OFFLINE MODE: data as of {summary['snapshot_at']}" in page
    holder = app.config["HOLDER"]
    holder._retry_at = 0  # five minutes later, Atlas still down: keep the copy
    assert "OFFLINE MODE" in c.get("/overview").get_data(as_text=True)
    monkeypatch.undo()  # Wi-Fi back
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    holder._retry_at = 0
    assert "OFFLINE MODE" not in c.get("/overview").get_data(as_text=True)


def test_a_world_built_offline_opens_offline_even_when_atlas_answers(world_dirs, monkeypatch):
    fake = FakeAtlas()  # Atlas reachable but holding nothing of this world
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    app = create_app(world_dirs / "data", world_dirs / "out", world_dirs / "users.db", background_reload=False)
    page = login(app, "admin").get("/overview").get_data(as_text=True)
    assert "OFFLINE MODE: local data from the last offline run" in page and "NO DATA" not in page


def test_waybills_print_prices_so_only_admins_download_them(app):
    employee = login(app, "employee")
    assert "admin only (shows prices)" in employee.get("/documents").get_data(as_text=True)
    w = app.config["HOLDER"].get()[0]
    waybill = next(d["doc_id"] for d in webdata.documents(w) if d["type"] == "customs_waybill")
    assert employee.get(f"/documents/{waybill}/download").status_code == 403
    assert login(app, "admin").get(f"/documents/{waybill}/download").status_code == 200


@pytest.mark.parametrize("text", [
    '{"doc_id": {"$ne": null}, "twin_sha256": "a", "ledger": "NP", "block": "b"}',
    '{"doc_id": "x", "twin_sha256": "a", "ledger": ["NP"], "block": "b"}',
    '{"batch_id": "HEMP-NP-0001", "gi": "x", "ledger": {"a": 1}, "anchor_block": "b"}',
    '{"doc_id": "", "twin_sha256": "a", "ledger": "NP", "block": "b"}',
])
def test_odd_qr_values_are_unknown_not_errors(app, text):
    c = login(app, "delivery")
    t = token(c, "/verify")
    r = c.post("/verify", data={"csrf": t, "qr": text})
    assert r.status_code == 200 and "<h2>UNKNOWN" in r.get_data(as_text=True)


def test_a_junk_pdf_named_like_a_document_reveals_nothing(app):
    import io
    c = login(app, "delivery")
    t = token(c, "/verify")
    r = c.post("/verify", data={"csrf": t, "pdf": (io.BytesIO(b"%PDF-1.4 junk"), "customs_waybill__ORD-0001.pdf")},
               content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert "<h2>UNKNOWN" in page and "ORD-0001" not in page


def test_signing_out_revokes_a_copied_cookie(app):
    c = login(app, "admin")
    cookie = c.get_cookie("gi_session").value
    t = token(c, "/overview")
    c.post("/logout", data={"csrf": t})
    thief = app.test_client()
    thief.set_cookie("gi_session", cookie)
    assert thief.get("/users").status_code == 302


def test_error_details_are_for_admins_only(app, world_dirs):
    manifest = world_dirs / "data" / "world.json"
    original = manifest.read_text()
    try:
        manifest.write_text("{")
        os.utime(manifest, ns=(time.time_ns(), time.time_ns() + 3 * 10**9))
        public = app.test_client().get("/login").get_data(as_text=True)
        delivery = login(app, "delivery").get("/shipments").get_data(as_text=True)
        admin = login(app, "admin").get("/overview").get_data(as_text=True)
        for page in (public, delivery):
            assert "NO DATA. Sign in as an admin" in page and str(world_dirs) not in page
        assert "WorldError" in admin
    finally:
        manifest.write_text(original)
        os.utime(manifest, ns=(time.time_ns(), time.time_ns() + 4 * 10**9))


def test_a_pending_run_is_announced(app, world_dirs):
    pending = world_dirs / "data.new"
    pending.mkdir()
    try:
        assert "is in progress or was interrupted" in login(app, "delivery").get("/shipments").get_data(as_text=True)
    finally:
        pending.rmdir()


def test_reloading_in_the_background_does_not_block_pages(world_dirs):
    import threading
    from gi_trace.seed import open_world
    calls = []

    def slow(d, mode):
        calls.append(mode)
        if len(calls) > 1:
            time.sleep(2)  # like waiting for Atlas
        return open_world(d, role="read", mode=mode)

    app = create_app(world_dirs / "data", world_dirs / "out", world_dirs / "users.db", opener=slow)
    c = login(app, "employee")
    first = app.config["HOLDER"].get()[0]
    manifest = world_dirs / "data" / "world.json"
    os.utime(manifest, ns=(time.time_ns(), time.time_ns() + 5 * 10**9))
    t = time.perf_counter()
    assert c.get("/overview").status_code == 200 and time.perf_counter() - t < 1.5  # old data, no wait
    for _ in range(50):
        if not app.config["HOLDER"].loading:
            break
        time.sleep(0.1)
    assert app.config["HOLDER"].get()[0] is not first
