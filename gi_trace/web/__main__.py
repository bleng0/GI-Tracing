"""Start the dashboard in its container:  docker compose up --build

Compose publishes it on http://127.0.0.1:5000 of the host.
"""
from __future__ import annotations

import argparse
import os
import sys

from .._docker import require_container
from ..doc_store import load_env
from .app import ROOT, create_app
from .users import DEFAULT_DB


def main(argv=None) -> int:
    require_container()
    ap = argparse.ArgumentParser(description="Run the dashboard inside its container.")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--users", default=str(DEFAULT_DB))
    # The image sets GI_HOST=0.0.0.0 so Docker can publish the port on 127.0.0.1
    # of the host; the flags are only used from inside the container.
    ap.add_argument("--host", default=os.environ.get("GI_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GI_PORT", "5000")))
    args = ap.parse_args(argv)
    load_env()
    app = create_app(args.data, args.out, args.users)
    print("Opening the data (waits up to 30 s for Atlas)...")
    world, error = app.config["HOLDER"].get()
    print("Data: " + ("ERROR " + error if world is None else "OFFLINE (local copy)" if world.offline else "MongoDB Atlas"))
    print(f"Dashboard: http://{args.host}:{args.port}   (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
