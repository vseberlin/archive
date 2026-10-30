#!/usr/bin/env python3
"""Reconcile the complete WF visual index against selected TFA MD masters.

This marks non-selected image copies and derivatives redundant, proposes those
source paths for drain, records exact local-to-MD duplicate decisions directed
by the user, and emits the remaining TFA-named TIFF candidates. It never
changes or deletes source files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WF_ROOT = SOURCE_ROOT / "restore/archive/WF"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
FULL_ROOT = WORK_ROOT / "work/reconciliation/wf-full-visual-index"
FULL_INDEX = FULL_ROOT / "wf-full-visual-index.sqlite"
FULL_COMPLETION = FULL_ROOT / "completions/afe749deaf74db6ae802721637125540.json"

SOURCE_CSV = REVIEW_ROOT / "source-disposition-proposal.csv"
DRAIN_CSV = REVIEW_ROOT / "drain-list-proposal.csv"
LOCAL_DECISIONS_CSV = REVIEW_ROOT / "local-object-decisions.csv"
LOCAL_DISPOSITIONS_CSV = REVIEW_ROOT / "local-object-candidate-dispositions.csv"
LOCAL_PREFLIGHT = REVIEW_ROOT / "local-object-workbench-preflight.json"
WORKBENCH_PREFLIGHT = REVIEW_ROOT / "workbench-preflight.json"

REDUNDANT_CSV = REVIEW_ROOT / "full-wf-md-redundant-images.csv"
GENUINE_CSV = REVIEW_ROOT / "genuine-new-local-tiff-candidates.csv"
NEAR_CSV = REVIEW_ROOT / "full-wf-tiff-near-md-review.csv"
RECEIPT = REVIEW_ROOT / "full-wf-md-redundancy-apply.json"

EXPECTED_BEFORE = {
    "objects": 5718,
    "candidates": 58217,
    "reconciliation_decisions": 5282,
    "source_disposition_proposal": 63374,
    "drain_list_proposal": 63374,
    "local_object_metadata": 436,
    "local_candidate_technical": 1261,
    "local_md_match_candidates": 583,
    "local_object_decisions": 2,
}
FULL_EXPECTED = {"files_seen": 291594, "images_seen": 65357, "indexed": 64806, "errors": 551}
IMAGE_EXTENSIONS = {"tif", "tiff", "jpg", "jpeg", "png", "webp", "psd"}
TIFF_EXTENSIONS = {"tif", "tiff"}
NEAR_MAX_PHASH = 14
NEAR_MAX_MEAN = 10.0
INVENTORY_RE = re.compile(r"(?i)(TFA(?:-(?:FOT|NEG|KB))?[-_ ]?[0-9][0-9A-Za-z._-]*)")

EXACT_ID_SQL = "(instr(x.match_basis,'exact_inventory') > 0 OR instr(x.match_basis,'live_historical_md_id') > 0)"
EXACT_SHARED_PATH_SQL = """EXISTS (
  SELECT 1 FROM candidates local_candidate
  JOIN candidates md_candidate
    ON md_candidate.record_key='md:'||x.md_object_id
   AND md_candidate.source_path=local_candidate.source_path
  WHERE local_candidate.record_key=x.record_key
)"""
EXACT_SELECTED_HASH_SQL = """EXISTS (
  SELECT 1 FROM candidates local_candidate
  JOIN local_candidate_technical technical ON technical.candidate_id=local_candidate.candidate_id
  JOIN reconciliation_decisions decision
    ON decision.record_key='md:'||x.md_object_id
   AND decision.decision_state='selected_master'
  JOIN candidates selected_master ON selected_master.candidate_id=decision.selected_candidate_id
  WHERE local_candidate.record_key=x.record_key
    AND technical.accession_eligible=1
    AND lower(local_candidate.extension) IN ('.tif','.tiff')
    AND local_candidate.reused_source_sha256!=''
    AND local_candidate.reused_source_sha256=selected_master.reused_source_sha256
)"""
SELECTED_MD_TIFF_SQL = """EXISTS (
  SELECT 1 FROM reconciliation_decisions selected_decision
  JOIN candidates selected_master
    ON selected_master.candidate_id=selected_decision.selected_candidate_id
  WHERE selected_decision.record_key='md:'||x.md_object_id
    AND selected_decision.decision_state='selected_master'
    AND lower(selected_master.extension) IN ('.tif','.tiff')
)"""
EXACT_EXISTING_MD_SQL = (
    f"(({EXACT_ID_SQL} OR {EXACT_SHARED_PATH_SQL} OR {EXACT_SELECTED_HASH_SQL}) "
    f"AND {SELECTED_MD_TIFF_SQL})"
)


class ReconcileError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_inventory(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"\.(?:TIF|TIFF|JPE?G|PSD|PNG)$", "", text)
    text = text.replace("_", "-").replace(" ", "")
    text = re.sub(r"^TFA-(?:FOT|NEG|KB)-?", "TFA-", text)
    text = re.sub(r"^TFA(?=\d)", "TFA-", text)
    return text.rstrip(".-")


def inventory_from_name(value: str) -> str:
    match = INVENTORY_RE.search(value)
    return normalize_inventory(match.group(1)) if match else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise ReconcileError("Elements did not resolve to one mounted filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options or "rw" in options:
        raise ReconcileError(f"Elements read-only mount guard failed: {row}")
    return row


def workbench_is_listening() -> bool:
    with socket.socket() as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", 8768)) == 0


def assert_work_path(path: Path) -> None:
    try:
        path.resolve().relative_to(WORK_ROOT.resolve())
    except ValueError as error:
        raise ReconcileError(f"write escaped approved TFA work root: {path}") from error


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
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
    assert_work_path(path)
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


def backup(path: Path, stamp: str) -> str:
    assert_work_path(path)
    if not path.exists():
        return ""
    destination = path.with_name(f"{path.stem}.pre-full-wf-md-redundancy-{stamp}{path.suffix}")
    if destination.exists():
        raise ReconcileError(f"backup already exists: {destination}")
    shutil.copy2(path, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return str(destination)


def validate_full_index(connection: sqlite3.Connection) -> dict[str, Any]:
    completion = json.loads(FULL_COMPLETION.read_text(encoding="utf-8"))
    if completion.get("state") != "complete" or completion.get("source_roots_unchanged") is not True:
        raise ReconcileError("full-WF completion receipt is incomplete")
    for key, expected in FULL_EXPECTED.items():
        if int(completion.get(key, -1)) != expected:
            raise ReconcileError(f"full-WF completion count drifted: {key}")
    if completion.get("database_sha256") != sha256_file(FULL_INDEX):
        raise ReconcileError("full-WF index fingerprint drifted")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ReconcileError("full-WF index integrity failed")
    return completion


def full_assets(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT a.relative_path,a.original_filename,lower(a.extension) AS extension,
               a.size_bytes,a.mtime_ns,a.width_px,a.height_px,
          MAX(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex END) AS ahash,
          MAX(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex END) AS dhash,
          MAX(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex END) AS phash
        FROM visual_assets a
        LEFT JOIN visual_features f ON f.asset_id=a.asset_id
        WHERE a.status='indexed'
        GROUP BY a.asset_id
        ORDER BY a.relative_path
        """
    )
    result = []
    for row in rows:
        item = dict(row)
        item["source_path"] = str(WF_ROOT / str(item["relative_path"]))
        item["inventory"] = inventory_from_name(str(item["original_filename"]))
        result.append(item)
    return result


