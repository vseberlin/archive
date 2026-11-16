#!/usr/bin/env python3
"""Local human-review workbench for WF-Alben Master selection.

The workbench reads the approved collection and candidate projections, stores
one current human decision per object in a small SQLite index, and mirrors the
decisions to CSV after every save. Source media is previewed in memory and is
never modified. No action creates accession, drain, or deletion authority.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


VERSION = "1.4.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-alben")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation"
OBJECTS_CSV = REVIEW_ROOT / "collection-records.csv"
CANDIDATES_CSV = REVIEW_ROOT / "candidate-files.csv"
QUEUE_CSV = REVIEW_ROOT / "review-queue.csv"
METADATA_CSV = REVIEW_ROOT / "metadata-crosswalk.csv"
MD_CAPTURE_RAW = Path(
    "/mnt/volume/iss-archive-work/collection-607/work/reconciliation/museum-digital/"
    "runs/20260822T111320Z-collection-607/raw"
)
DATABASE = REVIEW_ROOT / "workbench.sqlite"
DECISIONS_CSV = REVIEW_ROOT / "workbench-decisions.csv"
DISPOSITIONS_CSV = REVIEW_ROOT / "workbench-candidate-dispositions.csv"
PREFLIGHT = REVIEW_ROOT / "workbench-preflight.json"
BULK_UNIQUE_TIFF_RECEIPT = REVIEW_ROOT / "bulk-unique-tiff-selection.json"
LINKED_65080_RECEIPT = REVIEW_ROOT / "linked-object-65080.json"
LINKED_60243_RECEIPT = REVIEW_ROOT / "linked-object-60243.json"
LINKED_RELATIONSHIPS_CSV = REVIEW_ROOT / "linked-object-relationships.csv"
DERIVATIVE_REPAIR_RECEIPT = REVIEW_ROOT / "linked-derivative-correction.json"
DERIVATIVE_RELATIONSHIPS_CSV = REVIEW_ROOT / "derived-image-relationships.csv"
CATALOGUE_SURROGATE_RECEIPT = REVIEW_ROOT / "catalogue-surrogate-resolution.json"
CATALOGUE_SURROGATES_CSV = REVIEW_ROOT / "catalogue-surrogates.csv"
METADATA_EDITORIAL_RECEIPT = REVIEW_ROOT / "metadata-editorial-resolution.json"
METADATA_EDITORIAL_CSV = REVIEW_ROOT / "metadata-editorial-decisions.csv"
CATALOGUE_SURROGATE_PATHS = {
    "md:60243:image:43750": REVIEW_ROOT / "catalogue-surrogate-md-60243-image-43750.jpg",
    "md:60256": REVIEW_ROOT / "catalogue-surrogate-md-60256-image-43824.jpg",
}

DECISION_FIELDS = (
    "record_key", "md_object_id", "inventory", "decision_state",
    "selected_candidate_id", "selected_master_path", "note", "decided_by",
    "decided_at", "linked_record_key", "link_relation", "component_label",
    "linked_master_path", "drain_list_state", "deletion_authorized",
)
DISPOSITION_FIELDS = (
    "record_key", "md_object_id", "inventory", "candidate_id", "source_path",
    "disposition", "decision_basis", "linked_record_key", "linked_master_path",
    "drain_list_state", "deletion_authorized",
)
METADATA_EDITORIAL_FIELDS = (
    "md_object_id", "inventory", "field_name", "original_value", "resolved_value",
    "decision_state", "basis", "decided_by", "decided_at",
    "deletion_authorized",
)


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def human_size(value: Any) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise WorkbenchError("Elements did not resolve to one filesystem")
    mount = {key: str(value) for key, value in filesystems[0].items()}
    options = set(mount.get("options", "").split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise WorkbenchError(f"Elements read-only mount guard failed: {mount}")
    return mount


def inside_source(path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def allowed_candidate_path(path: Path) -> bool:
    if inside_source(path):
        return True
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved in {candidate.resolve(strict=True) for candidate in CATALOGUE_SURROGATE_PATHS.values() if candidate.exists()}


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise WorkbenchError(f"required projection missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_md_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(MD_CAPTURE_RAW.glob("page-*.json")):
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        objects = payload.get("results", {}).get("objects", {})
        if not isinstance(objects, dict):
            raise WorkbenchError(f"invalid museum-digital capture page: {path}")
        for object_id, record in objects.items():
            if object_id in records or not isinstance(record, dict):
                raise WorkbenchError(f"duplicate or invalid museum-digital record: {object_id}")
            records[str(object_id)] = record
    if len(records) != 178:
        raise WorkbenchError(f"expected 178 captured museum-digital records, found {len(records)}")
    return records


def md_image_url(image: dict[str, Any]) -> str:
    folder = str(image.get("folder") or "").strip("/")
    filename = str(image.get("filename_loc") or "").strip("/")
    if not folder.startswith("images/") or not filename or "/" in filename:
        return ""
    path = urllib.parse.quote(f"{folder}/{filename}", safe="/")
    return f"https://assets.museum-digital.org/berlin/{path}"


def effective_md_record(record: dict[str, Any] | None, object_id: str) -> dict[str, Any] | None:
    if record is None:
        return None
    effective = copy.deepcopy(record)
    with connect(read_only=True) as connection:
        if not table_exists(connection, "metadata_editorial_decisions"):
            return effective
        rows = connection.execute(
            """SELECT field_name, resolved_value FROM metadata_editorial_decisions
                WHERE md_object_id=? AND decision_state!='unresolved'""",
            (object_id,),
        ).fetchall()
    for row in rows:
        field = row["field_name"]
        if field in {"object_name", "object_description"}:
            effective[field] = row["resolved_value"]
            continue
        match = re.fullmatch(r"image:(\d+):(rights|name)", field)
        if match:
            for image in effective.get("object_images") or []:
                if str(image.get("quell_id")) == match.group(1):
                    image[match.group(2)] = row["resolved_value"]
    return effective


def md_record_panel(
    record: dict[str, Any] | None, official_url: str, md_image_id: str = ""
) -> str:
    if not record:
        return "<section class='notice'>Captured museum-digital comparison record unavailable.</section>"
    images = []
    all_images = record.get("object_images") or []
    selected_images = [
        image for image in all_images
        if not md_image_id or str(image.get("quell_id") or "") == str(md_image_id)
    ]
    for number, image in enumerate(selected_images, 1):
        source = md_image_url(image)
        if not source:
            continue
        label = image.get("name") or record.get("object_name") or f"museum-digital image {number}"
        images.append(
            f"""<figure><a href="{esc(source)}" target="_blank" rel="noreferrer"><img class="md-preview" src="{esc(source)}" alt="{esc(label)}"></a>
<figcaption>museum-digital image {esc(image.get('quell_id') or number)} · {esc(image.get('beschreibung') or image.get('name') or '')}<br>{esc(image.get('rights') or 'rights not stated')} · {esc(image.get('owner') or '')}</figcaption></figure>"""
        )
    tags = "; ".join(
        str(item.get("tag_name") or "") for item in (record.get("object_tags") or []) if item.get("tag_name")
    )
    collections = "; ".join(
        str(item.get("collection_name") or "")
        for item in (record.get("object_collection") or []) if item.get("collection_name")
    )
    event_parts = []
    for event in record.get("object_events") or []:
        people = event.get("people") if isinstance(event.get("people"), dict) else {}
        place = event.get("place") if isinstance(event.get("place"), dict) else {}
        time = event.get("time") if isinstance(event.get("time"), dict) else {}
        event_parts.append(
            " · ".join(
                part for part in (
                    str(event.get("event_type_name") or ""),
                    str(time.get("time_name") or ""),
                    str(people.get("displayname") or ""),
                    str(place.get("place_name") or ""),
                ) if part
            )
        )
    related_people = "; ".join(
        str((item.get("people") or {}).get("displayname") or "")
        for item in (record.get("object_relation_people") or [])
        if isinstance(item.get("people"), dict) and (item.get("people") or {}).get("displayname")
    )
    licence = record.get("licence") if isinstance(record.get("licence"), dict) else {}
    return f"""<section class="md-record"><div class="notice ok"><b>Comparison authority: current captured museum-digital record.</b> Compare every local candidate below with this image and metadata.</div>
<div class="md-layout"><div class="md-images">{''.join(images) if images else '<p class="notice">No captured MD image.</p>'}</div>
<article><span class="badge">museum-digital reference</span><h2>{esc(record.get('object_inventory_number'))}</h2>
<h3>{esc(record.get('object_name'))}</h3><p><a href="{esc(official_url)}" target="_blank" rel="noreferrer">Open official museum-digital object {esc(record.get('object_id'))}</a></p>
<p>{esc(record.get('object_description')) or 'No description'}</p>
<dl><dt>Object type</dt><dd>{esc(record.get('object_type')) or '—'}</dd><dt>Material / technique</dt><dd>{esc(record.get('object_material_technique')) or '—'}</dd>
<dt>Dimensions</dt><dd>{esc(record.get('object_dimensions')) or '—'}</dd><dt>Event / date / creator / place</dt><dd>{esc('; '.join(event_parts)) or '—'}</dd>
<dt>Related people or organisation</dt><dd>{esc(related_people) or '—'}</dd><dt>Collection</dt><dd>{esc(collections) or '—'}</dd>
<dt>Tags</dt><dd>{esc(tags) or '—'}</dd><dt>Additional description</dt><dd>{esc(record.get('detailed_description')) or '—'}</dd>
<dt>Last updated</dt><dd>{esc(record.get('object_last_updated')) or '—'}</dd><dt>Metadata rights</dt><dd>{esc(licence.get('metadata_rights_status')) or '—'} · {esc(licence.get('metadata_rights_holder'))}</dd></dl></article></div></section>"""


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE objects (
    record_key TEXT PRIMARY KEY,
    md_object_id TEXT NOT NULL,
    inventory TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    object_type TEXT NOT NULL,
    date_text TEXT NOT NULL,
    md_url TEXT NOT NULL,
    md_image_count INTEGER NOT NULL,
    queue_lane TEXT NOT NULL,
    review_note TEXT NOT NULL,
    suggested_master_path TEXT NOT NULL,
    metadata_comparison_state TEXT NOT NULL,
    metadata_reconciliation_state TEXT NOT NULL,
    parent_md_object_id TEXT,
    md_image_id TEXT,
    linked_record_key TEXT NOT NULL DEFAULT '',
    link_relation TEXT NOT NULL DEFAULT '',
    component_label TEXT NOT NULL DEFAULT '',
    UNIQUE(parent_md_object_id, md_image_id)
);
CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY,
    record_key TEXT NOT NULL REFERENCES objects(record_key),
    source_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    width_px INTEGER,
    height_px INTEGER,
    visual_index_state TEXT NOT NULL,
    visual_index_error TEXT NOT NULL,
    candidate_relation TEXT NOT NULL,
    relation_basis TEXT NOT NULL,
    suggested_master INTEGER NOT NULL CHECK(suggested_master IN (0, 1)),
    phash TEXT NOT NULL,
    dhash TEXT NOT NULL,
    ahash TEXT NOT NULL,
    UNIQUE(record_key, source_path)
);
CREATE INDEX candidates_record_key_idx ON candidates(record_key);
CREATE TABLE decisions (
    record_key TEXT PRIMARY KEY REFERENCES objects(record_key),
    decision_state TEXT NOT NULL CHECK(decision_state IN ('unresolved', 'selected_master', 'redundant_derivative')),
    selected_candidate_id TEXT REFERENCES candidates(candidate_id),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    deletion_authorized INTEGER NOT NULL DEFAULT 0 CHECK(deletion_authorized = 0),
    CHECK(
        (decision_state = 'unresolved' AND selected_candidate_id IS NULL)
        OR (decision_state = 'selected_master' AND selected_candidate_id IS NOT NULL)
        OR (decision_state = 'redundant_derivative' AND selected_candidate_id IS NULL)
    )
);
CREATE TABLE candidate_dispositions (
    record_key TEXT NOT NULL REFERENCES objects(record_key),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    disposition TEXT NOT NULL CHECK(disposition IN ('undecided', 'selected_master', 'redundant')),
    decision_basis TEXT NOT NULL DEFAULT '',
    deletion_authorized INTEGER NOT NULL DEFAULT 0 CHECK(deletion_authorized = 0),
    PRIMARY KEY(record_key, candidate_id)
);
CREATE TABLE metadata_editorial_decisions (
    md_object_id TEXT NOT NULL,
    inventory TEXT NOT NULL,
    field_name TEXT NOT NULL,
    original_value TEXT NOT NULL,
    resolved_value TEXT NOT NULL DEFAULT '',
    decision_state TEXT NOT NULL CHECK(decision_state IN (
        'unresolved', 'auto_resolved', 'resolved_keep_current', 'resolved_corrected'
    )),
    basis TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    deletion_authorized INTEGER NOT NULL DEFAULT 0 CHECK(deletion_authorized = 0),
    PRIMARY KEY(md_object_id, field_name)
);
"""


