#!/usr/bin/env python3
"""Museum-digital-object workbench for Brigadebücher collection 861.

Contract:
* one museum-digital object is one archive object;
* one explicitly selected existing page-image set is its Master representation;
* TIFF is preferred; an existing JPEG set or surviving PDF requires an explicit exception;
* the matching museum-digital PDF is retained as access/catalogue representation;
* generated derivatives belong to accession, not reconciliation;
* source files are never changed or deleted.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

VERSION = "3.1.0-existing-non-tiff-master"
SOURCE_ROOT = Path("/media/vladimir/Elements")
EXPECTED_SOURCE = "/dev/sdf1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-861")
RECON = WORK_ROOT / "work/reconciliation"

MD_OBJECTS = RECON / "brigadebuecher-md-objects.csv"
OBJECT_SUMMARY = RECON / "brigadebuecher-object-summary.csv"
PAGE_MATCHES = RECON / "brigadebuecher-page-matches.csv"
PAGE_EVIDENCE_AUDIT = RECON / "brigadebuecher-full-unit-page-evidence-audit.csv"
PDF_OCCURRENCES = RECON / "brigadebuecher-pdf-occurrences.csv"
CARRIERS = RECON / "brigadebuecher-carriers.jsonl"
OLD_SOURCE_DISPOSITION = RECON / "source-disposition.csv"
DOCUMENT_APPRAISAL = RECON / "brigadebuecher-document-appraisal.jsonl"
MD_VERIFICATION = RECON / "museum-digital-resource-verification.csv"

DECISIONS = RECON / "brigadebuecher-md-object-decisions.csv"
PAGE_DECISIONS = RECON / "brigadebuecher-md-page-decisions.csv"
SOURCE_DISPOSITION = RECON / "brigadebuecher-md-object-source-disposition.csv"
DRAIN_LIST = RECON / "brigadebuecher-md-object-drain-list.csv"
DATABASE = RECON / "brigadebuecher-md-object-workbench.sqlite"
PREFLIGHT = RECON / "brigadebuecher-md-object-preflight.json"
PREVIEW_PREFIX = ".brigade-preview-"

DECISION_FIELDS = (
    "object_key",
    "md_object_id",
    "inventory",
    "outcome",
    "master_representation_role",
    "selected_tiff_candidate_id",
    "selected_tiff_master_paths",
    "selected_non_tiff_master_paths",
    "reviewed_missing_pdf_pages",
    "retained_md_pdf_path",
    "redundant_candidate_paths",
    "note",
    "actor",
    "decided_utc",
    "accession_state",
    "accession_receipt_path",
    "metadata_authority",
    "deletion_authorized",
)

SURVIVING_PDF_EXCEPTIONS = {
    "md:114897": "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/Metadaten/UPLOADS/PDFs/BB-28.pdf/BB-28_3.pdf",
}
PAGE_DECISION_FIELDS = (
    "object_key",
    "md_object_id",
    "inventory",
    "pdf_page",
    "outcome",
    "selected_tiff_path",
    "selected_tiff_relation",
    "replaced_automatic_tiff_path",
    "note",
    "actor",
    "decided_utc",
    "deletion_authorized",
)
DISPOSITION_FIELDS = (
    "source_path",
    "object_keys",
    "base_inventory",
    "source_kind",
    "candidate_role",
    "disposition",
    "accession_relation",
    "drain_state",
    "size_bytes",
    "content_sha256",
    "evidence_basis",
    "deletion_authorized",
)


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(64 * 1024 * 1024)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def csv_payload(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(fields),
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()
    for source in rows:
        row = dict(source)
        for key, value in list(row.items()):
            if isinstance(value, (list, tuple, set)):
                row[key] = json.dumps(list(value), ensure_ascii=False)
            elif isinstance(value, bool):
                row[key] = "true" if value else "false"
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    with temporary.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o644)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if (
        len(fields) != 4
        or fields[0] != EXPECTED_SOURCE
        or fields[1] != str(SOURCE_ROOT)
        or fields[2] != "ntfs3"
        or "ro" not in fields[3].split(",")
    ):
        raise WorkbenchError(
            f"protected source must be /dev/sdf1 at {SOURCE_ROOT} as ntfs3 read-only; observed: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def source_path(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    path.relative_to(SOURCE_ROOT.resolve(strict=True))
    return path


def stable_id(role: str, paths: Iterable[str]) -> str:
    binding = role + "\0" + "\0".join(paths)
    return "CAND-" + hashlib.sha256(binding.encode("utf-8")).hexdigest()[:20].upper()


def natural_key(value: str | Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def human_size(value: int | str) -> str:
    amount = float(int(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return ""


def page_number(path: str) -> int | None:
    match = re.search(r"_(\d+)$", Path(path).stem)
    return int(match.group(1)) if match else None


class Collection861MD:
    def __init__(self, actor: str) -> None:
        self.actor = actor.strip() or "human-reviewer"
        self.mount = source_guard()
        self.lock = threading.Lock()
        self.preview_lock = threading.Lock()
        self._preview_paths_cache: set[str] | None = None
        self._family_paths_cache: dict[str, tuple[str, ...]] = {}
        self._page_decisions_cache_key: tuple[str, int, int, int] | None = None
        self._page_decisions_cache: dict[tuple[str, int], dict[str, Any]] = {}
        self.md_rows = read_csv(MD_OBJECTS)
        self.summary_rows = {row["inventory"]: row for row in read_csv(OBJECT_SUMMARY)}
        self.page_rows: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        for row in read_csv(PAGE_MATCHES):
            self.page_rows[row["inventory"]].append(row)
        self.pdf_rows = read_csv(PDF_OCCURRENCES)
        self.carriers = {row["carrier_key"]: row for row in read_jsonl(CARRIERS)}
        self.old_source_rows = read_csv(OLD_SOURCE_DISPOSITION)
        self.source_index = {row["source_path"]: row for row in self.old_source_rows}
        self.appraisal = {row["source_path"]: row for row in read_jsonl(DOCUMENT_APPRAISAL)}
        self.verification = {row["inventory"]: row for row in read_csv(MD_VERIFICATION)} if MD_VERIFICATION.is_file() else {}
        if len(self.md_rows) != 73 or len({row["object_id"] for row in self.md_rows}) != 73:
            raise WorkbenchError("expected exactly 73 unique museum-digital objects")
        self.objects = self._objects()
        self.remnant_audit = self._load_remnant_audit()
        self.candidates_by_object = self._candidates()
        self.candidates = {
            item["candidate_id"]: item
            for groups in self.candidates_by_object.values()
            for item in groups
        }
        self._validate()
        self.refresh()

    def _load_remnant_audit(self) -> dict[tuple[str, int], dict[str, Any]]:
        """Load the bounded full-unit rescan findings that still need human review."""
        if not PAGE_EVIDENCE_AUDIT.is_file():
            raise WorkbenchError("full-unit page evidence audit is missing")
        self.remnant_audit_cutoff = datetime.fromtimestamp(
            PAGE_EVIDENCE_AUDIT.stat().st_mtime,
            UTC,
        )
        output: dict[tuple[str, int], dict[str, Any]] = {}
        counts: collections.Counter[str] = collections.Counter()
        missing_outcomes = {
            "review_missing_tiff_decision_strong_tiff_candidate",
            "review_missing_tiff_decision_compatible_tiff_candidate",
        }
        for raw in read_csv(PAGE_EVIDENCE_AUDIT):
            category = ""
            if (
                raw.get("evidence_outcome") in missing_outcomes
                and raw.get("human_outcome") == "no_corresponding_tiff"
            ):
                category = "missing_tiff_candidate"
            elif (
                raw.get("evidence_outcome") == "effective_tiff_match_needs_review"
                and raw.get("effective_basis") == "automatic_tiff_suggestion"
                and not raw.get("human_outcome")
            ):
                category = "ambiguous_automatic"
            if not category:
                continue
            key = raw.get("object_key", "")
            obj = self.objects.get(key)
            try:
                pdf_page = int(raw.get("pdf_page", ""))
            except ValueError as exc:
                raise WorkbenchError("invalid remnant audit page number") from exc
            if obj is None or pdf_page < 1 or pdf_page > obj["md_pdf_pages"]:
                raise WorkbenchError(f"invalid remnant audit row: {key} page {pdf_page}")
            best_tiff_path = str(source_path(raw.get("best_tiff_path", "")))
            if Path(best_tiff_path).suffix.casefold() not in {".tif", ".tiff"}:
                raise WorkbenchError(f"remnant Master candidate is not TIFF: {best_tiff_path}")
            if raw.get("derivative_master_eligible", "").casefold() != "false":
                raise WorkbenchError("derivative evidence cannot be Master eligible")
            if raw.get("deletion_authorized", "").casefold() != "false":
                raise WorkbenchError("remnant audit cannot authorize deletion")
            cooked = dict(raw)
            cooked.update({
                "category": category,
                "pdf_page": pdf_page,
                "best_tiff_path": best_tiff_path,
            })
            audit_key = (key, pdf_page)
            if audit_key in output:
                raise WorkbenchError(f"duplicate remnant audit row: {key} page {pdf_page}")
            output[audit_key] = cooked
            counts[category] += 1
        if counts != collections.Counter({
            "missing_tiff_candidate": 32,
            "ambiguous_automatic": 48,
        }):
            raise WorkbenchError(f"unexpected actionable remnant audit scope: {dict(counts)}")
        return output

    def _audit_paths_for_object(self, key: str) -> set[str]:
        return {
            row["best_tiff_path"]
            for (object_key, _pdf_page), row in self.remnant_audit.items()
            if object_key == key
        }

    @staticmethod
    def _decision_time(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (AttributeError, ValueError):
            return None

    def _remnant_candidate(self, key: str) -> dict[str, Any] | None:
        groups = self.candidates_by_object.get(key, [])
        return next(
            (item for item in groups if item["role"] == "human_corrected_tiff_master_set"),
            next(
                (item for item in groups if item["role"] == "visual_match_tiff_master_set"),
                groups[0] if groups else None,
            ),
        )

    def remnant_for(self, key: str, pdf_page: int) -> dict[str, Any] | None:
        return self.remnant_audit.get((key, pdf_page))

    def remnants(
        self,
        kind: str = "all",
        include_reviewed: bool = False,
    ) -> list[dict[str, Any]]:
        """Project the audit through current human decisions without adding workflow state."""
        if kind not in {"all", "missing_tiff_candidate", "ambiguous_automatic"}:
            raise WorkbenchError("unknown remnant filter")
        object_decisions = self.decisions()
        page_decisions = self.page_decisions()
        output = []
        for audit_key, audit in sorted(
            self.remnant_audit.items(),
            key=lambda item: (natural_key(item[1]["inventory"]), item[0][1]),
        ):
            key, pdf_page = audit_key
            if kind != "all" and audit["category"] != kind:
                continue
            page_decision = page_decisions.get(audit_key)
            decision_time = self._decision_time(
                page_decision.get("decided_utc", "") if page_decision else ""
            )
            reviewed = bool(decision_time and decision_time > self.remnant_audit_cutoff)
            if reviewed and not include_reviewed:
                continue
            candidate = self._remnant_candidate(key)
            if candidate is None:
                raise WorkbenchError(f"remnant object lacks a TIFF comparison candidate: {key}")
            object_decision = object_decisions.get(key, {})
            cooked = dict(audit)
            cooked.update({
                "reviewed": reviewed,
                "page_decision": page_decision,
                "object_resolved": object_decision.get("outcome") == "selected_master",
                "candidate_id": candidate["candidate_id"],
                "candidate_role": candidate["role"],
            })
            output.append(cooked)
        return output

    def review_items(self, key: str | None = None) -> list[dict[str, Any]]:
        """Return one flat review list for missing, repeated, or uncertain pages."""
        keys = [key] if key else list(self.objects)
        if any(item not in self.objects for item in keys):
            raise WorkbenchError("unknown review object")
        audit = {
            (row["object_key"], row["pdf_page"]): row
            for row in self.remnants()
        }
        output: list[dict[str, Any]] = []
        for object_key in keys:
            candidate = self._remnant_candidate(object_key)
            if candidate is None:
                continue
            obj = self.objects[object_key]
            for row in self.comparison_rows(object_key, candidate["candidate_id"]):
                audit_row = audit.get((object_key, row["pdf_page"]))
                categories: list[str] = []
                if (
                    not row["tiff_path"]
                    and row["match_state"] != "human_recorded_no_corresponding_tiff"
                ) or (audit_row and audit_row["category"] == "missing_tiff_candidate"):
                    categories.append("missing")
                if row["duplicate_assignment"]:
                    categories.append("duplicated")
                if (
                    row["match_state"] in {"needs_review", "missing_visual_match"}
                    or row["decoder_error"]
                    or (audit_row and audit_row["category"] == "ambiguous_automatic")
                ):
                    categories.append("uncertain")
                if not categories:
                    continue
                output.append({
                    "object_key": object_key,
                    "inventory": obj["inventory"],
                    "title": obj["title"],
                    "pdf_page": row["pdf_page"],
                    "candidate_id": candidate["candidate_id"],
                    "categories": categories,
                    "tiff_path": row["tiff_path"],
                    "tiff_filename": row["tiff_filename"],
                })
        return sorted(
            output,
            key=lambda item: (natural_key(item["inventory"]), item["pdf_page"]),
        )

    def _objects(self) -> dict[str, dict[str, Any]]:
        preferred: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        for row in self.pdf_rows:
            if row["relationship"] == "preferred_occurrence_for_review":
                preferred[row["inventory"]].append(row)
        output: dict[str, dict[str, Any]] = {}
        for row in self.md_rows:
            inventory = row["inventory"]
            pdfs = preferred[inventory]
            if len(pdfs) != 1:
                raise WorkbenchError(f"{inventory} must have exactly one preferred MD PDF occurrence")
            pdf_path = str(source_path(pdfs[0]["source_path"]))
            with Path(pdf_path).open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise WorkbenchError(f"preferred MD PDF is not a PDF: {pdf_path}")
            key = f"md:{row['object_id']}"
            summary = self.summary_rows[inventory]
            verification = self.verification.get(inventory, {})
            output[key] = {
                "object_key": key,
                "md_object_id": row["object_id"],
                "inventory": inventory,
                "base_inventory": row["base_inventory"],
                "part_number": row["part_number"],
                "title": row["title"],
                "description": row["description"],
                "date": row["date"],
                "object_url": row["object_url"],
                "md_pdf_url": summary["md_pdf_url"],
                "md_pdf_path": pdf_path,
                "md_pdf_sha256": pdfs[0]["content_sha256"],
                "md_pdf_size": int(pdfs[0]["size_bytes"]),
                "md_pdf_pages": int(pdfs[0]["page_count"]),
                "description_page_start": row["description_page_start"],
                "description_page_end": row["description_page_end"],
                "issue_flags": [x for x in summary["issue_flags"].split(";") if x],
                "verification_state": verification.get("verification_state", "local_preferred_occurrence_from_captured_MD_projection"),
                "metadata_authority": "pre-accession: MD comparison projection; post-accession: archive unit authority and MD projection",
            }
        return output

    def _candidate(
        self,
        obj: dict[str, Any],
        role: str,
        label: str,
        paths: list[str],
        evidence: str,
        identity_paths: list[str] | None = None,
        reviewed_missing_pages: list[int] | None = None,
        page_review_complete: bool = False,
        bound_pdf_page_count: int | None = None,
        confirmed_crop_pdf_pages: list[int] | None = None,
    ) -> dict[str, Any]:
        paths = [str(source_path(path)) for path in paths]
        reviewed_missing_pages = sorted(set(reviewed_missing_pages or []))
        confirmed_crop_pdf_pages = sorted(set(confirmed_crop_pdf_pages or []))
        bound_pdf_page_count = (
            len(paths) if bound_pdf_page_count is None else bound_pdf_page_count
        )
        indexed = [self.source_index.get(path, {}) for path in paths]
        errors = sum(
            "error=" in row.get("technical_metadata", "")
            or "index_state=error" in row.get("technical_metadata", "")
            for row in indexed
        )
        complete_page_binding = bound_pdf_page_count == obj["md_pdf_pages"]
        reviewed_gap_binding = (
            role == "human_corrected_tiff_master_set"
            and page_review_complete
            and bool(paths)
            and bound_pdf_page_count + len(reviewed_missing_pages) == obj["md_pdf_pages"]
        )
        eligible = (
            bool(paths)
            and (complete_page_binding or reviewed_gap_binding)
            and len(set(paths)) == len(paths)
            and errors == 0
            and all(Path(path).suffix.casefold() in {".tif", ".tiff"} for path in paths)
        )
        return {
            "candidate_id": stable_id(role, identity_paths if identity_paths is not None else paths),
            "object_key": obj["object_key"],
            "role": role,
            "label": label,
            "paths": paths,
            "file_count": len(paths),
            "size_bytes": sum(int(self.source_index.get(path, {}).get("size_bytes") or Path(path).stat().st_size) for path in paths),
            "eligible": eligible,
            "error_count": errors,
            "reviewed_missing_pages": reviewed_missing_pages,
            "bound_pdf_page_count": bound_pdf_page_count,
            "confirmed_crop_pdf_pages": confirmed_crop_pdf_pages,
            "page_review_complete": page_review_complete,
            "evidence": evidence,
        }

    def _scan_family_paths(self, obj: dict[str, Any]) -> list[str]:
        carrier = self.carriers[obj["base_inventory"]]
        raw = carrier.get("proposed_master_path", "")
        if not raw or ";" in raw:
            return []
        directory = Path(raw)
        if not directory.is_dir():
            return []
        paths = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in {".tif", ".tiff"}
        ]
        return [str(path) for path in sorted(paths, key=natural_key)]

    def _family_paths(self, obj: dict[str, Any]) -> list[str]:
        """Return the immutable TIFF family without rescanning the protected source."""
        key = obj["base_inventory"]
        if key not in self._family_paths_cache:
            self._family_paths_cache[key] = tuple(self._scan_family_paths(obj))
        return list(self._family_paths_cache[key])

    @staticmethod
    def _copy_page_decisions(
        decisions: dict[tuple[str, int], dict[str, Any]],
    ) -> dict[tuple[str, int], dict[str, Any]]:
        return {key: dict(row) for key, row in decisions.items()}

    def _set_page_decisions_cache(
        self,
        decisions: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        stat = PAGE_DECISIONS.stat()
        self._page_decisions_cache = self._copy_page_decisions(decisions)
        self._page_decisions_cache_key = (
            str(PAGE_DECISIONS),
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )

    def page_decisions(self) -> dict[tuple[str, int], dict[str, Any]]:
        """Return the current human page mapping decisions."""
        if not PAGE_DECISIONS.is_file():
            self._page_decisions_cache = {}
            self._page_decisions_cache_key = None
            return {}
        stat = PAGE_DECISIONS.stat()
        cache_key = (
            str(PAGE_DECISIONS),
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
        if cache_key == self._page_decisions_cache_key:
            return self._copy_page_decisions(self._page_decisions_cache)
        output: dict[tuple[str, int], dict[str, Any]] = {}
        allowed_by_object: dict[str, set[str]] = {}
        for row in read_csv(PAGE_DECISIONS):
            key = row.get("object_key", "")
            try:
                pdf_page = int(row.get("pdf_page", ""))
            except ValueError as exc:
                raise WorkbenchError("invalid page-decision page number") from exc
            obj = self.objects.get(key)
            if obj is None or pdf_page < 1 or pdf_page > obj["md_pdf_pages"]:
                raise WorkbenchError(f"invalid page decision: {key} page {pdf_page}")
            outcome = row.get("outcome", "")
            selected_path = row.get("selected_tiff_path", "")
            selected_tiff_relation = row.get("selected_tiff_relation", "")
            if outcome not in {"selected_tiff", "no_corresponding_tiff"}:
                raise WorkbenchError(f"invalid page-decision outcome: {key} page {pdf_page}")
            if outcome == "selected_tiff":
                selected_tiff_relation = selected_tiff_relation or "full_page"
                if selected_tiff_relation not in {
                    "full_page", "cropped_detail", "rotated_90_ccw"
                }:
                    raise WorkbenchError(
                        f"invalid TIFF page relationship: {key} page {pdf_page}"
                    )
                selected_path = str(source_path(selected_path))
                if key not in allowed_by_object:
                    allowed_by_object[key] = (
                        set(self._family_paths(obj))
                        | set(self._automatic_page_mapping(obj).values())
                        | self._audit_paths_for_object(key)
                    )
                allowed = allowed_by_object[key]
                if selected_path not in allowed:
                    raise WorkbenchError(f"page decision TIFF is outside reviewed carrier candidates: {key} page {pdf_page}")
            elif selected_path or selected_tiff_relation:
                raise WorkbenchError(
                    "no-corresponding-TIFF decision cannot select a path or relationship"
                )
            cooked = dict(row)
            cooked["pdf_page"] = pdf_page
            cooked["selected_tiff_relation"] = selected_tiff_relation
            cooked["deletion_authorized"] = False
            output[(key, pdf_page)] = cooked
        self._set_page_decisions_cache(output)
        return self._copy_page_decisions(output)

    def _automatic_page_mapping(self, obj: dict[str, Any]) -> dict[int, str]:
        return {
            int(row["pdf_page"]): row.get("best_source_path", "")
            for row in self.page_rows[obj["inventory"]]
        }

    def _human_page_mapping(
        self,
        obj: dict[str, Any],
        page_decisions: dict[tuple[str, int], dict[str, Any]] | None = None,
    ) -> list[str]:
        automatic = self._automatic_page_mapping(obj)
        decisions = self.page_decisions() if page_decisions is None else page_decisions
        paths: list[str] = []
        for pdf_page in range(1, obj["md_pdf_pages"] + 1):
            decision = decisions.get((obj["object_key"], pdf_page))
            if decision:
                path = decision["selected_tiff_path"] if decision["outcome"] == "selected_tiff" else ""
            else:
                path = automatic.get(pdf_page, "")
            paths.append(path)
        return paths

    @staticmethod
    def _confirmed_crop_groups(
        key: str,
        page_paths: list[str],
        page_decisions: dict[tuple[str, int], dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Return only explicitly confirmed one-full-page plus crop relationships."""
        pages_by_path: dict[str, list[int]] = collections.defaultdict(list)
        for pdf_page, path in enumerate(page_paths, 1):
            if path:
                pages_by_path[path].append(pdf_page)
        groups: dict[str, dict[str, Any]] = {}
        for path, pdf_pages in pages_by_path.items():
            if len(pdf_pages) < 2:
                continue
            decisions = [page_decisions.get((key, page)) for page in pdf_pages]
            if any(
                not decision
                or decision["outcome"] != "selected_tiff"
                or decision["selected_tiff_path"] != path
                for decision in decisions
            ):
                continue
            full_pages = [
                page
                for page, decision in zip(pdf_pages, decisions)
                if decision.get("selected_tiff_relation", "full_page") == "full_page"
            ]
            cropped_pages = [
                page
                for page, decision in zip(pdf_pages, decisions)
                if decision.get("selected_tiff_relation", "full_page") == "cropped_detail"
            ]
            if len(full_pages) == 1 and len(cropped_pages) == len(pdf_pages) - 1:
                groups[path] = {
                    "tiff_path": path,
                    "full_pdf_page": full_pages[0],
                    "cropped_pdf_pages": cropped_pages,
                    "pdf_pages": pdf_pages,
                }
        return groups

    def _automatic_problem_pages(self, obj: dict[str, Any]) -> set[int]:
        """Return automatic mappings that require an explicit human page decision."""
        matches = {
            int(row["pdf_page"]): row
            for row in self.page_rows[obj["inventory"]]
        }
        counts = collections.Counter(
            row.get("best_source_path", "")
            for row in matches.values()
            if row.get("best_source_path", "")
        )
        problems: set[int] = set()
        for pdf_page in range(1, obj["md_pdf_pages"] + 1):
            row = matches.get(pdf_page, {})
            path = row.get("best_source_path", "")
            indexed = self.source_index.get(path, {}) if path else {}
            decoder_error = (
                "error=" in indexed.get("technical_metadata", "")
                or "index_state=error" in indexed.get("technical_metadata", "")
            )
            if (
                not path
                or counts.get(path, 0) > 1
                or row.get("match_state", "missing_visual_match") in {"needs_review", "missing_visual_match"}
                or decoder_error
            ):
                problems.add(pdf_page)
        return problems

    def _rebuild_candidates(self) -> None:
        self.candidates_by_object = self._candidates()
        self.candidates = {
            item["candidate_id"]: item
            for groups in self.candidates_by_object.values()
            for item in groups
        }
        self._preview_paths_cache = None
        self._validate()

    def _rebuild_candidates_for_object(
        self,
        key: str,
        page_decisions: dict[tuple[str, int], dict[str, Any]] | None = None,
    ) -> None:
        """Rebuild only the object changed by a page-level human decision."""
        if key not in self.objects:
            raise WorkbenchError("unknown museum-digital object")
        previous_ids = {
            candidate["candidate_id"]
            for candidate in self.candidates_by_object[key]
        }
        rebuilt = self._candidates([key], page_decisions=page_decisions)[key]
        self._validate_candidate_groups(key, rebuilt)
        self.candidates_by_object[key] = rebuilt
        for candidate_id in previous_ids:
            self.candidates.pop(candidate_id, None)
        for candidate in rebuilt:
            self.candidates[candidate["candidate_id"]] = candidate
        self._preview_paths_cache = None

    def _candidates(
        self,
        object_keys: Iterable[str] | None = None,
        page_decisions: dict[tuple[str, int], dict[str, Any]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        all_page_decisions = self.page_decisions() if page_decisions is None else page_decisions
        objects_by_base: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for obj in self.objects.values():
            objects_by_base[obj["base_inventory"]].append(obj)
        keys = list(self.objects) if object_keys is None else list(object_keys)
        if any(key not in self.objects for key in keys):
            raise WorkbenchError("unknown museum-digital object")
        for key in keys:
            obj = self.objects[key]
            candidates: list[dict[str, Any]] = []
            family = self._family_paths(obj)
            start = int(obj["description_page_start"]) if obj["description_page_start"] else None
            end = int(obj["description_page_end"]) if obj["description_page_end"] else None
            if start is not None and end is not None and family:
                by_sequence: dict[int, list[str]] = collections.defaultdict(list)
                for path in family:
                    sequence = page_number(path)
                    if sequence is not None:
                        by_sequence[sequence].append(path)
                range_paths = [by_sequence[number][0] for number in range(start, end + 1) if len(by_sequence[number]) == 1]
                candidates.append(self._candidate(
                    obj,
                    "catalogue_range_tiff_master_set",
                    "Catalogue-range ordered TIFF set",
                    range_paths,
                    f"catalogue range {start}–{end}; {len(range_paths)}/{end-start+1} unique sequence files; MD PDF has {obj['md_pdf_pages']} pages",
                ))
            if len(objects_by_base[obj["base_inventory"]]) == 1 and family:
                candidates.append(self._candidate(
                    obj,
                    "whole_carrier_tiff_master_set",
                    "Complete carrier TIFF set",
                    family,
                    f"single MD object for carrier; {len(family)} root-level TIFF files; MD PDF has {obj['md_pdf_pages']} pages",
                ))
            matches = sorted(self.page_rows[obj["inventory"]], key=lambda row: int(row["pdf_page"]))
            match_paths = [row["best_source_path"] for row in matches if row["best_source_path"]]
            states = collections.Counter(row["match_state"] for row in matches)
            if match_paths:
                candidates.append(self._candidate(
                    obj,
                    "visual_match_tiff_master_set",
                    "PDF-page visual-match TIFF set",
                    match_paths,
                    f"{len(match_paths)}/{obj['md_pdf_pages']} mapped pages; {len(set(match_paths))} unique TIFFs; match states {dict(states)}",
                ))
            object_page_decisions = {
                page: row
                for (decision_key, page), row in all_page_decisions.items()
                if decision_key == obj["object_key"]
            }
            if object_page_decisions:
                page_paths = self._human_page_mapping(obj, all_page_decisions)
                corrected_paths = [path for path in page_paths if path]
                confirmed_crop_groups = self._confirmed_crop_groups(
                    obj["object_key"], page_paths, all_page_decisions
                )
                repeated_path_counts = collections.Counter(corrected_paths)
                invalid_repeated_paths = {
                    path
                    for path, count in repeated_path_counts.items()
                    if count > 1 and path not in confirmed_crop_groups
                }
                ordered_master_paths = list(dict.fromkeys(corrected_paths))
                reviewed_missing_pages = sorted(
                    page
                    for page, row in object_page_decisions.items()
                    if row["outcome"] == "no_corresponding_tiff"
                )
                problem_pages = self._automatic_problem_pages(obj)
                page_review_complete = (
                    problem_pages <= set(object_page_decisions)
                    and not invalid_repeated_paths
                )
                confirmed_crop_pdf_pages = sorted(
                    page
                    for group in confirmed_crop_groups.values()
                    for page in group["cropped_pdf_pages"]
                )
                candidates.append(self._candidate(
                    obj,
                    "human_corrected_tiff_master_set",
                    "Human-corrected ordered TIFF set",
                    ordered_master_paths,
                    f"automatic page mapping with {len(object_page_decisions)} recorded human page decision(s); "
                    f"{len(corrected_paths)}/{obj['md_pdf_pages']} mapped pages; "
                    f"{len(ordered_master_paths)} unique TIFFs; "
                    f"{len(confirmed_crop_pdf_pages)} explicitly confirmed cropped PDF page(s); "
                    f"{len(reviewed_missing_pages)} explicitly reviewed missing page(s)",
                    identity_paths=[
                        (
                            f"{path}|cropped_detail"
                            if all_page_decisions.get((obj["object_key"], page), {}).get(
                                "selected_tiff_relation"
                            ) == "cropped_detail"
                            else path
                            if path
                            else "<no-corresponding-tiff>"
                        )
                        for page, path in enumerate(page_paths, 1)
                    ],
                    reviewed_missing_pages=reviewed_missing_pages,
                    page_review_complete=page_review_complete,
                    bound_pdf_page_count=len(corrected_paths),
                    confirmed_crop_pdf_pages=confirmed_crop_pdf_pages,
                ))
            deduplicated: dict[tuple[str, ...], dict[str, Any]] = {}
            for candidate in candidates:
                binding = tuple(candidate["paths"])
                if candidate["role"] == "human_corrected_tiff_master_set":
                    binding += (candidate["role"],)
                if binding not in deduplicated:
                    deduplicated[binding] = candidate
                elif candidate["eligible"] and not deduplicated[binding]["eligible"]:
                    deduplicated[binding] = candidate
            output[obj["object_key"]] = sorted(
                deduplicated.values(),
                key=lambda item: (not item["eligible"], item["role"]),
            )
        return output

    def _validate(self) -> None:
        if set(self.objects) != set(self.candidates_by_object):
            raise WorkbenchError("candidate/object key mismatch")
        for key, groups in self.candidates_by_object.items():
            self._validate_candidate_groups(key, groups)

    def _validate_candidate_groups(self, key: str, groups: list[dict[str, Any]]) -> None:
        ids = [group["candidate_id"] for group in groups]
        if len(ids) != len(set(ids)):
            raise WorkbenchError(f"duplicate candidate IDs for {key}")
        for group in groups:
            if group["eligible"]:
                complete = (
                    group["bound_pdf_page_count"]
                    == self.objects[key]["md_pdf_pages"]
                )
                reviewed_gaps = (
                    group["role"] == "human_corrected_tiff_master_set"
                    and group["page_review_complete"]
                    and group["bound_pdf_page_count"] + len(group["reviewed_missing_pages"])
                    == self.objects[key]["md_pdf_pages"]
                )
                if not (complete or reviewed_gaps):
                    raise WorkbenchError(f"eligible TIFF candidate has incomplete page binding: {key}")
            for path in group["paths"]:
                source_path(path)

    def decisions(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        if not DECISIONS.is_file():
            return output
        for row in read_csv(DECISIONS):
            key = row.get("object_key", "")
            if key not in self.objects:
                continue
            cooked: dict[str, Any] = dict(row)
            for field in (
                "selected_tiff_master_paths",
                "selected_non_tiff_master_paths",
                "reviewed_missing_pdf_pages",
                "redundant_candidate_paths",
            ):
                try:
                    cooked[field] = json.loads(row.get(field, "[]"))
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(f"invalid decision JSON array: {key} {field}") from exc
            cooked["deletion_authorized"] = False
            cooked["master_representation_role"] = row.get("master_representation_role", "")
            if cooked["outcome"] == "selected_master" and not cooked["master_representation_role"]:
                cooked["master_representation_role"] = (
                    "ordered_tiff_page_set"
                    if cooked.get("selected_tiff_candidate_id")
                    else "museum_digital_pdf"
                )
            output[key] = cooked
        return output

    def records(self) -> list[dict[str, Any]]:
        decisions = self.decisions()
        rows = []
        for key, obj in self.objects.items():
            row = dict(obj)
            row["candidates"] = self.candidates_by_object[key]
            row["decision"] = decisions.get(key)
            row["queue_state"] = "resolved" if row["decision"] and row["decision"]["outcome"] == "selected_master" else "unresolved"
            rows.append(row)
        return sorted(rows, key=lambda row: natural_key(row["inventory"]))

    def pdf_only_master_eligible(
        self,
        key: str,
        page_decisions: dict[tuple[str, int], dict[str, Any]] | None = None,
    ) -> bool:
        return bool(self.pdf_only_master_basis(key, page_decisions))

    def pdf_only_master_basis(
        self,
        key: str,
        page_decisions: dict[tuple[str, int], dict[str, Any]] | None = None,
    ) -> str:
        """Return the bounded evidence basis that permits the MD PDF to be the single Master."""
        obj = self.objects.get(key)
        if obj is None:
            return ""
        decisions = self.page_decisions() if page_decisions is None else page_decisions
        object_decisions = {
            page: row
            for (decision_key, page), row in decisions.items()
            if decision_key == key
        }
        expected_pages = set(range(1, obj["md_pdf_pages"] + 1))
        if (
            set(object_decisions) == expected_pages
            and all(
                row["outcome"] == "no_corresponding_tiff"
                and not row["selected_tiff_path"]
                for row in object_decisions.values()
            )
        ):
            return "complete_explicit_no_tiff_page_review"
        carrier = self.carriers[obj["base_inventory"]]
        no_indexed_tiff_family = (
            not self.candidates_by_object.get(key)
            and int(carrier.get("primary_original_tiff_count") or 0) == 0
            and not carrier.get("other_original_tiff_families")
            and not carrier.get("project_tiff_copy_families")
            and carrier.get("proposed_master_role") == "preservation_pdf_candidate"
            and Path(carrier.get("proposed_master_path", "")).suffix.casefold() == ".pdf"
            and any("no_original_tiff" in reason for reason in carrier.get("review_reasons", []))
        )
        return "indexed_carrier_has_no_tiff_family" if no_indexed_tiff_family else ""

    def page_choices(self, key: str, pdf_page: int) -> dict[str, Any]:
        """Return bounded TIFF choices and the evidence behind them."""
        obj = self.objects.get(key)
        if obj is None or pdf_page < 1 or pdf_page > obj["md_pdf_pages"]:
            raise WorkbenchError("unknown object or PDF page")
        matches = {
            int(row["pdf_page"]): row
            for row in self.page_rows[obj["inventory"]]
        }
        page_decisions = self.page_decisions()
        match = matches.get(pdf_page, {})
        family = self._family_paths(obj)
        automatic_paths = [path for path in self._automatic_page_mapping(obj).values() if path]
        audit_paths = self._audit_paths_for_object(key)
        allowed = sorted(set(family + automatic_paths) | audit_paths, key=natural_key)
        by_sequence: dict[int, list[str]] = collections.defaultdict(list)
        for path in allowed:
            sequence = page_number(path)
            if sequence is not None:
                by_sequence[sequence].append(path)

        reasons: dict[str, list[str]] = collections.defaultdict(list)
        automatic = match.get("best_source_path", "")
        if automatic:
            if match.get("selection_basis", "").endswith("sequence_context_within_10_hash_bits"):
                reasons[automatic].append("sequence-context visual suggestion")
            else:
                reasons[automatic].append("automatic visual match")
        current = page_decisions.get((key, pdf_page))
        if current and current["outcome"] == "selected_tiff":
            reasons[current["selected_tiff_path"]].append("currently recorded human choice")
        audit = self.remnant_for(key, pdf_page)
        if audit:
            reasons[audit["best_tiff_path"]].append("full-unit rescan TIFF candidate")
        start = int(obj["description_page_start"]) if obj["description_page_start"] else None
        catalogue_sequence = start + pdf_page - 1 if start is not None else None
        dominant_offset = int(match["dominant_offset"]) if match.get("dominant_offset") else None
        aligned_sequence = pdf_page + dominant_offset if dominant_offset is not None else catalogue_sequence
        if catalogue_sequence is not None:
            for path in by_sequence.get(catalogue_sequence, []):
                reasons[path].append(f"MD description sequence ({catalogue_sequence})")
        if aligned_sequence is not None:
            for path in by_sequence.get(aligned_sequence, []):
                reasons[path].append(f"neighbor-aligned sequence ({aligned_sequence}; offset {dominant_offset:+d})" if dominant_offset is not None else f"expected sequence ({aligned_sequence})")
            for sequence in range(max(1, aligned_sequence - 2), aligned_sequence + 3):
                if sequence == aligned_sequence:
                    continue
                for path in by_sequence.get(sequence, []):
                    reasons[path].append(f"near neighbor-aligned sequence ({sequence})")
        runner_up = int(match["runner_up_sequence"]) if match.get("runner_up_sequence") else None
        if runner_up is not None:
            for path in by_sequence.get(runner_up, []):
                reasons[path].append(f"automatic runner-up sequence ({runner_up})")

        def choice_sort(path: str) -> tuple[int, list[Any]]:
            tags = reasons[path]
            if "currently recorded human choice" in tags:
                priority = 0
            elif "full-unit rescan TIFF candidate" in tags:
                priority = 1
            elif any(tag.startswith("neighbor-aligned sequence") for tag in tags):
                priority = 2
            elif any(tag in {"automatic visual match", "sequence-context visual suggestion"} for tag in tags):
                priority = 3
            elif any(tag.startswith("automatic runner-up") for tag in tags):
                priority = 4
            else:
                priority = 5
            return priority, natural_key(path)

        choices = []
        for path in sorted(reasons, key=choice_sort):
            indexed = self.source_index.get(path, {})
            choices.append({
                "path": path,
                "filename": Path(path).name,
                "sequence": page_number(path),
                "reasons": reasons[path],
                "technical_metadata": indexed.get("technical_metadata", ""),
            })
        return {
            "choices": choices,
            "all_paths": allowed,
            "current": current,
            "automatic_path": automatic,
            "expected_sequence": aligned_sequence,
            "catalogue_sequence": catalogue_sequence,
            "dominant_offset": dominant_offset,
            "runner_up_sequence": runner_up,
        }

    def comparison_rows(self, key: str, candidate_id: str) -> list[dict[str, Any]]:
        """Return one explicit PDF-page to TIFF-page row for visual review."""
        obj = self.objects.get(key)
        candidate = self.candidates.get(candidate_id)
        if obj is None or candidate is None or candidate["object_key"] != key:
            raise WorkbenchError("unknown object/candidate comparison")
        matches = {
            int(row["pdf_page"]): row
            for row in self.page_rows[obj["inventory"]]
        }
        page_decisions = self.page_decisions()
        automatic_problem_pages = (
            self._automatic_problem_pages(obj)
            if candidate["role"] == "human_corrected_tiff_master_set"
            else set()
        )
        paths_by_sequence: dict[int, list[str]] = collections.defaultdict(list)
        for path in candidate["paths"]:
            sequence = page_number(path)
            if sequence is not None:
                paths_by_sequence[sequence].append(path)
        start = int(obj["description_page_start"]) if obj["description_page_start"] else None
        rows: list[dict[str, Any]] = []
        for pdf_page in range(1, obj["md_pdf_pages"] + 1):
            match = matches.get(pdf_page, {})
            sequence: int | None = None
            path = ""
            state = ""
            selected_tiff_relation = ""
            if candidate["role"] == "visual_match_tiff_master_set":
                path = match.get("best_source_path", "")
                sequence = int(match["matched_scan_sequence"]) if match.get("matched_scan_sequence") else page_number(path)
                state = match.get("match_state", "missing_visual_match")
            elif candidate["role"] == "human_corrected_tiff_master_set":
                decision = page_decisions.get((key, pdf_page))
                if decision:
                    if decision["outcome"] == "selected_tiff":
                        path = decision["selected_tiff_path"]
                        sequence = page_number(path)
                        state = "human_selected_tiff"
                        selected_tiff_relation = decision.get(
                            "selected_tiff_relation", "full_page"
                        )
                    else:
                        state = "human_recorded_no_corresponding_tiff"
                else:
                    path = match.get("best_source_path", "")
                    sequence = int(match["matched_scan_sequence"]) if match.get("matched_scan_sequence") else page_number(path)
                    state = match.get("match_state", "missing_visual_match")
                    if pdf_page in automatic_problem_pages:
                        state = "needs_review"
            elif candidate["role"] == "catalogue_range_tiff_master_set" and start is not None:
                sequence = start + pdf_page - 1
                options = paths_by_sequence.get(sequence, [])
                path = options[0] if len(options) == 1 else ""
                state = "catalogue_range_sequence" if path else "missing_catalogue_sequence"
            elif candidate["role"] == "whole_carrier_tiff_master_set":
                if pdf_page <= len(candidate["paths"]):
                    path = candidate["paths"][pdf_page - 1]
                    sequence = page_number(path)
                    state = "ordered_whole_carrier_sequence"
                else:
                    state = "missing_whole_carrier_sequence"
            indexed = self.source_index.get(path, {}) if path else {}
            decoder_error = bool(
                "error=" in indexed.get("technical_metadata", "")
                or "index_state=error" in indexed.get("technical_metadata", "")
            )
            rows.append({
                "pdf_page": pdf_page,
                "tiff_path": path,
                "tiff_filename": Path(path).name if path else "",
                "sequence": sequence,
                "match_state": state,
                "distance_total": match.get("distance_total", ""),
                "distance_phash": match.get("distance_phash", ""),
                "distance_dhash": match.get("distance_dhash", ""),
                "distance_ahash": match.get("distance_ahash", ""),
                "runner_up_sequence": match.get("runner_up_sequence", ""),
                "runner_up_total": match.get("runner_up_total", ""),
                "selection_basis": match.get("selection_basis", "legacy_global_perceptual_hash"),
                "global_best_scan_sequence": match.get("global_best_scan_sequence", ""),
                "global_best_total": match.get("global_best_total", ""),
                "decoder_error": decoder_error,
                "technical_metadata": indexed.get("technical_metadata", ""),
                "selected_tiff_relation": selected_tiff_relation,
            })
        pages_by_path: dict[str, list[int]] = collections.defaultdict(list)
        for row in rows:
            if row["tiff_path"]:
                pages_by_path[row["tiff_path"]].append(row["pdf_page"])
        confirmed_crop_groups = (
            self._confirmed_crop_groups(
                key,
                [row["tiff_path"] for row in rows],
                page_decisions,
            )
            if candidate["role"] == "human_corrected_tiff_master_set"
            else {}
        )
        for row in rows:
            duplicate_pages = pages_by_path.get(row["tiff_path"], [])
            crop_group = confirmed_crop_groups.get(row["tiff_path"])
            row["shared_crop_assignment_pages"] = (
                crop_group["pdf_pages"] if crop_group else []
            )
            row["duplicate_assignment_pages"] = (
                duplicate_pages
                if len(duplicate_pages) > 1 and not crop_group
                else []
            )
            row["duplicate_assignment"] = bool(row["duplicate_assignment_pages"])
            problems = []
            if not row["tiff_path"]:
                problems.append("missing TIFF mapping")
            if row["duplicate_assignment"]:
                page_list = ", ".join(str(page) for page in duplicate_pages)
                problems.append(f"same TIFF assigned to PDF pages {page_list}")
            if row["match_state"] in {"needs_review", "missing_visual_match"}:
                problems.append("visual match needs review")
            if row["decoder_error"]:
                problems.append("TIFF decoder/index error")
            row["problems"] = problems
        return rows

    def comparison_summary(self, key: str, candidate_id: str) -> dict[str, Any]:
        rows = self.comparison_rows(key, candidate_id)
        assigned = [row["tiff_path"] for row in rows if row["tiff_path"]]
        problem_pages = [row["pdf_page"] for row in rows if row["problems"]]
        reviewed_missing = sum(
            row["match_state"] == "human_recorded_no_corresponding_tiff"
            for row in rows
        )
        missing = sum(not row["tiff_path"] for row in rows)
        repeated_assignment_groups = []
        confirmed_crop_groups = []
        seen_repeated_paths: set[str] = set()
        seen_crop_paths: set[str] = set()
        for row in rows:
            path = row["tiff_path"]
            if row["shared_crop_assignment_pages"] and path not in seen_crop_paths:
                seen_crop_paths.add(path)
                full_page = next(
                    item["pdf_page"]
                    for item in rows
                    if item["tiff_path"] == path
                    and item["selected_tiff_relation"] == "full_page"
                )
                cropped_pages = [
                    item["pdf_page"]
                    for item in rows
                    if item["tiff_path"] == path
                    and item["selected_tiff_relation"] == "cropped_detail"
                ]
                confirmed_crop_groups.append({
                    "tiff_path": path,
                    "tiff_filename": row["tiff_filename"],
                    "full_pdf_page": full_page,
                    "cropped_pdf_pages": cropped_pages,
                    "pdf_pages": row["shared_crop_assignment_pages"],
                })
            if not row["duplicate_assignment"] or path in seen_repeated_paths:
                continue
            seen_repeated_paths.add(path)
            repeated_assignment_groups.append({
                "tiff_path": path,
                "tiff_filename": row["tiff_filename"],
                "pdf_pages": row["duplicate_assignment_pages"],
            })
        return {
            "pdf_pages": len(rows),
            "mapped_pages": len(assigned),
            "unique_tiffs": len(set(assigned)),
            "missing_pages": missing,
            "reviewed_missing_pages": reviewed_missing,
            "unreviewed_missing_pages": missing - reviewed_missing,
            "duplicate_page_assignments": sum(row["duplicate_assignment"] for row in rows),
            "repeated_assignment_groups": repeated_assignment_groups,
            "confirmed_crop_groups": confirmed_crop_groups,
            "cropped_detail_pages": sum(
                row["selected_tiff_relation"] == "cropped_detail"
                for row in rows
            ),
            "needs_visual_review": sum(row["match_state"] == "needs_review" for row in rows),
            "decoder_errors": sum(row["decoder_error"] for row in rows),
            "problem_pages": problem_pages,
        }

    def unresolved_pages(self, key: str, candidate_id: str) -> list[int]:
        """Return only problem pages that do not yet have a human decision."""
        decided = {
            pdf_page
            for (decision_key, pdf_page) in self.page_decisions()
            if decision_key == key
        }
        return [
            row["pdf_page"]
            for row in self.comparison_rows(key, candidate_id)
            if row["problems"] and row["pdf_page"] not in decided
        ]

    def save_page_decision(
        self,
        key: str,
        pdf_page: int,
        outcome: str,
        selected_path: str,
        note: str,
        selected_tiff_relation: str = "full_page",
    ) -> str:
        """Record one human page mapping and rebuild the corrected TIFF candidate."""
        source_guard()
        obj = self.objects.get(key)
        if obj is None or pdf_page < 1 or pdf_page > obj["md_pdf_pages"]:
            raise WorkbenchError("unknown object or PDF page")
        object_decision = self.decisions().get(key, {})
        if object_decision.get("accession_state", "not_accessioned") != "not_accessioned":
            raise WorkbenchError("page mappings cannot change after accession has started")
        selected_candidate = self.candidates.get(
            object_decision.get("selected_tiff_candidate_id", "")
        )
        if outcome not in {"selected_tiff", "no_corresponding_tiff"}:
            raise WorkbenchError("choose a TIFF or record that no corresponding TIFF exists")
        choices = self.page_choices(key, pdf_page)
        if outcome == "selected_tiff":
            selected_tiff_relation = selected_tiff_relation or "full_page"
            if selected_tiff_relation not in {
                "full_page", "cropped_detail", "rotated_90_ccw"
            }:
                raise WorkbenchError("unsupported TIFF page relationship")
            selected_path = str(source_path(selected_path))
            if selected_path not in choices["all_paths"]:
                raise WorkbenchError("selected TIFF is outside this carrier's reviewed page candidates")
        else:
            selected_path = ""
            selected_tiff_relation = ""
        record = {
            "object_key": key,
            "md_object_id": obj["md_object_id"],
            "inventory": obj["inventory"],
            "pdf_page": pdf_page,
            "outcome": outcome,
            "selected_tiff_path": selected_path,
            "selected_tiff_relation": selected_tiff_relation,
            "replaced_automatic_tiff_path": choices["automatic_path"],
            "note": note.strip(),
            "actor": self.actor,
            "decided_utc": utc_now(),
            "deletion_authorized": False,
        }
        with self.lock:
            decisions = self.page_decisions()
            decisions[(key, pdf_page)] = record
            ordered = [decisions[item] for item in sorted(decisions, key=lambda item: (natural_key(self.objects[item[0]]["inventory"]), item[1]))]
            atomic_replace(PAGE_DECISIONS, csv_payload(ordered, PAGE_DECISION_FIELDS))
            self._set_page_decisions_cache(decisions)
            self._rebuild_candidates_for_object(key, decisions)
            if (
                selected_candidate
                and selected_candidate["candidate_id"] not in self.candidates
            ):
                self.candidates_by_object[key].append(selected_candidate)
                self.candidates[selected_candidate["candidate_id"]] = selected_candidate
                self._validate_candidate_groups(key, self.candidates_by_object[key])
            self._update_page_decision_projection(key, decisions)
        corrected = next(
            (candidate for candidate in self.candidates_by_object[key] if candidate["role"] == "human_corrected_tiff_master_set"),
            None,
        )
        if corrected is None:
            raise WorkbenchError("human-corrected TIFF candidate was not rebuilt")
        return corrected["candidate_id"]

    def finalize_reviewed_object(self, key: str) -> bool:
        """Synchronize an existing TIFF Master after its final page correction."""
        decision = self.decisions().get(key, {})
        if (
            decision.get("outcome") != "selected_master"
            or decision.get("master_representation_role") != "ordered_tiff_page_set"
            or self.review_items(key)
        ):
            return False
        corrected = next(
            (
                candidate
                for candidate in self.candidates_by_object[key]
                if candidate["role"] == "human_corrected_tiff_master_set"
                and candidate["eligible"]
            ),
            None,
        )
        if corrected is None:
            return False
        stale_ids = {
            candidate["candidate_id"]
            for candidate in self.candidates_by_object[key]
            if candidate["role"] == "human_corrected_tiff_master_set"
            and candidate["candidate_id"] != corrected["candidate_id"]
        }
        self.candidates_by_object[key] = [
            candidate
            for candidate in self.candidates_by_object[key]
            if candidate["candidate_id"] not in stale_ids
        ]
        for candidate_id in stale_ids:
            self.candidates.pop(candidate_id, None)
        self.save_decision(
            key,
            "selected_master",
            corrected["candidate_id"],
            decision.get("note", ""),
        )
        return True

    def save_decision(self, key: str, outcome: str, selected_id: str, note: str) -> None:
        source_guard()
        if key not in self.objects:
            raise WorkbenchError("unknown museum-digital object")
        if outcome not in {"selected_master", "selected_pdf_master", "unresolved"}:
            raise WorkbenchError("unsupported outcome")
        pdf_only_master = outcome == "selected_pdf_master"
        pdf_master_basis = self.pdf_only_master_basis(key) if pdf_only_master else ""
        stored_outcome = "selected_master" if pdf_only_master else outcome
        selected = self.candidates.get(selected_id)
        if pdf_only_master:
            if selected_id:
                raise WorkbenchError("PDF-only Master decisions cannot select a TIFF set")
            if not pdf_master_basis:
                raise WorkbenchError(
                    "MD PDF can be selected as Master only after complete no-TIFF page review or indexed proof that no TIFF family exists"
                )
        elif outcome == "selected_master":
            if not selected or selected["object_key"] != key or not selected["eligible"]:
                raise WorkbenchError("select exactly one eligible ordered TIFF Master set")
        elif selected_id:
            raise WorkbenchError("unresolved decisions cannot select a TIFF set")
        obj = self.objects[key]
        selected_paths = list(selected["paths"] if selected else [])
        redundant = sorted(
            {
                path
                for group in self.candidates_by_object[key]
                for path in group["paths"]
            }
            - set(selected_paths),
            key=natural_key,
        ) if selected else []
        if set(selected_paths) & set(redundant):
            raise WorkbenchError("selected and redundant paths overlap")
        reviewed_missing_pages = (
            list(range(1, obj["md_pdf_pages"] + 1))
            if pdf_master_basis == "complete_explicit_no_tiff_page_review"
            else list(selected.get("reviewed_missing_pages", []) if selected else [])
        )
        master_role = (
            "museum_digital_pdf"
            if pdf_only_master
            else "ordered_tiff_page_set"
            if selected
            else ""
        )
        record = {
            "object_key": key,
            "md_object_id": obj["md_object_id"],
            "inventory": obj["inventory"],
            "outcome": stored_outcome,
            "master_representation_role": master_role,
            "selected_tiff_candidate_id": selected_id if selected else "",
            "selected_tiff_master_paths": selected_paths,
            "selected_non_tiff_master_paths": [],
            "reviewed_missing_pdf_pages": reviewed_missing_pages,
            "retained_md_pdf_path": obj["md_pdf_path"] if stored_outcome == "selected_master" else "",
            "redundant_candidate_paths": redundant,
            "note": note.strip(),
            "actor": self.actor,
            "decided_utc": utc_now(),
            "accession_state": "not_accessioned",
            "accession_receipt_path": "",
            "metadata_authority": (
                f"pre-accession_MD_pdf_master_{pdf_master_basis}"
                if pdf_master_basis
                else "pre-accession_MD_projection"
            ),
            "deletion_authorized": False,
        }
        with self.lock:
            decisions = self.decisions()
            decisions[key] = record
            ordered = [decisions[obj_key] for obj_key in self.objects if obj_key in decisions]
            atomic_replace(DECISIONS, csv_payload(ordered, DECISION_FIELDS))
            self.refresh()

    def _non_tiff_master_record(self, key: str, role: str, note: str) -> dict[str, Any]:
        if key not in self.objects:
            raise WorkbenchError("unknown museum-digital object")
        if role == "ordered_jpeg_page_set":
            audit_rows = sorted(
                (
                    row for row in read_csv(PAGE_EVIDENCE_AUDIT)
                    if row.get("object_key") == key
                ),
                key=lambda row: int(row["pdf_page"]),
            )
            expected_pages = list(range(1, self.objects[key]["md_pdf_pages"] + 1))
            if [int(row["pdf_page"]) for row in audit_rows] != expected_pages:
                raise WorkbenchError(f"incomplete derivative page evidence for {key}")
            paths = [row.get("best_derivative_path", "") for row in audit_rows]
            if (
                len(paths) != len(set(paths))
                or any(not path.lower().endswith((".jpg", ".jpeg")) for path in paths)
                or any(path not in self.source_index for path in paths)
            ):
                raise WorkbenchError(f"invalid existing JPEG Master set for {key}")
            reviewed_missing_pages: list[int] = []
            authority = "pre-accession_human_selected_existing_jpeg_page_set_no_tiff_source"
        elif role == "surviving_pdf_exception":
            path = SURVIVING_PDF_EXCEPTIONS.get(key, "")
            if not path or path not in self.source_index or not path.lower().endswith(".pdf"):
                raise WorkbenchError(f"unapproved surviving PDF exception for {key}")
            paths = [path]
            reviewed_missing_pages = list(range(1, self.objects[key]["md_pdf_pages"] + 1))
            authority = "pre-accession_human_selected_surviving_pdf_exception_no_corresponding_image_source"
        else:
            raise WorkbenchError("unsupported non-TIFF Master role")
        for path in paths:
            source_path(path)
        obj = self.objects[key]
        return {
            "object_key": key,
            "md_object_id": obj["md_object_id"],
            "inventory": obj["inventory"],
            "outcome": "selected_master",
            "master_representation_role": role,
            "selected_tiff_candidate_id": "",
            "selected_tiff_master_paths": [],
            "selected_non_tiff_master_paths": paths,
            "reviewed_missing_pdf_pages": reviewed_missing_pages,
            "retained_md_pdf_path": obj["md_pdf_path"],
            "redundant_candidate_paths": [],
            "note": note.strip(),
            "actor": self.actor,
            "decided_utc": utc_now(),
            "accession_state": "not_accessioned",
            "accession_receipt_path": "",
            "metadata_authority": authority,
            "deletion_authorized": False,
        }

    def save_non_tiff_master_decisions(
        self,
        selections: dict[str, tuple[str, str]],
    ) -> None:
        """Save an explicitly approved bounded set of existing non-TIFF Masters."""
        source_guard()
        records = {
            key: self._non_tiff_master_record(key, role, note)
            for key, (role, note) in selections.items()
        }
        with self.lock:
            decisions = self.decisions()
            decisions.update(records)
            ordered = [decisions[obj_key] for obj_key in self.objects if obj_key in decisions]
            atomic_replace(DECISIONS, csv_payload(ordered, DECISION_FIELDS))
            self.refresh()

    def set_accession_state(self, key: str, state: str, receipt_path: str) -> None:
        """Update drain planning when the separate accession process reports state."""
        if key not in self.objects:
            raise WorkbenchError("unknown museum-digital object")
        decisions = self.decisions()
        decision = decisions.get(key)
        if not decision or decision["outcome"] != "selected_master":
            raise WorkbenchError("object must have a selected Master before accession")
        current = decision.get("accession_state", "not_accessioned")
        allowed = {
            "not_accessioned": "accession_in_progress",
            "accession_in_progress": "verified_authority",
        }
        if allowed.get(current) != state:
            raise WorkbenchError(f"invalid accession transition {current} -> {state}")
        if state == "verified_authority":
            if not receipt_path:
                raise WorkbenchError("verified accession requires a receipt path")
            receipt = Path(receipt_path).resolve(strict=True)
            receipt.relative_to(Path("/mnt/volume/iss-archive").resolve(strict=True))
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            if payload.get("object_key") != key or payload.get("accession_verified") is not True:
                raise WorkbenchError("accession receipt does not verify this object")
            decision["accession_receipt_path"] = str(receipt)
            decision["metadata_authority"] = "archive_unit_authority_MD_projection"
        decision["accession_state"] = state
        decisions[key] = decision
        ordered = [decisions[obj_key] for obj_key in self.objects if obj_key in decisions]
        atomic_replace(DECISIONS, csv_payload(ordered, DECISION_FIELDS))
        self.refresh()

    def _path_bindings(self) -> tuple[
        dict[str, set[str]], dict[str, set[str]], dict[str, set[str]], dict[str, str]
    ]:
        candidate_objects: dict[str, set[str]] = collections.defaultdict(set)
        selected_objects: dict[str, set[str]] = collections.defaultdict(set)
        selected_roles: dict[str, set[str]] = collections.defaultdict(set)
        pdf_objects: dict[str, str] = {}
        for key, obj in self.objects.items():
            pdf_objects[obj["md_pdf_path"]] = key
            for group in self.candidates_by_object[key]:
                for path in group["paths"]:
                    candidate_objects[path].add(key)
        for key, decision in self.decisions().items():
            if decision["outcome"] == "selected_master":
                selected_paths = (
                    decision["selected_tiff_master_paths"]
                    + decision["selected_non_tiff_master_paths"]
                )
                for path in selected_paths:
                    selected_objects[path].add(key)
                    selected_roles[path].add(decision["master_representation_role"])
        return candidate_objects, selected_objects, selected_roles, pdf_objects

    def _source_rows(self) -> list[dict[str, Any]]:
        decisions = self.decisions()
        candidate_objects, selected_objects, selected_roles, pdf_objects = self._path_bindings()
        objects_by_base: dict[str, set[str]] = collections.defaultdict(set)
        for key, obj in self.objects.items():
            objects_by_base[obj["base_inventory"]].add(key)
        resolved = {key for key, row in decisions.items() if row["outcome"] == "selected_master"}

        def object_drain_state(key: str) -> str:
            state = decisions.get(key, {}).get("accession_state", "not_accessioned")
            if state == "verified_authority":
                return "listed_deferred_drain_after_verified_accession"
            if state == "accession_in_progress":
                return "blocked_accession_in_progress"
            return "planned_deferred_drain_after_verified_accession"

        def combined_drain_state(keys: set[str]) -> str:
            states = {object_drain_state(key) for key in keys}
            if states == {"listed_deferred_drain_after_verified_accession"}:
                return "listed_deferred_drain_after_verified_accession"
            if "blocked_accession_in_progress" in states:
                return "blocked_accession_in_progress"
            return "planned_deferred_drain_after_verified_accession"

        pdf_occurrence_object: dict[str, str] = {}
        for row in self.pdf_rows:
            key = f"md:{row['md_object_id']}"
            if key in self.objects:
                pdf_occurrence_object[row["source_path"]] = key
        rows: list[dict[str, Any]] = []
        for source in self.old_source_rows:
            path = source["source_path"]
            base = source["carrier_key"] if source["carrier_key"] in objects_by_base else ""
            object_keys = set(candidate_objects.get(path, set()))
            if path in pdf_occurrence_object:
                object_keys.add(pdf_occurrence_object[path])
            appraisal = self.appraisal.get(path, {})
            suggestion = appraisal.get("suggested_relationship", "")
            document_decision = appraisal.get("decision") or ""
            document_role = appraisal.get("representation_role") or ""
            if selected_objects.get(path):
                roles = selected_roles[path]
                if roles == {"ordered_tiff_page_set"}:
                    disposition = "selected_ordered_tiff_master_input"
                    relation = "tiff_master_input"
                elif roles == {"ordered_jpeg_page_set"}:
                    disposition = "selected_existing_jpeg_master_input"
                    relation = "jpeg_master_input"
                elif roles == {"surviving_pdf_exception"}:
                    disposition = "selected_surviving_pdf_master_exception_input"
                    relation = "pdf_master_exception_input"
                else:
                    raise WorkbenchError(f"conflicting selected Master roles for {path}")
                drain = combined_drain_state(selected_objects[path])
            elif path in pdf_objects and pdf_objects[path] in resolved:
                pdf_key = pdf_objects[path]
                if decisions[pdf_key].get("master_representation_role") == "museum_digital_pdf":
                    disposition = "selected_md_pdf_master_input"
                    relation = "md_pdf_master_input"
                else:
                    disposition = "retained_md_pdf_access_input"
                    relation = "md_pdf_access_catalogue_input"
                drain = object_drain_state(pdf_objects[path])
            elif path in pdf_occurrence_object and pdf_occurrence_object[path] in resolved:
                disposition = "redundant_pdf_occurrence_after_selection"
                relation = "redundant_source_evidence"
                drain = object_drain_state(pdf_occurrence_object[path])
            elif appraisal.get("selected_for_unit") is True:
                disposition = "confirmed_unit_document_input"
                relation = {
                    "selected_master": "document_master_input",
                    "access_derivative": "document_access_derivative_input",
                    "supplementary_representation": "document_supplementary_input",
                }.get(document_role, "documents_or_metadata_input")
                drain = "blocked_until_unit_verified_accession"
            elif document_decision.startswith("redundant_"):
                disposition = "confirmed_redundant_unit_document"
                relation = "redundant_document_evidence"
                drain = "blocked_until_unit_verified_accession"
            elif document_decision.startswith("exclude_"):
                disposition = "confirmed_outside_unit_documentation"
                relation = "non_unit_document_evidence"
                drain = "excluded_from_drain_scope"
            elif object_keys and object_keys <= resolved:
                disposition = "redundant_tiff_candidate_after_selection"
                relation = "redundant_source_evidence"
                drain = combined_drain_state(object_keys)
            elif base and objects_by_base[base] <= resolved:
                disposition = "redundant_carrier_visual_after_all_parts_resolved"
                relation = "redundant_source_evidence"
                drain = combined_drain_state(objects_by_base[base])
            elif suggestion.startswith("preserve_"):
                disposition = "confirmed_unit_document_input"
                relation = "documents_or_metadata_input"
                drain = "blocked_until_unit_verified_accession"
            elif suggestion.startswith("redundant_"):
                disposition = "confirmed_redundant_unit_document"
                relation = "redundant_document_evidence"
                drain = "blocked_until_unit_verified_accession"
            elif suggestion:
                disposition = "unit_document_or_metadata_pending_final_coverage"
                relation = "documents_or_metadata_evidence"
                drain = "blocked_until_document_appraisal_and_verified_accession"
            elif object_keys or base or path in pdf_occurrence_object:
                disposition = "candidate_pending_object_reconciliation"
                relation = "unresolved_source_evidence"
                drain = "excluded_from_drain_until_object_resolved"
            else:
                disposition = "context_or_metadata_pending_scope_confirmation"
                relation = "comparison_evidence_only"
                drain = "excluded_from_drain_scope"
            rows.append({
                "source_path": path,
                "object_keys": sorted(object_keys, key=natural_key),
                "base_inventory": base,
                "source_kind": source["source_kind"],
                "candidate_role": source["candidate_role"],
                "disposition": disposition,
                "accession_relation": relation,
                "drain_state": drain,
                "size_bytes": source["size_bytes"],
                "content_sha256": source["content_sha256"],
                "evidence_basis": source["evidence_basis"],
                "deletion_authorized": False,
            })
        return rows

    def refresh(self) -> None:
        current_page_decisions = self.page_decisions()
        ordered_page_decisions = [
            current_page_decisions[item]
            for item in sorted(
                current_page_decisions,
                key=lambda item: (natural_key(self.objects[item[0]]["inventory"]), item[1]),
            )
        ]
        atomic_replace(PAGE_DECISIONS, csv_payload(ordered_page_decisions, PAGE_DECISION_FIELDS))
        self._set_page_decisions_cache(current_page_decisions)
        current_decisions = self.decisions()
        ordered_decisions = [
            current_decisions[key] for key in self.objects if key in current_decisions
        ]
        atomic_replace(DECISIONS, csv_payload(ordered_decisions, DECISION_FIELDS))
        rows = self._source_rows()
        atomic_replace(SOURCE_DISPOSITION, csv_payload(rows, DISPOSITION_FIELDS))
        drain_rows = [row for row in rows if not row["drain_state"].startswith("excluded_")]
        atomic_replace(DRAIN_LIST, csv_payload(drain_rows, DISPOSITION_FIELDS))
        self._write_database(rows)
        decisions = self.decisions()
        payload = {
            "generated_utc": utc_now(),
            "version": VERSION,
            "source_mount": self.mount,
            "model": "one MD object equals one archive object; existing TIFF page set is preferred; explicitly approved existing JPEG sets or a surviving PDF exception are allowed; MD PDF remains access representation unless it is the approved exception",
            "objects": len(self.objects),
            "objects_with_eligible_tiff_candidate": sum(any(item["eligible"] for item in groups) for groups in self.candidates_by_object.values()),
            "resolved_objects": sum(row["outcome"] == "selected_master" for row in decisions.values()),
            "master_role_counts": dict(sorted(collections.Counter(
                row["master_representation_role"] for row in decisions.values()
                if row["outcome"] == "selected_master"
            ).items())),
            "page_decisions": len(current_page_decisions),
            "documentation_candidates": len(self.appraisal),
            "documentation_selected_masters": sum(
                row.get("representation_role") == "selected_master"
                for row in self.appraisal.values()
            ),
            "documentation_selected_representations": sum(
                row.get("selected_for_unit") is True
                for row in self.appraisal.values()
            ),
            "documentation_unresolved": sum(
                not row.get("decision") for row in self.appraisal.values()
            ),
            "source_disposition_rows": len(rows),
            "drain_list_rows": len(drain_rows),
            "deletion_authorized_true": 0,
            "published_archive_modified": False,
        }
        atomic_replace(PREFLIGHT, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())

    def _update_page_decision_projection(
        self,
        key: str,
        page_decisions: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        """Update the affected object in SQLite without rebuilding drain records."""
        con = sqlite3.connect(DATABASE, timeout=10)
        try:
            with con:
                candidate_ids = [
                    row[0]
                    for row in con.execute(
                        "SELECT candidate_id FROM tiff_candidates WHERE object_key = ?",
                        (key,),
                    )
                ]
                con.executemany(
                    "DELETE FROM tiff_candidate_members WHERE candidate_id = ?",
                    [(candidate_id,) for candidate_id in candidate_ids],
                )
                con.execute("DELETE FROM tiff_candidates WHERE object_key = ?", (key,))
                for candidate in self.candidates_by_object[key]:
                    con.execute("INSERT INTO tiff_candidates VALUES (?,?,?,?,?,?,?,?)", (
                        candidate["candidate_id"], candidate["object_key"], candidate["role"], candidate["label"],
                        int(candidate["eligible"]), candidate["evidence"], candidate["file_count"], candidate["size_bytes"],
                    ))
                    for sequence, path in enumerate(candidate["paths"], 1):
                        con.execute(
                            "INSERT INTO tiff_candidate_members VALUES (?,?,?)",
                            (candidate["candidate_id"], sequence, path),
                        )
                con.execute("DELETE FROM page_decisions WHERE object_key = ?", (key,))
                for (decision_key, _), decision in page_decisions.items():
                    if decision_key != key:
                        continue
                    con.execute("INSERT INTO page_decisions VALUES (?,?,?,?,?,?,?,?,?,0)", (
                        decision["object_key"], decision["pdf_page"], decision["outcome"],
                        decision["selected_tiff_path"], decision["selected_tiff_relation"],
                        decision["replaced_automatic_tiff_path"],
                        decision["note"], decision["actor"], decision["decided_utc"],
                    ))
            database_page_decisions = con.execute(
                "SELECT count(*) FROM page_decisions"
            ).fetchone()[0]
            if database_page_decisions != len(page_decisions):
                raise WorkbenchError("page-decision manifest and SQLite projection disagree")
        finally:
            con.close()

        payload = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
        payload.update({
            "generated_utc": utc_now(),
            "version": VERSION,
            "objects_with_eligible_tiff_candidate": sum(
                any(item["eligible"] for item in groups)
                for groups in self.candidates_by_object.values()
            ),
            "page_decisions": len(page_decisions),
        })
        atomic_replace(
            PREFLIGHT,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        )

    def _write_database(self, source_rows: list[dict[str, Any]]) -> None:
        temporary = DATABASE.with_name(f".{DATABASE.name}.tmp-{os.getpid()}")
        if temporary.exists():
            temporary.unlink()
        con = sqlite3.connect(temporary)
        try:
            con.executescript("""
                PRAGMA journal_mode=DELETE;
                CREATE TABLE objects (
                    object_key TEXT PRIMARY KEY, md_object_id TEXT UNIQUE NOT NULL,
                    inventory TEXT UNIQUE NOT NULL, base_inventory TEXT NOT NULL,
                    title TEXT NOT NULL, md_pdf_path TEXT NOT NULL,
                    md_pdf_sha256 TEXT NOT NULL, md_pdf_pages INTEGER NOT NULL,
                    metadata_authority TEXT NOT NULL
                );
                CREATE TABLE tiff_candidates (
                    candidate_id TEXT PRIMARY KEY, object_key TEXT NOT NULL REFERENCES objects,
                    role TEXT NOT NULL, label TEXT NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    evidence TEXT NOT NULL, file_count INTEGER NOT NULL, size_bytes INTEGER NOT NULL
                );
                CREATE TABLE tiff_candidate_members (
                    candidate_id TEXT NOT NULL REFERENCES tiff_candidates,
                    sequence INTEGER NOT NULL, source_path TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, sequence)
                );
                CREATE TABLE decisions (
                    object_key TEXT PRIMARY KEY REFERENCES objects, outcome TEXT NOT NULL,
                    master_representation_role TEXT NOT NULL,
                    selected_tiff_candidate_id TEXT NOT NULL,
                    selected_non_tiff_master_paths_json TEXT NOT NULL,
                    retained_md_pdf_path TEXT NOT NULL,
                    reviewed_missing_pdf_pages_json TEXT NOT NULL,
                    note TEXT NOT NULL, actor TEXT NOT NULL, decided_utc TEXT NOT NULL,
                    accession_state TEXT NOT NULL, accession_receipt_path TEXT NOT NULL,
                    metadata_authority TEXT NOT NULL,
                    deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
                );
                CREATE TABLE page_decisions (
                    object_key TEXT NOT NULL REFERENCES objects, pdf_page INTEGER NOT NULL,
                    outcome TEXT NOT NULL, selected_tiff_path TEXT NOT NULL,
                    selected_tiff_relation TEXT NOT NULL,
                    replaced_automatic_tiff_path TEXT NOT NULL, note TEXT NOT NULL,
                    actor TEXT NOT NULL, decided_utc TEXT NOT NULL,
                    deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0),
                    PRIMARY KEY(object_key, pdf_page)
                );
                CREATE TABLE source_disposition (
                    source_path TEXT PRIMARY KEY, object_keys_json TEXT NOT NULL,
                    base_inventory TEXT NOT NULL, source_kind TEXT NOT NULL,
                    disposition TEXT NOT NULL, accession_relation TEXT NOT NULL,
                    drain_state TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
                );
            """)
            for obj in self.objects.values():
                con.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?)", (
                    obj["object_key"], obj["md_object_id"], obj["inventory"], obj["base_inventory"],
                    obj["title"], obj["md_pdf_path"], obj["md_pdf_sha256"], obj["md_pdf_pages"], obj["metadata_authority"],
                ))
            for groups in self.candidates_by_object.values():
                for candidate in groups:
                    con.execute("INSERT INTO tiff_candidates VALUES (?,?,?,?,?,?,?,?)", (
                        candidate["candidate_id"], candidate["object_key"], candidate["role"], candidate["label"],
                        int(candidate["eligible"]), candidate["evidence"], candidate["file_count"], candidate["size_bytes"],
                    ))
                    for sequence, path in enumerate(candidate["paths"], 1):
                        con.execute("INSERT INTO tiff_candidate_members VALUES (?,?,?)", (candidate["candidate_id"], sequence, path))
            for decision in self.decisions().values():
                con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)", (
                    decision["object_key"], decision["outcome"], decision["master_representation_role"],
                    decision["selected_tiff_candidate_id"],
                    json.dumps(decision["selected_non_tiff_master_paths"]),
                    decision["retained_md_pdf_path"], json.dumps(decision["reviewed_missing_pdf_pages"]),
                    decision["note"], decision["actor"], decision["decided_utc"],
                    decision["accession_state"], decision.get("accession_receipt_path", ""),
                    decision["metadata_authority"],
                ))
            for decision in self.page_decisions().values():
                con.execute("INSERT INTO page_decisions VALUES (?,?,?,?,?,?,?,?,?,0)", (
                    decision["object_key"], decision["pdf_page"], decision["outcome"],
                    decision["selected_tiff_path"], decision["selected_tiff_relation"],
                    decision["replaced_automatic_tiff_path"],
                    decision["note"], decision["actor"], decision["decided_utc"],
                ))
            for row in source_rows:
                con.execute("INSERT INTO source_disposition VALUES (?,?,?,?,?,?,?,0)", (
                    row["source_path"], json.dumps(row["object_keys"]), row["base_inventory"], row["source_kind"],
                    row["disposition"], row["accession_relation"], row["drain_state"],
                ))
            con.commit()
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WorkbenchError("generated SQLite integrity check failed")
        finally:
            con.close()
        os.chmod(temporary, 0o644)
        os.replace(temporary, DATABASE)

    def check(self) -> dict[str, Any]:
        mount = source_guard()
        source_rows = read_csv(SOURCE_DISPOSITION)
        drain_rows = read_csv(DRAIN_LIST)
        decisions = self.decisions()
        page_decisions = self.page_decisions()
        appraisal_rows = list(self.appraisal.values())
        if len(source_rows) != 9256 or len({row["source_path"] for row in source_rows}) != len(source_rows):
            raise WorkbenchError("source disposition must contain 9,256 unique paths")
        if any(row["deletion_authorized"].casefold() != "false" for row in source_rows + drain_rows):
            raise WorkbenchError("deletion authorization must remain false")
        if any(row["deletion_authorized"] for row in page_decisions.values()):
            raise WorkbenchError("page decisions cannot authorize deletion")
        if (
            len(appraisal_rows) != 297
            or any(not row.get("decision") for row in appraisal_rows)
            or any(row.get("deletion_authorized") for row in appraisal_rows)
            or sum(row.get("representation_role") == "selected_master" for row in appraisal_rows) != 10
            or sum(row.get("selected_for_unit") is True for row in appraisal_rows) != 15
        ):
            raise WorkbenchError("documentation appraisal is incomplete or violates selection invariants")
        source_by_path = {row["source_path"]: row for row in source_rows}
        for row in appraisal_rows:
            projected = source_by_path.get(row["source_path"], {})
            if row.get("selected_for_unit") is True:
                expected_disposition = "confirmed_unit_document_input"
            elif str(row["decision"]).startswith("redundant_"):
                expected_disposition = "confirmed_redundant_unit_document"
            else:
                expected_disposition = "confirmed_outside_unit_documentation"
            if projected.get("disposition") != expected_disposition:
                raise WorkbenchError(f"documentation/source projection disagreement: {row['source_path']}")
        if {row["source_path"] for row in drain_rows} - {row["source_path"] for row in source_rows}:
            raise WorkbenchError("drain list is not a subset of source disposition")
        for key, decision in decisions.items():
            if decision["outcome"] != "selected_master":
                continue
            master_role = decision.get("master_representation_role", "")
            if master_role == "ordered_tiff_page_set":
                if decision["selected_non_tiff_master_paths"]:
                    raise WorkbenchError(f"TIFF Master cannot select non-TIFF inputs for {key}")
                candidate = self.candidates.get(decision["selected_tiff_candidate_id"])
                if not candidate or not candidate["eligible"] or candidate["object_key"] != key:
                    raise WorkbenchError(f"invalid selected TIFF candidate for {key}")
            elif master_role == "museum_digital_pdf":
                if (
                    decision["selected_tiff_candidate_id"]
                    or decision["selected_tiff_master_paths"]
                    or decision["selected_non_tiff_master_paths"]
                ):
                    raise WorkbenchError(f"PDF-only Master cannot select TIFF inputs for {key}")
                pdf_master_basis = self.pdf_only_master_basis(key, page_decisions)
                if not pdf_master_basis:
                    raise WorkbenchError(f"PDF-only Master lacks complete no-TIFF page review for {key}")
                expected_pages = (
                    list(range(1, self.objects[key]["md_pdf_pages"] + 1))
                    if pdf_master_basis == "complete_explicit_no_tiff_page_review"
                    else []
                )
                if decision["reviewed_missing_pdf_pages"] != expected_pages:
                    raise WorkbenchError(f"wrong reviewed missing PDF pages for {key}")
            elif master_role == "ordered_jpeg_page_set":
                if decision["selected_tiff_candidate_id"] or decision["selected_tiff_master_paths"]:
                    raise WorkbenchError(f"JPEG Master cannot select TIFF inputs for {key}")
                expected = self._non_tiff_master_record(key, master_role, "")["selected_non_tiff_master_paths"]
                if decision["selected_non_tiff_master_paths"] != expected:
                    raise WorkbenchError(f"wrong ordered JPEG Master set for {key}")
                if decision["reviewed_missing_pdf_pages"]:
                    raise WorkbenchError(f"JPEG Master cannot claim missing pages for {key}")
            elif master_role == "surviving_pdf_exception":
                if decision["selected_tiff_candidate_id"] or decision["selected_tiff_master_paths"]:
                    raise WorkbenchError(f"surviving PDF exception cannot select TIFF inputs for {key}")
                expected_record = self._non_tiff_master_record(key, master_role, "")
                if decision["selected_non_tiff_master_paths"] != expected_record["selected_non_tiff_master_paths"]:
                    raise WorkbenchError(f"wrong surviving PDF exception for {key}")
                if decision["reviewed_missing_pdf_pages"] != expected_record["reviewed_missing_pdf_pages"]:
                    raise WorkbenchError(f"wrong reviewed missing pages for {key}")
            else:
                raise WorkbenchError(f"unknown Master representation role for {key}")
            if decision["retained_md_pdf_path"] != self.objects[key]["md_pdf_path"]:
                raise WorkbenchError(f"wrong retained MD PDF for {key}")
        con = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            object_count = con.execute("SELECT count(*) FROM objects").fetchone()[0]
        finally:
            con.close()
        if integrity != "ok" or object_count != 73:
            raise WorkbenchError("derived SQLite validation failed")
        pending_remnants = self.remnants()
        remnant_counts = collections.Counter(
            row["category"] for row in pending_remnants
        )
        review_items = self.review_items()
        review_counts = collections.Counter(
            category for row in review_items for category in row["categories"]
        )
        return {
            "ok": True,
            "version": VERSION,
            "source_mount": mount,
            "objects": 73,
            "objects_with_eligible_tiff_candidate": sum(any(item["eligible"] for item in groups) for groups in self.candidates_by_object.values()),
            "resolved": sum(row["outcome"] == "selected_master" for row in decisions.values()),
            "page_decisions": len(page_decisions),
            "documentation_candidates": len(appraisal_rows),
            "documentation_selected_masters": sum(
                row.get("representation_role") == "selected_master" for row in appraisal_rows
            ),
            "documentation_selected_representations": sum(
                row.get("selected_for_unit") is True for row in appraisal_rows
            ),
            "documentation_unresolved": sum(not row.get("decision") for row in appraisal_rows),
            "review_pages": len(review_items),
            "review_objects": len({row["object_key"] for row in review_items}),
            "review_missing": review_counts["missing"],
            "review_duplicated": review_counts["duplicated"],
            "review_uncertain": review_counts["uncertain"],
            "remnants_pending": len(pending_remnants),
            "remnants_missing_tiff": remnant_counts["missing_tiff_candidate"],
            "remnants_ambiguous_automatic": remnant_counts["ambiguous_automatic"],
            "source_disposition_rows": len(source_rows),
            "drain_list_rows": len(drain_rows),
            "deletion_authorized_true": 0,
            "sqlite_integrity": integrity,
        }

    def preview_paths(self) -> set[str]:
        if self._preview_paths_cache is not None:
            return self._preview_paths_cache
        paths = {obj["md_pdf_path"] for obj in self.objects.values()}
        paths.update(path for groups in self.candidates_by_object.values() for group in groups for path in group["paths"])
        paths.update(path for obj in self.objects.values() for path in self._family_paths(obj))
        paths.update(row["best_tiff_path"] for row in self.remnant_audit.values())
        self._preview_paths_cache = paths
        return self._preview_paths_cache

    def _preview_cache_path(self, kind: str, binding: str, size: int) -> Path:
        digest = hashlib.sha256(f"{kind}\0{binding}\0{size}".encode()).hexdigest()[:32]
        return RECON / f"{PREVIEW_PREFIX}{kind}-{size}-{digest}.jpg"

    def tiff_preview_cache_path(self, path: str, size: int) -> Path:
        indexed = self.source_index.get(path, {})
        identity = indexed.get("content_sha256", "")
        if not identity:
            source = source_path(path)
            stat = source.stat()
            identity = f"{source}\0{stat.st_size}\0{stat.st_mtime_ns}"
        return self._preview_cache_path("tiff", identity, size)

    def pdf_preview_cache_path(self, key: str, page: int, size: int) -> Path:
        obj = self.objects[key]
        return self._preview_cache_path(
            "pdf-page",
            f"{obj['md_pdf_sha256']}\0{page}",
            size,
        )

    def _render_tiff_preview(self, path: str, size: int) -> Path:
        target = self.tiff_preview_cache_path(path, size)
        if target.is_file():
            return target
        source = source_path(path)
        temporary = target.with_name(
            f".{target.stem}.tmp-{os.getpid()}-{threading.get_ident()}.jpg"
        )
        result = subprocess.run(
            [
                "vipsthumbnail",
                str(source),
                "--size",
                f"{size}x{size}",
                "--output",
                f"{temporary}[Q=82,strip]",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            raise WorkbenchError(
                f"TIFF preview unavailable: {Path(path).name}: {result.stderr[:240]}"
            )
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        return target

    def _render_pdf_page_preview(self, key: str, page: int, size: int) -> Path:
        target = self.pdf_preview_cache_path(key, page, size)
        if target.is_file():
            return target
        obj = self.objects[key]
        source = source_path(obj["md_pdf_path"])
        result = subprocess.run(
            [
                "pdftocairo", "-jpeg", "-jpegopt", "quality=82",
                "-f", str(page), "-l", str(page), "-singlefile",
                "-scale-to", str(size), str(source), "-",
            ],
            capture_output=True,
            timeout=45,
        )
        if result.returncode or not result.stdout:
            raise WorkbenchError(f"MD PDF page preview unavailable: {obj['inventory']} page {page}")
        atomic_replace(target, result.stdout)
        return target

    def generate_previews(self, object_key: str = "") -> dict[str, Any]:
        """Pre-render normal browser previews without changing any source file."""
        source_guard()
        if object_key and object_key not in self.objects:
            raise WorkbenchError("unknown object key for preview generation")
        objects = [self.objects[object_key]] if object_key else list(self.objects.values())
        tiff_paths: set[str] = set()
        pdf_pages: set[tuple[str, int]] = set()
        if object_key:
            tiff_paths.update(
                path
                for candidate in self.candidates_by_object[object_key]
                for path in candidate["paths"]
            )
            tiff_paths.update(self._family_paths(self.objects[object_key]))
            pdf_pages.update(
                (object_key, page)
                for page in range(1, self.objects[object_key]["md_pdf_pages"] + 1)
            )
        else:
            decided = set(self.page_decisions())
            objects_by_inventory = {obj["inventory"]: obj for obj in objects}
            for inventory, matches in self.page_rows.items():
                obj = objects_by_inventory.get(inventory)
                if obj is None:
                    continue
                counts = collections.Counter(
                    row.get("best_source_path", "")
                    for row in matches
                    if row.get("best_source_path", "")
                )
                for row in matches:
                    page = int(row["pdf_page"])
                    path = row.get("best_source_path", "")
                    indexed = self.source_index.get(path, {}) if path else {}
                    decoder_error = (
                        "error=" in indexed.get("technical_metadata", "")
                        or "index_state=error" in indexed.get("technical_metadata", "")
                    )
                    problem = (
                        not path
                        or counts.get(path, 0) > 1
                        or row.get("match_state") in {"needs_review", "missing_visual_match"}
                        or decoder_error
                    )
                    if not problem or (obj["object_key"], page) in decided:
                        continue
                    pdf_pages.add((obj["object_key"], page))
                    if path:
                        tiff_paths.add(path)
        tasks: list[tuple[str, Any]] = []
        for path in sorted(tiff_paths, key=natural_key):
            if not self.tiff_preview_cache_path(path, 1000).is_file():
                tasks.append(("tiff", path))
        for key, page in sorted(pdf_pages):
            if not self.pdf_preview_cache_path(key, page, 1200).is_file():
                tasks.append(("pdf", (key, page)))
        counts = collections.Counter()
        errors: list[str] = []

        def render(task: tuple[str, Any]) -> str:
            kind, value = task
            if kind == "tiff":
                self._render_tiff_preview(value, 1000)
            else:
                key, page = value
                self._render_pdf_page_preview(key, page, 1200)
            return kind

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(render, task): task for task in tasks}
            completed = 0
            for future in as_completed(futures):
                completed += 1
                try:
                    counts[future.result()] += 1
                except Exception as exc:
                    errors.append(str(exc))
                if completed % 100 == 0 or completed == len(tasks):
                    print(
                        f"preview generation: {completed}/{len(tasks)}; errors={len(errors)}",
                        flush=True,
                    )
        return {
            "requested_objects": len(objects),
            "tiff_previews_generated": counts["tiff"],
            "pdf_page_previews_generated": counts["pdf"],
            "already_cached_or_not_required": len(tiff_paths) + len(pdf_pages) - len(tasks),
            "errors": errors,
            "source_files_changed": 0,
            "deletion_authorized": False,
        }


STYLE = """
body{font-family:system-ui,sans-serif;background:#f3f0e8;color:#222;margin:0}main,header{max-width:1280px;margin:auto;padding:20px}header{display:flex;justify-content:space-between;gap:16px;align-items:center}
a{color:#175845}.tabs{display:flex;gap:8px}.tabs a,button{padding:9px 12px;border:1px solid #31584e;border-radius:6px;background:#fff;color:#173e34;text-decoration:none;cursor:pointer}
.notice{background:#fff1c7;border-left:5px solid #b56a00;padding:12px}.crop-relation{background:#dff2e3;border-left:5px solid #28713e;padding:12px;margin:10px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}.card,.panel,.candidate{background:#fff;border:1px solid #d4cec1;border-radius:9px;padding:15px;margin:12px 0}
.ok{color:#28713e;font-weight:700}.bad{color:#9b2d20;font-weight:700}.muted,.technical{color:#6d685f;font-size:.88em}.technical{font-family:ui-monospace,monospace;overflow-wrap:anywhere}.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.thumbs img{width:100%;height:240px;object-fit:contain;background:#222}
textarea{width:100%;min-height:65px}.decision{position:sticky;bottom:0;background:#fff4cf;border:2px solid #b9872f;padding:14px;border-radius:8px}details{max-height:260px;overflow:auto}.badge{display:inline-block;padding:3px 8px;border-radius:12px;background:#e5e1d7}
.button{display:inline-block;padding:9px 12px;border:1px solid #31584e;border-radius:6px;background:#175845;color:#fff;text-decoration:none}.diagnostics{display:flex;flex-wrap:wrap;gap:7px}.diagnostics span{padding:5px 8px;border-radius:5px;background:#eee9df}.diagnostics .problem{background:#f7d2cc;color:#7d211b}.compare{display:grid;grid-template-columns:1fr 1fr;gap:14px}.compare figure{margin:0;background:#fff;border:1px solid #d4cec1;border-radius:8px;padding:10px}.compare img{width:100%;height:68vh;object-fit:contain;background:#222}.compare figcaption{font-weight:700;margin-bottom:8px}.pager{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 0}.unresolved-nav{position:sticky;top:0;z-index:10;background:#fff4cf;border:2px solid #b9872f;padding:10px;border-radius:8px;margin:10px 0}.problem-pages{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.problem-pages a{padding:4px 7px;border:1px solid #b45748;border-radius:4px;text-decoration:none;background:#fff}.quick-correct{display:flex;align-items:center;justify-content:center;gap:12px;background:#dff2e3;border:2px solid #28713e;padding:12px;margin:10px 0;border-radius:8px}.quick-correct button{font-size:1.05rem;font-weight:700;background:#28713e;color:#fff}.placeholder{height:68vh;display:grid;place-items:center;background:#eee;color:#8c2c22;font-weight:700}.page-choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.page-choice{border:1px solid #d4cec1;border-radius:7px;padding:12px;background:#faf9f5}.page-choice img{width:100%;height:420px;object-fit:contain;background:#222}.page-choice button{width:100%;margin-top:8px}.zoom-link{display:block;text-align:center;font-weight:700}.zoom-link span{display:block;margin:6px 0}.mapping-form select{max-width:100%;padding:8px}.mapping-form textarea{margin:8px 0 12px}.object-preview{width:100%;height:300px;object-fit:contain;background:#222}.issue-list{display:grid;gap:8px}.issue-link{display:flex;justify-content:space-between;gap:12px;padding:10px;border:1px solid #d4cec1;border-radius:7px;background:#fff;text-decoration:none}.save-panel{background:#fff4cf;border:2px solid #b9872f;padding:14px;border-radius:8px;margin:14px 0}.save-panel fieldset{display:grid;gap:8px;margin:0 0 12px;border:0;padding:0}.save-panel legend{font-weight:700;margin-bottom:6px}@media(max-width:800px){.compare{grid-template-columns:1fr}.compare img,.placeholder{height:auto;min-height:320px}.page-choices{grid-template-columns:1fr}.page-choice img{height:auto;max-height:70vh}.quick-correct{display:block}}
"""


def layout(title: str, body: str) -> bytes:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{esc(title)}</title><style>{STYLE}</style></head><body><header><div><strong>Brigadebücher · collection 861</strong><br><span class='muted'>one PDF with its corresponding TIFF pages</span></div><nav class='tabs'><a href='/'>Review</a></nav></header><main>{body}</main></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    collection: Collection861MD

    def send_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def parameters(self) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.index()
            elif path == "/object":
                self.review_object_page()
            elif path == "/compare":
                self.review_page()
            elif path == "/pdf-page":
                self.pdf_page()
            elif path == "/preview":
                self.preview()
            elif path in {"/remnants", "/drain"}:
                self.redirect("/")
            elif path == "/health":
                self.send_bytes((json.dumps(self.collection.check(), indent=2) + "\n").encode(), "application/json")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_bytes(layout("Workbench error", f"<h1>Workbench error</h1><p class='bad'>{esc(exc)}</p>"), "text/html; charset=utf-8", HTTPStatus.BAD_REQUEST)

    def index(self) -> None:
        items = self.collection.review_items()
        by_object: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for item in items:
            by_object[item["object_key"]].append(item)
        cards = []
        for key, object_items in by_object.items():
            first = object_items[0]
            counts = collections.Counter(
                category for item in object_items for category in item["categories"]
            )
            badges = "".join(
                f"<span class='badge'>{count} {esc(category)}</span>"
                for category, count in (("missing", counts["missing"]), ("duplicated", counts["duplicated"]), ("uncertain", counts["uncertain"]))
                if count
            )
            preview = "/pdf-page?" + urlencode({"key": key, "page": first["pdf_page"]})
            cards.append(
                f"""<article class='card'><a href='/object?{urlencode({'key':key})}'><img class='object-preview' loading='lazy' src='{esc(preview)}' alt='{esc(first['inventory'])} PDF preview'></a><h2>{esc(first['inventory'])}</h2><p>{esc(first['title'])}</p><p>{badges}</p><a class='button' href='/object?{urlencode({'key':key})}'>Review object</a></article>"""
            )
        saved = "<p class='ok'>Decision saved. The next page needing review is shown below.</p>" if self.parameters().get("saved") else ""
        complete = "<p class='ok'>No pages currently need review.</p>" if not items else ""
        body = f"""<h1>PDF and corresponding TIFF review</h1>{saved}<div class='notice'><strong>One route:</strong> open an object, inspect only missing, duplicated, or uncertain pages, choose the corresponding TIFF, and save. Existing PDF and Master decisions do not block page correction. No deletion is authorized.</div><p><strong>{len(items)} page(s)</strong> in {len(by_object)} object(s) need a decision.</p>{complete}<section class='grid'>{''.join(cards)}</section>"""
        self.send_bytes(layout("Brigadebücher", body), "text/html; charset=utf-8")

    def review_object_page(self) -> None:
        key = self.parameters().get("key", "")
        row = next((item for item in self.collection.records() if item["object_key"] == key), None)
        if row is None:
            raise WorkbenchError("unknown object")
        items = self.collection.review_items(key)
        candidate = self.collection._remnant_candidate(key)
        tiff_count = candidate["file_count"] if candidate else 0
        first_page = items[0]["pdf_page"] if items else 1
        preview = "/pdf-page?" + urlencode({"key": key, "page": first_page})
        issue_links = "".join(
            f"""<a class='issue-link' href='/compare?{urlencode({'key':key,'page':item['pdf_page']})}'><strong>PDF page {item['pdf_page']}</strong><span>{esc(' · '.join(item['categories']))}</span></a>"""
            for item in items
        )
        decision = row["decision"] or {}
        master_label = (
            "PDF Master"
            if decision.get("master_representation_role") == "museum_digital_pdf"
            else "TIFF Master set"
            if decision.get("master_representation_role") == "ordered_tiff_page_set"
            else "JPEG Master set"
            if decision.get("master_representation_role") == "ordered_jpeg_page_set"
            else "Surviving PDF Master exception"
            if decision.get("master_representation_role") == "surviving_pdf_exception"
            else "Master decision not yet recorded"
        )
        action = (
            f"<p><a class='button' href='/compare?{urlencode({'key':key,'page':first_page})}'>Review first problem page</a></p>"
            if items
            else "<p class='ok'>This object has no remaining page problems.</p>"
        )
        body = f"""<p><a href='/'>← Review</a></p><h1>{esc(row['inventory'])}</h1><p>{esc(row['title'])}</p><section class='compare'><figure><figcaption>Object preview — PDF page {first_page} of {row['md_pdf_pages']}</figcaption><img src='{esc(preview)}' alt='PDF page {first_page}'></figure><section class='panel'><h2>One archival object</h2><p><strong>{esc(master_label)}</strong></p><p>PDF: {row['md_pdf_pages']} pages · corresponding TIFF set: {tiff_count} image(s)</p><p class='muted'>The Master status is preserved and does not block page review.</p>{action}</section></section><section class='panel'><h2>Pages needing a decision ({len(items)})</h2><div class='issue-list'>{issue_links or '<p class="ok">none</p>'}</div></section>"""
        self.send_bytes(layout(row["inventory"], body), "text/html; charset=utf-8")

    def review_page(self) -> None:
        parameters = self.parameters()
        key = parameters.get("key", "")
        obj = self.collection.objects.get(key)
        if obj is None:
            raise WorkbenchError("unknown review object")
        try:
            pdf_page = int(parameters.get("page", "1"))
        except ValueError as exc:
            raise WorkbenchError("review page must be a number") from exc
        if pdf_page < 1 or pdf_page > obj["md_pdf_pages"]:
            raise WorkbenchError("review page is outside the PDF")
        candidate = self.collection._remnant_candidate(key)
        if candidate is None:
            raise WorkbenchError("object has no TIFF comparison set")
        row = self.collection.comparison_rows(key, candidate["candidate_id"])[pdf_page - 1]
        choices = self.collection.page_choices(key, pdf_page)
        current = choices["current"]
        current_path = (
            current["selected_tiff_path"]
            if current and current["outcome"] == "selected_tiff"
            else row["tiff_path"]
        )
        current_relation = (
            current.get("selected_tiff_relation", "full_page")
            if current and current["outcome"] == "selected_tiff"
            else "full_page"
        )
        pdf_url = "/pdf-page?" + urlencode({"key": key, "page": pdf_page})
        if current_path:
            tiff_url = "/preview?" + urlencode({"path": current_path})
            right = f"<img src='{esc(tiff_url)}' alt='{esc(Path(current_path).name)}'>"
            right_label = Path(current_path).name + (
                " · confirmed rotated 90° CCW"
                if current_relation == "rotated_90_ccw"
                else ""
            )
        else:
            right = "<div class='placeholder'>No TIFF currently corresponds to this PDF page</div>"
            right_label = "missing"
        review_item = next(
            (item for item in self.collection.review_items(key) if item["pdf_page"] == pdf_page),
            None,
        )
        categories = review_item["categories"] if review_item else ["review"]
        choice_cards = "".join(
            f"""<article class='page-choice'><a class='zoom-link' target='_blank' href='/preview?{urlencode({'path':choice['path'],'size':2400})}'><img loading='lazy' src='/preview?{urlencode({'path':choice['path']})}' alt='{esc(choice['filename'])}'></a><h3>{esc(choice['filename'])}</h3><p>{esc('; '.join(choice['reasons']))}</p><button name='selected_path' value='{esc(choice['path'])}'>Choose this TIFF and save</button></article>"""
            for choice in choices["choices"]
        )
        saved = "<p class='ok'>Decision saved.</p>" if parameters.get("saved") else ""
        error = f"<p class='bad'>{esc(parameters.get('error'))}</p>" if parameters.get("error") else ""
        rotation_confirm = (
            f"""<form class='quick-correct' method='post' action='/page-decision'><input type='hidden' name='key' value='{esc(key)}'><input type='hidden' name='page' value='{pdf_page}'><input type='hidden' name='selected_path' value='{esc(current_path)}'><span>If this is the correct page but its orientation differs:</span><button name='selected_tiff_relation' value='rotated_90_ccw'>Confirm current TIFF — rotated 90° CCW</button></form>"""
            if current_path
            else ""
        )
        relation_options = "".join((
            f"<label><input type='radio' name='selected_tiff_relation' value='full_page'{' checked' if current_relation == 'full_page' else ''}> Same orientation</label>",
            f"<label><input type='radio' name='selected_tiff_relation' value='rotated_90_ccw'{' checked' if current_relation == 'rotated_90_ccw' else ''}> Correct TIFF, rotated 90° counter-clockwise</label>",
            f"<label><input type='radio' name='selected_tiff_relation' value='cropped_detail'{' checked' if current_relation == 'cropped_detail' else ''}> Intentional cropped detail of the chosen TIFF</label>",
        ))
        body = f"""<p><a href='/object?{urlencode({'key':key})}'>← {esc(obj['inventory'])}</a></p>{saved}{error}<h1>{esc(obj['inventory'])} · PDF page {pdf_page}</h1><p><span class='badge'>{esc(' · '.join(categories))}</span> Choose only the TIFF that corresponds to the PDF page, then save.</p><section class='compare'><figure><figcaption>PDF page {pdf_page}</figcaption><img src='{esc(pdf_url)}' alt='PDF page {pdf_page}'></figure><figure><figcaption>Current correspondence — {esc(right_label)}</figcaption>{right}</figure></section>{rotation_confirm}<form method='post' action='/page-decision'><input type='hidden' name='key' value='{esc(key)}'><input type='hidden' name='page' value='{pdf_page}'><section class='save-panel'><fieldset><legend>How the chosen TIFF corresponds</legend>{relation_options}</fieldset><label>Decision note (optional)<textarea name='note'>{esc(current.get('note', '') if current else '')}</textarea></label></section><section class='page-choices'>{choice_cards}</section><section class='save-panel'><button name='outcome' value='no_corresponding_tiff'>No corresponding TIFF — save decision</button><p class='technical'>The PDF, TIFF sources, and existing Master remain protected. deletion_authorized=false.</p></section></form>"""
        self.send_bytes(layout(f"Review {obj['inventory']} page {pdf_page}", body), "text/html; charset=utf-8")

    def remnants_page(self) -> None:
        parameters = self.parameters()
        kind = parameters.get("kind", "all")
        include_reviewed = parameters.get("show_reviewed") == "1"
        all_pending = self.collection.remnants()
        pending_counts = collections.Counter(row["category"] for row in all_pending)
        rows = self.collection.remnants(kind=kind, include_reviewed=include_reviewed)
        labels = {
            "missing_tiff_candidate": "Missing TIFF decision contradicted by rescan",
            "ambiguous_automatic": "Ambiguous automatic TIFF match",
        }
        cards = []
        for row in rows:
            key = row["object_key"]
            page = row["pdf_page"]
            compare_parameters = {
                "key": key,
                "candidate": row["candidate_id"],
                "page": page,
                "show_resolved": "1",
                "remnant": "1",
            }
            compare_url = "/compare?" + urlencode(compare_parameters)
            object_url = "/object?" + urlencode({"key": key})
            derivative = (
                "<p class='muted'><strong>Derivative evidence only — never Master:</strong> "
                f"{esc(row['best_derivative_path'])}</p>"
                if row.get("best_derivative_path")
                else ""
            )
            if row["object_resolved"]:
                action = (
                    f"<a class='button' href='{esc(compare_url)}'>View evidence</a>"
                    "<form method='post' action='/remnant-reopen'>"
                    f"<input type='hidden' name='key' value='{esc(key)}'>"
                    f"<input type='hidden' name='page' value='{page}'>"
                    "<button type='submit'>Reopen object and compare</button></form>"
                )
            else:
                action = f"<a class='button' href='{esc(compare_url)}'>Compare and decide</a>"
            review_badge = "reviewed after audit" if row["reviewed"] else "pending human review"
            cards.append(
                f"""<article class='card remnant-card'><span class='badge'>{esc(review_badge)}</span><h2>{esc(row['inventory'])} · PDF page {page}</h2><p><strong>{esc(labels[row['category']])}</strong></p><p>Full-unit rescan TIFF candidate: <strong>{esc(Path(row['best_tiff_path']).name)}</strong></p><p>Visual distance total / pHash: {esc(row['best_tiff_distance_total'] or 'n/a')} / {esc(row['best_tiff_distance_phash'] or 'n/a')}</p><p class='technical'>{esc(row['best_tiff_path'])}</p>{derivative}<p class='muted'>Current object state: {'resolved — explicit reopen required before saving' if row['object_resolved'] else 'unresolved'}. deletion_authorized=false.</p><div class='remnant-actions'><a href='{esc(object_url)}'>Open object</a>{action}</div></article>"""
            )
        saved_notice = "<p class='ok'>Remnant page decision saved.</p>" if parameters.get("saved") else ""
        error_notice = f"<p class='bad'>{esc(parameters.get('error'))}</p>" if parameters.get("error") else ""
        reviewed_toggle = (
            "/remnants?" + urlencode({"kind": kind})
            if include_reviewed
            else "/remnants?" + urlencode({"kind": kind, "show_reviewed": "1"})
        )
        body = f"""<h1>Remnants</h1>{saved_notice}{error_notice}<div class='notice'><strong>Bounded review:</strong> {len(all_pending)} pending pages from the completed full-unit rescan: {pending_counts['missing_tiff_candidate']} prior no-TIFF decisions with credible TIFF candidates and {pending_counts['ambiguous_automatic']} ambiguous automatic matches. Derivatives are visible evidence only and cannot be selected as Master.</div><nav class='tabs'><a href='/remnants'>All pending</a><a href='/remnants?kind=missing_tiff_candidate'>Missing TIFF ({pending_counts['missing_tiff_candidate']})</a><a href='/remnants?kind=ambiguous_automatic'>Ambiguous automatic ({pending_counts['ambiguous_automatic']})</a><a href='{esc(reviewed_toggle)}'>{'Hide reviewed' if include_reviewed else 'Show reviewed'}</a></nav><p>{len(rows)} row(s) shown. Existing decisions remain unchanged until an explicit reopen or page-save action.</p><section class='grid'>{''.join(cards)}</section>"""
        self.send_bytes(layout("Remnants", body), "text/html; charset=utf-8")

    def object_page(self, message: str = "", error: str = "") -> None:
        parameters = self.parameters()
        key = parameters.get("key", "")
        pdf_master_basis = self.collection.pdf_only_master_basis(key)
        pdf_only_eligible = bool(pdf_master_basis)
        if not message and pdf_master_basis == "indexed_carrier_has_no_tiff_family":
            message = "Carrier inventory confirms that no TIFF family exists. Save the MD PDF as the single Master."
        elif parameters.get("page_review") == "complete" and not message:
            message = (
                "Page review complete. Save the MD PDF as Master; every page is explicitly reviewed with no corresponding TIFF."
                if pdf_master_basis == "complete_explicit_no_tiff_page_review"
                else "Page review complete. Select the Human-corrected ordered TIFF set below, then click Save TIFF Master + retained MD PDF."
            )
        row = next((item for item in self.collection.records() if item["object_key"] == key), None)
        if row is None:
            raise WorkbenchError("unknown object")
        decision = row["decision"] or {}
        decided_pages = {
            pdf_page
            for (decision_key, pdf_page) in self.collection.page_decisions()
            if decision_key == key
        }
        status = f"<p class='ok'>{esc(message)}</p>" if message else ""
        if error:
            status += f"<p class='bad'>{esc(error)}</p>"
        cards = []
        for candidate in row["candidates"]:
            diagnostics = self.collection.comparison_summary(key, candidate["candidate_id"])
            unresolved = self.collection.unresolved_pages(key, candidate["candidate_id"])
            indices = sorted({0, len(candidate["paths"]) // 2, len(candidate["paths"]) - 1}) if candidate["paths"] else []
            thumbs = "".join(f"<img loading='lazy' src='/preview?{urlencode({'path':candidate['paths'][index]})}' alt='{esc(Path(candidate['paths'][index]).name)}'>" for index in indices)
            checked = " checked" if decision.get("selected_tiff_candidate_id") == candidate["candidate_id"] else ""
            selector = f"<label><input type='radio' name='selected_id' value='{candidate['candidate_id']}'{checked}> Select as ordered TIFF Master representation</label>" if candidate["eligible"] else "<p class='bad'>Review required before this set can be selected.</p>"
            first_problem = (
                unresolved[0]
                if unresolved
                else diagnostics["problem_pages"][0]
                if diagnostics["problem_pages"]
                else 1
            )
            diagnostic_badges = "".join((
                f"<span>{diagnostics['mapped_pages']}/{diagnostics['pdf_pages']} PDF pages mapped</span>",
                f"<span>{diagnostics['unique_tiffs']} unique TIFFs</span>",
                f"<span class='{'problem' if diagnostics['unreviewed_missing_pages'] else ''}'>{diagnostics['missing_pages']} missing ({diagnostics['reviewed_missing_pages']} reviewed)</span>",
                f"<span class='{'problem' if diagnostics['duplicate_page_assignments'] else ''}'>{diagnostics['duplicate_page_assignments']} PDF pages in repeated assignments</span>",
                f"<span>{diagnostics.get('cropped_detail_pages', 0)} confirmed cropped PDF pages</span>",
                f"<span class='{'problem' if diagnostics['needs_visual_review'] else ''}'>{diagnostics['needs_visual_review']} visual matches need review</span>",
                f"<span class='{'problem' if diagnostics['decoder_errors'] else ''}'>{diagnostics['decoder_errors']} decoder errors</span>",
            ))
            compare_parameters = {
                "key": key,
                "candidate": candidate["candidate_id"],
                "page": first_problem,
            }
            if first_problem in decided_pages and first_problem not in unresolved:
                compare_parameters["show_resolved"] = "1"
            compare_url = "/compare?" + urlencode(compare_parameters)
            repeated_items = []
            for group in diagnostics.get("repeated_assignment_groups", []):
                page_links = []
                for pdf_page in group["pdf_pages"]:
                    repeated_parameters = {
                        "key": key,
                        "candidate": candidate["candidate_id"],
                        "page": pdf_page,
                        "show_resolved": "1",
                    }
                    if candidate["role"] == "visual_match_tiff_master_set":
                        repeated_parameters["evidence"] = "automatic"
                    repeated_url = "/compare?" + urlencode(repeated_parameters)
                    page_links.append(
                        f"<a href='{esc(repeated_url)}'>PDF page {pdf_page}</a>"
                    )
                repeated_items.append(
                    f"<li><strong>{esc(group['tiff_filename'])}</strong> is assigned to "
                    f"{', '.join(page_links)}<div class='technical'>{esc(group['tiff_path'])}</div></li>"
                )
            repeated_details = (
                "<div class='notice repeated-assignments'><strong>Repeated TIFF assignments — open either page to correct it:</strong>"
                f"<ul>{''.join(repeated_items)}</ul></div>"
                if repeated_items
                else ""
            )
            crop_items = []
            for group in diagnostics.get("confirmed_crop_groups", []):
                crop_items.append(
                    f"<li><strong>{esc(group['tiff_filename'])}</strong> is retained once: "
                    f"PDF page {group['full_pdf_page']} is the full view; "
                    f"PDF page(s) {', '.join(map(str, group['cropped_pdf_pages']))} are confirmed cropped details.</li>"
                )
            crop_details = (
                "<div class='crop-relation'><strong>Confirmed crop relationships:</strong>"
                f"<ul>{''.join(crop_items)}</ul></div>"
                if crop_items
                else ""
            )
            cards.append(f"""<article class='candidate'><h2>{esc(candidate['label'])}</h2>{selector}<div class='diagnostics'>{diagnostic_badges}</div>{repeated_details}{crop_details}<p>{esc(candidate['evidence'])}</p><p><a class='button' href='{esc(compare_url)}'>Compare MD PDF and TIFF pages</a></p><div class='thumbs'>{thumbs}</div><details><summary>Ordered source paths</summary><ol>{''.join(f'<li class="technical">{esc(path)}</li>' for path in candidate['paths'])}</ol></details></article>""")
        pdf_master_button = (
            " <button name='outcome' value='selected_pdf_master'>Save MD PDF as Master — no corresponding TIFF pages</button>"
            if pdf_only_eligible
            else ""
        )
        body = f"""<p><a href='/'>← queue</a></p>{status}<h1>{esc(row['inventory'])}</h1><p>{esc(row['title'])}</p><section class='panel'><h2>museum-digital projection</h2><p><a target='_blank' href='{esc(row['object_url'])}'>md:{esc(row['md_object_id'])}</a></p><p><strong>{'Selectable PDF Master' if pdf_only_eligible else 'Retained access/catalogue PDF'}:</strong> {row['md_pdf_pages']} pages, {human_size(row['md_pdf_size'])}</p><p class='technical'>{esc(row['md_pdf_path'])}</p><p>{esc(row['verification_state'])}</p><p class='muted'>{esc(row['metadata_authority'])}</p></section><form method='post' action='/decision'><input type='hidden' name='key' value='{esc(key)}'>{''.join(cards)}<div class='decision'><label>Short note<textarea name='note'>{esc(decision.get('note',''))}</textarea></label><button name='outcome' value='selected_master'>Save TIFF Master + retained MD PDF</button>{pdf_master_button} <button name='outcome' value='unresolved'>Leave unresolved</button><p class='technical'>Every source remains protected; deletion_authorized=false.</p></div></form>"""
        self.send_bytes(layout(row["inventory"], body), "text/html; charset=utf-8")

    def compare(self) -> None:
        parameters = self.parameters()
        key = parameters.get("key", "")
        candidate_id = parameters.get("candidate", "")
        obj = self.collection.objects.get(key)
        candidate = self.collection.candidates.get(candidate_id)
        if obj is None:
            raise WorkbenchError("unknown comparison object")
        try:
            page_number_value = int(parameters.get("page", "1"))
        except ValueError as exc:
            raise WorkbenchError("comparison page must be a number") from exc
        if page_number_value < 1 or page_number_value > obj["md_pdf_pages"]:
            raise WorkbenchError("comparison page is outside the MD PDF")
        corrected = next(
            (
                item
                for item in self.collection.candidates_by_object[key]
                if item["role"] == "human_corrected_tiff_master_set"
            ),
            None,
        )
        stale_candidate = candidate is None
        automatic_evidence = bool(
            candidate
            and candidate.get("object_key") == key
            and candidate.get("role") == "visual_match_tiff_master_set"
        )
        if corrected and (
            stale_candidate
            or (automatic_evidence and parameters.get("evidence") != "automatic")
        ):
            redirect_parameters = {
                "key": key,
                "candidate": corrected["candidate_id"],
                "page": page_number_value,
                "current": "1",
            }
            if parameters.get("show_resolved") == "1":
                redirect_parameters["show_resolved"] = "1"
            if parameters.get("remnant") == "1":
                redirect_parameters["remnant"] = "1"
            self.redirect("/compare?" + urlencode(redirect_parameters))
            return
        if candidate is None or candidate["object_key"] != key:
            raise WorkbenchError("unknown comparison candidate")
        rows = self.collection.comparison_rows(key, candidate_id)
        page_decisions = self.collection.page_decisions()
        decided_pages = {
            pdf_page
            for (decision_key, pdf_page) in page_decisions
            if decision_key == key
        }
        unresolved_pages = self.collection.unresolved_pages(key, candidate_id)
        if (
            candidate["role"] == "human_corrected_tiff_master_set"
            and page_number_value in decided_pages
            and parameters.get("show_resolved") != "1"
        ):
            if unresolved_pages:
                self.redirect("/compare?" + urlencode({
                    "key": key,
                    "candidate": candidate_id,
                    "page": unresolved_pages[0],
                    "current": "1",
                }))
            else:
                self.redirect("/object?" + urlencode({
                    "key": key,
                    "page_review": "complete",
                }))
            return
        row = rows[page_number_value - 1]
        remnant_notice = ""
        remnant = (
            self.collection.remnant_for(key, page_number_value)
            if parameters.get("remnant") == "1"
            else None
        )
        if remnant:
            remnant_notice = (
                "<div class='notice'><strong>Full-unit remnant review:</strong> "
                f"the rescan proposes {esc(Path(remnant['best_tiff_path']).name)} "
                f"(distance {esc(remnant['best_tiff_distance_total'] or 'n/a')}; "
                f"pHash {esc(remnant['best_tiff_distance_phash'] or 'n/a')}). "
                "It is highlighted among the bounded TIFF alternatives below. "
                "Derivatives remain evidence only. <a href='/remnants'>Return to Remnants</a>.</div>"
            )
        repeated_notice = ""
        if row["duplicate_assignment_pages"]:
            repeated_links = []
            for repeated_page in row["duplicate_assignment_pages"]:
                repeated_parameters = {
                    "key": key,
                    "candidate": candidate_id,
                    "page": repeated_page,
                    "show_resolved": "1",
                }
                if candidate["role"] == "visual_match_tiff_master_set":
                    repeated_parameters["evidence"] = "automatic"
                repeated_url = "/compare?" + urlencode(repeated_parameters)
                repeated_links.append(
                    f"<a href='{esc(repeated_url)}'>PDF page {repeated_page}</a>"
                )
            repeated_notice = (
                "<div class='notice repeated-assignments'><strong>Repeated assignment:</strong> "
                f"{esc(row['tiff_filename'])} is assigned to {', '.join(repeated_links)}. "
                "Open either page and change the incorrect decision.</div>"
            )
        crop_notice = ""
        if row.get("shared_crop_assignment_pages"):
            crop_links = []
            for related_page in row["shared_crop_assignment_pages"]:
                related_url = "/compare?" + urlencode({
                    "key": key,
                    "candidate": candidate_id,
                    "page": related_page,
                    "show_resolved": "1",
                })
                crop_links.append(
                    f"<a href='{esc(related_url)}'>PDF page {related_page}</a>"
                )
            crop_notice = (
                "<div class='crop-relation'><strong>Confirmed crop relationship:</strong> "
                f"{esc(row['tiff_filename'])} is retained once and represents "
                f"{', '.join(crop_links)}; the explicitly marked cropped detail is not a duplicate error.</div>"
            )
        if unresolved_pages:
            if page_number_value in unresolved_pages:
                unresolved_index = unresolved_pages.index(page_number_value)
            else:
                unresolved_index = 0
            previous_page = unresolved_pages[max(0, unresolved_index - 1)]
            next_page = unresolved_pages[min(len(unresolved_pages) - 1, unresolved_index + 1)]
            previous_url = "/compare?" + urlencode({"key": key, "candidate": candidate_id, "page": previous_page})
            next_url = "/compare?" + urlencode({"key": key, "candidate": candidate_id, "page": next_page})
            previous_control = f"<a class='button' href='{esc(previous_url)}'>← Previous unresolved</a>"
            next_control = f"<a class='button' href='{esc(next_url)}'>Next unresolved →</a>"
        else:
            object_url = "/object?" + urlencode({"key": key, "page_review": "complete"})
            previous_control = f"<a class='button' href='{esc(object_url)}'>← Return to object</a>"
            next_control = "<span class='ok'>No unresolved pages</span>"
        pdf_url = "/pdf-page?" + urlencode({"key": key, "page": page_number_value})
        if row["tiff_path"]:
            tiff_url = "/preview?" + urlencode({"path": row["tiff_path"]})
            tiff_zoom_url = "/preview?" + urlencode({"path": row["tiff_path"], "size": 2400})
            right = f"<a class='zoom-link' target='_blank' href='{esc(tiff_zoom_url)}'><img src='{esc(tiff_url)}' alt='{esc(row['tiff_filename'])}'><span>Click TIFF to open a 2400px preview</span></a>"
        else:
            right = "<div class='placeholder'>No TIFF is mapped to this PDF page</div>"
        problems = "".join(f"<li class='bad'>{esc(problem)}</li>" for problem in row["problems"]) or "<li class='ok'>No structural problem recorded for this page.</li>"
        unresolved_links = "".join(
            f"<a href='/compare?{urlencode({'key':key,'candidate':candidate_id,'page':pdf_page})}'>{pdf_page}</a>"
            for pdf_page in unresolved_pages
        ) or "<span class='ok'>none</span>"
        choice_data = self.collection.page_choices(key, page_number_value)
        current = choice_data["current"]
        current_text = "No human page decision recorded yet."
        if current:
            current_text = (
                (
                    f"Recorded cropped detail: {Path(current['selected_tiff_path']).name}"
                    if current.get("selected_tiff_relation") == "cropped_detail"
                    else f"Recorded full-page view: {Path(current['selected_tiff_path']).name}"
                )
                if current["outcome"] == "selected_tiff"
                else "Recorded: no corresponding TIFF"
            )
        choice_cards = "".join(
            f"""<article class='page-choice'><a class='zoom-link' target='_blank' href='/preview?{urlencode({'path':choice['path'],'size':2400})}'><img loading='lazy' src='/preview?{urlencode({'path':choice['path']})}' alt='{esc(choice['filename'])}'><span>Click image to open a 2400px preview</span></a><h3>{esc(choice['filename'])}</h3><p>{esc('; '.join(choice['reasons']))}</p><p class='technical'>{esc(choice['technical_metadata'] or choice['path'])}</p><button name='selected_path' value='{esc(choice['path'])}'>Record this TIFF for PDF page {page_number_value}</button></article>"""
            for choice in choice_data["choices"]
        )
        all_options = "".join(
            f"<option value='{esc(path)}'>{esc(Path(path).name)} — {esc(path)}</option>"
            for path in choice_data["all_paths"]
        )
        decision_fields = (
            f"<input type='hidden' name='key' value='{esc(key)}'>"
            f"<input type='hidden' name='candidate' value='{esc(candidate_id)}'>"
            f"<input type='hidden' name='page' value='{page_number_value}'>"
            + ("<input type='hidden' name='remnant' value='1'>" if parameters.get("remnant") == "1" else "")
        )
        if current:
            quick_decision = (
                f"<section class='quick-correct'><strong>Resolved:</strong> {esc(current_text)}. "
                f"This page is hidden from the unresolved list.</section>"
            )
        elif row["tiff_path"]:
            quick_decision = (
                f"<form class='quick-correct' method='post' action='/page-decision'>{decision_fields}"
                f"<input type='hidden' name='selected_path' value='{esc(row['tiff_path'])}'>"
                f"<span>If the left and right images are the same page:</span>"
                f"<button type='submit'>Correct — record {esc(row['tiff_filename'])}</button>"
                f"<button type='submit' name='selected_tiff_relation' value='cropped_detail'>Record as cropped detail</button></form>"
            )
        else:
            quick_decision = "<section class='quick-correct bad'>No TIFF is currently mapped; choose an alternative below.</section>"
        mapping_form = (
            f"<form class='panel mapping-form' method='post' action='/page-decision'>{decision_fields}"
            f"<h2>{'Change the recorded page mapping' if current else 'Choose a different TIFF'}</h2>"
            f"<p>Use these bounded alternatives only when the current right-hand TIFF is not correct.</p>"
            f"<label><input type='checkbox' name='selected_tiff_relation' value='cropped_detail'{' checked' if current and current.get('selected_tiff_relation') == 'cropped_detail' else ''}> This PDF page is a cropped detail of the selected full-spread TIFF</label>"
            f"<label>Decision note (optional)<textarea name='note'>{esc(current.get('note', '') if current else '')}</textarea></label>"
            f"<div class='page-choices'>{choice_cards}</div>"
            f"<h3>Choose another TIFF from this carrier</h3>"
            f"<select name='all_path'><option value=''>Select a TIFF…</option>{all_options}</select> "
            f"<button name='outcome' value='selected_tiff_from_list'>Record selected TIFF</button>"
            f"<p><button name='outcome' value='no_corresponding_tiff'>Record no corresponding TIFF</button></p>"
            f"<p class='technical'>Saved to {esc(PAGE_DECISIONS)}. No source file is changed; deletion_authorized=false.</p></form>"
        )
        if current:
            mapping_form = f"<details><summary>Change this resolved page decision</summary>{mapping_form}</details>"
        saved_notice = "<p class='ok'>Page mapping decision saved. The human-corrected ordered TIFF candidate has been rebuilt.</p>" if parameters.get("saved") else ""
        if parameters.get("current"):
            recorded_count = sum(
                decision_key == key
                for decision_key, _page in page_decisions
            )
            saved_notice = (
                f"<p class='ok'>Showing the current human-corrected candidate with "
                f"{recorded_count} recorded page decision(s). The old automatic warnings are evidence only.</p>"
            )
        error_notice = f"<p class='bad'>{esc(parameters.get('error'))}</p>" if parameters.get("error") else ""
        resolved_page_field = (
            "<input type='hidden' name='show_resolved' value='1'>"
            if parameters.get("show_resolved") == "1"
            else ""
        )
        body = f"""<p><a href='/object?{urlencode({'key':key})}'>← {esc(obj['inventory'])}</a></p>{saved_notice}{error_notice}<h1>Page comparison</h1><p>{esc(candidate['label'])}</p>{remnant_notice}{repeated_notice}{crop_notice}<section class='unresolved-nav'><strong>Unresolved pages ({len(unresolved_pages)})</strong><div class='problem-pages'>{unresolved_links}</div></section><div class='pager'>{previous_control}<form method='get' action='/compare'><input type='hidden' name='key' value='{esc(key)}'><input type='hidden' name='candidate' value='{esc(candidate_id)}'>{resolved_page_field}<label>PDF page <input type='number' min='1' max='{len(rows)}' name='page' value='{page_number_value}'></label><button type='submit'>Go</button></form>{next_control}</div><section class='compare'><figure><figcaption>Left — MD PDF page {page_number_value} of {len(rows)}</figcaption><img src='{esc(pdf_url)}' alt='MD PDF page {page_number_value}'></figure><figure><figcaption>Right — current proposed TIFF {esc(row['tiff_filename'] or 'missing')}</figcaption>{right}</figure></section>{quick_decision}<section class='panel'><h2>Current mapping evidence</h2><p><strong>{esc(current_text)}</strong></p><ul>{problems}</ul><dl><dt>TIFF relationship</dt><dd>{esc(row.get('selected_tiff_relation') or 'automatic suggestion')}</dd><dt>Suggestion basis</dt><dd>{esc(row['selection_basis'])}</dd><dt>Neighbor-aligned sequence</dt><dd>{esc(choice_data['expected_sequence'] or 'n/a')} (dominant offset {esc(choice_data['dominant_offset'] if choice_data['dominant_offset'] is not None else 'n/a')})</dd><dt>MD description sequence</dt><dd>{esc(choice_data['catalogue_sequence'] or 'n/a')}</dd><dt>Mapped TIFF sequence</dt><dd>{esc(row['sequence'] if row['sequence'] is not None else 'none')}</dd><dt>Global visual nearest sequence / distance</dt><dd>{esc(row['global_best_scan_sequence'] or 'n/a')} / {esc(row['global_best_total'] or 'n/a')}</dd><dt>Match state</dt><dd>{esc(row['match_state'])}</dd><dt>Visual distances: total / pHash / dHash / aHash</dt><dd>{esc(row['distance_total'] or 'n/a')} / {esc(row['distance_phash'] or 'n/a')} / {esc(row['distance_dhash'] or 'n/a')} / {esc(row['distance_ahash'] or 'n/a')}</dd><dt>Runner-up sequence / distance</dt><dd>{esc(row['runner_up_sequence'] or 'n/a')} / {esc(row['runner_up_total'] or 'n/a')}</dd><dt>Local path</dt><dd class='technical'>{esc(row['tiff_path'] or 'no TIFF mapping')}</dd><dt>Technical metadata</dt><dd class='technical'>{esc(row['technical_metadata'] or 'not available')}</dd></dl></section>{mapping_form}"""
        self.send_bytes(layout(f"Compare {obj['inventory']} page {page_number_value}", body), "text/html; charset=utf-8")

    def pdf_page(self) -> None:
        parameters = self.parameters()
        key = parameters.get("key", "")
        obj = self.collection.objects.get(key)
        if obj is None:
            raise WorkbenchError("unknown MD PDF object")
        try:
            page = int(parameters.get("page", "1"))
            size = int(parameters.get("size", "1200"))
        except ValueError as exc:
            raise WorkbenchError("PDF page and preview size must be numbers") from exc
        if page < 1 or page > obj["md_pdf_pages"]:
            raise WorkbenchError("PDF page is outside the document")
        if size not in {1200, 2400}:
            raise WorkbenchError("unsupported PDF preview size")
        target = self.collection.pdf_preview_cache_path(key, page, size)
        if not target.is_file():
            with self.collection.preview_lock:
                target = self.collection._render_pdf_page_preview(key, page, size)
        self.send_bytes(target.read_bytes(), "image/jpeg", cache_control="private, max-age=86400")

    def preview(self) -> None:
        parameters = self.parameters()
        path = unquote(parameters.get("path", ""))
        try:
            size = int(parameters.get("size", "1000"))
        except ValueError as exc:
            raise WorkbenchError("preview size must be a number") from exc
        if size not in {1000, 2400}:
            raise WorkbenchError("unsupported preview size")
        if path not in self.collection.preview_paths():
            raise WorkbenchError("path is not a workbench preview candidate")
        source = source_path(path)
        if source.suffix.casefold() == ".pdf":
            key = next(
                (key for key, obj in self.collection.objects.items() if obj["md_pdf_path"] == path),
                "",
            )
            if not key:
                raise WorkbenchError("PDF preview is not bound to a workbench object")
            target = self.collection.pdf_preview_cache_path(key, 1, size)
            if not target.is_file():
                with self.collection.preview_lock:
                    target = self.collection._render_pdf_page_preview(key, 1, size)
            payload = target.read_bytes()
        else:
            target = self.collection.tiff_preview_cache_path(path, size)
            if not target.is_file():
                with self.collection.preview_lock:
                    target = self.collection._render_tiff_preview(path, size)
            payload = target.read_bytes()
        self.send_bytes(payload, "image/jpeg", cache_control="private, max-age=86400")

    def drain(self) -> None:
        source_rows = read_csv(SOURCE_DISPOSITION)
        drain_rows = read_csv(DRAIN_LIST)
        counts = collections.Counter(row["disposition"] for row in drain_rows)
        body = f"""<h1>Deferred drain planning</h1><div class='notice'><strong>No deletion is authorized.</strong> Source disposition has {len(source_rows)} reviewed paths; the drain projection contains only {len(drain_rows)} confirmed/planned unit paths. It is updated after each decision and remains blocked until verified accession.</div><ul>{''.join(f'<li>{esc(key)}: {value}</li>' for key,value in sorted(counts.items()))}</ul><p class='technical'>{esc(DRAIN_LIST)}</p><p class='technical'>{esc(SOURCE_DISPOSITION)}</p>"""
        self.send_bytes(layout("Drain", body), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path != "/page-decision":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 128 * 1024:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = parse_qs(self.rfile.read(length).decode())
        key = form.get("key", [""])[0]
        if request_path == "/page-decision":
            page_value = form.get("page", ["1"])[0]
            try:
                pdf_page = int(page_value)
                selected_path = form.get("selected_path", [""])[0]
                outcome = form.get("outcome", [""])[0]
                if selected_path:
                    outcome = "selected_tiff"
                elif outcome == "selected_tiff_from_list":
                    outcome = "selected_tiff"
                    selected_path = form.get("all_path", [""])[0]
                self.collection.save_page_decision(
                    key,
                    pdf_page,
                    outcome,
                    selected_path,
                    form.get("note", [""])[0],
                    form.get("selected_tiff_relation", ["full_page"])[0],
                )
                self.collection.finalize_reviewed_object(key)
                remaining = self.collection.review_items()
                current_order = (
                    natural_key(self.collection.objects[key]["inventory"]),
                    pdf_page,
                )
                later = [
                    item for item in remaining
                    if (natural_key(item["inventory"]), item["pdf_page"]) > current_order
                ]
                if remaining:
                    next_item = later[0] if later else remaining[0]
                    self.redirect("/compare?" + urlencode({
                        "key": next_item["object_key"],
                        "page": next_item["pdf_page"],
                        "saved": "1",
                    }))
                else:
                    self.redirect("/?saved=1")
            except Exception as exc:
                self.redirect("/compare?" + urlencode({
                    "key": key,
                    "page": page_value,
                    "error": str(exc),
                }))
            return

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default=os.environ.get("USER", "human-reviewer"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--generate-previews", action="store_true")
    parser.add_argument("--set-accession-state", choices=("accession_in_progress", "verified_authority"))
    parser.add_argument("--object-key", default="")
    parser.add_argument("--receipt", default="")
    args = parser.parse_args()
    try:
        collection = Collection861MD(args.actor)
        if args.generate_previews:
            print(json.dumps(collection.generate_previews(args.object_key), ensure_ascii=False, indent=2))
            return 0
        if args.set_accession_state:
            collection.set_accession_state(args.object_key, args.set_accession_state, args.receipt)
            print(json.dumps(collection.check(), ensure_ascii=False, indent=2))
            return 0
        if args.prepare or args.check:
            print(json.dumps(collection.check(), ensure_ascii=False, indent=2))
            return 0
        Handler.collection = collection
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"Brigadebücher MD-object workbench: http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()
        return 0
    except (WorkbenchError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
