#!/usr/bin/env python3
"""Bind the confirmed TFA-538074 TIFF image to md:55436 with provenance.

This bounded correction preserves the existing md:85642 row as a retained
catalogue duplicate because the current TFA schema deliberately has no
separate duplicate-object state. It never modifies source media or authorizes
deletion.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

sys.path.insert(0, "/home/vladimir/wp/tools/archive-preservation")
import visual_index  # noqa: E402


REVIEW_ROOT = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation"
)
SOURCE_ROOT = Path("/media/vladimir/Elements")
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
CANDIDATES_CSV = REVIEW_ROOT / "candidates.csv"
SOURCE_DISPOSITIONS_CSV = REVIEW_ROOT / "source-disposition-proposal.csv"
DRAIN_DISPOSITIONS_CSV = REVIEW_ROOT / "drain-list-proposal.csv"
DECISIONS_CSV = REVIEW_ROOT / "workbench-decisions.csv"
WORKBENCH_DISPOSITIONS_CSV = REVIEW_ROOT / "workbench-candidate-dispositions.csv"
WORKBENCH_RECEIPT = REVIEW_ROOT / "workbench-preflight.json"
PROVENANCE = REVIEW_ROOT / "md-55436-md-85642-provenance-correction.json"
MD_REFERENCE = REVIEW_ROOT / "md-55436-reference-27192846895.jpg"
MD_REFERENCE_URL = (
    "https://berlin.museum-digital.de/data/berlin/images/"
    "import_29/201905/27192846895.jpg"
)
SOURCE_TIFF = (
    SOURCE_ROOT
    / "restore/archive/Alle-Tiff-Positive/TFA-FOT-538074.tif"
)
STAGED_TIFF = Path(
    "/mnt/volume/iss-archive-work/tfa/work/masters/TFA-FOT-538074.tif"
)
CANONICAL_KEY = "md:55436"
DUPLICATE_KEY = "md:85642"
EXPECTED_DUPLICATE_CANDIDATE_ID = 53352
EXPECTED_MD_REFERENCE_BYTES = 223182
EXPECTED_MD_REFERENCE_SHA256 = (
    "7701403bed98cfc3cdd0012eb2ae187f0b44272c180c505a317ae65698edb511"
)
EXPECTED_TIFF_BYTES = 65095018
EXPECTED_TIFF_SHA256 = (
    "73116ecdd249b27867267afb294f3dcf6be41eac75a883a873a9f629c0c706f3"
)


class CorrectionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise CorrectionError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_csv_temp(
    path: Path, fields: list[str], rows: list[dict[str, Any]], token: str
) -> Path:
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    if temporary.exists():
        raise CorrectionError(f"temporary output already exists: {temporary}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def atomic_json(path: Path, payload: dict[str, Any], token: str) -> None:
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    if temporary.exists():
        raise CorrectionError(f"temporary output already exists: {temporary}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def backup(path: Path, stamp: str) -> Path:
    destination = path.with_name(
        f"{path.stem}.pre-md-55436-provenance-rebind-{stamp}{path.suffix}"
    )
    if destination.exists():
        raise CorrectionError(f"backup already exists: {destination}")
    shutil.copy2(path, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return destination


def verify_boundaries() -> dict[str, str]:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-tfa-workbench.service"],
        capture_output=True,
        text=True,
    )
    if active.stdout.strip() == "active":
        raise CorrectionError("TFA workbench must be stopped before correction")
    mount = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(mount.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise CorrectionError("source mount did not resolve exactly once")
    row = filesystems[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise CorrectionError(f"source mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def image_hashes(path: Path) -> tuple[tuple[int, int], dict[str, str]]:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image).convert("L")
        dimensions = normalized.size
        hashes = visual_index.feature_hashes(normalized)
    return dimensions, hashes


def main() -> int:
    corrected_at = utc_now()
    stamp = stamp_now()
    token = f"md55436-{os.getpid()}"
    mount = verify_boundaries()
    if PROVENANCE.exists():
        raise CorrectionError(f"provenance correction already exists: {PROVENANCE}")

    required = (
        DATABASE,
        CANDIDATES_CSV,
        SOURCE_DISPOSITIONS_CSV,
        DRAIN_DISPOSITIONS_CSV,
        DECISIONS_CSV,
        WORKBENCH_DISPOSITIONS_CSV,
        WORKBENCH_RECEIPT,
        MD_REFERENCE,
        SOURCE_TIFF,
        STAGED_TIFF,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise CorrectionError(f"required evidence is missing: {missing}")

    md_stat = MD_REFERENCE.stat()
    md_sha256 = sha256_file(MD_REFERENCE)
    if md_stat.st_size != EXPECTED_MD_REFERENCE_BYTES or md_sha256 != EXPECTED_MD_REFERENCE_SHA256:
        raise CorrectionError("preserved MD reference fixity guard failed")
    source_stat = SOURCE_TIFF.stat()
    staged_stat = STAGED_TIFF.stat()
    source_sha256 = sha256_file(SOURCE_TIFF)
    staged_sha256 = sha256_file(STAGED_TIFF)
    if (
        source_stat.st_size != EXPECTED_TIFF_BYTES
        or staged_stat.st_size != EXPECTED_TIFF_BYTES
        or source_sha256 != EXPECTED_TIFF_SHA256
        or staged_sha256 != EXPECTED_TIFF_SHA256
    ):
        raise CorrectionError("source/staged TIFF exact-copy guard failed")

    md_dimensions, md_hashes = image_hashes(MD_REFERENCE)
    tiff_dimensions, tiff_hashes = image_hashes(STAGED_TIFF)
    distances = {
        "ahash_64": visual_index.hamming_distance(
            md_hashes["ahash_64"], tiff_hashes["ahash_64"]
        ),
        "dhash_64": visual_index.hamming_distance(
            md_hashes["dhash_64"], tiff_hashes["dhash_64"]
        ),
        "phash_dct_64": visual_index.hamming_distance(
            md_hashes["phash_dct_64"], tiff_hashes["phash_dct_64"]
        ),
    }
    if md_dimensions != (800, 1256) or tiff_dimensions != (3718, 5836):
        raise CorrectionError(
            f"image dimensions drifted: MD={md_dimensions}, TIFF={tiff_dimensions}"
        )
    if sum(distances.values()) != 2:
        raise CorrectionError(f"visual identity evidence drifted: {distances}")

    candidate_fields, candidates = read_csv(CANDIDATES_CSV)
    source_fields, source_dispositions = read_csv(SOURCE_DISPOSITIONS_CSV)
    drain_fields, drain_dispositions = read_csv(DRAIN_DISPOSITIONS_CSV)
    if source_fields != drain_fields or source_dispositions != drain_dispositions:
        raise CorrectionError("source and drain projections disagree before correction")

    source_path = str(SOURCE_TIFF)
    if any(
        row["record_key"] == CANONICAL_KEY and row["source_path"] == source_path
        for row in candidates
    ):
        raise CorrectionError("canonical candidate binding already exists")
    disposition_matches = [
        row for row in source_dispositions if row["source_path"] == source_path
    ]
    if len(disposition_matches) != 1:
        raise CorrectionError(
            f"expected one source disposition for TIFF, found {len(disposition_matches)}"
        )

    with closing(sqlite3.connect(DATABASE, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CorrectionError("SQLite pre-correction quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CorrectionError("SQLite pre-correction foreign-key check failed")
        canonical = connection.execute(
            "SELECT * FROM reconciliation_decisions WHERE record_key=?",
            (CANONICAL_KEY,),
        ).fetchone()
        duplicate = connection.execute(
            "SELECT * FROM reconciliation_decisions WHERE record_key=?",
            (DUPLICATE_KEY,),
        ).fetchone()
        if (
            canonical is None
            or canonical["decision_state"] != "unresolved"
            or canonical["selected_candidate_id"] is not None
        ):
            raise CorrectionError("md:55436 is no longer the expected unresolved decision")
        if (
            duplicate is None
            or duplicate["decision_state"] != "selected_master"
            or duplicate["selected_candidate_id"] != EXPECTED_DUPLICATE_CANDIDATE_ID
        ):
            raise CorrectionError("md:85642 selection drifted")
        duplicate_candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id=?",
            (EXPECTED_DUPLICATE_CANDIDATE_ID,),
        ).fetchone()
        if (
            duplicate_candidate is None
            or duplicate_candidate["record_key"] != DUPLICATE_KEY
            or duplicate_candidate["source_path"] != source_path
        ):
            raise CorrectionError("md:85642 source binding drifted")
        next_candidate_id = int(
            connection.execute("SELECT MAX(candidate_id) FROM candidates").fetchone()[0]
        ) + 1

    new_candidate: dict[str, Any] = {
        "candidate_id": next_candidate_id,
        "record_key": CANONICAL_KEY,
        "inventory": "TFA-538075",
        "source_path": source_path,
        "source_kind": "read_only_usb",
        "original_index_path": MD_REFERENCE_URL,
        "basename": SOURCE_TIFF.name,
        "extension": SOURCE_TIFF.suffix.casefold(),
        "size_bytes": str(source_stat.st_size),
        "mtime_ns": str(source_stat.st_mtime_ns),
        "file_kind": str(duplicate_candidate["file_kind"]),
        "proposed_role": "master_candidate",
        "match_basis": (
            "explicit_user_confirmed_visual_identity_to_md:55436; "
            "corrected_catalogue_binding; md:85642_retained_catalogue_duplicate; "
            "perceptual_hash_total_distance_2"
        ),
        "reused_source_sha256": f"sha256:{source_sha256}",
        "prior_unit": "",
        "prior_disposition": "",
    }
    if set(new_candidate) != set(candidate_fields):
        raise CorrectionError("candidate CSV schema drifted")

    provenance_note = (
        "User-confirmed provenance correction 2026-08-14: this local TIFF is the "
        "high-resolution image represented by museum-digital md:55436 / TFA-538075. "
        "museum-digital md:85642 / TFA-538074 is retained as a duplicate catalogue "
        "record and cross-reference. Exact MD JPEG and byte-identical staged TIFF "
        "are preserved in the approved TFA work root. Source remains read-only; "
        "deletion is not authorized."
    )
    for rows in (source_dispositions, drain_dispositions):
        matches = [row for row in rows if row["source_path"] == source_path]
        if len(matches) != 1:
            raise CorrectionError("disposition binding drifted")
        matches[0]["notes"] = provenance_note

    candidates_tmp = write_csv_temp(
        CANDIDATES_CSV, candidate_fields, candidates + [new_candidate], token
    )
    source_tmp = write_csv_temp(
        SOURCE_DISPOSITIONS_CSV, source_fields, source_dispositions, token
    )
    drain_tmp = write_csv_temp(
        DRAIN_DISPOSITIONS_CSV, drain_fields, drain_dispositions, token
    )

    backup_paths = {
        path.name: str(backup(path, stamp))
        for path in (
            DATABASE,
            CANDIDATES_CSV,
            SOURCE_DISPOSITIONS_CSV,
            DRAIN_DISPOSITIONS_CSV,
            DECISIONS_CSV,
            WORKBENCH_DISPOSITIONS_CSV,
            WORKBENCH_RECEIPT,
        )
    }

    canonical_note = (
        "Selected under explicit user confirmation: TFA-FOT-538074.tif is the "
        "correct high-resolution Master image for md:55436 / TFA-538075. The exact "
        "museum-digital JPEG was preserved locally before this decision; the staged "
        "TIFF is byte-identical to the read-only source. md:85642 / TFA-538074 is a "
        "retained duplicate catalogue record. Source not modified; deletion not authorized."
    )
    duplicate_note = (
        "User-confirmed duplicate catalogue record of md:55436 / TFA-538075. The "
        "shared local image TFA-FOT-538074.tif is canonically bound to md:55436; this "
        "selected_master row is retained solely to preserve the existing one-Master "
        "catalogue projection without a schema change. Source not modified; deletion "
        "not authorized."
    )
    try:
        with closing(sqlite3.connect(DATABASE, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO candidates VALUES (" + ",".join("?" for _ in candidate_fields) + ")",
                [new_candidate[field] for field in candidate_fields],
            )
            connection.execute(
                """
                UPDATE reconciliation_decisions
                SET selected_candidate_id=?, decision_state='selected_master', note=?,
                    decided_by='vladimir', decided_at=?
                WHERE record_key=?
                """,
                (next_candidate_id, canonical_note, corrected_at, CANONICAL_KEY),
            )
            connection.execute(
                "UPDATE objects SET decision_state='resolved' WHERE record_key=?",
                (CANONICAL_KEY,),
            )
            connection.execute(
                """
                UPDATE reconciliation_decisions
                SET note=?, decided_by='vladimir', decided_at=?
                WHERE record_key=? AND decision_state='selected_master'
                  AND selected_candidate_id=?
                """,
                (
                    duplicate_note,
                    corrected_at,
                    DUPLICATE_KEY,
                    EXPECTED_DUPLICATE_CANDIDATE_ID,
                ),
            )
            if connection.total_changes != 4:
                raise CorrectionError(
                    f"unexpected SQLite change count: {connection.total_changes} != 4"
                )
            connection.execute(
                "UPDATE source_disposition_proposal SET notes=? WHERE source_path=?",
                (provenance_note, source_path),
            )
            connection.execute(
                "UPDATE drain_list_proposal SET notes=? WHERE source_path=?",
                (provenance_note, source_path),
            )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise CorrectionError("SQLite post-correction foreign-key check failed")
            decisions = connection.execute(
                """
                SELECT d.record_key, d.decision_state, d.selected_candidate_id,
                       c.record_key AS candidate_record_key, c.source_path, c.extension
                FROM reconciliation_decisions d
                LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.record_key IN (?,?) ORDER BY d.record_key
                """,
                (CANONICAL_KEY, DUPLICATE_KEY),
            ).fetchall()
            if len(decisions) != 2 or any(
                row["decision_state"] != "selected_master"
                or row["candidate_record_key"] != row["record_key"]
                or row["source_path"] != source_path
                or str(row["extension"]).casefold() not in {".tif", ".tiff"}
                for row in decisions
            ):
                raise CorrectionError("post-correction one-Master binding failed")
            connection.commit()
    except Exception:
        for temporary in (candidates_tmp, source_tmp, drain_tmp):
            temporary.unlink(missing_ok=True)
        raise

    os.replace(candidates_tmp, CANDIDATES_CSV)
    os.replace(source_tmp, SOURCE_DISPOSITIONS_CSV)
    os.replace(drain_tmp, DRAIN_DISPOSITIONS_CSV)

    receipt = json.loads(WORKBENCH_RECEIPT.read_text(encoding="utf-8"))
    receipt["evidence_fingerprints"][CANDIDATES_CSV.name] = (
        "sha256:" + sha256_file(CANDIDATES_CSV)
    )
    receipt.update(
        {
            "md_55436_provenance_rebind_at": corrected_at,
            "md_55436_selected_candidate_id": next_candidate_id,
            "md_55436_canonical_record": CANONICAL_KEY,
            "md_85642_duplicate_record": DUPLICATE_KEY,
            "md_reference_path": str(MD_REFERENCE),
            "md_reference_sha256": "sha256:" + md_sha256,
            "staged_master_path": str(STAGED_TIFF),
            "staged_master_sha256": "sha256:" + staged_sha256,
            "staged_master_exact_copy_proven": True,
            "source_mount": mount,
            "source_read_only": True,
            "deletion_authorized": False,
        }
    )
    atomic_json(WORKBENCH_RECEIPT, receipt, token)

    provenance = {
        "format": "iss-tfa-md-record-provenance-correction-v1",
        "corrected_at": corrected_at,
        "decision_authority": "explicit_user_confirmation_2026-08-14",
        "canonical_object": {
            "record_key": CANONICAL_KEY,
            "museum_digital_id": "55436",
            "inventory": "TFA-538075",
            "selected_candidate_id": next_candidate_id,
            "selected_master_source_path": source_path,
        },
        "duplicate_catalogue_record": {
            "record_key": DUPLICATE_KEY,
            "museum_digital_id": "85642",
            "inventory": "TFA-538074",
            "retained_for_provenance": True,
            "schema_representation": (
                "selected_master retained with explicit duplicate note; no new state or schema"
            ),
        },
        "museum_digital_reference": {
            "source_url": MD_REFERENCE_URL,
            "local_path": str(MD_REFERENCE),
            "size_bytes": md_stat.st_size,
            "sha256": "sha256:" + md_sha256,
            "dimensions_px": list(md_dimensions),
            "exact_download_preserved_before_decision_change": True,
        },
        "master": {
            "read_only_source_path": source_path,
            "staged_work_copy_path": str(STAGED_TIFF),
            "size_bytes": source_stat.st_size,
            "sha256": "sha256:" + source_sha256,
            "dimensions_px": list(tiff_dimensions),
            "source_and_staged_copy_byte_identical": True,
        },
        "visual_identity_evidence": {
            "extractors": ["ahash-64-v2", "dhash-64-v2", "phash-dct-64-v2"],
            "hamming_distances": distances,
            "total_distance": sum(distances.values()),
            "decision_role": "supporting evidence; human confirmation is authoritative",
        },
        "backups": backup_paths,
        "source_read_only": True,
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(PROVENANCE, provenance, token)

    print(
        json.dumps(
            {
                "result": "ok",
                "corrected_at": corrected_at,
                "canonical_record": CANONICAL_KEY,
                "duplicate_record": DUPLICATE_KEY,
                "selected_candidate_id": next_candidate_id,
                "md_reference_sha256": "sha256:" + md_sha256,
                "master_sha256": "sha256:" + source_sha256,
                "source_modified": False,
                "deletion_authorized": False,
                "provenance": str(PROVENANCE),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