def initialize_database() -> bool:
    source_mount()
    fingerprints = {
        "objects_csv_sha256": sha256_file(OBJECTS_CSV),
        "candidates_csv_sha256": sha256_file(CANDIDATES_CSV),
        "queue_csv_sha256": sha256_file(QUEUE_CSV),
        "metadata_csv_sha256": sha256_file(METADATA_CSV),
    }
    if DATABASE.exists():
        with connect(read_only=True) as connection:
            stored = dict(connection.execute("SELECT key, value FROM meta"))
        for key, value in fingerprints.items():
            if stored.get(key) != value:
                raise WorkbenchError(f"projection fingerprint changed: {key}")
        return False

    objects = read_csv(OBJECTS_CSV)
    candidates = read_csv(CANDIDATES_CSV)
    queue = {row["record_key"]: row for row in read_csv(QUEUE_CSV)}
    metadata = {row["record_key"]: row for row in read_csv(METADATA_CSV)}
    if len(objects) != 178 or len(queue) != 178 or len(metadata) != 178 or len(candidates) != 1068:
        raise WorkbenchError("unexpected projection row counts")

    temporary = DATABASE.with_name(f".{DATABASE.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        meta_rows = {
            "schema_version": "2",
            "tool_version": VERSION,
            "created_at": utc_now(),
            "unit": "wf-alben",
            "metadata_authority": "current museum-digital collection 607 capture",
            "expected_object_count": "178",
            **fingerprints,
        }
        connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta_rows.items())
        object_rows = []
        for obj in objects:
            work = queue[obj["record_key"]]
            md = metadata[obj["record_key"]]
            object_rows.append(
                (
                    obj["record_key"], obj["md_object_id"], obj["inventory"], obj["title"],
                    obj["object_type"], obj["date_text"], obj["md_url"], int(obj["md_image_count"]),
                    work["queue_lane"], work["review_note"], work["suggested_master_path"],
                    md["comparison_state"], md["metadata_reconciliation_state"],
                )
            )
        connection.executemany(
            """INSERT INTO objects(
                   record_key, md_object_id, inventory, title, object_type, date_text,
                   md_url, md_image_count, queue_lane, review_note, suggested_master_path,
                   metadata_comparison_state, metadata_reconciliation_state
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            object_rows,
        )
        candidate_rows = []
        disposition_rows = []
        for row in candidates:
            source_path = Path(row["candidate_source_path"])
            if not inside_source(source_path):
                raise WorkbenchError(f"candidate escapes protected source root: {source_path}")
            stat = source_path.stat()
            if stat.st_size != int(row["size_bytes"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
                raise WorkbenchError(f"candidate changed since projection: {source_path}")
            candidate_rows.append(
                (
                    row["candidate_id"], row["record_key"], row["candidate_source_path"], row["filename"],
                    row["extension"], int(row["size_bytes"]), int(row["mtime_ns"]),
                    int(row["width_px"]) if row["width_px"] else None,
                    int(row["height_px"]) if row["height_px"] else None,
                    row["visual_index_state"], row["visual_index_error"], row["candidate_relation"],
                    row["relation_basis"], int(row["suggested_master"] == "yes"), row["phash"],
                    row["dhash"], row["ahash"],
                )
            )
            disposition_rows.append((row["record_key"], row["candidate_id"], "undecided", "", 0))
        connection.executemany(
            """INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            candidate_rows,
        )
        connection.executemany(
            """INSERT INTO decisions(record_key, decision_state, selected_candidate_id,
               note, decided_by, decided_at, deletion_authorized)
               VALUES (?, 'unresolved', NULL, '', '', '', 0)""",
            [(row["record_key"],) for row in objects],
        )
        connection.executemany(
            "INSERT INTO candidate_dispositions VALUES (?, ?, ?, ?, ?)", disposition_rows
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise WorkbenchError("new workbench database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkbenchError("new workbench database foreign-key check failed")
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temporary, DATABASE)
    export_decisions()
    return True


def decision_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    sql = """
        SELECT o.record_key, o.md_object_id, o.inventory, d.decision_state,
               COALESCE(d.selected_candidate_id, '') AS selected_candidate_id,
               COALESCE(c.source_path, '') AS selected_master_path,
               d.note, d.decided_by, d.decided_at, o.linked_record_key,
               o.link_relation, o.component_label,
               COALESCE(linked_master.source_path, '') AS linked_master_path
          FROM objects o
          JOIN decisions d USING(record_key)
          LEFT JOIN candidates c ON c.candidate_id = d.selected_candidate_id
          LEFT JOIN decisions linked_decision ON linked_decision.record_key=o.linked_record_key
          LEFT JOIN candidates linked_master ON linked_master.candidate_id=linked_decision.selected_candidate_id
         ORDER BY lower(o.inventory), o.md_object_id
    """
    return [
        {
            **dict(row),
            "drain_list_state": "not_eligible_before_accession",
            "deletion_authorized": "false",
        }
        for row in connection.execute(sql)
    ]


def disposition_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    sql = """
        SELECT o.record_key, o.md_object_id, o.inventory, c.candidate_id,
               c.source_path, cd.disposition, cd.decision_basis,
               o.linked_record_key,
               COALESCE(linked_master.source_path, '') AS linked_master_path
          FROM candidate_dispositions cd
          JOIN objects o USING(record_key)
          JOIN candidates c ON c.candidate_id = cd.candidate_id
          LEFT JOIN decisions linked_decision ON linked_decision.record_key=o.linked_record_key
          LEFT JOIN candidates linked_master ON linked_master.candidate_id=linked_decision.selected_candidate_id
         ORDER BY lower(o.inventory), c.source_path
    """
    return [
        {
            **dict(row),
            "drain_list_state": "not_eligible_before_accession",
            "deletion_authorized": "false",
        }
        for row in connection.execute(sql)
    ]


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def metadata_editorial_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(connection, "metadata_editorial_decisions"):
        return []
    return [
        {**dict(row), "deletion_authorized": "false"}
        for row in connection.execute(
            """SELECT md_object_id, inventory, field_name, original_value,
                      resolved_value, decision_state, basis, decided_by, decided_at
                 FROM metadata_editorial_decisions
                ORDER BY CAST(md_object_id AS INTEGER), field_name"""
        )
    ]


def export_decisions() -> None:
    with connect(read_only=True) as connection:
        decisions = decision_rows(connection)
        dispositions = disposition_rows(connection)
        metadata_editorial = metadata_editorial_rows(connection)
    atomic_csv(DECISIONS_CSV, DECISION_FIELDS, decisions)
    atomic_csv(DISPOSITIONS_CSV, DISPOSITION_FIELDS, dispositions)
    if metadata_editorial or METADATA_EDITORIAL_CSV.exists():
        atomic_csv(METADATA_EDITORIAL_CSV, METADATA_EDITORIAL_FIELDS, metadata_editorial)


def check_state() -> dict[str, Any]:
    mount = source_mount()
    if not DATABASE.is_file():
        raise WorkbenchError("workbench database is missing")
    with connect(read_only=True) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        expected_objects = int(meta.get("expected_object_count", "178"))
        expected_candidates = int(meta.get("expected_candidate_count", "1068"))
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            "objects": connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0],
            "candidates": connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "decisions": connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "selected_masters": connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE decision_state='selected_master'"
            ).fetchone()[0],
            "unresolved": connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE decision_state='unresolved'"
            ).fetchone()[0],
            "redundant_derivatives": connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE decision_state='redundant_derivative'"
            ).fetchone()[0],
            "dispositions": connection.execute("SELECT COUNT(*) FROM candidate_dispositions").fetchone()[0],
            "redundant": connection.execute(
                "SELECT COUNT(*) FROM candidate_dispositions WHERE disposition='redundant'"
            ).fetchone()[0],
            "deletion_authorized_true": connection.execute(
                "SELECT (SELECT COUNT(*) FROM decisions WHERE deletion_authorized!=0) + "
                "(SELECT COUNT(*) FROM candidate_dispositions WHERE deletion_authorized!=0) + "
                "CASE WHEN EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_editorial_decisions') "
                "THEN (SELECT COUNT(*) FROM metadata_editorial_decisions WHERE deletion_authorized!=0) ELSE 0 END"
            ).fetchone()[0],
        }
        if table_exists(connection, "metadata_editorial_decisions"):
            counts.update(
                metadata_editorial_decisions=connection.execute(
                    "SELECT COUNT(*) FROM metadata_editorial_decisions"
                ).fetchone()[0],
                metadata_auto_resolved=connection.execute(
                    "SELECT COUNT(*) FROM metadata_editorial_decisions WHERE decision_state='auto_resolved'"
                ).fetchone()[0],
                metadata_ambiguous=connection.execute(
                    "SELECT COUNT(*) FROM metadata_editorial_decisions WHERE decision_state='unresolved'"
                ).fetchone()[0],
                metadata_human_resolved=connection.execute(
                    """SELECT COUNT(*) FROM metadata_editorial_decisions
                       WHERE decision_state IN ('resolved_keep_current','resolved_corrected')"""
                ).fetchone()[0],
            )
        invalid_selected = connection.execute(
            """SELECT COUNT(*) FROM decisions d
               WHERE d.decision_state='selected_master' AND NOT EXISTS (
                   SELECT 1 FROM candidates c
                    WHERE c.candidate_id=d.selected_candidate_id AND c.record_key=d.record_key
               )"""
        ).fetchone()[0]
        invalid_dispositions = connection.execute(
            """SELECT COUNT(*) FROM decisions d
               WHERE (d.decision_state='selected_master' AND (
                       (SELECT COUNT(*) FROM candidate_dispositions cd
                         WHERE cd.record_key=d.record_key AND cd.disposition='selected_master') != 1
                       OR EXISTS (
                           SELECT 1 FROM candidate_dispositions cd
                            WHERE cd.record_key=d.record_key
                              AND cd.candidate_id!=d.selected_candidate_id
                              AND cd.disposition!='redundant'
                       )
                   ))
                  OR (d.decision_state='unresolved' AND EXISTS (
                       SELECT 1 FROM candidate_dispositions cd
                        WHERE cd.record_key=d.record_key AND cd.disposition!='undecided'
                   ))
                  OR (d.decision_state='redundant_derivative' AND (
                       d.selected_candidate_id IS NOT NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM objects o
                           JOIN decisions parent_d ON parent_d.record_key=o.linked_record_key
                           WHERE o.record_key=d.record_key
                             AND o.link_relation='derived_from'
                             AND parent_d.decision_state='selected_master'
                       )
                       OR EXISTS (
                           SELECT 1 FROM candidate_dispositions cd
                           WHERE cd.record_key=d.record_key AND cd.disposition!='redundant'
                       )
                   ))"""
        ).fetchone()[0]
        db_decisions = decision_rows(connection)
        db_dispositions = disposition_rows(connection)
        db_metadata_editorial = metadata_editorial_rows(connection)
    csv_decisions = read_csv(DECISIONS_CSV)
    csv_dispositions = read_csv(DISPOSITIONS_CSV)
    if db_decisions != csv_decisions or db_dispositions != csv_dispositions:
        raise WorkbenchError("SQLite decisions and human-readable CSV mirrors differ")
    if db_metadata_editorial:
        if db_metadata_editorial != read_csv(METADATA_EDITORIAL_CSV):
            raise WorkbenchError("SQLite metadata decisions and human-readable CSV mirror differ")
        expected_metadata = int(meta.get("expected_metadata_editorial_decisions", "0"))
        if counts.get("metadata_editorial_decisions") != expected_metadata:
            raise WorkbenchError("metadata editorial decision count changed")
    if quick != "ok" or foreign_keys or counts["objects"] != expected_objects or counts["candidates"] != expected_candidates:
        raise WorkbenchError("workbench structural validation failed")
    if invalid_selected or invalid_dispositions or counts["deletion_authorized_true"]:
        raise WorkbenchError("workbench decision invariant failed")
    return {
        "checked_at": utc_now(),
        "tool_version": VERSION,
        "database": str(DATABASE),
        "quick_check": quick,
        "foreign_key_violations": len(foreign_keys),
        "invalid_selected_master_bindings": invalid_selected,
        "invalid_candidate_dispositions": invalid_dispositions,
        "mount": mount,
        **counts,
    }


def write_preflight(actor: str, host: str, port: int, created: bool) -> dict[str, Any]:
    status = check_state()
    payload = {
        "format": "iss-wf-alben-workbench-preflight-v1",
        "created_at": utc_now(),
        "actor": actor,
        "url": f"http://{host}:{port}/",
        "database_created": created,
        "decision_model": "one selected Master per resolved object; all other candidates redundant",
        "metadata_authority": "current museum-digital collection 607 capture",
        "source_mutation": False,
        "accession_created": False,
        "drain_entries_created": False,
        "deletion_authorized": False,
        "status": status,
    }
    atomic_json(PREFLIGHT, payload)
    return payload


