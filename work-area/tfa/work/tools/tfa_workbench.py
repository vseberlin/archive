#!/usr/bin/env python3
"""Minimal local workbench for unresolved TFA Master selections.

The workbench projects the existing TFA reconciliation database and writes
only current human decisions plus readable CSV projections inside the approved
TFA work root.  It never modifies source media, copied TIFFs, or the final
archive.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.15.3"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
BACKUP = REVIEW_ROOT / "reconciliation.pre-tfa-workbench.sqlite"
OBJECTS_CSV = REVIEW_ROOT / "objects.csv"
CANDIDATES_CSV = REVIEW_ROOT / "candidates.csv"
MEMBERSHIPS_CSV = REVIEW_ROOT / "subdivision-memberships.csv"
EXCEPTIONS_CSV = REVIEW_ROOT / "selected-master-exceptions.csv"
SELECTED_CSV = REVIEW_ROOT / "selected-master-transfer.csv"
TRANSFER_PREFLIGHT = REVIEW_ROOT / "selected-master-transfer-preflight.json"
TRANSFER_COMPLETION = REVIEW_ROOT / "selected-master-transfer-completion.json"
DECISIONS_CSV = REVIEW_ROOT / "workbench-decisions.csv"
DISPOSITIONS_CSV = REVIEW_ROOT / "workbench-candidate-dispositions.csv"
WORKBENCH_RECEIPT = REVIEW_ROOT / "workbench-preflight.json"
LOCAL_METADATA_CSV = REVIEW_ROOT / "local-object-metadata.csv"
LOCAL_TECHNICAL_CSV = REVIEW_ROOT / "local-object-accession-candidates.csv"
LOCAL_MATCHES_CSV = REVIEW_ROOT / "local-object-md-match-candidates.csv"
LOCAL_DECISIONS_CSV = REVIEW_ROOT / "local-object-decisions.csv"
LOCAL_DISPOSITIONS_CSV = REVIEW_ROOT / "local-object-candidate-dispositions.csv"
LOCAL_WORKBENCH_RECEIPT = REVIEW_ROOT / "local-object-workbench-preflight.json"
VISUAL_ONLY_NEW_TIFF_CSV = REVIEW_ROOT / "visual-only-new-tiff-candidates.csv"
VISUAL_ONLY_REDUNDANT_CSV = REVIEW_ROOT / "visual-only-md-redundant-images.csv"
VISUAL_ONLY_ERRORS_CSV = REVIEW_ROOT / "visual-only-decode-errors.csv"
VISUAL_ONLY_INSTITUTION_BINDINGS_CSV = REVIEW_ROOT / "visual-only-institution-master-bindings.csv"
VISUAL_ONLY_INSTITUTION_HOLDS_CSV = REVIEW_ROOT / "visual-only-institution-master-holds.csv"
VISUAL_ONLY_RECEIPT = REVIEW_ROOT / "visual-only-reconciliation.json"
ORPHAN_DECISIONS_CSV = REVIEW_ROOT / "orphan-object-decisions.csv"
ORPHAN_DISPOSITIONS_CSV = REVIEW_ROOT / "orphan-source-disposition-proposal.csv"
REDUNDANCY_RECHECK_RECEIPT = REVIEW_ROOT / "redundancy-registration-result-20260923.json"
DRAIN_CORRECTIONS_RECEIPT = REVIEW_ROOT / "drain-corrections-20260923.json"
REFERENCE_PHOTO_RECEIPT = REVIEW_ROOT / "reference-photo-reconciliation.json"
REFERENCE_PHOTO_HOLD_RECOVERY_RECEIPT = REVIEW_ROOT / "reference-photo-hold-recovery.json"
SELECTED_MASTER_DRAIN_REPAIR_RECEIPT = REVIEW_ROOT / "selected-master-drain-repair.json"
REFERENCE_PHOTO_WORKBENCH_CSV = REVIEW_ROOT / "reference-photo-workbench-holds.csv"
REFERENCE_PHOTO_HOLD_WORKBENCH_RECEIPT = REVIEW_ROOT / "reference-photo-hold-workbench-preparation.json"
REFERENCE_PHOTO_HOLD_CONTEXT_CSV = REVIEW_ROOT / "reference-photo-hold-context.csv"
REFERENCE_PHOTO_SOURCE_DISPOSITION_CSV = REVIEW_ROOT / "reference-photo-source-disposition.csv"
ORPHAN_DECISION_FIELDS = (
    "record_key", "inventory", "decision", "selected_asset_id", "selected_visual_group_id",
    "selected_source_path", "note", "decided_by", "decided_at", "deletion_authorized",
)
ORPHAN_DISPOSITION_FIELDS = (
    "record_key", "inventory", "visual_group_id", "asset_id", "source_path", "extension",
    "size_bytes", "preservation_status", "preservation_basis", "drain_state",
    "deletion_authorized",
)
MD_ACCESSION_CATALOG_CSV = REVIEW_ROOT / "md-accession-catalog.csv"
MD_ACCESSION_HOLDS_CSV = REVIEW_ROOT / "md-accession-holds.csv"
MD_ACCESSION_RECEIPT = REVIEW_ROOT / "md-accession-preparation.json"
RECOVERY_QUEUE_CSV = REVIEW_ROOT / "master-recovery-ambiguous.csv"
RECOVERY_CANDIDATES_CSV = REVIEW_ROOT / "master-recovery-candidates.csv"
RECOVERY_RECEIPT = REVIEW_ROOT / "master-recovery-workbench.json"
DOCUMENT_CANDIDATES_CSV = REVIEW_ROOT / "tfa-document-candidates.csv"
DOCUMENT_ACCESSION_PLAN_CSV = REVIEW_ROOT / "tfa-document-accession-plan.scope-reviewed.csv"
DOCUMENT_COLLECTION_SUMMARY = REVIEW_ROOT / "tfa-document-collection-summary.json"
DOCUMENT_PREPARATION_SUMMARY = REVIEW_ROOT / "tfa-document-accession-preparation.scope-reviewed.json"
DOCUMENT_DECISIONS_CSV = REVIEW_ROOT / "tfa-document-review-decisions.csv"
METADATA_ACCESSION_PLAN_CSV = REVIEW_ROOT / "tfa-metadata-accession-plan.scope-reviewed.csv"
METADATA_PREPARATION_SUMMARY = REVIEW_ROOT / "tfa-metadata-accession-preparation.scope-reviewed.json"
METADATA_DECISIONS_CSV = REVIEW_ROOT / "tfa-metadata-review-decisions.csv"
LOCAL_METADATA_REVIEW_CANDIDATES_CSV = REVIEW_ROOT / "local-object-metadata-review-candidates.csv"
LOCAL_METADATA_REVIEW_DECISIONS_CSV = REVIEW_ROOT / "local-object-metadata-review-decisions.csv"
LOCAL_METADATA_REVIEW_RECEIPT = REVIEW_ROOT / "local-object-metadata-review-preparation.json"
LOCAL_METADATA_REVIEW_DECISION_FIELDS = (
    "record_key", "decision", "candidate_id", "title", "description",
    "date_text", "object_type", "material", "dimensions", "note",
    "decided_by", "decided_at", "source_path", "source_sha256",
    "sheet_name", "row_number", "deletion_authorized",
)
BUCHPROJEKT_UNBOUND_CANDIDATES_CSV = REVIEW_ROOT / "buchprojekt-unbound-object-candidates.csv"
BUCHPROJEKT_UNBOUND_DECISIONS_CSV = REVIEW_ROOT / "buchprojekt-unbound-object-decisions.csv"
BUCHPROJEKT_UNBOUND_RECEIPT = REVIEW_ROOT / "buchprojekt-unbound-object-preparation.json"
BUCHPROJEKT_UNBOUND_DECISION_FIELDS = (
    "record_key", "decision", "selected_candidate_id", "selected_master_path",
    "redundant_candidate_paths_json", "title", "description", "date_text",
    "object_type", "material", "dimensions", "keywords", "classification",
    "note", "decided_by",
    "decided_at", "book_image_key", "workbook_path", "workbook_sha256",
    "sheet_name", "row_number", "visual_group_id", "deletion_authorized",
)
ALL_LOCAL_METADATA_CANDIDATES_CSV = REVIEW_ROOT / "all-local-metadata-review-candidates.csv"
ALL_LOCAL_METADATA_DECISIONS_CSV = REVIEW_ROOT / "all-local-metadata-review-decisions.csv"
ALL_LOCAL_METADATA_RECEIPT = REVIEW_ROOT / "all-local-metadata-review-preparation.json"
ALL_LOCAL_METADATA_THUMBNAIL_ROOT = REVIEW_ROOT / "all-local-metadata-thumbnails"
ALL_LOCAL_METADATA_FIELDS = (
    "title", "description", "date_text", "object_type", "material",
    "dimensions", "keywords", "classification",
)
ALL_LOCAL_METADATA_DECISION_FIELDS = (
    "record_key", "decision", *ALL_LOCAL_METADATA_FIELDS, "note", "decided_by", "decided_at",
    "selected_master_path", "selected_master_size_bytes", "selected_master_mtime_ns",
    "candidate_fingerprint", "deletion_authorized",
)
RECOVERY_107_APPLIED = WORK_ROOT / "work/accession-runs/tfa-md-001/recovery-107-applied.json"
JPEG_SURROGATE_TWO_APPLIED = WORK_ROOT / "work/accession-runs/tfa-md-001/jpeg-surrogate-two-applied.json"
RECOVERY_TMP = WORK_ROOT / "work/accession-runs/tfa-md-001/tmp"
SOURCE_ROOT = Path("/media/vladimir/Elements")
BOOK_PROJECT_IMAGES = SOURCE_ROOT / "restore/archive/WF/Buchprojekt_Industriesalon-Schöneweide/Bilddaten"
FINAL_UNIT = Path("/mnt/volume/iss-archive/units/tfa")
SELECTION_TOKEN = "/Alle-Tiff-Positive/"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"}
TIFF_EXTENSIONS = {".tif", ".tiff"}
TFA_FILENAME_RE = re.compile(
    r"(?i)(?:^|[^A-Z0-9])TFA(?:-(?:FOT|NEG|KB))?[-_ ]?(\d{5,8}(?:[-_.]\d{1,2})?)"
)
JPEG_SURROGATE_INVENTORIES = frozenset({"TFA-6323033", "TFA-6423379"})
BROWSER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
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
    ON decision.record_key='md:'||x.md_object_id AND decision.decision_state='selected_master'
  JOIN candidates selected_master ON selected_master.candidate_id=decision.selected_candidate_id
  WHERE local_candidate.record_key=x.record_key
    AND technical.accession_eligible=1
    AND lower(local_candidate.extension) IN ('.tif','.tiff')
    AND lower(selected_master.extension) IN ('.tif','.tiff')
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
CURATED_LOCAL_TIFF_SQL = """(
  SELECT COUNT(*) FROM candidates curated_candidate
  JOIN local_candidate_technical curated_technical
    ON curated_technical.candidate_id=curated_candidate.candidate_id
  WHERE curated_candidate.record_key=x.record_key
    AND curated_technical.accession_eligible=1
    AND lower(curated_candidate.extension) IN ('.tif','.tiff')
    AND curated_candidate.source_path LIKE '%/Alle-Tiff-Positive/%'
)=1"""
EXACT_EXISTING_MD_SQL = (
    f"(({EXACT_ID_MATCH_SQL} OR {EXACT_SHARED_PATH_SQL} OR {EXACT_SELECTED_MASTER_HASH_SQL}) "
    f"AND ({SELECTED_MD_TIFF_SQL} OR {CURATED_LOCAL_TIFF_SQL}))"
)


def expected_local_candidate_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM meta WHERE key='tfa_local_candidate_technical_count'"
    ).fetchone()
    return int(row[0]) if row is not None else 1254


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def human_size(value: int | str) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1000
    return f"{size:.1f} TB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def all_local_candidate_fingerprint(row: dict[str, Any]) -> str:
    protected = {
        key: str(value)
        for key, value in row.items()
        if key not in {"candidate_fingerprint", "deletion_authorized"}
    }
    payload = json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise WorkbenchError("Elements did not resolve to one mounted filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise WorkbenchError(f"Elements read-only mount guard failed: {mount}")
    return mount


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def url(path: str, **parameters: Any) -> str:
    values = {key: value for key, value in parameters.items() if value not in (None, "")}
    return path + ("?" + urllib.parse.urlencode(values) if values else "")


def candidate_kind(extension: str, source_path: str) -> str:
    extension = extension.casefold()
    if extension in TIFF_EXTENSIONS and SELECTION_TOKEN in source_path:
        return "prior selected TIFF"
    if extension in TIFF_EXTENSIONS:
        return "TIFF candidate"
    if extension in BROWSER_EXTENSIONS:
        return "existing derivative/copy"
    if extension == ".psd":
        return "PSD evidence"
    return "metadata evidence"


def candidate_disposition(
    extension: str, candidate_id: int, selected_candidate_id: int | None, state: str
) -> str:
    extension = extension.casefold()
    if extension not in IMAGE_EXTENSIONS:
        return "retain_metadata_evidence"
    if state != "selected_master" or selected_candidate_id is None:
        return "unresolved"
    return "selected_master" if candidate_id == selected_candidate_id else "redundant"


def displayed_exception_type(original: str, tiff_count: int) -> str:
    if original == "no_tiff_candidate" and tiff_count > 0:
        return "repaired_exact_tiff_match"
    return original


def visual_inventory_hint(filename: str) -> str:
    match = TFA_FILENAME_RE.search(filename)
    if match is None:
        return ""
    suffix = re.sub(r"[_ .]+", "-", match.group(1)).strip("-")
    return "TFA-" + suffix


def orphan_candidate_disposition(decision: str, asset_id: str, selected_asset_id: str) -> str:
    if decision == "selected_master":
        return "selected_master" if asset_id == selected_asset_id else "redundant"
    if decision == "redundant":
        return "redundant"
    if decision == "genuine_loss":
        return "genuine_loss"
    if decision == "noise":
        return "noise"
    return "unresolved"


class TfaReview:
    def __init__(self, actor: str) -> None:
        self.actor = actor.strip()
        if not self.actor:
            raise WorkbenchError("named reviewer is required")
        for path in (
            DATABASE,
            OBJECTS_CSV,
            CANDIDATES_CSV,
            MEMBERSHIPS_CSV,
            EXCEPTIONS_CSV,
            SELECTED_CSV,
            TRANSFER_PREFLIGHT,
            TRANSFER_COMPLETION,
            LOCAL_METADATA_CSV,
            LOCAL_TECHNICAL_CSV,
            LOCAL_MATCHES_CSV,
            LOCAL_DECISIONS_CSV,
            LOCAL_WORKBENCH_RECEIPT,
            VISUAL_ONLY_NEW_TIFF_CSV,
            VISUAL_ONLY_REDUNDANT_CSV,
            VISUAL_ONLY_ERRORS_CSV,
            VISUAL_ONLY_INSTITUTION_BINDINGS_CSV,
            VISUAL_ONLY_INSTITUTION_HOLDS_CSV,
            VISUAL_ONLY_RECEIPT,
            MD_ACCESSION_CATALOG_CSV,
            MD_ACCESSION_HOLDS_CSV,
            MD_ACCESSION_RECEIPT,
            RECOVERY_QUEUE_CSV,
            RECOVERY_CANDIDATES_CSV,
            RECOVERY_RECEIPT,
            DOCUMENT_CANDIDATES_CSV,
            DOCUMENT_ACCESSION_PLAN_CSV,
            DOCUMENT_COLLECTION_SUMMARY,
            DOCUMENT_PREPARATION_SUMMARY,
            METADATA_ACCESSION_PLAN_CSV,
            METADATA_PREPARATION_SUMMARY,
            LOCAL_METADATA_REVIEW_CANDIDATES_CSV,
            LOCAL_METADATA_REVIEW_DECISIONS_CSV,
            LOCAL_METADATA_REVIEW_RECEIPT,
            BUCHPROJEKT_UNBOUND_CANDIDATES_CSV,
            BUCHPROJEKT_UNBOUND_DECISIONS_CSV,
            BUCHPROJEKT_UNBOUND_RECEIPT,
            ALL_LOCAL_METADATA_CANDIDATES_CSV,
            ALL_LOCAL_METADATA_DECISIONS_CSV,
            ALL_LOCAL_METADATA_RECEIPT,
        ):
            if not path.is_file():
                raise WorkbenchError(f"required TFA evidence is missing: {path}")
        if FINAL_UNIT.exists():
            completion_path = FINAL_UNIT / "db-restore/accession-completion.json"
            if not completion_path.is_file():
                raise WorkbenchError("existing TFA unit has no completion receipt")
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            if (
                completion.get("result") != "ok"
                or completion.get("source_read_only") is not True
                or completion.get("source_modified") is not False
                or completion.get("deletion_authorized") is not False
            ):
                raise WorkbenchError("existing TFA accession completion is not safe")
        self.mount = source_mount()
        self.exceptions = {row["record_key"]: row for row in read_csv(EXCEPTIONS_CSV)}
        if len(self.exceptions) != 190:
            raise WorkbenchError(f"exception scope drifted: {len(self.exceptions)} != 190")
        self.exception_keys = tuple(sorted(self.exceptions))
        self._technical_cache: dict[int, dict[str, str]] = {}
        self._technical_lock = threading.Lock()
        self._initialize()
        self._local_initialize()
        self._md_initialize()
        self._recovery_initialize()
        self._document_initialize()
        self._metadata_initialize()
        self._local_metadata_review_initialize()
        self._buchprojekt_unbound_initialize()
        self._all_local_metadata_initialize()

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=20)
            connection.execute("PRAGMA query_only=ON")
        else:
            connection = sqlite3.connect(DATABASE, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _evidence_fingerprints(self) -> dict[str, str]:
        preflight = json.loads(TRANSFER_PREFLIGHT.read_text(encoding="utf-8"))
        completion = json.loads(TRANSFER_COMPLETION.read_text(encoding="utf-8"))
        if preflight.get("result") != "ok" or completion.get("result") != "ok":
            raise WorkbenchError("selected-Master transfer evidence is not complete")
        if int(completion.get("copied_files", -1)) != 5092 or int(completion.get("copy_error_rows", -1)) != 0:
            raise WorkbenchError("selected-Master transfer count/error guard failed")
        expected = preflight.get("fingerprints", {})
        for name in (SELECTED_CSV.name, EXCEPTIONS_CSV.name):
            actual = sha256_file(REVIEW_ROOT / name)
            if expected.get(name) != actual:
                raise WorkbenchError(f"transfer evidence fingerprint changed: {name}")
        return {
            path.name: sha256_file(path)
            for path in (OBJECTS_CSV, CANDIDATES_CSV, MEMBERSHIPS_CSV, EXCEPTIONS_CSV, SELECTED_CSV)
        }

    def _initialize(self) -> None:
        fingerprints = self._evidence_fingerprints()
        existing_receipt = (
            json.loads(WORKBENCH_RECEIPT.read_text(encoding="utf-8"))
            if WORKBENCH_RECEIPT.exists()
            else None
        )
        if existing_receipt and existing_receipt.get("evidence_fingerprints") != fingerprints:
            raise WorkbenchError("TFA evidence changed; preserve decisions and review reprojection")

        with closing(self.connect(read_only=True)) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign:
            raise WorkbenchError(f"SQLite preflight failed: quick={quick}, foreign_keys={len(foreign)}")

        if not BACKUP.exists():
            shutil.copy2(DATABASE, BACKUP)
            with BACKUP.open("rb") as handle:
                os.fsync(handle.fileno())

        selected = read_csv(SELECTED_CSV)
        if len(selected) != 5092:
            raise WorkbenchError(f"prior selected scope drifted: {len(selected)} != 5092")
        imported = 0
        decided_at = str(existing_receipt.get("prepared_at", "")) if existing_receipt else utc_now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in selected:
                record_key = row["record_key"]
                candidate_id = int(row["candidate_id"])
                candidate = connection.execute(
                    "SELECT record_key, extension, source_path FROM candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None or candidate["record_key"] != record_key:
                    raise WorkbenchError(f"prior selection binding failed: {record_key}/{candidate_id}")
                if str(candidate["extension"]).casefold() not in TIFF_EXTENSIONS:
                    raise WorkbenchError(f"prior selection is not TIFF: {record_key}/{candidate_id}")
                current = connection.execute(
                    "SELECT * FROM reconciliation_decisions WHERE record_key=?", (record_key,)
                ).fetchone()
                if current is not None:
                    current_candidate = connection.execute(
                        "SELECT record_key,inventory,extension FROM candidates WHERE candidate_id=?",
                        (current["selected_candidate_id"],),
                    ).fetchone()
                    current_extension = (
                        str(current_candidate["extension"]).casefold()
                        if current_candidate is not None
                        else ""
                    )
                    current_is_allowed_master = (
                        current_extension in TIFF_EXTENSIONS
                        or (
                            current_candidate is not None
                            and current_candidate["inventory"] in JPEG_SURROGATE_INVENTORIES
                            and current_extension in {".jpg", ".jpeg"}
                        )
                    )
                    if (
                        current["decision_state"] != "selected_master"
                        or current_candidate is None
                        or current_candidate["record_key"] != record_key
                        or not current_is_allowed_master
                    ):
                        raise WorkbenchError(f"existing decision conflicts with prior selection: {record_key}")
                    # The transfer CSV is historical evidence.  Preserve a later
                    # explicit human-selected replacement instead of re-importing
                    # the failed original Master over the current decision.
                    continue
                connection.execute(
                    """
                    INSERT INTO reconciliation_decisions
                      (record_key, selected_candidate_id, decision_state, note, decided_by, decided_at)
                    VALUES (?, ?, 'selected_master', ?, ?, ?)
                    """,
                    (
                        record_key,
                        candidate_id,
                        "Prior curated Alle-Tiff-Positive selection confirmed by user; guarded transfer completed without errors.",
                        self.actor,
                        decided_at,
                    ),
                )
                connection.execute(
                    "UPDATE objects SET decision_state='resolved' WHERE record_key=?", (record_key,)
                )
                imported += 1
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_workbench_version',?)",
                (VERSION,),
            )
            connection.commit()

        self.export_decisions()
        receipt = {
            "format": "iss-tfa-workbench-preflight-v1",
            "prepared_at": decided_at,
            "result": "ok",
            "version": VERSION,
            "actor": self.actor,
            "database": str(DATABASE),
            "database_backup": str(BACKUP),
            "source_mount": self.mount,
            "source_read_only": True,
            "prior_selections_imported_this_run": imported,
            "prior_selected_total": 5092,
            "exception_total": len(self.exceptions),
            "evidence_fingerprints": fingerprints,
            "hashing_media_performed": False,
            "derivatives_generated": False,
            "deletion_authorized": False,
        }
        if WORKBENCH_RECEIPT.exists():
            if existing_receipt and existing_receipt.get("result") != "ok":
                raise WorkbenchError("existing workbench preflight is not ok")
        else:
            atomic_json(WORKBENCH_RECEIPT, receipt)

    def export_decisions(self) -> None:
        with closing(self.connect(read_only=True)) as connection:
            decision_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.record_key, o.md_object_id, o.inventory,
                           d.decision_state, d.selected_candidate_id,
                           COALESCE(c.source_path, '') AS selected_source_path,
                           d.note, d.decided_by, d.decided_at
                    FROM reconciliation_decisions d
                    JOIN objects o ON o.record_key=d.record_key
                    LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    ORDER BY CAST(o.md_object_id AS INTEGER), o.record_key
                    """
                )
            ]
            dispositions = []
            for row in connection.execute(
                """
                SELECT c.record_key, o.md_object_id, o.inventory, c.candidate_id,
                       c.source_path, c.extension, c.size_bytes,
                       d.decision_state, d.selected_candidate_id
                FROM candidates c
                JOIN objects o ON o.record_key=c.record_key
                JOIN reconciliation_decisions d ON d.record_key=c.record_key
                ORDER BY CAST(o.md_object_id AS INTEGER), c.source_path
                """
            ):
                disposition = candidate_disposition(
                    str(row["extension"]),
                    int(row["candidate_id"]),
                    int(row["selected_candidate_id"]) if row["selected_candidate_id"] is not None else None,
                    str(row["decision_state"]),
                )
                dispositions.append(
                    {
                        "record_key": row["record_key"],
                        "md_object_id": row["md_object_id"],
                        "inventory": row["inventory"],
                        "candidate_id": row["candidate_id"],
                        "source_path": row["source_path"],
                        "extension": row["extension"],
                        "size_bytes": row["size_bytes"],
                        "disposition": disposition,
                        "disposition_basis": (
                            "human_selected_master_all_other_images_redundant"
                            if row["decision_state"] == "selected_master"
                            else "human_left_unresolved"
                        ),
                        "deletion_authorized": "false",
                    }
                )
        atomic_csv(
            DECISIONS_CSV,
            (
                "record_key", "md_object_id", "inventory", "decision_state",
                "selected_candidate_id", "selected_source_path", "note",
                "decided_by", "decided_at",
            ),
            decision_rows,
        )
        atomic_csv(
            DISPOSITIONS_CSV,
            (
                "record_key", "md_object_id", "inventory", "candidate_id",
                "source_path", "extension", "size_bytes", "disposition",
                "disposition_basis", "deletion_authorized",
            ),
            dispositions,
        )

    def records(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in self.exception_keys)
        with closing(self.connect(read_only=True)) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT o.*, d.selected_candidate_id, d.decision_state AS review_decision,
                           d.note, d.decided_by, d.decided_at,
                           SUM(CASE WHEN lower(c.extension) IN ('.tif','.tiff') THEN 1 ELSE 0 END) AS tiff_count,
                           SUM(CASE WHEN lower(c.extension) IN ('.jpg','.jpeg','.png','.webp') THEN 1 ELSE 0 END) AS preview_count,
                           COUNT(c.candidate_id) AS candidate_count
                    FROM objects o
                    LEFT JOIN reconciliation_decisions d ON d.record_key=o.record_key
                    LEFT JOIN candidates c ON c.record_key=o.record_key
                    WHERE o.record_key IN ({placeholders})
                    GROUP BY o.record_key
                    ORDER BY CAST(o.md_object_id AS INTEGER), o.record_key
                    """,
                    self.exception_keys,
                )
            ]
            previews = {
                row["record_key"]: int(row["candidate_id"])
                for row in connection.execute(
                    f"""
                    SELECT c.record_key, MIN(c.candidate_id) AS candidate_id
                    FROM candidates c
                    WHERE c.record_key IN ({placeholders})
                      AND lower(c.extension) IN ('.jpg','.jpeg','.png','.webp')
                    GROUP BY c.record_key
                    """,
                    self.exception_keys,
                )
            }
        for row in rows:
            row.update(self.exceptions[row["record_key"]])
            row["exception_type"] = displayed_exception_type(
                str(row["exception_type"]), int(row["tiff_count"] or 0)
            )
            row["state"] = row.get("review_decision") or "unreviewed"
            row["preview_candidate_id"] = previews.get(row["record_key"], 0)
        return rows

    def _md_initialize(self) -> None:
        receipt = json.loads(MD_ACCESSION_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("result") != "ok"
            or receipt.get("metadata_authority") != "museum-digital"
            or int(receipt.get("md_objects", -1)) != 5282
            or int(receipt.get("metadata_reconciled_objects", -1)) != 5282
            or int(receipt.get("selected_tiff_masters", -1)) != 5282
            or int(receipt.get("ready_for_accession", -1)) != 5281
            or int(receipt.get("accession_holds", -1)) != 1
            or receipt.get("source_read_only") is not True
            or receipt.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("museum-digital accession preparation is incomplete")
        fingerprints = receipt.get("fingerprints") or {}
        for path in (
            MD_ACCESSION_CATALOG_CSV, MD_ACCESSION_HOLDS_CSV, OBJECTS_CSV,
            REVIEW_ROOT / "subdivision-register.csv", MEMBERSHIPS_CSV,
        ):
            if fingerprints.get(path.name) != sha256_file(path):
                raise WorkbenchError(f"museum-digital accession evidence changed: {path.name}")
        rows = read_csv(MD_ACCESSION_CATALOG_CSV)
        if len(rows) != 5282:
            raise WorkbenchError("museum-digital accession catalogue count drifted")
        self.md_accession_receipt = receipt
        self._md_accession = {row["record_key"]: row for row in rows}

    def _recovery_initialize(self) -> None:
        receipt = json.loads(RECOVERY_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("result") != "ok"
            or int(receipt.get("remaining_accession_holds", -1)) != 109
            or int(receipt.get("ambiguous_objects", -1)) != 43
            or int(receipt.get("ambiguous_valid_candidates", -1)) != 93
            or receipt.get("source_read_only") is not True
            or receipt.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("ambiguous Master recovery projection is incomplete")
        fingerprints = receipt.get("fingerprints") or {}
        for path in (RECOVERY_QUEUE_CSV, RECOVERY_CANDIDATES_CSV):
            if fingerprints.get(path.name) != sha256_file(path):
                raise WorkbenchError(f"Master recovery evidence changed: {path.name}")
        queue_rows = read_csv(RECOVERY_QUEUE_CSV)
        candidate_rows = read_csv(RECOVERY_CANDIDATES_CSV)
        if len(queue_rows) != 43 or len(candidate_rows) != 93:
            raise WorkbenchError("ambiguous Master recovery row count drifted")
        queue = {row["record_key"]: row for row in queue_rows}
        if len(queue) != len(queue_rows):
            raise WorkbenchError("ambiguous Master recovery records are not unique")
        candidates: dict[str, list[dict[str, str]]] = {key: [] for key in queue}
        candidate_ids: set[int] = set()
        with closing(self.connect(read_only=True)) as connection:
            for row in candidate_rows:
                record_key = row["record_key"]
                candidate_id = int(row["candidate_id"])
                if record_key not in queue or candidate_id in candidate_ids:
                    raise WorkbenchError("ambiguous Master recovery candidate binding is invalid")
                bound = connection.execute(
                    "SELECT record_key,inventory,source_path,extension,size_bytes,mtime_ns FROM candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if (
                    bound is None
                    or bound["record_key"] != record_key
                    or bound["inventory"] != row["inventory"]
                    or bound["source_path"] != row["source_path"]
                    or str(bound["extension"]).casefold() not in TIFF_EXTENSIONS
                    or int(bound["size_bytes"]) != int(row["size_bytes"])
                    or int(bound["mtime_ns"]) != int(row["mtime_ns"])
                    or row["decode_status"] != "full_decode_ok"
                    or row["deletion_authorized"] != "false"
                ):
                    raise WorkbenchError(f"ambiguous Master candidate drifted: {candidate_id}")
                self.guarded_candidate_path(candidate_id)
                candidates[record_key].append(row)
                candidate_ids.add(candidate_id)
        if any(len(rows) not in {2, 3} for rows in candidates.values()):
            raise WorkbenchError("ambiguous Master queue no longer has two or three choices per object")
        self.recovery_receipt = receipt
        self._recovery_queue = queue
        self._recovery_candidates = candidates
        self._recovery_candidate_ids = candidate_ids

    def recovery_records(self) -> list[dict[str, Any]]:
        keys = tuple(sorted(self._recovery_queue))
        placeholders = ",".join("?" for _ in keys)
        with closing(self.connect(read_only=True)) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT o.record_key,o.md_object_id,o.inventory,o.title,o.description,
                           o.subdivisions,d.selected_candidate_id,d.decision_state,
                           (SELECT MIN(p.candidate_id) FROM candidates p
                             WHERE p.record_key=o.record_key
                               AND lower(p.extension) IN ('.jpg','.jpeg','.png','.webp'))
                             AS preview_candidate_id
                    FROM objects o JOIN reconciliation_decisions d ON d.record_key=o.record_key
                    WHERE o.record_key IN ({placeholders})
                    ORDER BY CAST(o.md_object_id AS INTEGER),o.record_key
                    """,
                    keys,
                )
            ]
        for row in rows:
            evidence = self._recovery_queue[row["record_key"]]
            valid_ids = {int(item["candidate_id"]) for item in self._recovery_candidates[row["record_key"]]}
            row.update(evidence)
            row["state"] = (
                "replacement_selected"
                if row["decision_state"] == "selected_master"
                and int(row["selected_candidate_id"] or 0) in valid_ids
                else "needs_visual_choice"
            )
        return rows

    def recovery_record(self, record_key: str) -> dict[str, Any]:
        if record_key not in self._recovery_queue:
            raise WorkbenchError("record is outside the ambiguous Master recovery queue")
        with closing(self.connect(read_only=True)) as connection:
            row = connection.execute(
                """
                SELECT o.*,d.selected_candidate_id,d.decision_state AS review_decision,
                       d.note,d.decided_by,d.decided_at,
                       (SELECT MIN(p.candidate_id) FROM candidates p
                         WHERE p.record_key=o.record_key
                           AND lower(p.extension) IN ('.jpg','.jpeg','.png','.webp'))
                         AS preview_candidate_id
                FROM objects o JOIN reconciliation_decisions d ON d.record_key=o.record_key
                WHERE o.record_key=?
                """,
                (record_key,),
            ).fetchone()
            if row is None:
                raise WorkbenchError(f"unknown recovery record: {record_key}")
            candidates = []
            for evidence in self._recovery_candidates[record_key]:
                candidate = connection.execute(
                    "SELECT * FROM candidates WHERE candidate_id=?",
                    (int(evidence["candidate_id"]),),
                ).fetchone()
                if candidate is None:
                    raise WorkbenchError("recovery candidate disappeared")
                item = dict(candidate)
                item.update(evidence)
                candidates.append(item)
        result = dict(row)
        result.update(self._recovery_queue[record_key])
        result["candidates"] = candidates
        valid_ids = {int(item["candidate_id"]) for item in candidates}
        result["state"] = (
            "replacement_selected"
            if result["review_decision"] == "selected_master"
            and int(result["selected_candidate_id"] or 0) in valid_ids
            else "needs_visual_choice"
        )
        return result

    def save_recovery_decision(self, record_key: str, action: str, selected_raw: str, note: str) -> None:
        if record_key not in self._recovery_queue:
            raise WorkbenchError("decision record is outside the ambiguous Master recovery queue")
        if action not in {"select_master", "unresolved"}:
            raise WorkbenchError("unsupported recovery decision")
        selected_candidate_id: int | None = None
        if action == "select_master":
            if not selected_raw.strip().isdigit():
                raise WorkbenchError("select exactly one validated TIFF Master")
            selected_candidate_id = int(selected_raw)
            allowed = {
                int(row["candidate_id"]) for row in self._recovery_candidates[record_key]
            }
            if selected_candidate_id not in allowed:
                raise WorkbenchError("selected TIFF is not a full-decode validated recovery candidate")
            self.guarded_candidate_path(selected_candidate_id)
            state = "selected_master"
            object_state = "resolved"
        else:
            state = "unresolved"
            object_state = "unresolved"
        source_mount()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE reconciliation_decisions
                SET selected_candidate_id=?,decision_state=?,note=?,decided_by=?,decided_at=?
                WHERE record_key=?
                """,
                (selected_candidate_id, state, note.strip(), self.actor, utc_now(), record_key),
            )
            connection.execute(
                "UPDATE objects SET decision_state=? WHERE record_key=?",
                (object_state, record_key),
            )
            if connection.total_changes != 2:
                connection.rollback()
                raise WorkbenchError("recovery decision binding failed")
            connection.commit()
        self.export_decisions()

    def md_subdivision_facets(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT subdivision_id, label, assigned_object_count
                    FROM subdivisions
                    WHERE subdivision_type='collection_subdivision'
                    ORDER BY label COLLATE NOCASE, subdivision_id
                    """
                )
            ]

    def md_records(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT o.*, d.selected_candidate_id,
                           d.decision_state AS master_decision_state,
                           c.source_path AS selected_master_path,
                           c.size_bytes AS selected_master_size_bytes,
                           (SELECT MIN(p.candidate_id) FROM candidates p
                             WHERE p.record_key=o.record_key
                               AND lower(p.extension) IN ('.jpg','.jpeg','.png','.webp'))
                             AS preview_candidate_id
                    FROM objects o
                    JOIN reconciliation_decisions d ON d.record_key=o.record_key
                    JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    WHERE o.record_state='current_museum_digital'
                    ORDER BY o.inventory COLLATE NOCASE, CAST(o.md_object_id AS INTEGER)
                    """
                )
            ]
        for row in rows:
            prepared = self._md_accession.get(str(row["record_key"]))
            if prepared is None:
                raise WorkbenchError(f"MD record is absent from accession projection: {row['record_key']}")
            row["accession_state"] = prepared["accession_state"]
            row["accession_hold_reason"] = prepared["accession_hold_reason"]
        return rows

    def md_record(self, record_key: str) -> dict[str, Any]:
        if record_key not in self._md_accession:
            raise WorkbenchError(f"record is outside the current museum-digital scope: {record_key}")
        with closing(self.connect(read_only=True)) as connection:
            row = connection.execute(
                """
                SELECT o.*, d.selected_candidate_id,
                       d.decision_state AS master_decision_state,
                       d.note AS master_decision_note,
                       d.decided_by AS master_decided_by,
                       d.decided_at AS master_decided_at,
                       c.source_path AS selected_master_path,
                       c.size_bytes AS selected_master_size_bytes,
                       (SELECT MIN(p.candidate_id) FROM candidates p
                         WHERE p.record_key=o.record_key
                           AND lower(p.extension) IN ('.jpg','.jpeg','.png','.webp'))
                         AS preview_candidate_id
                FROM objects o
                JOIN reconciliation_decisions d ON d.record_key=o.record_key
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE o.record_key=? AND o.record_state='current_museum_digital'
                """,
                (record_key,),
            ).fetchone()
            if row is None:
                raise WorkbenchError(f"unknown museum-digital record: {record_key}")
            memberships = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT s.subdivision_id, s.label, s.subdivision_type,
                           os.membership_basis
                    FROM object_subdivisions os
                    JOIN subdivisions s ON s.subdivision_id=os.subdivision_id
                    WHERE os.record_key=?
                    ORDER BY CASE WHEN s.subdivision_type='collection_root' THEN 0 ELSE 1 END,
                             s.label COLLATE NOCASE
                    """,
                    (record_key,),
                )
            ]
        result = dict(row)
        result.update(
            {
                "accession_state": self._md_accession[record_key]["accession_state"],
                "accession_hold_reason": self._md_accession[record_key]["accession_hold_reason"],
                "memberships": memberships,
            }
        )
        return result

    def record(self, record_key: str) -> dict[str, Any]:
        if record_key not in self.exceptions:
            raise WorkbenchError(f"record is outside the 190-object exception scope: {record_key}")
        with closing(self.connect(read_only=True)) as connection:
            obj = connection.execute(
                """
                SELECT o.*, d.selected_candidate_id, d.decision_state AS review_decision,
                       d.note, d.decided_by, d.decided_at
                FROM objects o
                LEFT JOIN reconciliation_decisions d ON d.record_key=o.record_key
                WHERE o.record_key=?
                """,
                (record_key,),
            ).fetchone()
            if obj is None:
                raise WorkbenchError(f"unknown record: {record_key}")
            candidates = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM candidates WHERE record_key=?
                    ORDER BY CASE
                      WHEN lower(extension) IN ('.tif','.tiff') AND instr(source_path, ?) > 0 THEN 0
                      WHEN lower(extension) IN ('.tif','.tiff') THEN 1
                      WHEN lower(extension) IN ('.jpg','.jpeg','.png','.webp') THEN 2
                      ELSE 3 END,
                      source_path
                    """,
                    (record_key, SELECTION_TOKEN),
                )
            ]
        item = dict(obj)
        item.update(self.exceptions[record_key])
        item["candidates"] = candidates
        item["exception_type"] = displayed_exception_type(
            str(item["exception_type"]),
            sum(str(candidate["extension"]).casefold() in TIFF_EXTENSIONS for candidate in candidates),
        )
        item["state"] = item.get("review_decision") or "unreviewed"
        return item

    def candidate(self, candidate_id: int) -> dict[str, Any]:
        with closing(self.connect(read_only=True)) as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise WorkbenchError(f"unknown candidate: {candidate_id}")
        return dict(row)

    def guarded_candidate_path(self, candidate_id: int, *, browser_only: bool = False) -> Path:
        candidate = self.candidate(candidate_id)
        extension = str(candidate["extension"]).casefold()
        if browser_only and extension not in BROWSER_EXTENSIONS:
            raise WorkbenchError("candidate is not browser-previewable evidence")
        path = Path(str(candidate["source_path"]))
        if not inside(path, SOURCE_ROOT):
            raise WorkbenchError(f"candidate escaped read-only Elements root: {path}")
        stat = path.stat()
        if stat.st_size != int(candidate["size_bytes"]) or stat.st_mtime_ns != int(candidate["mtime_ns"]):
            raise WorkbenchError(f"candidate path/size/mtime guard failed: {path}")
        return path

    def technical(self, candidate: dict[str, Any]) -> dict[str, str]:
        candidate_id = int(candidate["candidate_id"])
        with self._technical_lock:
            if candidate_id in self._technical_cache:
                return self._technical_cache[candidate_id]
        path = self.guarded_candidate_path(candidate_id)
        result = {"dimensions": "not read", "format": str(candidate["extension"]), "status": "header unavailable"}
        if shutil.which("vipsheader"):
            try:
                completed = subprocess.run(
                    ["vipsheader", "-f", "width", "-f", "height", "-f", "format", str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                values = [value.strip() for value in completed.stdout.splitlines() if value.strip()]
                if len(values) >= 3:
                    result = {"dimensions": f"{values[0]}×{values[1]}", "format": values[2], "status": "header read"}
            except (OSError, subprocess.SubprocessError):
                pass
        with self._technical_lock:
            self._technical_cache[candidate_id] = result
        return result

    def save_decision(self, record_key: str, action: str, selected_raw: str, note: str) -> None:
        if record_key not in self.exceptions:
            raise WorkbenchError("decision record is outside the exception scope")
        note = note.strip()
        if action not in {"select_master", "unresolved"}:
            raise WorkbenchError("unsupported decision action")
        selected_candidate_id: int | None = None
        if action == "select_master":
            if not selected_raw.strip().isdigit():
                raise WorkbenchError("select exactly one TIFF Master")
            selected_candidate_id = int(selected_raw)
            candidate = self.candidate(selected_candidate_id)
            if candidate["record_key"] != record_key:
                raise WorkbenchError("selected candidate is not bound to this MD record")
            if str(candidate["extension"]).casefold() not in TIFF_EXTENSIONS:
                raise WorkbenchError("only a TIFF may be selected as Master")
            self.guarded_candidate_path(selected_candidate_id)
            state = "selected_master"
            object_state = "resolved"
        else:
            state = "unresolved"
            object_state = "unresolved"
        source_mount()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO reconciliation_decisions
                  (record_key, selected_candidate_id, decision_state, note, decided_by, decided_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_key) DO UPDATE SET
                  selected_candidate_id=excluded.selected_candidate_id,
                  decision_state=excluded.decision_state,
                  note=excluded.note,
                  decided_by=excluded.decided_by,
                  decided_at=excluded.decided_at
                """,
                (record_key, selected_candidate_id, state, note, self.actor, utc_now()),
            )
            connection.execute(
                "UPDATE objects SET decision_state=? WHERE record_key=?",
                (object_state, record_key),
            )
            selected_count = connection.execute(
                """
                SELECT COUNT(*) FROM reconciliation_decisions d
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.record_key=? AND d.decision_state='selected_master'
                  AND c.record_key=d.record_key AND lower(c.extension) IN ('.tif','.tiff')
                """,
                (record_key,),
            ).fetchone()[0]
            if state == "selected_master" and selected_count != 1:
                connection.rollback()
                raise WorkbenchError("one-Master invariant failed; decision was not saved")
            connection.commit()
        self.export_decisions()

    def _local_initialize(self) -> None:
        receipt = json.loads(VISUAL_ONLY_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("result") != "ok"
            or receipt.get("classification_authority") != "visual_only"
            or receipt.get("metadata_used_for_matching") is not False
            or receipt.get("filename_used_for_matching") is not False
            or receipt.get("filename_used_for_candidate_routing") is not True
            or int(receipt.get("selected_md_tiff_masters", -1)) != 5282
            or receipt.get("source_read_only") is not True
            or receipt.get("deletion_authorized") is not False
            or receipt.get("drain_contains_only_visual_redundancies") is not True
        ):
            raise WorkbenchError("visual-only workbench preflight is incomplete")
        self.visual_receipt = receipt
        self._visual_new_rows = read_csv(VISUAL_ONLY_NEW_TIFF_CSV)
        self._visual_redundant_rows = read_csv(VISUAL_ONLY_REDUNDANT_CSV)
        self._visual_error_rows = read_csv(VISUAL_ONLY_ERRORS_CSV)
        self._visual_institution_bindings = read_csv(VISUAL_ONLY_INSTITUTION_BINDINGS_CSV)
        self._visual_institution_holds = read_csv(VISUAL_ONLY_INSTITUTION_HOLDS_CSV)
        if len(self._visual_new_rows) != int(receipt.get("genuine_new_tiff_candidate_paths", -1)):
            raise WorkbenchError("visual-only new-TIFF CSV count drifted")
        if len(self._visual_redundant_rows) != int(receipt.get("redundant_image_paths", -1)):
            raise WorkbenchError("visual-only redundant CSV count drifted")
        if len(self._visual_error_rows) != int(receipt.get("visual_error_paths", -1)):
            raise WorkbenchError("visual-only error CSV count drifted")
        if len(self._visual_institution_bindings) != int(receipt.get("institution_selected_tiff_masters", -1)):
            raise WorkbenchError("institution visual binding CSV count drifted")
        if len(self._visual_institution_holds) != int(receipt.get("institution_master_holds", -1)):
            raise WorkbenchError("institution visual hold CSV count drifted")
        self._visual_assets = {
            str(row["asset_id"]): row
            for row in self._visual_new_rows + self._visual_redundant_rows + self._visual_error_rows
        }
        self._visual_groups: dict[str, list[dict[str, str]]] = {}
        for row in self._visual_new_rows:
            self._visual_groups.setdefault(str(row["visual_group_id"]), []).append(row)
        self.reference_photo_receipt: dict[str, Any] | None = None
        self.reference_photo_hold_recovery_receipt: dict[str, Any] | None = None
        self.reference_photo_hold_workbench_receipt: dict[str, Any] | None = None
        self.selected_master_drain_repair_receipt: dict[str, Any] | None = None
        self._reference_photo_workbench_rows: list[dict[str, str]] = []
        self._reference_photo_hold_contexts: dict[str, list[dict[str, str]]] = {}
        self._reference_photo_hold_context_by_id: dict[str, dict[str, str]] = {}
        self._reference_photo_resolved_visual: dict[str, list[dict[str, str]]] = {}
        if REFERENCE_PHOTO_RECEIPT.is_file() and REFERENCE_PHOTO_WORKBENCH_CSV.is_file():
            reference_receipt = json.loads(REFERENCE_PHOTO_RECEIPT.read_text(encoding="utf-8"))
            if (
                reference_receipt.get("result") != "ok"
                or reference_receipt.get("source_read_only") is not True
                or reference_receipt.get("source_files_modified") is not False
                or reference_receipt.get("deletion_authorized") is not False
                or reference_receipt.get("metadata_used_for_visual_matching") is not False
                or reference_receipt.get("filename_used_for_visual_matching") is not False
                or reference_receipt.get("drain_contains_only_redundant") is not True
            ):
                raise WorkbenchError("reference-photo reconciliation receipt is not safe")
            expected_workbench_paths = int(reference_receipt.get("workbench_candidate_paths", -1))
            if REFERENCE_PHOTO_HOLD_WORKBENCH_RECEIPT.is_file():
                if not REFERENCE_PHOTO_HOLD_CONTEXT_CSV.is_file():
                    raise WorkbenchError("reference-photo hold comparison context is missing")
                hold_workbench = json.loads(
                    REFERENCE_PHOTO_HOLD_WORKBENCH_RECEIPT.read_text(encoding="utf-8")
                )
                output_hashes = hold_workbench.get("output_sha256") or {}
                if (
                    hold_workbench.get("result") != "ok"
                    or hold_workbench.get("mode") != "apply"
                    or hold_workbench.get("source_read_only") is not True
                    or hold_workbench.get("source_files_modified") is not False
                    or hold_workbench.get("deletion_authorized") is not False
                    or hold_workbench.get("automatic_master_selection") is not False
                    or hold_workbench.get("sqlite_modified") is not False
                    or hold_workbench.get("sqlite_quick_check") != "ok"
                    or int(hold_workbench.get("sqlite_foreign_key_violations", -1)) != 0
                    or sha256_file(REFERENCE_PHOTO_WORKBENCH_CSV) != output_hashes.get("workbench")
                    or sha256_file(REFERENCE_PHOTO_HOLD_CONTEXT_CSV) != output_hashes.get("context")
                ):
                    raise WorkbenchError("reference-photo hold workbench receipt is not safe")
                expected_workbench_paths = int(
                    hold_workbench.get("workbench_candidate_paths", -1)
                )
                context_rows = read_csv(REFERENCE_PHOTO_HOLD_CONTEXT_CSV)
                if (
                    len(context_rows) != int(hold_workbench.get("comparison_context_rows", -1))
                    or len({row.get("context_id", "") for row in context_rows}) != len(context_rows)
                    or any(row.get("deletion_authorized") != "false" for row in context_rows)
                ):
                    raise WorkbenchError("reference-photo hold context count or safety drifted")
                for row in context_rows:
                    group_id = row.get("visual_group_id", "")
                    context_id = row.get("context_id", "")
                    if not group_id or not context_id:
                        raise WorkbenchError("reference-photo hold context identity is incomplete")
                    self._reference_photo_hold_contexts.setdefault(group_id, []).append(row)
                    self._reference_photo_hold_context_by_id[context_id] = row
                self.reference_photo_hold_workbench_receipt = hold_workbench
            self._reference_photo_workbench_rows = read_csv(REFERENCE_PHOTO_WORKBENCH_CSV)
            if len(self._reference_photo_workbench_rows) != expected_workbench_paths:
                raise WorkbenchError("reference-photo workbench candidate count drifted")
            self.reference_photo_receipt = reference_receipt
            if REFERENCE_PHOTO_HOLD_RECOVERY_RECEIPT.is_file():
                recovery_receipt = json.loads(
                    REFERENCE_PHOTO_HOLD_RECOVERY_RECEIPT.read_text(encoding="utf-8")
                )
                if (
                    recovery_receipt.get("result") != "ok"
                    or recovery_receipt.get("source_read_only") is not True
                    or recovery_receipt.get("source_files_modified") is not False
                    or recovery_receipt.get("deletion_authorized") is not False
                    or recovery_receipt.get("sqlite_quick_check") != "ok"
                    or int(recovery_receipt.get("sqlite_foreign_key_violations", -1)) != 0
                ):
                    raise WorkbenchError("reference-photo hold recovery receipt is not safe")
                self.reference_photo_hold_recovery_receipt = recovery_receipt
            if SELECTED_MASTER_DRAIN_REPAIR_RECEIPT.is_file():
                drain_repair = json.loads(
                    SELECTED_MASTER_DRAIN_REPAIR_RECEIPT.read_text(encoding="utf-8")
                )
                if (
                    drain_repair.get("result") != "ok"
                    or drain_repair.get("source_read_only") is not True
                    or drain_repair.get("source_files_modified") is not False
                    or drain_repair.get("deletion_authorized") is not False
                    or int(drain_repair.get("remaining_selected_master_drain_intersection", -1)) != 0
                ):
                    raise WorkbenchError("selected-Master drain repair receipt is not safe")
                self.selected_master_drain_repair_receipt = drain_repair
            self._visual_assets.update({
                str(row["asset_id"]): row for row in self._reference_photo_workbench_rows
            })
            if REFERENCE_PHOTO_SOURCE_DISPOSITION_CSV.is_file():
                with closing(self.connect(read_only=True)) as connection:
                    visual_group_by_path = {
                        str(row["source_path"]): "visual-" + hashlib.sha256(
                            f"{row['ahash_64']}:{row['dhash_64']}:{row['phash_64']}".encode()
                        ).hexdigest()[:16]
                        for row in connection.execute(
                            """
                            SELECT c.source_path,h.ahash_64,h.dhash_64,h.phash_64
                            FROM candidates c JOIN local_visual_hashes h ON h.candidate_id=c.candidate_id
                            WHERE h.decode_status='ok'
                            """
                        )
                    }
                for row in read_csv(REFERENCE_PHOTO_SOURCE_DISPOSITION_CSV):
                    if not row.get("record_key", "").startswith("local:"):
                        continue
                    asset = self._visual_assets.get(str(row.get("asset_id", "")))
                    group_id = (
                        str(asset.get("visual_group_id", ""))
                        if asset is not None
                        else visual_group_by_path.get(str(row.get("source_path", "")), "")
                    )
                    if group_id:
                        self._reference_photo_resolved_visual.setdefault(group_id, []).append(row)
        with closing(self.connect(read_only=True)) as connection:
            current_md_inventories = {
                str(row[0]).upper().strip()
                for row in connection.execute(
                    "SELECT inventory FROM objects WHERE record_state='current_museum_digital'"
                )
            }
        self._orphan_groups: dict[str, list[dict[str, str]]] = {}
        self._orphan_legacy_aliases: dict[str, list[str]] = {}
        if self.reference_photo_receipt is not None:
            aliases: dict[str, set[str]] = {}
            for row in self._reference_photo_workbench_rows:
                component = str(row.get("normalized_inventory") or "").strip()
                visual_group = str(row.get("visual_group_id") or "").strip()
                if component and visual_group:
                    record_key = "orphan:" + visual_group
                    self._orphan_groups.setdefault(record_key, []).append(row)
                    aliases.setdefault("orphan:" + component, set()).add(record_key)
            self._orphan_legacy_aliases = {
                key: sorted(values) for key, values in aliases.items()
            }
        else:
            for row in self._visual_new_rows:
                inventory = visual_inventory_hint(str(row["filename"]))
                if not inventory or inventory in current_md_inventories:
                    continue
                self._orphan_groups.setdefault("orphan:" + inventory, []).append(row)
        if not self._orphan_groups and not (
            self.reference_photo_receipt is not None
            and int(self.reference_photo_receipt.get("workbench_candidate_paths", -1)) == 0
        ):
            raise WorkbenchError("no TFA orphan inventory candidates were derived from the visual queue")
        self._orphan_lock = threading.Lock()
        self._orphan_initialize()
        self.export_local_decisions()

    def _orphan_initialize(self) -> None:
        if not ORPHAN_DECISIONS_CSV.exists():
            source_to_asset = {
                row["source_path"]: row
                for rows in self._orphan_groups.values()
                for row in rows
            }
            preserved = []
            for prior in read_csv(LOCAL_DECISIONS_CSV):
                if prior.get("outcome") != "new_local_object":
                    continue
                asset = source_to_asset.get(prior.get("selected_source_path", ""))
                inventory = str(prior.get("inventory", "")).upper().strip()
                record_key = "orphan:" + inventory
                if asset is None or record_key not in self._orphan_groups:
                    continue
                preserved.append({
                    "record_key": record_key,
                    "inventory": inventory,
                    "decision": "selected_master",
                    "selected_asset_id": asset["asset_id"],
                    "selected_visual_group_id": asset["visual_group_id"],
                    "selected_source_path": asset["source_path"],
                    "note": prior.get("note", "") or "Preserved prior human local-object Master selection.",
                    "decided_by": prior.get("decided_by", ""),
                    "decided_at": prior.get("decided_at", ""),
                    "deletion_authorized": "false",
                })
            atomic_csv(ORPHAN_DECISIONS_CSV, ORPHAN_DECISION_FIELDS, preserved)
        source_decisions = read_csv(ORPHAN_DECISIONS_CSV)
        if self.reference_photo_receipt is not None and any(
            row.get("record_key", "") in self._orphan_legacy_aliases for row in source_decisions
        ):
            migrated: dict[str, dict[str, str]] = {}
            for decision in source_decisions:
                legacy_key = decision.get("record_key", "")
                targets = self._orphan_legacy_aliases.get(legacy_key)
                if not targets:
                    migrated[legacy_key] = decision
                    continue
                selected_asset_id = decision.get("selected_asset_id", "")
                selected_target = next(
                    (
                        key for key in targets
                        if any(row["asset_id"] == selected_asset_id for row in self._orphan_groups[key])
                    ),
                    "",
                )
                for target in targets:
                    preserve_selection = (
                        decision.get("decision") == "selected_master" and target == selected_target
                    )
                    migrated[target] = {
                        "record_key": target,
                        "inventory": target.removeprefix("orphan:"),
                        "decision": "selected_master" if preserve_selection else "unresolved",
                        "selected_asset_id": selected_asset_id if preserve_selection else "",
                        "selected_visual_group_id": (
                            decision.get("selected_visual_group_id", "") if preserve_selection else ""
                        ),
                        "selected_source_path": (
                            decision.get("selected_source_path", "") if preserve_selection else ""
                        ),
                        "note": (
                            decision.get("note", "")
                            or "Split from the former cross-inventory collision component."
                        ),
                        "decided_by": decision.get("decided_by", ""),
                        "decided_at": decision.get("decided_at", ""),
                        "deletion_authorized": "false",
                    }
            source_decisions = [migrated[key] for key in sorted(migrated)]
            atomic_csv(ORPHAN_DECISIONS_CSV, ORPHAN_DECISION_FIELDS, source_decisions)
        decisions: dict[str, dict[str, str]] = {}
        for decision in source_decisions:
            record_key = decision.get("record_key", "")
            rows = self._orphan_groups.get(record_key)
            selected_asset_id = decision.get("selected_asset_id", "")
            selected = next(
                (row for row in rows or () if row["asset_id"] == selected_asset_id),
                None,
            )
            state = decision.get("decision", "")
            valid_selection = (
                state == "selected_master"
                and selected is not None
                and decision.get("selected_visual_group_id") == selected["visual_group_id"]
                and decision.get("selected_source_path") == selected["source_path"]
            )
            valid_nonselection = (
                state in {"redundant", "noise", "genuine_loss", "unresolved"}
                and selected_asset_id == ""
                and decision.get("selected_visual_group_id", "") == ""
                and decision.get("selected_source_path", "") == ""
            )
            if (
                rows is None
                or record_key in decisions
                or decision.get("inventory") != record_key.removeprefix("orphan:")
                or decision.get("deletion_authorized") != "false"
                or not (valid_selection or valid_nonselection)
            ):
                raise WorkbenchError(f"invalid orphan review decision: {record_key}")
            decisions[record_key] = decision
        self._orphan_decisions = decisions
        self.export_orphan_decisions()

    def orphan_records(self) -> list[dict[str, Any]]:
        records = []
        for record_key, rows in self._orphan_groups.items():
            ordered = sorted(
                rows,
                key=lambda row: (
                    -(int(row.get("width_px") or 0) * int(row.get("height_px") or 0)),
                    -int(row.get("size_bytes") or 0),
                    str(row.get("source_path") or ""),
                ),
            )
            decision = self._orphan_decisions.get(record_key, {})
            representative = next(
                (row for row in ordered if row["asset_id"] == decision.get("selected_asset_id")),
                ordered[0],
            )
            inventory_evidence = ", ".join(sorted({
                value.strip()
                for row in rows
                for value in (
                    str(row.get("normalized_inventory") or ""),
                    *str(row.get("inventory_members") or "").split(";"),
                )
                if value.strip()
            }))
            hold_reasons = "; ".join(sorted({
                str(row.get("classification_basis") or "").split(";", 1)[0]
                for row in rows if row.get("classification_basis")
            }))
            records.append({
                "record_key": record_key,
                "inventory": record_key.removeprefix("orphan:"),
                "inventory_evidence": inventory_evidence,
                "hold_reasons": hold_reasons,
                "state": decision.get("decision", "unreviewed"),
                "selected_asset_id": decision.get("selected_asset_id", ""),
                "note": decision.get("note", ""),
                "decided_by": decision.get("decided_by", ""),
                "decided_at": decision.get("decided_at", ""),
                "asset_id": representative["asset_id"],
                "filename": representative["filename"],
                "source_path": representative["source_path"],
                "width_px": representative["width_px"],
                "height_px": representative["height_px"],
                "size_bytes": representative["size_bytes"],
                "path_count": len(rows),
                "visual_group_count": len({row["visual_group_id"] for row in rows}),
                "decodable_path_count": sum(
                    bool(row.get("ahash"))
                    and int(row.get("width_px") or 0) > 0
                    and int(row.get("height_px") or 0) > 0
                    for row in rows
                ),
            })
        return sorted(records, key=lambda row: (row["inventory"], row["source_path"]))

    def orphan_review_records(self) -> list[dict[str, Any]]:
        """Project current review visibility without rewriting saved decisions."""
        records = self.orphan_records()
        rechecks = {}
        if REDUNDANCY_RECHECK_RECEIPT.is_file():
            receipt = json.loads(REDUNDANCY_RECHECK_RECEIPT.read_text(encoding="utf-8"))
            if (
                receipt.get("result") != "ok"
                or receipt.get("source_modified") is not False
                or receipt.get("deletion_authorized") is not False
            ):
                raise WorkbenchError("redundancy recheck receipt is not safe")
            rechecks = {row["record_key"]: row for row in receipt["hold_review"]}
        for record in records:
            record["review_state"] = record["state"]
            record["recheck_note"] = ""
            recheck = rechecks.get(record["record_key"], {})
            # A later human decision supersedes this particular recheck.
            if (
                recheck.get("outcome", "").startswith("retain_")
                and self._orphan_decisions.get(record["record_key"]) == recheck.get("prior_decision")
                and any(row["source_path"] == recheck.get("source_path")
                        for row in self._orphan_groups[record["record_key"]])
            ):
                record["review_state"] = "unresolved"
                record["recheck_note"] = recheck["evidence_note"]
        return records

    def orphan_record(self, record_key: str) -> dict[str, Any]:
        rows = self._orphan_groups.get(record_key)
        if rows is None:
            raise WorkbenchError("record is outside the TFA orphan review scope")
        record = next(row for row in self.orphan_records() if row["record_key"] == record_key)
        return {**record, "rows": sorted(rows, key=lambda row: (
            row["visual_group_id"],
            -(int(row.get("width_px") or 0) * int(row.get("height_px") or 0)),
            -int(row.get("size_bytes") or 0),
            row["source_path"],
        ))}

    def guarded_visual_asset(self, asset_id: str) -> Path:
        asset = self.visual_asset(asset_id)
        if (
            not asset.get("ahash")
            or int(asset.get("width_px") or 0) <= 0
            or int(asset.get("height_px") or 0) <= 0
        ):
            raise WorkbenchError("undecodable hold evidence cannot be selected as a Master")
        path = Path(str(asset["source_path"])).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("visual asset escaped the protected source root") from error
        stat = path.stat()
        if stat.st_size != int(asset["size_bytes"]) or stat.st_mtime_ns != int(asset["mtime_ns"]):
            raise WorkbenchError("visual asset changed after the protected scan")
        if path.suffix.casefold() not in TIFF_EXTENSIONS:
            raise WorkbenchError("orphan Master candidate is not a TIFF")
        return path

    def save_orphan_decision(
        self, record_key: str, action: str, selected_asset_id: str, note: str,
    ) -> None:
        rows = self._orphan_groups.get(record_key)
        if rows is None:
            raise WorkbenchError("record is outside the TFA orphan review scope")
        if action not in {
            "select_master", "mark_redundant", "dismiss_noise", "record_genuine_loss",
            "unresolved",
        }:
            raise WorkbenchError("unsupported orphan review decision")
        if len(note) > 2000:
            raise WorkbenchError("decision note is too large")
        if action == "mark_redundant" and not any(
            self.hold_contexts(row["visual_group_id"]) for row in rows
        ):
            raise WorkbenchError("held image has no existing Master comparison")
        if action == "record_genuine_loss" and any(
            row.get("ahash")
            or int(row.get("width_px") or 0) > 0
            or int(row.get("height_px") or 0) > 0
            for row in rows
        ):
            raise WorkbenchError("genuine loss is restricted to undecodable visual evidence")
        selected = None
        decision = (
            "redundant" if action == "mark_redundant"
            else "genuine_loss" if action == "record_genuine_loss"
            else "noise" if action == "dismiss_noise"
            else "unresolved"
        )
        if action == "select_master":
            selected = next((row for row in rows if row["asset_id"] == selected_asset_id), None)
            if selected is None:
                raise WorkbenchError("select exactly one TIFF from this orphan candidate")
            self.guarded_visual_asset(selected_asset_id)
            decision = "selected_master"
        source_mount()
        item = {
            "record_key": record_key,
            "inventory": record_key.removeprefix("orphan:"),
            "decision": decision,
            "selected_asset_id": selected["asset_id"] if selected else "",
            "selected_visual_group_id": selected["visual_group_id"] if selected else "",
            "selected_source_path": selected["source_path"] if selected else "",
            "note": note.strip(),
            "decided_by": self.actor,
            "decided_at": utc_now(),
            "deletion_authorized": "false",
        }
        with self._orphan_lock:
            self._orphan_decisions[record_key] = item
            self.export_orphan_decisions()

    def export_orphan_decisions(self) -> None:
        atomic_csv(
            ORPHAN_DECISIONS_CSV,
            ORPHAN_DECISION_FIELDS,
            (self._orphan_decisions[key] for key in sorted(self._orphan_decisions)),
        )
        dispositions = []
        for record_key in sorted(self._orphan_groups):
            decision = self._orphan_decisions.get(record_key, {})
            state = decision.get("decision", "unreviewed")
            selected_asset_id = decision.get("selected_asset_id", "")
            for row in sorted(self._orphan_groups[record_key], key=lambda item: item["source_path"]):
                disposition = orphan_candidate_disposition(state, row["asset_id"], selected_asset_id)
                if disposition == "selected_master":
                    basis, drain = "human_selected_single_tiff_master", "retained_master"
                elif disposition == "redundant":
                    basis, drain = (
                        "human_marked_redundant_to_existing_comparison_master"
                        if state == "redundant"
                        else "redundant_after_single_master_selection"
                    ), "proposed_redundant"
                elif disposition == "noise":
                    basis, drain = "human_dismissed_orphan_candidate_as_noise", "proposed_noise"
                elif disposition == "genuine_loss":
                    basis, drain = (
                        "human_confirmed_undecodable_no_surviving_visual",
                        "excluded_genuine_loss_preserve_evidence",
                    )
                else:
                    basis, drain = "awaiting_human_orphan_review", "pending_human_confirmation"
                dispositions.append({
                    "record_key": record_key,
                    "inventory": record_key.removeprefix("orphan:"),
                    "visual_group_id": row["visual_group_id"],
                    "asset_id": row["asset_id"],
                    "source_path": row["source_path"],
                    "extension": row["extension"],
                    "size_bytes": row["size_bytes"],
                    "preservation_status": disposition,
                    "preservation_basis": basis,
                    "drain_state": drain,
                    "deletion_authorized": "false",
                })
        atomic_csv(ORPHAN_DISPOSITIONS_CSV, ORPHAN_DISPOSITION_FIELDS, dispositions)

    def visual_records(self, state: str) -> list[dict[str, Any]]:
        if state == "new":
            records = []
            for group_id, rows in self._visual_groups.items():
                ordered = sorted(
                    rows,
                    key=lambda row: (
                        -(int(row.get("width_px") or 0) * int(row.get("height_px") or 0)),
                        -int(row.get("size_bytes") or 0),
                        str(row.get("source_path") or ""),
                    ),
                )
                representative = ordered[0]
                records.append({
                    "record_key": group_id,
                    "state": "new_tiff_candidate",
                    "asset_id": representative["asset_id"],
                    "filename": representative["filename"],
                    "source_path": representative["source_path"],
                    "width_px": representative["width_px"],
                    "height_px": representative["height_px"],
                    "size_bytes": representative["size_bytes"],
                    "path_count": len(rows),
                    "detail": f"{len(rows)} image path(s)",
                })
            return sorted(records, key=lambda row: (str(row["filename"]).casefold(), row["source_path"]))
        if state == "redundant":
            return [
                {
                    "record_key": "redundant:" + str(row["asset_id"]),
                    "state": "redundant_to_md",
                    "asset_id": row["asset_id"],
                    "filename": row["filename"],
                    "source_path": row["source_path"],
                    "width_px": row["width_px"],
                    "height_px": row["height_px"],
                    "size_bytes": row["size_bytes"],
                    "path_count": 1,
                    "detail": f"md:{row['matched_md_object_ids']} · distance {row['distance_total']}",
                }
                for row in self._visual_redundant_rows
            ]
        if state == "errors":
            return [
                {
                    "record_key": "error:" + str(row["asset_id"]),
                    "state": "decode_error_hold",
                    "asset_id": row["asset_id"],
                    "filename": row["filename"],
                    "source_path": row["source_path"],
                    "width_px": row["width_px"],
                    "height_px": row["height_px"],
                    "size_bytes": row["size_bytes"],
                    "path_count": 1,
                    "detail": row["error"],
                }
                for row in self._visual_error_rows
            ]
        raise WorkbenchError(f"unknown visual queue state: {state}")

    def visual_record(self, record_key: str) -> dict[str, Any]:
        if record_key.startswith("visual-"):
            rows = self._visual_groups.get(record_key)
            if not rows:
                raise WorkbenchError(f"unknown visual TIFF group: {record_key}")
            return {"kind": "new", "record_key": record_key, "rows": rows}
        if record_key.startswith("redundant:"):
            asset_id = record_key.split(":", 1)[1]
            row = self._visual_assets.get(asset_id)
            if row is None or "matched_md_object_ids" not in row:
                raise WorkbenchError(f"unknown visual redundant row: {record_key}")
            return {"kind": "redundant", "record_key": record_key, "rows": [row]}
        if record_key.startswith("error:"):
            asset_id = record_key.split(":", 1)[1]
            row = self._visual_assets.get(asset_id)
            if row is None or "error" not in row:
                raise WorkbenchError(f"unknown visual error row: {record_key}")
            return {"kind": "errors", "record_key": record_key, "rows": [row]}
        raise WorkbenchError("metadata-keyed local records are disabled during visual-only reconciliation")

    def visual_asset(self, asset_id: str) -> dict[str, str]:
        row = self._visual_assets.get(asset_id)
        if row is None:
            raise WorkbenchError("unknown visual asset")
        return row

    def hold_contexts(self, visual_group_id: str) -> list[dict[str, str]]:
        return list(self._reference_photo_hold_contexts.get(visual_group_id, ()))

    def guarded_hold_context(self, context_id: str) -> Path:
        row = self._reference_photo_hold_context_by_id.get(context_id)
        if row is None or not row.get("source_path") or row.get("image_url"):
            raise WorkbenchError("unknown local hold comparison context")
        path = Path(row["source_path"]).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("hold comparison context escaped the protected source root") from error
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
            raise WorkbenchError("hold comparison context changed after the protected scan")
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise WorkbenchError("hold comparison context is not a supported image")
        return path

    def local_records(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT o.record_key, o.inventory, m.title, m.description, m.date_text,
                           m.object_type, m.material, m.dimensions, m.metadata_state,
                           m.historical_md_id, m.source_count,
                           d.outcome, d.selected_candidate_id, d.matched_md_object_id,
                           d.note, d.decided_by, d.decided_at,
                           (SELECT COUNT(*) FROM local_candidate_technical t
                             WHERE t.record_key=o.record_key) AS image_count,
                           (SELECT COUNT(*) FROM local_candidate_technical t
                             WHERE t.record_key=o.record_key AND t.accession_eligible=1) AS eligible_count,
                           (SELECT COUNT(*) FROM local_candidate_technical t
                             WHERE t.record_key=o.record_key AND t.accession_eligible=1
                               AND lower(t.extension) IN ('.tif','.tiff')) AS eligible_tiff_count,
                           (SELECT COUNT(*) FROM local_candidate_technical t
                             WHERE t.record_key=o.record_key AND t.accession_eligible=1
                               AND lower(t.extension) IN ('.jpg','.jpeg')) AS eligible_jpeg_count,
                           (SELECT COUNT(*) FROM local_md_match_candidates x
                             WHERE x.record_key=o.record_key) AS md_match_count,
                           COALESCE((SELECT x.md_object_id FROM local_md_match_candidates x
                             WHERE x.record_key=o.record_key ORDER BY x.rank LIMIT 1),'') AS top_md_object_id,
                           COALESCE((SELECT x.md_inventory FROM local_md_match_candidates x
                             WHERE x.record_key=o.record_key ORDER BY x.rank LIMIT 1),'') AS top_md_inventory,
                           COALESCE((SELECT x.match_basis FROM local_md_match_candidates x
                             WHERE x.record_key=o.record_key ORDER BY x.rank LIMIT 1),'') AS top_match_basis,
                           (SELECT COUNT(*) FROM local_md_match_candidates x
                             WHERE x.record_key=o.record_key
                               AND {EXACT_EXISTING_MD_SQL}) AS exact_md_match_count,
                           COALESCE((SELECT c.candidate_id FROM candidates c
                             WHERE c.record_key=o.record_key
                               AND lower(c.extension) IN ('.jpg','.jpeg','.png','.webp')
                             ORDER BY CASE WHEN lower(c.source_path) LIKE '%800px%' THEN 0 ELSE 1 END,
                                      c.size_bytes, c.source_path LIMIT 1),0) AS preview_candidate_id
                    FROM objects o
                    JOIN local_object_metadata m ON m.record_key=o.record_key
                    LEFT JOIN local_object_decisions d ON d.record_key=o.record_key
                    WHERE o.record_state='local_tfa_review'
                    ORDER BY o.inventory COLLATE NOCASE, o.record_key
                    """
                )
            ]
        for row in rows:
            row["state"] = row.get("outcome") or (
                "existing_md_exact" if int(row["exact_md_match_count"]) == 1 else "unreviewed"
            )
        return rows

    def local_record(self, record_key: str) -> dict[str, Any]:
        with closing(self.connect(read_only=True)) as connection:
            obj = connection.execute(
                """
                SELECT o.record_key, o.inventory, m.*,
                       d.outcome, d.selected_candidate_id, d.matched_md_object_id,
                       d.title AS decided_title, d.description AS decided_description,
                       d.date_text AS decided_date_text, d.object_type AS decided_object_type,
                       d.material AS decided_material, d.dimensions AS decided_dimensions,
                       d.note, d.decided_by, d.decided_at
                FROM objects o JOIN local_object_metadata m ON m.record_key=o.record_key
                LEFT JOIN local_object_decisions d ON d.record_key=o.record_key
                WHERE o.record_state='local_tfa_review' AND o.record_key=?
                """,
                (record_key,),
            ).fetchone()
            if obj is None:
                raise WorkbenchError(f"unknown local TFA record: {record_key}")
            candidates = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT c.*, t.width_px, t.height_px, t.file_type, t.mime_type,
                           t.header_status, t.accession_eligible, t.eligibility_basis,
                           t.embedded_metadata_json, t.technical_error
                    FROM candidates c
                    JOIN local_candidate_technical t ON t.candidate_id=c.candidate_id
                    WHERE c.record_key=?
                    ORDER BY t.accession_eligible DESC,
                      CASE WHEN lower(c.extension) IN ('.tif','.tiff') THEN 0 ELSE 1 END,
                      c.size_bytes DESC, c.source_path
                    """,
                    (record_key,),
                )
            ]
            matches = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM local_md_match_candidates WHERE record_key=? ORDER BY rank",
                    (record_key,),
                )
            ]
            exact_matches = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT x.*,
                           CASE
                             WHEN {EXACT_ID_MATCH_SQL} THEN 'exact inventory or historical MD identifier'
                             WHEN {EXACT_SHARED_PATH_SQL} THEN 'same source candidate path already bound to current MD object'
                             ELSE 'byte-identical local TIFF and selected MD Master (stored SHA-256)'
                           END AS exact_identity_basis
                    FROM local_md_match_candidates x
                    WHERE x.record_key=? AND {EXACT_EXISTING_MD_SQL}
                    ORDER BY x.rank
                    """,
                    (record_key,),
                )
            ]
        item = dict(obj)
        item["candidates"] = candidates
        item["matches"] = matches
        item["exact_existing_md_match"] = exact_matches[0] if len(exact_matches) == 1 else None
        item["state"] = item.get("outcome") or (
            "existing_md_exact" if item["exact_existing_md_match"] else "unreviewed"
        )
        try:
            source_rows = json.loads(str(item.get("source_rows_json") or "[]"))
        except json.JSONDecodeError:
            source_rows = []
        item["source_rows"] = source_rows if isinstance(source_rows, list) else []
        return item

    def save_local_decision(self, form: dict[str, str]) -> None:
        record_key = form.get("record_key", "")
        action = form.get("action", "")
        if action not in {"duplicate_md", "new_local_object", "unresolved"}:
            raise WorkbenchError("unsupported local-object decision")
        current = self.local_record(record_key)
        fields = {
            name: form.get(name, str(current.get(name, ""))).strip()
            for name in ("title", "description", "date_text", "object_type", "material", "dimensions")
        }
        if not fields["title"]:
            raise WorkbenchError("a checked local title is required")
        if any(len(value) > 100_000 for value in fields.values()):
            raise WorkbenchError("metadata field is too large")
        note = form.get("note", "").strip()
        if len(note) > 10_000:
            raise WorkbenchError("decision note is too large")
        selected_candidate_id: int | None = None
        matched_md_object_id: str | None = None
        if action == "new_local_object":
            raw = form.get("selected_candidate_id", "")
            if not raw.isdigit():
                raise WorkbenchError("select exactly one eligible TIFF or HiRes JPEG Master")
            selected_candidate_id = int(raw)
            with closing(self.connect(read_only=True)) as connection:
                candidate = connection.execute(
                    """
                    SELECT c.record_key, t.accession_eligible FROM candidates c
                    JOIN local_candidate_technical t ON t.candidate_id=c.candidate_id
                    WHERE c.candidate_id=?
                    """,
                    (selected_candidate_id,),
                ).fetchone()
            if candidate is None or candidate["record_key"] != record_key or int(candidate["accession_eligible"]) != 1:
                raise WorkbenchError("selected file is not an eligible candidate for this local object")
            self.guarded_candidate_path(selected_candidate_id)
        elif action == "duplicate_md":
            matched_md_object_id = form.get("matched_md_object_id", "").strip()
            with closing(self.connect(read_only=True)) as connection:
                valid = connection.execute(
                    "SELECT COUNT(*) FROM local_md_match_candidates WHERE record_key=? AND md_object_id=?",
                    (record_key, matched_md_object_id),
                ).fetchone()[0]
            if valid != 1:
                raise WorkbenchError("select one reviewed museum-digital match")
        source_mount()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO local_object_decisions
                  (record_key,outcome,selected_candidate_id,matched_md_object_id,
                   title,description,date_text,object_type,material,dimensions,
                   note,decided_by,decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                  outcome=excluded.outcome,
                  selected_candidate_id=excluded.selected_candidate_id,
                  matched_md_object_id=excluded.matched_md_object_id,
                  title=excluded.title,description=excluded.description,date_text=excluded.date_text,
                  object_type=excluded.object_type,material=excluded.material,dimensions=excluded.dimensions,
                  note=excluded.note,decided_by=excluded.decided_by,decided_at=excluded.decided_at
                """,
                (
                    record_key, action, selected_candidate_id, matched_md_object_id,
                    fields["title"], fields["description"], fields["date_text"],
                    fields["object_type"], fields["material"], fields["dimensions"],
                    note, self.actor, utc_now(),
                ),
            )
            if action == "new_local_object":
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM local_object_decisions d
                    JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    JOIN local_candidate_technical t ON t.candidate_id=c.candidate_id
                    WHERE d.record_key=? AND d.outcome='new_local_object'
                      AND c.record_key=d.record_key AND t.accession_eligible=1
                    """,
                    (record_key,),
                ).fetchone()[0]
                if count != 1:
                    connection.rollback()
                    raise WorkbenchError("one-Master invariant failed; local decision was not saved")
            connection.commit()
        self.export_local_decisions()

    def export_local_decisions(self) -> None:
        with closing(self.connect(read_only=True)) as connection:
            decisions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.*, o.inventory, COALESCE(c.source_path,'') AS selected_source_path
                    FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
                    LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    ORDER BY o.inventory COLLATE NOCASE, d.record_key
                    """
                )
            ]
            dispositions = []
            for row in connection.execute(
                """
                SELECT d.record_key,o.inventory,d.outcome,d.selected_candidate_id,d.matched_md_object_id,
                       c.candidate_id,c.source_path,c.extension,c.size_bytes,
                       COALESCE((
                         SELECT selected_master.source_path
                         FROM reconciliation_decisions selected_decision
                         JOIN candidates selected_master
                           ON selected_master.candidate_id=selected_decision.selected_candidate_id
                         WHERE selected_decision.record_key='md:'||d.matched_md_object_id
                           AND selected_decision.decision_state='selected_master'
                           AND lower(selected_master.extension) IN ('.tif','.tiff')
                       ),'') AS existing_md_selected_master_path,
                       (
                         SELECT COUNT(*) FROM candidates curated_candidate
                         JOIN local_candidate_technical curated_technical
                           ON curated_technical.candidate_id=curated_candidate.candidate_id
                         WHERE curated_candidate.record_key=d.record_key
                           AND curated_technical.accession_eligible=1
                           AND lower(curated_candidate.extension) IN ('.tif','.tiff')
                           AND curated_candidate.source_path LIKE '%/Alle-Tiff-Positive/%'
                       ) AS curated_master_count
                FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
                JOIN candidates c ON c.record_key=d.record_key
                ORDER BY o.inventory COLLATE NOCASE,c.source_path
                """
            ):
                extension = str(row["extension"]).casefold()
                if extension not in IMAGE_EXTENSIONS:
                    disposition = "retain_metadata_evidence"
                    basis = "non-image evidence remains attached to the object"
                elif row["outcome"] == "new_local_object":
                    disposition = "selected_master" if row["candidate_id"] == row["selected_candidate_id"] else "redundant"
                    basis = "human selected one local Master; all other image candidates are redundant"
                elif row["outcome"] == "duplicate_md":
                    if (
                        row["existing_md_selected_master_path"]
                        and row["source_path"] == row["existing_md_selected_master_path"]
                    ):
                        disposition = "existing_md_selected_master"
                        basis = f"retained selected TIFF Master for md:{row['matched_md_object_id']}"
                    elif (
                        not row["existing_md_selected_master_path"]
                        and int(row["curated_master_count"]) == 1
                        and extension in TIFF_EXTENSIONS
                        and SELECTION_TOKEN in str(row["source_path"])
                    ):
                        disposition = "existing_md_selected_master"
                        basis = (
                            f"retained authoritative Alle-Tiff-Positive TIFF Master "
                            f"for institution md:{row['matched_md_object_id']}"
                        )
                    else:
                        disposition = "redundant_to_existing_md_object"
                        basis = f"human matched existing md:{row['matched_md_object_id']}"
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
        atomic_csv(
            LOCAL_DECISIONS_CSV,
            (
                "record_key","outcome","selected_candidate_id","matched_md_object_id","title","description",
                "date_text","object_type","material","dimensions","note","decided_by","decided_at",
                "inventory","selected_source_path",
            ),
            decisions,
        )
        atomic_csv(
            LOCAL_DISPOSITIONS_CSV,
            (
                "record_key","inventory","outcome","matched_md_object_id","candidate_id","source_path",
                "extension","size_bytes","disposition","disposition_basis","deletion_authorized",
            ),
            dispositions,
        )

    def _document_initialize(self) -> None:
        summary = json.loads(DOCUMENT_PREPARATION_SUMMARY.read_text(encoding="utf-8"))
        candidates = read_csv(DOCUMENT_CANDIDATES_CSV)
        candidates_by_id = {row["document_id"]: row for row in candidates}
        plan = {row["document_id"]: row for row in read_csv(DOCUMENT_ACCESSION_PLAN_CSV)}
        review_rows: list[dict[str, str]] = []
        for document_id, planned in plan.items():
            if planned.get("proposed_decision") != "unresolved":
                continue
            candidate = candidates_by_id.get(document_id)
            if candidate is None:
                raise WorkbenchError(f"unresolved TFA document lacks candidate evidence: {document_id}")
            if (
                planned.get("source_path") != candidate.get("source_path")
                or planned.get("source_sha256") != candidate.get("content_sha256")
                or planned.get("size_bytes") != candidate.get("size_bytes")
            ):
                raise WorkbenchError(f"ambiguous TFA document binding changed: {document_id}")
            review_rows.append({**candidate, **{
                "classification": planned.get("classification", ""),
                "final_relative_path": planned.get("final_relative_path", ""),
                "condition": planned.get("condition", ""),
                "selection_basis": planned.get("selection_basis", ""),
            }})

        expected_scope = int(summary.get("document_review_scope", -1))
        if len(review_rows) != expected_scope:
            raise WorkbenchError(
                f"TFA document review scope drifted: total={len(review_rows)}/{expected_scope}"
            )
        self._document_rows = tuple(
            sorted(review_rows, key=lambda row: (row["filename"].casefold(), row["source_path"]))
        )
        self._document_by_id = {row["document_id"]: row for row in self._document_rows}
        if len(self._document_by_id) != len(self._document_rows):
            raise WorkbenchError("TFA document review identifiers are not unique")

        if not DOCUMENT_DECISIONS_CSV.exists():
            atomic_csv(
                DOCUMENT_DECISIONS_CSV,
                (
                    "document_id", "decision", "note", "decided_by", "decided_at",
                    "source_path", "source_sha256", "deletion_authorized",
                ),
                (),
            )
        decisions: dict[str, dict[str, str]] = {}
        preserved_decisions: dict[str, dict[str, str]] = {}
        for decision in read_csv(DOCUMENT_DECISIONS_CSV):
            document_id = decision.get("document_id", "")
            candidate = self._document_by_id.get(document_id)
            planned = plan.get(document_id)
            if candidate is None:
                preserved_match = planned is not None and (
                    (
                        decision.get("decision") == "include_tfa_documentation"
                        and planned.get("proposed_decision") == "proposed_accession"
                    )
                    or (
                        decision.get("decision") == "exclude_from_tfa_documentation"
                        and planned.get("proposed_decision") == "proposed_exclusion"
                    )
                )
                if (
                    planned is None
                    or document_id in preserved_decisions
                    or not preserved_match
                    or decision.get("source_path") != planned.get("source_path")
                    or decision.get("source_sha256") != planned.get("source_sha256")
                    or decision.get("deletion_authorized") != "false"
                ):
                    raise WorkbenchError(f"invalid preserved TFA document review decision: {document_id}")
                preserved_decisions[document_id] = decision
                continue
            if (
                document_id in decisions
                or decision.get("decision") not in {
                    "include_tfa_documentation", "exclude_from_tfa_documentation", "unresolved"
                }
                or decision.get("source_path") != candidate.get("source_path")
                or decision.get("source_sha256") != candidate.get("content_sha256")
                or decision.get("deletion_authorized") != "false"
            ):
                raise WorkbenchError(f"invalid TFA document review decision: {document_id}")
            decisions[document_id] = decision
        self._document_decisions = decisions
        self._document_preserved_decisions = preserved_decisions

    def document_records(self) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for candidate in self._document_rows:
            decision = self._document_decisions.get(candidate["document_id"], {})
            records.append({
                **candidate,
                "review_state": decision.get("decision", "unreviewed"),
                "review_note": decision.get("note", ""),
                "decided_by": decision.get("decided_by", ""),
                "decided_at": decision.get("decided_at", ""),
            })
        return records

    def document_record(self, document_id: str) -> dict[str, str]:
        if document_id not in self._document_by_id:
            raise WorkbenchError("document is outside the ambiguous TFA documentation queue")
        return next(row for row in self.document_records() if row["document_id"] == document_id)

    def guarded_document_path(self, document_id: str) -> Path:
        candidate = self._document_by_id.get(document_id)
        if candidate is None:
            raise WorkbenchError("document is outside the ambiguous TFA documentation queue")
        path = Path(candidate["source_path"]).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("document escaped the protected source root") from error
        if path.stat().st_size != int(candidate["size_bytes"]):
            raise WorkbenchError("document size changed after indexing")
        actual_hash = sha256_file(path).removeprefix("sha256:")
        if actual_hash != candidate["content_sha256"]:
            raise WorkbenchError("document content changed after indexing")
        return path

    def save_document_decision(self, document_id: str, action: str, note: str) -> None:
        if action not in {
            "include_tfa_documentation", "exclude_from_tfa_documentation", "unresolved"
        }:
            raise WorkbenchError("unsupported TFA document review decision")
        candidate = self._document_by_id.get(document_id)
        if candidate is None:
            raise WorkbenchError("document is outside the ambiguous TFA documentation queue")
        if len(note) > 2000:
            raise WorkbenchError("decision note is too large")
        self.guarded_document_path(document_id)
        self._document_decisions[document_id] = {
            "document_id": document_id,
            "decision": action,
            "note": note.strip(),
            "decided_by": self.actor,
            "decided_at": utc_now(),
            "source_path": candidate["source_path"],
            "source_sha256": candidate["content_sha256"],
            "deletion_authorized": "false",
        }
        atomic_csv(
            DOCUMENT_DECISIONS_CSV,
            (
                "document_id", "decision", "note", "decided_by", "decided_at",
                "source_path", "source_sha256", "deletion_authorized",
            ),
            (
                {**self._document_preserved_decisions, **self._document_decisions}[key]
                for key in sorted({**self._document_preserved_decisions, **self._document_decisions})
            ),
        )

    def _metadata_initialize(self) -> None:
        summary = json.loads(METADATA_PREPARATION_SUMMARY.read_text(encoding="utf-8"))
        if (
            summary.get("status") != "ready_for_human_confirmation"
            or summary.get("authority") != "museum-digital current collection 857 capture"
            or summary.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("TFA metadata preparation receipt is not safe")
        rows = [
            row for row in read_csv(METADATA_ACCESSION_PLAN_CSV)
            if row.get("proposed_decision") == "unresolved"
        ]
        expected_scope = int(summary.get("metadata_review_scope", -1))
        if len(rows) != expected_scope:
            raise WorkbenchError(f"TFA metadata review scope drifted: {len(rows)}/{expected_scope}")
        self._metadata_rows = tuple(sorted(rows, key=lambda row: (row["filename"].casefold(), row["source_path"])))
        self._metadata_by_id = {row["metadata_id"]: row for row in self._metadata_rows}
        if len(self._metadata_by_id) != len(self._metadata_rows):
            raise WorkbenchError("TFA metadata review identifiers are not unique")
        if not METADATA_DECISIONS_CSV.exists():
            atomic_csv(
                METADATA_DECISIONS_CSV,
                ("metadata_id", "decision", "note", "decided_by", "decided_at",
                 "source_path", "source_sha256", "deletion_authorized"),
                (),
            )
        decisions: dict[str, dict[str, str]] = {}
        for decision in read_csv(METADATA_DECISIONS_CSV):
            metadata_id = decision.get("metadata_id", "")
            candidate = self._metadata_by_id.get(metadata_id)
            if (
                candidate is None or metadata_id in decisions
                or decision.get("decision") not in {
                    "include_metadata_evidence", "exclude_metadata_evidence", "unresolved"
                }
                or decision.get("source_path") != candidate.get("source_path")
                or decision.get("source_sha256") != candidate.get("source_sha256")
                or decision.get("deletion_authorized") != "false"
            ):
                raise WorkbenchError(f"invalid TFA metadata review decision: {metadata_id}")
            decisions[metadata_id] = decision
        self._metadata_decisions = decisions

    def metadata_records(self) -> list[dict[str, str]]:
        return [{
            **candidate,
            "review_state": self._metadata_decisions.get(candidate["metadata_id"], {}).get("decision", "unreviewed"),
            "review_note": self._metadata_decisions.get(candidate["metadata_id"], {}).get("note", ""),
            "decided_by": self._metadata_decisions.get(candidate["metadata_id"], {}).get("decided_by", ""),
            "decided_at": self._metadata_decisions.get(candidate["metadata_id"], {}).get("decided_at", ""),
        } for candidate in self._metadata_rows]

    def metadata_record(self, metadata_id: str) -> dict[str, str]:
        if metadata_id not in self._metadata_by_id:
            raise WorkbenchError("metadata item is outside the ambiguous TFA metadata queue")
        return next(row for row in self.metadata_records() if row["metadata_id"] == metadata_id)

    def guarded_metadata_path(self, metadata_id: str) -> Path:
        candidate = self._metadata_by_id.get(metadata_id)
        if candidate is None:
            raise WorkbenchError("metadata item is outside the ambiguous TFA metadata queue")
        path = Path(candidate["source_path"]).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("metadata item escaped the protected source root") from error
        if path.stat().st_size != int(candidate["size_bytes"]):
            raise WorkbenchError("metadata item size changed after preparation")
        if sha256_file(path).removeprefix("sha256:") != candidate["source_sha256"]:
            raise WorkbenchError("metadata item content changed after preparation")
        return path

    def save_metadata_decision(self, metadata_id: str, action: str, note: str) -> None:
        if action not in {"include_metadata_evidence", "exclude_metadata_evidence", "unresolved"}:
            raise WorkbenchError("unsupported TFA metadata review decision")
        candidate = self._metadata_by_id.get(metadata_id)
        if candidate is None:
            raise WorkbenchError("metadata item is outside the ambiguous TFA metadata queue")
        if len(note) > 2000:
            raise WorkbenchError("decision note is too large")
        self.guarded_metadata_path(metadata_id)
        self._metadata_decisions[metadata_id] = {
            "metadata_id": metadata_id, "decision": action, "note": note.strip(),
            "decided_by": self.actor, "decided_at": utc_now(),
            "source_path": candidate["source_path"], "source_sha256": candidate["source_sha256"],
            "deletion_authorized": "false",
        }
        atomic_csv(
            METADATA_DECISIONS_CSV,
            ("metadata_id", "decision", "note", "decided_by", "decided_at",
             "source_path", "source_sha256", "deletion_authorized"),
            (self._metadata_decisions[key] for key in sorted(self._metadata_decisions)),
        )

    def _local_metadata_review_initialize(self) -> None:
        receipt = json.loads(LOCAL_METADATA_REVIEW_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("format") != "iss-tfa-buchprojekt-local-metadata-review-v1"
            or receipt.get("result") != "ok"
            or receipt.get("source_read_only") is not True
            or receipt.get("source_modified") is not False
            or receipt.get("published_tfa_unit_changed") is not False
            or receipt.get("automatic_metadata_application") is not False
            or receipt.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("local-object metadata review receipt is not safe")
        candidates = read_csv(LOCAL_METADATA_REVIEW_CANDIDATES_CSV)
        if len(candidates) != int(receipt.get("structured_candidate_rows", -1)):
            raise WorkbenchError("local-object metadata candidate row count drifted")
        by_id: dict[str, dict[str, str]] = {}
        groups: dict[str, list[dict[str, str]]] = {}
        allowed_states = {
            "exact_local_inventory_row",
            "existing_md_row_inventory_requires_review",
            "alternate_row_inventory_requires_review",
            "row_inventory_missing_or_ambiguous",
            "comparison_only_multi_inventory_row",
            "book_inventory_and_visual_match",
            "book_visual_match_related_object",
        }
        for candidate in candidates:
            candidate_id = candidate.get("candidate_id", "")
            record_key = candidate.get("record_key", "")
            state = candidate.get("identity_review_state", "")
            if (
                not candidate_id or candidate_id in by_id or not record_key
                or state not in allowed_states
                or candidate.get("deletion_authorized") != "false"
                or candidate.get("source_path", "") == ""
                or candidate.get("source_sha256", "") == ""
            ):
                raise WorkbenchError(f"invalid local-object metadata candidate: {candidate_id}")
            if state == "exact_local_inventory_row" and (
                candidate.get("row_scope") != "single_inventory_row"
                or candidate.get("row_inventory") != candidate.get("inventory")
                or candidate.get("md_object_id")
            ):
                raise WorkbenchError(f"unsafe exact local metadata candidate: {candidate_id}")
            if state == "comparison_only_multi_inventory_row" and candidate.get("row_scope") == "single_inventory_row":
                raise WorkbenchError(f"invalid comparison-only metadata candidate: {candidate_id}")
            by_id[candidate_id] = candidate
            groups.setdefault(record_key, []).append(candidate)
        if len(groups) != int(receipt.get("structured_candidate_target_count", -1)):
            raise WorkbenchError("local-object metadata candidate object count drifted")
        self._local_metadata_review_receipt = receipt
        self._local_metadata_review_by_id = by_id
        self._local_metadata_review_groups = {
            key: tuple(sorted(rows, key=lambda row: (
                row["source_path"], row["sheet_name"], int(row["row_number"])
            )))
            for key, rows in groups.items()
        }
        with closing(self.connect(read_only=True)) as connection:
            selected_rows = {
                row["record_key"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT d.record_key,c.candidate_id,c.source_path
                    FROM local_object_decisions d
                    JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                    WHERE d.outcome='new_local_object'
                    """
                )
                if row["record_key"] in groups
            }
        for record_key, rows in self._local_metadata_review_groups.items():
            selected = selected_rows.get(record_key)
            if selected is None or selected["source_path"] != rows[0]["selected_master_path"]:
                raise WorkbenchError(f"local metadata preview Master drifted: {record_key}")
        self._local_metadata_review_selected = selected_rows
        decisions: dict[str, dict[str, str]] = {}
        for decision in read_csv(LOCAL_METADATA_REVIEW_DECISIONS_CSV):
            record_key = decision.get("record_key", "")
            action = decision.get("decision", "")
            candidate = by_id.get(decision.get("candidate_id", ""))
            source_bound = (
                action in {"apply_metadata", "apply_edited_metadata", "reject_noise"}
                and candidate is not None
                and candidate.get("record_key") == record_key
                and decision.get("source_path") == candidate.get("source_path")
                and decision.get("source_sha256") == candidate.get("source_sha256")
                and decision.get("sheet_name") == candidate.get("sheet_name")
                and decision.get("row_number") == candidate.get("row_number")
            )
            object_level = action in {"dismiss_object", "defer"} and not decision.get("candidate_id", "")
            proposed_unchanged = candidate is not None and all(
                decision.get(field, "") == candidate.get("suggested_" + field, "")
                for field in ("title", "description", "date_text", "object_type", "material", "dimensions")
            )
            if (
                record_key not in groups or record_key in decisions
                or not (source_bound or object_level)
                or action == "apply_metadata" and not proposed_unchanged
                or action in {"apply_metadata", "apply_edited_metadata"} and not decision.get("title", "")
                or decision.get("deletion_authorized") != "false"
            ):
                raise WorkbenchError(f"invalid local-object metadata review decision: {record_key}")
            decisions[record_key] = decision
        self._local_metadata_review_decisions = decisions

    def local_metadata_review_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record_key, candidates in self._local_metadata_review_groups.items():
            decision = self._local_metadata_review_decisions.get(record_key, {})
            first = candidates[0]
            states = {candidate["identity_review_state"] for candidate in candidates}
            state_rank = {
                "book_inventory_and_visual_match": 0,
                "book_visual_match_related_object": 1,
                "exact_local_inventory_row": 0,
                "existing_md_row_inventory_requires_review": 2,
                "alternate_row_inventory_requires_review": 3,
                "row_inventory_missing_or_ambiguous": 4,
                "comparison_only_multi_inventory_row": 5,
            }
            proposed = min(candidates, key=lambda candidate: (
                state_rank[candidate["identity_review_state"]],
                -sum(bool(candidate.get("suggested_" + field, "")) for field in (
                    "title", "description", "date_text", "object_type", "material", "dimensions"
                )),
                candidate["source_path"], candidate["sheet_name"], int(candidate["row_number"]),
            ))
            selected = self._local_metadata_review_selected[record_key]
            records.append({
                "record_key": record_key,
                "inventory": first["inventory"],
                "selected_master_path": first["selected_master_path"],
                "candidates": candidates,
                "proposed_candidate": proposed,
                "preview_candidate_id": selected["candidate_id"],
                "candidate_count": len(candidates),
                "confirmable_count": sum(
                    candidate["identity_review_state"] in {
                        "exact_local_inventory_row",
                        "book_inventory_and_visual_match",
                    }
                    for candidate in candidates
                ),
                "has_identity_conflict": any(
                    state not in {
                        "exact_local_inventory_row",
                        "book_inventory_and_visual_match",
                    }
                    for state in states
                ),
                "review_state": decision.get("decision", "unreviewed"),
                "review_note": decision.get("note", ""),
                "decided_by": decision.get("decided_by", ""),
                "decided_at": decision.get("decided_at", ""),
                "decided_candidate_id": decision.get("candidate_id", ""),
                "decision_record": decision,
            })
        return sorted(records, key=lambda row: row["inventory"])

    def local_metadata_review_record(self, record_key: str) -> dict[str, Any]:
        if record_key not in self._local_metadata_review_groups:
            raise WorkbenchError("object is outside the local metadata review queue")
        return next(
            row for row in self.local_metadata_review_records()
            if row["record_key"] == record_key
        )

    def guarded_local_metadata_source(self, candidate_id: str) -> Path:
        candidate = self._local_metadata_review_by_id.get(candidate_id)
        if candidate is None:
            raise WorkbenchError("row is outside the local metadata review queue")
        path = Path(candidate["source_path"]).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("metadata source escaped the protected source root") from error
        stat = path.stat()
        if stat.st_size != int(candidate["source_size_bytes"]):
            raise WorkbenchError("metadata source size changed after preparation")
        if sha256_file(path).removeprefix("sha256:") != candidate["source_sha256"]:
            raise WorkbenchError("metadata source content changed after preparation")
        return path

    def guarded_local_metadata_book_image(self, candidate_id: str) -> Path:
        candidate = self._local_metadata_review_by_id.get(candidate_id)
        if candidate is None or not candidate.get("book_image_path"):
            raise WorkbenchError("book image is outside the local metadata review queue")
        path = Path(candidate["book_image_path"]).resolve(strict=True)
        try:
            path.relative_to(BOOK_PROJECT_IMAGES.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("book image escaped the protected Buchprojekt image root") from error
        stat = path.stat()
        if (
            stat.st_size != int(candidate["book_image_size_bytes"])
            or stat.st_mtime_ns != int(candidate["book_image_mtime_ns"])
        ):
            raise WorkbenchError("book image size or mtime changed after preparation")
        return path

    def save_local_metadata_review_decision(
        self, record_key: str, action: str, candidate_id: str,
        fields: dict[str, str], note: str,
    ) -> None:
        if action not in {
            "apply_metadata", "apply_edited_metadata", "reject_noise",
            "dismiss_object", "defer",
        }:
            raise WorkbenchError("unsupported local-object metadata review decision")
        if record_key not in self._local_metadata_review_groups:
            raise WorkbenchError("object is outside the local metadata review queue")
        if len(note) > 2000 or any(len(fields.get(key, "")) > 4000 for key in (
            "title", "description", "date_text", "object_type", "material", "dimensions"
        )):
            raise WorkbenchError("local metadata decision text is too large")
        candidate: dict[str, str] | None = None
        if action in {"apply_metadata", "apply_edited_metadata", "reject_noise"}:
            candidate = self._local_metadata_review_by_id.get(candidate_id)
            if candidate is None or candidate.get("record_key") != record_key:
                raise WorkbenchError("metadata decision requires this object's proposed source row")
            self.guarded_local_metadata_source(candidate_id)
        if action == "apply_metadata" and candidate is not None:
            fields = {
                field: candidate.get("suggested_" + field, "")
                for field in ("title", "description", "date_text", "object_type", "material", "dimensions")
            }
        if action in {"apply_metadata", "apply_edited_metadata"} and not fields.get("title", "").strip():
            raise WorkbenchError("applying metadata requires a title")
        preserve_metadata = action in {"apply_metadata", "apply_edited_metadata"}
        decision = {
            "record_key": record_key,
            "decision": action,
            "candidate_id": candidate_id if candidate else "",
            "title": fields.get("title", "").strip() if preserve_metadata else "",
            "description": fields.get("description", "").strip() if preserve_metadata else "",
            "date_text": fields.get("date_text", "").strip() if preserve_metadata else "",
            "object_type": fields.get("object_type", "").strip() if preserve_metadata else "",
            "material": fields.get("material", "").strip() if preserve_metadata else "",
            "dimensions": fields.get("dimensions", "").strip() if preserve_metadata else "",
            "note": note.strip(),
            "decided_by": self.actor,
            "decided_at": utc_now(),
            "source_path": candidate["source_path"] if candidate else "",
            "source_sha256": candidate["source_sha256"] if candidate else "",
            "sheet_name": candidate["sheet_name"] if candidate else "",
            "row_number": candidate["row_number"] if candidate else "",
            "deletion_authorized": "false",
        }
        self._local_metadata_review_decisions[record_key] = decision
        atomic_csv(
            LOCAL_METADATA_REVIEW_DECISIONS_CSV,
            LOCAL_METADATA_REVIEW_DECISION_FIELDS,
            (
                self._local_metadata_review_decisions[key]
                for key in sorted(self._local_metadata_review_decisions)
            ),
        )

    def _buchprojekt_unbound_initialize(self) -> None:
        receipt = json.loads(BUCHPROJEKT_UNBOUND_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("format") != "iss-tfa-buchprojekt-unbound-object-review-v1"
            or receipt.get("result") != "ok"
            or receipt.get("source_read_only") is not True
            or receipt.get("source_modified") is not False
            or receipt.get("published_tfa_unit_changed") is not False
            or receipt.get("automatic_master_selection") is not False
            or receipt.get("automatic_metadata_application") is not False
            or receipt.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("unbound Buchprojekt review receipt is not safe")
        candidates = read_csv(BUCHPROJEKT_UNBOUND_CANDIDATES_CSV)
        if len(candidates) != int(receipt.get("candidate_row_count", -1)):
            raise WorkbenchError("unbound Buchprojekt candidate row count drifted")
        by_id: dict[str, dict[str, str]] = {}
        groups: dict[str, list[dict[str, str]]] = {}
        for candidate in candidates:
            candidate_id = candidate.get("candidate_id", "")
            record_key = candidate.get("record_key", "")
            if (
                not candidate_id or candidate_id in by_id or not record_key.startswith("local:TFA-")
                or candidate.get("scope_state") not in {
                    "no_inventory", "inventory_not_catalogued", "local_object_unresolved",
                }
                or candidate.get("candidate_role") not in {
                    "plausible_master", "existing_derivative_or_copy",
                }
                or candidate.get("selectable_master") not in {"true", "false"}
                or candidate.get("selectable_master") == "true"
                   and candidate.get("candidate_role") != "plausible_master"
                or candidate.get("deletion_authorized") != "false"
                or not candidate.get("source_path")
                or not candidate.get("suggested_keywords")
                or not candidate.get("suggested_classification")
            ):
                raise WorkbenchError(f"invalid unbound Buchprojekt candidate: {candidate_id}")
            by_id[candidate_id] = candidate
            groups.setdefault(record_key, []).append(candidate)
        if len(groups) != int(receipt.get("object_count", -1)):
            raise WorkbenchError("unbound Buchprojekt object count drifted")
        for record_key, rows in groups.items():
            stable = {
                (row["inventory"], row["visual_group_id"], row["book_image_key"],
                 row["workbook_path"], row["workbook_sha256"], row["sheet_name"], row["row_number"])
                for row in rows
            }
            if len(stable) != 1 or sum(row["is_book_image"] == "true" for row in rows) != 1:
                raise WorkbenchError(f"unbound Buchprojekt group provenance drifted: {record_key}")
            if not any(row["selectable_master"] == "true" for row in rows):
                raise WorkbenchError(f"unbound Buchprojekt group has no TIFF Master candidate: {record_key}")
        self._buchprojekt_unbound_receipt = receipt
        self._buchprojekt_unbound_by_id = by_id
        self._buchprojekt_unbound_groups = {
            key: tuple(sorted(rows, key=lambda row: (
                int(row["recommendation_rank"]),
                -int(row["width_px"] or 0) * int(row["height_px"] or 0),
                -int(row["size_bytes"]), row["source_path"],
            )))
            for key, rows in groups.items()
        }
        decisions: dict[str, dict[str, str]] = {}
        for decision in read_csv(BUCHPROJEKT_UNBOUND_DECISIONS_CSV):
            record_key = decision.get("record_key", "")
            action = decision.get("decision", "")
            candidate = by_id.get(decision.get("selected_candidate_id", ""))
            applied = action in {"apply_metadata", "apply_edited_metadata"}
            group = self._buchprojekt_unbound_groups.get(record_key, ())
            expected_redundant = sorted(
                row["source_path"] for row in group
                if candidate is not None and row["candidate_id"] != candidate["candidate_id"]
            )
            try:
                redundant = json.loads(decision.get("redundant_candidate_paths_json", "[]"))
            except json.JSONDecodeError as error:
                raise WorkbenchError(f"invalid redundant path JSON: {record_key}") from error
            proposed_unchanged = candidate is not None and all(
                decision.get(field, "") == candidate.get("suggested_" + field, "")
                for field in (
                    "title", "description", "date_text", "object_type", "material", "dimensions",
                    "keywords", "classification",
                )
            )
            if (
                record_key not in self._buchprojekt_unbound_groups or record_key in decisions
                or action not in {
                    "apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer",
                }
                or decision.get("deletion_authorized") != "false"
                or applied and (
                    candidate is None or candidate["record_key"] != record_key
                    or candidate["selectable_master"] != "true"
                    or decision.get("selected_master_path") != candidate["source_path"]
                    or sorted(redundant) != expected_redundant
                    or not decision.get("title", "")
                )
                or action == "apply_metadata" and not proposed_unchanged
                or not applied and (
                    decision.get("selected_candidate_id", "")
                    or decision.get("selected_master_path", "")
                    or any(decision.get(field, "") for field in (
                        "title", "description", "date_text", "object_type", "material", "dimensions",
                        "keywords", "classification",
                    ))
                )
            ):
                raise WorkbenchError(f"invalid unbound Buchprojekt decision: {record_key}")
            decisions[record_key] = decision
        self._buchprojekt_unbound_decisions = decisions

    def buchprojekt_unbound_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record_key, candidates in self._buchprojekt_unbound_groups.items():
            decision = self._buchprojekt_unbound_decisions.get(record_key, {})
            book = next(row for row in candidates if row["is_book_image"] == "true")
            records.append({
                "record_key": record_key, "inventory": book["inventory"],
                "scope_state": book["scope_state"], "book_candidate": book,
                "candidates": candidates, "candidate_count": len(candidates),
                "selectable_count": sum(row["selectable_master"] == "true" for row in candidates),
                "review_state": decision.get("decision", "unreviewed"),
                "decision_record": decision,
            })
        return sorted(records, key=lambda row: row["book_candidate"]["book_image_key"])

    def buchprojekt_unbound_record(self, record_key: str) -> dict[str, Any]:
        if record_key not in self._buchprojekt_unbound_groups:
            raise WorkbenchError("object is outside the unbound Buchprojekt queue")
        return next(row for row in self.buchprojekt_unbound_records() if row["record_key"] == record_key)

    def guarded_buchprojekt_candidate(self, candidate_id: str) -> Path:
        candidate = self._buchprojekt_unbound_by_id.get(candidate_id)
        if candidate is None:
            raise WorkbenchError("image is outside the unbound Buchprojekt queue")
        path = Path(candidate["source_path"]).resolve(strict=True)
        try:
            path.relative_to((SOURCE_ROOT / "restore/archive").resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("unbound Buchprojekt image escaped the protected archive root") from error
        stat = path.stat()
        if stat.st_size != int(candidate["size_bytes"]) or stat.st_mtime_ns != int(candidate["mtime_ns"]):
            raise WorkbenchError("unbound Buchprojekt image size or mtime changed after preparation")
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise WorkbenchError("unbound Buchprojekt candidate is not supported image evidence")
        return path

    def save_buchprojekt_unbound_decision(
        self, record_key: str, action: str, selected_candidate_id: str,
        fields: dict[str, str], note: str,
    ) -> None:
        if action not in {
            "apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer",
        }:
            raise WorkbenchError("unsupported unbound Buchprojekt decision")
        group = self._buchprojekt_unbound_groups.get(record_key)
        if group is None:
            raise WorkbenchError("object is outside the unbound Buchprojekt queue")
        if len(note) > 2000 or any(len(fields.get(key, "")) > 4000 for key in (
            "title", "description", "date_text", "object_type", "material", "dimensions",
            "keywords", "classification",
        )):
            raise WorkbenchError("unbound Buchprojekt decision text is too large")
        applied = action in {"apply_metadata", "apply_edited_metadata"}
        candidate = self._buchprojekt_unbound_by_id.get(selected_candidate_id) if applied else None
        if applied and (
            candidate is None or candidate["record_key"] != record_key
            or candidate["selectable_master"] != "true"
        ):
            raise WorkbenchError("applying metadata requires one TIFF selected as the single Master")
        book = next(row for row in group if row["is_book_image"] == "true")
        self.guarded_buchprojekt_candidate(book["candidate_id"])
        if candidate is not None:
            self.guarded_buchprojekt_candidate(candidate["candidate_id"])
        if action == "apply_metadata":
            fields = {
                field: book.get("suggested_" + field, "")
                for field in (
                    "title", "description", "date_text", "object_type", "material", "dimensions",
                    "keywords", "classification",
                )
            }
        if applied and not fields.get("title", "").strip():
            raise WorkbenchError("applying Buchprojekt metadata requires a title")
        redundant = sorted(
            row["source_path"] for row in group
            if candidate is not None and row["candidate_id"] != candidate["candidate_id"]
        )
        decision = {
            "record_key": record_key, "decision": action,
            "selected_candidate_id": candidate["candidate_id"] if candidate else "",
            "selected_master_path": candidate["source_path"] if candidate else "",
            "redundant_candidate_paths_json": json.dumps(redundant, ensure_ascii=False),
            "title": fields.get("title", "").strip() if applied else "",
            "description": fields.get("description", "").strip() if applied else "",
            "date_text": fields.get("date_text", "").strip() if applied else "",
            "object_type": fields.get("object_type", "").strip() if applied else "",
            "material": fields.get("material", "").strip() if applied else "",
            "dimensions": fields.get("dimensions", "").strip() if applied else "",
            "keywords": fields.get("keywords", "").strip() if applied else "",
            "classification": fields.get("classification", "").strip() if applied else "",
            "note": note.strip(), "decided_by": self.actor, "decided_at": utc_now(),
            "book_image_key": book["book_image_key"], "workbook_path": book["workbook_path"],
            "workbook_sha256": book["workbook_sha256"], "sheet_name": book["sheet_name"],
            "row_number": book["row_number"], "visual_group_id": book["visual_group_id"],
            "deletion_authorized": "false",
        }
        self._buchprojekt_unbound_decisions[record_key] = decision
        atomic_csv(
            BUCHPROJEKT_UNBOUND_DECISIONS_CSV, BUCHPROJEKT_UNBOUND_DECISION_FIELDS,
            (self._buchprojekt_unbound_decisions[key] for key in sorted(self._buchprojekt_unbound_decisions)),
        )

    def _all_local_metadata_initialize(self) -> None:
        receipt = json.loads(ALL_LOCAL_METADATA_RECEIPT.read_text(encoding="utf-8"))
        if (
            receipt.get("format") != "iss-tfa-all-local-metadata-review-v1"
            or receipt.get("result") != "ok"
            or receipt.get("source_read_only") is not True
            or receipt.get("source_modified") is not False
            or receipt.get("published_tfa_unit_changed") is not False
            or receipt.get("selected_master_decisions_changed") is not False
            or receipt.get("automatic_metadata_application") is not False
            or receipt.get("review_projection_only") is not True
            or receipt.get("deletion_authorized") is not False
            or receipt.get("candidate_sha256")
            != sha256_file(ALL_LOCAL_METADATA_CANDIDATES_CSV).removeprefix("sha256:")
        ):
            raise WorkbenchError("all-local metadata review receipt is not safe")
        candidates = read_csv(ALL_LOCAL_METADATA_CANDIDATES_CSV)
        if len(candidates) != int(receipt.get("object_count", -1)) or len(candidates) != 2483:
            raise WorkbenchError("all-local metadata review object count drifted")
        by_key: dict[str, dict[str, str]] = {}
        for row in candidates:
            record_key = row.get("record_key", "")
            if (
                not record_key or record_key in by_key
                or row.get("deletion_authorized") != "false"
                or row.get("candidate_fingerprint") != all_local_candidate_fingerprint(row)
                or not row.get("selected_master_path")
                or row.get("proposed_object_type") != "Foto"
            ):
                raise WorkbenchError(f"invalid all-local metadata candidate: {record_key}")
            by_key[record_key] = row
        decisions: dict[str, dict[str, str]] = {}
        allowed = {"apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer"}
        for decision in read_csv(ALL_LOCAL_METADATA_DECISIONS_CSV):
            record_key = decision.get("record_key", "")
            candidate = by_key.get(record_key)
            action = decision.get("decision", "")
            applied = action in {"apply_metadata", "apply_edited_metadata"}
            if (
                candidate is None or record_key in decisions or action not in allowed
                or decision.get("deletion_authorized") != "false"
                or decision.get("candidate_fingerprint") != candidate.get("candidate_fingerprint")
                or decision.get("selected_master_path") != candidate.get("selected_master_path")
                or decision.get("selected_master_size_bytes") != candidate.get("selected_master_size_bytes")
                or decision.get("selected_master_mtime_ns") != candidate.get("selected_master_mtime_ns")
                or applied and not decision.get("title", "").strip()
                or not applied and any(decision.get(field, "") for field in ALL_LOCAL_METADATA_FIELDS)
            ):
                raise WorkbenchError(f"invalid all-local metadata decision: {record_key}")
            decisions[record_key] = decision
        self._all_local_metadata_receipt = receipt
        self._all_local_metadata_by_key = by_key
        self._all_local_metadata_decisions = decisions
        tube_enrichment = receipt.get("tube_metadata_enrichment", {})
        tube_keys = tuple(tube_enrichment.get("record_keys", ()))
        if (
            tube_enrichment.get("format") != "iss-tfa-all-local-tube-metadata-enrichment-v1"
            or tube_enrichment.get("proposal_count") != 60
            or len(tube_keys) != 60
            or len(set(tube_keys)) != 60
            or any(key not in by_key for key in tube_keys)
            or any(not by_key[key].get("proposed_title") for key in tube_keys)
            or tube_enrichment.get("human_metadata_decisions_written") != 0
            or tube_enrichment.get("automatic_metadata_application") is not False
            or tube_enrichment.get("selected_master_decisions_changed") is not False
            or tube_enrichment.get("source_modified") is not False
            or tube_enrichment.get("deletion_authorized") is not False
        ):
            raise WorkbenchError("tube metadata proposal scope is unsafe or drifted")
        self._tube_metadata_keys = tuple(
            row["record_key"]
            for row in sorted(
                (by_key[key] for key in tube_keys),
                key=lambda row: (row["inventory"].casefold(), row["record_key"]),
            )
        )

    def all_local_metadata_records(self) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for record_key, candidate in self._all_local_metadata_by_key.items():
            decision = self._all_local_metadata_decisions.get(record_key, {})
            prior = candidate.get("prior_review_state", "")
            if decision:
                state = decision["decision"]
                origin = "all_local_review"
            elif prior in {"apply_metadata", "apply_edited_metadata"}:
                state = "existing_metadata"
                origin = candidate.get("prior_decision_source", "")
            elif prior:
                state = "existing_" + prior
                origin = candidate.get("prior_decision_source", "")
            else:
                state = "unreviewed"
                origin = ""
            records.append({
                **candidate,
                "review_state": state,
                "review_origin": origin,
                "review_note": decision.get("note", ""),
                "decided_by": decision.get("decided_by", candidate.get("prior_decided_by", "")),
                "decided_at": decision.get("decided_at", candidate.get("prior_decided_at", "")),
                "decision_record": decision,
            })
        return sorted(records, key=lambda row: (row["inventory"].casefold(), row["record_key"]))

    def all_local_metadata_record(self, record_key: str) -> dict[str, str]:
        if record_key not in self._all_local_metadata_by_key:
            raise WorkbenchError("object is outside the all-local metadata queue")
        return next(row for row in self.all_local_metadata_records() if row["record_key"] == record_key)

    def metadata_review_records(self) -> list[dict[str, str]]:
        review_keys = set(self._tube_metadata_keys) | set(self._all_local_metadata_decisions)
        return [
            row for row in self.all_local_metadata_records()
            if row["record_key"] in review_keys
        ]

    def metadata_review_record(self, record_key: str) -> dict[str, str]:
        if (
            record_key not in self._tube_metadata_keys
            and record_key not in self._all_local_metadata_decisions
        ):
            raise WorkbenchError("object is outside the identified metadata review bench")
        return self.all_local_metadata_record(record_key)

    def overall_local_metadata_records(self) -> list[dict[str, str]]:
        review_keys = set(self._tube_metadata_keys) | set(self._all_local_metadata_decisions)
        return [
            row for row in self.all_local_metadata_records()
            if (
                row["record_key"] not in review_keys
                and row["review_state"] != "existing_metadata"
            )
        ]

    def overall_local_metadata_record(self, record_key: str) -> dict[str, str]:
        candidate = self._all_local_metadata_by_key.get(record_key, {})
        if (
            record_key in self._tube_metadata_keys
            or record_key in self._all_local_metadata_decisions
        ):
            raise WorkbenchError("object is in the identified metadata review bench, not the overall census")
        if candidate.get("prior_review_state") in {"apply_metadata", "apply_edited_metadata"}:
            raise WorkbenchError("object already has applied metadata and is excluded from the overall census")
        return self.all_local_metadata_record(record_key)

    def tube_metadata_records(self) -> list[dict[str, str]]:
        by_key = {row["record_key"]: row for row in self.all_local_metadata_records()}
        return [by_key[key] for key in self._tube_metadata_keys]

    def tube_metadata_record(self, record_key: str) -> dict[str, str]:
        if record_key not in self._tube_metadata_keys:
            raise WorkbenchError("object is outside the tube metadata proposal bench")
        return self.all_local_metadata_record(record_key)

    def guarded_all_local_master(self, record_key: str) -> Path:
        candidate = self._all_local_metadata_by_key.get(record_key)
        if candidate is None:
            raise WorkbenchError("object is outside the all-local metadata queue")
        path = Path(candidate["selected_master_path"]).resolve(strict=True)
        try:
            path.relative_to((SOURCE_ROOT / "restore/archive").resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("selected local Master escaped the protected archive") from error
        stat = path.stat()
        if (
            stat.st_size != int(candidate["selected_master_size_bytes"])
            or stat.st_mtime_ns != int(candidate["selected_master_mtime_ns"])
        ):
            raise WorkbenchError("selected local Master size or mtime changed after preparation")
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise WorkbenchError("selected local Master is not supported image evidence")
        return path

    def guarded_all_local_thumbnail(self, record_key: str) -> Path:
        candidate = self._all_local_metadata_by_key.get(record_key)
        if candidate is None:
            raise WorkbenchError("object is outside the all-local metadata queue")
        thumbnail = (
            ALL_LOCAL_METADATA_THUMBNAIL_ROOT
            / f"{candidate['candidate_fingerprint']}.jpg"
        ).resolve(strict=True)
        try:
            thumbnail.relative_to(ALL_LOCAL_METADATA_THUMBNAIL_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("local thumbnail escaped its cache root") from error
        if not 0 < thumbnail.stat().st_size <= 2_000_000:
            raise WorkbenchError("local thumbnail is empty or unexpectedly large")
        return thumbnail

    def save_all_local_metadata_decision(
        self, record_key: str, action: str, fields: dict[str, str], note: str,
    ) -> None:
        allowed = {"apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer"}
        candidate = self._all_local_metadata_by_key.get(record_key)
        if candidate is None or action not in allowed:
            raise WorkbenchError("unsupported all-local metadata decision")
        if len(note) > 2000 or any(len(fields.get(key, "")) > 4000 for key in ALL_LOCAL_METADATA_FIELDS):
            raise WorkbenchError("all-local metadata decision text is too large")
        self.guarded_all_local_master(record_key)
        applied = action in {"apply_metadata", "apply_edited_metadata"}
        if action == "apply_metadata":
            fields = {field: candidate.get("proposed_" + field, "") for field in ALL_LOCAL_METADATA_FIELDS}
        if applied and not fields.get("title", "").strip():
            raise WorkbenchError("applying metadata requires a title")
        decision = {
            "record_key": record_key,
            "decision": action,
            **{
                field: fields.get(field, "").strip() if applied else ""
                for field in ALL_LOCAL_METADATA_FIELDS
            },
            "note": note.strip(),
            "decided_by": self.actor,
            "decided_at": utc_now(),
            "selected_master_path": candidate["selected_master_path"],
            "selected_master_size_bytes": candidate["selected_master_size_bytes"],
            "selected_master_mtime_ns": candidate["selected_master_mtime_ns"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "deletion_authorized": "false",
        }
        self._all_local_metadata_decisions[record_key] = decision
        atomic_csv(
            ALL_LOCAL_METADATA_DECISIONS_CSV,
            ALL_LOCAL_METADATA_DECISION_FIELDS,
            (self._all_local_metadata_decisions[key] for key in sorted(self._all_local_metadata_decisions)),
        )

    def validate(self) -> dict[str, Any]:
        source_mount()
        with closing(self.connect(read_only=True)) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            decisions = connection.execute("SELECT COUNT(*) FROM reconciliation_decisions").fetchone()[0]
            selected = connection.execute(
                "SELECT COUNT(*) FROM reconciliation_decisions WHERE decision_state='selected_master'"
            ).fetchone()[0]
            recovery_placeholders = ",".join("?" for _ in self._recovery_queue)
            unresolved_outside_recovery = connection.execute(
                f"SELECT COUNT(*) FROM reconciliation_decisions WHERE decision_state!='selected_master' AND record_key NOT IN ({recovery_placeholders})",
                tuple(self._recovery_queue),
            ).fetchone()[0]
            bad = connection.execute(
                """
                SELECT COUNT(*) FROM reconciliation_decisions d
                LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.decision_state='selected_master'
                  AND (c.candidate_id IS NULL OR c.record_key!=d.record_key
                       OR NOT (
                            lower(c.extension) IN ('.tif','.tiff')
                            OR (c.inventory IN ('TFA-6323033','TFA-6423379')
                                AND lower(c.extension) IN ('.jpg','.jpeg'))
                       ))
                """
            ).fetchone()[0]
            local_objects = connection.execute("SELECT COUNT(*) FROM local_object_metadata").fetchone()[0]
            local_candidates = connection.execute("SELECT COUNT(*) FROM local_candidate_technical").fetchone()[0]
            expected_local_candidates = expected_local_candidate_count(connection)
            local_decisions = connection.execute("SELECT COUNT(*) FROM local_object_decisions").fetchone()[0]
            local_bad = connection.execute(
                """
                SELECT COUNT(*) FROM local_object_decisions d
                LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                LEFT JOIN local_candidate_technical t ON t.candidate_id=d.selected_candidate_id
                WHERE (d.outcome='new_local_object' AND
                         (c.record_key IS NULL OR c.record_key!=d.record_key OR t.accession_eligible!=1))
                   OR (d.outcome='duplicate_md' AND NOT EXISTS
                         (SELECT 1 FROM local_md_match_candidates m
                          WHERE m.record_key=d.record_key AND m.md_object_id=d.matched_md_object_id))
                   OR (d.outcome='unresolved' AND
                         (d.selected_candidate_id IS NOT NULL OR d.matched_md_object_id IS NOT NULL))
                """
            ).fetchone()[0]
            drain_rows = connection.execute("SELECT COUNT(*) FROM drain_list_proposal").fetchone()[0]
            selected_drain_intersection = connection.execute(
                """SELECT COUNT(*) FROM drain_list_proposal x JOIN (
                     SELECT c.source_path FROM reconciliation_decisions d JOIN candidates c
                       ON c.candidate_id=d.selected_candidate_id
                     WHERE d.decision_state='selected_master'
                     UNION
                     SELECT c.source_path FROM local_object_decisions d JOIN candidates c
                       ON c.candidate_id=d.selected_candidate_id
                     WHERE d.outcome='new_local_object'
                   ) selected ON selected.source_path=x.source_path"""
            ).fetchone()[0]
            bad_drain = connection.execute(
                """
                SELECT COUNT(*) FROM drain_list_proposal
                WHERE preservation_status!='redundant'
                   OR preservation_basis NOT IN (
                        'visual_match_to_selected_md_tiff_master',
                        'visual_match_to_current_md_record',
                        'visual_redundant_after_single_master_selection',
                        'redundant_after_human_visual_group_master_selection',
                        'invalid_tiff_replaced_by_valid_inventory_match',
                        'redundant_to_user_selected_hires_jpeg_surrogate',
                        'redundant_visual_equivalent_after_single_master_selection',
                        'undecodable_same_filename_source_preserved_as_redundant_evidence'
                   )
                   OR drain_state!='proposed_redundant'
                   OR deletion_authorized!='false'
                """
            ).fetchone()[0]
            md_authority = connection.execute(
                "SELECT value FROM meta WHERE key='md_metadata_authority'"
            ).fetchone()
        recovery_drain_additions = 0
        if RECOVERY_107_APPLIED.is_file():
            applied = json.loads(RECOVERY_107_APPLIED.read_text(encoding="utf-8"))
            if applied.get("result") != "ok" or applied.get("deletion_authorized") is not False:
                raise WorkbenchError("107-object recovery application receipt is not safe")
            recovery_drain_additions = int(applied.get("drain_rows_added", -1))
        jpeg_surrogate_drain_delta = 0
        if JPEG_SURROGATE_TWO_APPLIED.is_file():
            applied = json.loads(JPEG_SURROGATE_TWO_APPLIED.read_text(encoding="utf-8"))
            if (
                applied.get("result") != "ok"
                or applied.get("deletion_authorized") is not False
                or set(applied.get("inventories", [])) != JPEG_SURROGATE_INVENTORIES
            ):
                raise WorkbenchError("two-object JPEG surrogate application receipt is not safe")
            jpeg_surrogate_drain_delta = (
                int(applied.get("drain_rows_added", -1))
                - int(applied.get("drain_rows_removed", -1))
            )
        if self.selected_master_drain_repair_receipt is not None:
            expected_drain = int(
                self.selected_master_drain_repair_receipt.get("catalogue_drain_rows", -1)
            )
        elif self.reference_photo_hold_recovery_receipt is not None:
            expected_drain = int(
                self.reference_photo_hold_recovery_receipt.get("catalogue_drain_rows", -1)
            )
        elif self.reference_photo_receipt is not None:
            expected_drain = int(self.reference_photo_receipt.get("catalogue_drain_rows", -1))
        else:
            expected_drain = (
                int(self.visual_receipt.get("redundant_image_paths", -1))
                + recovery_drain_additions
                + jpeg_surrogate_drain_delta
            )
        if DRAIN_CORRECTIONS_RECEIPT.is_file():
            correction = json.loads(DRAIN_CORRECTIONS_RECEIPT.read_text(encoding="utf-8"))
            if (correction.get("result") != "ok"
                    or correction.get("source_modified") is not False
                    or correction.get("deletion_authorized") is not False):
                raise WorkbenchError("drain correction receipt is not safe")
            excluded_paths = set(correction["overlaps"]) | set(correction["pending"])
            with closing(self.connect(read_only=True)) as connection:
                registered_paths = {r[0] for r in connection.execute("SELECT source_path FROM drain_list_proposal")}
            if excluded_paths & registered_paths:
                raise WorkbenchError("excluded or held source has re-entered the drain proposal")
            expected_drain = int(correction["work_drain_rows"])
        if quick != "ok" or foreign or bad or decisions != 5282 or unresolved_outside_recovery or local_objects != 436 or local_candidates != expected_local_candidates or local_bad or bad_drain or selected_drain_intersection or drain_rows != expected_drain or md_authority is None or md_authority[0] != "museum-digital":
            raise WorkbenchError(
                f"validation failed: quick={quick}, fk={len(foreign)}, bad={bad}, selected={selected}, unresolved_outside_recovery={unresolved_outside_recovery}, "
                f"local_bad={local_bad}, drain={drain_rows}/{expected_drain}, bad_drain={bad_drain}, selected_drain={selected_drain_intersection}"
            )
        csv_decisions = read_csv(DECISIONS_CSV)
        if len(csv_decisions) != decisions:
            raise WorkbenchError("SQLite and workbench-decisions.csv disagree")
        csv_local_decisions = read_csv(LOCAL_DECISIONS_CSV)
        if len(csv_local_decisions) != local_decisions:
            raise WorkbenchError("SQLite and local-object-decisions.csv disagree")
        document_records = self.document_records()
        document_decisions = read_csv(DOCUMENT_DECISIONS_CSV)
        expected_document_scope = int(json.loads(DOCUMENT_PREPARATION_SUMMARY.read_text(encoding="utf-8")).get("document_review_scope", -1))
        if (
            len(document_records) != expected_document_scope
            or len(document_decisions) != len(self._document_decisions) + len(self._document_preserved_decisions)
        ):
            raise WorkbenchError("TFA document review CSV disagrees with the guarded queue")
        metadata_records = self.metadata_records()
        metadata_decisions = read_csv(METADATA_DECISIONS_CSV)
        expected_metadata_scope = int(json.loads(METADATA_PREPARATION_SUMMARY.read_text(encoding="utf-8")).get("metadata_review_scope", -1))
        if len(metadata_records) != expected_metadata_scope or len(metadata_decisions) != len(self._metadata_decisions):
            raise WorkbenchError("TFA metadata review CSV disagrees with the guarded queue")
        local_metadata_records = self.local_metadata_review_records()
        local_metadata_candidates = read_csv(LOCAL_METADATA_REVIEW_CANDIDATES_CSV)
        local_metadata_decisions = read_csv(LOCAL_METADATA_REVIEW_DECISIONS_CSV)
        if (
            len(local_metadata_records) != int(
                self._local_metadata_review_receipt.get("structured_candidate_target_count", -1)
            )
            or len(local_metadata_candidates) != int(
                self._local_metadata_review_receipt.get("structured_candidate_rows", -1)
            )
            or len(local_metadata_decisions) != len(self._local_metadata_review_decisions)
            or any(row.get("deletion_authorized") != "false" for row in local_metadata_candidates)
            or any(row.get("deletion_authorized") != "false" for row in local_metadata_decisions)
        ):
            raise WorkbenchError("local-object metadata review projection drifted")
        buchprojekt_records = self.buchprojekt_unbound_records()
        buchprojekt_candidates = read_csv(BUCHPROJEKT_UNBOUND_CANDIDATES_CSV)
        buchprojekt_decisions = read_csv(BUCHPROJEKT_UNBOUND_DECISIONS_CSV)
        if (
            len(buchprojekt_records) != int(self._buchprojekt_unbound_receipt.get("object_count", -1))
            or len(buchprojekt_candidates) != int(
                self._buchprojekt_unbound_receipt.get("candidate_row_count", -1)
            )
            or len(buchprojekt_decisions) != len(self._buchprojekt_unbound_decisions)
            or any(row.get("deletion_authorized") != "false" for row in buchprojekt_candidates)
            or any(row.get("deletion_authorized") != "false" for row in buchprojekt_decisions)
        ):
            raise WorkbenchError("unbound Buchprojekt review projection drifted")
        all_local_metadata_records = self.all_local_metadata_records()
        metadata_review_records = self.metadata_review_records()
        overall_local_metadata_records = self.overall_local_metadata_records()
        metadata_review_keys = set(self._tube_metadata_keys) | set(self._all_local_metadata_decisions)
        existing_metadata_records = [
            row for row in all_local_metadata_records
            if (
                row["review_state"] == "existing_metadata"
                and row["record_key"] not in metadata_review_keys
            )
        ]
        all_local_metadata_candidates = read_csv(ALL_LOCAL_METADATA_CANDIDATES_CSV)
        all_local_metadata_decisions = read_csv(ALL_LOCAL_METADATA_DECISIONS_CSV)
        if (
            len(all_local_metadata_records) != int(self._all_local_metadata_receipt.get("object_count", -1))
            or len(all_local_metadata_records) != 2483
            or len(all_local_metadata_candidates) != len(all_local_metadata_records)
            or len(all_local_metadata_decisions) != len(self._all_local_metadata_decisions)
            or {row["record_key"] for row in metadata_review_records} != metadata_review_keys
            or len(all_local_metadata_records) != (
                len(metadata_review_records)
                + len(overall_local_metadata_records)
                + len(existing_metadata_records)
            )
            or set(row["record_key"] for row in metadata_review_records) & set(
                row["record_key"] for row in overall_local_metadata_records
            )
            or set(row["record_key"] for row in existing_metadata_records) & (
                set(row["record_key"] for row in metadata_review_records)
                | set(row["record_key"] for row in overall_local_metadata_records)
            )
            or any(
                row.get("deletion_authorized") != "false"
                for row in all_local_metadata_candidates + all_local_metadata_decisions
            )
        ):
            raise WorkbenchError("all-local metadata review projection drifted")
        tube_metadata_records = self.tube_metadata_records()
        if (
            len(tube_metadata_records) != 60
            or {row["record_key"] for row in tube_metadata_records} != set(self._tube_metadata_keys)
            or any(not row.get("proposed_title") for row in tube_metadata_records)
        ):
            raise WorkbenchError("tube metadata proposal bench drifted")
        orphan_records = self.orphan_records()
        orphan_decisions = read_csv(ORPHAN_DECISIONS_CSV)
        orphan_dispositions = read_csv(ORPHAN_DISPOSITIONS_CSV)
        orphan_path_count = sum(len(rows) for rows in self._orphan_groups.values())
        orphan_visual_groups = len({
            row["visual_group_id"]
            for rows in self._orphan_groups.values()
            for row in rows
        })
        if self.reference_photo_hold_workbench_receipt is not None:
            expected_orphan_records = int(
                self.reference_photo_hold_workbench_receipt.get("workbench_visual_groups", -1)
            )
            expected_orphan_paths = int(
                self.reference_photo_hold_workbench_receipt.get("workbench_candidate_paths", -1)
            )
            expected_orphan_visual_groups = expected_orphan_records
        elif self.reference_photo_receipt is not None:
            expected_orphan_records = int(self.reference_photo_receipt.get("workbench_visual_groups", -1))
            expected_orphan_paths = int(self.reference_photo_receipt.get("workbench_candidate_paths", -1))
            expected_orphan_visual_groups = int(self.reference_photo_receipt.get("workbench_visual_groups", -1))
        else:
            expected_orphan_records = 201
            expected_orphan_paths = 309
            expected_orphan_visual_groups = 236
        if (
            len(orphan_records) != expected_orphan_records
            or orphan_path_count != expected_orphan_paths
            or orphan_visual_groups != expected_orphan_visual_groups
            or len(orphan_decisions) != len(self._orphan_decisions)
            or len(orphan_dispositions) != orphan_path_count
            or any(row.get("deletion_authorized") != "false" for row in orphan_decisions + orphan_dispositions)
            or any(group_id not in {
                row["visual_group_id"]
                for rows in self._orphan_groups.values()
                for row in rows
            } for group_id in self._reference_photo_hold_contexts)
        ):
            raise WorkbenchError("TFA orphan review scope or human-readable projections drifted")
        disposition_by_record: dict[str, list[dict[str, str]]] = {}
        for row in orphan_dispositions:
            disposition_by_record.setdefault(row["record_key"], []).append(row)
        for record in orphan_records:
            rows = disposition_by_record.get(record["record_key"], [])
            statuses = [row["preservation_status"] for row in rows]
            if len(rows) != int(record["path_count"]):
                raise WorkbenchError(f"orphan disposition paths disagree: {record['record_key']}")
            if record["state"] == "selected_master" and (
                statuses.count("selected_master") != 1
                or statuses.count("redundant") != len(statuses) - 1
            ):
                raise WorkbenchError(f"one-Master orphan invariant failed: {record['record_key']}")
            if record["state"] == "redundant" and statuses.count("redundant") != len(statuses):
                raise WorkbenchError(f"orphan redundant disposition failed: {record['record_key']}")
            if record["state"] == "noise" and statuses.count("noise") != len(statuses):
                raise WorkbenchError(f"orphan noise disposition failed: {record['record_key']}")
            if record["state"] == "genuine_loss" and statuses.count("genuine_loss") != len(statuses):
                raise WorkbenchError(f"orphan genuine-loss disposition failed: {record['record_key']}")
            if record["state"] in {"unreviewed", "unresolved"} and statuses.count("unresolved") != len(statuses):
                raise WorkbenchError(f"orphan unresolved disposition failed: {record['record_key']}")
        orphan_selected = sum(row["state"] == "selected_master" for row in orphan_records)
        orphan_redundant = sum(row["state"] == "redundant" for row in orphan_records)
        orphan_noise = sum(row["state"] == "noise" for row in orphan_records)
        orphan_genuine_loss = sum(row["state"] == "genuine_loss" for row in orphan_records)
        return {
            "result": "ok",
            "quick_check": quick,
            "foreign_key_violations": len(foreign),
            "decisions": decisions,
            "selected_masters": selected,
            "exception_scope": len(self.exceptions),
            "local_object_scope": local_objects,
            "local_image_candidates": local_candidates,
            "local_decisions": local_decisions,
            "local_bad_decisions": local_bad,
            "local_queue_mode": (
                "reference_photo_remaining_hold_review"
                if self.reference_photo_hold_workbench_receipt is not None
                else "reference_photo_visual_object_review"
                if self.reference_photo_receipt is not None
                else "orphan_group_review"
            ),
            "orphan_object_candidates": len(orphan_records),
            "orphan_tiff_paths": orphan_path_count,
            "orphan_visual_groups": orphan_visual_groups,
            "orphan_decisions": len(orphan_decisions),
            "orphan_selected_masters": orphan_selected,
            "orphan_redundant": orphan_redundant,
            "orphan_noise": orphan_noise,
            "orphan_genuine_loss": orphan_genuine_loss,
            "orphan_open": (
                len(orphan_records) - orphan_selected - orphan_redundant
                - orphan_noise - orphan_genuine_loss
            ),
            "hold_review_comparison_contexts": sum(
                len(rows) for rows in self._reference_photo_hold_contexts.values()
            ),
            "hold_review_unviewable": sum(
                int(record["decodable_path_count"]) == 0 for record in orphan_records
            ),
            "visual_new_tiff_paths": len(self._visual_new_rows),
            "visual_new_tiff_groups": len(self._visual_groups),
            "visual_md_redundant_paths": len(self._visual_redundant_rows),
            "visual_error_holds": len(self._visual_error_rows),
            "institution_visual_master_bindings": len(self._visual_institution_bindings),
            "institution_visual_master_holds": len(self._visual_institution_holds),
            "crop_verified_redundant_paths": int(self.visual_receipt.get("crop_verified_redundant_paths", 0)),
            "drain_rows": drain_rows,
            "md_metadata_authority": md_authority[0],
            "md_metadata_reconciled_objects": len(self._md_accession),
            "md_ready_for_accession": int(self.md_accession_receipt["ready_for_accession"]),
            "md_accession_holds": int(self.md_accession_receipt["accession_holds"]),
            "recovery_queue_objects": len(self._recovery_queue),
            "recovery_validated_candidates": len(self._recovery_candidate_ids),
            "recovery_choices_recorded": sum(
                row["state"] == "replacement_selected" for row in self.recovery_records()
            ),
            "document_review_scope": len(document_records),
            "document_review_decisions": len(self._document_decisions),
            "document_review_preserved_decisions": len(self._document_preserved_decisions),
            "document_review_open": sum(row["review_state"] == "unreviewed" for row in document_records),
            "metadata_review_scope": len(metadata_records),
            "metadata_review_decisions": len(metadata_decisions),
            "metadata_review_open": sum(row["review_state"] == "unreviewed" for row in metadata_records),
            "local_metadata_review_objects": len(local_metadata_records),
            "local_metadata_review_candidate_rows": len(local_metadata_candidates),
            "local_metadata_review_confirmable_rows": sum(
                row.get("identity_review_state") in {
                    "exact_local_inventory_row",
                    "book_inventory_and_visual_match",
                }
                for row in local_metadata_candidates
            ),
            "local_metadata_review_applied": sum(
                row["review_state"] in {"apply_metadata", "apply_edited_metadata"}
                for row in local_metadata_records
            ),
            "local_metadata_review_rejected_noise": sum(
                row["review_state"] == "reject_noise" for row in local_metadata_records
            ),
            "local_metadata_review_dismissed_objects": sum(
                row["review_state"] == "dismiss_object" for row in local_metadata_records
            ),
            "local_metadata_review_deferred": sum(
                row["review_state"] == "defer" for row in local_metadata_records
            ),
            "local_metadata_review_open": sum(
                row["review_state"] == "unreviewed" for row in local_metadata_records
            ),
            "local_metadata_automatic_application": False,
            "buchprojekt_unbound_objects": len(buchprojekt_records),
            "buchprojekt_unbound_candidate_rows": len(buchprojekt_candidates),
            "buchprojekt_unbound_decisions": len(buchprojekt_decisions),
            "buchprojekt_unbound_applied": sum(
                row["review_state"] in {"apply_metadata", "apply_edited_metadata"}
                for row in buchprojekt_records
            ),
            "buchprojekt_unbound_rejected_noise": sum(
                row["review_state"] == "reject_noise" for row in buchprojekt_records
            ),
            "buchprojekt_unbound_dismissed": sum(
                row["review_state"] == "dismiss_object" for row in buchprojekt_records
            ),
            "buchprojekt_unbound_deferred": sum(
                row["review_state"] == "defer" for row in buchprojekt_records
            ),
            "buchprojekt_unbound_open": sum(
                row["review_state"] == "unreviewed" for row in buchprojekt_records
            ),
            "buchprojekt_unbound_automatic_master_selection": False,
            "buchprojekt_unbound_automatic_metadata_application": False,
            "all_local_metadata_source_objects": len(all_local_metadata_records),
            "all_local_metadata_objects": len(overall_local_metadata_records),
            "all_local_metadata_new_decisions": 0,
            "all_local_metadata_decisions_total": len(all_local_metadata_decisions),
            "all_local_metadata_existing": len(existing_metadata_records),
            "all_local_metadata_applied": sum(
                row["review_state"] in {"apply_metadata", "apply_edited_metadata"}
                for row in overall_local_metadata_records
            ),
            "all_local_metadata_rejected_noise": sum(
                row["review_state"] == "reject_noise" for row in overall_local_metadata_records
            ),
            "all_local_metadata_dismissed": sum(
                row["review_state"] == "dismiss_object" for row in overall_local_metadata_records
            ),
            "all_local_metadata_deferred": sum(
                row["review_state"] == "defer" for row in overall_local_metadata_records
            ),
            "all_local_metadata_open": sum(
                row["review_state"] == "unreviewed" for row in overall_local_metadata_records
            ),
            "all_local_metadata_automatic_application": False,
            "identified_metadata_review_objects": len(metadata_review_records),
            "identified_metadata_review_prior_proposals": len(tube_metadata_records),
            "identified_metadata_review_decisions": sum(
                row["review_state"] in {
                    "apply_metadata", "apply_edited_metadata", "reject_noise",
                    "dismiss_object", "defer",
                }
                for row in metadata_review_records
            ),
            "identified_metadata_review_open": sum(
                row["review_state"] == "unreviewed" for row in metadata_review_records
            ),
            "tube_metadata_objects": len(tube_metadata_records),
            "tube_metadata_open": sum(
                row["review_state"] == "unreviewed" for row in tube_metadata_records
            ),
            "tube_metadata_decisions": sum(
                row["review_state"] in {
                    "apply_metadata", "apply_edited_metadata", "reject_noise",
                    "dismiss_object", "defer",
                }
                for row in tube_metadata_records
            ),
            "tube_metadata_automatic_application": False,
            "source_read_only": True,
            "deletion_authorized": False,
        }


CSS = """
:root{font-family:system-ui,sans-serif;color:#20211f;background:#f2f0e9;line-height:1.45}
*{box-sizing:border-box}body{margin:0}a{color:#174f45}header{background:#183d36;color:white;padding:1rem 2rem;display:flex;justify-content:space-between;align-items:center}header a{color:white}main{max-width:1500px;margin:auto;padding:1.5rem}.panel,.record-card,.candidate{background:white;border:1px solid #d5d0c4;border-radius:.45rem;padding:1rem}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1rem}.metric strong{display:block;font-size:1.55rem}.filters{display:flex;gap:.75rem;flex-wrap:wrap;align-items:end;margin-bottom:1rem}.filters label{display:grid;gap:.2rem}.filters input,.filters select,textarea{font:inherit;padding:.55rem;border:1px solid #999;border-radius:.25rem}.records{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}.record-card{display:grid;grid-template-columns:112px 1fr;gap:.8rem}.record-card img{width:112px;height:112px;object-fit:contain;background:#222}.record-card h2{font-size:1rem;margin:.15rem 0}.badge{display:inline-block;border-radius:1rem;background:#e5e1d7;padding:.15rem .5rem;font-size:.78rem;margin:.1rem}.badge--selected{background:#cce8d3}.badge--warning{background:#f5df9b}.badge--critical{background:#f1b5ad}.technical,code{font-family:ui-monospace,monospace;font-size:.82rem;overflow-wrap:anywhere}.candidate-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:1rem}.candidate{display:grid;gap:.6rem}.candidate img{width:100%;height:280px;object-fit:contain;background:#262626}.candidate h3{margin:0}.choice{display:flex;gap:.55rem;align-items:center;background:#e8f2ec;padding:.65rem;border-radius:.3rem}.notice{padding:.8rem;border-left:5px solid #b68b21;background:#fff7d9;margin:1rem 0}.notice--safe{border-color:#267a52;background:#e4f4e9}.notice--critical{border-color:#a5281d;background:#fce4df}.decision{position:sticky;bottom:0;margin-top:1rem;box-shadow:0 -3px 20px #0002}.decision textarea{width:100%;min-height:4.5rem}.buttons{display:flex;gap:.75rem;flex-wrap:wrap}.button{font:inherit;font-weight:650;border:0;border-radius:.3rem;padding:.7rem 1rem;background:#216b58;color:white;cursor:pointer}.button--quiet{background:#686760}.button--critical{background:#a5281d}.pagination{display:flex;gap:.5rem;justify-content:center;margin:1.2rem}dt{font-weight:700}dd{margin:0 0 .6rem}.description{max-width:90ch}.empty-preview{height:280px;display:grid;place-items:center;background:#dedbd3;color:#555}.path{max-height:6rem;overflow:auto}.nav{display:flex;justify-content:space-between;gap:1rem;margin-bottom:1rem}@media(max-width:650px){header{padding:1rem}main{padding:.75rem}.record-card{grid-template-columns:80px 1fr}.record-card img{width:80px;height:80px}.candidate-grid{grid-template-columns:1fr}}
.record-card .empty-preview{width:112px;height:112px;text-align:center}.button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:650px){.record-card .empty-preview{width:80px;height:80px}}
.metadata-form input{font:inherit;padding:.55rem;border:1px solid #999;border-radius:.25rem}.metadata-form label{display:grid;gap:.25rem;margin:.65rem 0}.metadata-form textarea{width:100%;min-height:6rem}.metadata-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem}details{margin:.65rem 0}details code{display:block;max-height:12rem;overflow:auto;white-space:pre-wrap}details ol{max-height:28rem;overflow:auto}.evidence-table{width:100%;border-collapse:collapse;font-size:.9rem}.evidence-table th,.evidence-table td{border-bottom:1px solid #ddd;padding:.35rem;text-align:left;vertical-align:top}.candidate--disabled{background:#eee;color:#555}.candidate--selected{border:2px solid #267a52}.object-review{display:grid;grid-template-columns:minmax(520px,1.25fr) minmax(420px,1fr);gap:1rem;align-items:start;margin-bottom:1rem}.image-comparison{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}.image-comparison figure{margin:0}.image-comparison figcaption{font-size:.85rem;font-weight:650;margin-top:.35rem}.object-preview{display:block;width:100%;max-height:72vh;object-fit:contain;background:#222}@media(max-width:900px){.object-review{grid-template-columns:1fr}}@media(max-width:600px){.image-comparison{grid-template-columns:1fr}}
"""


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], review: TfaReview) -> None:
        self.review = review
        self.csrf_token = secrets.token_urlsafe(32)
        self.preview_slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: Any) -> None:
        print(f'tfa-workbench {self.address_string()} {format % args}', flush=True)

    def parameters(self) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(self.path)
        return {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise WorkbenchError("form is too large")
        data = self.rfile.read(length).decode("utf-8")
        return {key: values[-1] for key, values in urllib.parse.parse_qs(data).items()}

    def send_bytes(
        self, payload: bytes, content_type: str, status: int = 200,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def page(self, title: str, body: str, status: int = 200) -> None:
        payload = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · TFA</title><style>{CSS}</style></head><body><header><strong>TFA reconciliation</strong><nav><a href="/md">MD catalogue</a> · <a href="/tube-metadata">Metadata reviews</a> · <a href="/all-local-metadata">Overall local census</a> · <a href="/local-metadata-review">Matched book metadata</a> · <a href="/buchprojekt-review">Book objects</a> · <a href="/metadata-review">Metadata evidence</a> · <a href="/documents">Document review</a> · <a href="/recovery">Recovery comparison</a> · <a href="/master-history">Master history</a> · <a href="/holds">Hold review</a> · <a href="/visual-evidence">Visual evidence</a> · <span>port 8768</span></nav></header><main>{body}</main></body></html>""".encode()
        self.send_bytes(payload, "text/html; charset=utf-8", status)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in {"/", "/md"}:
                self.page_md_catalog()
            elif path == "/md-record":
                self.page_md_record()
            elif path == "/master-history":
                self.page_queue()
            elif path == "/record":
                self.page_record()
            elif path == "/image":
                self.image()
            elif path in {"/local", "/holds"}:
                self.page_local_queue()
            elif path == "/visual-evidence":
                self.page_visual_queue()
            elif path == "/local-record":
                self.page_local_record()
            elif path == "/local-image":
                self.local_image()
            elif path == "/visual-image":
                self.visual_image()
            elif path == "/hold-context-image":
                self.hold_context_image()
            elif path == "/recovery":
                self.page_recovery_queue()
            elif path == "/recovery-record":
                self.page_recovery_record()
            elif path == "/recovery-image":
                self.recovery_image()
            elif path == "/documents":
                self.page_document_queue()
            elif path == "/document-record":
                self.page_document_record()
            elif path == "/document-file":
                self.document_file()
            elif path == "/metadata-review":
                self.page_metadata_queue()
            elif path == "/metadata-record":
                self.page_metadata_record()
            elif path == "/metadata-file":
                self.metadata_file()
            elif path == "/local-metadata-review":
                self.page_local_metadata_review_queue()
            elif path == "/local-metadata-record":
                self.page_local_metadata_review_record()
            elif path == "/local-metadata-source":
                self.local_metadata_source()
            elif path == "/local-metadata-book-image":
                self.local_metadata_book_image()
            elif path == "/buchprojekt-review":
                self.page_buchprojekt_unbound_queue()
            elif path == "/buchprojekt-record":
                self.page_buchprojekt_unbound_record()
            elif path == "/buchprojekt-image":
                self.buchprojekt_unbound_image()
            elif path == "/all-local-metadata":
                self.page_all_local_metadata_queue()
            elif path == "/all-local-metadata-record":
                self.page_all_local_metadata_record()
            elif path == "/tube-metadata":
                self.page_all_local_metadata_queue(tube_only=True)
            elif path == "/tube-metadata-record":
                self.page_all_local_metadata_record(tube_only=True)
            elif path == "/all-local-metadata-image":
                self.all_local_metadata_image()
            elif path == "/health":
                self.send_bytes(json.dumps(self.server.review.validate(), sort_keys=True).encode(), "application/json")
            else:
                self.page("Not found", "<section class='notice notice--critical'><h1>Not found</h1></section>", 404)
        except (WorkbenchError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as error:
            self.page("Operation refused", f"<section class='notice notice--critical'><h1>Operation refused</h1><p>{esc(error)}</p></section>", 400)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path not in {"/decision", "/local-decision", "/recovery-decision", "/document-decision", "/metadata-decision", "/local-metadata-decision", "/buchprojekt-decision", "/all-local-metadata-decision"}:
                raise WorkbenchError("unknown workbench action")
            form = self.form()
            if not secrets.compare_digest(form.get("csrf", ""), self.server.csrf_token):
                raise WorkbenchError("invalid CSRF token")
            record_key = form.get("record_key", "")
            if path == "/all-local-metadata-decision":
                return_scope = form.get("return_scope", "")
                if return_scope not in {"", "tube"}:
                    raise WorkbenchError("unknown all-local metadata return scope")
                if return_scope == "tube":
                    self.server.review.metadata_review_record(record_key)
                self.server.review.save_all_local_metadata_decision(
                    record_key,
                    form.get("action", ""),
                    {field: form.get(field, "") for field in ALL_LOCAL_METADATA_FIELDS},
                    form.get("note", ""),
                )
                if return_scope == "tube":
                    next_key = self.next_tube_metadata_open(record_key)
                    target = (
                        url("/tube-metadata-record", record=next_key, notice="Metadata review decision recorded")
                        if next_key
                        else url("/tube-metadata", state="all", notice="Metadata review decision recorded")
                    )
                else:
                    next_key = self.next_all_local_metadata_open(record_key)
                    target = (
                        url("/all-local-metadata-record", record=next_key, notice="Local metadata decision recorded")
                        if next_key
                        else url("/all-local-metadata", state="all", notice="Local metadata decision recorded")
                    )
                self.redirect(target)
                return
            if path == "/buchprojekt-decision":
                self.server.review.save_buchprojekt_unbound_decision(
                    record_key,
                    form.get("action", ""),
                    form.get("selected_candidate_id", ""),
                    {
                        "title": form.get("title", ""),
                        "description": form.get("description", ""),
                        "date_text": form.get("date_text", ""),
                        "object_type": form.get("object_type", ""),
                        "material": form.get("material", ""),
                        "dimensions": form.get("dimensions", ""),
                        "keywords": form.get("keywords", ""),
                        "classification": form.get("classification", ""),
                    },
                    form.get("note", ""),
                )
                next_key = self.next_buchprojekt_unbound_open(record_key)
                target = (
                    url("/buchprojekt-record", record=next_key, notice="Book-object decision recorded")
                    if next_key
                    else url("/buchprojekt-review", state="all", notice="Book-object decision recorded")
                )
                self.redirect(target)
                return
            if path == "/local-metadata-decision":
                self.server.review.save_local_metadata_review_decision(
                    record_key,
                    form.get("action", ""),
                    form.get("candidate_id", ""),
                    {
                        "title": form.get("title", ""),
                        "description": form.get("description", ""),
                        "date_text": form.get("date_text", ""),
                        "object_type": form.get("object_type", ""),
                        "material": form.get("material", ""),
                        "dimensions": form.get("dimensions", ""),
                    },
                    form.get("note", ""),
                )
                next_key = self.next_local_metadata_review_open(record_key)
                target = (
                    url("/local-metadata-record", record=next_key, notice="Local metadata decision recorded")
                    if next_key
                    else url("/local-metadata-review", state="all", notice="Local metadata decision recorded")
                )
                self.redirect(target)
                return
            if path == "/local-decision":
                self.server.review.save_orphan_decision(
                    record_key,
                    form.get("action", ""),
                    form.get("selected_asset_id", ""),
                    form.get("note", ""),
                )
                next_key = self.next_local_open(record_key)
                target = (
                    url("/local-record", record=next_key, notice="Hold decision recorded")
                    if next_key else url("/holds", state="open", notice="Hold decision recorded")
                )
                self.redirect(target)
                return
            if path == "/metadata-decision":
                metadata_id = form.get("metadata_id", "")
                self.server.review.save_metadata_decision(metadata_id, form.get("action", ""), form.get("note", ""))
                next_id = self.next_metadata_open(metadata_id)
                target = (
                    url("/metadata-record", metadata=next_id, notice="Metadata decision recorded")
                    if next_id else url("/metadata-review", state="all", notice="Metadata decision recorded")
                )
                self.redirect(target)
                return
            if path == "/document-decision":
                document_id = form.get("document_id", "")
                self.server.review.save_document_decision(
                    document_id,
                    form.get("action", ""),
                    form.get("note", ""),
                )
                next_id = self.next_document_open(document_id)
                target = (
                    url("/document-record", document=next_id, notice="Document decision recorded")
                    if next_id
                    else url("/documents", state="all", notice="Document decision recorded")
                )
                self.redirect(target)
                return
            if path == "/recovery-decision":
                self.server.review.save_recovery_decision(
                    record_key,
                    form.get("action", ""),
                    form.get("selected_candidate_id", ""),
                    form.get("note", ""),
                )
                next_key = self.next_recovery_open(record_key)
                target = (
                    url("/recovery-record", record=next_key, notice="Decision recorded")
                    if next_key
                    else url("/recovery", state="all", notice="Decision recorded")
                )
                self.redirect(target)
                return
            self.server.review.save_decision(
                record_key,
                form.get("action", ""),
                form.get("selected_candidate_id", ""),
                form.get("note", ""),
            )
            next_key = self.next_unresolved(record_key)
            self.redirect(url("/record", record=next_key, notice="Decision recorded") if next_key else url("/master-history", notice="Decision recorded"))
        except (WorkbenchError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as error:
            self.page("Operation refused", f"<section class='notice notice--critical'><h1>Operation refused</h1><p>{esc(error)}</p></section>", 400)

    def next_unresolved(self, current: str) -> str:
        records = self.server.review.records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["state"] != "selected_master" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_local_open(self, current: str) -> str:
        records = self.server.review.orphan_review_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] in {"unreviewed", "unresolved"} and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_recovery_open(self, current: str) -> str:
        records = self.server.review.recovery_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["state"] == "needs_visual_choice" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_document_open(self, current: str) -> str:
        records = self.server.review.document_records()
        keys = [row["document_id"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["document_id"] != current:
                return str(row["document_id"])
        return ""

    def next_metadata_open(self, current: str) -> str:
        records = self.server.review.metadata_records()
        keys = [row["metadata_id"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["metadata_id"] != current:
                return str(row["metadata_id"])
        return ""

    def next_local_metadata_review_open(self, current: str) -> str:
        records = self.server.review.local_metadata_review_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_buchprojekt_unbound_open(self, current: str) -> str:
        records = self.server.review.buchprojekt_unbound_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_all_local_metadata_open(self, current: str) -> str:
        records = self.server.review.overall_local_metadata_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def next_tube_metadata_open(self, current: str) -> str:
        records = self.server.review.metadata_review_records()
        keys = [row["record_key"] for row in records]
        start = keys.index(current) if current in keys else -1
        for offset in range(1, len(keys) + 1):
            row = records[(start + offset) % len(records)]
            if row["review_state"] == "unreviewed" and row["record_key"] != current:
                return str(row["record_key"])
        return ""

    def page_all_local_metadata_queue(self, *, tube_only: bool = False) -> None:
        p = self.parameters()
        state = p.get("state", "open")
        wanted = {
            "applied": "apply_metadata", "edited": "apply_edited_metadata",
            "noise": "reject_noise", "dismissed": "dismiss_object", "deferred": "defer",
        }
        if state not in {"open", "existing", "all", *wanted}:
            raise WorkbenchError("unknown all-local metadata filter")
        query = p.get("q", "").casefold().strip()
        all_records = (
            self.server.review.metadata_review_records()
            if tube_only else self.server.review.overall_local_metadata_records()
        )
        queue_path = "/tube-metadata" if tube_only else "/all-local-metadata"
        record_path = "/tube-metadata-record" if tube_only else "/all-local-metadata-record"
        if state == "open":
            records = [row for row in all_records if row["review_state"] == "unreviewed"]
        elif state == "existing":
            records = [row for row in all_records if row["review_state"].startswith("existing_")]
        elif state in wanted:
            records = [row for row in all_records if row["review_state"] == wanted[state]]
        else:
            records = all_records
        if query:
            search_fields = (
                "record_key", "inventory", "selected_master_path", "proposed_title",
                "proposed_description", "proposed_date_text", "proposed_keywords",
                "proposed_classification", "proposed_project", "project_candidates_json",
            )
            records = [
                row for row in records
                if query in (
                    " ".join(str(row.get(field, "")) for field in search_fields)
                    + " "
                    + " ".join(str(value) for value in row["decision_record"].values())
                ).casefold()
            ]
        page_number = max(1, int(p.get("page", "1")))
        page_size = 48
        start = (page_number - 1) * page_size
        cards = []
        for row in records[start:start + page_size]:
            display_metadata = (
                row["decision_record"]
                if row["review_state"] in {"apply_metadata", "apply_edited_metadata"}
                else {}
            )
            display_title = display_metadata.get("title", "") or row["proposed_title"]
            display_date = display_metadata.get("date_text", "") or row["proposed_date_text"]
            display_classification = (
                display_metadata.get("classification", "") or row["proposed_classification"]
            )
            badge = (
                "selected" if row["review_state"] in {
                    "apply_metadata", "apply_edited_metadata", "existing_metadata",
                }
                else "critical" if row["review_state"] in {"reject_noise", "dismiss_object"}
                else "warning"
            )
            project = row["proposed_project"] or ", ".join(json.loads(row["project_candidates_json"]))
            cards.append(
                f"""<article class="record-card"><img loading="lazy" src="{esc(url('/all-local-metadata-image',record=row['record_key'],thumb='1' if not tube_only else ''))}" alt="{esc(row['inventory'])}"><div><span class="badge badge--{badge}">{esc(row['review_state'])}</span>{f'<span class="badge">{esc(project)}</span>' if project else ''}<h2><a href="{esc(url(record_path,record=row['record_key']))}">{esc(row['inventory'])}</a></h2><p><strong>{esc(display_title) or 'No reliable title proposal'}</strong></p><p>{esc(display_date) or 'No date'} · {esc(display_classification) or 'Unclassified'}</p><p class="technical">{esc(row['width_px']) or '?'}×{esc(row['height_px']) or '?'} · {esc(row['extension'])} · {esc(row['proposal_basis'])}</p></div></article>"""
            )
        counts = {
            "open": sum(row["review_state"] == "unreviewed" for row in all_records),
            "existing": sum(row["review_state"].startswith("existing_") for row in all_records),
            **{key: sum(row["review_state"] == value for row in all_records) for key, value in wanted.items()},
        }
        previous = url(queue_path, state=state, q=p.get("q", ""), page=page_number - 1) if page_number > 1 else ""
        following = url(queue_path, state=state, q=p.get("q", ""), page=page_number + 1) if start + page_size < len(records) else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {len(records)} matching objects</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Identified metadata reviews" if tube_only else "Overall local-only object census",
            f"""{notice}<section class="notice notice--safe"><strong>{'Identified metadata review bench.' if tube_only else 'Overall local-only object census.'}</strong><p>{'This bench combines the prior 60 tube and tube-component proposals with every object that has a metadata decision. All are excluded from the overall census view.' if tube_only else 'Objects in the identified metadata review bench are excluded here; every remaining card has its already selected Master.'} Open a card to review the image, edit metadata, apply the shown proposal, reject the proposal as noise, dismiss the object from metadata review, or defer it. Project labels are evidence only. No action changes the Master, source image, published unit, or deletion status.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>objects</div><div class="panel metric"><strong>{counts['open']}</strong>open</div><div class="panel metric"><strong>{counts['existing']}</strong>existing review</div><div class="panel metric"><strong>{counts['applied'] + counts['edited']}</strong>new metadata decisions</div><div class="panel metric"><strong>{counts['noise']}</strong>noise</div><div class="panel metric"><strong>{counts['dismissed']}</strong>dismissed</div><div class="panel metric"><strong>{counts['deferred']}</strong>deferred</div></section><form class="panel filters" method="get" action="{queue_path}"><label>State<select name="state"><option value="open"{' selected' if state=='open' else ''}>Open</option><option value="existing"{' selected' if state=='existing' else ''}>Existing review</option><option value="applied"{' selected' if state=='applied' else ''}>Applied proposed</option><option value="edited"{' selected' if state=='edited' else ''}>Applied edited</option><option value="noise"{' selected' if state=='noise' else ''}>Rejected noise</option><option value="dismissed"{' selected' if state=='dismissed' else ''}>Dismissed</option><option value="deferred"{' selected' if state=='deferred' else ''}>Deferred</option><option value="all"{' selected' if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="inventory, metadata, project, path"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching local-only objects.</p>'}</section>{pagination}""",
        )

    def page_all_local_metadata_record(self, *, tube_only: bool = False) -> None:
        p = self.parameters()
        record = (
            self.server.review.metadata_review_record(p.get("record", ""))
            if tube_only else self.server.review.overall_local_metadata_record(p.get("record", ""))
        )
        all_records = (
            self.server.review.metadata_review_records()
            if tube_only else self.server.review.overall_local_metadata_records()
        )
        queue_path = "/tube-metadata" if tube_only else "/all-local-metadata"
        record_path = "/tube-metadata-record" if tube_only else "/all-local-metadata-record"
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = (
            self.next_tube_metadata_open(record["record_key"])
            if tube_only else self.next_all_local_metadata_open(record["record_key"])
        )
        nav = f"<nav class='nav'>{f'<a href={esc(url(record_path,record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='{queue_path}'>{'Metadata reviews' if tube_only else 'Overall local census'}</a>{f'<a href={esc(url(record_path,record=following))}>Next →</a>' if following else '<span></span>'}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        current = ""
        if record["review_state"] != "unreviewed":
            current = f"<section class='notice notice--safe'><strong>Current metadata review: {esc(record['review_state'])}</strong><p>{esc(record['review_note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])} · {esc(record['review_origin'])}</p></section>"
        decision = record["decision_record"]
        values = decision if record["review_state"] in {"apply_metadata", "apply_edited_metadata"} else {
            field: record.get("proposed_" + field, "") for field in ALL_LOCAL_METADATA_FIELDS
        }
        values["note"] = record["review_note"]
        projects = json.loads(record["project_candidates_json"])
        evidence_paths = json.loads(record["project_evidence_paths_json"])
        project_notice = (
            f"<section class='notice'><strong>Project evidence: {esc(', '.join(projects))}</strong><p>State {esc(record['project_assignment_state'])}; confidence {esc(record['project_confidence'])}. This is routing/use evidence, not ownership authority.</p></section>"
            if projects else "<section class='notice'><strong>No identifiable project evidence.</strong><p>The TFA-style inventory prefix is not treated as a project assignment.</p></section>"
        )
        evidence = "".join(f"<li><code>{esc(path)}</code></li>" for path in evidence_paths)
        self.page(
            record["inventory"],
            f"""{nav}{notice}{current}{project_notice}<form class="metadata-form" method="post" action="/all-local-metadata-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record['record_key'])}"><input type="hidden" name="return_scope" value="{'tube' if tube_only else ''}"><section class="object-review"><article class="panel"><img class="object-preview" src="{esc(url('/all-local-metadata-image',record=record['record_key']))}" alt="{esc(record['inventory'])}"><h1>{esc(record['inventory'])}</h1><p><span class="badge badge--selected">selected Master unchanged</span> <span class="badge">{esc(record['width_px']) or '?'}×{esc(record['height_px']) or '?'}</span> <span class="badge">{esc(record['extension'])}</span></p><p class="technical path"><code>{esc(record['selected_master_path'])}</code></p></article><section class="panel"><h2>Supposed metadata</h2><p class="technical">{esc(record['proposal_basis'])}</p><div class="metadata-grid"><label>Title<input required name="title" value="{esc(values.get('title',''))}"></label><label>Date<input name="date_text" value="{esc(values.get('date_text',''))}"></label><label>Object type<input name="object_type" value="{esc(values.get('object_type',''))}"></label><label>Material / technique<input name="material" value="{esc(values.get('material',''))}"></label><label>Dimensions<input name="dimensions" value="{esc(values.get('dimensions',''))}"></label></div><label>Description<textarea name="description">{esc(values.get('description',''))}</textarea></label><label>Classification<input name="classification" value="{esc(values.get('classification',''))}"></label><label>Keywords<textarea name="keywords">{esc(values.get('keywords',''))}</textarea></label><p class="technical">Use semicolons between classifications or keywords.</p><label>Decision note<textarea name="note">{esc(values.get('note',''))}</textarea></label></section></section><section class="panel"><h2>Project and metadata evidence</h2><p>{esc(record['project_evidence_count'])} evidence row(s); up to 25 unique paths are listed.</p>{f'<ul>{evidence}</ul>' if evidence else '<p>No project metadata path identified.</p>'}</section><section class="panel decision"><div class="buttons"><button class="button" formnovalidate name="action" value="apply_metadata"{' disabled' if not record['proposed_title'] else ''}>Apply proposed metadata</button><button class="button" name="action" value="apply_edited_metadata">Apply edited metadata</button><button class="button button--critical" formnovalidate name="action" value="reject_noise">Reject metadata as noise</button><button class="button button--critical" formnovalidate name="action" value="dismiss_object">Dismiss object</button><button class="button button--quiet" formnovalidate name="action" value="defer">Defer</button>{f'<a class="button button--quiet" href="{esc(url(record_path,record=skip_key))}">Skip</a>' if skip_key else ''}</div><p class="technical">Apply records metadata in the all-local decision CSV. Reject affects the supposed metadata only. Dismiss removes the object from this metadata review, not from the archive. The selected Master and every source file remain unchanged; deletion is never authorized.</p></section></form>""",
        )

    def all_local_metadata_image(self) -> None:
        parameters = self.parameters()
        record_key = parameters.get("record", "")
        path = self.server.review.guarded_all_local_master(record_key)
        if parameters.get("thumb") == "1":
            thumbnail = self.server.review.guarded_all_local_thumbnail(record_key)
            self.send_bytes(
                thumbnail.read_bytes(), "image/jpeg",
                cache_control="public, max-age=86400",
            )
            return
        if not self.server.preview_slots.acquire(timeout=120):
            raise WorkbenchError("local preview queue is busy; retry this image")
        try:
            completed = subprocess.run(
                ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
                check=True, capture_output=True, timeout=45,
            )
        finally:
            self.server.preview_slots.release()
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded local preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def page_local_metadata_review_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "open")
        wanted = {
            "applied": "apply_metadata", "edited": "apply_edited_metadata",
            "noise": "reject_noise", "dismissed": "dismiss_object", "deferred": "defer",
        }
        if state not in {"open", "all", *wanted}:
            raise WorkbenchError("unknown local metadata review filter")
        query = p.get("q", "").casefold().strip()
        all_records = self.server.review.local_metadata_review_records()
        if state == "open":
            records = [row for row in all_records if row["review_state"] == "unreviewed"]
        elif state in wanted:
            records = [row for row in all_records if row["review_state"] == wanted[state]]
        else:
            records = all_records
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(candidate.get(key, ""))
                    for candidate in row["candidates"]
                    for key in (
                        "record_key", "inventory", "selected_master_path", "source_path",
                        "sheet_name", "row_inventory", "md_inventory", "md_title",
                        "suggested_title", "suggested_description", "suggested_date_text",
                    )
                ).casefold()
            ]
        page_number = max(1, int(p.get("page", "1")))
        page_size = 40
        start = (page_number - 1) * page_size
        cards = []
        for row in records[start:start + page_size]:
            proposed = row["proposed_candidate"]
            badge = "selected" if row["review_state"] in {"apply_metadata", "apply_edited_metadata"} else "warning"
            cards.append(
                f"""<article class="record-card"><img loading="lazy" src="{esc(url('/local-image',candidate=row['preview_candidate_id']))}" alt="{esc(row['inventory'])}"><div><span class="badge badge--{badge}">{esc(row['review_state'])}</span><h2><a href="{esc(url('/local-metadata-record',record=row['record_key']))}">{esc(row['inventory'])}</a></h2><p><strong>{esc(proposed['suggested_title']) or 'Untitled proposal'}</strong></p><p>{esc(proposed['suggested_date_text']) or 'No date'} · {esc(proposed['suggested_dimensions']) or 'No dimensions'}</p><p class="technical">row inventory: {esc(proposed['row_inventory']) or 'unclear'} · {esc(proposed['identity_review_state'])}</p></div></article>"""
            )
        counts = {
            "open": sum(row["review_state"] == "unreviewed" for row in all_records),
            **{key: sum(row["review_state"] == value for row in all_records) for key, value in wanted.items()},
        }
        previous = url("/local-metadata-review", state=state, q=p.get("q", ""), page=page_number - 1) if page_number > 1 else ""
        following = url("/local-metadata-review", state=state, q=p.get("q", ""), page=page_number + 1) if start + page_size < len(records) else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {len(records)} matching objects</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Local-object metadata review",
            f"""{notice}<section class="notice notice--safe"><strong>Object-first metadata review.</strong><p>Open an object to compare its selected Master with the proposed metadata, edit the fields if needed, or reject, dismiss, or defer it. Decisions stay in the reconciliation work area; source files and the published archive remain unchanged.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>objects</div><div class="panel metric"><strong>{counts['open']}</strong>open</div><div class="panel metric"><strong>{counts['applied']}</strong>meta applied</div><div class="panel metric"><strong>{counts['edited']}</strong>edited applied</div><div class="panel metric"><strong>{counts['noise']}</strong>noise</div><div class="panel metric"><strong>{counts['dismissed']}</strong>dismissed</div><div class="panel metric"><strong>{counts['deferred']}</strong>deferred</div></section><form class="panel filters" method="get" action="/local-metadata-review"><label>State<select name="state"><option value="open"{' selected' if state=='open' else ''}>Open</option><option value="applied"{' selected' if state=='applied' else ''}>Meta applied</option><option value="edited"{' selected' if state=='edited' else ''}>Edited meta applied</option><option value="noise"{' selected' if state=='noise' else ''}>Rejected noise</option><option value="dismissed"{' selected' if state=='dismissed' else ''}>Dismissed objects</option><option value="deferred"{' selected' if state=='deferred' else ''}>Deferred</option><option value="all"{' selected' if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="inventory, title, date, path"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching local metadata objects.</p>'}</section>{pagination}""",
        )

    def page_local_metadata_review_record(self) -> None:
        p = self.parameters()
        record = self.server.review.local_metadata_review_record(p.get("record", ""))
        all_records = self.server.review.local_metadata_review_records()
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_local_metadata_review_open(record["record_key"])
        nav = f"<nav class='nav'>{f'<a href={esc(url('/local-metadata-record',record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/local-metadata-review'>Local metadata queue</a>{f'<a href={esc(url('/local-metadata-record',record=following))}>Next →</a>' if following else '<span></span>'}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        current = ""
        if record["review_state"] != "unreviewed":
            current = f"<section class='notice notice--safe'><strong>Current decision: {esc(record['review_state'])}</strong><p>{esc(record['review_note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])}</p></section>"
        proposed = record["proposed_candidate"]
        decision = record["decision_record"]
        values = decision if record["review_state"] in {"apply_metadata", "apply_edited_metadata"} else {
            "title": proposed["suggested_title"], "description": proposed["suggested_description"],
            "date_text": proposed["suggested_date_text"], "object_type": proposed["suggested_object_type"],
            "material": proposed["suggested_material"], "dimensions": proposed["suggested_dimensions"],
            "note": record["review_note"],
        }
        md_warning = ""
        if proposed["md_object_id"]:
            md_warning = f"""<section class="notice"><strong>Identity warning: source row {esc(proposed['row_inventory'])} already has museum-digital object {esc(proposed['md_object_id'])}.</strong><p><a href="https://berlin.museum-digital.de/object/{esc(proposed['md_object_id'])}" target="_blank" rel="noreferrer">{esc(proposed['md_title']) or proposed['md_inventory']}</a>. Compare the image before applying this metadata to the local object.</p></section>"""
        if proposed["identity_review_state"] == "book_visual_match_related_object":
            md_warning += f"""<section class="notice"><strong>Related-frame warning.</strong><p>The selected Master visually matches book image {esc(proposed['book_image_key'])}, but its own inventory differs. Apply only if the caption also describes this adjacent frame; otherwise defer or reject it.</p></section>"""
        evidence = []
        for candidate in record["candidates"]:
            try:
                cells = json.loads(candidate["labelled_cells_json"])
            except json.JSONDecodeError as error:
                raise WorkbenchError("local metadata row evidence is not valid JSON") from error
            cell_rows = "".join(
                f"<tr><td class='technical'>{esc(cell.get('cell'))}</td><td>{esc(cell.get('header')) or '—'}</td><td>{esc(cell.get('value'))}</td></tr>"
                for cell in cells
            )
            evidence.append(
                f"""<details{' open' if candidate['candidate_id']==proposed['candidate_id'] else ''}><summary>{'Proposed row · ' if candidate['candidate_id']==proposed['candidate_id'] else ''}{esc(Path(candidate['source_path']).name)} · {esc(candidate['sheet_name'])} row {esc(candidate['row_number'])} · row inventory {esc(candidate['row_inventory']) or 'unclear'}</summary><p><span class="badge">{esc(candidate['identity_review_state'])}</span> <a href="{esc(url('/local-metadata-source',candidate=candidate['candidate_id']))}" target="_blank" rel="noreferrer">Open source file</a></p><table class="evidence-table"><thead><tr><th>Cell</th><th>Header</th><th>Value</th></tr></thead><tbody>{cell_rows}</tbody></table></details>"""
            )
        review_form = f"""<form class="metadata-form" method="post" action="/local-metadata-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record['record_key'])}"><input type="hidden" name="candidate_id" value="{esc(proposed['candidate_id'])}"><section class="panel"><h2>Supposed metadata</h2><p class="technical">Book image {esc(proposed['book_image_key'])} · {esc(proposed['book_page'])} · {esc(Path(proposed['source_path']).name)} · {esc(proposed['sheet_name'])} row {esc(proposed['row_number'])}</p><div class="metadata-grid"><label>Title<input required name="title" value="{esc(values.get('title',''))}"></label><label>Date<input name="date_text" value="{esc(values.get('date_text',''))}"></label><label>Object type<input name="object_type" value="{esc(values.get('object_type',''))}"></label><label>Material / technique<input name="material" value="{esc(values.get('material',''))}"></label><label>Dimensions<input name="dimensions" value="{esc(values.get('dimensions',''))}"></label></div><label>Description<textarea name="description">{esc(values.get('description',''))}</textarea></label><label>Decision note<textarea name="note">{esc(values.get('note',''))}</textarea></label><p class="technical">Visual distances: aHash {esc(proposed['visual_ahash_distance'])} · dHash {esc(proposed['visual_dhash_distance'])} · pHash {esc(proposed['visual_phash_distance'])} · mean {esc(proposed['visual_mean_distance'])} · aspect delta {esc(proposed['visual_aspect_ratio_delta'])}. PDF caption support {esc(proposed['pdf_caption_support_score'])}.</p></section>{md_warning}<section class="panel decision"><div class="buttons"><button class="button" formnovalidate name="action" value="apply_metadata"{' disabled' if not proposed['suggested_title'] else ''}>Apply proposed metadata</button><button class="button" name="action" value="apply_edited_metadata">Apply edited metadata</button><button class="button button--critical" formnovalidate name="action" value="reject_noise">Reject metadata as noise</button><button class="button button--critical" formnovalidate name="action" value="dismiss_object">Dismiss object</button><button class="button button--quiet" formnovalidate name="action" value="defer">Defer</button>{f'<a class="button button--quiet" href="{esc(url("/local-metadata-record",record=skip_key))}">Skip</a>' if skip_key else ''}</div><p class="technical">Apply writes the source-bound metadata decision. Dismiss marks the object out of this metadata/accession proposal but deletes nothing. Defer keeps it open for later evidence.</p></section></form>"""
        self.page(
            record["inventory"],
            f"""{nav}{notice}{current}<section class="object-review"><article class="panel"><div class="image-comparison"><figure><img class="object-preview" src="{esc(url('/local-image',candidate=record['preview_candidate_id']))}" alt="{esc(record['inventory'])}"><figcaption>Selected local Master · {esc(record['inventory'])}</figcaption></figure><figure><img class="object-preview" src="{esc(url('/local-metadata-book-image',candidate=proposed['candidate_id']))}" alt="{esc(proposed['book_image_key'])}"><figcaption>Captioned Buchprojekt image · {esc(proposed['book_image_key'])}</figcaption></figure></div><p class="technical path"><code>{esc(record['selected_master_path'])}</code></p></article>{review_form}</section><section class="panel"><h2>Source evidence</h2><p>{record['candidate_count']} source-bound candidate(s). The final caption-workbook row is open below.</p>{''.join(evidence)}</section>""",
        )

    def local_metadata_source(self) -> None:
        candidate_id = self.parameters().get("candidate", "")
        path = self.server.review.guarded_local_metadata_source(candidate_id)
        self.send_bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def local_metadata_book_image(self) -> None:
        candidate_id = self.parameters().get("candidate", "")
        path = self.server.review.guarded_local_metadata_book_image(candidate_id)
        if not self.server.preview_slots.acquire(timeout=120):
            raise WorkbenchError("Buchprojekt preview queue is busy; retry this image")
        try:
            completed = subprocess.run(
                ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
                check=True, capture_output=True, timeout=45,
            )
        finally:
            self.server.preview_slots.release()
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded Buchprojekt preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def page_buchprojekt_unbound_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "open")
        wanted = {
            "applied": "apply_metadata", "edited": "apply_edited_metadata",
            "noise": "reject_noise", "dismissed": "dismiss_object", "deferred": "defer",
        }
        if state not in {"open", "all", *wanted}:
            raise WorkbenchError("unknown unbound Buchprojekt filter")
        query = p.get("q", "").casefold().strip()
        all_records = self.server.review.buchprojekt_unbound_records()
        if state == "open":
            records = [row for row in all_records if row["review_state"] == "unreviewed"]
        elif state in wanted:
            records = [row for row in all_records if row["review_state"] == wanted[state]]
        else:
            records = all_records
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row["book_candidate"].get(key, "")) for key in (
                        "record_key", "inventory", "book_image_key", "book_page",
                        "book_caption", "suggested_title", "suggested_date_text", "scope_state",
                    )
                ).casefold()
            ]
        page_number = max(1, int(p.get("page", "1")))
        page_size = 40
        start = (page_number - 1) * page_size
        cards = []
        for row in records[start:start + page_size]:
            book = row["book_candidate"]
            badge = "selected" if row["review_state"] in {"apply_metadata", "apply_edited_metadata"} else "warning"
            cards.append(
                f"""<article class="record-card"><img loading="lazy" src="{esc(url('/buchprojekt-image',candidate=book['candidate_id']))}" alt="{esc(book['book_image_key'])}"><div><span class="badge badge--{badge}">{esc(row['review_state'])}</span><span class="badge">{esc(row['scope_state'])}</span><h2><a href="{esc(url('/buchprojekt-record',record=row['record_key']))}">{esc(book['book_image_key'])}</a></h2><p><strong>{esc(book['suggested_title']) or 'Untitled proposal'}</strong></p><p>{esc(row['inventory'])} · {esc(book['book_page'])}</p><p class="technical">{row['selectable_count']} TIFF Master candidate(s) · {row['candidate_count']} exact visual copies</p></div></article>"""
            )
        counts = {
            "open": sum(row["review_state"] == "unreviewed" for row in all_records),
            **{key: sum(row["review_state"] == value for row in all_records) for key, value in wanted.items()},
        }
        previous = url("/buchprojekt-review", state=state, q=p.get("q", ""), page=page_number - 1) if page_number > 1 else ""
        following = url("/buchprojekt-review", state=state, q=p.get("q", ""), page=page_number + 1) if start + page_size < len(records) else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {len(records)} matching pictures</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Unbound Buchprojekt pictures",
            f"""{notice}<section class="notice"><strong>Identity and metadata review.</strong><p>Each picture is absent from the selected-Master catalogue. Open it, compare every exact visual copy, select one TIFF as the single Master, and confirm or edit the final book caption. Applying records a reconciliation proposal only; it does not alter source files or accession the object.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>pictures</div><div class="panel metric"><strong>{counts['open']}</strong>open</div><div class="panel metric"><strong>{counts['applied']}</strong>meta applied</div><div class="panel metric"><strong>{counts['edited']}</strong>edited applied</div><div class="panel metric"><strong>{counts['noise']}</strong>metadata noise</div><div class="panel metric"><strong>{counts['dismissed']}</strong>dismissed</div><div class="panel metric"><strong>{counts['deferred']}</strong>deferred</div></section><form class="panel filters" method="get" action="/buchprojekt-review"><label>State<select name="state"><option value="open"{' selected' if state=='open' else ''}>Open</option><option value="applied"{' selected' if state=='applied' else ''}>Meta applied</option><option value="edited"{' selected' if state=='edited' else ''}>Edited applied</option><option value="noise"{' selected' if state=='noise' else ''}>Rejected metadata</option><option value="dismissed"{' selected' if state=='dismissed' else ''}>Dismissed objects</option><option value="deferred"{' selected' if state=='deferred' else ''}>Deferred</option><option value="all"{' selected' if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="book key, inventory, caption"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching Buchprojekt pictures.</p>'}</section>{pagination}""",
        )

    def page_buchprojekt_unbound_record(self) -> None:
        p = self.parameters()
        record = self.server.review.buchprojekt_unbound_record(p.get("record", ""))
        all_records = self.server.review.buchprojekt_unbound_records()
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_buchprojekt_unbound_open(record["record_key"])
        nav = f"<nav class='nav'>{f'<a href={esc(url('/buchprojekt-record',record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/buchprojekt-review'>Book-object queue</a>{f'<a href={esc(url('/buchprojekt-record',record=following))}>Next →</a>' if following else '<span></span>'}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        current = ""
        decision = record["decision_record"]
        if record["review_state"] != "unreviewed":
            current = f"<section class='notice notice--safe'><strong>Current decision: {esc(record['review_state'])}</strong><p>{esc(decision.get('note')) or 'No note'} · {esc(decision.get('decided_by'))} · {esc(decision.get('decided_at'))}</p></section>"
        book = record["book_candidate"]
        values = decision if record["review_state"] in {"apply_metadata", "apply_edited_metadata"} else {
            "title": book["suggested_title"], "description": book["suggested_description"],
            "date_text": book["suggested_date_text"], "object_type": book["suggested_object_type"],
            "material": book["suggested_material"], "dimensions": book["suggested_dimensions"],
            "keywords": book["suggested_keywords"],
            "classification": book["suggested_classification"],
            "note": decision.get("note", ""),
        }
        selected_id = decision.get("selected_candidate_id", "")
        candidate_cards = []
        binding_record_keys: set[str] = set()
        for candidate in record["candidates"]:
            selectable = candidate["selectable_master"] == "true"
            checked = candidate["candidate_id"] == selected_id
            try:
                bindings = json.loads(candidate["existing_candidate_bindings_json"])
            except json.JSONDecodeError as error:
                raise WorkbenchError("Buchprojekt candidate bindings are invalid JSON") from error
            binding_record_keys.update(str(item.get("record_key", "")) for item in bindings if item.get("record_key"))
            binding_badges = "".join(
                f'<span class="badge badge--warning">comparison candidate in {esc(item.get("record_key"))}</span>'
                for item in bindings
            )
            choice = (
                f"""<label class="choice"><input type="radio" name="selected_candidate_id" value="{esc(candidate['candidate_id'])}"{' checked' if checked else ''}>Select this TIFF as the single Master</label>"""
                if selectable
                else "<p class='notice'>Existing derivative/copy — shown for provenance and automatically redundant after Master selection.</p>"
            )
            candidate_cards.append(
                f"""<article class="candidate{' candidate--disabled' if not selectable else ''}"><img loading="lazy" src="{esc(url('/buchprojekt-image',candidate=candidate['candidate_id']))}" alt="{esc(candidate['filename'])}"><div><span class="badge badge--{'selected' if selectable else 'warning'}">{esc(candidate['candidate_role'])}</span><span class="badge">{esc(candidate['provenance_role'])}</span>{binding_badges}<h3>{esc(candidate['filename'])}</h3><p class="technical">{human_size(candidate['size_bytes'])} · {esc(candidate['width_px'])}×{esc(candidate['height_px'])} · {esc(candidate['extension'])}</p><p class="technical path"><code>{esc(candidate['source_path'])}</code></p>{choice}</div></article>"""
            )
        identity_notice = (
            "The proposed identifier uses the existing visual-signature convention because no archival inventory number is present."
            if record["scope_state"] == "no_inventory"
            else "The inventory is present in the book evidence but has no resolved selected Master in the current catalogue."
        )
        existing_binding_notice = (
            "<section class='notice'><strong>Existing catalogue-candidate relationship.</strong>"
            f"<p>An exact visual copy is already attached as a candidate to {esc(', '.join(sorted(binding_record_keys)))}. "
            "Compare that relationship before creating a separate local object; it is not an automatic identity match.</p></section>"
            if binding_record_keys else ""
        )
        self.page(
            book["book_image_key"],
            f"""{nav}{notice}{current}{existing_binding_notice}<form class="metadata-form" method="post" action="/buchprojekt-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record['record_key'])}"><section class="object-review"><article class="panel"><img class="object-preview" src="{esc(url('/buchprojekt-image',candidate=book['candidate_id']))}" alt="{esc(book['book_image_key'])}"><p><strong>Captioned book picture · {esc(book['book_image_key'])}</strong></p><p>{esc(book['book_page'])} · proposed object <code>{esc(record['inventory'])}</code></p><p>{esc(identity_notice)}</p></article><section class="panel"><h2>Supposed metadata</h2><p class="technical">{esc(Path(book['workbook_path']).name)} · {esc(book['sheet_name'])} row {esc(book['row_number'])}</p><div class="metadata-grid"><label>Title<input required name="title" value="{esc(values.get('title',''))}"></label><label>Date<input name="date_text" value="{esc(values.get('date_text',''))}"></label><label>Object type<input name="object_type" value="{esc(values.get('object_type',''))}"></label><label>Material / technique<input name="material" value="{esc(values.get('material',''))}"></label><label>Dimensions<input name="dimensions" value="{esc(values.get('dimensions',''))}"></label></div><label>Description<textarea name="description">{esc(values.get('description',''))}</textarea></label><label>Classification<input name="classification" value="{esc(values.get('classification',''))}"></label><label>Keywords<textarea name="keywords">{esc(values.get('keywords',''))}</textarea></label><p class="technical">Classification uses the existing museum-digital Fotostelle WF labels; keywords use existing catalogue terms. Separate multiple values with semicolons.</p><label>Decision note<textarea name="note">{esc(values.get('note',''))}</textarea></label></section></section><section class="panel"><h2>Master candidates</h2><p>All {record['candidate_count']} files are exact visual copies. Select exactly one of the {record['selectable_count']} TIFFs as Master; JPEG/PNG copies are comparison-only. Every other displayed path will be recorded as redundant, never deleted.</p><section class="candidate-grid">{''.join(candidate_cards)}</section></section><section class="panel decision"><div class="buttons"><button class="button" name="action" value="apply_metadata">Apply proposed metadata</button><button class="button" name="action" value="apply_edited_metadata">Apply edited metadata</button><button class="button button--critical" formnovalidate name="action" value="reject_noise">Reject metadata as noise</button><button class="button button--critical" formnovalidate name="action" value="dismiss_object">Dismiss object</button><button class="button button--quiet" formnovalidate name="action" value="defer">Defer</button>{f'<a class="button button--quiet" href="{esc(url("/buchprojekt-record",record=skip_key))}">Skip</a>' if skip_key else ''}</div><p class="technical">Apply records one proposed Master, the edited or proposed caption, classification and keywords, and all other copies as redundant. Dismiss and noise decisions select no Master. No action authorizes deletion or writes to the archive.</p></section></form>""",
        )

    def buchprojekt_unbound_image(self) -> None:
        candidate_id = self.parameters().get("candidate", "")
        path = self.server.review.guarded_buchprojekt_candidate(candidate_id)
        if not self.server.preview_slots.acquire(timeout=120):
            raise WorkbenchError("Buchprojekt object preview queue is busy; retry this image")
        try:
            completed = subprocess.run(
                ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
                check=True, capture_output=True, timeout=45,
            )
        finally:
            self.server.preview_slots.release()
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded Buchprojekt object preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def page_metadata_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "open")
        if state not in {"open", "include", "exclude", "unresolved", "all"}:
            raise WorkbenchError("unknown metadata review filter")
        query = p.get("q", "").casefold().strip()
        all_records = self.server.review.metadata_records()
        wanted = {
            "open": "unreviewed", "include": "include_metadata_evidence",
            "exclude": "exclude_metadata_evidence", "unresolved": "unresolved",
        }
        records = all_records if state == "all" else [row for row in all_records if row["review_state"] == wanted[state]]
        if query:
            records = [row for row in records if query in " ".join(str(row.get(key, "")) for key in (
                "filename", "source_path", "evidence_excerpt", "matched_tfa_inventory_ids", "category", "extension", "parser"
            )).casefold()]
        page_number = max(1, int(p.get("page", "1"))); page_size = 40; start = (page_number - 1) * page_size
        shown = records[start:start + page_size]
        cards = []
        for row in shown:
            badge = "selected" if row["review_state"] == "include_metadata_evidence" else "warning"
            cards.append(f"""<article class="record-card"><div class="empty-preview">{esc(row['extension'].upper().lstrip('.'))}</div><div><span class="badge badge--{badge}">{esc(row['review_state'])}</span><span class="badge">{esc(row['evidence_confidence'])}</span><h2><a href="{url('/metadata-record',metadata=row['metadata_id'])}">{esc(row['filename'])}</a></h2><p>{esc(row['category'])} · {human_size(row['size_bytes'])}</p><p class="technical">{esc(row['extraction_state'])} · {esc(row['parser'])}</p><p class="technical path"><code>{esc(row['source_path'])}</code></p></div></article>""")
        counts = {name: sum(row["review_state"] == value for row in all_records) for name, value in wanted.items()}
        previous = url("/metadata-review", state=state, q=p.get("q", ""), page=page_number - 1) if page_number > 1 else ""
        following = url("/metadata-review", state=state, q=p.get("q", ""), page=page_number + 1) if start + page_size < len(records) else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {len(records)} matching records</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page("Ambiguous TFA metadata", f"""{notice}<section class="notice"><strong>Human metadata review required.</strong><p>museum-digital remains the current authority. This queue contains only ambiguous working-metadata representatives; decisions record whether each file belongs in TFA provenance evidence. They do not rebuild the package, change authority, move files, or authorize deletion.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>review scope</div><div class="panel metric"><strong>{counts['open']}</strong>unreviewed</div><div class="panel metric"><strong>{counts['include']}</strong>include</div><div class="panel metric"><strong>{counts['exclude']}</strong>exclude</div><div class="panel metric"><strong>{counts['unresolved']}</strong>unresolved</div></section><form class="panel filters" method="get" action="/metadata-review"><label>State<select name="state"><option value="open"{' selected' if state=='open' else ''}>Unreviewed</option><option value="include"{' selected' if state=='include' else ''}>Include evidence</option><option value="exclude"{' selected' if state=='exclude' else ''}>Exclude / cross-unit</option><option value="unresolved"{' selected' if state=='unresolved' else ''}>Unresolved</option><option value="all"{' selected' if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="filename, path, evidence, TFA id"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching metadata records.</p>'}</section>{pagination}""")

    def page_metadata_record(self) -> None:
        p = self.parameters(); record = self.server.review.metadata_record(p.get("metadata", ""))
        all_records = self.server.review.metadata_records(); keys = [row["metadata_id"] for row in all_records]
        index = keys.index(record["metadata_id"]); previous = keys[index - 1] if index > 0 else ""; following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_metadata_open(record["metadata_id"])
        nav = f"<nav class='nav'>{f'<a href={esc(url('/metadata-record',metadata=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/metadata-review'>Metadata queue</a>{f'<a href={esc(url('/metadata-record',metadata=following))}>Next →</a>' if following else '<span></span>'}</nav>"
        current = ""
        if record["review_state"] != "unreviewed":
            current = f"<section class='notice notice--safe'><strong>Current decision: {esc(record['review_state'])}</strong><p>{esc(record['review_note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])}</p></section>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(record["filename"], f"""{nav}{notice}{current}<section class="candidate-grid"><article class="candidate"><div><span class="badge badge--warning">ambiguous</span><span class="badge">{esc(record['extension'])}</span><h1>{esc(record['filename'])}</h1><p class="technical">{human_size(record['size_bytes'])} · {esc(record['extraction_state'])} · {esc(record['parser'])}</p><p class="technical path"><code>{esc(record['source_path'])}</code></p><p><a class="button button--quiet" href="{esc(url('/metadata-file',metadata=record['metadata_id']))}" target="_blank" rel="noreferrer">Open original metadata file</a></p><dl><dt>Indexed SHA-256</dt><dd class="technical">{esc(record['source_sha256'])}</dd><dt>Matched TFA identifiers</dt><dd>{esc(record['matched_tfa_inventory_ids']) or '—'}</dd><dt>Evidence</dt><dd>{esc(record['evidence_basis']) or '—'}</dd><dt>Evidence excerpt</dt><dd class="description">{esc(record['evidence_excerpt']) or '—'}</dd></dl></div></article><article class="candidate"><div><span class="badge">current proposal</span><h2>{esc(record['category'])}</h2><p class="technical path"><code>{esc(record['final_relative_path'])}</code></p><dl><dt>Condition</dt><dd>{esc(record['condition'])}</dd><dt>Selection basis</dt><dd>{esc(record['selection_basis'])}</dd></dl><p>This proposal does not override authoritative museum-digital metadata and is not yet accession.</p></div></article></section><form method="post" action="/metadata-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="metadata_id" value="{esc(record['metadata_id'])}"><section class="panel decision"><label>Review note<textarea name="note">{esc(record['review_note'])}</textarea></label><p>Decide only whether this file belongs in the TFA metadata/provenance package.</p><div class="buttons"><button class="button" name="action" value="include_metadata_evidence">Include metadata evidence</button><button class="button button--quiet" name="action" value="exclude_metadata_evidence">Exclude / cross-unit</button><button class="button button--quiet" name="action" value="unresolved">Leave unresolved</button>{f'<a class="button button--quiet" href="{esc(url("/metadata-record",metadata=skip_key))}">Skip without saving</a>' if skip_key else ''}</div></section></form>""")

    def metadata_file(self) -> None:
        metadata_id = self.parameters().get("metadata", "")
        path = self.server.review.guarded_metadata_path(metadata_id)
        self.send_bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def page_document_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "open")
        if state not in {"open", "include", "exclude", "unresolved", "all"}:
            raise WorkbenchError("unknown document review filter")
        query = p.get("q", "").casefold().strip()
        all_records = self.server.review.document_records()
        wanted = {
            "open": "unreviewed",
            "include": "include_tfa_documentation",
            "exclude": "exclude_from_tfa_documentation",
            "unresolved": "unresolved",
        }
        records = all_records
        if state != "all":
            records = [row for row in records if row["review_state"] == wanted[state]]
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row.get(key, "")) for key in (
                        "filename", "source_path", "evidence_excerpt", "matched_tfa_inventory_ids",
                        "classification", "extension", "parser",
                    )
                ).casefold()
            ]
        page_number = max(1, int(p.get("page", "1")))
        page_size = 40
        start = (page_number - 1) * page_size
        shown = records[start:start + page_size]
        cards = []
        for row in shown:
            badge = "critical" if row["extraction_state"] == "error" else (
                "selected" if row["review_state"] == "include_tfa_documentation" else "warning"
            )
            cards.append(
                f"""<article class="record-card"><div class="empty-preview">{esc(row['extension'].upper().lstrip('.'))}</div><div><span class="badge badge--{badge}">{esc(row['review_state'])}</span><span class="badge">{esc(row['evidence_confidence'])}</span><h2><a href="{url('/document-record',document=row['document_id'])}">{esc(row['filename'])}</a></h2><p>{esc(row['classification'])} · {human_size(row['size_bytes'])}</p><p class="technical">{esc(row['extraction_state'])} · {esc(row['parser'])} · {esc(row['exact_copy_count'])} exact copy/copies</p><p class="technical path"><code>{esc(row['source_path'])}</code></p></div></article>"""
            )
        counts = {name: sum(row["review_state"] == value for row in all_records) for name, value in wanted.items()}
        previous = url("/documents", state=state, q=p.get("q", ""), page=page_number - 1) if page_number > 1 else ""
        following = url("/documents", state=state, q=p.get("q", ""), page=page_number + 1) if start + page_size < len(records) else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {len(records)} matching records</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Ambiguous TFA documentation",
            f"""{notice}<section class="notice"><strong>Human documentation review required.</strong><p>This queue contains only records left unresolved after full-content TFA scope review. Clear TFA documents, general WF history, institution-wide inventories, other projects, and metadata have already been separated. Decisions do not rebuild the stage, alter the published unit, move files, or authorize deletion.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>review scope</div><div class="panel metric"><strong>{counts['open']}</strong>unreviewed</div><div class="panel metric"><strong>{counts['include']}</strong>include</div><div class="panel metric"><strong>{counts['exclude']}</strong>exclude</div><div class="panel metric"><strong>{counts['unresolved']}</strong>unresolved</div></section><form class="panel filters" method="get" action="/documents"><label>State<select name="state"><option value="open"{" selected" if state=='open' else ''}>Unreviewed</option><option value="include"{" selected" if state=='include' else ''}>Include in TFA</option><option value="exclude"{" selected" if state=='exclude' else ''}>Exclude from TFA</option><option value="unresolved"{" selected" if state=='unresolved' else ''}>Unresolved</option><option value="all"{" selected" if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="filename, path, evidence, TFA id"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching document records.</p>'}</section>{pagination}""",
        )

    def page_document_record(self) -> None:
        p = self.parameters()
        record = self.server.review.document_record(p.get("document", ""))
        all_records = self.server.review.document_records()
        keys = [row["document_id"] for row in all_records]
        index = keys.index(record["document_id"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_document_open(record["document_id"])
        nav = f"<nav class='nav'>{f'<a href={esc(url('/document-record',document=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/documents'>Document queue</a>{f'<a href={esc(url('/document-record',document=following))}>Next →</a>' if following else '<span></span>'}</nav>"
        current = ""
        if record["review_state"] != "unreviewed":
            current = f"<section class='notice notice--safe'><strong>Current decision: {esc(record['review_state'])}</strong><p>{esc(record['review_note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])}</p></section>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        extraction_warning = (
            "<section class='notice notice--critical'><strong>Content extraction failed.</strong><p>Review the original file and provenance; exact source bytes remain preserved.</p></section>"
            if record["extraction_state"] == "error" else ""
        )
        self.page(
            record["filename"],
            f"""{nav}{notice}{extraction_warning}{current}<section class="candidate-grid"><article class="candidate"><div><span class="badge badge--warning">{esc(record['evidence_confidence'])}</span><span class="badge">{esc(record['extension'])}</span><h1>{esc(record['filename'])}</h1><p class="technical">{human_size(record['size_bytes'])} · {esc(record['extraction_state'])} · {esc(record['parser'])}</p><p class="technical path"><code>{esc(record['source_path'])}</code></p><p><a class="button button--quiet" href="{esc(url('/document-file',document=record['document_id']))}" target="_blank" rel="noreferrer">Open original document</a></p><dl><dt>Indexed SHA-256</dt><dd class="technical">{esc(record['content_sha256'])}</dd><dt>Matched TFA identifiers</dt><dd>{esc(record['matched_tfa_inventory_ids']) or '—'}</dd><dt>Evidence</dt><dd>{esc(record['evidence_basis']) or '—'}</dd><dt>Evidence excerpt</dt><dd class="description">{esc(record['evidence_excerpt']) or '—'}</dd><dt>Exact-copy paths</dt><dd>{esc(record['exact_copy_count'])}</dd></dl></div></article><article class="candidate"><div><span class="badge">current proposal</span><h2>{esc(record['classification'])}</h2><p class="technical path"><code>{esc(record['final_relative_path']) or 'not proposed'}</code></p><dl><dt>Condition</dt><dd>{esc(record['condition'])}</dd><dt>Selection basis</dt><dd>{esc(record['selection_basis'])}</dd></dl><p>This proposal is not a human decision and does not change the staged package.</p></div></article></section><form method="post" action="/document-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="document_id" value="{esc(record['document_id'])}"><section class="panel decision"><label>Review note<textarea name="note">{esc(record['review_note'])}</textarea></label><p>Choose only the document's relationship to the TFA unit. Exact copies remain non-deletable evidence.</p><div class="buttons"><button class="button" name="action" value="include_tfa_documentation">Include in TFA documentation</button><button class="button button--quiet" name="action" value="exclude_from_tfa_documentation">Exclude / cross-unit</button><button class="button button--quiet" name="action" value="unresolved">Leave unresolved</button>{f'<a class="button button--quiet" href="{esc(url("/document-record",document=skip_key))}">Skip without saving</a>' if skip_key else ''}</div></section></form>""",
        )

    def document_file(self) -> None:
        document_id = self.parameters().get("document", "")
        path = self.server.review.guarded_document_path(document_id)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_bytes(path.read_bytes(), content_type)

    def page_recovery_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "all")
        if state not in {"open", "resolved", "all"}:
            raise WorkbenchError("unknown recovery queue filter")
        query = p.get("q", "").casefold().strip()
        all_records = self.server.review.recovery_records()
        records = all_records
        if state == "open":
            records = [row for row in records if row["state"] == "needs_visual_choice"]
        elif state == "resolved":
            records = [row for row in records if row["state"] == "replacement_selected"]
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row.get(key, ""))
                    for key in ("inventory", "md_object_id", "title", "failed_source_path")
                ).casefold()
            ]
        cards = []
        for row in records:
            preview = (
                f'<img loading="lazy" src="{url("/image",candidate=row["preview_candidate_id"])}" alt="">'
                if row.get("preview_candidate_id") else "<div class='empty-preview'>No MD JPEG preview</div>"
            )
            resolved = row["state"] == "replacement_selected"
            cards.append(
                f"""<article class="record-card">{preview}<div><span class="badge badge--{'selected' if resolved else 'warning'}">{esc(row['state'])}</span><h2><a href="{url('/recovery-record',record=row['record_key'])}">{esc(row['inventory'])}</a></h2><p>{esc(row['title'])}</p><p class="technical">MD {esc(row['md_object_id'])} · {esc(row['valid_candidate_count'])} full-decode TIFF choices</p><p class="technical path">Failed Master: <code>{esc(row['failed_source_path'])}</code></p></div></article>"""
            )
        resolved_count = sum(row["state"] == "replacement_selected" for row in all_records)
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Ambiguous TIFF Master recovery",
            f"""{notice}<section class="notice"><strong>Human visual comparison required.</strong><p>Each object has two or three exact-inventory TIFFs that passed a complete ImageMagick RGBA decode. The previously selected Master failed accession decoding. Filenames route candidates to the MD object but do not choose the Master. Selecting one makes the alternatives redundant in the reconciliation record; it does not delete or move any source file and it does not modify the completed accession.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>ambiguous MD objects</div><div class="panel metric"><strong>{len(self.server.review._recovery_candidate_ids)}</strong>validated TIFF choices</div><div class="panel metric"><strong>{resolved_count}</strong>replacement choices recorded</div><div class="panel metric"><strong>{len(all_records)-resolved_count}</strong>still open</div></section><form class="panel filters" method="get" action="/recovery"><label>State<select name="state"><option value="open"{" selected" if state=='open' else ''}>Needs visual choice</option><option value="resolved"{" selected" if state=='resolved' else ''}>Replacement selected</option><option value="all"{" selected" if state=='all' else ''}>All</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="inventory, MD id, title, path"></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching recovery records.</p>'}</section>""",
        )

    def page_recovery_record(self) -> None:
        p = self.parameters()
        record = self.server.review.recovery_record(p.get("record", ""))
        all_records = self.server.review.recovery_records()
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_recovery_open(record["record_key"])
        previous_link = (
            f'<a href="{esc(url("/recovery-record",record=previous))}">← Previous</a>'
            if previous else "<span></span>"
        )
        following_link = (
            f'<a href="{esc(url("/recovery-record",record=following))}">Next →</a>'
            if following else "<span></span>"
        )
        nav = f"<nav class='nav'>{previous_link}<a href='/recovery'>Recovery queue</a>{following_link}</nav>"
        md_preview = (
            f'<img loading="lazy" src="{url("/image",candidate=record["preview_candidate_id"])}" alt="{esc(record["inventory"])}">'
            if record.get("preview_candidate_id") else "<div class='empty-preview'>No MD JPEG preview</div>"
        )
        current_selected = int(record["selected_candidate_id"] or 0)
        candidate_cards = []
        for candidate in record["candidates"]:
            candidate_id = int(candidate["candidate_id"])
            selected = record["state"] == "replacement_selected" and current_selected == candidate_id
            technical = self.server.review.technical(candidate)
            is_positive = "positive" in str(candidate["variant_label"]).casefold()
            polarity = "POSITIVE" if is_positive else "NEGATIVE"
            polarity_badge = "selected" if is_positive else "critical"
            candidate_cards.append(
                f"""<article class="candidate"><img loading="lazy" src="{url('/recovery-image',candidate=candidate_id)}" alt="{esc(candidate['basename'])}"><div><span class="badge badge--{polarity_badge}">{polarity}</span><span class="badge badge--selected">full decode passed</span><span class="badge">{esc(candidate['variant_label'])}</span>{'<span class="badge badge--selected">current replacement choice</span>' if selected else ''}<h3>{esc(candidate['basename'])}</h3><p class="technical">{human_size(candidate['size_bytes'])} · {esc(technical['dimensions'])} · {esc(technical['format'])}</p><p class="technical path"><code>{esc(candidate['source_path'])}</code></p><label class="choice"><input type="radio" name="selected_candidate_id" value="{candidate_id}"{" checked" if selected else ""}>Select this {polarity} TIFF as the single Master</label></div></article>"""
            )
        current = (
            f"<section class='notice notice--safe'><strong>Replacement choice recorded.</strong><p>{esc(record.get('note',''))} · {esc(record.get('decided_by',''))} · {esc(record.get('decided_at',''))}</p></section>"
            if record["state"] == "replacement_selected" else ""
        )
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            record["inventory"],
            f"""{nav}{notice}<section class="candidate-grid"><article class="candidate">{md_preview}<div><span class="badge">museum-digital reference</span><h1>{esc(record['inventory'])}</h1><p><a href="{esc(record['md_url'])}" target="_blank" rel="noreferrer">museum-digital object {esc(record['md_object_id'])}</a></p><h2>{esc(record['title'])}</h2><p class="description">{esc(record['description'])}</p><p>{esc(record['subdivisions'])}</p></div></article><article class="candidate"><div><span class="badge badge--critical">previous selected TIFF failed decode</span><h2>{esc(Path(record['failed_source_path']).name)}</h2><p class="technical path"><code>{esc(record['failed_source_path'])}</code></p><p class="technical">{esc(record['accession_error'])}</p></div></article></section>{current}<form method="post" action="/recovery-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record['record_key'])}"><section class="candidate-grid">{''.join(candidate_cards)}</section><section class="panel decision"><label>Decision note (optional)<textarea name="note">{esc(record.get('note',''))}</textarea></label><p>Record one visually matching TIFF as Master. Every other candidate becomes redundant evidence; no source deletion is authorized. Skipping writes nothing.</p><div class="buttons"><button class="button" name="action" value="select_master">Record selected Master</button><button class="button button--quiet" name="action" value="unresolved">Leave unresolved</button>{f'<a class="button button--quiet" href="{esc(url("/recovery-record",record=skip_key))}">Skip without saving</a>' if skip_key else ''}</div></section></form>""",
        )

    def recovery_image(self) -> None:
        candidate_id = int(self.parameters().get("candidate", "0"))
        if candidate_id not in self.server.review._recovery_candidate_ids:
            raise WorkbenchError("candidate is outside the validated recovery comparison scope")
        path = self.server.review.guarded_candidate_path(candidate_id)
        environment = dict(os.environ)
        environment.update({"MAGICK_TEMPORARY_PATH": str(RECOVERY_TMP), "TMPDIR": str(RECOVERY_TMP)})
        completed = subprocess.run(
            ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
            check=True,
            capture_output=True,
            env=environment,
            timeout=90,
        )
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded recovery preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def page_local_queue(self) -> None:
        p = self.parameters()
        history = p.get("history") == "1"
        state = p.get("state", "all") if history else "open"
        if state not in {"open", "selected", "redundant", "noise", "loss", "all"}:
            raise WorkbenchError("unknown orphan queue filter")
        query = p.get("q", "").casefold().strip()
        per_page_raw = p.get("per_page", "60")
        per_page = len(self.server.review._orphan_groups) if per_page_raw == "all" else max(1, min(120, int(per_page_raw)))
        page_number = max(1, int(p.get("page", "1")))
        all_records = self.server.review.orphan_review_records()
        records = all_records
        if state == "open":
            records = [row for row in records if row["review_state"] in {"unreviewed", "unresolved"}]
        elif state == "selected":
            records = [row for row in records if row["review_state"] == "selected_master"]
        elif state == "redundant":
            records = [row for row in records if row["review_state"] == "redundant"]
        elif state == "noise":
            records = [row for row in records if row["review_state"] == "noise"]
        elif state == "loss":
            records = [row for row in records if row["review_state"] == "genuine_loss"]
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row.get(key, "")) for key in (
                        "inventory", "inventory_evidence", "hold_reasons", "filename", "source_path"
                    )
                ).casefold()
            ]
        total = len(records)
        start = (page_number - 1) * per_page
        visible = records[start : start + per_page]
        cards = []
        for row in visible:
            badge = "selected" if row["review_state"] == "selected_master" else "critical" if row["review_state"] == "noise" else "warning"
            preview = (
                f'<img loading="lazy" src="{url("/visual-image",asset=row["asset_id"])}" alt="">'
                if int(row["decodable_path_count"]) > 0
                else "<div class='empty-preview'>No decodable image</div>"
            )
            cards.append(
                f"""<article class="record-card">{preview}<div><span class="badge badge--{badge}">{esc(row['review_state'])}</span><h2><a href="{url('/local-record',record=row['record_key'],history='1' if history else '')}">{esc(row['inventory_evidence'] or row['inventory'])}</a></h2><p class="technical">{esc(row['visual_group_count'])} visual group(s) · {esc(row['path_count'])} image path(s)</p><p>{esc(row['recheck_note'] or row['hold_reasons'])}</p><p>{esc(row['filename'])}</p><p class="technical path"><code>{esc(row['source_path'])}</code></p></div></article>"""
            )
        previous = url("/holds", **{**p, "page": page_number - 1}) if page_number > 1 else ""
        following = url("/holds", **{**p, "page": page_number + 1}) if start + per_page < total else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {total} candidates</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        selected = sum(row["review_state"] == "selected_master" for row in all_records)
        redundant = sum(row["review_state"] == "redundant" for row in all_records)
        noise = sum(row["review_state"] == "noise" for row in all_records)
        genuine_loss = sum(row["review_state"] == "genuine_loss" for row in all_records)
        open_count = len(all_records) - selected - redundant - noise - genuine_loss
        path_count = sum(int(row["path_count"]) for row in all_records)
        group_count = sum(int(row["visual_group_count"]) for row in all_records)
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        if self.server.review.reference_photo_hold_workbench_receipt is not None:
            scope_heading = "Remaining reference-photo visual holds requiring a human decision."
            scope_copy = (
                "Each held image is an independent visual candidate. Existing MD Masters, earlier human selections, "
                "and current local Masters appear only as comparison evidence and cannot be selected from this queue. "
                "Select one decodable held TIFF as a new Master, dismiss it as noise, or leave it unresolved. "
                "No decision deletes or changes source files."
            )
            candidate_label = "held visual objects"
        elif self.server.review.reference_photo_receipt is not None:
            scope_heading = "Reference-photo visual objects requiring a decision."
            scope_copy = (
                "Clear reference-photo groups already have one selected TIFF Master; all other related TIFFs are recorded as redundant. "
                "Available metadata is attached to accession candidates and photos without metadata are deferred to the next pass. "
                "Each remaining visual signature is presented as one independent object; filename collisions do not merge different images. "
                "No decision deletes or changes source files."
            )
            candidate_label = "visual objects"
        else:
            scope_heading = "201-candidate TFA orphan scope."
            scope_copy = (
                "Each record is routed by its full TFA filename token only after excluding inventories already present in the current 5,282-record MD scope. "
                "Visual groups and paths are shown together. Select exactly one TIFF Master, which makes every other path for that orphan redundant, "
                "or dismiss the entire orphan candidate as noise. No decision deletes or changes source files."
            )
            candidate_label = "orphan candidates"
        if history:
            scope_heading = "Hold decision history"
            scope_copy = "Saved decisions remain available here. Use active review for unresolved files."
        else:
            scope_heading = "Unresolved hold review"
            scope_copy = (
                "Resolved files are hidden. The redundancy recheck keeps different photographs, "
                "distinct annotations and files without a proven archive counterpart open. "
                "Previous/Next and Skip visit only unresolved holds."
            )
        view_controls = (
            '<input type="hidden" name="history" value="1">'
            if history else ''
        )
        view_link = (
            '<a class="button button--quiet" href="/holds">Back to active review</a>'
            if history else
            '<a class="button button--quiet" href="/holds?history=1&amp;state=all">Decision history</a>'
        )
        state_options = ''.join(
            f'<option value="{value}"{" selected" if state == value else ""}>{label}</option>'
            for value, label in (
                [("open", "Unresolved")]
                if not history else
                [("all", "All history"), ("open", "Unresolved"), ("selected", "Master selected"),
                 ("redundant", "Redundant"), ("noise", "Noise"), ("loss", "Genuine loss")]
            )
        )
        self.page(
            "TFA hold review",
            f"""{notice}<section class="notice notice--safe"><strong>{esc(scope_heading)}</strong><p>{esc(scope_copy)}</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>{esc(candidate_label)}</div><div class="panel metric"><strong>{open_count}</strong>open</div><div class="panel metric"><strong>{selected}</strong>Masters selected</div><div class="panel metric"><strong>{redundant}</strong>marked redundant</div><div class="panel metric"><strong>{noise}</strong>dismissed as noise</div><div class="panel metric"><strong>{genuine_loss}</strong>genuine loss</div><div class="panel metric"><strong>{group_count}</strong>visual groups</div><div class="panel metric"><strong>{path_count}</strong>image paths</div></section><form class="panel filters" method="get">{view_controls}<label>State<select name="state">{state_options}</select></label><label>Search inventory / path<input type="search" name="q" value="{esc(p.get('q',''))}"></label><label>Per page<select name="per_page"><option value="60"{" selected" if per_page_raw=='60' else ''}>60</option><option value="120"{" selected" if per_page_raw=='120' else ''}>120</option><option value="all"{" selected" if per_page_raw=='all' else ''}>All</option></select></label><button class="button">Filter</button>{view_link}<a class="button button--quiet" href="/visual-evidence">Open full visual evidence</a></form><section class="records">{''.join(cards) or '<p>No matching hold candidates.</p>'}</section>{pagination}""",
        )

    def page_visual_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "new")
        if state not in {"new", "redundant", "errors"}:
            raise WorkbenchError("unknown visual queue filter")
        query = p.get("q", "").casefold().strip()
        per_page_raw = p.get("per_page", "60")
        per_page = 100_000 if per_page_raw == "all" else max(1, min(120, int(per_page_raw)))
        page_number = max(1, int(p.get("page", "1")))
        records = self.server.review.visual_records(state)
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row.get(key, "")) for key in ("filename", "source_path", "detail")
                ).casefold()
            ]
        total = len(records)
        start = (page_number - 1) * per_page
        visible = records[start : start + per_page]
        cards = []
        for row in visible:
            preview = (
                "<div class='empty-preview'>Decode error</div>"
                if state == "errors"
                else f'<img loading="lazy" src="{url("/visual-image",asset=row["asset_id"])}" alt="">'
            )
            badge = "selected" if state == "new" else "warning" if state == "redundant" else "critical"
            cards.append(
                f"""<article class="record-card">{preview}<div><span class="badge badge--{badge}">{esc(row['state'])}</span><h2><a href="{url('/local-record',record=row['record_key'])}">{esc(row['filename'])}</a></h2><p class="technical">{esc(row['width_px'])}×{esc(row['height_px'])} · {human_size(row['size_bytes'])} · {esc(row['path_count'])} path(s)</p><p>{esc(row['detail'])}</p><p class="technical path"><code>{esc(row['source_path'])}</code></p></div></article>"""
            )
        previous = url("/visual-evidence", **{**p, "page": page_number - 1}) if page_number > 1 else ""
        following = url("/visual-evidence", **{**p, "page": page_number + 1}) if start + per_page < total else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {total} records</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Visual-only TFA reconciliation",
            f"""{notice}<section class="notice notice--safe"><strong>Visual classification evidence.</strong> The comparison authority is each reconciled MD object's selected TIFF Master. Metadata, filenames, inventory numbers, titles, and spreadsheet cells do not create visual matches. This full WF evidence view remains read-only; use <a href="/local">Orphan review</a> for the bounded 201 TFA candidates and human decisions. No deletion is authorized.</section><section class="summary"><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['genuine_new_tiff_candidate_paths'])}</strong>new TIFF paths</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['genuine_new_tiff_visual_groups'])}</strong>new visual groups</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['redundant_image_paths'])}</strong>MD-matched redundant</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['crop_verified_redundant_paths'])}</strong>crop-verified redundant</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['visual_error_paths'])}</strong>decode-error holds</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['selected_md_tiff_masters'])}</strong>reconciled MD TIFF Masters</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['institution_selected_tiff_masters'])}</strong>institution visual Master bindings</div><div class="panel metric"><strong>{esc(self.server.review.visual_receipt['institution_master_holds'])}</strong>institution Master holds</div></section><form class="panel filters" method="get"><label>View<select name="state"><option value="new"{" selected" if state=='new' else ''}>New TIFF candidates</option><option value="redundant"{" selected" if state=='redundant' else ''}>MD-matched redundant</option><option value="errors"{" selected" if state=='errors' else ''}>Decode-error holds</option></select></label><label>Search path / filename<input type="search" name="q" value="{esc(p.get('q',''))}"></label><label>Per page<select name="per_page"><option>60</option><option value="120"{" selected" if per_page_raw=='120' else ''}>120</option><option value="all"{" selected" if per_page_raw=='all' else ''}>All</option></select></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching visual records.</p>'}</section>{pagination}""",
        )

    def page_local_record(self) -> None:
        p = self.parameters()
        requested = p.get("record", "")
        if requested.startswith("orphan:"):
            if requested in self.server.review._orphan_groups:
                self.page_orphan_record(requested)
                return
            resolved_rows = self.server.review._reference_photo_resolved_visual.get(
                requested.removeprefix("orphan:")
            )
            if resolved_rows:
                selected = next(
                    (
                        row for row in resolved_rows
                        if row["preservation_status"] == "selected_master"
                    ),
                    None,
                )
                if selected is not None:
                    evidence = "".join(
                        f"<li><strong>{esc(row['preservation_status'])}</strong> · <code>{esc(row['source_path'])}</code></li>"
                        for row in sorted(resolved_rows, key=lambda item: (
                            item["preservation_status"] != "selected_master", item["source_path"]
                        ))
                    )
                    self.page(
                        selected["inventory"],
                        f"""<nav class='nav'><span></span><a href='/holds'>Visual-object review</a><span></span></nav><section class='notice notice--safe'><strong>Already resolved; removed from the workbench.</strong><p><code>{esc(selected['record_key'])}</code> has one selected TIFF Master. Its other copy paths are recorded as redundant; deletion is not authorized.</p></section><section class='panel'><h1>{esc(selected['inventory'])}</h1><ul>{evidence}</ul><p>Metadata remains deferred to the next pass where no authoritative metadata is available.</p></section>""",
                    )
                    return
            split_records = self.server.review._orphan_legacy_aliases.get(requested)
            if split_records:
                self.redirect(url(
                    "/local-record",
                    record=split_records[0],
                    notice=f"Former collision component split into {len(split_records)} independent visual objects",
                ))
                return
            self.page_orphan_record(requested)
            return
        if requested.startswith("local:"):
            label = requested.split(":", 1)[1]
            self.page(
                label,
                f"""<nav class='nav'><span></span><a href='/visual-evidence'>Visual evidence</a><span></span></nav><section class='notice notice--safe'><strong>Metadata-keyed local record pages are disabled in this phase.</strong><p>The current workbench is classified only by image content. The old record key <code>{esc(requested)}</code> is not used as matching evidence.</p></section><section class='panel'><h2>Find paths carrying this label</h2><div class='buttons'><a class='button' href='{esc(url('/visual-evidence',state='new',q=label))}'>Search new TIFF candidates</a><a class='button button--quiet' href='{esc(url('/visual-evidence',state='redundant',q=label))}'>Search MD-matched redundant</a><a class='button button--quiet' href='{esc(url('/visual-evidence',state='errors',q=label))}'>Search decode-error holds</a></div><p class='technical'>These searches are navigation only; the filename does not affect classification.</p></section>""",
            )
            return
        record = self.server.review.visual_record(requested)
        state = "new" if record["kind"] == "new" else record["kind"]
        all_records = self.server.review.visual_records(state)
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        nav = f"<nav class='nav'>{f'<a href={esc(url('/local-record',record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='{esc(url('/visual-evidence',state=state))}'>Visual evidence</a>{f'<a href={esc(url('/local-record',record=following))}>Next →</a>' if following else ''}</nav>"
        cards = []
        for row in record["rows"]:
            preview = (
                "<div class='empty-preview'>Decode error</div>"
                if record["kind"] == "errors"
                else f'<img loading="lazy" src="{url("/visual-image",asset=row["asset_id"])}" alt="{esc(row["filename"])}">'
            )
            if record["kind"] == "new":
                detail = f"""<span class="badge badge--selected">genuine new TIFF candidate</span><p>No selected-MD visual match under the audited policy. Exact-signature local TIFF copies remain together for later object and metadata reconciliation.</p><p class="technical">aHash {esc(row['ahash'])} · dHash {esc(row['dhash'])} · pHash {esc(row['phash'])}</p>"""
            elif record["kind"] == "redundant":
                evidence = (
                    f"crop-aware d/p {esc(row['dhash_distance'])}/{esc(row['phash_distance'])} · total {esc(row['distance_total'])}"
                    if row["disposition_basis"] == "selected_md_visual_crop_verified"
                    else f"a/d/p {esc(row['ahash_distance'])}/{esc(row['dhash_distance'])}/{esc(row['phash_distance'])} · total {esc(row['distance_total'])} · aspect delta {esc(row['aspect_ratio_delta'])}"
                )
                detail = f"""<span class="badge badge--warning">redundant to selected MD TIFF Master</span><p>Visual match to md:{esc(row['matched_md_object_ids'])} · {esc(row['matched_md_inventories'])}</p><p class="technical">{evidence}<br>Master: <code>{esc(row['selected_master_paths'])}</code><br>Drain: proposed redundant · deletion authorized: false</p>"""
            else:
                detail = f"""<span class="badge badge--critical">visual decode-error hold</span><p>This path was not classified as redundant or genuinely new.</p><p class="technical">{esc(row['error'])}</p>"""
            cards.append(f"""<article class="candidate">{preview}<div>{detail}<h3>{esc(row['filename'])}</h3><p class="technical">{human_size(row['size_bytes'])} · {esc(row['width_px'])}×{esc(row['height_px'])} · {esc(row['extension'])}</p><p class="technical path"><code>{esc(row['source_path'])}</code></p></div></article>""")
        self.page(
            str(record["record_key"]),
            f"""{nav}<section class="notice"><strong>Visual-only phase.</strong> No metadata reconciliation or accession decision is available on this page. Source files remain read-only and deletion is not authorized.</section><section class="candidate-grid">{''.join(cards)}</section>""",
        )

    def page_orphan_record(self, record_key: str) -> None:
        p = self.parameters()
        record = self.server.review.orphan_record(record_key)
        known_md = record_key.removeprefix("orphan:") if record_key.startswith("orphan:md:") else ""
        boundary_copy = (
            f"Known museum-digital record {esc(known_md)}. Archive coverage is unconfirmed. "
            "The source copies and existing TIFF candidates are grouped from saved visual evidence for review; "
            "they are not asserted to have identical signatures."
            if known_md else
            "The image is the proposed object boundary; the inventory string is routing evidence only."
        )
        selection_copy = (
            "the chosen TIFF is recorded for this existing museum-digital object; it does not create a new local identity or accession files"
            if known_md else "the held TIFF becomes the single Master for a separate local object"
        )
        md_link = (
            f'<p><a href="https://berlin.museum-digital.de/object/{esc(known_md.removeprefix("md:"))}" target="_blank" rel="noreferrer">museum-digital {esc(known_md)}</a></p>'
            if known_md else ""
        )
        records = self.server.review.orphan_review_records()
        review_record = next(row for row in records if row["record_key"] == record_key)
        if review_record["review_state"] not in {"unreviewed", "unresolved"}:
            if p.get("history") != "1":
                self.redirect(url("/holds", notice="Resolved hold hidden from active review. See Decision history for its saved decision."))
                return
            evidence = ''.join(f'<li><code>{esc(row["source_path"])}</code></li>' for row in record["rows"])
            self.page(
                "Saved hold decision",
                f"<nav class='nav'><a href='/holds'>Active review</a><a href='/holds?history=1&amp;state=all'>Decision history</a></nav>"
                f"<section class='panel'><h1>{esc(record['inventory_evidence'] or record['inventory'])}</h1>"
                f"<p>Saved decision: <strong>{esc(record['state'])}</strong></p>"
                f"<p>{esc(record['note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])}</p>"
                f"<ul>{evidence}</ul><p>This resolved hold is hidden from active review. No deletion is authorized.</p></section>",
            )
            return
        records = [row for row in records if row["review_state"] in {"unreviewed", "unresolved"}]
        keys = [row["record_key"] for row in records]
        index = keys.index(record_key)
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = self.next_local_open(record_key)
        nav = f"<nav class='nav'>{f'<a href={esc(url('/local-record',record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/holds'>Hold queue</a>{f'<a href={esc(url('/local-record',record=following))}>Next →</a>' if following else ''}</nav>"
        current = ""
        if record["state"] != "unreviewed":
            style = (
                "notice--safe" if record["state"] == "selected_master"
                else "notice--critical" if record["state"] in {"noise", "genuine_loss"}
                else ""
            )
            current = f"<section class='notice {style}'><strong>Current decision: {esc(record['state'])}</strong><p>{esc(record['note']) or 'No note'} · {esc(record['decided_by'])} · {esc(record['decided_at'])}</p></section>"
        if review_record["recheck_note"]:
            current = (
                f"<section class='notice'><strong>Unresolved after redundancy recheck</strong>"
                f"<p>{esc(review_record['recheck_note'])}</p>"
                f"<p>Previous saved decision: {esc(record['state'])}. This file remains outside the drain list.</p></section>"
            )
            record = {**record, "state": "unresolved"}
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in record["rows"]:
            grouped.setdefault(row["visual_group_id"], []).append(row)
        context_cards = []
        for group_id in sorted(grouped):
            for context in self.server.review.hold_contexts(group_id):
                preview_url = (
                    context["image_url"]
                    if context.get("image_url")
                    else url("/hold-context-image", context=context["context_id"])
                )
                context_cards.append(
                    f"""<article class="candidate"><img loading="lazy" src="{esc(preview_url)}" alt="{esc(context['filename'])}"><div><span class="badge badge--selected">comparison only · not a candidate</span><h3>{esc(context['context_label'])}</h3><p class="technical">{human_size(context['size_bytes'])} · {esc(context['width_px'])}×{esc(context['height_px'])} · {esc(context['extension'])}</p><p class="technical path"><code>{esc(context.get('source_path') or context.get('image_url'))}</code></p><p>This existing selection remains unchanged. Use it only to decide whether the held image is a separate object, redundant/noise, or still unresolved.</p></div></article>"""
                )
        context_section = (
            "<section class='panel'><h2>Existing selection for comparison</h2>"
            "<p>These images are authoritative context and are not selectable in this review.</p>"
            f"<section class='candidate-grid'>{''.join(context_cards)}</section></section>"
            if context_cards else ""
        )
        group_sections = []
        for number, (group_id, rows) in enumerate(sorted(grouped.items()), 1):
            cards = []
            for row in rows:
                selected = row["asset_id"] == record["selected_asset_id"]
                decodable = (
                    bool(row.get("ahash"))
                    and int(row.get("width_px") or 0) > 0
                    and int(row.get("height_px") or 0) > 0
                )
                disposition = orphan_candidate_disposition(
                    record["state"], row["asset_id"], record["selected_asset_id"],
                )
                disposition_badge = (
                    "selected" if disposition == "selected_master"
                    else "critical" if disposition in {"noise", "genuine_loss"}
                    else "warning"
                )
                preview = (
                    f'<img loading="lazy" src="{url("/visual-image",asset=row["asset_id"])}" alt="{esc(row["filename"])}">'
                    if decodable else "<div class='empty-preview'>No decodable image survives</div>"
                )
                choice = (
                    f"""<label class="choice"><input type="radio" name="selected_asset_id" value="{esc(row['asset_id'])}"{" checked" if selected else ""}>Select this TIFF as the single Master</label>"""
                    if decodable and row["extension"].lower().lstrip(".") in {"tif", "tiff"}
                    else "<p class='technical'>Image copy for comparison; only a decodable TIFF can be selected here.</p>"
                    if decodable
                    else "<p class='notice notice--critical'>This path cannot be selected as a Master because no decoder can read it.</p>"
                )
                cards.append(
                    f"""<article class="candidate">{preview}<div><span class="badge">visual group {number}</span><span class="badge badge--{disposition_badge}">{esc(disposition)}</span><h3>{esc(row['filename'])}</h3><p><strong>Inventory evidence:</strong> {esc(row.get('normalized_inventory') or row.get('inventory_members'))}</p><p><strong>Hold reason:</strong> {esc(row.get('classification_basis'))}</p><p class="technical">{human_size(row['size_bytes'])} · {esc(row['width_px'])}×{esc(row['height_px'])} · {esc(row['extension'])}</p><p class="technical path"><code>{esc(row['source_path'])}</code></p><p class="technical">aHash {esc(row['ahash'])} · dHash {esc(row['dhash'])} · pHash {esc(row['phash'])}</p>{choice}</div></article>"""
                )
            group_sections.append(
                f"<section class='panel'><h2>Visual group {number}</h2><p>{len(rows)} image path(s) · <code>{esc(group_id)}</code></p><section class='candidate-grid'>{''.join(cards)}</section></section>"
            )
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            record["inventory_evidence"] or record["inventory"],
            f"""{nav}{notice}<section class="panel"><span class="badge">held visual object candidate</span><h1>{esc(record['inventory_evidence'] or record['inventory'])}</h1><p>{esc(record['path_count'])} image path(s). {boundary_copy}</p><p class="technical"><code>{esc(record_key)}</code></p>{md_link}</section>{current}{context_section}<form method="post" action="/local-decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record_key)}">{''.join(group_sections)}<section class="panel decision"><label>Decision note (optional)<textarea name="note">{esc(record['note'])}</textarea></label><p><strong>Select Master:</strong> {selection_copy}. <strong>Mark redundant:</strong> it is recorded as redundant to the existing comparison Master. <strong>Dismiss as noise:</strong> it is not an archival object. <strong>Leave unresolved:</strong> no decision is made. Existing comparison Masters remain unchanged, and no action authorizes deletion.</p><div class="buttons"><button class="button" name="action" value="select_master"{" disabled" if int(record['decodable_path_count']) == 0 else ""}>Record selected Master</button><button class="button button--quiet" name="action" value="mark_redundant"{" disabled" if not context_cards else ""}>Mark redundant to existing Master</button><button class="button button--critical" name="action" value="dismiss_noise">Dismiss object as noise</button><button class="button button--quiet" name="action" value="unresolved">Leave unresolved</button>{f'<a class="button button--quiet" href="{esc(url("/local-record",record=skip_key))}">Skip without saving</a>' if skip_key else ''}</div></section></form>""",
        )

    def local_image(self) -> None:
        candidate_id = int(self.parameters().get("candidate", "0"))
        candidate = self.server.review.candidate(candidate_id)
        if not str(candidate["record_key"]).startswith("local:"):
            raise WorkbenchError("candidate is outside the local-only review scope")
        path = self.server.review.guarded_candidate_path(candidate_id)
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise WorkbenchError("candidate is not local image evidence")
        completed = subprocess.run(
            ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
            check=True,
            capture_output=True,
            timeout=45,
        )
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded local preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def visual_image(self) -> None:
        asset = self.server.review.visual_asset(self.parameters().get("asset", ""))
        path = Path(str(asset["source_path"])).resolve(strict=True)
        try:
            path.relative_to(SOURCE_ROOT.resolve(strict=True))
        except ValueError as error:
            raise WorkbenchError("visual asset escaped the protected source root") from error
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            raise WorkbenchError("visual asset is not a supported image")
        if not self.server.preview_slots.acquire(timeout=120):
            raise WorkbenchError("visual preview queue is busy; retry this image")
        try:
            completed = subprocess.run(
                ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
                check=True,
                capture_output=True,
                timeout=45,
            )
        finally:
            self.server.preview_slots.release()
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded visual preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def hold_context_image(self) -> None:
        path = self.server.review.guarded_hold_context(self.parameters().get("context", ""))
        if not self.server.preview_slots.acquire(timeout=120):
            raise WorkbenchError("hold comparison preview queue is busy; retry this image")
        try:
            completed = subprocess.run(
                ["magick", f"{path}[0]", "-auto-orient", "-thumbnail", "1200x1200>", "-strip", "jpg:-"],
                check=True,
                capture_output=True,
                timeout=45,
            )
        finally:
            self.server.preview_slots.release()
        if not completed.stdout or len(completed.stdout) > 20_000_000:
            raise WorkbenchError("bounded hold comparison preview generation failed")
        self.send_bytes(completed.stdout, "image/jpeg")

    def page_md_catalog(self) -> None:
        p = self.parameters()
        subdivision = p.get("subdivision", "")
        readiness = p.get("readiness", "all")
        query = p.get("q", "").casefold().strip()
        per_page_raw = p.get("per_page", "60")
        per_page = 5282 if per_page_raw == "all" else max(1, min(120, int(per_page_raw)))
        page_number = max(1, int(p.get("page", "1")))
        all_records = self.server.review.md_records()
        records = all_records
        if subdivision:
            collection_id = subdivision.rsplit(":", 1)[-1]
            records = [
                row for row in records
                if collection_id in {value.strip() for value in str(row["subdivision_ids"]).split(";")}
            ]
        if readiness == "ready":
            records = [row for row in records if row["accession_state"] == "ready_for_accession"]
        elif readiness == "hold":
            records = [row for row in records if row["accession_state"] != "ready_for_accession"]
        elif readiness != "all":
            raise WorkbenchError(f"unknown accession readiness filter: {readiness}")
        if query:
            search_fields = (
                "inventory", "md_object_id", "title", "description", "date_text",
                "object_type", "material", "places", "tags", "creator", "subdivisions",
            )
            records = [
                row for row in records
                if query in " ".join(str(row.get(field, "")) for field in search_fields).casefold()
            ]
        total = len(records)
        start = (page_number - 1) * per_page
        page_rows = records[start : start + per_page]
        cards = []
        for row in page_rows:
            preview = (
                f'<img loading="lazy" src="{url("/image", candidate=row["preview_candidate_id"])}" alt="">'
                if row["preview_candidate_id"] else "<div class='empty-preview'>No JPEG preview</div>"
            )
            is_ready = row["accession_state"] == "ready_for_accession"
            cards.append(
                f"""<article class="record-card">{preview}<div><span class="badge badge--{'selected' if is_ready else 'warning'}">{esc(row['accession_state'])}</span><h2><a href="{url('/md-record',record=row['record_key'])}">{esc(row['inventory'])}</a></h2><p>{esc(row['title'])}</p><p class="technical">MD {esc(row['md_object_id'])} · one selected Master</p><p>{esc(row['subdivisions'])}</p></div></article>"""
            )
        facet_options = "".join(
            f'<option value="{esc(row["subdivision_id"])}"{" selected" if row["subdivision_id"] == subdivision else ""}>{esc(row["label"])} ({esc(row["assigned_object_count"])})</option>'
            for row in self.server.review.md_subdivision_facets()
        )
        params = {key: value for key, value in p.items() if key != "page"}
        previous = url("/md", **{**params, "page": page_number - 1}) if page_number > 1 else ""
        following = url("/md", **{**params, "page": page_number + 1}) if start + per_page < total else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page_number} · {total} records</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        ready = sum(row["accession_state"] == "ready_for_accession" for row in all_records)
        holds = len(all_records) - ready
        self.page(
            "museum-digital catalogue",
            f"""<section class="notice notice--safe"><strong>museum-digital is the authoritative descriptive metadata source for this complete scope.</strong><p>The catalogue is metadata-reconciled and read-only here. Facets preserve current museum-digital collection memberships, including objects assigned to more than one subdivision.</p></section><section class="summary"><div class="panel metric"><strong>{len(all_records)}</strong>MD records reconciled</div><div class="panel metric"><strong>{ready}</strong>ready for accession</div><div class="panel metric"><strong>{holds}</strong>catalogue-duplicate hold</div><div class="panel metric"><strong>10,723</strong>facet memberships</div></section><form class="panel filters" method="get" action="/md"><label>Subdivision<select name="subdivision"><option value="">All TFA subdivisions</option>{facet_options}</select></label><label>Readiness<select name="readiness"><option value="all"{" selected" if readiness=='all' else ''}>All</option><option value="ready"{" selected" if readiness=='ready' else ''}>Ready for accession</option><option value="hold"{" selected" if readiness=='hold' else ''}>On hold</option></select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}" placeholder="inventory, title, tags, place…"></label><label>Per page<select name="per_page"><option value="60"{" selected" if per_page_raw=='60' else ''}>60</option><option value="120"{" selected" if per_page_raw=='120' else ''}>120</option><option value="all"{" selected" if per_page_raw=='all' else ''}>All</option></select></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching museum-digital records.</p>'}</section>{pagination}""",
        )

    def page_md_record(self) -> None:
        record = self.server.review.md_record(self.parameters().get("record", ""))
        all_records = self.server.review.md_records()
        keys = [row["record_key"] for row in all_records]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        nav = f"<nav class='nav'>{f'<a href={esc(url('/md-record',record=previous))}>← Previous</a>' if previous else '<span></span>'}<a href='/md'>MD catalogue</a>{f'<a href={esc(url('/md-record',record=following))}>Next →</a>' if following else ''}</nav>"
        preview = (
            f'<img loading="lazy" src="{url("/image", candidate=record["preview_candidate_id"])}" alt="{esc(record["inventory"])}">'
            if record["preview_candidate_id"] else "<div class='empty-preview'>No JPEG preview</div>"
        )
        memberships = "".join(
            f"<li><a href='{esc(url('/md',subdivision=row['subdivision_id']))}'>{esc(row['label'])}</a> <span class='technical'>({esc(row['subdivision_id'])})</span></li>"
            for row in record["memberships"]
        )
        ready = record["accession_state"] == "ready_for_accession"
        readiness = (
            "<section class='notice notice--safe'><strong>Master selected and metadata reconciled.</strong><p>One selected Master and authoritative museum-digital metadata are bound. The accession catalogue records publication state.</p></section>"
            if ready
            else f"<section class='notice'><strong>Accession hold: catalogue duplicate.</strong><p>{esc(record['accession_hold_reason'])}</p></section>"
        )
        self.page(
            record["inventory"],
            f"""{nav}{readiness}<section class="candidate-grid"><article class="candidate">{preview}<div><span class="badge badge--{'selected' if ready else 'warning'}">{esc(record['accession_state'])}</span><h1>{esc(record['inventory'])}</h1><p><a href="{esc(record['md_url'])}" target="_blank" rel="noreferrer">museum-digital object {esc(record['md_object_id'])}</a></p><h2>{esc(record['title'])}</h2><p>{esc(record['date_text']) or '—'} · {esc(record['object_type']) or '—'} · {esc(record['material']) or '—'} · {esc(record['dimensions']) or '—'}</p><p class="description">{esc(record['description']) or '—'}</p><dl><dt>Places</dt><dd>{esc(record['places']) or '—'}</dd><dt>Tags</dt><dd>{esc(record['tags']) or '—'}</dd><dt>Creator</dt><dd>{esc(record['creator']) or '—'}</dd><dt>Last updated in museum-digital</dt><dd>{esc(record['last_updated'])}</dd></dl><h3>museum-digital subdivisions</h3><ul>{memberships}</ul></div></article><article class="candidate"><div><span class="badge badge--selected">selected Master</span><h2>{esc(Path(record['selected_master_path']).name)}</h2><p class="technical">{human_size(record['selected_master_size_bytes'])}</p><p class="technical path"><code>{esc(record['selected_master_path'])}</code></p><dl><dt>Decision</dt><dd>{esc(record['master_decision_state'])}</dd><dt>Decided by</dt><dd>{esc(record['master_decided_by'])} · {esc(record['master_decided_at'])}</dd><dt>Decision note</dt><dd>{esc(record['master_decision_note'])}</dd></dl></div></article></section>""",
        )

    def page_queue(self) -> None:
        p = self.parameters()
        state = p.get("state", "unresolved")
        category = p.get("category", "")
        subdivision = p.get("subdivision", "")
        query = p.get("q", "").casefold().strip()
        per_page_raw = p.get("per_page", "60")
        per_page = 190 if per_page_raw == "all" else max(1, min(120, int(per_page_raw)))
        page = max(1, int(p.get("page", "1")))
        records = self.server.review.records()
        all_records = records
        if state == "unresolved":
            records = [row for row in records if row["state"] != "selected_master"]
        elif state == "selected":
            records = [row for row in records if row["state"] == "selected_master"]
        if category:
            records = [row for row in records if row["exception_type"] == category]
        if subdivision:
            records = [row for row in records if subdivision in str(row["subdivision_ids"]).split("; ")]
        if query:
            records = [
                row for row in records
                if query in " ".join(
                    str(row.get(key, "")) for key in ("inventory", "md_object_id", "title", "description", "tags")
                ).casefold()
            ]
        total = len(records)
        start = (page - 1) * per_page
        records = records[start : start + per_page]
        selected_count = sum(row["state"] == "selected_master" for row in all_records)
        categories = sorted({row["exception_type"] for row in all_records})
        subdivisions = sorted(
            {
                (sid.strip(), label.strip())
                for row in all_records
                for sid, label in zip(str(row["subdivision_ids"]).split(";"), str(row["subdivisions"]).split(";"))
                if sid.strip()
            }
        )
        cards = []
        for row in records:
            preview = (
                f'<img loading="lazy" src="{url("/image", candidate=row["preview_candidate_id"])}" alt="">'
                if row["preview_candidate_id"] else "<div class='empty-preview'>No existing JPEG</div>"
            )
            status_badge = "selected" if row["state"] == "selected_master" else "warning"
            cards.append(
                f"""<article class="record-card">{preview}<div><span class="badge badge--{status_badge}">{esc(row['state'])}</span><span class="badge">{esc(row['exception_type'])}</span><h2><a href="{url('/record',record=row['record_key'])}">{esc(row['inventory'])}</a></h2><p>{esc(row['title'])}</p><p class="technical">MD {esc(row['md_object_id'])} · TIFF {esc(row['tiff_count'])} · candidates {esc(row['candidate_count'])}</p><p>{esc(row['subdivisions'])}</p></div></article>"""
            )
        category_options = "".join(f'<option value="{esc(value)}"{" selected" if value == category else ""}>{esc(value)}</option>' for value in categories)
        subdivision_options = "".join(f'<option value="{esc(sid)}"{" selected" if sid == subdivision else ""}>{esc(label)}</option>' for sid, label in subdivisions)
        previous = url("/master-history", **{**p, "page": page - 1}) if page > 1 else ""
        following = url("/master-history", **{**p, "page": page + 1}) if start + per_page < total else ""
        pagination = f"<nav class='pagination'>{f'<a class=button href={esc(previous)}>Previous</a>' if previous else ''}<span>Page {page} · {total} records</span>{f'<a class=button href={esc(following)}>Next</a>' if following else ''}</nav>"
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        self.page(
            "Exception queue",
            f"""{notice}<section class="summary"><div class="panel metric"><strong>5,092</strong>prior selected Masters</div><div class="panel metric"><strong>{selected_count}</strong>exceptions resolved</div><div class="panel metric"><strong>{190-selected_count}</strong>exceptions unresolved</div><div class="panel metric"><strong>5,282</strong>MD objects</div></section><form class="panel filters" method="get"><label>State<select name="state"><option value="unresolved"{" selected" if state=='unresolved' else ''}>Unresolved</option><option value="selected"{" selected" if state=='selected' else ''}>Selected</option><option value="all"{" selected" if state=='all' else ''}>All</option></select></label><label>Exception<select name="category"><option value="">All</option>{category_options}</select></label><label>Subdivision<select name="subdivision"><option value="">All</option>{subdivision_options}</select></label><label>Search<input type="search" name="q" value="{esc(p.get('q',''))}"></label><label>Per page<select name="per_page"><option>60</option><option value="120"{" selected" if per_page_raw=='120' else ''}>120</option><option value="all"{" selected" if per_page_raw=='all' else ''}>All</option></select></label><button class="button">Filter</button></form><section class="records">{''.join(cards) or '<p>No matching records.</p>'}</section>{pagination}""",
        )

    def page_record(self) -> None:
        p = self.parameters()
        record = self.server.review.record(p.get("record", ""))
        image_candidates = [c for c in record["candidates"] if str(c["extension"]).casefold() in IMAGE_EXTENSIONS]
        browser_candidates = [c for c in image_candidates if str(c["extension"]).casefold() in BROWSER_EXTENSIONS]
        fallback_preview = browser_candidates[0] if browser_candidates else None
        current_selected = int(record["selected_candidate_id"]) if record.get("selected_candidate_id") is not None else None
        cards = []
        for candidate in image_candidates:
            extension = str(candidate["extension"]).casefold()
            direct_preview = candidate if extension in BROWSER_EXTENSIONS else None
            proxy = direct_preview or fallback_preview
            preview = (
                f'<img loading="lazy" src="{url("/image",candidate=proxy["candidate_id"])}" alt="{esc(candidate["basename"])}"><small>{"Direct existing image" if direct_preview else "Existing JPEG proxy for this MD object"}</small>'
                if proxy else "<div class='empty-preview'>No existing browser preview</div>"
            )
            technical = self.server.review.technical(candidate)
            selected = current_selected == int(candidate["candidate_id"])
            control = (
                f'<label class="choice"><input type="radio" name="selected_candidate_id" value="{candidate["candidate_id"]}"{" checked" if selected else ""}>Select this TIFF as the single Master</label>'
                if extension in TIFF_EXTENSIONS else "<p class='technical'>Cannot be selected as Master; redundant after TIFF selection.</p>"
            )
            disposition = candidate_disposition(extension, int(candidate["candidate_id"]), current_selected, str(record.get("review_decision") or ""))
            cards.append(
                f"""<article class="candidate">{preview}<div><span class="badge">{esc(candidate_kind(extension,candidate['source_path']))}</span><span class="badge badge--{'selected' if selected else 'warning'}">{esc(disposition)}</span><h3>{esc(candidate['basename'])}</h3><p class="technical">{human_size(candidate['size_bytes'])} · {esc(technical['dimensions'])} · {esc(technical['format'])} · {esc(technical['status'])}</p><p class="technical path"><code>{esc(candidate['source_path'])}</code></p><p class="technical">match: {esc(candidate['match_basis'])}<br>existing SHA: {esc(candidate['reused_source_sha256']) or 'not available'}<br>mtime ns: {esc(candidate['mtime_ns'])}</p>{control}</div></article>"""
            )
        metadata_paths = [c for c in record["candidates"] if str(c["extension"]).casefold() not in IMAGE_EXTENSIONS]
        metadata = "".join(f"<li><code>{esc(c['source_path'])}</code></li>" for c in metadata_paths)
        notice = f"<section class='notice notice--safe'>{esc(p['notice'])}</section>" if p.get("notice") else ""
        current = (
            f"<section class='notice notice--safe'><strong>Current decision: {esc(record['review_decision'])}</strong><p>{esc(record.get('note',''))} · {esc(record.get('decided_by',''))} · {esc(record.get('decided_at',''))}</p></section>"
            if record.get("review_decision") else ""
        )
        previous_next = self.server.review.records()
        keys = [row["record_key"] for row in previous_next]
        index = keys.index(record["record_key"])
        previous = keys[index - 1] if index > 0 else ""
        following = keys[index + 1] if index + 1 < len(keys) else ""
        skip_key = ""
        for offset in range(1, len(keys) + 1):
            possible = previous_next[(index + offset) % len(keys)]
            if possible["record_key"] != record["record_key"] and possible["state"] != "selected_master":
                skip_key = str(possible["record_key"])
                break
        nav = f"<nav class='nav'>{f'<a href={esc(url("/record",record=previous))}>← Previous</a>' if previous else '<span></span>'}{f'<a href={esc(url("/record",record=following))}>Next →</a>' if following else ''}</nav>"
        self.page(
            record["inventory"],
            f"""{nav}{notice}<section class="panel"><span class="badge">{esc(record['exception_type'])}</span><h1>{esc(record['inventory'])}</h1><p><a href="{esc(record['md_url'])}" target="_blank" rel="noreferrer">museum-digital object {esc(record['md_object_id'])}</a></p><h2>{esc(record['title'])}</h2><p>{esc(record['date_text'])} · {esc(record['object_type'])} · {esc(record['material'])} · {esc(record['dimensions'])}</p><p class="description">{esc(record['description'])}</p><dl><dt>Subdivisions</dt><dd>{esc(record['subdivisions'])}</dd><dt>Places</dt><dd>{esc(record['places'])}</dd><dt>Tags</dt><dd>{esc(record['tags'])}</dd><dt>Creator</dt><dd>{esc(record['creator'])}</dd></dl></section>{current}<form method="post" action="/decision"><input type="hidden" name="csrf" value="{esc(self.server.csrf_token)}"><input type="hidden" name="record_key" value="{esc(record['record_key'])}"><section class="candidate-grid">{''.join(cards) or '<section class="notice">No image candidate is currently mapped.</section>'}</section>{f'<section class="panel"><h2>Metadata evidence</h2><ul>{metadata}</ul></section>' if metadata else ''}<section class="panel decision"><label>Decision note (optional)<textarea name="note">{esc(record.get('note',''))}</textarea></label><p>Selecting one TIFF makes all other image candidates Redundant. It never authorizes deletion. Skipping writes nothing.</p><div class="buttons"><button class="button" name="action" value="select_master">Record selected Master</button><button class="button button--quiet" name="action" value="unresolved">Leave unresolved</button>{f'<a class="button button--quiet" href="{esc(url("/record",record=skip_key))}">Skip without saving</a>' if skip_key else ''}</div></section></form>""",
        )

    def image(self) -> None:
        candidate_id = int(self.parameters().get("candidate", "0"))
        path = self.server.review.guarded_candidate_path(candidate_id, browser_only=True)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)


