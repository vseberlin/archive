"""Minimal filesystem-authoritative archive catalog core."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from archive_core.layout import (
    OBJECT_LAYOUT_VERSION,
    OBJECT_ROLE_TOKENS,
    matches_object_path,
)


TOOL_VERSION = "0.15.0"
SCHEMA_VERSION = 3
CATALOG_MODE = "minimal-core-v2"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ENTITY_ID_RE = re.compile(
    r"^ISS-(?P<year>[0-9]{4})-"
    r"(?P<body>[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4})-"
    r"(?P<check>[0-9ABCDEFGHJKMNPQRSTVWXYZ])$"
)
NULL_TOKEN = r"\N"
PROTECTED_SOURCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
)

DIRTY_TRACKED_TABLES = (
    "units",
    "entity_kinds",
    "entities",
    "evidence_sources",
    "entity_names",
    "entity_identifiers",
    "entity_redirects",
    "entity_unit_memberships",
    "relation_predicates",
    "entity_relations",
    "assertion_predicates",
    "evidence_assertions",
    "archive_objects",
    "historical_events",
    "physical_carriers",
    "source_batches",
    "content_blobs",
    "file_occurrences",
    "entity_files",
)


class CoreCatalogError(RuntimeError):
    """Raised when a minimal-core catalog invariant fails."""


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version             INTEGER PRIMARY KEY,
    applied_at          TEXT NOT NULL,
    tool_version        TEXT NOT NULL,
    migration_checksum  TEXT NOT NULL UNIQUE
);

CREATE TABLE catalog_state (
    singleton           INTEGER PRIMARY KEY CHECK (singleton = 1),
    catalog_revision    INTEGER NOT NULL,
    published_revision  INTEGER NOT NULL,
    dirty_since         TEXT,
    CHECK (catalog_revision >= published_revision)
);

CREATE TABLE units (
    unit_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('open', 'sealed', 'withdrawn')),
    created_at   TEXT NOT NULL,
    sealed_at    TEXT
);

CREATE TABLE entity_kinds (
    kind    TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    status  TEXT NOT NULL
            CHECK (status IN ('active', 'deprecated'))
);

CREATE TABLE entities (
    entity_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL REFERENCES entity_kinds(kind),
    status      TEXT NOT NULL
                CHECK (status IN (
                    'provisional',
                    'active',
                    'merged',
                    'withdrawn'
                )),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE evidence_sources (
    source_id            TEXT PRIMARY KEY,
    source_type          TEXT NOT NULL,
    system               TEXT NOT NULL DEFAULT '',
    external_id          TEXT NOT NULL DEFAULT '',
    captured_at          TEXT,
    snapshot_digest      TEXT NOT NULL DEFAULT '',
    source_root_id       TEXT NOT NULL DEFAULT '',
    relative_path        TEXT NOT NULL DEFAULT '',
    local_absolute_path  TEXT NOT NULL DEFAULT '',
    citation             TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT ''
);

CREATE TABLE entity_names (
    name_id      TEXT PRIMARY KEY,
    entity_id    TEXT NOT NULL REFERENCES entities(entity_id),
    name_type    TEXT NOT NULL
                 CHECK (name_type IN (
                     'canonical',
                     'historical',
                     'alternative',
                     'abbreviation',
                     'source_label',
                     'former'
                 )),
    value        TEXT NOT NULL,
    language     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL
                 CHECK (status IN ('active', 'retired', 'rejected')),
    source_id    TEXT REFERENCES evidence_sources(source_id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE UNIQUE INDEX one_active_canonical_name_per_language
ON entity_names(entity_id, language)
WHERE name_type = 'canonical'
  AND status = 'active';

CREATE TABLE entity_identifiers (
    identifier_id     TEXT PRIMARY KEY,
    entity_id         TEXT NOT NULL REFERENCES entities(entity_id),
    namespace         TEXT NOT NULL,
    value             TEXT NOT NULL,
    normalized_value  TEXT NOT NULL,
    scheme_version    TEXT NOT NULL DEFAULT '',
    role              TEXT NOT NULL
                      CHECK (role IN (
                          'accession',
                          'legacy',
                          'external',
                          'resolver',
                          'former'
                      )),
    status            TEXT NOT NULL
                      CHECK (status IN (
                          'active',
                          'retired',
                          'rejected',
                          'unresolved'
                      )),
    created_at        TEXT NOT NULL,
    retired_at        TEXT
);

CREATE UNIQUE INDEX active_alias_identifier_unique
ON entity_identifiers(namespace, normalized_value)
WHERE status = 'active';

CREATE TABLE entity_redirects (
    former_entity_id     TEXT PRIMARY KEY REFERENCES entities(entity_id),
    canonical_entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
    reason               TEXT NOT NULL,
    actor                TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    CHECK (former_entity_id <> canonical_entity_id)
);

CREATE TABLE entity_unit_memberships (
    entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
    unit_id    TEXT NOT NULL REFERENCES units(unit_id),
    role       TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (entity_id, unit_id, role)
);

CREATE TABLE relation_predicates (
    predicate          TEXT PRIMARY KEY,
    label              TEXT NOT NULL,
    inverse_predicate  TEXT REFERENCES relation_predicates(predicate),
    status             TEXT NOT NULL
                       CHECK (status IN ('active', 'deprecated'))
);

CREATE TABLE entity_relations (
    relation_id        TEXT PRIMARY KEY,
    subject_entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
    predicate          TEXT NOT NULL REFERENCES relation_predicates(predicate),
    object_entity_id   TEXT NOT NULL REFERENCES entities(entity_id),
    status             TEXT NOT NULL
                       CHECK (status IN (
                           'proposed',
                           'accepted',
                           'rejected',
                           'superseded'
                       )),
    confidence         TEXT NOT NULL
                       CHECK (confidence IN (
                           'certain',
                           'probable',
                           'possible',
                           'unknown'
                       )),
    source_id          TEXT REFERENCES evidence_sources(source_id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (subject_entity_id <> object_entity_id)
);

CREATE TABLE assertion_predicates (
    predicate    TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    value_kind   TEXT NOT NULL
                 CHECK (value_kind IN ('text', 'entity')),
    cardinality  TEXT NOT NULL
                 CHECK (cardinality IN ('one', 'many')),
    status       TEXT NOT NULL
                 CHECK (status IN ('active', 'deprecated'))
);

CREATE TABLE evidence_assertions (
    assertion_id             TEXT PRIMARY KEY,
    subject_entity_id        TEXT NOT NULL REFERENCES entities(entity_id),
    predicate                TEXT NOT NULL
                             REFERENCES assertion_predicates(predicate),
    value_text               TEXT,
    object_entity_id         TEXT REFERENCES entities(entity_id),
    status                   TEXT NOT NULL
                             CHECK (status IN (
                                 'proposed',
                                 'accepted',
                                 'rejected',
                                 'superseded',
                                 'unresolved'
                             )),
    confidence               TEXT NOT NULL
                             CHECK (confidence IN (
                                 'certain',
                                 'probable',
                                 'possible',
                                 'unknown'
                             )),
    source_id                TEXT REFERENCES evidence_sources(source_id),
    actor                    TEXT NOT NULL,
    asserted_at              TEXT NOT NULL,
    supersedes_assertion_id  TEXT
                             REFERENCES evidence_assertions(assertion_id),
    note                     TEXT NOT NULL DEFAULT '',
    CHECK ((value_text IS NULL) <> (object_entity_id IS NULL))
);

CREATE TABLE archive_objects (
    entity_id            TEXT PRIMARY KEY REFERENCES entities(entity_id),
    archival_label       TEXT NOT NULL,
    descriptive_title    TEXT NOT NULL DEFAULT '',
    description          TEXT NOT NULL DEFAULT '',
    material_technique   TEXT NOT NULL DEFAULT '',
    dimensions           TEXT NOT NULL DEFAULT '',
    publication_status   TEXT NOT NULL DEFAULT 'unknown',
    status               TEXT NOT NULL DEFAULT 'active',
    note                 TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE historical_events (
    entity_id     TEXT PRIMARY KEY REFERENCES entities(entity_id),
    title         TEXT NOT NULL,
    starts_on     TEXT NOT NULL DEFAULT '',
    ends_on       TEXT NOT NULL DEFAULT '',
    circa         INTEGER NOT NULL DEFAULT 0 CHECK (circa IN (0, 1)),
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE physical_carriers (
    entity_id        TEXT PRIMARY KEY REFERENCES entities(entity_id),
    carrier_type     TEXT NOT NULL,
    dimensions_text  TEXT NOT NULL DEFAULT '',
    condition_note   TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE source_batches (
    batch_id                  TEXT PRIMARY KEY,
    unit_id                   TEXT REFERENCES units(unit_id),
    source_root_id            TEXT NOT NULL,
    local_source_root_path    TEXT NOT NULL DEFAULT '',
    status                    TEXT NOT NULL
                              CHECK (status IN (
                                  'open',
                                  'complete',
                                  'failed',
                                  'withdrawn'
                              )),
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE TABLE content_blobs (
    content_digest  TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0),
    media_type      TEXT NOT NULL DEFAULT '',
    width_px        INTEGER,
    height_px       INTEGER,
    bit_depth       INTEGER,
    color_space     TEXT NOT NULL DEFAULT '',
    compression     TEXT NOT NULL DEFAULT '',
    page_count      INTEGER,
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE file_occurrences (
    occurrence_id      TEXT PRIMARY KEY,
    unit_id            TEXT REFERENCES units(unit_id),
    batch_id           TEXT REFERENCES source_batches(batch_id),
    content_digest     TEXT NOT NULL REFERENCES content_blobs(content_digest),
    occurrence_kind    TEXT NOT NULL
                       CHECK (occurrence_kind IN (
                           'source',
                           'intake',
                           'archive'
                       )),
    source_root_id     TEXT NOT NULL DEFAULT '',
    relative_path      TEXT NOT NULL,
    original_filename  TEXT NOT NULL,
    observed_mtime_utc TEXT NOT NULL DEFAULT '',
    observed_at_utc    TEXT NOT NULL,
    status             TEXT NOT NULL
                       CHECK (status IN (
                           'observed',
                           'verified',
                           'missing',
                           'superseded',
                           'rejected'
                       )),
    note               TEXT NOT NULL DEFAULT '',
    CHECK (
        (occurrence_kind = 'archive' AND unit_id IS NOT NULL)
        OR
        (
            occurrence_kind IN ('source', 'intake')
            AND source_root_id <> ''
        )
    )
);

CREATE UNIQUE INDEX exact_source_occurrence_bytes
ON file_occurrences (
    source_root_id,
    relative_path,
    occurrence_kind,
    content_digest,
    observed_at_utc
)
WHERE occurrence_kind IN ('source', 'intake');

CREATE UNIQUE INDEX exact_archive_occurrence_bytes
ON file_occurrences (unit_id, relative_path, content_digest, observed_at_utc)
WHERE occurrence_kind = 'archive';

CREATE TABLE entity_files (
    entity_id      TEXT NOT NULL REFERENCES entities(entity_id),
    occurrence_id  TEXT NOT NULL REFERENCES file_occurrences(occurrence_id),
    role           TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('active', 'rejected', 'superseded')),
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_id, occurrence_id, role)
);

CREATE TABLE logbook (
    log_id       TEXT PRIMARY KEY,
    at_utc       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    unit_id      TEXT REFERENCES units(unit_id),
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    before_json  TEXT NOT NULL DEFAULT '',
    after_json   TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE filesystem_generations (
    generation_id          TEXT PRIMARY KEY,
    previous_generation_id TEXT
                           REFERENCES filesystem_generations(generation_id),
    schema_version         TEXT NOT NULL,
    tool_version           TEXT NOT NULL,
    catalog_revision       INTEGER NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN (
                               'pending',
                               'current',
                               'superseded',
                               'failed'
                           )),
    manifest_digest        TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    completed_at           TEXT
);

CREATE UNIQUE INDEX one_current_filesystem_generation
ON filesystem_generations(status)
WHERE status = 'current';

CREATE TRIGGER entities_entity_id_immutable
BEFORE UPDATE OF entity_id ON entities
BEGIN
    SELECT RAISE(ABORT, 'entity_id is immutable');
END;

CREATE TRIGGER entities_entity_id_shape
BEFORE INSERT ON entities
WHEN length(NEW.entity_id) <> 15
  OR NEW.entity_id NOT GLOB
     'ISS-[0-9][0-9][0-9][0-9]-[0-9ABCDEFGHJKMNPQRSTVWXYZ][0-9ABCDEFGHJKMNPQRSTVWXYZ][0-9ABCDEFGHJKMNPQRSTVWXYZ][0-9ABCDEFGHJKMNPQRSTVWXYZ]-[0-9ABCDEFGHJKMNPQRSTVWXYZ]'
BEGIN
    SELECT RAISE(ABORT, 'invalid canonical entity ID shape');
END;

CREATE TRIGGER entity_identifiers_no_canonical_insert
BEFORE INSERT ON entity_identifiers
WHEN lower(NEW.namespace) = 'iss'
  OR NEW.value = NEW.entity_id
  OR NEW.normalized_value = NEW.entity_id
  OR EXISTS (
      SELECT 1 FROM entities WHERE entity_id = NEW.normalized_value
  )
BEGIN
    SELECT RAISE(ABORT, 'canonical ISS IDs do not belong in entity_identifiers');
END;

CREATE TRIGGER entity_identifiers_no_canonical_update
BEFORE UPDATE ON entity_identifiers
WHEN lower(NEW.namespace) = 'iss'
  OR NEW.value = NEW.entity_id
  OR NEW.normalized_value = NEW.entity_id
  OR EXISTS (
      SELECT 1 FROM entities WHERE entity_id = NEW.normalized_value
  )
BEGIN
    SELECT RAISE(ABORT, 'canonical ISS IDs do not belong in entity_identifiers');
END;

CREATE TRIGGER archive_objects_kind_guard
BEFORE INSERT ON archive_objects
WHEN (SELECT kind FROM entities WHERE entity_id = NEW.entity_id)
     <> 'archive_object'
BEGIN
    SELECT RAISE(ABORT, 'archive_objects require archive_object entities');
END;

CREATE TRIGGER historical_events_kind_guard
BEFORE INSERT ON historical_events
WHEN (SELECT kind FROM entities WHERE entity_id = NEW.entity_id)
     <> 'historical_event'
BEGIN
    SELECT RAISE(ABORT, 'historical_events require historical_event entities');
END;

CREATE TRIGGER physical_carriers_kind_guard
BEFORE INSERT ON physical_carriers
WHEN (SELECT kind FROM entities WHERE entity_id = NEW.entity_id)
     <> 'physical_carrier'
BEGIN
    SELECT RAISE(ABORT, 'physical_carriers require physical_carrier entities');
END;

CREATE TRIGGER logbook_append_only_update
BEFORE UPDATE ON logbook
BEGIN
    SELECT RAISE(ABORT, 'logbook is append-only');
END;

CREATE TRIGGER logbook_append_only_delete
BEFORE DELETE ON logbook
BEGIN
    SELECT RAISE(ABORT, 'logbook is append-only');
END;
"""

for _table in DIRTY_TRACKED_TABLES:
    for _operation in ("INSERT", "UPDATE", "DELETE"):
        SCHEMA_SQL += f"""
CREATE TRIGGER catalog_dirty_{_table}_{_operation.lower()}
AFTER {_operation} ON {_table}
WHEN EXISTS (SELECT 1 FROM catalog_state WHERE singleton = 1)
BEGIN
    UPDATE catalog_state
    SET catalog_revision = catalog_revision + 1,
        dirty_since = COALESCE(
            dirty_since,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    WHERE singleton = 1;
END;
"""


INITIAL_ENTITY_KINDS = (
    ("archive_object", "Archive object", "active"),
    ("historical_event", "Historical event", "active"),
    ("physical_carrier", "Physical carrier", "active"),
)

INITIAL_RELATION_PREDICATES = (
    ("depicts", "depicts", None, "active"),
    ("embodied_on", "embodied on", None, "active"),
    ("component_of", "component of", None, "active"),
)

INITIAL_ASSERTION_PREDICATES = (
    ("archive_descriptive_title", "Archive descriptive title", "text", "one", "active"),
    ("archive_description", "Archive description", "text", "one", "active"),
    ("archive_material_technique", "Archive material or technique", "text", "one", "active"),
    ("archive_dimensions", "Archive dimensions", "text", "one", "active"),
)

PROMOTABLE_TEXT_ASSERTIONS = {
    "archive_descriptive_title": ("archive_object", "archive_objects", "descriptive_title"),
    "archive_description": ("archive_object", "archive_objects", "description"),
    "archive_material_technique": (
        "archive_object",
        "archive_objects",
        "material_technique",
    ),
    "archive_dimensions": ("archive_object", "archive_objects", "dimensions"),
}

RELATION_KIND_RULES = {
    "depicts": ("archive_object", "historical_event"),
    "embodied_on": ("archive_object", "physical_carrier"),
    "component_of": ("archive_object", "archive_object"),
}

CSV_EXPORTS = (
    ("schema_migrations", "support/schema-migrations.csv", ()),
    ("catalog_state", "support/catalog-state.csv", ()),
    ("units", "support/units.csv", ()),
    ("entity_kinds", "identity/entity-kinds.csv", ()),
    ("entities", "identity/entities.csv", ()),
    ("entity_names", "identity/names.csv", ()),
    ("entity_identifiers", "identity/identifiers.csv", ()),
    ("entity_redirects", "identity/redirects.csv", ()),
    ("entity_unit_memberships", "identity/unit-memberships.csv", ()),
    ("relation_predicates", "knowledge/relation-predicates.csv", ()),
    ("entity_relations", "knowledge/relations.csv", ()),
    ("evidence_sources", "knowledge/evidence-sources.csv", ("local_absolute_path",)),
    ("assertion_predicates", "knowledge/assertion-predicates.csv", ()),
    ("evidence_assertions", "knowledge/assertions.csv", ()),
    ("archive_objects", "archive-domains/archive-objects.csv", ()),
    ("historical_events", "archive-domains/historical-events.csv", ()),
    ("physical_carriers", "archive-domains/physical-carriers.csv", ()),
    ("source_batches", "files/source-batches.csv", ("local_source_root_path",)),
    ("content_blobs", "files/content-blobs.csv", ()),
    ("file_occurrences", "files/file-occurrences.csv", ()),
    ("entity_files", "files/entity-files.csv", ()),
    ("filesystem_generations", "durability/filesystem-generations.csv", ()),
)