def bulk_select_unique_tiffs(actor: str) -> dict[str, Any]:
    """Apply the user's bounded approval to sole readable exact-name TIFFs."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before bulk selection")
    if BULK_UNIQUE_TIFF_RECEIPT.exists():
        raise WorkbenchError(f"bulk selection receipt already exists: {BULK_UNIQUE_TIFF_RECEIPT}")

    query = """
        SELECT o.record_key, o.md_object_id, o.inventory,
               c.candidate_id, c.source_path, c.size_bytes, c.mtime_ns
          FROM objects o
          JOIN decisions d USING(record_key)
          JOIN candidates c USING(record_key)
         WHERE o.queue_lane='suggest_unique_tiff'
           AND d.decision_state='unresolved'
           AND c.suggested_master=1
           AND lower(c.extension) IN ('tif','tiff')
           AND c.visual_index_state='indexed'
           AND c.width_px IS NOT NULL AND c.height_px IS NOT NULL
           AND c.candidate_relation='exact_inventory_filename'
         ORDER BY lower(o.inventory), o.md_object_id
    """
    with connect(read_only=True) as connection:
        targets = [dict(row) for row in connection.execute(query)]
        target_objects = connection.execute(
            """SELECT COUNT(*) FROM objects o JOIN decisions d USING(record_key)
               WHERE o.queue_lane='suggest_unique_tiff' AND d.decision_state='unresolved'"""
        ).fetchone()[0]
        bad_suggestion_counts = connection.execute(
            """SELECT COUNT(*) FROM (
                   SELECT o.record_key, SUM(CASE WHEN c.suggested_master=1 THEN 1 ELSE 0 END) AS n
                     FROM objects o JOIN decisions d USING(record_key) JOIN candidates c USING(record_key)
                    WHERE o.queue_lane='suggest_unique_tiff' AND d.decision_state='unresolved'
                    GROUP BY o.record_key HAVING n != 1
               )"""
        ).fetchone()[0]
        existing_selected = connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decision_state='selected_master'"
        ).fetchone()[0]
    if len(targets) != 126 or target_objects != 126 or bad_suggestion_counts or existing_selected:
        raise WorkbenchError(
            "bulk scope changed; expected 126 unresolved objects, 126 sole TIFF suggestions, and no prior selections"
        )
    for target in targets:
        path = Path(target["source_path"])
        if not inside_source(path):
            raise WorkbenchError(f"target escapes protected source: {path}")
        stat = path.stat()
        if stat.st_size != target["size_bytes"] or stat.st_mtime_ns != target["mtime_ns"]:
            raise WorkbenchError(f"target changed since projection: {path}")

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-bulk-unique-tiff-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-bulk-unique-tiff-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-bulk-unique-tiff-{token}.csv"
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup)):
        raise WorkbenchError("pre-change backup target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    decided_at = utc_now()
    note = "User-approved bulk selection: sole readable TIFF with exact current-inventory filename match"
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for target in targets:
            connection.execute(
                """UPDATE decisions SET decision_state='selected_master', selected_candidate_id=?,
                   note=?, decided_by=?, decided_at=?
                   WHERE record_key=? AND decision_state='unresolved'""",
                (target["candidate_id"], note, actor, decided_at, target["record_key"]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkbenchError(f"decision scope changed during update: {target['record_key']}")
            connection.execute(
                """UPDATE candidate_dispositions
                      SET disposition=CASE WHEN candidate_id=? THEN 'selected_master' ELSE 'redundant' END,
                          decision_basis='explicit user bulk confirmation of sole readable exact-name TIFF'
                    WHERE record_key=?""",
                (target["candidate_id"], target["record_key"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    export_decisions()
    status = check_state()
    if status["selected_masters"] != 126 or status["unresolved"] != 52:
        raise WorkbenchError(f"unexpected post-selection state: {status}")
    with connect(read_only=True) as connection:
        remaining_lanes = dict(
            connection.execute(
                """SELECT o.queue_lane, COUNT(*) FROM objects o JOIN decisions d USING(record_key)
                   WHERE d.decision_state='unresolved' GROUP BY o.queue_lane ORDER BY o.queue_lane"""
            )
        )
    receipt = {
        "format": "iss-wf-alben-bulk-unique-tiff-selection-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "explicit user instruction to select all matching TIFFs and leave ambiguous cases",
        "selection_rule": "unresolved suggest_unique_tiff object with exactly one readable exact-inventory TIFF suggestion",
        "selected_objects": len(targets),
        "selected_candidates": [
            {
                "record_key": target["record_key"],
                "md_object_id": target["md_object_id"],
                "inventory": target["inventory"],
                "candidate_id": target["candidate_id"],
                "selected_master_path": target["source_path"],
            }
            for target in targets
        ],
        "remaining_unresolved": status["unresolved"],
        "remaining_queue_lanes": remaining_lanes,
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "source_mount": mount,
        "source_files_modified": 0,
        "accession_created": False,
        "drain_entries_created": 0,
        "deletion_authorized": False,
        "validation": status,
    }
    atomic_json(BULK_UNIQUE_TIFF_RECEIPT, receipt)
    return receipt


def link_65080_components(actor: str) -> dict[str, Any]:
    """Replace compound md:65080 with two linked one-Master objects."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before linked-object migration")
    if LINKED_65080_RECEIPT.exists() or LINKED_RELATIONSHIPS_CSV.exists():
        raise WorkbenchError("linked-object 65080 migration has already been recorded")

    with connect(read_only=True) as source:
        meta = dict(source.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != "1":
            raise WorkbenchError(f"expected schema version 1, found {meta.get('schema_version')}")
        objects = [dict(row) for row in source.execute("SELECT * FROM objects ORDER BY record_key")]
        candidates = [dict(row) for row in source.execute("SELECT * FROM candidates ORDER BY candidate_id")]
        decisions = [dict(row) for row in source.execute("SELECT * FROM decisions ORDER BY record_key")]
        dispositions = [
            dict(row) for row in source.execute(
                "SELECT * FROM candidate_dispositions ORDER BY record_key, candidate_id"
            )
        ]
    parent = next((row for row in objects if row["record_key"] == "md:65080"), None)
    parent_decision = next((row for row in decisions if row["record_key"] == "md:65080"), None)
    parent_candidates = [row for row in candidates if row["record_key"] == "md:65080"]
    if parent is None or parent_decision is None or parent_decision["decision_state"] != "unresolved":
        raise WorkbenchError("md:65080 is missing or no longer unresolved")
    if int(parent["md_image_count"]) != 2 or len(parent_candidates) != 13:
        raise WorkbenchError("md:65080 compound scope changed")

    def is_extension(row: dict[str, Any]) -> bool:
        return Path(row["filename"]).stem.casefold().replace("_", "-") == "alb-001-50v"

    primary_candidates = [row for row in parent_candidates if not is_extension(row)]
    extension_candidates = [row for row in parent_candidates if is_extension(row)]
    primary_tiffs = [
        row for row in primary_candidates
        if row["filename"].casefold() == "alb-001-50.tif"
        and row["visual_index_state"] == "indexed" and row["width_px"] and row["height_px"]
    ]
    extension_tiffs = [
        row for row in extension_candidates
        if row["filename"].casefold() == "alb-001-50v.tif"
        and row["visual_index_state"] == "indexed" and row["width_px"] and row["height_px"]
    ]
    if len(primary_candidates) != 8 or len(extension_candidates) != 5:
        raise WorkbenchError("unexpected primary/extension candidate partition for md:65080")
    if len(primary_tiffs) != 1 or len(extension_tiffs) != 1:
        raise WorkbenchError("expected one readable TIFF for each md:65080 component")
    for target in (primary_tiffs[0], extension_tiffs[0]):
        path = Path(target["source_path"])
        if not inside_source(path):
            raise WorkbenchError(f"component Master escapes protected source: {path}")
        stat = path.stat()
        if stat.st_size != target["size_bytes"] or stat.st_mtime_ns != target["mtime_ns"]:
            raise WorkbenchError(f"component Master changed since projection: {path}")

    md_record = load_md_records().get("65080")
    images = {str(row.get("quell_id")): row for row in (md_record or {}).get("object_images", [])}
    if set(images) != {"47045", "51266"}:
        raise WorkbenchError("captured md:65080 image membership changed")
    primary_key = "md:65080:image:47045"
    extension_key = "md:65080:image:51266"
    primary_image = images["47045"]
    extension_image = images["51266"]
    decided_at = utc_now()
    decision_note = "User-confirmed linked compound: one photo extends the other; sole readable component TIFF selected"

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-linked-65080-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-linked-65080-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-linked-65080-{token}.csv"
    temporary = DATABASE.with_name(f".{DATABASE.name}.linked-65080-{os.getpid()}.tmp")
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup, temporary)):
        raise WorkbenchError("linked-object backup or temporary target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    object_fields = (
        "record_key", "md_object_id", "inventory", "title", "object_type", "date_text",
        "md_url", "md_image_count", "queue_lane", "review_note", "suggested_master_path",
        "metadata_comparison_state", "metadata_reconciliation_state", "parent_md_object_id",
        "md_image_id", "linked_record_key", "link_relation", "component_label",
    )
    candidate_fields = (
        "candidate_id", "record_key", "source_path", "filename", "extension", "size_bytes",
        "mtime_ns", "width_px", "height_px", "visual_index_state", "visual_index_error",
        "candidate_relation", "relation_basis", "suggested_master", "phash", "dhash", "ahash",
    )
    decision_fields = (
        "record_key", "decision_state", "selected_candidate_id", "note", "decided_by",
        "decided_at", "deletion_authorized",
    )
    disposition_fields = (
        "record_key", "candidate_id", "disposition", "decision_basis", "deletion_authorized",
    )
    ordinary_objects = [row for row in objects if row["record_key"] != "md:65080"]
    new_objects = []
    for row in ordinary_objects:
        new_objects.append({**row, "parent_md_object_id": None, "md_image_id": None,
                            "linked_record_key": "", "link_relation": "", "component_label": ""})
    common = {
        "md_object_id": "65080",
        "object_type": parent["object_type"],
        "date_text": parent["date_text"],
        "md_url": parent["md_url"],
        "md_image_count": 1,
        "queue_lane": "linked_component",
        "metadata_comparison_state": parent["metadata_comparison_state"],
        "metadata_reconciliation_state": parent["metadata_reconciliation_state"],
        "parent_md_object_id": "65080",
    }
    new_objects.extend(
        (
            {
                **common, "record_key": primary_key, "inventory": "ALB-001-50",
                "title": primary_image.get("name") or parent["title"],
                "review_note": "Primary photograph linked to extending legend ALB-001-50v",
                "suggested_master_path": primary_tiffs[0]["source_path"],
                "md_image_id": "47045", "linked_record_key": extension_key,
                "link_relation": "extended_by", "component_label": "primary photograph",
            },
            {
                **common, "record_key": extension_key, "inventory": "ALB-001-50v",
                "title": extension_image.get("name") or "Später hinzugefügte Legende",
                "review_note": "Extending legend for primary photograph ALB-001-50",
                "suggested_master_path": extension_tiffs[0]["source_path"],
                "md_image_id": "51266", "linked_record_key": primary_key,
                "link_relation": "extends", "component_label": "extending legend",
            },
        )
    )

    candidate_target: dict[str, str] = {}
    new_candidates = []
    for row in candidates:
        item = dict(row)
        if row["record_key"] == "md:65080":
            item["record_key"] = extension_key if is_extension(row) else primary_key
            candidate_target[row["candidate_id"]] = item["record_key"]
        new_candidates.append(item)
    new_decisions = [row for row in decisions if row["record_key"] != "md:65080"]
    new_decisions.extend(
        (
            {
                "record_key": primary_key, "decision_state": "selected_master",
                "selected_candidate_id": primary_tiffs[0]["candidate_id"], "note": decision_note,
                "decided_by": actor, "decided_at": decided_at, "deletion_authorized": 0,
            },
            {
                "record_key": extension_key, "decision_state": "selected_master",
                "selected_candidate_id": extension_tiffs[0]["candidate_id"], "note": decision_note,
                "decided_by": actor, "decided_at": decided_at, "deletion_authorized": 0,
            },
        )
    )
    new_dispositions = [row for row in dispositions if row["record_key"] != "md:65080"]
    for row in parent_candidates:
        record_key = candidate_target[row["candidate_id"]]
        selected_id = (
            extension_tiffs[0]["candidate_id"] if record_key == extension_key
            else primary_tiffs[0]["candidate_id"]
        )
        new_dispositions.append(
            {
                "record_key": record_key,
                "candidate_id": row["candidate_id"],
                "disposition": "selected_master" if row["candidate_id"] == selected_id else "redundant",
                "decision_basis": "explicit user confirmation of linked primary and extending photo components",
                "deletion_authorized": 0,
            }
        )

    rebuilt = sqlite3.connect(temporary)
    try:
        rebuilt.execute("PRAGMA foreign_keys=ON")
        rebuilt.executescript(SCHEMA)
        new_meta = {
            **meta,
            "schema_version": "2",
            "tool_version": VERSION,
            "expected_object_count": "179",
            "linked_object_parent_65080": f"{primary_key};{extension_key}",
        }
        rebuilt.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", new_meta.items())
        rebuilt.executemany(
            f"INSERT INTO objects({','.join(object_fields)}) VALUES ({','.join('?' for _ in object_fields)})",
            [tuple(row.get(field) for field in object_fields) for row in new_objects],
        )
        rebuilt.executemany(
            f"INSERT INTO candidates({','.join(candidate_fields)}) VALUES ({','.join('?' for _ in candidate_fields)})",
            [tuple(row.get(field) for field in candidate_fields) for row in new_candidates],
        )
        rebuilt.executemany(
            f"INSERT INTO decisions({','.join(decision_fields)}) VALUES ({','.join('?' for _ in decision_fields)})",
            [tuple(row.get(field) for field in decision_fields) for row in new_decisions],
        )
        rebuilt.executemany(
            f"INSERT INTO candidate_dispositions({','.join(disposition_fields)}) VALUES ({','.join('?' for _ in disposition_fields)})",
            [tuple(row.get(field) for field in disposition_fields) for row in new_dispositions],
        )
        rebuilt.commit()
        if rebuilt.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise WorkbenchError("rebuilt linked-object database integrity check failed")
        if rebuilt.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkbenchError("rebuilt linked-object database foreign-key check failed")
    except Exception:
        rebuilt.close()
        temporary.unlink(missing_ok=True)
        raise
    rebuilt.close()
    os.replace(temporary, DATABASE)
    export_decisions()
    status = check_state()
    if status["objects"] != 179 or status["selected_masters"] != 128 or status["unresolved"] != 51:
        raise WorkbenchError(f"unexpected linked-object state: {status}")

    relationship = {
        "parent_md_object_id": "65080",
        "parent_inventory": "ALB-001-50",
        "primary_record_key": primary_key,
        "primary_inventory": "ALB-001-50",
        "primary_md_image_id": "47045",
        "primary_selected_master_path": primary_tiffs[0]["source_path"],
        "extension_record_key": extension_key,
        "extension_inventory": "ALB-001-50v",
        "extension_md_image_id": "51266",
        "extension_selected_master_path": extension_tiffs[0]["source_path"],
        "relationship": "extension extends primary photograph; both remain linked to one museum-digital record",
        "deletion_authorized": "false",
    }
    atomic_csv(LINKED_RELATIONSHIPS_CSV, relationship.keys(), [relationship])
    receipt = {
        "format": "iss-wf-alben-linked-object-65080-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "user confirmed the two photos belong together and one extends the other",
        "parent_md_object_id": "65080",
        "relationship": relationship,
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "validation": status,
        "source_mount": mount,
        "source_files_modified": 0,
        "accession_created": False,
        "drain_entries_created": 0,
        "deletion_authorized": False,
    }
    atomic_json(LINKED_65080_RECEIPT, receipt)
    return receipt


