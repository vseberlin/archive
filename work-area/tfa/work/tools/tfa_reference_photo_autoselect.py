#!/usr/bin/env python3
"""Select clear TFA reference-photo Masters and prepare accession candidates.

The protected reference-photo TIFF folder is compared with current TFA selected
Masters and the complete institution museum-digital display-image index.  Clear
outside-MD objects receive one technically best decodable TIFF Master.  Related
images are recorded as redundant.  Cross-inventory visual collisions, current
MD identity conflicts, protected prior selections, and decode failures remain
held.  Metadata is descriptive evidence only and never participates in visual
classification.

No source file is changed or deleted.  Apply mode writes only below the existing
TFA work root and updates the existing reconciliation SQLite catalogue; it does
not create a new database or a new workflow phase.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_ROOT = SOURCE_ROOT / "restore/archive"
REFERENCE_ROOT = ARCHIVE_ROOT / "SCANS-fertig/01_Referenzfotobuecher/Alle-freigestellten-Tiffs"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
MD_CATALOG = REVIEW_ROOT / "md-accession-catalog.csv"
INSTITUTION_VISUAL = REVIEW_ROOT / "institution-md-visual-references.csv"
INSTITUTION_VISUAL_RECEIPT = REVIEW_ROOT / "institution-md-visual-index.json"
INSTITUTION_CAPTURE = (
    WORK_ROOT
    / "work/reconciliation/museum-digital/runs/"
    "20260814T201913Z-tfa-institution-29-clean-local-only-recheck"
)
LOCAL_METADATA = REVIEW_ROOT / "local-object-metadata.csv"
ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
LOCAL_PREP_TOOL = Path(__file__).with_name("tfa_local_objects_prepare.py")

MASTER_DECISIONS = REVIEW_ROOT / "reference-photo-master-decisions.csv"
SOURCE_DISPOSITION = REVIEW_ROOT / "reference-photo-source-disposition.csv"
DRAIN_LIST = REVIEW_ROOT / "reference-photo-drain-list.csv"
ACCESSION_CANDIDATES = REVIEW_ROOT / "reference-photo-accession-candidates.csv"
METADATA_OUTPUT = REVIEW_ROOT / "reference-photo-metadata.csv"
HOLDS = REVIEW_ROOT / "reference-photo-holds.csv"
WORKBENCH_HOLDS = REVIEW_ROOT / "reference-photo-workbench-holds.csv"
RECEIPT = REVIEW_ROOT / "reference-photo-reconciliation.json"
VISUAL_CONSOLIDATION_RECEIPT = REVIEW_ROOT / "reference-photo-visual-review-consolidation.json"

CORE_LOCAL_DECISIONS = REVIEW_ROOT / "local-object-decisions.csv"
CORE_LOCAL_DISPOSITIONS = REVIEW_ROOT / "local-object-candidate-dispositions.csv"
CORE_SOURCE_DISPOSITION = REVIEW_ROOT / "source-disposition-proposal.csv"
CORE_DRAIN_LIST = REVIEW_ROOT / "drain-list-proposal.csv"
ORPHAN_DECISIONS = REVIEW_ROOT / "orphan-object-decisions.csv"
ORPHAN_DISPOSITIONS = REVIEW_ROOT / "orphan-source-disposition-proposal.csv"

TIFF_EXTENSIONS = {"tif", "tiff"}
HASH_IDS = ("ahash-64-v2", "dhash-64-v2", "phash-dct-64-v2")
EXACT_MAX_ASPECT_DELTA = 0.10
NEAR_MAX_TOTAL = 15
NEAR_MAX_ASPECT_DELTA = 0.03
EXPECTED_REFERENCE_TIFFS = 4972
EXPECTED_INSTITUTION_OBJECTS = 11170
ACTOR = "codex:user-directed-reference-photo-best-candidate"

TFA_RE = re.compile(
    r"(?i)(?:^|[^A-Z0-9])TFA(?:-(?:FOT|NEG|KB))?[-_ ]?"
    r"(\d{5,8}(?:[-_.]\d{1,2})?)"
)

MASTER_FIELDS = (
    "record_key", "inventory", "decision", "selected_asset_id", "selected_source_path",
    "selected_width_px", "selected_height_px", "selected_size_bytes", "candidate_path_count",
    "selection_basis", "metadata_state", "accession_candidate_state", "decided_by", "decided_at",
    "deletion_authorized",
)
SOURCE_FIELDS = (
    "record_key", "inventory", "asset_id", "source_path", "extension", "width_px", "height_px",
    "size_bytes", "preservation_status", "preservation_basis", "drain_state",
    "deletion_authorized",
)
ACCESSION_FIELDS = (
    "record_key", "inventory", "selected_asset_id", "selected_master_path", "extension",
    "width_px", "height_px", "size_bytes", "title", "description", "date_text", "object_type",
    "material", "dimensions", "collection_text", "historical_md_id", "metadata_state",
    "metadata_source", "metadata_source_row", "provenance_chain", "accession_candidate_state",
    "final_path", "final_hash", "deletion_authorized",
)
METADATA_FIELDS = (
    "record_key", "inventory", "title", "description", "date_text", "object_type", "material",
    "dimensions", "collection_text", "historical_md_id", "metadata_state", "preferred_source",
    "preferred_row", "source_count", "source_rows_json",
)
HOLD_FIELDS = (
    "hold_id", "inventory", "inventory_members", "asset_id", "source_path", "filename",
    "extension", "width_px", "height_px", "size_bytes", "hold_reason", "error",
    "deletion_authorized",
)
WORKBENCH_FIELDS = (
    "normalized_inventory", "inventory_members", "visual_group_id", "asset_id", "source_path",
    "filename", "extension", "size_bytes", "mtime_ns", "width_px", "height_px", "ahash",
    "dhash", "phash", "exact_visual_group_tiff_count", "classification_basis", "review_state",
    "deletion_authorized",
)


class ReconciliationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def inventory(value: Any) -> str:
    match = TFA_RE.search(str(value or ""))
    if match is None:
        return ""
    suffix = re.sub(r"[_ .]+", "-", match.group(1)).strip("-")
    return "TFA-" + suffix


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    else:
        connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def source_mount() -> dict[str, Any]:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(completed.stdout).get("filesystems") or []
    if len(values) != 1:
        raise ReconciliationError("Elements did not resolve to one mounted filesystem")
    mount = values[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise ReconciliationError(f"Elements read-only guard failed: {mount}")
    return mount


def assert_work_path(path: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ReconciliationError(f"write path escaped TFA work root: {path}") from error


def workbench_listening() -> bool:
    with socket.socket() as client:
        client.settimeout(0.25)
        return client.connect_ex(("127.0.0.1", 8768)) == 0


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL, extrasaction="ignore"
            )
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
    if not path.exists():
        return ""
    assert_work_path(path)
    target = path.with_name(f"{path.stem}.pre-reference-photo-{stamp}{path.suffix}")
    if target.exists():
        raise ReconciliationError(f"backup already exists: {target}")
    if path.suffix == ".sqlite":
        with closing(connect(path, read_only=True)) as source, closing(connect(target)) as destination:
            source.backup(destination)
    else:
        shutil.copy2(path, target)
    return str(target)


def visual_signature(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["ahash"]), str(row["dhash"]), str(row["phash"]))


def visual_group_id(row: dict[str, Any]) -> str:
    return "visual-" + hashlib.sha256(":".join(visual_signature(row)).encode()).hexdigest()[:16]


def arrays(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.array([int(row["ahash"], 16) for row in rows], dtype=np.uint64),
        np.array([int(row["dhash"], 16) for row in rows], dtype=np.uint64),
        np.array([int(row["phash"], 16) for row in rows], dtype=np.uint64),
        np.array([int(row["width_px"]) / int(row["height_px"]) for row in rows], dtype=np.float64),
    )


def load_visual_assets() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not VISUAL_INDEX.is_file():
        raise ReconciliationError("complete archive visual index is missing")
    archive_prefix = str(ARCHIVE_ROOT.relative_to(SOURCE_ROOT)) + "/%"
    reference_prefix = str(REFERENCE_ROOT.relative_to(SOURCE_ROOT)) + "/"
    indexed: list[dict[str, Any]] = []
    folder: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with closing(connect(VISUAL_INDEX, read_only=True)) as connection:
        root = connection.execute(
            "SELECT root_id FROM visual_roots WHERE root_path=?", (str(SOURCE_ROOT),)
        ).fetchone()
        if root is None:
            raise ReconciliationError("visual index is not bound to Elements")
        query = """
            SELECT a.asset_id,a.relative_path,a.original_filename,a.extension,a.size_bytes,
                   a.mtime_ns,a.width_px,a.height_px,
                   MAX(CASE WHEN f.extractor_id=? THEN f.feature_hex ELSE '' END) ahash,
                   MAX(CASE WHEN f.extractor_id=? THEN f.feature_hex ELSE '' END) dhash,
                   MAX(CASE WHEN f.extractor_id=? THEN f.feature_hex ELSE '' END) phash
            FROM visual_assets a
            JOIN visual_features f ON f.asset_id=a.asset_id
            WHERE a.root_id=? AND a.status='indexed' AND a.relative_path LIKE ?
            GROUP BY a.asset_id
        """
        for row in connection.execute(query, (*HASH_IDS, str(root[0]), archive_prefix)):
            item = {
                "asset_id": str(row["asset_id"]),
                "source_path": str(SOURCE_ROOT / str(row["relative_path"])),
                "relative_path": str(row["relative_path"]),
                "filename": str(row["original_filename"]),
                "extension": str(row["extension"]).casefold().lstrip("."),
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "width_px": int(row["width_px"] or 0),
                "height_px": int(row["height_px"] or 0),
                "ahash": str(row["ahash"]),
                "dhash": str(row["dhash"]),
                "phash": str(row["phash"]),
                "status": "indexed",
                "error": "",
            }
            if not all(item[key] for key in ("ahash", "dhash", "phash")):
                continue
            indexed.append(item)
            if item["relative_path"].startswith(reference_prefix):
                folder.append(item)
        for row in connection.execute(
            """
            SELECT asset_id,relative_path,original_filename,extension,size_bytes,mtime_ns,
                   width_px,height_px,error
            FROM visual_assets
            WHERE root_id=? AND status='error' AND relative_path LIKE ?
            ORDER BY relative_path
            """,
            (str(root[0]), reference_prefix + "%"),
        ):
            errors.append({
                "asset_id": str(row["asset_id"]),
                "source_path": str(SOURCE_ROOT / str(row["relative_path"])),
                "relative_path": str(row["relative_path"]),
                "filename": str(row["original_filename"]),
                "extension": str(row["extension"]).casefold().lstrip("."),
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "width_px": int(row["width_px"] or 0),
                "height_px": int(row["height_px"] or 0),
                "ahash": "", "dhash": "", "phash": "", "status": "error",
                "error": clean(row["error"]),
            })
    if len(folder) + len(errors) != EXPECTED_REFERENCE_TIFFS:
        raise ReconciliationError(
            f"reference TIFF scope drifted: indexed={len(folder)} errors={len(errors)}"
        )
    return indexed, folder, errors


def load_institution_capture() -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, str]]]:
    coverage = json.loads((INSTITUTION_CAPTURE / "coverage.json").read_text(encoding="utf-8"))
    if (
        int(coverage.get("captured_objects", -1)) != EXPECTED_INSTITUTION_OBJECTS
        or int(coverage.get("request_failures", -1)) != 0
    ):
        raise ReconciliationError("institution museum-digital capture is incomplete")
    by_inventory: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    by_id: dict[str, dict[str, str]] = {}
    for path in sorted((INSTITUTION_CAPTURE / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("results", {}).get("objects", {})
        iterable = values.values() if isinstance(values, dict) else values
        for item in iterable:
            md_id = clean(item.get("object_id"))
            inv = inventory(item.get("object_inventory_number"))
            row = {
                "md_object_id": md_id,
                "inventory": inv,
                "title": clean(item.get("object_name")),
            }
            by_id[md_id] = row
            if inv:
                by_inventory[inv].append(row)
    if len(by_id) != EXPECTED_INSTITUTION_OBJECTS:
        raise ReconciliationError(f"institution capture object count drifted: {len(by_id)}")
    return dict(by_inventory), by_id


def load_md_references(
    by_path: dict[str, dict[str, Any]], institution_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    references: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    selected_asset_ids: set[str] = set()
    rows = read_csv(MD_CATALOG)
    if len(rows) != 5282:
        raise ReconciliationError(f"current TFA MD catalogue drifted: {len(rows)}")
    for row in rows:
        path = row["selected_master_path"]
        asset = by_path.get(path)
        if asset is None:
            continue
        references.append({
            **asset,
            "md_object_id": row["md_object_id"],
            "md_inventory": row["inventory"],
            "reference_kind": "selected_tiff_master",
        })
        selected_paths.add(path)
        selected_asset_ids.add(asset["asset_id"])
    visual_receipt = json.loads(INSTITUTION_VISUAL_RECEIPT.read_text(encoding="utf-8"))
    if (
        int(visual_receipt.get("institution_objects", -1)) != EXPECTED_INSTITUTION_OBJECTS
        or visual_receipt.get("result") != "ok"
    ):
        raise ReconciliationError("institution visual reference receipt is invalid")
    if hashlib.sha256(INSTITUTION_VISUAL.read_bytes()).hexdigest() != visual_receipt.get("output_sha256"):
        raise ReconciliationError("institution visual reference fingerprint drifted")
    for row in read_csv(INSTITUTION_VISUAL):
        if row.get("status") != "indexed":
            continue
        md_id = row["md_object_id"]
        identity = institution_by_id.get(md_id, {})
        references.append({
            "asset_id": "md-display:" + md_id,
            "source_path": row["image_url"],
            "relative_path": "",
            "filename": Path(row["image_url"]).name,
            "extension": Path(row["image_url"]).suffix.casefold().lstrip("."),
            "size_bytes": int(row["download_bytes"]),
            "mtime_ns": 0,
            "width_px": int(row["image_width_px"]),
            "height_px": int(row["image_height_px"]),
            "ahash": row["ahash"], "dhash": row["dhash"], "phash": row["phash"],
            "status": "indexed", "error": "", "md_object_id": md_id,
            "md_inventory": identity.get("inventory") or row["md_inventory"],
            "reference_kind": "institution_display_image",
        })
    if not references:
        raise ReconciliationError("no MD visual references are available")
    return references, selected_paths, selected_asset_ids


def classify_md(
    folder: list[dict[str, Any]], references: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ref_a, ref_d, ref_p, ref_ratio = arrays(references)
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for start in range(0, len(folder), 128):
        group = folder[start : start + 128]
        a, d, p, ratio = arrays(group)
        da = np.bitwise_count(a[:, None] ^ ref_a)
        dd = np.bitwise_count(d[:, None] ^ ref_d)
        dp = np.bitwise_count(p[:, None] ^ ref_p)
        total = da + dd + dp
        aspect = np.abs(ratio[:, None] - ref_ratio) / ref_ratio
        qualified = ((total == 0) & (aspect <= EXACT_MAX_ASPECT_DELTA)) | (
            (total <= NEAR_MAX_TOTAL) & (aspect <= NEAR_MAX_ASPECT_DELTA)
        )
        masked = np.where(qualified, total, 255)
        best = masked.argmin(axis=1)
        values = masked[np.arange(len(group)), best]
        for position, asset in enumerate(group):
            if int(values[position]) == 255:
                unmatched.append(asset)
                continue
            index = int(best[position])
            reference = references[index]
            tied_indexes = np.where(
                qualified[position]
                & (total[position] == values[position])
                & (dp[position] == dp[position, index])
            )[0]
            tied = [references[int(value)] for value in tied_indexes]
            matched.append({
                **asset,
                "matched_md_object_ids": "; ".join(sorted({row["md_object_id"] for row in tied})),
                "matched_md_inventories": "; ".join(sorted({row["md_inventory"] for row in tied if row["md_inventory"]})),
                "reference_kinds": "; ".join(sorted({row["reference_kind"] for row in tied})),
                "ahash_distance": int(da[position, index]),
                "dhash_distance": int(dd[position, index]),
                "phash_distance": int(dp[position, index]),
                "distance_total": int(values[position]),
                "aspect_ratio_delta": round(float(aspect[position, index]), 8),
            })
    return matched, unmatched


def relate_assets(
    seeds: list[dict[str, Any]], assets: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]], int]:
    all_a, all_d, all_p, all_ratio = arrays(assets)
    related: defaultdict[str, set[str]] = defaultdict(set)
    asset_to_inventories: defaultdict[str, set[str]] = defaultdict(set)
    missing_inventory = 0
    for start in range(0, len(seeds), 24):
        group = seeds[start : start + 24]
        a, d, p, ratio = arrays(group)
        da = np.bitwise_count(a[:, None] ^ all_a)
        dd = np.bitwise_count(d[:, None] ^ all_d)
        dp = np.bitwise_count(p[:, None] ^ all_p)
        total = da + dd + dp
        aspect = np.abs(ratio[:, None] - all_ratio) / all_ratio
        qualified = ((total == 0) & (aspect <= EXACT_MAX_ASPECT_DELTA)) | (
            (total <= NEAR_MAX_TOTAL) & (aspect <= NEAR_MAX_ASPECT_DELTA)
        )
        for position, seed in enumerate(group):
            inv = inventory(seed["filename"])
            if not inv:
                missing_inventory += 1
                continue
            for index in np.where(qualified[position])[0]:
                asset_id = assets[int(index)]["asset_id"]
                related[inv].add(asset_id)
                asset_to_inventories[asset_id].add(inv)
    return dict(related), dict(asset_to_inventories), missing_inventory


def load_prior_decisions() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    selected: dict[str, dict[str, str]] = {}
    noise: dict[str, dict[str, str]] = {}
    if DATABASE.exists():
        with closing(connect(DATABASE, read_only=True)) as connection:
            for row in connection.execute(
                """
                SELECT o.inventory,c.source_path selected_source_path,d.decided_by,d.decided_at,d.note
                FROM local_object_decisions d
                JOIN objects o ON o.record_key=d.record_key
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.outcome='new_local_object'
                """
            ):
                inv = inventory(row["inventory"])
                if inv:
                    selected[inv] = dict(row)
    if ORPHAN_DECISIONS.exists():
        for row in read_csv(ORPHAN_DECISIONS):
            inv = inventory(row.get("inventory"))
            if not inv:
                continue
            if row.get("decision") == "selected_master":
                selected[inv] = row
            elif row.get("decision") == "noise":
                noise[inv] = row
    return selected, noise


def load_metadata(targets: set[str]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for row in read_csv(LOCAL_METADATA):
        inv = inventory(row.get("inventory"))
        if inv in targets:
            item: dict[str, Any] = dict(row)
            item["inventory"] = inv
            metadata[inv] = item

    spec = importlib.util.spec_from_file_location("tfa_local_prepare_for_reference", LOCAL_PREP_TOOL)
    if spec is None or spec.loader is None:
        raise ReconciliationError("cannot load local metadata parser")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    objects: dict[str, dict[str, Any]] = {}
    sources: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    with closing(connect(ARCHIVE_INDEX, read_only=True)) as connection:
        for row in connection.execute("SELECT * FROM objects"):
            inv = inventory(row["inventory_number"])
            if inv in targets and inv not in objects:
                objects[inv] = dict(row)
        for row in connection.execute("SELECT * FROM object_sources"):
            inv = inventory(row["inventory_number"])
            if inv in targets:
                sources[inv].append(dict(row))
    for inv in targets:
        if inv not in sources:
            continue
        obj = objects.get(inv, {
            "inventory_number": inv, "collection": "", "object_type": "", "title": "",
            "description": "", "md_id": "", "preferred_source": "",
        })
        item = module.parsed_metadata(obj, sources[inv])
        item["inventory"] = inv
        item["record_key"] = "local:" + inv
        best_sources = json.loads(item.get("source_rows_json") or "[]")
        if best_sources:
            cells = module.raw_cells(best_sources[0].get("raw_json", ""))
            if (
                len(cells) >= 14
                and re.fullmatch(r"\d{5,8}", clean(cells[0]))
                and clean(cells[1]).upper().startswith("TFA-FOT-")
            ):
                item.update({
                    "date_text": cells[2],
                    "object_type": "Foto",
                    "title": cells[4] or cells[5] or item.get("title", ""),
                    "description": cells[6] or item.get("description", ""),
                    "material": cells[12],
                    "dimensions": "",
                    "collection_text": "Archiv der Bildstelle des WF (TFA)",
                })
        date_value = clean(item.get("date_text"))
        if re.fullmatch(r"\d{5}", date_value) and 15000 <= int(date_value) <= 60000:
            item["date_text"] = (datetime(1899, 12, 30) + timedelta(days=int(date_value))).date().isoformat()
        preferred = clean(item.get("preferred_source"))
        legacy_prefix = "/mnt/volume/archive/"
        if preferred.startswith(legacy_prefix):
            current = ARCHIVE_ROOT / preferred.removeprefix(legacy_prefix)
            if current.is_file():
                item["preferred_source"] = str(current)
        metadata[inv] = item

    reliable = {}
    for inv, row in metadata.items():
        descriptive = [clean(row.get(key)) for key in (
            "title", "description", "date_text", "object_type", "material", "dimensions"
        )]
        if clean(row.get("preferred_source")) and sum(bool(value) for value in descriptive) >= 2:
            row["metadata_state"] = "historical_working_metadata_applied"
            reliable[inv] = row
    return reliable


def protected_selections() -> set[str]:
    with closing(connect(DATABASE, read_only=True)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT source_path FROM source_disposition_proposal
                WHERE preservation_status IN ('selected_master','retained_existing_unit_evidence')
                """
            )
        }


