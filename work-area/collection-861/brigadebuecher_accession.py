#!/usr/bin/env python3
"""Accession the fully reconciled Brigadebuecher collection 861 unit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


WORK = Path("/mnt/volume/iss-archive-work/collection-861")
RECON = WORK / "work/reconciliation"
UNIT = Path("/mnt/volume/iss-archive/units/brigadebuecher")
SOURCE_ROOT = Path("/media/vladimir/Elements")
DECISIONS = RECON / "brigadebuecher-md-object-decisions.csv"
PAGE_DECISIONS = RECON / "brigadebuecher-md-page-decisions.csv"
SOURCE_DISPOSITION = RECON / "brigadebuecher-md-object-source-disposition.csv"
DOCUMENT_APPRAISAL = RECON / "brigadebuecher-document-appraisal.jsonl"
MD_OBJECTS = RECON / "brigadebuecher-md-objects.csv"
METADATA_COMPARISON = RECON / "brigadebuecher-metadata-comparison.jsonl"
WORKBENCH = WORK / "brigadebuecher_md_workbench.py"
EXPECTED_ROLES = {
    "ordered_tiff_page_set": 69,
    "ordered_jpeg_page_set": 3,
    "surviving_pdf_exception": 1,
}
EXPECTED_SOURCE_ROWS = 9256
EXPECTED_PAGE_DECISIONS = 701
EXPECTED_DOCUMENT_ROWS = 297
EXPECTED_SELECTED_DOCUMENTS = 15
EXPECTED_DOCUMENT_GROUPS = 10
BLOCK = 8 * 1024 * 1024


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def copy_exact(source: Path, target: Path) -> tuple[str, str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(BLOCK), b""):
            source_digest.update(block)
            outgoing.write(block)
            size += len(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    final_digest, final_size = hash_file(target)
    source_hex = source_digest.hexdigest()
    if source_hex != final_digest or size != final_size:
        raise AccessionError(f"copy verification failed: {source} -> {target}")
    return source_hex, final_digest, final_size


def normalized_hash(value: str) -> str:
    value = value.strip().lower()
    return value.removeprefix("sha256:")


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise AccessionError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise AccessionError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def approved_source(raw: str) -> Path:
    path = Path(raw).resolve(strict=True)
    if not path.is_file():
        raise AccessionError(f"selected source is not a file: {path}")
    try:
        path.relative_to(SOURCE_ROOT.resolve(strict=True))
    except ValueError as exc:
        raise AccessionError(f"selected source is outside protected source root: {path}") from exc
    return path


def safe_inventory(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise AccessionError(f"unsafe inventory for final path: {value!r}")
    return value


def document_name(document_id: str, filename: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß._-]+", "-", filename).strip("-.")
    if not clean:
        raise AccessionError(f"document filename cannot be normalized: {filename!r}")
    return f"{document_id}__{clean}"


def manifest_hash(members: list[dict[str, Any]]) -> str:
    projection = [
        {
            "sequence": int(row["sequence"]),
            "final_relative_path": row["final_relative_path"],
            "final_sha256": row["final_sha256"],
            "size_bytes": int(row["size_bytes"]),
        }
        for row in members
    ]
    payload = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def workbench_check() -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(WORKBENCH), "--actor", "vladimir", "--check"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def build_plan(*, require_target_absent: bool) -> dict[str, Any]:
    mount = source_guard()
    check = workbench_check()
    if not check.get("ok") or check.get("resolved") != 73 or check.get("documentation_unresolved") != 0:
        raise AccessionError(f"workbench readiness check failed: {check}")
    if require_target_absent and UNIT.exists():
        raise AccessionError(f"approved target already exists: {UNIT}")

    decision_fields, raw_decisions = read_csv(DECISIONS)
    _, raw_pages = read_csv(PAGE_DECISIONS)
    _, source_rows = read_csv(SOURCE_DISPOSITION)
    documents = read_jsonl(DOCUMENT_APPRAISAL)
    _, md_rows = read_csv(MD_OBJECTS)
    comparisons = read_jsonl(METADATA_COMPARISON)
    if len(raw_decisions) != 73 or len({row["object_key"] for row in raw_decisions}) != 73:
        raise AccessionError("expected 73 unique object decisions")
    if Counter(row["master_representation_role"] for row in raw_decisions) != Counter(EXPECTED_ROLES):
        raise AccessionError("unexpected Master role counts")
    if any(row["outcome"] != "selected_master" for row in raw_decisions):
        raise AccessionError("all objects must have an explicit selected Master")
    if any(row["accession_state"] != "not_accessioned" for row in raw_decisions):
        raise AccessionError("preflight requires all objects to be not_accessioned")
    if any(row["deletion_authorized"].casefold() != "false" for row in raw_decisions):
        raise AccessionError("object decisions cannot authorize deletion")
    if len(raw_pages) != EXPECTED_PAGE_DECISIONS or any(
        row["deletion_authorized"].casefold() != "false" for row in raw_pages
    ):
        raise AccessionError("page-decision count or deletion guard is invalid")
    if len(source_rows) != EXPECTED_SOURCE_ROWS or len({row["source_path"] for row in source_rows}) != EXPECTED_SOURCE_ROWS:
        raise AccessionError("source disposition is incomplete")
    if any(row["deletion_authorized"].casefold() != "false" for row in source_rows):
        raise AccessionError("source disposition cannot authorize deletion")
    if len(documents) != EXPECTED_DOCUMENT_ROWS or any(
        not row.get("decision") or row.get("deletion_authorized") for row in documents
    ):
        raise AccessionError("documentation appraisal is incomplete")
    selected_documents = [row for row in documents if row.get("selected_for_unit") is True]
    groups = {row["document_group"] for row in selected_documents}
    if len(selected_documents) != EXPECTED_SELECTED_DOCUMENTS or len(groups) != EXPECTED_DOCUMENT_GROUPS:
        raise AccessionError("selected documentation counts are wrong")
    for group in groups:
        if sum(
            row["document_group"] == group and row["representation_role"] == "selected_master"
            for row in selected_documents
        ) != 1:
            raise AccessionError(f"documentation group does not have one selected Master: {group}")
    md_by_id = {row["object_id"]: row for row in md_rows}
    comparison_by_id = {str(row["md_object_id"]): row for row in comparisons}
    if len(md_by_id) != 73 or set(md_by_id) != {row["md_object_id"] for row in raw_decisions}:
        raise AccessionError("museum-digital metadata does not align with decisions")
    if len(comparison_by_id) != 73 or any(
        row.get("metadata_authority") != "live_museum_digital_capture" for row in comparisons
    ):
        raise AccessionError("live museum-digital metadata authority is incomplete")
    disposition_by_path = {row["source_path"]: row for row in source_rows}

    objects: list[dict[str, Any]] = []
    selected_references: list[Path] = []
    output_paths: set[str] = set()
    for row in raw_decisions:
        inventory = safe_inventory(row["inventory"])
        role = row["master_representation_role"]
        tiff_paths = json.loads(row["selected_tiff_master_paths"] or "[]")
        non_tiff_paths = json.loads(row["selected_non_tiff_master_paths"] or "[]")
        master_raw = tiff_paths if role == "ordered_tiff_page_set" else non_tiff_paths
        if not master_raw or len(master_raw) != len(set(master_raw)):
            raise AccessionError(f"Master representation is empty or repeats a member: {row['object_key']}")
        masters = [approved_source(value) for value in master_raw]
        suffixes = {path.suffix.casefold() for path in masters}
        if role == "ordered_tiff_page_set" and not suffixes <= {".tif", ".tiff"}:
            raise AccessionError(f"non-TIFF member in TIFF Master: {row['object_key']}")
        if role == "ordered_jpeg_page_set" and not suffixes <= {".jpg", ".jpeg"}:
            raise AccessionError(f"non-JPEG member in JPEG Master: {row['object_key']}")
        if role == "surviving_pdf_exception" and (len(masters) != 1 or suffixes != {".pdf"}):
            raise AccessionError(f"invalid surviving PDF exception: {row['object_key']}")
        access = approved_source(row["retained_md_pdf_path"])
        if access.suffix.casefold() != ".pdf":
            raise AccessionError(f"access representation is not PDF: {row['object_key']}")
        members = []
        for sequence, source in enumerate(masters, 1):
            if role == "surviving_pdf_exception":
                relative = f"masters/{inventory}/{source.name}"
            else:
                relative = f"masters/{inventory}/pages/{source.name}"
            if relative in output_paths:
                raise AccessionError(f"final Master path collision: {relative}")
            output_paths.add(relative)
            members.append({"sequence": sequence, "source": source, "relative": relative})
        access_relative = f"export/{inventory}.pdf"
        if access_relative in output_paths:
            raise AccessionError(f"final access path collision: {access_relative}")
        output_paths.add(access_relative)
        for source in masters + [access]:
            if str(source) not in disposition_by_path:
                raise AccessionError(f"selected path absent from source disposition: {source}")
            selected_references.append(source)
        objects.append(
            {
                "decision": row,
                "metadata": md_by_id[row["md_object_id"]],
                "comparison": comparison_by_id[row["md_object_id"]],
                "master_members": members,
                "access_source": access,
                "access_relative": access_relative,
            }
        )

    document_plan = []
    for row in selected_documents:
        source = approved_source(row["source_path"])
        relative = f"documents/{document_name(row['document_id'], row['filename'])}"
        if relative in output_paths:
            raise AccessionError(f"final document path collision: {relative}")
        output_paths.add(relative)
        selected_references.append(source)
        document_plan.append({"appraisal": row, "source": source, "relative": relative})

    required_bytes = sum(path.stat().st_size for path in selected_references)
    free_bytes = shutil.disk_usage(UNIT.parent).free
    if free_bytes < required_bytes + max(required_bytes // 10, 10 * 1024**3):
        raise AccessionError("insufficient destination capacity")
    return {
        "mount": mount,
        "workbench_check": check,
        "decision_fields": decision_fields,
        "decisions": raw_decisions,
        "page_decisions": raw_pages,
        "source_rows": source_rows,
        "documents": documents,
        "selected_documents": document_plan,
        "objects": objects,
        "required_bytes": required_bytes,
        "free_bytes": free_bytes,
    }


def update_states(
    decisions: list[dict[str, str]], fields: list[str], state: str, receipt_paths: dict[str, str] | None = None
) -> None:
    expected = "not_accessioned" if state == "accession_in_progress" else "accession_in_progress"
    for row in decisions:
        if row["accession_state"] != expected:
            raise AccessionError(f"invalid accession transition {row['accession_state']} -> {state}")
        row["accession_state"] = state
        if state == "verified_authority":
            receipt = (receipt_paths or {}).get(row["object_key"], "")
            if not receipt:
                raise AccessionError(f"missing receipt for {row['object_key']}")
            row["accession_receipt_path"] = receipt
            row["metadata_authority"] = "archive_unit_authority_MD_projection"
    atomic_bytes(DECISIONS, csv_bytes(fields, decisions))


def write_sqlite(
    catalog: list[dict[str, Any]],
    representations: list[dict[str, Any]],
    members: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    source_rows: list[dict[str, str]],
    decisions: list[dict[str, str]],
    pages: list[dict[str, str]],
    appraisal: list[dict[str, Any]],
) -> None:
    path = UNIT / "db-restore/RESTORE.sqlite"
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE catalog (
              object_key TEXT PRIMARY KEY, md_object_id TEXT NOT NULL UNIQUE,
              inventory TEXT NOT NULL UNIQUE, title TEXT NOT NULL, object_type TEXT NOT NULL,
              date_text TEXT NOT NULL, description TEXT NOT NULL, material_technique TEXT NOT NULL,
              dimensions TEXT NOT NULL, md_url TEXT NOT NULL, metadata_rights TEXT NOT NULL,
              md_last_updated TEXT NOT NULL, master_role TEXT NOT NULL,
              master_relative_path TEXT NOT NULL, master_member_count INTEGER NOT NULL,
              master_manifest_sha256 TEXT NOT NULL, access_relative_path TEXT NOT NULL,
              access_sha256 TEXT NOT NULL, access_size_bytes INTEGER NOT NULL,
              metadata_authority TEXT NOT NULL, accession_result TEXT NOT NULL,
              deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE representations (
              representation_id TEXT PRIMARY KEY, object_key TEXT NOT NULL REFERENCES catalog,
              inventory TEXT NOT NULL, role TEXT NOT NULL, representation_type TEXT NOT NULL,
              final_relative_path TEXT NOT NULL, member_count INTEGER NOT NULL,
              representation_manifest_sha256 TEXT NOT NULL, copy_policy TEXT NOT NULL,
              basis TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE representation_members (
              representation_id TEXT NOT NULL REFERENCES representations, sequence INTEGER NOT NULL,
              source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL,
              final_relative_path TEXT NOT NULL UNIQUE, final_sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL, PRIMARY KEY(representation_id, sequence)
            );
            CREATE TABLE documents (
              document_id TEXT PRIMARY KEY, document_group TEXT NOT NULL,
              representation_role TEXT NOT NULL, source_path TEXT NOT NULL,
              source_sha256 TEXT NOT NULL, final_relative_path TEXT NOT NULL UNIQUE,
              final_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
              decision TEXT NOT NULL, content_evidence TEXT NOT NULL,
              deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE source_disposition (
              source_path TEXT PRIMARY KEY, object_keys TEXT NOT NULL, base_inventory TEXT NOT NULL,
              source_kind TEXT NOT NULL, candidate_role TEXT NOT NULL, disposition TEXT NOT NULL,
              accession_relation TEXT NOT NULL, drain_state TEXT NOT NULL, size_bytes INTEGER NOT NULL,
              content_sha256 TEXT NOT NULL, evidence_basis TEXT NOT NULL,
              deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE object_decisions (
              object_key TEXT PRIMARY KEY, md_object_id TEXT NOT NULL, inventory TEXT NOT NULL,
              outcome TEXT NOT NULL, master_representation_role TEXT NOT NULL,
              selected_tiff_candidate_id TEXT NOT NULL, selected_tiff_master_paths TEXT NOT NULL,
              selected_non_tiff_master_paths TEXT NOT NULL, reviewed_missing_pdf_pages TEXT NOT NULL,
              retained_md_pdf_path TEXT NOT NULL, redundant_candidate_paths TEXT NOT NULL,
              note TEXT NOT NULL, actor TEXT NOT NULL, decided_utc TEXT NOT NULL,
              accession_state TEXT NOT NULL, accession_receipt_path TEXT NOT NULL,
              metadata_authority TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE page_decisions (
              object_key TEXT NOT NULL, md_object_id TEXT NOT NULL, inventory TEXT NOT NULL,
              pdf_page INTEGER NOT NULL, outcome TEXT NOT NULL, selected_tiff_path TEXT NOT NULL,
              selected_tiff_relation TEXT NOT NULL, replaced_automatic_tiff_path TEXT NOT NULL,
              note TEXT NOT NULL, actor TEXT NOT NULL, decided_utc TEXT NOT NULL,
              deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0),
              PRIMARY KEY(object_key,pdf_page)
            );
            CREATE TABLE document_appraisal (
              document_id TEXT PRIMARY KEY, source_path TEXT NOT NULL UNIQUE,
              document_group TEXT NOT NULL, representation_role TEXT NOT NULL,
              selected_for_unit INTEGER NOT NULL, decision TEXT NOT NULL,
              review_state TEXT NOT NULL, content_sha256 TEXT NOT NULL,
              deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            """
        )
        con.executemany(
            "INSERT INTO catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            [tuple(row[field] for field in CATALOG_FIELDS[:-1]) for row in catalog],
        )
        con.executemany(
            "INSERT INTO representations VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            [tuple(row[field] for field in REPRESENTATION_FIELDS[:-1]) for row in representations],
        )
        con.executemany(
            "INSERT INTO representation_members VALUES (?,?,?,?,?,?,?)",
            [tuple(row[field] for field in MEMBER_FIELDS) for row in members],
        )
        con.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            [tuple(row[field] for field in DOCUMENT_FIELDS[:-1]) for row in documents],
        )
        con.executemany(
            "INSERT INTO source_disposition VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            [
                (
                    row["source_path"], row["object_keys"], row["base_inventory"], row["source_kind"],
                    row["candidate_role"], row["disposition"], row["accession_relation"],
                    row["drain_state"], int(row["size_bytes"] or 0), row["content_sha256"],
                    row["evidence_basis"],
                )
                for row in source_rows
            ],
        )
        con.executemany(
            "INSERT INTO object_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            [
                (
                    row["object_key"], row["md_object_id"], row["inventory"], row["outcome"],
                    row["master_representation_role"], row["selected_tiff_candidate_id"],
                    row["selected_tiff_master_paths"], row["selected_non_tiff_master_paths"],
                    row["reviewed_missing_pdf_pages"], row["retained_md_pdf_path"],
                    row["redundant_candidate_paths"], row["note"], row["actor"], row["decided_utc"],
                    row["accession_state"], row["accession_receipt_path"], row["metadata_authority"],
                )
                for row in decisions
            ],
        )
        con.executemany(
            "INSERT INTO page_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            [
                (
                    row["object_key"], row["md_object_id"], row["inventory"], int(row["pdf_page"]),
                    row["outcome"], row["selected_tiff_path"], row["selected_tiff_relation"],
                    row["replaced_automatic_tiff_path"], row["note"], row["actor"], row["decided_utc"],
                )
                for row in pages
            ],
        )
        con.executemany(
            "INSERT INTO document_appraisal VALUES (?,?,?,?,?,?,?,?,0)",
            [
                (
                    row["document_id"], row["source_path"], row["document_group"],
                    row["representation_role"], int(bool(row["selected_for_unit"])), row["decision"],
                    row["review_state"], row["content_sha256"],
                )
                for row in appraisal
            ],
        )
        con.commit()
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("RESTORE.sqlite integrity check failed")
    finally:
        con.close()


