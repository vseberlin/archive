#!/usr/bin/env python3
"""Rebuild the TFA local queue from visual evidence only.

The 5,282 reconciled museum-digital objects are anchored to their selected
TIFF Masters.  Images that conservatively match one of those Masters are
recorded as redundant and proposed for drain.  Indexed TIFFs with no such
match become local-only visual candidates.  Metadata and spreadsheet values
never participate in classification.  Filename numbers may route a bounded
crop-aware comparison, but only the resulting visual hashes can establish a
match.

No source file is changed or deleted.  All writes stay in the existing TFA
work root, and every drain row keeps deletion_authorized=false.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import socket
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np


VERSION = "2.1.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WF_ROOT = SOURCE_ROOT / "restore/archive/WF"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
BASELINE_DATABASE = REVIEW_ROOT / "reconciliation.pre-full-wf-md-redundancy-20260816T070035Z.sqlite"
LEGACY_DECISION_AUDIT_DATABASE = REVIEW_ROOT / "reconciliation.pre-visual-only-20260816T080948Z.sqlite"
FULL_INDEX = WORK_ROOT / "work/reconciliation/wf-full-visual-index/wf-full-visual-index.sqlite"
FULL_COMPLETION = WORK_ROOT / "work/reconciliation/wf-full-visual-index/completions/afe749deaf74db6ae802721637125540.json"
ARCHIVE_VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
INSTITUTION_VISUAL_CSV = REVIEW_ROOT / "institution-md-visual-references.csv"
INSTITUTION_VISUAL_RECEIPT = REVIEW_ROOT / "institution-md-visual-index.json"
CROP_VISUAL_CSV = REVIEW_ROOT / "visual-only-crop-verified-redundant.csv"
CROP_VISUAL_RECEIPT = REVIEW_ROOT / "visual-only-crop-audit.json"

SOURCE_CSV = REVIEW_ROOT / "source-disposition-proposal.csv"
DRAIN_CSV = REVIEW_ROOT / "drain-list-proposal.csv"
LOCAL_DECISIONS_CSV = REVIEW_ROOT / "local-object-decisions.csv"
NEW_TIFF_CSV = REVIEW_ROOT / "visual-only-new-tiff-candidates.csv"
REDUNDANT_CSV = REVIEW_ROOT / "visual-only-md-redundant-images.csv"
ERRORS_CSV = REVIEW_ROOT / "visual-only-decode-errors.csv"
REJECTED_DECISIONS_CSV = REVIEW_ROOT / "visual-only-rejected-legacy-decisions.csv"
INSTITUTION_BINDINGS_CSV = REVIEW_ROOT / "visual-only-institution-master-bindings.csv"
INSTITUTION_HOLDS_CSV = REVIEW_ROOT / "visual-only-institution-master-holds.csv"
RECEIPT = REVIEW_ROOT / "visual-only-reconciliation.json"

IMAGE_EXTENSIONS = {"tif", "tiff", "jpg", "jpeg", "png", "webp"}
TIFF_EXTENSIONS = {"tif", "tiff"}
HASH_IDS = ("ahash-64-v2", "dhash-64-v2", "phash-dct-64-v2")
EXACT_MAX_ASPECT_DELTA = 0.10
NEAR_MAX_TOTAL = 15
NEAR_MAX_ASPECT_DELTA = 0.03
EXPECTED_MD_MASTERS = 5282
EXPECTED_FULL_INDEXED = 64806
AUTOMATED_DECISION_ACTORS = (
    "codex:user-directed-md-exclusion",
    "codex:user-confirmed-institution-md-master",
)
VISUALLY_REJECTED_LEGACY_DECISIONS = ("local:TFA-6120176",)


class VisualOnlyError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


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
    import subprocess

    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(completed.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise VisualOnlyError("Elements did not resolve to one mounted filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise VisualOnlyError(f"Elements read-only guard failed: {mount}")
    return mount


def assert_work_path(path: Path) -> None:
    resolved_parent = path.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(WORK_ROOT.resolve(strict=True))
    except ValueError as error:
        raise VisualOnlyError(f"write path escaped TFA work root: {path}") from error


def workbench_is_listening() -> bool:
    with socket.socket() as client:
        client.settimeout(0.25)
        return client.connect_ex(("127.0.0.1", 8768)) == 0


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(fields),
                quoting=csv.QUOTE_ALL,
                extrasaction="ignore",
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
    target = path.with_name(f"{path.stem}.pre-visual-only-{stamp}{path.suffix}")
    if target.exists():
        raise VisualOnlyError(f"backup already exists: {target}")
    if path.suffix == ".sqlite":
        with closing(connect(path, read_only=True)) as source, closing(connect(target)) as destination:
            source.backup(destination)
    else:
        shutil.copy2(path, target)
    return str(target)


def hash_rows(connection: sqlite3.Connection, asset_id: str) -> dict[str, str]:
    return {
        str(row["extractor_id"]): str(row["feature_hex"])
        for row in connection.execute(
            "SELECT extractor_id,feature_hex FROM visual_features WHERE asset_id=?",
            (asset_id,),
        )
    }


def indexed_asset_for_path(
    connection: sqlite3.Connection,
    root_id: str,
    relative_path: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM visual_assets
        WHERE root_id=? AND relative_path=? AND status!='missing'
        ORDER BY CASE status WHEN 'indexed' THEN 0 ELSE 1 END, indexed_at DESC
        LIMIT 1
        """,
        (root_id, relative_path),
    ).fetchone()


