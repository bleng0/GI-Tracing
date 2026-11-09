"""Dashboard accounts: roles, scrypt password hashes, lockout and an audit log.

    data/users.db   kept apart from gi_trace.db, which every run_all re-seeds

Roles are admin, employee and delivery (PLAN.md section 4). Passwords are stored only
as scrypt hashes with a random salt. Five failed logins in a row lock the account for
five minutes, during which even the right password is refused; a successful login
resets the count. Every account creation and login attempt goes into audit_log
(never a password, and never the name typed for an unknown account, which is often a
mistyped password); the newest AUDIT_MAX_ROWS rows are kept. Unknown, locked and
ordinary attempts all cost one scrypt hash, so response time does not reveal which.
Passwords are NFKC-normalised, so the same text typed on another system still matches.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROLES = ("admin", "employee", "delivery")
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "users.db"  # the project's data/, from any folder
MAX_FAILURES = 5
LOCK_SECONDS = 300
MIN_PASSWORD, MAX_PASSWORD = 8, 1024
AUDIT_MAX_ROWS = 10000
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
USERNAME_RE = re.compile(r"[a-z0-9._@+-]{3,254}")
HASH_RE = re.compile(rf"scrypt\${SCRYPT_N}\${SCRYPT_R}\${SCRYPT_P}\$([0-9a-f]{{32}})\$([0-9a-f]{{{SCRYPT_LEN * 2}}})")
# Each scrypt call takes 16 MB (128 * r * n bytes). At most this many run at once, so a
# burst of logins queues briefly instead of using hundreds of MB.
_HASHING = threading.BoundedSemaphore(4)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'employee', 'delivery')),
    password_hash TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    event    TEXT NOT NULL,
    username TEXT,
    detail   TEXT NOT NULL DEFAULT ''
);
"""


class UserError(ValueError):
    pass


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    username: str | None = None
    role: str | None = None
    reason: str = ""  # "ok", "bad_credentials" or "locked"; for logs, not for the login page


def _scrypt(password: str, salt: bytes) -> bytes:
    data = unicodedata.normalize("NFKC", password).encode("utf-8", "surrogatepass")
    with _HASHING:
        return hashlib.scrypt(data, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Only the exact format hash_password writes is accepted (fixed parameters, 16-byte
    salt, 32-byte digest); anything else, including a shortened digest, never verifies."""
    if not isinstance(password, str):
        return False
    match = HASH_RE.fullmatch(stored) if isinstance(stored, str) else None
    if match is None:
        _scrypt(password, bytes(16))  # same cost either way
        return False
    salt, digest = bytes.fromhex(match[1]), bytes.fromhex(match[2])
    return hmac.compare_digest(_scrypt(password, salt), digest)


_DUMMY_HASH: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_hex(16)))
    return _DUMMY_HASH[0]


def normalize_username(username: str) -> str:
    name = username.strip().lower() if isinstance(username, str) else ""
    if not USERNAME_RE.fullmatch(name):
        raise UserError("username must be 3 to 254 characters: letters, digits and . _ @ + - (an email address is fine)")
    return name


def check_password(password: str, username: str) -> None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise UserError(f"password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    if not password.strip():
        raise UserError("password cannot be only spaces")
    if password.strip().lower() == username:
        raise UserError("password cannot be the username")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)  # owner only (on Windows this only clears read-only)
        except OSError:
            pass
        _dummy_hash()  # built now, so the first unknown-user login is not slower than the rest

    def _connect(self) -> sqlite3.Connection:
        """A fresh connection per call, so the store is safe across the web server's threads.
        Autocommit; writes that read-then-update take an IMMEDIATE lock."""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _audit(self, conn, event: str, username: str | None, detail: str = "") -> None:
        cur = conn.execute("INSERT INTO audit_log (ts, event, username, detail) VALUES (?, ?, ?, ?)",
                           (_utc_now(), event, username, detail))
        conn.execute("DELETE FROM audit_log WHERE id <= ?", (cur.lastrowid - AUDIT_MAX_ROWS,))

    def create_user(self, username: str, role: str, password: str) -> str:
        name = normalize_username(username)
        if role not in ROLES:
            raise UserError(f"role must be one of {', '.join(ROLES)}")
        if not isinstance(password, str):
            raise UserError("password must be text")
        check_password(password, name)
        password_hash = hash_password(password)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO users (username, role, password_hash, created_at) VALUES (?, ?, ?, ?)",
                             (name, role, password_hash, _utc_now()))
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise UserError(f"user {name} already exists") from None
            self._audit(conn, "user_created", name, f"role={role}")
            conn.execute("COMMIT")
        finally:
            conn.close()
        return name

    def authenticate(self, username: str, password: str, now: float | None = None) -> AuthResult:
        now = time.time() if now is None else now
        try:
            name = normalize_username(username)
        except UserError:
            name = None
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone() if name else None
            password = password if isinstance(password, str) else ""
            if row is None:
                verify_password(password, _dummy_hash())  # same cost as a real check
                self._audit(conn, "login_failed", None, "unknown user")  # the typed name may be a password
                return AuthResult(False, reason="bad_credentials")
            if row["locked_until"] > now:
                verify_password(password, _dummy_hash())  # a locked account must not answer faster
                self._audit(conn, "login_refused_locked", name)
                return AuthResult(False, reason="locked")
            ok = verify_password(password, row["password_hash"])  # slow: done outside the lock

            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
            if row is None:  # removed meanwhile
                conn.execute("COMMIT")
                return AuthResult(False, reason="bad_credentials")
            if row["locked_until"] > now:  # locked by a concurrent attempt meanwhile
                self._audit(conn, "login_refused_locked", name)
                conn.execute("COMMIT")
                return AuthResult(False, reason="locked")
            if ok:
                conn.execute("UPDATE users SET failed_logins = 0, locked_until = 0 WHERE username = ?", (name,))
                self._audit(conn, "login_ok", name)
                conn.execute("COMMIT")
                return AuthResult(True, name, row["role"], "ok")
            failures = row["failed_logins"] + 1
            self._audit(conn, "login_failed", name, f"wrong password ({failures} of {MAX_FAILURES})")
            if failures >= MAX_FAILURES:
                conn.execute("UPDATE users SET failed_logins = 0, locked_until = ? WHERE username = ?",
                             (now + LOCK_SECONDS, name))
                self._audit(conn, "account_locked", name, f"{LOCK_SECONDS // 60} minutes")
            else:
                conn.execute("UPDATE users SET failed_logins = ? WHERE username = ?", (failures, name))
            conn.execute("COMMIT")
            return AuthResult(False, reason="bad_credentials")
        finally:
            conn.close()
