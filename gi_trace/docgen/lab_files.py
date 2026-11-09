"""Raw lab files: out/labfiles/<batch>.csv.

The hemp assay certificate carries the SHA-256 of its lab file, and the oracle
re-hashes the file it is given, so editing a CSV here makes that batch fail.
"""
from __future__ import annotations

from pathlib import Path


def write_lab_files(lab_files: dict[str, bytes], out_dir) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for batch_id, content in sorted(lab_files.items()):
        p = out_dir / f"{batch_id}.csv"
        p.write_bytes(content)
        paths.append(p)
    return paths


def read_lab_file(out_dir, batch_id: str) -> bytes | None:
    p = Path(out_dir) / f"{batch_id}.csv"
    return p.read_bytes() if p.exists() else None