def visual_row(
    index: sqlite3.Connection,
    *,
    asset: sqlite3.Row,
    source_path: str,
) -> dict[str, Any]:
    values = hash_rows(index, str(asset["asset_id"])) if asset["status"] == "indexed" else {}
    complete = all(key in values for key in HASH_IDS)
    return {
        "asset_id": str(asset["asset_id"]),
        "source_path": source_path,
        "filename": str(asset["original_filename"]),
        "extension": str(asset["extension"]).casefold(),
        "size_bytes": int(asset["size_bytes"]),
        "mtime_ns": int(asset["mtime_ns"]),
        "width_px": int(asset["width_px"] or 0),
        "height_px": int(asset["height_px"] or 0),
        "ahash": values.get(HASH_IDS[0], ""),
        "dhash": values.get(HASH_IDS[1], ""),
        "phash": values.get(HASH_IDS[2], ""),
        "status": "indexed" if complete else str(asset["status"]),
        "error": "" if complete else clean(asset["error"]),
    }


def load_selected_masters(
    queue: sqlite3.Connection,
    archive_index: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = str(SOURCE_ROOT)
    root_id_row = archive_index.execute(
        "SELECT root_id FROM visual_roots WHERE root_path=?", (root,)
    ).fetchone()
    if root_id_row is None:
        raise VisualOnlyError("archive visual index does not contain Elements")
    root_id = str(root_id_row[0])
    selected = [
        dict(row)
        for row in queue.execute(
            """
            SELECT o.md_object_id,o.inventory,o.title,d.selected_candidate_id,
                   c.source_path,c.extension,c.size_bytes,c.mtime_ns
            FROM objects o
            JOIN reconciliation_decisions d
              ON d.record_key=o.record_key AND d.decision_state='selected_master'
            JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            WHERE o.record_state='current_museum_digital'
            ORDER BY CAST(o.md_object_id AS INTEGER)
            """
        )
    ]
    if len(selected) != EXPECTED_MD_MASTERS:
        raise VisualOnlyError(f"selected MD Master count drifted: {len(selected)}")
    references: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for row in selected:
        extension = str(row["extension"]).casefold().lstrip(".")
        if extension not in TIFF_EXTENSIONS:
            raise VisualOnlyError(f"selected MD Master is not TIFF: md:{row['md_object_id']}")
        path = Path(str(row["source_path"]))
        try:
            relative = str(path.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True)))
        except (OSError, ValueError) as error:
            raise VisualOnlyError(f"selected MD Master escaped protected source: {path}") from error
        asset = indexed_asset_for_path(archive_index, root_id, relative)
        visual = visual_row(archive_index, asset=asset, source_path=str(path)) if asset else None
        if (
            visual
            and visual["status"] == "indexed"
            and visual["size_bytes"] == int(row["size_bytes"])
            and visual["mtime_ns"] == int(row["mtime_ns"])
        ):
            references.append({
                **row,
                "visual_asset_id": visual["asset_id"],
                "visual_source_path": visual["source_path"],
                "width_px": visual["width_px"],
                "height_px": visual["height_px"],
                "ahash": visual["ahash"],
                "dhash": visual["dhash"],
                "phash": visual["phash"],
                "reference_kind": "selected_tiff",
            })
            continue

        # A selected TIFF that cannot be decoded may use the already-indexed
        # display image attached to that same MD record.  This remains visual
        # evidence and is explicitly labelled as a fallback.
        fallback = queue.execute(
            """
            SELECT c.source_path
            FROM local_visual_hashes h
            JOIN candidates c ON c.candidate_id=h.candidate_id
            WHERE c.record_key='md:'||? AND h.decode_status='ok'
            ORDER BY c.candidate_id LIMIT 1
            """,
            (row["md_object_id"],),
        ).fetchone()
        fallback_visual = None
        if fallback is not None:
            fallback_path = Path(str(fallback["source_path"]))
            try:
                fallback_relative = str(
                    fallback_path.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True))
                )
            except (OSError, ValueError):
                fallback_relative = ""
            if fallback_relative:
                fallback_asset = indexed_asset_for_path(archive_index, root_id, fallback_relative)
                if fallback_asset is not None:
                    fallback_visual = visual_row(
                        archive_index, asset=fallback_asset, source_path=str(fallback_path)
                    )
        if fallback_visual and fallback_visual["status"] == "indexed":
            references.append({
                **row,
                "visual_asset_id": fallback_visual["asset_id"],
                "visual_source_path": fallback_visual["source_path"],
                "width_px": fallback_visual["width_px"],
                "height_px": fallback_visual["height_px"],
                "ahash": fallback_visual["ahash"],
                "dhash": fallback_visual["dhash"],
                "phash": fallback_visual["phash"],
                "reference_kind": "md_display_fallback",
            })
        else:
            failures.append({
                "md_object_id": row["md_object_id"],
                "inventory": row["inventory"],
                "selected_master_path": row["source_path"],
                "error": visual["error"] if visual else "selected Master missing from visual index",
            })
    return references, failures


