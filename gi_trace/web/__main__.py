"""Start the dashboard:  python -m gi_trace.web   then open http://127.0.0.1:5000"""
from __future__ import annotations

import argparse
import sys

from ..doc_store import load_env
from .app import ROOT, create_app
from .users import DEFAULT_DB


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the local dashboard on 127.0.0.1.")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--users", default=str(DEFAULT_DB))
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args(argv)
    load_env()
    app = create_app(args.data, args.out, args.users)
    print("Opening the data (waits up to 30 s for Atlas)...")
    world, error = app.config["HOLDER"].get()
    print("Data: " + ("ERROR " + error if world is None else "OFFLINE (local copy)" if world.offline else "MongoDB Atlas"))
    print(f"Dashboard: http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