def connected_components(shared: dict[str, set[str]]) -> list[set[str]]:
    graph: defaultdict[str, set[str]] = defaultdict(set)
    for values in shared.values():
        if len(values) < 2:
            continue
        for value in values:
            graph[value].update(values - {value})
    components: list[set[str]] = []
    remaining = set(graph)
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            value = stack.pop()
            new = graph[value] - component
            component.update(new)
            remaining.difference_update(new)
            stack.extend(new)
        components.append(component)
    return sorted(components, key=lambda values: sorted(values))


def build_projection() -> dict[str, Any]:
    mount = source_mount()
    assets, folder, decode_errors = load_visual_assets()
    by_id = {row["asset_id"]: row for row in assets}
    by_path = {row["source_path"]: row for row in assets}
    institution_by_inventory, institution_by_id = load_institution_capture()
    references, md_selected_paths, md_selected_asset_ids = load_md_references(by_path, institution_by_id)
    md_matches, unmatched = classify_md(folder, references)
    related, asset_to_inventories, missing_inventory = relate_assets(unmatched, assets)
    shared = {key: values for key, values in asset_to_inventories.items() if len(values) > 1}
    collision_groups: list[dict[str, Any]] = []
    for component in connected_components(shared):
        members = sorted(component)
        component_id = "REFAMB-" + hashlib.sha256(";".join(members).encode()).hexdigest()[:12].upper()
        component_assets = set().union(*(related.get(inv, set()) for inv in component))
        tiffs = [by_id[value] for value in component_assets if by_id[value]["extension"] in TIFF_EXTENSIONS]
        rows_by_visual: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in tiffs:
            rows_by_visual[visual_group_id(row)].append(row)
        for group_id, rows in rows_by_visual.items():
            filename_inventories = sorted({
                value for value in (inventory(row["filename"]) for row in rows) if value
            })
            collision_groups.append({
                "component_id": component_id,
                "component_members": members,
                "visual_group_id": group_id,
                "rows": rows,
                "filename_inventories": filename_inventories,
            })
    inventory_group_counts = Counter(
        inv for group in collision_groups for inv in group["filename_inventories"]
    )
    resolvable_collision_groups: dict[str, dict[str, Any]] = {}
    unresolved_collision_groups: list[dict[str, Any]] = []
    for group in collision_groups:
        values = group["filename_inventories"]
        if len(values) == 1 and inventory_group_counts[values[0]] == 1:
            inv = values[0]
            resolvable_collision_groups[inv] = group
            related[inv] = {row["asset_id"] for row in group["rows"]}
        else:
            unresolved_collision_groups.append(group)
    collision_member_inventories = {
        inv for group in collision_groups for inv in group["component_members"]
    }
    ambiguous_inventories = (
        collision_member_inventories - set(resolvable_collision_groups)
    ) | {
        inv for group in unresolved_collision_groups for inv in group["filename_inventories"]
    }
    prior_selected, prior_noise = load_prior_decisions()
    legacy_selection_hold_inventories = {
        row.get("inventory", "")
        for row in (read_csv(HOLDS) if HOLDS.exists() else [])
        if row.get("hold_reason") == "prior_human_selection_not_in_complete_visual_group"
        and row.get("inventory", "")
    }
    prior_visual_selected: dict[str, dict[str, str]] = {}
    prior_visual_noise: set[str] = set()
    if ORPHAN_DECISIONS.exists():
        for row in read_csv(ORPHAN_DECISIONS):
            group_id = row.get("record_key", "").removeprefix("orphan:")
            if not group_id.startswith("visual-"):
                continue
            if row.get("decision") == "selected_master":
                prior_visual_selected[group_id] = row
            elif row.get("decision") == "noise":
                prior_visual_noise.add(group_id)
    protected = protected_selections()

    error_by_inventory: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    no_inventory_errors: list[dict[str, Any]] = []
    for row in decode_errors:
        inv = inventory(row["filename"])
        if inv:
            error_by_inventory[inv].append(row)
        else:
            no_inventory_errors.append(row)

    all_unmatched_inventories = {inventory(row["filename"]) for row in unmatched if inventory(row["filename"])}
    identity_conflicts = all_unmatched_inventories & set(institution_by_inventory)
    md_bridge_conflicts: set[str] = set()
    protected_conflicts: set[str] = set()
    for inv, asset_ids in related.items():
        paths = {by_id[asset_id]["source_path"] for asset_id in asset_ids}
        if asset_ids & md_selected_asset_ids or paths & md_selected_paths:
            md_bridge_conflicts.add(inv)
        prior_path = prior_selected.get(inv, {}).get("selected_source_path", "")
        foreign_protected = (paths & protected) - ({prior_path} if prior_path else set())
        if foreign_protected:
            protected_conflicts.add(inv)

    held_inventories = (
        ambiguous_inventories
        | identity_conflicts
        | md_bridge_conflicts
        | protected_conflicts
        | set(error_by_inventory)
        | legacy_selection_hold_inventories
    )
    clear = all_unmatched_inventories - held_inventories
    metadata = load_metadata(clear)
    prepared_at = now()

    decisions: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    accession: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    selected_by_inventory: dict[str, dict[str, Any]] = {}
    selected_paths: set[str] = set()

    for row in md_matches:
        inv = inventory(row["filename"])
        dispositions.append({
            "record_key": "md:" + row["matched_md_object_ids"].split(";", 1)[0].strip(),
            "inventory": inv,
            "asset_id": row["asset_id"], "source_path": row["source_path"],
            "extension": row["extension"], "width_px": row["width_px"],
            "height_px": row["height_px"], "size_bytes": row["size_bytes"],
            "preservation_status": "redundant",
            "preservation_basis": "visual_match_to_current_md_record",
            "drain_state": "proposed_redundant", "deletion_authorized": "false",
        })

    for inv in sorted(clear):
        asset_ids = related.get(inv, set())
        collision_group = resolvable_collision_groups.get(inv)
        collision_visual_group = collision_group["visual_group_id"] if collision_group else ""
        candidates = [by_id[value] for value in asset_ids if by_id[value]["extension"] in TIFF_EXTENSIONS]
        if not candidates:
            held_inventories.add(inv)
            holds.append({
                "hold_id": "hold:" + inv, "inventory": inv, "inventory_members": inv,
                "asset_id": "", "source_path": "", "filename": "", "extension": "",
                "width_px": 0, "height_px": 0, "size_bytes": 0,
                "hold_reason": "no_decodable_tiff_candidate", "error": "",
                "deletion_authorized": "false",
            })
            for asset in (row for row in unmatched if inventory(row["filename"]) == inv):
                dispositions.append({
                    "record_key": "orphan:" + inv, "inventory": inv,
                    "asset_id": asset["asset_id"], "source_path": asset["source_path"],
                    "extension": asset["extension"], "width_px": asset["width_px"],
                    "height_px": asset["height_px"], "size_bytes": asset["size_bytes"],
                    "preservation_status": "unresolved",
                    "preservation_basis": "no_decodable_tiff_candidate",
                    "drain_state": "pending_next_pass", "deletion_authorized": "false",
                })
            continue
        if inv in prior_noise or collision_visual_group in prior_visual_noise:
            for asset_id in sorted(asset_ids):
                asset = by_id[asset_id]
                dispositions.append({
                    "record_key": "orphan:" + inv, "inventory": inv, "asset_id": asset_id,
                    "source_path": asset["source_path"], "extension": asset["extension"],
                    "width_px": asset["width_px"], "height_px": asset["height_px"],
                    "size_bytes": asset["size_bytes"], "preservation_status": "noise",
                    "preservation_basis": "preserved_prior_human_noise_decision",
                    "drain_state": "proposed_noise", "deletion_authorized": "false",
                })
            continue
        prior = prior_selected.get(inv) or prior_visual_selected.get(collision_visual_group)
        chosen = None
        basis = "largest_decodable_tiff_by_pixel_area_then_size; stable_path_tiebreak; mtime_not_used"
        if prior:
            chosen = next(
                (row for row in candidates if row["source_path"] == prior.get("selected_source_path")),
                None,
            )
            if chosen is None:
                held_inventories.add(inv)
                holds.append({
                    "hold_id": "hold:" + inv, "inventory": inv, "inventory_members": inv,
                    "asset_id": "", "source_path": prior.get("selected_source_path", ""),
                    "filename": Path(prior.get("selected_source_path", "")).name,
                    "extension": Path(prior.get("selected_source_path", "")).suffix.casefold().lstrip("."),
                    "width_px": 0, "height_px": 0, "size_bytes": 0,
                    "hold_reason": "prior_human_selection_not_in_complete_visual_group", "error": "",
                    "deletion_authorized": "false",
                })
                for asset in (row for row in unmatched if inventory(row["filename"]) == inv):
                    dispositions.append({
                        "record_key": "orphan:" + inv, "inventory": inv,
                        "asset_id": asset["asset_id"], "source_path": asset["source_path"],
                        "extension": asset["extension"], "width_px": asset["width_px"],
                        "height_px": asset["height_px"], "size_bytes": asset["size_bytes"],
                        "preservation_status": "unresolved",
                        "preservation_basis": "prior_human_selection_not_in_complete_visual_group",
                        "drain_state": "pending_human_confirmation",
                        "deletion_authorized": "false",
                    })
                continue
            basis = "preserved_prior_human_selected_master"
        if chosen is None:
            chosen = sorted(
                candidates,
                key=lambda row: (
                    -(int(row["width_px"]) * int(row["height_px"])),
                    -int(row["size_bytes"]),
                    str(row["source_path"]),
                ),
            )[0]
        if chosen["source_path"] in selected_paths:
            raise ReconciliationError(f"one path selected for multiple local objects: {chosen['source_path']}")
        selected_paths.add(chosen["source_path"])
        selected_by_inventory[inv] = chosen
        meta = metadata.get(inv, {})
        metadata_state = "historical_working_metadata_applied" if meta else "metadata_deferred_next_pass"
        accession_state = "ready_for_accession_candidate" if meta else "master_selected_metadata_deferred"
        decisions.append({
            "record_key": "local:" + inv, "inventory": inv, "decision": "selected_master",
            "selected_asset_id": chosen["asset_id"], "selected_source_path": chosen["source_path"],
            "selected_width_px": chosen["width_px"], "selected_height_px": chosen["height_px"],
            "selected_size_bytes": chosen["size_bytes"], "candidate_path_count": len(asset_ids),
            "selection_basis": basis, "metadata_state": metadata_state,
            "accession_candidate_state": accession_state,
            "decided_by": prior.get("decided_by", "") if prior else ACTOR,
            "decided_at": prior.get("decided_at", "") if prior else prepared_at,
            "deletion_authorized": "false",
        })
        provenance = f"{chosen['source_path']} -> local:{inv} -> pending accession"
        accession.append({
            "record_key": "local:" + inv, "inventory": inv,
            "selected_asset_id": chosen["asset_id"], "selected_master_path": chosen["source_path"],
            "extension": chosen["extension"], "width_px": chosen["width_px"],
            "height_px": chosen["height_px"], "size_bytes": chosen["size_bytes"],
            "title": clean(meta.get("title")), "description": clean(meta.get("description")),
            "date_text": clean(meta.get("date_text")), "object_type": clean(meta.get("object_type")),
            "material": clean(meta.get("material")), "dimensions": clean(meta.get("dimensions")),
            "collection_text": clean(meta.get("collection_text")),
            "historical_md_id": clean(meta.get("historical_md_id")),
            "metadata_state": metadata_state, "metadata_source": clean(meta.get("preferred_source")),
            "metadata_source_row": clean(meta.get("preferred_row")), "provenance_chain": provenance,
            "accession_candidate_state": accession_state, "final_path": "", "final_hash": "",
            "deletion_authorized": "false",
        })
        metadata_rows.append({
            "record_key": "local:" + inv, "inventory": inv,
            "title": clean(meta.get("title")), "description": clean(meta.get("description")),
            "date_text": clean(meta.get("date_text")), "object_type": clean(meta.get("object_type")),
            "material": clean(meta.get("material")), "dimensions": clean(meta.get("dimensions")),
            "collection_text": clean(meta.get("collection_text")),
            "historical_md_id": clean(meta.get("historical_md_id")),
            "metadata_state": metadata_state, "preferred_source": clean(meta.get("preferred_source")),
            "preferred_row": clean(meta.get("preferred_row")), "source_count": clean(meta.get("source_count")),
            "source_rows_json": clean(meta.get("source_rows_json")),
        })
        for asset_id in sorted(asset_ids):
            asset = by_id[asset_id]
            selected = asset_id == chosen["asset_id"]
            dispositions.append({
                "record_key": "local:" + inv, "inventory": inv, "asset_id": asset_id,
                "source_path": asset["source_path"], "extension": asset["extension"],
                "width_px": asset["width_px"], "height_px": asset["height_px"],
                "size_bytes": asset["size_bytes"],
                "preservation_status": "selected_master" if selected else "redundant",
                "preservation_basis": basis if selected else "visual_redundant_after_single_master_selection",
                "drain_state": "retained_master" if selected else "proposed_redundant",
                "deletion_authorized": "false",
            })

    def append_hold(inv: str, reason: str, members: str = "") -> None:
        relevant = [row for row in unmatched if inventory(row["filename"]) == inv]
        relevant.extend(error_by_inventory.get(inv, []))
        for row in relevant or [{
            "asset_id": "", "source_path": "", "filename": "", "extension": "",
            "width_px": 0, "height_px": 0, "size_bytes": 0, "error": "",
        }]:
            holds.append({
                "hold_id": "hold:" + inv, "inventory": inv, "inventory_members": members or inv,
                "asset_id": row["asset_id"], "source_path": row["source_path"],
                "filename": row["filename"], "extension": row["extension"],
                "width_px": row["width_px"], "height_px": row["height_px"],
                "size_bytes": row["size_bytes"], "hold_reason": reason,
                "error": row.get("error", ""), "deletion_authorized": "false",
            })
            if row["source_path"]:
                dispositions.append({
                    "record_key": "orphan:" + inv, "inventory": inv, "asset_id": row["asset_id"],
                    "source_path": row["source_path"], "extension": row["extension"],
                    "width_px": row["width_px"], "height_px": row["height_px"],
                    "size_bytes": row["size_bytes"], "preservation_status": "unresolved",
                    "preservation_basis": reason, "drain_state": "pending_next_pass",
                    "deletion_authorized": "false",
                })

    for inv in sorted(identity_conflicts):
        append_hold(inv, "current_md_inventory_identity_without_confirming_visual_match")
    for inv in sorted(md_bridge_conflicts - identity_conflicts):
        append_hold(inv, "visual_group_reaches_existing_md_selected_master")
    for inv in sorted(protected_conflicts - identity_conflicts - md_bridge_conflicts):
        append_hold(inv, "visual_group_contains_protected_prior_selected_path")
    for inv in sorted(set(error_by_inventory) - identity_conflicts - md_bridge_conflicts - protected_conflicts):
        append_hold(inv, "contains_visual_decode_error_tiff")
    for inv in sorted(
        legacy_selection_hold_inventories
        - identity_conflicts - md_bridge_conflicts - protected_conflicts - set(error_by_inventory)
    ):
        append_hold(inv, "prior_human_selection_not_in_complete_visual_group")

    unmatched_without_inventory = [row for row in unmatched if not inventory(row["filename"])]
    for row in unmatched_without_inventory + no_inventory_errors:
        holds.append({
            "hold_id": "hold:unparsed:" + row["asset_id"], "inventory": "",
            "inventory_members": "", "asset_id": row["asset_id"],
            "source_path": row["source_path"], "filename": row["filename"],
            "extension": row["extension"], "width_px": row["width_px"],
            "height_px": row["height_px"], "size_bytes": row["size_bytes"],
            "hold_reason": "nonstandard_or_ambiguous_inventory_filename",
            "error": row.get("error", ""), "deletion_authorized": "false",
        })
        dispositions.append({
            "record_key": "", "inventory": "", "asset_id": row["asset_id"],
            "source_path": row["source_path"], "extension": row["extension"],
            "width_px": row["width_px"], "height_px": row["height_px"],
            "size_bytes": row["size_bytes"], "preservation_status": "unresolved",
            "preservation_basis": "nonstandard_or_ambiguous_inventory_filename",
            "drain_state": "pending_next_pass", "deletion_authorized": "false",
        })

    workbench: list[dict[str, Any]] = []
    remaining_collision_groups = list(unresolved_collision_groups)
    remaining_collision_groups.extend(
        group for inv, group in resolvable_collision_groups.items()
        if inv not in selected_by_inventory
        and inv not in prior_noise
        and group["visual_group_id"] not in prior_visual_noise
    )
    unresolved_group_ids = {group["visual_group_id"] for group in unresolved_collision_groups}
    for group in remaining_collision_groups:
        members = group["filename_inventories"] or group["component_members"]
        rows = group["rows"]
        for row in sorted(rows, key=lambda item: item["source_path"]):
            workbench.append({
                "normalized_inventory": group["component_id"], "inventory_members": "; ".join(members),
                "visual_group_id": group["visual_group_id"], "asset_id": row["asset_id"],
                "source_path": row["source_path"], "filename": row["filename"],
                "extension": row["extension"], "size_bytes": row["size_bytes"],
                "mtime_ns": row["mtime_ns"], "width_px": row["width_px"],
                "height_px": row["height_px"], "ahash": row["ahash"],
                "dhash": row["dhash"], "phash": row["phash"],
                "exact_visual_group_tiff_count": len(rows),
                "classification_basis": "cross_inventory_visual_collision_requires_human_identity_decision",
                "review_state": "unresolved_visual_identity_group",
                "deletion_authorized": "false",
            })
        if group["visual_group_id"] in unresolved_group_ids:
            for inv in members:
                append_hold(inv, "cross_inventory_visual_collision", "; ".join(members))

    # One source path receives one disposition. Selected and unresolved paths
    # are never allowed to be shadowed by a redundant proposal.
    rank = {"selected_master": 4, "unresolved": 3, "noise": 2, "redundant": 1}
    disposition_by_path: dict[str, dict[str, Any]] = {}
    for row in dispositions:
        path = row["source_path"]
        prior = disposition_by_path.get(path)
        if prior is None or rank[row["preservation_status"]] > rank[prior["preservation_status"]]:
            disposition_by_path[path] = row
    dispositions = sorted(disposition_by_path.values(), key=lambda row: row["source_path"])
    drain = [row for row in dispositions if row["preservation_status"] == "redundant"]

    folder_paths = {row["source_path"] for row in folder + decode_errors}
    accounted = folder_paths & set(disposition_by_path)
    if accounted != folder_paths:
        missing = sorted(folder_paths - accounted)
        raise ReconciliationError(f"reference folder paths are not fully accounted: {missing[:10]}")
    if len(selected_paths) != len(decisions):
        raise ReconciliationError("one-Master uniqueness failed")
    if any(row["deletion_authorized"] != "false" for row in dispositions + decisions + holds):
        raise ReconciliationError("deletion authorization invariant failed")

    return {
        "mount": mount, "prepared_at": prepared_at, "assets": assets, "folder": folder,
        "decode_errors": decode_errors, "md_matches": md_matches, "unmatched": unmatched,
        "related": related, "decisions": sorted(decisions, key=lambda row: row["inventory"]),
        "dispositions": dispositions, "drain": drain,
        "accession": sorted(accession, key=lambda row: row["inventory"]),
        "metadata": sorted(metadata_rows, key=lambda row: row["inventory"]),
        "holds": sorted(holds, key=lambda row: (row["hold_reason"], row["inventory"], row["source_path"])),
        "workbench": sorted(workbench, key=lambda row: (row["normalized_inventory"], row["source_path"])),
        "identity_conflicts": identity_conflicts, "md_bridge_conflicts": md_bridge_conflicts,
        "protected_conflicts": protected_conflicts, "ambiguous_inventories": ambiguous_inventories,
        "missing_inventory_indexed": missing_inventory,
    }


