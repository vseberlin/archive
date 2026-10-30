#!/usr/bin/env python3
"""Rank TIFF master candidates for missing-master stress-pilot records."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

import visual_index


FORMAT = "iss-visual-candidate-search-v1"
TOOL_VERSION = "0.2.0"
REQUIRED_EXTRACTORS = (
    "ahash-64-v2",
    "dhash-64-v2",
    "phash-dct-64-v2",
)


class CandidateSearchError(RuntimeError):
    """Raised when a bounded candidate search cannot be proven."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(parent.expanduser().resolve())
    except ValueError as error:
        raise CandidateSearchError(f"path escaped allowed root: {resolved}") from error
    return resolved


def load_assets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT
               a.asset_id, r.root_path, a.relative_path,
               a.original_filename, a.extension, a.size_bytes, a.mtime_ns,
               a.width_px, a.height_px,
               max(CASE WHEN f.extractor_id = 'ahash-64-v2'
                        THEN f.feature_hex END) AS ahash,
               max(CASE WHEN f.extractor_id = 'dhash-64-v2'
                        THEN f.feature_hex END) AS dhash,
               max(CASE WHEN f.extractor_id = 'phash-dct-64-v2'
                        THEN f.feature_hex END) AS phash
           FROM visual_assets a
           JOIN visual_roots r USING (root_id)
           JOIN visual_features f USING (asset_id)
           WHERE a.status = 'indexed'
             AND f.extractor_id IN (
                 'ahash-64-v2', 'dhash-64-v2', 'phash-dct-64-v2'
             )
           GROUP BY a.asset_id
           HAVING count(DISTINCT f.extractor_id) = 3"""
    ).fetchall()
    if not rows:
        raise CandidateSearchError("visual index contains no complete feature rows")
    return rows


def distance_band(total: int) -> str:
    if total <= 15:
        return "very_close"
    if total <= 30:
        return "close"
    return "exploratory"


def _occurrence_preference(row: sqlite3.Row) -> tuple[int, str]:
    """Prefer archive evidence while retaining every mirrored occurrence."""
    root = str(row["root_path"])
    priority = {
        "/mnt/volume/archive": 0,
        "/mnt/volume/archive-intake": 1,
        "/media/vladimir/Elements": 2,
    }.get(root.rstrip("/"), 3)
    return priority, str(row["relative_path"]).casefold()


def _occurrence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "asset_id": row["asset_id"],
        "path": str(Path(row["root_path"]) / row["relative_path"]),
        "filename": row["original_filename"],
        "size_bytes": row["size_bytes"],
        "mtime_ns": row["mtime_ns"],
        "width_px": row["width_px"],
        "height_px": row["height_px"],
    }


def rank_candidates(
    query_hashes: tuple[str, str, str],
    candidates: list[sqlite3.Row],
    limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in candidates:
        grouped.setdefault(
            (row["ahash"], row["dhash"], row["phash"]), []
        ).append(row)

    ranked: list[tuple[int, int, int, int, sqlite3.Row, list[sqlite3.Row]]] = []
    for signature, occurrences in grouped.items():
        occurrences.sort(key=_occurrence_preference)
        row = occurrences[0]
        ahash = visual_index.hamming_distance(query_hashes[0], signature[0])
        dhash = visual_index.hamming_distance(query_hashes[1], signature[1])
        phash = visual_index.hamming_distance(query_hashes[2], signature[2])
        ranked.append(
            (ahash + dhash + phash, phash, dhash, ahash, row, occurrences)
        )
    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            item[3],
            item[4]["relative_path"],
        )
    )
    results = []
    for total, phash, dhash, ahash, row, occurrences in ranked[:limit]:
        results.append(
            {
                **_occurrence(row),
                "filename": row["original_filename"],
                "extension": row["extension"],
                "distance_total": total,
                "distance_ahash": ahash,
                "distance_dhash": dhash,
                "distance_phash": phash,
                "distance_band": distance_band(total),
                "occurrence_count": len(occurrences),
                "alternate_occurrences": [
                    _occurrence(item) for item in occurrences[1:]
                ],
            }
        )
    return results


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise CandidateSearchError(f"refusing to replace report: {output}")
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    temporary.replace(output)
    csv_path = output.with_suffix(".csv")
    if csv_path.exists():
        raise CandidateSearchError(f"refusing to replace report: {csv_path}")
    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "unit_id",
                "inventory_label",
                "museum_digital_id",
                "query_evidence",
                "rank",
                "distance_band",
                "distance_total",
                "distance_phash",
                "distance_dhash",
                "distance_ahash",
                "filename",
                "size_bytes",
                "width_px",
                "height_px",
                "occurrence_count",
                "path",
            ],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        for query in report["queries"]:
            for rank, candidate in enumerate(query["candidates"], 1):
                writer.writerow(
                    {
                        "unit_id": query["unit_id"],
                        "inventory_label": query["inventory_label"],
                        "museum_digital_id": query["museum_digital_id"],
                        "query_evidence": query["query_evidence"],
                        "rank": rank,
                        **{
                            key: candidate[key]
                            for key in writer.fieldnames
                            if key in candidate
                        },
                    }
                )


def run_search(
    *,
    database: Path,
    manifest: Path,
    pilot_root: Path,
    work_root: Path,
    output: Path,
    limit: int,
) -> dict[str, Any]:
    database = _inside(database, work_root)
    output = _inside(output, work_root)
    pilot_root = pilot_root.expanduser().resolve()
    manifest = manifest.expanduser().resolve()
    if limit < 1 or limit > 50:
        raise CandidateSearchError("candidate limit must be between 1 and 50")
    manifest_data = json.loads(manifest.read_text())
    if manifest_data.get("format") != "iss-provenance-stress-pilot-v1":
        raise CandidateSearchError("unsupported stress-pilot manifest")
    if Path(manifest_data.get("pilot_root", "")).resolve() != pilot_root:
        raise CandidateSearchError("manifest does not bind the requested pilot")

    connection = sqlite3.connect(
        f"file:{database}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CandidateSearchError(f"visual index integrity failed: {integrity}")
        run = connection.execute(
            """SELECT run_id, token, completed_at, status, images_seen,
                      indexed, errors
               FROM index_runs ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        if not run or run[3] != "complete":
            raise CandidateSearchError("latest visual-index run is not complete")
        assets = load_assets(connection)
    finally:
        connection.close()

    by_path = {
        str(Path(row["root_path"]) / row["relative_path"]): row
        for row in assets
    }
    tiffs = [
        row for row in assets if row["extension"].casefold() in {"tif", "tiff"}
    ]
    preview_cache = _inside(pilot_root / "work/editor-preview-cache", pilot_root)
    query_cache = work_root / "tmp/candidate-search"
    queries = []
    for case in manifest_data.get("cases", []):
        if case.get("action") not in {"image_surrogate", "metadata_only"}:
            continue
        source_path = str(case.get("source_path") or "")
        indexed_source = by_path.get(source_path)
        if indexed_source:
            query_evidence = source_path
            query_hashes = (
                indexed_source["ahash"],
                indexed_source["dhash"],
                indexed_source["phash"],
            )
        else:
            museum_id = str(case.get("museum_digital_id") or "")
            matches = sorted(preview_cache.glob(f"md-{museum_id}-*.jpg"))
            if len(matches) != 1:
                raise CandidateSearchError(
                    f"{case.get('unit_id')}: expected one cached MD preview, "
                    f"found {len(matches)}"
                )
            query_evidence = str(matches[0])
            preview, _elapsed = visual_index.normalized_preview(
                matches[0],
                query_cache,
                180,
            )
            features = visual_index.feature_hashes(preview)
            query_hashes = (
                features["ahash_64"],
                features["dhash_64"],
                features["phash_dct_64"],
            )
        queries.append(
            {
                "unit_id": case["unit_id"],
                "inventory_label": case["inventory_label"],
                "museum_digital_id": case.get("museum_digital_id", ""),
                "query_evidence": query_evidence,
                "candidate_scope": "indexed_tiff_occurrences_only",
                "candidates": rank_candidates(query_hashes, tiffs, limit),
            }
        )
    report = {
        "format": FORMAT,
        "tool_version": TOOL_VERSION,
        "created_at": visual_index.utc_now(),
        "manifest": str(manifest),
        "manifest_sha256": file_sha256(manifest),
        "pilot_root": str(pilot_root),
        "visual_index": str(database),
        "visual_index_run": {
            "run_id": run[0],
            "token": run[1],
            "completed_at": run[2],
            "images_seen": run[4],
            "indexed": run[5],
            "errors": run[6],
        },
        "extractors": list(REQUIRED_EXTRACTORS),
        "ranking": (
            "ascending sum of 64-bit aHash, dHash, and DCT-pHash Hamming "
            "distances; exact feature triples collapsed to one visual "
            "candidate with alternate occurrences retained as evidence; "
            "candidate evidence only"
        ),
        "candidate_limit": limit,
        "queries": queries,
    }
    write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pilot-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        report = run_search(**vars(args))
    except Exception as error:
        raise SystemExit(f"error: {error}") from error
    print(
        json.dumps(
            {
                "result": "ok",
                "queries": len(report["queries"]),
                "output": str(args.output.resolve()),
                "csv": str(args.output.with_suffix(".csv").resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
