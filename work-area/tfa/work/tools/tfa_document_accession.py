#!/usr/bin/env python3
"""Accession the approved, scope-reviewed TFA documentation package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work")
UNIT = Path("/mnt/volume/iss-archive/units/tfa")
SOURCE_ROOT = Path("/media/vladimir/Elements")
RECON = WORK_ROOT / "reconciliation/unit-reconciliation"
PLAN = RECON / "tfa-document-accession-plan.scope-reviewed.csv"
PROPOSED_DISPOSITION = RECON / "tfa-document-source-disposition-proposal.scope-reviewed.csv"
PREPARATION = RECON / "tfa-document-accession-preparation.scope-reviewed.json"
PREPARED_STAGE = RECON / "tfa-document-accession-stage.scope-reviewed.zip"
RUN = WORK_ROOT / "accession-runs/tfa-documents-001"
STAGE = RUN / "stage"
BACKUP = RUN / "backup-controls"
COMPLETION = RUN / "completion.json"
DB = UNIT / "db-restore/RESTORE.sqlite"

APPROVED_OUTPUTS = (
    "documents/historical/drafts-and-superseded.zip",
    "documents/project-final/01-project-completion-presentation.pptx",
    "documents/project-final/02-project-presentation.pptx",
    "documents/project-final/05-preservation-submission-manifest.txt",
    "documents/project-final/06-scan-corrections.txt",
    "documents/restricted/contracts.zip",
    "documents/restricted/finance.zip",
)
PACKAGE_OUTPUTS = {
    "documents/historical/drafts-and-superseded.zip": ("historical_document_package", "Historical TFA working inventories, drafts, version chains, and contextual project evidence", "", 11),
    "documents/restricted/contracts.zip": ("restricted_contract_package", "Restricted TFA project contracts, grant records, offers, and delivery confirmations", "restricted", 7),
    "documents/restricted/finance.zip": ("restricted_finance_package", "Restricted TFA project invoices and finance records", "restricted", 2),
}
DIRECT_DESCRIPTIONS = {
    "documents/project-final/01-project-completion-presentation.pptx": "TFA digitization project completion presentation",
    "documents/project-final/02-project-presentation.pptx": "TFA digitization project presentation",
    "documents/project-final/05-preservation-submission-manifest.txt": "TFA digitization preservation submission manifest",
    "documents/project-final/06-scan-corrections.txt": "TFA scan-correction record",
}
DOCUMENT_FIELDS = (
    "document_id", "final_relative_path", "filename", "file_role", "description", "date",
    "source_path", "source_kind", "relationship", "access_restriction", "condition",
    "disposition", "package_member_count", "size_bytes", "sha256", "deletion_authorized",
)
SOURCE_FIELDS = (
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state", "deletion_authorized", "notes",
)


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(fields: list[str] | tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tfa-doc-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict[str, object]) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT / "restore/archive")],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise AccessionError("Elements mount cannot be resolved")
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise AccessionError("Elements mount resolution is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise AccessionError(f"Elements is not protected /dev/sdf1 ntfs3 read-only: {mount}")
    return {
        "source": str(mount.get("source", "")), "target": str(mount.get("target", "")),
        "fstype": str(mount.get("fstype", "")), "options": str(mount.get("options", "")),
    }


def normalized_hash(value: str) -> str:
    value = value.strip().lower()
    return value if value.startswith("sha256:") else "sha256:" + value


def load_scope() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    plan = csv_rows(PLAN)
    proposed = csv_rows(PROPOSED_DISPOSITION)
    accepted = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    excluded = [row for row in plan if row["proposed_decision"] == "proposed_exclusion"]
    if len(plan) != 59 or len(accepted) != 24 or len(excluded) != 35 or len(proposed) != 82:
        raise AccessionError(
            f"reviewed documentation scope changed: plan={len(plan)} accepted={len(accepted)} "
            f"excluded={len(excluded)} disposition={len(proposed)}"
        )
    if any(row.get("deletion_authorized", "").casefold() != "false" for row in plan + proposed):
        raise AccessionError("reviewed documentation scope contains deletion authority")
    if set(row["final_relative_path"] for row in accepted) != set(APPROVED_OUTPUTS):
        raise AccessionError("approved documentation output paths changed")
    if len({row["source_path"] for row in plan}) != len(plan):
        raise AccessionError("documentation plan contains duplicate source paths")
    return plan, proposed, accepted, excluded


def verify_sources(
    rows: list[dict[str, str]], *, hash_content: bool = True
) -> dict[str, dict[str, int | str]]:
    root = SOURCE_ROOT.resolve(strict=True)
    state: dict[str, dict[str, int | str]] = {}
    for row in rows:
        path = Path(row["source_path"]).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise AccessionError(f"source resolves outside Elements: {path}") from error
        stat = path.stat()
        expected_size = int(row["source_size_bytes"] if "source_size_bytes" in row else row["size_bytes"])
        expected_hash = normalized_hash(row["source_sha256"])
        if stat.st_size != expected_size:
            raise AccessionError(f"source size changed: {path}")
        actual_hash = expected_hash
        if hash_content:
            actual_hash = sha256(path)
            if actual_hash != expected_hash:
                raise AccessionError(f"source hash changed: {path}")
        state[str(path)] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": actual_hash}
    return state


def eligible_disposition(proposed: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row for row in proposed
        if row["preservation_status"] in {"proposed_accession", "proposed_redundant_exact_copy"}
    ]


def preflight() -> dict[str, object]:
    mount = source_mount()
    plan, proposed, accepted, excluded = load_scope()
    preparation = json.loads(PREPARATION.read_text(encoding="utf-8"))
    if preparation.get("status") != "ready_for_human_confirmation" or preparation.get("final_hashes_generated") is not False:
        raise AccessionError("documentation preparation is not the approved pre-accession state")
    if preparation.get("stage_path") != str(PREPARED_STAGE):
        raise AccessionError("documentation preparation points to a different stage")
    with zipfile.ZipFile(PREPARED_STAGE) as archive:
        if archive.testzip() is not None:
            raise AccessionError("prepared documentation stage has a ZIP CRC failure")
        names = set(archive.namelist())
        if not set(APPROVED_OUTPUTS) <= names:
            raise AccessionError("prepared documentation stage lacks an approved output")
    eligible = eligible_disposition(proposed)
    if len(eligible) != 25:
        raise AccessionError(f"expected 24 selected sources plus one exact duplicate, found {len(eligible)}")
    verified = verify_sources(eligible, hash_content=False)
    return {
        "result": "ok", "source_mount": mount, "source_read_only": True,
        "plan_rows": len(plan), "accepted_source_records": len(accepted),
        "excluded_plan_records": len(excluded), "reviewed_source_occurrences": len(proposed),
        "drain_eligible_document_sources": len(eligible), "approved_document_outputs": len(APPROVED_OUTPUTS),
        "verified_source_bytes": sum(int(value["size"]) for value in verified.values()),
        "deletion_authorized": False,
    }


def extract_and_verify_outputs(accepted: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    output_hashes: dict[str, str] = {}
    output_info: dict[str, dict[str, object]] = {}
    accepted_by_final: dict[str, list[dict[str, str]]] = {}
    for row in accepted:
        accepted_by_final.setdefault(row["final_relative_path"], []).append(row)
    with zipfile.ZipFile(PREPARED_STAGE) as outer:
        for relative in APPROVED_OUTPUTS:
            payload = outer.read(relative)
            target = STAGE / relative
            atomic_bytes(target, payload)
            digest = sha256(target)
            rows = accepted_by_final[relative]
            if relative in PACKAGE_OUTPUTS:
                with zipfile.ZipFile(io.BytesIO(payload)) as package:
                    if package.testzip() is not None:
                        raise AccessionError(f"package ZIP failed CRC validation: {relative}")
                    manifest = list(csv.DictReader(io.StringIO(package.read("MANIFEST.csv").decode("utf-8-sig"))))
                    manifest_by_source = {row["source_path"]: row for row in manifest}
                    if set(manifest_by_source) != {row["source_path"] for row in rows}:
                        raise AccessionError(f"package manifest scope mismatch: {relative}")
                    for row in rows:
                        manifest_row = manifest_by_source[row["source_path"]]
                        if normalized_hash(manifest_row["source_sha256"]) != normalized_hash(row["source_sha256"]):
                            raise AccessionError(f"package manifest source hash mismatch: {row['source_path']}")
                        member_hash = sha256_bytes(package.read(manifest_row["member"]))
                        if member_hash != normalized_hash(row["source_sha256"]):
                            raise AccessionError(f"packaged member differs from source: {row['source_path']}")
            else:
                if len(rows) != 1 or digest != normalized_hash(rows[0]["source_sha256"]):
                    raise AccessionError(f"direct documentation output differs from source: {relative}")
            output_hashes[relative] = digest
            output_info[relative] = {"size": target.stat().st_size, "mtime_ns": target.stat().st_mtime_ns}
    return output_hashes, output_info


def build_documents(
    accepted: list[dict[str, str]], output_hashes: dict[str, str]
) -> list[dict[str, str]]:
    accepted_by_final: dict[str, list[dict[str, str]]] = {}
    for row in accepted:
        accepted_by_final.setdefault(row["final_relative_path"], []).append(row)
    documents: list[dict[str, str]] = []
    for index, relative in enumerate(APPROVED_OUTPUTS, 1):
        rows = accepted_by_final[relative]
        if relative in PACKAGE_OUTPUTS:
            role, description, restriction, expected_members = PACKAGE_OUTPUTS[relative]
            if len(rows) != expected_members:
                raise AccessionError(f"unexpected package member count: {relative}")
            source_path = "multiple; see package MANIFEST.csv"
            source_kind = "generated_package"
            relationship = "direct"
            condition = "Scope-reviewed source records packaged without modifying member payloads."
            member_count = str(len(rows))
        else:
            role, description, restriction = "project_document", DIRECT_DESCRIPTIONS[relative], ""
            source_path = rows[0]["source_path"]
            source_kind = "file"
            relationship = "direct"
            condition = rows[0].get("condition", "")
            member_count = "0"
        target = STAGE / relative
        documents.append({
            "document_id": f"TFADOC-{index:04d}", "final_relative_path": relative,
            "filename": target.name, "file_role": role, "description": description, "date": "",
            "source_path": source_path, "source_kind": source_kind, "relationship": relationship,
            "access_restriction": restriction, "condition": condition, "disposition": "accessioned",
            "package_member_count": member_count, "size_bytes": str(target.stat().st_size),
            "sha256": output_hashes[relative], "deletion_authorized": "false",
        })
    return documents


def contents_markdown(documents: list[dict[str, str]]) -> str:
    lines = [
        "# TFA documentation", "",
        "This directory contains the approved documentation accession for the Technisches Fotoarchiv (TFA).",
        "The 24 selected USB source records were accessioned as seven outputs; package-member provenance is in each ZIP's `MANIFEST.csv`.",
        "", "| Path | Role | Source members | SHA-256 |", "| --- | --- | ---: | --- |",
    ]
    for row in documents:
        lines.append(
            f"| `{row['final_relative_path']}` | {row['file_role']} | {row['package_member_count']} | `{row['sha256']}` |"
        )
    lines += [
        "", "Restricted contract and finance packages are identified by their path and by `access_restriction=restricted` in `db-restore/manifests/documents.csv`.",
        "The reviewed scope also preserves 35 excluded plan records outside the TFA documentation accession. One proven exact copy of an accessioned source is recorded in the drain list.",
        "No deletion is authorized. USB source files remain unchanged.", "",
    ]
    return "\n".join(lines)


def build_source_and_drain(
    proposed: list[dict[str, str]], output_hashes: dict[str, str]
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
    current_source = csv_rows(UNIT / "db-restore/manifests/source-disposition.csv")
    current_drain = csv_rows(UNIT / "db-restore/manifests/drain-list.csv")
    if len({row["source_path"] for row in current_source}) != len(current_source):
        raise AccessionError("current source-disposition contains duplicate source paths")
    if len({row["source_path"] for row in current_drain}) != len(current_drain):
        raise AccessionError("current drain list contains duplicate source paths")
    reviewed_paths = {row["source_path"] for row in proposed}
    updated_by_path = {row["source_path"]: dict(row) for row in current_source if row["source_path"] not in reviewed_paths}
    eligible_paths: set[str] = set()
    excluded_occurrences = 0
    for proposal in proposed:
        row = {field: proposal.get(field, "") for field in SOURCE_FIELDS}
        status = proposal["preservation_status"]
        final_path = proposal["final_path"]
        if status == "proposed_accession":
            row.update({
                "preservation_status": "accessioned_documentation",
                "preservation_basis": "human_confirmed_content_supported_tfa_documentary_function",
                "final_sha256": output_hashes[final_path],
                "exact_copy_proven": "true" if normalized_hash(row["source_sha256"]) == output_hashes[final_path] else "false",
                "drain_state": "source_covered_by_documentation_accession",
                "notes": "Documentation accession completed; USB source unchanged; deletion not authorized.",
            })
            eligible_paths.add(row["source_path"])
        elif status == "proposed_redundant_exact_copy" and final_path in output_hashes:
            row.update({
                "preservation_status": "redundant_exact_document_copy_of_accessioned_documentation",
                "preservation_basis": "SHA-256 equals the accessioned scope-reviewed representative",
                "final_sha256": output_hashes[final_path], "exact_copy_proven": "true",
                "drain_state": "proposed_redundant",
                "notes": "Exact duplicate of an accessioned documentation source; USB source unchanged; deletion not authorized.",
            })
            eligible_paths.add(row["source_path"])
        else:
            excluded_occurrences += 1
            row.update({
                "final_path": "", "final_sha256": "", "drain_state": "outside_tfa_document_scope",
                "deletion_authorized": "false",
                "notes": "Scope-reviewed and preserved outside the TFA documentation accession; not added to drain; deletion not authorized.",
            })
            if status == "proposed_redundant_noise":
                row["preservation_status"] = "reviewed_not_accessioned_technical_noise"
            elif status == "excluded_exact_copy_outside_tfa_document_scope":
                row["preservation_status"] = "excluded_exact_copy_outside_tfa_document_scope"
            else:
                row["preservation_status"] = "reviewed_not_accessioned"
        row["deletion_authorized"] = "false"
        updated_by_path[row["source_path"]] = row
    final_source = [updated_by_path[path] for path in sorted(updated_by_path)]
    drain_by_path = {row["source_path"]: dict(row) for row in current_drain if row["source_path"] not in reviewed_paths}
    for path in eligible_paths:
        drain_by_path[path] = dict(updated_by_path[path])
    final_drain = [drain_by_path[path] for path in sorted(drain_by_path)]
    return final_source, final_drain, {
        "reviewed_source_occurrences": len(proposed), "documentation_sources_added_to_drain": len(eligible_paths),
        "excluded_occurrences_kept_out_of_drain": excluded_occurrences,
    }


def add_document_controls(
    documents: list[dict[str, str]], output_hashes: dict[str, str],
    output_info: dict[str, dict[str, object]], source_rows: list[dict[str, str]],
    drain_rows: list[dict[str, str]], completed_at: str,
) -> dict[str, object]:
    rep_path = UNIT / "db-restore/manifests/representations.csv"
    checksum_path = UNIT / "db-restore/checksums.csv"
    reps = csv_rows(rep_path)
    checksums = csv_rows(checksum_path)
    rep_fields = list(reps[0])
    checksum_fields = list(checksums[0])
    if any(row["final_relative_path"].startswith("documents/") for row in reps):
        raise AccessionError("published TFA unit already contains document representations")
    if any(row["path"].startswith("documents/") for row in checksums):
        raise AccessionError("published TFA unit already contains document checksums")
    accepted_by_final: dict[str, list[dict[str, str]]] = {}
    for row in source_rows:
        if row["preservation_status"] == "accessioned_documentation":
            accepted_by_final.setdefault(row["final_path"], []).append(row)
    for document in documents:
        relative = document["final_relative_path"]
        source_sha = ""
        source_path = document["source_path"]
        if source_path.startswith(str(SOURCE_ROOT)):
            source_sha = accepted_by_final[relative][0]["source_sha256"]
        reps.append({
            "record_key": "unit:tfa", "inventory": "", "role": document["file_role"],
            "source_path": source_path, "source_sha256": source_sha,
            "final_relative_path": relative, "final_sha256": document["sha256"],
            "size_bytes": document["size_bytes"], "completed_at": completed_at,
            "copy_policy": "exact_copy" if source_sha == document["sha256"] and source_sha else "generated_review_package_or_index",
            "basis": document["description"], "deletion_authorized": "false",
        })
        checksums.append({
            "path": relative, "sha256": document["sha256"], "size_bytes": document["size_bytes"],
            "content_role": document["file_role"],
        })
    atomic_bytes(STAGE / "db-restore/manifests/representations.csv", csv_bytes(rep_fields, reps))
    atomic_bytes(STAGE / "db-restore/checksums.csv", csv_bytes(checksum_fields, checksums))
    atomic_bytes(STAGE / "db-restore/manifests/source-disposition.csv", csv_bytes(SOURCE_FIELDS, source_rows))
    atomic_bytes(STAGE / "db-restore/manifests/drain-list.csv", csv_bytes(SOURCE_FIELDS, drain_rows))
    atomic_bytes(STAGE / "db-restore/manifests/documents.csv", csv_bytes(DOCUMENT_FIELDS, documents))

    build_db = STAGE / "db-restore/RESTORE.sqlite"
    build_db.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(DB)
    target_db = sqlite3.connect(build_db)
    try:
        source_db.backup(target_db)
        for table, rows, fields in (
            ("representations", reps, rep_fields), ("checksums", checksums, checksum_fields),
            ("source_disposition", source_rows, list(SOURCE_FIELDS)), ("drain_list", drain_rows, list(SOURCE_FIELDS)),
        ):
            target_db.execute(f'DELETE FROM "{table}"')
            columns = ",".join(f'"{field}"' for field in fields)
            placeholders = ",".join("?" for _ in fields)
            target_db.executemany(
                f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})',
                [[row.get(field, "") for field in fields] for row in rows],
            )
        existing_meta = {row[0]: row[1] for row in target_db.execute("SELECT key,value FROM meta")}
        existing_meta.update({
            "documentation_accessioned": "true",
            "documentation_manifest": "db-restore/manifests/documents.csv",
            "documentation_scope": "24 selected sources; 7 outputs plus human-readable index",
        })
        target_db.execute("DELETE FROM meta")
        target_db.executemany("INSERT INTO meta(key,value) VALUES (?,?)", sorted(existing_meta.items()))
        target_db.commit()
        quick = target_db.execute("PRAGMA quick_check").fetchone()[0]
        foreign = list(target_db.execute("PRAGMA foreign_key_check"))
        sql = "\n".join(target_db.iterdump()) + "\n"
    finally:
        target_db.close()
        source_db.close()
    if quick != "ok" or foreign:
        raise AccessionError(f"staged SQLite failed validation: quick={quick} foreign={len(foreign)}")
    atomic_bytes(STAGE / "db-restore/RESTORE.sql", sql.encode("utf-8"))
    return {
        "representation_rows": len(reps), "checksum_rows": len(checksums),
        "source_disposition_rows": len(source_rows), "drain_rows": len(drain_rows),
        "sqlite_quick_check": quick, "sqlite_foreign_key_violations": len(foreign),
    }


def readme_bytes(drain_count: int) -> bytes:
    return f"""# Technisches Fotoarchiv (TFA) des WF