def candidate_id_for(connection: sqlite3.Connection, record_key: str, asset: dict[str, Any]) -> int:
    existing = connection.execute(
        "SELECT candidate_id FROM candidates WHERE record_key=? AND source_path=?",
        (record_key, asset["source_path"]),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO candidates(
                record_key,inventory,source_path,source_kind,original_index_path,basename,extension,
                size_bytes,mtime_ns,file_kind,proposed_role,match_basis,reused_source_sha256,
                prior_unit,prior_disposition
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_key, record_key.removeprefix("local:"), asset["source_path"], "read_only_usb", "",
                asset["filename"], "." + asset["extension"], asset["size_bytes"], asset["mtime_ns"],
                "asset", "master_candidate" if asset["extension"] in TIFF_EXTENSIONS else "derivative_candidate",
                "reference_photo_visual_similarity", "", "", "",
            ),
        )
        candidate_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        candidate_id = int(existing[0])
    connection.execute(
        """
        INSERT OR REPLACE INTO local_visual_hashes(
            candidate_id,size_bytes,mtime_ns,ahash_64,dhash_64,phash_64,decode_status
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            candidate_id, asset["size_bytes"], asset["mtime_ns"], asset["ahash"], asset["dhash"],
            asset["phash"], "ok",
        ),
    )
    is_tiff = asset["extension"] in TIFF_EXTENSIONS
    file_type = "TIFF" if is_tiff else asset["extension"].upper()
    mime_type = "image/tiff" if is_tiff else "image/" + asset["extension"].replace("jpg", "jpeg")
    connection.execute(
        """
        INSERT OR REPLACE INTO local_candidate_technical(
            candidate_id,record_key,source_path,extension,width_px,height_px,file_type,mime_type,
            header_status,accession_eligible,eligibility_basis,embedded_metadata_json,technical_error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            candidate_id, record_key, asset["source_path"], "." + asset["extension"],
            asset["width_px"], asset["height_px"], file_type, mime_type, "read", int(is_tiff),
            "TIFF with readable visual-index dimensions" if is_tiff else
            "related derivative; derivatives are generated during accession",
            json.dumps({
                "ImageWidth": asset["width_px"], "ImageHeight": asset["height_px"],
                "FileType": file_type, "MIMEType": mime_type,
            }, ensure_ascii=False, separators=(",", ":")),
            "",
        ),
    )
    return candidate_id


