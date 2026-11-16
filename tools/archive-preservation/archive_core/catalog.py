"""Legacy catalog mechanics retained behind archive-core contracts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import catalog_core
import source_registry

from archive_core import ArchiveError
from archive_core.filesystem import LEGACY_FIXTURE_MODE, _read_json_object, utc_now


TOOL_VERSION = catalog_core.TOOL_VERSION

UNIT_CATALOG_FORMAT = "iss-unit-catalog-v1"
UNIT_CATALOG_COLUMNS = (
    "catalog_format",
    "catalog_revision",
    "unit_id",
    "unit_title",
    "iss_id",
    "membership_roles",
    "canonical_name",
    "archival_label",
    "descriptive_title",
    "description",
    "material_technique",
    "dimensions",
    "publication_status",
    "object_status",
    "object_note",
    "identifiers",
    "rights_statement",
    "rights_holder",
    "rights_usage_terms",
    "rights_web_statement",
    "rights_status",
    "representation_role",
    "representation_status",
    "file_status",
    "relative_path",
    "original_filename",
    "sha256",
    "size_bytes",
    "media_type",
    "pdf_version",
    "pdf_encrypted",
    "pdf_text_status",
    "pdf_indexing_status",
    "pdf_validation_tool",
    "width_px",
    "height_px",
    "bit_depth",
    "color_space",
    "compression",
    "page_count",
    "duration_ms",
    "observed_mtime_utc",
    "source_root_id",
    "source_batch_id",
    "provenance_note",
    "provenance_gaps",
    "provenance_status",
    "record_state",
)


class ArchiveConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the file handle."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_context (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    actor TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('retrospective', 'contemporary', 'fixture')),
    status TEXT NOT NULL CHECK (status IN ('open', 'sealed')) DEFAULT 'open',
    created_at TEXT NOT NULL,
    sealed_at TEXT
);
CREATE TABLE IF NOT EXISTS objects (
    object_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    display_identifier TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL,
    archival_label TEXT NOT NULL,
    descriptive_title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    material_technique TEXT NOT NULL DEFAULT '',
    dimensions TEXT NOT NULL DEFAULT '',
    publication_status TEXT NOT NULL CHECK (publication_status IN ('allowed', 'blocked', 'unknown')) DEFAULT 'unknown',
    status TEXT NOT NULL CHECK (status IN ('active', 'withdrawn', 'superseded', 'unresolved')) DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    title TEXT NOT NULL,
    starts_on TEXT NOT NULL DEFAULT '',
    ends_on TEXT NOT NULL DEFAULT '',
    circa INTEGER NOT NULL CHECK (circa IN (0, 1)) DEFAULT 0,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'withdrawn', 'superseded', 'unresolved')) DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testimonies (
    testimony_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    event_id TEXT REFERENCES events(event_id),
    contributor TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    verbatim TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('certain', 'probable', 'possible', 'unknown')) DEFAULT 'unknown',
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'unresolved')) DEFAULT 'unresolved',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_batches (
    batch_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    source_root_id TEXT NOT NULL,
    source_root_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'missing')) DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_files (
    source_file_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    batch_id TEXT NOT NULL REFERENCES source_batches(batch_id),
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('master', 'source')),
    file_id TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    observed_mtime_utc TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    intake_relpath TEXT NOT NULL DEFAULT '',
    archive_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'withdrawn', 'superseded', 'missing')) DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (batch_id, relative_path)
);
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('object', 'event', 'testimony', 'source_file')),
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('object', 'event', 'testimony', 'source_file')),
    object_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'unresolved')) DEFAULT 'unresolved',
    confidence TEXT NOT NULL CHECK (confidence IN ('certain', 'probable', 'possible', 'unknown')) DEFAULT 'unknown',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS identifiers (
    identifier_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('object', 'event')),
    entity_id TEXT NOT NULL,
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'unresolved')) DEFAULT 'unresolved',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (scheme, value)
);
CREATE TABLE IF NOT EXISTS gaps (
    gap_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('unit', 'object', 'event', 'testimony', 'source_file')),
    entity_id TEXT NOT NULL,
    gap_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    suggested_next_action TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('open', 'reviewing', 'resolved', 'accepted_unknown')) DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS external_identity_reviews (
    review_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    local_object_id TEXT NOT NULL REFERENCES objects(object_id),
    external_system TEXT NOT NULL,
    external_id TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL CHECK (outcome IN (
        'same_object', 'component_of_carrier', 'wrong_master', 'not_same', 'defer'
    )),
    related_object_id TEXT REFERENCES objects(object_id),
    target_relationship_id TEXT REFERENCES relationships(relationship_id),
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    reviewed_at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'deferred')),
    CHECK (
        (outcome = 'component_of_carrier' AND related_object_id IS NOT NULL AND target_relationship_id IS NULL)
        OR (outcome = 'wrong_master' AND target_relationship_id IS NOT NULL AND related_object_id IS NULL)
        OR (outcome IN ('same_object', 'not_same', 'defer') AND related_object_id IS NULL AND target_relationship_id IS NULL)
    )
);
CREATE TABLE IF NOT EXISTS metadata_decisions (
    decision_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    object_id TEXT NOT NULL REFERENCES objects(object_id),
    field_name TEXT NOT NULL CHECK (field_name IN (
        'descriptive_title', 'description', 'material_technique', 'dimensions', 'depicted_event'
    )),
    action TEXT NOT NULL CHECK (action IN (
        'keep_local', 'accept_external', 'corrected', 'split_event',
        'component_events', 'defer'
    )),
    accepted_value TEXT NOT NULL DEFAULT '',
    external_system TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    snapshot_digest TEXT NOT NULL DEFAULT '',
    event_id TEXT REFERENCES events(event_id),
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    decided_at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'deferred')),
    CHECK (
        (action = 'split_event' AND field_name = 'depicted_event' AND event_id IS NOT NULL)
        OR (action = 'component_events' AND field_name = 'depicted_event' AND event_id IS NULL)
        OR (action = 'defer' AND event_id IS NULL)
        OR (action IN ('keep_local', 'accept_external', 'corrected')
            AND field_name != 'depicted_event' AND event_id IS NULL)
    )
);
CREATE TABLE IF NOT EXISTS logbook (
    log_id TEXT PRIMARY KEY,
    at_utc TEXT NOT NULL,
    actor TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '',
    after_json TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS preservation_copies (
    unit_id TEXT PRIMARY KEY REFERENCES units(unit_id),
    bag_path TEXT NOT NULL,
    verified_at_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified', 'failed'))
);
CREATE TABLE IF NOT EXISTS fixity_checks (
    check_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    copy_role TEXT NOT NULL CHECK (copy_role IN ('primary', 'preservation')),
    checked_at_utc TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    manifest_sha256 TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""

LOGGED_TABLES = {
    "units": "unit_id",
    "objects": "object_id",
    "events": "event_id",
    "testimonies": "testimony_id",
    "source_batches": "batch_id",
    "source_files": "source_file_id",
    "relationships": "relationship_id",
    "identifiers": "identifier_id",
    "gaps": "gap_id",
    "external_identity_reviews": "review_id",
    "metadata_decisions": "decision_id",
}

PROJECTED_TABLES = (
    "units",
    "objects",
    "events",
    "testimonies",
    "source_batches",
    "source_files",
    "relationships",
    "gaps",
)

LOCAL_ONLY_COLUMNS = {
    "source_batches": {"source_root_path"},
    "source_files": {"intake_relpath"},
}

CATALOG_STATE_TABLES = (
    "meta",
    "session_context",
    "units",
    "objects",
    "events",
    "testimonies",
    "source_batches",
    "source_files",
    "relationships",
    "identifiers",
    "gaps",
    "external_identity_reviews",
    "metadata_decisions",
    "logbook",
    "preservation_copies",
    "fixity_checks",
)

def connect_db(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        raise ArchiveError(f"working catalog does not exist: {db_path}")
    if not read_only:
        marker = db_path.resolve().parent / "catalog-mode.json"
        mode = (
            _read_json_object(marker).get("catalog_mode")
            if marker.is_file()
            else None
        )
        if mode != LEGACY_FIXTURE_MODE:
            raise ArchiveError(
                "legacy catalog writes are frozen; use the minimal-core catalog "
                "operations or an explicitly initialized legacy fixture"
            )
    if read_only:
        connection = sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro", uri=True, factory=ArchiveConnection
        )
    else:
        connection = sqlite3.connect(db_path, factory=ArchiveConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection

def connect_registry_db(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        if not db_path.exists():
            raise ArchiveError(f"source registry does not exist: {db_path}")
        connection = sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro", uri=True, factory=ArchiveConnection
        )
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, factory=ArchiveConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        source_registry.ensure_schema(connection)
        connection.commit()
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection

def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]

def sql_json_object(prefix: str, columns: list[str]) -> str:
    values: list[str] = []
    for column in columns:
        values.extend((f"'{column}'", f'{prefix}."{column}"'))
    return "json_object(" + ",".join(values) + ")"

def install_log_triggers(connection: sqlite3.Connection) -> None:
    for table, primary_key in LOGGED_TABLES.items():
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone():
            continue
        columns = table_columns(connection, table)
        unit_new = "NEW.unit_id" if "unit_id" in columns else "NEW.unit_id"
        unit_old = "OLD.unit_id" if "unit_id" in columns else "OLD.unit_id"
        for action, prefix, unit_expr, before_expr, after_expr in (
            ("insert", "NEW", unit_new, "''", sql_json_object("NEW", columns)),
            ("update", "NEW", unit_new, sql_json_object("OLD", columns), sql_json_object("NEW", columns)),
            ("delete", "OLD", unit_old, sql_json_object("OLD", columns), "''"),
        ):
            trigger = f"log_{table}_{action}"
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            connection.execute(
                f"""
                CREATE TRIGGER "{trigger}" AFTER {action.upper()} ON "{table}"
                WHEN COALESCE((SELECT value FROM meta WHERE key = 'import_mode'), '0') != '1'
                BEGIN
                    SELECT CASE WHEN TRIM(COALESCE((SELECT actor FROM session_context WHERE singleton = 1), '')) = ''
                        THEN RAISE(ABORT, 'session actor required') END;
                    INSERT INTO logbook (
                        log_id, at_utc, actor, unit_id, action, entity_type,
                        entity_id, before_json, after_json, evidence, note
                    ) VALUES (
                        lower(hex(randomblob(16))),
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        (SELECT actor FROM session_context WHERE singleton = 1),
                        {unit_expr},
                        '{action}',
                        '{table}',
                        {prefix}."{primary_key}",
                        {before_expr},
                        {after_expr},
                        COALESCE((SELECT evidence FROM session_context WHERE singleton = 1), ''),
                        COALESCE((SELECT note FROM session_context WHERE singleton = 1), '')
                    );
                END
                """
            )

def install_edit_views(connection: sqlite3.Connection) -> None:
    editable = {
        "objects": (
            "object_id",
            (
                "display_identifier",
                "object_type",
                "archival_label",
                "descriptive_title",
                "description",
                "material_technique",
                "dimensions",
                "publication_status",
                "status",
                "note",
            ),
        ),
        "events": ("event_id", ("title", "starts_on", "ends_on", "circa", "location", "description", "status", "note")),
        "testimonies": ("testimony_id", ("contributor", "recorded_at", "verbatim", "confidence", "status", "note")),
        "gaps": ("gap_id", ("gap_type", "detail", "suggested_next_action", "status")),
        "relationships": ("relationship_id", ("predicate", "status", "confidence", "note")),
        "identifiers": ("identifier_id", ("scheme", "value", "status", "note")),
    }
    for table, (primary_key, fields) in editable.items():
        view = f"edit_{table}"
        connection.execute(f'DROP TRIGGER IF EXISTS "write_{view}"')
        connection.execute(f'DROP VIEW IF EXISTS "{view}"')
        connection.execute(f'CREATE VIEW "{view}" AS SELECT * FROM "{table}"')
        assignments = ", ".join(f'"{field}" = NEW."{field}"' for field in fields)
        connection.execute(
            f"""
            CREATE TRIGGER "write_{view}" INSTEAD OF UPDATE ON "{view}"
            BEGIN
                SELECT CASE WHEN COALESCE(
                    (SELECT status FROM units WHERE unit_id = OLD.unit_id),
                    'missing'
                ) != 'open' THEN RAISE(ABORT, 'sealed units are read-only') END;
                UPDATE "{table}" SET {assignments}
                WHERE "{primary_key}" = OLD."{primary_key}";
            END
            """
        )

def create_schema(db_path: Path, workstation_id: str, actor: str) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    now = utc_now()
    connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1')")
    connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('tool_version', ?)", (TOOL_VERSION,))
    connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('workstation_id', ?)", (workstation_id,))
    connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('import_mode', '0')")
    connection.execute(
        "INSERT OR REPLACE INTO session_context (singleton, actor, evidence, note, updated_at) VALUES (1, ?, '', '', ?)",
        (actor, now),
    )
    install_log_triggers(connection)
    install_edit_views(connection)
    connection.commit()
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection

def ensure_identity_review_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS external_identity_reviews (
            review_id TEXT PRIMARY KEY,
            unit_id TEXT NOT NULL REFERENCES units(unit_id),
            local_object_id TEXT NOT NULL REFERENCES objects(object_id),
            external_system TEXT NOT NULL,
            external_id TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL CHECK (outcome IN (
                'same_object', 'component_of_carrier', 'wrong_master', 'not_same', 'defer'
            )),
            related_object_id TEXT REFERENCES objects(object_id),
            target_relationship_id TEXT REFERENCES relationships(relationship_id),
            reason TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL,
            reviewed_at_utc TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('resolved', 'deferred')),
            CHECK (
                (outcome = 'component_of_carrier' AND related_object_id IS NOT NULL AND target_relationship_id IS NULL)
                OR (outcome = 'wrong_master' AND target_relationship_id IS NOT NULL AND related_object_id IS NULL)
                OR (outcome IN ('same_object', 'not_same', 'defer') AND related_object_id IS NULL AND target_relationship_id IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS external_identity_reviews_object_idx
            ON external_identity_reviews(local_object_id, external_system, external_id, reviewed_at_utc);
        """
    )
    install_log_triggers(connection)
    connection.commit()

