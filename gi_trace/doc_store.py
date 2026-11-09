"""Document tier: signed compliance certificates in MongoDB.

Set MONGO_URI to use a real MongoDB server; otherwise a FileDocStore is used
(mongomock, saved to a JSON file when given a path, in memory when not), so the
prototype runs with nothing installed.
"""
from __future__ import annotations

import os

from .crypto import Node, canonical
from .storage import FileDocStore

# Fields the signer does not cover: the DB id, the signature itself, and the
# status the oracle writes after verification.
UNSIGNED_FIELDS = {"_id", "signature", "compliance_status"}


def open_store(path=None, load: bool = True):
    """The gi_trace database: real MongoDB if MONGO_URI is set, else a FileDocStore at `path`."""
    uri = os.environ.get("MONGO_URI")
    if uri:
        import pymongo
        return pymongo.MongoClient(uri).gi_trace
    return FileDocStore(path, load=load)


def open_collection(store=None):
    store = store if store is not None else open_store()
    coll = store.get_collection("certificates")
    coll.create_index("batch_id")
    return coll


def signed_part(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in UNSIGNED_FIELDS}


def submit_certificate(coll, cert: dict, signer: Node) -> str:
    """Store a certificate signed by the issuing lab or registry; returns its id."""
    doc = dict(cert, signer=signer.name)
    doc["signature"] = signer.sign(canonical(signed_part(doc)))
    doc["compliance_status"] = "pending"
    return str(coll.insert_one(doc).inserted_id)
