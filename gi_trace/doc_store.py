"""Document tier: signed compliance certificates in MongoDB.

open_store() chooses where the gi_trace database lives from GI_MODE:

  auto     (default) MongoDB Atlas if it answers within 30 seconds, otherwise the
           on-disk file store (OFFLINE MODE). Data written offline stays local.
  atlas    Atlas only; an unreachable Atlas is an error.
  offline  the on-disk file store only (a FileDocStore: mongomock saved to a JSON
           file when given a path, in memory when not).

The pipeline connects with ATLAS_WRITE_URI, the dashboard with ATLAS_READ_URI.
Neither is read from .env here: the command-line entry points call load_env().
"""
from __future__ import annotations

import os
from pathlib import Path

from .crypto import Node, canonical
from .storage import FileDocStore, ensure_index

# Fields the signer does not cover: the DB id, the signature itself, and the
# status the oracle writes after verification.
UNSIGNED_FIELDS = {"_id", "signature", "compliance_status"}

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MODES = ("auto", "atlas", "offline")
URI_VARS = {"write": "ATLAS_WRITE_URI", "read": "ATLAS_READ_URI"}
ATLAS_TIMEOUT_MS = 30000


class AtlasUnavailable(Exception):
    pass


def load_env() -> None:
    """Loads the project's .env into os.environ (variables already set win)."""
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)


def connect_atlas(uri: str):
    """The gi_trace database on Atlas. Raises if no server answers within 30 seconds."""
    import pymongo
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=ATLAS_TIMEOUT_MS)
    client.admin.command("ping")  # the client is lazy; this forces the connection now
    return client.gi_trace


def _unreachable(e: Exception) -> bool:
    """Network or DNS failure (falls back to offline), as opposed to a bad URI or login (raised)."""
    from pymongo.errors import ConfigurationError, ConnectionFailure, InvalidURI
    if isinstance(e, ConnectionFailure):
        return True
    return isinstance(e, ConfigurationError) and not isinstance(e, InvalidURI)


def open_store(path=None, load: bool = True, role: str = "write", log=print, mode: str | None = None):
    """The gi_trace database, chosen by `mode`, else GI_MODE (see the module docstring).
    `role` is "write" (pipeline) or "read" (dashboard). Returns a pymongo Database
    online or a FileDocStore at `path` offline; is_offline() tells which."""
    mode = mode or os.environ.get("GI_MODE", "auto").strip().lower() or "auto"
    if mode not in MODES:
        raise ValueError(f"GI_MODE must be one of {MODES}, not {mode!r}")
    if mode != "offline":
        var = URI_VARS[role]
        uri = os.environ.get(var)
        if not uri:
            if mode == "atlas":
                raise AtlasUnavailable(f"GI_MODE=atlas but {var} is not set")
            log(f"OFFLINE MODE: {var} is not set; using the local file store")
        else:
            try:
                return connect_atlas(uri)
            except Exception as e:
                if mode == "atlas" or not _unreachable(e):
                    raise
                log(f"OFFLINE MODE: Atlas not reachable within {ATLAS_TIMEOUT_MS // 1000} s "
                    f"({type(e).__name__}); using the local file store")
    return FileDocStore(path, load=load)


def is_offline(store) -> bool:
    return isinstance(store, FileDocStore)


def open_collection(store=None):
    store = store if store is not None else open_store()
    coll = store.get_collection("certificates")
    ensure_index(coll, "batch_id")
    return coll


def signed_part(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in UNSIGNED_FIELDS}


def submit_certificate(coll, cert: dict, signer: Node) -> str:
    """Store a certificate signed by the issuing lab or registry; returns its id."""
    doc = dict(cert, signer=signer.name)
    doc["signature"] = signer.sign(canonical(signed_part(doc)))
    doc["compliance_status"] = "pending"
    return str(coll.insert_one(doc).inserted_id)