def ensure_metadata_decision_schema(connection: sqlite3.Connection) -> None:
    object_columns = set(table_columns(connection, "objects"))
    for column in ("descriptive_title", "description", "material_technique", "dimensions"):
        if column not in object_columns:
            connection.execute(
                f'ALTER TABLE objects ADD COLUMN "{column}" TEXT NOT NULL DEFAULT \'\''
            )
    existing_schema = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'metadata_decisions'
        """
    ).fetchone()
    migrate_component_events = bool(
        existing_schema and "'component_events'" not in str(existing_schema["sql"])
    )
    if migrate_component_events:
        for action in ("insert", "update", "delete"):
            connection.execute(
                f'DROP TRIGGER IF EXISTS "log_metadata_decisions_{action}"'
            )
        connection.execute(
            "DROP INDEX IF EXISTS metadata_decisions_object_field_idx"
        )
        connection.execute(
            "ALTER TABLE metadata_decisions RENAME TO metadata_decisions_legacy"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_decisions (
            decision_id TEXT PRIMARY KEY,
            unit_id TEXT NOT NULL REFERENCES units(unit_id),
            object_id TEXT NOT NULL REFERENCES objects(object_id),
            field_name TEXT NOT NULL CHECK (field_name IN (
                'descriptive_title', 'description', 'material_technique', 'dimensions', 'depicted_event'
            )),
            action TEXT NOT NULL CHECK (action IN (
                'keep_local', 'accept_external', 'corrected', 'split_event',
                'component_events', 'defer'
            )),
            accepted_value TEXT NOT NULL DEFAULT '',
            external_system TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            snapshot_digest TEXT NOT NULL DEFAULT '',
            event_id TEXT REFERENCES events(event_id),
            reason TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL,
            decided_at_utc TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('accepted', 'deferred')),
            CHECK (
                (action = 'split_event' AND field_name = 'depicted_event' AND event_id IS NOT NULL)
                OR (action = 'component_events' AND field_name = 'depicted_event' AND event_id IS NULL)
                OR (action = 'defer' AND event_id IS NULL)
                OR (action IN ('keep_local', 'accept_external', 'corrected')
                    AND field_name != 'depicted_event' AND event_id IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS metadata_decisions_object_field_idx
            ON metadata_decisions(object_id, field_name, decided_at_utc);
        """
    )
    if migrate_component_events:
        columns = table_columns(connection, "metadata_decisions")
        quoted = ",".join(f'"{column}"' for column in columns)
        connection.execute(
            f"""
            INSERT INTO metadata_decisions ({quoted})
            SELECT {quoted} FROM metadata_decisions_legacy
            """
        )
        connection.execute("DROP TABLE metadata_decisions_legacy")
    install_log_triggers(connection)
    install_edit_views(connection)
    connection.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('tool_version', ?)",
        (TOOL_VERSION,),
    )
    connection.commit()

