"""Step 9: the guides stay true to the code."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEMO = (ROOT / "DEMO.md").read_text(encoding="utf-8")


def test_readme_test_count_is_current(request):
    import pytest
    ran = {Path(str(item.fspath)).name for item in request.session.items}
    narrowed = any(request.config.getoption(o, None) for o in ("keyword", "markexpr", "deselect"))
    if ran != {f.name for f in (ROOT / "tests").glob("test_*.py")} or narrowed:
        pytest.skip("only meaningful when the whole suite runs")
    counts = {int(n) for n in re.findall(r"(\d+) tests", README)}
    assert counts == {request.session.testscollected}, (counts, request.session.testscollected)


def test_every_command_in_the_guides_exists():
    modules = set(re.findall(r"python -m (gi_trace[\w.]*)", README + DEMO))
    assert {"gi_trace.run_all", "gi_trace.web", "gi_trace.snapshot", "gi_trace.web.create_user"} <= modules
    for m in modules:
        assert importlib.util.find_spec(m) is not None, m


def test_demo_labels_the_blockchain_parts():
    assert "permissioned ledger simulation" in DEMO
    for part in ("Hash-linked, signed ledgers [BLOCKCHAIN]", "Multi-party border commit [BLOCKCHAIN]",
                 "Controlled correction (chameleon hash) [BLOCKCHAIN]", "Dashboard, roles, login [NOT BLOCKCHAIN]",
                 "Query console [NOT BLOCKCHAIN]", "Data tiers [NOT BLOCKCHAIN]"):
        assert part in DEMO, part
    for page in ("Query console", "Ledger viewer", "Verify"):
        assert page in README
