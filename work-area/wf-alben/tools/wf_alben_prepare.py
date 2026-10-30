#!/usr/bin/env python3
"""Build the review-only WF-Alben reconciliation projection.

The tool reads the immutable museum-digital capture, the whole-USB visual
index, the document index, the older archive catalogue index, and published
unit catalogues.  It writes only human-readable review material below the
approved wf-alben work root.  It never selects a Master, changes a source,
creates an accession, or authorizes a drain/deletion action.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.1"
SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_SOURCE = SOURCE_ROOT / "restore/archive"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-alben")
OUTPUT_ROOT = WORK_ROOT / "work/reconciliation"
CAPTURE_RUN = Path(
    "/mnt/volume/iss-archive-work/collection-607/work/reconciliation/"
    "museum-digital/runs/20260822T111320Z-collection-607"
)
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
DOCUMENT_INDEX = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
PUBLISHED_UNITS = Path("/mnt/volume/iss-archive/units")
LOCAL_CROSSWALK = ARCHIVE_SOURCE / (
    "WF/WF-Sender Projekt/Webseite/Tabellenbäckerei-ALBEN/"
    "mdExcel_Alben_05_completeCaption.csv"
)
IMAGE_EXTENSIONS = {"jpg", "jpeg", "tif", "tiff"}
TIFF_EXTENSIONS = {"tif", "tiff"}
DOCUMENT_QUERY = '"ALB 001" OR "ALB 002" OR "ALB 003" OR "Album NEF"'

OUTPUT_FILES = {
    "collection": OUTPUT_ROOT / "collection-records.csv",
    "candidates": OUTPUT_ROOT / "candidate-files.csv",
    "queue": OUTPUT_ROOT / "review-queue.csv",
    "metadata": OUTPUT_ROOT / "metadata-crosswalk.csv",
    "documents": OUTPUT_ROOT / "document-candidates.csv",
    "archive_index": OUTPUT_ROOT / "archive-index-comparison.csv",
    "published": OUTPUT_ROOT / "published-unit-collisions.csv",
    "summary": OUTPUT_ROOT / "summary.json",
    "readme": WORK_ROOT / "README.md",
}


class PrepareError(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_inventory(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise PrepareError("Elements did not resolve to exactly one filesystem")
    mount = filesystems[0]
    options = set(clean(mount.get("options")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements read-only mount guard failed: {mount}")
    return mount


def ro_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PrepareError(f"required SQLite index missing: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    violations = connection.execute("PRAGMA foreign_key_check").fetchmany(1)
    if violations:
        connection.close()
        raise PrepareError(f"foreign-key violation in {path}: {violations[0]}")
    return connection


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


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_capture() -> tuple[list[dict[str, str]], dict[str, Any]]:
    objects_path = CAPTURE_RUN / "objects.csv"
    coverage_path = CAPTURE_RUN / "coverage.json"
    if not objects_path.is_file() or not coverage_path.is_file():
        raise PrepareError(f"completed museum-digital capture missing: {CAPTURE_RUN}")
    with coverage_path.open(encoding="utf-8") as handle:
        coverage = json.load(handle)
    with objects_path.open(encoding="utf-8", newline="") as handle:
        objects = list(csv.DictReader(handle))
    if coverage.get("query") != "collection:607":
        raise PrepareError(f"wrong museum-digital query: {coverage.get('query')}")
    expected = int(coverage.get("reported_total") or 0)
    if expected != 178 or len(objects) != expected or int(coverage.get("captured_objects") or 0) != expected:
        raise PrepareError("museum-digital capture is not the expected complete 178-object collection")
    if int(coverage.get("request_failures") or 0):
        raise PrepareError("museum-digital capture contains request failures")
    if len({row["object_id"] for row in objects}) != len(objects):
        raise PrepareError("duplicate museum-digital object IDs")
    if len({normalize_inventory(row["inventory"]) for row in objects}) != len(objects):
        raise PrepareError("duplicate normalized museum-digital inventories")
    return objects, coverage


def collection_rows(objects: list[dict[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in sorted(objects, key=lambda item: (item["inventory"].casefold(), int(item["object_id"]))):
        rows.append(
            {
                "record_key": f"md:{obj['object_id']}",
                "md_object_id": obj["object_id"],
                "inventory": obj["inventory"],
                "title": obj["title"],
                "object_type": obj["object_type"],
                "date_text": obj["date"],
                "collection": obj["collections"],
                "institution_id": obj["institution_id"],
                "institution_name": obj["institution_name"],
                "md_image_count": obj["image_count"],
                "md_last_updated": obj["last_updated"],
                "metadata_rights": obj["metadata_rights"],
                "md_url": obj["object_url"],
                "metadata_authority": "current museum-digital collection 607 capture",
                "master_decision_state": "unresolved",
                "selected_master_path": "",
                "deletion_authorized": "false",
            }
        )
    return rows


def load_visual_assets() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    with ro_connection(VISUAL_INDEX) as connection:
        root = connection.execute(
            "SELECT * FROM visual_roots WHERE root_path = ?", (str(SOURCE_ROOT),)
        ).fetchone()
        if root is None:
            raise PrepareError("whole-USB visual index has no Elements root")
        run = connection.execute(
            """SELECT * FROM index_runs
               WHERE roots_json LIKE ? AND status = 'complete'
               ORDER BY completed_at DESC LIMIT 1""",
            (f"%{SOURCE_ROOT}%",),
        ).fetchone()
        if run is None:
            raise PrepareError("whole-USB visual index has no complete Elements run")
        sql = """
            SELECT va.*,
                   MAX(CASE WHEN fe.name = 'phash-dct' THEN vf.feature_hex ELSE '' END) AS phash,
                   MAX(CASE WHEN fe.name = 'dhash' THEN vf.feature_hex ELSE '' END) AS dhash,
                   MAX(CASE WHEN fe.name = 'ahash' THEN vf.feature_hex ELSE '' END) AS ahash
              FROM visual_assets va
              JOIN visual_roots vr ON vr.root_id = va.root_id
              LEFT JOIN visual_features vf ON vf.asset_id = va.asset_id
              LEFT JOIN feature_extractors fe ON fe.extractor_id = vf.extractor_id
             WHERE vr.root_path = ?
               AND lower(va.extension) IN ('jpg', 'jpeg', 'tif', 'tiff')
               AND (lower(va.original_filename) LIKE '%alb%'
                    OR lower(va.original_filename) LIKE '%nef%')
             GROUP BY va.asset_id
             ORDER BY va.relative_path
        """
        assets = [dict(row) for row in connection.execute(sql, (str(SOURCE_ROOT),))]
        meta = dict(connection.execute("SELECT key, value FROM visual_index_meta"))
        run_info = dict(run)
    return assets, meta, run_info


SPECIAL_STEMS = {
    "ALB-001-50": {"alb00150v"},
    "ALB_002": {"alb002cover", "alb002titel", "alb002titel2"},
    "ALB_003": {"alb003covergross", "alb003titel"},
    "ALB_003-Inhalt": {"alb003inhaltsverzeichnis"},
}


def candidate_projection(
    objects: list[dict[str, str]], assets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    objects_by_inventory = {normalize_inventory(row["inventory"]): row for row in objects}
    special_by_stem: dict[str, str] = {}
    for inventory, stems in SPECIAL_STEMS.items():
        for stem in stems:
            special_by_stem[stem] = inventory

    assigned: dict[str, dict[str, tuple[dict[str, Any], str, str]]] = defaultdict(dict)
    for asset in assets:
        stem = normalize_inventory(Path(asset["original_filename"]).stem)
        obj = objects_by_inventory.get(stem)
        relation = "exact_inventory_filename"
        basis = "normalized filename stem equals current museum-digital inventory"
        if obj is None and stem in special_by_stem:
            inventory = special_by_stem[stem]
            obj = objects_by_inventory[normalize_inventory(inventory)]
            relation = "related_component_filename"
            basis = f"explicit component-name relationship for compound or alternate-name record {inventory}"
        if obj is None:
            continue
        assigned[obj["object_id"]][asset["relative_path"]] = (asset, relation, basis)

    candidate_rows: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    stat_checked = 0
    for obj in sorted(objects, key=lambda item: (item["inventory"].casefold(), int(item["object_id"]))):
        candidates = list(assigned.get(obj["object_id"], {}).values())
        candidates.sort(key=lambda item: item[0]["relative_path"].casefold())
        readable_tiffs = [
            item
            for item in candidates
            if item[0]["extension"].casefold() in TIFF_EXTENSIONS
            and item[0]["status"] == "indexed"
            and item[0]["width_px"]
            and item[0]["height_px"]
        ]
        exact_readable_tiffs = [item for item in readable_tiffs if item[1] == "exact_inventory_filename"]
        inventory = obj["inventory"]
        if int(obj["image_count"] or 0) > 1:
            lane = "review_compound_multi_image"
            note = "museum-digital has multiple images; review all local components before one Master decision"
        elif inventory == "ALB_003-Inhalt":
            lane = "review_alternate_filename"
            note = "local contents candidates use Inhaltsverzeichnis rather than the current inventory suffix"
        elif inventory == "ALB_003-3":
            lane = "unresolved_no_local_candidate"
            note = "no exact or approved alternate-name local candidate found in the whole-USB visual index"
        elif inventory == "Album_NEF_Seite_07":
            lane = "review_two_tiff_candidates"
            note = "two readable TIFF candidates require visual human selection"
        elif len(exact_readable_tiffs) == 1:
            lane = "suggest_unique_tiff"
            note = "one readable exact-name TIFF is suggested for human review; it is not selected"
        else:
            lane = "review_jpeg_or_album_page_relation"
            note = "no readable exact-name TIFF; review page, JPEG, and related-version evidence"

        suggested_path = ""
        if lane == "suggest_unique_tiff":
            suggested_path = str(SOURCE_ROOT / exact_readable_tiffs[0][0]["relative_path"])

        for asset, relation, basis in candidates:
            source_path = SOURCE_ROOT / asset["relative_path"]
            try:
                stat = source_path.stat()
            except OSError as exc:
                raise PrepareError(f"indexed candidate is not readable in place: {source_path}: {exc}") from exc
            if stat.st_size != int(asset["size_bytes"]) or stat.st_mtime_ns != int(asset["mtime_ns"]):
                raise PrepareError(f"indexed candidate changed since indexing: {source_path}")
            stat_checked += 1
            is_suggestion = str(source_path) == suggested_path
            candidate_rows.append(
                {
                    "record_key": f"md:{obj['object_id']}",
                    "md_object_id": obj["object_id"],
                    "inventory": inventory,
                    "title": obj["title"],
                    "candidate_id": asset["asset_id"],
                    "candidate_source_path": str(source_path),
                    "candidate_relative_path": asset["relative_path"],
                    "filename": asset["original_filename"],
                    "extension": asset["extension"],
                    "size_bytes": asset["size_bytes"],
                    "mtime_ns": asset["mtime_ns"],
                    "width_px": asset["width_px"] or "",
                    "height_px": asset["height_px"] or "",
                    "visual_index_state": asset["status"],
                    "visual_index_error": asset["error"],
                    "phash": asset["phash"],
                    "dhash": asset["dhash"],
                    "ahash": asset["ahash"],
                    "candidate_relation": relation,
                    "relation_basis": basis,
                    "suggested_master": "yes" if is_suggestion else "no",
                    "suggestion_rank": "1" if is_suggestion else "",
                    "selected_master": "false",
                    "redundant_decision": "undecided",
                    "drain_list_state": "not_eligible_before_accession",
                    "deletion_authorized": "false",
                }
            )

        queue_rows.append(
            {
                "record_key": f"md:{obj['object_id']}",
                "md_object_id": obj["object_id"],
                "inventory": inventory,
                "title": obj["title"],
                "object_type": obj["object_type"],
                "date_text": obj["date"],
                "md_url": obj["object_url"],
                "md_image_count": obj["image_count"],
                "candidate_count": len(candidates),
                "readable_tiff_candidate_count": len(readable_tiffs),
                "exact_readable_tiff_candidate_count": len(exact_readable_tiffs),
                "queue_lane": lane,
                "review_note": note,
                "suggested_master_path": suggested_path,
                "master_decision_state": "unresolved",
                "selected_master_path": "",
                "redundant_decisions_complete": "false",
                "drain_list_state": "not_eligible_before_accession",
                "deletion_authorized": "false",
            }
        )

    metrics = {
        "family_assets_considered": len(assets),
        "candidate_rows": len(candidate_rows),
        "objects_with_candidates": sum(int(row["candidate_count"]) > 0 for row in queue_rows),
        "objects_without_candidates": sum(int(row["candidate_count"]) == 0 for row in queue_rows),
        "candidate_source_stats_verified": stat_checked,
        "queue_lanes": dict(sorted(Counter(row["queue_lane"] for row in queue_rows).items())),
        "suggested_master_rows": sum(row["suggested_master"] == "yes" for row in candidate_rows),
    }
    return candidate_rows, queue_rows, metrics


def metadata_crosswalk(objects: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not LOCAL_CROSSWALK.is_file():
        raise PrepareError(f"local metadata crosswalk missing: {LOCAL_CROSSWALK}")
    with LOCAL_CROSSWALK.open(encoding="utf-8-sig", newline="") as handle:
        local_rows = list(csv.DictReader(handle, delimiter=";"))
    local_by_inventory: dict[str, dict[str, str]] = {}
    for row in local_rows:
        key = normalize_inventory(row.get("Inventarnummer"))
        if not key or key in local_by_inventory:
            raise PrepareError(f"duplicate or empty inventory in local crosswalk: {row.get('Inventarnummer')}")
        local_by_inventory[key] = row

    rows: list[dict[str, Any]] = []
    for obj in sorted(objects, key=lambda item: (item["inventory"].casefold(), int(item["object_id"]))):
        local = local_by_inventory.get(normalize_inventory(obj["inventory"]))
        if local is None:
            comparison = "missing_local_crosswalk_row"
            reconciliation = "current_md_only_no_local_crosswalk"
            title_match = ""
            id_match = ""
        else:
            title_match = "yes" if clean(local.get("TitelSource")) == clean(obj["title"]) else "no"
            id_match = "yes" if clean(local.get("md:number")) == obj["object_id"] else "no"
            if id_match != "yes":
                raise PrepareError(f"local crosswalk MD ID mismatch for {obj['inventory']}")
            comparison = "exact_id_and_title" if title_match == "yes" else "current_md_title_differs"
            reconciliation = (
                "current_md_matches_local_crosswalk"
                if title_match == "yes"
                else "current_md_supersedes_historical_local"
            )
        rows.append(
            {
                "record_key": f"md:{obj['object_id']}",
                "md_object_id": obj["object_id"],
                "inventory": obj["inventory"],
                "current_md_title": obj["title"],
                "current_md_date": obj["date"],
                "current_md_object_type": obj["object_type"],
                "current_md_last_updated": obj["last_updated"],
                "local_inventory": local.get("Inventarnummer", "") if local else "",
                "local_file_source": local.get("FileSource", "") if local else "",
                "local_title": local.get("TitelSource", "") if local else "",
                "local_long_text": local.get("Langtext", "") if local else "",
                "local_complete_caption": local.get("CAPTIONSOURCEcomplete", "") if local else "",
                "local_keywords": local.get("Schlagwort", "") if local else "",
                "local_md_object_id": local.get("md:number", "") if local else "",
                "md_id_match": id_match,
                "title_match": title_match,
                "comparison_state": comparison,
                "metadata_reconciliation_state": reconciliation,
                "metadata_authority": "current museum-digital collection 607 capture",
                "local_evidence_role": "historical provenance crosswalk only",
                "metadata_reconciled": "yes",
                "deletion_authorized": "false",
            }
        )
    metrics = {
        "source_path": str(LOCAL_CROSSWALK),
        "local_rows": len(local_rows),
        "matched_rows": sum(bool(row["local_inventory"]) for row in rows),
        "missing_local_rows": sum(not bool(row["local_inventory"]) for row in rows),
        "exact_id_and_title": sum(row["comparison_state"] == "exact_id_and_title" for row in rows),
        "current_md_title_differs": sum(
            row["comparison_state"] == "current_md_title_differs" for row in rows
        ),
        "metadata_reconciled_yes": sum(row["metadata_reconciled"] == "yes" for row in rows),
    }
    return rows, metrics


def document_role(source_path: str) -> str:
    folded = source_path.casefold()
    if "/webseite/backup/" in folded:
        return "website_backup_context"
    if "tabellenbäckerei-alben" in folded or "/tabellen-baeckerei/alben/" in folded:
        return "album_working_table"
    if "/thesaurus/" in folded:
        return "thesaurus_context"
    if "/buchprojekt" in folded:
        return "book_project_context"
    return "other_context"


def inventory_fts_query(inventory: str) -> str:
    terms = re.findall(r"[A-Za-z0-9]+", inventory)
    return '"' + " ".join(terms).replace('"', '""') + '"'


def document_projection(objects: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with ro_connection(DOCUMENT_INDEX) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        last_scan_row = connection.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if last_scan_row is None or last_scan_row["status"] != "complete" or int(last_scan_row["errors"]):
            raise PrepareError("document index has no clean completed scan")
        if meta.get("extraction_status") != "complete":
            raise PrepareError("document index extraction is not complete")
        query = """
            SELECT d.*
              FROM document_fts f
              JOIN documents d ON d.document_id = f.document_id
             WHERE document_fts MATCH ?
             ORDER BY d.source_path
        """
        documents = [dict(row) for row in connection.execute(query, (DOCUMENT_QUERY,))]
        document_ids = {row["document_id"] for row in documents}
        coverage: Counter[str] = Counter()
        for obj in objects:
            ids = {
                row[0]
                for row in connection.execute(
                    "SELECT document_id FROM document_fts WHERE document_fts MATCH ?",
                    (inventory_fts_query(obj["inventory"]),),
                )
            }
            for document_id in ids & document_ids:
                coverage[document_id] += 1
        duplicate_counts = Counter(row["content_sha256"] for row in documents if row["content_sha256"])
        links: dict[str, list[str]] = defaultdict(list)
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            sql = f"SELECT * FROM unit_links WHERE document_id IN ({placeholders}) ORDER BY unit"
            for link in connection.execute(sql, tuple(sorted(document_ids))):
                links[link["document_id"]].append(
                    f"{link['unit']}|{link['relation_state']}|{link['basis']}"
                )

    rows: list[dict[str, Any]] = []
    for document in documents:
        role = document_role(document["source_path"])
        proposed = (
            "review_as_primary_metadata_provenance"
            if document["source_path"] == str(LOCAL_CROSSWALK)
            else "review_as_competing_metadata_evidence"
            if role == "album_working_table"
            else "context_only"
        )
        rows.append(
            {
                "document_id": document["document_id"],
                "source_path": document["source_path"],
                "relative_path": document["relative_path"],
                "filename": document["filename"],
                "extension": document["extension"],
                "size_bytes": document["size_bytes"],
                "mtime_ns": document["mtime_ns"],
                "content_sha256": document["content_sha256"],
                "observed_state": document["observed_state"],
                "extraction_state": document["extraction_state"],
                "parser": document["parser"],
                "extraction_error": document["extraction_error"],
                "document_role": role,
                "inventory_coverage_count": coverage[document["document_id"]],
                "same_content_occurrence_count": duplicate_counts[document["content_sha256"]],
                "existing_unit_links": "; ".join(links.get(document["document_id"], [])),
                "proposed_review_state": proposed,
                "metadata_authority": "none; documentary evidence only",
                "drain_list_state": "not_eligible_before_accession",
                "deletion_authorized": "false",
            }
        )
    metrics = {
        "query": DOCUMENT_QUERY,
        "documents": len(rows),
        "unique_content_hashes": len({row["content_sha256"] for row in rows if row["content_sha256"]}),
        "duplicate_content_groups": sum(count > 1 for count in duplicate_counts.values()),
        "duplicate_occurrences": sum(count for count in duplicate_counts.values() if count > 1),
        "roles": dict(sorted(Counter(row["document_role"] for row in rows).items())),
        "objects_with_document_hits": 0,
        "index_meta": meta,
        "last_scan": dict(last_scan_row),
    }
    # Coverage was computed only across the 64 bounded documents.
    object_hit_count = 0
    with ro_connection(DOCUMENT_INDEX) as connection:
        for obj in objects:
            hit = connection.execute(
                "SELECT 1 FROM document_fts WHERE document_fts MATCH ? LIMIT 1",
                (inventory_fts_query(obj["inventory"]),),
            ).fetchone()
            object_hit_count += int(hit is not None)
    metrics["objects_with_document_hits"] = object_hit_count
    return rows, metrics


def archive_index_projection(objects: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inventories = {normalize_inventory(row["inventory"]): row for row in objects}
    ids = {row["object_id"]: row for row in objects}
    counts: dict[str, Counter[str]] = {row["object_id"]: Counter() for row in objects}
    inventory_tables = ["objects", "object_sources", "file_object_links", "asset_refs", "provenance_until_md"]
    with ro_connection(ARCHIVE_INDEX) as connection:
        for table in inventory_tables:
            for row in connection.execute(f"SELECT inventory_number FROM {table}"):
                obj = inventories.get(normalize_inventory(row[0]))
                if obj:
                    counts[obj["object_id"]][table] += 1
        for row in connection.execute("SELECT md_id, inventory_number FROM online_md_objects"):
            obj = ids.get(clean(row[0])) or inventories.get(normalize_inventory(row[1]))
            if obj:
                counts[obj["object_id"]]["online_md_objects"] += 1
        for row in connection.execute("SELECT stem FROM files"):
            obj = inventories.get(normalize_inventory(row[0]))
            if obj:
                counts[obj["object_id"]]["exact_normalized_file_stem"] += 1
        run = connection.execute("SELECT * FROM run_log ORDER BY started_at DESC LIMIT 1").fetchone()
    rows: list[dict[str, Any]] = []
    for obj in sorted(objects, key=lambda item: (item["inventory"].casefold(), int(item["object_id"]))):
        counter = counts[obj["object_id"]]
        rows.append(
            {
                "record_key": f"md:{obj['object_id']}",
                "md_object_id": obj["object_id"],
                "inventory": obj["inventory"],
                "objects_rows": counter["objects"],
                "object_source_rows": counter["object_sources"],
                "file_object_link_rows": counter["file_object_links"],
                "asset_ref_rows": counter["asset_refs"],
                "provenance_rows": counter["provenance_until_md"],
                "online_md_object_rows": counter["online_md_objects"],
                "exact_normalized_file_stem_rows": counter["exact_normalized_file_stem"],
                "comparison_state": (
                    "historical_catalogue_or_provenance_match"
                    if any(counter[field] for field in (
                        "objects", "object_sources", "file_object_links", "asset_refs",
                        "provenance_until_md", "online_md_objects"
                    ))
                    else "historical_index_file_stem_only"
                    if counter["exact_normalized_file_stem"]
                    else "not_in_historical_index"
                ),
                "interpretation": "historical index evidence only; absence does not prove absence from USB",
            }
        )
    catalogue_fields = (
        "objects", "object_sources", "file_object_links", "asset_refs",
        "provenance_until_md", "online_md_objects"
    )
    metrics = {
        "index_path": str(ARCHIVE_INDEX),
        "last_run": dict(run) if run else {},
        "objects_with_catalogue_or_provenance_match": sum(
            any(counts[row["object_id"]][field] for field in catalogue_fields) for row in objects
        ),
        "catalogue_or_provenance_matching_rows": sum(
            sum(counts[row["object_id"]][field] for field in catalogue_fields) for row in objects
        ),
        "objects_with_exact_normalized_file_stem_match": sum(
            bool(counts[row["object_id"]]["exact_normalized_file_stem"]) for row in objects
        ),
        "exact_normalized_file_stem_matching_rows": sum(
            counts[row["object_id"]]["exact_normalized_file_stem"] for row in objects
        ),
    }
    return rows, metrics


def published_collisions(objects: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inventories = {normalize_inventory(row["inventory"]): row for row in objects}
    ids = {row["object_id"]: row for row in objects}
    collisions: list[dict[str, Any]] = []
    catalogues = sorted(PUBLISHED_UNITS.glob("*/CATALOG.csv"))
    for catalogue in catalogues:
        unit = catalogue.parent.name
        with catalogue.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for line_number, row in enumerate(reader, 2):
                matched: dict[str, tuple[dict[str, str], str]] = {}
                for field in ("inventory", "object_current_inventory", "object_historical_inventory"):
                    value = clean(row.get(field))
                    obj = inventories.get(normalize_inventory(value)) if value else None
                    if obj:
                        matched[obj["object_id"]] = (obj, f"exact normalized {field}")
                for field in ("md_object_id", "museum_digital_object_id", "object_md_object_id"):
                    value = clean(row.get(field))
                    obj = ids.get(value)
                    if obj:
                        matched[obj["object_id"]] = (obj, f"exact {field}")
                for value in re.findall(r"\d+", clean(row.get("md_object_ids"))):
                    obj = ids.get(value)
                    if obj:
                        matched[obj["object_id"]] = (obj, "exact token in md_object_ids")
                for obj, basis in matched.values():
                    collisions.append(
                        {
                            "record_key": f"md:{obj['object_id']}",
                            "md_object_id": obj["object_id"],
                            "inventory": obj["inventory"],
                            "published_unit": unit,
                            "catalogue_path": str(catalogue),
                            "catalogue_line": line_number,
                            "match_basis": basis,
                            "review_state": "published_collision_requires_review",
                        }
                    )
    return collisions, {"catalogues_checked": len(catalogues), "collisions": len(collisions)}


def validate(
    objects: list[dict[str, str]], candidates: list[dict[str, Any]], queue: list[dict[str, Any]],
    metadata: list[dict[str, Any]], archive_rows: list[dict[str, Any]], collisions: list[dict[str, Any]],
) -> None:
    if not (len(objects) == len(queue) == len(metadata) == len(archive_rows) == 178):
        raise PrepareError("object-level projections do not all contain 178 rows")
    expected_lanes = {
        "suggest_unique_tiff": 126,
        "review_two_tiff_candidates": 1,
        "review_jpeg_or_album_page_relation": 45,
        "review_compound_multi_image": 4,
        "review_alternate_filename": 1,
        "unresolved_no_local_candidate": 1,
    }
    actual_lanes = dict(Counter(row["queue_lane"] for row in queue))
    if actual_lanes != expected_lanes:
        raise PrepareError(f"unexpected queue distribution: {actual_lanes}")
    if any(row["selected_master"] != "false" for row in candidates):
        raise PrepareError("candidate projection selected a Master")
    if any(row["master_decision_state"] != "unresolved" for row in queue):
        raise PrepareError("review queue contains a resolved Master decision")
    if any(row["deletion_authorized"] != "false" for row in candidates + queue + metadata):
        raise PrepareError("projection contains deletion authority")
    if len({(row["record_key"], row["candidate_source_path"]) for row in candidates}) != len(candidates):
        raise PrepareError("duplicate candidate path within an object")
    if collisions:
        raise PrepareError(f"published-unit collisions require review before proceeding: {len(collisions)}")


def main() -> int:
    if WORK_ROOT.resolve() != Path("/mnt/volume/iss-archive-work/wf-alben"):
        raise PrepareError("unexpected work root")
    if not OUTPUT_ROOT.is_dir() or not (WORK_ROOT / "tools").is_dir():
        raise PrepareError("approved work directory skeleton is missing")
    existing = [str(path) for path in OUTPUT_FILES.values() if path.exists()]
    if existing:
        raise PrepareError(f"refusing to overwrite existing projection outputs: {existing}")

    created_at = now_utc()
    mount = source_mount()
    objects, coverage = load_capture()
    collection = collection_rows(objects)
    assets, visual_meta, visual_run = load_visual_assets()
    candidates, queue, candidate_metrics = candidate_projection(objects, assets)
    metadata, metadata_metrics = metadata_crosswalk(objects)
    documents, document_metrics = document_projection(objects)
    archive_rows, archive_metrics = archive_index_projection(objects)
    collisions, collision_metrics = published_collisions(objects)
    validate(objects, candidates, queue, metadata, archive_rows, collisions)

    summary = {
        "format": "iss-wf-alben-review-projection-v1",
        "tool_version": VERSION,
        "created_at": created_at,
        "unit": "wf-alben",
        "state": "ready_for_human_master_review",
        "scope": "review-only reconciliation projection; no accession or source mutation",
        "museum_digital": {
            "collection_id": "607",
            "collection_title": "WF-Alben",
            "capture_run": str(CAPTURE_RUN),
            "objects": len(objects),
            "coverage": coverage,
        },
        "source_guard": {
            "root": str(SOURCE_ROOT),
            "mount": mount,
            "source_files_modified": 0,
        },
        "visual_index": {
            "path": str(VISUAL_INDEX),
            "meta": visual_meta,
            "latest_complete_usb_run": visual_run,
            **candidate_metrics,
        },
        "metadata_crosswalk": metadata_metrics,
        "document_index": {"path": str(DOCUMENT_INDEX), **document_metrics},
        "historical_archive_index": archive_metrics,
        "published_unit_check": collision_metrics,
        "decision_guards": {
            "selected_master_rows": 0,
            "resolved_master_decisions": 0,
            "redundant_decisions_complete": 0,
            "drain_list_entries_created": 0,
            "deletion_authorized_true": 0,
            "candidate_content_hashes_computed_by_this_run": 0,
            "published_unit_created": False,
            "sqlite_database_created": False,
            "service_created_or_started": False,
        },
        "outputs": {name: str(path) for name, path in OUTPUT_FILES.items()},
    }

    atomic_csv(OUTPUT_FILES["collection"], collection[0].keys(), collection)
    atomic_csv(OUTPUT_FILES["candidates"], candidates[0].keys(), candidates)
    atomic_csv(OUTPUT_FILES["queue"], queue[0].keys(), queue)
    atomic_csv(OUTPUT_FILES["metadata"], metadata[0].keys(), metadata)
    atomic_csv(OUTPUT_FILES["documents"], documents[0].keys(), documents)
    atomic_csv(OUTPUT_FILES["archive_index"], archive_rows[0].keys(), archive_rows)
    published_fields = (
        "record_key", "md_object_id", "inventory", "published_unit", "catalogue_path",
        "catalogue_line", "match_basis", "review_state"
    )
    atomic_csv(OUTPUT_FILES["published"], published_fields, collisions)
    atomic_json(OUTPUT_FILES["summary"], summary)

    readme = "# WF-Alben reconciliation work\n\n"
    readme += f"Created: `{created_at}`\n\n"
    readme += "This is a review-only projection for museum-digital collection 607 (`WF-Alben`). "
    readme += "It contains 178 unresolved archival objects. It is not an accession and it does not "
    readme += "select Masters, mark candidates redundant, add source files to a drain list, or authorize deletion.\n\n"
    readme += "## Review order\n\n"
    readme += "1. Use `work/reconciliation/review-queue.csv` as the object list.\n"
    readme += "2. Inspect all rows for an object in `candidate-files.csv`; `suggested_master=yes` is evidence only.\n"
    readme += "3. Use `metadata-crosswalk.csv` for current museum-digital authority versus the historical local table.\n"
    readme += "4. Use `document-candidates.csv` to review competing working tables and contextual documents.\n"
    readme += "5. Do not create redundant or drain decisions until one Master has been explicitly selected and accessioned.\n\n"
    readme += "## Current queue\n\n"
    for lane, count in candidate_metrics["queue_lanes"].items():
        readme += f"- `{lane}`: {count}\n"
    readme += "\nCurrent metadata authority is the captured live museum-digital collection. "
    readme += "The local 173-row table is provenance evidence, not a competing authority.\n"
    atomic_text(OUTPUT_FILES["readme"], readme)

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PrepareError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        raise SystemExit(1)
