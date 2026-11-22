"""Keep every runnable command inside the Docker image.

The image sets ``GI_DOCKER=1``. The tests set the same value in
``tests/conftest.py`` so the command-line ``main()`` functions can still be
exercised in-process. Any other invocation stops here with a short message
pointing at Docker Compose.
"""
from __future__ import annotations

import os
import sys

DOCKER_FLAG = "GI_DOCKER"


def in_container() -> bool:
    """True only when the process was started by the project's Docker image."""
    return os.environ.get(DOCKER_FLAG) == "1"


def require_container() -> None:
    """Refuse to run outside the container, before any data or config is read."""
    if in_container():
        return
    print(
        "This project only runs in its Docker container.\n"
        "Start the dashboard: docker compose up --build\n"
        "Run a one-off task: docker compose run --rm web <command>",
        file=sys.stderr,
    )
    raise SystemExit(2)