CATALOG_FIELDS = (
    "object_key", "md_object_id", "inventory", "title", "object_type", "date_text",
    "description", "material_technique", "dimensions", "md_url", "metadata_rights",
    "md_last_updated", "master_role", "master_relative_path", "master_member_count",
    "master_manifest_sha256", "access_relative_path", "access_sha256", "access_size_bytes",
    "metadata_authority", "accession_result", "deletion_authorized",
)
REPRESENTATION_FIELDS = (
    "representation_id", "object_key", "inventory", "role", "representation_type",
    "final_relative_path", "member_count", "representation_manifest_sha256", "copy_policy",
    "basis", "deletion_authorized",
)
MEMBER_FIELDS = (
    "representation_id", "sequence", "source_path", "source_sha256",
    "final_relative_path", "final_sha256", "size_bytes",
)
DOCUMENT_FIELDS = (
    "document_id", "document_group", "representation_role", "source_path", "source_sha256",
    "final_relative_path", "final_sha256", "size_bytes", "decision", "content_evidence",
    "deletion_authorized",
)
CHECKSUM_FIELDS = ("relative_path", "size_bytes", "sha256", "payload_type")


def accession() -> dict[str, Any]:
    plan = build_plan(require_target_absent=True)
    update_states(plan["decisions"], plan["decision_fields"], "accession_in_progress")
    workbench_check()
    UNIT.mkdir(parents=False)
    for relative in ("00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"):
        (UNIT / relative).mkdir()
    started = now()
    atomic_json(
        UNIT / "00_MANIFESTS/accession-preparation.json",
        {
            "format": "iss-brigadebuecher-accession-preparation-v1",
            "started_utc": started,
            "source_mount": plan["mount"],
            "object_count": 73,
            "master_role_counts": EXPECTED_ROLES,
            "selected_document_representations": EXPECTED_SELECTED_DOCUMENTS,
            "required_source_bytes": plan["required_bytes"],
            "target_unit": str(UNIT),
            "deletion_authorized": False,
        },
    )

    final_hashes: dict[str, tuple[str, int, str]] = {}
    members: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    receipts: dict[str, str] = {}
    for index, obj in enumerate(plan["objects"], 1):
        decision = obj["decision"]
        inventory = decision["inventory"]
        object_key = decision["object_key"]
        master_id = f"iss:brigadebuecher:{inventory}:master"
        master_rows = []
        for item in obj["master_members"]:
            source_sha, final_sha, size = copy_exact(item["source"], UNIT / item["relative"])
            expected = normalized_hash(plan["source_rows"][0].get("content_sha256", ""))
            disposition = next(row for row in plan["source_rows"] if row["source_path"] == str(item["source"]))
            expected = normalized_hash(disposition.get("content_sha256", ""))
            if expected and expected != source_sha:
                raise AccessionError(f"indexed source hash changed: {item['source']}")
            member = {
                "representation_id": master_id,
                "sequence": item["sequence"],
                "source_path": str(item["source"]),
                "source_sha256": source_sha,
                "final_relative_path": item["relative"],
                "final_sha256": final_sha,
                "size_bytes": size,
            }
            members.append(member)
            master_rows.append(member)
            final_hashes[item["relative"]] = (final_sha, size, "preservation_master_member")
        master_sha = manifest_hash(master_rows)
        master_root = f"masters/{inventory}"
        representations.append(
            {
                "representation_id": master_id,
                "object_key": object_key,
                "inventory": inventory,
                "role": "preservation_master",
                "representation_type": decision["master_representation_role"],
                "final_relative_path": master_root,
                "member_count": len(master_rows),
                "representation_manifest_sha256": master_sha,
                "copy_policy": "exact_source_bytes",
                "basis": "explicit_human_master_selection",
                "deletion_authorized": "false",
            }
        )

        access_id = f"iss:brigadebuecher:{inventory}:access-pdf"
        source_sha, final_sha, size = copy_exact(obj["access_source"], UNIT / obj["access_relative"])
        disposition = next(row for row in plan["source_rows"] if row["source_path"] == str(obj["access_source"]))
        expected = normalized_hash(disposition.get("content_sha256", ""))
        if expected and expected != source_sha:
            raise AccessionError(f"indexed source hash changed: {obj['access_source']}")
        access_member = {
            "representation_id": access_id,
            "sequence": 1,
            "source_path": str(obj["access_source"]),
            "source_sha256": source_sha,
            "final_relative_path": obj["access_relative"],
            "final_sha256": final_sha,
            "size_bytes": size,
        }
        members.append(access_member)
        final_hashes[obj["access_relative"]] = (final_sha, size, "retained_md_pdf_access_derivative")
        access_manifest_sha = manifest_hash([access_member])
        representations.append(
            {
                "representation_id": access_id,
                "object_key": object_key,
                "inventory": inventory,
                "role": "access_derivative",
                "representation_type": "retained_museum_digital_compatible_pdf",
                "final_relative_path": obj["access_relative"],
                "member_count": 1,
                "representation_manifest_sha256": access_manifest_sha,
                "copy_policy": "exact_source_bytes",
                "basis": "explicitly_retained_MD_PDF_access_representation",
                "deletion_authorized": "false",
            }
        )
        md = obj["metadata"]
        catalog.append(
            {
                "object_key": object_key,
                "md_object_id": decision["md_object_id"],
                "inventory": inventory,
                "title": md["title"],
                "object_type": md["object_type"],
                "date_text": md["date"],
                "description": md["description"],
                "material_technique": md["material_technique"],
                "dimensions": md["dimensions"],
                "md_url": md["object_url"],
                "metadata_rights": md["metadata_rights"],
                "md_last_updated": md["last_updated"],
                "master_role": decision["master_representation_role"],
                "master_relative_path": master_root,
                "master_member_count": len(master_rows),
                "master_manifest_sha256": master_sha,
                "access_relative_path": obj["access_relative"],
                "access_sha256": final_sha,
                "access_size_bytes": size,
                "metadata_authority": "live_museum_digital_capture_plus_reconciled_local_provenance",
                "accession_result": "accessioned",
                "deletion_authorized": "false",
            }
        )
        receipt_path = UNIT / f"00_MANIFESTS/accession-receipt-md-{decision['md_object_id']}.json"
        atomic_json(
            receipt_path,
            {
                "format": "iss-brigadebuecher-object-accession-receipt-v1",
                "object_key": object_key,
                "inventory": inventory,
                "master_representation_role": decision["master_representation_role"],
                "master_relative_path": master_root,
                "master_member_count": len(master_rows),
                "master_manifest_sha256": master_sha,
                "access_relative_path": obj["access_relative"],
                "access_sha256": final_sha,
                "accession_verified": True,
                "deletion_authorized": False,
            },
        )
        receipts[object_key] = str(receipt_path)
        print(f"[{index:02d}/73] verified {inventory}: {len(master_rows)} Master member(s) + PDF", flush=True)

    document_rows: list[dict[str, Any]] = []
    for item in plan["selected_documents"]:
        source_sha, final_sha, size = copy_exact(item["source"], UNIT / item["relative"])
        appraisal = item["appraisal"]
        expected = normalized_hash(appraisal.get("content_sha256", ""))
        if expected and expected != source_sha:
            raise AccessionError(f"appraised document hash changed: {item['source']}")
        row = {
            "document_id": appraisal["document_id"],
            "document_group": appraisal["document_group"],
            "representation_role": appraisal["representation_role"],
            "source_path": str(item["source"]),
            "source_sha256": source_sha,
            "final_relative_path": item["relative"],
            "final_sha256": final_sha,
            "size_bytes": size,
            "decision": appraisal["decision"],
            "content_evidence": appraisal["content_evidence"],
            "deletion_authorized": "false",
        }
        document_rows.append(row)
        final_hashes[item["relative"]] = (final_sha, size, "selected_document_representation")
    print(f"[documents] verified {len(document_rows)} selected representation(s)", flush=True)

    update_states(plan["decisions"], plan["decision_fields"], "verified_authority", receipts)
    final_check = workbench_check()
    _, final_source_rows = read_csv(SOURCE_DISPOSITION)
    _, final_decisions = read_csv(DECISIONS)
    if final_check.get("resolved") != 73 or any(row["accession_state"] != "verified_authority" for row in final_decisions):
        raise AccessionError("workbench did not accept verified accession authority")

    atomic_bytes(UNIT / "CATALOG.csv", csv_bytes(CATALOG_FIELDS, catalog))
    atomic_bytes(
        UNIT / "00_MANIFESTS/representations.csv",
        csv_bytes(REPRESENTATION_FIELDS, representations),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/representation-members.csv",
        csv_bytes(MEMBER_FIELDS, members),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/document-provenance.csv",
        csv_bytes(DOCUMENT_FIELDS, document_rows),
    )
    source_fields = list(final_source_rows[0])
    atomic_bytes(
        UNIT / "00_MANIFESTS/source-disposition.csv",
        csv_bytes(source_fields, final_source_rows),
    )
    shutil.copyfile(MD_OBJECTS, UNIT / "metadata/museum-digital-objects.csv")
    shutil.copyfile(METADATA_COMPARISON, UNIT / "metadata/metadata-comparison.jsonl")
    shutil.copyfile(DECISIONS, UNIT / "db-restore/reconciliation-decisions.csv")
    shutil.copyfile(PAGE_DECISIONS, UNIT / "db-restore/page-decisions.csv")
    shutil.copyfile(DOCUMENT_APPRAISAL, UNIT / "db-restore/document-appraisal.jsonl")
    shutil.copyfile(UNIT / "CATALOG.csv", UNIT / "db-restore/RESTORE.csv")
    write_sqlite(
        catalog, representations, members, document_rows, final_source_rows,
        final_decisions, plan["page_decisions"], plan["documents"],
    )
    con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        sql = "\n".join(con.iterdump()) + "\n"
    finally:
        con.close()
    atomic_bytes(UNIT / "db-restore/RESTORE.sql", sql.encode("utf-8"))
    readme = f"""# Brigadebücher archive unit

This unit contains the fully reconciled and accessioned Brigadebücher collection 861.

- Archival objects: 73
- Selected Master representations: 73
- Ordered TIFF page-set Masters: 69
- Ordered high-resolution JPEG page-set Masters: 3
- Approved surviving PDF Master exception: 1 (BB-28_3)
- Retained museum-digital-compatible PDF access derivatives: 73
- Selected documentation groups: 10
- Selected documentation representations: 15
- Unresolved Master or documentation decisions: 0
- Deletion authorized: false

`masters/` contains exactly one explicitly selected Master representation per object. An
ordered page-set Master is stored under its inventory and `pages/`; the directory is one
representation and `00_MANIFESTS/representation-members.csv` records its ordered members
and provenance. BB-28_3 retains the explicitly approved higher-quality surviving PDF as
its Master exception.

`export/` contains the one retained museum-digital-compatible PDF access derivative for
each object. These PDFs were explicitly retained by the approved reconciliation model;
they are not competing Master candidates. `documents/` contains the 15 content-reviewed
project-document representations belonging to 10 documentation objects.

`CATALOG.csv` is the human-readable object catalogue. `00_MANIFESTS/` records every
representation member, original source path, final path, disposition, and final hash.
`db-restore/` contains a rebuildable SQLite catalogue, SQL export, and the final explicit
object, page, and documentation decisions. Source files remain unchanged and read-only.
Redundant never means deletion-authorized.
"""
    atomic_bytes(UNIT / "00_README.md", readme.encode("utf-8"))

    excluded = {
        "00_MANIFESTS/payload-checksums.csv",
        "00_MANIFESTS/accession-completion.json",
    }
    checksum_rows = []
    for path in sorted(item for item in UNIT.rglob("*") if item.is_file()):
        relative = path.relative_to(UNIT).as_posix()
        if relative in excluded:
            continue
        if relative in final_hashes:
            digest, size, payload_type = final_hashes[relative]
        else:
            digest, size = hash_file(path)
            payload_type = "unit_record"
        checksum_rows.append(
            {"relative_path": relative, "size_bytes": size, "sha256": digest, "payload_type": payload_type}
        )
    atomic_bytes(
        UNIT / "00_MANIFESTS/payload-checksums.csv",
        csv_bytes(CHECKSUM_FIELDS, checksum_rows),
    )
    checksum_sha, checksum_size = hash_file(UNIT / "00_MANIFESTS/payload-checksums.csv")
    sqlite_con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        integrity = sqlite_con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            table: sqlite_con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "catalog", "representations", "representation_members", "documents",
                "source_disposition", "object_decisions", "page_decisions", "document_appraisal",
            )
        }
    finally:
        sqlite_con.close()
    expected_counts = {
        "catalog": 73,
        "representations": 146,
        "representation_members": len(members),
        "documents": 15,
        "source_disposition": 9256,
        "object_decisions": 73,
        "page_decisions": 701,
        "document_appraisal": 297,
    }
    actual_payload_paths = {
        path.relative_to(UNIT).as_posix()
        for path in UNIT.rglob("*")
        if path.is_file() and path.relative_to(UNIT).as_posix() not in excluded
    }
    manifest_paths = {row["relative_path"] for row in checksum_rows}
    if integrity != "ok" or counts != expected_counts or actual_payload_paths != manifest_paths:
        raise AccessionError(
            f"final structural validation failed: integrity={integrity} counts={counts} path_match={actual_payload_paths == manifest_paths}"
        )
    completed = now()
    completion = {
        "format": "iss-brigadebuecher-accession-completion-v1",
        "started_utc": started,
        "completed_utc": completed,
        "target_unit": str(UNIT),
        "source_mount": source_guard(),
        "objects": 73,
        "selected_master_representations": 73,
        "master_role_counts": EXPECTED_ROLES,
        "master_members": sum(
            int(row["member_count"])
            for row in representations
            if row["role"] == "preservation_master"
        ),
        "retained_pdf_access_derivatives": 73,
        "selected_document_representations": 15,
        "documentation_groups": 10,
        "source_disposition_rows": 9256,
        "payload_checksum_rows": len(checksum_rows),
        "payload_checksum_manifest_sha256": checksum_sha,
        "payload_checksum_manifest_size_bytes": checksum_size,
        "sqlite_integrity": integrity,
        "sqlite_counts": counts,
        "workbench_check": final_check,
        "accession_verified": True,
        "deletion_authorized": False,
    }
    atomic_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    print(json.dumps(completion, ensure_ascii=False, indent=2), flush=True)
    return completion