This unit contains the accessioned museum-digital collection 857 scope, the human-reconciled local TFA expansion, and the approved TFA project documentation. Museum-digital remains authoritative for museum-digital descriptive records; local descriptive metadata is preserved from explicit human reconciliation decisions, with identifier-only labels where no reliable description exists.

- Museum-digital records: 5282
- Local expansion catalogue records: 2481
- Accessioned archival objects: 7762
- Selected Masters: 7762
- High-resolution JPEG surrogate Masters: 5
- Generated full-resolution JPEG access derivatives: 7762
- Catalogue duplicate and non-critical accession holds: 1
- Subdivision memberships: 15345
- Accessioned documentation source records: 24
- Accessioned documentation outputs: 7 plus `documents/CONTENTS.md`
- Redundant USB source paths in the drain list: {drain_count}
- Deletion authorized: false

`masters/` contains one metadata-embedded selected Master per accessioned object. `export/access/` contains one full-resolution JPEG generated from that finalized Master at quality 95. `documents/` contains the approved project documentation; `documents/CONTENTS.md` and `db-restore/manifests/documents.csv` describe its identity, provenance, relationships, restrictions, and final hashes.

The drain list records USB source copies, duplicates, and derivatives only after the related object or documentation was accessioned or independently reconciled as redundant. It is a disposition record, not deletion authority. Genuine-loss, unresolved, excluded documentation, noise not covered by an accession, and metadata-evidence paths remain outside the drain. Source files remain unchanged and read-only. Final media and document hashes are in `db-restore/checksums.csv`; human-readable provenance and dispositions are under `db-restore/manifests/`.
""".encode("utf-8")


def validate_stage(
    documents: list[dict[str, str]], source_rows: list[dict[str, str]], drain_rows: list[dict[str, str]],
    scope_plan: list[dict[str, str]], proposed: list[dict[str, str]], counts: dict[str, object],
) -> dict[str, object]:
    errors: list[str] = []
    doc_paths = {row["final_relative_path"] for row in documents}
    if doc_paths != set(APPROVED_OUTPUTS) | {"documents/CONTENTS.md"}:
        errors.append("final document manifest output set mismatch")
    for row in documents:
        path = STAGE / row["final_relative_path"]
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            errors.append(f"document payload binding mismatch: {row['final_relative_path']}")
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    errors.append(f"document ZIP CRC failure: {row['final_relative_path']}")
    reps = csv_rows(STAGE / "db-restore/manifests/representations.csv")
    checksums = csv_rows(STAGE / "db-restore/checksums.csv")
    if {row["final_relative_path"] for row in reps if row["final_relative_path"].startswith("documents/")} != doc_paths:
        errors.append("document representations do not match documents manifest")
    if {row["path"] for row in checksums if row["path"].startswith("documents/")} != doc_paths:
        errors.append("document checksums do not match documents manifest")
    if any(row["deletion_authorized"].casefold() != "false" for row in source_rows + drain_rows + documents):
        errors.append("a final documentation record authorizes deletion")
    accepted_paths = {row["source_path"] for row in proposed if row["preservation_status"] == "proposed_accession"}
    duplicate_paths = {row["source_path"] for row in proposed if row["preservation_status"] == "proposed_redundant_exact_copy"}
    excluded_paths = {row["source_path"] for row in proposed} - accepted_paths - duplicate_paths
    drain_paths = {row["source_path"] for row in drain_rows}
    if not accepted_paths | duplicate_paths <= drain_paths:
        errors.append("drain list lacks an accessioned documentation source or exact duplicate")
    if excluded_paths & drain_paths:
        errors.append("outside-scope documentation occurrence entered drain")
    if len([row for row in scope_plan if row["proposed_decision"] == "proposed_exclusion"]) != 35:
        errors.append("excluded plan scope changed")
    db = sqlite3.connect(STAGE / "db-restore/RESTORE.sqlite")
    try:
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        foreign = list(db.execute("PRAGMA foreign_key_check"))
        db_counts = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("objects", "representations", "checksums", "source_disposition", "drain_list", "accession_holds")
        }
        object_masters = db.execute("SELECT COUNT(*) FROM objects WHERE accession_result='accessioned'").fetchone()[0]
        meta_docs = db.execute("SELECT value FROM meta WHERE key='documentation_accessioned'").fetchone()
    finally:
        db.close()
    if quick != "ok" or foreign or db_counts["objects"] != 7763 or object_masters != 7762 or meta_docs != ("true",):
        errors.append(f"SQLite invariant failed: quick={quick} foreign={len(foreign)} counts={db_counts} objects={object_masters}")
    if db_counts["representations"] != counts["representation_rows"] or db_counts["checksums"] != counts["checksum_rows"]:
        errors.append("SQLite representation/checksum counts differ from staged manifests")
    if errors:
        raise AccessionError(json.dumps({"result": "error", "errors": errors}, ensure_ascii=False))
    return {
        "result": "ok", "document_manifest_rows": len(documents), "documentation_outputs": len(APPROVED_OUTPUTS),
        "documentation_index_files": 1, "accepted_source_records": len(accepted_paths),
        "exact_duplicate_sources_in_drain": len(duplicate_paths), "excluded_plan_records": 35,
        "excluded_source_occurrences_outside_drain": len(excluded_paths), "objects_accessioned": object_masters,
        **db_counts, "sqlite_quick_check": quick, "sqlite_foreign_key_violations": len(foreign),
        "deletion_authorized": False,
    }


def backup_controls() -> dict[str, str]:
    relative_paths = (
        "00_README.md", "db-restore/RESTORE.sqlite", "db-restore/RESTORE.sql",
        "db-restore/checksums.csv", "db-restore/manifests/representations.csv",
        "db-restore/manifests/source-disposition.csv", "db-restore/manifests/drain-list.csv",
        "db-restore/accession-completion.json", "metadata/accession-state.json",
    )
    hashes: dict[str, str] = {}
    for relative in relative_paths:
        source = UNIT / relative
        target = BACKUP / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative] = sha256(target)
    return hashes


def publish() -> None:
    documents_root = UNIT / "documents"
    if documents_root.exists():
        raise AccessionError(f"refusing to merge into existing documentation directory: {documents_root}")
    for source in sorted((STAGE / "documents").rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(STAGE)
        target = UNIT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise AccessionError(f"refusing to overwrite documentation payload: {target}")
        os.replace(source, target)
    replacements = (
        "00_README.md", "db-restore/RESTORE.sqlite", "db-restore/RESTORE.sql",
        "db-restore/checksums.csv", "db-restore/manifests/representations.csv",
        "db-restore/manifests/source-disposition.csv", "db-restore/manifests/drain-list.csv",
        "db-restore/manifests/documents.csv",
    )
    for relative in replacements:
        source = STAGE / relative
        target = UNIT / relative
        atomic_bytes(target, source.read_bytes())


def execute() -> dict[str, object]:
    preflight_result = preflight()
    if RUN.exists():
        raise AccessionError(f"documentation accession run already exists: {RUN}")
    STAGE.mkdir(parents=True)
    atomic_json(RUN / "preflight.json", preflight_result)
    plan, proposed, accepted, _ = load_scope()
    eligible = eligible_disposition(proposed)
    source_before = verify_sources(eligible)
    output_hashes, output_info = extract_and_verify_outputs(accepted)
    documents = build_documents(accepted, output_hashes)
    contents = contents_markdown(documents).encode("utf-8")
    atomic_bytes(STAGE / "documents/CONTENTS.md", contents)
    contents_hash = sha256(STAGE / "documents/CONTENTS.md")
    documents.append({
        "document_id": "TFADOC-0008", "final_relative_path": "documents/CONTENTS.md",
        "filename": "CONTENTS.md", "file_role": "documentation_index",
        "description": "Human-readable index to the approved TFA documentation accession", "date": "",
        "source_path": "generated from db-restore/manifests/documents.csv", "source_kind": "generated_index",
        "relationship": "direct", "access_restriction": "", "condition": "",
        "disposition": "accessioned", "package_member_count": "0",
        "size_bytes": str((STAGE / "documents/CONTENTS.md").stat().st_size),
        "sha256": contents_hash, "deletion_authorized": "false",
    })
    output_hashes["documents/CONTENTS.md"] = contents_hash
    output_info["documents/CONTENTS.md"] = {
        "size": (STAGE / "documents/CONTENTS.md").stat().st_size,
        "mtime_ns": (STAGE / "documents/CONTENTS.md").stat().st_mtime_ns,
    }
    source_rows, drain_rows, drain_audit = build_source_and_drain(proposed, output_hashes)
    completed_at = now()
    control_counts = add_document_controls(documents, output_hashes, output_info, source_rows, drain_rows, completed_at)
    atomic_bytes(STAGE / "00_README.md", readme_bytes(len(drain_rows)))
    validation = validate_stage(documents, source_rows, drain_rows, plan, proposed, control_counts)
    pre_documentation_hashes = backup_controls()
    publish()

    source_after = verify_sources(eligible, hash_content=False)
    for path, before in source_before.items():
        after = source_after[path]
        if after["size"] != before["size"] or after["mtime_ns"] != before["mtime_ns"]:
            raise AccessionError("USB documentation source state changed during accession")
    final_mount = source_mount()
    for row in documents:
        target = UNIT / row["final_relative_path"]
        if target.stat().st_size != int(row["size_bytes"]):
            raise AccessionError(f"published document binding failed: {target}")
    final_db = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    try:
        final_quick = final_db.execute("PRAGMA quick_check").fetchone()[0]
        final_foreign = list(final_db.execute("PRAGMA foreign_key_check"))
    finally:
        final_db.close()
    if final_quick != "ok" or final_foreign:
        raise AccessionError("published SQLite validation failed")

    overall_controls = (
        "00_README.md", "db-restore/RESTORE.sqlite", "db-restore/RESTORE.sql",
        "db-restore/checksums.csv", "db-restore/manifests/representations.csv",
        "db-restore/manifests/source-disposition.csv", "db-restore/manifests/drain-list.csv",
        "db-restore/manifests/documents.csv", "CATALOG.csv", "CATALOG.xlsx",
        "db-restore/RESTORE.csv", "db-restore/RESTORE.xlsx",
        "db-restore/manifests/accession-holds.csv", "db-restore/manifests/reconciliation-decisions.csv",
        "db-restore/manifests/subdivision-memberships.csv", "db-restore/manifests/subdivision-register.csv",
        "metadata/museum-digital-catalog.csv", "metadata/subdivision-memberships.csv",
        "metadata/subdivision-register.csv",
    )
    control_hashes = {relative: sha256(UNIT / relative) for relative in overall_controls}
    control_hashes.update({row["final_relative_path"]: row["sha256"] for row in documents})
    document_completion = {
        "format": "iss-tfa-document-accession-completion-v1", "completed_at": completed_at,
        "result": "ok", "run": str(RUN), "unit": str(UNIT), "source_mount": final_mount,
        "source_read_only": True, "source_modified": False, "metadata_reconciled": True,
        "accepted_source_records": 24, "exact_duplicate_sources_in_drain": 1,
        "excluded_plan_records": 35, "reviewed_source_occurrences": 82,
        "documentation_outputs": 7, "document_manifest_rows": 8,
        "final_hash_policy": "one SHA-256 per finalized documentation output and generated index",
        "control_file_sha256": control_hashes, "pre_documentation_control_backup_sha256": pre_documentation_hashes,
        "drain_audit": drain_audit, **control_counts, "sqlite_quick_check": final_quick,
        "sqlite_foreign_key_violations": len(final_foreign), "data_integrity_suspected": False,
        "deletion_authorized": False, "write_boundary_state": "confined_to_iss_archive_work_and_iss_archive",
    }
    atomic_json(COMPLETION, document_completion)
    atomic_json(UNIT / "db-restore/document-accession-completion.json", document_completion)

    overall = json.loads((BACKUP / "db-restore/accession-completion.json").read_text(encoding="utf-8"))
    overall.update({
        "completed_at": completed_at, "image_accession_completed_at": overall.get("completed_at", ""),
        "documentation_accessioned": True, "documentation_run": str(RUN),
        "documentation_outputs": 7, "document_manifest_rows": 8,
        "documentation_source_records_accessioned": 24, "documentation_exact_duplicates_in_drain": 1,
        "documentation_excluded_plan_records": 35,
        "representation_rows": control_counts["representation_rows"], "checksum_rows": control_counts["checksum_rows"],
        "source_disposition_rows": control_counts["source_disposition_rows"], "drain_rows": control_counts["drain_rows"],
        "pre_documentation_control_backup_sha256": pre_documentation_hashes,
        "control_file_sha256": control_hashes,
        "sqlite_quick_check": final_quick, "sqlite_foreign_key_violations": len(final_foreign),
        "source_read_only": True, "source_modified": False, "data_integrity_suspected": False,
        "deletion_authorized": False,
    })
    atomic_json(UNIT / "db-restore/accession-completion.json", overall)
    atomic_json(UNIT / "metadata/accession-state.json", {
        "format": "iss-tfa-accession-state-v1", "updated_at": completed_at, "state": "complete",
        "image_accession_completion": "/mnt/volume/iss-archive-work/tfa/work/accession-runs/tfa-local-001/completion.json",
        "documentation_accession_completion": str(COMPLETION),
        "completion": str(UNIT / "db-restore/accession-completion.json"),
    })
    return document_completion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run"))
    args = parser.parse_args()
    try:
        result = preflight() if args.command == "preflight" else execute()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (AccessionError, OSError, csv.Error, json.JSONDecodeError, sqlite3.Error, zipfile.BadZipFile, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
