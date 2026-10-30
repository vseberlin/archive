#!/usr/bin/env python3
"""Guarded, resumable accession of the reconciled TFA local expansion.

The existing published museum-digital scope remains intact. This tool appends
human-selected local Masters, generates access JPEGs during accession, and
rebuilds the small catalogue/manifests so SQLite and readable files agree.
USB sources are never modified or deleted. After successful accession their
source copies and reviewed redundant/derivative candidates are recorded in the
drain manifest with deletion_authorized=false.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


WORK = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW = WORK / "work/reconciliation/unit-reconciliation"
UNIT = Path("/mnt/volume/iss-archive/units/tfa")
SOURCE_ROOT = Path("/media/vladimir/Elements")
RUN = WORK / "work/accession-runs/tfa-local-001"
PILOT = RUN / "pilot"
CANDIDATES = RUN / "candidates"
RECEIPTS = RUN / "receipts"
BACKUP = RUN / "backup-controls"
PLAN = RUN / "accession-plan.csv"
PREPARATION = RUN / "preparation.json"
STATUS = RUN / "status.json"
ERRORS = RUN / "errors.jsonl"
CRITICAL = RUN / "critical.json"
COMPLETION = RUN / "completion.json"
PUBLICATION = RUN / "publication.json"
LOCK = RUN / "worker.lock"

ALL_LOCAL_CANDIDATES = REVIEW / "all-local-metadata-review-candidates.csv"
ALL_LOCAL_DECISIONS = REVIEW / "all-local-metadata-review-decisions.csv"
ALL_LOCAL_RECEIPT = REVIEW / "all-local-metadata-review-preparation.json"
LOCAL_DECISIONS = REVIEW / "local-object-decisions.csv"
LOCAL_DISPOSITIONS = REVIEW / "local-object-candidate-dispositions.csv"
BOOK_DECISIONS = REVIEW / "buchprojekt-unbound-object-decisions.csv"
ORPHAN_DECISIONS = REVIEW / "orphan-object-decisions.csv"
ORPHAN_DISPOSITIONS = REVIEW / "orphan-source-disposition-proposal.csv"
WORK_SOURCE_DISPOSITION = REVIEW / "source-disposition-proposal.csv"
WORK_DRAIN = REVIEW / "drain-list-proposal.csv"

PROFILE = "iss-tfa-local-reconciliation-metadata-v1"
DERIVATIVE_PROFILE = "libvips-full-resolution-jpeg-q95-v1"
EXPECTED_SOURCE_OBJECTS = 2483
EXPECTED_EXCLUDED = 2
EXPECTED_READY = 2481
EXPECTED_MINIMAL = 449
ROOT_SUBDIVISION = "md-collection:857"
MINIMAL_NOTE = (
    "User-authorized local accession with minimal metadata: the inventory identifier "
    "is used as a non-descriptive display label; no title, date, person, subject, or "
    "technical identification was inferred."
)
LOCAL_METADATA_FIELDS = (
    "title", "description", "date_text", "object_type", "material",
    "dimensions", "keywords", "classification",
)
LOCAL_DECISION_FIELDS = (
    "record_key", "decision", *LOCAL_METADATA_FIELDS, "note", "decided_by", "decided_at",
    "selected_master_path", "selected_master_size_bytes", "selected_master_mtime_ns",
    "candidate_fingerprint", "deletion_authorized",
)
PLAN_FIELDS = (
    "record_key", "archival_object_identifier", "md_object_id", "inventory",
    "metadata_authority", "metadata_reconciled", "metadata_reconciliation_basis",
    "title", "description", "date_text", "object_type", "material", "dimensions",
    "places", "tags", "creator", "md_url", "collection_ids", "collections",
    "subdivision_ids", "subdivisions", "last_updated", "selected_candidate_id",
    "selected_master_path", "selected_master_size_bytes", "master_decision_state",
    "master_decided_by", "master_decided_at", "master_decision_note",
    "provenance_chain", "accession_state", "accession_hold_reason",
)
CATALOG_EXTRA_FIELDS = (
    "master_relative_path", "master_sha256", "source_sha256",
    "access_relative_path", "access_sha256", "access_size_bytes",
    "derivative_profile", "accession_result", "accession_error",
)
REPRESENTATION_FIELDS = (
    "record_key", "inventory", "role", "source_path", "source_sha256",
    "final_relative_path", "final_sha256", "size_bytes", "completed_at",
    "copy_policy", "basis", "deletion_authorized",
)
CHECKSUM_FIELDS = ("path", "sha256", "size_bytes", "content_role")
HOLD_FIELDS = (
    "record_key", "md_object_id", "inventory", "accession_state",
    "accession_hold_reason", "related_record_key", "selected_master_path",
    "evidence_path",
)
SOURCE_FIELDS = (
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state",
    "deletion_authorized", "notes",
)
SEP = "\x1f"
RIGHTS_TAGS = (
    "XMP-dc:Rights", "IPTC:CopyrightNotice", "XMP-xmpRights:UsageTerms",
    "XMP-xmpRights:WebStatement",
)


class LocalAccessionError(RuntimeError):
    pass


def load_md_module() -> Any:
    path = Path(__file__).with_name("tfa_accession.py")
    spec = importlib.util.spec_from_file_location("tfa_md_accession_helpers", path)
    if spec is None or spec.loader is None:
        raise LocalAccessionError("cannot load existing TFA accession helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RUN = RUN
    return module


MD = load_md_module()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    MD.atomic_bytes(path, csv_bytes(fields, rows))


def sha256(path: Path) -> str:
    return MD.sha256(path)


def hex_sha256(path: Path) -> str:
    return sha256(path).removeprefix("sha256:")


def candidate_fingerprint(row: dict[str, Any]) -> str:
    protected = {
        key: str(value)
        for key, value in row.items()
        if key not in {"candidate_fingerprint", "deletion_authorized"}
    }
    payload = json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_source_mount() -> dict[str, str]:
    return MD.source_mount()


def split_terms(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def master_suffix(row: dict[str, str]) -> str:
    suffix = Path(row["selected_master_path"]).suffix.casefold()
    if suffix in {".tif", ".tiff"}:
        return ".tif"
    if suffix in {".jpg", ".jpeg"}:
        return ".jpg"
    raise LocalAccessionError(f"unsupported selected Master format: {row['selected_master_path']}")


def load_review_state() -> dict[str, Any]:
    candidate_fields, candidates = read_csv(ALL_LOCAL_CANDIDATES)
    decision_fields, decisions = read_csv(ALL_LOCAL_DECISIONS)
    _, local = read_csv(LOCAL_DECISIONS)
    _, books = read_csv(BOOK_DECISIONS)
    _, orphans = read_csv(ORPHAN_DECISIONS)
    receipt = json.loads(ALL_LOCAL_RECEIPT.read_text(encoding="utf-8"))
    if (
        len(candidates) != EXPECTED_SOURCE_OBJECTS
        or len({row["record_key"] for row in candidates}) != len(candidates)
        or receipt.get("result") != "ok"
        or receipt.get("source_modified") is not False
        or receipt.get("published_tfa_unit_changed") is not False
        or receipt.get("deletion_authorized") is not False
        or receipt.get("candidate_sha256") != hex_sha256(ALL_LOCAL_CANDIDATES)
    ):
        raise LocalAccessionError("all-local review projection is not a safe fixed scope")
    for row in candidates:
        if (
            row.get("deletion_authorized") != "false"
            or row.get("candidate_fingerprint") != candidate_fingerprint(row)
        ):
            raise LocalAccessionError(f"candidate fingerprint/authorization failed: {row['record_key']}")
    if decision_fields != list(LOCAL_DECISION_FIELDS):
        raise LocalAccessionError("all-local decision schema drifted")
    decision_by_key = {row["record_key"]: row for row in decisions}
    if len(decision_by_key) != len(decisions):
        raise LocalAccessionError("duplicate all-local metadata decision")
    master_decisions: dict[str, dict[str, str]] = {}
    for row in local:
        if row["outcome"] == "new_local_object":
            master_decisions[row["record_key"]] = {
                "selected_candidate_id": row["selected_candidate_id"],
                "selected_master_path": row["selected_source_path"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "note": row["note"], "basis": "local_object_human_selection",
            }
    for row in books:
        if row["decision"] in {"apply_metadata", "apply_edited_metadata"}:
            master_decisions[row["record_key"]] = {
                "selected_candidate_id": row["selected_candidate_id"],
                "selected_master_path": row["selected_master_path"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "note": row["note"], "basis": "buchprojekt_human_selection",
            }
    for row in orphans:
        if row["decision"] == "selected_master":
            master_decisions[row["record_key"]] = {
                "selected_candidate_id": row["selected_asset_id"],
                "selected_master_path": row["selected_source_path"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "note": row["note"], "basis": "reference_photo_hold_human_selection",
            }
    return {
        "candidate_fields": candidate_fields,
        "candidates": candidates,
        "decision_fields": decision_fields,
        "decisions": decisions,
        "decision_by_key": decision_by_key,
        "master_decisions": master_decisions,
        "receipt": receipt,
    }


def close_minimal_metadata() -> dict[str, Any]:
    verify_source_mount()
    state = load_review_state()
    candidates = state["candidates"]
    decisions = state["decision_by_key"]
    open_rows = [
        row for row in candidates
        if row["record_key"] not in decisions and not row["prior_review_state"]
    ]
    if len(open_rows) not in {0, EXPECTED_MINIMAL}:
        raise LocalAccessionError(f"minimal metadata scope drifted: {len(open_rows)}")
    if open_rows:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = ALL_LOCAL_DECISIONS.with_name(
            f"{ALL_LOCAL_DECISIONS.stem}.pre-local-accession-minimal-{stamp}.csv"
        )
        if backup.exists():
            raise LocalAccessionError(f"decision backup already exists: {backup}")
        shutil.copy2(ALL_LOCAL_DECISIONS, backup)
        MD.fsync_file(backup)
        decided_at = now()
        for candidate in open_rows:
            decisions[candidate["record_key"]] = {
                "record_key": candidate["record_key"],
                "decision": "apply_edited_metadata",
                "title": candidate["inventory"],
                "description": "", "date_text": "", "object_type": "Foto",
                "material": "", "dimensions": "", "keywords": "", "classification": "",
                "note": MINIMAL_NOTE, "decided_by": "vladimir", "decided_at": decided_at,
                "selected_master_path": candidate["selected_master_path"],
                "selected_master_size_bytes": candidate["selected_master_size_bytes"],
                "selected_master_mtime_ns": candidate["selected_master_mtime_ns"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "deletion_authorized": "false",
            }
        atomic_csv(
            ALL_LOCAL_DECISIONS, LOCAL_DECISION_FIELDS,
            (decisions[key] for key in sorted(decisions)),
        )
    return {
        "closed_minimal_metadata": len(open_rows),
        "decision_rows": len(decisions),
        "deletion_authorized": False,
    }


def effective_metadata(candidate: dict[str, str], decision: dict[str, str] | None) -> tuple[dict[str, str], str]:
    if decision is not None:
        if decision["decision"] == "reject_noise":
            return {}, "excluded_reject_noise"
        if decision["decision"] not in {"apply_metadata", "apply_edited_metadata"}:
            raise LocalAccessionError(f"unresolved metadata decision: {candidate['record_key']}")
        fields = {field: decision[field].strip() for field in LOCAL_METADATA_FIELDS}
        basis = "human_reviewed_local_metadata"
        if decision["note"] == MINIMAL_NOTE:
            basis = "human_authorized_minimal_identity_and_provenance"
        return fields, basis
    if candidate["prior_review_state"] == "dismiss_object":
        return {}, "excluded_dismiss_object"
    if candidate["prior_review_state"] not in {"apply_metadata", "apply_edited_metadata"}:
        raise LocalAccessionError(f"metadata remains open: {candidate['record_key']}")
    return (
        {field: candidate["proposed_" + field].strip() for field in LOCAL_METADATA_FIELDS},
        "preserved_prior_human_metadata_decision",
    )


def build_plan_rows() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    state = load_review_state()
    rows: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    for candidate in state["candidates"]:
        key = candidate["record_key"]
        metadata, basis = effective_metadata(candidate, state["decision_by_key"].get(key))
        if basis.startswith("excluded_"):
            excluded.append({
                "record_key": key, "inventory": candidate["inventory"], "reason": basis,
                "selected_master_path": candidate["selected_master_path"],
            })
            continue
        decision = state["master_decisions"].get(key)
        if decision is None or decision["selected_master_path"] != candidate["selected_master_path"]:
            raise LocalAccessionError(f"selected Master decision mismatch: {key}")
        classifications = split_terms(metadata["classification"])
        row = {
            "record_key": key,
            "archival_object_identifier": key,
            "md_object_id": "",
            "inventory": candidate["inventory"],
            "metadata_authority": "human reconciliation manifest",
            "metadata_reconciled": "true",
            "metadata_reconciliation_basis": basis,
            "title": metadata["title"], "description": metadata["description"],
            "date_text": metadata["date_text"], "object_type": metadata["object_type"] or "Foto",
            "material": metadata["material"], "dimensions": metadata["dimensions"],
            "places": "", "tags": metadata["keywords"], "creator": "", "md_url": "",
            "collection_ids": "857",
            "collections": "Technisches Fotoarchiv (TFA) des Werks für Fernsehelektronik (WF)",
            "subdivision_ids": "", "subdivisions": "; ".join(classifications),
            "last_updated": decision["decided_at"],
            "selected_candidate_id": decision["selected_candidate_id"],
            "selected_master_path": candidate["selected_master_path"],
            "selected_master_size_bytes": candidate["selected_master_size_bytes"],
            "master_decision_state": "selected_master",
            "master_decided_by": decision["decided_by"],
            "master_decided_at": decision["decided_at"],
            "master_decision_note": decision["note"],
            "provenance_chain": (
                f"{decision['basis']} | read-only source {candidate['selected_master_path']} | "
                f"metadata {basis}"
            ),
            "accession_state": "ready_for_accession", "accession_hold_reason": "",
        }
        rows.append(row)
    if len(excluded) != EXPECTED_EXCLUDED or len(rows) != EXPECTED_READY:
        raise LocalAccessionError(f"local accession scope drifted: ready={len(rows)} excluded={len(excluded)}")
    if (
        len({row["record_key"] for row in rows}) != len(rows)
        or len({row["inventory"] for row in rows}) != len(rows)
        or len({row["selected_master_path"] for row in rows}) != len(rows)
    ):
        raise LocalAccessionError("local accession scope is not one Master per object")
    return rows, excluded


def prepare() -> dict[str, Any]:
    closed = close_minimal_metadata()
    verify_source_mount()
    rows, excluded = build_plan_rows()
    RUN.mkdir(parents=True, exist_ok=True)
    atomic_csv(PLAN, PLAN_FIELDS, rows)
    total = sum(int(row["selected_master_size_bytes"]) for row in rows)
    payload = {
        "format": "iss-tfa-local-accession-preparation-v1", "prepared_at": now(),
        "result": "ok", "ready_objects": len(rows), "excluded_objects": excluded,
        "minimal_metadata_closed": closed["closed_minimal_metadata"],
        "metadata_reconciled": True, "automatic_master_selection": False,
        "selected_master_bytes": total, "selected_master_gib": round(total / 1024**3, 3),
        "fingerprints": {
            PLAN.name: sha256(PLAN),
            ALL_LOCAL_CANDIDATES.name: sha256(ALL_LOCAL_CANDIDATES),
            ALL_LOCAL_DECISIONS.name: sha256(ALL_LOCAL_DECISIONS),
            LOCAL_DECISIONS.name: sha256(LOCAL_DECISIONS),
            BOOK_DECISIONS.name: sha256(BOOK_DECISIONS),
            ORPHAN_DECISIONS.name: sha256(ORPHAN_DECISIONS),
            WORK_SOURCE_DISPOSITION.name: sha256(WORK_SOURCE_DISPOSITION),
            WORK_DRAIN.name: sha256(WORK_DRAIN),
            "published_accession_completion": sha256(UNIT / "db-restore/accession-completion.json"),
        },
        "source_mount": verify_source_mount(), "source_modified": False,
        "published_tfa_unit_changed": False, "deletion_authorized": False,
    }
    MD.atomic_json(PREPARATION, payload)
    return payload


def load_plan() -> list[dict[str, str]]:
    if not PREPARATION.is_file() or not PLAN.is_file():
        raise LocalAccessionError("run prepare before accession")
    preparation = json.loads(PREPARATION.read_text(encoding="utf-8"))
    if (
        preparation.get("result") != "ok"
        or preparation.get("metadata_reconciled") is not True
        or preparation.get("automatic_master_selection") is not False
        or preparation.get("deletion_authorized") is not False
    ):
        raise LocalAccessionError("local accession preparation is not safe")
    fingerprints = preparation["fingerprints"]
    paths = {
        PLAN.name: PLAN, ALL_LOCAL_CANDIDATES.name: ALL_LOCAL_CANDIDATES,
        ALL_LOCAL_DECISIONS.name: ALL_LOCAL_DECISIONS, LOCAL_DECISIONS.name: LOCAL_DECISIONS,
        BOOK_DECISIONS.name: BOOK_DECISIONS, ORPHAN_DECISIONS.name: ORPHAN_DECISIONS,
        WORK_SOURCE_DISPOSITION.name: WORK_SOURCE_DISPOSITION, WORK_DRAIN.name: WORK_DRAIN,
        "published_accession_completion": UNIT / "db-restore/accession-completion.json",
    }
    for name, path in paths.items():
        if fingerprints.get(name) != sha256(path):
            raise LocalAccessionError(f"prepared evidence changed: {name}")
    fields, rows = read_csv(PLAN)
    if fields != list(PLAN_FIELDS) or len(rows) != EXPECTED_READY:
        raise LocalAccessionError("prepared local accession plan drifted")
    return rows


def preflight() -> dict[str, Any]:
    mount = verify_source_mount()
    rows = load_plan()
    _, published = read_csv(UNIT / "CATALOG.csv")
    published_keys = {row["record_key"] for row in published}
    published_inventories = {row["inventory"] for row in published}
    published_sources = {
        row["selected_master_path"] for row in published
        if row.get("accession_result") == "accessioned"
    }
    existing_receipts = load_receipts()
    total = 0
    bad = []
    for row in rows:
        source = MD.inside(Path(row["selected_master_path"]), SOURCE_ROOT, exists=True)
        stat = source.stat()
        master_target = UNIT / "masters" / f"{row['inventory']}{master_suffix(row)}"
        access_target = UNIT / "export/access" / f"{row['inventory']}.jpg"
        unreceipted_collision = (
            (master_target.exists() or access_target.exists())
            and row["inventory"] not in existing_receipts
        )
        if (
            not source.is_file()
            or stat.st_size != int(row["selected_master_size_bytes"])
            or row["record_key"] in published_keys
            or row["inventory"] in published_inventories
            or row["selected_master_path"] in published_sources
            or not re.fullmatch(r"[A-Za-z0-9._-]+", row["inventory"])
            or unreceipted_collision
        ):
            bad.append(row["record_key"])
        total += stat.st_size
    free = shutil.disk_usage(Path("/mnt/volume")).free
    required = total * 2 + 100 * 1024**3
    if bad:
        raise LocalAccessionError(f"local source/collision preflight failed: {bad[:10]}")
    if free < required:
        raise LocalAccessionError(f"insufficient free space: {free} < {required}")
    check = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("tfa_workbench.py")), "--actor", "vladimir", "--check"],
        check=True, capture_output=True, text=True, timeout=600,
    )
    health = json.loads(check.stdout)
    if (
        health.get("result") != "ok" or health.get("quick_check") != "ok"
        or int(health.get("foreign_key_violations", -1)) != 0
        or health.get("source_read_only") is not True
        or int(health.get("all_local_metadata_open", -1)) != 0
        or health.get("deletion_authorized") is not False
    ):
        raise LocalAccessionError(f"workbench health gate failed: {health}")
    return {
        "result": "ok", "checked_at": now(), "ready_objects": len(rows),
        "selected_master_bytes": total, "selected_master_gib": round(total / 1024**3, 3),
        "free_bytes": free, "required_free_bytes_guard": required,
        "source_mount": mount, "workbench_quick_check": health["quick_check"],
        "metadata_open": health["all_local_metadata_open"],
        "deletion_authorized": False,
    }


def local_metadata_plan(
    row: dict[str, str], source_hash: str, master_hash: str = "", master_relative_path: str = "",
) -> dict[str, str | None]:
    subjects = SEP.join(split_terms(row["tags"]))
    date_text = row["date_text"].strip()
    strict_date: str | None = date_text if re.fullmatch(r"\d{4}", date_text) else None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        strict_date = date_text.replace("-", ":")
    physical = "; ".join(
        part for part in (
            f"Material/Technik: {row['material'].strip()}" if row["material"].strip() else "",
            f"Maße: {row['dimensions'].strip()}" if row["dimensions"].strip() else "",
        ) if part
    )
    relations = [
        f"iss:metadata-profile:{PROFILE}", source_hash,
        f"iss:metadata-basis:{row['metadata_reconciliation_basis']}",
    ]
    relations.extend(f"iss:subdivision:{value}" for value in split_terms(row["subdivisions"]))
    if master_hash:
        if not master_relative_path:
            raise LocalAccessionError("master hash supplied without a master path")
        relations.extend((f"master:{master_relative_path}", master_hash))
    title = row["title"].strip()
    description = row["description"].strip()
    plan: dict[str, str | None] = {
        "XMP-dc:Identifier": row["record_key"],
        "XMP-iptcExt:ArtworkSourceInventoryNo": row["inventory"],
        "XMP-dc:Title": title or None,
        "XMP-iptcExt:ArtworkTitle": title or None,
        "XMP-dc:Description": description or None,
        "XMP-iptcExt:ArtworkContentDescription": description or None,
        "IFD0:ImageDescription": description or None,
        "XMP-dc:Date": strict_date,
        "XMP-iptcExt:ArtworkCircaDateCreated": date_text or None,
        "XMP-dc:Type": row["object_type"].strip() or None,
        "XMP-iptcExt:ArtworkPhysicalDescription": physical or None,
        "XMP-dc:Subject": subjects or None,
        "XMP-dc:Source": row["selected_master_path"],
        "XMP-dc:Relation": SEP.join(relations),
        "IPTC:ObjectName": title if title and len(title.encode()) <= 64 else None,
        "IPTC:Caption-Abstract": description if description and len(description.encode()) <= 2000 else None,
        "IPTC:Keywords": subjects or None,
    }
    plan["IPTC:CodedCharacterSet"] = (
        "UTF8" if any(plan[key] for key in ("IPTC:ObjectName", "IPTC:Caption-Abstract", "IPTC:Keywords")) else None
    )
    return plan


def process_one(row: dict[str, str], *, publish: bool) -> dict[str, Any]:
    verify_source_mount()
    source = MD.inside(Path(row["selected_master_path"]), SOURCE_ROOT, exists=True)
    before_stat = source.stat()
    before = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)
    if before_stat.st_size != int(row["selected_master_size_bytes"]):
        raise LocalAccessionError(f"source size changed: {source}")
    if shutil.disk_usage(Path("/mnt/volume")).free < max(100 * 1024**3, before_stat.st_size * 4):
        raise LocalAccessionError("free-space safety floor reached")
    output = CANDIDATES if publish else PILOT
    output.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    suffix = master_suffix(row)
    master_relative = f"masters/{row['inventory']}{suffix}"
    access_relative = f"export/access/{row['inventory']}.jpg"
    master_candidate = output / f".{row['inventory']}.{token}.master{suffix}"
    access_candidate = output / f".{row['inventory']}.{token}.access.jpg"
    started = time.monotonic()
    try:
        source_hash = sha256(source)
        rights = MD.exif_json(source, RIGHTS_TAGS)
        plan_master = local_metadata_plan(row, source_hash)
        MD.copy_with_metadata(source, master_candidate, plan_master, rights)
        MD.verify_metadata(master_candidate, plan_master, rights)
        technical, pixels = MD.verify_structure(source, master_candidate)
        after_stat = source.stat()
        after = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns)
        if before != after:
            raise LocalAccessionError(f"source changed during accession: {source}")
        master_hash = sha256(master_candidate)
        plan_access = local_metadata_plan(row, source_hash, master_hash, master_relative)
        MD.generate_access(master_candidate, access_candidate)
        MD.write_metadata(access_candidate, plan_access, rights)
        MD.verify_metadata(access_candidate, plan_access, rights)
        width, height, orientation = MD.dimensions(master_candidate)
        access_width, access_height, _ = MD.dimensions(access_candidate)
        expected = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
        if (access_width, access_height) != expected:
            raise LocalAccessionError(f"access dimensions changed: {(access_width, access_height)} != {expected}")
        if MD.exif_json(access_candidate, ("File:FileType",)).get("File:FileType") != "JPEG":
            raise LocalAccessionError("access derivative is not JPEG")
        receipt: dict[str, Any] = {
            "format": "iss-tfa-local-accession-object-v1", "completed_at": now(),
            "record_key": row["record_key"], "inventory": row["inventory"],
            "source_path": str(source), "source_sha256": source_hash,
            "source_size_bytes": before_stat.st_size, "source_mtime_ns": before_stat.st_mtime_ns,
            "master_relative_path": master_relative, "master_sha256": master_hash,
            "master_size_bytes": master_candidate.stat().st_size,
            "master_pixel_digest": pixels, "master_technical": technical,
            "access_relative_path": access_relative, "access_sha256": sha256(access_candidate),
            "access_size_bytes": access_candidate.stat().st_size,
            "access_width": access_width, "access_height": access_height,
            "metadata_profile": PROFILE, "derivative_profile": DERIVATIVE_PROFILE,
            "elapsed_seconds": round(time.monotonic() - started, 3), "published": publish,
            "source_modified": False, "deletion_authorized": False,
        }
        if not publish:
            MD.atomic_json(PILOT / f"{row['inventory']}.json", receipt)
            return receipt
        master = UNIT / master_relative
        access = UNIT / access_relative
        if master.exists() or access.exists():
            raise LocalAccessionError(f"unreceipted destination exists: {row['inventory']}")
        os.replace(master_candidate, master); MD.fsync_dir(master.parent)
        os.replace(access_candidate, access); MD.fsync_dir(access.parent)
        os.chmod(master, 0o444); os.chmod(access, 0o444)
        for path, size_key, inode_key, mtime_key in (
            (master, "master_size_bytes", "master_inode", "master_mtime_ns"),
            (access, "access_size_bytes", "access_inode", "access_mtime_ns"),
        ):
            stat = path.stat()
            if stat.st_size != receipt[size_key] or stat.st_nlink != 1:
                raise LocalAccessionError(f"published file binding failed: {path}")
            receipt[inode_key] = stat.st_ino; receipt[mtime_key] = stat.st_mtime_ns
        MD.atomic_json(RECEIPTS / f"{row['inventory']}.json", receipt)
        return receipt
    finally:
        master_candidate.unlink(missing_ok=True)
        access_candidate.unlink(missing_ok=True)


def load_receipts() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not RECEIPTS.exists():
        return found
    for path in sorted(RECEIPTS.glob("*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        inventory = str(receipt.get("inventory") or "")
        if not inventory or inventory in found or receipt.get("published") is not True:
            raise LocalAccessionError(f"invalid receipt: {path}")
        for prefix in ("master", "access"):
            target = UNIT / receipt[f"{prefix}_relative_path"]
            stat = target.stat()
            if (
                stat.st_size != int(receipt[f"{prefix}_size_bytes"])
                or stat.st_ino != int(receipt[f"{prefix}_inode"])
                or stat.st_nlink != 1
            ):
                raise LocalAccessionError(f"receipt/file binding drifted: {target}")
        found[inventory] = receipt
    return found


def load_errors() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not ERRORS.exists():
        return found
    for line in ERRORS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        inventory = str(row.get("inventory") or "")
        if not inventory or inventory in found:
            raise LocalAccessionError("invalid or duplicate local accession error")
        found[inventory] = row
    return found


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    MD.append_jsonl(path, payload)


def backup_controls() -> dict[str, str]:
    controls = [
        UNIT / "00_README.md", UNIT / "CATALOG.csv", UNIT / "CATALOG.xlsx",
        UNIT / "metadata/museum-digital-catalog.csv",
        UNIT / "metadata/subdivision-register.csv", UNIT / "metadata/subdivision-memberships.csv",
        UNIT / "metadata/accession-state.json", UNIT / "db-restore/RESTORE.csv",
        UNIT / "db-restore/RESTORE.xlsx", UNIT / "db-restore/RESTORE.sql",
        UNIT / "db-restore/RESTORE.sqlite", UNIT / "db-restore/checksums.csv",
        UNIT / "db-restore/accession-completion.json",
        *sorted((UNIT / "db-restore/manifests").glob("*.csv")),
    ]
    manifest_path = BACKUP / "manifest.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        for rel, expected in payload["sha256"].items():
            if sha256(BACKUP / rel) != expected:
                raise LocalAccessionError(f"control backup drifted: {rel}")
        return payload["sha256"]
    BACKUP.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for source in controls:
        relative = source.relative_to(UNIT)
        destination = BACKUP / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        MD.fsync_file(destination)
        hashes[str(relative)] = sha256(destination)
    MD.atomic_json(manifest_path, {"created_at": now(), "sha256": hashes})
    return hashes


def create_table(db: sqlite3.Connection, name: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    db.execute(f'CREATE TABLE "{name}" (' + ",".join(f'"{field}" TEXT NOT NULL' for field in fields) + ")")
    if rows:
        db.executemany(
            f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
            [[str(row.get(field, "")) for field in fields] for row in rows],
        )


def drain_rows_for_final(
    successful_plan: list[dict[str, str]], receipts: dict[str, dict[str, Any]],
    published_catalog: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
    source_fields, source_rows = read_csv(WORK_SOURCE_DISPOSITION)
    if source_fields != list(SOURCE_FIELDS):
        raise LocalAccessionError("source-disposition schema drifted")
    by_path = {row["source_path"]: dict(row) for row in source_rows}
    if len(by_path) != len(source_rows):
        raise LocalAccessionError("duplicate source-disposition path")
    local_by_key = {row["record_key"]: row for row in successful_plan}
    final_by_key = {
        row["record_key"]: row["master_relative_path"] for row in published_catalog
        if row.get("accession_result") == "accessioned"
    }
    final_by_key.update({
        row["record_key"]: receipts[row["inventory"]]["master_relative_path"]
        for row in successful_plan
    })

    related: dict[str, tuple[str, str, str]] = {}
    _, local_dispositions = read_csv(LOCAL_DISPOSITIONS)
    for row in local_dispositions:
        if row["record_key"] in local_by_key and row["disposition"] == "redundant":
            related[row["source_path"]] = (
                row["record_key"], row["inventory"], row["size_bytes"]
            )
    _, books = read_csv(BOOK_DECISIONS)
    for row in books:
        if row["record_key"] not in local_by_key:
            continue
        for path in json.loads(row["redundant_candidate_paths_json"]):
            size = str(Path(path).stat().st_size)
            related[path] = (row["record_key"], local_by_key[row["record_key"]]["inventory"], size)

    added_related = 0
    for path, (key, inventory, size) in related.items():
        target = final_by_key[key]
        current = by_path.get(path)
        if current is None:
            current = {field: "" for field in SOURCE_FIELDS}
            current.update({
                "source_path": path, "source_kind": "file", "unit": "tfa",
                "inventory": inventory, "source_size_bytes": size,
            })
            by_path[path] = current
            added_related += 1
        current.update({
            "inventory": inventory,
            "source_role": "derivative_candidate" if Path(path).suffix.casefold() in {".jpg", ".jpeg", ".png"} else "master_candidate",
            "final_path": target, "preservation_status": "redundant",
            "preservation_basis": "human_reconciled_redundant_to_accessioned_local_master",
            "exact_copy_proven": current.get("exact_copy_proven") or "false",
            "drain_state": "proposed_redundant", "deletion_authorized": "false",
            "notes": "Reviewed duplicate/derivative of the accessioned local Master; USB source unchanged; deletion not authorized.",
        })

    accessioned_sources: dict[str, dict[str, Any]] = {}
    for row in published_catalog:
        if row.get("accession_result") == "accessioned":
            accessioned_sources[row["selected_master_path"]] = {
                "inventory": row["inventory"], "final_path": row["master_relative_path"],
                "source_sha256": row["source_sha256"], "final_sha256": row["master_sha256"],
                "source_size_bytes": row["selected_master_size_bytes"],
                "basis": "accessioned_source_copy_of_published_master",
            }
    for row in successful_plan:
        receipt = receipts[row["inventory"]]
        accessioned_sources[receipt["source_path"]] = {
            "inventory": row["inventory"], "final_path": receipt["master_relative_path"],
            "source_sha256": receipt["source_sha256"], "final_sha256": receipt["master_sha256"],
            "source_size_bytes": str(receipt["source_size_bytes"]),
            "basis": "accessioned_source_copy_of_local_master",
        }
    added_sources = 0
    for path, evidence in accessioned_sources.items():
        current = by_path.get(path)
        if current is None:
            current = {field: "" for field in SOURCE_FIELDS}
            current.update({"source_path": path, "source_kind": "file", "unit": "tfa"})
            by_path[path] = current
            added_sources += 1
        current.update({
            "inventory": evidence["inventory"], "source_role": "accessioned_source_copy",
            "final_path": evidence["final_path"], "preservation_status": "redundant",
            "preservation_basis": evidence["basis"],
            "source_sha256": evidence["source_sha256"], "final_sha256": evidence["final_sha256"],
            "source_size_bytes": evidence["source_size_bytes"], "exact_copy_proven": "false",
            "drain_state": "proposed_redundant", "deletion_authorized": "false",
            "notes": "Source copy was accessioned to the finalized metadata-embedded Master; USB source unchanged; deletion not authorized.",
        })

    final_source = [by_path[path] for path in sorted(by_path)]
    final_drain = [row for row in final_source if row["drain_state"] == "proposed_redundant"]
    loss_paths = {
        row["source_path"] for row in final_source
        if row["preservation_status"] == "genuine_loss"
        or row["drain_state"] == "excluded_genuine_loss_preserve_evidence"
    }
    drain_paths = {row["source_path"] for row in final_drain}
    if (
        len(drain_paths) != len(final_drain)
        or loss_paths & drain_paths
        or set(accessioned_sources) - drain_paths
        or set(related) - drain_paths
        or any(row["deletion_authorized"] != "false" for row in final_source + final_drain)
        or any(not row["source_path"].startswith(str(SOURCE_ROOT) + "/") for row in final_drain)
    ):
        raise LocalAccessionError("final drain/source-disposition invariants failed")
    return final_source, final_drain, {
        "prior_drain_rows": len(read_csv(WORK_DRAIN)[1]),
        "final_drain_rows": len(final_drain),
        "accessioned_usb_sources_in_drain": len(accessioned_sources),
        "reviewed_related_paths_in_drain": len(related),
        "new_source_rows_for_related_paths": added_related,
        "new_source_rows_for_accessioned_sources": added_sources,
        "genuine_loss_paths_excluded": len(loss_paths),
    }


def finalize(plan: list[dict[str, str]]) -> dict[str, Any]:
    receipts = load_receipts()
    errors = load_errors()
    if set(receipts) | set(errors) != {row["inventory"] for row in plan}:
        raise LocalAccessionError("receipt/error scope differs from local accession plan")
    backup_hashes = backup_controls()

    catalog_fields, published_catalog = read_csv(UNIT / "CATALOG.csv")
    representation_fields, representations = read_csv(UNIT / "db-restore/manifests/representations.csv")
    checksum_fields, checksums = read_csv(UNIT / "db-restore/checksums.csv")
    subdivision_fields, subdivisions = read_csv(UNIT / "metadata/subdivision-register.csv")
    membership_fields, memberships = read_csv(UNIT / "metadata/subdivision-memberships.csv")
    decision_fields, reconciliation_decisions = read_csv(UNIT / "db-restore/manifests/reconciliation-decisions.csv")
    hold_fields, holds = read_csv(UNIT / "db-restore/manifests/accession-holds.csv")
    if (
        catalog_fields != list(PLAN_FIELDS) + list(CATALOG_EXTRA_FIELDS)
        or representation_fields != list(REPRESENTATION_FIELDS)
        or checksum_fields != list(CHECKSUM_FIELDS)
        or hold_fields != list(HOLD_FIELDS)
    ):
        raise LocalAccessionError("published catalogue schema drifted")

    label_to_subdivision = {row["label"]: row["subdivision_id"] for row in subdivisions}
    subdivision_ids_by_key: dict[str, str] = {}
    for row in plan:
        labels = [
            "Technisches Fotoarchiv (TFA) des Werks für Fernsehelektronik (WF)",
            *split_terms(row["subdivisions"]),
        ]
        ids = []
        for label in labels:
            subdivision_id = label_to_subdivision.get(label)
            if subdivision_id is None:
                raise LocalAccessionError(f"unregistered local classification: {label}")
            ids.append(subdivision_id)
            memberships.append({
                "record_key": row["record_key"], "md_object_id": "", "inventory": row["inventory"],
                "scheme_id": "museum-digital-collection", "subdivision_id": subdivision_id,
                "label": label, "membership_basis": "human local metadata reconciliation",
            })
        row["subdivision_ids"] = "; ".join(ids)
        subdivision_ids_by_key[row["record_key"]] = row["subdivision_ids"]
    membership_counts = Counter(row["subdivision_id"] for row in memberships)
    for row in subdivisions:
        row["assigned_object_count"] = str(membership_counts[row["subdivision_id"]])

    successful_plan: list[dict[str, str]] = []
    for row in plan:
        output = dict(row)
        output.update({field: "" for field in CATALOG_EXTRA_FIELDS})
        receipt = receipts.get(row["inventory"])
        error = errors.get(row["inventory"])
        if receipt:
            successful_plan.append(row)
            output.update({
                "master_relative_path": receipt["master_relative_path"],
                "master_sha256": receipt["master_sha256"],
                "source_sha256": receipt["source_sha256"],
                "access_relative_path": receipt["access_relative_path"],
                "access_sha256": receipt["access_sha256"],
                "access_size_bytes": str(receipt["access_size_bytes"]),
                "derivative_profile": DERIVATIVE_PROFILE, "accession_result": "accessioned",
            })
            representations.extend((
                {
                    "record_key": row["record_key"], "inventory": row["inventory"],
                    "role": "preservation_master", "source_path": receipt["source_path"],
                    "source_sha256": receipt["source_sha256"],
                    "final_relative_path": receipt["master_relative_path"],
                    "final_sha256": receipt["master_sha256"], "size_bytes": str(receipt["master_size_bytes"]),
                    "completed_at": receipt["completed_at"],
                    "copy_policy": "verified_metadata_rewrite_atomic_publication",
                    "basis": "human_selected_local_master_metadata_embedded_pixels_and_structure_unchanged",
                    "deletion_authorized": "false",
                },
                {
                    "record_key": row["record_key"], "inventory": row["inventory"],
                    "role": "access_derivative", "source_path": receipt["master_relative_path"],
                    "source_sha256": receipt["master_sha256"],
                    "final_relative_path": receipt["access_relative_path"],
                    "final_sha256": receipt["access_sha256"], "size_bytes": str(receipt["access_size_bytes"]),
                    "completed_at": receipt["completed_at"], "copy_policy": DERIVATIVE_PROFILE,
                    "basis": "generated_from_finalized_metadata_embedded_master",
                    "deletion_authorized": "false",
                },
            ))
            checksums.extend((
                {"path": receipt["master_relative_path"], "sha256": receipt["master_sha256"], "size_bytes": str(receipt["master_size_bytes"]), "content_role": "preservation_master"},
                {"path": receipt["access_relative_path"], "sha256": receipt["access_sha256"], "size_bytes": str(receipt["access_size_bytes"]), "content_role": "access_derivative"},
            ))
        elif error:
            output.update({
                "accession_state": "noncritical_error_hold",
                "accession_result": "noncritical_error_hold", "accession_error": error["error"],
            })
            holds.append({
                "record_key": row["record_key"], "md_object_id": "", "inventory": row["inventory"],
                "accession_state": "noncritical_error_hold", "accession_hold_reason": error["error"],
                "related_record_key": "", "selected_master_path": row["selected_master_path"],
                "evidence_path": str(ERRORS),
            })
        published_catalog.append(output)
        reconciliation_decisions.append({
            "record_key": row["record_key"], "md_object_id": "", "inventory": row["inventory"],
            "decision_state": "selected_master", "selected_candidate_id": row["selected_candidate_id"],
            "selected_source_path": row["selected_master_path"], "note": row["master_decision_note"],
            "decided_by": row["master_decided_by"], "decided_at": row["master_decided_at"],
        })

    final_source, final_drain, drain_stats = drain_rows_for_final(
        successful_plan, receipts, published_catalog[: -len(plan)],
    )

    catalog_keys = [row["record_key"] for row in published_catalog]
    master_paths = [row["master_relative_path"] for row in published_catalog if row["accession_result"] == "accessioned"]
    access_paths = [row["access_relative_path"] for row in published_catalog if row["accession_result"] == "accessioned"]
    if (
        len(catalog_keys) != len(set(catalog_keys))
        or len(master_paths) != len(set(master_paths))
        or len(access_paths) != len(set(access_paths))
    ):
        raise LocalAccessionError("final object or representation path collision")

    build = RUN / f"RESTORE.building-{os.getpid()}.sqlite"
    build.unlink(missing_ok=True)
    db = sqlite3.connect(build)
    try:
        tables = (
            ("objects", catalog_fields, published_catalog),
            ("representations", representation_fields, representations),
            ("checksums", checksum_fields, checksums),
            ("subdivisions", subdivision_fields, subdivisions),
            ("object_subdivisions", membership_fields, memberships),
            ("reconciliation_decisions", decision_fields, reconciliation_decisions),
            ("source_disposition", list(SOURCE_FIELDS), final_source),
            ("drain_list", list(SOURCE_FIELDS), final_drain),
            ("accession_holds", hold_fields, holds),
        )
        for name, fields, rows in tables:
            create_table(db, name, list(fields), rows)
        create_table(db, "meta", ["key", "value"], [
            {"key": "format", "value": "iss-tfa-restore-v2"},
            {"key": "metadata_authority", "value": "museum-digital plus human local reconciliation manifests"},
            {"key": "metadata_profile", "value": f"iss-tfa-md-metadata-v1; {PROFILE}"},
            {"key": "derivative_profile", "value": DERIVATIVE_PROFILE},
            {"key": "deletion_authorized", "value": "false"},
        ])
        db.commit()
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        sql = "\n".join(db.iterdump()) + "\n"
    finally:
        db.close()
    if quick != "ok" or foreign:
        raise LocalAccessionError(f"rebuilt SQLite failed: quick={quick} foreign={len(foreign)}")

    sys.path.insert(0, "/home/vladimir/wp/tools/archive-preservation")
    import paragon_document_bundle  # type: ignore[import-not-found]  # noqa: PLC0415
    xlsx = paragon_document_bundle.table_xlsx_bytes(
        catalog_fields, published_catalog, sheet_name="Catalog", table_name="Catalog"
    )
    controls = {
        UNIT / "CATALOG.csv": csv_bytes(catalog_fields, published_catalog),
        UNIT / "CATALOG.xlsx": xlsx,
        UNIT / "metadata/museum-digital-catalog.csv": csv_bytes(
            catalog_fields, published_catalog[: -len(plan)]
        ),
        UNIT / "metadata/subdivision-register.csv": csv_bytes(subdivision_fields, subdivisions),
        UNIT / "metadata/subdivision-memberships.csv": csv_bytes(membership_fields, memberships),
        UNIT / "db-restore/RESTORE.csv": csv_bytes(catalog_fields, published_catalog),
        UNIT / "db-restore/RESTORE.xlsx": xlsx,
        UNIT / "db-restore/RESTORE.sql": sql.encode(),
        UNIT / "db-restore/checksums.csv": csv_bytes(checksum_fields, checksums),
        UNIT / "db-restore/manifests/representations.csv": csv_bytes(representation_fields, representations),
        UNIT / "db-restore/manifests/subdivision-register.csv": csv_bytes(subdivision_fields, subdivisions),
        UNIT / "db-restore/manifests/subdivision-memberships.csv": csv_bytes(membership_fields, memberships),
        UNIT / "db-restore/manifests/reconciliation-decisions.csv": csv_bytes(decision_fields, reconciliation_decisions),
        UNIT / "db-restore/manifests/source-disposition.csv": csv_bytes(SOURCE_FIELDS, final_source),
        UNIT / "db-restore/manifests/drain-list.csv": csv_bytes(SOURCE_FIELDS, final_drain),
        UNIT / "db-restore/manifests/accession-holds.csv": csv_bytes(hold_fields, holds),
    }
    for path, payload in controls.items():
        MD.atomic_bytes(path, payload)
    os.replace(build, UNIT / "db-restore/RESTORE.sqlite"); MD.fsync_dir(UNIT / "db-restore")

    accessioned = sum(row["accession_result"] == "accessioned" for row in published_catalog)
    local_accessioned = len(successful_plan)
    jpeg_masters = sum(path.casefold().endswith((".jpg", ".jpeg")) for path in master_paths)
    readme = f"""# Technisches Fotoarchiv (TFA) des WF

