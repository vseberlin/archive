"""Canonical metadata decision and projection primitives."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from archive_core import ArchiveError
from archive_core.catalog import LOCAL_ONLY_COLUMNS, LOGGED_TABLES


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

def normalize_snapshot_digest(value: str) -> str:
    digest = value.strip().casefold()
    if not digest:
        return ""
    if not digest.startswith("sha256:"):
        digest = "sha256:" + digest
    if not SHA256_RE.fullmatch(digest):
        raise ArchiveError("snapshot digest must be a SHA-256 digest")
    return digest

def supersede_depicted_events(
    connection: sqlite3.Connection,
    unit_id: str,
    object_id: str,
    reason: str,
) -> None:
    prior_event_ids = {
        row["object_id"]
        for row in connection.execute(
            """
            SELECT object_id FROM relationships
            WHERE unit_id = ? AND subject_type = 'object' AND subject_id = ?
              AND predicate = 'depicts_event' AND object_type = 'event'
              AND status = 'accepted'
            """,
            (unit_id, object_id),
        )
    }
    for prior_event_id in prior_event_ids:
        connection.execute(
            """
            UPDATE relationships
            SET status = 'rejected', note = ?
            WHERE unit_id = ? AND subject_type = 'object' AND subject_id = ?
              AND predicate = 'depicts_event'
              AND object_type = 'event' AND object_id = ?
              AND status = 'accepted'
            """,
            (
                f"Superseded by a later depicted-event decision: {reason}",
                unit_id,
                object_id,
                prior_event_id,
            ),
        )
        still_linked = connection.execute(
            """
            SELECT 1 FROM relationships
            WHERE unit_id = ? AND object_type = 'event' AND object_id = ?
              AND status = 'accepted'
            LIMIT 1
            """,
            (unit_id, prior_event_id),
        ).fetchone()
        if not still_linked:
            connection.execute(
                "UPDATE events SET status = 'superseded', note = ? WHERE event_id = ?",
                (
                    f"Superseded by a later depicted-event decision: {reason}",
                    prior_event_id,
                ),
            )

def record_id_for(table: str, row: sqlite3.Row | dict[str, Any]) -> str:
    key = LOGGED_TABLES[table]
    return str(row[key])

def projection_payload(table: str, row: sqlite3.Row) -> dict[str, Any]:
    excluded = LOCAL_ONLY_COLUMNS.get(table, set())
    return {key: row[key] for key in row.keys() if key not in excluded}
