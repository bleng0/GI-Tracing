"""Step 5: dashboard accounts, scrypt hashes, lockout, audit log."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gi_trace.web import create_user, users
from gi_trace.web.users import LOCK_SECONDS, UserError, UserStore, hash_password, verify_password

PASSWORDS = {"admin": "Adm1n-Mango-Kite!", "employee": "Empl0yee-River-77", "delivery": "Del1very-Cedar-42"}
NAMES = {"admin": "Admin@Example.org", "employee": "emp.one@example.org", "delivery": "driver_7"}


def answers(*values):
    it = iter(values)
    return lambda prompt="": next(it)


def make(db, role, name=None, password=None, repeat=None):
    password = password or PASSWORDS[role]
    log = []
    code = create_user.run(db, ask=answers(name or NAMES[role], role),
                           ask_hidden=answers(password, repeat if repeat is not None else password), log=log.append)
    return code, log


def audit(db):
    with sqlite3.connect(db) as conn:
        return [tuple(r) for r in conn.execute("SELECT event, username, detail FROM audit_log ORDER BY id")]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "users.db"


# ------------------------------------------------------------------ hashing

def test_hashes_are_salted_scrypt_and_verify():
    a, b = hash_password("same-password"), hash_password("same-password")
    assert a != b and a.startswith("scrypt$16384$8$1$")
    assert verify_password("same-password", a) and not verify_password("same-passworD", a)


@pytest.mark.parametrize("stored", ["", "x", "scrypt$1$8$1$00$00", "bcrypt$16384$8$1$00$00", "scrypt$16384$8$1$zz$00", None])
def test_malformed_hash_never_verifies(stored):
    assert verify_password("anything", stored) is False


# ------------------------------------------------------------------ creating accounts

def test_three_accounts_can_be_created_and_log_in(db):
    for role in ("admin", "employee", "delivery"):
        code, log = make(db, role)
        assert code == 0, log
    store = UserStore(db)
    for role in ("admin", "employee", "delivery"):
        r = store.authenticate(NAMES[role], PASSWORDS[role])
        assert (r.ok, r.role, r.username) == (True, role, NAMES[role].strip().lower())


def test_email_usernames_are_case_insensitive(db):
    make(db, "admin", name="  Bruce.C@Example.COM ")
    assert UserStore(db).authenticate("bruce.c@example.com", PASSWORDS["admin"]).ok


@pytest.mark.parametrize("kwargs,message", [
    ({"repeat": "something-else"}, "do not match"),
    ({"password": "short"}, "8 to 1024"),
    ({"password": " " * 12}, "only spaces"),
    ({"name": "longname1", "password": "LongName1"}, "cannot be the username"),
    ({"name": "a b"}, "username must be"),
    ({"name": "x"}, "username must be"),
])
def test_bad_input_creates_nothing(db, kwargs, message):
    code, log = make(db, "delivery", **kwargs)
    assert code == 1 and message in log[-1]
    if db.exists():
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_unknown_role_and_duplicate_are_refused(db):
    log = []
    assert create_user.run(db, ask=answers("someone", "superuser"), ask_hidden=answers("Pass-word-123", "Pass-word-123"),
                           log=log.append) == 1
    assert "role must be one of admin, employee, delivery" in log[-1]
    assert make(db, "admin")[0] == 0
    code, log = make(db, "admin", password="Another-Pass-99")
    assert code == 1 and "already exists" in log[-1]
    assert UserStore(db).authenticate(NAMES["admin"], PASSWORDS["admin"]).ok  # the original password still works


def test_cancelled_prompt_creates_nothing(db):
    def eof(prompt=""):
        raise EOFError
    log = []
    assert create_user.run(db, ask=eof, log=log.append) == 1 and "Cancelled" in log[-1]


# ------------------------------------------------------------------ lockout

def test_five_wrong_passwords_lock_the_account_for_five_minutes(db):
    make(db, "employee")
    store, name, t0 = UserStore(db), NAMES["employee"], 1_000_000.0
    for i in range(5):
        assert store.authenticate(name, f"wrong-{i}", now=t0 + i).reason == "bad_credentials"
    assert store.authenticate(name, PASSWORDS["employee"], now=t0 + 10).reason == "locked"
    assert store.authenticate(name, PASSWORDS["employee"], now=t0 + 4 + LOCK_SECONDS - 1).reason == "locked"
    assert store.authenticate(name, PASSWORDS["employee"], now=t0 + 4 + LOCK_SECONDS + 1).ok
    # the count starts again from zero after the lock
    for i in range(4):
        store.authenticate(name, "wrong", now=t0 + 1000 + i)
    assert store.authenticate(name, PASSWORDS["employee"], now=t0 + 1010).ok


def test_a_correct_login_resets_the_count(db):
    make(db, "delivery")
    store, name = UserStore(db), NAMES["delivery"]
    for _ in range(2):
        for _ in range(4):
            store.authenticate(name, "wrong")
        assert store.authenticate(name, PASSWORDS["delivery"]).ok


def test_unknown_user_fails_like_a_wrong_password(db):
    make(db, "admin")
    r = UserStore(db).authenticate("nobody@example.org", "whatever-123")
    assert (r.ok, r.reason) == (False, "bad_credentials")
    assert UserStore(db).authenticate("not valid!", "x").reason == "bad_credentials"


def test_every_attempt_is_audited(db):
    make(db, "admin")
    store, name = UserStore(db), NAMES["admin"].lower()
    for _ in range(5):
        store.authenticate(name, "wrong")
    store.authenticate(name, PASSWORDS["admin"])
    events = [e for e, _, _ in audit(db)]
    assert events == ["user_created"] + ["login_failed"] * 5 + ["account_locked", "login_refused_locked"]
    assert audit(db)[5][2] == "wrong password (5 of 5)"


# ------------------------------------------------------------------ no password on disk

def test_no_password_appears_in_any_file(db, tmp_path):
    typed = list(PASSWORDS.values()) + ["Wrong-Guess-123", "Typo-Adm1n-Mango"]
    for role in PASSWORDS:
        make(db, role)
    store = UserStore(db)
    for role, pw in PASSWORDS.items():
        store.authenticate(NAMES[role], pw)
        store.authenticate(NAMES[role], "Wrong-Guess-123")
    store.authenticate("nobody", "Typo-Adm1n-Mango")
    files = [f for f in tmp_path.rglob("*") if f.is_file()]
    assert files
    for f in files:
        data = f.read_bytes()
        for pw in typed:
            for form in (pw.encode(), pw.encode("utf-16-le"), pw.encode().hex().encode()):
                assert form not in data, (f, pw)


# ------------------------------------------------------------------ stress

def test_concurrent_wrong_passwords_lock_exactly_once(db):
    """20 simultaneous wrong guesses: no lost counts, one lock, the rest refused."""
    make(db, "employee")
    store, name = UserStore(db), NAMES["employee"]
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda i: store.authenticate(name, f"guess-{i}"), range(20)))
    assert not any(r.ok for r in results)
    events = [e for e, _, _ in audit(db)[1:]]
    assert events.count("account_locked") == 1
    assert events.count("login_failed") == 5
    assert events.count("login_refused_locked") == 15
    assert store.authenticate(name, PASSWORDS["employee"]).reason == "locked"


def test_correct_logins_survive_concurrent_attackers_on_other_accounts(db):
    for role in PASSWORDS:
        make(db, role)
    store = UserStore(db)

    def attempt(i):
        if i % 3 == 0:
            return store.authenticate(NAMES["admin"], PASSWORDS["admin"]).ok
        return store.authenticate(NAMES["delivery"], f"bad-{i}").ok

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(attempt, range(48)))
    assert all(results[i] for i in range(0, 48, 3))
    assert store.authenticate(NAMES["delivery"], PASSWORDS["delivery"]).reason == "locked"
    assert store.authenticate(NAMES["admin"], PASSWORDS["admin"]).ok


def test_one_login_is_fast_enough_and_unknown_users_cost_the_same(db):
    make(db, "admin")
    store = UserStore(db)
    store.authenticate("warm-up", "x" * 8)

    def timed(name, pw):
        t = time.perf_counter()
        store.authenticate(name, pw)
        return time.perf_counter() - t

    known = min(timed(NAMES["admin"], "Wrong-Pass-1") for _ in range(3))
    unknown = min(timed("ghost@example.org", "Wrong-Pass-1") for _ in range(3))
    assert known < 1.0
    assert 0.5 < unknown / known < 2.0


# ------------------------------------------------------------------ review fixes

def test_a_locked_account_does_not_answer_faster(db):
    make(db, "admin")
    store, name = UserStore(db), NAMES["admin"]
    for _ in range(5):
        store.authenticate(name, "wrong-pass")

    def timed():
        t = time.perf_counter()
        r = store.authenticate(name, "wrong-pass")
        return time.perf_counter() - t, r.reason

    locked = [timed() for _ in range(3)]
    assert all(reason == "locked" for _, reason in locked)
    normal = min(timed_unknown(store) for _ in range(3))
    assert 0.5 < min(t for t, _ in locked) / normal < 2.0


def timed_unknown(store):
    t = time.perf_counter()
    store.authenticate("ghost@example.org", "wrong-pass")
    return time.perf_counter() - t


def test_a_name_typed_for_an_unknown_account_is_not_stored(db):
    make(db, "admin")
    UserStore(db).authenticate("Correct-Horse-1", NAMES["admin"])  # password typed into the username box
    with sqlite3.connect(db) as conn:
        assert b"correct-horse-1" not in db.read_bytes().lower()
        assert conn.execute("SELECT username, detail FROM audit_log ORDER BY id DESC").fetchone() == (None, "unknown user")


def test_audit_log_keeps_only_the_newest_rows(db, monkeypatch):
    monkeypatch.setattr(users, "AUDIT_MAX_ROWS", 5)
    make(db, "admin")
    store = UserStore(db)
    for _ in range(12):
        store.authenticate("ghost@example.org", "wrong-pass")
    with sqlite3.connect(db) as conn:
        ids = [i for (i,) in conn.execute("SELECT id FROM audit_log ORDER BY id")]
    assert ids == [9, 10, 11, 12, 13]


def test_console_that_cannot_hide_typing_is_refused(db):
    import getpass
    import warnings

    def echoing(prompt=""):
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
        return "Visible-Pass-1"

    log = []
    assert create_user.run(db, ask=answers("someone", "admin"), ask_hidden=echoing, log=log.append) == 1
    assert "cannot hide the password" in log[-1]


@pytest.mark.parametrize("mangle", [
    lambda h: h[:-62],                       # digest shortened to 1 byte
    lambda h: h.replace("$16384$", "$ 16384$"),
    lambda h: h.replace("$16384$", "$16_384$"),
    lambda h: h.replace("$16384$", "$1024$"),  # weaker cost
    lambda h: h.upper(),
])
def test_tampered_hash_formats_never_verify(mangle):
    stored = hash_password("Pass-word-123")
    assert verify_password("Pass-word-123", stored)
    assert not verify_password("Pass-word-123", mangle(stored))


def test_passwords_match_across_unicode_forms(db):
    import unicodedata
    pw = "Café-Crème-2026"
    make(db, "admin", password=unicodedata.normalize("NFC", pw))
    assert UserStore(db).authenticate(NAMES["admin"], unicodedata.normalize("NFD", pw)).ok


def test_odd_input_is_refused_not_crashed(db):
    make(db, "admin")
    store = UserStore(db)
    assert store.authenticate(None, "x").reason == "bad_credentials"
    assert store.authenticate(123, None).reason == "bad_credentials"
    assert store.authenticate(NAMES["admin"], None).reason == "bad_credentials"
    with pytest.raises(UserError):
        store.create_user("ok-name", "admin", None)


def test_default_database_is_the_tracked_accounts_folder():
    from pathlib import Path
    assert users.DEFAULT_DB == Path(users.__file__).resolve().parents[2] / "accounts" / "users.db"
