"""Every executable path must go through the container."""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENTRY_MODULES = (
    "gi_trace.atlas_setup",
    "gi_trace.check_atlas",
    "gi_trace.demo",
    "gi_trace.docgen.verify",
    "gi_trace.run_all",
    "gi_trace.snapshot",
    "gi_trace.web.__main__",
    "gi_trace.web.create_user",
    "gi_trace.web.query_worker",
)


def test_every_command_requires_the_container_flag():
    for name in ENTRY_MODULES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, name
        source = Path(spec.origin).read_text(encoding="utf-8")
        assert "require_container()" in source, name


def test_a_local_python_command_is_refused(monkeypatch):
    monkeypatch.delenv("GI_DOCKER", raising=False)
    r = subprocess.run([sys.executable, "-m", "gi_trace.demo"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "only runs in its Docker container" in r.stderr
    assert "docker compose up --build" in r.stderr


def test_the_image_and_compose_set_the_flag():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GI_DOCKER=1" in dockerfile
    assert "GI_DOCKER: 1" in compose
