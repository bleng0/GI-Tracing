"""The local dashboard (Flask, 127.0.0.1 only):  python -m gi_trace.web

Pages and who sees them follow PLAN.md section 4. Every route checks the role:
logged-out visitors go to the login page, a wrong role gets 403. Forms carry a
CSRF token; the session cookie is HttpOnly and SameSite=Strict; responses forbid
framing, scripts from elsewhere and caching. The world is opened with the read-only
Atlas user (or the offline copy) and reopened when a run is published. If Atlas
stops answering, the dashboard switches to the offline copy and tries Atlas again
every five minutes. Sessions live in this process: signing out ends them for good.
"""
from __future__ import annotations

import hmac
import os
import secrets
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path

from pymongo.errors import ConnectionFailure
from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for

from ..crypto import sha256_hex
from ..docgen.verify import verify_pdf, verify_qr
from ..seed import staging_dir
from ..storage import mtime_ns
from . import data, ledger_view, query
from .users import DEFAULT_DB, UserStore

ROOT = Path(__file__).resolve().parents[2]
ADMIN, EMPLOYEE, DELIVERY = "admin", "employee", "delivery"
ALL = (ADMIN, EMPLOYEE, DELIVERY)
STAFF = (ADMIN, EMPLOYEE)
# endpoint, menu label, roles: the menu and the route checks both come from this table
PAGES = [
    ("overview", "Overview", STAFF), ("batches", "Batches", STAFF), ("shipments", "Shipments", ALL),
    ("documents", "Documents", STAFF), ("verify", "Verify", ALL),
    ("query", "Query console", (ADMIN,)), ("ledger", "Ledger", (ADMIN,)),
    ("audit", "Audit log", (ADMIN,)), ("users", "Users", (ADMIN,)),
]
ROLES_FOR = {endpoint: roles for endpoint, _, roles in PAGES}
LOGIN_FAILED = "Wrong username or password, or the account is locked for a few minutes."
# Document types whose PDF prints order prices: only admins may download them (PLAN.md section 4).
PRICED_DOCUMENTS = {"customs_waybill"}


class WorldHolder:
    """Holds the opened world.

    - Reopens it when data/world.json changes (a run was published). The first open
      waits; later ones run in the background while pages keep showing the old data.
    - fall_back() switches to the offline copy when Atlas stops answering mid-session.
    - While on that copy, it retries Atlas every RETRY_SECONDS in the background and
      keeps the copy if Atlas is still unreachable.
    """
    RETRY_SECONDS = 300

    def __init__(self, data_dir: Path, opener, background: bool = True):
        self.data_dir, self.opener, self.background = Path(data_dir), opener, background
        self._lock = threading.Lock()
        self.world, self.error, self.loading = None, None, False
        self._seen, self._retry_at = ("never",), None

    def get(self):
        how = None
        with self._lock:
            stamp = mtime_ns(self.data_dir / "world.json")
            retry = self._retry_at is not None and time.monotonic() >= self._retry_at
            changed = stamp != self._seen
            if (changed or retry) and not self.loading:
                how = "retry" if not changed and self.world is not None else "open"
                self.loading, self._seen, self._retry_at = True, stamp, None
                if self.world is None or not self.background:
                    how += "-now"
        if how is not None:
            keep = how.startswith("retry")
            if how.endswith("-now"):
                self._load(None, keep_on_failure=keep)
            else:
                threading.Thread(target=self._load, args=(None, keep), daemon=True).start()
        return self.world, self.error

    def fall_back(self) -> None:
        with self._lock:
            if self.world is None or self.world.offline or self.loading:
                return
            self.loading = True
        self._load("offline", keep_on_failure=False)

    def _load(self, mode, keep_on_failure: bool) -> None:
        try:
            world = self.opener(self.data_dir, mode)
            world.conn.close()  # pages open their own read-only connection per request
            world.valid_chains = set(world.ledgers)  # open_world has just verified every chain
            error = None
        except Exception as e:  # no data yet, or data that fails verification
            world, error = None, f"{type(e).__name__}: {e}"
        with self._lock:
            self.loading = False
            still_offline = world is not None and world.offline
            if not (keep_on_failure and (world is None or still_offline)):
                self.world, self.error = world, error
            current = self.world
            if current is not None and current.offline and getattr(current, "built_on", None) == "atlas" \
                    and os.environ.get("GI_MODE", "auto").strip().lower() in ("", "auto"):
                self._retry_at = time.monotonic() + self.RETRY_SECONDS


