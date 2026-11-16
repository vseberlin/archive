"""Disposable, rebuildable reconciliation campaign state over the archive core.

Campaign rows, jobs, cached receipts, and visual files are operational working
state excluded from filesystem generations. Accepted facts and decision
receipts live in the core; deleting and re-importing this database must not
remove or reinterpret them.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import catalog_core


CAMPAIGN_SCHEMA_VERSION = 1
CAMPAIGN_DB_RELATIVE = Path("work/reconciliation/museum-digital.sqlite")
CAMPAIGN_CACHE_RELATIVE = Path("work/reconciliation/cache")

FIELD_DEFINITIONS = (
    (
        "archive_descriptive_title",
        "Descriptive title",
        "descriptive_title",
        "source_title",
        "museum_digital_title",
    ),
    (
        "archive_description",
        "Description",
        "description",
        "source_description",
        "museum_digital_description",
    ),
    (
        "archive_material_technique",
        "Material / technique",
        "material_technique",
        "source_material_technique",
        "museum_digital_material_technique",
    ),
    (
        "archive_dimensions",
        "Dimensions",
        "dimensions",
        "source_dimensions",
        "museum_digital_dimensions",
    ),
)

CONTEXT_DEFINITIONS = (
    ("object_type", "Object type", "local_object_type", "source_object_type", "museum_digital_object_type"),
    ("date", "Date context", "", "source_date", "museum_digital_event_times"),
    ("image_rights", "Image rights", "", "source_image_rights", "museum_digital_primary_image_rights"),
)

LOCAL_VISUAL_ROLES = {
    "preservation_master",
    "source_record_image",
    "complete_carrier_reference",
    "high_resolution_part",
}
EXTERNAL_VISUAL_ROLES = {"museum_digital_snapshot"}


class ReconciliationError(RuntimeError):
    """Raised when operational reconciliation state is invalid."""


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE campaign_schema (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE campaigns (
    campaign_id     TEXT PRIMARY KEY,
    adapter          TEXT NOT NULL,
    title            TEXT NOT NULL,
    source_run_path  TEXT NOT NULL,
    status           TEXT NOT NULL
                     CHECK (status IN ('active', 'completed', 'archived')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE campaign_jobs (
    job_id         TEXT PRIMARY KEY,
    campaign_id    TEXT NOT NULL REFERENCES campaigns(campaign_id),
    job_type       TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('pending', 'running', 'complete', 'failed')),
    cursor_text    TEXT NOT NULL DEFAULT '',
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    error_text     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE reconciliation_records (
    record_id              TEXT PRIMARY KEY,
    campaign_id            TEXT NOT NULL REFERENCES campaigns(campaign_id),
    source_record_key      TEXT NOT NULL,
    entity_id              TEXT,
    local_archival_id      TEXT NOT NULL DEFAULT '',
    inventory_label        TEXT NOT NULL DEFAULT '',
    external_system        TEXT NOT NULL,
    external_id            TEXT NOT NULL DEFAULT '',
    external_url           TEXT NOT NULL DEFAULT '',
    source_record_status   TEXT NOT NULL
                           CHECK (source_record_status IN (
                               'exact_inventory',
                               'not_found',
                               'not_supplied',
                               'ambiguous'
                           )),
    match_status           TEXT NOT NULL DEFAULT '',
    identity_status        TEXT NOT NULL
                           CHECK (identity_status IN (
                               'pending',
                               'resolved',
                               'rejected',
                               'deferred',
                               'blocked'
                           )),
    field_status           TEXT NOT NULL
                           CHECK (field_status IN (
                               'blocked',
                               'pending',
                               'in_progress',
                               'complete',
                               'deferred'
                           )),
    queue_status           TEXT NOT NULL
                           CHECK (queue_status IN (
                               'pending',
                               'review',
                               'complete',
                               'deferred',
                               'error'
                           )),
    captured_at            TEXT NOT NULL,
    snapshot_digest        TEXT NOT NULL DEFAULT '',
    snapshot_relpath       TEXT NOT NULL DEFAULT '',
    source_record_locator  TEXT NOT NULL DEFAULT '',
    note                   TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (campaign_id, source_record_key)
);

CREATE INDEX reconciliation_queue
ON reconciliation_records(campaign_id, queue_status, inventory_label, record_id);

CREATE INDEX reconciliation_entity
ON reconciliation_records(entity_id, campaign_id);

CREATE TABLE field_candidates (
    candidate_id     TEXT PRIMARY KEY,
    record_id        TEXT NOT NULL REFERENCES reconciliation_records(record_id),
    field_key        TEXT NOT NULL,
    field_label      TEXT NOT NULL,
    source_side      TEXT NOT NULL
                     CHECK (source_side IN ('catalog', 'local', 'external')),
    value_text       TEXT NOT NULL DEFAULT '',
    source_locator   TEXT NOT NULL DEFAULT '',
    snapshot_digest  TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL
                     CHECK (status IN ('available', 'missing', 'not_applicable')),
    UNIQUE (record_id, field_key, source_side)
);

CREATE TABLE context_values (
    context_id    TEXT PRIMARY KEY,
    record_id     TEXT NOT NULL REFERENCES reconciliation_records(record_id),
    context_key   TEXT NOT NULL,
    context_label TEXT NOT NULL,
    source_side   TEXT NOT NULL
                  CHECK (source_side IN ('catalog', 'local', 'external')),
    value_text    TEXT NOT NULL DEFAULT '',
    UNIQUE (record_id, context_key, source_side)
);

CREATE TABLE visual_evidence (
    evidence_id      TEXT PRIMARY KEY,
    record_id        TEXT NOT NULL REFERENCES reconciliation_records(record_id),
    role             TEXT NOT NULL,
    label            TEXT NOT NULL,
    cache_relpath    TEXT NOT NULL DEFAULT '',
    source_locator   TEXT NOT NULL DEFAULT '',
    content_digest   TEXT NOT NULL DEFAULT '',
    media_type       TEXT NOT NULL DEFAULT '',
    width_px         INTEGER,
    height_px        INTEGER,
    status           TEXT NOT NULL
                     CHECK (status IN (
                         'available',
                         'missing',
                         'preview_unavailable'
                     )),
    detail           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX visual_evidence_record
ON visual_evidence(record_id, role, evidence_id);

CREATE TABLE identity_decisions (
    decision_id       TEXT PRIMARY KEY,
    record_id         TEXT NOT NULL REFERENCES reconciliation_records(record_id),
    entity_id         TEXT NOT NULL,
    outcome           TEXT NOT NULL
                      CHECK (outcome IN (
                          'same_entity',
                          'component_of',
                          'wrong_master',
                          'not_same',
                          'defer'
                      )),
    parent_entity_id  TEXT NOT NULL DEFAULT '',
    actor             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    core_state        TEXT NOT NULL,
    decided_at        TEXT NOT NULL
);

CREATE INDEX identity_decisions_record
ON identity_decisions(record_id, decided_at, decision_id);

CREATE TABLE field_decisions (
    decision_id   TEXT PRIMARY KEY,
    record_id     TEXT NOT NULL REFERENCES reconciliation_records(record_id),
    entity_id     TEXT NOT NULL,
    field_key     TEXT NOT NULL,
    action        TEXT NOT NULL
                  CHECK (action IN (
                      'keep_catalog',
                      'accept_local',
                      'accept_external',
                      'corrected',
                      'defer'
                  )),
    value_text    TEXT NOT NULL DEFAULT '',
    actor         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    source_id     TEXT NOT NULL DEFAULT '',
    assertion_id  TEXT NOT NULL DEFAULT '',
    core_state    TEXT NOT NULL,
    decided_at    TEXT NOT NULL
);

CREATE INDEX field_decisions_record
ON field_decisions(record_id, field_key, decided_at, decision_id);
"""