def validate_existing() -> dict[str, Any]:
    if not UNIT.is_dir():
        raise AccessionError(f"unit is absent: {UNIT}")
    completion = json.loads((UNIT / "00_MANIFESTS/accession-completion.json").read_text())
    _, checksums = read_csv(UNIT / "00_MANIFESTS/payload-checksums.csv")
    _, catalog = read_csv(UNIT / "CATALOG.csv")
    _, representations = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    _, members = read_csv(UNIT / "00_MANIFESTS/representation-members.csv")
    _, documents = read_csv(UNIT / "00_MANIFESTS/document-provenance.csv")
    _, dispositions = read_csv(UNIT / "00_MANIFESTS/source-disposition.csv")
    con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    actual = {
        path.relative_to(UNIT).as_posix()
        for path in UNIT.rglob("*")
        if path.is_file()
        and path.relative_to(UNIT).as_posix()
        not in {"00_MANIFESTS/payload-checksums.csv", "00_MANIFESTS/accession-completion.json"}
    }
    listed = {row["relative_path"] for row in checksums}
    expected_member_count = int(completion["master_members"]) + int(
        completion["retained_pdf_access_derivatives"]
    )
    result = {
        "ok": bool(
            completion.get("accession_verified")
            and completion.get("deletion_authorized") is False
            and len(catalog) == 73
            and Counter(row["role"] for row in representations) == {"preservation_master": 73, "access_derivative": 73}
            and len(members) == expected_member_count
            and len(documents) == 15
            and len(dispositions) == 9256
            and all(row["deletion_authorized"].casefold() == "false" for row in dispositions)
            and actual == listed
            and integrity == "ok"
        ),
        "objects": len(catalog),
        "representations": len(representations),
        "representation_members": len(members),
        "documents": len(documents),
        "source_disposition_rows": len(dispositions),
        "payload_checksum_rows": len(checksums),
        "filesystem_matches_checksum_manifest": actual == listed,
        "sqlite_integrity": integrity,
        "source_mount": source_guard(),
        "deletion_authorized_true": sum(row["deletion_authorized"].casefold() == "true" for row in dispositions),
    }
    if not result["ok"]:
        raise AccessionError(f"existing unit validation failed: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="read-only preflight")
    group.add_argument("--run", action="store_true", help="perform approved accession")
    group.add_argument("--validate", action="store_true", help="validate completed unit structurally")
    args = parser.parse_args()
    try:
        if args.check:
            plan = build_plan(require_target_absent=True)
            result = {
                "ok": True,
                "source_mount": plan["mount"],
                "objects": len(plan["objects"]),
                "master_role_counts": EXPECTED_ROLES,
                "page_decisions": len(plan["page_decisions"]),
                "selected_document_representations": len(plan["selected_documents"]),
                "selected_source_bytes": plan["required_bytes"],
                "destination_free_bytes": plan["free_bytes"],
                "target_absent": not UNIT.exists(),
                "deletion_authorized": False,
            }
        elif args.run:
            result = accession()
        else:
            result = validate_existing()
        if not args.run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AccessionError, OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