def _banners(world, error, loading: bool, run_pending: bool, is_admin: bool) -> list[dict]:
    out = []
    if world is None and (loading or error is None):
        out.append({"kind": "offline", "text": "Opening the data (up to 30 s if Atlas is slow). Reload in a moment."})
    elif world is None:
        detail = f": {error}" if is_admin else ". Sign in as an admin to see why"
        out.append({"kind": "error", "text": f"NO DATA{detail}. Run python -m gi_trace.run_all, then reload this page."})
    elif world.offline and world.snapshot_at:
        out.append({"kind": "offline", "text": f"OFFLINE MODE: data as of {world.snapshot_at} (copy taken after the "
                                               "last online run). Atlas could not be reached."})
    elif world.offline:
        out.append({"kind": "offline", "text": "OFFLINE MODE: local data from the last offline run."})
    if run_pending:
        out.append({"kind": "offline", "text": "A run (python -m gi_trace.run_all) is in progress or was interrupted; "
                                               "this shows the data from the previous run."})
    return out


def create_app(data_dir=ROOT / "data", out_dir=ROOT / "out", users_db=DEFAULT_DB, opener=None,
               background_reload: bool = True) -> Flask:
    from ..seed import open_world

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32),  # new on every start: restarting signs everyone out
        SESSION_COOKIE_NAME="gi_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )
    holder = WorldHolder(data_dir, opener or (lambda d, mode: open_world(d, role="read", mode=mode)),
                         background=background_reload)
    sessions: dict[str, str] = {}  # live session id -> username; signing out removes it
    users = UserStore(users_db)
    certs_dir = Path(out_dir) / "certificates"
    app.config.update(HOLDER=holder, USERS=users, DATA_DIR=Path(data_dir), CERTS_DIR=certs_dir)

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def check_session_and_csrf():
        if "user" in session and sessions.get(session.get("sid")) != session.get("user"):
            session.clear()  # signed out, or from before a restart: the cookie alone is not enough
        if request.method == "POST":
            sent, expected = request.form.get("csrf", ""), session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400)

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; form-action 'self'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def common():
        world, error = holder.get()
        role = session.get("role")
        menu = [(e, label) for e, label, roles in PAGES if role in roles]
        banners = _banners(world, error, holder.loading, staging_dir(holder.data_dir).exists(), role == ADMIN)
        return {"banners": banners, "menu": menu, "user": session.get("user"), "role": role,
                "csrf_token": csrf_token, "priced": PRICED_DOCUMENTS}

    def allowed(page: str):
        """Only the roles PAGES gives `page`; detail routes name the list page they belong to."""
        roles = ROLES_FOR[page]

        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if "user" not in session:
                    return redirect(url_for("login"))
                if session.get("role") not in roles:
                    abort(403)
                return fn(*args, **kwargs)
            wrapper.page = page
            return wrapper
        return deco

    def world_or_none():
        return holder.get()[0]

    def no_data():
        return render_template("nodata.html", title="No data"), 503

    # ---------------------------------------------------------------- login

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            result = users.authenticate(request.form.get("username", ""), request.form.get("password", ""))
            if result.ok:
                session.clear()
                sid = secrets.token_urlsafe(32)
                sessions[sid] = result.username
                session.update(user=result.username, role=result.role, sid=sid, csrf=secrets.token_urlsafe(32))
                return redirect(url_for("home"))
            flash(LOGIN_FAILED)
            return render_template("login.html", title="Sign in"), 401
        if "user" in session:
            return redirect(url_for("home"))
        return render_template("login.html", title="Sign in")

    @app.route("/logout", methods=["POST"])
    def logout():
        sessions.pop(session.get("sid"), None)
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def home():
        role = session.get("role")
        if role is None:
            return redirect(url_for("login"))
        return redirect(url_for(next(e for e, _, roles in PAGES if role in roles)))

    # ---------------------------------------------------------------- pages

    def with_sql(fn):
        world = world_or_none()
        if world is None:
            return no_data()
        conn = data.sql_connect(Path(world.data_dir) / "gi_trace.db")
        try:
            return fn(world, conn)
        finally:
            conn.close()

    @app.route("/overview")
    @allowed("overview")
    def overview():
        return with_sql(lambda w, conn: render_template(
            "overview.html", title="Overview", o=data.overview(conn, w, with_prices=session["role"] == ADMIN)))

    @app.route("/batches")
    @allowed("batches")
    def batches():
        return with_sql(lambda w, conn: render_template("batches.html", title="Batches and certificates",
                                                        rows=data.batches(conn, w)))

    @app.route("/batches/<batch_id>")
    @allowed("batches")
    def batch(batch_id):
        def page(w, conn):
            d = data.batch_detail(conn, w, batch_id)
            if d is None:
                abort(404)
            return render_template("batch.html", title=f"Batch {batch_id}", d=d)
        return with_sql(page)

    @app.route("/shipments")
    @allowed("shipments")
    def shipments():
        world = world_or_none()
        if world is None:
            return no_data()
        return render_template("shipments.html", title="Shipments", rows=data.shipments(world))

    @app.route("/documents")
    @allowed("documents")
    def documents():
        world = world_or_none()
        if world is None:
            return no_data()
        return render_template("documents.html", title="Documents", rows=data.documents(world))

    @app.route("/documents/<doc_id>/download")
    @allowed("documents")
    def download(doc_id):
        world = world_or_none()
        if world is None:
            return no_data()
        rec = world.store.get_collection("documents").find_one({"doc_id": doc_id},
                                                               {"_id": 0, "file_name": 1, "sha256": 1, "type": 1})
        if rec and rec.get("type") in PRICED_DOCUMENTS and session["role"] != ADMIN:
            abort(403)
        name = (rec or {}).get("file_name")
        if not isinstance(name, str) or Path(name).name != name or name.startswith("."):
            abort(404)
        path = certs_dir / name
        if path.resolve().parent != certs_dir.resolve() or not path.is_file():
            abort(404)
        pdf = path.read_bytes()
        if sha256_hex(pdf) != rec.get("sha256"):
            return render_template("error.html", title="File does not match",
                                   message="The PDF on disk is not the registered one. Use Verify to check it."), 409
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=name)

    @app.route("/verify", methods=["GET", "POST"])
    @allowed("verify")
    def verify():
        result = None
        if request.method == "POST":
            world = world_or_none()
            if world is None:
                return no_data()
            upload = request.files.get("pdf")
            if upload and upload.filename:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "upload.pdf"  # the visitor's file name is not used to find documents
                    upload.save(path)
                    result = verify_pdf(path, world, valid_chains=world.valid_chains)
            elif request.form.get("qr", "").strip():
                result = verify_qr(request.form["qr"].strip(), world, valid_chains=world.valid_chains)
            else:
                flash("Choose a PDF or paste the text of a QR code.")
        return render_template("verify.html", title="Verify a document or QR code", result=result)

    @app.route("/query", methods=["GET", "POST"], endpoint="query")
    @allowed("query")
    def query_console():
        world = world_or_none()
        if world is None:
            return no_data()
        form = {"collection": "certificates", "filter": "{}", "projection": "", "sort": "",
                "limit": str(query.DEFAULT_LIMIT)}
        example = request.args.get("example")
        if request.method == "GET" and example is not None:
            if not (example.isascii() and example.isdigit() and len(example) < 4 and int(example) < len(query.EXAMPLES)):
                abort(404)
            form.update({k: str(v) for k, v in query.EXAMPLES[int(example)].items() if k in form})
        result, error = None, None
        if request.method == "POST":
            form = {k: request.form.get(k, "") for k in form}
            try:
                q = query.parse(form)
                columns, cells = query.table(query.run(world, q))
            except query.QueryError as e:
                error = str(e)
            else:
                if request.form.get("output") == "csv":
                    return Response("\ufeff" + query.to_csv(columns, cells), mimetype="text/csv",
                                    headers={"Content-Disposition": f'attachment; filename="{q.collection}.csv"'})
                result = {"columns": columns, "rows": [[query.cell_text(v) for v in row] for row in cells]}
        page = render_template("query.html", title="Query console", form=form, result=result, error=error,
                               examples=query.EXAMPLES, collections=query.COLLECTIONS, max_limit=query.MAX_LIMIT,
                               seconds=query.MAX_TIME_MS // 1000)
        return page, (400 if error else 200)

    @app.route("/ledger")
    @allowed("ledger")
    def ledger():
        world = world_or_none()
        if world is None:
            return no_data()
        j = request.args.get("j", "NP")
        if j not in world.ledgers:
            abort(404)
        raw_page = request.args.get("page", "1")
        info = ledger_view.inspect(world, j)
        pages = max(1, -(-len(info["blocks"]) // ledger_view.PAGE_SIZE))
        number = min(int(raw_page), pages) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) < 7 and int(raw_page) > 0 else 1
        start = (number - 1) * ledger_view.PAGE_SIZE
        return render_template("ledger.html", title=f"Ledger {j}", j=j, ledgers=sorted(world.ledgers),
                               info=info, blocks=info["blocks"][start:start + ledger_view.PAGE_SIZE],
                               page=number, pages=pages)

    @app.route("/audit")
    @allowed("audit")
    def audit():
        return render_template("audit.html", title="Audit log", rows=users.recent_audit(200))

    @app.route("/users", endpoint="users")
    @allowed("users")
    def users_page():
        return render_template("users.html", title="Users", rows=users.list_users())

    @app.errorhandler(ConnectionFailure)
    def atlas_dropped(e):
        holder.fall_back()
        if request.method == "GET":
            return redirect(request.full_path)
        return render_template("error.html", title="Atlas stopped answering",
                               message="Switched to the offline copy. Send the form again."), 503

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", title="Bad request",
                               message="The form was out of date or incomplete. Go back, reload and try again."), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", title="Not allowed", message="Your role cannot open this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", title="Not found", message="There is nothing here."), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("error.html", title="File too large", message="PDFs up to 10 MB can be checked."), 413

    return app