def load_universe(
    queue: sqlite3.Connection,
    full_index: sqlite3.Connection,
    archive_index: sqlite3.Connection,
) -> list[dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    root_row = full_index.execute("SELECT root_id,root_path FROM visual_roots").fetchone()
    if root_row is None or Path(str(root_row["root_path"])).resolve() != WF_ROOT.resolve():
        raise VisualOnlyError("full visual index is not bound to the complete WF root")
    for asset in full_index.execute("SELECT * FROM visual_assets WHERE status!='missing'"):
        path = str(WF_ROOT / str(asset["relative_path"]))
        assets[path] = visual_row(full_index, asset=asset, source_path=path)

    archive_root = str(SOURCE_ROOT)
    archive_root_id = archive_index.execute(
        "SELECT root_id FROM visual_roots WHERE root_path=?", (archive_root,)
    ).fetchone()[0]
    candidate_paths = {
        str(row[0])
        for row in queue.execute(
            """
            SELECT DISTINCT source_path FROM candidates
            WHERE lower(extension) IN ('.tif','.tiff','.jpg','.jpeg','.png','.webp')
            """
        )
    }
    for path in sorted(candidate_paths):
        if path in assets:
            continue
        source = Path(path)
        try:
            relative = str(source.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True)))
        except (OSError, ValueError):
            continue
        indexed = indexed_asset_for_path(archive_index, str(archive_root_id), relative)
        if indexed is None:
            stat = source.stat()
            assets[path] = {
                "asset_id": "missing-" + hashlib.sha256(path.encode()).hexdigest()[:32],
                "source_path": path,
                "filename": source.name,
                "extension": source.suffix.casefold().lstrip("."),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "width_px": 0,
                "height_px": 0,
                "ahash": "",
                "dhash": "",
                "phash": "",
                "status": "error",
                "error": "candidate missing from complete archive visual index",
            }
            continue
        row = visual_row(archive_index, asset=indexed, source_path=path)
        stat = source.stat()
        if row["size_bytes"] != stat.st_size or row["mtime_ns"] != stat.st_mtime_ns:
            row["status"] = "error"
            row["error"] = "source stat differs from visual index"
        assets[path] = row
    return sorted(assets.values(), key=lambda row: row["source_path"])


