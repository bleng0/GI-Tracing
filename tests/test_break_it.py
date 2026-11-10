"""Step 8 break-it check: the access-control tests must notice when a guard is removed.

Copies gi_trace/ and tests/ into a temporary folder (never data/, out/, .env or .git),
removes one guard from the copy's app.py, and runs test_roles.py there. The unchanged
copy must pass and every broken copy must fail."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = {
    "role check removed": ('                if session.get("role") not in roles:\n                    abort(403)\n', ""),
    "login check removed": ('                if "user" not in session:\n                    return redirect(url_for("login"))\n', ""),
    "CSRF check removed": ('            if not expected or not hmac.compare_digest(sent, expected):\n                abort(400)\n',
                           "            pass\n"),
    "audit log opened to every role": ('("audit", "Audit log", (ADMIN,))', '("audit", "Audit log", ALL)'),
    "query console opened to staff": ('("query", "Query console", (ADMIN,))', '("query", "Query console", STAFF)'),
    "waybill price rule removed": ('PRICED_DOCUMENTS = {"customs_waybill"}', "PRICED_DOCUMENTS = set()"),
    "sign-out only enforced on GET": ('        if "user" in session and sessions.get(',
                                      '        if request.method == "GET" and "user" in session and sessions.get('),
    "a form with a file skips the role check": ('                if session.get("role") not in roles:',
                                                '                if session.get("role") not in roles and not request.files:'),
    "CSV download skips the role check": ('                if session.get("role") not in roles:',
                                          '                if session.get("role") not in roles and request.form.get("output") != "csv":'),
}


def scratch_copy(dest: Path) -> Path:
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in ("gi_trace", "tests"):
        shutil.copytree(ROOT / name, dest / name, ignore=skip)
    shutil.copy(ROOT / "pytest.ini", dest / "pytest.ini")
    return dest


def run_role_tests(copy: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ATLAS_WRITE_URI", "ATLAS_READ_URI")}
    env.update(GI_MODE="offline", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_roles.py"],
                          cwd=copy, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600)


def test_the_unchanged_copy_passes(tmp_path):
    r = run_role_tests(scratch_copy(tmp_path))
    assert r.returncode == 0, r.stdout[-2000:]


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_removing_a_guard_makes_the_role_tests_fail(tmp_path, name):
    copy = scratch_copy(tmp_path)
    app_py = copy / "gi_trace" / "web" / "app.py"
    old, new = MUTATIONS[name]
    text = app_py.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"the guard for '{name}' moved; update MUTATIONS"
    app_py.write_text(text.replace(old, new), encoding="utf-8")
    r = run_role_tests(copy)
    assert r.returncode == 1 and "FAILED tests/test_roles.py::test_" in r.stdout, (name, r.stdout[-2000:])
    assert (ROOT / "gi_trace" / "web" / "app.py").read_text(encoding="utf-8").count(old) == 1  # the real file is untouched