This unit contains the accessioned museum-digital collection 857 scope and the human-reconciled local TFA expansion. Museum-digital remains authoritative for museum-digital descriptive records; local descriptive metadata is preserved from explicit human reconciliation decisions, with identifier-only labels where no reliable description exists.

- Museum-digital records: 5282
- Local expansion catalogue records: {len(plan)}
- Accessioned archival objects: {accessioned}
- Selected Masters: {accessioned}
- High-resolution JPEG surrogate Masters: {jpeg_masters}
- Generated full-resolution JPEG access derivatives: {accessioned}
- Catalogue duplicate and non-critical accession holds: {len(holds)}
- Subdivision memberships: {len(memberships)}
- Redundant USB source paths in the drain list: {len(final_drain)}
- Deletion authorized: false

`masters/` contains one metadata-embedded selected Master per accessioned object. `export/access/` contains one full-resolution JPEG generated from that finalized Master at quality 95. The drain list records USB source copies, duplicates, and derivatives only after the related object was accessioned or independently reconciled as redundant. It is a disposition record, not deletion authority. Genuine-loss, unresolved, noise, document, and metadata-evidence paths are excluded. Source files remain unchanged and read-only. Final media hashes are in `db-restore/checksums.csv`; human-readable provenance and dispositions are under `db-restore/manifests/`.
"""
    MD.atomic_bytes(UNIT / "00_README.md", readme.encode())

    db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1", uri=True)
    try:
        final_quick = db.execute("PRAGMA quick_check").fetchone()[0]
        final_foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        db_counts = {
            name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in ("objects", "representations", "checksums", "object_subdivisions", "source_disposition", "drain_list", "accession_holds")
        }
        selected_drain = db.execute(
            "SELECT COUNT(*) FROM objects o JOIN drain_list d ON d.source_path=o.selected_master_path WHERE o.accession_result='accessioned'"
        ).fetchone()[0]
        deletion_true = db.execute(
            "SELECT (SELECT COUNT(*) FROM source_disposition WHERE lower(deletion_authorized)!='false') + "
            "(SELECT COUNT(*) FROM drain_list WHERE lower(deletion_authorized)!='false')"
        ).fetchone()[0]
        genuine_loss_drain = db.execute(
            "SELECT COUNT(*) FROM drain_list WHERE preservation_status='genuine_loss' OR drain_state='excluded_genuine_loss_preserve_evidence'"
        ).fetchone()[0]
    finally:
        db.close()
    if (
        final_quick != "ok" or final_foreign or db_counts["objects"] != len(published_catalog)
        or db_counts["representations"] != 2 * accessioned
        or db_counts["checksums"] != 2 * accessioned
        or db_counts["drain_list"] != len(final_drain)
        or selected_drain != accessioned or deletion_true or genuine_loss_drain
    ):
        raise LocalAccessionError(
            f"final validation failed: quick={final_quick} foreign={len(final_foreign)} "
            f"counts={db_counts} selected_drain={selected_drain}/{accessioned} "
            f"deletion={deletion_true} loss_drain={genuine_loss_drain}"
        )
    for receipt in receipts.values():
        for prefix in ("master", "access"):
            path = UNIT / receipt[f"{prefix}_relative_path"]
            stat = path.stat()
            if stat.st_size != int(receipt[f"{prefix}_size_bytes"]) or stat.st_nlink != 1:
                raise LocalAccessionError(f"final receipt binding changed: {path}")

    control_hashes = {str(path.relative_to(UNIT)): sha256(path) for path in controls}
    control_hashes["00_README.md"] = sha256(UNIT / "00_README.md")
    completion = {
        "format": "iss-tfa-accession-completion-v2", "completed_at": now(), "result": "ok",
        "museum_digital_records": 5282, "local_expansion_records": len(plan),
        "local_objects_accessioned": local_accessioned,
        "local_noncritical_error_holds": len(errors), "objects_accessioned": accessioned,
        "catalogue_rows": len(published_catalog), "representation_rows": len(representations),
        "checksum_rows": len(checksums), "subdivision_memberships": len(memberships),
        "source_disposition_rows": len(final_source), "drain_rows": len(final_drain),
        "drain_audit": drain_stats, "metadata_reconciled": True,
        "metadata_authority": "museum-digital plus human local reconciliation manifests",
        "metadata_profiles": ["iss-tfa-md-metadata-v1", PROFILE],
        "derivative_profile": DERIVATIVE_PROFILE,
        "final_hash_policy": "one SHA-256 per finalized Master and derivative; existing hashes retained",
        "control_file_sha256": control_hashes, "pre_expansion_control_backup_sha256": backup_hashes,
        "sqlite_quick_check": final_quick, "sqlite_foreign_key_violations": len(final_foreign),
        "all_accessioned_usb_sources_in_drain": selected_drain == accessioned,
        "genuine_loss_paths_in_drain": genuine_loss_drain,
        "source_read_only": True, "source_modified": False,
        "data_integrity_suspected": False, "deletion_authorized": False,
        "write_boundary_state": "confined_to_iss_archive_work_and_iss_archive",
        "unit": str(UNIT), "run": str(RUN),
    }
    MD.atomic_json(COMPLETION, completion)
    MD.atomic_json(PUBLICATION, completion)
    MD.atomic_json(UNIT / "db-restore/accession-completion.json", completion)
    MD.atomic_json(UNIT / "metadata/accession-state.json", {
        "format": "iss-tfa-accession-state-v1", "updated_at": now(),
        "run": str(RUN), "state": "complete", "completion": str(COMPLETION),
    })
    return completion


def run_pilot(inventories: list[str]) -> int:
    result = preflight()
    rows = {row["inventory"]: row for row in load_plan()}
    output = []
    for inventory in inventories:
        if inventory not in rows:
            raise LocalAccessionError(f"unknown local pilot inventory: {inventory}")
        output.append(process_one(rows[inventory], publish=False))
    payload = {
        "format": "iss-tfa-local-accession-pilot-v1", "completed_at": now(), "result": "ok",
        "inventories": inventories, "preflight": result, "receipts": output,
        "source_modified": False, "published": False, "deletion_authorized": False,
    }
    MD.atomic_json(RUN / "pilot-validation.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run_all() -> int:
    preflight_result = preflight()
    MD.atomic_json(RUN / "preflight.json", preflight_result)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LocalAccessionError("another local accession worker holds the run lock") from error
        plan = load_plan()
        receipts = load_receipts(); errors = load_errors(); started = time.monotonic()
        for index, row in enumerate(plan, 1):
            inventory = row["inventory"]
            if inventory in receipts or inventory in errors:
                continue
            try:
                receipts[inventory] = process_one(row, publish=True)
            except (MD.IntegrityError, LocalAccessionError) as error:
                payload = {
                    "failed_at": now(), "state": "stopped_integrity", "index": index,
                    "inventory": inventory, "error": str(error), "data_integrity_suspected": True,
                }
                MD.atomic_json(CRITICAL, payload); MD.atomic_json(STATUS, payload)
                raise LocalAccessionError(str(error)) from error
            except (MD.NonCriticalError, OSError) as error:
                if isinstance(error, OSError) and getattr(error, "errno", None) in {5, 30}:
                    payload = {
                        "failed_at": now(), "state": "stopped_integrity", "index": index,
                        "inventory": inventory, "error": str(error), "data_integrity_suspected": True,
                    }
                    MD.atomic_json(CRITICAL, payload); MD.atomic_json(STATUS, payload)
                    raise LocalAccessionError(str(error)) from error
                payload = {
                    "failed_at": now(), "index": index, "record_key": row["record_key"],
                    "inventory": inventory, "error": str(error), "critical": False,
                }
                append_jsonl(ERRORS, payload); errors[inventory] = payload
            elapsed = max(time.monotonic() - started, 0.001)
            processed = len(receipts) + len(errors)
            rate = processed / elapsed
            remaining = len(plan) - processed
            MD.atomic_json(STATUS, {
                "updated_at": now(), "state": "running", "index": index, "total": len(plan),
                "completed": len(receipts), "errors": len(errors), "processed": processed,
                "last_inventory": inventory, "elapsed_seconds": round(elapsed, 1),
                "estimated_remaining_seconds": round(remaining / rate, 1) if rate else 0,
            })
        MD.atomic_json(STATUS, {
            "updated_at": now(), "state": "finalizing", "total": len(plan),
            "completed": len(receipts), "errors": len(errors),
        })
        completion = finalize(plan)
        MD.atomic_json(STATUS, {**completion, "state": "complete"})
        print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        os.close(descriptor)


def show_status() -> int:
    payload = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {
        "state": "not_started", "prepared": PREPARATION.exists(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("preflight")
    pilot = commands.add_parser("pilot"); pilot.add_argument("inventory", nargs="+")
    commands.add_parser("run")
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(json.dumps(prepare(), ensure_ascii=False, indent=2, sort_keys=True)); return 0
        if args.command == "preflight":
            print(json.dumps(preflight(), ensure_ascii=False, indent=2, sort_keys=True)); return 0
        if args.command == "pilot":
            return run_pilot(args.inventory)
        if args.command == "run":
            return run_all()
        return show_status()
    except (LocalAccessionError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
