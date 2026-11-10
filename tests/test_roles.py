"""Step 8: access control. Every route the dashboard has, for admin, employee, delivery
and a logged-out visitor, with GET and POST. The expected table is written out here from
PLAN.md section 4, not read from app.PAGES, so a wrong entry in PAGES fails too."""
import io
import re

import pytest

from gi_trace.run_all import run_pipeline
from gi_trace.web.app import create_app
from gi_trace.web.users import UserStore

PW = {"admin": "Admin-Pass-123", "employee": "Employee-Pass-123", "delivery": "Delivery-Pass-123"}
ROLES = ("admin", "employee", "delivery", None)  # None: logged out
A, E, D = "admin", "employee", "delivery"
LAB = "hemp_lab_assay:HEMP-NP-0001"
WAYBILL = "customs_waybill:ORD-0001"

# path -> roles that get the page; every other signed-in role gets 403, logged-out visitors go to /login
GET = {
    "/overview": {A, E}, "/batches": {A, E}, "/batches/HEMP-NP-0001": {A, E},
    "/shipments": {A, E, D}, "/documents": {A, E},
    f"/documents/{LAB}/download": {A, E}, f"/documents/{WAYBILL}/download": {A},  # waybills print prices
    "/verify": {A, E, D}, "/query": {A}, "/query?example=0": {A}, "/ledger": {A}, "/ledger?j=PK": {A},
    "/audit": {A}, "/users": {A},
}
POST = {"/verify": {A, E, D}, "/query": {A}}
# Every URL rule must appear here: a new route without a row fails test_every_route_is_in_the_matrix.
RULES = {
    "/overview": "/overview", "/batches": "/batches", "/batches/<batch_id>": "/batches/HEMP-NP-0001",
    "/shipments": "/shipments", "/documents": "/documents", "/documents/<doc_id>/download": f"/documents/{LAB}/download",
    "/verify": "/verify", "/query": "/query", "/ledger": "/ledger", "/audit": "/audit", "/users": "/users",
    "/login": None, "/logout": None, "/": None, "/static/<path:filename>": None,
}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("roles")
    mp = pytest.MonkeyPatch()
    mp.setenv("GI_MODE", "offline")
    run_pipeline(root / "data", root / "out", sample="core", log=lambda *_: None)
    mp.undo()
    users = UserStore(root / "users.db")
    for role, pw in PW.items():
        users.create_user(role, role, pw)
    return create_app(root / "data", root / "out", root / "users.db", background_reload=False)


def csrf(client, path="/login"):
    page = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


@pytest.fixture(scope="module")
def clients(app):
    out = {None: app.test_client()}
    for role in (A, E, D):
        c = app.test_client()
        r = c.post("/login", data={"username": role, "password": PW[role], "csrf": csrf(c)})
        assert r.status_code == 302
        out[role] = c
    return out


def form_for(path):
    if path == "/verify":
        return {"qr": "hello"}
    return {"collection": "certificates", "filter": "{}", "limit": "5"}


# Other ways into the same POST routes: the CSV download and the PDF upload.
POST_VARIANTS = {
    "/query csv": ("/query", {A}, lambda: {"collection": "certificates", "filter": "{}", "limit": "5", "output": "csv"}),
    "/verify upload": ("/verify", {A, E, D}, lambda: {"pdf": (io.BytesIO(b"%PDF-1.4 not ours"), "x.pdf")}),
    "/query with a file": ("/query", {A}, lambda: {"collection": "certificates", "filter": "{}", "limit": "5",
                                                   "pdf": (io.BytesIO(b"x"), "x.pdf")}),
}


def test_every_route_is_in_the_matrix(app):
    assert {r.rule for r in app.url_map.iter_rules()} == set(RULES)
    covered = {RULES[r.rule] for r in app.url_map.iter_rules() if RULES[r.rule]}
    assert covered <= {p.split("?")[0] for p in GET} | set(GET)
    posts = {r.rule for r in app.url_map.iter_rules() if "POST" in r.methods}
    assert posts == {"/login", "/logout"} | set(POST)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
@pytest.mark.parametrize("path", sorted(GET))
def test_get_matrix(clients, role, path):
    r = clients[role].get(path)
    if role is None:
        assert r.status_code == 302 and r.headers["Location"].endswith("/login"), (role, path)
    elif role in GET[path]:
        assert r.status_code == 200, (role, path, r.status_code)
    else:
        assert r.status_code == 403, (role, path, r.status_code)
        assert "Your role cannot open this page" in r.get_data(as_text=True)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