def set_context(connection: sqlite3.Connection, actor: str, evidence: str = "", note: str = "") -> None:
    if not actor.strip():
        raise ArchiveError("actor is required")
    connection.execute(
        "UPDATE session_context SET actor = ?, evidence = ?, note = ?, updated_at = ? WHERE singleton = 1",
        (actor.strip(), evidence, note, utc_now()),
    )
    connection.commit()

def assert_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ArchiveError(f"SQLite integrity check failed: {result}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ArchiveError(f"SQLite foreign key check failed: {violations}")


def _joined_values(
    connection: sqlite3.Connection,
    query: str,
    arguments: tuple[Any, ...],
) -> str:
    return "\n".join(str(row[0]) for row in connection.execute(query, arguments))


def unit_catalog_rows(
    connection: sqlite3.Connection,
    unit_id: str,
) -> list[dict[str, str]]:
    """Return one flat row per active representation, including empty records."""

    unit = connection.execute(
        "SELECT unit_id, title FROM units WHERE unit_id = ?",
        (unit_id,),
    ).fetchone()
    if not unit:
        raise catalog_core.CoreCatalogError(f"unit does not exist: {unit_id}")
    state = catalog_core.catalog_state(connection)
    revision = str(state["catalog_revision"])
    objects = connection.execute(
        """SELECT e.entity_id, a.archival_label, a.descriptive_title,
                  a.description, a.material_technique, a.dimensions,
                  a.publication_status, a.status AS object_status,
                  a.note AS object_note,
                  COALESCE((
                      SELECT value FROM entity_names n
                      WHERE n.entity_id = e.entity_id
                        AND n.name_type = 'canonical'
                        AND n.status = 'active'
                      ORDER BY CASE WHEN n.language = '' THEN 0 ELSE 1 END,
                               n.language, n.name_id
                      LIMIT 1
                  ), '') AS canonical_name
           FROM entities e
           JOIN archive_objects a ON a.entity_id = e.entity_id
           JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
           WHERE m.unit_id = ?
             AND e.status = 'active'
           GROUP BY e.entity_id
           ORDER BY a.archival_label COLLATE NOCASE, e.entity_id""",
        (unit_id,),
    ).fetchall()
    rows: list[dict[str, str]] = []
    for object_row in objects:
        entity_id = str(object_row["entity_id"])
        memberships = _joined_values(
            connection,
            """SELECT role FROM entity_unit_memberships
               WHERE entity_id = ? AND unit_id = ? ORDER BY role""",
            (entity_id, unit_id),
        )
        identifiers = _joined_values(
            connection,
            """SELECT namespace || ':' || value ||
                      CASE WHEN role = '' THEN '' ELSE ' [' || role || ']' END
               FROM entity_identifiers
               WHERE entity_id = ? AND status = 'active'
               ORDER BY namespace COLLATE NOCASE, normalized_value, identifier_id""",
            (entity_id,),
        )
        representations = connection.execute(
            """SELECT ef.role AS representation_role,
                      ef.status AS representation_status,
                      fo.status AS file_status, fo.relative_path,
                      fo.original_filename, fo.content_digest, fo.source_root_id,
                      COALESCE(fo.batch_id, '') AS batch_id,
                      fo.observed_mtime_utc, fo.note AS occurrence_note,
                      cb.size_bytes, cb.media_type, cb.width_px, cb.height_px,
                      cb.bit_depth, cb.color_space, cb.compression,
                      cb.page_count, cb.duration_ms
               FROM entity_files ef
               JOIN file_occurrences fo
                 ON fo.occurrence_id = ef.occurrence_id
               JOIN content_blobs cb
                 ON cb.content_digest = fo.content_digest
               WHERE ef.entity_id = ?
                 AND ef.status = 'active'
               ORDER BY ef.role, fo.relative_path, fo.occurrence_id""",
            (entity_id,),
        ).fetchall()
        for representation in representations or (None,):
            values = {
                "catalog_format": UNIT_CATALOG_FORMAT,
                "catalog_revision": revision,
                "unit_id": str(unit["unit_id"]),
                "unit_title": str(unit["title"]),
                "iss_id": entity_id,
                "membership_roles": memberships,
                "canonical_name": str(object_row["canonical_name"]),
                "archival_label": str(object_row["archival_label"]),
                "descriptive_title": str(object_row["descriptive_title"]),
                "description": str(object_row["description"]),
                "material_technique": str(object_row["material_technique"]),
                "dimensions": str(object_row["dimensions"]),
                "publication_status": str(object_row["publication_status"]),
                "object_status": str(object_row["object_status"]),
                "object_note": str(object_row["object_note"]),
                "identifiers": identifiers,
                "rights_statement": "",
                "rights_holder": "",
                "rights_usage_terms": "",
                "rights_web_statement": "",
                "rights_status": "not_recorded_in_minimal_core_v1",
                "representation_role": "",
                "representation_status": "",
                "file_status": "",
                "relative_path": "",
                "original_filename": "",
                "sha256": "",
                "size_bytes": "",
                "media_type": "",
                "pdf_version": "",
                "pdf_encrypted": "",
                "pdf_text_status": "",
                "pdf_indexing_status": "",
                "pdf_validation_tool": "",
                "width_px": "",
                "height_px": "",
                "bit_depth": "",
                "color_space": "",
                "compression": "",
                "page_count": "",
                "duration_ms": "",
                "observed_mtime_utc": "",
                "source_root_id": "",
                "source_batch_id": "",
                "provenance_note": "",
                "provenance_gaps": "",
                "provenance_status": "not_recorded_in_minimal_core_v1",
                "record_state": "no_representation",
            }
            if representation is not None:
                for source, target in (
                    ("representation_role", "representation_role"),
                    ("representation_status", "representation_status"),
                    ("file_status", "file_status"),
                    ("relative_path", "relative_path"),
                    ("original_filename", "original_filename"),
                    ("size_bytes", "size_bytes"),
                    ("media_type", "media_type"),
                    ("width_px", "width_px"),
                    ("height_px", "height_px"),
                    ("bit_depth", "bit_depth"),
                    ("color_space", "color_space"),
                    ("compression", "compression"),
                    ("page_count", "page_count"),
                    ("duration_ms", "duration_ms"),
                    ("observed_mtime_utc", "observed_mtime_utc"),
                    ("source_root_id", "source_root_id"),
                    ("batch_id", "source_batch_id"),
                    ("occurrence_note", "provenance_note"),
                ):
                    value = representation[source]
                    values[target] = "" if value is None else str(value)
                values["sha256"] = str(representation["content_digest"]).removeprefix(
                    "sha256:"
                )
                values["record_state"] = "represented"
                if values["media_type"] == "application/pdf":
                    try:
                        document_note = json.loads(
                            values["provenance_note"]
                        )
                    except (json.JSONDecodeError, TypeError):
                        document_note = {}
                    if isinstance(document_note, dict):
                        profile = document_note.get("document_profile")
                        provenance = document_note.get("provenance")
                        if isinstance(profile, dict):
                            values["pdf_version"] = str(
                                profile.get("pdf_version") or ""
                            )
                            values["pdf_encrypted"] = str(
                                profile.get("encrypted", "")
                            ).lower()
                            values["pdf_text_status"] = str(
                                profile.get("text_status") or ""
                            )
                            values["pdf_indexing_status"] = str(
                                profile.get("indexing_status") or ""
                            )
                            values["pdf_validation_tool"] = str(
                                profile.get("validation_tool") or ""
                            )
                        if isinstance(provenance, dict):
                            values["provenance_note"] = str(
                                provenance.get("selection_note") or ""
                            )
                            values["provenance_status"] = (
                                "pdf_reconciliation_plan_available"
                            )
                elif values["provenance_note"]:
                    values["provenance_status"] = "occurrence_note_available"
            _validate_unit_catalog_row(values)
            rows.append(values)
    return rows


def _validate_unit_catalog_row(row: dict[str, str]) -> None:
    if tuple(row) != UNIT_CATALOG_COLUMNS:
        raise catalog_core.CoreCatalogError("unit catalog columns are not canonical")
    for column, value in row.items():
        if not isinstance(value, str):
            raise catalog_core.CoreCatalogError(
                f"unit catalog value is not text: {column}"
            )
        if "\x00" in value:
            raise catalog_core.CoreCatalogError(
                f"unit catalog value contains NUL: {column}"
            )
        for character in value:
            codepoint = ord(character)
            if codepoint < 32 and character not in "\t\n\r":
                raise catalog_core.CoreCatalogError(
                    f"unit catalog value contains an unsupported control: {column}"
                )


def unit_catalog_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    """Encode canonical RFC-4180 CSV with every field quoted."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=UNIT_CATALOG_COLUMNS,
        quoting=csv.QUOTE_ALL,
        lineterminator="\r\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    encoded = output.getvalue().encode("utf-8-sig")
    decoded = list(
        csv.DictReader(
            io.StringIO(encoded.decode("utf-8-sig"), newline="")
        )
    )
    if decoded != rows or (
        decoded and tuple(decoded[0]) != UNIT_CATALOG_COLUMNS
    ):
        raise catalog_core.CoreCatalogError("unit catalog CSV round-trip failed")
    return encoded


def _xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def unit_catalog_xlsx_bytes(rows: list[dict[str, str]]) -> bytes:
    """Create a deterministic, values-only XLSX without runtime dependencies."""

    matrix = [list(UNIT_CATALOG_COLUMNS)]
    matrix.extend([[row[column] for column in UNIT_CATALOG_COLUMNS] for row in rows])
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", spreadsheet_ns)
    ET.register_namespace("r", relationship_ns)

    worksheet = ET.Element(f"{{{spreadsheet_ns}}}worksheet")
    views = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetViews")
    view = ET.SubElement(
        views,
        f"{{{spreadsheet_ns}}}sheetView",
        {"workbookViewId": "0"},
    )
    ET.SubElement(
        view,
        f"{{{spreadsheet_ns}}}pane",
        {
            "ySplit": "1",
            "topLeftCell": "A2",
            "activePane": "bottomLeft",
            "state": "frozen",
        },
    )
    columns = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}cols")
    ET.SubElement(
        columns,
        f"{{{spreadsheet_ns}}}col",
        {
            "min": "1",
            "max": str(len(UNIT_CATALOG_COLUMNS)),
            "width": "18",
            "customWidth": "1",
        },
    )
    sheet_data = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetData")
    for row_number, values in enumerate(matrix, 1):
        row_element = ET.SubElement(
            sheet_data,
            f"{{{spreadsheet_ns}}}row",
            {"r": str(row_number)},
        )
        for column_number, value in enumerate(values, 1):
            cell = ET.SubElement(
                row_element,
                f"{{{spreadsheet_ns}}}c",
                {
                    "r": f"{_xlsx_column_name(column_number)}{row_number}",
                    "t": "inlineStr",
                    "s": "1" if row_number == 1 else "0",
                },
            )
            inline = ET.SubElement(cell, f"{{{spreadsheet_ns}}}is")
            text = ET.SubElement(inline, f"{{{spreadsheet_ns}}}t")
            if value[:1].isspace() or value[-1:].isspace() or "\n" in value:
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = value
    last_cell = (
        f"{_xlsx_column_name(len(UNIT_CATALOG_COLUMNS))}{max(1, len(matrix))}"
    )
    ET.SubElement(
        worksheet,
        f"{{{spreadsheet_ns}}}autoFilter",
        {"ref": f"A1:{last_cell}"},
    )
    table_parts = ET.SubElement(
        worksheet,
        f"{{{spreadsheet_ns}}}tableParts",
        {"count": "1"},
    )
    ET.SubElement(
        table_parts,
        f"{{{spreadsheet_ns}}}tablePart",
        {f"{{{relationship_ns}}}id": "rId1"},
    )

    table = ET.Element(
        f"{{{spreadsheet_ns}}}table",
        {
            "id": "1",
            "name": "ArchiveCatalog",
            "displayName": "ArchiveCatalog",
            "ref": f"A1:{last_cell}",
            "totalsRowShown": "0",
        },
    )
    ET.SubElement(
        table,
        f"{{{spreadsheet_ns}}}autoFilter",
        {"ref": f"A1:{last_cell}"},
    )
    table_columns = ET.SubElement(
        table,
        f"{{{spreadsheet_ns}}}tableColumns",
        {"count": str(len(UNIT_CATALOG_COLUMNS))},
    )
    for index, name in enumerate(UNIT_CATALOG_COLUMNS, 1):
        ET.SubElement(
            table_columns,
            f"{{{spreadsheet_ns}}}tableColumn",
            {"id": str(index), "name": name},
        )
    ET.SubElement(
        table,
        f"{{{spreadsheet_ns}}}tableStyleInfo",
        {
            "name": "TableStyleMedium2",
            "showFirstColumn": "0",
            "showLastColumn": "0",
            "showRowStripes": "1",
            "showColumnStripes": "0",
        },
    )

    entries = {
        "[Content_Types].xml": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>
</Types>""",
        "_rels/.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Catalog" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        "xl/styles.xml": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>""",
        "xl/worksheets/sheet1.xml": _xml_bytes(worksheet),
        "xl/worksheets/_rels/sheet1.xml.rels": b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>
</Relationships>""",
        "xl/tables/table1.xml": _xml_bytes(table),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    return output.getvalue()


def write_unit_catalog(
    connection: sqlite3.Connection,
    unit_id: str,
    destination: Path,
) -> dict[str, Any]:
    """Atomically replace both derived unit-catalog views after validation."""

    rows = unit_catalog_rows(connection, unit_id)
    csv_bytes = unit_catalog_csv_bytes(rows)
    xlsx_bytes = unit_catalog_xlsx_bytes(rows)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {
        destination / "CATALOG.csv": csv_bytes,
        destination / "CATALOG.xlsx": xlsx_bytes,
    }
    temporary_paths: list[Path] = []
    try:
        for path, payload in outputs.items():
            temporary = path.with_name(f".{path.name}.partial")
            if temporary.exists():
                raise catalog_core.CoreCatalogError(
                    f"unit catalog temporary path already exists: {temporary}"
                )
            temporary.write_bytes(payload)
            temporary_paths.append(temporary)
        for path, payload in outputs.items():
            temporary = path.with_name(f".{path.name}.partial")
            if temporary.read_bytes() != payload:
                raise catalog_core.CoreCatalogError(
                    f"unit catalog write verification failed: {temporary}"
                )
        for path in outputs:
            temporary = path.with_name(f".{path.name}.partial")
            temporary.replace(path)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
    return {
        "result": "ok",
        "unit_id": unit_id,
        "catalog_revision": int(
            catalog_core.catalog_state(connection)["catalog_revision"]
        ),
        "rows": len(rows),
        "csv": str(destination / "CATALOG.csv"),
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "xlsx": str(destination / "CATALOG.xlsx"),
        "xlsx_sha256": hashlib.sha256(xlsx_bytes).hexdigest(),
    }
