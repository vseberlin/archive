#!/usr/bin/env python3
"""Bounded projection and decision log for Kurt Schwarz unit reconciliation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import unicodedata
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-reconciliation-v1"
PREVIEW_CACHE_VERSION = "3"
DOCUMENT_PREVIEW_CACHE_VERSION = "1"
SOURCE_ARCHIVE_ROOT = Path("/media/vladimir/Elements/restore/archive")
SOURCE_KURT_ROOT = SOURCE_ARCHIVE_ROOT / "WF/Kurt Schwarz"
AUTO_METADATA_POLICY = "md-preferred-obvious-mismatch-v1"
AUTOMATIC_CENSUS_MATCH_PREFIX = "Unique bounded"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd"}
MEDIA_DISPOSITIONS = {
    "selected_master",
    "genuine_variant",
    "access_derivative",
    "redundant",
    "wrong_object",
    "defer",
}
METADATA_ACTIONS = {
    "use_md",
    "use_local",
    "corrected",
    "combine",
    "defer",
}
OBJECT_RELATION_ACTIONS = {
    "same_object_alias",
    "independent",
}
DOCUMENT_DISPOSITIONS = {
    "keep_direct",
    "keep_contextual",
    "cross_unit_reference",
    "historical_package",
    "exclude",
    "defer",
}
OBJECT_CENSUS_QUEUE_CLASSES = {
    "master_with_metadata",
    "master_without_metadata",
    "hires_jpeg_without_master",
    "existing_object_reconciled",
    "surrogate_master_review",
    "new_local_review",
    "new_local_companion_reconciled",
}
OBJECT_CENSUS_DISPOSITIONS = {
    "new_local_object",
    "existing_object",
    "context_not_object",
    "new_local_metadata_right",
    "new_local_metadata_wrong",
    "surrogate_master",
    "surrogate_keep",
    "wrong_match",
    "same_new_candidate",
    "redundant",
    "cross_unit_hold",
    "defer",
}
OBJECT_CENSUS_SIMILARITY_OUTCOMES = {
    "same_photo",
    "different_photo",
    "defer",
}
CROSS_UNIT_HOLD_RELATIONSHIPS = {
    "ordered_scan_component",
    "supporting_variant",
    "capture_instruction_note",
    "unresolved_component",
}
MULTI_VALUE_FIELDS = {"tags", "places"}
METADATA_STOP_WORDS = {
    "als",
    "am",
    "an",
    "auch",
    "auf",
    "aus",
    "bei",
    "bild",
    "das",
    "dem",
    "den",
    "der",
    "des",
    "die",
    "durch",
    "ein",
    "eine",
    "einem",
    "einen",
    "einer",
    "eines",
    "er",
    "es",
    "foto",
    "für",
    "hinter",
    "im",
    "in",
    "ist",
    "jahr",
    "jahre",
    "kurt",
    "mit",
    "nach",
    "neben",
    "oder",
    "schwarz",
    "sie",
    "sich",
    "sind",
    "sw",
    "über",
    "und",
    "unter",
    "vom",
    "von",
    "vor",
    "war",
    "waren",
    "werden",
    "wird",
    "wurde",
    "zu",
    "zum",
    "zur",
}
METADATA_FIELDS = {
    "inventory": (
        "Inventory",
        "current_inventory",
        "historical_inventory",
    ),
    "title": ("Title", "current_title", "historical_title"),
    "description": (
        "Description",
        "current_description",
        "historical_description",
    ),
    "tags": ("Tags", "current_tags", "historical_tags"),
    "places": ("Places", "current_places", "historical_places"),
    "date_text": ("Date", "current_date_text", "historical_date_text"),
    "material": ("Material", "current_material", "historical_material"),
    "dimensions": (
        "Dimensions",
        "current_dimensions",
        "historical_dimensions",
    ),
}
OBJECT_CATALOG_FIELDS = {
    "record_key",
    "md_object_id",
    "current_inventory",
    "historical_inventory",
    "current_title",
    "selection_state",
    "selected_source_format",
    "selected_source_path",
    "tiff_candidate_count",
    "jpeg_candidate_count",
    *(
        column
        for _label, md_column, local_column in METADATA_FIELDS.values()
        for column in (md_column, local_column)
    ),
}


class KurtSchwarzReconciliationError(RuntimeError):
    """Raised when the bounded Kurt Schwarz review contract is violated."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(max(csv.field_size_limit(), 64 * 1024 * 1024))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def media_candidate_id(record_key: str, final_path: str) -> str:
    return (
        "MEDIA-"
        + hashlib.sha256(f"{record_key}\0{final_path}".encode()).hexdigest()[
            :24
        ].upper()
    )


def document_candidate_id(final_path: str) -> str:
    return "DOC-" + hashlib.sha256(final_path.encode()).hexdigest()[:24].upper()