IMPORT_ORDER = (
    "schema_migrations",
    "catalog_state",
    "units",
    "entity_kinds",
    "entities",
    "evidence_sources",
    "entity_names",
    "entity_identifiers",
    "entity_redirects",
    "entity_unit_memberships",
    "relation_predicates",
    "entity_relations",
    "assertion_predicates",
    "evidence_assertions",
    "archive_objects",
    "historical_events",
    "physical_carriers",
    "source_batches",
    "content_blobs",
    "file_occurrences",
    "entity_files",
    "filesystem_generations",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def schema_checksum() -> str:
    return hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def assert_safe_work_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    for protected in PROTECTED_SOURCE_ROOTS:
        protected_resolved = protected.resolve()
        if resolved == protected_resolved or protected_resolved in resolved.parents:
            raise CoreCatalogError(
                f"minimal-core work root cannot be inside protected source tree: {protected}"
            )
    return resolved


def normalize_entity_id(value: str) -> str:
    try:
        normalized = value.strip(" \t\r\n\f\v").upper()
        normalized.encode("ascii")
    except (AttributeError, UnicodeEncodeError) as error:
        raise CoreCatalogError("entity ID must be ASCII text") from error
    if not ENTITY_ID_RE.fullmatch(normalized):
        raise CoreCatalogError("entity ID must use canonical ISS-YYYY-XXXX-C form")
    return normalized


def entity_id_check_symbol(year: int | str, body: str) -> str:
    """Return the frozen non-mod-37 ISS check symbol.

    Hash uppercase ASCII ``YYYYXXXX`` with SHA-256, mask the first byte with
    ``0x1f``, then select that index from the Crockford Base32 alphabet.
    ``2026K7M3`` therefore maps to ``J``.
    """
    year_text = f"{int(year):04d}"
    body_text = body.upper()
    if not re.fullmatch(r"[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}", body_text):
        raise CoreCatalogError("entity ID body must contain four Crockford symbols")
    digest = hashlib.sha256(f"{year_text}{body_text}".encode("ascii")).digest()
    return CROCKFORD[digest[0] & 0x1F]


def validate_entity_id(value: str) -> bool:
    try:
        normalized = normalize_entity_id(value)
    except CoreCatalogError:
        return False
    match = ENTITY_ID_RE.fullmatch(normalized)
    assert match
    return match["check"] == entity_id_check_symbol(match["year"], match["body"])


def generate_entity_id(
    connection: sqlite3.Connection,
    year: int | None = None,
) -> str:
    assignment_year = datetime.now(UTC).year if year is None else int(year)
    if assignment_year < 1000 or assignment_year > 9999:
        raise CoreCatalogError("assignment year must contain four digits")
    for _ in range(1000):
        body = "".join(secrets.choice(CROCKFORD) for _ in range(4))
        check = entity_id_check_symbol(assignment_year, body)
        candidate = f"ISS-{assignment_year:04d}-{body}-{check}"
        if not connection.execute(
            "SELECT 1 FROM entities WHERE entity_id = ?",
            (candidate,),
        ).fetchone():
            return candidate
    raise CoreCatalogError("could not allocate a unique entity ID")


def connect_catalog(path: Path, read_only: bool = False) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise CoreCatalogError(f"catalog does not exist: {path}")
    if read_only:
        uri_path = urllib.parse.quote(path.as_posix(), safe="/")
        connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
    else:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_catalog(path: Path, tool_version: str, actor: str) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if path.exists():
        raise CoreCatalogError(f"refusing to replace existing catalog: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA_SQL)
        now = utc_now()
        connection.execute(
            """INSERT INTO schema_migrations (
                   version, applied_at, tool_version, migration_checksum
               ) VALUES (?, ?, ?, ?)""",
            (SCHEMA_VERSION, now, tool_version, schema_checksum()),
        )
        connection.executemany(
            "INSERT INTO entity_kinds (kind, label, status) VALUES (?, ?, ?)",
            INITIAL_ENTITY_KINDS,
        )
        connection.executemany(
            """INSERT INTO relation_predicates (
                   predicate, label, inverse_predicate, status
               ) VALUES (?, ?, ?, ?)""",
            INITIAL_RELATION_PREDICATES,
        )
        connection.executemany(
            """INSERT INTO assertion_predicates (
                   predicate, label, value_kind, cardinality, status
               ) VALUES (?, ?, ?, ?, ?)""",
            INITIAL_ASSERTION_PREDICATES,
        )
        connection.execute(
            """INSERT INTO catalog_state (
                   singleton, catalog_revision, published_revision, dirty_since
               ) VALUES (1, 1, 0, ?)""",
            (now,),
        )
        connection.execute(
            """INSERT INTO logbook (
                   log_id, at_utc, actor, unit_id, action, entity_type,
                   entity_id, before_json, after_json, evidence, note
               ) VALUES (?, ?, ?, NULL, 'initialize_catalog', 'catalog',
                         'minimal-core', '', ?, '', ?)""",
            (
                f"LOG-{uuid.uuid4().hex.upper()}",
                now,
                actor,
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, "tool_version": tool_version},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "new empty catalog; no pilot rows migrated",
            ),
        )
        connection.commit()
        assert_catalog_integrity(connection)
    except Exception:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    return connection


def assert_catalog_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise CoreCatalogError(f"catalog integrity check failed: {integrity}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise CoreCatalogError(f"catalog foreign-key check failed: {violations}")
    trigger_names = {
        row["name"]
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'trigger' AND name LIKE 'catalog_dirty_%'"""
        )
    }
    expected_dirty_triggers = {
        f"catalog_dirty_{table}_{operation}"
        for table in DIRTY_TRACKED_TABLES
        for operation in ("insert", "update", "delete")
    }
    if trigger_names != expected_dirty_triggers:
        raise CoreCatalogError("catalog dirty-tracking triggers are incomplete")
    migration = connection.execute(
        """SELECT version, migration_checksum
           FROM schema_migrations
           ORDER BY version DESC LIMIT 1"""
    ).fetchone()
    if (
        not migration
        or int(migration["version"]) != SCHEMA_VERSION
        or migration["migration_checksum"] != schema_checksum()
    ):
        raise CoreCatalogError("catalog schema migration does not match this tool")
    for row in connection.execute("SELECT entity_id FROM entities"):
        if not validate_entity_id(row["entity_id"]):
            raise CoreCatalogError(f"invalid canonical entity ID: {row['entity_id']}")
    canonical_alias = connection.execute(
        """SELECT identifier_id
           FROM entity_identifiers
           WHERE lower(namespace) = 'iss'
              OR value = entity_id
              OR normalized_value = entity_id
              OR normalized_value IN (SELECT entity_id FROM entities)
           LIMIT 1"""
    ).fetchone()
    if canonical_alias:
        raise CoreCatalogError(
            f"canonical ISS ID found in alias table: {canonical_alias['identifier_id']}"
        )
    state = connection.execute(
        """SELECT catalog_revision, published_revision
           FROM catalog_state WHERE singleton = 1"""
    ).fetchone()
    if not state:
        raise CoreCatalogError("catalog_state singleton is missing")
    dirty_expected = int(state["catalog_revision"]) > int(state["published_revision"])
    dirty_row = connection.execute(
        "SELECT dirty_since FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    if dirty_expected != bool(dirty_row["dirty_since"]):
        raise CoreCatalogError("catalog_state dirty marker is inconsistent")


def catalog_state(connection: sqlite3.Connection) -> sqlite3.Row:
    state = connection.execute(
        """SELECT catalog_revision, published_revision, dirty_since
           FROM catalog_state WHERE singleton = 1"""
    ).fetchone()
    if not state:
        raise CoreCatalogError("catalog_state singleton is missing")
    return state


def require_filesystem_current(connection: sqlite3.Connection) -> None:
    state = catalog_state(connection)
    if int(state["catalog_revision"]) != int(state["published_revision"]):
        raise CoreCatalogError(
            "filesystem projection is unpublished; publish the current "
            "catalog revision before sealing or cleanup"
        )


def _append_log(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    unit_id: str | None,
    after: dict[str, Any],
    note: str = "",
    before: dict[str, Any] | None = None,
    evidence: str = "",
    at_utc: str | None = None,
) -> str:
    log_id = f"LOG-{uuid.uuid4().hex.upper()}"
    connection.execute(
        """INSERT INTO logbook (
               log_id, at_utc, actor, unit_id, action, entity_type, entity_id,
               before_json, after_json, evidence, note
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log_id,
            at_utc or utc_now(),
            actor,
            unit_id,
            action,
            entity_type,
            entity_id,
            json.dumps(before or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            evidence,
            note,
        ),
    )
    return log_id


def create_unit(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    title: str,
    kind: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO units (
                   unit_id, title, kind, status, created_at, sealed_at
               ) VALUES (?, ?, ?, 'open', ?, NULL)""",
            (unit_id, title, kind, now),
        )
        _append_log(
            connection,
            actor,
            "create_unit",
            "unit",
            unit_id,
            unit_id,
            {"kind": kind, "status": "open", "title": title},
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "unit_id": unit_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def create_entity(
    connection: sqlite3.Connection,
    *,
    kind: str,
    canonical_name: str,
    unit_id: str,
    membership_role: str,
    actor: str,
    assignment_year: int | None = None,
    domain: dict[str, Any] | None = None,
    language: str = "",
    note: str = "",
) -> dict[str, Any]:
    domain = dict(domain or {})
    canonical_name = canonical_name.strip()
    if not canonical_name:
        raise CoreCatalogError("canonical name is required")
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        kind_row = connection.execute(
            "SELECT status FROM entity_kinds WHERE kind = ?",
            (kind,),
        ).fetchone()
        if not kind_row or kind_row["status"] != "active":
            raise CoreCatalogError(f"entity kind is not active: {kind}")
        unit = connection.execute(
            "SELECT status FROM units WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
        if not unit or unit["status"] != "open":
            raise CoreCatalogError(f"unit is not open: {unit_id}")
        entity_id = generate_entity_id(connection, assignment_year)
        if not validate_entity_id(entity_id):
            raise CoreCatalogError("generated entity ID failed validation")
        connection.execute(
            """INSERT INTO entities (
                   entity_id, kind, status, created_at, updated_at
               ) VALUES (?, ?, 'active', ?, ?)""",
            (entity_id, kind, now, now),
        )
        if kind == "archive_object":
            archival_label = str(domain.get("archival_label") or "").strip()
            if not archival_label:
                raise CoreCatalogError("archive_object requires archival_label")
            connection.execute(
                """INSERT INTO archive_objects (
                       entity_id, archival_label, descriptive_title,
                       description, material_technique, dimensions,
                       publication_status, status, note, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    entity_id,
                    archival_label,
                    str(domain.get("descriptive_title") or ""),
                    str(domain.get("description") or ""),
                    str(domain.get("material_technique") or ""),
                    str(domain.get("dimensions") or ""),
                    str(domain.get("publication_status") or "unknown"),
                    str(domain.get("note") or ""),
                    now,
                    now,
                ),
            )
        elif kind == "historical_event":
            title = str(domain.get("title") or canonical_name).strip()
            if not title:
                raise CoreCatalogError("historical_event requires title")
            connection.execute(
                """INSERT INTO historical_events (
                       entity_id, title, starts_on, ends_on, circa,
                       description, status, note, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    entity_id,
                    title,
                    str(domain.get("starts_on") or ""),
                    str(domain.get("ends_on") or ""),
                    int(bool(domain.get("circa"))),
                    str(domain.get("description") or ""),
                    str(domain.get("note") or ""),
                    now,
                    now,
                ),
            )
        elif kind == "physical_carrier":
            carrier_type = str(domain.get("carrier_type") or "").strip()
            if not carrier_type:
                raise CoreCatalogError("physical_carrier requires carrier_type")
            connection.execute(
                """INSERT INTO physical_carriers (
                       entity_id, carrier_type, dimensions_text, condition_note,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (
                    entity_id,
                    carrier_type,
                    str(domain.get("dimensions_text") or ""),
                    str(domain.get("condition_note") or ""),
                    now,
                    now,
                ),
            )
        else:
            raise CoreCatalogError(f"no typed domain operation for kind: {kind}")
        connection.execute(
            """INSERT INTO entity_names (
                   name_id, entity_id, name_type, value, language, status,
                   source_id, created_at, updated_at
               ) VALUES (?, ?, 'canonical', ?, ?, 'active', NULL, ?, ?)""",
            (f"NAME-{uuid.uuid4().hex.upper()}", entity_id, canonical_name, language, now, now),
        )
        connection.execute(
            """INSERT INTO entity_unit_memberships (
                   entity_id, unit_id, role, added_at
               ) VALUES (?, ?, ?, ?)""",
            (entity_id, unit_id, membership_role, now),
        )
        _append_log(
            connection,
            actor,
            "create_entity",
            kind,
            entity_id,
            unit_id,
            {
                "canonical_name": canonical_name,
                "kind": kind,
                "membership_role": membership_role,
                "status": "active",
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "entity_id": entity_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def add_alias_identifier(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    namespace: str,
    value: str,
    normalized_value: str,
    role: str,
    actor: str,
    scheme_version: str = "",
    note: str = "",
) -> dict[str, Any]:
    if namespace.casefold() == "iss" or validate_entity_id(normalized_value):
        raise CoreCatalogError("canonical ISS IDs do not belong in the alias table")
    now = utc_now()
    identifier_id = f"ID-{uuid.uuid4().hex.upper()}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            "SELECT status FROM entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if not entity or entity["status"] not in ("active", "provisional"):
            raise CoreCatalogError(f"entity cannot receive an alias: {entity_id}")
        connection.execute(
            """INSERT INTO entity_identifiers (
                   identifier_id, entity_id, namespace, value,
                   normalized_value, scheme_version, role, status,
                   created_at, retired_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL)""",
            (
                identifier_id,
                entity_id,
                namespace,
                value,
                normalized_value,
                scheme_version,
                role,
                now,
            ),
        )
        _append_log(
            connection,
            actor,
            "add_alias_identifier",
            "entity_identifier",
            identifier_id,
            None,
            {
                "entity_id": entity_id,
                "namespace": namespace,
                "normalized_value": normalized_value,
                "role": role,
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "identifier_id": identifier_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def add_unit_membership(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    role: str,
    actor: str,
) -> dict[str, Any]:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO entity_unit_memberships (
                   entity_id, unit_id, role, added_at
               ) VALUES (?, ?, ?, ?)""",
            (entity_id, unit_id, role, now),
        )
        _append_log(
            connection,
            actor,
            "add_unit_membership",
            "entity",
            entity_id,
            unit_id,
            {"role": role},
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "catalog_revision": revision,
        "state": "unpublished",
    }


def create_relation(
    connection: sqlite3.Connection,
    *,
    subject_entity_id: str,
    predicate: str,
    object_entity_id: str,
    actor: str,
    confidence: str = "certain",
    source_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    now = utc_now()
    relation_id = f"REL-{uuid.uuid4().hex.upper()}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        rule = RELATION_KIND_RULES.get(predicate)
        if not rule:
            raise CoreCatalogError(f"unsupported relation predicate: {predicate}")
        subject = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (subject_entity_id,),
        ).fetchone()
        object_row = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (object_entity_id,),
        ).fetchone()
        if not subject or not object_row:
            raise CoreCatalogError("relation endpoint does not exist")
        if (subject["kind"], object_row["kind"]) != rule:
            raise CoreCatalogError(
                f"{predicate} requires endpoint kinds {rule[0]} -> {rule[1]}"
            )
        if subject["status"] != "active" or object_row["status"] != "active":
            raise CoreCatalogError("relation endpoints must be active")
        connection.execute(
            """INSERT INTO entity_relations (
                   relation_id, subject_entity_id, predicate, object_entity_id,
                   status, confidence, source_id, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?, ?)""",
            (
                relation_id,
                subject_entity_id,
                predicate,
                object_entity_id,
                confidence,
                source_id,
                now,
                now,
            ),
        )
        _append_log(
            connection,
            actor,
            "create_relation",
            "entity_relation",
            relation_id,
            None,
            {
                "object_entity_id": object_entity_id,
                "predicate": predicate,
                "subject_entity_id": subject_entity_id,
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "relation_id": relation_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


RECONCILIATION_REVIEW_STATUSES = ("not_ready", "reconciled", "rejected")


def set_component_carrier(
    connection: sqlite3.Connection,
    *,
    component_entity_id: str,
    carrier_entity_id: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Assign or clear one editor-reviewed component-to-carrier relationship.

    An empty ``carrier_entity_id`` clears the current assignment. Historical
    relationships are superseded, never deleted.
    """
    component_entity_id = component_entity_id.strip()
    carrier_entity_id = carrier_entity_id.strip()
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise CoreCatalogError("named actor is required")
    if not component_entity_id:
        raise CoreCatalogError("component entity is required")
    if component_entity_id == carrier_entity_id:
        raise CoreCatalogError("an archive object cannot be its own carrier")
    now = utc_now()
    relation_id = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        component = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (component_entity_id,),
        ).fetchone()
        if (
            not component
            or component["kind"] != "archive_object"
            or component["status"] != "active"
        ):
            raise CoreCatalogError("component must be an active archive object")
        if carrier_entity_id:
            carrier = connection.execute(
                "SELECT kind, status FROM entities WHERE entity_id = ?",
                (carrier_entity_id,),
            ).fetchone()
            if (
                not carrier
                or carrier["kind"] != "archive_object"
                or carrier["status"] != "active"
            ):
                raise CoreCatalogError("carrier must be an active archive object")
            cycle = connection.execute(
                """WITH RECURSIVE ancestors(entity_id) AS (
                       SELECT object_entity_id
                       FROM entity_relations
                       WHERE subject_entity_id = ?
                         AND predicate = 'component_of'
                         AND status = 'accepted'
                       UNION
                       SELECT relation.object_entity_id
                       FROM entity_relations relation
                       JOIN ancestors
                         ON relation.subject_entity_id = ancestors.entity_id
                       WHERE relation.predicate = 'component_of'
                         AND relation.status = 'accepted'
                   )
                   SELECT 1 FROM ancestors WHERE entity_id = ? LIMIT 1""",
                (carrier_entity_id, component_entity_id),
            ).fetchone()
            if cycle:
                raise CoreCatalogError("carrier assignment would create a cycle")
        current = list(
            connection.execute(
                """SELECT relation_id, object_entity_id
                   FROM entity_relations
                   WHERE subject_entity_id = ?
                     AND predicate = 'component_of'
                     AND status = 'accepted'
                   ORDER BY updated_at DESC, relation_id DESC""",
                (component_entity_id,),
            )
        )
        if (not current and not carrier_entity_id) or (
            len(current) == 1
            and str(current[0]["object_entity_id"]) == carrier_entity_id
        ):
            connection.rollback()
            return {
                "result": "ok",
                "relation_id": (
                    str(current[0]["relation_id"]) if current else ""
                ),
                "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
                "state": "unchanged",
                "component_entity_id": component_entity_id,
                "carrier_entity_id": carrier_entity_id,
            }
        before = {
            "carrier_entity_ids": [str(row["object_entity_id"]) for row in current]
        }
        connection.execute(
            """UPDATE entity_relations
               SET status = 'superseded', updated_at = ?
               WHERE subject_entity_id = ?
                 AND predicate = 'component_of'
                 AND status = 'accepted'""",
            (now, component_entity_id),
        )
        if carrier_entity_id:
            relation_id = f"REL-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO entity_relations (
                       relation_id, subject_entity_id, predicate,
                       object_entity_id, status, confidence, source_id,
                       created_at, updated_at
                   ) VALUES (?, ?, 'component_of', ?, 'accepted', 'certain',
                             NULL, ?, ?)""",
                (
                    relation_id,
                    component_entity_id,
                    carrier_entity_id,
                    now,
                    now,
                ),
            )
        _append_log(
            connection,
            actor,
            "set_component_carrier",
            "entity_relation",
            relation_id or component_entity_id,
            _entity_unit_id(connection, component_entity_id),
            {
                "component_entity_id": component_entity_id,
                "carrier_entity_id": carrier_entity_id,
                "relation_id": relation_id,
            },
            before=before,
            note=reason,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "relation_id": relation_id,
        "catalog_revision": revision,
        "state": "unpublished",
        "component_entity_id": component_entity_id,
        "carrier_entity_id": carrier_entity_id,
    }


def reconciliation_review_statuses(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """Return latest explicit or accession-implied review state per entity."""
    return {
        entity_id: str(detail["status"])
        for entity_id, detail in reconciliation_review_details(connection).items()
    }


def reconciliation_review_details(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    """Return latest review state and retained-record context per entity."""
    details: dict[str, dict[str, str]] = {}
    for row in connection.execute(
        """SELECT entity_id, action, after_json, actor, note, at_utc
           FROM logbook
           WHERE action IN (
               'prepare_recovery_accession_publication',
               'set_reconciliation_review_status'
           )
           ORDER BY at_utc, log_id"""
    ):
        entity_id = str(row["entity_id"] or "")
        if row["action"] == "prepare_recovery_accession_publication":
            details[entity_id] = {
                "status": "reconciled",
                "retained_entity_id": "",
                "actor": str(row["actor"] or ""),
                "reason": str(row["note"] or ""),
                "decided_at": str(row["at_utc"] or ""),
            }
            continue
        try:
            payload = json.loads(str(row["after_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        status = str(payload.get("status") or "")
        if status in RECONCILIATION_REVIEW_STATUSES:
            details[entity_id] = {
                "status": status,
                "retained_entity_id": str(
                    payload.get("retained_entity_id") or ""
                ),
                "actor": str(row["actor"] or ""),
                "reason": str(row["note"] or ""),
                "decided_at": str(row["at_utc"] or ""),
            }
    return details


def set_reconciliation_review_status(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    status: str,
    actor: str,
    reason: str = "",
    retained_entity_id: str = "",
) -> dict[str, Any]:
    """Record an explicit editor completion or reopening decision."""
    entity_id = normalize_entity_id(entity_id)
    status = status.strip().casefold()
    actor = actor.strip()
    reason = reason.strip()
    retained_entity_id = retained_entity_id.strip()
    if status not in RECONCILIATION_REVIEW_STATUSES:
        raise CoreCatalogError(f"unsupported reconciliation review status: {status}")
    if not actor:
        raise CoreCatalogError("named reconciliation reviewer is required")
    if status != "rejected" and retained_entity_id:
        raise CoreCatalogError(
            "a retained record is only valid for rejected reconciliation records"
        )
    if retained_entity_id == entity_id:
        raise CoreCatalogError("a rejected record cannot retain itself")
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            """SELECT e.kind, e.status, m.unit_id
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               WHERE e.entity_id=? AND m.role='primary'""",
            (entity_id,),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
        ):
            raise CoreCatalogError(
                "reconciliation status requires an active archive object"
            )
        if retained_entity_id:
            retained = connection.execute(
                """SELECT kind, status FROM entities WHERE entity_id=?""",
                (retained_entity_id,),
            ).fetchone()
            if (
                not retained
                or retained["kind"] != "archive_object"
                or retained["status"] != "active"
            ):
                raise CoreCatalogError(
                    "retained duplicate target must be an active archive object"
                )
        current_detail = reconciliation_review_details(connection).get(
            entity_id,
            {
                "status": "not_ready",
                "retained_entity_id": "",
            },
        )
        if (
            current_detail["status"] == status
            and current_detail.get("retained_entity_id", "")
            == retained_entity_id
        ):
            connection.rollback()
            return {
                "result": "ok",
                "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
                "state": "unchanged",
                "status": status,
            }
        _append_log(
            connection,
            actor,
            "set_reconciliation_review_status",
            "archive_object",
            entity_id,
            str(entity["unit_id"]),
            {
                "status": status,
                "retained_entity_id": retained_entity_id,
                "future_drain_review": status == "rejected",
                "deletion_authorized": False,
            },
            before={
                "status": current_detail["status"],
                "retained_entity_id": current_detail.get(
                    "retained_entity_id", ""
                ),
            },
            note=reason,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "catalog_revision": revision,
        "state": "reviewed",
        "status": status,
    }


def merge_entities(
    connection: sqlite3.Connection,
    *,
    former_entity_id: str,
    canonical_entity_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        former = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (former_entity_id,),
        ).fetchone()
        target = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (canonical_entity_id,),
        ).fetchone()
        if not former or former["status"] not in ("active", "provisional"):
            raise CoreCatalogError("former entity is not mergeable")
        if not target or target["status"] != "active":
            raise CoreCatalogError("merge target must be active")
        if former_entity_id == canonical_entity_id:
            raise CoreCatalogError("entity cannot merge into itself")
        if former["kind"] != target["kind"]:
            raise CoreCatalogError("merge endpoints must have the same entity kind")
        if connection.execute(
            """SELECT 1 FROM entity_redirects
               WHERE former_entity_id = ?""",
            (canonical_entity_id,),
        ).fetchone():
            raise CoreCatalogError("merge target must not itself redirect")
        connection.execute(
            "UPDATE entities SET status = 'merged', updated_at = ? WHERE entity_id = ?",
            (now, former_entity_id),
        )
        connection.execute(
            """UPDATE entity_redirects
               SET canonical_entity_id = ?
               WHERE canonical_entity_id = ?""",
            (canonical_entity_id, former_entity_id),
        )
        connection.execute(
            """INSERT INTO entity_redirects (
                   former_entity_id, canonical_entity_id, reason, actor, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (former_entity_id, canonical_entity_id, reason, actor, now),
        )
        _append_log(
            connection,
            actor,
            "merge_entities",
            "entity",
            former_entity_id,
            None,
            {"canonical_entity_id": canonical_entity_id, "status": "merged"},
            before={"status": former["status"]},
            note=reason,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "catalog_revision": revision,
        "state": "unpublished",
    }


def add_evidence_source(
    connection: sqlite3.Connection,
    *,
    source_type: str,
    actor: str,
    system: str = "",
    external_id: str = "",
    captured_at: str | None = None,
    snapshot_digest: str = "",
    source_root_id: str = "",
    relative_path: str = "",
    local_absolute_path: str = "",
    citation: str = "",
    note: str = "",
) -> dict[str, Any]:
    _assert_controlled_relative_path(relative_path, "evidence source")
    source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO evidence_sources (
                   source_id, source_type, system, external_id, captured_at,
                   snapshot_digest, source_root_id, relative_path,
                   local_absolute_path, citation, note
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                source_type,
                system,
                external_id,
                captured_at,
                snapshot_digest,
                source_root_id,
                relative_path,
                local_absolute_path,
                citation,
                note,
            ),
        )
        _append_log(
            connection,
            actor,
            "add_evidence_source",
            "evidence_source",
            source_id,
            None,
            {
                "external_id": external_id,
                "source_root_id": source_root_id,
                "source_type": source_type,
                "system": system,
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "source_id": source_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def create_text_assertion(
    connection: sqlite3.Connection,
    *,
    subject_entity_id: str,
    predicate: str,
    value_text: str,
    actor: str,
    confidence: str,
    source_id: str,
    note: str = "",
) -> dict[str, Any]:
    assertion_id = f"ASSERT-{uuid.uuid4().hex.upper()}"
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        predicate_row = connection.execute(
            """SELECT value_kind, status
               FROM assertion_predicates WHERE predicate = ?""",
            (predicate,),
        ).fetchone()
        if (
            not predicate_row
            or predicate_row["status"] != "active"
            or predicate_row["value_kind"] != "text"
        ):
            raise CoreCatalogError(f"active text assertion predicate required: {predicate}")
        if not connection.execute(
            "SELECT 1 FROM evidence_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone():
            raise CoreCatalogError(f"unknown evidence source: {source_id}")
        connection.execute(
            """INSERT INTO evidence_assertions (
                   assertion_id, subject_entity_id, predicate, value_text,
                   object_entity_id, status, confidence, source_id, actor,
                   asserted_at, supersedes_assertion_id, note
               ) VALUES (?, ?, ?, ?, NULL, 'proposed', ?, ?, ?, ?, NULL, ?)""",
            (
                assertion_id,
                subject_entity_id,
                predicate,
                value_text,
                confidence,
                source_id,
                actor,
                now,
                note,
            ),
        )
        _append_log(
            connection,
            actor,
            "create_evidence_assertion",
            "evidence_assertion",
            assertion_id,
            None,
            {
                "predicate": predicate,
                "subject_entity_id": subject_entity_id,
                "value_text": value_text,
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "assertion_id": assertion_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def promote_text_assertion(
    connection: sqlite3.Connection,
    *,
    assertion_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        assertion = connection.execute(
            """SELECT a.*, e.kind
               FROM evidence_assertions a
               JOIN entities e ON e.entity_id = a.subject_entity_id
               WHERE a.assertion_id = ?""",
            (assertion_id,),
        ).fetchone()
        if not assertion or assertion["status"] not in ("proposed", "unresolved"):
            raise CoreCatalogError("assertion is not promotable")
        target = PROMOTABLE_TEXT_ASSERTIONS.get(assertion["predicate"])
        if not target or assertion["kind"] != target[0]:
            raise CoreCatalogError("assertion predicate is incompatible with entity kind")
        table, column = target[1], target[2]
        connection.execute(
            f'UPDATE "{table}" SET "{column}" = ?, updated_at = ? WHERE entity_id = ?',
            (assertion["value_text"], now, assertion["subject_entity_id"]),
        )
        connection.execute(
            """UPDATE evidence_assertions
               SET status = 'superseded'
               WHERE subject_entity_id = ? AND predicate = ?
                 AND status = 'accepted' AND assertion_id <> ?""",
            (
                assertion["subject_entity_id"],
                assertion["predicate"],
                assertion_id,
            ),
        )
        connection.execute(
            "UPDATE evidence_assertions SET status = 'accepted' WHERE assertion_id = ?",
            (assertion_id,),
        )
        _append_log(
            connection,
            actor,
            "promote_evidence_assertion",
            "evidence_assertion",
            assertion_id,
            None,
            {
                "entity_id": assertion["subject_entity_id"],
                "target": f"{table}.{column}",
                "value": assertion["value_text"],
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "catalog_revision": revision,
        "state": "unpublished",
    }


IDENTITY_REVIEW_OUTCOMES = (
    "same_entity",
    "component_of",
    "wrong_master",
    "not_same",
    "defer",
)

RECONCILIATION_FIELD_ACTIONS = (
    "keep_catalog",
    "accept_local",
    "accept_external",
    "corrected",
    "defer",
)

SOURCE_CANDIDATE_REVIEW_STATES = (
    "candidate",
    "redundant",
    "selected_master",
    "selected_derivative",
)

SURROGATE_SWAP_REVIEW_STATES = (
    "candidate",
    "marked_for_swap",
    "rejected",
)

RECOVERY_CORRECTION_STATES = ("open", "ready", "complete")


def recovery_correction_states(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    """Return the latest explicit corrective-reaccession state by entity."""

    states: dict[str, dict[str, str]] = {}
    for row in connection.execute(
        """SELECT after_json, actor, note, at_utc
           FROM logbook
           WHERE action='set_recovery_correction_status'
           ORDER BY at_utc DESC, log_id DESC"""
    ):
        try:
            payload = json.loads(str(row["after_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        entity_id = str(payload.get("entity_id") or "")
        state = str(payload.get("state") or "")
        if (
            entity_id
            and entity_id not in states
            and state in RECOVERY_CORRECTION_STATES
        ):
            states[entity_id] = {
                "state": state,
                "actor": str(row["actor"]),
                "reason": str(row["note"] or ""),
                "decided_at": str(row["at_utc"]),
            }
    return states


def set_recovery_correction_status(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    state: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Log that an audited correction is open, ready, or complete."""

    entity_id = normalize_entity_id(entity_id)
    state = state.strip()
    if state not in RECOVERY_CORRECTION_STATES:
        raise CoreCatalogError("unsupported recovery correction state")
    entity = connection.execute(
        """SELECT 1 FROM archive_objects
           WHERE entity_id=? AND status='active'""",
        (entity_id,),
    ).fetchone()
    if not entity:
        raise CoreCatalogError(
            f"recovery correction requires an active archive object: {entity_id}"
        )
    current = recovery_correction_states(connection).get(entity_id, {})
    if current.get("state") == state:
        return {
            "result": "ok",
            "entity_id": entity_id,
            "state": "unchanged",
            "correction_state": state,
            "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
        }
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _append_log(
            connection,
            actor,
            "set_recovery_correction_status",
            "archive_object",
            entity_id,
            None,
            {"entity_id": entity_id, "state": state},
            note=reason,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "entity_id": entity_id,
        "state": "unpublished",
        "correction_state": state,
        "catalog_revision": revision,
    }


def source_candidate_reviews(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
) -> dict[str, dict[str, Any]]:
    """Return the latest logged review for each recovery source candidate."""

    reviews: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT after_json, actor, note, at_utc
           FROM logbook WHERE action = 'review_source_candidate'
           ORDER BY at_utc DESC, log_id DESC"""
    ):
        try:
            payload = json.loads(str(row["after_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        evidence_id = str(payload.get("evidence_id") or "")
        if (
            payload.get("entity_id") == entity_id
            and evidence_id
            and evidence_id not in reviews
        ):
            reviews[evidence_id] = {
                **payload,
                "actor": str(row["actor"]),
                "reason": str(row["note"] or ""),
                "reviewed_at": str(row["at_utc"]),
            }
    return reviews


def review_source_candidate(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    evidence_id: str,
    content_digest: str,
    candidate_role: str,
    source_name: str,
    state: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Log a non-destructive recovery-candidate disposition."""

    entity_id = normalize_entity_id(entity_id)
    evidence_id = evidence_id.strip()
    content_digest = content_digest.strip().casefold()
    candidate_role = candidate_role.strip()
    source_name = Path(source_name).name
    state = state.strip().casefold()
    actor = actor.strip()
    reason = reason.strip()
    if state not in SOURCE_CANDIDATE_REVIEW_STATES:
        raise CoreCatalogError(f"unsupported source-candidate state: {state}")
    if state == "selected_master" and candidate_role not in {
        "master_tiff_candidate",
        "high_resolution_jpeg_candidate",
    }:
        raise CoreCatalogError("only a master candidate can be selected as master")
    if state == "selected_derivative" and candidate_role != "local_derivative":
        raise CoreCatalogError(
            "only a local derivative candidate can be selected as derivative"
        )
    if not actor:
        raise CoreCatalogError("named source-candidate reviewer is required")
    if not evidence_id or not candidate_role or not source_name:
        raise CoreCatalogError("source-candidate evidence is incomplete")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest):
        raise CoreCatalogError("source-candidate SHA-256 evidence is invalid")
    expected = {
        "entity_id": entity_id,
        "evidence_id": evidence_id,
        "content_digest": content_digest,
        "candidate_role": candidate_role,
        "source_name": source_name,
        "state": state,
    }
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            """SELECT u.unit_id, u.status AS unit_status, e.kind, e.status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               JOIN units u ON u.unit_id=m.unit_id
               WHERE e.entity_id=? AND m.role='primary'""",
            (entity_id,),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                "source-candidate review requires an active object in an open unit"
            )
        current_reviews = source_candidate_reviews(connection, entity_id=entity_id)
        if state == "selected_derivative" and any(
            review.get("state") == "selected_master"
            and review.get("candidate_role") == "master_tiff_candidate"
            for review in current_reviews.values()
        ):
            raise CoreCatalogError(
                "a TIFF master uses the uniformly generated archive derivative"
            )
        prior = current_reviews.get(evidence_id)
        if prior and all(prior.get(key) == value for key, value in expected.items()):
            if prior.get("reason", "") == reason:
                connection.rollback()
                return {
                    "result": "ok",
                    "operation_state": "unchanged",
                    **expected,
                    "catalog_revision": int(
                        catalog_state(connection)["catalog_revision"]
                    ),
                }
        if state.startswith("selected_"):
            for other_id, review in current_reviews.items():
                if other_id != evidence_id and review.get("state") == state:
                    raise CoreCatalogError(
                        f"another source candidate is already {state.replace('_', ' ')}"
                    )
        decision_id = f"SOURCE-CANDIDATE-{uuid.uuid4().hex.upper()}"
        _append_log(
            connection,
            actor,
            "review_source_candidate",
            "reconciliation_decision",
            decision_id,
            str(entity["unit_id"]),
            expected,
            note=reason,
            evidence=content_digest,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "operation_state": "reviewed",
        "catalog_revision": revision,
        **expected,
    }


def select_tiff_master_with_generated_derivative(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    master_candidate: dict[str, str],
    other_candidates: list[dict[str, str]],
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Select one TIFF master and retire supplied derivatives atomically.

    Every non-selected candidate remains preserved evidence. ``redundant``
    means only that it will not become the active archive representation.
    """

    entity_id = normalize_entity_id(entity_id)
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise CoreCatalogError("named source-candidate reviewer is required")
    candidates = [dict(master_candidate), *(dict(item) for item in other_candidates)]
    if master_candidate.get("candidate_role") != "master_tiff_candidate":
        raise CoreCatalogError("generated derivative policy requires a TIFF master")
    evidence_ids = [str(item.get("evidence_id") or "").strip() for item in candidates]
    if not evidence_ids or any(not value for value in evidence_ids):
        raise CoreCatalogError("source-candidate evidence is incomplete")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise CoreCatalogError("source-candidate evidence is duplicated")
    desired: list[dict[str, str]] = []
    for index, item in enumerate(candidates):
        digest = str(item.get("content_digest") or "").strip().casefold()
        role = str(item.get("candidate_role") or "").strip()
        source_name = Path(str(item.get("source_name") or "")).name
        if not role or not source_name or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise CoreCatalogError("source-candidate evidence is incomplete")
        desired.append(
            {
                "entity_id": entity_id,
                "evidence_id": evidence_ids[index],
                "content_digest": digest,
                "candidate_role": role,
                "source_name": source_name,
                "state": "selected_master" if index == 0 else "redundant",
            }
        )
    now = utc_now()
    changed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            """SELECT u.unit_id, u.status AS unit_status, e.kind, e.status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               JOIN units u ON u.unit_id=m.unit_id
               WHERE e.entity_id=? AND m.role='primary'""",
            (entity_id,),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                "source-candidate review requires an active object in an open unit"
            )
        current = source_candidate_reviews(connection, entity_id=entity_id)
        for expected in desired:
            prior = current.get(expected["evidence_id"], {})
            if all(prior.get(key) == value for key, value in expected.items()):
                continue
            decision_id = f"SOURCE-CANDIDATE-{uuid.uuid4().hex.upper()}"
            _append_log(
                connection,
                actor,
                "review_source_candidate",
                "reconciliation_decision",
                decision_id,
                str(entity["unit_id"]),
                expected,
                note=reason,
                evidence=expected["content_digest"],
                at_utc=now,
            )
            changed += 1
        if not changed:
            connection.rollback()
            return {
                "result": "ok",
                "operation_state": "unchanged",
                "state": "selected_master",
                "retired_candidates": len(other_candidates),
                "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
            }
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "operation_state": "reviewed",
        "state": "selected_master",
        "retired_candidates": len(other_candidates),
        "catalog_revision": revision,
    }


def surrogate_swap_reviews(
    connection: sqlite3.Connection,
    *,
    entity_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the latest review state for later surrogate-to-master swaps."""

    reviews: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT after_json, actor, note, at_utc
           FROM logbook WHERE action = 'review_surrogate_swap_candidate'
           ORDER BY at_utc DESC, log_id DESC"""
    ):
        try:
            payload = json.loads(str(row["after_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        key = str(payload.get("candidate_key") or "")
        payload_entity = str(payload.get("entity_id") or "")
        state = str(payload.get("state") or "")
        if (
            key
            and key not in reviews
            and state in SURROGATE_SWAP_REVIEW_STATES
            and (entity_id is None or payload_entity == entity_id)
        ):
            reviews[key] = {
                **payload,
                "actor": str(row["actor"]),
                "reason": str(row["note"] or ""),
                "reviewed_at": str(row["at_utc"]),
            }
    return reviews


def review_surrogate_swap_candidate(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    candidate_key: str,
    candidate_path: str,
    candidate_size_bytes: int,
    candidate_width_px: int,
    candidate_height_px: int,
    active_digest: str,
    active_size_bytes: int,
    active_width_px: int,
    active_height_px: int,
    state: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Log a non-destructive marker for later surrogate replacement."""

    entity_id = normalize_entity_id(entity_id)
    candidate_key = candidate_key.strip()
    candidate_path = candidate_path.strip()
    state = state.strip().casefold()
    actor = actor.strip()
    reason = reason.strip()
    active_digest = active_digest.strip().casefold()
    if state not in SURROGATE_SWAP_REVIEW_STATES:
        raise CoreCatalogError(f"unsupported surrogate-swap state: {state}")
    if not actor:
        raise CoreCatalogError("named surrogate-swap reviewer is required")
    if not candidate_key or not candidate_path:
        raise CoreCatalogError("surrogate-swap candidate evidence is incomplete")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", active_digest):
        raise CoreCatalogError("active surrogate digest evidence is invalid")
    expected = {
        "entity_id": entity_id,
        "candidate_key": candidate_key,
        "candidate_path": candidate_path,
        "candidate_size_bytes": int(candidate_size_bytes),
        "candidate_width_px": int(candidate_width_px),
        "candidate_height_px": int(candidate_height_px),
        "active_digest": active_digest,
        "active_size_bytes": int(active_size_bytes),
        "active_width_px": int(active_width_px),
        "active_height_px": int(active_height_px),
        "state": state,
    }
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            """SELECT u.unit_id, u.status AS unit_status, e.kind, e.status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               JOIN units u ON u.unit_id=m.unit_id
               WHERE e.entity_id=? AND m.role='primary'""",
            (entity_id,),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                "surrogate-swap review requires an active object in an open unit"
            )
        active = connection.execute(
            """SELECT 1
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               WHERE ef.entity_id=? AND ef.status='active'
                 AND ef.role='preservation_surrogate'
                 AND fo.content_digest=?""",
            (entity_id, active_digest),
        ).fetchone()
        if not active:
            raise CoreCatalogError(
                "surrogate-swap review is not bound to the active surrogate"
            )
        prior = surrogate_swap_reviews(connection, entity_id=entity_id).get(
            candidate_key
        )
        if prior and all(prior.get(key) == value for key, value in expected.items()):
            if prior.get("reason", "") == reason:
                connection.rollback()
                return {
                    "result": "ok",
                    "operation_state": "unchanged",
                    **expected,
                    "catalog_revision": int(
                        catalog_state(connection)["catalog_revision"]
                    ),
                }
        decision_id = f"SURROGATE-SWAP-{uuid.uuid4().hex.upper()}"
        _append_log(
            connection,
            actor,
            "review_surrogate_swap_candidate",
            "reconciliation_decision",
            decision_id,
            str(entity["unit_id"]),
            expected,
            note=reason,
            evidence=active_digest,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "operation_state": "reviewed",
        "catalog_revision": revision,
        **expected,
    }


def _decision_log_payload(
    connection: sqlite3.Connection,
    *,
    action: str,
    decision_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT after_json
           FROM logbook
           WHERE action = ? AND entity_id = ?
           ORDER BY at_utc DESC, log_id DESC
           LIMIT 1""",
        (action, decision_id),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["after_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise CoreCatalogError(
            f"invalid idempotency record for decision: {decision_id}"
        ) from error
    if not isinstance(payload, dict):
        raise CoreCatalogError(f"invalid idempotency record for decision: {decision_id}")
    return payload


def _entity_unit_id(
    connection: sqlite3.Connection,
    entity_id: str,
) -> str | None:
    row = connection.execute(
        """SELECT unit_id
           FROM entity_unit_memberships
           WHERE entity_id = ?
           ORDER BY added_at, unit_id
           LIMIT 1""",
        (entity_id,),
    ).fetchone()
    return str(row["unit_id"]) if row else None


def _resolved_external_identity(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    external_system: str,
    external_id: str,
) -> str:
    for row in connection.execute(
        """SELECT after_json
           FROM logbook
           WHERE action = 'review_external_identity'
           ORDER BY at_utc DESC, log_id DESC"""
    ):
        try:
            payload = json.loads(row["after_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("entity_id") == entity_id
            and payload.get("external_system") == external_system
            and payload.get("external_id") == external_id
        ):
            outcome = str(payload.get("outcome") or "")
            if outcome not in ("same_entity", "component_of"):
                raise CoreCatalogError(
                    "external identity is not currently resolved for this entity"
                )
            return outcome
    raise CoreCatalogError(
        "a resolved same-entity or component identity review is required"
    )


def _resolved_local_identity(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
) -> str:
    for row in connection.execute(
        """SELECT after_json FROM logbook
           WHERE action='review_local_identity'
           ORDER BY at_utc DESC, log_id DESC"""
    ):
        try:
            payload = json.loads(row["after_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("entity_id") == entity_id
        ):
            outcome = str(payload.get("outcome") or "")
            if outcome != "same_entity":
                raise CoreCatalogError(
                    "local identity is not currently resolved for this entity"
                )
            return outcome
    raise CoreCatalogError(
        "a resolved exact-local identity review is required"
    )


def review_external_identity(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    entity_id: str,
    external_system: str,
    external_id: str,
    outcome: str,
    actor: str,
    reason: str,
    parent_entity_id: str = "",
    captured_at: str | None = None,
    snapshot_digest: str = "",
    relative_path: str = "",
    citation: str = "",
) -> dict[str, Any]:
    """Record one idempotent identity review and promote accepted identity facts."""
    if outcome not in IDENTITY_REVIEW_OUTCOMES:
        raise CoreCatalogError(f"unsupported identity review outcome: {outcome}")
    decision_id = decision_id.strip()
    external_system = external_system.strip().casefold()
    external_id = external_id.strip()
    reason = reason.strip()
    if not decision_id:
        raise CoreCatalogError("identity decision ID is required")
    if not external_system or not external_id:
        raise CoreCatalogError("external system and identifier are required")
    if outcome == "component_of" and not parent_entity_id:
        raise CoreCatalogError("component identity requires a parent entity")
    _assert_controlled_relative_path(relative_path, "identity evidence")
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _decision_log_payload(
            connection,
            action="review_external_identity",
            decision_id=decision_id,
        )
        expected = {
            "entity_id": entity_id,
            "external_system": external_system,
            "external_id": external_id,
            "outcome": outcome,
            "parent_entity_id": parent_entity_id,
        }
        if existing:
            if any(existing.get(key, "") != value for key, value in expected.items()):
                raise CoreCatalogError(
                    "identity decision ID was already used for different inputs"
                )
            connection.rollback()
            return {
                "result": "ok",
                "decision_id": decision_id,
                "catalog_revision": int(
                    catalog_state(connection)["catalog_revision"]
                ),
                "state": "unchanged",
                **existing,
            }
        entity = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        if not entity or entity["status"] not in ("active", "provisional"):
            raise CoreCatalogError(f"entity cannot be reviewed: {entity_id}")
        source_id = ""
        identifier_id = ""
        relation_id = ""
        if outcome in ("same_entity", "component_of"):
            source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO evidence_sources (
                       source_id, source_type, system, external_id, captured_at,
                       snapshot_digest, source_root_id, relative_path,
                       local_absolute_path, citation, note
                   ) VALUES (?, 'external_snapshot', ?, ?, ?, ?, '',
                             ?, '', ?, ?)""",
                (
                    source_id,
                    external_system,
                    external_id,
                    captured_at,
                    snapshot_digest,
                    relative_path,
                    citation,
                    reason,
                ),
            )
            normalized = external_id.casefold()
            alias = connection.execute(
                """SELECT identifier_id, entity_id
                   FROM entity_identifiers
                   WHERE namespace = ? AND normalized_value = ?
                     AND status = 'active'""",
                (external_system, normalized),
            ).fetchone()
            if alias and alias["entity_id"] != entity_id:
                raise CoreCatalogError(
                    "external identifier is already assigned to another entity"
                )
            if alias:
                identifier_id = str(alias["identifier_id"])
            else:
                identifier_id = f"ID-{uuid.uuid4().hex.upper()}"
                connection.execute(
                    """INSERT INTO entity_identifiers (
                           identifier_id, entity_id, namespace, value,
                           normalized_value, scheme_version, role, status,
                           created_at, retired_at
                       ) VALUES (?, ?, ?, ?, ?, '', 'external', 'active',
                                 ?, NULL)""",
                    (
                        identifier_id,
                        entity_id,
                        external_system,
                        external_id,
                        normalized,
                        now,
                    ),
                )
            if outcome == "component_of":
                parent = connection.execute(
                    "SELECT kind, status FROM entities WHERE entity_id = ?",
                    (parent_entity_id,),
                ).fetchone()
                if (
                    not parent
                    or entity["kind"] != "archive_object"
                    or parent["kind"] != "archive_object"
                    or parent["status"] != "active"
                ):
                    raise CoreCatalogError(
                        "component identity requires active archive-object endpoints"
                    )
                relation = connection.execute(
                    """SELECT relation_id
                       FROM entity_relations
                       WHERE subject_entity_id = ?
                         AND predicate = 'component_of'
                         AND object_entity_id = ?
                         AND status = 'accepted'""",
                    (entity_id, parent_entity_id),
                ).fetchone()
                if relation:
                    relation_id = str(relation["relation_id"])
                else:
                    relation_id = f"REL-{uuid.uuid4().hex.upper()}"
                    connection.execute(
                        """INSERT INTO entity_relations (
                               relation_id, subject_entity_id, predicate,
                               object_entity_id, status, confidence, source_id,
                               created_at, updated_at
                           ) VALUES (?, ?, 'component_of', ?, 'accepted',
                                     'certain', ?, ?, ?)""",
                        (
                            relation_id,
                            entity_id,
                            parent_entity_id,
                            source_id,
                            now,
                            now,
                        ),
                    )
        payload = {
            **expected,
            "source_id": source_id,
            "identifier_id": identifier_id,
            "relation_id": relation_id,
        }
        _append_log(
            connection,
            actor,
            "review_external_identity",
            "reconciliation_decision",
            decision_id,
            _entity_unit_id(connection, entity_id),
            payload,
            note=reason,
            evidence=snapshot_digest or citation,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "catalog_revision": revision,
        "state": (
            "unpublished"
            if outcome in ("same_entity", "component_of")
            else "reviewed"
        ),
        **payload,
    }


def review_local_identity(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    entity_id: str,
    local_archival_id: str,
    source_record_locator: str,
    outcome: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Resolve one exact local record without inventing an external identity."""

    if outcome not in {"same_entity", "wrong_master", "defer"}:
        raise CoreCatalogError(
            "local-only identity supports same entity, wrong master, or defer"
        )
    decision_id = decision_id.strip()
    local_archival_id = local_archival_id.strip()
    source_record_locator = source_record_locator.strip()
    reason = reason.strip()
    if not decision_id or not local_archival_id or not source_record_locator:
        raise CoreCatalogError(
            "local identity requires a decision ID, archival ID, and source record"
        )
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _decision_log_payload(
            connection,
            action="review_local_identity",
            decision_id=decision_id,
        )
        expected = {
            "entity_id": entity_id,
            "local_archival_id": local_archival_id,
            "outcome": outcome,
            "source_record_locator": source_record_locator,
        }
        if existing:
            if any(existing.get(key, "") != value for key, value in expected.items()):
                raise CoreCatalogError(
                    "local identity decision ID was already used for different inputs"
                )
            connection.rollback()
            return {
                "result": "ok",
                "decision_id": decision_id,
                "catalog_revision": int(
                    catalog_state(connection)["catalog_revision"]
                ),
                "state": "unchanged",
                **existing,
            }
        entity = connection.execute(
            """SELECT e.kind, e.status, a.archival_label
               FROM entities e
               JOIN archive_objects a ON a.entity_id=e.entity_id
               WHERE e.entity_id=?""",
            (entity_id,),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] not in ("active", "provisional")
        ):
            raise CoreCatalogError(f"archive object cannot be reviewed: {entity_id}")
        if str(entity["archival_label"]).casefold() != local_archival_id.casefold():
            raise CoreCatalogError(
                "local archival ID does not match the archive object"
            )
        source_id = ""
        if outcome == "same_entity":
            source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO evidence_sources (
                       source_id, source_type, system, external_id, captured_at,
                       snapshot_digest, source_root_id, relative_path,
                       local_absolute_path, citation, note
                   ) VALUES (?, 'local_record', 'local-archive-record', ?, ?,
                             '', '', '', '', ?, ?)""",
                (
                    source_id,
                    local_archival_id,
                    now,
                    source_record_locator,
                    reason,
                ),
            )
        payload = {**expected, "source_id": source_id}
        _append_log(
            connection,
            actor,
            "review_local_identity",
            "reconciliation_decision",
            decision_id,
            _entity_unit_id(connection, entity_id),
            payload,
            note=reason,
            evidence=source_record_locator,
            at_utc=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
        "state": "unpublished" if outcome == "same_entity" else "reviewed",
        **payload,
    }


def accept_reconciliation_text(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    entity_id: str,
    predicate: str,
    action: str,
    value_text: str,
    actor: str,
    reason: str,
    external_system: str,
    external_id: str,
    local_search_status: str,
    source_type: str,
    captured_at: str | None = None,
    snapshot_digest: str = "",
    source_root_id: str = "",
    relative_path: str = "",
    citation: str = "",
) -> dict[str, Any]:
    """Accept one reconciled text value through evidence and typed-domain state."""
    if action not in RECONCILIATION_FIELD_ACTIONS:
        raise CoreCatalogError(f"unsupported reconciliation field action: {action}")
    decision_id = decision_id.strip()
    reason = reason.strip()
    value_text = value_text.strip()
    external_system = external_system.strip().casefold()
    external_id = external_id.strip()
    if not decision_id:
        raise CoreCatalogError("field decision ID is required")
    if predicate not in PROMOTABLE_TEXT_ASSERTIONS:
        raise CoreCatalogError(f"unsupported reconciliation field: {predicate}")
    if action != "defer" and not value_text:
        raise CoreCatalogError("accepted reconciliation value cannot be empty")
    if action == "accept_local" and local_search_status != "exact_inventory":
        raise CoreCatalogError("local value requires one exact local source record")
    if action == "accept_external" and local_search_status not in (
        "exact_inventory",
        "not_found",
    ):
        raise CoreCatalogError(
            "external value requires an exact local record or a completed not-found search"
        )
    _assert_controlled_relative_path(relative_path, "reconciliation evidence")
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _decision_log_payload(
            connection,
            action="accept_reconciliation_field",
            decision_id=decision_id,
        )
        expected = {
            "entity_id": entity_id,
            "predicate": predicate,
            "action": action,
            "value_text": value_text,
            "external_system": external_system,
            "external_id": external_id,
        }
        if existing:
            if any(existing.get(key, "") != value for key, value in expected.items()):
                raise CoreCatalogError(
                    "field decision ID was already used for different inputs"
                )
            connection.rollback()
            return {
                "result": "ok",
                "decision_id": decision_id,
                "catalog_revision": int(
                    catalog_state(connection)["catalog_revision"]
                ),
                "state": "unchanged",
                **existing,
            }
        if action != "defer":
            if external_id:
                _resolved_external_identity(
                    connection,
                    entity_id=entity_id,
                    external_system=external_system,
                    external_id=external_id,
                )
            elif (
                local_search_status == "exact_inventory"
                and action != "accept_external"
            ):
                _resolved_local_identity(
                    connection,
                    entity_id=entity_id,
                )
            else:
                raise CoreCatalogError(
                    "metadata acceptance requires a resolved external or exact-local identity"
                )
        entity = connection.execute(
            "SELECT kind, status FROM entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        target = PROMOTABLE_TEXT_ASSERTIONS[predicate]
        if (
            not entity
            or entity["status"] != "active"
            or entity["kind"] != target[0]
        ):
            raise CoreCatalogError(
                "reconciliation field requires an active compatible entity"
            )
        source_id = ""
        assertion_id = ""
        if action != "defer":
            table, column = target[1], target[2]
            current = connection.execute(
                f'SELECT "{column}" AS value FROM "{table}" WHERE entity_id = ?',
                (entity_id,),
            ).fetchone()
            if action == "keep_catalog" and (
                not current or str(current["value"] or "").strip() != value_text
            ):
                raise CoreCatalogError(
                    "keep-catalog decision must preserve the current catalog value"
                )
            source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO evidence_sources (
                       source_id, source_type, system, external_id, captured_at,
                       snapshot_digest, source_root_id, relative_path,
                       local_absolute_path, citation, note
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
                (
                    source_id,
                    source_type,
                    external_system if action == "accept_external" else "",
                    external_id if action == "accept_external" else "",
                    captured_at,
                    snapshot_digest,
                    source_root_id,
                    relative_path,
                    citation,
                    reason,
                ),
            )
            assertion_id = f"ASSERT-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO evidence_assertions (
                       assertion_id, subject_entity_id, predicate, value_text,
                       object_entity_id, status, confidence, source_id, actor,
                       asserted_at, supersedes_assertion_id, note
                   ) VALUES (?, ?, ?, ?, NULL, 'accepted', 'certain', ?, ?,
                             ?, NULL, ?)""",
                (
                    assertion_id,
                    entity_id,
                    predicate,
                    value_text,
                    source_id,
                    actor,
                    now,
                    reason,
                ),
            )
            connection.execute(
                """UPDATE evidence_assertions
                   SET status = 'superseded'
                   WHERE subject_entity_id = ? AND predicate = ?
                     AND status = 'accepted' AND assertion_id <> ?""",
                (entity_id, predicate, assertion_id),
            )
            connection.execute(
                f'UPDATE "{table}" SET "{column}" = ?, updated_at = ? '
                "WHERE entity_id = ?",
                (value_text, now, entity_id),
            )
        payload = {
            **expected,
            "source_id": source_id,
            "assertion_id": assertion_id,
            "local_search_status": local_search_status,
        }
        _append_log(
            connection,
            actor,
            "accept_reconciliation_field",
            "reconciliation_decision",
            decision_id,
            _entity_unit_id(connection, entity_id),
            payload,
            note=reason,
            evidence=snapshot_digest or citation,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "decision_id": decision_id,
        "catalog_revision": revision,
        "state": "unpublished" if action != "defer" else "reviewed",
        **payload,
    }


def record_file_occurrence(
    connection: sqlite3.Connection,
    *,
    content_digest: str,
    size_bytes: int,
    occurrence_kind: str,
    relative_path: str,
    original_filename: str,
    actor: str,
    unit_id: str | None = None,
    batch_id: str | None = None,
    source_root_id: str = "",
    observed_mtime_utc: str = "",
    observed_at_utc: str | None = None,
    media_type: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Append one immutable provenance observation or reuse its exact ID.

    Re-observing the same scoped path, digest, kind, and observation time is
    idempotent. Changed bytes or a new observation time create a new row; the
    earlier occurrence and all entity-file links to it remain intact.
    """
    _assert_controlled_relative_path(relative_path, "file occurrence")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest):
        raise CoreCatalogError(
            "content digest must use canonical sha256:<lowercase-hex> form"
        )
    if occurrence_kind == "archive":
        if not unit_id:
            raise CoreCatalogError("archive occurrence requires unit_id")
    elif occurrence_kind in ("source", "intake"):
        if not source_root_id:
            raise CoreCatalogError("source/intake occurrence requires source_root_id")
    else:
        raise CoreCatalogError(f"unsupported occurrence kind: {occurrence_kind}")
    now = utc_now()
    observed_at = observed_at_utc or now
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_blob = connection.execute(
            "SELECT size_bytes FROM content_blobs WHERE content_digest = ?",
            (content_digest,),
        ).fetchone()
        if existing_blob and int(existing_blob["size_bytes"]) != int(size_bytes):
            raise CoreCatalogError("content digest already has a different size")
        if not existing_blob:
            connection.execute(
                """INSERT INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, ?, ?, NULL, NULL, NULL, '', '', NULL, NULL, ?)""",
                (content_digest, int(size_bytes), media_type, now),
            )
        if occurrence_kind == "archive":
            existing = connection.execute(
                """SELECT occurrence_id FROM file_occurrences
                   WHERE occurrence_kind = 'archive'
                     AND unit_id = ? AND relative_path = ?
                     AND content_digest = ? AND observed_at_utc = ?""",
                (unit_id, relative_path, content_digest, observed_at),
            ).fetchone()
        else:
            existing = connection.execute(
                """SELECT occurrence_id FROM file_occurrences
                   WHERE occurrence_kind = ? AND source_root_id = ?
                     AND relative_path = ? AND content_digest = ?
                     AND observed_at_utc = ?""",
                (
                    occurrence_kind,
                    source_root_id,
                    relative_path,
                    content_digest,
                    observed_at,
                ),
            ).fetchone()
        if existing:
            connection.rollback()
            return {
                "result": "ok",
                "occurrence_id": existing["occurrence_id"],
                "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
                "state": "unchanged",
            }
        occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
        connection.execute(
            """INSERT INTO file_occurrences (
                   occurrence_id, unit_id, batch_id, content_digest,
                   occurrence_kind, source_root_id, relative_path,
                   original_filename, observed_mtime_utc, observed_at_utc,
                   status, note
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)""",
            (
                occurrence_id,
                unit_id,
                batch_id,
                content_digest,
                occurrence_kind,
                source_root_id,
                relative_path,
                original_filename,
                observed_mtime_utc,
                observed_at,
                note,
            ),
        )
        _append_log(
            connection,
            actor,
            "record_file_occurrence",
            "file_occurrence",
            occurrence_id,
            unit_id,
            {
                "content_digest": content_digest,
                "occurrence_kind": occurrence_kind,
                "relative_path": relative_path,
                "source_root_id": source_root_id,
            },
            note=note,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "occurrence_id": occurrence_id,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def link_entity_file(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    occurrence_id: str,
    role: str,
    actor: str,
) -> dict[str, Any]:
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        observed = connection.execute(
            """SELECT content_digest FROM file_occurrences
               WHERE occurrence_id = ?""",
            (occurrence_id,),
        ).fetchone()
        if not observed:
            raise CoreCatalogError(f"unknown file occurrence: {occurrence_id}")
        connection.execute(
            """INSERT INTO entity_files (
                   entity_id, occurrence_id, role, status, created_at
               ) VALUES (?, ?, ?, 'active', ?)""",
            (entity_id, occurrence_id, role, now),
        )
        _append_log(
            connection,
            actor,
            "link_entity_file",
            "entity_file",
            entity_id,
            None,
            {
                "content_digest": observed["content_digest"],
                "occurrence_id": occurrence_id,
                "role": role,
            },
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "occurrence_id": occurrence_id,
        "content_digest": observed["content_digest"],
        "catalog_revision": revision,
        "state": "unpublished",
    }


def register_access_derivative(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    content_digest: str,
    size_bytes: int,
    relative_path: str,
    original_filename: str,
    observed_mtime_utc: str,
    media_type: str,
    width_px: int,
    height_px: int,
    master_digest: str,
    museum_digital_id: str,
    museum_digital_filename: str,
    museum_digital_url: str,
    actor: str,
) -> dict[str, Any]:
    """Register one exact local derivative and its publication chain."""

    entity_id = normalize_entity_id(entity_id)
    for label, digest in (("derivative", content_digest), ("master", master_digest)):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise CoreCatalogError(f"{label} digest is invalid")
    if media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise CoreCatalogError("unsupported access-derivative media type")
    if int(size_bytes) <= 0 or int(width_px) <= 0 or int(height_px) <= 0:
        raise CoreCatalogError("access-derivative technical values are invalid")
    _assert_controlled_relative_path(relative_path, "access derivative")
    path = PurePosixPath(relative_path)
    if (
        not matches_object_path(
            path,
            unit_id=unit_id,
            entity_id=entity_id,
            role="access_derivative",
        )
        or path.name != original_filename
    ):
        raise CoreCatalogError(
            "access derivative must use the flat unit data/derivatives path"
        )
    now = utc_now()
    provenance = {
        "derivation": "local_master_to_local_derivative_to_museum_digital_publication",
        "master_digest": master_digest,
        "museum_digital_filename": museum_digital_filename.strip(),
        "museum_digital_id": museum_digital_id.strip(),
        "museum_digital_url": museum_digital_url.strip(),
    }
    note = json.dumps(
        provenance,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        entity = connection.execute(
            """SELECT e.kind, e.status, u.status AS unit_status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id=e.entity_id
               JOIN units u ON u.unit_id=m.unit_id
               WHERE e.entity_id=? AND m.unit_id=?""",
            (entity_id, unit_id),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                "access derivative requires an active object in an open unit"
            )
        existing = connection.execute(
            """SELECT fo.occurrence_id, fo.content_digest, fo.relative_path,
                      fo.note
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               WHERE ef.entity_id=? AND ef.role='access_derivative'
                 AND ef.status='active'""",
            (entity_id,),
        ).fetchone()
        if existing:
            if (
                existing["content_digest"] == content_digest
                and existing["relative_path"] == relative_path
                and existing["note"] == note
            ):
                connection.rollback()
                return {
                    "result": "ok",
                    "occurrence_id": existing["occurrence_id"],
                    "content_digest": content_digest,
                    "catalog_revision": int(
                        catalog_state(connection)["catalog_revision"]
                    ),
                    "state": "unchanged",
                }
            raise CoreCatalogError("entity already has another active access derivative")
        blob = connection.execute(
            "SELECT size_bytes, media_type FROM content_blobs WHERE content_digest=?",
            (content_digest,),
        ).fetchone()
        if blob and (
            int(blob["size_bytes"]) != int(size_bytes)
            or str(blob["media_type"]) not in ("", media_type)
        ):
            raise CoreCatalogError("access-derivative blob metadata conflicts")
        if not blob:
            connection.execute(
                """INSERT INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, NULL, '', '', 1, NULL, ?)""",
                (
                    content_digest,
                    int(size_bytes),
                    media_type,
                    int(width_px),
                    int(height_px),
                    now,
                ),
            )
        occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
        connection.execute(
            """INSERT INTO file_occurrences (
                   occurrence_id, unit_id, batch_id, content_digest,
                   occurrence_kind, source_root_id, relative_path,
                   original_filename, observed_mtime_utc, observed_at_utc,
                   status, note
               ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                         'verified', ?)""",
            (
                occurrence_id,
                unit_id,
                content_digest,
                relative_path,
                original_filename,
                observed_mtime_utc,
                now,
                note,
            ),
        )
        connection.execute(
            """INSERT INTO entity_files (
                   entity_id, occurrence_id, role, status, created_at
               ) VALUES (?, ?, 'access_derivative', 'active', ?)""",
            (entity_id, occurrence_id, now),
        )
        _append_log(
            connection,
            actor,
            "register_access_derivative",
            "entity_file",
            entity_id,
            unit_id,
            {
                "content_digest": content_digest,
                "occurrence_id": occurrence_id,
                "provenance": provenance,
                "relative_path": relative_path,
            },
            evidence=content_digest,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "occurrence_id": occurrence_id,
        "content_digest": content_digest,
        "catalog_revision": revision,
        "state": "unpublished",
    }


def record_recovery_accession_provenance(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    selection_digest: str,
    master_digest: str,
    derivative_digest: str,
    museum_digital_id: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Bind the reviewed recovery selection to the pending publication."""

    entity_id = normalize_entity_id(entity_id)
    for value in (selection_digest, master_digest):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise CoreCatalogError("recovery accession digest is invalid")
    if derivative_digest and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", derivative_digest
    ):
        raise CoreCatalogError("recovery derivative digest is invalid")
    payload = {
        "derivative_digest": derivative_digest,
        "master_digest": master_digest,
        "museum_digital_id": museum_digital_id.strip(),
        "selection_digest": selection_digest,
    }
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            """SELECT after_json, note FROM logbook
               WHERE action='prepare_recovery_accession_publication'
                 AND entity_id=? ORDER BY at_utc DESC, log_id DESC LIMIT 1""",
            (entity_id,),
        ).fetchone()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if prior and prior["after_json"] == encoded and prior["note"] == reason.strip():
            connection.rollback()
            return {
                "result": "ok",
                "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
                "state": "unchanged",
            }
        _append_log(
            connection,
            actor,
            "prepare_recovery_accession_publication",
            "archive_object",
            entity_id,
            unit_id,
            payload,
            evidence=selection_digest,
            note=reason.strip(),
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"result": "ok", "catalog_revision": revision, "state": "unpublished"}


def register_preservation_document(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    content_digest: str,
    size_bytes: int,
    relative_path: str,
    original_filename: str,
    observed_mtime_utc: str,
    page_count: int,
    expected_catalog_revision: int,
    representation_role: str,
    technical_profile: dict[str, Any],
    provenance: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Atomically register one exact-byte PDF preservation representation."""

    if representation_role not in {
        "preservation_document",
        "preservation_document_surrogate",
    }:
        raise CoreCatalogError("unsupported document preservation role")
    entity_id = normalize_entity_id(entity_id)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest):
        raise CoreCatalogError(
            "document digest must use canonical sha256:<lowercase-hex> form"
        )
    if int(size_bytes) <= 0 or int(page_count) <= 0:
        raise CoreCatalogError("document technical values are invalid")
    _assert_controlled_relative_path(relative_path, "preservation document")
    path = PurePosixPath(relative_path)
    if (
        not matches_object_path(
            path,
            unit_id=unit_id,
            entity_id=entity_id,
            role=representation_role,
        )
        or path.name != original_filename
    ):
        raise CoreCatalogError(
            "preservation document must use the flat unit data/documents path"
        )
    required_profile = {
        "pdf_version",
        "pages",
        "encrypted",
        "page_size",
        "tagged",
        "form",
        "javascript",
        "text_status",
        "indexing_status",
        "validation_tool",
    }
    if set(technical_profile) != required_profile:
        raise CoreCatalogError("document technical profile is incomplete")
    if int(technical_profile["pages"]) != int(page_count):
        raise CoreCatalogError("document page counts disagree")
    required_provenance = {
        "plan_digest",
        "selected_candidate",
        "source_kind",
        "source_locator",
        "museum_digital_id",
        "museum_digital_resource_id",
        "selection_note",
    }
    if set(provenance) != required_provenance:
        raise CoreCatalogError("document provenance is incomplete")
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = catalog_state(connection)
        if (
            int(state["catalog_revision"]) != int(expected_catalog_revision)
            or int(state["published_revision"]) != int(expected_catalog_revision)
        ):
            raise CoreCatalogError(
                "catalog changed after PDF reconciliation; rebuild the plan "
                "from the current published revision"
            )
        entity = connection.execute(
            """SELECT e.kind, e.status, u.status AS unit_status
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               WHERE e.entity_id = ? AND m.unit_id = ?""",
            (entity_id, unit_id),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                "document accession requires an active archive object in an "
                "open member unit"
            )
        existing = connection.execute(
            """SELECT fo.occurrence_id, fo.content_digest, fo.relative_path
               FROM entity_files ef
               JOIN file_occurrences fo
                 ON fo.occurrence_id = ef.occurrence_id
               WHERE ef.entity_id = ?
                 AND ef.role IN (
                     'preservation_document',
                     'preservation_document_surrogate'
                 )
                 AND ef.status = 'active'""",
            (entity_id,),
        ).fetchone()
        if existing:
            if (
                existing["content_digest"] == content_digest
                and existing["relative_path"] == relative_path
            ):
                connection.rollback()
                return {
                    "result": "ok",
                    "occurrence_id": existing["occurrence_id"],
                    "content_digest": content_digest,
                    "catalog_revision": int(state["catalog_revision"]),
                    "representation_role": representation_role,
                    "state": "unchanged",
                }
            raise CoreCatalogError(
                f"entity already has an active preservation document: {entity_id}"
            )
        blob = connection.execute(
            "SELECT size_bytes, media_type FROM content_blobs WHERE content_digest = ?",
            (content_digest,),
        ).fetchone()
        if blob and (
            int(blob["size_bytes"]) != int(size_bytes)
            or str(blob["media_type"]) not in ("", "application/pdf")
        ):
            raise CoreCatalogError(
                "document digest already has incompatible blob metadata"
            )
        if not blob:
            connection.execute(
                """INSERT INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, ?, 'application/pdf', NULL, NULL, NULL, '',
                             '', ?, NULL, ?)""",
                (content_digest, int(size_bytes), int(page_count), now),
            )
        occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
        note = json.dumps(
            {
                "document_profile": technical_profile,
                "provenance": provenance,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """INSERT INTO file_occurrences (
                   occurrence_id, unit_id, batch_id, content_digest,
                   occurrence_kind, source_root_id, relative_path,
                   original_filename, observed_mtime_utc, observed_at_utc,
                   status, note
               ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                         'verified', ?)""",
            (
                occurrence_id,
                unit_id,
                content_digest,
                relative_path,
                original_filename,
                observed_mtime_utc,
                now,
                note,
            ),
        )
        connection.execute(
            """INSERT INTO entity_files (
                   entity_id, occurrence_id, role, status, created_at
               ) VALUES (?, ?, ?, 'active', ?)""",
            (entity_id, occurrence_id, representation_role, now),
        )
        _append_log(
            connection,
            actor,
            "register_preservation_document",
            "archive_object",
            entity_id,
            unit_id,
            {
                "content_digest": content_digest,
                "occurrence_id": occurrence_id,
                "page_count": int(page_count),
                "relative_path": relative_path,
                "representation_role": representation_role,
                "technical_profile": technical_profile,
            },
            note=str(provenance["selection_note"]),
            evidence=str(provenance["plan_digest"]),
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "occurrence_id": occurrence_id,
        "content_digest": content_digest,
        "catalog_revision": revision,
        "representation_role": representation_role,
        "state": "unpublished",
    }


def register_preservation_master(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    content_digest: str,
    size_bytes: int,
    relative_path: str,
    original_filename: str,
    observed_mtime_utc: str,
    width_px: int,
    height_px: int,
    bit_depth: int,
    color_space: str,
    compression: str,
    page_count: int,
    source_digest: str,
    decoded_pixel_digest: str,
    embedding_profile: str,
    exiftool_version: str,
    imagemagick_version: str,
    expected_catalog_revision: int,
    archival_label: str,
    descriptive_title: str,
    description: str,
    material_technique: str,
    dimensions: str,
    actor: str,
    _representation_role: str = "preservation_master",
    _surrogate_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically register one verified, metadata-enriched TIFF representation."""
    if _representation_role not in {
        "preservation_master",
        "preservation_surrogate",
    }:
        raise CoreCatalogError(
            "unsupported preservation representation role"
        )
    representation_label = (
        "preservation master"
        if _representation_role == "preservation_master"
        else "preservation surrogate"
    )
    surrogate_source = dict(_surrogate_source or {})
    if _representation_role == "preservation_master" and surrogate_source:
        raise CoreCatalogError(
            "preservation master registration cannot attach a surrogate source"
        )
    entity_id = normalize_entity_id(entity_id)
    for label, digest in (
        ("content", content_digest),
        ("source", source_digest),
        ("decoded pixel", decoded_pixel_digest),
    ):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise CoreCatalogError(
                f"{label} digest must use canonical sha256:<lowercase-hex> form"
            )
    _assert_controlled_relative_path(relative_path, representation_label)
    path = PurePosixPath(relative_path)
    if (
        not matches_object_path(
            path,
            unit_id=unit_id,
            entity_id=entity_id,
            role=_representation_role,
        )
        or path.name != original_filename
    ):
        raise CoreCatalogError(
            f"{representation_label} must use the flat unit data/masters path"
        )
    if _representation_role == "preservation_surrogate":
        required_source = {
            "content_digest",
            "size_bytes",
            "relative_path",
            "original_filename",
            "observed_mtime_utc",
            "media_type",
        }
        if set(surrogate_source) != required_source:
            raise CoreCatalogError(
                "preservation surrogate requires one exact selected-source "
                "registration"
            )
        if surrogate_source["content_digest"] != source_digest:
            raise CoreCatalogError(
                "surrogate source copy digest differs from selected source"
            )
        _assert_controlled_relative_path(
            str(surrogate_source["relative_path"]),
            "preservation surrogate source",
        )
        source_path = PurePosixPath(
            str(surrogate_source["relative_path"])
        )
        if (
            not matches_object_path(
                source_path,
                unit_id=unit_id,
                entity_id=entity_id,
                role="access_derivative",
            )
            or source_path.name
            != str(surrogate_source["original_filename"])
            or source_path == path
        ):
            raise CoreCatalogError(
                "preservation surrogate source must be the distinct flat access "
                "derivative"
            )
    if (
        int(size_bytes) <= 0
        or int(width_px) <= 0
        or int(height_px) <= 0
        or int(bit_depth) <= 0
        or int(page_count) != 1
    ):
        raise CoreCatalogError(
            f"{representation_label} technical values are invalid"
        )
    if not embedding_profile.strip() or not exiftool_version.strip():
        raise CoreCatalogError(
            f"{representation_label} requires embedding profile and "
            "ExifTool version"
        )
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = catalog_state(connection)
        if (
            int(state["catalog_revision"]) != int(expected_catalog_revision)
            or int(state["published_revision"]) != int(expected_catalog_revision)
        ):
            raise CoreCatalogError(
                "catalog changed after metadata embedding began; discard the "
                "candidate and restart from current published metadata"
            )
        entity = connection.execute(
            """SELECT e.kind, e.status, u.status AS unit_status,
                      a.archival_label, a.descriptive_title, a.description,
                      a.material_technique, a.dimensions
               FROM entities e
               JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               JOIN archive_objects a ON a.entity_id = e.entity_id
               WHERE e.entity_id = ? AND m.unit_id = ?""",
            (entity_id, unit_id),
        ).fetchone()
        if (
            not entity
            or entity["kind"] != "archive_object"
            or entity["status"] != "active"
            or entity["unit_status"] != "open"
        ):
            raise CoreCatalogError(
                f"{representation_label} requires an active archive object "
                "in an open member unit"
            )
        expected_values = {
            "archival_label": archival_label,
            "descriptive_title": descriptive_title,
            "description": description,
            "material_technique": material_technique,
            "dimensions": dimensions,
        }
        if any(
            str(entity[key]) != value
            for key, value in expected_values.items()
        ):
            raise CoreCatalogError(
                "catalog metadata changed after TIFF embedding began"
            )
        existing = connection.execute(
            """SELECT fo.occurrence_id, fo.content_digest, fo.relative_path
               FROM entity_files ef
               JOIN file_occurrences fo
                 ON fo.occurrence_id = ef.occurrence_id
               WHERE ef.entity_id = ?
                 AND ef.role = ?
                 AND ef.status = 'active'""",
            (entity_id, _representation_role),
        ).fetchone()
        if existing:
            if (
                existing["content_digest"] == content_digest
                and existing["relative_path"] == relative_path
            ):
                connection.rollback()
                return {
                    "result": "ok",
                    "occurrence_id": existing["occurrence_id"],
                    "content_digest": content_digest,
                    "catalog_revision": int(
                        catalog_state(connection)["catalog_revision"]
                    ),
                    "state": "unchanged",
                }
            raise CoreCatalogError(
                f"entity already has an active {representation_label}: "
                f"{entity_id}"
            )
        blob = connection.execute(
            "SELECT size_bytes FROM content_blobs WHERE content_digest = ?",
            (content_digest,),
        ).fetchone()
        if blob and int(blob["size_bytes"]) != int(size_bytes):
            raise CoreCatalogError("content digest already has a different size")
        if not blob:
            connection.execute(
                """INSERT INTO content_blobs (
                       content_digest, size_bytes, media_type, width_px,
                       height_px, bit_depth, color_space, compression,
                       page_count, duration_ms, created_at
                   ) VALUES (?, ?, 'image/tiff', ?, ?, ?, ?, ?, 1, NULL, ?)""",
                (
                    content_digest,
                    int(size_bytes),
                    int(width_px),
                    int(height_px),
                    int(bit_depth),
                    color_space,
                    compression,
                    now,
                ),
            )
        occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
        connection.execute(
            """INSERT INTO file_occurrences (
                   occurrence_id, unit_id, batch_id, content_digest,
                   occurrence_kind, source_root_id, relative_path,
                   original_filename, observed_mtime_utc, observed_at_utc,
                   status, note
               ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                         'verified', ?)""",
            (
                occurrence_id,
                unit_id,
                content_digest,
                relative_path,
                original_filename,
                observed_mtime_utc,
                now,
                (
                    f"metadata-enriched {representation_label}; "
                    f"profile {embedding_profile}; source {source_digest}; "
                    f"decoded pixels {decoded_pixel_digest}"
                ),
            ),
        )
        connection.execute(
            """INSERT INTO entity_files (
                   entity_id, occurrence_id, role, status, created_at
               ) VALUES (?, ?, ?, 'active', ?)""",
            (entity_id, occurrence_id, _representation_role, now),
        )
        surrogate_source_occurrence_id = ""
        if surrogate_source:
            source_blob = connection.execute(
                "SELECT size_bytes FROM content_blobs "
                "WHERE content_digest = ?",
                (surrogate_source["content_digest"],),
            ).fetchone()
            if source_blob and int(source_blob["size_bytes"]) != int(
                surrogate_source["size_bytes"]
            ):
                raise CoreCatalogError(
                    "surrogate source digest already has a different size"
                )
            if not source_blob:
                connection.execute(
                    """INSERT INTO content_blobs (
                           content_digest, size_bytes, media_type, width_px,
                           height_px, bit_depth, color_space, compression,
                           page_count, duration_ms, created_at
                       ) VALUES (?, ?, ?, NULL, NULL, NULL, '', '', 1,
                                 NULL, ?)""",
                    (
                        surrogate_source["content_digest"],
                        int(surrogate_source["size_bytes"]),
                        surrogate_source["media_type"],
                        now,
                    ),
                )
            surrogate_source_occurrence_id = (
                f"OCC-{uuid.uuid4().hex.upper()}"
            )
            connection.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc, observed_at_utc,
                       status, note
                   ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                             'verified', ?)""",
                (
                    surrogate_source_occurrence_id,
                    unit_id,
                    surrogate_source["content_digest"],
                    surrogate_source["relative_path"],
                    surrogate_source["original_filename"],
                    surrogate_source["observed_mtime_utc"],
                    now,
                    "exact selected JPEG retained as the sole access derivative "
                    "and source of the preservation surrogate",
                ),
            )
            connection.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, ?, 'access_derivative',
                             'active', ?)""",
                (entity_id, surrogate_source_occurrence_id, now),
            )
        payload = {
            "content_digest": content_digest,
            "decoded_pixel_digest": decoded_pixel_digest,
            "embedding_profile": embedding_profile,
            "exiftool_version": exiftool_version,
            "height_px": int(height_px),
            "imagemagick_version": imagemagick_version,
            "occurrence_id": occurrence_id,
            "relative_path": relative_path,
            "source_digest": source_digest,
            "width_px": int(width_px),
        }
        if surrogate_source_occurrence_id:
            payload["surrogate_source_occurrence_id"] = (
                surrogate_source_occurrence_id
            )
        _append_log(
            connection,
            actor,
            (
                "register_preservation_master"
                if _representation_role == "preservation_master"
                else "register_preservation_surrogate"
            ),
            "entity_file",
            entity_id,
            unit_id,
            payload,
            evidence=content_digest,
            at_utc=now,
        )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "occurrence_id": occurrence_id,
        "content_digest": content_digest,
        "catalog_revision": revision,
        "representation_role": _representation_role,
        "surrogate_source_occurrence_id": (
            surrogate_source_occurrence_id
        ),
        "state": "unpublished",
    }


def relayout_active_representations(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    changes: list[dict[str, str]],
    actor: str,
) -> dict[str, Any]:
    """Supersede active links after verified non-destructive flat copies."""

    unit_id = unit_id.strip()
    actor = actor.strip()
    if not unit_id or not actor:
        raise CoreCatalogError("relayout requires a unit and named actor")
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    normalized: list[dict[str, str]] = []
    for item in changes:
        entity_id = normalize_entity_id(str(item.get("entity_id") or ""))
        role = str(item.get("role") or "")
        old_occurrence_id = str(item.get("old_occurrence_id") or "")
        new_relative_path = str(item.get("new_relative_path") or "")
        new_original_filename = str(item.get("new_original_filename") or "")
        content_digest = str(item.get("content_digest") or "").casefold()
        observed_mtime_utc = str(item.get("observed_mtime_utc") or "")
        if role not in OBJECT_ROLE_TOKENS:
            raise CoreCatalogError(f"unsupported relayout role: {role}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest):
            raise CoreCatalogError("relayout content digest is invalid")
        path = PurePosixPath(new_relative_path)
        _assert_controlled_relative_path(new_relative_path, "flat representation")
        if (
            not matches_object_path(
                path,
                unit_id=unit_id,
                entity_id=entity_id,
                role=role,
            )
            or path.name != new_original_filename
        ):
            raise CoreCatalogError("relayout destination violates role-flat layout")
        if old_occurrence_id in seen_old or new_relative_path in seen_new:
            raise CoreCatalogError("relayout contains duplicate occurrence paths")
        seen_old.add(old_occurrence_id)
        seen_new.add(new_relative_path)
        normalized.append(
            {
                "entity_id": entity_id,
                "role": role,
                "old_occurrence_id": old_occurrence_id,
                "new_relative_path": new_relative_path,
                "new_original_filename": new_original_filename,
                "content_digest": content_digest,
                "observed_mtime_utc": observed_mtime_utc,
            }
        )
    if not normalized:
        return {
            "result": "ok",
            "state": "unchanged",
            "representations": 0,
            "catalog_revision": int(catalog_state(connection)["catalog_revision"]),
        }
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        unit = connection.execute(
            "SELECT status FROM units WHERE unit_id=?",
            (unit_id,),
        ).fetchone()
        if not unit or unit["status"] != "open":
            raise CoreCatalogError("relayout requires an open unit")
        for item in normalized:
            old = connection.execute(
                """SELECT fo.content_digest, fo.relative_path, fo.status,
                          ef.status AS link_status
                   FROM file_occurrences fo
                   JOIN entity_files ef ON ef.occurrence_id=fo.occurrence_id
                   WHERE fo.occurrence_id=? AND fo.unit_id=?
                     AND ef.entity_id=? AND ef.role=?""",
                (
                    item["old_occurrence_id"],
                    unit_id,
                    item["entity_id"],
                    item["role"],
                ),
            ).fetchone()
            if (
                not old
                or old["status"] != "verified"
                or old["link_status"] != "active"
                or old["content_digest"] != item["content_digest"]
            ):
                raise CoreCatalogError("relayout source is not the active verified link")
            collision = connection.execute(
                """SELECT content_digest FROM file_occurrences
                   WHERE unit_id=? AND relative_path=? AND status='verified'""",
                (unit_id, item["new_relative_path"]),
            ).fetchone()
            if collision:
                raise CoreCatalogError("relayout destination is already cataloged")
            new_occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc, observed_at_utc,
                       status, note
                   ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                             'verified', ?)""",
                (
                    new_occurrence_id,
                    unit_id,
                    item["content_digest"],
                    item["new_relative_path"],
                    item["new_original_filename"],
                    item["observed_mtime_utc"],
                    now,
                    "verified non-destructive flat-layout copy; old bytes retained",
                ),
            )
            connection.execute(
                """UPDATE entity_files SET status='superseded'
                   WHERE entity_id=? AND occurrence_id=? AND role=?
                     AND status='active'""",
                (
                    item["entity_id"],
                    item["old_occurrence_id"],
                    item["role"],
                ),
            )
            connection.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, ?, ?, 'active', ?)""",
                (item["entity_id"], new_occurrence_id, item["role"], now),
            )
            _append_log(
                connection,
                actor,
                "relayout_representation",
                "entity_file",
                item["entity_id"],
                unit_id,
                {
                    "content_digest": item["content_digest"],
                    "new_occurrence_id": new_occurrence_id,
                    "new_relative_path": item["new_relative_path"],
                    "old_bytes_retained": True,
                    "old_occurrence_id": item["old_occurrence_id"],
                    "role": item["role"],
                },
                before={
                    "old_relative_path": str(old["relative_path"]),
                    "role": item["role"],
                },
                evidence=item["content_digest"],
                note="Filesystem-browsable flat unit layout; no old file deleted.",
                at_utc=now,
            )
        revision = int(catalog_state(connection)["catalog_revision"])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "result": "ok",
        "state": "unpublished",
        "representations": len(normalized),
        "catalog_revision": revision,
    }


def register_preservation_surrogate(
    connection: sqlite3.Connection,
    **arguments: Any,
) -> dict[str, Any]:
    """Register a TIFF representation without claiming an original master."""

    if (
        "_representation_role" in arguments
        or "_surrogate_source" in arguments
    ):
        raise CoreCatalogError(
            "surrogate registration role is fixed by the guarded operation"
        )
    if "surrogate_source" not in arguments:
        raise CoreCatalogError(
            "surrogate registration requires exact selected-source details"
        )
    return register_preservation_master(
        connection,
        _representation_role="preservation_surrogate",
        _surrogate_source=arguments.pop("surrogate_source"),
        **arguments,
    )


def _assert_controlled_relative_path(value: str, label: str) -> None:
    if not value:
        return
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise CoreCatalogError(f"{label} is not a controlled relative path: {value}")


def assert_exportable_catalog(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        "SELECT source_id, relative_path FROM evidence_sources"
    ):
        _assert_controlled_relative_path(
            row["relative_path"],
            f"evidence source {row['source_id']}",
        )
    for row in connection.execute(
        "SELECT occurrence_id, relative_path FROM file_occurrences"
    ):
        _assert_controlled_relative_path(
            row["relative_path"],
            f"file occurrence {row['occurrence_id']}",
        )


def _fingerprint(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def assert_independent_device(root: Path, preservation_root: Path) -> None:
    if root.stat().st_dev == preservation_root.stat().st_dev:
        raise CoreCatalogError(
            "independent generation copy must be on a different filesystem device"
        )


def verify_preservation_masters(
    connection: sqlite3.Connection,
    root: Path,
    preservation_root: Path,
) -> dict[str, Any]:
    """Deep-fixity sweep for every active preservation representation pair."""
    root = assert_safe_work_root(root)
    preservation_root = assert_safe_work_root(preservation_root)
    rows = connection.execute(
        """SELECT ef.entity_id, ef.role, fo.occurrence_id, fo.relative_path,
                  fo.content_digest
           FROM entity_files ef
           JOIN file_occurrences fo ON fo.occurrence_id = ef.occurrence_id
           WHERE ef.role IN (
               'preservation_master',
               'preservation_surrogate',
               'preservation_surrogate_source',
               'access_derivative',
               'preservation_document',
               'preservation_document_surrogate'
           )
             AND ef.status = 'active'
             AND fo.status = 'verified'
           ORDER BY ef.entity_id, fo.occurrence_id"""
    ).fetchall()
    primary_permission_read_only = 0
    independent_permission_read_only = 0
    ntfs_read_only_attributes = 0
    role_counts = {
        "preservation_master": 0,
        "preservation_surrogate": 0,
        "preservation_surrogate_source": 0,
        "access_derivative": 0,
        "preservation_document": 0,
        "preservation_document_surrogate": 0,
    }
    for row in rows:
        role_counts[str(row["role"])] += 1
        relative = str(row["relative_path"])
        _assert_controlled_relative_path(
            relative,
            f"preservation master {row['occurrence_id']}",
        )
        expected = str(row["content_digest"]).removeprefix("sha256:")
        primary = root / relative
        independent = preservation_root / relative
        for label, path in (
            ("primary", primary),
            ("independent", independent),
        ):
            if not path.is_file():
                raise CoreCatalogError(
                    f"{label} preservation master is missing: {path}"
                )
            if sha256_file(path) != expected:
                raise CoreCatalogError(
                    f"{label} preservation master fixity failed: {path}"
                )
        if primary.stat().st_mode & 0o222 == 0:
            primary_permission_read_only += 1
        else:
            try:
                attributes = int.from_bytes(
                    os.getxattr(primary, "system.ntfs_attrib"),
                    "little",
                )
            except OSError:
                attributes = 0
            if attributes & 0x1:
                ntfs_read_only_attributes += 1
            else:
                raise CoreCatalogError(
                    "primary preservation master lacks a read-only marker: "
                    f"{primary}"
                )
        if independent.stat().st_mode & 0o222 == 0:
            independent_permission_read_only += 1
        else:
            raise CoreCatalogError(
                "independent preservation master is not permission-read-only: "
                f"{independent}"
            )
    return {
        "representations": len(rows),
        "masters": role_counts["preservation_master"],
        "surrogates": role_counts["preservation_surrogate"],
        "surrogate_sources": role_counts[
            "preservation_surrogate_source"
        ],
        "derivatives": role_counts["access_derivative"],
        "documents": role_counts["preservation_document"],
        "document_surrogates": role_counts[
            "preservation_document_surrogate"
        ],
        "primary_permission_read_only": primary_permission_read_only,
        "primary_ntfs_read_only_attribute": ntfs_read_only_attributes,
        "independent_permission_read_only": independent_permission_read_only,
        "fixity": "ok",
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def initialize_root(
    root: Path,
    tool_version: str,
    actor: str,
    retained_index: Path | None = None,
) -> dict[str, Any]:
    root = assert_safe_work_root(root)
    if root.exists():
        raise CoreCatalogError(f"refusing to replace existing root: {root}")
    temporary = root.with_name(f".{root.name}.init-{uuid.uuid4().hex}")
    try:
        (temporary / "work").mkdir(parents=True)
        (temporary / "registry" / "generations").mkdir(parents=True)
        readme = """# ISS Minimal Archive Catalog

The preservation filesystem is authoritative. `work/catalog.sqlite` is a
rebuildable editing and validation surface. Complete open generations live
under `registry/generations/`; `registry/CURRENT` names the accepted generation.

The historical pilot is not migrated. Protected archive, intake, and recovery
USB sources are read/copy-only. The configured retained search index is opened
read-only and is never part of a generation.

Inside each unit, accepted representations use flat `data/masters/`,
`data/derivatives/`, and `data/documents/` directories. Human-readable
inventory labels, immutable entity IDs, and roles are carried in filenames so
filesystem recovery never requires SQLite or per-object directory traversal.
"""
        (temporary / "00_README.md").write_text(
            readme,
            encoding="utf-8",
            newline="\n",
        )
        catalog_path = temporary / "work" / "catalog.sqlite"
        connection = create_catalog(catalog_path, tool_version, actor)
        connection.close()
        config: dict[str, Any] = {
            "config_version": 3,
            "catalog_mode": CATALOG_MODE,
            "object_layout": OBJECT_LAYOUT_VERSION,
            "independent_generation_root": "",
            "retained_search_index": None,
        }
        if retained_index is not None:
            retained_index = retained_index.expanduser().resolve()
            if not retained_index.is_file():
                raise CoreCatalogError(
                    f"retained search index does not exist: {retained_index}"
                )
            config["retained_search_index"] = _fingerprint(retained_index)
        (temporary / "work" / "catalog-config.json").write_text(
            json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "result": "ok",
        "root": str(root),
        "catalog": str(root / "work" / "catalog.sqlite"),
        "retained_search_index": (
            str(retained_index) if retained_index is not None else ""
        ),
        "schema_version": SCHEMA_VERSION,
        "pilot_rows_migrated": 0,
    }


def load_config(root: Path) -> dict[str, Any]:
    config_path = assert_safe_work_root(root) / "work" / "catalog-config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoreCatalogError(f"cannot read catalog config: {config_path}") from error


def save_config(root: Path, config: dict[str, Any]) -> None:
    config_path = assert_safe_work_root(root) / "work" / "catalog-config.json"
    _atomic_write_text(
        config_path,
        json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def require_core_mode(root: Path) -> dict[str, Any]:
    config = load_config(root)
    if config.get("catalog_mode") != CATALOG_MODE:
        raise CoreCatalogError(
            f"catalog root mode must be {CATALOG_MODE!r}; "
            f"found {config.get('catalog_mode')!r}"
        )
    return config


def retained_index_path(root: Path) -> Path:
    configured = require_core_mode(root).get("retained_search_index")
    if not isinstance(configured, dict) or not configured.get("path"):
        raise CoreCatalogError("no retained search index is configured")
    path = Path(str(configured["path"])).resolve()
    if not path.is_file():
        raise CoreCatalogError(f"retained search index is unavailable: {path}")
    current = _fingerprint(path)
    expected = {
        key: configured[key]
        for key in ("path", "device", "inode", "size", "mtime_ns")
    }
    if current != expected:
        raise CoreCatalogError(
            "retained search index fingerprint changed; refusing to open it"
        )
    return path


def connect_retained_index(root: Path) -> sqlite3.Connection:
    path = retained_index_path(root)
    uri_path = urllib.parse.quote(path.as_posix(), safe="/")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _encode_csv_value(value: Any) -> str:
    if value is None:
        return NULL_TOKEN
    text = str(value)
    return f"\\{text}" if text.startswith("\\") else text


def _decode_csv_value(value: str) -> str | None:
    if value == NULL_TOKEN:
        return None
    return value[1:] if value.startswith("\\\\") else value


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
    excluded: Iterable[str] = (),
) -> list[str]:
    excluded_set = set(excluded)
    return [
        row["name"]
        for row in connection.execute(f'PRAGMA table_info("{table}")')
        if row["name"] not in excluded_set
    ]


def _write_table_csv(
    connection: sqlite3.Connection,
    table: str,
    destination: Path,
    excluded: Iterable[str],
    generation_id: str,
    previous_generation_id: str | None,
    target_revision: int,
) -> None:
    columns = _table_columns(connection, table, excluded)
    order = ", ".join(f'"{column}"' for column in columns)
    rows = [dict(row) for row in connection.execute(
        f'SELECT {order} FROM "{table}" ORDER BY {order}'
    )]
    if table == "filesystem_generations":
        for row in rows:
            if row["generation_id"] == generation_id:
                row["status"] = "current"
                row["manifest_digest"] = ""
            elif (
                previous_generation_id
                and row["generation_id"] == previous_generation_id
            ):
                row["status"] = "superseded"
    elif table == "catalog_state":
        rows = [
            {
                "singleton": 1,
                "catalog_revision": target_revision,
                "published_revision": target_revision,
                "dirty_since": None,
            }
        ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: _encode_csv_value(row[column]) for column in columns}
            )


def _write_logbook_jsonl(
    connection: sqlite3.Connection,
    destination: Path,
) -> None:
    columns = _table_columns(connection, "logbook")
    order = ", ".join(f'"{column}"' for column in columns)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for row in connection.execute(
            f'SELECT {order} FROM logbook ORDER BY at_utc, log_id'
        ):
            handle.write(
                json.dumps(
                    dict(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def _manifest_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "registry-manifest-sha256.txt"
    )


def write_manifest(root: Path) -> Path:
    manifest = root / "registry-manifest-sha256.txt"
    lines = [
        f"{sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in _manifest_files(root)
    ]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return manifest


def verify_manifest(root: Path) -> str:
    manifest = root / "registry-manifest-sha256.txt"
    if not manifest.is_file():
        raise CoreCatalogError(f"generation manifest is missing: {manifest}")
    seen: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            expected, relative_text = line.split("  ", 1)
        except ValueError as error:
            raise CoreCatalogError("invalid generation manifest line") from error
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
            or relative_text in seen
        ):
            raise CoreCatalogError(f"unsafe generation manifest path: {relative_text}")
        seen.add(relative_text)
        path = root.joinpath(*relative.parts)
        root_resolved = root.resolve()
        path_resolved = path.resolve()
        if (
            path.is_symlink()
            or root_resolved not in path_resolved.parents
            or not path.is_file()
            or sha256_file(path) != expected
        ):
            raise CoreCatalogError(f"generation manifest mismatch: {relative_text}")
    actual = {
        path.relative_to(root).as_posix()
        for path in _manifest_files(root)
    }
    if seen != actual:
        raise CoreCatalogError("generation manifest coverage mismatch")
    return sha256_file(manifest)


def read_generation_header(generation: Path) -> dict[str, str]:
    with (generation / "generation.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise CoreCatalogError("generation.csv must contain exactly one row")
    required = {
        "generation_id",
        "previous_generation_id",
        "schema_version",
        "tool_version",
        "catalog_revision",
        "status",
        "created_at",
    }
    if set(rows[0]) != required:
        raise CoreCatalogError("generation.csv columns do not match the core schema")
    return rows[0]


def migrate_empty_catalog(
    root: Path,
    preservation_root: Path,
    *,
    tool_version: str,
    actor: str,
) -> dict[str, Any]:
    root = assert_safe_work_root(root)
    preservation_root = assert_safe_work_root(preservation_root)
    catalog_path = root / "work" / "catalog.sqlite"
    if not catalog_path.is_file():
        raise CoreCatalogError(f"catalog does not exist: {catalog_path}")
    current_id = (root / "registry" / "CURRENT").read_text(
        encoding="utf-8"
    ).strip()
    primary_generation = root / "registry" / "generations" / current_id
    copy_current = preservation_root / "registry" / "CURRENT"
    if (
        not copy_current.is_file()
        or copy_current.read_text(encoding="utf-8").strip() != current_id
    ):
        raise CoreCatalogError("schema migration requires matching independent CURRENT")
    primary_digest = verify_manifest(primary_generation)
    copy_generation = preservation_root / "registry" / "generations" / current_id
    if verify_manifest(copy_generation) != primary_digest:
        raise CoreCatalogError("schema migration requires a verified independent copy")

    old = sqlite3.connect(catalog_path)
    old.row_factory = sqlite3.Row
    old.execute("PRAGMA foreign_keys = ON")
    old_schema = int(
        old.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    )
    if old_schema >= SCHEMA_VERSION:
        old.close()
        raise CoreCatalogError(
            f"catalog is already at schema version {old_schema}"
        )
    has_catalog_state = bool(
        old.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'catalog_state'"""
        ).fetchone()
    )
    if has_catalog_state:
        old_state = dict(
            old.execute(
                """SELECT catalog_revision, published_revision, dirty_since
                   FROM catalog_state WHERE singleton = 1"""
            ).fetchone()
        )
    else:
        old_state = {
            "catalog_revision": 0,
            "published_revision": 0,
            "dirty_since": None,
        }
    data_tables = (
        "units",
        "entities",
        "entity_names",
        "entity_identifiers",
        "entity_redirects",
        "entity_unit_memberships",
        "entity_relations",
        "evidence_sources",
        "evidence_assertions",
        "archive_objects",
        "historical_events",
        "physical_carriers",
        "source_batches",
        "content_blobs",
        "file_occurrences",
        "entity_files",
    )
    nonempty = {
        table: old.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        for table in data_tables
        if old.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    }
    if nonempty:
        old.close()
        raise CoreCatalogError(
            f"automatic schema migration is limited to the empty core catalog: {nonempty}"
        )
    migrations = [dict(row) for row in old.execute(
        "SELECT * FROM schema_migrations ORDER BY version"
    )]
    log_rows = [dict(row) for row in old.execute(
        "SELECT * FROM logbook ORDER BY at_utc, log_id"
    )]
    generation_rows = [dict(row) for row in old.execute(
        "SELECT * FROM filesystem_generations ORDER BY created_at, generation_id"
    )]
    backups = root / "work" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups / f"{stamp}-PRE-CORE-SCHEMA-{SCHEMA_VERSION}.sqlite"
    backup_connection = sqlite3.connect(backup_path)
    try:
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        old.backup(backup_connection)
        backup_connection.commit()
    finally:
        backup_connection.close()
    backup_check = sqlite3.connect(backup_path)
    try:
        if backup_check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            old.close()
            raise CoreCatalogError(
                "pre-migration catalog backup failed integrity check"
            )
    finally:
        backup_check.close()
    old.close()

    temporary = catalog_path.with_name(
        f".catalog-schema-{SCHEMA_VERSION}-{uuid.uuid4().hex}.sqlite"
    )
    new = sqlite3.connect(temporary)
    new.row_factory = sqlite3.Row
    now = utc_now()
    try:
        new.execute("PRAGMA foreign_keys = ON")
        new.execute("PRAGMA journal_mode = DELETE")
        new.execute("PRAGMA synchronous = FULL")
        new.executescript(SCHEMA_SQL)
        for row in migrations:
            new.execute(
                """INSERT INTO schema_migrations (
                       version, applied_at, tool_version, migration_checksum
                   ) VALUES (?, ?, ?, ?)""",
                (
                    row["version"],
                    row["applied_at"],
                    row["tool_version"],
                    row["migration_checksum"],
                ),
            )
        new.execute(
            """INSERT INTO schema_migrations (
                   version, applied_at, tool_version, migration_checksum
               ) VALUES (?, ?, ?, ?)""",
            (SCHEMA_VERSION, now, tool_version, schema_checksum()),
        )
        new.executemany(
            "INSERT INTO entity_kinds (kind, label, status) VALUES (?, ?, ?)",
            INITIAL_ENTITY_KINDS,
        )
        new.executemany(
            """INSERT INTO relation_predicates (
                   predicate, label, inverse_predicate, status
               ) VALUES (?, ?, ?, ?)""",
            INITIAL_RELATION_PREDICATES,
        )
        new.executemany(
            """INSERT INTO assertion_predicates (
                   predicate, label, value_kind, cardinality, status
               ) VALUES (?, ?, ?, ?, ?)""",
            INITIAL_ASSERTION_PREDICATES,
        )
        new_revision = max(
            int(old_state["catalog_revision"]),
            int(old_state["published_revision"]),
        ) + 1
        published_revision = int(old_state["published_revision"])
        new.execute(
            """INSERT INTO catalog_state (
                   singleton, catalog_revision, published_revision, dirty_since
               ) VALUES (1, ?, ?, ?)""",
            (new_revision, published_revision, now),
        )
        for row in log_rows:
            columns = list(row)
            quoted = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            new.execute(
                f"INSERT INTO logbook ({quoted}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
        _append_log(
            new,
            actor,
            "migrate_empty_catalog_schema",
            "catalog",
            "minimal-core",
            None,
            {
                "from_schema": old_schema,
                "to_schema": SCHEMA_VERSION,
                "pilot_rows_migrated": 0,
            },
            note=(
                "schema-triggered freshness, occurrence-level entity files, "
                "and dedicated core source registry"
            ),
            at_utc=now,
        )
        for row in generation_rows:
            generation_revision = int(row.get("catalog_revision") or 0)
            new.execute(
                """INSERT INTO filesystem_generations (
                       generation_id, previous_generation_id, schema_version,
                       tool_version, catalog_revision, status, manifest_digest,
                       created_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["generation_id"],
                    row["previous_generation_id"],
                    row["schema_version"],
                    row["tool_version"],
                    generation_revision,
                    row["status"],
                    row["manifest_digest"],
                    row["created_at"],
                    row["completed_at"],
                ),
            )
        new.commit()
        assert_catalog_integrity(new)
    except Exception:
        new.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        new.close()

    os.replace(temporary, catalog_path)
    catalog_path.with_name(catalog_path.name + "-wal").unlink(missing_ok=True)
    catalog_path.with_name(catalog_path.name + "-shm").unlink(missing_ok=True)
    config = load_config(root)
    config.update(
        {
            "config_version": 2,
            "catalog_mode": CATALOG_MODE,
            "independent_generation_root": str(preservation_root),
            "migration_from_generation": current_id,
        }
    )
    save_config(root, config)
    connection = connect_catalog(catalog_path, read_only=True)
    try:
        assert_catalog_integrity(connection)
    finally:
        connection.close()
    return {
        "result": "ok",
        "schema_version": SCHEMA_VERSION,
        "catalog_revision": new_revision,
        "published_revision": published_revision,
        "filesystem_freshness": "unpublished",
        "backup": str(backup_path),
        "pilot_rows_migrated": 0,
    }


def migrate_empty_catalog_v2(
    root: Path,
    preservation_root: Path,
    *,
    tool_version: str,
    actor: str,
) -> dict[str, Any]:
    """Compatibility alias for the original empty-core migration entrypoint."""
    return migrate_empty_catalog(
        root,
        preservation_root,
        tool_version=tool_version,
        actor=actor,
    )


def verified_current_generation(root: Path) -> dict[str, Any] | None:
    root = assert_safe_work_root(root)
    current_path = root / "registry" / "CURRENT"
    if not current_path.is_file():
        return None
    generation_id = current_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"GEN-[0-9TZ]+-[0-9A-F]{8}", generation_id):
        raise CoreCatalogError("registry/CURRENT contains an invalid generation ID")
    generation = root / "registry" / "generations" / generation_id
    manifest_digest = verify_manifest(generation)
    header = read_generation_header(generation)
    if (
        header["generation_id"] != generation_id
        or header["status"] != "current"
        or header["schema_version"] != str(SCHEMA_VERSION)
    ):
        raise CoreCatalogError("CURRENT generation metadata is inconsistent")
    config = require_core_mode(root)
    copy_root_text = str(config.get("independent_generation_root") or "")
    if not copy_root_text:
        raise CoreCatalogError("independent generation root is not configured")
    copy_root = Path(copy_root_text).expanduser().resolve()
    copy_current = copy_root / "registry" / "CURRENT"
    if (
        not copy_current.is_file()
        or copy_current.read_text(encoding="utf-8").strip() != generation_id
    ):
        raise CoreCatalogError("independent CURRENT pointer does not match")
    copy_generation = copy_root / "registry" / "generations" / generation_id
    if verify_manifest(copy_generation) != manifest_digest:
        raise CoreCatalogError("independent current generation differs")
    return {
        "generation_id": generation_id,
        "generation": generation,
        "manifest_digest": manifest_digest,
        "header": header,
        "independent_generation": copy_generation,
    }


def reconcile_current_generation(
    connection: sqlite3.Connection,
    root: Path,
) -> dict[str, Any]:
    verified = verified_current_generation(root)
    if verified is None:
        return {"healed": False, "current_generation": ""}
    header = verified["header"]
    generation_id = verified["generation_id"]
    target_revision = int(header["catalog_revision"])
    state = catalog_state(connection)
    catalog_revision = int(state["catalog_revision"])
    if catalog_revision < target_revision:
        raise CoreCatalogError(
            "working SQLite is older than filesystem CURRENT; rebuild is required"
        )
    row = connection.execute(
        """SELECT status, manifest_digest, catalog_revision
           FROM filesystem_generations WHERE generation_id = ?""",
        (generation_id,),
    ).fetchone()
    expected_state = (
        row
        and row["status"] == "current"
        and row["manifest_digest"] == verified["manifest_digest"]
        and int(row["catalog_revision"]) == target_revision
        and int(state["published_revision"]) == target_revision
        and (
            (catalog_revision == target_revision and state["dirty_since"] is None)
            or (catalog_revision > target_revision and state["dirty_since"] is not None)
        )
    )
    if expected_state:
        return {"healed": False, "current_generation": generation_id}
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE filesystem_generations
               SET status = 'superseded'
               WHERE status = 'current' AND generation_id <> ?""",
            (generation_id,),
        )
        previous = _decode_csv_value(header["previous_generation_id"])
        connection.execute(
            """INSERT INTO filesystem_generations (
                   generation_id, previous_generation_id, schema_version,
                   tool_version, catalog_revision, status, manifest_digest,
                   created_at, completed_at
               ) VALUES (?, ?, ?, ?, ?, 'current', ?, ?, ?)
               ON CONFLICT(generation_id) DO UPDATE SET
                   previous_generation_id = excluded.previous_generation_id,
                   schema_version = excluded.schema_version,
                   tool_version = excluded.tool_version,
                   catalog_revision = excluded.catalog_revision,
                   status = 'current',
                   manifest_digest = excluded.manifest_digest,
                   completed_at = excluded.completed_at""",
            (
                generation_id,
                previous,
                header["schema_version"],
                header["tool_version"],
                target_revision,
                verified["manifest_digest"],
                header["created_at"],
                now,
            ),
        )
        connection.execute(
            """UPDATE catalog_state
               SET published_revision = ?,
                   dirty_since = CASE
                       WHEN catalog_revision = ? THEN NULL
                       ELSE COALESCE(dirty_since, ?)
                   END
               WHERE singleton = 1""",
            (target_revision, target_revision, now),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"healed": True, "current_generation": generation_id}


def _generation_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"GEN-{stamp}-{secrets.token_hex(4).upper()}"


def _export_generation(
    connection: sqlite3.Connection,
    destination: Path,
    generation_id: str,
    previous_generation_id: str | None,
    tool_version: str,
) -> str:
    generation_row = connection.execute(
        """SELECT generation_id, previous_generation_id, schema_version,
                  tool_version, catalog_revision, created_at
           FROM filesystem_generations
           WHERE generation_id = ?""",
        (generation_id,),
    ).fetchone()
    if not generation_row:
        raise CoreCatalogError("pending filesystem generation is missing")
    target_revision = int(generation_row["catalog_revision"])
    assert_exportable_catalog(connection)
    readme = """# Open Archive Catalog Generation

This directory is a complete open CSV/JSONL projection of the minimal catalog.
SQLite may be rebuilt from these files. `\\N` represents SQL NULL in CSV; a
literal leading backslash is escaped with one additional backslash.

Machine-specific absolute source paths and the retained broad-search index are
excluded.
"""
    (destination / "README.md").parent.mkdir(parents=True, exist_ok=True)
    (destination / "README.md").write_text(
        readme,
        encoding="utf-8",
        newline="\n",
    )
    with (destination / "generation.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        columns = [
            "generation_id",
            "previous_generation_id",
            "schema_version",
            "tool_version",
            "catalog_revision",
            "status",
            "created_at",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerow(
            {
                "generation_id": generation_id,
                "previous_generation_id": _encode_csv_value(
                    previous_generation_id
                ),
                "schema_version": SCHEMA_VERSION,
                "tool_version": tool_version,
                "catalog_revision": target_revision,
                "status": "current",
                "created_at": generation_row["created_at"],
            }
        )
    for table, relative, excluded in CSV_EXPORTS:
        _write_table_csv(
            connection,
            table,
            destination / relative,
            excluded,
            generation_id,
            previous_generation_id,
            target_revision,
        )
    _write_logbook_jsonl(
        connection,
        destination / "durability" / "logbook.jsonl",
    )
    write_manifest(destination)
    return verify_manifest(destination)


def publish_generation(
    root: Path,
    preservation_root: Path,
    tool_version: str,
    actor: str,
) -> dict[str, Any]:
    """Publish one generation while excluding other publisher processes.

    Publication crosses SQLite transactions and filesystem pointer updates, so
    SQLite's writer lock alone cannot protect the complete operation. The
    root-local advisory lock also serializes workbench threads and CLI runs.
    """

    root = assert_safe_work_root(root)
    lock_path = root / "work" / ".filesystem-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return _publish_generation_locked(
                root,
                preservation_root,
                tool_version,
                actor,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _publish_generation_locked(
    root: Path,
    preservation_root: Path,
    tool_version: str,
    actor: str,
) -> dict[str, Any]:
    root = assert_safe_work_root(root)
    preservation_root = assert_safe_work_root(preservation_root)
    config = require_core_mode(root)
    if not root.is_dir():
        raise CoreCatalogError(f"catalog root does not exist: {root}")
    preservation_root.mkdir(parents=True, exist_ok=True)
    assert_independent_device(root, preservation_root)
    configured_copy = str(config.get("independent_generation_root") or "")
    if configured_copy and Path(configured_copy).resolve() != preservation_root:
        raise CoreCatalogError(
            "preservation root differs from configured independent generation root"
        )
    if not configured_copy:
        config["independent_generation_root"] = str(preservation_root)
        save_config(root, config)
    catalog_path = root / "work" / "catalog.sqlite"
    connection = connect_catalog(catalog_path)
    generation_id = _generation_id()
    generations = root / "registry" / "generations"
    copy_generations = preservation_root / "registry" / "generations"
    temporary = generations / f".{generation_id}.tmp"
    copy_temporary = copy_generations / f".{generation_id}.tmp"
    destination = generations / generation_id
    copy_destination = copy_generations / generation_id
    current_pointer = root / "registry" / "CURRENT"
    migration_from = str(config.get("migration_from_generation") or "")
    pointer_id = (
        current_pointer.read_text(encoding="utf-8").strip()
        if current_pointer.is_file()
        else ""
    )
    if not migration_from or pointer_id != migration_from:
        reconcile_current_generation(connection, root)
    previous = connection.execute(
        """SELECT generation_id
           FROM filesystem_generations
           WHERE status = 'current'"""
    ).fetchone()
    previous_id = previous["generation_id"] if previous else None
    target_revision = int(catalog_state(connection)["catalog_revision"])
    now = utc_now()
    manifest_digest = ""
    try:
        connection.execute(
            """INSERT INTO filesystem_generations (
                   generation_id, previous_generation_id, schema_version,
                   tool_version, catalog_revision, status, manifest_digest, created_at,
                   completed_at
               ) VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
            (
                generation_id,
                previous_id,
                str(SCHEMA_VERSION),
                tool_version,
                target_revision,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO logbook (
                   log_id, at_utc, actor, unit_id, action, entity_type,
                   entity_id, before_json, after_json, evidence, note
               ) VALUES (?, ?, ?, NULL, 'publish_filesystem_generation',
                         'filesystem_generation', ?, '', ?, '', ?)""",
            (
                f"LOG-{uuid.uuid4().hex.upper()}",
                now,
                actor,
                generation_id,
                json.dumps(
                    {"status": "pending"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "candidate open generation",
            ),
        )
        connection.commit()
        assert_catalog_integrity(connection)
        verify_preservation_masters(connection, root, preservation_root)
        if any(
            path.exists()
            for path in (
                temporary,
                copy_temporary,
                destination,
                copy_destination,
            )
        ):
            raise CoreCatalogError("generation destination already exists")
        manifest_digest = _export_generation(
            connection,
            temporary,
            generation_id,
            previous_id,
            tool_version,
        )
        verify_manifest(temporary)
        copy_temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(temporary, copy_temporary, copy_function=shutil.copy2)
        copy_manifest_digest = verify_manifest(copy_temporary)
        if copy_manifest_digest != manifest_digest:
            raise CoreCatalogError("independent generation manifest differs")
        os.replace(temporary, destination)
        os.replace(copy_temporary, copy_destination)
        _atomic_write_text(
            preservation_root / "registry" / "CURRENT",
            generation_id + "\n",
        )
        _atomic_write_text(root / "registry" / "CURRENT", generation_id + "\n")
        connection.execute("BEGIN IMMEDIATE")
        if previous_id:
            connection.execute(
                """UPDATE filesystem_generations
                   SET status = 'superseded'
                   WHERE generation_id = ? AND status = 'current'""",
                (previous_id,),
            )
        connection.execute(
            """UPDATE filesystem_generations
               SET status = 'current', manifest_digest = ?
               WHERE generation_id = ? AND status = 'pending'""",
            (manifest_digest, generation_id),
        )
        connection.execute(
            """UPDATE catalog_state
               SET published_revision = ?,
                   dirty_since = CASE
                       WHEN catalog_revision = ? THEN NULL
                       ELSE dirty_since
                   END
               WHERE singleton = 1
                 AND catalog_revision >= ?""",
            (target_revision, target_revision, target_revision),
        )
        connection.commit()
        assert_catalog_integrity(connection)
        if config.pop("migration_from_generation", None) is not None:
            save_config(root, config)
    except Exception:
        connection.rollback()
        current_pointer = root / "registry" / "CURRENT"
        pointer_committed = (
            current_pointer.is_file()
            and current_pointer.read_text(encoding="utf-8").strip() == generation_id
        )
        if not pointer_committed and connection.execute(
            """SELECT 1 FROM filesystem_generations
               WHERE generation_id = ? AND status = 'pending'""",
            (generation_id,),
        ).fetchone():
            connection.execute(
                """UPDATE filesystem_generations
                   SET status = 'failed', completed_at = ?
                   WHERE generation_id = ?""",
                (utc_now(), generation_id),
            )
            connection.commit()
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(copy_temporary, ignore_errors=True)
        raise
    finally:
        connection.close()
    return {
        "result": "ok",
        "generation_id": generation_id,
        "generation": str(destination),
        "independent_copy": str(copy_destination),
        "manifest_digest": manifest_digest,
        "catalog_revision": target_revision,
        "status": "current",
    }


def _csv_export_by_table() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        table: (relative, tuple(excluded))
        for table, relative, excluded in CSV_EXPORTS
    }


def _import_csv_table(
    connection: sqlite3.Connection,
    table: str,
    path: Path,
    excluded: Iterable[str],
) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        expected = _table_columns(connection, table, excluded)
        if columns != expected:
            raise CoreCatalogError(f"invalid columns for {table}: {columns}")
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(f'"{column}"' for column in columns)
        sql = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
        for row in reader:
            connection.execute(
                sql,
                tuple(_decode_csv_value(row[column]) for column in columns),
            )


def rebuild_from_generation(
    generation: Path,
    output_db: Path,
) -> dict[str, Any]:
    generation = generation.expanduser().resolve()
    output_db = output_db.expanduser().resolve()
    manifest_digest = verify_manifest(generation)
    if output_db.exists():
        raise CoreCatalogError(f"refusing to replace rebuild database: {output_db}")
    output_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_db)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_SQL)
        exports = _csv_export_by_table()
        for table in IMPORT_ORDER:
            relative, excluded = exports[table]
            _import_csv_table(
                connection,
                table,
                generation / relative,
                excluded,
            )
        log_path = generation / "durability" / "logbook.jsonl"
        columns = _table_columns(connection, "logbook")
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(f'"{column}"' for column in columns)
        for line in log_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if set(row) != set(columns):
                raise CoreCatalogError("invalid logbook JSONL columns")
            connection.execute(
                f"INSERT INTO logbook ({quoted}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
        generation_header = read_generation_header(generation)
        generation_id = generation_header["generation_id"]
        connection.execute(
            """UPDATE filesystem_generations
               SET manifest_digest = ?, status = 'current'
               WHERE generation_id = ?""",
            (manifest_digest, generation_id),
        )
        connection.execute(
            """UPDATE catalog_state
               SET catalog_revision = ?,
                   published_revision = ?,
                   dirty_since = NULL
               WHERE singleton = 1""",
            (
                int(generation_header["catalog_revision"]),
                int(generation_header["catalog_revision"]),
            ),
        )
        connection.commit()
        assert_catalog_integrity(connection)
        assert_exportable_catalog(connection)
    except Exception:
        connection.close()
        output_db.unlink(missing_ok=True)
        raise
    finally:
        if connection:
            connection.close()
    return {
        "result": "ok",
        "generation_id": generation_id,
        "manifest_digest": manifest_digest,
        "output_db": str(output_db),
    }


def status(root: Path) -> dict[str, Any]:
    root = assert_safe_work_root(root)
    config = require_core_mode(root)
    catalog_path = root / "work" / "catalog.sqlite"
    connection = connect_catalog(catalog_path)
    try:
        recovery = reconcile_current_generation(connection, root)
        assert_catalog_integrity(connection)
        table_count = connection.execute(
            """SELECT count(*)
               FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
        ).fetchone()[0]
        entities = connection.execute("SELECT count(*) FROM entities").fetchone()[0]
        current = connection.execute(
            """SELECT generation_id, manifest_digest, catalog_revision
               FROM filesystem_generations
               WHERE status = 'current'"""
        ).fetchone()
        state = catalog_state(connection)
        preservation_root_text = str(
            config.get("independent_generation_root") or ""
        )
        master_health = (
            verify_preservation_masters(
                connection,
                root,
                Path(preservation_root_text),
            )
            if preservation_root_text
            else {
                "masters": 0,
                "primary_permission_read_only": 0,
                "primary_ntfs_read_only_attribute": 0,
                "independent_permission_read_only": 0,
                "fixity": "not-configured",
            }
        )
    finally:
        connection.close()
    retained = ""
    retained_tables = 0
    if config.get("retained_search_index"):
        retained = str(retained_index_path(root))
        connection = connect_retained_index(root)
        try:
            retained_tables = connection.execute(
                """SELECT count(*) FROM sqlite_master
                   WHERE type = 'table'"""
            ).fetchone()[0]
        finally:
            connection.close()
    return {
        "result": "ok",
        "catalog": str(catalog_path),
        "integrity": "ok",
        "tables": table_count,
        "entities": entities,
        "current_generation": current["generation_id"] if current else "",
        "current_manifest_digest": current["manifest_digest"] if current else "",
        "current_generation_revision": (
            int(current["catalog_revision"]) if current else None
        ),
        "catalog_revision": int(state["catalog_revision"]),
        "published_revision": int(state["published_revision"]),
        "dirty_since": state["dirty_since"] or "",
        "filesystem_freshness": (
            "current"
            if int(state["catalog_revision"]) == int(state["published_revision"])
            else "unpublished"
        ),
        "publication_state_healed": bool(recovery["healed"]),
        "retained_search_index": retained,
        "retained_index_tables": retained_tables,
        "retained_index_mode": "immutable-read-only" if retained else "",
        "preservation_masters": master_health,
    }