def link_60243_components(actor: str) -> dict[str, Any]:
    """Replace unresolved md:60243 with two linked, unresolved image objects."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before linked-object migration")
    if LINKED_60243_RECEIPT.exists():
        raise WorkbenchError("linked-object 60243 migration has already been recorded")

    with connect(read_only=True) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        parent = connection.execute(
            "SELECT * FROM objects WHERE record_key='md:60243'"
        ).fetchone()
        parent_decision = connection.execute(
            "SELECT * FROM decisions WHERE record_key='md:60243'"
        ).fetchone()
        parent_candidates = connection.execute(
            "SELECT * FROM candidates WHERE record_key='md:60243' ORDER BY size_bytes DESC"
        ).fetchall()
        before_selected = connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decision_state='selected_master'"
        ).fetchone()[0]
        before_unresolved = connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decision_state='unresolved'"
        ).fetchone()[0]
    if meta.get("schema_version") != "2":
        raise WorkbenchError(f"expected schema version 2, found {meta.get('schema_version')}")
    if parent is None or parent_decision is None or parent_decision["decision_state"] != "unresolved":
        raise WorkbenchError("md:60243 is missing or no longer unresolved")
    if int(parent["md_image_count"]) != 2 or len(parent_candidates) != 2:
        raise WorkbenchError("md:60243 compound scope changed")
    if any(row["filename"].casefold() != "alb_001.jpg" for row in parent_candidates):
        raise WorkbenchError("unexpected md:60243 candidate filenames")
    full_candidates = [
        row for row in parent_candidates
        if row["width_px"] == 4128 and row["height_px"] == 2322 and row["size_bytes"] == 1224038
    ]
    thumbnail_candidates = [
        row for row in parent_candidates
        if row["width_px"] == 300 and row["height_px"] == 169
    ]
    if len(full_candidates) != 1 or len(thumbnail_candidates) != 1:
        raise WorkbenchError("expected one full-size cover image and one 300px derivative for md:60243")
    for candidate in parent_candidates:
        path = Path(candidate["source_path"])
        if not inside_source(path):
            raise WorkbenchError(f"component candidate escapes protected source: {path}")
        stat = path.stat()
        if stat.st_size != candidate["size_bytes"] or stat.st_mtime_ns != candidate["mtime_ns"]:
            raise WorkbenchError(f"component candidate changed since projection: {path}")
    full_digest = sha256_file(Path(full_candidates[0]["source_path"]))
    expected_digest = "sha256:d0801f295431aaeae9e0ee47e3946cdaba84218f476a2ec794c67d99369e55ec"
    if full_digest != expected_digest:
        raise WorkbenchError("full-size local cover no longer matches MD image 43747")

    md_record = load_md_records().get("60243")
    images = {str(row.get("quell_id")): row for row in (md_record or {}).get("object_images", [])}
    if set(images) != {"43747", "43750"}:
        raise WorkbenchError("captured md:60243 image membership changed")

    primary_key = "md:60243:image:43747"
    second_key = "md:60243:image:43750"
    primary_inventory = "ALB_001"
    second_inventory = "ALB_001 [MD image 43750]"
    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-linked-60243-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-linked-60243-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-linked-60243-{token}.csv"
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup)):
        raise WorkbenchError("linked-object backup target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            common = {
                "md_object_id": parent["md_object_id"],
                "object_type": parent["object_type"],
                "date_text": parent["date_text"],
                "md_url": parent["md_url"],
                "md_image_count": 1,
                "queue_lane": "linked_component",
                "metadata_comparison_state": parent["metadata_comparison_state"],
                "metadata_reconciliation_state": parent["metadata_reconciliation_state"],
                "parent_md_object_id": "60243",
            }
            object_fields = (
                "record_key", "md_object_id", "inventory", "title", "object_type", "date_text",
                "md_url", "md_image_count", "queue_lane", "review_note", "suggested_master_path",
                "metadata_comparison_state", "metadata_reconciliation_state", "parent_md_object_id",
                "md_image_id", "linked_record_key", "link_relation", "component_label",
            )
            new_objects = (
                {
                    **common,
                    "record_key": primary_key,
                    "inventory": "__temporary_md_60243_primary__",
                    "title": f"{parent['title']} - geschlossener Umschlag",
                    "review_note": "Closed album cover; exact local full-size match plus a 300px derivative.",
                    "suggested_master_path": "",
                    "md_image_id": "43747",
                    "linked_record_key": second_key,
                    "link_relation": "paired_with",
                    "component_label": "closed album cover",
                },
                {
                    **common,
                    "record_key": second_key,
                    "inventory": second_inventory,
                    "title": f"{parent['title']} - geoeffnetes Album mit Titelblatt",
                    "review_note": "Opened album title page; no local candidate identified in the current archive index.",
                    "suggested_master_path": "",
                    "md_image_id": "43750",
                    "linked_record_key": primary_key,
                    "link_relation": "paired_with",
                    "component_label": "opened album title page",
                },
            )
            connection.executemany(
                f"INSERT INTO objects({','.join(object_fields)}) VALUES ({','.join('?' for _ in object_fields)})",
                [tuple(row[field] for field in object_fields) for row in new_objects],
            )
            connection.executemany(
                "INSERT INTO decisions(record_key, decision_state) VALUES (?, 'unresolved')",
                ((primary_key,), (second_key,)),
            )
            connection.execute(
                "UPDATE candidates SET record_key=? WHERE record_key='md:60243'", (primary_key,)
            )
            connection.execute(
                "UPDATE candidate_dispositions SET record_key=? WHERE record_key='md:60243'",
                (primary_key,),
            )
            connection.execute("DELETE FROM decisions WHERE record_key='md:60243'")
            connection.execute("DELETE FROM objects WHERE record_key='md:60243'")
            connection.execute(
                "UPDATE objects SET inventory=? WHERE record_key=?", (primary_inventory, primary_key)
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('expected_object_count', '180')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('tool_version', ?)", (VERSION,)
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('linked_object_parent_60243', ?)",
                (f"{primary_key};{second_key}",),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    export_decisions()
    status = check_state()
    if (
        status["objects"] != 180
        or status["selected_masters"] != before_selected
        or status["unresolved"] != before_unresolved + 1
    ):
        raise WorkbenchError(f"unexpected linked-object state: {status}")

    relationship_fields = (
        "parent_md_object_id", "parent_inventory", "primary_record_key",
        "primary_inventory", "primary_md_image_id", "primary_selected_master_path",
        "extension_record_key", "extension_inventory", "extension_md_image_id",
        "extension_selected_master_path", "relationship", "deletion_authorized",
    )
    relationship_rows = read_csv(LINKED_RELATIONSHIPS_CSV)
    if any(row["parent_md_object_id"] == "60243" for row in relationship_rows):
        raise WorkbenchError("linked-object relationship for 60243 already exists")
    relationship = {
        "parent_md_object_id": "60243",
        "parent_inventory": primary_inventory,
        "primary_record_key": primary_key,
        "primary_inventory": primary_inventory,
        "primary_md_image_id": "43747",
        "primary_selected_master_path": "",
        "extension_record_key": second_key,
        "extension_inventory": second_inventory,
        "extension_md_image_id": "43750",
        "extension_selected_master_path": "",
        "relationship": "paired views of the same album under one museum-digital record; each archival image object requires its own Master",
        "deletion_authorized": "false",
    }
    atomic_csv(LINKED_RELATIONSHIPS_CSV, relationship_fields, [*relationship_rows, relationship])
    receipt = {
        "format": "iss-wf-alben-linked-object-60243-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "user confirmed museum-digital 60243 contains two linked archival objects",
        "parent_md_object_id": "60243",
        "relationship": relationship,
        "identification_evidence": {
            "md_image_43747_local_exact_sha256": full_digest,
            "md_image_43747_local_full_path": full_candidates[0]["source_path"],
            "md_image_43747_local_derivative_path": thumbnail_candidates[0]["source_path"],
            "md_image_43750_local_candidate": "not identified in current whole-USB visual index",
        },
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "validation": status,
        "source_mount": mount,
        "source_files_modified": 0,
        "accession_created": False,
        "drain_entries_created": 0,
        "deletion_authorized": False,
    }
    atomic_json(LINKED_60243_RECEIPT, receipt)
    return receipt


def split_component_for_candidate(parent_id: str, filename: str) -> str:
    """Return the captured MD image id for the two approved compound records."""
    lowered = filename.casefold()
    if parent_id == "60244":
        return "43752" if "cover" in lowered else "43753"
    if parent_id == "60245":
        return "43748" if "cover" in lowered else "43749"
    raise WorkbenchError(f"unsupported compound parent: {parent_id}")


def nef_parent_inventory(inventory: str) -> str:
    match = re.fullmatch(r"(Album_NEF_Seite_\d+)-[1-3]", inventory)
    return match.group(1) if match else ""


def repair_linked_derivatives(actor: str) -> dict[str, Any]:
    """Split two compound MD records and close confirmed NEF derivatives."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before correction")
    if DERIVATIVE_REPAIR_RECEIPT.exists() or DERIVATIVE_RELATIONSHIPS_CSV.exists():
        raise WorkbenchError("linked derivative correction has already been recorded")

    with connect(read_only=True) as source:
        meta = dict(source.execute("SELECT key, value FROM meta"))
        objects = [dict(row) for row in source.execute("SELECT * FROM objects ORDER BY record_key")]
        candidates = [dict(row) for row in source.execute("SELECT * FROM candidates ORDER BY candidate_id")]
        decisions = [dict(row) for row in source.execute("SELECT * FROM decisions ORDER BY record_key")]
        dispositions = [
            dict(row) for row in source.execute(
                "SELECT * FROM candidate_dispositions ORDER BY record_key, candidate_id"
            )
        ]
    if meta.get("schema_version") != "2":
        raise WorkbenchError(f"expected schema version 2, found {meta.get('schema_version')}")

    object_by_key = {row["record_key"]: row for row in objects}
    decision_by_key = {row["record_key"]: row for row in decisions}
    candidates_by_key: dict[str, list[dict[str, Any]]] = {}
    dispositions_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        candidates_by_key.setdefault(row["record_key"], []).append(row)
    for row in dispositions:
        dispositions_by_key.setdefault(row["record_key"], []).append(row)

    split_specs = {
        "60244": (
            ("43752", "ALB_002", "closed album cover"),
            ("43753", "ALB_002 [MD image 43753]", "album title page"),
        ),
        "60245": (
            ("43748", "ALB_003", "closed album cover"),
            ("43749", "ALB_003 [MD image 43749]", "first double page with title"),
        ),
    }
    md_records = load_md_records()
    for parent_id, components in split_specs.items():
        parent_key = f"md:{parent_id}"
        parent = object_by_key.get(parent_key)
        decision = decision_by_key.get(parent_key)
        image_ids = {str(row.get("quell_id")) for row in md_records[parent_id].get("object_images", [])}
        expected_images = {item[0] for item in components}
        if parent is None or decision is None or decision["decision_state"] != "unresolved":
            raise WorkbenchError(f"{parent_key} is missing or no longer unresolved")
        if int(parent["md_image_count"]) != 2 or image_ids != expected_images:
            raise WorkbenchError(f"captured image membership changed for {parent_key}")
        partition = {
            image_id: [
                row for row in candidates_by_key.get(parent_key, [])
                if split_component_for_candidate(parent_id, row["filename"]) == image_id
            ]
            for image_id in expected_images
        }
        expected_counts = {"60244": {"43752": 2, "43753": 4}, "60245": {"43748": 1, "43749": 2}}
        if {key: len(value) for key, value in partition.items()} != expected_counts[parent_id]:
            raise WorkbenchError(f"candidate partition changed for {parent_key}")
        if any(row["disposition"] != "undecided" for row in dispositions_by_key[parent_key]):
            raise WorkbenchError(f"candidate dispositions changed for {parent_key}")

    inventory_to_key = {row["inventory"]: row["record_key"] for row in objects}
    derivative_targets: list[dict[str, Any]] = []
    for obj in objects:
        parent_inventory = nef_parent_inventory(obj["inventory"])
        if not parent_inventory or decision_by_key[obj["record_key"]]["decision_state"] != "unresolved":
            continue
        parent_key = inventory_to_key.get(parent_inventory, "")
        parent_decision = decision_by_key.get(parent_key)
        child_candidates = candidates_by_key.get(obj["record_key"], [])
        child_dispositions = dispositions_by_key.get(obj["record_key"], [])
        if parent_decision is None or parent_decision["decision_state"] != "selected_master":
            continue
        parent_candidate = next(
            (row for row in candidates_by_key[parent_key]
             if row["candidate_id"] == parent_decision["selected_candidate_id"]),
            None,
        )
        if (
            parent_candidate is None
            or parent_candidate["extension"].casefold() not in {"tif", "tiff"}
            or not child_candidates
            or any(row["extension"].casefold() not in {"jpg", "jpeg"} for row in child_candidates)
            or any(row["disposition"] != "undecided" for row in child_dispositions)
        ):
            raise WorkbenchError(f"derivative evidence changed for {obj['record_key']}")
        derivative_targets.append(
            {
                "record_key": obj["record_key"],
                "md_object_id": obj["md_object_id"],
                "inventory": obj["inventory"],
                "parent_record_key": parent_key,
                "parent_md_object_id": object_by_key[parent_key]["md_object_id"],
                "parent_inventory": parent_inventory,
                "parent_selected_master_path": parent_candidate["source_path"],
                "candidate_count": len(child_candidates),
            }
        )
    if (
        len(derivative_targets) != 39
        or sum(row["candidate_count"] for row in derivative_targets) != 158
        or len({row["parent_record_key"] for row in derivative_targets}) != 21
    ):
        raise WorkbenchError("confirmed NEF derivative scope changed; expected 39 records, 158 files, 21 Masters")

    before_selected = sum(row["decision_state"] == "selected_master" for row in decisions)
    before_unresolved = sum(row["decision_state"] == "unresolved" for row in decisions)
    before_redundant = sum(row["disposition"] == "redundant" for row in dispositions)
    decided_at = utc_now()
    derivative_keys = {row["record_key"] for row in derivative_targets}
    derivative_by_key = {row["record_key"]: row for row in derivative_targets}
    split_parent_keys = {f"md:{parent_id}" for parent_id in split_specs}

    new_objects: list[dict[str, Any]] = []
    for row in objects:
        if row["record_key"] in split_parent_keys:
            continue
        item = dict(row)
        if row["record_key"] in derivative_keys:
            target = derivative_by_key[row["record_key"]]
            item.update(
                linked_record_key=target["parent_record_key"],
                link_relation="derived_from",
                component_label="museum-digital derivative image",
                queue_lane="redundant_derivative",
                review_note="Confirmed derivative of the selected album-page TIFF Master; source retained.",
            )
        new_objects.append(item)
    relationship_rows: list[dict[str, Any]] = []
    new_split_keys: dict[tuple[str, str], str] = {}
    for parent_id, components in split_specs.items():
        parent = object_by_key[f"md:{parent_id}"]
        images = {str(row.get("quell_id")): row for row in md_records[parent_id]["object_images"]}
        component_keys = [f"md:{parent_id}:image:{image_id}" for image_id, _, _ in components]
        for index, (image_id, inventory, label) in enumerate(components):
            linked_key = component_keys[1 - index]
            image = images[image_id]
            new_key = component_keys[index]
            new_split_keys[(parent_id, image_id)] = new_key
            new_objects.append(
                {
                    **parent,
                    "record_key": new_key,
                    "inventory": inventory,
                    "title": image.get("name") or parent["title"],
                    "md_image_count": 1,
                    "queue_lane": "linked_component",
                    "review_note": f"{image.get('beschreibung') or label}; separate archival image object under MD {parent_id}.",
                    "suggested_master_path": "",
                    "parent_md_object_id": parent_id,
                    "md_image_id": image_id,
                    "linked_record_key": linked_key,
                    "link_relation": "paired_with",
                    "component_label": label,
                }
            )
        relationship_rows.append(
            {
                "parent_md_object_id": parent_id,
                "parent_inventory": components[0][1],
                "primary_record_key": component_keys[0],
                "primary_inventory": components[0][1],
                "primary_md_image_id": components[0][0],
                "primary_selected_master_path": "",
                "extension_record_key": component_keys[1],
                "extension_inventory": components[1][1],
                "extension_md_image_id": components[1][0],
                "extension_selected_master_path": "",
                "relationship": "paired views under one museum-digital record; each archival image object requires its own Master",
                "deletion_authorized": "false",
            }
        )

    new_candidates: list[dict[str, Any]] = []
    candidate_new_key: dict[str, str] = {}
    for row in candidates:
        item = dict(row)
        if row["record_key"] in split_parent_keys:
            parent_id = row["record_key"].removeprefix("md:")
            image_id = split_component_for_candidate(parent_id, row["filename"])
            item["record_key"] = new_split_keys[(parent_id, image_id)]
            candidate_new_key[row["candidate_id"]] = item["record_key"]
        new_candidates.append(item)

    new_decisions = [row for row in decisions if row["record_key"] not in split_parent_keys]
    for row in new_decisions:
        if row["record_key"] in derivative_keys:
            target = derivative_by_key[row["record_key"]]
            row.update(
                decision_state="redundant_derivative",
                selected_candidate_id=None,
                note=f"User-confirmed derivative of selected album-page Master {target['parent_record_key']}; source retained.",
                decided_by=actor,
                decided_at=decided_at,
            )
    for parent_id, components in split_specs.items():
        for image_id, _, _ in components:
            new_decisions.append(
                {
                    "record_key": new_split_keys[(parent_id, image_id)],
                    "decision_state": "unresolved",
                    "selected_candidate_id": None,
                    "note": "",
                    "decided_by": "",
                    "decided_at": "",
                    "deletion_authorized": 0,
                }
            )

    new_dispositions: list[dict[str, Any]] = []
    for row in dispositions:
        item = dict(row)
        if row["record_key"] in split_parent_keys:
            item["record_key"] = candidate_new_key[row["candidate_id"]]
        if row["record_key"] in derivative_keys:
            target = derivative_by_key[row["record_key"]]
            item.update(
                disposition="redundant",
                decision_basis=f"user-confirmed derivative of selected album-page Master {target['parent_record_key']}; source retained",
            )
        new_dispositions.append(item)

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-linked-derivative-correction-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-linked-derivative-correction-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-linked-derivative-correction-{token}.csv"
    temporary = DATABASE.with_name(f".{DATABASE.name}.linked-derivative-{os.getpid()}.tmp")
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup, temporary)):
        raise WorkbenchError("correction backup or temporary target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    with connect(read_only=True) as structure:
        object_fields = tuple(row["name"] for row in structure.execute("PRAGMA table_info(objects)"))
        candidate_fields = tuple(row["name"] for row in structure.execute("PRAGMA table_info(candidates)"))
        decision_fields = tuple(row["name"] for row in structure.execute("PRAGMA table_info(decisions)"))
        disposition_fields = tuple(row["name"] for row in structure.execute("PRAGMA table_info(candidate_dispositions)"))
    rebuilt = sqlite3.connect(temporary)
    try:
        rebuilt.execute("PRAGMA foreign_keys=ON")
        rebuilt.executescript(SCHEMA)
        new_meta = {
            **meta,
            "schema_version": "3",
            "tool_version": VERSION,
            "expected_object_count": "182",
            "linked_object_parent_60244": ";".join(
                new_split_keys[("60244", image_id)] for image_id, _, _ in split_specs["60244"]
            ),
            "linked_object_parent_60245": ";".join(
                new_split_keys[("60245", image_id)] for image_id, _, _ in split_specs["60245"]
            ),
            "redundant_derivative_count": "39",
        }
        rebuilt.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", new_meta.items())
        for table, fields, rows in (
            ("objects", object_fields, new_objects),
            ("candidates", candidate_fields, new_candidates),
            ("decisions", decision_fields, new_decisions),
            ("candidate_dispositions", disposition_fields, new_dispositions),
        ):
            rebuilt.executemany(
                f"INSERT INTO {table}({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                [tuple(row.get(field) for field in fields) for row in rows],
            )
        rebuilt.commit()
        if rebuilt.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise WorkbenchError("rebuilt correction database integrity check failed")
        if rebuilt.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkbenchError("rebuilt correction database foreign-key check failed")
    except Exception:
        rebuilt.close()
        temporary.unlink(missing_ok=True)
        raise
    rebuilt.close()
    os.replace(temporary, DATABASE)
    export_decisions()
    status = check_state()
    if (
        status["objects"] != 182
        or status["selected_masters"] != before_selected
        or status["unresolved"] != before_unresolved - 39 + 2
        or status["redundant_derivatives"] != 39
        or status["redundant"] != before_redundant + 158
    ):
        raise WorkbenchError(f"unexpected corrected state: {status}")

    relationship_fields = (
        "parent_md_object_id", "parent_inventory", "primary_record_key",
        "primary_inventory", "primary_md_image_id", "primary_selected_master_path",
        "extension_record_key", "extension_inventory", "extension_md_image_id",
        "extension_selected_master_path", "relationship", "deletion_authorized",
    )
    existing_relationships = read_csv(LINKED_RELATIONSHIPS_CSV)
    atomic_csv(
        LINKED_RELATIONSHIPS_CSV,
        relationship_fields,
        [*existing_relationships, *relationship_rows],
    )
    derivative_fields = (
        "derived_record_key", "derived_md_object_id", "derived_inventory",
        "parent_record_key", "parent_md_object_id", "parent_inventory",
        "parent_selected_master_path", "relationship", "candidate_count",
        "deletion_authorized",
    )
    derivative_rows = [
        {
            "derived_record_key": row["record_key"],
            "derived_md_object_id": row["md_object_id"],
            "derived_inventory": row["inventory"],
            "parent_record_key": row["parent_record_key"],
            "parent_md_object_id": row["parent_md_object_id"],
            "parent_inventory": row["parent_inventory"],
            "parent_selected_master_path": row["parent_selected_master_path"],
            "relationship": "derived museum-digital image redundant after human-confirmed TIFF Master selection",
            "candidate_count": row["candidate_count"],
            "deletion_authorized": "false",
        }
        for row in derivative_targets
    ]
    atomic_csv(DERIVATIVE_RELATIONSHIPS_CSV, derivative_fields, derivative_rows)
    receipt = {
        "format": "iss-wf-alben-linked-derivative-correction-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "explicit user approval: one Master per object and all confirmed derivatives redundant",
        "split_md_parents": ["60244", "60245"],
        "redundant_derivative_records": len(derivative_targets),
        "redundant_derivative_candidates": sum(row["candidate_count"] for row in derivative_targets),
        "selected_parent_masters": len({row["parent_record_key"] for row in derivative_targets}),
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "source_mount": mount,
        "source_files_modified": 0,
        "accession_created": False,
        "drain_entries_created": 0,
        "deletion_authorized": False,
        "validation": status,
    }
    atomic_json(DERIVATIVE_REPAIR_RECEIPT, receipt)
    return receipt


def resolve_catalogue_surrogates(actor: str) -> dict[str, Any]:
    """Resolve the final two objects with exact museum-digital JPEG surrogates."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before surrogate resolution")
    if CATALOGUE_SURROGATE_RECEIPT.exists() or CATALOGUE_SURROGATES_CSV.exists():
        raise WorkbenchError("catalogue surrogate resolution has already been recorded")

    specifications = (
        {
            "record_key": "md:60243:image:43750",
            "md_object_id": "60243",
            "md_image_id": "43750",
            "inventory": "ALB_001 [MD image 43750]",
            "source_url": "https://assets.museum-digital.org/berlin/images/201911/fotoalbum-produktion-im-lkvo-anfang-1946-60243-2.jpg",
            "path": CATALOGUE_SURROGATE_PATHS["md:60243:image:43750"],
            "sha256": "07e62c1a4de53c745763568c1fd37c7c28ba0848ed5433b97c1c2f208cf2aac7",
            "size_bytes": 1123585,
            "width_px": 4128,
            "height_px": 2322,
            "ahash": "ff2f0f0e0e0e00e0",
            "dhash": "5a4968ccdcc80911",
            "phash": "b297e885c46c957a",
            "search_evidence": "no matching asset among 144966 indexed Elements images; nearest pHash distance 10; official catalogue asset is full-size 4128x2322",
        },
        {
            "record_key": "md:60256",
            "md_object_id": "60256",
            "md_image_id": "43824",
            "inventory": "ALB_003-3",
            "source_url": "https://assets.museum-digital.org/berlin/images/201911/s-3-fotoalbum-produktion-im-werk-fuer-fernmeldewesen-hf-1951-60256-2.jpg",
            "path": CATALOGUE_SURROGATE_PATHS["md:60256"],
            "sha256": "309dc50cb94248eee1d5b569f2b0dd520ea47bad03c5beccc1cbb95b434de759",
            "size_bytes": 93347,
            "width_px": 624,
            "height_px": 552,
            "ahash": "00fe7c2000007c7c",
            "dhash": "96e8c9434767f8c8",
            "phash": "c7c2903d3dc86fc2",
            "search_evidence": "ALB-003 source sequence omits page 3 and no matching asset exists among 144966 indexed Elements images; nearest pHash distance 14; only the 624x552 catalogue JPEG survives",
        },
    )
    with connect(read_only=True) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        unresolved = [
            dict(row) for row in connection.execute(
                """SELECT o.record_key, o.md_object_id, o.inventory,
                          (SELECT COUNT(*) FROM candidates c WHERE c.record_key=o.record_key) AS candidate_count
                     FROM objects o JOIN decisions d USING(record_key)
                    WHERE d.decision_state='unresolved' ORDER BY o.record_key"""
            )
        ]
        before_selected = connection.execute(
            "SELECT COUNT(*) FROM decisions WHERE decision_state='selected_master'"
        ).fetchone()[0]
        before_candidates = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    expected_scope = [
        {"record_key": item["record_key"], "md_object_id": item["md_object_id"],
         "inventory": item["inventory"], "candidate_count": 0}
        for item in specifications
    ]
    if unresolved != sorted(expected_scope, key=lambda row: row["record_key"]):
        raise WorkbenchError(f"final unresolved scope changed: {unresolved}")
    if meta.get("schema_version") != "3" or before_candidates != 1068:
        raise WorkbenchError("surrogate resolution requires the validated schema-3, 1068-candidate state")
    if any(item["path"].exists() for item in specifications):
        raise WorkbenchError("catalogue surrogate target already exists")

    downloaded: list[Path] = []
    try:
        for item in specifications:
            temporary = item["path"].with_name(f".{item['path'].name}.download-{os.getpid()}")
            downloaded.append(temporary)
            request = urllib.request.Request(
                item["source_url"], headers={"User-Agent": f"iss-wf-alben-workbench/{VERSION}"}
            )
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as output:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > 100 * 1024 * 1024:
                        raise WorkbenchError("catalogue surrogate exceeds 100 MB guard")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if total != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise WorkbenchError(f"catalogue asset changed for {item['record_key']}")
            with Image.open(temporary) as image:
                if image.format != "JPEG" or image.size != (item["width_px"], item["height_px"]):
                    raise WorkbenchError(f"unexpected catalogue image format for {item['record_key']}")
            os.replace(temporary, item["path"])
            downloaded.remove(temporary)
    finally:
        for temporary in downloaded:
            temporary.unlink(missing_ok=True)

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-catalogue-surrogates-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-catalogue-surrogates-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-catalogue-surrogates-{token}.csv"
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup)):
        raise WorkbenchError("catalogue surrogate backup target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    decided_at = utc_now()
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in specifications:
            stat = item["path"].stat()
            candidate_id = "SURROGATE-" + item["sha256"][:32].upper()
            connection.execute(
                """INSERT INTO candidates(
                       candidate_id, record_key, source_path, filename, extension,
                       size_bytes, mtime_ns, width_px, height_px, visual_index_state,
                       visual_index_error, candidate_relation, relation_basis,
                       suggested_master, phash, dhash, ahash
                   ) VALUES (?, ?, ?, ?, 'jpg', ?, ?, ?, ?, 'indexed', '',
                             'museum_digital_catalogue_surrogate', ?, 0, ?, ?, ?)""",
                (
                    candidate_id, item["record_key"], str(item["path"]), item["path"].name,
                    stat.st_size, stat.st_mtime_ns, item["width_px"], item["height_px"],
                    item["search_evidence"], item["phash"], item["dhash"], item["ahash"],
                ),
            )
            connection.execute(
                """INSERT INTO candidate_dispositions(
                       record_key, candidate_id, disposition, decision_basis
                   ) VALUES (?, ?, 'selected_master', ?)""",
                (
                    item["record_key"], candidate_id,
                    "explicit user-approved catalogue surrogate; no local original or higher-resolution match found",
                ),
            )
            connection.execute(
                """UPDATE decisions SET decision_state='selected_master', selected_candidate_id=?,
                          note=?, decided_by=?, decided_at=?
                       WHERE record_key=? AND decision_state='unresolved'""",
                (
                    candidate_id,
                    "Selected museum-digital catalogue surrogate after complete local visual-index search found no matching source file; original remains unavailable.",
                    actor, decided_at, item["record_key"],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkbenchError(f"surrogate decision scope changed: {item['record_key']}")
            connection.execute(
                """UPDATE objects SET queue_lane='catalogue_surrogate',
                          review_note=? WHERE record_key=?""",
                (item["search_evidence"], item["record_key"]),
            )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('expected_candidate_count', '1070')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tool_version', ?)", (VERSION,)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    export_decisions()
    status = check_state()
    if (
        status["selected_masters"] != before_selected + 2
        or status["unresolved"] != 0
        or status["candidates"] != before_candidates + 2
        or status["deletion_authorized_true"] != 0
    ):
        raise WorkbenchError(f"unexpected catalogue surrogate state: {status}")

    relationship_fields = (
        "parent_md_object_id", "parent_inventory", "primary_record_key",
        "primary_inventory", "primary_md_image_id", "primary_selected_master_path",
        "extension_record_key", "extension_inventory", "extension_md_image_id",
        "extension_selected_master_path", "relationship", "deletion_authorized",
    )
    relationship_rows = read_csv(LINKED_RELATIONSHIPS_CSV)
    for row in relationship_rows:
        if row["parent_md_object_id"] == "60243":
            row["extension_selected_master_path"] = str(
                CATALOGUE_SURROGATE_PATHS["md:60243:image:43750"]
            )
    atomic_csv(LINKED_RELATIONSHIPS_CSV, relationship_fields, relationship_rows)

    manifest_fields = (
        "record_key", "md_object_id", "md_image_id", "inventory", "surrogate_path",
        "source_url", "source_dimensions", "source_size_bytes", "sha256",
        "selection_state", "preservation_basis", "search_evidence",
        "drain_list_state", "deletion_authorized",
    )
    manifest_rows = [
        {
            "record_key": item["record_key"],
            "md_object_id": item["md_object_id"],
            "md_image_id": item["md_image_id"],
            "inventory": item["inventory"],
            "surrogate_path": str(item["path"]),
            "source_url": item["source_url"],
            "source_dimensions": f"{item['width_px']}x{item['height_px']}",
            "source_size_bytes": item["size_bytes"],
            "sha256": "sha256:" + item["sha256"],
            "selection_state": "selected_master_catalogue_surrogate",
            "preservation_basis": "user_approved_catalogue_surrogate_no_local_match",
            "search_evidence": item["search_evidence"],
            "drain_list_state": "excluded_catalogue_surrogate_preserve_evidence",
            "deletion_authorized": "false",
        }
        for item in specifications
    ]
    atomic_csv(CATALOGUE_SURROGATES_CSV, manifest_fields, manifest_rows)
    receipt = {
        "format": "iss-wf-alben-catalogue-surrogate-resolution-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "explicit user instruction to use the existing catalogue image as a surrogate when no higher-resolution local match is found",
        "surrogates": manifest_rows,
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "source_mount": mount,
        "source_files_modified": 0,
        "catalogue_assets_reencoded": False,
        "accession_created": False,
        "drain_entries_created": 0,
        "deletion_authorized": False,
        "validation": status,
    }
    atomic_json(CATALOGUE_SURROGATE_RECEIPT, receipt)
    return receipt


def apply_metadata_editorial_resolution(actor: str) -> dict[str, Any]:
    """Resolve deterministic text errors and queue only the rights ambiguity."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before metadata migration")
    if METADATA_EDITORIAL_RECEIPT.exists() or METADATA_EDITORIAL_CSV.exists():
        raise WorkbenchError("metadata editorial resolution has already been recorded")

    records = load_md_records()
    description_ids = sorted(
        object_id for object_id, record in records.items()
        if "das das 6. WTB" in str(record.get("object_description") or "")
        and "dass sich mit Entwicklung" in str(record.get("object_description") or "")
    )
    if len(description_ids) != 63:
        raise WorkbenchError(f"obvious NEF boilerplate scope changed: {len(description_ids)}")
    isolated_description_replacements = {
        "93482": (("Titel das Fotoalbums", "Titel des Fotoalbums"),),
        "93535": (("Vervielfältgungsapparat", "Vervielfältigungsapparat"),),
        "93536": (("beim Anfertigung von Lichtpausen", "beim Anfertigen von Lichtpausen"),),
        "93550": (("einesElektrokrans", "eines Elektrokrans"),),
    }
    obvious_rows: list[dict[str, Any]] = []
    decided_at = utc_now()
    for object_id in description_ids:
        record = records[object_id]
        original = str(record["object_description"])
        resolved = original.replace("das das 6. WTB", "das 6. WTB").replace(
            "dass sich mit Entwicklung", "das sich mit Entwicklung"
        )
        for before, after in isolated_description_replacements.get(object_id, ()):
            if before not in resolved:
                raise WorkbenchError(f"isolated correction evidence changed for MD {object_id}")
            resolved = resolved.replace(before, after)
        obvious_rows.append(
            {
                "md_object_id": object_id,
                "inventory": str(record["object_inventory_number"]),
                "field_name": "object_description",
                "original_value": original,
                "resolved_value": resolved,
                "decision_state": "auto_resolved",
                "basis": "deterministic editorial correction: duplicated article and relative-pronoun typo in repeated NEF boilerplate"
                + ("; plus isolated grammatical or spacing typo" if object_id in isolated_description_replacements else ""),
                "decided_by": actor,
                "decided_at": decided_at,
                "deletion_authorized": 0,
            }
        )
    title_record = records["93486"]
    original_title = str(title_record["object_name"])
    expected_title = "Behrensbau; Fotoalbum NEF, S.7.Fotoalbum NEF, S.7.; Foto 1946"
    if original_title != expected_title:
        raise WorkbenchError("MD 93486 duplicated-title evidence changed")
    corrected_title = "Behrensbau; Fotoalbum NEF, S. 7; Foto 1946"
    obvious_rows.append(
        {
            "md_object_id": "93486",
            "inventory": str(title_record["object_inventory_number"]),
            "field_name": "object_name",
            "original_value": original_title,
            "resolved_value": corrected_title,
            "decision_state": "auto_resolved",
            "basis": "deterministic editorial correction: remove duplicated title phrase and normalize spacing",
            "decided_by": actor,
            "decided_at": decided_at,
            "deletion_authorized": 0,
        }
    )
    rights_record = records["65109"]
    rights_images = [
        image for image in rights_record.get("object_images") or []
        if str(image.get("quell_id")) == "48177"
    ]
    if len(rights_images) != 1 or rights_images[0].get("rights") != "CC BY-NC-SA":
        raise WorkbenchError("MD 65109 image-rights ambiguity changed")
    ambiguous_row = {
        "md_object_id": "65109",
        "inventory": str(rights_record["object_inventory_number"]),
        "field_name": "image:48177:rights",
        "original_value": "CC BY-NC-SA",
        "resolved_value": "",
        "decision_state": "unresolved",
        "basis": "ambiguous rights outlier: this image is CC BY-NC-SA while 181 other WF-Alben images are CC BY-SA; rights cannot be normalized automatically",
        "decided_by": "",
        "decided_at": "",
        "deletion_authorized": 0,
    }
    if len(obvious_rows) != 64:
        raise WorkbenchError(f"obvious metadata decision scope changed: {len(obvious_rows)}")

    with connect(read_only=True) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != "3":
            raise WorkbenchError(f"expected schema version 3, found {meta.get('schema_version')}")
        if table_exists(connection, "metadata_editorial_decisions"):
            raise WorkbenchError("metadata editorial table already exists")
        current_title = connection.execute(
            "SELECT title FROM objects WHERE md_object_id='93486'"
        ).fetchone()
        if current_title is None or current_title["title"] != original_title:
            raise WorkbenchError("workbench MD 93486 title no longer matches captured title")

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-metadata-editorial-{token}.sqlite"
    decisions_backup = REVIEW_ROOT / f"workbench-decisions.pre-metadata-editorial-{token}.csv"
    dispositions_backup = REVIEW_ROOT / f"workbench-candidate-dispositions.pre-metadata-editorial-{token}.csv"
    if any(path.exists() for path in (database_backup, decisions_backup, dispositions_backup)):
        raise WorkbenchError("metadata editorial backup target already exists")
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(DECISIONS_CSV, decisions_backup)
    shutil.copy2(DISPOSITIONS_CSV, dispositions_backup)

    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE metadata_editorial_decisions (
                   md_object_id TEXT NOT NULL,
                   inventory TEXT NOT NULL,
                   field_name TEXT NOT NULL,
                   original_value TEXT NOT NULL,
                   resolved_value TEXT NOT NULL DEFAULT '',
                   decision_state TEXT NOT NULL CHECK(decision_state IN (
                       'unresolved', 'auto_resolved', 'resolved_keep_current', 'resolved_corrected'
                   )),
                   basis TEXT NOT NULL,
                   decided_by TEXT NOT NULL DEFAULT '',
                   decided_at TEXT NOT NULL DEFAULT '',
                   deletion_authorized INTEGER NOT NULL DEFAULT 0 CHECK(deletion_authorized = 0),
                   PRIMARY KEY(md_object_id, field_name)
               )"""
        )
        fields = (
            "md_object_id", "inventory", "field_name", "original_value",
            "resolved_value", "decision_state", "basis", "decided_by",
            "decided_at", "deletion_authorized",
        )
        rows = [*obvious_rows, ambiguous_row]
        connection.executemany(
            f"INSERT INTO metadata_editorial_decisions({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            [tuple(row[field] for field in fields) for row in rows],
        )
        connection.execute(
            "UPDATE objects SET title=? WHERE md_object_id='93486'", (corrected_title,)
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise WorkbenchError("unexpected MD 93486 workbench title update count")
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '4')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tool_version', ?)", (VERSION,)
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('expected_metadata_editorial_decisions', '65')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('metadata_editorial_state', '64_auto_resolved_1_ambiguous')"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    export_decisions()
    status = check_state()
    if (
        status.get("metadata_editorial_decisions") != 65
        or status.get("metadata_auto_resolved") != 64
        or status.get("metadata_ambiguous") != 1
        or status.get("metadata_human_resolved") != 0
    ):
        raise WorkbenchError(f"unexpected metadata editorial state: {status}")
    receipt = {
        "format": "iss-wf-alben-metadata-editorial-resolution-v1",
        "completed_at": utc_now(),
        "actor": actor,
        "authority": "explicit user instruction to resolve obvious metadata cases and queue only ambiguous cases",
        "capture_modified": False,
        "auto_resolved_fields": len(obvious_rows),
        "auto_resolved_objects": len({row["md_object_id"] for row in obvious_rows}),
        "ambiguous_fields": 1,
        "ambiguous_objects": ["65109"],
        "ambiguous_basis": ambiguous_row["basis"],
        "pre_change_backups": {
            "database": str(database_backup),
            "decisions_csv": str(decisions_backup),
            "candidate_dispositions_csv": str(dispositions_backup),
        },
        "source_mount": mount,
        "source_files_modified": 0,
        "deletion_authorized": False,
        "validation": status,
    }
    atomic_json(METADATA_EDITORIAL_RECEIPT, receipt)
    return receipt


def complete_metadata_editorial_image_name(actor: str) -> dict[str, Any]:
    """Add the deterministic MD 93486 image-name correction found in UI verification."""
    mount = source_mount()
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-wf-alben-workbench.service"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active == "active":
        raise WorkbenchError("stop iss-archive-wf-alben-workbench.service before metadata completion")
    records = load_md_records()
    images = [
        image for image in records["93486"].get("object_images") or []
        if str(image.get("quell_id")) == "69826"
    ]
    original = "Behrensbau; Fotoalbum NEF, S.7.Fotoalbum NEF, S.7.; Foto 1946"
    corrected = "Behrensbau; Fotoalbum NEF, S. 7; Foto 1946"
    if len(images) != 1 or images[0].get("name") != original:
        raise WorkbenchError("MD 93486 image-name evidence changed")
    with connect(read_only=True) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        counts = dict(
            connection.execute(
                "SELECT decision_state, COUNT(*) FROM metadata_editorial_decisions GROUP BY decision_state"
            )
        )
        exists = connection.execute(
            """SELECT 1 FROM metadata_editorial_decisions
                WHERE md_object_id='93486' AND field_name='image:69826:name'"""
        ).fetchone()
    if meta.get("schema_version") != "4" or counts.get("auto_resolved") != 64 or counts.get("unresolved") != 1 or exists:
        raise WorkbenchError("metadata completion scope changed")

    token = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database_backup = REVIEW_ROOT / f"workbench.pre-metadata-image-name-{token}.sqlite"
    metadata_backup = REVIEW_ROOT / f"metadata-editorial-decisions.pre-image-name-{token}.csv"
    with connect(read_only=True) as source, sqlite3.connect(database_backup) as destination:
        source.backup(destination)
    shutil.copy2(METADATA_EDITORIAL_CSV, metadata_backup)
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO metadata_editorial_decisions(
                   md_object_id, inventory, field_name, original_value, resolved_value,
                   decision_state, basis, decided_by, decided_at
               ) VALUES ('93486', 'Album_NEF_Seite_07-1', 'image:69826:name', ?, ?,
                         'auto_resolved', ?, ?, ?)""",
            (
                original, corrected,
                "deterministic editorial correction: remove duplicated image-name phrase and normalize spacing",
                actor, utc_now(),
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('expected_metadata_editorial_decisions', '66')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('metadata_editorial_state', '65_auto_resolved_1_ambiguous')"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    export_decisions()
    status = check_state()
    if status.get("metadata_editorial_decisions") != 66 or status.get("metadata_auto_resolved") != 65 or status.get("metadata_ambiguous") != 1:
        raise WorkbenchError(f"unexpected completed metadata state: {status}")
    receipt = json.loads(METADATA_EDITORIAL_RECEIPT.read_text(encoding="utf-8"))
    receipt.update(
        completed_at=utc_now(),
        auto_resolved_fields=65,
        validation=status,
        ui_verification_followup={
            "md_object_id": "93486",
            "field_name": "image:69826:name",
            "basis": "duplicated image-level name found during live workbench verification",
            "pre_change_database": str(database_backup),
            "pre_change_metadata_csv": str(metadata_backup),
        },
    )
    atomic_json(METADATA_EDITORIAL_RECEIPT, receipt)
    return receipt


def self_test() -> dict[str, Any]:
    cases = {
        ("60244", "ALB-002-Cover.jpg"): "43752",
        ("60244", "ALB-002-Titel.tif"): "43753",
        ("60245", "ALB_003-Cover-gross.jpg"): "43748",
        ("60245", "Alb-003-Titel.jpg"): "43749",
        ("60245", "Alb-003-Titel.tif"): "43749",
    }
    for arguments, expected in cases.items():
        actual = split_component_for_candidate(*arguments)
        if actual != expected:
            raise WorkbenchError(f"component partition test failed: {arguments} -> {actual}")
    if nef_parent_inventory("Album_NEF_Seite_07-1") != "Album_NEF_Seite_07":
        raise WorkbenchError("NEF parent parsing test failed")
    if nef_parent_inventory("ALB_003-3"):
        raise WorkbenchError("non-NEF inventory was incorrectly treated as a derivative")
    return {"tool_version": VERSION, "tests": len(cases) + 2, "status": "ok"}


CSS = """
:root{font-family:system-ui,sans-serif;color:#222;background:#f4f1eb}
body{max-width:1500px;margin:auto;padding:18px}a{color:#174f78}header{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.notice{background:#fff3cd;border:1px solid #d7b553;padding:10px;border-radius:8px}.ok{background:#dff2e4;border-color:#7ab589}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}.card{background:white;border:1px solid #ccc;border-radius:9px;padding:12px}
.candidate{position:relative}.candidate.suggested{border:2px solid #c18b1b}.candidate.selected{border:3px solid #24703a}.preview{width:100%;height:260px;object-fit:contain;background:#eee}
.meta{font-size:.88rem;color:#444;overflow-wrap:anywhere}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#e4e8ed;margin:2px;font-size:.8rem}
.md-record{background:#e9f1f6;border:2px solid #487998;border-radius:9px;padding:12px;margin:14px 0}.md-layout{display:grid;grid-template-columns:minmax(360px,1fr) minmax(360px,1fr);gap:16px}.md-images{display:grid;gap:10px}.md-images figure{margin:0}.md-preview{display:block;width:100%;max-height:520px;object-fit:contain;background:#222}.md-images figcaption{font-size:.85rem;margin-top:4px}.md-record dl{display:grid;grid-template-columns:minmax(130px,auto) 1fr;gap:5px 10px}.md-record dt{font-weight:700}.md-record dd{margin:0}
input[type=search]{min-width:320px;padding:8px}textarea{width:100%;min-height:70px}.actions{position:sticky;bottom:0;background:#fff;padding:12px;border:1px solid #bbb;border-radius:8px}
button{padding:10px 14px;margin:4px;font-weight:650}.danger-note{color:#8a2e18}table{width:100%;border-collapse:collapse;background:white}td,th{padding:7px;border-bottom:1px solid #ddd;text-align:left}
@media(max-width:850px){.md-layout{grid-template-columns:1fr}}
"""


def page(title: str, body: str) -> bytes:
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style></head><body><header><h1>{esc(title)}</h1><a href="/">Master queue</a><a href="/metadata">Metadata review</a></header>{body}</body></html>"""
    return document.encode("utf-8")


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], actor: str):
        self.actor = actor
        self.csrf_token = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        self.md_records = load_md_records()
        super().__init__(address, WorkbenchHandler)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' https://assets.museum-digital.org",
        )
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, target: str) -> None:
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.queue_page(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path == "/metadata":
            self.metadata_page(urllib.parse.parse_qs(parsed.query))
            return
        if parsed.path.startswith("/object/"):
            self.object_page(
                urllib.parse.unquote(parsed.path.removeprefix("/object/")),
                urllib.parse.parse_qs(parsed.query),
            )
            return
        if parsed.path.startswith("/preview/"):
            self.preview(urllib.parse.unquote(parsed.path.removeprefix("/preview/")))
            return
        if parsed.path == "/health":
            try:
                payload = json.dumps(check_state(), sort_keys=True).encode()
                self.send_bytes(200, payload, "application/json")
            except Exception as exc:  # health must report failure
                self.send_bytes(503, json.dumps({"error": str(exc)}).encode(), "application/json")
            return
        self.send_bytes(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        request_path = urllib.parse.urlparse(self.path).path
        if request_path not in {"/decision", "/metadata-decision"}:
            self.send_bytes(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32768:
                raise WorkbenchError("invalid form size")
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            if secrets.compare_digest(form.get("csrf", [""])[0], self.server.csrf_token) is False:
                raise WorkbenchError("invalid request token")
            if request_path == "/metadata-decision":
                save_metadata_decision(
                    form.get("md_object_id", [""])[0],
                    form.get("field_name", [""])[0],
                    form.get("action", [""])[0],
                    self.server.actor,
                    self.server.write_lock,
                )
                self.redirect("/metadata?saved=1")
                return
            record_key = form.get("record_key", [""])[0]
            action = form.get("action", [""])[0]
            selected = form.get("selected_candidate_id", [""])[0]
            note = form.get("note", [""])[0].strip()
            if len(note) > 1000:
                raise WorkbenchError("note is longer than 1000 characters")
            save_decision(record_key, action, selected, note, self.server.actor, self.server.write_lock)
            next_record_key = next_unresolved_record(record_key)
            if next_record_key:
                query = urllib.parse.urlencode({"previous_saved": record_key})
                self.redirect(f"/object/{urllib.parse.quote(next_record_key, safe='')}?{query}")
            else:
                self.redirect("/?saved=1")
        except (WorkbenchError, OSError, sqlite3.Error) as exc:
            self.send_bytes(400, page("Decision rejected", f"<p class='notice'>{esc(exc)}</p>"), "text/html; charset=utf-8")

    def metadata_page(self, params: dict[str, list[str]]) -> None:
        with connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT * FROM metadata_editorial_decisions
                    WHERE decision_state='unresolved'
                    ORDER BY CAST(md_object_id AS INTEGER), field_name"""
            ).fetchall()
            counts = dict(
                connection.execute(
                    "SELECT decision_state, COUNT(*) FROM metadata_editorial_decisions GROUP BY decision_state"
                )
            )
        saved = (
            "<div class='notice ok'><b>Metadata decision saved.</b></div>"
            if params.get("saved", [""])[0] else ""
        )
        summary = f"""{saved}<div class="notice ok"><b>Obvious metadata resolved:</b> {counts.get('auto_resolved', 0)} fields. The immutable museum-digital capture is unchanged; effective archive metadata uses the recorded overlay.</div>
<p>{counts.get('unresolved', 0)} ambiguous metadata cases require human review · {counts.get('resolved_keep_current', 0) + counts.get('resolved_corrected', 0)} human decisions saved.</p>"""
        if not rows:
            self.send_bytes(
                200,
                page("WF-Alben metadata review", summary + "<div class='notice ok'><b>Metadata queue complete.</b></div>"),
                "text/html; charset=utf-8",
            )
            return
        cards = []
        for row in rows:
            record = self.server.md_records.get(row["md_object_id"])
            image_id = row["field_name"].split(":")[1]
            image = next(
                (item for item in (record or {}).get("object_images", []) if str(item.get("quell_id")) == image_id),
                {},
            )
            cards.append(
                f"""<article class="card"><span class="badge">ambiguous rights</span>
<h2>{esc(row['inventory'])}</h2><p>{esc((record or {}).get('object_name'))}</p>
<img class="preview" src="{esc(md_image_url(image))}" alt="museum-digital image">
<p><b>Current image rights:</b> {esc(row['original_value'])}</p>
<p><b>Collection comparison:</b> 181 other WF-Alben images are CC BY-SA.</p>
<p class="meta">{esc(row['basis'])}</p>
<p><a href="/object/md%3A{esc(row['md_object_id'])}">Open archival object</a> · <a href="https://berlin.museum-digital.de/object/{esc(row['md_object_id'])}">Open museum-digital record</a></p>
<form method="post" action="/metadata-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="md_object_id" value="{esc(row['md_object_id'])}"><input type="hidden" name="field_name" value="{esc(row['field_name'])}">
<button name="action" value="keep_current">Keep CC BY-NC-SA</button>
<button name="action" value="use_collection_standard">Use CC BY-SA</button>
<span class="danger-note">Rights are never changed automatically.</span></form></article>"""
            )
        self.send_bytes(
            200,
            page("WF-Alben metadata review", summary + '<div class="grid">' + "".join(cards) + "</div>"),
            "text/html; charset=utf-8",
        )

    def queue_page(self, params: dict[str, list[str]]) -> None:
        state = params.get("state", ["unresolved"])[0]
        if state not in {"unresolved", "selected_master", "redundant_derivative", "all"}:
            state = "unresolved"
        query = params.get("q", [""])[0].strip()
        lane = params.get("lane", [""])[0]
        sql = """
            SELECT o.*, d.decision_state, d.selected_candidate_id,
                   (SELECT COUNT(*) FROM candidates c WHERE c.record_key=o.record_key) AS candidate_count
              FROM objects o JOIN decisions d USING(record_key) WHERE 1=1
        """
        values: list[Any] = []
        if state != "all":
            sql += " AND d.decision_state=?"
            values.append(state)
        if query:
            sql += " AND (lower(o.inventory) LIKE ? OR lower(o.title) LIKE ? OR o.md_object_id LIKE ?)"
            pattern = f"%{query.casefold()}%"
            values.extend((pattern, pattern, f"%{query}%"))
        if lane:
            sql += " AND o.queue_lane=?"
            values.append(lane)
        sql += " ORDER BY lower(o.inventory), o.md_object_id"
        with connect(read_only=True) as connection:
            rows = connection.execute(sql, values).fetchall()
            counts = dict(connection.execute("SELECT decision_state, COUNT(*) FROM decisions GROUP BY decision_state"))
            lanes = connection.execute("SELECT queue_lane, COUNT(*) FROM objects GROUP BY queue_lane ORDER BY queue_lane").fetchall()
            metadata_ambiguous = connection.execute(
                "SELECT COUNT(*) FROM metadata_editorial_decisions WHERE decision_state='unresolved'"
            ).fetchone()[0]
        controls = f"""<div class="notice"><b>No automatic selection.</b> Suggestions are evidence only. Drain and deletion remain disabled.</div>
<form method="get"><input type="search" name="q" value="{esc(query)}" placeholder="Inventory, title, or MD ID">
<select name="state"><option value="unresolved"{' selected' if state=='unresolved' else ''}>Unresolved</option><option value="selected_master"{' selected' if state=='selected_master' else ''}>Resolved</option><option value="redundant_derivative"{' selected' if state=='redundant_derivative' else ''}>Redundant derivatives</option><option value="all"{' selected' if state=='all' else ''}>All</option></select>
<select name="lane"><option value="">All lanes</option>{''.join(f'<option value="{esc(x[0])}"{" selected" if lane==x[0] else ""}>{esc(x[0])} ({x[1]})</option>' for x in lanes)}</select><button>Filter</button></form>
<p>{counts.get('unresolved',0)} unresolved · {counts.get('selected_master',0)} selected Masters · {counts.get('redundant_derivative',0)} redundant derivative records · <a href="/metadata">{metadata_ambiguous} ambiguous metadata cases</a> · showing {len(rows)}</p>"""
        cards = []
        for row in rows:
            cards.append(
                f"""<article class="card"><div><span class="badge">{esc(row['queue_lane'])}</span><span class="badge">{row['candidate_count']} candidates</span></div>
<h3><a href="/object/{urllib.parse.quote(row['record_key'], safe='')}">{esc(row['inventory'])}</a></h3>
<p>{esc(row['title'])}</p><p class="meta">MD {esc(row['md_object_id'])} · {esc(row['decision_state'])}</p></article>"""
            )
        self.send_bytes(200, page("WF-Alben Master review", controls + '<div class="grid">' + "".join(cards) + "</div>"), "text/html; charset=utf-8")

    def object_page(self, record_key: str, params: dict[str, list[str]]) -> None:
        with connect(read_only=True) as connection:
            obj = connection.execute(
                "SELECT o.*, d.* FROM objects o JOIN decisions d USING(record_key) WHERE o.record_key=?",
                (record_key,),
            ).fetchone()
            linked = []
            if obj is None and record_key.startswith("md:"):
                parent_id = record_key.removeprefix("md:")
                linked = connection.execute(
                    """SELECT o.*, d.decision_state, d.selected_candidate_id,
                              c.source_path AS selected_master_path
                         FROM objects o JOIN decisions d USING(record_key)
                         LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                        WHERE o.parent_md_object_id=?
                        ORDER BY CASE o.link_relation WHEN 'extended_by' THEN 0 ELSE 1 END""",
                    (parent_id,),
                ).fetchall()
            candidates = connection.execute(
                """SELECT c.*, cd.disposition FROM candidates c
                   JOIN candidate_dispositions cd USING(record_key, candidate_id)
                  WHERE c.record_key=?
                  ORDER BY c.suggested_master DESC,
                           CASE WHEN lower(c.extension) IN ('tif','tiff') THEN 0 ELSE 1 END,
                           c.source_path""",
                (record_key,),
            ).fetchall()
            linked_master = None
            if obj is not None and obj["linked_record_key"]:
                linked_master = connection.execute(
                    """SELECT o.inventory, d.decision_state, c.source_path
                         FROM objects o JOIN decisions d USING(record_key)
                         LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                        WHERE o.record_key=?""",
                    (obj["linked_record_key"],),
                ).fetchone()
        if obj is None:
            if linked:
                self.linked_group_page(record_key.removeprefix("md:"), linked)
                return
            self.send_bytes(404, b"unknown object", "text/plain; charset=utf-8")
            return
        saved_notice = ""
        if params.get("previous_saved", [""])[0]:
            saved_notice = "<div class='notice ok'><b>Previous decision saved.</b> Showing the next unresolved object.</div>"
        info = saved_notice + f"""<div class="notice"><b>{esc(obj['queue_lane'])}</b>: {esc(obj['review_note'])}</div>
<h2>{esc(obj['inventory'])}</h2><p>{esc(obj['title'])}</p>
<p class="meta">MD object <a href="{esc(obj['md_url'])}">{esc(obj['md_object_id'])}</a> · {esc(obj['date_text'])} · {esc(obj['object_type'])} · MD images: {obj['md_image_count']}</p>
<p class="meta">Metadata: {esc(obj['metadata_reconciliation_state'])} ({esc(obj['metadata_comparison_state'])})</p>
<p><b>Current decision:</b> {esc(obj['decision_state'])}</p>"""
        linked_banner = ""
        if obj["linked_record_key"]:
            if obj["link_relation"] == "derived_from":
                linked_banner = f"""<div class="notice ok"><b>Redundant derivative record.</b> These source files are retained but do not compete as Masters. Parent: <a href="/object/{urllib.parse.quote(obj['linked_record_key'], safe='')}">{esc(linked_master['inventory'] if linked_master else obj['linked_record_key'])}</a> · selected TIFF Master: {esc(linked_master['source_path'] if linked_master else '')}</div>"""
            else:
                linked_banner = f"""<div class="notice ok"><b>Linked component:</b> {esc(obj['component_label'])} · relation <code>{esc(obj['link_relation'])}</code> · <a href="/object/{urllib.parse.quote(obj['linked_record_key'], safe='')}">open linked component</a> · <a href="/object/md%3A{esc(obj['parent_md_object_id'])}">open combined MD group</a></div>"""
        md_panel = md_record_panel(
            effective_md_record(self.server.md_records.get(obj["md_object_id"]), obj["md_object_id"]),
            obj["md_url"], obj["md_image_id"] or ""
        )
        cards = []
        derivative_record = obj["decision_state"] == "redundant_derivative"
        for candidate in candidates:
            checked = candidate["candidate_id"] == obj["selected_candidate_id"]
            classes = "card candidate" + (" suggested" if candidate["suggested_master"] else "") + (" selected" if checked else "")
            badges = []
            if candidate["suggested_master"]:
                badges.append("suggested only")
            badges.extend((candidate["extension"].upper(), candidate["visual_index_state"], candidate["disposition"]))
            selection_control = (
                "<p><b>Redundant derivative of the linked selected Master; source retained.</b></p>"
                if derivative_record
                else f"<p><label><input type=\"radio\" name=\"selected_candidate_id\" value=\"{esc(candidate['candidate_id'])}\"{' checked' if checked else ''}> <b>Select this file as the single Master</b></label></p>"
            )
            cards.append(
                f"""<article class="{classes}"><img class="preview" loading="lazy" src="/preview/{urllib.parse.quote(candidate['candidate_id'], safe='')}" alt="Preview unavailable if decode fails">
{selection_control}
<div>{''.join(f'<span class="badge">{esc(label)}</span>' for label in badges)}</div>
<p><b>{esc(candidate['filename'])}</b></p>
<p class="meta">{candidate['width_px'] or '?'} × {candidate['height_px'] or '?'} px · {human_size(candidate['size_bytes'])}<br>{esc(candidate['source_path'])}<br>{esc(candidate['candidate_relation'])}: {esc(candidate['relation_basis'])}</p>
{f'<p class="danger-note">Decode evidence: {esc(candidate["visual_index_error"])}</p>' if candidate['visual_index_error'] else ''}
<details><summary>Similarity evidence</summary><p class="meta">pHash {esc(candidate['phash']) or '—'}<br>dHash {esc(candidate['dhash']) or '—'}<br>aHash {esc(candidate['ahash']) or '—'}<br>These hashes indicate related versions, not exact identity.</p></details></article>"""
            )
        if derivative_record:
            form = f"""<div class="grid">{''.join(cards)}</div><div class="actions"><a href="/">Return to unresolved queue</a><span class="danger-note"> No drain or deletion action is created.</span></div>"""
        else:
            next_key = next_unresolved_record(record_key)
            next_label = "Skip without saving" if obj["decision_state"] == "unresolved" else "Open next unresolved"
            skip_link = (
                f'<a class="button" href="/object/{urllib.parse.quote(next_key, safe="")}">{next_label}</a>'
                if next_key else '<a href="/">Return to queue</a>'
            )
            form = f"""<form method="post" action="/decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record_key)}">
<div class="grid">{''.join(cards) if cards else '<p class="notice">No local candidate. Leave this object unresolved.</p>'}</div>
<div class="actions"><label>Review note<br><textarea name="note" maxlength="1000">{esc(obj['note'])}</textarea></label><br>
<button name="action" value="select"{' disabled' if not candidates else ''}>Select checked Master and mark all others redundant</button>
{skip_link}
<span class="danger-note">No drain or deletion action is created.</span></div></form>"""
        self.send_bytes(200, page(f"WF-Alben · {obj['inventory']}", info + linked_banner + md_panel + form), "text/html; charset=utf-8")

    def linked_group_page(self, parent_md_object_id: str, linked: list[sqlite3.Row]) -> None:
        md_record = effective_md_record(self.server.md_records.get(parent_md_object_id), parent_md_object_id)
        official_url = f"https://berlin.museum-digital.de/object/{urllib.parse.quote(parent_md_object_id)}"
        md_panel = md_record_panel(md_record, official_url)
        if any(row["link_relation"] in {"extends", "extended_by"} for row in linked):
            relationship_text = "The extending legend belongs with the primary photograph"
        else:
            relationship_text = "The paired views belong together under the same MD parent"
        cards = []
        for row in linked:
            cards.append(
                f"""<article class="card {'selected' if row['decision_state']=='selected_master' else ''}"><span class="badge">{esc(row['component_label'])}</span><span class="badge">{esc(row['link_relation'])}</span>
<h2><a href="/object/{urllib.parse.quote(row['record_key'], safe='')}">{esc(row['inventory'])}</a></h2><p>{esc(row['title'])}</p>
<p><b>{esc(row['decision_state'])}</b></p><p class="meta">MD image {esc(row['md_image_id'])}<br>{esc(row['selected_master_path']) or 'No Master selected'}</p></article>"""
            )
        body = f"""<div class="notice ok"><b>One museum-digital record, two linked archival objects.</b> {relationship_text}, while each object retains exactly one Master.</div>{md_panel}<h2>Linked archival components</h2><div class="grid">{''.join(cards)}</div>"""
        self.send_bytes(200, page(f"WF-Alben · linked MD {parent_md_object_id}", body), "text/html; charset=utf-8")

    def preview(self, candidate_id: str) -> None:
        with connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT source_path, size_bytes, mtime_ns FROM candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            self.send_bytes(404, b"unknown candidate", "text/plain; charset=utf-8")
            return
        try:
            path = Path(row["source_path"])
            if not allowed_candidate_path(path):
                raise WorkbenchError("candidate source guard failed")
            stat = path.stat()
            if stat.st_size != row["size_bytes"] or stat.st_mtime_ns != row["mtime_ns"]:
                raise WorkbenchError("candidate changed since projection")
            with Image.open(path) as image:
                image.seek(0)
                image.thumbnail((1200, 1200))
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=84, optimize=True)
            self.send_bytes(200, output.getvalue(), "image/jpeg")
        except (OSError, ValueError, UnidentifiedImageError, WorkbenchError) as exc:
            placeholder = page("Preview unavailable", f"<p class='notice'>{esc(exc)}</p>")
            self.send_bytes(415, placeholder, "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def save_metadata_decision(
    md_object_id: str, field_name: str, action: str,
    actor: str, write_lock: threading.Lock,
) -> None:
    source_mount()
    if (md_object_id, field_name) != ("65109", "image:48177:rights"):
        raise WorkbenchError("unknown ambiguous metadata case")
    if action not in {"keep_current", "use_collection_standard"}:
        raise WorkbenchError("unknown metadata decision action")
    state = "resolved_keep_current" if action == "keep_current" else "resolved_corrected"
    value = "CC BY-NC-SA" if action == "keep_current" else "CC BY-SA"
    basis = (
        "explicit human confirmation to retain the captured image-specific rights value"
        if action == "keep_current"
        else "explicit human confirmation that the collection-standard image rights value applies"
    )
    with write_lock:
        connection = connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE metadata_editorial_decisions
                      SET resolved_value=?, decision_state=?, basis=?, decided_by=?, decided_at=?
                    WHERE md_object_id=? AND field_name=?
                      AND decision_state='unresolved' AND original_value='CC BY-NC-SA'""",
                (value, state, basis, actor, utc_now(), md_object_id, field_name),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkbenchError("metadata case is no longer unresolved")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('metadata_editorial_state', 'complete')"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        export_decisions()
        check_state()


def save_decision(
    record_key: str, action: str, selected_candidate_id: str, note: str,
    actor: str, write_lock: threading.Lock,
) -> None:
    source_mount()
    if action not in {"select", "unresolved"}:
        raise WorkbenchError("unknown decision action")
    with write_lock:
        connection = connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            obj = connection.execute(
                """SELECT d.decision_state FROM objects o JOIN decisions d USING(record_key)
                   WHERE o.record_key=?""",
                (record_key,),
            ).fetchone()
            if obj is None:
                raise WorkbenchError("unknown object")
            if obj["decision_state"] == "redundant_derivative":
                raise WorkbenchError("confirmed derivative records cannot be promoted to Masters")
            if action == "unresolved" and obj["decision_state"] != "unresolved":
                raise WorkbenchError("a resolved Master cannot be cleared from the review queue")
            decided_at = utc_now()
            if action == "select":
                candidate = connection.execute(
                    """SELECT source_path, size_bytes, mtime_ns FROM candidates
                       WHERE candidate_id=? AND record_key=?""",
                    (selected_candidate_id, record_key),
                ).fetchone()
                if candidate is None:
                    raise WorkbenchError("select one candidate belonging to this object")
                path = Path(candidate["source_path"])
                if not allowed_candidate_path(path):
                    raise WorkbenchError("selected candidate source guard failed")
                stat = path.stat()
                if stat.st_size != candidate["size_bytes"] or stat.st_mtime_ns != candidate["mtime_ns"]:
                    raise WorkbenchError("selected candidate changed since projection")
                connection.execute(
                    """UPDATE decisions SET decision_state='selected_master', selected_candidate_id=?,
                       note=?, decided_by=?, decided_at=?
                       WHERE record_key=? AND decision_state IN ('unresolved', 'selected_master')""",
                    (selected_candidate_id, note, actor, decided_at, record_key),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WorkbenchError("decision changed before save")
                connection.execute(
                    """UPDATE candidate_dispositions
                          SET disposition=CASE WHEN candidate_id=? THEN 'selected_master' ELSE 'redundant' END,
                              decision_basis='explicit human Master selection; non-selected candidates redundant'
                        WHERE record_key=?""",
                    (selected_candidate_id, record_key),
                )
            else:
                connection.execute(
                    """UPDATE decisions SET decision_state='unresolved', selected_candidate_id=NULL,
                       note=?, decided_by=?, decided_at=? WHERE record_key=?""",
                    (note, actor, decided_at, record_key),
                )
                connection.execute(
                    """UPDATE candidate_dispositions SET disposition='undecided',
                       decision_basis='object left unresolved by human reviewer' WHERE record_key=?""",
                    (record_key,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        export_decisions()
        check_state()


def next_unresolved_record(current_record_key: str) -> str:
    with connect(read_only=True) as connection:
        current = connection.execute(
            "SELECT inventory FROM objects WHERE record_key=?", (current_record_key,)
        ).fetchone()
        if current is None:
            return ""
        row = connection.execute(
            """SELECT o.record_key FROM objects o JOIN decisions d USING(record_key)
                WHERE d.decision_state='unresolved' AND o.record_key!=?
                  AND (lower(o.inventory)>lower(?)
                       OR (lower(o.inventory)=lower(?) AND o.record_key>?))
                ORDER BY lower(o.inventory), o.record_key LIMIT 1""",
            (current_record_key, current["inventory"], current["inventory"], current_record_key),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """SELECT o.record_key FROM objects o JOIN decisions d USING(record_key)
                    WHERE d.decision_state='unresolved' AND o.record_key!=?
                    ORDER BY lower(o.inventory), o.record_key LIMIT 1""",
                (current_record_key,),
            ).fetchone()
    return row["record_key"] if row else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="vladimir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bulk-select-unique-tiff", action="store_true")
    parser.add_argument("--link-65080-components", action="store_true")
    parser.add_argument("--link-60243-components", action="store_true")
    parser.add_argument("--repair-linked-derivatives", action="store_true")
    parser.add_argument("--resolve-catalogue-surrogates", action="store_true")
    parser.add_argument("--apply-metadata-editorial-resolution", action="store_true")
    parser.add_argument("--complete-metadata-editorial-image-name", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise WorkbenchError("workbench may listen only on localhost")
    if not args.actor.strip():
        raise WorkbenchError("actor must not be empty")
    created = initialize_database()
    if args.bulk_select_unique_tiff:
        print(json.dumps(bulk_select_unique_tiffs(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.link_65080_components:
        print(json.dumps(link_65080_components(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.link_60243_components:
        print(json.dumps(link_60243_components(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.repair_linked_derivatives:
        print(json.dumps(repair_linked_derivatives(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.resolve_catalogue_surrogates:
        print(json.dumps(resolve_catalogue_surrogates(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.apply_metadata_editorial_resolution:
        print(json.dumps(apply_metadata_editorial_resolution(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.complete_metadata_editorial_image_name:
        print(json.dumps(complete_metadata_editorial_image_name(args.actor.strip()), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.check:
        print(json.dumps(check_state(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    payload = write_preflight(args.actor.strip(), args.host, args.port, created)
    server = WorkbenchServer((args.host, args.port), args.actor.strip())
    url = payload["url"]
    print(f"WF-Alben workbench {VERSION}: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (WorkbenchError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
