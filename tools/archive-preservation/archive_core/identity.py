"""Stable archive identity rules and entity lookup primitives."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3

from archive_core import ArchiveError


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

ARCHIVAL_ID_RE = re.compile(
    r"^ISS-(?P<kind>[AE])-(?P<year>[0-9]{4})-"
    r"(?P<body>[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4})$"
)

UNIT_ID_RE = re.compile(r"^(?:TEST-)?[A-Z0-9]+(?:-[A-Z0-9]+)+$")

def validate_archival_id(value: str) -> bool:
    return bool(ARCHIVAL_ID_RE.fullmatch(value))

def generate_archival_id(connection: sqlite3.Connection, kind: str, year: int) -> str:
    prefix = {"object": "A", "event": "E"}[kind]
    table, column = ("objects", "object_id") if kind == "object" else ("events", "event_id")
    for _ in range(1000):
        body = "".join(secrets.choice(CROCKFORD) for _ in range(4))
        candidate = f"ISS-{prefix}-{year:04d}-{body}"
        if not connection.execute(f'SELECT 1 FROM "{table}" WHERE "{column}" = ?', (candidate,)).fetchone():
            return candidate
    raise ArchiveError("could not allocate a unique archival ID")

def require_unit(connection: sqlite3.Connection, unit_id: str, open_only: bool = False) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
    if not row:
        raise ArchiveError(f"unknown unit: {unit_id}")
    if open_only and row["status"] != "open":
        raise ArchiveError(f"unit is not open: {unit_id}")
    return row

def entity_exists(connection: sqlite3.Connection, entity_type: str, entity_id: str, unit_id: str) -> bool:
    mapping = {
        "object": ("objects", "object_id"),
        "event": ("events", "event_id"),
        "testimony": ("testimonies", "testimony_id"),
        "source_file": ("source_files", "source_file_id"),
        "unit": ("units", "unit_id"),
    }
    table, column = mapping[entity_type]
    row = connection.execute(
        f'SELECT 1 FROM "{table}" WHERE "{column}" = ? AND unit_id = ?',
        (entity_id, unit_id),
    ).fetchone() if table != "units" else connection.execute(
        "SELECT 1 FROM units WHERE unit_id = ?", (entity_id,)
    ).fetchone()
    return bool(row)

def source_file_identifier(batch_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{batch_id}\0{relative_path}".encode("utf-8")).hexdigest()
    return "SRC-" + digest[:20].upper()
