"""Document store interface and the local, file-backed implementation.

`DocStore` lists the collection calls the rest of the code uses. Two things
satisfy it: a pymongo collection (real MongoDB, Phase 8) and `FileCollection`
below, which keeps the data in mongomock and writes the whole database to one
JSON file (Extended JSON, so ObjectIds and dates survive) after every write.
No server is needed. With `path=None` nothing is written to disk.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Protocol

import mongomock
from bson import json_util

FORMAT_VERSION = 1


class StorageError(Exception):
    pass


def atomic_write_text(path: Path, text: str) -> None:
    """Writes a temp file, flushes it to disk, then renames it over `path`, so a crash
    never leaves half a file. On Windows the rename fails while another program (an
    editor, antivirus, a sync client) briefly holds the target open, so it is retried."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def mtime_ns(path: Path) -> int | None:
    try:
        return Path(path).stat().st_mtime_ns
    except FileNotFoundError:
        return None


def read_text(path: Path) -> str:
    """UTF-8, with or without the byte-order mark some Windows editors add."""
    return Path(path).read_text(encoding="utf-8-sig")


class DocStore(Protocol):
    def insert_one(self, document: dict) -> Any: ...
    def find(self, filter: dict | None = None, projection: dict | None = None) -> Any: ...
    def find_one(self, filter: dict | None = None, projection: dict | None = None) -> dict | None: ...
    def update_one(self, filter: dict, update: dict) -> Any: ...
    def update_many(self, filter: dict, update: dict) -> Any: ...
    def delete_many(self, filter: dict) -> Any: ...
    def create_index(self, keys, **kwargs) -> str: ...


class FileDocStore:
    """A mongomock database snapshotted to `path` after each write and reloaded on start.

    One writer at a time: if the file changes on disk after this store loaded or last
    saved it (another process wrote it), the next save is refused instead of silently
    overwriting the other process's data.
    """

    def __init__(self, path: str | Path | None = None, load: bool = True):
        """`load=False` ignores any existing file (it is overwritten on the first write)."""
        self.path = Path(path) if path is not None else None
        self._db = mongomock.MongoClient().gi_trace
        self._collections: dict[str, FileCollection] = {}
        self._disk_mtime = mtime_ns(self.path) if self.path is not None else None
        if load and self.path is not None and self.path.exists():
            self._load()

    def get_collection(self, name: str) -> "FileCollection":
        if name not in self._collections:
            self._collections[name] = FileCollection(self, self._db[name])
        return self._collections[name]

    __getitem__ = get_collection

    def list_collection_names(self) -> list[str]:
        return sorted(self._db.list_collection_names())

    def save(self) -> None:
        if self.path is None:
            return
        snapshot = {"format": FORMAT_VERSION, "collections": {}}
        for name in self.list_collection_names():
            coll = self._db[name]
            indexes = [
                {"keys": [list(k) for k in spec["key"]],
                 "options": {k: v for k, v in spec.items() if k not in ("key", "v", "ns")}}
                for idx_name, spec in coll.index_information().items() if idx_name != "_id_"
            ]
            snapshot["collections"][name] = {
                "indexes": indexes,
                "documents": list(coll.find()),
            }
        if mtime_ns(self.path) != self._disk_mtime:
            raise StorageError(f"{self.path} was changed on disk by another process; reopen it before writing")
        atomic_write_text(self.path, json_util.dumps(snapshot, indent=1,
                                                     json_options=json_util.CANONICAL_JSON_OPTIONS))
        self._disk_mtime = mtime_ns(self.path)

    def _load(self) -> None:
        try:
            snapshot = json_util.loads(read_text(self.path))
            if snapshot.get("format") != FORMAT_VERSION:
                raise ValueError(f"unknown format {snapshot.get('format')!r}")
            for name, data in snapshot["collections"].items():
                coll = self._db[name]
                for idx in data["indexes"]:
                    coll.create_index([tuple(k) for k in idx["keys"]], **idx["options"])
                if data["documents"]:
                    coll.insert_many(data["documents"])
        except Exception as e:  # corrupt or hand-edited into an invalid shape
            raise StorageError(f"cannot load document store {self.path}: {e}") from e


class FileCollection:
    """One collection of a FileDocStore. Reads pass through; writes save the file."""

    def __init__(self, store: FileDocStore, coll):
        self._store, self._coll = store, coll
        self.name = coll.name

    def find(self, *args, **kwargs):
        return self._coll.find(*args, **kwargs)

    def find_one(self, *args, **kwargs):
        return self._coll.find_one(*args, **kwargs)

    def _write(self, method: str, *args, **kwargs):
        result = getattr(self._coll, method)(*args, **kwargs)
        self._store.save()
        return result

    def insert_one(self, document, **kwargs):
        return self._write("insert_one", document, **kwargs)

    def update_one(self, filter, update, **kwargs):
        return self._write("update_one", filter, update, **kwargs)

    def update_many(self, filter, update, **kwargs):
        return self._write("update_many", filter, update, **kwargs)

    def delete_many(self, filter, **kwargs):
        return self._write("delete_many", filter, **kwargs)

    def create_index(self, keys, **kwargs):
        before = self._coll.index_information()
        name = self._coll.create_index(keys, **kwargs)
        if name not in before:  # opening an existing store must not rewrite it
            self._store.save()
        return name