def exact_local_bindings(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        f"""
        WITH exact_pairs AS (
          SELECT x.record_key,x.md_object_id
          FROM local_md_match_candidates x
          WHERE {EXACT_EXISTING_MD_SQL}
        )
        SELECT record_key,MIN(md_object_id) AS md_object_id
        FROM exact_pairs
        GROUP BY record_key
        HAVING COUNT(*)=1
        ORDER BY record_key
        """
    )
    return {str(row["record_key"]): str(row["md_object_id"]) for row in rows}


def add_evidence(
    path: str,
    md_ids: Iterable[str],
    basis: str,
    *,
    redundant_ids: dict[str, set[str]],
    redundant_basis: dict[str, set[str]],
) -> None:
    redundant_ids[path].update(str(value) for value in md_ids)
    redundant_basis[path].add(basis)


def projection(queue: sqlite3.Connection, index: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        table: int(queue.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in EXPECTED_BEFORE
    }
    if counts != EXPECTED_BEFORE:
        raise ReconcileError(f"queue scope drifted: {counts}")
    if queue.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ReconcileError("queue SQLite quick_check failed")
    if queue.execute("PRAGMA foreign_key_check").fetchall():
        raise ReconcileError("queue SQLite foreign-key check failed")

    selected_by_id: dict[str, dict[str, Any]] = {}
    selected_paths: dict[str, set[str]] = defaultdict(set)
    inventory_to_md: dict[str, set[str]] = defaultdict(set)
    md_metadata: dict[str, dict[str, Any]] = {}
    for row in queue.execute(
        """
        SELECT o.md_object_id,o.inventory,o.title,d.selected_candidate_id,
               c.source_path,c.extension,c.size_bytes
        FROM objects o
        JOIN reconciliation_decisions d
          ON d.record_key=o.record_key AND d.decision_state='selected_master'
        JOIN candidates c ON c.candidate_id=d.selected_candidate_id
        WHERE o.record_state='current_museum_digital'
        ORDER BY CAST(o.md_object_id AS INTEGER)
        """
    ):
        item = dict(row)
        md_id = str(item["md_object_id"])
        if str(item["extension"]).casefold().lstrip(".") not in TIFF_EXTENSIONS:
            raise ReconcileError(f"selected MD Master is not TIFF: md:{md_id}")
        selected_by_id[md_id] = item
        selected_paths[str(item["source_path"])].add(md_id)
        inventory_to_md[normalize_inventory(item["inventory"])].add(md_id)
        md_metadata[md_id] = item
    if len(selected_by_id) != 5282:
        raise ReconcileError(f"selected MD Master scope drifted: {len(selected_by_id)}")

    md_hashes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    md_hash_rows = []
    for row in queue.execute(
        """
        SELECT o.md_object_id,h.ahash_64,h.dhash_64,h.phash_64
        FROM local_visual_hashes h
        JOIN candidates c ON c.candidate_id=h.candidate_id
        JOIN objects o ON o.record_key=c.record_key
        WHERE o.record_state='current_museum_digital'
          AND h.decode_status='ok' AND h.ahash_64!='' AND h.dhash_64!='' AND h.phash_64!=''
        """
    ):
        key = (str(row["ahash_64"]), str(row["dhash_64"]), str(row["phash_64"]))
        md_id = str(row["md_object_id"])
        md_hashes[key].add(md_id)
        md_hash_rows.append((int(key[0], 16), int(key[1], 16), int(key[2], 16), md_id))

    redundant_ids: dict[str, set[str]] = defaultdict(set)
    redundant_basis: dict[str, set[str]] = defaultdict(set)
    path_info: dict[str, dict[str, Any]] = {}
    current_md_paths: dict[str, set[str]] = defaultdict(set)
    for row in queue.execute(
        """
        SELECT c.source_path,c.extension,c.size_bytes,c.basename,c.inventory,o.md_object_id
        FROM candidates c JOIN objects o ON o.record_key=c.record_key
        WHERE o.record_state='current_museum_digital'
          AND lower(c.extension) IN ('.tif','.tiff','.jpg','.jpeg','.png','.webp','.psd')
        ORDER BY c.candidate_id
        """
    ):
        path = str(row["source_path"])
        current_md_paths[path].add(str(row["md_object_id"]))
        path_info.setdefault(path, dict(row))
    for path, md_ids in current_md_paths.items():
        if path not in selected_paths:
            add_evidence(
                path, md_ids, "reconciled_md_non_selected_image",
                redundant_ids=redundant_ids, redundant_basis=redundant_basis,
            )

    local_bindings = exact_local_bindings(queue)
    if len(local_bindings) != 293:
        raise ReconcileError(f"exact local-to-MD scope drifted: {len(local_bindings)}")
    existing_overlap = queue.execute(
        "SELECT COUNT(*) FROM local_object_decisions WHERE record_key IN (%s)"
        % ",".join("?" for _ in local_bindings),
        tuple(local_bindings),
    ).fetchone()[0]
    if int(existing_overlap) != 0:
        raise ReconcileError("an existing human local decision overlaps the exact MD exclusion scope")
    for row in queue.execute(
        """
        SELECT c.record_key,c.source_path,c.extension,c.size_bytes,c.basename,c.inventory
        FROM candidates c
        JOIN objects o ON o.record_key=c.record_key
        WHERE o.record_state='local_tfa_review'
          AND lower(c.extension) IN ('.tif','.tiff','.jpg','.jpeg','.png','.webp','.psd')
        ORDER BY c.candidate_id
        """
    ):
        record_key = str(row["record_key"])
        if record_key not in local_bindings:
            continue
        path = str(row["source_path"])
        path_info.setdefault(path, dict(row))
        if path not in selected_paths:
            add_evidence(
                path, (local_bindings[record_key],), "exact_local_record_duplicate_of_md",
                redundant_ids=redundant_ids, redundant_basis=redundant_basis,
            )

    assets = full_assets(index)
    if len(assets) != FULL_EXPECTED["indexed"]:
        raise ReconcileError(f"full-WF indexed asset scope drifted: {len(assets)}")
    index_conflicts = []
    exact_index_matches = 0
    for asset in assets:
        path = str(asset["source_path"])
        path_info.setdefault(path, asset)
        if path in selected_paths:
            continue
        if path in current_md_paths:
            inventory_ids = inventory_to_md.get(str(asset["inventory"]), set()) if asset["inventory"] else set()
            visual_ids = md_hashes.get((asset["ahash"], asset["dhash"], asset["phash"]), set())
            if inventory_ids and visual_ids and inventory_ids != visual_ids:
                index_conflicts.append({
                    "source_path": path,
                    "inventory_md_ids": sorted(inventory_ids, key=int),
                    "visual_md_ids": sorted(visual_ids, key=int),
                    "resolution": "existing_reconciled_candidate_binding",
                })
                redundant_basis[path].add("existing_binding_overrode_filename_visual_conflict")
            continue
        inventory_ids = inventory_to_md.get(str(asset["inventory"]), set()) if asset["inventory"] else set()
        visual_ids = md_hashes.get((asset["ahash"], asset["dhash"], asset["phash"]), set())
        if inventory_ids and visual_ids and inventory_ids != visual_ids:
            index_conflicts.append({
                "source_path": path,
                "inventory_md_ids": sorted(inventory_ids, key=int),
                "visual_md_ids": sorted(visual_ids, key=int),
                "resolution": "left_unresolved",
            })
            continue
        matched = inventory_ids | visual_ids
        if len(matched) != 1:
            continue
        basis = (
            "full_wf_exact_inventory_and_visual" if inventory_ids and visual_ids else
            "full_wf_exact_inventory" if inventory_ids else
            "full_wf_exact_visual"
        )
        add_evidence(path, matched, basis, redundant_ids=redundant_ids, redundant_basis=redundant_basis)
        exact_index_matches += 1

    if any(row["resolution"] == "left_unresolved" for row in index_conflicts):
        raise ReconcileError(f"unresolved exact index evidence conflict: {index_conflicts[:3]}")
    if len(index_conflicts) != 1:
        raise ReconcileError(f"expected one reconciled filename/visual conflict, found {len(index_conflicts)}")
    if set(redundant_ids) & set(selected_paths):
        raise ReconcileError("a selected MD Master entered the redundant path set")
    for path in redundant_ids:
        try:
            Path(path).resolve().relative_to(SOURCE_ROOT.resolve())
        except ValueError as error:
            raise ReconcileError(f"redundant source path escaped protected Elements root: {path}") from error

    near_rows = []
    genuine_assets = []
    for asset in assets:
        if asset["extension"] not in TIFF_EXTENSIONS or not asset["inventory"]:
            continue
        path = str(asset["source_path"])
        if path in selected_paths or path in redundant_ids:
            continue
        ahash = int(str(asset["ahash"]), 16)
        dhash = int(str(asset["dhash"]), 16)
        phash = int(str(asset["phash"]), 16)
        matches = []
        for md_ahash, md_dhash, md_phash, md_id in md_hash_rows:
            phash_distance = (phash ^ md_phash).bit_count()
            if phash_distance > NEAR_MAX_PHASH:
                continue
            ahash_distance = (ahash ^ md_ahash).bit_count()
            dhash_distance = (dhash ^ md_dhash).bit_count()
            score = (ahash_distance + dhash_distance + phash_distance) / 3
            if score <= NEAR_MAX_MEAN:
                matches.append((score, phash_distance, ahash_distance, dhash_distance, md_id))
        if not matches:
            genuine_assets.append(asset)
            continue
        matches.sort(key=lambda value: (value[0], value[1], value[2], value[3], int(value[4])))
        best = matches[0]
        tied_ids = sorted({value[4] for value in matches if value[:4] == best[:4]}, key=int)
        near_rows.append({
            "source_path": path,
            "filename": asset["original_filename"],
            "normalized_inventory": asset["inventory"],
            "width_px": asset["width_px"],
            "height_px": asset["height_px"],
            "size_bytes": asset["size_bytes"],
            "best_md_object_ids": "; ".join(tied_ids),
            "best_md_inventories": "; ".join(md_metadata[value]["inventory"] for value in tied_ids),
            "best_md_titles": " | ".join(md_metadata[value]["title"] for value in tied_ids),
            "ahash_distance": best[2],
            "dhash_distance": best[3],
            "phash_distance": best[1],
            "visual_score": round(best[0], 6),
            "review_state": "possible_existing_md_near_match",
            "deletion_authorized": "false",
        })

    inventory_counts = Counter(str(asset["inventory"]) for asset in genuine_assets)
    visual_counts = Counter((asset["ahash"], asset["dhash"], asset["phash"]) for asset in genuine_assets)
    genuine_rows = []
    for asset in genuine_assets:
        visual_key = (asset["ahash"], asset["dhash"], asset["phash"])
        visual_id = "visual-" + hashlib.sha256(":".join(visual_key).encode("ascii")).hexdigest()[:16]
        genuine_rows.append({
            "visual_group_id": visual_id,
            "normalized_inventory": asset["inventory"],
            "source_path": asset["source_path"],
            "filename": asset["original_filename"],
            "extension": "." + asset["extension"],
            "width_px": asset["width_px"],
            "height_px": asset["height_px"],
            "size_bytes": asset["size_bytes"],
            "same_inventory_path_count": inventory_counts[str(asset["inventory"])],
            "exact_visual_group_path_count": visual_counts[visual_key],
            "classification_basis": (
                f"no exact MD inventory or visual match; no near MD match at "
                f"pHash<={NEAR_MAX_PHASH} and mean<={NEAR_MAX_MEAN:g}"
            ),
            "review_state": "genuine_new_local_tiff_candidate",
            "deletion_authorized": "false",
        })

    redundant_rows = []
    for path in sorted(redundant_ids):
        info = path_info[path]
        md_ids = sorted(redundant_ids[path], key=int)
        extension = str(info.get("extension") or "").casefold()
        if extension and not extension.startswith("."):
            extension = "." + extension
        redundant_rows.append({
            "source_path": path,
            "extension": extension,
            "size_bytes": info.get("size_bytes", ""),
            "normalized_inventory": normalize_inventory(
                info.get("inventory")
                or inventory_from_name(str(info.get("basename") or info.get("original_filename") or ""))
            ),
            "matched_md_object_ids": "; ".join(md_ids),
            "matched_md_inventories": "; ".join(str(md_metadata[value]["inventory"]) for value in md_ids),
            "selected_master_paths": " | ".join(str(selected_by_id[value]["source_path"]) for value in md_ids),
            "disposition": "redundant_to_existing_md_selected_master",
            "disposition_basis": "; ".join(sorted(redundant_basis[path])),
            "drain_state": "proposed_redundant",
            "deletion_authorized": "false",
        })

    return {
        "counts_before": counts,
        "selected_by_id": selected_by_id,
        "selected_paths": selected_paths,
        "local_bindings": local_bindings,
        "redundant_ids": redundant_ids,
        "redundant_basis": redundant_basis,
        "path_info": path_info,
        "redundant_rows": redundant_rows,
        "genuine_rows": sorted(genuine_rows, key=lambda row: (row["normalized_inventory"], row["source_path"])),
        "near_rows": sorted(near_rows, key=lambda row: (float(row["visual_score"]), row["source_path"])),
        "index_conflicts": index_conflicts,
        "exact_index_matches": exact_index_matches,
        "full_assets": len(assets),
    }


def export_local(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions = [
        dict(row) for row in connection.execute(
            """
            SELECT d.*,o.inventory,COALESCE(c.source_path,'') AS selected_source_path
            FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
            LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            ORDER BY o.inventory COLLATE NOCASE,d.record_key
            """
        )
    ]
    dispositions = []
    for row in connection.execute(
        """
        SELECT d.record_key,o.inventory,d.outcome,d.selected_candidate_id,d.matched_md_object_id,
               c.candidate_id,c.source_path,c.extension,c.size_bytes
        FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
        JOIN candidates c ON c.record_key=d.record_key
        ORDER BY o.inventory COLLATE NOCASE,c.source_path
        """
    ):
        extension = str(row["extension"]).casefold()
        if extension.lstrip(".") not in IMAGE_EXTENSIONS:
            disposition = "retain_metadata_evidence"
            basis = "non-image evidence remains attached to the object"
        elif row["outcome"] == "new_local_object":
            disposition = "selected_master" if row["candidate_id"] == row["selected_candidate_id"] else "redundant"
            basis = "human selected one local Master; all other image candidates are redundant"
        elif row["outcome"] == "duplicate_md":
            disposition = "redundant_to_existing_md_object"
            basis = f"user-directed exact match to md:{row['matched_md_object_id']} with an existing selected TIFF Master"
        else:
            disposition = "unresolved"
            basis = "human left object unresolved"
        dispositions.append({
            "record_key": row["record_key"], "inventory": row["inventory"],
            "outcome": row["outcome"], "matched_md_object_id": row["matched_md_object_id"] or "",
            "candidate_id": row["candidate_id"], "source_path": row["source_path"],
            "extension": row["extension"], "size_bytes": row["size_bytes"],
            "disposition": disposition, "disposition_basis": basis,
            "deletion_authorized": "false",
        })
    return decisions, dispositions


def database_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    fields = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY source_path")]
    return fields, rows


def run(*, apply: bool) -> dict[str, Any]:
    mount = source_mount()
    required = (
        DATABASE, FULL_INDEX, FULL_COMPLETION, SOURCE_CSV, DRAIN_CSV,
        LOCAL_DECISIONS_CSV, LOCAL_DISPOSITIONS_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT,
    )
    if not all(path.is_file() for path in required):
        raise ReconcileError("required queue, CSV, preflight, or full-index evidence is missing")
    if apply and RECEIPT.exists():
        raise ReconcileError(f"application receipt already exists: {RECEIPT}")
    with closing(connect(FULL_INDEX, read_only=True)) as index:
        completion = validate_full_index(index)
        with closing(connect(DATABASE, read_only=True)) as queue:
            projected = projection(queue, index)

    summary = {
        "result": "ok",
        "mode": "apply" if apply else "check",
        "md_objects_with_selected_tiff_master": len(projected["selected_by_id"]),
        "exact_local_records_to_exclude": len(projected["local_bindings"]),
        "redundant_image_paths": len(projected["redundant_rows"]),
        "full_wf_indexed_assets_compared": projected["full_assets"],
        "full_wf_new_exact_md_image_matches": projected["exact_index_matches"],
        "genuine_new_local_tiff_candidate_paths": len(projected["genuine_rows"]),
        "genuine_new_local_tiff_visual_groups": len({row["visual_group_id"] for row in projected["genuine_rows"]}),
        "genuine_new_local_tiff_inventories": len({row["normalized_inventory"] for row in projected["genuine_rows"]}),
        "near_md_tiff_paths_left_for_review": len(projected["near_rows"]),
        "resolved_index_evidence_conflicts": len(projected["index_conflicts"]),
        "deletion_authorized": False,
    }
    if not apply:
        return summary
    if workbench_is_listening():
        raise ReconcileError("port 8768 must be stopped before applying the queue update")
    if len(projected["genuine_rows"]) != 209 or len(projected["near_rows"]) != 49:
        raise ReconcileError("bounded genuine/near TIFF scope guard failed")

    prepared_at = utc_now()
    stamp = prepared_at.replace("-", "").replace(":", "")
    backups = [
        backup(path, stamp) for path in (
            DATABASE, SOURCE_CSV, DRAIN_CSV, LOCAL_DECISIONS_CSV,
            LOCAL_DISPOSITIONS_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT,
        )
    ]
    with closing(connect(DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in EXPECTED_BEFORE
        }
        if current != projected["counts_before"]:
            raise ReconcileError(f"queue changed after comparison preflight: {current}")

        for record_key, md_id in sorted(projected["local_bindings"].items()):
            metadata = connection.execute(
                "SELECT * FROM local_object_metadata WHERE record_key=?", (record_key,)
            ).fetchone()
            if metadata is None:
                raise ReconcileError(f"local metadata disappeared: {record_key}")
            connection.execute(
                """
                INSERT INTO local_object_decisions
                  (record_key,outcome,selected_candidate_id,matched_md_object_id,
                   title,description,date_text,object_type,material,dimensions,
                   note,decided_by,decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_key, "duplicate_md", None, md_id,
                    metadata["title"], metadata["description"], metadata["date_text"],
                    metadata["object_type"], metadata["material"], metadata["dimensions"],
                    (
                        f"User-directed exact MD exclusion: md:{md_id} already has a selected TIFF Master; "
                        "other local TIFFs and derivatives are redundant. Deletion is not authorized."
                    ),
                    "codex:user-directed-md-exclusion", prepared_at,
                ),
            )

        source_fields = [
            str(row["name"]) for row in connection.execute("PRAGMA table_info(source_disposition_proposal)")
        ]
        for path in sorted(projected["redundant_ids"]):
            md_ids = sorted(projected["redundant_ids"][path], key=int)
            basis = "; ".join(sorted(projected["redundant_basis"][path]))
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?", (path,)
            ).fetchone()
            if existing is None:
                info = projected["path_info"][path]
                extension = str(info.get("extension") or "").casefold().lstrip(".")
                inventory = normalize_inventory(
                    info.get("inventory")
                    or inventory_from_name(str(info.get("original_filename") or info.get("basename") or ""))
                )
                row = {
                    "source_path": path, "source_kind": "file", "unit": "tfa",
                    "inventory": inventory,
                    "source_role": "master_candidate" if extension in TIFF_EXTENSIONS else "derivative_candidate",
                    "final_path": "", "preservation_status": "redundant",
                    "preservation_basis": "existing_md_selected_tiff_master",
                    "source_sha256": "", "final_sha256": "", "source_size_bytes": int(info["size_bytes"]),
                    "exact_copy_proven": "false", "drain_state": "proposed_redundant",
                    "deletion_authorized": "false", "notes": "",
                }
            else:
                row = dict(existing)
            note = (
                f"Redundant image; matched reconciled {', '.join('md:' + value for value in md_ids)}; "
                f"selected TIFF Master exists; evidence: {basis}; deletion not authorized"
            )
            old_note = clean(row.get("notes"))
            row.update({
                "preservation_status": "redundant",
                "preservation_basis": "existing_md_selected_tiff_master",
                "drain_state": "proposed_redundant",
                "deletion_authorized": "false",
                "notes": f"{old_note}; {note}" if old_note else note,
            })
            values = [row[field] for field in source_fields]
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
                + ",".join("?" for _ in source_fields) + ")",
                values,
            )
            connection.execute("DELETE FROM drain_list_proposal WHERE source_path=?", (path,))
            connection.execute(
                "INSERT INTO drain_list_proposal VALUES ("
                + ",".join("?" for _ in source_fields) + ")",
                values,
            )

        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_full_wf_md_redundancy_receipt',?)",
            (str(RECEIPT),),
        )
        connection.commit()

    with closing(connect(DATABASE, read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        local_decision_count = int(connection.execute("SELECT COUNT(*) FROM local_object_decisions").fetchone()[0])
        duplicate_count = int(
            connection.execute("SELECT COUNT(*) FROM local_object_decisions WHERE outcome='duplicate_md'").fetchone()[0]
        )
        proposed_redundant = int(
            connection.execute("SELECT COUNT(*) FROM drain_list_proposal WHERE drain_state='proposed_redundant'").fetchone()[0]
        )
        unauthorized = int(
            connection.execute("SELECT COUNT(*) FROM drain_list_proposal WHERE deletion_authorized!='false'").fetchone()[0]
        )
        source_fields, source_rows = database_rows(connection, "source_disposition_proposal")
        drain_fields, drain_rows = database_rows(connection, "drain_list_proposal")
        local_decisions, local_dispositions = export_local(connection)
        unmatched_redundant = sum(
            connection.execute(
                """
                SELECT COUNT(*) FROM drain_list_proposal
                WHERE source_path=? AND drain_state='proposed_redundant' AND deletion_authorized='false'
                """,
                (row["source_path"],),
            ).fetchone()[0] != 1
            for row in projected["redundant_rows"]
        )
    if quick != "ok" or foreign or unauthorized or unmatched_redundant:
        raise ReconcileError(
            f"post-apply validation failed: quick={quick}, foreign={len(foreign)}, "
            f"unauthorized={unauthorized}, unmatched_redundant={unmatched_redundant}"
        )
    if local_decision_count != 295 or duplicate_count != 294:
        raise ReconcileError(
            f"local decision scope failed: total={local_decision_count}, duplicate_md={duplicate_count}"
        )

    atomic_csv(SOURCE_CSV, source_fields, source_rows)
    atomic_csv(DRAIN_CSV, drain_fields, drain_rows)
    atomic_csv(
        LOCAL_DECISIONS_CSV,
        (
            "record_key", "outcome", "selected_candidate_id", "matched_md_object_id", "title", "description",
            "date_text", "object_type", "material", "dimensions", "note", "decided_by", "decided_at",
            "inventory", "selected_source_path",
        ),
        local_decisions,
    )
    atomic_csv(
        LOCAL_DISPOSITIONS_CSV,
        (
            "record_key", "inventory", "outcome", "matched_md_object_id", "candidate_id", "source_path",
            "extension", "size_bytes", "disposition", "disposition_basis", "deletion_authorized",
        ),
        local_dispositions,
    )
    atomic_csv(
        REDUNDANT_CSV,
        (
            "source_path", "extension", "size_bytes", "normalized_inventory", "matched_md_object_ids",
            "matched_md_inventories", "selected_master_paths", "disposition", "disposition_basis",
            "drain_state", "deletion_authorized",
        ),
        projected["redundant_rows"],
    )
    atomic_csv(
        GENUINE_CSV,
        (
            "visual_group_id", "normalized_inventory", "source_path", "filename", "extension", "width_px",
            "height_px", "size_bytes", "same_inventory_path_count", "exact_visual_group_path_count",
            "classification_basis", "review_state", "deletion_authorized",
        ),
        projected["genuine_rows"],
    )
    atomic_csv(
        NEAR_CSV,
        (
            "source_path", "filename", "normalized_inventory", "width_px", "height_px", "size_bytes",
            "best_md_object_ids", "best_md_inventories", "best_md_titles", "ahash_distance",
            "dhash_distance", "phash_distance", "visual_score", "review_state", "deletion_authorized",
        ),
        projected["near_rows"],
    )

    local_preflight = json.loads(LOCAL_PREFLIGHT.read_text(encoding="utf-8"))
    local_preflight.update({
        "prepared_at": prepared_at,
        "existing_local_decisions_preserved": local_decision_count,
        "full_wf_md_redundancy_receipt": str(RECEIPT),
        "genuine_new_local_tiff_candidates": str(GENUINE_CSV),
        "near_md_tiff_review": str(NEAR_CSV),
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
    })
    atomic_json(LOCAL_PREFLIGHT, local_preflight)
    workbench_preflight = json.loads(WORKBENCH_PREFLIGHT.read_text(encoding="utf-8"))
    workbench_preflight["full_wf_md_redundancy_apply"] = {
        "applied_at": prepared_at,
        "receipt": str(RECEIPT),
        "redundant_images": str(REDUNDANT_CSV),
        "genuine_new_tiffs": str(GENUINE_CSV),
        "near_md_tiff_review": str(NEAR_CSV),
        "deletion_authorized": False,
    }
    atomic_json(WORKBENCH_PREFLIGHT, workbench_preflight)

    receipt = {
        "format": "iss-tfa-full-wf-md-redundancy-apply-v1",
        "version": VERSION,
        "applied_at": prepared_at,
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "full_index_completion": str(FULL_COMPLETION),
        "full_index_database_sha256": completion["database_sha256"],
        "backups": backups,
        "redundant_images_csv": str(REDUNDANT_CSV),
        "genuine_new_local_tiffs_csv": str(GENUINE_CSV),
        "near_md_tiff_review_csv": str(NEAR_CSV),
        "source_disposition_csv": str(SOURCE_CSV),
        "drain_list_csv": str(DRAIN_CSV),
        "local_decisions_csv": str(LOCAL_DECISIONS_CSV),
        "counts_after": {
            "local_object_decisions": local_decision_count,
            "duplicate_md_decisions": duplicate_count,
            "drain_proposed_redundant": proposed_redundant,
        },
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
        **summary,
        "mode": "apply",
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
    raise SystemExit(
        "superseded: metadata and filename evidence are forbidden for the visual-only queue; "
        "use tfa_visual_only_rebuild.py"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = run(apply=args.apply)
    except Exception as error:
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
