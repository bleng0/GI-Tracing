"""Runs one offline query-console query in its own process (started by query.run_isolated,
which stops it at the time limit). Not meant to be run by hand.

stdin:  one BSON document {docs, filter, projection, sort, limit}
stdout: one status byte, b"R" once the documents are loaded or b"E" if that failed, then one
        BSON document {"rows": [...]} or {"error": "..."}
"""
from __future__ import annotations

import sys

import bson


def main() -> int:
    out = sys.stdout.buffer
    ready = False
    try:
        req = bson.decode(sys.stdin.buffer.read())
        import mongomock
        coll = mongomock.MongoClient().gi_trace.query
        if req["docs"]:
            coll.insert_many(req["docs"])
        out.write(b"R")
        out.flush()
        ready = True
        cursor = coll.find(req["filter"], req["projection"])
        if req["sort"]:
            cursor = cursor.sort([tuple(p) for p in req["sort"]])
        reply = {"rows": list(cursor.limit(req["limit"]))}
    except Exception as e:  # anything mongomock raises goes back as a message
        reply = {"error": f"{type(e).__name__}: {e}"[:500]}
    if not ready:
        out.write(b"E")
    try:
        data = bson.encode(reply)
    except Exception as e:
        data = bson.encode({"error": f"the result cannot be encoded: {type(e).__name__}"})
    out.write(data)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
