"""Checks the Atlas connection:  docker compose run --rm web python -m gi_trace.check_atlas

Connects with ATLAS_WRITE_URI (from .env), lists the gi_trace collections, writes
one test document to `certificates`, reads it back with ATLAS_READ_URI, then
deletes it. Prints no secrets. Exits 0 when every step works, 1 otherwise.
"""
from __future__ import annotations

import os
import secrets
import sys

from ._docker import require_container
from .doc_store import ATLAS_TIMEOUT_MS, connect_atlas, load_env


def run(env, connect=connect_atlas, log=print) -> int:
    for var in ("ATLAS_WRITE_URI", "ATLAS_READ_URI"):
        if not env.get(var):
            log(f"ERROR: {var} is not set (expected in .env)")
            return 1
    marker = {"_check": "check_atlas", "token": secrets.token_hex(8)}
    try:
        log(f"1/4 connecting as the writer (waits up to {ATLAS_TIMEOUT_MS // 1000} s)")
        writer = connect(env["ATLAS_WRITE_URI"])
        log("2/4 collections: " + ", ".join(sorted(writer.list_collection_names())))
        log("3/4 writing a test document to certificates")
        writer["certificates"].insert_one(dict(marker))
        try:
            log("4/4 reading it back as the reader")
            reader = connect(env["ATLAS_READ_URI"])
            found = reader["certificates"].find_one(marker)
        finally:
            writer["certificates"].delete_many(marker)
    except Exception as e:  # report the failure without the URI (pymongo errors do not include it)
        log(f"ERROR: {type(e).__name__}: {e}")
        return 1
    if found is None:
        log("ERROR: the reader did not see the test document")
        return 1
    log("OK: Atlas reachable, write and read work; the test document was removed")
    return 0


def main() -> int:
    require_container()
    load_env()
    return run(os.environ)


if __name__ == "__main__":
    sys.exit(main())