def self_test() -> None:
    assert candidate_kind(".tif", "/x/Alle-Tiff-Positive/a.tif") == "prior selected TIFF"
    assert candidate_kind(".jpg", "/x/a.jpg") == "existing derivative/copy"
    assert candidate_disposition(".tif", 1, 1, "selected_master") == "selected_master"
    assert candidate_disposition(".jpg", 2, 1, "selected_master") == "redundant"
    assert candidate_disposition(".xml", 3, 1, "selected_master") == "retain_metadata_evidence"
    assert candidate_disposition(".tif", 2, None, "unresolved") == "unresolved"
    assert displayed_exception_type("no_tiff_candidate", 1) == "repaired_exact_tiff_match"
    assert displayed_exception_type("no_tiff_candidate", 0) == "no_tiff_candidate"
    assert visual_inventory_hint("TFA-FOT-538520.tif") == "TFA-538520"
    assert visual_inventory_hint("TFA-NEG-538669_1.tiff") == "TFA-538669-1"
    assert visual_inventory_hint("BB-17_Seite_030.tiff") == ""
    assert orphan_candidate_disposition("selected_master", "a", "a") == "selected_master"
    assert orphan_candidate_disposition("selected_master", "b", "a") == "redundant"
    assert orphan_candidate_disposition("redundant", "b", "") == "redundant"
    assert orphan_candidate_disposition("genuine_loss", "b", "") == "genuine_loss"
    assert orphan_candidate_disposition("noise", "b", "") == "noise"
    assert orphan_candidate_disposition("unresolved", "b", "") == "unresolved"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="vladimir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        print("self-test: ok")
        return 0
    review = TfaReview(args.actor)
    if args.check:
        print(json.dumps(review.validate(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    server = Server((args.host, args.port), review)
    print(f"TFA workbench {VERSION} at http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
