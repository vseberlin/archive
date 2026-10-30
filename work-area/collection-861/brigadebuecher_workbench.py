#!/usr/bin/env python3
"""Carrier-level reconciliation workbench for museum-digital collection 861.

The workbench records one explicit human Master decision per physical carrier.
All other local representations become redundant after that decision.  Every
in-scope source occurrence remains in the evidence-only drain list with
``deletion_authorized=false``.  No source file is changed or deleted.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.parse
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-861")
RECON = WORK_ROOT / "work/reconciliation"
SOURCE_ROOT = Path("/media/vladimir/Elements")
PROJECT_PREFIX = (
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/"
)
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
DOCUMENT_INDEX = Path(
    "/mnt/volume/iss-archive-work/document-index/document-index.sqlite"
)
DECISION_LOG = RECON / "brigadebuecher-decisions.jsonl"
CURRENT_DECISIONS = RECON / "brigadebuecher-decisions-current.csv"
SOURCE_DISPOSITION = RECON / "source-disposition.csv"
DRAIN_LIST = RECON / "drain-list.csv"
DRAIN_SUMMARY = RECON / "drain-list-summary.json"
MD_RESOURCE_VERIFICATION = RECON / "museum-digital-resource-verification.csv"

DECISION_FIELDS = (
    "carrier_key",
    "outcome",
    "selected_master_id",
    "selected_master_role",
    "selected_master_paths",
    "retained_candidate_ids",
    "retained_source_paths",
    "redundant_candidate_ids",
    "redundant_source_paths",
    "note",
    "actor",
    "decided_utc",
    "deletion_authorized",
)
DRAIN_FIELDS = (
    "source_path",
    "carrier_key",
    "source_kind",
    "candidate_role",
    "candidate_id",
    "disposition",
    "accession_relation",
    "drain_state",
    "size_bytes",
    "content_sha256",
    "visual_similarity_evidence",
    "technical_metadata",
    "evidence_basis",
    "deletion_authorized",
)


class WorkbenchError(RuntimeError):
    """Fail closed when the bounded reconciliation contract is violated."""


def e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(64 * 1024 * 1024)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def csv_bytes(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(fields),
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o644)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def ensure_source_guard() -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(SOURCE_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if (
        len(fields) != 4
        or fields[0] != "/dev/sdf1"
        or fields[1] != str(SOURCE_ROOT)
        or fields[2] != "ntfs3"
        or "ro" not in fields[3].split(",")
    ):
        raise WorkbenchError(
            f"protected source must be /dev/sdf1 ntfs3 ro: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def base_inventory(value: str) -> str:
    return re.sub(r"_\d+$", "", value.strip().upper())


def visual_base(filename: str) -> str:
    match = re.match(r"((?:BB|BX)-\d+)", filename, re.I)
    return match.group(1).upper() if match else ""


def candidate_id(role: str, paths: Iterable[str]) -> str:
    binding = role + "\0" + "\0".join(sorted(paths))
    return "CAND-" + hashlib.sha256(binding.encode("utf-8")).hexdigest()[:20].upper()


def human_size(value: int | str) -> str:
    amount = float(int(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return ""


def classify_visual(relative_path: str, extension: str) -> str:
    extension = extension.casefold().lstrip(".")
    path = "/" + relative_path
    if extension in {"jpg", "jpeg"}:
        return "jpeg_derivative"
    if "/SCANS-Canon/" in path and extension in {"tif", "tiff"}:
        return "canon_tiff_source"
    if relative_path.startswith("restore/archive/Bibliocopy/") and extension in {
        "tif",
        "tiff",
    }:
        return "bibliocopy_tiff_source"
    if "/SCANS-Bibliocopy/" in path and extension in {"tif", "tiff"}:
        return "project_tiff_copy"
    return "uncertain_visual_candidate"


def pdf_group(path: str, base: str) -> str:
    markers = (
        ("/Metadaten/UPLOADS/PDFs_upload/", "upload_pdf_set"),
        ("/Metadaten/UPLOADS/PDFs/", "preupload_pdf_set"),
        ("/PDFs_neu/", "new_whole_or_part_pdf_set"),
        ("/PDFs/", "older_whole_or_part_pdf_set"),
    )
    for marker, label in markers:
        if marker in path:
            suffix = path.split(marker, 1)[1]
            parts = suffix.split("/")
            if len(parts) > 1:
                return label + ":bundle:" + parts[0]
            return label + ":flat"
    return "pdf_set:" + str(Path(path).parent)


class Collection861:
    def __init__(self, actor: str) -> None:
        self.actor = actor.strip()
        if not self.actor:
            raise WorkbenchError("a named actor is required")
        self.mount = ensure_source_guard()
        self.lock = threading.Lock()
        self.carriers = {
            row["carrier_key"]: row
            for row in read_jsonl(RECON / "brigadebuecher-carriers.jsonl")
        }
        self.metadata = collections.defaultdict(list)
        for row in read_jsonl(RECON / "brigadebuecher-metadata-comparison.jsonl"):
            self.metadata[row["base_inventory"]].append(row)
        self.document_appraisal = {
            row["source_path"]: row
            for row in read_jsonl(RECON / "brigadebuecher-document-appraisal.jsonl")
        }
        self.scan_families = read_csv(RECON / "brigadebuecher-scan-families.csv")
        self.pdf_occurrences = read_csv(RECON / "brigadebuecher-pdf-occurrences.csv")
        self.other_pdfs = read_csv(RECON / "brigadebuecher-other-pdfs.csv")
        self.md_resource_verification = (
            read_csv(MD_RESOURCE_VERIFICATION)
            if MD_RESOURCE_VERIFICATION.is_file()
            else []
        )
        self.visual_rows = self._visual_rows()
        self.document_rows = self._document_rows()
        self.candidates_by_carrier = self._build_candidates()
        self.candidates = {
            item["candidate_id"]: item
            for values in self.candidates_by_carrier.values()
            for item in values
        }
        self._validate_candidates()
        self.refresh_manifests()

    @staticmethod
    def _ro_database(path: Path):
        import sqlite3

        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _visual_rows(self) -> list[dict[str, Any]]:
        connection = self._ro_database(VISUAL_INDEX)
        try:
            root = connection.execute(
                "SELECT root_id FROM visual_roots WHERE root_path=?",
                (str(SOURCE_ROOT),),
            ).fetchone()
            if root is None:
                raise WorkbenchError("Elements is absent from the visual index")
            rows = connection.execute(
                """
                SELECT a.asset_id, a.relative_path, a.original_filename,
                       a.extension, a.size_bytes, a.width_px, a.height_px,
                       a.status, a.error,
                       max(CASE WHEN f.extractor_id='ahash-64-v2'
                                THEN f.feature_hex END) AS ahash,
                       max(CASE WHEN f.extractor_id='dhash-64-v2'
                                THEN f.feature_hex END) AS dhash,
                       max(CASE WHEN f.extractor_id='phash-dct-64-v2'
                                THEN f.feature_hex END) AS phash
                  FROM visual_assets AS a
                  LEFT JOIN visual_features AS f USING(asset_id)
                 WHERE a.root_id=?
                   AND (upper(a.original_filename) LIKE 'BB-%'
                        OR upper(a.original_filename) LIKE 'BX-%')
                 GROUP BY a.asset_id
                 ORDER BY a.relative_path
                """,
                (root["root_id"],),
            ).fetchall()
            output = []
            for source in rows:
                row = dict(source)
                row["source_path"] = str(SOURCE_ROOT / row["relative_path"])
                row["carrier_key"] = visual_base(row["original_filename"])
                row["candidate_role"] = classify_visual(
                    row["relative_path"], row["extension"]
                )
                output.append(row)
            return output
        finally:
            connection.close()

    def _document_rows(self) -> list[dict[str, Any]]:
        connection = self._ro_database(DOCUMENT_INDEX)
        try:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT document_id, source_path, filename, extension,
                                      size_bytes, content_sha256, observed_state,
                                      extraction_state, parser, extraction_error
                               FROM documents
                              WHERE source_path LIKE ?
                              ORDER BY source_path""",
                    (PROJECT_PREFIX + "%",),
                )
            ]
        finally:
            connection.close()

    def _visual_family_members(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        output: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
        for row in self.visual_rows:
            output[(row["carrier_key"], str(Path(row["source_path"]).parent))].append(row)
        for rows in output.values():
            rows.sort(key=lambda item: item["source_path"].casefold())
        return output

    def _candidate(
        self,
        carrier: str,
        role: str,
        label: str,
        paths: list[str],
        *,
        members: list[dict[str, Any]],
        eligible: bool,
        evidence: str,
    ) -> dict[str, Any]:
        formats = sorted({Path(path).suffix.casefold().lstrip(".") for path in paths})
        dimensions = sorted(
            {
                f"{row.get('width_px')}x{row.get('height_px')}"
                for row in members
                if row.get("width_px") and row.get("height_px")
            }
        )
        content_hashes = sorted(
            {str(row.get("content_sha256") or "") for row in members}
            - {""}
        )
        error_count = sum(
            bool(row.get("error") or row.get("extraction_error")) for row in members
        )
        return {
            "candidate_id": candidate_id(role, paths),
            "carrier_key": carrier,
            "role": role,
            "label": label,
            "paths": paths,
            "file_count": len(paths),
            "size_bytes": sum(int(row.get("size_bytes") or 0) for row in members),
            "formats": formats,
            "dimensions": dimensions,
            "content_hashes": content_hashes,
            "visual_signature_count": sum(bool(row.get("phash")) for row in members),
            "error_count": error_count,
            "master_eligible": eligible and error_count == 0,
            "evidence": evidence,
        }

    def _build_candidates(self) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        family_members = self._visual_family_members()
        largest_tiff_family: dict[str, int] = collections.defaultdict(int)
        largest_jpeg_family: dict[str, int] = collections.defaultdict(int)
        for family in self.scan_families:
            count = int(family["file_count"])
            if family["candidate_role"] in {
                "bibliocopy_tiff_source",
                "canon_tiff_source",
                "project_tiff_copy",
            }:
                largest_tiff_family[family["base_inventory"]] = max(
                    largest_tiff_family[family["base_inventory"]], count
                )
            elif family["candidate_role"] == "jpeg_derivative":
                largest_jpeg_family[family["base_inventory"]] = max(
                    largest_jpeg_family[family["base_inventory"]], count
                )
        for family in self.scan_families:
            key = family["base_inventory"]
            path = family["family_path"]
            members = family_members.get((key, path), [])
            paths = [row["source_path"] for row in members]
            if not paths:
                continue
            role = family["candidate_role"]
            labels = {
                "bibliocopy_tiff_source": "Original Bibliocopy TIFF page set",
                "canon_tiff_source": "Canon TIFF page set",
                "project_tiff_copy": "Project TIFF copy page set",
                "jpeg_derivative": "Existing low-resolution JPEG page set",
            }
            evidence = (
                f"{family['file_count']} file(s); indexed={family['indexed_count']}; "
                f"decoder errors={family['error_count']}; sequences "
                f"{family['sequence_min'] or '?'}–{family['sequence_max'] or '?'}"
            )
            if family["missing_sequences"]:
                evidence += f"; missing sequences {family['missing_sequences']}"
            count = int(family["file_count"])
            if role == "jpeg_derivative":
                eligible = (
                    largest_tiff_family[key] == 0
                    and count > 1
                    and count >= largest_jpeg_family[key] * 0.9
                )
            else:
                eligible = (
                    role
                    in {
                        "bibliocopy_tiff_source",
                        "canon_tiff_source",
                        "project_tiff_copy",
                    }
                    and count > 1
                    and count >= largest_tiff_family[key] * 0.9
                )
            output[key].append(
                self._candidate(
                    key,
                    role,
                    labels.get(role, role.replace("_", " ")),
                    paths,
                    members=members,
                    eligible=eligible,
                    evidence=evidence,
                )
            )

        documents = {row["source_path"]: row for row in self.document_rows}
        preferred_by_carrier: dict[str, list[dict[str, str]]] = (
            collections.defaultdict(list)
        )
        for row in self.pdf_occurrences:
            if row["relationship"] == "preferred_occurrence_for_review":
                preferred_by_carrier[base_inventory(row["inventory"])].append(row)
        verified_by_inventory = {
            row["inventory"]: row for row in self.md_resource_verification
        }
        for key, carrier in self.carriers.items():
            if carrier["museum_digital_state"] != "matched":
                continue
            preferred = sorted(
                preferred_by_carrier.get(key, []), key=lambda row: row["inventory"]
            )
            expected = list(carrier["md_inventories"])
            if [row["inventory"] for row in preferred] != expected:
                raise WorkbenchError(
                    f"{key} lacks one preferred local occurrence per MD PDF part"
                )
            paths = [row["source_path"] for row in preferred]
            members = [documents[path] for path in paths]
            verified = [
                verified_by_inventory[inventory]
                for inventory in expected
                if inventory in verified_by_inventory
                and verified_by_inventory[inventory]["matching_local_source_path"]
                in paths
            ]
            candidate = self._candidate(
                key,
                "museum_digital_pdf_master",
                "museum-digital PDF Master representation",
                paths,
                members=members,
                eligible=True,
                evidence=(
                    f"{len(expected)}/{len(expected)} published MD PDF part(s); "
                    f"{len(verified)}/{len(expected)} part(s) byte-verified against the "
                    "live museum-digital resource; local paths are the preferred upload occurrences"
                ),
            )
            candidate["external_urls"] = list(carrier["md_object_urls"])
            output[key].append(candidate)

        pdfs_by_carrier_group: dict[tuple[str, str], list[dict[str, str]]] = (
            collections.defaultdict(list)
        )
        for row in self.pdf_occurrences:
            key = base_inventory(row["inventory"])
            pdfs_by_carrier_group[(key, pdf_group(row["source_path"], key))].append(row)
        for (key, group), rows in sorted(pdfs_by_carrier_group.items()):
            unique_paths = sorted({row["source_path"] for row in rows})
            if any(
                item["role"] == "museum_digital_pdf_master"
                and set(item["paths"]) == set(unique_paths)
                for item in output[key]
            ):
                continue
            members = [documents[path] for path in unique_paths if path in documents]
            expected = len(self.carriers.get(key, {}).get("md_inventories", []))
            represented = len({row["inventory"] for row in rows})
            complete = bool(expected) and represented == expected
            output[key].append(
                self._candidate(
                    key,
                    "split_pdf_representation_set",
                    "Local split-PDF representation set",
                    unique_paths,
                    members=members,
                    eligible=complete,
                    evidence=(
                        f"{represented}/{expected} museum-digital part(s); "
                        f"{len({row['content_sha256'] for row in rows})} distinct byte hash(es); "
                        f"source family {group}"
                    ),
                )
            )

        for row in self.other_pdfs:
            stem = base_inventory(Path(row["filename"]).stem)
            if stem not in self.carriers:
                continue
            relationship = row["relationship"]
            if relationship not in {
                "whole_book_pdf_related_to_split_md_objects",
                "local_only_or_noncollection_pdf_candidate",
            }:
                continue
            member = documents.get(row["source_path"], row)
            output[stem].append(
                self._candidate(
                    stem,
                    "whole_book_pdf_candidate",
                    "Whole-book PDF candidate",
                    [row["source_path"]],
                    members=[member],
                    eligible=True,
                    evidence=(
                        f"SHA-256 {row['content_sha256']}; indexed as {relationship}"
                    ),
                )
            )
        order = {
            "museum_digital_pdf_master": 0,
            "bibliocopy_tiff_source": 1,
            "canon_tiff_source": 2,
            "project_tiff_copy": 3,
            "whole_book_pdf_candidate": 4,
            "split_pdf_representation_set": 5,
            "jpeg_derivative": 6,
        }
        for key, values in output.items():
            if self.carriers[key]["museum_digital_state"] == "matched":
                proposed_tiff_path = self.carriers[key]["proposed_master_path"]
                for item in values:
                    item["master_eligible"] = (
                        item["role"] == "museum_digital_pdf_master"
                        and item["error_count"] == 0
                    )
                    item["retention_role"] = (
                        "corresponding_tiff_page_set"
                        if item["role"]
                        in {
                            "bibliocopy_tiff_source",
                            "canon_tiff_source",
                            "project_tiff_copy",
                        }
                        and str(Path(item["paths"][0]).parent)
                        == proposed_tiff_path
                        else ""
                    )
            else:
                for item in values:
                    item["retention_role"] = ""
            values.sort(
                key=lambda item: (
                    order.get(item["role"], 9),
                    item["paths"][0].casefold(),
                )
            )
        return output

    def _validate_candidates(self) -> None:
        if set(self.carriers) != set(self.candidates_by_carrier):
            missing = sorted(set(self.carriers) - set(self.candidates_by_carrier))
            raise WorkbenchError(f"carrier candidates missing: {missing}")
        for key, candidates in self.candidates_by_carrier.items():
            ids = [item["candidate_id"] for item in candidates]
            if len(ids) != len(set(ids)):
                raise WorkbenchError(f"duplicate candidate IDs for {key}")
            for item in candidates:
                for source in item["paths"]:
                    path = Path(source).resolve(strict=True)
                    path.relative_to(SOURCE_ROOT.resolve(strict=True))

    def decisions(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in read_jsonl(DECISION_LOG):
            if row.get("carrier_key") in self.carriers:
                latest[row["carrier_key"]] = row
        return latest

    def records(self) -> list[dict[str, Any]]:
        decisions = self.decisions()
        records = []
        for key, carrier in self.carriers.items():
            row = dict(carrier)
            row["decision"] = decisions.get(key)
            row["candidates"] = self.candidates_by_carrier[key]
            row["queue_state"] = (
                "resolved"
                if row["decision"] and row["decision"]["outcome"] == "selected_master"
                else row["review_state"]
            )
            records.append(row)
        return sorted(records, key=lambda row: row["carrier_key"])

    def save_decision(
        self, carrier_key: str, outcome: str, selected_master_id: str, note: str
    ) -> None:
        if carrier_key not in self.carriers:
            raise WorkbenchError("unknown carrier")
        candidates = self.candidates_by_carrier[carrier_key]
        by_id = {item["candidate_id"]: item for item in candidates}
        if outcome not in {"selected_master", "unresolved"}:
            raise WorkbenchError("unsupported decision")
        selected: dict[str, Any] | None = None
        if outcome == "selected_master":
            selected = by_id.get(selected_master_id)
            if selected is None or not selected["master_eligible"]:
                raise WorkbenchError("select one eligible Master representation")
        elif selected_master_id:
            raise WorkbenchError("unresolved decisions cannot select a Master")
        redundant = [
            item
            for item in candidates
            if selected is not None
            and item is not selected
            and item.get("retention_role") != "corresponding_tiff_page_set"
        ]
        retained = [
            item
            for item in candidates
            if selected is not None
            and item.get("retention_role") == "corresponding_tiff_page_set"
        ]
        selected_paths = set(selected["paths"] if selected else [])
        retained_paths = {path for item in retained for path in item["paths"]}
        redundant_paths = {
            path for item in redundant for path in item["paths"]
        } - selected_paths - retained_paths
        record = {
            "carrier_key": carrier_key,
            "outcome": outcome,
            "selected_master_id": selected["candidate_id"] if selected else "",
            "selected_master_role": selected["role"] if selected else "",
            "selected_master_paths": selected["paths"] if selected else [],
            "retained_candidate_ids": [item["candidate_id"] for item in retained],
            "retained_source_paths": sorted(
                {path for item in retained for path in item["paths"]}
            ),
            "redundant_candidate_ids": [item["candidate_id"] for item in redundant],
            "redundant_source_paths": sorted(redundant_paths),
            "note": note.strip(),
            "actor": self.actor,
            "decided_utc": utc_now(),
            "deletion_authorized": False,
        }
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with self.lock:
            with DECISION_LOG.open("ab") as handle:
                os.fchmod(handle.fileno(), 0o644)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.refresh_manifests()

    def _candidate_paths(self) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for candidate in self.candidates.values():
            for path in candidate["paths"]:
                output[path].append(candidate)
        return output

    def _document_carrier(self, row: dict[str, Any]) -> str:
        match = re.match(r"((?:BB|BX)-\d+)", Path(row["filename"]).stem, re.I)
        return match.group(1).upper() if match else ""

    def _drain_rows(self) -> list[dict[str, str]]:
        decisions = self.decisions()
        bindings = self._candidate_paths()
        rows: list[dict[str, str]] = []

        def disposition(path: str, carrier: str, default_role: str) -> tuple[str, str, str, str]:
            candidates = bindings.get(path, [])
            ids = ";".join(sorted({item["candidate_id"] for item in candidates}))
            roles = ";".join(sorted({item["role"] for item in candidates})) or default_role
            decision = decisions.get(carrier)
            if decision and decision["outcome"] == "selected_master":
                if decision["selected_master_id"] in ids.split(";"):
                    return (
                        roles,
                        ids,
                        "selected_master_source_pending_accession",
                        "listed_for_later_drain_after_verified_accession",
                    )
                if any(
                    item.get("retention_role") == "corresponding_tiff_page_set"
                    for item in candidates
                ):
                    return (
                        roles,
                        ids,
                        "retained_corresponding_tiff_page_pending_accession",
                        "listed_for_later_drain_after_verified_accession",
                    )
                return (
                    roles,
                    ids,
                    "redundant_after_master_selection",
                    "listed_for_later_drain_after_verified_accession",
                )
            return (
                roles,
                ids,
                "candidate_pending_reconciliation",
                "blocked_until_master_selection_and_verified_accession",
            )

        for source in self.visual_rows:
            path = source["source_path"]
            carrier = source["carrier_key"]
            role, ids, state, drain = disposition(
                path, carrier, source["candidate_role"]
            )
            rows.append(
                {
                    "source_path": path,
                    "carrier_key": carrier,
                    "source_kind": "image",
                    "candidate_role": role,
                    "candidate_id": ids,
                    "disposition": state,
                    "accession_relation": (
                        "master_input"
                        if state == "selected_master_source_pending_accession"
                        else "corresponding_tiff_page_input"
                        if state
                        == "retained_corresponding_tiff_page_pending_accession"
                        else "source_representation_evidence"
                    ),
                    "drain_state": drain,
                    "size_bytes": str(source["size_bytes"]),
                    "content_sha256": "",
                    "visual_similarity_evidence": ";".join(
                        filter(
                            None,
                            (
                                f"ahash:{source.get('ahash')}" if source.get("ahash") else "",
                                f"dhash:{source.get('dhash')}" if source.get("dhash") else "",
                                f"phash:{source.get('phash')}" if source.get("phash") else "",
                            ),
                        )
                    ),
                    "technical_metadata": (
                        f"{source['extension']}; {source.get('width_px') or '?'}x"
                        f"{source.get('height_px') or '?'} px; index_state={source['status']}"
                        + (f"; error={source['error']}" if source.get("error") else "")
                    ),
                    "evidence_basis": "existing_visual_index",
                    "deletion_authorized": "false",
                }
            )

        for source in self.document_rows:
            path = source["source_path"]
            carrier = self._document_carrier(source)
            if carrier in self.carriers and bindings.get(path):
                role, ids, state, drain = disposition(path, carrier, "object_document")
                relation = (
                    "master_input"
                    if state == "selected_master_source_pending_accession"
                    else "source_representation_evidence"
                )
            else:
                appraisal = self.document_appraisal.get(path, {})
                suggestion = appraisal.get("suggested_relationship", "")
                role = suggestion or "project_document"
                ids = ""
                if suggestion.startswith("preserve_"):
                    state = "documentation_accession_candidate"
                    drain = "listed_for_later_drain_after_verified_accession"
                    relation = "documentation_input"
                elif suggestion.startswith("redundant_"):
                    state = "redundant_document_version"
                    drain = "listed_for_later_drain_after_verified_accession"
                    relation = "documentation_evidence"
                else:
                    state = "pending_document_appraisal"
                    drain = "blocked_until_document_appraisal_and_verified_accession"
                    relation = "project_document_evidence"
            rows.append(
                {
                    "source_path": path,
                    "carrier_key": carrier if carrier in self.carriers else "",
                    "source_kind": "document",
                    "candidate_role": role,
                    "candidate_id": ids,
                    "disposition": state,
                    "accession_relation": relation,
                    "drain_state": drain,
                    "size_bytes": str(source["size_bytes"]),
                    "content_sha256": source["content_sha256"],
                    "visual_similarity_evidence": "",
                    "technical_metadata": (
                        f"{source['extension']}; observed={source['observed_state']}; "
                        f"extraction={source['extraction_state']}; parser={source['parser']}"
                        + (
                            f"; error={source['extraction_error']}"
                            if source["extraction_error"]
                            else ""
                        )
                    ),
                    "evidence_basis": "existing_document_index",
                    "deletion_authorized": "false",
                }
            )
        rows.sort(key=lambda item: item["source_path"].casefold())
        if len(rows) != len({row["source_path"] for row in rows}):
            duplicates = collections.Counter(row["source_path"] for row in rows)
            raise WorkbenchError(
                "duplicate drain paths: "
                + ", ".join(path for path, count in duplicates.items() if count > 1)
            )
        if any(row["deletion_authorized"] != "false" for row in rows):
            raise WorkbenchError("drain list must not authorize deletion")
        return rows

    def refresh_manifests(self) -> None:
        decisions = self.decisions()
        current_rows = []
        for key in sorted(decisions):
            row = dict(decisions[key])
            for field in (
                "selected_master_paths",
                "retained_candidate_ids",
                "retained_source_paths",
                "redundant_candidate_ids",
                "redundant_source_paths",
            ):
                row[field] = ";".join(row.get(field, []))
            row["deletion_authorized"] = "false"
            current_rows.append(row)
        drain = self._drain_rows()
        summary = {
            "format": "iss-brigadebuecher-drain-list-v1",
            "generated_utc": utc_now(),
            "source_mount": self.mount,
            "source_policy": "read_only_no_source_changes",
            "physical_carriers": len(self.carriers),
            "resolved_carriers": sum(
                row["outcome"] == "selected_master" for row in decisions.values()
            ),
            "unresolved_carriers": len(self.carriers)
            - sum(row["outcome"] == "selected_master" for row in decisions.values()),
            "source_rows": len(drain),
            "image_rows": sum(row["source_kind"] == "image" for row in drain),
            "document_rows": sum(row["source_kind"] == "document" for row in drain),
            "source_bytes": sum(int(row["size_bytes"]) for row in drain),
            "disposition_counts": dict(
                sorted(collections.Counter(row["disposition"] for row in drain).items())
            ),
            "drain_state_counts": dict(
                sorted(collections.Counter(row["drain_state"] for row in drain).items())
            ),
            "duplicate_source_paths": 0,
            "deletion_authorized_rows": 0,
        }
        atomic_replace(CURRENT_DECISIONS, csv_bytes(current_rows, DECISION_FIELDS))
        payload = csv_bytes(drain, DRAIN_FIELDS)
        atomic_replace(SOURCE_DISPOSITION, payload)
        atomic_replace(DRAIN_LIST, payload)
        atomic_replace(
            DRAIN_SUMMARY,
            (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def summary(self) -> dict[str, Any]:
        return json.loads(DRAIN_SUMMARY.read_text(encoding="utf-8"))


CSS = """
:root{font-family:system-ui,sans-serif;color:#20231f;background:#f2f1eb}
body{margin:0}main,header{max-width:1320px;margin:auto;padding:22px}header{display:flex;gap:18px;align-items:center;justify-content:space-between}
a{color:#145c4c}.tabs a,.button,button{display:inline-block;padding:.65rem .9rem;border:1px solid #31584e;border-radius:.35rem;background:#fff;color:#163f35;text-decoration:none;cursor:pointer}
.tabs{display:flex;gap:.5rem;flex-wrap:wrap}.metrics,.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.metric,.card,.panel{background:#fff;border:1px solid #d6d2c5;border-radius:.5rem;padding:16px}.metric strong{font-size:1.7rem;display:block}
.card h2,.candidate h2{margin:.2rem 0}.technical{font-family:ui-monospace,monospace;font-size:.78rem;overflow-wrap:anywhere;color:#555}.badge{display:inline-block;padding:.16rem .42rem;border-radius:1rem;background:#e5e2d6;font-size:.78rem}.badge.warn{background:#f7d9a8}.badge.ok{background:#cde8d7}.badge.red{background:#f4c6c4}
.candidate{background:#fff;border:1px solid #d6d2c5;border-radius:.5rem;margin:16px 0;padding:16px}.previews{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.previews img{width:100%;height:260px;object-fit:contain;background:#292b29}.paths{max-height:190px;overflow:auto}.paths li{overflow-wrap:anywhere}.metadata{width:100%;border-collapse:collapse}.metadata td,.metadata th{padding:.5rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}.decision{position:sticky;bottom:0;background:#f6f1dd;border:2px solid #bd8d35;border-radius:.5rem;padding:16px;margin-top:20px}.note{width:100%;min-height:70px}.notice{padding:12px;background:#dff0e4;border-left:5px solid #2c765b}.danger{color:#8f2e27}.muted{color:#686868}@media(max-width:700px){.previews{grid-template-columns:1fr}.previews img{height:auto}header{display:block}}
"""


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], collection: Collection861) -> None:
        self.collection = collection
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def parameters(self) -> dict[str, str]:
        values = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {key: items[-1] for key, items in values.items() if items}

    def send_bytes(self, payload: bytes, mime: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def page(self, title: str, body: str) -> None:
        document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body><header><div><strong>Brigadebücher · collection 861</strong><br><span class="muted">one physical carrier = one Master decision</span></div><nav class="tabs"><a href="/?queue=ambiguous">Ambiguous</a><a href="/?queue=ready">Obvious</a><a href="/?queue=resolved">Resolved</a><a href="/?queue=all">All</a><a href="/drain">Drain register</a></nav></header><main>{body}</main></body></html>"""
        self.send_bytes(document.encode("utf-8"), "text/html; charset=utf-8")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self.queue()
            elif path == "/carrier":
                self.carrier()
            elif path == "/preview":
                self.preview()
            elif path == "/source":
                self.source()
            elif path == "/drain":
                self.drain()
            elif path == "/health":
                self.send_bytes(b"ok\n", "text/plain; charset=utf-8")
            else:
                self.send_error(404)
        except (WorkbenchError, OSError, ValueError, KeyError) as error:
            self.page("Workbench error", f"<h1>Workbench error</h1><pre>{e(error)}</pre>")

    def do_POST(self) -> None:  # noqa: N802
        try:
            if urllib.parse.urlparse(self.path).path != "/decision":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8")
                ).items()
            }
            if not secrets.compare_digest(form.get("csrf", ""), self.server.csrf):
                raise WorkbenchError("invalid form token")
            key = form.get("carrier_key", "")
            self.server.collection.save_decision(
                key,
                form.get("outcome", ""),
                form.get("selected_master_id", ""),
                form.get("note", ""),
            )
            self.redirect(
                "/carrier?"
                + urllib.parse.urlencode(
                    {"key": key, "notice": "Decision saved; drain register refreshed."}
                )
            )
        except (WorkbenchError, OSError, ValueError, KeyError) as error:
            self.page("Decision error", f"<h1>Decision not saved</h1><pre>{e(error)}</pre>")

    def queue(self) -> None:
        queue = self.parameters().get("queue", "ambiguous")
        if queue not in {"ambiguous", "ready", "resolved", "all"}:
            queue = "ambiguous"
        records = self.server.collection.records()
        if queue == "ambiguous":
            visible = [
                row
                for row in records
                if row["queue_state"] == "needs_individual_review"
            ]
        elif queue == "ready":
            visible = [
                row
                for row in records
                if row["queue_state"] == "ready_for_batch_confirmation"
            ]
        elif queue == "resolved":
            visible = [row for row in records if row["queue_state"] == "resolved"]
        else:
            visible = records
        summary = self.server.collection.summary()
        cards = []
        for row in visible:
            decision = row.get("decision") or {}
            reasons = "; ".join(row["review_reasons"]) or "no unresolved evidence flags"
            cards.append(
                f"""<article class="card"><p><span class="badge {'ok' if row['queue_state']=='resolved' else 'warn'}">{e(row['queue_state'].replace('_',' '))}</span></p>
<h2><a href="/carrier?key={urllib.parse.quote(row['carrier_key'])}">{e(row['carrier_key'])}</a></h2>
<p>{e(' · '.join(row['md_titles']) or 'Local-only carrier')}</p>
<p>{len(row['candidates'])} representation candidates · {row['primary_original_tiff_count']} primary TIFF pages</p>
<p class="technical">{e(reasons)}</p>{f'<p><strong>Master:</strong> {e(decision.get("selected_master_role"))}</p>' if decision.get('selected_master_id') else ''}</article>"""
            )
        body = f"""<h1>{e(queue.title())} reconciliation queue</h1>
<p>Only a human decision selects the one Master. On selection, every other candidate is recorded Redundant. No source is modified, and Redundant never authorizes deletion.</p>
<section class="metrics"><div class="metric"><strong>{summary['resolved_carriers']}</strong>resolved carriers</div><div class="metric"><strong>{summary['unresolved_carriers']}</strong>unresolved carriers</div><div class="metric"><strong>{summary['source_rows']}</strong>drain/source rows</div><div class="metric"><strong>0</strong>deletion-authorized rows</div></section>
<p>Showing {len(visible)} of {len(records)} physical carriers.</p><section class="grid">{''.join(cards) or '<div class="panel">No carriers in this view.</div>'}</section>"""
        self.page("Brigadebücher reconciliation", body)

    def carrier(self) -> None:
        key = self.parameters().get("key", "")
        record = next(
            (row for row in self.server.collection.records() if row["carrier_key"] == key),
            None,
        )
        if record is None:
            raise WorkbenchError("unknown carrier")
        decision = record.get("decision") or {}
        notice = self.parameters().get("notice", "")
        metadata_rows = []
        for row in self.server.collection.metadata.get(key, []):
            changed = ", ".join(row["changed_fields"]) or "none"
            metadata_rows.append(
                f"<tr><th>{e(row['inventory'])}</th><td><a target=\"_blank\" href=\"https://berlin.museum-digital.de/object/{e(row['md_object_id'])}\">md:{e(row['md_object_id'])}</a></td><td>{e(row['comparison_state'])}</td><td>{e(changed)}</td><td>{e(row['metadata_authority'])}</td></tr>"
            )
        primary_cards = []
        evidence_cards = []
        for candidate in record["candidates"]:
            paths = candidate["paths"]
            indices = sorted({0, len(paths) // 2, len(paths) - 1})
            previews = "".join(
                f'<a href="/source?candidate={e(candidate["candidate_id"])}&index={index}" target="_blank"><img loading="lazy" src="/preview?candidate={e(candidate["candidate_id"])}&index={index}" alt="{e(Path(paths[index]).name)}"></a>'
                for index in indices
            )
            path_items = "".join(f"<li>{e(path)}</li>" for path in paths)
            selected = decision.get("selected_master_id") == candidate["candidate_id"]
            disposition = (
                "selected Master"
                if selected
                else "retained corresponding TIFF pages"
                if (
                    decision.get("outcome") == "selected_master"
                    and candidate.get("retention_role")
                    == "corresponding_tiff_page_set"
                )
                else "Redundant after selection"
                if decision.get("outcome") == "selected_master"
                else "candidate"
            )
            selector = (
                f'<label><input type="radio" name="selected_master_id" value="{e(candidate["candidate_id"])}" form="decision" {"checked" if selected else ""}> Select this representation as the one Master</label>'
                if candidate["master_eligible"]
                else '<strong>Retained corresponding TIFF page representation—not another Master.</strong>'
                if candidate.get("retention_role") == "corresponding_tiff_page_set"
                else '<span class="danger">Incomplete evidence set; not selectable as Master.</span>'
            )
            external_links = "".join(
                f'<a target="_blank" href="{e(link)}">museum-digital record</a> '
                for link in candidate.get("external_urls", [])
            )
            card = f"""<article class="candidate"><p><span class="badge {'ok' if selected else ''}">{e(disposition)}</span> <span class="badge">{e(candidate['role'].replace('_',' '))}</span></p><h2>{e(candidate['label'])}</h2>
<p>{selector}</p>{f'<p>{external_links}</p>' if external_links else ''}<div class="previews">{previews}</div>
<p><strong>{candidate['file_count']} file(s), {human_size(candidate['size_bytes'])}</strong> · formats {e(', '.join(candidate['formats']))} · decoder/index errors {candidate['error_count']}</p>
<p>{e(candidate['evidence'])}</p><p class="technical">Dimensions: {e(', '.join(candidate['dimensions'][:8]) or 'not indexed')}{' …' if len(candidate['dimensions']) > 8 else ''}<br>Known SHA-256: {e(', '.join(candidate['content_hashes'][:3]) or 'not precomputed')}{' …' if len(candidate['content_hashes']) > 3 else ''}<br>Rebuildable visual signatures: {candidate['visual_signature_count']}/{candidate['file_count']}<br>{e(candidate['candidate_id'])}</p>
<details class="paths"><summary>All original source paths</summary><ul>{path_items}</ul></details></article>"""
            if (
                record["museum_digital_state"] == "matched"
                and candidate["role"] != "museum_digital_pdf_master"
                and candidate.get("retention_role")
                != "corresponding_tiff_page_set"
            ):
                evidence_cards.append(card)
            else:
                primary_cards.append(card)
        reasons = "".join(f"<li>{e(reason)}</li>" for reason in record["review_reasons"])
        body = f"""{f'<p class="notice">{e(notice)}</p>' if notice else ''}<p><a href="/">← queue</a></p><h1>{e(key)}</h1>
<p>{e(' · '.join(record['md_titles']) or 'Local-only physical carrier')}</p>
<section class="panel"><h2>Identity and review evidence</h2><p>museum-digital objects: {e(', '.join('md:'+x for x in record['md_object_ids']) or 'none')}</p><ul>{reasons or '<li>No automatic issue flags; explicit confirmation is still required.</li>'}</ul><p class="technical">Proposed path from projection: {e(record['proposed_master_path'])}</p></section>
<section class="panel"><h2>Metadata comparison</h2><p>Live museum-digital is the current descriptive authority; the final local workbook remains provenance evidence.</p><table class="metadata"><thead><tr><th>Inventory</th><th>Record</th><th>State</th><th>Changed fields</th><th>Authority</th></tr></thead><tbody>{''.join(metadata_rows) or '<tr><td colspan="5">Local-only: metadata still unresolved.</td></tr>'}</tbody></table></section>
<h2>Master and retained page representation</h2><p>For MD-backed carriers, the complete museum-digital PDF set is the only selectable Master. One canonical TIFF page set is retained alongside it as the corresponding source-page representation—not as another Master. Additional TIFF variants/copies, local PDF versions and JPEGs become Redundant after selection. Local-only carriers retain TIFF/PDF/surrogate fallback choices.</p>{''.join(primary_cards)}
{f'<details class="panel"><summary>{len(evidence_cards)} local alternative/duplicate representation group(s) — retained as Redundant evidence</summary>{"".join(evidence_cards)}</details>' if evidence_cards else ''}
<form id="decision" class="decision" method="post" action="/decision"><input type="hidden" name="csrf" value="{e(self.server.csrf)}"><input type="hidden" name="carrier_key" value="{e(key)}"><h2>Record decision</h2><label>Short note<textarea class="note" name="note">{e(decision.get('note',''))}</textarea></label><p><button name="outcome" value="selected_master">Select one Master; mark every other candidate Redundant</button> <button name="outcome" value="unresolved">Leave unresolved</button></p><p class="technical">Every source occurrence remains in the drain register with deletion_authorized=false.</p></form>"""
        self.page(f"Brigadebücher {key}", body)

    def _candidate_path(self) -> Path:
        parameters = self.parameters()
        candidate = self.server.collection.candidates.get(parameters.get("candidate", ""))
        if candidate is None:
            raise WorkbenchError("unknown preview candidate")
        index = int(parameters.get("index", "0"))
        if index < 0 or index >= len(candidate["paths"]):
            raise WorkbenchError("preview member is out of range")
        path = Path(candidate["paths"][index]).resolve(strict=True)
        path.relative_to(SOURCE_ROOT.resolve(strict=True))
        return path

    def preview(self) -> None:
        path = self._candidate_path()
        try:
            if path.suffix.casefold() == ".pdf":
                result = subprocess.run(
                    [
                        "pdftocairo",
                        "-f",
                        "1",
                        "-l",
                        "1",
                        "-singlefile",
                        "-scale-to",
                        "1000",
                        "-jpeg",
                        str(path),
                        "-",
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode or not result.stdout:
                    raise WorkbenchError(f"PDF preview failed: {path.name}")
                self.send_bytes(result.stdout, "image/jpeg")
                return
            from PIL import Image, ImageOps

            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1000, 1000))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=84)
            self.send_bytes(buffer.getvalue(), "image/jpeg")
        except (OSError, subprocess.SubprocessError, WorkbenchError) as error:
            message = e(f"Preview unavailable\n{path.name}\n{error}")
            svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="700"><rect width="100%" height="100%" fill="#2b2c29"/><text x="50" y="100" fill="#fff" font-family="sans-serif" font-size="26">{message}</text></svg>"""
            self.send_bytes(svg.encode("utf-8"), "image/svg+xml; charset=utf-8")

    def source(self) -> None:
        path = self._candidate_path()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 1024 * 1024)

    def drain(self) -> None:
        summary = self.server.collection.summary()
        counts = "".join(
            f"<li>{e(key)}: {value}</li>"
            for key, value in summary["disposition_counts"].items()
        )
        body = f"""<h1>Comprehensive source-disposition / drain register</h1><p>This register includes all {summary['source_rows']} indexed in-scope source occurrences: {summary['image_rows']} images and {summary['document_rows']} documents. It includes selected accession inputs, current derivatives, duplicate occurrences, rejected candidates, metadata evidence, and contextual project documentation.</p><p><strong>No row authorizes deletion.</strong> A listed source becomes eligible for any later drain action only after successful accession has been independently verified and the user gives a separate explicit deletion instruction.</p><section class="metrics"><div class="metric"><strong>{summary['source_rows']}</strong>unique source paths</div><div class="metric"><strong>{human_size(summary['source_bytes'])}</strong>indexed source bytes</div><div class="metric"><strong>{summary['deletion_authorized_rows']}</strong>deletion authorized</div></section><section class="panel"><h2>Current dispositions</h2><ul>{counts}</ul><p>Durable human-readable CSV files:</p><p class="technical">{e(DRAIN_LIST)}</p><p class="technical">{e(SOURCE_DISPOSITION)}</p></section>"""
        self.page("Brigadebücher drain register", body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--prepare-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    collection = Collection861(args.actor)
    if args.prepare_only:
        print(json.dumps(collection.summary(), ensure_ascii=False, indent=2))
        return 0
    server = Server(("127.0.0.1", args.port), collection)
    print(f"Brigadebücher workbench: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