def campaign_db_path(root: Path) -> Path:
    root = catalog_core.assert_safe_work_root(root)
    catalog_core.require_core_mode(root)
    return root / CAMPAIGN_DB_RELATIVE


def connect_campaign(root: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = campaign_db_path(root)
    if not path.is_file():
        raise ReconciliationError(f"reconciliation database does not exist: {path}")
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
    else:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = connection.execute(
        "SELECT schema_version FROM campaign_schema WHERE singleton = 1"
    ).fetchone()
    if not version or int(version["schema_version"]) != CAMPAIGN_SCHEMA_VERSION:
        connection.close()
        raise ReconciliationError("unsupported reconciliation database schema")
    return connection


def initialize_campaign_store(root: Path) -> Path:
    path = campaign_db_path(root)
    if path.exists():
        connection = connect_campaign(root)
        connection.close()
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            """INSERT INTO campaign_schema (singleton, schema_version, created_at)
               VALUES (1, ?, ?)""",
            (CAMPAIGN_SCHEMA_VERSION, catalog_core.utc_now()),
        )
        connection.commit()
    except Exception:
        connection.close()
        path.unlink(missing_ok=True)
        raise
    connection.close()
    return path


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24].upper()}"


def _safe_run_file(run: Path, relative: str) -> Path | None:
    relative = relative.strip()
    if not relative:
        return None
    candidate = (run / relative).resolve()
    if run != candidate and run not in candidate.parents:
        raise ReconciliationError(f"run manifest path escapes run directory: {relative}")
    return candidate if candidate.is_file() else None


def _copy_campaign_file(
    *,
    root: Path,
    run: Path,
    campaign_id: str,
    record_id: str,
    source: Path,
) -> str:
    cache_root = root / CAMPAIGN_CACHE_RELATIVE
    destination_dir = cache_root / campaign_id / record_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    name = f"{hashlib.sha256(str(source).encode('utf-8')).hexdigest()[:16]}-{source.name}"
    destination = destination_dir / name
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    return destination.relative_to(root).as_posix()