def normalized_value(value: str, *, multi: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if multi:
        parts = {
            re.sub(r"\s+", " ", part.strip(" .,;"))
            for part in re.split(r"[;,|]", normalized)
            if part.strip(" .,;")
        }
        return "\x1f".join(sorted(parts))
    return re.sub(r"\s+", " ", normalized).strip(" .")


def metadata_state(field_key: str, md_value: str, local_value: str) -> str:
    if not md_value and not local_value:
        return "missing_both"
    if not md_value:
        return "missing_md"
    if not local_value:
        return "missing_local"
    if normalized_value(
        md_value, multi=field_key in MULTI_VALUE_FIELDS
    ) == normalized_value(
        local_value, multi=field_key in MULTI_VALUE_FIELDS
    ):
        return "same"
    return "conflict"


def combined_value(md_value: str, local_value: str) -> str:
    values: dict[str, str] = {}
    for value in (md_value, local_value):
        for part in re.split(r"[;,|]", value):
            display = re.sub(r"\s+", " ", part.strip(" .,;"))
            if display:
                values.setdefault(normalized_value(display), display)
    return "; ".join(values[key] for key in sorted(values))


def metadata_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return {
        token
        for token in re.findall(r"[\wäöüß]+", normalized)
        if len(token) > 2
        and not token.isdigit()
        and token not in METADATA_STOP_WORDS
    }


def metadata_token_overlap(md_value: str, local_value: str) -> float:
    md_tokens = metadata_tokens(md_value)
    local_tokens = metadata_tokens(local_value)
    if not md_tokens or not local_tokens:
        return 0.0
    return len(md_tokens & local_tokens) / min(
        len(md_tokens), len(local_tokens)
    )


def metadata_year_span(value: str) -> tuple[int, int] | None:
    normalized = normalized_value(value)
    decade = re.search(r"\b(19\d)0er(?: jahre)?\b", normalized)
    if decade:
        start = int(decade.group(1) + "0")
        return start, start + 9
    range_match = re.search(
        r"\b(19\d{2})\s*(?:bis|[-–])\s*(19\d{2}|\d{2})\b",
        normalized,
    )
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if end < 100:
            end = (start // 100) * 100 + end
        return min(start, end), max(start, end)
    years = [int(value) for value in re.findall(r"\b19\d{2}\b", normalized)]
    if years:
        return min(years), max(years)
    short_years = [
        1900 + int(value)
        for value in re.findall(r"(?<!\d)(\d{2})(?!\d)", normalized)
        if 30 <= int(value) <= 99
    ]
    if short_years:
        return min(short_years), max(short_years)
    return None


def metadata_year_gap(md_value: str, local_value: str) -> int:
    md_span = metadata_year_span(md_value)
    local_span = metadata_year_span(local_value)
    if not md_span or not local_span:
        return 0
    if md_span[1] < local_span[0]:
        return local_span[0] - md_span[1]
    if local_span[1] < md_span[0]:
        return md_span[0] - local_span[1]
    return 0


def obvious_metadata_mismatch_reason(
    field_key: str,
    md_value: str,
    local_value: str,
) -> str:
    if field_key in {"inventory", "material"}:
        return f"{field_key}_identity_conflict"
    if field_key == "date_text":
        gap = metadata_year_gap(md_value, local_value)
        return f"date_gap_{gap}_years" if gap >= 5 else ""
    overlap = metadata_token_overlap(md_value, local_value)
    if field_key == "title" and overlap < 0.25:
        return f"title_token_overlap_{overlap:.2f}"
    if field_key == "description" and overlap < 0.25:
        return f"description_token_overlap_{overlap:.2f}"
    if field_key in {"places", "tags"} and overlap == 0:
        return f"{field_key}_disjoint"
    return ""


class KurtSchwarzReview:
    """Project a published unit and append curator decisions outside that unit."""

    def __init__(self, unit_root: Path, review_root: Path) -> None:
        self.unit_root = unit_root.expanduser().resolve(strict=True)
        self.review_root = review_root.expanduser().resolve()
        if not self.unit_root.is_dir() or self.unit_root.name != "kurt-schwarz":
            raise KurtSchwarzReconciliationError(
                f"expected the published kurt-schwarz unit: {self.unit_root}"
            )
        if self.review_root == self.unit_root or self.review_root.is_relative_to(
            self.unit_root
        ):
            raise KurtSchwarzReconciliationError(
                "reconciliation work must remain outside the published unit"
            )
        self.catalog_path = self.unit_root / "CATALOG.csv"
        self.representations_path = (
            self.unit_root / "db-restore/manifests/representations.csv"
        )
        self.disposition_path = (
            self.unit_root / "db-restore/manifests/source-disposition.csv"
        )
        self.restore_database_path = (
            self.unit_root / "db-restore/RESTORE.sqlite"
        )
        for path in (
            self.catalog_path,
            self.representations_path,
            self.disposition_path,
        ):
            if not path.is_file():
                raise KurtSchwarzReconciliationError(
                    f"published unit control file is missing: {path}"
                )
        self.review_root.mkdir(parents=True, exist_ok=True)
        self.database = self.review_root / "reconciliation.sqlite"
        self.cache_root = self.review_root / "cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._analysis_lock = threading.Lock()
        self._preview_lock = threading.Semaphore(2)
        self.source_fingerprint = self._source_fingerprint()
        if self.database.exists():
            self._validate_database()
        else:
            self._build_projection()

    def _source_fingerprint(self) -> str:
        digest = hashlib.sha256()
        paths = [
            self.catalog_path,
            self.representations_path,
            self.disposition_path,
        ]
        catalog = read_csv(self.catalog_path)
        if not catalog or not OBJECT_CATALOG_FIELDS.issubset(catalog[0]):
            if not self.restore_database_path.is_file():
                raise KurtSchwarzReconciliationError(
                    "published object catalogue is unavailable"
                )
            paths.append(self.restore_database_path)
        for path in paths:
            digest.update(str(path.relative_to(self.unit_root)).encode())
            digest.update(b"\0")
            digest.update(sha256_file(path).encode())
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def _published_objects(self) -> list[dict[str, str]]:
        catalog = read_csv(self.catalog_path)
        if catalog and OBJECT_CATALOG_FIELDS.issubset(catalog[0]):
            return catalog
        if not self.restore_database_path.is_file():
            raise KurtSchwarzReconciliationError(
                "published object catalogue is unavailable"
            )
        with closing(
            sqlite3.connect(
                f"file:{self.restore_database_path}?mode=ro", uri=True
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise KurtSchwarzReconciliationError(
                    "published restore database failed quick_check"
                )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(objects)")
            }
            if not OBJECT_CATALOG_FIELDS.issubset(columns):
                raise KurtSchwarzReconciliationError(
                    "published restore database lacks the object catalogue"
                )
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM objects ORDER BY record_key"
                )
            ]
        for row in rows:
            row.setdefault(
                "object_directory",
                str(Path(row.get("master_relative_path", "")).parent),
            )
        return rows

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.database}?mode=ro", uri=True, timeout=20
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _validate_database(self) -> None:
        with closing(self.connect(read_only=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise KurtSchwarzReconciliationError(
                    f"reconciliation database integrity failed: {result}"
                )
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            required = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if meta.get("format") != FORMAT:
            raise KurtSchwarzReconciliationError(
                "reconciliation database format is unsupported"
            )
        if meta.get("source_fingerprint") != self.source_fingerprint:
            raise KurtSchwarzReconciliationError(
                "published Kurt Schwarz controls changed; preserve the decision "
                "database and start an explicitly reviewed reprojection"
            )
        if not {
            "objects",
            "media_candidates",
            "metadata_fields",
            "media_decisions",
            "metadata_decisions",
            "technical_analysis",
        }.issubset(required):
            raise KurtSchwarzReconciliationError(
                "reconciliation database schema is incomplete"
            )
        if "object_alias_decisions" not in required:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._create_alias_schema(connection)
                connection.commit()
        if "media_candidate_links" not in required:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._create_candidate_link_schema(connection)
                connection.commit()
        if "document_candidates" not in required:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._create_document_schema(connection)
                connection.commit()
        if "object_census_candidates" not in required:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._create_object_census_schema(connection)
                connection.commit()
        else:
            with closing(self.connect(read_only=True)) as connection:
                decision_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(object_census_decisions)"
                    )
                }
            missing_curator_fields = [
                column
                for column in (
                    "curated_title", "curated_date_text",
                    "curated_description", "curated_tags"
                )
                if column not in decision_columns
            ]
            if missing_curator_fields:
                with closing(self.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for column in missing_curator_fields:
                        connection.execute(
                            "ALTER TABLE object_census_decisions "
                            f"ADD COLUMN {column} TEXT"
                        )
                    connection.commit()
            if "cross_unit_hold_json" not in decision_columns:
                with closing(self.connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "ALTER TABLE object_census_decisions "
                        "ADD COLUMN cross_unit_hold_json TEXT"
                    )
                    connection.commit()
        if "object_census_similarity_decisions" not in required:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._create_object_census_similarity_schema(connection)
                connection.commit()

    @staticmethod
    def _create_alias_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS object_alias_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_record_key TEXT NOT NULL REFERENCES objects(record_key),
                canonical_record_key TEXT NOT NULL
                    REFERENCES objects(record_key),
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                CHECK (
                    action = 'independent'
                    OR alias_record_key <> canonical_record_key
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS object_alias_decisions_latest
                ON object_alias_decisions(alias_record_key, decision_id)
            """
        )

    @staticmethod
    def _create_candidate_link_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS media_candidate_links (
                link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL UNIQUE
                    REFERENCES media_candidates(candidate_id),
                target_record_key TEXT NOT NULL
                    REFERENCES objects(record_key),
                final_path TEXT NOT NULL,
                source_record_keys TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                UNIQUE(target_record_key, final_path)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS media_candidate_links_target
                ON media_candidate_links(target_record_key, link_id)
            """
        )

    @staticmethod
    def _create_document_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_candidates (
                document_id TEXT PRIMARY KEY,
                final_path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                source_path TEXT NOT NULL,
                content_family TEXT NOT NULL,
                proposed_relationship TEXT NOT NULL,
                proposed_disposition TEXT NOT NULL,
                text_evidence TEXT NOT NULL,
                review_basis TEXT NOT NULL,
                access_restriction TEXT NOT NULL,
                review_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL
                    REFERENCES document_candidates(document_id),
                disposition TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS document_decisions_latest
                ON document_decisions(document_id, decision_id)
            """
        )

    @staticmethod
    def _create_object_census_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS object_census_candidates (
                candidate_id TEXT PRIMARY KEY,
                queue_class TEXT NOT NULL,
                content_sha256 TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                width_px INTEGER,
                height_px INTEGER,
                final_path TEXT NOT NULL UNIQUE,
                primary_source_path TEXT NOT NULL,
                source_paths_json TEXT NOT NULL,
                metadata_matches_json TEXT NOT NULL,
                metadata_match_count INTEGER NOT NULL,
                metadata_summary TEXT NOT NULL,
                review_basis TEXT NOT NULL,
                review_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS object_census_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL
                    REFERENCES object_census_candidates(candidate_id),
                disposition TEXT NOT NULL,
                target_record_key TEXT REFERENCES objects(record_key),
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                curated_title TEXT,
                curated_date_text TEXT,
                curated_description TEXT,
                curated_tags TEXT,
                cross_unit_hold_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS object_census_decisions_latest
                ON object_census_decisions(candidate_id, decision_id)
            """
        )

    @staticmethod
    def _create_object_census_similarity_schema(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS object_census_similarity_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                candidate_a_id TEXT NOT NULL
                    REFERENCES object_census_candidates(candidate_id),
                candidate_b_id TEXT NOT NULL
                    REFERENCES object_census_candidates(candidate_id),
                outcome TEXT NOT NULL,
                primary_candidate_id TEXT
                    REFERENCES object_census_candidates(candidate_id),
                companion_candidate_id TEXT
                    REFERENCES object_census_candidates(candidate_id),
                run_fingerprint TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                CHECK (candidate_a_id < candidate_b_id),
                CHECK (outcome IN ('same_photo', 'different_photo', 'defer')),
                CHECK (
                    (outcome = 'same_photo'
                     AND primary_candidate_id IS NOT NULL
                     AND companion_candidate_id IS NOT NULL
                     AND primary_candidate_id <> companion_candidate_id)
                    OR
                    (outcome <> 'same_photo'
                     AND primary_candidate_id IS NULL
                     AND companion_candidate_id IS NULL)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS object_census_similarity_latest
                ON object_census_similarity_decisions(pair_id, decision_id)
            """
        )

    def _build_projection(self) -> None:
        temporary = self.database.with_name(
            f".{self.database.name}.tmp-{os.getpid()}"
        )
        if temporary.exists():
            raise KurtSchwarzReconciliationError(
                f"temporary reconciliation database already exists: {temporary}"
            )
        objects = self._published_objects()
        if len(objects) != len({row["record_key"] for row in objects}):
            raise KurtSchwarzReconciliationError(
                "published catalog contains duplicate record keys"
            )
        occurrences: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in read_csv(self.disposition_path):
            if row["final_path"]:
                occurrences[row["final_path"]].append(
                    {
                        "source_path": row["source_path"],
                        "source_sha256": row["source_sha256"],
                        "source_size_bytes": row["source_size_bytes"],
                        "preservation_status": row["preservation_status"],
                        "drain_state": row["drain_state"],
                        "deletion_authorized": row["deletion_authorized"],
                    }
                )
        object_keys = {row["record_key"] for row in objects}
        media: list[dict[str, Any]] = []
        for row in read_csv(self.representations_path):
            relative = row["final_relative_path"]
            if Path(relative).suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            keys = [
                value
                for value in row["record_keys"].split("; ")
                if value in object_keys
            ]
            for record_key in keys:
                candidate_id = media_candidate_id(record_key, relative)
                media.append(
                    {
                        "candidate_id": candidate_id,
                        "record_key": record_key,
                        "final_path": relative,
                        "filename": Path(relative).name,
                        "extension": Path(relative).suffix.casefold(),
                        "content_role": row["role"],
                        "content_sha256": row["final_sha256"],
                        "size_bytes": int(row["size_bytes"]),
                        "source_occurrences": json.dumps(
                            occurrences.get(relative, []),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )
        if len(media) != len(
            {(row["record_key"], row["final_path"]) for row in media}
        ):
            raise KurtSchwarzReconciliationError(
                "published representations create duplicate object candidates"
            )
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = DELETE;
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE objects (
                    record_key TEXT PRIMARY KEY,
                    md_object_id TEXT NOT NULL,
                    current_inventory TEXT NOT NULL,
                    historical_inventory TEXT NOT NULL,
                    current_title TEXT NOT NULL,
                    selection_state TEXT NOT NULL,
                    selected_source_format TEXT NOT NULL,
                    selected_source_path TEXT NOT NULL,
                    tiff_candidate_count INTEGER NOT NULL,
                    jpeg_candidate_count INTEGER NOT NULL,
                    object_directory TEXT NOT NULL,
                    catalog_json TEXT NOT NULL
                );
                CREATE TABLE media_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    record_key TEXT NOT NULL REFERENCES objects(record_key),
                    final_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    content_role TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    source_occurrences_json TEXT NOT NULL,
                    UNIQUE(record_key, final_path)
                );
                CREATE INDEX media_candidates_record
                    ON media_candidates(record_key, content_role, filename);
                CREATE TABLE metadata_fields (
                    record_key TEXT NOT NULL REFERENCES objects(record_key),
                    field_key TEXT NOT NULL,
                    field_label TEXT NOT NULL,
                    md_value TEXT NOT NULL,
                    local_value TEXT NOT NULL,
                    comparison_state TEXT NOT NULL,
                    PRIMARY KEY(record_key, field_key)
                );
                CREATE INDEX metadata_fields_state
                    ON metadata_fields(comparison_state, record_key);
                CREATE TABLE media_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_key TEXT NOT NULL REFERENCES objects(record_key),
                    candidate_id TEXT NOT NULL REFERENCES media_candidates(candidate_id),
                    disposition TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE INDEX media_decisions_latest
                    ON media_decisions(record_key, candidate_id, decision_id);
                CREATE TABLE metadata_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_key TEXT NOT NULL REFERENCES objects(record_key),
                    field_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    accepted_value TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    FOREIGN KEY(record_key, field_key)
                        REFERENCES metadata_fields(record_key, field_key)
                );
                CREATE INDEX metadata_decisions_latest
                    ON metadata_decisions(record_key, field_key, decision_id);
                CREATE TABLE technical_analysis (
                    candidate_id TEXT PRIMARY KEY
                        REFERENCES media_candidates(candidate_id),
                    reference_candidate_id TEXT NOT NULL,
                    width_px INTEGER NOT NULL,
                    height_px INTEGER NOT NULL,
                    bit_depth INTEGER NOT NULL,
                    colorspace TEXT NOT NULL,
                    media_format TEXT NOT NULL,
                    comparison_state TEXT NOT NULL,
                    pixel_difference_count TEXT NOT NULL,
                    embedded_metadata_json TEXT NOT NULL,
                    analyzed_at TEXT NOT NULL
                );
                """
            )
            self._create_alias_schema(connection)
            self._create_candidate_link_schema(connection)
            self._create_document_schema(connection)
            self._create_object_census_schema(connection)
            self._create_object_census_similarity_schema(connection)
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (
                    ("format", FORMAT),
                    ("created_at", utc_now()),
                    ("source_fingerprint", self.source_fingerprint),
                    ("unit_root", str(self.unit_root)),
                ),
            )
            connection.executemany(
                """INSERT INTO objects (
                       record_key, md_object_id, current_inventory,
                       historical_inventory, current_title, selection_state,
                       selected_source_format, selected_source_path,
                       tiff_candidate_count, jpeg_candidate_count,
                       object_directory, catalog_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["record_key"],
                        row["md_object_id"],
                        row["current_inventory"],
                        row["historical_inventory"],
                        row["current_title"],
                        row["selection_state"],
                        row["selected_source_format"],
                        row["selected_source_path"],
                        int(row["tiff_candidate_count"] or 0),
                        int(row["jpeg_candidate_count"] or 0),
                        row["object_directory"],
                        json.dumps(row, ensure_ascii=False, sort_keys=True),
                    )
                    for row in objects
                ],
            )
            connection.executemany(
                """INSERT INTO media_candidates (
                       candidate_id, record_key, final_path, filename, extension,
                       content_role, content_sha256, size_bytes,
                       source_occurrences_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["candidate_id"],
                        row["record_key"],
                        row["final_path"],
                        row["filename"],
                        row["extension"],
                        row["content_role"],
                        row["content_sha256"],
                        row["size_bytes"],
                        row["source_occurrences"],
                    )
                    for row in media
                ],
            )
            connection.executemany(
                """INSERT INTO metadata_fields (
                       record_key, field_key, field_label, md_value,
                       local_value, comparison_state
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["record_key"],
                        field_key,
                        label,
                        row[md_column],
                        row[local_column],
                        metadata_state(
                            field_key,
                            row[md_column],
                            row[local_column],
                        ),
                    )
                    for row in objects
                    for field_key, (
                        label,
                        md_column,
                        local_column,
                    ) in METADATA_FIELDS.items()
                ],
            )
            connection.commit()
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise KurtSchwarzReconciliationError(
                    f"new reconciliation projection failed integrity: {result}"
                )
        except Exception:
            connection.close()
            temporary.unlink(missing_ok=True)
            raise
        connection.close()
        os.replace(temporary, self.database)

    @staticmethod
    def _latest_media_decisions(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            row["candidate_id"]: dict(row)
            for row in connection.execute(
                """SELECT * FROM media_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM media_decisions
                     GROUP BY candidate_id
                   )"""
            )
        }

    @staticmethod
    def _latest_metadata_decisions(
        connection: sqlite3.Connection,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (row["record_key"], row["field_key"]): dict(row)
            for row in connection.execute(
                """SELECT * FROM metadata_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM metadata_decisions
                     GROUP BY record_key, field_key
                   )"""
            )
        }

    @staticmethod
    def _effective_metadata_value(
        field: dict[str, Any],
        decision: dict[str, Any] | None,
    ) -> str:
        if decision and decision["action"] != "defer":
            accepted = str(decision["accepted_value"]).strip()
            if accepted:
                return accepted
        return str(field["md_value"] or field["local_value"])

    @staticmethod
    def _latest_alias_decisions(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            row["alias_record_key"]: dict(row)
            for row in connection.execute(
                """SELECT * FROM object_alias_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM object_alias_decisions
                     GROUP BY alias_record_key
                   )"""
            )
        }

    @classmethod
    def _active_alias_decisions(
        cls,
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            record_key: decision
            for record_key, decision in cls._latest_alias_decisions(
                connection
            ).items()
            if decision["action"] == "same_object_alias"
        }

    def import_document_review(
        self,
        review_csv: Path,
        *,
        actor: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        review_csv = review_csv.resolve()
        work_root = next(
            (
                parent.resolve()
                for parent in (self.review_root, *self.review_root.parents)
                if parent.name == "work"
            ),
            None,
        )
        if work_root is None:
            raise KurtSchwarzReconciliationError(
                "review root does not have an owning work directory"
            )
        try:
            review_csv.relative_to(work_root)
        except ValueError as error:
            raise KurtSchwarzReconciliationError(
                "document review must be inside the Kurt Schwarz work root"
            ) from error
        if not review_csv.is_file():
            raise KurtSchwarzReconciliationError(
                f"document review is missing: {review_csv}"
            )
        rows = read_csv(review_csv)
        if len(rows) != 484 or len({row["final_path"] for row in rows}) != 484:
            raise KurtSchwarzReconciliationError(
                "document review must contain 484 unique published paths"
            )
        manifested = {
            row["final_relative_path"]: row
            for row in read_csv(self.representations_path)
            if row["final_relative_path"].startswith("documents/")
            and row["final_relative_path"] != "documents/CONTENTS.md"
        }
        candidates = []
        seed_map = {
            "package_historical": "historical_package",
            "retain_contextual": "keep_contextual",
            "cross_unit_reference": "cross_unit_reference",
        }
        seed_counts: Counter[str] = Counter()
        for row in rows:
            final_path = row["final_path"]
            manifest = manifested.get(final_path)
            if manifest is None:
                raise KurtSchwarzReconciliationError(
                    f"document review path is not manifested: {final_path}"
                )
            if row["sha256"] != manifest["final_sha256"]:
                raise KurtSchwarzReconciliationError(
                    f"document review digest drift: {final_path}"
                )
            if row["deletion_authorized"].casefold() != "false":
                raise KurtSchwarzReconciliationError(
                    f"document review grants deletion authority: {final_path}"
                )
            disposition = seed_map.get(row["proposed_disposition"], "")
            if disposition:
                seed_counts[disposition] += 1
            candidates.append(
                {
                    "document_id": document_candidate_id(final_path),
                    "final_path": final_path,
                    "filename": row["filename"],
                    "extension": row["extension"],
                    "size_bytes": int(row["size_bytes"]),
                    "content_sha256": row["sha256"],
                    "source_path": row["source_path"],
                    "content_family": row["content_family"],
                    "proposed_relationship": row["proposed_relationship"],
                    "proposed_disposition": row["proposed_disposition"],
                    "text_evidence": row["text_evidence"],
                    "review_basis": row["review_basis"],
                    "access_restriction": row["access_restriction"],
                    "review_json": json.dumps(
                        row, ensure_ascii=False, sort_keys=True
                    ),
                    "seed_disposition": disposition,
                }
            )
        fingerprint = sha256_file(review_csv)
        result = {
            "state": "dry_run",
            "review_csv": str(review_csv),
            "review_fingerprint": fingerprint,
            "candidates": len(candidates),
            "seed_decisions": sum(seed_counts.values()),
            "pending_visual_review": sum(
                not row["seed_disposition"] for row in candidates
            ),
            "seed_counts": dict(seed_counts),
            "deletion_authorized": False,
        }
        with closing(self.connect(read_only=True)) as connection:
            existing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM document_candidates"
                ).fetchone()[0]
            )
            meta = dict(connection.execute("SELECT key, value FROM meta"))
        if existing:
            if existing != len(candidates) or meta.get(
                "document_review_fingerprint"
            ) != fingerprint:
                raise KurtSchwarzReconciliationError(
                    "a different document review is already imported"
                )
            return {**result, "state": "already_imported"}
        if not apply:
            return result
        decided_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """INSERT INTO document_candidates (
                       document_id, final_path, filename, extension,
                       size_bytes, content_sha256, source_path,
                       content_family, proposed_relationship,
                       proposed_disposition, text_evidence, review_basis,
                       access_restriction, review_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["document_id"],
                        row["final_path"],
                        row["filename"],
                        row["extension"],
                        row["size_bytes"],
                        row["content_sha256"],
                        row["source_path"],
                        row["content_family"],
                        row["proposed_relationship"],
                        row["proposed_disposition"],
                        row["text_evidence"],
                        row["review_basis"],
                        row["access_restriction"],
                        row["review_json"],
                    )
                    for row in candidates
                ],
            )
            connection.executemany(
                """INSERT INTO document_decisions (
                       document_id, disposition, actor, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        row["document_id"],
                        row["seed_disposition"],
                        actor,
                        row["review_basis"],
                        decided_at,
                    )
                    for row in candidates
                    if row["seed_disposition"]
                ],
            )
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                (
                    ("document_review_fingerprint", fingerprint),
                    ("document_review_path", str(review_csv)),
                ),
            )
            connection.commit()
        return {**result, "state": "applied", "decided_at": decided_at}

    @staticmethod
    def _latest_document_decisions(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            row["document_id"]: dict(row)
            for row in connection.execute(
                """SELECT * FROM document_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM document_decisions
                     GROUP BY document_id
                   )"""
            )
        }

    def documents(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            latest = self._latest_document_decisions(connection)
            rows = connection.execute(
                """SELECT * FROM document_candidates
                   ORDER BY filename, final_path"""
            ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            decision = latest.get(item["document_id"])
            item["decision"] = decision
            item["effective_disposition"] = (
                decision["disposition"] if decision else ""
            )
            item["review_state"] = (
                "pending"
                if not decision or decision["disposition"] == "defer"
                else "decided"
            )
            result.append(item)
        return result

    def document(self, document_id: str) -> dict[str, Any]:
        item = next(
            (
                row
                for row in self.documents()
                if row["document_id"] == document_id
            ),
            None,
        )
        if item is None:
            raise KurtSchwarzReconciliationError(
                f"unknown document candidate: {document_id}"
            )
        return item

    def document_path(self, document_id: str) -> Path:
        item = self.document(document_id)
        path = (self.unit_root / item["final_path"]).resolve()
        documents_root = (self.unit_root / "documents").resolve()
        try:
            path.relative_to(documents_root)
        except ValueError as error:
            raise KurtSchwarzReconciliationError(
                "document path escaped the published documentation root"
            ) from error
        if path.is_file():
            return path
        source_path = str(item["source_path"])
        if "::" in source_path:
            container_text, member = source_path.split("::", 1)
            container = self._source_archive_path(container_text)
            member_path = Path(member)
            if (
                not member
                or member_path.is_absolute()
                or ".." in member_path.parts
            ):
                raise KurtSchwarzReconciliationError(
                    "document archive member escaped its source container"
                )
            suffix = Path(item["filename"]).suffix.casefold()
            digest = str(item["content_sha256"]).removeprefix("sha256:")
            destination = self.cache_root / (
                f"document-original-{digest}{suffix}"
            )
            if destination.is_file() and destination.stat().st_size == int(
                item["size_bytes"]
            ):
                return destination
            temporary = destination.with_name(
                f".{destination.name}.tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                with zipfile.ZipFile(container) as archive:
                    try:
                        info = archive.getinfo(member)
                    except KeyError as error:
                        raise KurtSchwarzReconciliationError(
                            f"document archive member is missing: {member}"
                        ) from error
                    if info.is_dir() or info.file_size != int(item["size_bytes"]):
                        raise KurtSchwarzReconciliationError(
                            f"document archive member size drifted: {member}"
                        )
                    with archive.open(info) as source, temporary.open("wb") as output:
                        shutil.copyfileobj(source, output)
                if sha256_file(temporary) != item["content_sha256"]:
                    raise KurtSchwarzReconciliationError(
                        f"document archive member digest drifted: {member}"
                    )
                os.replace(temporary, destination)
            except (OSError, zipfile.BadZipFile) as error:
                raise KurtSchwarzReconciliationError(
                    f"cannot read document archive: {container}"
                ) from error
            finally:
                temporary.unlink(missing_ok=True)
            return destination
        return self._source_archive_path(source_path)

    @staticmethod
    def _source_archive_path(source_path: str) -> Path:
        try:
            root = SOURCE_ARCHIVE_ROOT.resolve(strict=True)
            path = Path(source_path).resolve(strict=True)
        except OSError as error:
            raise KurtSchwarzReconciliationError(
                f"source evidence is unavailable: {source_path}"
            ) from error
        if not path.is_file() or not path.is_relative_to(root):
            raise KurtSchwarzReconciliationError(
                f"source evidence escaped the configured archive: {source_path}"
            )
        return path

    def document_preview(self, document_id: str) -> tuple[Path, str]:
        item = self.document(document_id)
        source = self.document_path(document_id)
        extension = str(item["extension"]).casefold()
        if extension == ".pdf":
            return source, "application/pdf"
        if extension == ".mp3":
            return source, "audio/mpeg"

        digest = hashlib.sha256(
            (
                f"document-preview-v{DOCUMENT_PREVIEW_CACHE_VERSION}\0"
                f"{document_id}\0{item['content_sha256']}"
            ).encode()
        ).hexdigest()
        destination = self.cache_root / f"document-preview-{digest}.html"
        if destination.is_file() and destination.stat().st_size > 0:
            return destination, "text/html; charset=utf-8"

        text = self._document_preview_text(source, extension)
        escaped = html.escape(text or "No readable standalone content was found.")
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<style>body{font:15px/1.45 system-ui,sans-serif;margin:1.5rem;"
            "color:#222;background:#fff}h1{font-size:1.1rem}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>"
            f"<h1>{html.escape(str(item['filename']))}</h1><pre>{escaped}</pre>"
            "</body></html>"
        ).encode("utf-8")
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            temporary.write_bytes(body)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination, "text/html; charset=utf-8"

    @staticmethod
    def _document_preview_text(source: Path, extension: str) -> str:
        if extension in {".txt", ".csv", ".url", ".vcf", ".html", ""}:
            return source.read_bytes()[:1_000_000].decode(
                "utf-8", errors="replace"
            )
        if extension == ".doc" and shutil.which("catdoc"):
            command = ["catdoc", str(source)]
        elif extension == ".xls" and shutil.which("xls2csv"):
            command = ["xls2csv", str(source)]
        else:
            command = []
        if command:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
                return result.stdout[:1_000_000].decode(
                    "utf-8", errors="replace"
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        if extension in {".docx", ".xlsx", ".odt", ".pptx"}:
            prefixes = {
                ".docx": ("word/document.xml",),
                ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
                ".odt": ("content.xml",),
                ".pptx": ("ppt/slides/",),
            }[extension]
            try:
                chunks: list[str] = []
                with zipfile.ZipFile(source) as archive:
                    names = sorted(
                        name for name in archive.namelist()
                        if any(name.startswith(prefix) for prefix in prefixes)
                        and name.endswith(".xml")
                    )
                    for name in names:
                        payload = archive.read(name)[:5_000_000].decode(
                            "utf-8", errors="replace"
                        )
                        values = [
                            html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
                            for value in re.findall(r"<[^>]*?(?:t|v)(?:\s[^>]*)?>(.*?)</[^>]+>", payload, re.DOTALL)
                        ]
                        values = [value for value in values if value]
                        if values:
                            chunks.append(f"[{name}]\n" + "\n".join(values))
                        if sum(len(chunk) for chunk in chunks) >= 1_000_000:
                            break
                return "\n\n".join(chunks)[:1_000_000]
            except (OSError, zipfile.BadZipFile):
                pass
        if extension == ".zip":
            try:
                with zipfile.ZipFile(source) as archive:
                    rows = [
                        f"{info.filename}\t{info.file_size:,} bytes"
                        for info in archive.infolist()[:5000]
                    ]
                return "Archive contents:\n" + "\n".join(rows)
            except (OSError, zipfile.BadZipFile):
                pass
        return (
            f"Binary companion format {extension or '(no extension)'}; "
            "no reliable standalone rendering is available. Use the extracted "
            "evidence and original-file link in the review page."
        )

    def object_census_path(self, candidate_id: str) -> Path:
        with closing(self.connect(read_only=True)) as connection:
            raw = connection.execute(
                """SELECT final_path, primary_source_path, source_paths_json
                   FROM object_census_candidates WHERE candidate_id=?""",
                (candidate_id,),
            ).fetchone()
        if raw is None:
            raise KurtSchwarzReconciliationError(
                f"unknown object census candidate: {candidate_id}"
            )
        item = dict(raw)
        item["source_paths"] = json.loads(item.pop("source_paths_json"))
        path = (self.unit_root / item["final_path"]).resolve()
        allowed = (
            (self.unit_root / "masters/unassigned").resolve(),
            (self.unit_root / "export/access/unassigned").resolve(),
        )
        if path.is_file() and any(path.is_relative_to(root) for root in allowed):
            return path
        source_path = str(item["primary_source_path"])
        if source_path not in item["source_paths"]:
            raise KurtSchwarzReconciliationError(
                "object census primary source is not manifested for the candidate"
            )
        source = self._source_archive_path(source_path)
        try:
            kurt_root = SOURCE_KURT_ROOT.resolve(strict=True)
        except OSError as error:
            raise KurtSchwarzReconciliationError(
                f"Kurt Schwarz source root is unavailable: {SOURCE_KURT_ROOT}"
            ) from error
        if not source.is_relative_to(kurt_root):
            raise KurtSchwarzReconciliationError(
                f"object census candidate escaped the Kurt Schwarz source: {source}"
            )
        return source

    def object_census_preview(self, candidate_id: str) -> Path:
        source = self.object_census_path(candidate_id)
        stat = source.stat()
        digest = hashlib.sha256(
            (
                f"object-census-preview-v1\0{candidate_id}\0"
                f"{stat.st_size}\0{stat.st_mtime_ns}"
            ).encode()
        ).hexdigest()
        destination = self.cache_root / "object-census-previews" / f"{digest}.jpg"
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.stem}.tmp-{os.getpid()}-"
            f"{threading.get_ident()}.jpg"
        )
        with self._preview_lock:
            if destination.is_file() and destination.stat().st_size > 0:
                return destination
            commands = []
            if (
                source.suffix.casefold() in {".tif", ".tiff"}
                and shutil.which("vipsthumbnail")
            ):
                commands.append(
                    [
                        "vipsthumbnail", str(source), "--size", "1200x900",
                        "--output", str(temporary), "--vips-concurrency=1",
                        "--vips-disc-threshold=104857600", "--vips-cache-max=100",
                        "--vips-cache-max-memory=268435456",
                        "--vips-cache-max-files=32",
                    ]
                )
            commands.append(
                [
                    "magick", f"{source}[0]", "-auto-orient", "-thumbnail",
                    "1200x900>", "-strip", "-colorspace", "sRGB", "-quality",
                    "88", f"jpeg:{temporary}",
                ]
            )
            failures = []
            try:
                for command in commands:
                    temporary.unlink(missing_ok=True)
                    try:
                        subprocess.run(
                            command,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=120,
                        )
                        break
                    except (
                        FileNotFoundError,
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                    ) as error:
                        stderr = getattr(error, "stderr", b"")
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode("utf-8", errors="replace")
                        failures.append(
                            f"{command[0]}: {str(stderr or error).strip()}"
                        )
                else:
                    placeholder = [
                        "magick", "-size", "1200x900", "xc:#f2eee6",
                        "-fill", "#332f2a", "-gravity", "center",
                        "-pointsize", "32", "-annotate", "+0-25",
                        "Preview unavailable", "-pointsize", "20",
                        "-annotate", "+0+30",
                        f"{source.suffix.upper().lstrip('.')} decoder failure",
                        f"jpeg:{temporary}",
                    ]
                    try:
                        subprocess.run(
                            placeholder,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=30,
                        )
                    except (
                        FileNotFoundError,
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                    ) as error:
                        raise KurtSchwarzReconciliationError(
                            f"object census preview failed for {source}: "
                            + " | ".join(failures)
                        ) from error
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def save_document_decision(
        self,
        *,
        document_id: str,
        disposition: str,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        disposition = disposition.strip()
        actor = actor.strip()
        reason = reason.strip()
        if disposition not in DOCUMENT_DISPOSITIONS:
            raise KurtSchwarzReconciliationError(
                f"unsupported document disposition: {disposition}"
            )
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        self.document(document_id)
        decided_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO document_decisions (
                       document_id, disposition, actor, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (document_id, disposition, actor, reason, decided_at),
            )
            decision_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            connection.commit()
        return {
            "decision_id": decision_id,
            "document_id": document_id,
            "disposition": disposition,
            "decided_at": decided_at,
        }

    def import_object_census(
        self,
        census_csv: Path,
        *,
        actor: str,
        apply: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        census_csv = census_csv.resolve()
        work_root = next(
            (
                parent.resolve()
                for parent in (self.review_root, *self.review_root.parents)
                if parent.name == "work"
            ),
            None,
        )
        if work_root is None:
            raise KurtSchwarzReconciliationError(
                "review root does not have an owning work directory"
            )
        try:
            census_csv.relative_to(work_root)
        except ValueError as error:
            raise KurtSchwarzReconciliationError(
                "object census must be inside the Kurt Schwarz work root"
            ) from error
        if not census_csv.is_file():
            raise KurtSchwarzReconciliationError(
                f"object census is missing: {census_csv}"
            )
        rows = read_csv(census_csv)
        if not rows or len(rows) != len({row["candidate_id"] for row in rows}):
            raise KurtSchwarzReconciliationError(
                "object census must contain unique candidates"
            )
        if len(rows) != len({row["content_sha256"] for row in rows}):
            raise KurtSchwarzReconciliationError(
                "object census must contain unique exact-byte states"
            )
        disposition = read_csv(self.disposition_path)
        reconciled_fields = {
            "target_record_key",
            "target_inventory",
            "target_media_candidate_id",
            "related_candidate_id",
            "selected_metadata_json",
            "selected_metadata_basis",
            "metadata_enrichment_json",
            "visual_match_json",
            "auto_decision_reason",
        }
        reconciled = reconciled_fields.issubset(rows[0])
        if refresh and not reconciled:
            raise KurtSchwarzReconciliationError(
                "an object census refresh requires reconciled census fields"
            )
        with closing(self.connect(read_only=True)) as connection:
            object_keys = {
                str(row[0]) for row in connection.execute("SELECT record_key FROM objects")
            }
            media_targets = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT candidate_id, record_key FROM media_candidates"
                )
            }
            metadata_field_targets = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT record_key, field_key FROM metadata_fields"
                )
            }
        manifested: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        manifested_by_digest: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in disposition:
            if item["final_path"] and item["source_sha256"]:
                manifested[(item["final_path"], item["source_sha256"])].append(item)
                manifested_by_digest[item["source_sha256"]].append(item)
        candidates = []
        counts: Counter[str] = Counter()
        for row in rows:
            queue_class = row["queue_class"]
            if queue_class not in OBJECT_CENSUS_QUEUE_CLASSES:
                raise KurtSchwarzReconciliationError(
                    f"unsupported object census queue: {queue_class}"
                )
            if row["deletion_authorized"].casefold() != "false":
                raise KurtSchwarzReconciliationError(
                    f"object census grants deletion authority: {row['candidate_id']}"
                )
            occurrences = manifested.get(
                (row["final_path"], row["content_sha256"]), []
            )
            if not occurrences:
                raise KurtSchwarzReconciliationError(
                    f"object census path/digest is not manifested: {row['final_path']}"
                )
            try:
                source_paths = json.loads(row["source_paths_json"])
                metadata_matches = json.loads(row["metadata_matches_json"])
            except json.JSONDecodeError as error:
                raise KurtSchwarzReconciliationError(
                    f"invalid object census JSON: {row['candidate_id']}"
                ) from error
            if not isinstance(source_paths, list) or not isinstance(
                metadata_matches, list
            ):
                raise KurtSchwarzReconciliationError(
                    f"object census evidence is not a list: {row['candidate_id']}"
                )
            manifested_sources = {
                item["source_path"]
                for item in manifested_by_digest[row["content_sha256"]]
            }
            if not set(source_paths).issubset(manifested_sources):
                raise KurtSchwarzReconciliationError(
                    f"object census source occurrences drifted: {row['candidate_id']}"
                )
            if any(
                not source.startswith(
                    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
                )
                for source in source_paths
            ):
                raise KurtSchwarzReconciliationError(
                    f"object census escaped the Kurt Schwarz source: {row['candidate_id']}"
                )
            metadata_count = int(row["metadata_match_count"])
            if metadata_count != len(metadata_matches):
                raise KurtSchwarzReconciliationError(
                    f"object census metadata count drifted: {row['candidate_id']}"
                )
            suffix = row["extension"].casefold()
            if suffix in {".tif", ".tiff", ".psd"}:
                if not row["final_path"].startswith("masters/unassigned/"):
                    raise KurtSchwarzReconciliationError(
                        f"master queue contains a non-master candidate: {row['candidate_id']}"
                    )
                expected_metadata = queue_class == "master_with_metadata"
                if queue_class in {
                    "master_with_metadata", "master_without_metadata"
                } and bool(metadata_count) != expected_metadata:
                    raise KurtSchwarzReconciliationError(
                        f"metadata queue disagrees with evidence: {row['candidate_id']}"
                    )
            elif suffix in {".jpg", ".jpeg"}:
                width = int(row["width_px"])
                height = int(row["height_px"])
                if (
                    not row["final_path"].startswith("export/access/unassigned/")
                    or max(width, height) < 2000
                    or width * height < 4_000_000
                    or "listviewimgs" in row["primary_source_path"].casefold()
                ):
                    raise KurtSchwarzReconciliationError(
                        f"JPEG queue contains a low-resolution/invalid candidate: {row['candidate_id']}"
                    )
            else:
                raise KurtSchwarzReconciliationError(
                    f"object census contains an unsupported image: {row['candidate_id']}"
                )
            if reconciled:
                try:
                    selected_metadata = json.loads(row["selected_metadata_json"])
                    metadata_enrichment = json.loads(
                        row["metadata_enrichment_json"]
                    )
                    visual = json.loads(row["visual_match_json"])
                except json.JSONDecodeError as error:
                    raise KurtSchwarzReconciliationError(
                        f"invalid reconciled census JSON: {row['candidate_id']}"
                    ) from error
                if (
                    not isinstance(selected_metadata, dict)
                    or not isinstance(metadata_enrichment, dict)
                    or not isinstance(visual, dict)
                ):
                    raise KurtSchwarzReconciliationError(
                        f"reconciled census evidence is not an object: {row['candidate_id']}"
                    )
                target_key = row["target_record_key"]
                target_media = row["target_media_candidate_id"]
                related_candidate = row["related_candidate_id"]
                needs_target = queue_class in {
                    "existing_object_reconciled", "surrogate_master_review"
                }
                if needs_target != bool(target_key):
                    raise KurtSchwarzReconciliationError(
                        f"reconciled target disagrees with queue: {row['candidate_id']}"
                    )
                if target_key and target_key not in object_keys:
                    raise KurtSchwarzReconciliationError(
                        f"reconciled target is unknown: {row['candidate_id']}"
                    )
                if target_media and media_targets.get(target_media) != target_key:
                    raise KurtSchwarzReconciliationError(
                        f"comparison media is not bound to its target: {row['candidate_id']}"
                    )
                needs_related = queue_class == "new_local_companion_reconciled"
                if needs_related != bool(related_candidate):
                    raise KurtSchwarzReconciliationError(
                        f"related candidate disagrees with queue: {row['candidate_id']}"
                    )
                if related_candidate == row["candidate_id"]:
                    raise KurtSchwarzReconciliationError(
                        f"candidate cannot be its own companion: {row['candidate_id']}"
                    )
                if queue_class in {
                    "existing_object_reconciled",
                    "new_local_companion_reconciled",
                } and not row["auto_decision_reason"].strip():
                    raise KurtSchwarzReconciliationError(
                        f"automatic reconciliation lacks a reason: {row['candidate_id']}"
                    )
                if metadata_enrichment and not target_key:
                    raise KurtSchwarzReconciliationError(
                        f"metadata enrichment has no target: {row['candidate_id']}"
                    )
                for field_key, value in metadata_enrichment.items():
                    if (target_key, field_key) not in metadata_field_targets:
                        raise KurtSchwarzReconciliationError(
                            f"metadata enrichment field is unknown: "
                            f"{row['candidate_id']} {field_key}"
                        )
                    if not str(value).strip():
                        raise KurtSchwarzReconciliationError(
                            f"metadata enrichment value is empty: "
                            f"{row['candidate_id']} {field_key}"
                        )
            size = int(row["size_bytes"])
            candidate_occurrences = [
                item
                for item in manifested_by_digest[row["content_sha256"]]
                if item["source_path"] in set(source_paths)
            ]
            if any(
                int(item["source_size_bytes"]) != size
                for item in candidate_occurrences
            ):
                raise KurtSchwarzReconciliationError(
                    f"object census size drifted: {row['candidate_id']}"
                )
            counts[queue_class] += 1
            candidates.append(
                {
                    **row,
                    "size_bytes": size,
                    "width_px": int(row["width_px"]) if row["width_px"] else None,
                    "height_px": int(row["height_px"]) if row["height_px"] else None,
                    "metadata_match_count": metadata_count,
                    "review_json": json.dumps(row, ensure_ascii=False, sort_keys=True),
                }
            )
        fingerprint = sha256_file(census_csv)
        result = {
            "state": "dry_run",
            "census_csv": str(census_csv),
            "census_fingerprint": fingerprint,
            "candidates": len(candidates),
            "queue_counts": dict(counts),
            "deletion_authorized": False,
        }
        with closing(self.connect(read_only=True)) as connection:
            existing = int(
                connection.execute(
                    "SELECT COUNT(*) FROM object_census_candidates"
                ).fetchone()[0]
            )
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT candidate_id FROM object_census_candidates"
                )
            }
            latest_decisions = self._latest_object_census_decisions(connection)
        supplied_ids = {row["candidate_id"] for row in candidates}
        for row in candidates:
            related = row.get("related_candidate_id", "")
            if related and related not in supplied_ids:
                raise KurtSchwarzReconciliationError(
                    f"related census candidate is unknown: {row['candidate_id']}"
                )
        if existing:
            if (
                existing == len(candidates)
                and meta.get("object_census_fingerprint") == fingerprint
            ):
                return {**result, "state": "already_imported"}
            if not refresh:
                raise KurtSchwarzReconciliationError(
                    "a different object census is already imported"
                )
            if existing_ids != supplied_ids:
                raise KurtSchwarzReconciliationError(
                    "object census refresh changed the candidate identity set"
                )
        automatic = [
            row for row in candidates
            if row["queue_class"] == "existing_object_reconciled"
        ]
        automatic_companions = [
            row for row in candidates
            if row["queue_class"] == "new_local_companion_reconciled"
        ]
        automatic_reopens = []
        for row in candidates:
            current = latest_decisions.get(row["candidate_id"])
            if (
                row["queue_class"] == "new_local_review"
                and current
                and current["disposition"] == "existing_object"
                and str(current["reason"]).startswith(
                    AUTOMATIC_CENSUS_MATCH_PREFIX
                )
            ):
                automatic_reopens.append(row)
        enrichments = [
            (row, field_key, str(value).strip())
            for row in candidates
            for field_key, value in json.loads(
                row.get("metadata_enrichment_json", "{}")
            ).items()
        ]
        automatic_appends = 0
        automatic_supersedes = 0
        for row in automatic:
            current = latest_decisions.get(row["candidate_id"])
            if (
                current
                and current["disposition"] == "existing_object"
                and current["target_record_key"] == row["target_record_key"]
            ):
                continue
            automatic_appends += 1
            if current and current["disposition"] != "defer":
                automatic_supersedes += 1
        automatic_companion_appends = 0
        for row in automatic_companions:
            current = latest_decisions.get(row["candidate_id"])
            if current and current["disposition"] == "same_new_candidate":
                continue
            automatic_companion_appends += 1
            if current and current["disposition"] != "defer":
                automatic_supersedes += 1
        automatic_supersedes += len(automatic_reopens)
        result.update(
            {
                "refresh": bool(existing),
                "automatic_existing_decisions": automatic_appends,
                "automatic_companion_decisions": automatic_companion_appends,
                "automatic_ambiguous_reopens": len(automatic_reopens),
                "superseded_census_decisions": automatic_supersedes,
                "automatic_metadata_enrichments": len(enrichments),
            }
        )
        if not apply:
            return result
        imported_at = utc_now()
        backup_path = ""
        if existing:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.database.with_name(
                f"{self.database.stem}.pre-object-census-refresh-{stamp}.sqlite"
            )
            if backup.exists():
                raise KurtSchwarzReconciliationError(
                    f"object census refresh backup already exists: {backup}"
                )
            with closing(sqlite3.connect(self.database)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
            backup_path = str(backup)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            statement = (
                """INSERT INTO object_census_candidates (
                       candidate_id, queue_class, content_sha256, size_bytes,
                       filename, extension, width_px, height_px, final_path,
                       primary_source_path, source_paths_json,
                       metadata_matches_json, metadata_match_count,
                       metadata_summary, review_basis, review_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
                if not existing
                else """UPDATE object_census_candidates SET
                       queue_class=?, content_sha256=?, size_bytes=?, filename=?,
                       extension=?, width_px=?, height_px=?, final_path=?,
                       primary_source_path=?, source_paths_json=?,
                       metadata_matches_json=?, metadata_match_count=?,
                       metadata_summary=?, review_basis=?, review_json=?
                       WHERE candidate_id=?"""
            )
            values = [
                    (
                        row["candidate_id"], row["queue_class"],
                        row["content_sha256"], row["size_bytes"],
                        row["filename"], row["extension"], row["width_px"],
                        row["height_px"], row["final_path"],
                        row["primary_source_path"], row["source_paths_json"],
                        row["metadata_matches_json"],
                        row["metadata_match_count"], row["metadata_summary"],
                        row["review_basis"], row["review_json"],
                    )
                    for row in candidates
                ]
            if existing:
                values = [value[1:] + (value[0],) for value in values]
            connection.executemany(statement, values)
            connection.executemany(
                """INSERT INTO meta(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (
                    ("object_census_fingerprint", fingerprint),
                    ("object_census_path", str(census_csv)),
                    ("object_census_imported_at", imported_at),
                    ("object_census_import_actor", actor),
                ),
            )
            decided_at = utc_now()
            for row in automatic:
                current = latest_decisions.get(row["candidate_id"])
                if (
                    current
                    and current["disposition"] == "existing_object"
                    and current["target_record_key"] == row["target_record_key"]
                ):
                    continue
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'existing_object', ?, ?, ?, ?)""",
                    (
                        row["candidate_id"], row["target_record_key"], actor,
                        row["auto_decision_reason"], decided_at,
                    ),
                )
            for row in automatic_companions:
                current = latest_decisions.get(row["candidate_id"])
                if current and current["disposition"] == "same_new_candidate":
                    continue
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'same_new_candidate', NULL, ?, ?, ?)""",
                    (
                        row["candidate_id"], actor,
                        row["auto_decision_reason"], decided_at,
                    ),
                )
            for row in automatic_reopens:
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'defer', NULL, ?, ?, ?)""",
                    (
                        row["candidate_id"], actor,
                        "Broader comparison with all previously selected and "
                        "Redundant representations found matches to multiple "
                        "existing Kurt objects; reopen for curator review.",
                        decided_at,
                    ),
                )
            for row, field_key, value in enrichments:
                connection.execute(
                    """INSERT INTO metadata_decisions (
                           record_key, field_key, action, accepted_value,
                           actor, reason, decided_at
                       ) VALUES (?, ?, 'corrected', ?, ?, ?, ?)""",
                    (
                        row["target_record_key"], field_key, value, actor,
                        "Filled a previously empty field from metadata bound "
                        "to a unique visual match in the full-source census.",
                        decided_at,
                    ),
                )
            connection.commit()
        return {
            **result,
            "state": "applied",
            "imported_at": imported_at,
            "rollback_database": backup_path,
        }

    @staticmethod
    def _latest_object_census_decisions(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            row["candidate_id"]: dict(row)
            for row in connection.execute(
                """SELECT * FROM object_census_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id) FROM object_census_decisions
                     GROUP BY candidate_id
                   )"""
            )
        }

    @staticmethod
    def _latest_object_census_similarity_decisions(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        return {
            row["pair_id"]: dict(row)
            for row in connection.execute(
                """SELECT * FROM object_census_similarity_decisions
                   WHERE decision_id IN (
                     SELECT MAX(decision_id)
                     FROM object_census_similarity_decisions
                     GROUP BY pair_id
                   )"""
            )
        }

    def object_census_similarity_run(self) -> dict[str, Any] | None:
        runs = self.review_root / "object-census-similarity/runs"
        paths = sorted(runs.glob("*/similarity-pairs.json"), reverse=True)
        if not paths:
            return None
        path = paths[0]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KurtSchwarzReconciliationError(
                f"cannot read object-census similarity evidence: {path}"
            ) from error
        if (
            payload.get("format") not in {
                "iss-kurt-schwarz-census-similarity-v1",
                "iss-kurt-schwarz-census-similarity-v2",
            }
            or payload.get("deletion_authorized") is not False
            or not str(payload.get("run_fingerprint", "")).startswith("sha256:")
            or not isinstance(payload.get("pairs"), list)
        ):
            raise KurtSchwarzReconciliationError(
                f"invalid object-census similarity evidence: {path}"
            )
        payload["path"] = str(path)
        return payload

    def object_census_reference_matches(
        self, candidate_id: str = ""
    ) -> list[dict[str, Any]]:
        """Return whole-unit matches from census candidates to selected masters."""

        run = self.object_census_similarity_run()
        if run is None:
            return []
        raw_matches = run.get("reference_matches", [])
        if not isinstance(raw_matches, list):
            raise KurtSchwarzReconciliationError(
                "similarity reference evidence is not a list"
            )
        if candidate_id:
            raw_matches = [
                item
                for item in raw_matches
                if str(item.get("candidate_id", "")) == candidate_id
            ]
        if not raw_matches:
            return []
        candidates = {
            item["candidate_id"]: item for item in self.object_census_candidates()
        }
        with closing(self.connect(read_only=True)) as connection:
            latest_media = self._latest_media_decisions(connection)
            references = {
                str(row["candidate_id"]): dict(row)
                for row in connection.execute(
                    """SELECT media.*, object.current_inventory,
                              object.historical_inventory, object.current_title
                       FROM media_candidates AS media
                       JOIN objects AS object
                         ON object.record_key=media.record_key"""
                )
                if latest_media.get(str(row["candidate_id"]), {}).get(
                    "disposition"
                ) == "selected_master"
            }
        result = []
        seen = set()
        for raw in raw_matches:
            item = dict(raw)
            match_id = str(item.get("match_id", ""))
            census_id = str(item.get("candidate_id", ""))
            reference_id = str(item.get("reference_candidate_id", ""))
            reference = references.get(reference_id)
            if (
                not match_id
                or match_id in seen
                or census_id not in candidates
                or reference is None
                or str(item.get("reference_record_key", ""))
                != str(reference["record_key"])
            ):
                raise KurtSchwarzReconciliationError(
                    "similarity evidence contains an invalid master reference"
                )
            seen.add(match_id)
            item["candidate"] = candidates[census_id]
            item["reference"] = reference
            item["run_fingerprint"] = run["run_fingerprint"]
            result.append(item)
        return result

    def object_census_reference_match_counts(self) -> dict[str, int]:
        """Return lightweight per-candidate counts for the bounded queue page."""

        run = self.object_census_similarity_run()
        if run is None:
            return {}
        matches = run.get("reference_matches", [])
        if not isinstance(matches, list):
            raise KurtSchwarzReconciliationError(
                "similarity reference evidence is not a list"
            )
        counts: Counter[str] = Counter()
        for item in matches:
            candidate_id = str(item.get("candidate_id", ""))
            if not candidate_id:
                raise KurtSchwarzReconciliationError(
                    "similarity master reference lacks a census candidate"
                )
            counts[candidate_id] += 1
        return dict(counts)

    def object_census_similarity_pairs(self) -> list[dict[str, Any]]:
        run = self.object_census_similarity_run()
        if run is None:
            return []
        candidates = {
            item["candidate_id"]: item for item in self.object_census_candidates()
        }
        with closing(self.connect(read_only=True)) as connection:
            latest = self._latest_object_census_similarity_decisions(connection)
        result = []
        seen_pairs = set()
        for raw in run["pairs"]:
            item = dict(raw)
            pair = str(item.get("pair_id", ""))
            left = str(item.get("candidate_a_id", ""))
            right = str(item.get("candidate_b_id", ""))
            if (
                not pair
                or pair in seen_pairs
                or not left
                or not right
                or left == right
                or left not in candidates
                or right not in candidates
            ):
                raise KurtSchwarzReconciliationError(
                    "similarity evidence contains an invalid candidate pair"
                )
            seen_pairs.add(pair)
            item["candidate_a"] = candidates[left]
            item["candidate_b"] = candidates[right]
            item["decision"] = latest.get(pair)
            outcome = item["decision"]["outcome"] if item["decision"] else ""
            if outcome not in {"", "defer"}:
                item["review_state"] = "decided"
            elif any(
                item[key]["review_state"] != "pending"
                for key in ("candidate_a", "candidate_b")
            ):
                item["review_state"] = "superseded"
            else:
                item["review_state"] = "pending"
            item["run_fingerprint"] = run["run_fingerprint"]
            result.append(item)
        return result

    def object_census_similarity_groups(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for pair in self.object_census_similarity_pairs():
            group_id = str(pair["group_id"])
            group = grouped.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "run_fingerprint": pair["run_fingerprint"],
                    "pairs": [],
                    "candidates": {},
                },
            )
            group["pairs"].append(pair)
            for key in ("candidate_a", "candidate_b"):
                candidate = pair[key]
                group["candidates"][candidate["candidate_id"]] = candidate
        result = []
        for group in grouped.values():
            active_roles: dict[str, str] = {}
            for pair in group["pairs"]:
                decision = pair.get("decision") or {}
                if decision.get("outcome") != "same_photo":
                    continue
                active_roles[str(decision["primary_candidate_id"])] = "master"
                active_roles[str(decision["companion_candidate_id"])] = (
                    "redundant"
                )
            candidates = sorted(
                group["candidates"].values(),
                key=lambda item: (item["filename"].casefold(), item["candidate_id"]),
            )
            result.append(
                {
                    **group,
                    "candidates": candidates,
                    "active_roles": active_roles,
                    "pending_pair_count": sum(
                        pair["review_state"] == "pending"
                        for pair in group["pairs"]
                    ),
                }
            )
        return sorted(
            result,
            key=lambda group: (
                -len(group["candidates"]),
                group["group_id"],
            ),
        )

    def object_census_candidates(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            latest = self._latest_object_census_decisions(connection)
            similarity_latest = self._latest_object_census_similarity_decisions(
                connection
            )
            rows = connection.execute(
                """SELECT * FROM object_census_candidates
                   ORDER BY CASE queue_class
                     WHEN 'master_with_metadata' THEN 1
                     WHEN 'master_without_metadata' THEN 2 ELSE 3 END,
                     filename COLLATE NOCASE, candidate_id"""
            ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            review = json.loads(item["review_json"])
            for key in (
                "target_record_key",
                "target_inventory",
                "target_media_candidate_id",
                "related_candidate_id",
                "selected_metadata_basis",
                "auto_decision_reason",
            ):
                item[key] = str(review.get(key, ""))
            for key, default in (
                ("selected_metadata_json", "{}"),
                ("metadata_enrichment_json", "{}"),
                ("visual_match_json", "{}"),
            ):
                item[key] = str(review.get(key, default))
            item["selected_metadata"] = json.loads(
                item["selected_metadata_json"]
            )
            item["metadata_enrichment"] = json.loads(
                item["metadata_enrichment_json"]
            )
            item["visual_match"] = json.loads(item["visual_match_json"])
            item["source_paths"] = json.loads(item["source_paths_json"])
            item["metadata_matches"] = json.loads(
                item["metadata_matches_json"]
            )
            item["decision"] = latest.get(item["candidate_id"])
            disposition = (
                item["decision"]["disposition"] if item["decision"] else ""
            )
            similarity_primary = next(
                (
                    str(decision["primary_candidate_id"])
                    for decision in similarity_latest.values()
                    if decision["outcome"] == "same_photo"
                    and decision["companion_candidate_id"] == item["candidate_id"]
                ),
                "",
            )
            if disposition == "same_new_candidate" and similarity_primary:
                item["queue_class"] = "new_local_companion_reconciled"
                item["related_candidate_id"] = similarity_primary
            if disposition == "wrong_match" and item["queue_class"] in {
                "existing_object_reconciled", "surrogate_master_review",
                "new_local_companion_reconciled",
            }:
                item["queue_class"] = "new_local_review"
                item["target_record_key"] = ""
                item["target_inventory"] = ""
                item["target_media_candidate_id"] = ""
                item["related_candidate_id"] = ""
                item["selected_metadata"] = {}
                item["selected_metadata_basis"] = "existing_visual_match_rejected"
            item["review_state"] = (
                "pending"
                if not disposition or disposition in {"defer", "wrong_match"}
                else "decided"
            )
            result.append(item)
        return result

    def object_census_queue_candidates(self) -> list[dict[str, Any]]:
        """Return only fields needed by the bounded census list page."""

        with closing(self.connect(read_only=True)) as connection:
            latest = self._latest_object_census_decisions(connection)
            similarity_latest = self._latest_object_census_similarity_decisions(
                connection
            )
            rows = connection.execute(
                """SELECT candidate_id, queue_class, size_bytes, filename,
                          width_px, height_px, primary_source_path,
                          source_paths_json, metadata_summary,
                          metadata_matches_json
                   FROM object_census_candidates
                   ORDER BY CASE queue_class
                     WHEN 'master_with_metadata' THEN 1
                     WHEN 'master_without_metadata' THEN 2 ELSE 3 END,
                     filename COLLATE NOCASE, candidate_id"""
            ).fetchall()
        similarity_companions = {
            str(decision["companion_candidate_id"]): str(
                decision["primary_candidate_id"]
            )
            for decision in similarity_latest.values()
            if decision["outcome"] == "same_photo"
        }
        result = []
        for raw in rows:
            item = dict(raw)
            item["source_occurrence_count"] = len(
                json.loads(item.pop("source_paths_json"))
            )
            item["decision"] = latest.get(item["candidate_id"])
            decision = item["decision"] or {}
            curated_summary = " · ".join(
                str(decision.get(field) or "").strip()
                for field in (
                    "curated_title",
                    "curated_date_text",
                    "curated_description",
                    "curated_tags",
                )
                if str(decision.get(field) or "").strip()
            )
            if curated_summary:
                item["metadata_summary"] = curated_summary
            disposition = (
                decision["disposition"] if decision else ""
            )
            if disposition == "same_new_candidate" and item[
                "candidate_id"
            ] in similarity_companions:
                item["queue_class"] = "new_local_companion_reconciled"
            if disposition == "wrong_match" and item["queue_class"] in {
                "existing_object_reconciled",
                "surrogate_master_review",
                "new_local_companion_reconciled",
            }:
                item["queue_class"] = "new_local_review"
            item["review_state"] = (
                "pending"
                if not disposition or disposition in {"defer", "wrong_match"}
                else "decided"
            )
            result.append(item)
        return result

    def object_census_queue_summary(
        self, candidates: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        candidates = candidates or self.object_census_queue_candidates()
        return {
            "object_census_queues": dict(
                Counter(item["queue_class"] for item in candidates)
            ),
            "object_census_pending": sum(
                item["review_state"] == "pending" for item in candidates
            ),
        }

    def document_queue_summary(
        self, documents: list[dict[str, Any]] | None = None
    ) -> dict[str, int]:
        documents = documents or self.documents()
        with closing(self.connect(read_only=True)) as connection:
            decisions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM document_decisions"
                ).fetchone()[0]
            )
        return {
            "document_candidates": len(documents),
            "document_review_pending": sum(
                item["review_state"] == "pending" for item in documents
            ),
            "document_decisions": decisions,
        }

    def object_census_candidate(self, candidate_id: str) -> dict[str, Any]:
        item = next(
            (
                row
                for row in self.object_census_candidates()
                if row["candidate_id"] == candidate_id
            ),
            None,
        )
        if item is None:
            raise KurtSchwarzReconciliationError(
                f"unknown object census candidate: {candidate_id}"
            )
        return item

    @staticmethod
    def _cross_unit_hold_json(
        *,
        target_scope: str,
        target_subunit: str,
        group_key: str,
        provisional_title: str,
        provisional_date_text: str,
        object_type: str,
        component_sequence: int | str,
        component_relationship: str,
    ) -> str:
        target_scope = target_scope.strip().casefold()
        target_subunit = target_subunit.strip().casefold() or "unresolved"
        group_key = group_key.strip().casefold()
        provisional_title = re.sub(r"\s+", " ", provisional_title).strip()
        provisional_date_text = re.sub(
            r"\s+", " ", provisional_date_text
        ).strip()
        object_type = object_type.strip().casefold()
        component_relationship = component_relationship.strip().casefold()
        key_pattern = re.compile(r"[a-z0-9][a-z0-9._:-]*\Z")
        for label, value, maximum in (
            ("target scope", target_scope, 80),
            ("target subunit", target_subunit, 120),
            ("hold group key", group_key, 160),
            ("object type", object_type, 80),
        ):
            if not value or len(value) > maximum or not key_pattern.fullmatch(value):
                raise KurtSchwarzReconciliationError(
                    f"invalid cross-unit {label}: {value or '(empty)'}"
                )
        if not provisional_title or len(provisional_title) > 1_000:
            raise KurtSchwarzReconciliationError(
                "cross-unit provisional title is required and limited to "
                "1,000 characters"
            )
        if len(provisional_date_text) > 250:
            raise KurtSchwarzReconciliationError(
                "cross-unit provisional date exceeds 250 characters"
            )
        try:
            sequence = int(component_sequence)
        except (TypeError, ValueError) as error:
            raise KurtSchwarzReconciliationError(
                "cross-unit component sequence must be an integer"
            ) from error
        if sequence < 1:
            raise KurtSchwarzReconciliationError(
                "cross-unit component sequence must be positive"
            )
        if component_relationship not in CROSS_UNIT_HOLD_RELATIONSHIPS:
            raise KurtSchwarzReconciliationError(
                "unsupported cross-unit component relationship: "
                f"{component_relationship}"
            )
        return json.dumps(
            {
                "format": "iss-object-census-cross-unit-hold-v1",
                "status": "held_for_future_unit",
                "target_scope": target_scope,
                "target_subunit": target_subunit,
                "group_key": group_key,
                "provisional_title": provisional_title,
                "provisional_date_text": provisional_date_text,
                "object_type": object_type,
                "component_sequence": sequence,
                "component_relationship": component_relationship,
                "deletion_authorized": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def save_object_census_decision(
        self,
        *,
        candidate_id: str,
        disposition: str,
        actor: str,
        target_reference: str = "",
        reason: str = "",
        curated_title: str | None = None,
        curated_date_text: str | None = None,
        curated_description: str | None = None,
        curated_tags: str | None = None,
        cross_unit_target_scope: str = "",
        cross_unit_target_subunit: str = "",
        cross_unit_group_key: str = "",
        cross_unit_provisional_title: str = "",
        cross_unit_provisional_date_text: str = "",
        cross_unit_object_type: str = "",
        cross_unit_component_sequence: int | str = "",
        cross_unit_component_relationship: str = "",
    ) -> dict[str, Any]:
        disposition = disposition.strip()
        actor = actor.strip()
        reason = reason.strip()
        if disposition not in OBJECT_CENSUS_DISPOSITIONS:
            raise KurtSchwarzReconciliationError(
                f"unsupported object census disposition: {disposition}"
            )
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        candidate = self.object_census_candidate(candidate_id)
        if curated_title is not None:
            curated_title = re.sub(r"\s+", " ", curated_title).strip()
            if len(curated_title) > 1_000:
                raise KurtSchwarzReconciliationError(
                    "curated title exceeds 1,000 characters"
                )
        if curated_date_text is not None:
            curated_date_text = re.sub(r"\s+", " ", curated_date_text).strip()
            if len(curated_date_text) > 250:
                raise KurtSchwarzReconciliationError(
                    "curated date exceeds 250 characters"
                )
        if curated_description is not None:
            curated_description = curated_description.replace("\r\n", "\n").strip()
            if len(curated_description) > 10_000:
                raise KurtSchwarzReconciliationError(
                    "curated description exceeds 10,000 characters"
                )
        if curated_tags is not None:
            tags = []
            seen_tags = set()
            for value in re.split(r"[;|\n]+", curated_tags):
                display = re.sub(r"\s+", " ", value).strip(" .,;")
                normalized = display.casefold()
                if display and normalized not in seen_tags:
                    seen_tags.add(normalized)
                    tags.append(display)
            curated_tags = "; ".join(tags)
            if len(tags) > 100 or len(curated_tags) > 5_000:
                raise KurtSchwarzReconciliationError(
                    "curated keywords exceed 100 tags or 5,000 characters"
                )
        if (
            curated_title is not None
            or curated_date_text is not None
            or curated_description is not None
            or curated_tags is not None
        ) and candidate["queue_class"] != "new_local_review":
            raise KurtSchwarzReconciliationError(
                "curated metadata is only valid for a new-local candidate"
            )
        target_record_key = None
        cross_unit_hold_json = None
        if disposition == "existing_object":
            target_record_key = self.resolve_record_reference(target_reference)
        elif disposition in {"surrogate_master", "surrogate_keep"}:
            if candidate["queue_class"] != "surrogate_master_review":
                raise KurtSchwarzReconciliationError(
                    "surrogate decisions require a surrogate comparison candidate"
                )
            target_record_key = candidate["target_record_key"]
            if not target_record_key:
                raise KurtSchwarzReconciliationError(
                    "surrogate comparison has no bound existing object"
                )
        elif disposition in {
            "new_local_metadata_right",
            "new_local_metadata_wrong",
        } and candidate["queue_class"] != "new_local_review":
            raise KurtSchwarzReconciliationError(
                "new-local metadata decisions require a new-local candidate"
            )
        elif disposition == "cross_unit_hold":
            if candidate["queue_class"] != "new_local_review":
                raise KurtSchwarzReconciliationError(
                    "cross-unit holds require a new-local candidate"
                )
            if not reason:
                raise KurtSchwarzReconciliationError(
                    "cross-unit holds require an identification reason"
                )
            cross_unit_hold_json = self._cross_unit_hold_json(
                target_scope=cross_unit_target_scope,
                target_subunit=cross_unit_target_subunit,
                group_key=cross_unit_group_key,
                provisional_title=cross_unit_provisional_title,
                provisional_date_text=cross_unit_provisional_date_text,
                object_type=cross_unit_object_type,
                component_sequence=cross_unit_component_sequence,
                component_relationship=cross_unit_component_relationship,
            )
        elif target_reference.strip():
            raise KurtSchwarzReconciliationError(
                "an existing-object target is only valid for Existing object"
            )
        if disposition != "cross_unit_hold" and any(
            str(value).strip()
            for value in (
                cross_unit_target_scope,
                cross_unit_target_subunit,
                cross_unit_group_key,
                cross_unit_provisional_title,
                cross_unit_provisional_date_text,
                cross_unit_object_type,
                cross_unit_component_sequence,
                cross_unit_component_relationship,
            )
        ):
            raise KurtSchwarzReconciliationError(
                "cross-unit hold details require the cross-unit disposition"
            )
        decided_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO object_census_decisions (
                       candidate_id, disposition, target_record_key,
                       actor, reason, decided_at, curated_title,
                       curated_date_text, curated_description, curated_tags,
                       cross_unit_hold_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    disposition,
                    target_record_key,
                    actor,
                    reason,
                    decided_at,
                    curated_title,
                    curated_date_text,
                    curated_description,
                    curated_tags,
                    cross_unit_hold_json,
                ),
            )
            decision_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            connection.commit()
        return {
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "disposition": disposition,
            "target_record_key": target_record_key or "",
            "curated_title": curated_title,
            "curated_date_text": curated_date_text,
            "curated_description": curated_description,
            "curated_tags": curated_tags,
            "cross_unit_hold": (
                json.loads(cross_unit_hold_json)
                if cross_unit_hold_json else None
            ),
            "decided_at": decided_at,
        }

    def save_object_census_similarity_decision(
        self,
        *,
        pair_id: str,
        outcome: str,
        actor: str,
        run_fingerprint: str,
        primary_candidate_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        pair_id = pair_id.strip()
        outcome = outcome.strip()
        actor = actor.strip()
        run_fingerprint = run_fingerprint.strip()
        primary_candidate_id = primary_candidate_id.strip()
        reason = reason.strip()
        if outcome not in OBJECT_CENSUS_SIMILARITY_OUTCOMES:
            raise KurtSchwarzReconciliationError(
                f"unsupported similarity outcome: {outcome}"
            )
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        pairs = {
            item["pair_id"]: item for item in self.object_census_similarity_pairs()
        }
        pair = pairs.get(pair_id)
        if pair is None:
            raise KurtSchwarzReconciliationError(
                f"unknown object-census similarity pair: {pair_id}"
            )
        if run_fingerprint != pair["run_fingerprint"]:
            raise KurtSchwarzReconciliationError(
                "similarity evidence changed; reload the duplicate review"
            )
        candidate_ids = tuple(
            sorted((pair["candidate_a_id"], pair["candidate_b_id"]))
        )
        primary: str | None = None
        companion: str | None = None
        if outcome == "same_photo":
            if primary_candidate_id not in candidate_ids:
                raise KurtSchwarzReconciliationError(
                    "same-photo review requires one displayed primary candidate"
                )
            primary = primary_candidate_id
            companion = next(
                candidate for candidate in candidate_ids if candidate != primary
            )
            current_by_id = {
                pair["candidate_a"]["candidate_id"]: pair["candidate_a"],
                pair["candidate_b"]["candidate_id"]: pair["candidate_b"],
            }
            previous_pair_decision = pair.get("decision")
            if previous_pair_decision and previous_pair_decision["outcome"] == "same_photo":
                if previous_pair_decision["primary_candidate_id"] != primary:
                    raise KurtSchwarzReconciliationError(
                        "reopen the existing same-photo decision before changing "
                        "its retained primary"
                    )
            elif any(
                current_by_id[candidate]["queue_class"] != "new_local_review"
                or current_by_id[candidate]["review_state"] != "pending"
                for candidate in candidate_ids
            ):
                raise KurtSchwarzReconciliationError(
                    "same-photo decisions require two pending new-local candidates"
                )
        elif primary_candidate_id:
            raise KurtSchwarzReconciliationError(
                "a primary candidate is only valid for Same photograph"
            )
        decided_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            raw_candidates = {
                str(row[0])
                for row in connection.execute(
                    "SELECT candidate_id FROM object_census_candidates "
                    "WHERE candidate_id IN ("
                    + ",".join("?" for _ in candidate_ids)
                    + ")",
                    candidate_ids,
                )
            }
            if raw_candidates != set(candidate_ids):
                raise KurtSchwarzReconciliationError(
                    "similarity candidates drifted from the census"
                )
            latest_similarity = self._latest_object_census_similarity_decisions(
                connection
            )
            previous = latest_similarity.get(pair_id)
            active_same = [
                decision
                for decision in latest_similarity.values()
                if decision["outcome"] == "same_photo"
                and decision["pair_id"] != pair_id
            ]
            if outcome == "same_photo" and companion and primary:
                active_companions = {
                    str(decision["companion_candidate_id"])
                    for decision in active_same
                }
                active_primaries = {
                    str(decision["primary_candidate_id"])
                    for decision in active_same
                }
                if primary in active_companions:
                    raise KurtSchwarzReconciliationError(
                        "the selected primary is already a companion; choose its "
                        "retained primary instead"
                    )
                if companion in active_companions or companion in active_primaries:
                    raise KurtSchwarzReconciliationError(
                        "the selected companion already belongs to another confirmed "
                        "similarity relationship"
                    )
            decision_reason = reason or {
                "same_photo": "Curator confirmed the same photograph from similarity evidence.",
                "different_photo": "Curator confirmed that similarity does not establish the same photograph.",
                "defer": "Similarity relationship left for later review.",
            }[outcome]
            connection.execute(
                """INSERT INTO object_census_similarity_decisions (
                       pair_id, candidate_a_id, candidate_b_id, outcome,
                       primary_candidate_id, companion_candidate_id,
                       run_fingerprint, actor, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pair_id,
                    candidate_ids[0],
                    candidate_ids[1],
                    outcome,
                    primary,
                    companion,
                    run_fingerprint,
                    actor,
                    decision_reason,
                    decided_at,
                ),
            )
            similarity_decision_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            census_decision_id = None
            if outcome == "same_photo" and companion:
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'same_new_candidate', NULL, ?, ?, ?)""",
                    (
                        companion,
                        actor,
                        f"Similarity pair {pair_id}: {decision_reason}",
                        decided_at,
                    ),
                )
                census_decision_id = int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
            elif previous and previous["outcome"] == "same_photo":
                previous_companion = str(previous["companion_candidate_id"])
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'wrong_match', NULL, ?, ?, ?)""",
                    (
                        previous_companion,
                        actor,
                        f"Similarity pair {pair_id} was corrected to {outcome}.",
                        decided_at,
                    ),
                )
                census_decision_id = int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
            connection.commit()
        return {
            "decision_id": similarity_decision_id,
            "census_decision_id": census_decision_id,
            "pair_id": pair_id,
            "outcome": outcome,
            "primary_candidate_id": primary or "",
            "companion_candidate_id": companion or "",
            "decided_at": decided_at,
        }

    def save_object_census_similarity_group_decision(
        self,
        *,
        group_id: str,
        master_candidate_id: str,
        redundant_candidate_ids: list[str],
        actor: str,
        run_fingerprint: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Replace one discovery group's active roles in one transaction."""

        group_id = group_id.strip()
        master_candidate_id = master_candidate_id.strip()
        redundant_candidate_ids = sorted(
            {candidate.strip() for candidate in redundant_candidate_ids if candidate.strip()}
        )
        actor = actor.strip()
        run_fingerprint = run_fingerprint.strip()
        reason = reason.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        if not master_candidate_id or not redundant_candidate_ids:
            raise KurtSchwarzReconciliationError(
                "group review requires one Master and at least one Redundant candidate"
            )
        if master_candidate_id in redundant_candidate_ids:
            raise KurtSchwarzReconciliationError(
                "the group Master cannot also be Redundant"
            )
        group = next(
            (
                item
                for item in self.object_census_similarity_groups()
                if item["group_id"] == group_id
            ),
            None,
        )
        if group is None:
            raise KurtSchwarzReconciliationError(
                f"unknown object-census similarity group: {group_id}"
            )
        if run_fingerprint != group["run_fingerprint"]:
            raise KurtSchwarzReconciliationError(
                "similarity evidence changed; reload the group review"
            )
        members = {
            item["candidate_id"]: item for item in group["candidates"]
        }
        requested = {master_candidate_id, *redundant_candidate_ids}
        if not requested.issubset(members):
            raise KurtSchwarzReconciliationError(
                "group decision contains a candidate outside the displayed group"
            )
        edge_by_members = {
            frozenset((pair["candidate_a_id"], pair["candidate_b_id"])): pair
            for pair in group["pairs"]
        }
        desired: dict[str, tuple[str, str]] = {}
        for companion in redundant_candidate_ids:
            pair = edge_by_members.get(
                frozenset((master_candidate_id, companion))
            )
            if pair is None:
                raise KurtSchwarzReconciliationError(
                    "every Redundant selection requires a direct reviewed edge "
                    "to the selected Master"
                )
            desired[pair["pair_id"]] = (master_candidate_id, companion)
        group_pair_ids = {pair["pair_id"] for pair in group["pairs"]}
        decided_at = utc_now()
        decision_reason = reason or (
            f"Curator selected {members[master_candidate_id]['filename']} as "
            f"the group Master and {len(redundant_candidate_ids)} companion "
            "representations after side-by-side review."
        )
        similarity_decision_ids: list[int] = []
        census_decision_ids: list[int] = []
        reopened_pair_ids: list[str] = []
        reopened_candidate_ids: list[str] = []
        unchanged_pair_ids: list[str] = []
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            raw_candidates = {
                str(row["candidate_id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM object_census_candidates WHERE candidate_id IN ("
                    + ",".join("?" for _ in members)
                    + ")",
                    tuple(members),
                )
            }
            if set(raw_candidates) != set(members) or any(
                row["queue_class"] != "new_local_review"
                for row in raw_candidates.values()
            ):
                raise KurtSchwarzReconciliationError(
                    "similarity group drifted from the new-local census"
                )
            latest_census = self._latest_object_census_decisions(connection)
            latest_similarity = self._latest_object_census_similarity_decisions(
                connection
            )
            permitted = {"", "defer", "wrong_match", "same_new_candidate"}
            for candidate_id in requested:
                latest = latest_census.get(candidate_id)
                disposition = str(latest["disposition"]) if latest else ""
                if disposition not in permitted:
                    raise KurtSchwarzReconciliationError(
                        f"candidate {candidate_id} already has an independent "
                        f"census decision: {disposition}"
                    )
            active_group = {
                pair_id: decision
                for pair_id, decision in latest_similarity.items()
                if pair_id in group_pair_ids
                and decision["outcome"] == "same_photo"
            }
            for pair_id, decision in active_group.items():
                expected = desired.get(pair_id)
                if expected == (
                    str(decision["primary_candidate_id"]),
                    str(decision["companion_candidate_id"]),
                ):
                    unchanged_pair_ids.append(pair_id)
                    continue
                previous_companion = str(decision["companion_candidate_id"])
                previous_latest = latest_census.get(previous_companion)
                if (
                    previous_latest
                    and previous_latest["disposition"]
                    not in {"same_new_candidate", "wrong_match", "defer"}
                ):
                    raise KurtSchwarzReconciliationError(
                        "an existing companion has a later independent census "
                        "decision; correct it separately"
                    )
                connection.execute(
                    """INSERT INTO object_census_similarity_decisions (
                           pair_id, candidate_a_id, candidate_b_id, outcome,
                           primary_candidate_id, companion_candidate_id,
                           run_fingerprint, actor, reason, decided_at
                       ) VALUES (?, ?, ?, 'defer', NULL, NULL, ?, ?, ?, ?)""",
                    (
                        pair_id,
                        decision["candidate_a_id"],
                        decision["candidate_b_id"],
                        run_fingerprint,
                        actor,
                        f"Group {group_id} roles replaced: {decision_reason}",
                        decided_at,
                    ),
                )
                similarity_decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
                reopened_pair_ids.append(pair_id)
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'wrong_match', NULL, ?, ?, ?)""",
                    (
                        previous_companion,
                        actor,
                        f"Similarity group {group_id} roles were replaced; "
                        "candidate reopened for review.",
                        decided_at,
                    ),
                )
                census_decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
                reopened_candidate_ids.append(previous_companion)
            for pair_id, (primary, companion) in desired.items():
                if pair_id in unchanged_pair_ids:
                    continue
                pair = next(
                    item for item in group["pairs"] if item["pair_id"] == pair_id
                )
                candidate_a_id, candidate_b_id = sorted(
                    (pair["candidate_a_id"], pair["candidate_b_id"])
                )
                connection.execute(
                    """INSERT INTO object_census_similarity_decisions (
                           pair_id, candidate_a_id, candidate_b_id, outcome,
                           primary_candidate_id, companion_candidate_id,
                           run_fingerprint, actor, reason, decided_at
                       ) VALUES (?, ?, ?, 'same_photo', ?, ?, ?, ?, ?, ?)""",
                    (
                        pair_id,
                        candidate_a_id,
                        candidate_b_id,
                        primary,
                        companion,
                        run_fingerprint,
                        actor,
                        decision_reason,
                        decided_at,
                    ),
                )
                similarity_decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'same_new_candidate', NULL, ?, ?, ?)""",
                    (
                        companion,
                        actor,
                        f"Similarity group {group_id}: {decision_reason}",
                        decided_at,
                    ),
                )
                census_decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
            connection.commit()
        return {
            "group_id": group_id,
            "run_fingerprint": run_fingerprint,
            "master_candidate_id": master_candidate_id,
            "master_filename": members[master_candidate_id]["filename"],
            "redundant_candidate_ids": redundant_candidate_ids,
            "redundant_filenames": [
                members[candidate_id]["filename"]
                for candidate_id in redundant_candidate_ids
            ],
            "reopened_pair_ids": reopened_pair_ids,
            "reopened_candidate_ids": reopened_candidate_ids,
            "unchanged_pair_ids": unchanged_pair_ids,
            "similarity_decision_ids": similarity_decision_ids,
            "census_decision_ids": census_decision_ids,
            "actor": actor,
            "reason": decision_reason,
            "decided_at": decided_at,
            "deletion_authorized": False,
        }

    def save_manual_object_census_duplicate_decision(
        self,
        *,
        companion_candidate_id: str,
        retained_candidate_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Mark one census candidate Redundant, optionally linking its Master."""

        companion_candidate_id = companion_candidate_id.strip()
        retained_candidate_id = retained_candidate_id.strip()
        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        if retained_candidate_id and companion_candidate_id == retained_candidate_id:
            raise KurtSchwarzReconciliationError(
                "a Redundant candidate cannot retain itself as Master"
            )
        companion = self.object_census_candidate(companion_candidate_id)
        retained = (
            self.object_census_candidate(retained_candidate_id)
            if retained_candidate_id
            else None
        )
        for label, candidate in (("Redundant", companion),):
            if candidate["queue_class"] != "new_local_review":
                raise KurtSchwarzReconciliationError(
                    f"the {label} candidate is not an independent new-local candidate"
                )
            disposition = (candidate.get("decision") or {}).get(
                "disposition", ""
            )
            if disposition not in {
                "",
                "defer",
                "wrong_match",
                "new_local_object",
                "new_local_metadata_right",
                "new_local_metadata_wrong",
                "redundant",
            }:
                raise KurtSchwarzReconciliationError(
                    f"the {label} candidate has an incompatible decision: "
                    f"{disposition}"
                )
        if retained:
            if retained["queue_class"] != "new_local_review":
                raise KurtSchwarzReconciliationError(
                    "the retained candidate is not an independent new-local candidate"
                )
            retained_disposition = (retained.get("decision") or {}).get(
                "disposition", ""
            )
            if retained_disposition not in {
                "",
                "defer",
                "wrong_match",
                "new_local_object",
                "new_local_metadata_right",
                "new_local_metadata_wrong",
            }:
                raise KurtSchwarzReconciliationError(
                    "the retained candidate has an incompatible decision: "
                    f"{retained_disposition}"
                )

        candidate_ids = tuple(
            sorted(
                candidate_id
                for candidate_id in (
                    companion_candidate_id,
                    retained_candidate_id,
                )
                if candidate_id
            )
        )
        pair_id = (
            "KSMANUAL-"
            + hashlib.sha256("\0".join(candidate_ids).encode())
            .hexdigest()[:24]
            .upper()
            if retained_candidate_id
            else ""
        )
        run_fingerprint = "manual-curator-duplicate-v1"
        decided_at = utc_now()
        similarity_decision_id = None
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            raw_candidates = {
                str(row[0])
                for row in connection.execute(
                    "SELECT candidate_id FROM object_census_candidates "
                    "WHERE candidate_id IN ("
                    + ",".join("?" for _ in candidate_ids)
                    + ")",
                    candidate_ids,
                )
            }
            if raw_candidates != set(candidate_ids):
                raise KurtSchwarzReconciliationError(
                    "manual duplicate candidates drifted from the census"
                )
            if retained_candidate_id:
                latest_similarity = self._latest_object_census_similarity_decisions(
                    connection
                )
                previous = latest_similarity.get(pair_id)
                if previous and previous["outcome"] == "same_photo":
                    raise KurtSchwarzReconciliationError(
                        "this direct duplicate relationship is already recorded"
                    )
                active_same = [
                    decision
                    for decision in latest_similarity.values()
                    if decision["outcome"] == "same_photo"
                ]
                active_companions = {
                    str(decision["companion_candidate_id"])
                    for decision in active_same
                }
                active_primaries = {
                    str(decision["primary_candidate_id"])
                    for decision in active_same
                }
                if retained_candidate_id in active_companions:
                    raise KurtSchwarzReconciliationError(
                        "the retained candidate is already Redundant; use its retained "
                        "Master instead"
                    )
                if (
                    companion_candidate_id in active_companions
                    or companion_candidate_id in active_primaries
                ):
                    raise KurtSchwarzReconciliationError(
                        "the selected Redundant candidate already belongs to another "
                        "confirmed duplicate relationship"
                    )
                connection.execute(
                    """INSERT INTO object_census_similarity_decisions (
                           pair_id, candidate_a_id, candidate_b_id, outcome,
                           primary_candidate_id, companion_candidate_id,
                           run_fingerprint, actor, reason, decided_at
                       ) VALUES (?, ?, ?, 'same_photo', ?, ?, ?, ?, ?, ?)""",
                    (
                        pair_id,
                        candidate_ids[0],
                        candidate_ids[1],
                        retained_candidate_id,
                        companion_candidate_id,
                        run_fingerprint,
                        actor,
                        reason,
                        decided_at,
                    ),
                )
                similarity_decision_id = int(
                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
            disposition = (
                "same_new_candidate" if retained_candidate_id else "redundant"
            )
            decision_reason = reason
            if retained_candidate_id:
                decision_reason = f"Manual duplicate {pair_id}"
                if reason:
                    decision_reason += f": {reason}"
            connection.execute(
                """INSERT INTO object_census_decisions (
                       candidate_id, disposition, target_record_key,
                       actor, reason, decided_at
                   ) VALUES (?, ?, NULL, ?, ?, ?)""",
                (
                    companion_candidate_id,
                    disposition,
                    actor,
                    decision_reason,
                    decided_at,
                ),
            )
            census_decision_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            connection.commit()
        return {
            "pair_id": pair_id,
            "run_fingerprint": run_fingerprint,
            "similarity_decision_id": similarity_decision_id,
            "census_decision_id": census_decision_id,
            "retained_candidate_id": retained_candidate_id,
            "retained_filename": retained["filename"] if retained else "",
            "relationship_recorded": bool(retained_candidate_id),
            "companion_candidate_id": companion_candidate_id,
            "companion_filename": companion["filename"],
            "companion_source_paths": companion["source_paths"],
            "actor": actor,
            "reason": reason,
            "decided_at": decided_at,
            "deletion_authorized": False,
        }

    def assign_object_census_similarity_group_to_existing_object(
        self,
        *,
        group_id: str,
        target_reference: str,
        actor: str,
        run_fingerprint: str,
        reason: str,
    ) -> dict[str, Any]:
        """Attach one confirmed Master/Redundant set beneath an existing object."""

        group_id = group_id.strip()
        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        if not reason:
            raise KurtSchwarzReconciliationError(
                "moving a duplicate group requires a curator reason"
            )
        target_record_key = self.resolve_record_reference(target_reference)
        group = next(
            (
                item
                for item in self.object_census_similarity_groups()
                if item["group_id"] == group_id
            ),
            None,
        )
        if group is None:
            raise KurtSchwarzReconciliationError(
                f"unknown object-census similarity group: {group_id}"
            )
        if run_fingerprint.strip() != group["run_fingerprint"]:
            raise KurtSchwarzReconciliationError(
                "similarity evidence changed; reload the group review"
            )
        active_roles = group["active_roles"]
        masters = [key for key, role in active_roles.items() if role == "master"]
        companions = [
            key for key, role in active_roles.items() if role == "redundant"
        ]
        if len(masters) != 1 or not companions:
            raise KurtSchwarzReconciliationError(
                "first confirm exactly one group Master and at least one "
                "Redundant copy"
            )
        candidate_ids = sorted({masters[0], *companions})
        decided_at = utc_now()
        decision_ids = []
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = self._latest_object_census_decisions(connection)
            rows = {
                str(row[0])
                for row in connection.execute(
                    "SELECT candidate_id FROM object_census_candidates WHERE "
                    "candidate_id IN ("
                    + ",".join("?" for _ in candidate_ids)
                    + ")",
                    tuple(candidate_ids),
                )
            }
            if rows != set(candidate_ids):
                raise KurtSchwarzReconciliationError(
                    "duplicate group drifted from the object census"
                )
            for candidate_id in candidate_ids:
                current = latest.get(candidate_id)
                disposition = str(current["disposition"]) if current else ""
                if disposition == "existing_object" and str(
                    current["target_record_key"]
                ) == target_record_key:
                    continue
                if disposition not in {
                    "",
                    "defer",
                    "wrong_match",
                    "same_new_candidate",
                }:
                    raise KurtSchwarzReconciliationError(
                        f"candidate {candidate_id} has an independent census "
                        f"decision: {disposition}"
                    )
                connection.execute(
                    """INSERT INTO object_census_decisions (
                           candidate_id, disposition, target_record_key,
                           actor, reason, decided_at
                       ) VALUES (?, 'existing_object', ?, ?, ?, ?)""",
                    (
                        candidate_id,
                        target_record_key,
                        actor,
                        f"Similarity group {group_id} assigned to existing "
                        f"object: {reason}",
                        decided_at,
                    ),
                )
                decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
            connection.commit()
        return {
            "group_id": group_id,
            "target_record_key": target_record_key,
            "master_candidate_id": masters[0],
            "redundant_candidate_ids": sorted(companions),
            "candidate_ids": candidate_ids,
            "census_decision_ids": decision_ids,
            "actor": actor,
            "reason": reason,
            "decided_at": decided_at,
            "deletion_authorized": False,
        }

    def write_object_census_group_assignment_receipt(
        self, result: dict[str, Any]
    ) -> Path | None:
        if not result.get("census_decision_ids"):
            return None
        destination = (
            self.review_root
            / "object-census-similarity/group-assignments"
            / (
                f"group-assignment-{int(result['census_decision_ids'][0]):06d}-"
                f"{result['group_id']}.json"
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "iss-kurt-schwarz-census-group-assignment-v1",
            **result,
            "published_unit_changed": False,
            "source_files_changed": False,
            "identifiers_assigned": False,
        }
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return destination

    def write_object_census_similarity_group_receipt(
        self,
        result: dict[str, Any],
        *,
        rollback_database: str = "",
    ) -> Path | None:
        if not result.get("similarity_decision_ids"):
            return None
        destination = (
            self.review_root
            / "object-census-similarity/group-decisions"
            / (
                f"group-decision-{int(result['similarity_decision_ids'][0]):06d}-"
                f"{result['group_id']}.json"
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "iss-kurt-schwarz-census-similarity-group-decision-v1",
            **result,
            "rollback_database": rollback_database,
            "published_unit_changed": False,
            "source_files_changed": False,
            "identifiers_assigned": False,
        }
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return destination

    def write_manual_object_census_duplicate_receipt(
        self, result: dict[str, Any]
    ) -> Path:
        receipt_id = result.get("similarity_decision_id") or result[
            "census_decision_id"
        ]
        destination = (
            self.review_root
            / "object-census-similarity/group-decisions"
            / (
                "manual-duplicate-"
                f"{int(receipt_id):06d}.json"
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "iss-kurt-schwarz-census-manual-duplicate-v1",
            **result,
            "disposition": "Redundant",
            "source_disposition_changed": False,
            "drain_list_changed": False,
            "drain_evidence_preserved": True,
            "published_unit_changed": False,
            "source_files_changed": False,
            "identifiers_assigned": False,
            "deletion_authorized": False,
        }
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return destination

    def save_object_census_cross_unit_hold(
        self,
        *,
        candidate_ids: list[str],
        target_scope: str,
        target_subunit: str,
        group_key: str,
        provisional_title: str,
        provisional_date_text: str,
        object_type: str,
        actor: str,
        reason: str,
        receipt_path: Path,
        evidence_files: list[Path] | None = None,
        component_relationships: dict[str, str] | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Hold an ordered candidate group for another unit without accession."""

        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        if not reason:
            raise KurtSchwarzReconciliationError(
                "cross-unit holds require an identification reason"
            )
        candidate_ids = [value.strip() for value in candidate_ids if value.strip()]
        if not candidate_ids:
            raise KurtSchwarzReconciliationError(
                "cross-unit hold requires at least one candidate"
            )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise KurtSchwarzReconciliationError(
                "cross-unit hold repeats a candidate"
            )
        relationships = component_relationships or {}
        unknown_relationships = set(relationships) - set(candidate_ids)
        if unknown_relationships:
            raise KurtSchwarzReconciliationError(
                "cross-unit relationship references unknown candidates: "
                + ", ".join(sorted(unknown_relationships))
            )

        candidates_by_id = {
            row["candidate_id"]: row for row in self.object_census_candidates()
        }
        missing = [value for value in candidate_ids if value not in candidates_by_id]
        if missing:
            raise KurtSchwarzReconciliationError(
                "unknown cross-unit census candidates: " + ", ".join(missing)
            )
        candidates = [candidates_by_id[value] for value in candidate_ids]
        for candidate in candidates:
            if candidate["queue_class"] != "new_local_review":
                raise KurtSchwarzReconciliationError(
                    "cross-unit hold candidate is not in new-local review: "
                    f"{candidate['candidate_id']} ({candidate['queue_class']})"
                )
            current = candidate.get("decision") or {}
            if current and current.get("disposition") not in {"", "defer"}:
                raise KurtSchwarzReconciliationError(
                    "cross-unit hold would supersede a decided candidate: "
                    f"{candidate['candidate_id']} ({current.get('disposition')})"
                )

        hold_json = []
        members = []
        for sequence, candidate in enumerate(candidates, 1):
            relationship = relationships.get(
                candidate["candidate_id"], "ordered_scan_component"
            )
            serialized = self._cross_unit_hold_json(
                target_scope=target_scope,
                target_subunit=target_subunit,
                group_key=group_key,
                provisional_title=provisional_title,
                provisional_date_text=provisional_date_text,
                object_type=object_type,
                component_sequence=sequence,
                component_relationship=relationship,
            )
            hold_json.append(serialized)
            members.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "component_sequence": sequence,
                    "component_relationship": relationship,
                    "filename": candidate["filename"],
                    "content_sha256": candidate["content_sha256"],
                    "size_bytes": int(candidate["size_bytes"]),
                    "source_paths": candidate["source_paths"],
                    "preserved_evidence_relative_path": candidate["final_path"],
                }
            )

        receipt = receipt_path.expanduser().resolve(strict=False)
        if not receipt.is_relative_to(self.review_root):
            raise KurtSchwarzReconciliationError(
                "cross-unit hold receipt must remain inside the review root"
            )
        if receipt.exists():
            raise KurtSchwarzReconciliationError(
                f"cross-unit hold receipt already exists: {receipt}"
            )
        evidence = []
        for raw_path in evidence_files or []:
            path = raw_path.expanduser().resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(self.review_root):
                raise KurtSchwarzReconciliationError(
                    f"cross-unit evidence must be a file inside review root: {path}"
                )
            evidence.append(
                {
                    "relative_path": str(path.relative_to(self.review_root)),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )

        normalized_hold = json.loads(hold_json[0])
        result: dict[str, Any] = {
            "format": "iss-kurt-schwarz-cross-unit-hold-receipt-v1",
            "state": "dry_run",
            "source_fingerprint": self.source_fingerprint,
            "reconciliation_database": str(self.database),
            "actor": actor,
            "reason": reason,
            "hold": {
                key: value
                for key, value in normalized_hold.items()
                if key not in {"component_sequence", "component_relationship"}
            },
            "component_count": len(members),
            "total_size_bytes": sum(row["size_bytes"] for row in members),
            "members": members,
            "derived_review_evidence": evidence,
            "published_unit_changed": False,
            "source_bytes_changed": False,
            "identifiers_assigned": False,
            "deletion_authorized": False,
        }
        if not apply:
            return result

        receipt.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.database.with_name(
            f"{self.database.stem}.pre-cross-unit-hold-{stamp}.sqlite"
        )
        if backup.exists():
            raise KurtSchwarzReconciliationError(
                f"cross-unit hold backup already exists: {backup}"
            )
        with closing(sqlite3.connect(self.database)) as source:
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)

        decided_at = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = int(
                connection.execute(
                    "SELECT COALESCE(MAX(decision_id), 0) "
                    "FROM object_census_decisions"
                ).fetchone()[0]
            )
            connection.executemany(
                """INSERT INTO object_census_decisions (
                       candidate_id, disposition, target_record_key,
                       actor, reason, decided_at, cross_unit_hold_json
                   ) VALUES (?, 'cross_unit_hold', NULL, ?, ?, ?, ?)""",
                [
                    (candidate_id, actor, reason, decided_at, serialized)
                    for candidate_id, serialized in zip(
                        candidate_ids, hold_json, strict=True
                    )
                ],
            )
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_keys:
                connection.rollback()
                raise KurtSchwarzReconciliationError(
                    "cross-unit hold created foreign-key violations"
                )
            after = int(
                connection.execute(
                    "SELECT COALESCE(MAX(decision_id), 0) "
                    "FROM object_census_decisions"
                ).fetchone()[0]
            )
            connection.commit()

        result.update(
            {
                "state": "applied",
                "decided_at": decided_at,
                "decision_id_first": before + 1,
                "decision_id_last": after,
                "rollback_database": str(backup),
                "receipt": str(receipt),
            }
        )
        temporary = receipt.with_name(f".{receipt.name}.tmp-{os.getpid()}")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(
                    result, handle, ensure_ascii=False, sort_keys=True, indent=2
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, receipt)
            directory_fd = os.open(receipt.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return result

    def records(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            media_decisions = self._latest_media_decisions(connection)
            metadata_decisions = self._latest_metadata_decisions(connection)
            alias_decisions = self._active_alias_decisions(connection)
            rows = connection.execute(
                """SELECT object.*,
                          COUNT(media.candidate_id) AS media_count,
                          SUM(CASE WHEN media.content_role =
                              'preservation_source_variant' THEN 1 ELSE 0 END)
                              AS variant_count,
                          SUM(CASE WHEN media.content_role =
                              'preservation_master' THEN 1 ELSE 0 END)
                              AS proposed_master_count
                   FROM objects AS object
                   LEFT JOIN media_candidates AS media
                     ON media.record_key=object.record_key
                   GROUP BY object.record_key
                   ORDER BY object.current_inventory, object.record_key"""
            ).fetchall()
            candidate_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in connection.execute(
                """SELECT record_key, candidate_id, content_role,
                          content_sha256, filename
                   FROM media_candidates
                   ORDER BY record_key,
                     CASE content_role
                       WHEN 'preservation_master' THEN 0
                       WHEN 'preservation_source_tiff' THEN 1
                       WHEN 'preservation_source_variant' THEN 2
                       ELSE 3
                     END,
                     filename, candidate_id"""
            ):
                candidate_rows[row["record_key"]].append(dict(row))
            conflict_counts_work: dict[str, dict[str, int]] = defaultdict(
                lambda: {
                    "metadata_issue_count": 0,
                    "metadata_pending_count": 0,
                }
            )
            metadata_by_record: dict[str, dict[str, dict[str, Any]]] = (
                defaultdict(dict)
            )
            for row in connection.execute(
                """SELECT record_key, field_key, md_value, local_value,
                          comparison_state
                   FROM metadata_fields"""
            ):
                metadata_by_record[row["record_key"]][row["field_key"]] = dict(
                    row
                )
                if row["comparison_state"] == "same":
                    continue
                counts = conflict_counts_work[row["record_key"]]
                counts["metadata_issue_count"] += 1
                if (row["record_key"], row["field_key"]) not in metadata_decisions:
                    counts["metadata_pending_count"] += 1
            conflict_counts = dict(conflict_counts_work)
        records: list[dict[str, Any]] = []
        for raw in rows:
            item = dict(raw)
            alias_decision = alias_decisions.get(item["record_key"])
            candidates = candidate_rows.get(item["record_key"], [])
            decisions = [
                media_decisions.get(candidate["candidate_id"])
                for candidate in candidates
            ]
            dispositions = [
                decision["disposition"] for decision in decisions if decision
            ]
            media_count = int(item["media_count"])
            selected_masters = dispositions.count("selected_master")
            unresolved_media = media_count > 0 and (
                selected_masters != 1
                or len([value for value in dispositions if value != "defer"])
                != media_count
            )
            metadata = conflict_counts.get(
                item["record_key"],
                {"metadata_issue_count": 0, "metadata_pending_count": 0},
            )
            item.update(metadata)
            item["effective_metadata"] = {
                field_key: self._effective_metadata_value(
                    field,
                    metadata_decisions.get((item["record_key"], field_key)),
                )
                for field_key, field in metadata_by_record.get(
                    item["record_key"], {}
                ).items()
            }
            item["display_title"] = item["effective_metadata"].get(
                "title", item["current_title"]
            )
            item["direct_media_pending"] = unresolved_media
            item["selected_master_count"] = selected_masters
            item["wrong_candidate_count"] = dispositions.count("wrong_object")
            item["direct_metadata_pending_count"] = metadata[
                "metadata_pending_count"
            ]
            item["alias_decision"] = alias_decision
            item["alias_of"] = (
                alias_decision["canonical_record_key"]
                if alias_decision
                else ""
            )
            item["media_pending"] = unresolved_media and not alias_decision
            if alias_decision:
                item["metadata_pending_count"] = 0
            item["no_tiff_master"] = item["selection_state"] in {
                "provisional_jpeg_only",
                "resolved_md_surrogate",
            }
            item["review_state"] = (
                "aliased"
                if alias_decision
                else "pending"
                if unresolved_media or metadata["metadata_pending_count"]
                else "decided"
            )
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if (
                        media_decisions.get(candidate["candidate_id"], {}).get(
                            "disposition"
                        )
                        == "selected_master"
                    )
                ),
                None,
            )
            proposed = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["content_role"] == "preservation_master"
                ),
                None,
            )
            source_tiff = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["content_role"] == "preservation_source_tiff"
                ),
                None,
            )
            access_derivative = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["content_role"] == "access_derivative"
                ),
                None,
            )
            representative = selected or proposed or source_tiff
            if representative is None and candidates:
                representative = candidates[0]
            preview_representative = access_derivative or representative
            item["thumbnail_candidate_id"] = (
                representative["candidate_id"] if representative else ""
            )
            item["thumbnail_preview_candidate_id"] = (
                preview_representative["candidate_id"]
                if preview_representative else ""
            )
            item["thumbnail_content_sha256"] = (
                representative["content_sha256"] if representative else ""
            )
            item["thumbnail_role"] = (
                "curator_selected_master"
                if selected
                else representative["content_role"]
                if representative
                else "missing_image"
            )
            item["md_missing"] = not bool(item["md_object_id"].strip())
            item["display_inventory"] = (
                item["effective_metadata"].get("inventory", "")
                or item["current_inventory"]
                or item["historical_inventory"]
                or item["record_key"]
            )
            records.append(item)
        by_record_key = {row["record_key"]: row for row in records}
        for item in records:
            item["aliases"] = []
        for alias_record_key, decision in alias_decisions.items():
            canonical = by_record_key.get(decision["canonical_record_key"])
            alias = by_record_key.get(alias_record_key)
            if canonical is not None and alias is not None:
                canonical["aliases"].append(
                    {
                        "record_key": alias_record_key,
                        "display_inventory": (
                            alias["current_inventory"]
                            or alias["historical_inventory"]
                            or alias_record_key
                        ),
                        "md_object_id": alias["md_object_id"],
                        "decision": decision,
                    }
                )
        md_ids = Counter(
            row["md_object_id"] for row in records if row["md_object_id"]
        )
        inventories = Counter(
            normalized_value(row["display_inventory"]) for row in records
        )
        representative_digests = Counter(
            row["thumbnail_content_sha256"]
            for row in records
            if row["thumbnail_content_sha256"]
        )
        for item in records:
            item["md_duplicate_count"] = (
                md_ids[item["md_object_id"]] if item["md_object_id"] else 0
            )
            item["inventory_duplicate_count"] = inventories[
                normalized_value(item["display_inventory"])
            ]
            item["thumbnail_duplicate_count"] = (
                representative_digests[item["thumbnail_content_sha256"]]
                if item["thumbnail_content_sha256"]
                else 0
            )
        return records

    def _candidate_rows(self, record_key: str) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            rows = connection.execute(
                """SELECT * FROM media_candidates WHERE record_key=?
                   ORDER BY CASE content_role
                     WHEN 'preservation_master' THEN 0
                     WHEN 'preservation_source_variant' THEN 1
                     ELSE 2 END,
                   filename, candidate_id""",
                (record_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record(self, record_key: str) -> dict[str, Any]:
        with closing(self.connect(read_only=True)) as connection:
            raw = connection.execute(
                "SELECT * FROM objects WHERE record_key=?", (record_key,)
            ).fetchone()
            if raw is None:
                raise KurtSchwarzReconciliationError(
                    f"unknown Kurt Schwarz object: {record_key}"
                )
            media_decisions = self._latest_media_decisions(connection)
            metadata_decisions = self._latest_metadata_decisions(connection)
            alias_decisions = self._active_alias_decisions(connection)
            analysis = {
                row["candidate_id"]: dict(row)
                for row in connection.execute(
                    """SELECT * FROM technical_analysis
                       WHERE candidate_id IN (
                         SELECT candidate_id FROM media_candidates
                         WHERE record_key=?
                       )""",
                    (record_key,),
                )
            }
            candidate_links = {
                row["candidate_id"]: dict(row)
                for row in connection.execute(
                    """SELECT * FROM media_candidate_links
                       WHERE target_record_key=?""",
                    (record_key,),
                )
            }
            metadata = [
                {
                    **dict(row),
                    "decision": metadata_decisions.get(
                        (record_key, row["field_key"])
                    ),
                }
                for row in connection.execute(
                    """SELECT * FROM metadata_fields WHERE record_key=?
                       ORDER BY CASE field_key
                         WHEN 'inventory' THEN 0 WHEN 'title' THEN 1
                         WHEN 'description' THEN 2 WHEN 'date_text' THEN 3
                         WHEN 'places' THEN 4 WHEN 'tags' THEN 5
                         WHEN 'material' THEN 6 ELSE 7 END""",
                    (record_key,),
                )
            ]
            alias_decision = alias_decisions.get(record_key)
            canonical = None
            if alias_decision:
                canonical_raw = connection.execute(
                    "SELECT * FROM objects WHERE record_key=?",
                    (alias_decision["canonical_record_key"],),
                ).fetchone()
                if canonical_raw is not None:
                    canonical = dict(canonical_raw)
                    canonical["display_inventory"] = (
                        canonical["current_inventory"]
                        or canonical["historical_inventory"]
                        or canonical["record_key"]
                    )
            aliases = [
                {
                    "record_key": row["alias_record_key"],
                    "canonical_record_key": row["canonical_record_key"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "reason": row["reason"],
                    "decided_at": row["decided_at"],
                    "current_inventory": row["current_inventory"],
                    "historical_inventory": row["historical_inventory"],
                    "md_object_id": row["md_object_id"],
                    "display_inventory": (
                        row["current_inventory"]
                        or row["historical_inventory"]
                        or row["alias_record_key"]
                    ),
                }
                for row in connection.execute(
                    """SELECT decision.*, object.current_inventory,
                              object.historical_inventory,
                              object.md_object_id
                       FROM object_alias_decisions AS decision
                       JOIN objects AS object
                         ON object.record_key=decision.alias_record_key
                       WHERE decision.decision_id IN (
                         SELECT MAX(decision_id)
                         FROM object_alias_decisions
                         GROUP BY alias_record_key
                       )
                         AND decision.action='same_object_alias'
                         AND decision.canonical_record_key=?
                       ORDER BY object.current_inventory,
                                object.historical_inventory,
                                decision.alias_record_key""",
                    (record_key,),
                )
            ]
        candidates = []
        for candidate in self._candidate_rows(record_key):
            candidate["source_occurrences"] = json.loads(
                candidate.pop("source_occurrences_json")
            )
            candidate["decision"] = media_decisions.get(candidate["candidate_id"])
            candidate["analysis"] = analysis.get(candidate["candidate_id"])
            candidate["link"] = candidate_links.get(candidate["candidate_id"])
            candidates.append(candidate)
        item = dict(raw)
        item["catalog"] = json.loads(item.pop("catalog_json"))
        item["candidates"] = candidates
        item["metadata"] = metadata
        item["effective_metadata"] = {
            field["field_key"]: self._effective_metadata_value(
                field, field["decision"]
            )
            for field in metadata
        }
        item["display_title"] = item["effective_metadata"].get(
            "title", item["current_title"]
        )
        item["display_inventory"] = (
            item["effective_metadata"].get("inventory", "")
            or item["current_inventory"]
            or item["historical_inventory"]
            or item["record_key"]
        )
        item["alias_decision"] = alias_decision
        item["canonical"] = canonical
        item["aliases"] = aliases
        return item

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        with closing(self.connect(read_only=True)) as connection:
            row = connection.execute(
                "SELECT * FROM media_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KurtSchwarzReconciliationError(
                f"unknown Kurt Schwarz media candidate: {candidate_id}"
            )
        return dict(row)

    def search_representations(
        self,
        query: str,
        *,
        target_record_key: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = normalized_value(query)
        if len(query) < 2:
            return []
        if limit < 1 or limit > 100:
            raise KurtSchwarzReconciliationError(
                "representation search limit must be between 1 and 100"
            )
        with closing(self.connect(read_only=True)) as connection:
            target = connection.execute(
                "SELECT record_key FROM objects WHERE record_key=?",
                (target_record_key,),
            ).fetchone()
            if target is None:
                raise KurtSchwarzReconciliationError(
                    f"unknown Kurt Schwarz object: {target_record_key}"
                )
            bound_paths = {
                str(row["final_path"])
                for row in connection.execute(
                    "SELECT final_path FROM media_candidates WHERE record_key=?",
                    (target_record_key,),
                )
            }
        matches: list[dict[str, Any]] = []
        for row in read_csv(self.representations_path):
            final_path = row["final_relative_path"]
            if Path(final_path).suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            values = (
                final_path,
                row.get("inventories", ""),
                row.get("record_keys", ""),
                row.get("source_path", ""),
                row.get("source_member", ""),
            )
            if query not in normalized_value(" ".join(values)):
                continue
            filename = Path(final_path).name
            normalized_filename = normalized_value(filename)
            matches.append(
                {
                    "final_path": final_path,
                    "filename": filename,
                    "content_role": row["role"],
                    "content_sha256": row["final_sha256"],
                    "size_bytes": int(row["size_bytes"]),
                    "source_record_keys": row.get("record_keys", ""),
                    "inventories": row.get("inventories", ""),
                    "already_bound": final_path in bound_paths,
                    "search_rank": (
                        0
                        if query == normalized_filename
                        or query == normalized_value(Path(filename).stem)
                        else 1
                        if normalized_filename.startswith(query)
                        else 2
                    ),
                }
            )
        matches.sort(
            key=lambda row: (
                row["search_rank"],
                row["already_bound"],
                normalized_value(row["filename"]),
                row["final_path"],
            )
        )
        for row in matches:
            row.pop("search_rank", None)
        return matches[:limit]

    def link_representation_candidate(
        self,
        *,
        target_record_key: str,
        final_path: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        target_record_key = target_record_key.strip()
        final_path = final_path.strip()
        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        if not reason:
            raise KurtSchwarzReconciliationError(
                "candidate-link reason is required"
            )
        relative = Path(final_path)
        if (
            not final_path
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.casefold() not in IMAGE_EXTENSIONS
        ):
            raise KurtSchwarzReconciliationError(
                "candidate must be an image in the published representation manifest"
            )
        manifest_rows = [
            row
            for row in read_csv(self.representations_path)
            if row["final_relative_path"] == final_path
        ]
        if len(manifest_rows) != 1:
            raise KurtSchwarzReconciliationError(
                "candidate path is not uniquely manifested in the published unit"
            )
        representation = manifest_rows[0]
        published_path = (self.unit_root / relative).resolve(strict=True)
        if not published_path.is_relative_to(self.unit_root):
            raise KurtSchwarzReconciliationError(
                "candidate path escaped the published unit"
            )
        if (
            not published_path.is_file()
            or published_path.stat().st_size
            != int(representation["size_bytes"])
        ):
            raise KurtSchwarzReconciliationError(
                "published candidate no longer matches its manifest binding"
            )
        occurrences = [
            {
                "source_path": row["source_path"],
                "source_sha256": row["source_sha256"],
                "source_size_bytes": row["source_size_bytes"],
                "preservation_status": row["preservation_status"],
                "drain_state": row["drain_state"],
                "deletion_authorized": row["deletion_authorized"],
            }
            for row in read_csv(self.disposition_path)
            if row["final_path"] == final_path
        ]
        candidate_id = media_candidate_id(target_record_key, final_path)
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT record_key FROM objects WHERE record_key=?",
                (target_record_key,),
            ).fetchone()
            if target is None:
                raise KurtSchwarzReconciliationError(
                    f"unknown Kurt Schwarz object: {target_record_key}"
                )
            if target_record_key in self._active_alias_decisions(connection):
                raise KurtSchwarzReconciliationError(
                    "link candidates to the canonical object, not its alias"
                )
            existing = connection.execute(
                "SELECT * FROM media_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["record_key"] != target_record_key
                    or existing["final_path"] != final_path
                ):
                    raise KurtSchwarzReconciliationError(
                        "candidate identifier collision"
                    )
                connection.rollback()
                return {
                    "candidate_id": candidate_id,
                    "record_key": target_record_key,
                    "final_path": final_path,
                    "unchanged": True,
                }
            connection.execute(
                """INSERT INTO media_candidates (
                       candidate_id, record_key, final_path, filename,
                       extension, content_role, content_sha256, size_bytes,
                       source_occurrences_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    target_record_key,
                    final_path,
                    relative.name,
                    relative.suffix.casefold(),
                    representation["role"],
                    representation["final_sha256"],
                    int(representation["size_bytes"]),
                    json.dumps(
                        occurrences, ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO media_candidate_links (
                       candidate_id, target_record_key, final_path,
                       source_record_keys, actor, reason, linked_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    target_record_key,
                    final_path,
                    representation.get("record_keys", ""),
                    actor,
                    reason,
                    now,
                ),
            )
            link_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            connection.commit()
        return {
            "link_id": link_id,
            "candidate_id": candidate_id,
            "record_key": target_record_key,
            "final_path": final_path,
            "source_record_keys": representation.get("record_keys", ""),
            "linked_at": now,
            "unchanged": False,
        }

    @staticmethod
    def _metadata_auto_plan(
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        latest = KurtSchwarzReview._latest_metadata_decisions(connection)
        aliases = set(KurtSchwarzReview._active_alias_decisions(connection))
        fields = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM metadata_fields
                   WHERE comparison_state<>'same'
                   ORDER BY record_key, field_key"""
            )
        ]
        obvious: dict[str, list[dict[str, str]]] = defaultdict(list)
        for field in fields:
            key = (field["record_key"], field["field_key"])
            if key in latest or field["record_key"] in aliases:
                continue
            if field["comparison_state"] != "conflict":
                continue
            reason = obvious_metadata_mismatch_reason(
                field["field_key"],
                field["md_value"],
                field["local_value"],
            )
            if reason:
                obvious[field["record_key"]].append(
                    {
                        "field_key": field["field_key"],
                        "reason": reason,
                    }
                )
        decisions: list[dict[str, str]] = []
        held: list[dict[str, str]] = []
        for field in fields:
            record_key = field["record_key"]
            field_key = field["field_key"]
            if (record_key, field_key) in latest or record_key in aliases:
                continue
            state = field["comparison_state"]
            if state == "conflict" and record_key in obvious:
                held.append(
                    {
                        "record_key": record_key,
                        "field_key": field_key,
                        "comparison_state": state,
                    }
                )
                continue
            if field["md_value"]:
                action = "use_md"
                accepted_value = field["md_value"]
                basis = (
                    "routine conflict accepted from current museum-digital"
                    if state == "conflict"
                    else "museum-digital is the only supplied value"
                )
            elif field["local_value"]:
                action = "use_local"
                accepted_value = field["local_value"]
                basis = "museum-digital has no value; retained local evidence"
            else:
                action = "defer"
                accepted_value = ""
                basis = "neither source supplies a value"
            decisions.append(
                {
                    "record_key": record_key,
                    "field_key": field_key,
                    "action": action,
                    "accepted_value": accepted_value,
                    "reason": f"Automatic {AUTO_METADATA_POLICY}: {basis}.",
                    "comparison_state": state,
                }
            )
        return {
            "decisions": decisions,
            "held": held,
            "obvious_records": [
                {"record_key": record_key, "signals": signals}
                for record_key, signals in sorted(obvious.items())
            ],
            "skipped_alias_records": len(aliases),
        }

    def auto_resolve_metadata(
        self,
        *,
        actor: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        now = utc_now()
        connection = self.connect() if apply else self.connect(read_only=True)
        try:
            if apply:
                connection.execute("BEGIN IMMEDIATE")
            plan = self._metadata_auto_plan(connection)
            decisions = plan["decisions"]
            before = int(
                connection.execute(
                    "SELECT COALESCE(MAX(decision_id), 0) FROM metadata_decisions"
                ).fetchone()[0]
            )
            if apply and decisions:
                connection.executemany(
                    """INSERT INTO metadata_decisions (
                           record_key, field_key, action, accepted_value,
                           actor, reason, decided_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            row["record_key"],
                            row["field_key"],
                            row["action"],
                            row["accepted_value"],
                            actor,
                            row["reason"],
                            now,
                        )
                        for row in decisions
                    ],
                )
                connection.commit()
            after = int(
                connection.execute(
                    "SELECT COALESCE(MAX(decision_id), 0) FROM metadata_decisions"
                ).fetchone()[0]
            )
        except Exception:
            if apply:
                connection.rollback()
            raise
        finally:
            connection.close()
        action_counts = Counter(row["action"] for row in decisions)
        state_counts = Counter(row["comparison_state"] for row in decisions)
        result = {
            "format": "iss-kurt-schwarz-metadata-auto-policy-v1",
            "policy": AUTO_METADATA_POLICY,
            "source_fingerprint": self.source_fingerprint,
            "state": "applied" if apply else "dry_run",
            "actor": actor,
            "evaluated_at": now,
            "decision_count": len(decisions),
            "decision_id_first": before + 1 if apply and decisions else 0,
            "decision_id_last": after if apply and decisions else 0,
            "action_counts": dict(sorted(action_counts.items())),
            "comparison_state_counts": dict(sorted(state_counts.items())),
            "held_field_count": len(plan["held"]),
            "obvious_record_count": len(plan["obvious_records"]),
            "obvious_records": plan["obvious_records"],
            "skipped_alias_records": plan["skipped_alias_records"],
        }
        if apply:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            receipt = self.review_root / f"metadata-auto-policy-{timestamp}.json"
            temporary = receipt.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, receipt)
            result["receipt"] = str(receipt)
        return result

    def candidate_path(self, candidate_id: str) -> Path:
        candidate = self.candidate(candidate_id)
        path = (self.unit_root / candidate["final_path"]).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(self.unit_root):
            raise KurtSchwarzReconciliationError(
                f"candidate escaped the published unit: {path}"
            )
        return path

    def preview_cache_path(self, candidate_id: str) -> Path:
        source = self.candidate_path(candidate_id)
        stat = source.stat()
        digest = hashlib.sha256(
            f"jpeg-preview-v{PREVIEW_CACHE_VERSION}\0{candidate_id}\0"
            f"{stat.st_size}\0"
            f"{stat.st_mtime_ns}".encode()
        ).hexdigest()
        return self.cache_root / "previews" / f"{digest}.jpg"

    def preview(self, candidate_id: str) -> Path:
        source = self.candidate_path(candidate_id)
        destination = self.preview_cache_path(candidate_id)
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.stem}.tmp-{os.getpid()}-"
            f"{threading.get_ident()}.jpg"
        )
        with self._preview_lock:
            if destination.is_file() and destination.stat().st_size > 0:
                return destination
            try:
                commands: list[list[str]] = []
                if shutil.which("vipsthumbnail"):
                    commands.append(
                        [
                            "vipsthumbnail",
                            str(source),
                            "--size",
                            "1200x900",
                            "--output",
                            str(temporary),
                            "--vips-concurrency=1",
                            "--vips-disc-threshold=104857600",
                            "--vips-cache-max=100",
                            "--vips-cache-max-memory=268435456",
                            "--vips-cache-max-files=32",
                        ]
                    )
                commands.append(
                    [
                        "magick",
                        f"{source}[0]",
                        "-auto-orient",
                        "-thumbnail",
                        "1200x900>",
                        "-strip",
                        "-colorspace",
                        "sRGB",
                        "-quality",
                        "88",
                        f"jpeg:{temporary}",
                    ]
                )
                failures = []
                for command in commands:
                    temporary.unlink(missing_ok=True)
                    try:
                        subprocess.run(
                            command,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=120,
                        )
                        break
                    except (
                        FileNotFoundError,
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                    ) as error:
                        stderr = getattr(error, "stderr", b"")
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode("utf-8", errors="replace")
                        failures.append(
                            f"{command[0]}: {str(stderr or error).strip()}"
                        )
                else:
                    raise KurtSchwarzReconciliationError(
                        f"preview generation failed for {source}: "
                        + " | ".join(failures)
                    )
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def warm_representative_previews(
        self,
        *,
        workers: int = 1,
    ) -> dict[str, Any]:
        """Pre-generate the current representative used by every object tile."""

        if workers < 1 or workers > 2:
            raise KurtSchwarzReconciliationError(
                "preview warm-up workers must be between 1 and 2"
            )
        receipt_path = self.review_root / "preview-warmup.json"
        records = self.records()
        started_at = utc_now()
        result: dict[str, Any] = {
            "format": "iss-kurt-schwarz-preview-warmup-v1",
            "state": "running",
            "source_fingerprint": self.source_fingerprint,
            "preview_cache_version": PREVIEW_CACHE_VERSION,
            "workers": workers,
            "started_at": started_at,
            "updated_at": started_at,
            "total": len(records),
            "cached_before": 0,
            "generated": 0,
            "failed": 0,
            "processed": 0,
            "remaining": len(records),
            "failures": [],
        }

        def write_receipt() -> None:
            result["updated_at"] = utc_now()
            temporary = receipt_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, receipt_path)

        write_receipt()
        pending = []
        for record in records:
            candidate_id = record["thumbnail_preview_candidate_id"]
            try:
                destination = self.preview_cache_path(candidate_id)
                if destination.is_file() and destination.stat().st_size > 0:
                    result["cached_before"] += 1
                else:
                    pending.append(record)
            except (KurtSchwarzReconciliationError, OSError):
                pending.append(record)
        result["processed"] = result["cached_before"]
        result["remaining"] = len(records) - result["processed"]
        write_receipt()

        def generate(record: dict[str, Any]) -> str:
            try:
                self.preview(record["thumbnail_preview_candidate_id"])
                return ""
            except (KurtSchwarzReconciliationError, OSError) as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(generate, record): record for record in pending
            }
            for future in as_completed(futures):
                record = futures[future]
                error = future.result()
                if error:
                    result["failed"] += 1
                    result["failures"].append(
                        {
                            "record_key": record["record_key"],
                            "inventory": record["display_inventory"],
                            "candidate_id": record[
                                "thumbnail_preview_candidate_id"
                            ],
                            "error": error,
                        }
                    )
                else:
                    result["generated"] += 1
                result["processed"] += 1
                result["remaining"] = len(records) - result["processed"]
                if result["processed"] % 10 == 0 or error:
                    write_receipt()
                if result["processed"] % 25 == 0:
                    print(
                        "preview-warmup "
                        f"{result['processed']}/{len(records)} "
                        f"generated={result['generated']} "
                        f"cached={result['cached_before']} "
                        f"failed={result['failed']}",
                        flush=True,
                    )
        result["state"] = (
            "complete_with_failures" if result["failed"] else "complete"
        )
        result["completed_at"] = utc_now()
        write_receipt()
        return result

    def preview_warmup_status(self) -> dict[str, Any]:
        receipt = self.review_root / "preview-warmup.json"
        if not receipt.is_file():
            return {}
        try:
            result = json.loads(receipt.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if (
            result.get("source_fingerprint") != self.source_fingerprint
            or str(result.get("preview_cache_version"))
            != PREVIEW_CACHE_VERSION
        ):
            return {}
        return result

    @staticmethod
    def _identify(path: Path) -> dict[str, Any]:
        process = subprocess.run(
            [
                "magick",
                "identify",
                "-quiet",
                "-format",
                "%w|%h|%z|%[colorspace]|%m",
                f"{path}[0]",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        width, height, depth, colorspace, media_format = process.stdout.split(
            "|", 4
        )
        return {
            "width_px": int(width),
            "height_px": int(height),
            "bit_depth": int(depth),
            "colorspace": colorspace,
            "media_format": media_format,
        }

    @staticmethod
    def _embedded_metadata(path: Path) -> dict[str, Any]:
        process = subprocess.run(
            [
                "exiftool",
                "-j",
                "-Title",
                "-Description",
                "-ImageDescription",
                "-Subject",
                "-Keywords",
                "-CreateDate",
                "-DateTimeOriginal",
                "-Artist",
                "-Copyright",
                str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        payload = json.loads(process.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise KurtSchwarzReconciliationError(
                f"cannot read embedded metadata: {path}"
            )
        return {
            key: value
            for key, value in payload[0].items()
            if key not in {"SourceFile"} and value not in ("", None, [])
        }

    @staticmethod
    def _pixel_difference(reference: Path, candidate: Path) -> str:
        process = subprocess.run(
            [
                "magick",
                "compare",
                "-metric",
                "AE",
                f"{reference}[0]",
                f"{candidate}[0]",
                "null:",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
        metric = process.stderr.strip().splitlines()[-1].strip()
        if process.returncode not in {0, 1} or not metric:
            raise KurtSchwarzReconciliationError(
                f"pixel comparison failed for {candidate.name}: "
                f"{process.stderr.strip()}"
            )
        return metric

    def analyze(self, record_key: str) -> dict[str, Any]:
        with self._analysis_lock:
            record = self.record(record_key)
            if not record["candidates"]:
                raise KurtSchwarzReconciliationError(
                    f"{record_key} has no media candidates"
                )
            selected = next(
                (
                    item
                    for item in record["candidates"]
                    if item.get("decision")
                    and item["decision"]["disposition"] == "selected_master"
                ),
                None,
            )
            reference = selected or next(
                (
                    item
                    for item in record["candidates"]
                    if item["content_role"] == "preservation_master"
                ),
                None,
            )
            reference = reference or max(
                record["candidates"], key=lambda item: int(item["size_bytes"])
            )
            reference_path = self.candidate_path(reference["candidate_id"])
            reference_info = self._identify(reference_path)
            analyzed: list[dict[str, Any]] = []
            for candidate in record["candidates"]:
                path = self.candidate_path(candidate["candidate_id"])
                technical = self._identify(path)
                if candidate["candidate_id"] == reference["candidate_id"]:
                    state = "reference"
                    difference = "0"
                elif (
                    technical["width_px"],
                    technical["height_px"],
                ) != (
                    reference_info["width_px"],
                    reference_info["height_px"],
                ):
                    state = "different_dimensions"
                    difference = ""
                else:
                    difference = self._pixel_difference(reference_path, path)
                    state = (
                        "identical_pixels"
                        if difference in {"0", "0 (0)"}
                        else "different_pixels"
                    )
                row = {
                    "candidate_id": candidate["candidate_id"],
                    "reference_candidate_id": reference["candidate_id"],
                    **technical,
                    "comparison_state": state,
                    "pixel_difference_count": difference,
                    "embedded_metadata_json": json.dumps(
                        self._embedded_metadata(path),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "analyzed_at": utc_now(),
                }
                analyzed.append(row)
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    """INSERT INTO technical_analysis (
                           candidate_id, reference_candidate_id, width_px,
                           height_px, bit_depth, colorspace, media_format,
                           comparison_state, pixel_difference_count,
                           embedded_metadata_json, analyzed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(candidate_id) DO UPDATE SET
                         reference_candidate_id=excluded.reference_candidate_id,
                         width_px=excluded.width_px,
                         height_px=excluded.height_px,
                         bit_depth=excluded.bit_depth,
                         colorspace=excluded.colorspace,
                         media_format=excluded.media_format,
                         comparison_state=excluded.comparison_state,
                         pixel_difference_count=excluded.pixel_difference_count,
                         embedded_metadata_json=excluded.embedded_metadata_json,
                         analyzed_at=excluded.analyzed_at""",
                    [
                        (
                            row["candidate_id"],
                            row["reference_candidate_id"],
                            row["width_px"],
                            row["height_px"],
                            row["bit_depth"],
                            row["colorspace"],
                            row["media_format"],
                            row["comparison_state"],
                            row["pixel_difference_count"],
                            row["embedded_metadata_json"],
                            row["analyzed_at"],
                        )
                        for row in analyzed
                    ],
                )
                connection.commit()
            return {
                "record_key": record_key,
                "reference_candidate_id": reference["candidate_id"],
                "analyzed_candidates": len(analyzed),
                "comparison_states": dict(
                    Counter(row["comparison_state"] for row in analyzed)
                ),
            }

    @staticmethod
    def _resolve_record_reference(
        connection: sqlite3.Connection,
        reference: str,
    ) -> sqlite3.Row:
        reference = reference.strip()
        if not reference:
            raise KurtSchwarzReconciliationError(
                "canonical record key, inventory, or MD object ID is required"
            )
        rows = connection.execute("SELECT * FROM objects").fetchall()
        normalized = normalized_value(reference)
        matches = {
            row["record_key"]: row
            for row in rows
            if reference == row["record_key"]
            or normalized
            in {
                normalized_value(str(row["record_key"])),
                normalized_value(str(row["md_object_id"])),
                normalized_value(str(row["current_inventory"])),
                normalized_value(str(row["historical_inventory"])),
            }
        }
        if not matches:
            raise KurtSchwarzReconciliationError(
                f"unknown canonical object reference: {reference}"
            )
        if len(matches) != 1:
            raise KurtSchwarzReconciliationError(
                f"ambiguous canonical object reference: {reference}"
            )
        return next(iter(matches.values()))

    def resolve_record_reference(self, reference: str) -> str:
        with closing(self.connect(read_only=True)) as connection:
            row = self._resolve_record_reference(connection, reference)
        return str(row["record_key"])

    def save_alias_decision(
        self,
        *,
        alias_record_key: str,
        canonical_reference: str,
        action: str,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        alias_record_key = alias_record_key.strip()
        action = action.strip()
        actor = actor.strip()
        reason = reason.strip()
        if action not in OBJECT_RELATION_ACTIONS:
            raise KurtSchwarzReconciliationError(
                f"unsupported object relation action: {action}"
            )
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            alias = connection.execute(
                "SELECT * FROM objects WHERE record_key=?",
                (alias_record_key,),
            ).fetchone()
            if alias is None:
                raise KurtSchwarzReconciliationError(
                    f"unknown alias object: {alias_record_key}"
                )
            active_aliases = self._active_alias_decisions(connection)
            if action == "same_object_alias":
                canonical = self._resolve_record_reference(
                    connection,
                    canonical_reference,
                )
                canonical_record_key = str(canonical["record_key"])
                if canonical_record_key == alias_record_key:
                    raise KurtSchwarzReconciliationError(
                        "an object cannot be its own alias"
                    )
                if canonical_record_key in active_aliases:
                    parent = active_aliases[canonical_record_key][
                        "canonical_record_key"
                    ]
                    raise KurtSchwarzReconciliationError(
                        f"{canonical_record_key} is already an alias of {parent}; "
                        "choose the canonical object directly"
                    )
                children = sorted(
                    child
                    for child, decision in active_aliases.items()
                    if decision["canonical_record_key"] == alias_record_key
                    and child != canonical_record_key
                )
                if children:
                    raise KurtSchwarzReconciliationError(
                        f"{alias_record_key} is canonical for "
                        f"{', '.join(children)}; reassign those aliases first"
                    )
                latest = active_aliases.get(alias_record_key)
                if (
                    latest
                    and latest["canonical_record_key"]
                    == canonical_record_key
                ):
                    connection.rollback()
                    return {
                        **latest,
                        "unchanged": True,
                    }
            else:
                canonical_record_key = alias_record_key
                canonical = alias
                latest = active_aliases.get(alias_record_key)
                if latest is None:
                    connection.rollback()
                    return {
                        "decision_id": 0,
                        "alias_record_key": alias_record_key,
                        "canonical_record_key": canonical_record_key,
                        "action": action,
                        "actor": actor,
                        "reason": reason,
                        "decided_at": now,
                        "unchanged": True,
                    }
            connection.execute(
                """INSERT INTO object_alias_decisions (
                       alias_record_key, canonical_record_key, action,
                       actor, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    alias_record_key,
                    canonical_record_key,
                    action,
                    actor,
                    reason,
                    now,
                ),
            )
            decision_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            connection.commit()
        return {
            "decision_id": decision_id,
            "alias_record_key": alias_record_key,
            "canonical_record_key": canonical_record_key,
            "canonical_inventory": (
                canonical["current_inventory"]
                or canonical["historical_inventory"]
                or canonical_record_key
            ),
            "action": action,
            "actor": actor,
            "reason": reason,
            "decided_at": now,
            "unchanged": False,
        }

    def save_media_decision(
        self,
        *,
        record_key: str,
        candidate_id: str,
        disposition: str,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        result = self.save_media_decisions(
            record_key=record_key,
            decisions=[
                {
                    "candidate_id": candidate_id,
                    "disposition": disposition,
                    "reason": reason,
                }
            ],
            actor=actor,
        )
        return {
            "decision_id": (
                result["decision_ids"][-1] if result["decision_ids"] else 0
            ),
            "record_key": record_key,
            "candidate_id": candidate_id,
            "disposition": disposition.strip(),
            "auto_redundant_candidate_ids": result[
                "auto_redundant_candidate_ids"
            ],
            "auto_redundant_count": result["auto_redundant_count"],
            "decided_at": result["decided_at"],
        }

    def save_media_decisions(
        self,
        *,
        record_key: str,
        decisions: list[dict[str, str]],
        actor: str,
    ) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        normalized: dict[str, dict[str, str]] = {}
        for raw in decisions:
            candidate_id = str(raw.get("candidate_id", "")).strip()
            disposition = str(raw.get("disposition", "")).strip()
            reason = str(raw.get("reason", "")).strip()
            if not candidate_id:
                raise KurtSchwarzReconciliationError(
                    "media candidate is required"
                )
            if candidate_id in normalized:
                raise KurtSchwarzReconciliationError(
                    f"duplicate media decision: {candidate_id}"
                )
            if disposition not in MEDIA_DISPOSITIONS:
                raise KurtSchwarzReconciliationError(
                    f"unsupported media disposition: {disposition}"
                )
            normalized[candidate_id] = {
                "disposition": disposition,
                "reason": reason,
            }
        if not normalized:
            raise KurtSchwarzReconciliationError(
                "select at least one media disposition"
            )
        selected_masters = [
            candidate_id
            for candidate_id, item in normalized.items()
            if item["disposition"] == "selected_master"
        ]
        if len(selected_masters) > 1:
            raise KurtSchwarzReconciliationError(
                "select only one authoritative master"
            )
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate_ids = {
                str(row["candidate_id"])
                for row in connection.execute(
                    """SELECT candidate_id FROM media_candidates
                       WHERE record_key=?""",
                    (record_key,),
                )
            }
            unknown = set(normalized) - candidate_ids
            if unknown:
                raise KurtSchwarzReconciliationError(
                    "media candidate is not bound to this object: "
                    + ", ".join(sorted(unknown))
                )
            latest = {
                str(row["candidate_id"]): str(row["disposition"])
                for row in connection.execute(
                    """SELECT decision.candidate_id, decision.disposition
                       FROM media_decisions AS decision
                       WHERE decision.decision_id IN (
                         SELECT MAX(decision_id)
                         FROM media_decisions
                         WHERE record_key=?
                         GROUP BY candidate_id
                       )
                         AND decision.record_key=?""",
                    (record_key, record_key),
                )
            }
            desired: dict[str, dict[str, str]] = {}
            auto_redundant_candidate_ids: list[str] = []
            master_id = selected_masters[0] if selected_masters else ""
            if master_id:
                master_reason = normalized[master_id]["reason"]
                for item_id in sorted(candidate_ids):
                    submitted = normalized.get(item_id)
                    if item_id == master_id:
                        desired[item_id] = normalized[item_id]
                    elif submitted and submitted["disposition"] == "wrong_object":
                        desired[item_id] = submitted
                    elif submitted is None and latest.get(item_id) == "wrong_object":
                        desired[item_id] = {
                            "disposition": "wrong_object",
                            "reason": "",
                        }
                    else:
                        desired[item_id] = {
                            "disposition": "redundant",
                            "reason": (
                                "Automatically redundant after selecting the "
                                f"authoritative master {master_id}. Retain "
                                "provenance and derive future drain-list scope "
                                "without deletion authority."
                                + (f" {master_reason}" if master_reason else "")
                            ),
                        }
                        if latest.get(item_id) != "redundant":
                            auto_redundant_candidate_ids.append(item_id)
            else:
                desired = dict(normalized)
            ordered_ids = sorted(
                desired,
                key=lambda item_id: (item_id == master_id, item_id),
            )
            decision_ids: list[int] = []
            changed: list[dict[str, str]] = []
            for item_id in ordered_ids:
                item = desired[item_id]
                if latest.get(item_id) == item["disposition"]:
                    continue
                connection.execute(
                    """INSERT INTO media_decisions (
                           record_key, candidate_id, disposition, actor,
                           reason, decided_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        record_key,
                        item_id,
                        item["disposition"],
                        actor,
                        item["reason"],
                        now,
                    ),
                )
                decision_ids.append(
                    int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                )
                changed.append(
                    {
                        "candidate_id": item_id,
                        "disposition": item["disposition"],
                    }
                )
            connection.commit()
        return {
            "record_key": record_key,
            "decision_ids": decision_ids,
            "changed": changed,
            "changed_count": len(changed),
            "master_candidate_id": master_id,
            "auto_redundant_candidate_ids": auto_redundant_candidate_ids,
            "auto_redundant_count": len(auto_redundant_candidate_ids),
            "decided_at": now,
        }

    def save_metadata_decision(
        self,
        *,
        record_key: str,
        field_key: str,
        action: str,
        actor: str,
        corrected_value: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        action = action.strip()
        actor = actor.strip()
        reason = reason.strip()
        if action not in METADATA_ACTIONS:
            raise KurtSchwarzReconciliationError(
                f"unsupported metadata action: {action}"
            )
        if not actor:
            raise KurtSchwarzReconciliationError("named reviewer is required")
        with closing(self.connect(read_only=True)) as connection:
            field = connection.execute(
                """SELECT * FROM metadata_fields
                   WHERE record_key=? AND field_key=?""",
                (record_key, field_key),
            ).fetchone()
        if field is None:
            raise KurtSchwarzReconciliationError(
                f"unknown metadata field: {record_key}/{field_key}"
            )
        if action == "use_md":
            accepted = str(field["md_value"])
        elif action == "use_local":
            accepted = str(field["local_value"])
        elif action == "combine":
            if field_key not in MULTI_VALUE_FIELDS:
                raise KurtSchwarzReconciliationError(
                    "only tags and places support an explicit combined value"
                )
            accepted = combined_value(
                str(field["md_value"]), str(field["local_value"])
            )
        elif action == "corrected":
            accepted = corrected_value.strip()
        else:
            accepted = ""
        if action != "defer" and not accepted:
            raise KurtSchwarzReconciliationError(
                "accepted metadata value cannot be empty"
            )
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO metadata_decisions (
                       record_key, field_key, action, accepted_value,
                       actor, reason, decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_key,
                    field_key,
                    action,
                    accepted,
                    actor,
                    reason,
                    now,
                ),
            )
            decision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.commit()
        return {
            "decision_id": decision_id,
            "record_key": record_key,
            "field_key": field_key,
            "action": action,
            "accepted_value": accepted,
            "decided_at": now,
        }

    def summary(self) -> dict[str, Any]:
        records = self.records()
        documents = self.documents()
        object_census = self.object_census_candidates()
        with closing(self.connect(read_only=True)) as connection:
            media = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_candidates"
                ).fetchone()[0]
            )
            decisions = {
                "media": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM media_decisions"
                    ).fetchone()[0]
                ),
                "metadata": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM metadata_decisions"
                    ).fetchone()[0]
                ),
                "aliases": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM object_alias_decisions"
                    ).fetchone()[0]
                ),
                "documents": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM document_decisions"
                    ).fetchone()[0]
                ),
                "object_census": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM object_census_decisions"
                    ).fetchone()[0]
                ),
                "object_census_similarity": int(
                    connection.execute(
                        "SELECT COUNT(*) "
                        "FROM object_census_similarity_decisions"
                    ).fetchone()[0]
                ),
            }
        return {
            "format": FORMAT,
            "source_fingerprint": self.source_fingerprint,
            "objects": len(records),
            "media_candidates": media,
            "pending_objects": sum(
                row["review_state"] == "pending" for row in records
            ),
            "decided_objects": sum(
                row["review_state"] == "decided" for row in records
            ),
            "aliased_objects": sum(
                row["review_state"] == "aliased" for row in records
            ),
            "multiple_tiff_objects": sum(
                row["selection_state"] == "provisional_multiple_tiff_review"
                for row in records
            ),
            "no_tiff_master_objects": sum(
                bool(row["no_tiff_master"]) for row in records
            ),
            "metadata_issue_objects": sum(
                bool(row["metadata_issue_count"]) for row in records
            ),
            "metadata_review_objects": sum(
                bool(row["metadata_pending_count"]) for row in records
            ),
            "document_candidates": len(documents),
            "document_review_pending": sum(
                row["review_state"] == "pending" for row in documents
            ),
            "object_census_candidates": len(object_census),
            "object_census_pending": sum(
                row["review_state"] == "pending" for row in object_census
            ),
            "object_census_queues": dict(
                Counter(row["queue_class"] for row in object_census)
            ),
            "decisions": decisions,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kurt Schwarz reconciliation projection utilities"
    )
    parser.add_argument("--unit-root", type=Path, required=True)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument(
        "--warm-representative-previews",
        action="store_true",
        help="pre-generate the current object representative thumbnails",
    )
    parser.add_argument(
        "--import-document-review",
        type=Path,
        help="import the bound documentation review and seed reviewed decisions",
    )
    parser.add_argument(
        "--import-object-census",
        type=Path,
        help="import the bound object census",
    )
    parser.add_argument(
        "--refresh-object-census",
        action="store_true",
        help="replace the imported census projection with the same candidate IDs",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="write an immutable JSON receipt for an applied import",
    )
    parser.add_argument(
        "--auto-resolve-routine-metadata",
        action="store_true",
        help=(
            "prefer museum-digital for routine unresolved metadata and hold "
            "obvious mismatches for curator review"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append the planned metadata or import records",
    )
    parser.add_argument(
        "--actor",
        default="",
        help="named reviewer required for decision-writing operations",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="preview workers, limited to 1 or 2 (default: 2)",
    )
    arguments = parser.parse_args()
    review = KurtSchwarzReview(arguments.unit_root, arguments.review_root)
    receipt_format = ""
    operations = sum(
        (
            arguments.warm_representative_previews,
            arguments.auto_resolve_routine_metadata,
            arguments.import_document_review is not None,
            arguments.import_object_census is not None,
        )
    )
    if operations != 1:
        parser.error("choose exactly one operation")
    if arguments.warm_representative_previews:
        if arguments.apply:
            parser.error("--apply is only valid for metadata auto-resolution")
        result = review.warm_representative_previews(workers=arguments.workers)
    elif arguments.auto_resolve_routine_metadata:
        if not arguments.actor.strip():
            parser.error("--actor is required for metadata auto-resolution")
        result = review.auto_resolve_metadata(
            actor=arguments.actor,
            apply=arguments.apply,
        )
    elif arguments.import_document_review is not None:
        if not arguments.actor.strip():
            parser.error("--actor is required for document review import")
        if arguments.apply and arguments.receipt is None:
            parser.error("--receipt is required when applying a document review")
        result = review.import_document_review(
            arguments.import_document_review,
            actor=arguments.actor,
            apply=arguments.apply,
        )
        receipt_format = "iss-kurt-schwarz-document-review-import-v1"
    else:
        if not arguments.actor.strip():
            parser.error("--actor is required for object census import")
        if arguments.apply and arguments.receipt is None:
            parser.error("--receipt is required when applying an object census")
        result = review.import_object_census(
            arguments.import_object_census,
            actor=arguments.actor,
            apply=arguments.apply,
            refresh=arguments.refresh_object_census,
        )
        receipt_format = "iss-kurt-schwarz-object-census-import-v2"
    if arguments.receipt is not None:
        if not receipt_format:
            parser.error("--receipt is only valid for an import")
        receipt = arguments.receipt.resolve(strict=False)
        try:
            receipt.relative_to(arguments.review_root.resolve())
        except ValueError as error:
            parser.error(f"receipt must be inside review root: {error}")
        if result["state"] != "applied":
            parser.error("receipt is only written for a newly applied import")
        if receipt.exists():
            parser.error(f"receipt already exists: {receipt}")
        payload = {
            **result,
            "receipt_format": receipt_format,
            "reconciliation_database": str(review.database),
        }
        temporary = receipt.with_name(f".{receipt.name}.tmp-{os.getpid()}")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt)
        result["receipt"] = str(receipt)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return (
        0
        if result["state"].startswith("complete")
        or result["state"] in {"dry_run", "applied", "already_imported"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
