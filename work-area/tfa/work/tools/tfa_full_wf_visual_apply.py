#!/usr/bin/env python3
"""Apply the completed full-WF visual index to the existing TFA local queue."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from tfa_local_objects_prepare import technical_batch


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WF_ROOT = SOURCE_ROOT / "restore/archive/WF"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
FULL_ROOT = WORK_ROOT / "work/reconciliation/wf-full-visual-index"
FULL_INDEX = FULL_ROOT / "wf-full-visual-index.sqlite"
FULL_COMPLETION = FULL_ROOT / "completions/afe749deaf74db6ae802721637125540.json"
CANDIDATES_CSV = REVIEW_ROOT / "candidates.csv"
TECHNICAL_CSV = REVIEW_ROOT / "local-object-accession-candidates.csv"
MATCHES_CSV = REVIEW_ROOT / "local-object-md-match-candidates.csv"
LOCAL_PREFLIGHT = REVIEW_ROOT / "local-object-workbench-preflight.json"
WORKBENCH_PREFLIGHT = REVIEW_ROOT / "workbench-preflight.json"
EVIDENCE_CSV = REVIEW_ROOT / "full-wf-visual-queue-evidence.csv"
RECEIPT = REVIEW_ROOT / "full-wf-visual-queue-apply.json"

EXPECTED_BEFORE = {
    "candidates": 58210,
    "local_candidate_technical": 1254,
    "local_md_match_candidates": 583,
    "local_object_metadata": 436,
    "local_object_decisions": 2,
}
FULL_EXPECTED = {"files_seen": 291594, "images_seen": 65357, "indexed": 64806, "errors": 551}
IMAGE_EXTENSIONS = {"tif", "tiff", "jpg", "jpeg"}
TIFF_EXTENSIONS = {"tif", "tiff"}
INVENTORY_RE = re.compile(
    r"(?i)(TFA(?:-(?:FOT|NEG|KB))?[-_ ]?[0-9][0-9A-Za-z._-]*)"
)

EXACT_ID_MATCH_SQL = "(instr(x.match_basis,'exact_inventory') > 0 OR instr(x.match_basis,'live_historical_md_id') > 0)"
EXACT_SHARED_PATH_SQL = """EXISTS (
  SELECT 1 FROM candidates local_candidate
  JOIN candidates md_candidate
    ON md_candidate.record_key='md:'||x.md_object_id
   AND md_candidate.source_path=local_candidate.source_path
  WHERE local_candidate.record_key=x.record_key
)"""
EXACT_SELECTED_MASTER_HASH_SQL = """EXISTS (
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
EXACT_EXISTING_MD_SQL = f"({EXACT_ID_MATCH_SQL} OR {EXACT_SHARED_PATH_SQL} OR {EXACT_SELECTED_MASTER_HASH_SQL})"


class ApplyError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize(value: Any) -> str:
    text = clean(value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def normalize_inventory(value: Any) -> str:
    value = clean(value).upper()
    value = re.sub(r"\.(?:TIF|TIFF|JPE?G|PSD|PNG)$", "", value)
    value = value.replace("_", "-").replace(" ", "")
    value = re.sub(r"^TFA-(?:FOT|NEG|KB)-?", "TFA-", value)
    value = re.sub(r"^TFA(?=\d)", "TFA-", value)
    return value.rstrip(".-")


def inventory_from_basename(value: str) -> str:
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
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise ApplyError("Elements did not resolve to one mounted filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options or "rw" in options:
        raise ApplyError(f"Elements read-only mount guard failed: {row}")
    return row


def inside_wf(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(WF_ROOT.resolve(strict=True))
    except ValueError as error:
        raise ApplyError(f"candidate escaped full-WF source root: {resolved}") from error
    return resolved


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


def backup(path: Path, stamp: str) -> str:
    if not path.exists():
        return ""
    destination = path.with_name(f"{path.stem}.pre-full-wf-visual-queue-{stamp}{path.suffix}")
    if destination.exists():
        raise ApplyError(f"backup already exists: {destination}")
    shutil.copy2(path, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return str(destination)


def workbench_is_listening() -> bool:
    with socket.socket() as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", 8768)) == 0


def full_index_assets(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT a.*,
          MAX(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex END) AS ahash,
          MAX(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex END) AS dhash,
          MAX(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex END) AS phash
        FROM visual_assets a
        LEFT JOIN visual_features f ON f.asset_id=a.asset_id
        GROUP BY a.asset_id
        """
    )
    return {
        str(WF_ROOT / str(row["relative_path"])): dict(row)
        for row in rows
    }


def validate_full_index(connection: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    completion = json.loads(FULL_COMPLETION.read_text(encoding="utf-8"))
    if completion.get("state") != "complete" or completion.get("source_roots_unchanged") is not True:
        raise ApplyError("full-WF completion receipt is incomplete")
    for key, expected in FULL_EXPECTED.items():
        if int(completion.get(key, -1)) != expected:
            raise ApplyError(f"full-WF receipt count drifted: {key}")
    if completion.get("database_sha256") != sha256_file(FULL_INDEX):
        raise ApplyError("full-WF index fingerprint drifted")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ApplyError("full-WF visual index integrity failed")
    run = connection.execute(
        "SELECT status,files_seen,images_seen,indexed,errors FROM index_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if run is None or str(run["status"]) != "complete":
        raise ApplyError("full-WF index run is not complete")
    actual = {key: int(run[key]) for key in FULL_EXPECTED}
    if actual != FULL_EXPECTED:
        raise ApplyError(f"full-WF index database counts drifted: {actual}")
    return completion, full_index_assets(connection)


def projection(connection: sqlite3.Connection, assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    local_objects = {
        normalize_inventory(row["inventory"]): dict(row)
        for row in connection.execute(
            "SELECT record_key,inventory FROM objects WHERE record_state='local_tfa_review'"
        )
    }
    local_titles = {
        str(row["record_key"]): str(row["title"])
        for row in connection.execute("SELECT record_key,title FROM local_object_metadata")
    }
    references: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute("SELECT record_key,source_rows_json FROM local_object_metadata"):
        try:
            sources = json.loads(str(row["source_rows_json"]))
        except json.JSONDecodeError:
            sources = []
        for source in sources if isinstance(sources, list) else []:
            for field in ("image_name", "master_name"):
                basename = Path(str(source.get(field) or "")).name.casefold()
                if basename:
                    references[basename].add(str(row["record_key"]))

    existing_pairs = {
        (str(row["record_key"]), str(row["source_path"]))
        for row in connection.execute(
            """SELECT c.record_key,c.source_path FROM candidates c
               JOIN objects o ON o.record_key=c.record_key
               WHERE o.record_state='local_tfa_review'"""
        )
    }
    path_templates: dict[str, dict[str, Any]] = {}
    for row in connection.execute("SELECT * FROM candidates ORDER BY candidate_id"):
        path_templates.setdefault(str(row["source_path"]), dict(row))
    next_id = int(connection.execute("SELECT MAX(candidate_id) FROM candidates").fetchone()[0]) + 1
    new_candidates = []
    ambiguous = []
    for path, asset in sorted(assets.items()):
        extension = str(asset["extension"]).casefold()
        if extension not in IMAGE_EXTENSIONS or str(asset["status"]) != "indexed":
            continue
        targets: dict[str, set[str]] = defaultdict(set)
        inventory = inventory_from_basename(str(asset["original_filename"]))
        if inventory in local_objects:
            targets[str(local_objects[inventory]["record_key"])].add("full_wf_exact_inventory_filename")
        reference_targets = references.get(str(asset["original_filename"]).casefold(), set())
        if reference_targets:
            # Some early TFA rows abbreviate the 538xxx inventory as 8xxx.
            # Prefer the exact suffix-preserving local identity when a later
            # spreadsheet points the same filename at a sibling record.
            historical_alias = ""
            if inventory.startswith("TFA-53"):
                historical = "TFA-" + inventory.removeprefix("TFA-")[2:]
                if historical in local_objects:
                    historical_alias = str(local_objects[historical]["record_key"])
            if historical_alias and any(record_key != historical_alias for record_key in reference_targets):
                targets.clear()
                targets[historical_alias].update((
                    "full_wf_historical_inventory_alias",
                    "full_wf_metadata_filename_reference_conflict",
                ))
            else:
                for record_key in reference_targets:
                    targets[record_key].add("full_wf_metadata_filename_reference")
        if len(targets) > 1:
            ambiguous.append({"source_path": path, "record_keys": sorted(targets)})
            continue
        if not targets:
            continue
        record_key = next(iter(targets))
        if (record_key, path) in existing_pairs:
            continue
        source = inside_wf(Path(path))
        stat = source.stat()
        if stat.st_size != int(asset["size_bytes"]) or stat.st_mtime_ns != int(asset["mtime_ns"]):
            raise ApplyError(f"full-WF candidate stat drifted: {source}")
        template = path_templates.get(path, {})
        folded = path.casefold()
        proposed_role = (
            "master_candidate" if extension in TIFF_EXTENSIONS else
            "derivative_candidate" if any(token in folded for token in ("400px", "800px", "freistell", "preview", "web")) else
            "image_candidate"
        )
        obj = next(row for row in local_objects.values() if str(row["record_key"]) == record_key)
        new_candidates.append({
            "candidate_id": next_id,
            "record_key": record_key,
            "inventory": str(obj["inventory"]),
            "source_path": path,
            "source_kind": "read_only_usb",
            "original_index_path": clean(template.get("original_index_path")),
            "basename": str(asset["original_filename"]),
            "extension": "." + extension,
            "size_bytes": int(asset["size_bytes"]),
            "mtime_ns": int(asset["mtime_ns"]),
            "file_kind": clean(template.get("file_kind")) or "other",
            "proposed_role": proposed_role,
            "match_basis": "; ".join(sorted(targets[record_key])),
            "reused_source_sha256": clean(template.get("reused_source_sha256")),
            "prior_unit": clean(template.get("prior_unit")),
            "prior_disposition": clean(template.get("prior_disposition")),
        })
        next_id += 1
    if ambiguous:
        raise ApplyError(f"full-WF candidate binding is ambiguous: {ambiguous[:3]}")

    current_by_path: dict[str, set[str]] = defaultdict(set)
    local_by_path: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """SELECT c.source_path,c.record_key,o.record_state,o.md_object_id
           FROM candidates c JOIN objects o ON o.record_key=c.record_key
           WHERE lower(c.extension) IN ('.tif','.tiff','.jpg','.jpeg')"""
    ):
        if row["record_state"] == "current_museum_digital" and row["md_object_id"]:
            current_by_path[str(row["source_path"])].add(str(row["md_object_id"]))
        elif row["record_state"] == "local_tfa_review":
            local_by_path[str(row["source_path"])].add(str(row["record_key"]))
    for candidate in new_candidates:
        local_by_path[str(candidate["source_path"])].add(str(candidate["record_key"]))

    indexed = {
        path: (int(row["ahash"], 16), int(row["dhash"], 16), int(row["phash"], 16))
        for path, row in assets.items()
        if row.get("ahash") and row.get("dhash") and row.get("phash")
    }
    current_assets = [(path, ids, *indexed[path]) for path, ids in current_by_path.items() if path in indexed]
    local_assets = [(path, keys, *indexed[path]) for path, keys in local_by_path.items() if path in indexed]
    chunk_index: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(4)]
    for index, row in enumerate(current_assets):
        phash = int(row[4])
        for part in range(4):
            chunk_index[part][(phash >> (part * 16)) & 0xFFFF].append(index)
    neighbor_cache: dict[int, list[int]] = {}

    def neighbors(value: int) -> list[int]:
        if value in neighbor_cache:
            return neighbor_cache[value]
        result = [value]
        for distance in (1, 2, 3):
            for positions in itertools.combinations(range(16), distance):
                other = value
                for position in positions:
                    other ^= 1 << position
                result.append(other)
        neighbor_cache[value] = result
        return result

    best: dict[tuple[str, str], tuple[float, int, int, int, str, str]] = {}
    for local_path, record_keys, local_ahash, local_dhash, local_phash in local_assets:
        candidate_indexes: set[int] = set()
        for part in range(4):
            chunk = (int(local_phash) >> (part * 16)) & 0xFFFF
            for neighbor in neighbors(chunk):
                candidate_indexes.update(chunk_index[part].get(neighbor, ()))
        for index in candidate_indexes:
            md_path, md_ids, md_ahash, md_dhash, md_phash = current_assets[index]
            phash_distance = (int(local_phash) ^ int(md_phash)).bit_count()
            if phash_distance > 14:
                continue
            ahash_distance = (int(local_ahash) ^ int(md_ahash)).bit_count()
            dhash_distance = (int(local_dhash) ^ int(md_dhash)).bit_count()
            score = (ahash_distance + dhash_distance + phash_distance) / 3
            if score > 10:
                continue
            for record_key in record_keys:
                for md_id in md_ids:
                    value = (score, phash_distance, ahash_distance, dhash_distance, local_path, md_path)
                    pair = (record_key, md_id)
                    if pair not in best or value < best[pair]:
                        best[pair] = value

    existing_matches = {
        (str(row["record_key"]), str(row["md_object_id"])): dict(row)
        for row in connection.execute("SELECT * FROM local_md_match_candidates")
    }
    shared_pairs: dict[tuple[str, str], str] = {}
    for candidate in new_candidates:
        path = str(candidate["source_path"])
        for md_id in current_by_path.get(path, set()):
            shared_pairs[(str(candidate["record_key"]), md_id)] = path

    updates: dict[tuple[str, str], dict[str, Any]] = {}
    new_matches: dict[tuple[str, str], dict[str, Any]] = {}
    evidence = []

    def add_basis(original: str, additions: Iterable[str]) -> str:
        values = {value for value in str(original).split("; ") if value}
        values.update(additions)
        return "; ".join(sorted(values))

    def md_template(md_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM local_md_match_candidates WHERE md_object_id=? LIMIT 1", (md_id,)
        ).fetchone()
        if row is not None:
            return dict(row)
        obj = connection.execute("SELECT * FROM objects WHERE record_key='md:'||?", (md_id,)).fetchone()
        if obj is None:
            raise ApplyError(f"current MD object disappeared: {md_id}")
        return {
            "md_object_id": md_id,
            "md_inventory": obj["inventory"], "md_title": obj["title"],
            "md_description": obj["description"], "md_date": obj["date_text"],
            "md_type": obj["object_type"], "md_material": obj["material"],
            "md_dimensions": obj["dimensions"], "md_collections": obj["collections"],
            "md_image_url": "", "md_preview_url": "", "md_url": obj["md_url"],
        }

    applied_pairs = set(existing_matches) & set(best)
    applied_pairs.update(pair for pair, value in best.items() if value[0] == 0)
    applied_pairs.update(shared_pairs)
    for pair in sorted(applied_pairs):
        record_key, md_id = pair
        visual = best.get(pair)
        additions = []
        if visual is not None:
            additions.append("full_wf_visual_exact" if visual[0] == 0 else "full_wf_visual_near_duplicate")
        if pair in shared_pairs:
            additions.extend(("full_wf_metadata_filename_reference", "full_wf_same_source_path"))
        if pair in existing_matches:
            row = dict(existing_matches[pair])
            row["match_basis"] = add_basis(str(row["match_basis"]), additions)
            if pair in shared_pairs:
                row["confidence_order"] = 0
            if visual is not None and (row["visual_score"] in (None, "") or float(visual[0]) < float(row["visual_score"])):
                row["visual_score"] = round(visual[0], 6)
                row["phash_distance"] = visual[1]
                row["ahash_distance"] = visual[2]
                row["dhash_distance"] = visual[3]
            updates[pair] = row
            action = "enriched_existing_match"
        else:
            template = md_template(md_id)
            visual_score = visual[0] if visual is not None else None
            local_title = normalize(local_titles.get(record_key, ""))
            md_title = normalize(template.get("md_title", ""))
            row = {
                "record_key": record_key, "md_object_id": md_id,
                **{key: template.get(key, "") for key in (
                    "md_inventory", "md_title", "md_description", "md_date", "md_type",
                    "md_material", "md_dimensions", "md_collections", "md_image_url",
                    "md_preview_url", "md_url",
                )},
                "match_basis": add_basis("", additions),
                "title_score": round(SequenceMatcher(None, local_title, md_title).ratio(), 6) if local_title else 0.0,
                "ahash_distance": visual[2] if visual is not None else None,
                "dhash_distance": visual[3] if visual is not None else None,
                "phash_distance": visual[1] if visual is not None else None,
                "visual_score": round(visual_score, 6) if visual_score is not None else None,
                "confidence_order": 0 if pair in shared_pairs else 4,
                "rank": 999,
            }
            new_matches[pair] = row
            action = "new_exact_shared_path" if pair in shared_pairs else "new_exact_visual_match"
        evidence.append({
            "record_key": record_key,
            "md_object_id": md_id,
            "action": action,
            "local_source_path": visual[4] if visual is not None else shared_pairs.get(pair, ""),
            "md_source_path": visual[5] if visual is not None else shared_pairs.get(pair, ""),
            "ahash_distance": visual[2] if visual is not None else "",
            "dhash_distance": visual[3] if visual is not None else "",
            "phash_distance": visual[1] if visual is not None else "",
            "visual_score": round(visual[0], 6) if visual is not None else "",
        })

    rejected_near = [pair for pair, value in best.items() if pair not in existing_matches and pair not in shared_pairs and value[0] > 0]
    return {
        "new_candidates": new_candidates,
        "updates": updates,
        "new_matches": new_matches,
        "evidence": evidence,
        "rejected_near_only": len(rejected_near),
        "full_visual_pairs": len(best),
        "shared_pairs": sorted([list(pair) for pair in shared_pairs]),
    }


def exact_exclusions(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM objects o
            WHERE o.record_state='local_tfa_review' AND
              (SELECT COUNT(*) FROM local_md_match_candidates x
               WHERE x.record_key=o.record_key AND {EXACT_EXISTING_MD_SQL})=1"""
    ).fetchone()[0])


def database_rows(connection: sqlite3.Connection, table: str, order: str) -> tuple[list[str], list[dict[str, Any]]]:
    fields = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    return fields, rows


def run(*, apply: bool) -> dict[str, Any]:
    mount = source_mount()
    if not all(path.is_file() for path in (DATABASE, FULL_INDEX, FULL_COMPLETION, CANDIDATES_CSV, TECHNICAL_CSV, MATCHES_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT)):
        raise ApplyError("required queue or full-index evidence is missing")
    if RECEIPT.exists():
        raise ApplyError(f"application receipt already exists: {RECEIPT}")
    with closing(connect(FULL_INDEX, read_only=True)) as full_connection:
        completion, assets = validate_full_index(full_connection)
    with closing(connect(DATABASE, read_only=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ApplyError("queue SQLite quick_check failed")
        before = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in EXPECTED_BEFORE}
        if before != EXPECTED_BEFORE:
            raise ApplyError(f"queue scope drifted before full-WF apply: {before}")
        projected = projection(connection, assets)
        exact_before = exact_exclusions(connection)
        decision_snapshot = [tuple(row) for row in connection.execute("SELECT * FROM local_object_decisions ORDER BY record_key")]
    technical = technical_batch(projected["new_candidates"])
    summary = {
        "result": "ok",
        "mode": "apply" if apply else "check",
        "new_candidates": len(projected["new_candidates"]),
        "new_candidate_records": len({row["record_key"] for row in projected["new_candidates"]}),
        "new_technical_rows": len(technical),
        "enriched_existing_matches": len(projected["updates"]),
        "new_exact_match_rows": len(projected["new_matches"]),
        "full_visual_pairs": projected["full_visual_pairs"],
        "rejected_near_only_matches": projected["rejected_near_only"],
        "shared_path_pairs": projected["shared_pairs"],
        "exact_exclusions_before": exact_before,
        "expected_candidates_after": before["candidates"] + len(projected["new_candidates"]),
        "expected_local_technical_after": before["local_candidate_technical"] + len(technical),
        "expected_matches_after": before["local_md_match_candidates"] + len(projected["new_matches"]),
    }
    if not apply:
        return summary
    if workbench_is_listening():
        raise ApplyError("port 8768 must be stopped before applying the queue projection")
    if len(projected["new_candidates"]) != 7 or len(technical) != 7:
        raise ApplyError("bounded seven-candidate scope guard failed")

    prepared_at = utc_now()
    stamp = prepared_at.replace("-", "").replace(":", "")
    backups = [
        backup(path, stamp)
        for path in (DATABASE, CANDIDATES_CSV, TECHNICAL_CSV, MATCHES_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT)
    ]
    with closing(connect(DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in EXPECTED_BEFORE}
        if current != before:
            raise ApplyError(f"queue changed after projection preflight: {current}")
        candidate_fields = [str(row["name"]) for row in connection.execute("PRAGMA table_info(candidates)")]
        connection.executemany(
            "INSERT INTO candidates VALUES (" + ",".join("?" for _ in candidate_fields) + ")",
            [[row[field] for field in candidate_fields] for row in projected["new_candidates"]],
        )
        technical_fields = [str(row["name"]) for row in connection.execute("PRAGMA table_info(local_candidate_technical)")]
        connection.executemany(
            "INSERT INTO local_candidate_technical VALUES (" + ",".join("?" for _ in technical_fields) + ")",
            [[row[field] for field in technical_fields] for row in technical],
        )
        for pair, row in projected["updates"].items():
            connection.execute(
                """UPDATE local_md_match_candidates SET match_basis=?,ahash_distance=?,dhash_distance=?,
                   phash_distance=?,visual_score=?,confidence_order=? WHERE record_key=? AND md_object_id=?""",
                (row["match_basis"], row["ahash_distance"], row["dhash_distance"], row["phash_distance"],
                 row["visual_score"], row["confidence_order"], pair[0], pair[1]),
            )
        match_fields = [str(row["name"]) for row in connection.execute("PRAGMA table_info(local_md_match_candidates)")]
        connection.executemany(
            "INSERT INTO local_md_match_candidates VALUES (" + ",".join("?" for _ in match_fields) + ")",
            [[row[field] for field in match_fields] for row in projected["new_matches"].values()],
        )
        affected = {pair[0] for pair in projected["updates"]} | {pair[0] for pair in projected["new_matches"]}
        for record_key in sorted(affected):
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM local_md_match_candidates WHERE record_key=?", (record_key,)
            )]
            rows.sort(key=lambda row: (
                int(row["confidence_order"]),
                float(row["visual_score"]) if row["visual_score"] not in (None, "") else 999.0,
                -float(row["title_score"]),
                int(row["md_object_id"]),
            ))
            for rank, row in enumerate(rows, 1):
                connection.execute(
                    "UPDATE local_md_match_candidates SET rank=? WHERE record_key=? AND md_object_id=?",
                    (rank, record_key, row["md_object_id"]),
                )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_candidate_technical_count',?)",
            (str(summary["expected_local_technical_after"]),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_full_wf_visual_index_receipt',?)",
            (str(FULL_COMPLETION),),
        )
        after_decisions = [tuple(row) for row in connection.execute("SELECT * FROM local_object_decisions ORDER BY record_key")]
        if after_decisions != decision_snapshot:
            raise ApplyError("human local decisions changed during full-WF projection")
        connection.commit()

    with closing(connect(DATABASE, read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts_after = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in EXPECTED_BEFORE}
        exact_after = exact_exclusions(connection)
        candidate_fields, candidate_rows = database_rows(connection, "candidates", "candidate_id")
        technical_fields, technical_rows = database_rows(connection, "local_candidate_technical", "record_key,source_path")
        match_fields, match_rows = database_rows(connection, "local_md_match_candidates", "record_key,rank")
        eligible_tiff = connection.execute(
            "SELECT COUNT(*) FROM local_candidate_technical WHERE accession_eligible=1 AND lower(extension) IN ('.tif','.tiff')"
        ).fetchone()[0]
        eligible_jpeg = connection.execute(
            "SELECT COUNT(*) FROM local_candidate_technical WHERE accession_eligible=1 AND lower(extension) IN ('.jpg','.jpeg')"
        ).fetchone()[0]
        records_eligible = connection.execute(
            "SELECT COUNT(DISTINCT record_key) FROM local_candidate_technical WHERE accession_eligible=1"
        ).fetchone()[0]
        records_matches = connection.execute(
            "SELECT COUNT(DISTINCT record_key) FROM local_md_match_candidates"
        ).fetchone()[0]
    if quick != "ok" or foreign:
        raise ApplyError(f"queue validation failed after apply: quick={quick}, foreign={len(foreign)}")
    if counts_after["candidates"] != summary["expected_candidates_after"] or counts_after["local_candidate_technical"] != summary["expected_local_technical_after"] or counts_after["local_md_match_candidates"] != summary["expected_matches_after"]:
        raise ApplyError(f"queue counts failed after apply: {counts_after}")
    if exact_after < exact_before:
        raise ApplyError(f"exact MD exclusions regressed: {exact_before} -> {exact_after}")

    atomic_csv(CANDIDATES_CSV, candidate_fields, candidate_rows)
    atomic_csv(TECHNICAL_CSV, technical_fields, technical_rows)
    atomic_csv(MATCHES_CSV, match_fields, match_rows)
    evidence_fields = (
        "record_key", "md_object_id", "action", "local_source_path", "md_source_path",
        "ahash_distance", "dhash_distance", "phash_distance", "visual_score",
    )
    atomic_csv(EVIDENCE_CSV, evidence_fields, projected["evidence"])
    preflight = json.loads(LOCAL_PREFLIGHT.read_text(encoding="utf-8"))
    preflight.update({
        "prepared_at": prepared_at,
        "version": "1.0.1+full-wf-visual",
        "local_image_candidates": counts_after["local_candidate_technical"],
        "eligible_tiff_candidates": int(eligible_tiff),
        "eligible_hires_jpeg_candidates": int(eligible_jpeg),
        "records_with_eligible_candidate": int(records_eligible),
        "records_without_eligible_candidate": 436 - int(records_eligible),
        "md_match_suggestions": counts_after["local_md_match_candidates"],
        "records_with_md_suggestions": int(records_matches),
        "existing_local_decisions_preserved": counts_after["local_object_decisions"],
        "full_wf_visual_index_completion": str(FULL_COMPLETION),
        "full_wf_visual_queue_evidence": str(EVIDENCE_CSV),
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
    })
    atomic_json(LOCAL_PREFLIGHT, preflight)
    workbench_preflight = json.loads(WORKBENCH_PREFLIGHT.read_text(encoding="utf-8"))
    workbench_preflight["evidence_fingerprints"]["candidates.csv"] = "sha256:" + sha256_file(CANDIDATES_CSV)
    workbench_preflight["version"] = "1.1.3"
    workbench_preflight["full_wf_visual_queue_apply"] = {
        "applied_at": prepared_at,
        "receipt": str(RECEIPT),
        "evidence_csv": str(EVIDENCE_CSV),
        "new_candidates": len(projected["new_candidates"]),
        "human_decisions_written": 0,
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(WORKBENCH_PREFLIGHT, workbench_preflight)
    receipt = {
        "format": "iss-tfa-full-wf-visual-queue-apply-v1",
        "version": VERSION,
        "applied_at": prepared_at,
        "result": "ok",
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "master_or_duplicate_decisions_written": 0,
        "deletion_authorized": False,
        "full_index_completion": str(FULL_COMPLETION),
        "full_index_database_sha256": completion["database_sha256"],
        "backups": backups,
        **summary,
        "mode": "apply",
        "counts_after": counts_after,
        "exact_exclusions_after": exact_after,
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
        "evidence_csv": str(EVIDENCE_CSV),
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
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