@pytest.mark.parametrize("path", sorted(POST))
def test_post_matrix(clients, role, path):
    c = clients[role]
    r = c.post(path, data={"csrf": csrf(c, "/login" if role is None else "/shipments"), **form_for(path)})
    if role is None:
        assert r.status_code == 302 and r.headers["Location"].endswith("/login"), (role, path)
    elif role in POST[path]:
        assert r.status_code == 200, (role, path, r.status_code)
    else:
        assert r.status_code == 403, (role, path, r.status_code)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
@pytest.mark.parametrize("variant", sorted(POST_VARIANTS))
def test_post_variants(clients, role, variant):
    path, allowed, form = POST_VARIANTS[variant]
    c = clients[role]
    r = c.post(path, data={"csrf": csrf(c, "/login" if role is None else "/shipments"), **form()},
               content_type="multipart/form-data")
    if role is None:
        assert r.status_code == 302 and r.headers["Location"].endswith("/login"), (role, variant)
    elif role in allowed:
        assert r.status_code == 200, (role, variant, r.status_code)
        if variant == "/query csv":
            assert r.mimetype == "text/csv"
    else:
        assert r.status_code == 403, (role, variant, r.status_code)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
@pytest.mark.parametrize("path", sorted(POST) + ["/logout", "/login"])
def test_posts_without_csrf_are_refused_for_everyone(clients, role, path):
    assert clients[role].post(path, data=form_for(path)).status_code == 400
    assert clients[role].post(path, data={"csrf": "forged", **form_for(path)}).status_code == 400


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
@pytest.mark.parametrize("path", sorted(set(GET) - set(POST) - {"/ledger?j=PK", "/query?example=0"}))
def test_get_only_pages_refuse_post(clients, role, path):
    c = clients[role]
    t = csrf(c, "/login" if role is None else "/shipments")
    assert c.post(path, data={"csrf": t}).status_code == 405


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "logged-out")
def test_home_login_logout(app, clients, role):
    c = clients[role]
    first = {A: "/overview", E: "/overview", D: "/shipments", None: "/login"}[role]
    assert c.get("/").headers["Location"].endswith(first)
    r = c.get("/login")  # signed in: back to the home page, which picks the role's first page
    assert (r.status_code, r.headers.get("Location", "")) == ((200, "") if role is None else (302, "/"))
    assert c.get("/static/style.css").status_code == 200  # the stylesheet is public, like the sign-in page


@pytest.mark.parametrize("role", (A, E, D))
def test_logout_ends_each_role_even_for_a_copied_cookie(app, role):
    c = app.test_client()
    c.post("/login", data={"username": role, "password": PW[role], "csrf": csrf(c)})
    token = csrf(c, "/shipments")
    cookie = c.get_cookie("gi_session").value
    assert c.post("/logout", data={"csrf": token}).status_code == 302
    for path in GET:
        assert c.get(path).status_code == 302, path
    thief = app.test_client()  # replays the cookie from before sign-out, CSRF token included
    thief.set_cookie("gi_session", cookie)
    for path in POST:
        r = thief.post(path, data={"csrf": token, **form_for(path)})
        assert r.status_code in (302, 400), (path, r.status_code)  # signed out: never runs the page
        assert r.status_code != 200
    assert thief.get("/users").status_code == 302


def test_menu_shows_exactly_the_allowed_pages(clients):
    menus = {A: {"/overview", "/batches", "/shipments", "/documents", "/verify", "/query", "/ledger", "/audit", "/users"},
             E: {"/overview", "/batches", "/shipments", "/documents", "/verify"},
             D: {"/shipments", "/verify"}}
    for role, expected in menus.items():
        page = clients[role].get("/shipments").get_data(as_text=True)
        nav = re.search(r"<nav>(.*?)</nav>", page, re.S).group(1)
        assert set(re.findall(r'href="([^"]+)"', nav)) == expected, role


def test_a_cookie_signed_with_another_key_is_ignored(app):
    """The role is kept in the signed cookie, so this is what stops a visitor writing role=admin into it."""
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    other = Flask("x")
    other.secret_key = "not-the-dashboard-key"
    forged = SecureCookieSessionInterface().get_signing_serializer(other).dumps(
        {"user": "delivery", "role": "admin", "sid": "x", "csrf": "t"})
    c = app.test_client()
    c.set_cookie("gi_session", forged)
    assert c.get("/users").status_code == 302


def test_a_valid_session_with_an_unknown_sid_is_signed_out(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user="admin", role="admin", sid="never-issued", csrf="t")
    assert c.get("/users").status_code == 302
    with c.session_transaction() as s:
        s.update(user="admin", role="admin", sid="never-issued", csrf="t")
    r = c.post("/query", data={"csrf": "t", **form_for("/query")})
    assert r.status_code in (302, 400) and r.status_code != 200