def _sha256_digest(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def _run_captured_at(run: Path) -> str:
    match = re.match(r"(?P<stamp>[0-9]{8}T[0-9]{6}Z)", run.name)
    if not match:
        return catalog_core.utc_now()
    try:
        parsed = datetime.strptime(
            match["stamp"],
            "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=UTC)
    except ValueError:
        return catalog_core.utc_now()
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _portable_source_locator(locator: str) -> str:
    parts = [part.strip() for part in locator.split(" / ") if part.strip()]
    if not parts:
        return ""
    first = Path(parts[0]).name if Path(parts[0]).is_absolute() else parts[0]
    return " / ".join((first, *parts[1:]))


def _resolve_entity(
    catalog: sqlite3.Connection,
    *,
    local_archival_id: str,
    inventory_label: str,
) -> str:
    values = [
        value.strip()
        for value in (local_archival_id, *inventory_label.split(";"))
        if value.strip()
    ]
    for value in values:
        row = catalog.execute(
            "SELECT entity_id FROM entities WHERE entity_id = ? AND status = 'active'",
            (value,),
        ).fetchone()
        if row:
            return str(row["entity_id"])
    matches: set[str] = set()
    for value in values:
        normalized = value.casefold()
        matches.update(
            str(row["entity_id"])
            for row in catalog.execute(
                """SELECT entity_id
                   FROM entity_identifiers
                   WHERE normalized_value = ? AND status = 'active'""",
                (normalized,),
            )
        )
        matches.update(
            str(row["entity_id"])
            for row in catalog.execute(
                """SELECT entity_id
                   FROM archive_objects
                   WHERE archival_label = ? AND status = 'active'""",
                (value,),
            )
        )
    return next(iter(matches)) if len(matches) == 1 else ""


def _catalog_field_values(
    catalog: sqlite3.Connection,
    entity_id: str,
) -> dict[str, str]:
    if not entity_id:
        return {}
    row = catalog.execute(
        """SELECT descriptive_title, description, material_technique, dimensions
           FROM archive_objects WHERE entity_id = ?""",
        (entity_id,),
    ).fetchone()
    return dict(row) if row else {}


def import_museum_digital_run(
    root: Path,
    run: Path,
    *,
    campaign_id: str = "",
    title: str = "",
    record_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Import one old read-only comparison run into replaceable campaign state."""
    root = catalog_core.assert_safe_work_root(root)
    catalog_core.require_core_mode(root)
    run = run.expanduser().resolve()
    records_path = run / "records.csv"
    visuals_path = run / "visual-comparison.csv"
    if not records_path.is_file() or not visuals_path.is_file():
        raise ReconciliationError(
            "museum-digital run requires records.csv and visual-comparison.csv"
        )
    initialize_campaign_store(root)
    campaign_id = campaign_id.strip() or f"MD-{run.name}"
    title = title.strip() or f"Museum-digital reconciliation {run.name}"
    now = catalog_core.utc_now()
    captured_at = _run_captured_at(run)
    with records_path.open(encoding="utf-8", newline="") as handle:
        source_records = list(csv.DictReader(handle))
    requested = {value.strip().casefold() for value in record_keys if value.strip()}
    if requested:
        source_records = [
            record
            for record in source_records
            if requested
            & {
                value.strip().casefold()
                for value in (
                    record.get("local_archival_id", ""),
                    record.get("local_archival_label", ""),
                    record.get("museum_digital_id", ""),
                    *record.get("local_inventory", "").split(";"),
                )
                if value.strip()
            }
        ]
        if not source_records:
            raise ReconciliationError(
                "none of the requested records exist in the comparison run"
            )
    with visuals_path.open(encoding="utf-8", newline="") as handle:
        visual_rows = list(csv.DictReader(handle))
    visuals_by_local: dict[str, list[dict[str, str]]] = {}
    for row in visual_rows:
        visuals_by_local.setdefault(row.get("local_archival_id", ""), []).append(row)

    campaign = connect_campaign(root)
    catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    imported = 0
    linked = 0
    copied = 0
    try:
        if campaign.execute(
            "SELECT 1 FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone():
            raise ReconciliationError(f"campaign already exists: {campaign_id}")
        campaign.execute("BEGIN IMMEDIATE")
        campaign.execute(
            """INSERT INTO campaigns (
                   campaign_id, adapter, title, source_run_path, status,
                   created_at, updated_at
               ) VALUES (?, 'museum-digital', ?, ?, 'active', ?, ?)""",
            (campaign_id, title, str(run), now, now),
        )
        job_id = _stable_id("JOB", campaign_id, "import")
        campaign.execute(
            """INSERT INTO campaign_jobs (
                   job_id, campaign_id, job_type, status, cursor_text,
                   attempt_count, error_text, created_at, updated_at
               ) VALUES (?, ?, 'import_run', 'running', '', 1, '', ?, ?)""",
            (job_id, campaign_id, now, now),
        )
        for index, record in enumerate(source_records, start=1):
            local_archival_id = record.get("local_archival_id", "").strip()
            inventory = record.get("local_inventory", "").strip()
            external_id = record.get("museum_digital_id", "").strip()
            source_key = local_archival_id or inventory or f"row:{index}"
            record_id = _stable_id("REC", campaign_id, source_key, external_id)
            entity_id = _resolve_entity(
                catalog,
                local_archival_id=local_archival_id,
                inventory_label=inventory,
            )
            if entity_id:
                linked += 1
            source_status = record.get("source_record_status", "").strip()
            if source_status not in {
                "exact_inventory",
                "not_found",
                "not_supplied",
                "ambiguous",
            }:
                source_status = "not_supplied"
            raw = run / "raw" / f"{external_id}.json"
            snapshot_relpath = ""
            snapshot_digest = ""
            if external_id and raw.is_file():
                snapshot_relpath = _copy_campaign_file(
                    root=root,
                    run=run,
                    campaign_id=campaign_id,
                    record_id=record_id,
                    source=raw,
                )
                snapshot_digest = _sha256_digest(raw)
                copied += 1
            source_locator = " / ".join(
                value
                for value in (
                    record.get("source_record_file", "").strip(),
                    (
                        f"sheet:{record.get('source_record_sheet', '').strip()}"
                        if record.get("source_record_sheet", "").strip()
                        else ""
                    ),
                    (
                        f"row:{record.get('source_record_row', '').strip()}"
                        if record.get("source_record_row", "").strip()
                        else ""
                    ),
                )
                if value
            )
            identity_status = (
                "blocked"
                if not entity_id or not external_id
                else "pending"
            )
            campaign.execute(
                """INSERT INTO reconciliation_records (
                       record_id, campaign_id, source_record_key, entity_id,
                       local_archival_id, inventory_label, external_system,
                       external_id, external_url, source_record_status,
                       match_status, identity_status, field_status, queue_status,
                       captured_at, snapshot_digest, snapshot_relpath,
                       source_record_locator, note, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'museum-digital', ?, ?, ?, ?,
                             ?, 'blocked', 'pending', ?, ?, ?, ?, '', ?, ?)""",
                (
                    record_id,
                    campaign_id,
                    source_key,
                    entity_id or None,
                    local_archival_id,
                    inventory,
                    external_id,
                    record.get("museum_digital_url", "").strip(),
                    source_status,
                    record.get("match_status", "").strip(),
                    identity_status,
                    captured_at,
                    snapshot_digest,
                    snapshot_relpath,
                    source_locator,
                    now,
                    now,
                ),
            )
            catalog_values = _catalog_field_values(catalog, entity_id)
            for field_key, field_label, catalog_key, local_key, external_key in FIELD_DEFINITIONS:
                candidates = (
                    ("catalog", catalog_values.get(catalog_key, ""), ""),
                    ("local", record.get(local_key, ""), source_locator),
                    (
                        "external",
                        record.get(external_key, ""),
                        record.get("museum_digital_url", "").strip(),
                    ),
                )
                for source_side, value, locator in candidates:
                    value = str(value or "").strip()
                    campaign.execute(
                        """INSERT INTO field_candidates (
                               candidate_id, record_id, field_key, field_label,
                               source_side, value_text, source_locator,
                               snapshot_digest, status
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            _stable_id("CAND", record_id, field_key, source_side),
                            record_id,
                            field_key,
                            field_label,
                            source_side,
                            value,
                            locator,
                            snapshot_digest if source_side == "external" else "",
                            "available" if value else "missing",
                        ),
                    )
            for context_key, context_label, catalog_key, local_key, external_key in CONTEXT_DEFINITIONS:
                context = (
                    ("catalog", catalog_values.get(catalog_key, "") if catalog_key else ""),
                    ("local", record.get(local_key, "")),
                    ("external", record.get(external_key, "")),
                )
                for source_side, value in context:
                    campaign.execute(
                        """INSERT INTO context_values (
                               context_id, record_id, context_key, context_label,
                               source_side, value_text
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            _stable_id("CTX", record_id, context_key, source_side),
                            record_id,
                            context_key,
                            context_label,
                            source_side,
                            str(value or "").strip(),
                        ),
                    )
            for visual in visuals_by_local.get(local_archival_id, []):
                cache_relpath = ""
                source = _safe_run_file(
                    run,
                    visual.get("snapshot_relpath", "")
                    or visual.get("evidence_relpath", ""),
                )
                if source:
                    cache_relpath = _copy_campaign_file(
                        root=root,
                        run=run,
                        campaign_id=campaign_id,
                        record_id=record_id,
                        source=source,
                    )
                    copied += 1
                width = visual.get("width", "").strip()
                height = visual.get("height", "").strip()
                status = visual.get("status", "").strip()
                if status not in {"available", "missing", "preview_unavailable"}:
                    status = "available" if cache_relpath else "missing"
                if status == "available" and not cache_relpath:
                    status = "preview_unavailable"
                campaign.execute(
                    """INSERT INTO visual_evidence (
                           evidence_id, record_id, role, label, cache_relpath,
                           source_locator, content_digest, media_type, width_px,
                           height_px, status, detail
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _stable_id(
                            "VIS",
                            record_id,
                            visual.get("image_id", ""),
                            visual.get("role", ""),
                        ),
                        record_id,
                        visual.get("role", "").strip(),
                        visual.get("label", "").strip() or "Visual evidence",
                        cache_relpath,
                        visual.get("original_path", "").strip(),
                        (
                            f"sha256:{visual.get('sha256', '').strip()}"
                            if visual.get("sha256", "").strip()
                            and not visual.get("sha256", "").startswith("sha256:")
                            else visual.get("sha256", "").strip()
                        ),
                        "",
                        int(width) if width.isdigit() else None,
                        int(height) if height.isdigit() else None,
                        status,
                        visual.get("detail", "").strip(),
                    ),
                )
            campaign.execute(
                """UPDATE campaign_jobs
                   SET cursor_text = ?, updated_at = ?
                   WHERE job_id = ?""",
                (str(index), catalog_core.utc_now(), job_id),
            )
            imported += 1
        campaign.execute(
            """UPDATE campaign_jobs
               SET status = 'complete', cursor_text = ?, updated_at = ?
               WHERE job_id = ?""",
            (str(imported), catalog_core.utc_now(), job_id),
        )
        campaign.commit()
    except Exception as error:
        campaign.rollback()
        raise ReconciliationError(str(error)) from error
    finally:
        catalog.close()
        campaign.close()
    return {
        "result": "ok",
        "campaign_id": campaign_id,
        "records": imported,
        "linked_entities": linked,
        "cached_files": copied,
        "record_filter": sorted(requested),
        "campaign_db": str(campaign_db_path(root)),
    }


def merge_visual_evidence(
    root: Path,
    run: Path,
    *,
    campaign_id: str,
    visuals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add only new immutable visual evidence to an existing campaign."""

    root = catalog_core.assert_safe_work_root(root)
    run = run.expanduser().resolve(strict=True)
    campaign = connect_campaign(root)
    inserted = 0
    unchanged = 0
    copied = 0
    try:
        if not campaign.execute(
            "SELECT 1 FROM campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone():
            raise ReconciliationError(f"unknown campaign: {campaign_id}")
        campaign.execute("BEGIN IMMEDIATE")
        for visual in visuals:
            entity_id = str(visual.get("local_archival_id") or "").strip()
            record = campaign.execute(
                """SELECT record_id FROM reconciliation_records
                   WHERE campaign_id=? AND entity_id=?""",
                (campaign_id, entity_id),
            ).fetchone()
            if not record:
                continue
            record_id = str(record["record_id"])
            evidence_id = _stable_id(
                "VIS",
                record_id,
                str(visual.get("image_id") or ""),
                str(visual.get("role") or ""),
            )
            source = _safe_run_file(
                run,
                str(
                    visual.get("snapshot_relpath")
                    or visual.get("evidence_relpath")
                    or ""
                ),
            )
            cache_relpath = ""
            if source:
                cache_relpath = _copy_campaign_file(
                    root=root,
                    run=run,
                    campaign_id=campaign_id,
                    record_id=record_id,
                    source=source,
                )
                copied += 1
            width = str(visual.get("width") or "").strip()
            height = str(visual.get("height") or "").strip()
            status = str(visual.get("status") or "").strip()
            if status not in {"available", "missing", "preview_unavailable"}:
                status = "available" if cache_relpath else "missing"
            if status == "available" and not cache_relpath:
                status = "preview_unavailable"
            digest = str(visual.get("sha256") or "").strip()
            if digest and not digest.startswith("sha256:"):
                digest = f"sha256:{digest}"
            expected = {
                "record_id": record_id,
                "role": str(visual.get("role") or "").strip(),
                "label": str(visual.get("label") or "").strip()
                or "Visual evidence",
                "cache_relpath": cache_relpath,
                "source_locator": str(visual.get("original_path") or "").strip(),
                "content_digest": digest,
                "media_type": "",
                "width_px": int(width) if width.isdigit() else None,
                "height_px": int(height) if height.isdigit() else None,
                "status": status,
                "detail": str(visual.get("detail") or "").strip(),
            }
            existing = campaign.execute(
                "SELECT * FROM visual_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if existing:
                if any(existing[key] != value for key, value in expected.items()):
                    raise ReconciliationError(
                        f"visual evidence changed after import: {evidence_id}"
                    )
                unchanged += 1
                continue
            campaign.execute(
                """INSERT INTO visual_evidence (
                       evidence_id, record_id, role, label, cache_relpath,
                       source_locator, content_digest, media_type, width_px,
                       height_px, status, detail
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, *expected.values()),
            )
            inserted += 1
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise
    finally:
        campaign.close()
    return {
        "result": "ok",
        "inserted": inserted,
        "unchanged": unchanged,
        "cached_files": copied,
    }


def campaign_summary(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {
        "campaigns": int(
            connection.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        ),
        "records": int(
            connection.execute(
                "SELECT count(*) FROM reconciliation_records"
            ).fetchone()[0]
        ),
    }
    for status, count in connection.execute(
        """SELECT queue_status, count(*)
           FROM reconciliation_records GROUP BY queue_status"""
    ):
        counts[str(status)] = int(count)
    return counts


def _core_decision_payload(
    catalog: sqlite3.Connection,
    *,
    action: str,
    decision_id: str,
) -> dict[str, Any] | None:
    row = catalog.execute(
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
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def campaign_health(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
) -> dict[str, Any]:
    """Compare cached campaign receipts with durable core decisions."""
    issues: list[dict[str, str]] = []

    def issue(decision_id: str, code: str, detail: str) -> None:
        issues.append(
            {
                "decision_id": decision_id,
                "code": code,
                "detail": detail,
            }
        )

    for row in campaign.execute(
        """SELECT d.*, r.external_system, r.external_id
           FROM identity_decisions d
           JOIN reconciliation_records r ON r.record_id = d.record_id
           ORDER BY d.decided_at, d.decision_id"""
    ):
        decision_id = str(row["decision_id"])
        payload = _core_decision_payload(
            catalog,
            action="review_external_identity",
            decision_id=decision_id,
        )
        if not payload:
            issue(
                decision_id,
                "core_receipt_missing",
                "campaign identity decision has no durable core logbook receipt",
            )
            continue
        expected = {
            "entity_id": str(row["entity_id"]),
            "external_system": str(row["external_system"]),
            "external_id": str(row["external_id"]),
            "outcome": str(row["outcome"]),
            "parent_entity_id": str(row["parent_entity_id"]),
        }
        if any(payload.get(key, "") != value for key, value in expected.items()):
            issue(
                decision_id,
                "core_receipt_mismatch",
                "campaign identity decision differs from its core logbook receipt",
            )
            continue
        accepted = row["outcome"] in ("same_entity", "component_of")
        expected_states = (
            {"unpublished", "unchanged"}
            if accepted
            else {"reviewed", "unchanged"}
        )
        if row["core_state"] not in expected_states:
            issue(
                decision_id,
                "cached_core_state_mismatch",
                "cached identity core_state is inconsistent with its outcome",
            )
        if accepted:
            identifier_id = str(payload.get("identifier_id") or "")
            alias = catalog.execute(
                """SELECT entity_id, status
                   FROM entity_identifiers WHERE identifier_id = ?""",
                (identifier_id,),
            ).fetchone()
            if (
                not alias
                or alias["entity_id"] != row["entity_id"]
                or alias["status"] != "active"
            ):
                issue(
                    decision_id,
                    "accepted_alias_missing",
                    "accepted external identity is no longer an active core alias",
                )
            source_id = str(payload.get("source_id") or "")
            if not catalog.execute(
                "SELECT 1 FROM evidence_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone():
                issue(
                    decision_id,
                    "identity_source_missing",
                    "accepted identity evidence source is missing from the core",
                )
        if row["outcome"] == "component_of":
            relation_id = str(payload.get("relation_id") or "")
            relation = catalog.execute(
                """SELECT status FROM entity_relations WHERE relation_id = ?""",
                (relation_id,),
            ).fetchone()
            if not relation or relation["status"] != "accepted":
                issue(
                    decision_id,
                    "component_relation_missing",
                    "accepted component relationship is not active in the core",
                )

    for row in campaign.execute(
        """SELECT * FROM field_decisions
           ORDER BY decided_at, decision_id"""
    ):
        decision_id = str(row["decision_id"])
        payload = _core_decision_payload(
            catalog,
            action="accept_reconciliation_field",
            decision_id=decision_id,
        )
        if not payload:
            issue(
                decision_id,
                "core_receipt_missing",
                "campaign field decision has no durable core logbook receipt",
            )
            continue
        expected = {
            "entity_id": str(row["entity_id"]),
            "predicate": str(row["field_key"]),
            "action": str(row["action"]),
            "value_text": str(row["value_text"]),
        }
        if any(payload.get(key, "") != value for key, value in expected.items()):
            issue(
                decision_id,
                "core_receipt_mismatch",
                "campaign field decision differs from its core logbook receipt",
            )
            continue
        accepted = row["action"] != "defer"
        expected_states = (
            {"unpublished", "unchanged"}
            if accepted
            else {"reviewed", "unchanged"}
        )
        if row["core_state"] not in expected_states:
            issue(
                decision_id,
                "cached_core_state_mismatch",
                "cached field core_state is inconsistent with its action",
            )
        if accepted:
            if (
                str(payload.get("source_id") or "") != row["source_id"]
                or str(payload.get("assertion_id") or "") != row["assertion_id"]
            ):
                issue(
                    decision_id,
                    "accepted_fact_receipt_mismatch",
                    "cached evidence IDs differ from the core decision receipt",
                )
                continue
            assertion = catalog.execute(
                """SELECT status FROM evidence_assertions
                   WHERE assertion_id = ? AND source_id = ?""",
                (row["assertion_id"], row["source_id"]),
            ).fetchone()
            if not assertion or assertion["status"] not in ("accepted", "superseded"):
                issue(
                    decision_id,
                    "accepted_assertion_missing",
                    "accepted field assertion is missing from durable core state",
                )

    return {
        "status": "yellow" if issues else "safe",
        "issues": issues,
        "issue_count": len(issues),
    }


def list_records(
    connection: sqlite3.Connection,
    *,
    campaign_id: str = "",
    queue_status: str = "",
    query: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if campaign_id:
        clauses.append("campaign_id = ?")
        parameters.append(campaign_id)
    if queue_status:
        clauses.append("queue_status = ?")
        parameters.append(queue_status)
    if query:
        clauses.append(
            """(inventory_label LIKE ? OR local_archival_id LIKE ?
                OR external_id LIKE ? OR entity_id LIKE ?)"""
        )
        term = f"%{query}%"
        parameters.extend((term, term, term, term))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.extend((max(1, min(limit, 250)), max(0, offset)))
    return list(
        connection.execute(
            f"""SELECT *
                FROM reconciliation_records
                {where}
                ORDER BY
                  CASE queue_status
                    WHEN 'pending' THEN 0
                    WHEN 'review' THEN 1
                    WHEN 'deferred' THEN 2
                    WHEN 'error' THEN 3
                    ELSE 4
                  END,
                  inventory_label,
                  record_id
                LIMIT ? OFFSET ?""",
            parameters,
        )
    )


def record_bundle(
    connection: sqlite3.Connection,
    record_id: str,
) -> dict[str, Any]:
    record = connection.execute(
        "SELECT * FROM reconciliation_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if not record:
        raise ReconciliationError(f"unknown reconciliation record: {record_id}")
    return {
        "record": record,
        "fields": list(
            connection.execute(
                """SELECT * FROM field_candidates
                   WHERE record_id = ?
                   ORDER BY field_key,
                     CASE source_side
                       WHEN 'catalog' THEN 0
                       WHEN 'local' THEN 1
                       ELSE 2
                     END""",
                (record_id,),
            )
        ),
        "context": list(
            connection.execute(
                """SELECT * FROM context_values
                   WHERE record_id = ?
                   ORDER BY context_key, source_side""",
                (record_id,),
            )
        ),
        "visuals": list(
            connection.execute(
                """SELECT * FROM visual_evidence
                   WHERE record_id = ? ORDER BY role, evidence_id""",
                (record_id,),
            )
        ),
        "identity_decisions": list(
            connection.execute(
                """SELECT * FROM identity_decisions
                   WHERE record_id = ?
                   ORDER BY decided_at DESC, decision_id DESC""",
                (record_id,),
            )
        ),
        "field_decisions": list(
            connection.execute(
                """SELECT * FROM field_decisions
                   WHERE record_id = ?
                   ORDER BY decided_at DESC, decision_id DESC""",
                (record_id,),
            )
        ),
    }


def link_record_entity(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
    *,
    record_id: str,
    entity_id: str,
) -> None:
    if not catalog.execute(
        """SELECT 1 FROM entities
           WHERE entity_id = ? AND status IN ('active', 'provisional')""",
        (entity_id,),
    ).fetchone():
        raise ReconciliationError(f"unknown active core entity: {entity_id}")
    now = catalog_core.utc_now()
    campaign.execute("BEGIN IMMEDIATE")
    try:
        cursor = campaign.execute(
            """UPDATE reconciliation_records
               SET entity_id = ?, identity_status = 'pending',
                   field_status = 'blocked', queue_status = 'pending',
                   updated_at = ?
               WHERE record_id = ?""",
            (entity_id, now, record_id),
        )
        if cursor.rowcount != 1:
            raise ReconciliationError(f"unknown reconciliation record: {record_id}")
        values = _catalog_field_values(catalog, entity_id)
        for field_key, _label, catalog_key, _local, _external in FIELD_DEFINITIONS:
            value = values.get(catalog_key, "")
            campaign.execute(
                """UPDATE field_candidates
                   SET value_text = ?, status = ?
                   WHERE record_id = ? AND field_key = ?
                     AND source_side = 'catalog'""",
                (
                    value,
                    "available" if value else "missing",
                    record_id,
                    field_key,
                ),
            )
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise


def _visual_identity_ready(
    connection: sqlite3.Connection,
    record_id: str,
) -> bool:
    roles = {
        str(row["role"])
        for row in connection.execute(
            """SELECT role FROM visual_evidence
               WHERE record_id = ? AND status = 'available'
                 AND cache_relpath <> ''""",
            (record_id,),
        )
    }
    return bool(roles & LOCAL_VISUAL_ROLES) and bool(roles & EXTERNAL_VISUAL_ROLES)


def _local_identity_ready(
    connection: sqlite3.Connection,
    record_id: str,
) -> bool:
    """Require both a usable local image and local descriptive metadata."""

    visual = connection.execute(
        """SELECT 1 FROM visual_evidence
           WHERE record_id=? AND role IN (
               'preservation_master', 'source_record_image',
               'complete_carrier_reference', 'high_resolution_part'
           )
             AND status='available' AND cache_relpath<>''
           LIMIT 1""",
        (record_id,),
    ).fetchone()
    metadata = connection.execute(
        """SELECT 1 FROM field_candidates
           WHERE record_id=? AND source_side='local'
             AND status='available' AND trim(value_text)<>''
           LIMIT 1""",
        (record_id,),
    ).fetchone()
    return bool(visual and metadata)


def _selected_exact_inventory_source(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
    record: sqlite3.Row,
) -> str:
    """Return one selected source whose filename is the exact inventory label."""

    inventory = str(record["inventory_label"] or "").strip()
    if not inventory:
        return ""
    reviews = catalog_core.source_candidate_reviews(
        catalog,
        entity_id=str(record["entity_id"]),
    )
    matches: list[str] = []
    for row in campaign.execute(
        """SELECT evidence_id, source_locator FROM visual_evidence
           WHERE record_id=? AND role IN (
               'preservation_master', 'source_record_image',
               'complete_carrier_reference', 'high_resolution_part'
           )
             AND status='available' AND cache_relpath<>''""",
        (record["record_id"],),
    ):
        review = reviews.get(str(row["evidence_id"]), {})
        if review.get("state") != "selected_master":
            continue
        source = str(row["source_locator"] or "").strip()
        if Path(source).stem.casefold() == inventory.casefold():
            matches.append(source)
    return matches[0] if len(matches) == 1 else ""


def review_identity(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
    *,
    record_id: str,
    outcome: str,
    actor: str,
    reason: str,
    parent_entity_id: str = "",
) -> dict[str, Any]:
    record = campaign.execute(
        "SELECT * FROM reconciliation_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if not record or not record["entity_id"]:
        raise ReconciliationError("reconciliation record must be linked to a core entity")
    actor = actor.strip()
    reason = reason.strip()
    parent_entity_id = parent_entity_id.strip()
    latest = campaign.execute(
        """SELECT * FROM identity_decisions WHERE record_id = ?
           ORDER BY decided_at DESC, decision_id DESC LIMIT 1""",
        (record_id,),
    ).fetchone()
    if latest and all(
        str(latest[key] or "") == expected
        for key, expected in (
            ("outcome", outcome),
            ("parent_entity_id", parent_entity_id),
            ("actor", actor),
            ("reason", reason),
        )
    ):
        return {
            "result": "ok",
            "decision_id": str(latest["decision_id"]),
            "catalog_revision": int(catalog_core.catalog_state(catalog)["catalog_revision"]),
            "state": "unchanged",
            "entity_id": str(record["entity_id"]),
            "outcome": outcome,
            "parent_entity_id": parent_entity_id,
        }
    if outcome in ("same_entity", "component_of") and not _visual_identity_ready(
        campaign, record_id
    ):
        raise ReconciliationError(
            "accepted identity requires available local and external visual evidence"
        )
    version = int(
        campaign.execute(
            "SELECT count(*) FROM identity_decisions WHERE record_id = ?",
            (record_id,),
        ).fetchone()[0]
    ) + 1
    decision_id = _stable_id("MD-IDENTITY", record_id, str(version))
    result = catalog_core.review_external_identity(
        catalog,
        decision_id=decision_id,
        entity_id=str(record["entity_id"]),
        external_system=str(record["external_system"]),
        external_id=str(record["external_id"]),
        outcome=outcome,
        actor=actor,
        reason=reason,
        parent_entity_id=parent_entity_id,
        captured_at=str(record["captured_at"]),
        snapshot_digest=str(record["snapshot_digest"]),
        relative_path="",
        citation=str(record["external_url"]),
    )
    identity_status = {
        "same_entity": "resolved",
        "component_of": "resolved",
        "wrong_master": "rejected",
        "not_same": "rejected",
        "defer": "deferred",
    }[outcome]
    field_status = "pending" if identity_status == "resolved" else "blocked"
    queue_status = (
        "review"
        if identity_status == "resolved"
        else "deferred"
        if identity_status == "deferred"
        else "complete"
    )
    campaign.execute("BEGIN IMMEDIATE")
    try:
        campaign.execute(
            """INSERT OR IGNORE INTO identity_decisions (
                   decision_id, record_id, entity_id, outcome,
                   parent_entity_id, actor, reason, core_state, decided_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                record_id,
                record["entity_id"],
                outcome,
                parent_entity_id,
                actor,
                reason,
                result["state"],
                catalog_core.utc_now(),
            ),
        )
        campaign.execute(
            """UPDATE reconciliation_records
               SET identity_status = ?, field_status = ?, queue_status = ?,
                   updated_at = ?
               WHERE record_id = ?""",
            (
                identity_status,
                field_status,
                queue_status,
                catalog_core.utc_now(),
                record_id,
            ),
        )
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise
    return result


def review_local_identity(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
    *,
    record_id: str,
    outcome: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Review an exact local record when no external record was supplied."""

    record = campaign.execute(
        "SELECT * FROM reconciliation_records WHERE record_id=?",
        (record_id,),
    ).fetchone()
    if not record or not record["entity_id"]:
        raise ReconciliationError(
            "local reconciliation record must be linked to a core entity"
        )
    if str(record["external_id"] or ""):
        raise ReconciliationError(
            "local-only identity cannot replace an available external identity"
        )
    inventory = str(record["inventory_label"] or "").strip()
    exact_source_record = (
        record["source_record_status"] == "exact_inventory"
        and str(record["local_archival_id"] or "").casefold()
        == inventory.casefold()
    )
    selected_inventory_source = _selected_exact_inventory_source(
        campaign,
        catalog,
        record,
    )
    if not exact_source_record and not selected_inventory_source:
        raise ReconciliationError(
            "local identity requires one selected source whose filename exactly "
            "matches the inventory label"
        )
    if (
        outcome == "same_entity"
        and exact_source_record
        and not _local_identity_ready(campaign, record_id)
    ):
        raise ReconciliationError(
            "accepted local identity requires available local image and metadata evidence"
        )
    actor = actor.strip()
    reason = reason.strip()
    latest = campaign.execute(
        """SELECT * FROM identity_decisions WHERE record_id=?
           ORDER BY decided_at DESC, decision_id DESC LIMIT 1""",
        (record_id,),
    ).fetchone()
    if latest and all(
        str(latest[key] or "") == expected
        for key, expected in (
            ("outcome", outcome),
            ("parent_entity_id", ""),
            ("actor", actor),
            ("reason", reason),
        )
    ):
        return {
            "result": "ok",
            "decision_id": str(latest["decision_id"]),
            "catalog_revision": int(
                catalog_core.catalog_state(catalog)["catalog_revision"]
            ),
            "state": "unchanged",
            "entity_id": str(record["entity_id"]),
            "outcome": outcome,
        }
    if outcome not in {"same_entity", "wrong_master", "defer"}:
        raise ReconciliationError(
            "unsupported local-only identity decision"
        )
    version = int(
        campaign.execute(
            "SELECT count(*) FROM identity_decisions WHERE record_id=?",
            (record_id,),
        ).fetchone()[0]
    ) + 1
    decision_id = _stable_id("LOCAL-IDENTITY", record_id, str(version))
    result = catalog_core.review_local_identity(
        catalog,
        decision_id=decision_id,
        entity_id=str(record["entity_id"]),
        local_archival_id=inventory,
        source_record_locator=(
            selected_inventory_source
            or str(record["source_record_locator"])
        ),
        outcome=outcome,
        actor=actor,
        reason=reason,
    )
    identity_status = {
        "same_entity": "resolved",
        "wrong_master": "rejected",
        "defer": "deferred",
    }[outcome]
    field_status = "pending" if identity_status == "resolved" else "blocked"
    queue_status = "review" if identity_status == "resolved" else "deferred"
    campaign.execute("BEGIN IMMEDIATE")
    try:
        campaign.execute(
            """INSERT INTO identity_decisions (
                   decision_id, record_id, entity_id, outcome,
                   parent_entity_id, actor, reason, core_state, decided_at
               ) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)""",
            (
                decision_id,
                record_id,
                record["entity_id"],
                outcome,
                actor,
                reason,
                result["state"],
                catalog_core.utc_now(),
            ),
        )
        campaign.execute(
            """UPDATE reconciliation_records
               SET identity_status=?, field_status=?, queue_status=?,
                   updated_at=?
               WHERE record_id=?""",
            (
                identity_status,
                field_status,
                queue_status,
                catalog_core.utc_now(),
                record_id,
            ),
        )
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise
    return result


def decide_field(
    campaign: sqlite3.Connection,
    catalog: sqlite3.Connection,
    *,
    record_id: str,
    field_key: str,
    action: str,
    actor: str,
    reason: str,
    corrected_value: str = "",
) -> dict[str, Any]:
    record = campaign.execute(
        "SELECT * FROM reconciliation_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if (
        not record
        or not record["entity_id"]
        or record["identity_status"] != "resolved"
    ):
        raise ReconciliationError(
            "field decisions require a resolved identity and linked core entity"
        )
    source_side = {
        "keep_catalog": "catalog",
        "accept_local": "local",
        "accept_external": "external",
    }.get(action, "")
    candidate = None
    if source_side:
        candidate = campaign.execute(
            """SELECT * FROM field_candidates
               WHERE record_id = ? AND field_key = ? AND source_side = ?""",
            (record_id, field_key, source_side),
        ).fetchone()
        if not candidate or candidate["status"] != "available":
            raise ReconciliationError("selected metadata candidate is unavailable")
    value = (
        corrected_value.strip()
        if action == "corrected"
        else str(candidate["value_text"]) if candidate else ""
    )
    actor = actor.strip()
    reason = reason.strip()
    latest = campaign.execute(
        """SELECT * FROM field_decisions
           WHERE record_id = ? AND field_key = ?
           ORDER BY decided_at DESC, decision_id DESC LIMIT 1""",
        (record_id, field_key),
    ).fetchone()
    if latest and all(
        str(latest[key] or "") == expected
        for key, expected in (
            ("action", action),
            ("value_text", value),
            ("actor", actor),
            ("reason", reason),
        )
    ):
        return {
            "result": "ok",
            "decision_id": str(latest["decision_id"]),
            "catalog_revision": int(catalog_core.catalog_state(catalog)["catalog_revision"]),
            "state": "unchanged",
            "entity_id": str(record["entity_id"]),
            "predicate": field_key,
            "action": action,
            "value_text": value,
        }
    version = int(
        campaign.execute(
            """SELECT count(*) FROM field_decisions
               WHERE record_id = ? AND field_key = ?""",
            (record_id, field_key),
        ).fetchone()[0]
    ) + 1
    decision_id = _stable_id("MD-FIELD", record_id, field_key, str(version))
    source_type = {
        "keep_catalog": "catalog_state",
        "accept_local": "local_source_record",
        "accept_external": "external_snapshot",
        "corrected": "editor_correction",
        "defer": "reconciliation_defer",
    }.get(action, "")
    if not source_type:
        raise ReconciliationError(f"unsupported field decision action: {action}")
    snapshot_digest = (
        str(candidate["snapshot_digest"]) if candidate else ""
    )
    citation = (
        _portable_source_locator(str(candidate["source_locator"]))
        if candidate and action == "accept_local"
        else str(candidate["source_locator"])
        if candidate
        else reason
    )
    result = catalog_core.accept_reconciliation_text(
        catalog,
        decision_id=decision_id,
        entity_id=str(record["entity_id"]),
        predicate=field_key,
        action=action,
        value_text=value,
        actor=actor,
        reason=reason,
        external_system=str(record["external_system"]),
        external_id=str(record["external_id"]),
        local_search_status=str(record["source_record_status"]),
        source_type=source_type,
        captured_at=str(record["captured_at"]),
        snapshot_digest=snapshot_digest,
        relative_path="",
        citation=citation,
    )
    now = catalog_core.utc_now()
    campaign.execute("BEGIN IMMEDIATE")
    try:
        campaign.execute(
            """INSERT OR IGNORE INTO field_decisions (
                   decision_id, record_id, entity_id, field_key, action,
                   value_text, actor, reason, source_id, assertion_id,
                   core_state, decided_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                record_id,
                record["entity_id"],
                field_key,
                action,
                value,
                actor,
                reason,
                result.get("source_id", ""),
                result.get("assertion_id", ""),
                result["state"],
                now,
            ),
        )
        if action != "defer":
            campaign.execute(
                """UPDATE field_candidates
                   SET value_text = ?, status = 'available',
                       source_locator = 'accepted core catalog state',
                       snapshot_digest = ''
                   WHERE record_id = ? AND field_key = ?
                     AND source_side = 'catalog'""",
                (value, record_id, field_key),
            )
        latest: dict[str, str] = {}
        for row in campaign.execute(
            """SELECT field_key, action
               FROM field_decisions
               WHERE record_id = ?
               ORDER BY decided_at, decision_id""",
            (record_id,),
        ):
            latest[str(row["field_key"])] = str(row["action"])
        expected = {definition[0] for definition in FIELD_DEFINITIONS}
        accepted = {
            key for key, latest_action in latest.items() if latest_action != "defer"
        }
        if expected <= accepted:
            field_status = "complete"
            queue_status = "complete"
        elif latest and all(latest_action == "defer" for latest_action in latest.values()):
            field_status = "deferred"
            queue_status = "deferred"
        else:
            field_status = "in_progress"
            queue_status = "review"
        campaign.execute(
            """UPDATE reconciliation_records
               SET field_status = ?, queue_status = ?, updated_at = ?
               WHERE record_id = ?""",
            (field_status, queue_status, now, record_id),
        )
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise
    return result


def safe_cached_evidence_path(root: Path, evidence_id: str) -> Path:
    connection = connect_campaign(root, read_only=True)
    try:
        row = connection.execute(
            """SELECT cache_relpath
               FROM visual_evidence
               WHERE evidence_id = ? AND status = 'available'""",
            (evidence_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row or not row["cache_relpath"]:
        raise ReconciliationError("visual evidence is not available")
    cache_root = (root / CAMPAIGN_CACHE_RELATIVE).resolve()
    path = (root / str(row["cache_relpath"])).resolve()
    if cache_root != path and cache_root not in path.parents:
        raise ReconciliationError("visual evidence path escapes campaign cache")
    if not path.is_file():
        raise ReconciliationError("cached visual evidence is missing")
    return path


def attach_visual_preview(
    root: Path,
    *,
    evidence_id: str,
    source_path: Path,
) -> dict[str, Any]:
    """Create one disposable preview from a verified read-only evidence file."""
    root = catalog_core.assert_safe_work_root(root)
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise ReconciliationError(f"visual evidence source does not exist: {source_path}")
    connection = connect_campaign(root)
    try:
        row = connection.execute(
            """SELECT v.*, r.campaign_id
               FROM visual_evidence v
               JOIN reconciliation_records r ON r.record_id = v.record_id
               WHERE v.evidence_id = ?""",
            (evidence_id,),
        ).fetchone()
        if not row:
            raise ReconciliationError(f"unknown visual evidence: {evidence_id}")
        expected = str(row["content_digest"] or "")
        observed = f"sha256:{catalog_core.sha256_file(source_path)}"
        if expected and observed != expected:
            raise ReconciliationError(
                "visual evidence source digest differs from the campaign manifest"
            )
        cache_dir = (
            root
            / CAMPAIGN_CACHE_RELATIVE
            / str(row["campaign_id"])
            / str(row["record_id"])
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        destination = cache_dir / f"preview-{evidence_id}.jpg"
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        magick = shutil.which("magick")
        if not magick:
            raise ReconciliationError(
                "ImageMagick is required to create this visual preview"
            )
        try:
            completed = subprocess.run(
                [
                    magick,
                    f"{source_path}[0]",
                    "-auto-orient",
                    "-thumbnail",
                    "1800x1800>",
                    "-strip",
                    "-quality",
                    "88",
                    f"jpg:{temporary}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0 or not temporary.is_file():
                raise ReconciliationError(
                    "ImageMagick preview failed: "
                    + (completed.stderr.strip() or "no output produced")
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE visual_evidence
               SET cache_relpath = ?, status = 'available',
                   detail = CASE
                     WHEN detail = '' THEN 'Disposable verified preview'
                     ELSE detail || ' — disposable verified preview'
                   END
               WHERE evidence_id = ?""",
            (destination.relative_to(root).as_posix(), evidence_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "result": "ok",
        "evidence_id": evidence_id,
        "source_digest": observed,
        "preview": str(destination),
        "preview_bytes": destination.stat().st_size,
    }