def load_institution_visual_references() -> list[dict[str, Any]]:
    if not INSTITUTION_VISUAL_CSV.is_file() or not INSTITUTION_VISUAL_RECEIPT.is_file():
        raise VisualOnlyError("institution-wide visual reference index is missing")
    receipt = json.loads(INSTITUTION_VISUAL_RECEIPT.read_text(encoding="utf-8"))
    if (
        receipt.get("result") != "ok"
        or int(receipt.get("institution_objects", -1)) != 11170
        or receipt.get("metadata_used_for_visual_matching") is not False
        or receipt.get("source_read_only") is not True
    ):
        raise VisualOnlyError("institution-wide visual reference receipt is invalid")
    digest = hashlib.sha256(INSTITUTION_VISUAL_CSV.read_bytes()).hexdigest()
    if digest != receipt.get("output_sha256"):
        raise VisualOnlyError("institution-wide visual reference CSV fingerprint drifted")
    with INSTITUTION_VISUAL_CSV.open(encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if len(rows) != int(receipt.get("display_images_indexed", -1)):
        raise VisualOnlyError("institution-wide visual reference count drifted")
    return rows


def load_crop_visual_matches() -> list[dict[str, Any]]:
    if not CROP_VISUAL_CSV.is_file() or not CROP_VISUAL_RECEIPT.is_file():
        raise VisualOnlyError("crop-aware visual audit evidence is missing")
    receipt = json.loads(CROP_VISUAL_RECEIPT.read_text(encoding="utf-8"))
    if (
        receipt.get("result") != "ok"
        or receipt.get("source_read_only") is not True
        or receipt.get("deletion_authorized") is not False
        or receipt.get("filename_used_as_matching_evidence") is not False
        or receipt.get("full_object_token_required_for_candidate_routing") is not True
        or receipt.get("metadata_used_for_matching") is not False
    ):
        raise VisualOnlyError("crop-aware visual audit receipt is invalid")
    if hashlib.sha256(CROP_VISUAL_CSV.read_bytes()).hexdigest() != receipt.get("output_sha256"):
        raise VisualOnlyError("crop-aware visual audit CSV fingerprint drifted")
    with CROP_VISUAL_CSV.open(encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if len(rows) != int(receipt.get("crop_verified_redundant_paths", -1)):
        raise VisualOnlyError("crop-aware visual match count drifted")
    return rows


def select_institution_masters(
    institution: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    existing_selected_paths: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    curated = [
        row for row in assets
        if row["status"] == "indexed"
        and row["extension"] in TIFF_EXTENSIONS
        and "/restore/archive/Alle-Tiff-Positive/" in row["source_path"]
        and row["source_path"] not in existing_selected_paths
    ]
    if not institution or not curated:
        return [], [
            {**row, "hold_reason": "no available curated TIFF comparison scope"}
            for row in institution
        ]
    ref_ahash = np.array([int(row["ahash"], 16) for row in institution], dtype=np.uint64)
    ref_dhash = np.array([int(row["dhash"], 16) for row in institution], dtype=np.uint64)
    ref_phash = np.array([int(row["phash"], 16) for row in institution], dtype=np.uint64)
    ref_aspect = np.array(
        [int(row["image_width_px"]) / int(row["image_height_px"]) for row in institution],
        dtype=np.float64,
    )
    asset_ahash = np.array([int(row["ahash"], 16) for row in curated], dtype=np.uint64)
    asset_dhash = np.array([int(row["dhash"], 16) for row in curated], dtype=np.uint64)
    asset_phash = np.array([int(row["phash"], 16) for row in curated], dtype=np.uint64)
    asset_aspect = np.array(
        [row["width_px"] / row["height_px"] for row in curated], dtype=np.float64
    )

    # Each institution reference and curated TIFF must be each other's unique
    # best conservative visual match.  This prevents a similar sequence frame
    # or a shared low-detail image from becoming a Master automatically.
    candidates_by_ref: dict[int, tuple[int, int, int, int, float, int]] = {}
    best_ref_by_asset: dict[int, tuple[int, int]] = {}
    for start in range(0, len(curated), 256):
        group = curated[start : start + 256]
        ahash_distance = np.bitwise_count(asset_ahash[start : start + len(group), None] ^ ref_ahash)
        dhash_distance = np.bitwise_count(asset_dhash[start : start + len(group), None] ^ ref_dhash)
        phash_distance = np.bitwise_count(asset_phash[start : start + len(group), None] ^ ref_phash)
        total = ahash_distance + dhash_distance + phash_distance
        aspect_delta = np.abs(asset_aspect[start : start + len(group), None] - ref_aspect) / ref_aspect
        qualified = ((total == 0) & (aspect_delta <= EXACT_MAX_ASPECT_DELTA)) | (
            (total <= NEAR_MAX_TOTAL) & (aspect_delta <= NEAR_MAX_ASPECT_DELTA)
        )
        for local_position in range(len(group)):
            asset_position = start + local_position
            values = np.where(qualified[local_position], total[local_position], 255)
            best_total = int(values.min())
            if best_total == 255:
                continue
            best_refs = np.where(values == best_total)[0]
            if len(best_refs) == 1:
                best_ref_by_asset[asset_position] = (int(best_refs[0]), best_total)
            for ref_position in np.where(qualified[local_position])[0]:
                value = (
                    int(total[local_position, ref_position]),
                    int(phash_distance[local_position, ref_position]),
                    int(ahash_distance[local_position, ref_position]),
                    int(dhash_distance[local_position, ref_position]),
                    float(aspect_delta[local_position, ref_position]),
                )
                prior = candidates_by_ref.get(int(ref_position))
                if prior is None or value < prior:
                    candidates_by_ref[int(ref_position)] = (*value, asset_position)

    selected: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    provisional_paths: defaultdict[str, list[int]] = defaultdict(list)
    provisional: dict[int, dict[str, Any]] = {}
    for ref_position, reference in enumerate(institution):
        candidate = candidates_by_ref.get(ref_position)
        if candidate is None:
            holds.append({**reference, "hold_reason": "no conservative visual match to a curated TIFF"})
            continue
        total, phash, ahash, dhash, aspect_delta, asset_position = candidate
        reciprocal = best_ref_by_asset.get(int(asset_position))
        if reciprocal is None or reciprocal[0] != ref_position:
            holds.append({**reference, "hold_reason": "curated TIFF did not have this MD record as its unique best visual match"})
            continue
        asset = curated[int(asset_position)]
        row = {
            **reference,
            **asset,
            "inventory": reference["md_inventory"],
            "title": reference["md_title"],
            "selected_master_path": asset["source_path"],
            "visual_source_path": reference["image_url"],
            "reference_kind": "institution_display_with_unique_curated_tiff",
            "ahash_distance": int(ahash),
            "dhash_distance": int(dhash),
            "phash_distance": int(phash),
            "distance_total": int(total),
            "aspect_ratio_delta": round(float(aspect_delta), 8),
        }
        provisional[ref_position] = row
        provisional_paths[str(asset["source_path"])].append(ref_position)
    for ref_position, row in provisional.items():
        if len(provisional_paths[str(row["source_path"])]) != 1:
            holds.append({
                **institution[ref_position],
                "hold_reason": "one curated TIFF was the best match for multiple institution MD records",
            })
            continue
        selected.append(row)
    return selected, holds


def classify(
    references: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    selected_master_paths: set[str],
    crop_visual_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    if not references:
        raise VisualOnlyError("no selected MD visual references are available")
    master_ahash = np.array([int(row["ahash"], 16) for row in references], dtype=np.uint64)
    master_dhash = np.array([int(row["dhash"], 16) for row in references], dtype=np.uint64)
    master_phash = np.array([int(row["phash"], 16) for row in references], dtype=np.uint64)
    master_aspect = np.array(
        [row["width_px"] / row["height_px"] for row in references], dtype=np.float64
    )
    selected_paths = set(selected_master_paths)
    crop_by_path = {row["source_path"]: row for row in crop_visual_matches}
    indexed = [row for row in assets if row["status"] == "indexed" and row["source_path"] not in selected_paths]
    redundant: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for start in range(0, len(indexed), 256):
        group = indexed[start : start + 256]
        ahash = np.array([int(row["ahash"], 16) for row in group], dtype=np.uint64)[:, None]
        dhash = np.array([int(row["dhash"], 16) for row in group], dtype=np.uint64)[:, None]
        phash = np.array([int(row["phash"], 16) for row in group], dtype=np.uint64)[:, None]
        aspect = np.array(
            [row["width_px"] / row["height_px"] for row in group], dtype=np.float64
        )[:, None]
        ahash_distance = np.bitwise_count(ahash ^ master_ahash)
        dhash_distance = np.bitwise_count(dhash ^ master_dhash)
        phash_distance = np.bitwise_count(phash ^ master_phash)
        total = ahash_distance + dhash_distance + phash_distance
        aspect_delta = np.abs(aspect - master_aspect) / master_aspect
        exact = (total == 0) & (aspect_delta <= EXACT_MAX_ASPECT_DELTA)
        near = (total <= NEAR_MAX_TOTAL) & (aspect_delta <= NEAR_MAX_ASPECT_DELTA)
        qualified = exact | near
        masked = np.where(qualified, total, 255)
        best_indexes = masked.argmin(axis=1)
        best_totals = masked[np.arange(len(group)), best_indexes]
        for position, (asset, best_index, best_total) in enumerate(
            zip(group, best_indexes, best_totals)
        ):
            if int(best_total) == 255:
                crop = crop_by_path.get(asset["source_path"])
                if crop is not None:
                    redundant.append({
                        **asset,
                        "matched_md_object_ids": crop["matched_md_object_id"],
                        "matched_md_inventories": crop["matched_md_inventory"],
                        "selected_master_paths": crop["selected_master_path"],
                        "reference_kinds": "selected_md_display_crop_visual",
                        "ahash_distance": "",
                        "dhash_distance": crop["crop_dhash_distance"],
                        "phash_distance": crop["crop_phash_distance"],
                        "distance_total": crop["crop_distance_total"],
                        "aspect_ratio_delta": "",
                        "disposition": "redundant_to_existing_md_selected_master",
                        "disposition_basis": "selected_md_visual_crop_verified",
                        "drain_state": "proposed_redundant",
                        "deletion_authorized": "false",
                    })
                    continue
                unmatched.append(asset)
                continue
            index = int(best_index)
            distance_tuple = (
                int(ahash_distance[position, index]),
                int(dhash_distance[position, index]),
                int(phash_distance[position, index]),
            )
            tied_indexes = np.where(
                qualified[position]
                & (total[position] == best_total)
                & (phash_distance[position] == distance_tuple[2])
            )[0]
            tied = [references[int(value)] for value in tied_indexes]
            basis = "selected_md_visual_exact" if int(best_total) == 0 else "selected_md_visual_very_close"
            if all(row["reference_kind"] == "md_display_fallback" for row in tied):
                basis += "_display_fallback"
            redundant.append({
                **asset,
                "matched_md_object_ids": "; ".join(str(row["md_object_id"]) for row in tied),
                "matched_md_inventories": "; ".join(str(row["inventory"]) for row in tied),
                "selected_master_paths": " | ".join(str(row["source_path"]) for row in tied),
                "reference_kinds": "; ".join(sorted({str(row["reference_kind"]) for row in tied})),
                "ahash_distance": distance_tuple[0],
                "dhash_distance": distance_tuple[1],
                "phash_distance": distance_tuple[2],
                "distance_total": int(best_total),
                "aspect_ratio_delta": round(float(aspect_delta[position, index]), 8),
                "disposition": "redundant_to_existing_md_selected_master",
                "disposition_basis": basis,
                "drain_state": "proposed_redundant",
                "deletion_authorized": "false",
            })

    new_tiffs = [row for row in unmatched if row["extension"] in TIFF_EXTENSIONS]
    visual_counts = Counter((row["ahash"], row["dhash"], row["phash"]) for row in new_tiffs)
    for row in new_tiffs:
        signature = (row["ahash"], row["dhash"], row["phash"])
        row["visual_group_id"] = "visual-" + hashlib.sha256(":".join(signature).encode()).hexdigest()[:16]
        row["exact_visual_group_tiff_count"] = visual_counts[signature]
        row["classification_basis"] = (
            "no selected-MD visual match under exact/very-close aspect-gated policy"
        )
        row["review_state"] = "genuine_new_tiff_candidate"
        row["deletion_authorized"] = "false"

    error_rows = []
    for row in assets:
        if row["status"] == "indexed" or row["source_path"] in selected_paths:
            continue
        error_rows.append({
            **row,
            "review_state": "visual_decode_error_hold",
            "deletion_authorized": "false",
        })
    return {
        "selected_paths": selected_paths,
        "redundant_rows": sorted(redundant, key=lambda row: row["source_path"]),
        "new_tiff_rows": sorted(new_tiffs, key=lambda row: (row["visual_group_id"], row["source_path"])),
        "error_rows": sorted(error_rows, key=lambda row: row["source_path"]),
        "indexed_assets": len(indexed),
        "unmatched_non_tiff": sum(row["extension"] not in TIFF_EXTENSIONS for row in unmatched),
    }


def disposition_fields(connection: sqlite3.Connection) -> list[str]:
    return [str(row["name"]) for row in connection.execute("PRAGMA table_info(source_disposition_proposal)")]


def disposition_row(
    existing: sqlite3.Row | None,
    *,
    asset: dict[str, Any],
    status: str,
    basis: str,
    drain_state: str,
    note: str,
) -> dict[str, Any]:
    if existing is not None:
        row = dict(existing)
    else:
        row = {
            "source_path": asset["source_path"],
            "source_kind": "file",
            "unit": "tfa",
            "inventory": "",
            "source_role": "master_candidate" if asset["extension"] in TIFF_EXTENSIONS else "derivative_candidate",
            "final_path": "",
            "preservation_status": "not_assessed",
            "preservation_basis": "visual_only_reconciliation",
            "source_sha256": "",
            "final_sha256": "",
            "source_size_bytes": asset["size_bytes"],
            "exact_copy_proven": "false",
            "drain_state": "needs_reconciliation",
            "deletion_authorized": "false",
            "notes": "",
        }
    old_note = clean(row.get("notes"))
    row.update({
        "source_path": asset["source_path"],
        "source_size_bytes": asset["size_bytes"],
        "preservation_status": status,
        "preservation_basis": basis,
        "drain_state": drain_state,
        "deletion_authorized": "false",
        "notes": f"{old_note}; {note}" if old_note else note,
    })
    return row


def export_table(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    fields = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY source_path")]
    return fields, rows


def export_local_decisions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT d.*,o.inventory,COALESCE(c.source_path,'') AS selected_source_path
            FROM local_object_decisions d
            JOIN objects o ON o.record_key=d.record_key
            LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            ORDER BY o.inventory COLLATE NOCASE,d.record_key
            """
        )
    ]


def build_projection() -> dict[str, Any]:
    required = (
        DATABASE, BASELINE_DATABASE, FULL_INDEX, FULL_COMPLETION, ARCHIVE_VISUAL_INDEX,
        INSTITUTION_VISUAL_CSV, INSTITUTION_VISUAL_RECEIPT, CROP_VISUAL_CSV, CROP_VISUAL_RECEIPT,
    )
    if not all(path.is_file() for path in required):
        missing = [str(path) for path in required if not path.is_file()]
        raise VisualOnlyError(f"required evidence is missing: {missing}")
    completion = json.loads(FULL_COMPLETION.read_text(encoding="utf-8"))
    if (
        completion.get("state") != "complete"
        or int(completion.get("indexed", -1)) != EXPECTED_FULL_INDEXED
        or completion.get("source_roots_unchanged") is not True
    ):
        raise VisualOnlyError("full WF visual index completion is invalid")
    with closing(connect(DATABASE, read_only=True)) as queue, closing(
        connect(FULL_INDEX, read_only=True)
    ) as full_index, closing(connect(ARCHIVE_VISUAL_INDEX, read_only=True)) as archive_index:
        if queue.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise VisualOnlyError("queue SQLite quick_check failed")
        references, master_failures = load_selected_masters(queue, archive_index)
        assets = load_universe(queue, full_index, archive_index)
        institution = load_institution_visual_references()
        institution_by_id = {str(row["md_object_id"]): row for row in institution}
        remaining_failures = []
        for failure in master_failures:
            fallback = institution_by_id.pop(str(failure["md_object_id"]), None)
            if fallback is None:
                remaining_failures.append(failure)
                continue
            references.append({
                "md_object_id": failure["md_object_id"],
                "inventory": failure["inventory"],
                "title": fallback["md_title"],
                "source_path": failure["selected_master_path"],
                "visual_source_path": fallback["image_url"],
                "width_px": int(fallback["image_width_px"]),
                "height_px": int(fallback["image_height_px"]),
                "ahash": fallback["ahash"],
                "dhash": fallback["dhash"],
                "phash": fallback["phash"],
                "reference_kind": "selected_tiff_md_display_fallback",
            })
        master_failures = remaining_failures
        selected_master_paths = {
            str(row[0])
            for row in queue.execute(
                """
                SELECT c.source_path
                FROM reconciliation_decisions d
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.decision_state='selected_master'
                """
            )
        }
        institution_candidates = [
            row for md_id, row in institution_by_id.items()
            if md_id not in {str(reference["md_object_id"]) for reference in references}
        ]
        institution_bindings, institution_holds = select_institution_masters(
            institution_candidates, assets, selected_master_paths
        )
        references.extend(institution_bindings)
        selected_master_paths.update(str(row["selected_master_path"]) for row in institution_bindings)
        crop_visual_matches = load_crop_visual_matches()
        projected = classify(references, assets, selected_master_paths, crop_visual_matches)
    return {
        **projected,
        "references": references,
        "master_failures": master_failures,
        "institution_visual_references": institution,
        "institution_bindings": institution_bindings,
        "institution_holds": institution_holds,
        "crop_visual_matches": crop_visual_matches,
        "assets": assets,
        "completion": completion,
    }


def apply_projection(projected: dict[str, Any], mount: dict[str, Any]) -> dict[str, Any]:
    if workbench_is_listening():
        raise VisualOnlyError("port 8768 must be stopped before applying the visual-only rebuild")
    prepared_at = utc_now()
    stamp = prepared_at.replace("-", "").replace(":", "")
    backup_paths = [
        DATABASE, SOURCE_CSV, DRAIN_CSV, LOCAL_DECISIONS_CSV, NEW_TIFF_CSV, REDUNDANT_CSV,
        ERRORS_CSV, REJECTED_DECISIONS_CSV, INSTITUTION_BINDINGS_CSV, INSTITUTION_HOLDS_CSV, RECEIPT,
    ]
    backups = [value for value in (backup(path, stamp) for path in backup_paths) if value]
    rejected: list[dict[str, Any]] = []
    if not LEGACY_DECISION_AUDIT_DATABASE.is_file():
        raise VisualOnlyError("pre-visual decision audit database is missing")
    with closing(connect(LEGACY_DECISION_AUDIT_DATABASE, read_only=True)) as legacy:
        placeholders = ",".join("?" for _ in AUTOMATED_DECISION_ACTORS)
        for row in legacy.execute(
            f"SELECT * FROM local_object_decisions WHERE decided_by IN ({placeholders})",
            AUTOMATED_DECISION_ACTORS,
        ):
            rejected.append({
                **dict(row),
                "rejection_basis": "automated metadata/filename-derived duplicate decision removed by visual-only rebuild",
            })
        for record_key in VISUALLY_REJECTED_LEGACY_DECISIONS:
            row = legacy.execute(
                "SELECT * FROM local_object_decisions WHERE record_key=?", (record_key,)
            ).fetchone()
            if row is not None:
                rejected.append({
                    **dict(row),
                    "rejection_basis": "visual audit found a different photographic frame; not a derivative or duplicate",
                })
    with closing(connect(DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ATTACH DATABASE ? AS baseline", (str(BASELINE_DATABASE),))
        baseline_quick = connection.execute("PRAGMA baseline.quick_check").fetchone()[0]
        if baseline_quick != "ok":
            raise VisualOnlyError("baseline SQLite quick_check failed")
        connection.execute("DELETE FROM source_disposition_proposal")
        connection.execute(
            "INSERT INTO source_disposition_proposal SELECT * FROM baseline.source_disposition_proposal"
        )
        connection.execute("DELETE FROM drain_list_proposal")

        placeholders = ",".join("?" for _ in AUTOMATED_DECISION_ACTORS)
        connection.execute(
            f"DELETE FROM local_object_decisions WHERE decided_by IN ({placeholders})",
            AUTOMATED_DECISION_ACTORS,
        )
        for record_key in VISUALLY_REJECTED_LEGACY_DECISIONS:
            row = connection.execute(
                "SELECT * FROM local_object_decisions WHERE record_key=?", (record_key,)
            ).fetchone()
            if row is not None:
                connection.execute("DELETE FROM local_object_decisions WHERE record_key=?", (record_key,))

        fields = disposition_fields(connection)
        for asset in projected["institution_bindings"]:
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?",
                (asset["selected_master_path"],),
            ).fetchone()
            row = disposition_row(
                existing,
                asset=asset,
                status="selected_master",
                basis="institution_md_visual_match_unique_curated_tiff",
                drain_state="retain_selected_master",
                note=(
                    f"Selected as the single TIFF Master for institution md:{asset['md_object_id']} by "
                    f"mutual unique visual match to its MD display image; deletion not authorized"
                ),
            )
            values = [row[field] for field in fields]
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
                + ",".join("?" for _ in fields) + ")",
                values,
            )
        for asset in projected["redundant_rows"]:
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?",
                (asset["source_path"],),
            ).fetchone()
            note = (
                f"Visual-only match to selected MD TIFF Master(s) md:{asset['matched_md_object_ids']}; "
                f"a/d/p={asset['ahash_distance']}/{asset['dhash_distance']}/{asset['phash_distance']}; "
                f"aspect_delta={asset['aspect_ratio_delta']}; deletion not authorized"
            )
            row = disposition_row(
                existing,
                asset=asset,
                status="redundant",
                basis="visual_match_to_selected_md_tiff_master",
                drain_state="proposed_redundant",
                note=note,
            )
            values = [row[field] for field in fields]
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
                + ",".join("?" for _ in fields) + ")",
                values,
            )
            connection.execute(
                "INSERT INTO drain_list_proposal VALUES ("
                + ",".join("?" for _ in fields) + ")",
                values,
            )

        for asset in projected["new_tiff_rows"]:
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?",
                (asset["source_path"],),
            ).fetchone()
            if existing is not None and str(existing["preservation_status"]) == "retained_existing_unit_evidence":
                continue
            row = disposition_row(
                existing,
                asset=asset,
                status="not_assessed",
                basis="visual_only_new_tiff_candidate",
                drain_state="needs_reconciliation",
                note="No selected-MD visual match; candidate for later object/metadata reconciliation; deletion not authorized",
            )
            values = [row[field] for field in fields]
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
                + ",".join("?" for _ in fields) + ")",
                values,
            )

        for asset in projected["error_rows"]:
            if asset["extension"] not in TIFF_EXTENSIONS:
                continue
            existing = connection.execute(
                "SELECT * FROM source_disposition_proposal WHERE source_path=?",
                (asset["source_path"],),
            ).fetchone()
            row = disposition_row(
                existing,
                asset=asset,
                status="not_assessed",
                basis="visual_decode_error_hold",
                drain_state="needs_reconciliation",
                note=f"Visual decode/index error; not classified: {asset['error']}; deletion not authorized",
            )
            values = [row[field] for field in fields]
            connection.execute(
                "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
                + ",".join("?" for _ in fields) + ")",
                values,
            )

        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_visual_only_reconciliation_receipt',?)",
            (str(RECEIPT),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_queue_mode','visual_only')"
        )
        connection.commit()
        connection.execute("DETACH DATABASE baseline")

    with closing(connect(DATABASE, read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        unauthorized = connection.execute(
            "SELECT COUNT(*) FROM drain_list_proposal WHERE deletion_authorized!='false'"
        ).fetchone()[0]
        nonredundant_drain = connection.execute(
            "SELECT COUNT(*) FROM drain_list_proposal WHERE preservation_status!='redundant' OR drain_state!='proposed_redundant'"
        ).fetchone()[0]
        selected_in_drain = connection.execute(
            """
            SELECT COUNT(*) FROM drain_list_proposal p
            JOIN candidates c ON c.source_path=p.source_path
            JOIN reconciliation_decisions d ON d.selected_candidate_id=c.candidate_id
            WHERE d.decision_state='selected_master'
            """
        ).fetchone()[0]
        if quick != "ok" or foreign or unauthorized or nonredundant_drain or selected_in_drain:
            raise VisualOnlyError(
                f"post-apply validation failed: quick={quick}, foreign={len(foreign)}, "
                f"unauthorized={unauthorized}, nonredundant_drain={nonredundant_drain}, selected_in_drain={selected_in_drain}"
            )
        source_fields, source_rows = export_table(connection, "source_disposition_proposal")
        drain_fields, drain_rows = export_table(connection, "drain_list_proposal")
        local_decisions = export_local_decisions(connection)

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
        NEW_TIFF_CSV,
        (
            "visual_group_id", "asset_id", "source_path", "filename", "extension", "size_bytes", "mtime_ns",
            "width_px", "height_px", "ahash", "dhash", "phash", "exact_visual_group_tiff_count",
            "classification_basis", "review_state", "deletion_authorized",
        ),
        projected["new_tiff_rows"],
    )
    atomic_csv(
        REDUNDANT_CSV,
        (
            "asset_id", "source_path", "filename", "extension", "size_bytes", "mtime_ns", "width_px", "height_px",
            "matched_md_object_ids", "matched_md_inventories", "selected_master_paths", "reference_kinds",
            "ahash_distance", "dhash_distance", "phash_distance", "distance_total", "aspect_ratio_delta",
            "disposition", "disposition_basis", "drain_state", "deletion_authorized",
        ),
        projected["redundant_rows"],
    )
    atomic_csv(
        ERRORS_CSV,
        (
            "asset_id", "source_path", "filename", "extension", "size_bytes", "mtime_ns", "width_px", "height_px",
            "status", "error", "review_state", "deletion_authorized",
        ),
        projected["error_rows"],
    )
    rejected_fields = (
        "record_key", "outcome", "selected_candidate_id", "matched_md_object_id", "title", "description", "date_text",
        "object_type", "material", "dimensions", "note", "decided_by", "decided_at", "rejection_basis",
    )
    atomic_csv(REJECTED_DECISIONS_CSV, rejected_fields, rejected)
    atomic_csv(
        INSTITUTION_BINDINGS_CSV,
        (
            "md_object_id", "md_inventory", "md_title", "md_url", "image_url", "visual_source_path",
            "selected_master_path", "asset_id", "filename", "extension", "size_bytes", "mtime_ns",
            "width_px", "height_px", "ahash", "dhash", "phash", "ahash_distance", "dhash_distance",
            "phash_distance", "distance_total", "aspect_ratio_delta", "reference_kind",
        ),
        projected["institution_bindings"],
    )
    atomic_csv(
        INSTITUTION_HOLDS_CSV,
        (
            "md_object_id", "md_inventory", "md_title", "md_url", "image_url", "image_width_px",
            "image_height_px", "ahash", "dhash", "phash", "hold_reason",
        ),
        projected["institution_holds"],
    )

    tiff_errors = sum(row["extension"] in TIFF_EXTENSIONS for row in projected["error_rows"])
    receipt = {
        "format": "iss-tfa-visual-only-reconciliation-v1",
        "version": VERSION,
        "result": "ok",
        "mode": "apply",
        "applied_at": prepared_at,
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "deletion_authorized": False,
        "classification_authority": "visual_only",
        "metadata_used_for_matching": False,
        "filename_used_for_matching": False,
        "filename_used_for_candidate_routing": True,
        "crop_verified_redundant_paths": len(projected["crop_visual_matches"]),
        "selected_md_tiff_masters": EXPECTED_MD_MASTERS,
        "selected_tiff_visual_references": sum(row["reference_kind"] == "selected_tiff" for row in projected["references"]),
        "md_display_fallback_references": sum("display_fallback" in row["reference_kind"] for row in projected["references"]),
        "md_records_without_visual_reference": projected["master_failures"],
        "institution_display_visual_references": len(projected["institution_visual_references"]),
        "institution_selected_tiff_masters": len(projected["institution_bindings"]),
        "institution_master_holds": len(projected["institution_holds"]),
        "policy": {
            "exact_total_distance": 0,
            "exact_max_aspect_ratio_delta": EXACT_MAX_ASPECT_DELTA,
            "very_close_max_total_distance": NEAR_MAX_TOTAL,
            "very_close_max_aspect_ratio_delta": NEAR_MAX_ASPECT_DELTA,
        },
        "indexed_assets_compared": projected["indexed_assets"],
        "redundant_image_paths": len(projected["redundant_rows"]),
        "redundant_tiff_paths": sum(row["extension"] in TIFF_EXTENSIONS for row in projected["redundant_rows"]),
        "genuine_new_tiff_candidate_paths": len(projected["new_tiff_rows"]),
        "genuine_new_tiff_visual_groups": len({row["visual_group_id"] for row in projected["new_tiff_rows"]}),
        "unmatched_non_tiff_paths": projected["unmatched_non_tiff"],
        "visual_error_paths": len(projected["error_rows"]),
        "visual_error_tiff_paths": tiff_errors,
        "removed_automated_or_visually_wrong_decisions": len(rejected),
        "preserved_local_human_decisions": len(local_decisions),
        "drain_rows": len(drain_rows),
        "drain_contains_only_visual_redundancies": True,
        "backups": backups,
        "outputs": {
            "new_tiff_candidates": str(NEW_TIFF_CSV),
            "redundant_images": str(REDUNDANT_CSV),
            "decode_errors": str(ERRORS_CSV),
            "rejected_legacy_decisions": str(REJECTED_DECISIONS_CSV),
            "institution_master_bindings": str(INSTITUTION_BINDINGS_CSV),
            "institution_master_holds": str(INSTITUTION_HOLDS_CSV),
            "source_disposition": str(SOURCE_CSV),
            "drain_list": str(DRAIN_CSV),
        },
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def run(*, apply: bool) -> dict[str, Any]:
    mount = source_mount()
    projected = build_projection()
    summary = {
        "result": "ok",
        "mode": "apply" if apply else "check",
        "selected_md_tiff_masters": EXPECTED_MD_MASTERS,
        "selected_tiff_visual_references": sum(row["reference_kind"] == "selected_tiff" for row in projected["references"]),
        "md_display_fallback_references": sum("display_fallback" in row["reference_kind"] for row in projected["references"]),
        "md_records_without_visual_reference": len(projected["master_failures"]),
        "institution_display_visual_references": len(projected["institution_visual_references"]),
        "institution_selected_tiff_masters": len(projected["institution_bindings"]),
        "institution_master_holds": len(projected["institution_holds"]),
        "indexed_assets_compared": projected["indexed_assets"],
        "redundant_image_paths": len(projected["redundant_rows"]),
        "redundant_tiff_paths": sum(row["extension"] in TIFF_EXTENSIONS for row in projected["redundant_rows"]),
        "genuine_new_tiff_candidate_paths": len(projected["new_tiff_rows"]),
        "genuine_new_tiff_visual_groups": len({row["visual_group_id"] for row in projected["new_tiff_rows"]}),
        "visual_error_paths": len(projected["error_rows"]),
        "visual_error_tiff_paths": sum(row["extension"] in TIFF_EXTENSIONS for row in projected["error_rows"]),
        "metadata_used_for_matching": False,
        "filename_used_for_matching": False,
        "filename_used_for_candidate_routing": True,
        "crop_verified_redundant_paths": len(projected["crop_visual_matches"]),
        "source_read_only": True,
        "deletion_authorized": False,
    }
    if not apply:
        return summary
    return apply_projection(projected, mount)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