def core_disposition(row: dict[str, Any], existing: sqlite3.Row | None) -> dict[str, Any]:
    result = dict(existing) if existing is not None else {
        "source_path": row["source_path"], "source_kind": "file", "unit": "tfa",
        "inventory": row["inventory"], "source_role": "master_candidate", "final_path": "",
        "preservation_status": "not_assessed", "preservation_basis": "reference_photo_reconciliation",
        "source_sha256": "", "final_sha256": "", "source_size_bytes": row["size_bytes"],
        "exact_copy_proven": "false", "drain_state": "needs_reconciliation",
        "deletion_authorized": "false", "notes": "",
    }
    result.update({
        "source_path": row["source_path"], "inventory": row["inventory"],
        "source_role": "master_candidate" if row["extension"] in TIFF_EXTENSIONS else "derivative_candidate",
        "preservation_status": row["preservation_status"],
        "preservation_basis": row["preservation_basis"], "source_size_bytes": row["size_bytes"],
        "exact_copy_proven": "false", "drain_state": row["drain_state"],
        "deletion_authorized": "false",
        "notes": "Reference-photo visual reconciliation; source remains read-only; deletion is not authorized.",
    })
    return result


def apply_projection(projected: dict[str, Any]) -> dict[str, Any]:
    if workbench_listening():
        raise ReconciliationError("stop port 8768 before applying reference-photo reconciliation")
    prior_visual_decisions = {
        row.get("record_key", ""): row
        for row in (read_csv(ORPHAN_DECISIONS) if ORPHAN_DECISIONS.exists() else [])
        if row.get("record_key", "").startswith("orphan:visual-")
        and row.get("deletion_authorized") == "false"
    }
    for row in projected["decisions"]:
        path = Path(row["selected_source_path"])
        stat = path.stat()
        if stat.st_size != int(row["selected_size_bytes"]):
            raise ReconciliationError(f"selected source size drifted: {path}")
        asset = next(item for item in projected["assets"] if item["asset_id"] == row["selected_asset_id"])
        if stat.st_mtime_ns != int(asset["mtime_ns"]):
            raise ReconciliationError(f"selected source mtime drifted: {path}")
    source_mount()
    stamp = projected["prepared_at"].replace("-", "").replace(":", "")
    backup_targets = (
        DATABASE, CORE_LOCAL_DECISIONS, CORE_LOCAL_DISPOSITIONS, CORE_SOURCE_DISPOSITION,
        CORE_DRAIN_LIST, ORPHAN_DECISIONS, ORPHAN_DISPOSITIONS, MASTER_DECISIONS,
        SOURCE_DISPOSITION, DRAIN_LIST, ACCESSION_CANDIDATES, METADATA_OUTPUT, HOLDS,
        WORKBENCH_HOLDS, RECEIPT,
    )
    backups = [value for value in (backup(path, stamp) for path in backup_targets) if value]
    asset_by_id = {row["asset_id"]: row for row in projected["assets"]}
    accession_by_inventory = {row["inventory"]: row for row in projected["accession"]}

    with closing(connect(DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fields = [str(row["name"]) for row in connection.execute("PRAGMA table_info(source_disposition_proposal)")]
        connection.execute("CREATE TEMP TABLE reference_scope_paths(path TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO reference_scope_paths(path) VALUES(?)",
            ((row["source_path"],) for row in projected["dispositions"]),
        )
        connection.execute(
            "DELETE FROM drain_list_proposal WHERE source_path IN (SELECT path FROM reference_scope_paths)"
        )
        candidate_ids: dict[tuple[str, str], int] = {}
        for decision in projected["decisions"]:
            inv = decision["inventory"]
            record_key = "local:" + inv
            meta = accession_by_inventory[inv]
            prior = connection.execute("SELECT * FROM objects WHERE record_key=?", (record_key,)).fetchone()
            md_id = str(prior["md_object_id"]) if prior is not None else meta["historical_md_id"]
            md_url = str(prior["md_url"]) if prior is not None else (
                f"https://berlin.museum-digital.de/object/{md_id}" if md_id else ""
            )
            connection.execute(
                """
                INSERT INTO objects(
                    record_key,md_object_id,inventory,title,description,date_text,object_type,material,
                    dimensions,places,tags,creator,md_url,collection_ids,collections,subdivision_ids,
                    subdivisions,last_updated,record_state,decision_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                    title=excluded.title,description=excluded.description,date_text=excluded.date_text,
                    object_type=excluded.object_type,material=excluded.material,dimensions=excluded.dimensions,
                    record_state='local_tfa_review'
                """,
                (
                    record_key, md_id, inv, meta["title"], meta["description"], meta["date_text"],
                    meta["object_type"], meta["material"], meta["dimensions"], "", "", "", md_url,
                    "", meta["collection_text"], "", "", "", "local_tfa_review", "unresolved",
                ),
            )
            for asset_id in projected["related"][inv]:
                asset = asset_by_id[asset_id]
                cid = candidate_id_for(connection, record_key, asset)
                candidate_ids[(inv, asset_id)] = cid
            selected_id = candidate_ids[(inv, decision["selected_asset_id"])]
            connection.execute(
                """
                INSERT INTO local_object_decisions(
                    record_key,outcome,selected_candidate_id,matched_md_object_id,title,description,
                    date_text,object_type,material,dimensions,note,decided_by,decided_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                    outcome=excluded.outcome,selected_candidate_id=excluded.selected_candidate_id,
                    matched_md_object_id=NULL,title=excluded.title,description=excluded.description,
                    date_text=excluded.date_text,object_type=excluded.object_type,
                    material=excluded.material,dimensions=excluded.dimensions,note=excluded.note,
                    decided_by=excluded.decided_by,decided_at=excluded.decided_at
                """,
                (
                    record_key, "new_local_object", selected_id, None, meta["title"], meta["description"],
                    meta["date_text"], meta["object_type"], meta["material"], meta["dimensions"],
                    decision["selection_basis"], decision["decided_by"], decision["decided_at"],
                ),
            )

        for row in projected["dispositions"]:
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?", (row["source_path"],)
            ).fetchone()
            core = core_disposition(row, existing)
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES(" + ",".join("?" for _ in fields) + ")",
                [core[field] for field in fields],
            )
            if row["preservation_status"] == "redundant":
                connection.execute(
                    "INSERT INTO drain_list_proposal VALUES(" + ",".join("?" for _ in fields) + ")",
                    [core[field] for field in fields],
                )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_reference_photo_reconciliation_receipt',?)",
            (str(RECEIPT),),
        )
        technical_count = connection.execute(
            "SELECT COUNT(*) FROM local_candidate_technical"
        ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_candidate_technical_count',?)",
            (str(technical_count),),
        )
        connection.commit()

    with closing(connect(DATABASE, read_only=True)) as connection:
        local_decisions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT d.*,o.inventory,COALESCE(c.source_path,'') selected_source_path
                FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
                LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                ORDER BY o.inventory COLLATE NOCASE
                """
            )
        ]
        local_dispositions = [
            {
                "record_key": row["record_key"], "inventory": row["inventory"],
                "outcome": "new_local_object", "matched_md_object_id": "",
                "candidate_id": candidate_ids.get((row["inventory"], row["asset_id"]), ""),
                "source_path": row["source_path"], "extension": "." + row["extension"],
                "size_bytes": row["size_bytes"], "disposition": row["preservation_status"],
                "disposition_basis": row["preservation_basis"], "deletion_authorized": "false",
            }
            for row in projected["dispositions"]
            if row["record_key"].startswith("local:")
        ]
        source_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM source_disposition_proposal ORDER BY source_path"
        )]
        drain_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM drain_list_proposal ORDER BY source_path"
        )]
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        duplicate_selected = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT selected_candidate_id,COUNT(*) n FROM local_object_decisions
                WHERE outcome='new_local_object' GROUP BY selected_candidate_id HAVING n>1
            )
            """
        ).fetchone()[0]
        catalogue_local_objects = connection.execute(
            "SELECT COUNT(*) FROM objects WHERE record_state='local_tfa_review'"
        ).fetchone()[0]
        catalogue_local_candidates = connection.execute(
            "SELECT COUNT(*) FROM local_candidate_technical"
        ).fetchone()[0]
        catalogue_drain_rows = connection.execute(
            "SELECT COUNT(*) FROM drain_list_proposal"
        ).fetchone()[0]
    if quick != "ok" or foreign or duplicate_selected:
        raise ReconciliationError(
            f"post-apply validation failed: quick={quick} foreign={len(foreign)} duplicate_selected={duplicate_selected}"
        )

    atomic_csv(MASTER_DECISIONS, MASTER_FIELDS, projected["decisions"])
    atomic_csv(SOURCE_DISPOSITION, SOURCE_FIELDS, projected["dispositions"])
    atomic_csv(DRAIN_LIST, SOURCE_FIELDS, projected["drain"])
    atomic_csv(ACCESSION_CANDIDATES, ACCESSION_FIELDS, projected["accession"])
    atomic_csv(METADATA_OUTPUT, METADATA_FIELDS, projected["metadata"])
    atomic_csv(HOLDS, HOLD_FIELDS, projected["holds"])
    atomic_csv(WORKBENCH_HOLDS, WORKBENCH_FIELDS, projected["workbench"])
    atomic_csv(
        CORE_LOCAL_DECISIONS,
        (
            "record_key", "outcome", "selected_candidate_id", "matched_md_object_id", "title",
            "description", "date_text", "object_type", "material", "dimensions", "note",
            "decided_by", "decided_at", "inventory", "selected_source_path",
        ),
        local_decisions,
    )
    atomic_csv(
        CORE_LOCAL_DISPOSITIONS,
        (
            "record_key", "inventory", "outcome", "matched_md_object_id", "candidate_id",
            "source_path", "extension", "size_bytes", "disposition", "disposition_basis",
            "deletion_authorized",
        ),
        local_dispositions,
    )
    atomic_csv(CORE_SOURCE_DISPOSITION, source_rows[0].keys() if source_rows else (), source_rows)
    atomic_csv(CORE_DRAIN_LIST, drain_rows[0].keys() if drain_rows else (), drain_rows)
    workbench_by_visual: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in projected["workbench"]:
        workbench_by_visual[row["visual_group_id"]].append(row)
    orphan_decision_rows: list[dict[str, str]] = []
    orphan_decision_by_group: dict[str, dict[str, str]] = {}
    for visual_group, rows in sorted(workbench_by_visual.items()):
        record_key = "orphan:" + visual_group
        prior = prior_visual_decisions.get(record_key, {})
        selected = next(
            (row for row in rows if row["asset_id"] == prior.get("selected_asset_id", "")),
            None,
        )
        if prior.get("decision") == "selected_master" and selected is not None:
            state = "selected_master"
        elif prior.get("decision") == "noise":
            state = "noise"
        else:
            state = "unresolved"
            selected = None
        decision = {
            "record_key": record_key, "inventory": visual_group, "decision": state,
            "selected_asset_id": selected["asset_id"] if selected else "",
            "selected_visual_group_id": visual_group if selected else "",
            "selected_source_path": selected["source_path"] if selected else "",
            "note": prior.get("note", "") or (
                "Cross-inventory filename evidence: " + rows[0]["inventory_members"]
            ),
            "decided_by": prior.get("decided_by", "") or ACTOR,
            "decided_at": prior.get("decided_at", "") or projected["prepared_at"],
            "deletion_authorized": "false",
        }
        orphan_decision_rows.append(decision)
        orphan_decision_by_group[visual_group] = decision
    atomic_csv(
        ORPHAN_DECISIONS,
        (
            "record_key", "inventory", "decision", "selected_asset_id", "selected_visual_group_id",
            "selected_source_path", "note", "decided_by", "decided_at", "deletion_authorized",
        ),
        orphan_decision_rows,
    )
    atomic_csv(
        ORPHAN_DISPOSITIONS,
        (
            "record_key", "inventory", "visual_group_id", "asset_id", "source_path", "extension",
            "size_bytes", "preservation_status", "preservation_basis", "drain_state",
            "deletion_authorized",
        ),
        ({
            "record_key": "orphan:" + row["visual_group_id"],
            "inventory": row["visual_group_id"], "visual_group_id": row["visual_group_id"],
            "asset_id": row["asset_id"], "source_path": row["source_path"],
            "extension": row["extension"], "size_bytes": row["size_bytes"],
            "preservation_status": (
                "selected_master"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                and orphan_decision_by_group[row["visual_group_id"]]["selected_asset_id"] == row["asset_id"]
                else "redundant"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                else "noise"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "noise"
                else "unresolved"
            ),
            "preservation_basis": (
                "human_selected_single_tiff_master"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                and orphan_decision_by_group[row["visual_group_id"]]["selected_asset_id"] == row["asset_id"]
                else "redundant_after_single_master_selection"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                else "human_dismissed_visual_object_as_noise"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "noise"
                else "awaiting_human_visual_object_review"
            ),
            "drain_state": (
                "retained_master"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                and orphan_decision_by_group[row["visual_group_id"]]["selected_asset_id"] == row["asset_id"]
                else "proposed_redundant"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "selected_master"
                else "proposed_noise"
                if orphan_decision_by_group[row["visual_group_id"]]["decision"] == "noise"
                else "pending_human_confirmation"
            ),
            "deletion_authorized": "false",
        } for row in projected["workbench"]),
    )

    receipt = {
        "format": "iss-tfa-reference-photo-reconciliation-v1", "version": VERSION,
        "result": "ok", "mode": "apply", "applied_at": projected["prepared_at"],
        "source_mount": projected["mount"], "source_read_only": True,
        "source_files_modified": False, "deletion_authorized": False,
        "final_hashes_generated": False, "masters_copied": False,
        "selection_authority": "user_directed_best_decodable_tiff",
        "best_candidate_policy": "largest pixel area, then file size, then stable path; mtime excluded",
        "metadata_used_for_visual_matching": False, "filename_used_for_visual_matching": False,
        "filename_used_for_inventory_routing": True,
        "reference_tiff_paths": EXPECTED_REFERENCE_TIFFS,
        "reference_tiffs_indexed": len(projected["folder"]),
        "reference_tiff_decode_errors": len(projected["decode_errors"]),
        "reference_tiffs_visually_redundant_to_md": len(projected["md_matches"]),
        "local_objects_with_selected_master": len(projected["decisions"]),
        "selection_basis_counts": dict(sorted(Counter(
            row["selection_basis"] for row in projected["decisions"]
        ).items())),
        "local_objects_with_metadata_applied": sum(
            row["metadata_state"] == "historical_working_metadata_applied" for row in projected["decisions"]
        ),
        "local_objects_metadata_deferred": sum(
            row["metadata_state"] == "metadata_deferred_next_pass" for row in projected["decisions"]
        ),
        "local_redundant_paths": sum(
            row["record_key"].startswith("local:") and row["preservation_status"] == "redundant"
            for row in projected["dispositions"]
        ),
        "current_md_identity_conflict_inventories": len(projected["identity_conflicts"]),
        "md_bridge_conflict_inventories": len(projected["md_bridge_conflicts"]),
        "protected_selection_conflict_inventories": len(projected["protected_conflicts"]),
        "cross_inventory_ambiguous_inventories": len(projected["ambiguous_inventories"]),
        "workbench_visual_collision_components": len({row["normalized_inventory"] for row in projected["workbench"]}),
        "workbench_candidate_paths": len(projected["workbench"]),
        "workbench_visual_groups": len({row["visual_group_id"] for row in projected["workbench"]}),
        "hold_rows": len(projected["holds"]), "drain_rows_in_scope": len(projected["drain"]),
        "hold_reason_counts": dict(sorted(Counter(
            row["hold_reason"] for row in projected["holds"]
        ).items())),
        "drain_contains_only_redundant": all(
            row["preservation_status"] == "redundant" for row in projected["drain"]
        ),
        "sqlite_quick_check": quick, "sqlite_foreign_key_violations": len(foreign),
        "catalogue_local_objects": catalogue_local_objects,
        "catalogue_local_candidates": catalogue_local_candidates,
        "catalogue_drain_rows": catalogue_drain_rows,
        "backups": backups,
        "outputs": {
            "master_decisions": str(MASTER_DECISIONS), "source_disposition": str(SOURCE_DISPOSITION),
            "drain_list": str(DRAIN_LIST), "accession_candidates": str(ACCESSION_CANDIDATES),
            "metadata": str(METADATA_OUTPUT), "holds": str(HOLDS),
            "workbench_holds": str(WORKBENCH_HOLDS), "sqlite": str(DATABASE),
        },
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def dry_summary(projected: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "iss-tfa-reference-photo-reconciliation-dry-run-v1", "version": VERSION,
        "result": "ok", "mode": "dry-run", "source_read_only": True,
        "reference_tiff_paths": EXPECTED_REFERENCE_TIFFS,
        "indexed": len(projected["folder"]), "decode_errors": len(projected["decode_errors"]),
        "md_visual_redundant": len(projected["md_matches"]),
        "selected_local_masters": len(projected["decisions"]),
        "selection_basis_counts": dict(sorted(Counter(
            row["selection_basis"] for row in projected["decisions"]
        ).items())),
        "metadata_applied": sum(
            row["metadata_state"] == "historical_working_metadata_applied" for row in projected["decisions"]
        ),
        "metadata_deferred": sum(
            row["metadata_state"] == "metadata_deferred_next_pass" for row in projected["decisions"]
        ),
        "identity_conflicts": len(projected["identity_conflicts"]),
        "md_bridge_conflicts": len(projected["md_bridge_conflicts"]),
        "protected_selection_conflicts": len(projected["protected_conflicts"]),
        "ambiguous_inventories": len(projected["ambiguous_inventories"]),
        "workbench_components": len({row["normalized_inventory"] for row in projected["workbench"]}),
        "metadata_inventories": [
            row["inventory"] for row in projected["decisions"]
            if row["metadata_state"] == "historical_working_metadata_applied"
        ],
        "hold_reason_counts": dict(sorted(Counter(
            row["hold_reason"] for row in projected["holds"]
        ).items())),
        "hold_rows": len(projected["holds"]), "scope_disposition_rows": len(projected["dispositions"]),
        "scope_drain_rows": len(projected["drain"]), "writes_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and VISUAL_CONSOLIDATION_RECEIPT.is_file():
        consolidated = json.loads(VISUAL_CONSOLIDATION_RECEIPT.read_text(encoding="utf-8"))
        if (
            consolidated.get("result") == "ok"
            and consolidated.get("source_files_modified") is False
            and consolidated.get("deletion_authorized") is False
        ):
            raise ReconciliationError(
                "completed visual review is already consolidated; base projection apply is guarded"
            )
    projected = build_projection()
    result = apply_projection(projected) if args.apply else dry_summary(projected)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
