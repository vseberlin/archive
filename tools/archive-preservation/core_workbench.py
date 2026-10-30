#!/usr/bin/env python3
"""Local V2 archive workbench with reusable visual reconciliation."""

from __future__ import annotations

import argparse
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
import webbrowser
import uuid
from collections import Counter
from contextlib import closing
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import catalog_core
import kurt_schwarz_reconciliation
import reconciliation
import source_registry
import tiff_accession
import tiff_metadata
import wf_sender_workbench
from jobs.provenance_restoration import (
    md_capture,
    surrogate_accession,
    unit_recovery,
)
from workflows import (
    editor_reconcile,
    master_candidate,
    recovery_accession,
    recovery_correction,
    recovery_metadata,
)


WORKBENCH_VERSION = "0.20.19"
CSS_PATH = Path(__file__).with_name("workbench.css")
JS_PATH = Path(__file__).with_name("workbench.js")
EDITOR_PREVIEW_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
    Path("/media/vladimir/Elements"),
)


def e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def url(path: str, **parameters: Any) -> str:
    values = {key: value for key, value in parameters.items() if value not in ("", None)}
    return path + (f"?{urllib.parse.urlencode(values)}" if values else "")


def next_pending_kurt_record(
    records: list[dict[str, Any]], current_record_key: str
) -> str:
    if not records:
        return ""
    current_index = next(
        (
            index
            for index, record in enumerate(records)
            if record["record_key"] == current_record_key
        ),
        -1,
    )
    for offset in range(1, len(records) + 1):
        record = records[(current_index + offset) % len(records)]
        if (
            record["record_key"] != current_record_key
            and record["review_state"] == "pending"
        ):
            return str(record["record_key"])
    return ""


def next_pending_kurt_document(
    documents: list[dict[str, Any]], current_document_id: str
) -> str:
    if not documents:
        return ""
    current_index = next(
        (
            index
            for index, document in enumerate(documents)
            if document["document_id"] == current_document_id
        ),
        -1,
    )
    for offset in range(1, len(documents) + 1):
        document = documents[(current_index + offset) % len(documents)]
        if (
            document["document_id"] != current_document_id
            and document["review_state"] == "pending"
        ):
            return str(document["document_id"])
    return ""


def next_pending_kurt_object_census(
    candidates: list[dict[str, Any]], current_candidate_id: str
) -> str:
    if not candidates:
        return ""
    current_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate["candidate_id"] == current_candidate_id
        ),
        -1,
    )
    for offset in range(1, len(candidates) + 1):
        candidate = candidates[(current_index + offset) % len(candidates)]
        if (
            candidate["candidate_id"] != current_candidate_id
            and candidate["review_state"] == "pending"
        ):
            return str(candidate["candidate_id"])
    return ""


def object_census_candidate_is_confirmed_group_master(
    candidate_id: str, pairs: list[dict[str, Any]]
) -> bool:
    """Avoid asking for another Master decision after group confirmation."""

    return any(
        (pair.get("decision") or {}).get("outcome") == "same_photo"
        and str((pair.get("decision") or {}).get("primary_candidate_id", ""))
        == candidate_id
        for pair in pairs
    )


def badge(value: Any) -> str:
    text = str(value or "unknown")
    return f'<span class="badge badge--{e(text.replace("_", "-"))}">{e(text.replace("_", " "))}</span>'


def recovery_correction_queue_state(state: str) -> str:
    """Map the explicit correction lifecycle to the editor queue."""

    return {
        "ready": "ready",
        "complete": "reconciled",
    }.get(state, "visual_review")


def recovery_correction_is_locked(state: str) -> bool:
    """Completed corrective accessions require an explicit reopen before edits."""

    return state == "complete"


def option(value: str, label: str, selected: str = "") -> str:
    return (
        f'<option value="{e(value)}"'
        f'{" selected" if value == selected else ""}>{e(label)}</option>'
    )


def wf_sender_candidate_selection(
    candidates: list[dict[str, Any]],
    *,
    requested: str = "",
    decision: sqlite3.Row | dict[str, Any] | None = None,
) -> str:
    """Keep a decided WF-Sender record on its reviewed representation."""

    if requested:
        return requested
    if decision:
        try:
            basis = json.loads(str(decision["basis"]))
            decided_candidate = str(basis.get("candidate_id") or "")
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            decided_candidate = ""
        if any(
            str(item.get("candidate_id") or "") == decided_candidate
            for item in candidates
        ):
            return decided_candidate
    return str(candidates[0]["candidate_id"]) if candidates else ""


def human_size(value: Any) -> str:
    size = int(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def recovery_derivative_ready(
    master_source: str,
    *,
    available_derivatives: int,
    selected_derivatives: int,
) -> bool:
    """A JPEG surrogate source is sufficient without another derivative."""

    if not master_source or selected_derivatives > 1:
        return False
    if Path(master_source).suffix.casefold() not in {".tif", ".tiff"}:
        return True
    return (
        selected_derivatives == 1
        if available_derivatives
        else selected_derivatives == 0
    )


def recovery_visual_confirmation_ready(
    visuals: list[sqlite3.Row] | list[dict[str, Any]],
) -> bool:
    """Match the identity guard before offering the combined review action."""

    roles = {
        str(item["role"])
        for item in visuals
        if item["status"] == "available" and item["cache_relpath"]
    }
    return bool(roles & reconciliation.LOCAL_VISUAL_ROLES) and bool(
        roles & reconciliation.EXTERNAL_VISUAL_ROLES
    )


def recovery_candidate_role(
    record: dict[str, Any],
    evidence: sqlite3.Row | dict[str, Any],
    local_evidence: list[sqlite3.Row] | list[dict[str, Any]],
) -> tuple[str, str]:
    """Classify one bounded recovery image without trusting its filename alone."""

    source = Path(str(evidence["source_locator"] or ""))
    if source.suffix.casefold() in {".tif", ".tiff"}:
        return "master_tiff_candidate", "Local TIFF master candidate"
    sizes = {
        str(Path(str(item["path"])).resolve()): int(item["size"])
        for item in record["files"]
    }
    local_candidates = [
        item
        for item in local_evidence
        if Path(str(item["source_locator"] or "")).suffix.casefold()
        not in {".tif", ".tiff"}
    ]

    def resolution_rank(item: sqlite3.Row | dict[str, Any]) -> tuple[int, int]:
        width = int(item["width_px"] or 0)
        height = int(item["height_px"] or 0)
        byte_size = sizes.get(
            str(Path(str(item["source_locator"])).resolve()),
            0,
        )
        return (width * height, byte_size)

    rank = resolution_rank(evidence)
    if rank != (0, 0) and rank == max(
        (resolution_rank(item) for item in local_candidates),
        default=(0, 0),
    ):
        return (
            "high_resolution_jpeg_candidate",
            "Local high-resolution JPEG candidate",
        )
    return "local_derivative", "Local derivative candidate"


class CoreWorkbenchServer(ThreadingHTTPServer):
    # Requests may run in parallel when one editor uses multiple tabs. Every
    # write remains correct because guarded operations use BEGIN IMMEDIATE and
    # SQLite serializes the single catalog writer before any mutation occurs.
    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        actor: str,
        stress_manifest: Path | None = None,
        visual_candidates: Path | None = None,
        recovery_scope: Path | None = None,
        surrogate_swap_source: Path | None = None,
        wf_sender_run: Path | None = None,
        wf_sender_fallback_run: Path | None = None,
        kurt_schwarz_unit: Path | None = None,
    ) -> None:
        self.root = catalog_core.assert_safe_work_root(root)
        self.actor = actor.strip()
        if not self.actor:
            raise catalog_core.CoreCatalogError("named workbench actor is required")
        self.wf_sender_review: wf_sender_workbench.WfSenderReview | None = None
        self.kurt_schwarz_review: (
            kurt_schwarz_reconciliation.KurtSchwarzReview | None
        ) = None
        if kurt_schwarz_unit is not None:
            if any(
                value is not None
                for value in (
                    stress_manifest,
                    visual_candidates,
                    recovery_scope,
                    surrogate_swap_source,
                    wf_sender_run,
                    wf_sender_fallback_run,
                )
            ):
                raise catalog_core.CoreCatalogError(
                    "Kurt Schwarz mode cannot be combined with another "
                    "workbench mode"
                )
            review_root = (
                self.root
                / "work"
                / "reconciliation"
                / "unit-reconciliation-v1"
            )
            self.kurt_schwarz_review = (
                kurt_schwarz_reconciliation.KurtSchwarzReview(
                    kurt_schwarz_unit,
                    review_root,
                )
            )
            self.catalog_path = self.kurt_schwarz_review.database
            self.stress_manifest = None
            self.recovery_scope = None
            self.csrf_token = secrets.token_urlsafe(32)
            super().__init__(address, CoreWorkbenchHandler)
            return
        if wf_sender_run is not None or wf_sender_fallback_run is not None:
            if wf_sender_run is None or wf_sender_fallback_run is None:
                raise catalog_core.CoreCatalogError(
                    "WF-Sender mode requires both primary and fallback runs"
                )
            if any(
                value is not None
                for value in (
                    stress_manifest,
                    visual_candidates,
                    recovery_scope,
                    surrogate_swap_source,
                )
            ):
                raise catalog_core.CoreCatalogError(
                    "WF-Sender mode cannot be combined with another workbench mode"
                )
            self.catalog_path = self.root / "work" / "catalog.sqlite"
            self.wf_sender_review = wf_sender_workbench.WfSenderReview(
                self.root,
                wf_sender_run,
                wf_sender_fallback_run,
            )
            for evidence_root in self.wf_sender_review.evidence_roots():
                if not source_registry.root_is_read_only(evidence_root):
                    raise catalog_core.CoreCatalogError(
                        f"WF-Sender evidence root must be mounted read-only: "
                        f"{evidence_root}"
                    )
            self.stress_manifest = None
            self.recovery_scope = None
            self.csrf_token = secrets.token_urlsafe(32)
            super().__init__(address, CoreWorkbenchHandler)
            return
        catalog_core.require_core_mode(self.root)
        self.catalog_path = self.root / "work" / "catalog.sqlite"
        connection = catalog_core.connect_catalog(self.catalog_path)
        try:
            catalog_core.reconcile_current_generation(connection, self.root)
            catalog_core.assert_catalog_integrity(connection)
        finally:
            connection.close()
        # Accession spans verified copies, several catalog transactions and two
        # publications. A double click must not start an overlapping workflow.
        self.recovery_accession_lock = threading.Lock()
        self.stress_manifest_path: Path | None = None
        self.stress_manifest_digest = ""
        self.stress_manifest: dict[str, Any] | None = None
        self.stress_cases: dict[str, dict[str, Any]] = {}
        self.retained_index_path: Path | None = None
        self.visual_candidate_report_path: Path | None = None
        self.visual_candidate_report_digest = ""
        self.visual_candidate_queries: dict[str, dict[str, Any]] = {}
        self.recovery_records: dict[str, dict[str, Any]] = {}
        self.recovery_scope: dict[str, Any] | None = None
        self.surrogate_swap_source: Path | None = None
        self.surrogate_swap_candidates: dict[str, list[dict[str, Any]]] = {}
        self.recovery_supplemental_summary = {
            "files": 0,
            "linked": 0,
            "unlinked": 0,
            "retired": 0,
            "package_fallbacks": 0,
        }
        if stress_manifest is not None:
            manifest_path = stress_manifest.expanduser().resolve(strict=True)
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise catalog_core.CoreCatalogError(
                    f"cannot read stress-pilot manifest: {manifest_path}"
                ) from error
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "iss-provenance-stress-pilot-v1"
            ):
                raise catalog_core.CoreCatalogError(
                    "stress-pilot manifest format is invalid"
                )
            if Path(str(manifest.get("pilot_root") or "")).resolve() != self.root:
                raise catalog_core.CoreCatalogError(
                    "stress-pilot manifest does not describe this catalog root"
                )
            cases = manifest.get("cases")
            if not isinstance(cases, list):
                raise catalog_core.CoreCatalogError(
                    "stress-pilot manifest cases are invalid"
                )
            self.stress_cases = {
                str(case.get("unit_id") or ""): case
                for case in cases
                if isinstance(case, dict) and case.get("unit_id")
            }
            retained = manifest.get("retained_index")
            if not isinstance(retained, dict) or not retained.get("path"):
                raise catalog_core.CoreCatalogError(
                    "stress-pilot retained index binding is missing"
                )
            retained_path = Path(str(retained["path"])).resolve(strict=True)
            stat = retained_path.stat()
            observed = {
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
            if any(
                int(retained.get(key, -1)) != value
                for key, value in observed.items()
            ):
                raise catalog_core.CoreCatalogError(
                    "stress-pilot retained index fingerprint changed"
                )
            self.stress_manifest_path = manifest_path
            self.stress_manifest_digest = (
                "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            )
            self.stress_manifest = manifest
            self.retained_index_path = retained_path
        if visual_candidates is not None:
            if self.stress_manifest_path is None:
                raise catalog_core.CoreCatalogError(
                    "visual candidates require a bound stress-pilot manifest"
                )
            report_path = visual_candidates.expanduser().resolve(strict=True)
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise catalog_core.CoreCatalogError(
                    f"cannot read visual-candidate report: {report_path}"
                ) from error
            if (
                not isinstance(report, dict)
                or report.get("format") != "iss-visual-candidate-search-v1"
                or Path(str(report.get("pilot_root") or "")).resolve()
                != self.root
                or report.get("manifest_sha256")
                != self.stress_manifest_digest.removeprefix("sha256:")
            ):
                raise catalog_core.CoreCatalogError(
                    "visual-candidate report does not match this pilot"
                )
            queries = report.get("queries")
            if not isinstance(queries, list):
                raise catalog_core.CoreCatalogError(
                    "visual-candidate report queries are invalid"
                )
            for query in queries:
                if not isinstance(query, dict):
                    raise catalog_core.CoreCatalogError(
                        "visual-candidate query is invalid"
                    )
                unit_id = str(query.get("unit_id") or "")
                candidates = query.get("candidates")
                if (
                    unit_id not in self.stress_cases
                    or not isinstance(candidates, list)
                    or len(candidates) > 50
                ):
                    raise catalog_core.CoreCatalogError(
                        "visual-candidate query escaped the stress pilot"
                    )
                seen: set[str] = set()
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        raise catalog_core.CoreCatalogError(
                            "visual candidate is invalid"
                        )
                    asset_id = str(candidate.get("asset_id") or "")
                    path = Path(str(candidate.get("path") or ""))
                    if (
                        not asset_id
                        or asset_id in seen
                        or path.suffix.casefold() not in {".tif", ".tiff"}
                        or int(candidate.get("size_bytes") or 0) <= 0
                        or int(candidate.get("mtime_ns") or 0) <= 0
                    ):
                        raise catalog_core.CoreCatalogError(
                            "visual candidate lacks immutable file evidence"
                        )
                    seen.add(asset_id)
                    resolved = path.expanduser().resolve()
                    if not any(
                        resolved != root.resolve()
                        and resolved.is_relative_to(root.resolve())
                        for root in EDITOR_PREVIEW_ROOTS
                    ):
                        raise catalog_core.CoreCatalogError(
                            "visual candidate escaped protected evidence roots"
                        )
                    alternates = candidate.get("alternate_occurrences", [])
                    if not isinstance(alternates, list):
                        raise catalog_core.CoreCatalogError(
                            "visual candidate alternates are invalid"
                        )
                    for alternate in alternates:
                        if not isinstance(alternate, dict):
                            raise catalog_core.CoreCatalogError(
                                "visual candidate alternate is invalid"
                            )
                        alternate_path = Path(str(alternate.get("path") or ""))
                        alternate_resolved = alternate_path.expanduser().resolve()
                        if (
                            not alternate.get("asset_id")
                            or int(alternate.get("size_bytes") or 0) <= 0
                            or int(alternate.get("mtime_ns") or 0) <= 0
                            or alternate_path.suffix.casefold()
                            not in {".tif", ".tiff"}
                            or not any(
                                alternate_resolved != root.resolve()
                                and alternate_resolved.is_relative_to(root.resolve())
                                for root in EDITOR_PREVIEW_ROOTS
                            )
                        ):
                            raise catalog_core.CoreCatalogError(
                                "visual candidate alternate lacks protected evidence"
                            )
                self.visual_candidate_queries[unit_id] = query
            self.visual_candidate_report_path = report_path
            self.visual_candidate_report_digest = (
                "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
            )
        if recovery_scope is not None:
            if stress_manifest is not None:
                raise catalog_core.CoreCatalogError("recovery scope and stress manifest are mutually exclusive")
            scope_path = recovery_scope.expanduser().resolve(strict=True)
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
            if scope.get("format") != "iss-bounded-unit-recovery-v1":
                raise catalog_core.CoreCatalogError("bounded recovery scope format is invalid")
            if not scope_path.is_relative_to(self.root / "work" / "recovery"):
                raise catalog_core.CoreCatalogError("bounded recovery scope escaped the recovery root")
            retained_path = Path(str(scope["retained_index"]["path"])).resolve(strict=True)
            connection = sqlite3.connect(f"file:{retained_path}?mode=ro&immutable=1", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                for inventory in scope["inventories"]:
                    local = connection.execute("SELECT * FROM objects WHERE inventory_number = ?", (inventory,)).fetchone()
                    files = connection.execute(
                        """SELECT f.path, f.basename, f.size, f.extension, l.link_type, l.confidence
                           FROM file_object_links l JOIN files f ON f.id=l.file_id
                           WHERE l.inventory_number=? AND f.path LIKE ? ORDER BY f.path""",
                        (inventory, str(scope["source"]["root"]) + "/%"),
                    ).fetchall()
                    terminal = scope_path.parent / "museum-digital" / "objects" / f"{urllib.parse.quote(inventory, safe='-_.')}.json"
                    md_evidence = json.loads(terminal.read_text(encoding="utf-8"))
                    md = md_evidence.get("exact_objects", [{}])[0] if md_evidence.get("state") == "found" else None
                    historical_files = [dict(row) for row in files]
                    available_files = [
                        item
                        for item in historical_files
                        if Path(str(item["path"])).is_file()
                    ]
                    self.recovery_supplemental_summary["retired"] += (
                        len(historical_files) - len(available_files)
                    )
                    self.recovery_records[inventory] = {
                        "inventory": inventory,
                        "local": dict(local) if local else {},
                        "files": available_files,
                        "retired_files": [
                            item
                            for item in historical_files
                            if not Path(str(item["path"])).is_file()
                        ],
                        "md_state": md_evidence["state"],
                        "md": md or {},
                        "exception_categories": [],
                        "correction_required": False,
                        "correction_reason": "",
                    }
            finally:
                connection.close()
            try:
                supplements = unit_recovery.supplemental_sources(scope_path.parent)
            except unit_recovery.UnitRecoveryError:
                # A source-retirement pass may intentionally remove files after
                # the frozen manifest was captured. Keep the manifest as
                # provenance, but expose only files that still exist.
                supplements = []
                supplement_dir = scope_path.parent / "supplemental-sources"
                for manifest_path in sorted(supplement_dir.glob("source-*.json")):
                    supplement = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    if supplement.get("format") != unit_recovery.SUPPLEMENTAL_FORMAT:
                        raise
                    supplements.append(supplement)
            for supplement in supplements:
                supplemental_root = Path(
                    str(supplement["source"]["root"])
                ).expanduser().resolve()
                for item in supplement["files"]:
                    self.recovery_supplemental_summary["files"] += 1
                    inventory = str(item.get("linked_inventory") or "")
                    if not inventory or inventory not in self.recovery_records:
                        self.recovery_supplemental_summary["unlinked"] += 1
                        continue
                    source = (
                        supplemental_root / str(item["relative_path"])
                    ).resolve()
                    if not source.is_file():
                        self.recovery_supplemental_summary["retired"] += 1
                        self.recovery_records[inventory]["retired_files"].append(
                            {
                                "path": str(source),
                                "basename": source.name,
                                "size": int(item["size_bytes"]),
                                "extension": source.suffix.casefold().lstrip("."),
                                "link_type": "retired_supplemental_source",
                                "confidence": 100,
                            }
                        )
                        continue
                    self.recovery_supplemental_summary["linked"] += 1
                    self.recovery_records[inventory]["files"].append(
                        {
                            "path": str(source),
                            "basename": source.name,
                            "size": int(item["size_bytes"]),
                            "extension": source.suffix.casefold().lstrip("."),
                            "link_type": "exact_filename_inventory",
                            "confidence": 100,
                        }
                    )
            exceptions_path = scope_path.parent / "reconciliation-exceptions.json"
            if exceptions_path.is_file():
                exceptions = json.loads(
                    exceptions_path.read_text(encoding="utf-8")
                )
                if exceptions.get("format") != "iss-recovery-exceptions-v1":
                    raise catalog_core.CoreCatalogError(
                        "recovery exception manifest format is invalid"
                    )
                for item in exceptions.get("records") or []:
                    inventory = str(item.get("inventory") or "")
                    if not inventory:
                        raise catalog_core.CoreCatalogError(
                            "recovery exception inventory is missing"
                        )
                    record = self.recovery_records.setdefault(
                        inventory,
                        {
                            "inventory": inventory,
                            "local": {},
                            "files": [],
                            "retired_files": [],
                            "md_state": "not_found",
                            "md": {},
                            "exception_categories": [],
                            "correction_required": False,
                            "correction_reason": "",
                        },
                    )
                    record["local"].update(item.get("local") or {})
                    record["exception_categories"] = list(
                        item.get("categories") or []
                    )
                    record["correction_required"] = bool(
                        item.get("correction_required")
                    )
                    record["correction_reason"] = str(
                        item.get("reason") or ""
                    )
                    known = {str(value["path"]) for value in record["files"]}
                    for candidate in item.get("files") or []:
                        path = Path(str(candidate.get("path") or "")).resolve()
                        if not path.is_file() or str(path) in known:
                            continue
                        record["files"].append(
                            {
                                "path": str(path),
                                "basename": path.name,
                                "size": int(candidate.get("size_bytes") or path.stat().st_size),
                                "extension": path.suffix.casefold().lstrip("."),
                                "link_type": "reconciliation_exception",
                                "confidence": 100,
                            }
                        )
                        known.add(str(path))
            # Retired source paths remain provenance. For browsing, bind the
            # verified active package representations as explicit fallbacks.
            catalog = catalog_core.connect_catalog(
                self.catalog_path, read_only=True
            )
            try:
                for row in catalog.execute(
                    """SELECT ao.archival_label, ef.role, fo.relative_path
                       FROM archive_objects ao
                       JOIN entity_files ef ON ef.entity_id=ao.entity_id
                       JOIN file_occurrences fo
                         ON fo.occurrence_id=ef.occurrence_id
                       WHERE ao.status='active' AND ef.status='active'
                         AND fo.status='verified'
                         AND ef.role IN ('preservation_master',
                                         'preservation_surrogate',
                                         'access_derivative')
                       ORDER BY ao.archival_label,
                         CASE ef.role
                           WHEN 'preservation_master' THEN 0
                           WHEN 'preservation_surrogate' THEN 1 ELSE 2 END"""
                ):
                    inventory = str(row["archival_label"])
                    record = self.recovery_records.get(inventory)
                    if not record:
                        continue
                    path = (self.root / str(row["relative_path"])).resolve()
                    if not path.is_file():
                        continue
                    known = {str(item["path"]) for item in record["files"]}
                    if str(path) in known:
                        continue
                    record["files"].append(
                        {
                            "path": str(path),
                            "basename": path.name,
                            "size": path.stat().st_size,
                            "extension": path.suffix.casefold().lstrip("."),
                            "link_type": "active_package_fallback",
                            "confidence": 100,
                        }
                    )
                    self.recovery_supplemental_summary[
                        "package_fallbacks"
                    ] += 1
            finally:
                catalog.close()
            self.retained_index_path = retained_path
            self.recovery_scope = scope
        if surrogate_swap_source is not None:
            source_root = surrogate_swap_source.expanduser().resolve(strict=True)
            if not source_root.is_dir():
                raise catalog_core.CoreCatalogError(
                    f"surrogate-swap source is not a directory: {source_root}"
                )
            if not source_registry.root_is_read_only(source_root):
                raise catalog_core.CoreCatalogError(
                    f"surrogate-swap source must be mounted read-only: {source_root}"
                )
            self.surrogate_swap_source = source_root
            self.surrogate_swap_candidates = self._scan_surrogate_swap_candidates(
                source_root
            )
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, CoreWorkbenchHandler)

    @property
    def editor_mode(self) -> bool:
        return self.stress_manifest is not None or self.recovery_scope is not None

    @property
    def recovery_mode(self) -> bool:
        return self.recovery_scope is not None

    @property
    def wf_sender_mode(self) -> bool:
        return self.wf_sender_review is not None

    @property
    def kurt_schwarz_mode(self) -> bool:
        return self.kurt_schwarz_review is not None

    @staticmethod
    def _surrogate_swap_inventory(filename: str) -> str:
        match = re.match(r"^(vlv-\d+(?:-\d+)?)\b", filename, re.IGNORECASE)
        return match.group(1).upper() if match else ""

    def _scan_surrogate_swap_candidates(
        self,
        source_root: Path,
    ) -> dict[str, list[dict[str, Any]]]:
        candidates: dict[str, list[dict[str, Any]]] = {}
        for directory, dirs, files in os.walk(source_root):
            dirs[:] = [
                item
                for item in dirs
                if item not in {"$RECYCLE.BIN", "System Volume Information"}
            ]
            for filename in files:
                suffix = Path(filename).suffix.casefold()
                if suffix not in {".tif", ".tiff"}:
                    continue
                inventory = self._surrogate_swap_inventory(filename)
                if not inventory:
                    continue
                path = Path(directory) / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue
                candidates.setdefault(inventory, []).append(
                    {
                        "inventory": inventory,
                        "path": str(path),
                        "basename": filename,
                        "size_bytes": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns),
                    }
                )
        for items in candidates.values():
            items.sort(
                key=lambda item: (
                    int(item["size_bytes"]),
                    str(item["path"]),
                ),
                reverse=True,
            )
        return candidates

    def recovery_structure(self) -> dict[str, dict[str, Any]]:
        """Return editor-confirmed carrier structure from the live catalog."""
        structure = {
            inventory: {
                "components": [],
                "carrier_id": "",
                "record_kind": "standalone",
            }
            for inventory in self.recovery_records
        }
        connection = catalog_core.connect_catalog(self.catalog_path, read_only=True)
        try:
            rows = connection.execute(
                """SELECT component.archival_label AS component_inventory,
                          carrier.archival_label AS carrier_inventory
                   FROM entity_relations relation
                   JOIN archive_objects component
                     ON component.entity_id = relation.subject_entity_id
                   JOIN archive_objects carrier
                     ON carrier.entity_id = relation.object_entity_id
                   WHERE relation.predicate = 'component_of'
                     AND relation.status = 'accepted'
                   ORDER BY relation.updated_at, relation.relation_id"""
            )
            for row in rows:
                component = str(row["component_inventory"])
                carrier = str(row["carrier_inventory"])
                if component not in structure or carrier not in structure:
                    continue
                structure[component]["carrier_id"] = carrier
                structure[component]["record_kind"] = "component"
                if component not in structure[carrier]["components"]:
                    structure[carrier]["components"].append(component)
        finally:
            connection.close()
        for item in structure.values():
            item["components"].sort()
            if item["components"] and item["record_kind"] == "standalone":
                item["record_kind"] = "carrier"
        return structure

    def recovery_entity_id(self, inventory: str) -> str:
        if inventory not in self.recovery_records:
            raise catalog_core.CoreCatalogError(
                f"record is outside the bounded Paragon recovery: {inventory}"
            )
        connection = catalog_core.connect_catalog(self.catalog_path, read_only=True)
        try:
            rows = list(
                connection.execute(
                    """SELECT entity_id FROM archive_objects
                       WHERE archival_label = ? AND status = 'active'""",
                    (inventory,),
                )
            )
        finally:
            connection.close()
        if len(rows) != 1:
            raise catalog_core.CoreCatalogError(
                f"record does not resolve to one active archive object: {inventory}"
            )
        return str(rows[0]["entity_id"])

    def recovery_review_states(self) -> dict[str, str]:
        return {
            inventory: str(detail["status"])
            for inventory, detail in self.recovery_review_details().items()
        }

    def recovery_review_details(self) -> dict[str, dict[str, str]]:
        details = {
            inventory: {
                "status": "not_ready",
                "retained_entity_id": "",
                "retained_inventory": "",
                "actor": "",
                "reason": "",
                "decided_at": "",
            }
            for inventory in self.recovery_records
        }
        connection = catalog_core.connect_catalog(self.catalog_path, read_only=True)
        try:
            entity_details = catalog_core.reconciliation_review_details(connection)
            entity_inventories = {
                str(row["entity_id"]): str(row["archival_label"])
                for row in connection.execute(
                """SELECT entity_id, archival_label FROM archive_objects
                   WHERE status='active'"""
                )
            }
            for entity_id, inventory in entity_inventories.items():
                if inventory in details:
                    current = entity_details.get(entity_id, {})
                    details[inventory].update(current)
                    details[inventory]["retained_inventory"] = (
                        entity_inventories.get(
                            str(current.get("retained_entity_id") or ""), ""
                        )
                    )
        finally:
            connection.close()
        return details

    def recovery_correction_states(self) -> dict[str, dict[str, str]]:
        """Map explicit correction readiness from the append-only catalog log."""

        connection = catalog_core.connect_catalog(
            self.catalog_path, read_only=True
        )
        try:
            entity_by_inventory = {
                str(row["archival_label"]): str(row["entity_id"])
                for row in connection.execute(
                    """SELECT entity_id, archival_label FROM archive_objects
                       WHERE status='active'"""
                )
            }
            by_entity = catalog_core.recovery_correction_states(connection)
        finally:
            connection.close()
        return {
            inventory: by_entity.get(
                entity_id,
                {"state": "open", "actor": "", "reason": "", "decided_at": ""},
            )
            for inventory, entity_id in entity_by_inventory.items()
            if inventory in self.recovery_records
        }

    def recovery_accession_plan(self, inventory: str) -> dict[str, Any]:
        """Derive accession readiness from accepted evidence, never a manual flag."""

        record = self.recovery_records.get(inventory)
        if not record:
            raise reconciliation.ReconciliationError("unknown recovery record")
        review = self.recovery_review_details()[inventory]
        if review["status"] == "rejected":
            return {"inventory": inventory, "state": "rejected", "reason": "record is rejected"}
        if record.get("correction_required"):
            correction = self.recovery_correction_states().get(
                inventory, {"state": "open"}
            )
            correction_state = str(correction.get("state") or "open")
            return {
                "inventory": inventory,
                "state": {
                    "ready": "correction_ready",
                    "complete": "reconciled",
                }.get(correction_state, "visual_review"),
                "reason": str(record.get("correction_reason") or "correction required"),
            }
        with closing(reconciliation.connect_campaign(self.root, read_only=True)) as campaign:
            campaign_record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE campaign_id='MD-RECOVERY-WF-PARAGON'
                     AND inventory_label=?""",
                (inventory,),
            ).fetchone()
            if not campaign_record:
                return {
                    "inventory": inventory,
                    "state": "visual_review",
                    "reason": "reconciliation evidence is not prepared",
                }
            evidence_rows = list(
                campaign.execute(
                    """SELECT * FROM visual_evidence
                       WHERE record_id=? AND role<>'museum_digital_snapshot'""",
                    (campaign_record["record_id"],),
                )
            )
        entity_id = str(campaign_record["entity_id"])
        with closing(catalog_core.connect_catalog(
            self.catalog_path, read_only=True
        )) as catalog:
            preserved = catalog.execute(
                """SELECT 1
                   FROM entity_files
                   WHERE entity_id=? AND status='active'
                     AND role IN ('preservation_master',
                                  'preservation_surrogate')
                   LIMIT 1""",
                (entity_id,),
            ).fetchone()
            if preserved:
                return {
                    "inventory": inventory,
                    "entity_id": entity_id,
                    "state": "reconciled",
                    "reason": "preservation representation already exists",
                }
            reviews = catalog_core.source_candidate_reviews(
                catalog, entity_id=entity_id
            )
            membership = catalog.execute(
                """SELECT unit_id FROM entity_unit_memberships
                   WHERE entity_id=? AND role='primary'""",
                (entity_id,),
            ).fetchone()
        if campaign_record["identity_status"] != "resolved":
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "visual identity is not confirmed",
            }
        if not membership:
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "primary unit membership is missing",
            }
        by_state: dict[str, list[dict[str, Any]]] = {
            "selected_master": [],
            "selected_derivative": [],
        }
        for row in evidence_rows:
            state = str(reviews.get(str(row["evidence_id"]), {}).get("state") or "")
            if state in by_state:
                by_state[state].append(dict(row))
        if len(by_state["selected_master"]) != 1:
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "exactly one local master or surrogate must be selected",
            }
        if len(by_state["selected_derivative"]) > 1:
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "more than one local derivative is selected",
            }
        selected_master_source = str(by_state["selected_master"][0]["source_locator"])
        available_derivatives = []
        for row in evidence_rows:
            role, _label = recovery_candidate_role(record, row, evidence_rows)
            state = str(
                reviews.get(str(row["evidence_id"]), {}).get("state") or "candidate"
            )
            if role == "local_derivative" and state != "redundant":
                available_derivatives.append(row)
        if not recovery_derivative_ready(
            selected_master_source,
            available_derivatives=len(available_derivatives),
            selected_derivatives=len(by_state["selected_derivative"]),
        ):
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "local derivative policy is unresolved",
            }
        config = catalog_core.require_core_mode(self.root)
        independent = str(config.get("independent_generation_root") or "")
        if not independent:
            return {
                "inventory": inventory,
                "entity_id": entity_id,
                "state": "visual_review",
                "reason": "independent pilot destination is not configured",
            }
        return {
            "inventory": inventory,
            "entity_id": entity_id,
            "unit_id": str(membership["unit_id"]),
            "state": "ready",
            "reason": "visual identity and representation selection are complete",
            "master_evidence": by_state["selected_master"][0],
            "derivative_evidence": (
                by_state["selected_derivative"][0]
                if by_state["selected_derivative"]
                else None
            ),
            "independent_root": independent,
        }

    def recovery_queue_states(self) -> dict[str, str]:
        """Derive all queue states with bounded shared database reads."""

        states = {inventory: "visual_review" for inventory in self.recovery_records}
        reviews = self.recovery_review_details()
        correction_states = self.recovery_correction_states()
        for inventory, detail in reviews.items():
            if detail["status"] == "rejected":
                states[inventory] = "rejected"
        for inventory, record in self.recovery_records.items():
            if (
                record.get("correction_required")
                and states[inventory] != "rejected"
            ):
                correction = correction_states.get(
                    inventory, {"state": "open"}
                )
                states[inventory] = recovery_correction_queue_state(
                    str(correction.get("state") or "open")
                )
        with closing(reconciliation.connect_campaign(
            self.root, read_only=True
        )) as campaign:
            campaign_rows = {
                str(row["inventory_label"]): row
                for row in campaign.execute(
                    """SELECT * FROM reconciliation_records
                       WHERE campaign_id='MD-RECOVERY-WF-PARAGON'"""
                )
            }
            evidence_by_record: dict[str, list[sqlite3.Row]] = {}
            for row in campaign.execute(
                """SELECT * FROM visual_evidence
                   WHERE role<>'museum_digital_snapshot'"""
            ):
                evidence_by_record.setdefault(str(row["record_id"]), []).append(row)
        with closing(catalog_core.connect_catalog(
            self.catalog_path, read_only=True
        )) as catalog:
            entity_by_inventory = {
                str(row["archival_label"]): str(row["entity_id"])
                for row in catalog.execute(
                    """SELECT entity_id, archival_label FROM archive_objects
                       WHERE status='active'"""
                )
            }
            preserved = {
                str(row["entity_id"])
                for row in catalog.execute(
                    """SELECT DISTINCT entity_id FROM entity_files
                       WHERE status='active'
                         AND role IN ('preservation_master',
                                      'preservation_surrogate')"""
                )
            }
            memberships = {
                str(row["entity_id"])
                for row in catalog.execute(
                    """SELECT entity_id FROM entity_unit_memberships
                       WHERE role='primary'"""
                )
            }
            config = catalog_core.require_core_mode(self.root)
            independent = bool(config.get("independent_generation_root"))
            for inventory, record in self.recovery_records.items():
                if states[inventory] == "rejected":
                    continue
                if record.get("correction_required"):
                    continue
                entity_id = entity_by_inventory.get(inventory, "")
                if entity_id in preserved:
                    states[inventory] = "reconciled"
                    continue
                campaign_record = campaign_rows.get(inventory)
                if (
                    not campaign_record
                    or campaign_record["identity_status"] != "resolved"
                    or entity_id not in memberships
                    or not independent
                ):
                    continue
                evidence_rows = evidence_by_record.get(
                    str(campaign_record["record_id"]), []
                )
                candidate_reviews = catalog_core.source_candidate_reviews(
                    catalog, entity_id=entity_id
                )
                masters = []
                derivatives = []
                available_derivatives = []
                for row in evidence_rows:
                    state = str(
                        candidate_reviews.get(
                            str(row["evidence_id"]), {}
                        ).get("state")
                        or "candidate"
                    )
                    if state == "selected_master":
                        masters.append(row)
                    elif state == "selected_derivative":
                        derivatives.append(row)
                    role, _label = recovery_candidate_role(
                        record, row, evidence_rows
                    )
                    if role == "local_derivative" and state != "redundant":
                        available_derivatives.append(row)
                if (
                    len(masters) == 1
                    and len(derivatives) <= 1
                    and recovery_derivative_ready(
                        str(masters[0]["source_locator"]),
                        available_derivatives=len(available_derivatives),
                        selected_derivatives=len(derivatives),
                    )
                ):
                    states[inventory] = "ready"
        return states

    def next_visual_inventory(self, current: str) -> str:
        states = self.recovery_queue_states()
        inventories = list(self.recovery_records)
        start = inventories.index(current) + 1 if current in inventories else 0
        ordered = inventories[start:] + inventories[:start]
        return next(
            (inventory for inventory in ordered if states[inventory] == "visual_review"),
            "",
        )

    def accession_recovery_record(
        self,
        inventory: str,
        *,
        reason: str = "",
        expected_binding: str = "",
    ) -> dict[str, Any]:
        plan = self.recovery_accession_plan(inventory)
        if plan["state"] != "ready":
            raise reconciliation.ReconciliationError(
                f"{inventory} is not ready for accession: {plan['reason']}"
            )
        binding = self.recovery_plan_binding(plan)
        if expected_binding and binding != expected_binding:
            raise reconciliation.ReconciliationError(
                f"{inventory} selection changed after the batch dry-run"
            )
        record = self.recovery_records[inventory]
        result = recovery_accession.accession_and_publish(
            self.root,
            Path(str(plan["independent_root"])),
            unit_id=str(plan["unit_id"]),
            entity_id=str(plan["entity_id"]),
            inventory=inventory,
            master_evidence=dict(plan["master_evidence"]),
            derivative_evidence=(
                dict(plan["derivative_evidence"])
                if plan["derivative_evidence"]
                else None
            ),
            museum_digital=dict(record["md"]),
            actor=self.actor,
            tool_version=f"core-workbench-{WORKBENCH_VERSION}",
            reason=reason,
        )
        with closing(catalog_core.connect_catalog(self.catalog_path)) as catalog:
            catalog_core.set_reconciliation_review_status(
                catalog,
                entity_id=str(plan["entity_id"]),
                status="reconciled",
                actor=self.actor,
                reason=reason,
            )
        return result

    @staticmethod
    def recovery_plan_binding(plan: dict[str, Any]) -> str:
        master = plan.get("master_evidence") or {}
        derivative = plan.get("derivative_evidence") or {}
        inventory = str(plan.get("inventory") or "")
        payload = {
            "inventory": inventory,
            "entity_id": str(plan.get("entity_id") or ""),
            "unit_id": str(plan.get("unit_id") or ""),
            "master_evidence_id": str(master.get("evidence_id") or ""),
            "master_digest": str(master.get("content_digest") or ""),
            "derivative_evidence_id": str(derivative.get("evidence_id") or ""),
            "derivative_digest": str(derivative.get("content_digest") or ""),
            "embedding_metadata": (
                recovery_metadata.require_embedding_metadata(inventory)
            ),
            "embedding_mapping": tiff_metadata.V2_MAPPING_VERSION,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def recovery_batch_plan(
        self,
        *,
        inventories: list[str] | None = None,
        limit: int = 0,
    ) -> dict[str, Any]:
        requested = set(inventories or [])
        unknown = sorted(requested - set(self.recovery_records))
        if unknown:
            raise reconciliation.ReconciliationError(
                "batch inventory escaped the bounded recovery: " + ", ".join(unknown)
            )
        items = []
        for inventory in self.recovery_records:
            if requested and inventory not in requested:
                continue
            plan = self.recovery_accession_plan(inventory)
            if plan["state"] != "ready":
                continue
            master = plan["master_evidence"]
            derivative = plan["derivative_evidence"]
            items.append(
                {
                    "inventory": inventory,
                    "entity_id": str(plan["entity_id"]),
                    "unit_id": str(plan["unit_id"]),
                    "master_evidence_id": str(master["evidence_id"]),
                    "master_digest": str(master["content_digest"]),
                    "master_source": str(master["source_locator"]),
                    "derivative_evidence_id": (
                        str(derivative["evidence_id"]) if derivative else ""
                    ),
                    "derivative_digest": (
                        str(derivative["content_digest"]) if derivative else ""
                    ),
                    "workbook_record_digest": str(
                        recovery_metadata.require_embedding_metadata(
                            inventory
                        )["workbook_record_digest"]
                    ),
                    "binding": self.recovery_plan_binding(plan),
                }
            )
            if limit and len(items) >= limit:
                break
        digest_payload = json.dumps(
            items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "format": "iss-recovery-accession-batch-plan-v1",
            "root": str(self.root),
            "scope_unit": str((self.recovery_scope or {}).get("unit_id") or ""),
            "count": len(items),
            "items": items,
            "plan_digest": "sha256:" + hashlib.sha256(digest_payload).hexdigest(),
        }


class CoreWorkbenchHandler(BaseHTTPRequestHandler):
    server: CoreWorkbenchServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"core-workbench {self.address_string()} {format % args}")

    @property
    def root(self) -> Path:
        return self.server.root

    def catalog(self, *, read_only: bool = False) -> sqlite3.Connection:
        return catalog_core.connect_catalog(
            self.server.catalog_path,
            read_only=read_only,
        )

    def campaign(self, *, read_only: bool = False) -> sqlite3.Connection:
        return reconciliation.connect_campaign(self.root, read_only=read_only)

    def require_recovery_record_editable(self, inventory: str) -> None:
        if not self.server.recovery_mode:
            return
        correction = self.server.recovery_correction_states().get(
            inventory, {"state": "open"}
        )
        if recovery_correction_is_locked(
            str(correction.get("state") or "open")
        ):
            raise reconciliation.ReconciliationError(
                f"{inventory} has a completed corrective accession; "
                "reopen the correction explicitly before editing"
            )

    def recovery_inventory_for_record(self, record_id: str) -> str:
        if not self.server.recovery_mode:
            return ""
        with closing(self.campaign(read_only=True)) as campaign:
            row = campaign.execute(
                """SELECT inventory_label FROM reconciliation_records
                   WHERE record_id=?
                     AND campaign_id='MD-RECOVERY-WF-PARAGON'""",
                (record_id,),
            ).fetchone()
        return str(row["inventory_label"]) if row else ""

    def retained_index(self) -> sqlite3.Connection:
        if self.server.retained_index_path is None:
            raise reconciliation.ReconciliationError(
                "no retained restoration index is bound to this workbench"
            )
        connection = sqlite3.connect(
            f"file:{self.server.retained_index_path}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_path(self, path: Path, content_type: str) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'self'",
        )
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_html(self, title: str, body: str, *, active: str = "", status: int = 200) -> None:
        notice = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query
        ).get("notice", [""])[0]
        notice_html = (
            f'<div class="notice notice--safe">{e(notice)}</div>' if notice else ""
        )
        nav = (
            (
                ("queue", "/", "Review queue"),
                ("masters", "/kurt-schwarz/masters", "Master overview"),
                (
                    "documents",
                    "/kurt-schwarz/documents",
                    "Documentation",
                ),
                (
                    "object_census",
                    "/kurt-schwarz/object-census",
                    "Object census",
                ),
                ("multiple_tiff", "/?category=multiple_tiff", "Multiple TIFFs"),
                ("no_master", "/?category=no_master", "No TIFF master"),
                ("metadata", "/?category=metadata", "Metadata conflicts"),
                (
                    "missing_selection",
                    "/?category=missing_selection",
                    "Missing selection",
                ),
                (
                    "wrong_candidates",
                    "/?category=wrong_candidates&state=all",
                    "Wrong candidates",
                ),
                ("decided", "/?state=decided", "Decided"),
            )
            if self.server.kurt_schwarz_mode
            else
            (
                ("queue", "/", "Disputed objects"),
                ("duplicate_inventory", "/?category=duplicate_inventory", "Duplicate inventory"),
                ("fallback_candidates", "/?category=fallback_candidates", "Fallback candidates"),
                ("cross_candidate", "/?category=cross_candidate", "Cross-candidates"),
                (
                    "missing_local",
                    "/?category=missing_local&state=all",
                    "No local candidate yet",
                ),
                ("local_only", "/?category=local_only", "Local-only review"),
            )
            if self.server.wf_sender_mode
            else
            tuple(
                [
                    ("visual_review", "/browse", "Visual review"),
                    ("ready", "/browse?view=ready", "Ready for accession"),
                    ("reconciled", "/browse?view=reconciled", "Reconciled"),
                    ("rejected", "/browse?view=rejected", "Rejected"),
                    ("all", "/browse?view=all", "All records"),
                ]
                + (
                    [("surrogate_swaps", "/surrogate-swaps", "Surrogate swaps")]
                    if self.server.surrogate_swap_source
                    else []
                )
            )
            if self.server.recovery_mode
            else
            (
                ("browse", "/browse", "Browse archive"),
                ("compare", "/browse?state=needs_review", "Needs review"),
                ("admin", "/admin", "Admin"),
            )
            if self.server.editor_mode
            else (
                ("overview", "/", "Overview"),
                ("entities", "/entities", "Entities"),
                ("create", "/create", "Create"),
                ("reconcile", "/reconcile", "Reconcile"),
                ("pdfs", "/pdfs", "PDFs"),
                ("publication", "/publication", "Publication"),
            )
        )
        nav_html = "".join(
            f'<a class="nav__link{" is-active" if key == active else ""}" href="{href}">{e(label)}</a>'
            for key, href, label in nav
        )
        page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{e(title)} · ISS Archive</title>
  <link rel="stylesheet" href="/assets/workbench.css?v={e(WORKBENCH_VERSION)}">
  <script src="/assets/workbench.js?v={e(WORKBENCH_VERSION)}" defer></script>
</head>
<body>
  <header class="site-header">
    <a class="brand" href="/">ISS <span>Archive</span></a>
    <span class="actor">Session: {e(self.server.actor)}</span>
  </header>
  <nav class="nav" aria-label="Workbench">{nav_html}</nav>
  <main class="main">{notice_html}{body}</main>
	  <footer class="footer">Minimal core V2 workbench {e(WORKBENCH_VERSION)} · localhost only · {'Kurt Schwarz decisions and analysis cache remain outside the published unit' if self.server.kurt_schwarz_mode else 'WF-Sender decisions are logged to the reconciliation work database; evidence sources remain read-only' if self.server.wf_sender_mode else 'guarded editor reconciliation pilot' if self.server.editor_mode else 'filesystem generations remain authoritative'}</footer>
</body>
</html>"""
        self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8", status)

    def redirect(self, destination: str) -> None:
        self.send_response(303)
        self.send_header("Location", destination)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def parameters(self) -> dict[str, str]:
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query,
            keep_blank_values=True,
        )
        return {key: values[-1] for key, values in query.items()}

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise reconciliation.ReconciliationError("invalid form size")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise reconciliation.ReconciliationError("unsupported form encoding")
        payload = self.rfile.read(length).decode("utf-8")
        values = urllib.parse.parse_qs(payload, keep_blank_values=True)
        form = {key: items[-1] for key, items in values.items()}
        if not secrets.compare_digest(
            form.get("csrf_token", ""),
            self.server.csrf_token,
        ):
            raise reconciliation.ReconciliationError("invalid workbench form token")
        return form

    def upload_form(self) -> tuple[dict[str, str], Path, str]:
        """Read one bounded pilot TIFF upload into pilot-owned staging."""

        length = int(self.headers.get("Content-Length", "0"))
        maximum = master_candidate.MAX_UPLOAD_BYTES + 1024 * 1024
        if length <= 0 or length > maximum:
            raise reconciliation.ReconciliationError(
                "TIFF upload is empty or exceeds the 192 MB pilot limit"
            )
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise reconciliation.ReconciliationError(
                "master candidate requires a multipart upload"
            )
        body = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n\r\n"
            ).encode("ascii")
            + body
        )
        fields: dict[str, str] = {}
        file_bytes: bytes | None = None
        filename = ""
        for part in message.iter_parts():
            name = str(part.get_param("name", header="content-disposition") or "")
            uploaded_name = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if uploaded_name is not None:
                if name != "master_file" or file_bytes is not None:
                    raise reconciliation.ReconciliationError(
                        "master candidate form must contain exactly one TIFF"
                    )
                filename = uploaded_name
                file_bytes = payload
            elif name:
                fields[name] = payload.decode("utf-8")
        if not secrets.compare_digest(
            fields.get("csrf_token", ""),
            self.server.csrf_token,
        ):
            raise reconciliation.ReconciliationError(
                "invalid workbench form token"
            )
        if file_bytes is None or not filename:
            raise reconciliation.ReconciliationError(
                "choose one TIFF master candidate"
            )
        if len(file_bytes) > master_candidate.MAX_UPLOAD_BYTES:
            raise reconciliation.ReconciliationError(
                "TIFF upload exceeds the 192 MB pilot limit"
            )
        staging = self.root / "work" / "editor-upload-staging"
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / f"{uuid.uuid4().hex}.upload"
        try:
            with temporary.open("xb") as handle:
                handle.write(file_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            del body
            del file_bytes
        return fields, temporary, filename

    def csrf(self) -> str:
        return f'<input type="hidden" name="csrf_token" value="{e(self.server.csrf_token)}">'

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if self.server.kurt_schwarz_mode and path == "/":
                self.page_kurt_schwarz_queue()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/masters"
            ):
                self.page_kurt_schwarz_masters()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object"
            ):
                self.page_kurt_schwarz_object()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/image"
            ):
                self.page_kurt_schwarz_image()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/documents"
            ):
                self.page_kurt_schwarz_documents()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/document"
            ):
                self.page_kurt_schwarz_document()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/document-file"
            ):
                self.page_kurt_schwarz_document_file()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/document-preview"
            ):
                self.page_kurt_schwarz_document_preview()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object-census"
            ):
                self.page_kurt_schwarz_object_census()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object-census-duplicates"
            ):
                self.page_kurt_schwarz_object_census_duplicates()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object-census-duplicate-group"
            ):
                self.page_kurt_schwarz_object_census_duplicate_group()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object-candidate"
            ):
                self.page_kurt_schwarz_object_candidate()
            elif (
                self.server.kurt_schwarz_mode
                and path == "/kurt-schwarz/object-census-image"
            ):
                self.page_kurt_schwarz_object_census_image()
            elif self.server.wf_sender_mode and path == "/":
                self.page_wf_sender_queue()
            elif self.server.wf_sender_mode and path == "/compare":
                self.page_wf_sender_compare()
            elif self.server.wf_sender_mode and path == "/wf-sender/local-file":
                self.page_wf_sender_local_file()
            elif self.server.wf_sender_mode and path == "/wf-sender/md-pdf":
                self.page_wf_sender_md_pdf()
            elif self.server.wf_sender_mode and path == "/wf-sender/md-image":
                self.page_wf_sender_md_image()
            elif self.server.wf_sender_mode and path == "/wf-sender/local-image":
                self.page_wf_sender_local_image()
            elif self.server.recovery_mode and path in ("/", "/browse"):
                self.page_recovery_browse()
            elif self.server.recovery_mode and path == "/compare":
                self.page_recovery_compare()
            elif self.server.recovery_mode and path == "/recovery/image":
                self.page_recovery_image()
            elif self.server.recovery_mode and path == "/recovery/active-image":
                self.page_recovery_active_image()
            elif self.server.recovery_mode and path == "/surrogate-swaps":
                self.page_surrogate_swaps()
            elif self.server.recovery_mode and path == "/surrogate-swap/image":
                self.page_surrogate_swap_image()
            elif self.server.editor_mode and path in ("/", "/browse"):
                self.page_editor_browse()
            elif self.server.editor_mode and path == "/compare":
                self.page_editor_compare()
            elif self.server.editor_mode and path == "/editor/image":
                self.page_editor_image()
            elif self.server.editor_mode and path == "/admin":
                self.page_editor_admin()
            elif path == "/":
                self.page_overview()
            elif path == "/entities":
                self.page_entities()
            elif path == "/entity":
                self.page_entity()
            elif path == "/create":
                self.page_create()
            elif path == "/reconcile":
                self.page_reconcile()
            elif path == "/reconcile/record":
                self.page_reconciliation_record()
            elif path == "/reconcile/image":
                self.page_reconciliation_image()
            elif path == "/pdfs":
                self.page_pdfs()
            elif path == "/pdf":
                self.page_pdf()
            elif path == "/publication":
                self.page_publication()
            elif path == "/assets/workbench.css":
                self.send_bytes(CSS_PATH.read_bytes(), "text/css; charset=utf-8")
            elif path == "/assets/workbench.js":
                self.send_bytes(JS_PATH.read_bytes(), "text/javascript; charset=utf-8")
            else:
                self.send_html(
                    "Not found",
                    '<section class="empty"><h1>Not found</h1></section>',
                    status=404,
                )
        except (
            catalog_core.CoreCatalogError,
            kurt_schwarz_reconciliation.KurtSchwarzReconciliationError,
            reconciliation.ReconciliationError,
            recovery_correction.RecoveryCorrectionError,
            wf_sender_workbench.WfSenderWorkbenchError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            self.send_html(
                "Workbench error",
                f'<section class="notice notice--critical"><h1>Operation refused</h1><p>{e(error)}</p></section>',
                status=400,
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if self.server.kurt_schwarz_mode:
                form = self.form()
                if path == "/kurt-schwarz/analyze":
                    self.action_kurt_schwarz_analyze(form)
                    return
                if path == "/kurt-schwarz/media-decision":
                    self.action_kurt_schwarz_media_decision(form)
                    return
                if path == "/kurt-schwarz/media-decisions":
                    self.action_kurt_schwarz_media_decisions(form)
                    return
                if path == "/kurt-schwarz/metadata-decision":
                    self.action_kurt_schwarz_metadata_decision(form)
                    return
                if path == "/kurt-schwarz/alias-decision":
                    self.action_kurt_schwarz_alias_decision(form)
                    return
                if path == "/kurt-schwarz/candidate-link":
                    self.action_kurt_schwarz_candidate_link(form)
                    return
                if path == "/kurt-schwarz/document-decision":
                    self.action_kurt_schwarz_document_decision(form)
                    return
                if path == "/kurt-schwarz/object-census-decision":
                    self.action_kurt_schwarz_object_census_decision(form)
                    return
                if path == "/kurt-schwarz/object-census-manual-duplicate":
                    self.action_kurt_schwarz_object_census_manual_duplicate(form)
                    return
                if path == "/kurt-schwarz/object-census-similarity-decision":
                    self.action_kurt_schwarz_object_census_similarity_decision(
                        form
                    )
                    return
                if path == "/kurt-schwarz/object-census-similarity-group-decision":
                    self.action_kurt_schwarz_object_census_similarity_group_decision(
                        form
                    )
                    return
                if path == "/kurt-schwarz/object-census-similarity-group-existing-object":
                    self.action_kurt_schwarz_object_census_similarity_group_existing_object(
                        form
                    )
                    return
                raise reconciliation.ReconciliationError(
                    "unknown Kurt Schwarz workbench action"
                )
            if self.server.wf_sender_mode:
                form = self.form()
                if path == "/wf-sender/decision":
                    self.action_wf_sender_decision(form)
                    return
                raise reconciliation.ReconciliationError(
                    "unknown WF-Sender workbench action"
                )
            if self.server.editor_mode and path == "/editor/master/upload":
                self.action_editor_master_upload()
                return
            form = self.form()
            if self.server.editor_mode and path == "/editor/metadata":
                self.action_editor_metadata(form)
            elif self.server.recovery_mode and path == "/recovery/metadata-extra":
                self.action_recovery_metadata_extra(form)
            elif self.server.recovery_mode and path == "/recovery/candidate-review":
                self.action_recovery_candidate_review(form)
            elif self.server.recovery_mode and path == "/surrogate-swap/review":
                self.action_surrogate_swap_review(form)
            elif self.server.recovery_mode and path == "/recovery/visual-confirm":
                self.action_recovery_visual_confirm(form)
            elif self.server.recovery_mode and path == "/recovery/carrier":
                self.action_recovery_carrier(form)
            elif self.server.recovery_mode and path == "/recovery/review-status":
                self.action_recovery_review_status(form)
            elif self.server.recovery_mode and path == "/recovery/correction-status":
                self.action_recovery_correction_status(form)
            elif self.server.recovery_mode and path == "/recovery/corrective-publish":
                self.action_recovery_corrective_publish(form)
            elif self.server.recovery_mode and path == "/recovery/accession-publish":
                self.action_recovery_accession_publish(form)
            elif self.server.editor_mode and path == "/editor/master/pin":
                self.action_editor_master_pin(form)
            elif self.server.editor_mode and path == "/editor/master/approve":
                self.action_editor_master_approve(form)
            elif path == "/core/create-unit":
                self.action_create_unit(form)
            elif path == "/core/create-entity":
                self.action_create_entity(form)
            elif path == "/core/publish":
                self.action_publish(form)
            elif path == "/reconcile/link":
                self.action_link_record(form)
            elif path == "/reconcile/identity":
                self.action_identity(form)
            elif path == "/reconcile/field":
                self.action_field(form)
            else:
                raise reconciliation.ReconciliationError("unknown workbench action")
        except (
            catalog_core.CoreCatalogError,
            kurt_schwarz_reconciliation.KurtSchwarzReconciliationError,
            reconciliation.ReconciliationError,
            tiff_accession.TiffAccessionError,
            recovery_accession.RecoveryAccessionError,
            recovery_correction.RecoveryCorrectionError,
            wf_sender_workbench.WfSenderWorkbenchError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            self.send_html(
                "Operation refused",
                f'<section class="notice notice--critical"><h1>Operation refused</h1><p>{e(error)}</p><p><a class="button button--quiet" href="{e(self.headers.get("Referer", "/"))}">Return</a></p></section>',
                status=400,
            )

    def kurt_schwarz(
        self,
    ) -> kurt_schwarz_reconciliation.KurtSchwarzReview:
        review = self.server.kurt_schwarz_review
        if review is None:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                "Kurt Schwarz reconciliation mode is not active"
            )
        return review

    def page_kurt_schwarz_queue(self) -> None:
        parameters = self.parameters()
        category = parameters.get("category", "")
        state = parameters.get("state", "pending")
        if state not in {"pending", "decided", "aliased", "all"}:
            state = "pending"
        query = parameters.get("q", "").strip().casefold()
        records = self.kurt_schwarz().records()
        filtered = []
        for item in records:
            if state != "all" and item["review_state"] != state:
                continue
            if (
                category == "multiple_tiff"
                and item["selection_state"]
                != "provisional_multiple_tiff_review"
            ):
                continue
            if category == "no_master" and not item["no_tiff_master"]:
                continue
            if category == "metadata" and not item["metadata_pending_count"]:
                continue
            if (
                category == "missing_selection"
                and (
                    item["selected_master_count"]
                    or not item["media_count"]
                    or item["alias_of"]
                )
            ):
                continue
            if (
                category == "wrong_candidates"
                and not item["wrong_candidate_count"]
            ):
                continue
            if category == "decided" and item["review_state"] != "decided":
                continue
            haystack = " ".join(
                (
                    item["record_key"],
                    item["current_inventory"],
                    item["historical_inventory"],
                    item["display_title"],
                    item["alias_of"],
                )
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)
        page_size = 48
        try:
            page = max(1, int(parameters.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages)
        visible = filtered[(page - 1) * page_size : page * page_size]
        summary = self.kurt_schwarz().summary()
        warmup = self.kurt_schwarz().preview_warmup_status()
        failed_previews = {
            item["candidate_id"] for item in warmup.get("failures", [])
        }
        for item in visible:
            item["thumbnail_preview_failed"] = (
                item["thumbnail_preview_candidate_id"] in failed_previews
            )
        cards = "".join(
            f"""<article class="object-card">
<a class="object-card__image" href="{url('/kurt-schwarz/object', record=item['record_key'])}">
{
    f'<img loading="{"eager" if index < 12 else "lazy"}" src="{url("/kurt-schwarz/image", id=item["thumbnail_preview_candidate_id"], v=kurt_schwarz_reconciliation.PREVIEW_CACHE_VERSION)}" alt="{e(item["display_inventory"])}">'
    if item["thumbnail_preview_candidate_id"] and not item["thumbnail_preview_failed"]
    else '<span>Preview unavailable</span>'
}
</a>
<div class="object-card__body">
<p>{badge(item['review_state'])}
{badge(item['selection_state'])}
{' ' + badge('metadata_review') if item['metadata_pending_count'] else ''}
{' ' + badge('no_tiff_master') if item['no_tiff_master'] else ''}
{' ' + badge('missing_selection') if item['media_count'] and not item['selected_master_count'] and not item['alias_of'] else ''}
{' ' + badge('wrong_candidate') if item['wrong_candidate_count'] else ''}
{' ' + badge('same_object_alias') if item['alias_of'] else ''}</p>
<h2><a href="{url('/kurt-schwarz/object', record=item['record_key'])}">{e(item['display_inventory'])}</a></h2>
<p>{e(item['display_title']) or 'Untitled object'}</p>
{f'<p class="technical">Alias of <a href="{url("/kurt-schwarz/object", record=item["alias_of"])}">{e(item["alias_of"])}</a></p>' if item['alias_of'] else ''}
<p class="technical">{e(item['media_count'])} media · {e(item['variant_count'])} alleged variants · {e(item['wrong_candidate_count'])} Wrong · {e(item['metadata_pending_count'])} metadata decisions pending · {e(item['thumbnail_role'].replace('_', ' '))}</p>
</div></article>"""
            for index, item in enumerate(visible)
        )
        base = {
            "category": category,
            "state": state,
            "q": parameters.get("q", ""),
        }
        pagination = (
            '<nav class="pagination" aria-label="Queue pages">'
            + (
                f'<a class="button button--quiet" href="{url("/", **base, page=page - 1)}">Previous</a>'
                if page > 1
                else ""
            )
            + f"<span>Page {page} of {pages}</span>"
            + (
                f'<a class="button button--quiet" href="{url("/", **base, page=page + 1)}">Next</a>'
                if page < pages
                else ""
            )
            + "</nav>"
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Kurt Schwarz reconciliation</p>
<h1>Choose the evidence that survives</h1>
<p>The published unit is read-only evidence. Decisions and technical analysis
are logged under <code>iss-archive-work</code>; no flattening, embedding,
draining or deletion occurs here.</p></section>
<section class="metrics">
<div><strong>{summary['objects']}</strong><span>objects</span></div>
<div><strong>{summary['multiple_tiff_objects']}</strong><span>multiple TIFF review</span></div>
<div><strong>{summary['no_tiff_master_objects']}</strong><span>without TIFF master</span></div>
<div><strong>{summary['metadata_review_objects']}</strong><span>metadata review</span></div>
<div><strong>{summary['aliased_objects']}</strong><span>object aliases</span></div>
</section>
<form class="filter-bar" method="get" action="/">
<label>Search<input type="search" name="q" value="{e(parameters.get('q', ''))}" placeholder="Inventory, title or record key"></label>
<label>State<select name="state">
{option('pending', 'Pending', state)}
{option('decided', 'Decided', state)}
{option('aliased', 'Aliased', state)}
{option('all', 'All', state)}
</select></label>
<input type="hidden" name="category" value="{e(category)}">
<button class="button" type="submit">Filter</button></form>
<p class="technical">Showing {len(visible)} of {len(filtered)} matching objects.</p>
<section class="object-grid">{cards or '<div class="empty">No objects match this queue.</div>'}</section>
{pagination}"""
        self.send_html(
            "Kurt Schwarz reconciliation",
            body,
            active=category or ("decided" if state == "decided" else "queue"),
        )

    def page_kurt_schwarz_masters(self) -> None:
        parameters = self.parameters()
        state = parameters.get("state", "all")
        if state not in {
            "all",
            "missing_md",
            "duplicate_signal",
            "no_master",
        }:
            state = "all"
        query = parameters.get("q", "").strip().casefold()
        records = self.kurt_schwarz().records()
        missing_md = sum(item["md_missing"] for item in records)
        duplicate_md = sum(item["md_duplicate_count"] > 1 for item in records)
        shared_master = sum(
            item["thumbnail_duplicate_count"] > 1 for item in records
        )
        duplicate_groups = len(
            {
                item["thumbnail_content_sha256"]
                for item in records
                if item["thumbnail_duplicate_count"] > 1
            }
        )
        filtered = []
        for item in records:
            duplicate_signal = (
                item["md_duplicate_count"] > 1
                or item["inventory_duplicate_count"] > 1
                or item["thumbnail_duplicate_count"] > 1
            )
            if state == "missing_md" and not item["md_missing"]:
                continue
            if state == "duplicate_signal" and not duplicate_signal:
                continue
            if state == "no_master" and not item["no_tiff_master"]:
                continue
            haystack = " ".join(
                (
                    item["record_key"],
                    item["md_object_id"],
                    item["display_inventory"],
                    item["display_title"],
                )
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)
        per_page = parameters.get("per_page", "all")
        if per_page not in {"120", "360", "all"}:
            per_page = "all"
        page_size = max(1, len(filtered)) if per_page == "all" else int(per_page)
        try:
            page = (
                1
                if per_page == "all"
                else max(1, int(parameters.get("page", "1")))
            )
        except ValueError:
            page = 1
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages)
        visible = filtered[(page - 1) * page_size : page * page_size]
        warmup = self.kurt_schwarz().preview_warmup_status()
        failed_previews = {
            item["candidate_id"] for item in warmup.get("failures", [])
        }
        cards = []
        for index, item in enumerate(visible):
            preview_failed = (
                item["thumbnail_preview_candidate_id"] in failed_previews
            )
            image = (
                f'<img loading="{"eager" if index < 12 else "lazy"}" src="{url("/kurt-schwarz/image", id=item["thumbnail_preview_candidate_id"], v=kurt_schwarz_reconciliation.PREVIEW_CACHE_VERSION)}" alt="{e(item["display_inventory"])}">'
                if item["thumbnail_preview_candidate_id"] and not preview_failed
                else "<span>Preview unavailable</span>"
            )
            signals = "".join(
                (
                    badge("missing_md") if item["md_missing"] else "",
                    badge("duplicate_md_id")
                    if item["md_duplicate_count"] > 1
                    else "",
                    badge("duplicate_inventory")
                    if item["inventory_duplicate_count"] > 1
                    else "",
                    badge("shared_exact_master")
                    if item["thumbnail_duplicate_count"] > 1
                    else "",
                    badge("same_object_alias") if item["alias_of"] else "",
                    badge("no_tiff_master") if item["no_tiff_master"] else "",
                    badge("preview_unavailable") if preview_failed else "",
                )
            )
            cards.append(
                f"""<article class="master-tile">
<a class="master-tile__image" href="{url('/kurt-schwarz/object', record=item['record_key'])}">{image}</a>
<div class="master-tile__body">
<h2><a href="{url('/kurt-schwarz/object', record=item['record_key'])}">{e(item['display_inventory'])}</a></h2>
<p class="master-tile__title">{e(item['display_title']) or 'Untitled object'}</p>
<p>{signals or badge(item['thumbnail_role'])}</p>
<p class="technical">{'MD ' + e(item['md_object_id']) if item['md_object_id'] else e(item['record_key'])}<br>{e(item['thumbnail_role'].replace('_', ' '))}</p>
</div></article>"""
            )
        base = {
            "state": state,
            "q": parameters.get("q", ""),
            "per_page": per_page,
        }
        pagination = (
            '<nav class="pagination" aria-label="Master overview pages">'
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/masters", **base, page=page - 1)}">Previous</a>'
                if page > 1
                else ""
            )
            + f"<span>Page {page} of {pages}</span>"
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/masters", **base, page=page + 1)}">Next</a>'
                if page < pages
                else ""
            )
            + "</nav>"
            if per_page != "all"
            else '<nav class="pagination" aria-label="Master overview pages"><span>Showing all matching masters</span></nav>'
        )
        warmup_notice = ""
        if warmup:
            ready = int(warmup.get("cached_before", 0)) + int(
                warmup.get("generated", 0)
            )
            warmup_notice = (
                f'<section class="notice notice--safe"><strong>Thumbnail cache: '
                f'{ready} of {e(warmup.get("total", len(records)))} ready.</strong> '
                f'{e(warmup.get("failed", 0))} unreadable master(s); '
                f'background state: {e(warmup.get("state", "unknown").replace("_", " "))}.'
                "</section>"
            )
        body = f"""
<section class="page-heading"><p class="eyebrow">Kurt Schwarz visual census</p>
<h1>Master overview</h1>
<p>One representative master per object, with the curator-selected master
taking precedence. Where no master exists, the best available TIFF or
derivative is shown. Use the wall to spot visually repeated objects and
missing museum-digital records; open any tile for full evidence.</p></section>
<section class="metrics">
<div><strong>{len(records)}</strong><span>object representatives</span></div>
<div><strong>{missing_md}</strong><span>without MD record</span></div>
<div><strong>{duplicate_md}</strong><span>duplicate MD IDs</span></div>
<div><strong>{shared_master}</strong><span>objects sharing exact master in {duplicate_groups} groups</span></div>
</section>
{warmup_notice}
<form class="filter-bar" method="get" action="/kurt-schwarz/masters">
<label>Search<input type="search" name="q" value="{e(parameters.get('q', ''))}" placeholder="Inventory, title, MD ID or record key"></label>
<label>Signal<select name="state">
{option('all', 'All representatives', state)}
{option('missing_md', 'Missing museum-digital record', state)}
{option('duplicate_signal', 'Detected duplicate signals', state)}
{option('no_master', 'No TIFF master', state)}
</select></label>
<label>Per page<select name="per_page">
{option('120', '120', per_page)}
{option('360', '360', per_page)}
{option('all', 'All', per_page)}
</select></label>
<button class="button" type="submit">Filter</button></form>
<p class="technical">Showing {len(visible)} of {len(filtered)} matching objects.
Exact digest matches are flagged automatically; visual similarity remains a
curator judgment. Thumbnails load lazily; choose 120, 360, or all matching
masters above.</p>
<section class="master-overview-grid">{''.join(cards) or '<div class="empty">No objects match this view.</div>'}</section>
{pagination}"""
        self.send_html(
            "Kurt Schwarz master overview",
            body,
            active="masters",
        )

    @staticmethod
    def _kurt_embedded_metadata(analysis: dict[str, Any] | None) -> str:
        if not analysis:
            return ""
        try:
            values = json.loads(analysis["embedded_metadata_json"])
        except (json.JSONDecodeError, KeyError, TypeError):
            values = {}
        if not values:
            return "<p class=\"technical\">No selected embedded fields found.</p>"
        rows = "".join(
            f"<dt>{e(key)}</dt><dd>{e(value)}</dd>"
            for key, value in values.items()
        )
        return f'<dl class="metadata-list">{rows}</dl>'

    def _kurt_media_card(
        self,
        record_key: str,
        candidate: dict[str, Any],
        *,
        controls_allowed: bool = True,
    ) -> str:
        decision = candidate.get("decision")
        analysis = candidate.get("analysis")
        current = (
            f"<p>{badge(decision['disposition'])}<br><small>{e(decision['actor'])} · {e(decision['decided_at'])}</small></p>"
            if decision
            else '<p><span class="badge">not reviewed</span></p>'
        )
        comparison = ""
        if analysis:
            difference = (
                f" · changed pixels {e(analysis['pixel_difference_count'])}"
                if analysis["pixel_difference_count"]
                else ""
            )
            comparison = (
                f"<p>{badge(analysis['comparison_state'])}<br>"
                f"<span class=\"technical\">{e(analysis['width_px'])}×"
                f"{e(analysis['height_px'])} · {e(analysis['bit_depth'])}-bit · "
                f"{e(analysis['colorspace'])}{difference}</span></p>"
            )
        occurrences = candidate["source_occurrences"]
        occurrence_rows = "".join(
            f"<li><code>{e(item['source_path'])}</code><br>"
            f"{badge(item['drain_state'])} deletion={e(item['deletion_authorized'])}</li>"
            for item in occurrences
        )
        combine_label = (
            "Current proposed master"
            if candidate["content_role"] == "preservation_master"
            else candidate["content_role"].replace("_", " ")
        )
        link = candidate.get("link")
        link_evidence = (
            f"<p>{badge('linked candidate')}<br>"
            f"<span class=\"technical\">Added by {e(link['actor'])} at "
            f"{e(link['linked_at'])}. Original manifest records: "
            f"{e(link['source_record_keys']) or 'unassigned'}. "
            f"Reason: {e(link['reason'])}</span></p>"
            if link
            else ""
        )
        preview = (
            '<span class="badge badge--preview-unavailable">Preview unavailable</span>'
            if candidate.get("preview_failed")
            else f'<img loading="eager" src="{url("/kurt-schwarz/image", id=candidate["candidate_id"], v=kurt_schwarz_reconciliation.PREVIEW_CACHE_VERSION)}" alt="{e(candidate["filename"])}">'
        )
        decision_controls = (
            f"""<div class="form candidate-disposition">
<fieldset class="disposition-fieldset"><legend>Disposition</legend>
<div class="disposition-toggles">
<label class="disposition-toggle"><input form="kurt-media-review" type="radio" name="disposition__{e(candidate['candidate_id'])}" value="selected_master" data-master-choice{' checked' if decision and decision['disposition'] == 'selected_master' else ''}><span>Master</span></label>
<label class="disposition-toggle"><input form="kurt-media-review" type="radio" name="disposition__{e(candidate['candidate_id'])}" value="redundant"{' checked' if decision and decision['disposition'] == 'redundant' else ''}><span>Redundant</span></label>
<label class="disposition-toggle"><input form="kurt-media-review" type="radio" name="disposition__{e(candidate['candidate_id'])}" value="wrong_object"{' checked' if decision and decision['disposition'] == 'wrong_object' else ''}><span>Wrong</span></label>
<label class="disposition-toggle"><input form="kurt-media-review" type="radio" name="disposition__{e(candidate['candidate_id'])}" value="defer"{' checked' if decision and decision['disposition'] == 'defer' else ''}><span>Decide later</span></label>
</div></fieldset>
<label>Reason<textarea form="kurt-media-review" name="reason__{e(candidate['candidate_id'])}">{e(decision['reason'] if decision else '')}</textarea></label>
</div>"""
            if controls_allowed
            else (
                '<p class="technical">This record is an object alias. Its '
                "representations remain bound to the canonical object; no "
                "repeat media decision is required here.</p>"
            )
        )
        wrong_reassignment = (
            f"""<section class="notice notice--warning">
<h4>Add this Wrong candidate to the correct object</h4>
<p>The Wrong decision remains on this object. This only adds the same
manifested representation to another object's review; it does not select a
Master or move a file.</p>
<form method="post" action="/kurt-schwarz/candidate-link">{self.csrf()}
<input type="hidden" name="final_path" value="{e(candidate['final_path'])}">
<label>Target record key, inventory, or MD object ID
<input name="target_reference" required placeholder="KS-… or md:…"></label>
<label>Reason
<textarea name="reason" required
placeholder="Identity evidence linking this image to the target object"></textarea></label>
<button class="button button--quiet" type="submit">Add to target review</button>
</form></section>"""
            if controls_allowed
            and decision
            and decision["disposition"] == "wrong_object"
            else ""
        )
        return f"""<article class="kurt-media-card" data-candidate-id="{e(candidate['candidate_id'])}">
<div class="kurt-media-card__preview">
{preview}
</div>
<div class="kurt-media-card__body">
<p>{badge(combine_label)}</p>
<h3>{e(candidate['filename'])}</h3>
<p class="technical">{human_size(candidate['size_bytes'])}<br><code>{e(candidate['content_sha256'])}</code><br><code>{e(candidate['final_path'])}</code></p>
{link_evidence}{comparison}{current}
<details><summary>Embedded metadata</summary>{self._kurt_embedded_metadata(analysis)}</details>
<details><summary>{len(occurrences)} source occurrence(s)</summary><ul class="path-list">{occurrence_rows or '<li>No USB occurrence bound.</li>'}</ul></details>
{decision_controls}{wrong_reassignment}</div></article>"""

    def _kurt_metadata_field(
        self,
        record_key: str,
        field: dict[str, Any],
        *,
        controls_allowed: bool = True,
    ) -> str:
        decision = field.get("decision")
        current = (
            f'<div class="notice notice--safe"><strong>Accepted: {e(decision["accepted_value"]) or "Deferred"}</strong><p>{e(decision["action"].replace("_", " "))} · {e(decision["actor"])}</p></div>'
            if decision
            else ""
        )
        selected = decision["action"] if decision else ""
        combine = (
            option("combine", "Explicitly combine MD and local values", selected)
            if field["field_key"]
            in kurt_schwarz_reconciliation.MULTI_VALUE_FIELDS
            else ""
        )
        md_disabled = " disabled" if not field["md_value"] else ""
        local_disabled = " disabled" if not field["local_value"] else ""
        decision_controls = (
            f"""<form class="form" method="post" action="/kurt-schwarz/metadata-decision" data-async-save>{self.csrf()}
<input type="hidden" name="record_key" value="{e(record_key)}">
<input type="hidden" name="field_key" value="{e(field['field_key'])}">
<label>Accepted value<select name="action" required>
<option value="">Choose…</option>
<option value="use_md"{' selected' if selected == 'use_md' else ''}{md_disabled}>Use museum-digital</option>
<option value="use_local"{' selected' if selected == 'use_local' else ''}{local_disabled}>Use local/historical value</option>
{combine}
{option('corrected', 'Use corrected value', selected)}
{option('defer', 'Decide later', selected)}
</select></label>
<label>Corrected value<textarea name="corrected_value">{e(decision['accepted_value'] if decision and decision['action'] == 'corrected' else '')}</textarea></label>
<label>Reason<textarea name="reason">{e(decision['reason'] if decision else '')}</textarea></label>
<button class="button" type="submit">Record metadata decision</button></form>"""
            if controls_allowed
            else (
                '<p class="technical">Canonical metadata is inherited through '
                "the object alias. The alias values remain provenance "
                "evidence; no repeat field decision is required.</p>"
            )
        )
        return f"""<section class="field-decision">
<h3>{e(field['field_label'])} {badge(field['comparison_state'])}</h3>{current}
<div class="comparison-fields table-wrap"><table><thead><tr><th>Source</th><th>Value</th></tr></thead><tbody>
<tr><th scope="row">Current museum-digital</th><td>{e(field['md_value']) or '—'}</td></tr>
<tr><th scope="row">Local/historical record</th><td>{e(field['local_value']) or '—'}</td></tr>
</tbody></table></div>
{decision_controls}
</section>"""

    def page_kurt_schwarz_object(self) -> None:
        parameters = self.parameters()
        record_key = parameters.get("record", "")
        record = self.kurt_schwarz().record(record_key)
        alias_decision = record.get("alias_decision")
        controls_allowed = not bool(alias_decision)
        if alias_decision:
            canonical = record.get("canonical") or {}
            canonical_key = alias_decision["canonical_record_key"]
            canonical_label = (
                canonical.get("display_inventory") or canonical_key
            )
            identity_panel = f"""<section class="notice notice--safe">
<h2>Object alias recorded</h2>
<p><strong>{e(record['current_inventory'] or record['historical_inventory'] or record_key)}</strong>
is the same archival object as
<a href="{url('/kurt-schwarz/object', record=canonical_key)}"><strong>{e(canonical_label)}</strong></a>.
Its inventory, MD object ID, media, metadata, and source occurrences remain
provenance aliases of the canonical object. No repeat media or metadata review
is required on this record.</p>
<p class="technical">Recorded by {e(alias_decision['actor'])} at
{e(alias_decision['decided_at'])}{' · ' + e(alias_decision['reason']) if alias_decision['reason'] else ''}</p>
<form method="post" action="/kurt-schwarz/alias-decision">{self.csrf()}
<input type="hidden" name="alias_record_key" value="{e(record_key)}">
<input type="hidden" name="canonical_reference" value="{e(record_key)}">
<input type="hidden" name="action" value="independent">
<label>Reason for reopening this as an independent object
<textarea name="reason" required></textarea></label>
<button class="button button--quiet" type="submit">Remove alias relationship</button>
</form></section>"""
        else:
            alias_rows = "".join(
                f'<li><a href="{url("/kurt-schwarz/object", record=item["record_key"])}">{e(item["display_inventory"])}</a>'
                f' · MD {e(item["md_object_id"]) if item["md_object_id"] else "—"}</li>'
                for item in record.get("aliases", [])
            )
            existing_aliases = (
                f"<p><strong>Recorded aliases:</strong></p><ul>{alias_rows}</ul>"
                if alias_rows
                else ""
            )
            identity_panel = f"""<section class="panel">
<h2>Object identity</h2>
{existing_aliases}
<p>Use this only when the entire other record describes the same archival
object. The other inventory and MD ID become aliases; its media and source
occurrences remain preserved with this canonical object. This is not a
file-level redundant decision.</p>
<form method="post" action="/kurt-schwarz/alias-decision">{self.csrf()}
<input type="hidden" name="alias_record_key" value="{e(record_key)}">
<input type="hidden" name="action" value="same_object_alias">
<label>Canonical record key, inventory, or MD object ID
<input name="canonical_reference" required
placeholder="KS-… or md:…"></label>
<label>Reason
<textarea name="reason" required
placeholder="Curator-confirmed duplicate in local records and museum-digital"></textarea></label>
<button class="button" type="submit">Make this record an alias</button>
</form></section>"""
        candidate_query = parameters.get("candidate_q", "").strip()
        candidate_results = (
            self.kurt_schwarz().search_representations(
                candidate_query,
                target_record_key=record_key,
            )
            if controls_allowed and len(candidate_query) >= 2
            else []
        )
        candidate_rows = "".join(
            "<tr>"
            f"<td><strong>{e(item['filename'])}</strong><br>"
            f"{badge(item['content_role'])}<br>"
            f"<span class=\"technical\">{human_size(item['size_bytes'])}</span></td>"
            f"<td><code>{e(item['final_path'])}</code><br>"
            f"<span class=\"technical\">Manifest records: "
            f"{e(item['source_record_keys']) or 'unassigned'}<br>"
            f"Inventories: {e(item['inventories']) or '—'}</span></td>"
            + (
                '<td><span class="badge">already bound</span></td>'
                if item["already_bound"]
                else f"""<td><form method="post" action="/kurt-schwarz/candidate-link">{self.csrf()}
<input type="hidden" name="target_record_key" value="{e(record_key)}">
<input type="hidden" name="final_path" value="{e(item['final_path'])}">
<label>Reason<input name="reason" required
placeholder="Why this may belong to the object"></label>
<button class="button button--quiet" type="submit">Add candidate</button>
</form></td>"""
            )
            + "</tr>"
            for item in candidate_results
        )
        search_status = (
            f"<p class=\"technical\">Showing {len(candidate_results)} bounded manifest match(es).</p>"
            if candidate_query
            else ""
        )
        candidate_recovery = (
            f"""<section class="panel">
<h2>Recover or reassign media candidates</h2>
<p>Searches only image representations already manifested in this published
unit. Adding a result records a candidate link in the reconciliation database;
it does not move a file or make it Master. Review the linked image below using
Master, Redundant, Wrong, or Decide later.</p>
<form class="filter-bar" method="get" action="/kurt-schwarz/object">
<input type="hidden" name="record" value="{e(record_key)}">
<label>Filename, inventory, record or path
<input type="search" name="candidate_q" value="{e(candidate_query)}"
placeholder="KS-… or filename" minlength="2" required></label>
<button class="button" type="submit">Find candidates</button>
</form>
{search_status}
{f'<div class="table-wrap"><table><thead><tr><th>Representation</th><th>Manifest binding</th><th>Action</th></tr></thead><tbody>{candidate_rows}</tbody></table></div>' if candidate_results else ''}
</section>"""
            if controls_allowed
            else ""
        )
        warmup = self.kurt_schwarz().preview_warmup_status()
        failed_previews = {
            item["candidate_id"] for item in warmup.get("failures", [])
        }
        for candidate in record["candidates"]:
            candidate["preview_failed"] = (
                candidate["candidate_id"] in failed_previews
            )
        media_cards = "".join(
            self._kurt_media_card(
                record_key,
                candidate,
                controls_allowed=controls_allowed,
            )
            for candidate in record["candidates"]
        )
        media_review_form = (
            f"""<form id="kurt-media-review" method="post"
action="/kurt-schwarz/media-decisions" data-async-save
data-media-batch-save="yes">{self.csrf()}
<input type="hidden" name="record_key" value="{e(record_key)}">
</form>"""
            if controls_allowed and record["candidates"]
            else ""
        )
        media_submit = (
            """<section class="panel kurt-media-submit">
<p>Review the full image set first. Selecting Master makes every other
non-Wrong image Redundant.</p>
<button class="button" type="submit" form="kurt-media-review">Record media decision</button>
</section>"""
            if controls_allowed and record["candidates"]
            else ""
        )
        metadata_differences = [
            field
            for field in record["metadata"]
            if field["comparison_state"] != "same"
        ]
        metadata_issues = (
            [
                field
                for field in metadata_differences
                if not field.get("decision")
            ]
            if controls_allowed
            else []
        )
        resolved_metadata = [
            field for field in metadata_differences if field.get("decision")
        ]
        metadata_cards = "".join(
            self._kurt_metadata_field(
                record_key,
                field,
                controls_allowed=controls_allowed,
            )
            for field in metadata_issues
        )
        resolved_metadata_rows = "".join(
            "<tr>"
            f"<th scope=\"row\">{e(field['field_label'])}</th>"
            f"<td>{e(field['decision']['action'].replace('_', ' '))}</td>"
            f"<td>{e(field['decision']['accepted_value']) or 'No supplied value'}</td>"
            "</tr>"
            for field in resolved_metadata
        )
        resolved_metadata_details = (
            f"""<details class="panel"><summary>{len(resolved_metadata)} resolved metadata difference(s)</summary>
<div class="table-wrap"><table><thead><tr><th>Field</th><th>Decision</th><th>Accepted value</th></tr></thead>
<tbody>{resolved_metadata_rows}</tbody></table></div></details>"""
            if resolved_metadata
            else ""
        )
        analyzed = sum(bool(item.get("analysis")) for item in record["candidates"])
        body = f"""
<p><a href="/">← Review queue</a></p>
<section class="page-heading"><p class="eyebrow">{e(record['record_key'])}</p>
<h1>{e(record['current_inventory'] or record['historical_inventory'])}</h1>
<p>{e(record['display_title']) or 'Untitled object'}</p>
<p>{badge(record['selection_state'])} {len(record['candidates'])} media candidates · {len(metadata_issues)} metadata decisions pending</p>
</section>
{identity_panel}
{candidate_recovery}
<section class="panel kurt-analysis">
<h2>Visual equivalence evidence</h2>
<p>Analysis compares decoded pixels against the selected master, or the current
proposed master when no curator selection exists. It also reads embedded
metadata without changing any file.</p>
<form method="post" action="/kurt-schwarz/analyze">{self.csrf()}
<input type="hidden" name="record_key" value="{e(record_key)}">
<button class="button" type="submit">{'Refresh' if analyzed else 'Analyze'} all candidates for this object</button>
</form><p class="technical">{analyzed} of {len(record['candidates'])} candidates currently analyzed.</p></section>
{media_review_form}
<section class="kurt-media-grid">{media_cards or '<div class="empty">No media candidates are bound to this object.</div>'}</section>
<section class="page-heading kurt-metadata-heading"><p class="eyebrow">Canonical metadata</p>
<h2>Resolve obvious metadata mismatches</h2>
<p>Non-selected values remain provenance evidence. Accepted values are only
embedded later, while building the flattened replacement unit.</p></section>
<section class="metadata-decision-grid">{metadata_cards or '<div class="empty">No metadata decisions remain for this object.</div>'}</section>
{resolved_metadata_details}
{media_submit}
"""
        self.send_html(
            f"Kurt Schwarz {record['current_inventory']}",
            body,
            active="queue",
        )

    def page_kurt_schwarz_object_census(self) -> None:
        parameters = self.parameters()
        state = parameters.get("state", "pending")
        if state not in {"pending", "decided", "all"}:
            state = "pending"
        queue = parameters.get("queue", "")
        if queue not in {
            "",
            "new_local_review",
            "surrogate_master_review",
            "existing_object_reconciled",
            "new_local_companion_reconciled",
        }:
            queue = ""
        query = parameters.get("q", "").strip().casefold()
        candidates = self.kurt_schwarz().object_census_queue_candidates()
        reference_match_counts = (
            self.kurt_schwarz().object_census_reference_match_counts()
        )
        filtered = []
        for item in candidates:
            if state != "all" and item["review_state"] != state:
                continue
            if queue and item["queue_class"] != queue:
                continue
            haystack = " ".join(
                (
                    item["filename"], item["primary_source_path"],
                    item["metadata_summary"], item["metadata_matches_json"],
                )
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)
        page_size = 100
        try:
            page = max(1, int(parameters.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages)
        visible = filtered[(page - 1) * page_size : page * page_size]
        cards = []
        for index, item in enumerate(visible):
            decision = item.get("decision") or {}
            dimensions = (
                f"{item['width_px']} × {item['height_px']} px · "
                if item["width_px"] and item["height_px"] else ""
            )
            cards.append(
                f"""<article class="object-census-card">
<a class="object-census-card__image" href="{url('/kurt-schwarz/object-candidate', id=item['candidate_id'])}">
<img loading="{'eager' if index < 12 else 'lazy'}" src="{url('/kurt-schwarz/object-census-image', id=item['candidate_id'])}" alt="{e(item['filename'])}"></a>
<div class="object-census-card__body">
<p>{badge(item['review_state'])} {badge(item['queue_class'])}
{badge(decision.get('disposition')) if decision else ''}
{badge(f"{reference_match_counts.get(item['candidate_id'], 0)} master match(es)") if reference_match_counts.get(item['candidate_id'], 0) else ''}</p>
<h2><a href="{url('/kurt-schwarz/object-candidate', id=item['candidate_id'])}">{e(item['filename'])}</a></h2>
<p>{e(item['metadata_summary'][:360])}</p>
<p class="technical">{dimensions}{int(item['size_bytes']):,} bytes · {item['source_occurrence_count']} source occurrence(s)<br>{e(item['primary_source_path'])}</p>
</div></article>"""
            )
        summary = self.kurt_schwarz().object_census_queue_summary(candidates)
        queue_counts = summary["object_census_queues"]
        queue_labels = (
            ("", "All census states"),
            ("new_local_review", "New objects outside MD scope"),
            ("surrogate_master_review", "TIFF/PSD versus current surrogate"),
            ("existing_object_reconciled", "Auto-reconciled existing objects"),
            ("new_local_companion_reconciled", "Exact companions of new objects"),
        )
        queue_options = "".join(option(value, label, queue) for value, label in queue_labels)
        base = {"state": state, "queue": queue, "q": parameters.get("q", "")}
        pagination = (
            '<nav class="pagination" aria-label="Object census pages">'
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/object-census", **base, page=page - 1)}">Previous</a>'
                if page > 1 else ""
            )
            + f"<span>Page {page} of {pages}</span>"
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/object-census", **base, page=page + 1)}">Next</a>'
                if page < pages else ""
            )
            + "</nav>"
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Kurt Schwarz full-source object census</p>
<h1>Review unresolved images with metadata and retained-master matches</h1>
<p>Files uniquely matching an existing reconciled Kurt object are removed from the
pending queue automatically. Spreadsheet mentions remain audit evidence; visual
identity controls object reconciliation. Decisions do not copy, move, delete, assign
IDs, or modify the USB and published unit. Existing Redundant files and generated
derivatives are hidden; every published object contributes only its one selected
Master to similarity evidence.</p></section>
<section class="metrics object-census-metrics">
<div><strong>{queue_counts.get('new_local_review', 0)}</strong><span>new outside-MD candidates</span></div>
<div><strong>{queue_counts.get('surrogate_master_review', 0)}</strong><span>Master comparisons</span></div>
<div><strong>{queue_counts.get('existing_object_reconciled', 0)}</strong><span>auto-reconciled existing</span></div>
<div><strong>{queue_counts.get('new_local_companion_reconciled', 0)}</strong><span>exact companions grouped</span></div>
<div><strong>{summary['object_census_pending']}</strong><span>decisions pending</span></div>
</section>
<form class="filter-bar" method="get" action="/kurt-schwarz/object-census">
<label>Search<input type="search" name="q" value="{e(parameters.get('q', ''))}" placeholder="Filename, metadata, spreadsheet or source path"></label>
<label>State<select name="state">
{option('pending', 'Pending', state)}{option('decided', 'Decided', state)}{option('all', 'All', state)}
</select></label>
<label>Queue<select name="queue">{queue_options}</select></label>
<button class="button" type="submit">Filter</button></form>
<p class="technical">Showing {len(visible)} of {len(filtered)} matching candidates in polite pages of {page_size}.</p>
<section class="object-census-grid">{''.join(cards) or '<div class="empty">No candidates match this queue.</div>'}</section>
{pagination}"""
        self.send_html("Kurt Schwarz object census", body, active="object_census")

    def page_kurt_schwarz_object_census_duplicates(self) -> None:
        run = self.kurt_schwarz().object_census_similarity_run()
        if run is None:
            output = (
                self.kurt_schwarz().review_root
                / "object-census-similarity/runs/YYYYMMDDTHHMMSSZ-census-pairs"
            )
            command = (
                "python3 tools/archive-preservation/kurt_schwarz_similarity.py "
                f"--unit-root {self.kurt_schwarz().unit_root} "
                f"--review-root {self.kurt_schwarz().review_root} "
                f"--output {output} --workers 2"
            )
            body = f"""
<section class="page-heading"><p class="eyebrow">Object-census duplicate review</p>
<h1>No similarity evidence run is available</h1>
<p>Generate immutable candidate-to-candidate evidence first. The scanner creates
only replaceable previews and review artifacts; it cannot delete, move, accession,
or assign identifiers.</p></section>
<section class="panel"><pre>{e(command)}</pre></section>"""
            self.send_html(
                "Kurt Schwarz duplicate review",
                body,
                active="object_duplicates",
            )
            return
        parameters = self.parameters()
        state = parameters.get("state", "pending")
        if state not in {"pending", "decided", "superseded", "all"}:
            state = "pending"
        confidence = parameters.get("confidence", "")
        confidence_values = {
            "",
            "exact_visual",
            "high_confidence",
            "likely_review",
            "possible_review",
        }
        if confidence not in confidence_values:
            confidence = ""
        pairs = self.kurt_schwarz().object_census_similarity_pairs()
        visible = [
            item
            for item in pairs
            if (state == "all" or item["review_state"] == state)
            and (not confidence or item["confidence"] == confidence)
        ]
        page_size = 25
        try:
            page = max(1, int(parameters.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, (len(visible) + page_size - 1) // page_size)
        page = min(page, pages)
        page_pairs = visible[(page - 1) * page_size : page * page_size]
        cards = []
        for item in page_pairs:
            left = item["candidate_a"]
            right = item["candidate_b"]
            decision = item.get("decision") or {}
            selected = str(decision.get("outcome", ""))
            if selected == "same_photo":
                primary = str(decision.get("primary_candidate_id", ""))
                selected = (
                    "redundant_b"
                    if primary == left["candidate_id"]
                    else "redundant_a"
                )
            controls = ""
            if item["review_state"] != "superseded":
                controls = f"""
<form class="form duplicate-review__form" method="post" action="/kurt-schwarz/object-census-similarity-decision">
{self.csrf()}
<input type="hidden" name="pair_id" value="{e(item['pair_id'])}">
<input type="hidden" name="run_fingerprint" value="{e(item['run_fingerprint'])}">
<fieldset><legend>Pair decision</legend>
<label class="choice"><input type="radio" name="decision" value="redundant_a"{' checked' if selected == 'redundant_a' else ''} required><span><strong>Mark {e(left['filename'])} Redundant</strong><br>Retain {e(right['filename'])} as Master for this photograph.</span></label>
<label class="choice"><input type="radio" name="decision" value="redundant_b"{' checked' if selected == 'redundant_b' else ''} required><span><strong>Mark {e(right['filename'])} Redundant</strong><br>Retain {e(left['filename'])} as Master for this photograph.</span></label>
<label class="choice"><input type="radio" name="decision" value="different_photo"{' checked' if selected == 'different_photo' else ''} required><span><strong>Different photographs</strong><br>Keep both candidates in the normal census queue.</span></label>
<label class="choice"><input type="radio" name="decision" value="defer"{' checked' if selected == 'defer' else ''} required><span><strong>Decide later</strong></span></label>
</fieldset>
<label>Reason or visual distinction<textarea name="reason">{e(str(decision.get('reason', '')))}</textarea></label>
<button class="button" type="submit">Record pair decision</button>
</form>"""
            else:
                controls = (
                    '<div class="notice notice--yellow"><p>This pair is superseded '
                    "because one candidate was decided elsewhere. Reopen that "
                    "candidate before reviewing this edge.</p></div>"
                )
            cards.append(
                f"""<article class="panel duplicate-review" id="{e(item['pair_id'])}">
<header class="duplicate-review__heading"><div><p class="eyebrow">{e(item['pair_id'])}</p>
<h2>{e(left['filename'])} ↔ {e(right['filename'])}</h2></div>
<p>{badge(item['review_state'])} {badge(item['confidence'])} {badge(str(decision.get('outcome', ''))) if decision else ''}</p></header>
<section class="object-census-detail duplicate-review__images">
<div class="panel object-census-detail__image"><h3><a href="{url('/kurt-schwarz/object-candidate', id=left['candidate_id'])}">{e(left['filename'])}</a></h3>
<img loading="lazy" src="{url('/kurt-schwarz/object-census-image', id=left['candidate_id'])}" alt="{e(left['filename'])}">
<p class="technical">{human_size(int(left['size_bytes']))}<br>{e(left['primary_source_path'])}</p></div>
<div class="panel object-census-detail__image"><h3><a href="{url('/kurt-schwarz/object-candidate', id=right['candidate_id'])}">{e(right['filename'])}</a></h3>
<img loading="lazy" src="{url('/kurt-schwarz/object-census-image', id=right['candidate_id'])}" alt="{e(right['filename'])}">
<p class="technical">{human_size(int(right['size_bytes']))}<br>{e(right['primary_source_path'])}</p></div>
</section>
<details><summary>Similarity evidence</summary><div class="table-wrap"><table><tbody>
<tr><th>SSIM / trimmed</th><td>{float(item['ssim']):.6f} / {float(item['trimmed_ssim']):.6f}</td></tr>
<tr><th>NCC / trimmed</th><td>{float(item['ncc']):.6f} / {float(item['trimmed_ncc']):.6f}</td></tr>
<tr><th>pHash / dHash distance</th><td>{int(item['phash_distance'])} / {int(item['dhash_distance'])}</td></tr>
<tr><th>Mean / gradient distance</th><td>{int(item['mean_hash_distance'])} / {int(item['gradient_hash_distance'])}</td></tr>
<tr><th>Decoded pixels</th><td>{e(str(item['decoded_pixel_state']))}</td></tr>
<tr><th>Discovery group</th><td><a href="{url('/kurt-schwarz/object-census-duplicate-group', id=item['group_id'])}">{e(item['group_id'])}</a></td></tr>
</tbody></table></div><p class="technical">A discovery group is not transitive identity proof. Confirm relationships pairwise or use the explicit Master/Redundant group form.</p></details>
{controls}</article>"""
            )
        counts = Counter(item["review_state"] for item in pairs)
        confidence_counts = Counter(item["confidence"] for item in pairs)
        filters = f"""<form class="filter-bar" method="get" action="/kurt-schwarz/object-census-duplicates">
<label>State<select name="state">{option('pending', 'Pending', state)}{option('decided', 'Decided', state)}{option('superseded', 'Superseded', state)}{option('all', 'All', state)}</select></label>
<label>Confidence<select name="confidence"><option value="">All confidence levels</option>{''.join(option(value, value.replace('_', ' ').title(), confidence) for value in ('exact_visual', 'high_confidence', 'likely_review', 'possible_review'))}</select></label>
<button class="button" type="submit">Filter</button></form>"""
        base = {"state": state, "confidence": confidence}
        pagination = (
            '<nav class="pagination" aria-label="Duplicate review pages">'
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/object-census-duplicates", **base, page=page - 1)}">Previous</a>'
                if page > 1 else ""
            )
            + f"<span>Page {page} of {pages}</span>"
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/object-census-duplicates", **base, page=page + 1)}">Next</a>'
                if page < pages else ""
            )
            + "</nav>"
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Object-census duplicate review</p>
<h1>Confirm photographs, not hash proximity</h1>
<p>The scanner compares pending census candidates to each other using four
perceptual hashes plus SSIM, NCC, trimmed-image comparison, and bounded decoded-pixel
checks. Similarity only creates this review queue. No pair is grouped automatically.</p>
<p><a class="button button--quiet" href="{url('/kurt-schwarz/object-census-duplicate-group', id=pairs[0]['group_id']) if pairs else '#'}">Review the first discovery group as Master / Redundant</a></p>
<p class="technical">Run {e(run['generated_at'])} · {e(run['run_fingerprint'])}<br>{e(run['path'])}</p></section>
<section class="metrics"><div><strong>{len(pairs)}</strong><span>candidate pairs</span></div>
<div><strong>{counts['pending']}</strong><span>pending pair decisions</span></div>
<div><strong>{counts['decided']}</strong><span>decided pairs</span></div>
<div><strong>{confidence_counts['exact_visual'] + confidence_counts['high_confidence']}</strong><span>strong evidence pairs</span></div></section>
<section class="notice notice--yellow"><h2>Archive safety boundary</h2><p>“Same photograph” appends a companion relationship only. It does not delete, move, copy, accession, assign an identifier, or change the published unit. Similar layouts and sequential frames must remain different photographs.</p></section>
{filters}<p class="technical">Showing {len(page_pairs)} of {len(visible)} matching pairs in bounded pages of {page_size}.</p>
<section class="duplicate-review-list">{''.join(cards) or '<div class="empty">No pairs match this view.</div>'}</section>{pagination}"""
        self.send_html(
            "Kurt Schwarz duplicate review",
            body,
            active="object_duplicates",
        )

    def page_kurt_schwarz_object_census_duplicate_group(self) -> None:
        group_id = self.parameters().get("id", "")
        group = next(
            (
                item
                for item in self.kurt_schwarz().object_census_similarity_groups()
                if item["group_id"] == group_id
            ),
            None,
        )
        if group is None:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                f"unknown object-census similarity group: {group_id}"
            )
        roles = group["active_roles"]
        cards = []
        for candidate in group["candidates"]:
            candidate_id = candidate["candidate_id"]
            current_role = roles.get(candidate_id, "pending")
            edges = [
                pair
                for pair in group["pairs"]
                if candidate_id
                in {pair["candidate_a_id"], pair["candidate_b_id"]}
            ]
            strongest = min(
                edges,
                key=lambda pair: (
                    {
                        "exact_visual": 0,
                        "high_confidence": 1,
                        "likely_review": 2,
                        "possible_review": 3,
                    }.get(pair["confidence"], 4),
                    -float(pair.get("ssim", 0)),
                ),
            )
            cards.append(
                f"""<article class="object-census-card duplicate-group-card">
<a class="object-census-card__image" href="{url('/kurt-schwarz/object-candidate', id=candidate_id)}"><img loading="lazy" src="{url('/kurt-schwarz/object-census-image', id=candidate_id)}" alt="{e(candidate['filename'])}"></a>
<div class="object-census-card__body"><h2>{e(candidate['filename'])}</h2>
<p>{badge(candidate['review_state'])} {badge(current_role) if current_role != 'pending' else ''}</p>
<p class="technical">Strongest edge: {e(strongest['confidence'].replace('_', ' '))}, SSIM {float(strongest.get('ssim', 0)):.6f}<br>{e(candidate['primary_source_path'])}</p>
<label>Group role<select name="role__{e(candidate_id)}">
{option('pending', 'Keep separate / pending', current_role)}
{option('master', 'Master (retain)', current_role)}
{option('redundant', 'Redundant (companion)', current_role)}
</select></label></div></article>"""
            )
        active_master_count = sum(
            role == "master" for role in roles.values()
        )
        active_redundant_count = sum(
            role == "redundant" for role in roles.values()
        )
        existing_object_assignment = (
            f"""<section class="panel"><h2>Move this confirmed set beneath another object</h2>
<p>Use this when the whole Master/Redundant set is another representation set
of an already published Kurt object. Its selected published Master remains in
place; every file in this set is recorded beneath that object for the final
one-Master review.</p>
<form class="form" method="post" action="/kurt-schwarz/object-census-similarity-group-existing-object">
{self.csrf()}<input type="hidden" name="group_id" value="{e(group_id)}">
<input type="hidden" name="run_fingerprint" value="{e(group['run_fingerprint'])}">
<label>Existing object reference (inventory, MD ID, or record key)
<input name="target_reference" required></label>
<label>Visual identity reason<textarea name="reason" required></textarea></label>
<button class="button button--quiet" type="submit">Move Master and Redundant copies to this object</button>
</form></section>"""
            if active_master_count == 1 and active_redundant_count >= 1
            else ""
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Object-census similarity group</p>
<h1>Choose one Master and its Redundant representations</h1>
<p>This is an explicit curator batch, not automatic transitive grouping. Select
exactly one Master and one or more Redundant files. Files left pending stay
separate; an earlier pair relationship omitted from this form is reopened.</p>
<p class="technical">{e(group_id)} · {len(group['candidates'])} candidates · {len(group['pairs'])} evidence pairs</p></section>
<section class="notice notice--yellow"><h2>Archive safety boundary</h2><p>Master means the retained candidate within this duplicate group. Redundant files remain preserved byte states. This action does not assign an archive identifier, accession, delete, move, copy, or change the published unit.</p></section>
<form class="form" method="post" action="/kurt-schwarz/object-census-similarity-group-decision">
{self.csrf()}<input type="hidden" name="group_id" value="{e(group_id)}">
<input type="hidden" name="run_fingerprint" value="{e(group['run_fingerprint'])}">
<section class="object-census-grid duplicate-group-grid">{''.join(cards)}</section>
<label>Reason<textarea name="reason" placeholder="Why these files are the same photograph"></textarea></label>
<button class="button" type="submit">Record Master and selected Redundant files</button>
</form>
{existing_object_assignment}
<p><a class="button button--quiet" href="/kurt-schwarz/object-census">Back to object census</a></p>"""
        self.send_html(
            "Kurt Schwarz similarity group",
            body,
            active="object_duplicates",
        )

    def page_kurt_schwarz_object_candidate(self) -> None:
        candidate_id = self.parameters().get("id", "")
        item = self.kurt_schwarz().object_census_candidate(candidate_id)
        decision = item.get("decision") or {}
        selected_metadata = dict(item.get("selected_metadata") or {})
        selected_metadata_basis = item.get("selected_metadata_basis", "")
        current = decision.get("disposition", "")
        queue_class = item["queue_class"]
        curated_fields = {
            "title": "curated_title",
            "date_text": "curated_date_text",
            "description": "curated_description",
            "tags": "curated_tags",
        }
        has_curated_decision = queue_class == "new_local_review" and any(
            decision.get(source) is not None
            for source in curated_fields.values()
        )
        if has_curated_decision:
            selected_metadata.update(
                {
                    target: decision.get(source, "")
                    for target, source in curated_fields.items()
                    if decision.get(source) is not None
                }
            )
            selected_metadata_basis = "latest curator decision"
        candidate_pairs = [
            pair
            for pair in self.kurt_schwarz().object_census_similarity_pairs()
            if (
                pair["review_state"] == "pending"
                or (pair.get("decision") or {}).get("outcome") == "same_photo"
            )
            if candidate_id
            in {pair["candidate_a_id"], pair["candidate_b_id"]}
        ]
        confirmed_group_master = (
            object_census_candidate_is_confirmed_group_master(
                candidate_id, candidate_pairs
            )
        )
        confirmed_group_pairs = [
            pair
            for pair in candidate_pairs
            if (pair.get("decision") or {}).get("outcome") == "same_photo"
            and str(
                (pair.get("decision") or {}).get("primary_candidate_id", "")
            )
            == candidate_id
        ]
        master_matches = (
            []
            if confirmed_group_master
            else self.kurt_schwarz().object_census_reference_matches(candidate_id)
        )
        selected_facts = []
        for key, label in (
            ("inventory", "Inventory"),
            ("title", "Title"),
            ("description", "Description"),
            ("tags", "Tags"),
            ("date_text", "Date"),
            ("places", "Places"),
            ("material", "Material / technique"),
            ("dimensions", "Dimensions"),
            ("scan_signature", "Scan / image reference"),
            ("bag_label", "Bag label / remarks"),
        ):
            if (
                selected_metadata.get(key)
                and not (
                    key in {"title", "description", "tags"}
                    and queue_class == "new_local_review"
                    and not has_curated_decision
                )
            ):
                selected_facts.append(
                    f"<div><dt>{e(label)}</dt><dd>{e(selected_metadata[key])}</dd></div>"
                )
        source_paths = "".join(f"<li><code>{e(path)}</code></li>" for path in item["source_paths"])
        if queue_class == "new_local_review":
            choices = (
                (
                    "new_local_metadata_right",
                    "Accept date and edited title/description/tags — new local object",
                ),
                (
                    "new_local_metadata_wrong",
                    "Selected date or group context is wrong — keep unidentified for correction",
                ),
                (
                    "cross_unit_hold",
                    "Belongs to another or future archive unit",
                ),
                ("context_not_object", "Context or supporting image, not a distinct object"),
                ("defer", "Decide later"),
            )
            if confirmed_group_master:
                legend = "New local object metadata"
            else:
                choices = (
                    (
                        "existing_object",
                        "Redundant copy of an existing Kurt object — retain its selected Master",
                    ),
                    *choices,
                )
                legend = "Selected metadata"
        elif queue_class == "surrogate_master_review":
            choices = (
                ("surrogate_master", "Same object — use TIFF/PSD as Master"),
                ("surrogate_keep", "Same object — keep current surrogate"),
                ("wrong_match", "Wrong object match"),
                ("defer", "Decide later"),
            )
            legend = "Master comparison"
        elif queue_class == "existing_object_reconciled":
            choices = (
                ("wrong_match", "Correct this automatic object match"),
                ("context_not_object", "Context or supporting image, not a distinct object"),
                ("defer", "Reopen for later review"),
            )
            legend = "Correct the automatic reconciliation only if needed"
        elif queue_class == "new_local_companion_reconciled":
            choices = (
                ("wrong_match", "Not the same image — return to new-object review"),
                ("defer", "Reopen for later review"),
            )
            legend = "Correct the exact companion grouping only if needed"
        else:
            choices = (
                ("new_local_object", "New local archive object"),
                ("existing_object", "Existing Kurt object / additional representation"),
                ("context_not_object", "Context or supporting image, not a distinct object"),
                ("defer", "Decide later"),
            )
            legend = "Object relationship"
        radios = "".join(
            f'<label class="choice"><input type="radio" name="disposition" value="{value}"{" checked" if value == current else ""} required><span><strong>{e(label)}</strong></span></label>'
            for value, label in choices
        )
        comparison = ""
        if queue_class == "surrogate_master_review":
            comparison = f"""<section class="object-census-detail">
<div class="panel object-census-detail__image"><h2>Preservation candidate</h2><img src="{url('/kurt-schwarz/object-census-image', id=candidate_id)}" alt="{e(item['filename'])}"></div>
<div class="panel object-census-detail__image"><h2>Current surrogate · {e(item['target_inventory'])}</h2><img src="{url('/kurt-schwarz/image', id=item['target_media_candidate_id'], v=3)}" alt="Current surrogate for {e(item['target_inventory'])}"></div>
</section>"""
        target_input = (
            f'<label>Existing object reference (inventory, MD ID, or record key)<input name="target_reference" value="{e(decision.get("target_record_key", ""))}"><small>Required only when marking this as a redundant copy of an existing object. You may enter another object group here even when it was not suggested by the scanner.</small></label>'
            if not confirmed_group_master
            and queue_class not in {
                "surrogate_master_review",
                "existing_object_reconciled",
                "new_local_companion_reconciled",
            }
            else ""
        )
        dimensions = (
            f"{item['width_px']} × {item['height_px']} px"
            if item["width_px"] and item["height_px"] else "Not pre-read; preview is derived on demand"
        )
        hero_image = (
            ""
            if queue_class == "surrogate_master_review"
            else f'<div class="panel object-census-detail__image"><img src="{url("/kurt-schwarz/object-census-image", id=candidate_id)}" alt="{e(item["filename"])}"></div>'
        )
        selected_metadata_html = (
            f'<dl class="facts">{"".join(selected_facts)}</dl>'
            if selected_facts
            else '<div class="empty">No reliable metadata is selected. Keep the image unidentified or defer it rather than inheriting a conflicting MD record.</div>'
        )
        metadata_editor = ""
        cross_unit_editor = ""
        if queue_class == "new_local_review":
            curated_title = decision.get("curated_title")
            if curated_title is None:
                curated_title = selected_metadata.get("title", "")
            curated_date_text = decision.get("curated_date_text")
            if curated_date_text is None:
                curated_date_text = selected_metadata.get("date_text", "")
            curated_description = decision.get("curated_description")
            if curated_description is None:
                curated_description = selected_metadata.get("description", "")
            curated_tags = decision.get("curated_tags")
            if curated_tags is None:
                curated_tags = selected_metadata.get("tags", "")
            metadata_editor = f"""
<label>Object title (editable)
<input name="curated_title" value="{e(curated_title)}" maxlength="1000" placeholder="Title for this individual object only">
<small>The finding-aid title may cover several images. Split and rewrite it for this frame.</small>
</label>
<label>Object date (editable)
<input name="curated_date_text" value="{e(curated_date_text)}" maxlength="250" placeholder="Date or cautious date range for this object">
<small>Use a cautious formulation when the date is approximate.</small>
</label>
<label>Object description (editable)
<textarea name="curated_description" rows="6" maxlength="10000" placeholder="Describe only what belongs to this object.">{e(curated_description)}</textarea>
<small>The physical inventory may describe several images in one group. Keep only what belongs to this frame; the selected date above remains unchanged.</small>
</label>
<label>Keywords / tags (editable)
<input name="curated_tags" value="{e(curated_tags)}" maxlength="5000" placeholder="Keyword; Keyword; Place; Subject">
<small>Separate terms with semicolons. Repeated terms are removed when the decision is saved.</small>
</label>"""
            try:
                hold = json.loads(decision.get("cross_unit_hold_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                hold = {}
            cross_unit_editor = f"""
<details class="panel panel--nested">
<summary><strong>Cross-unit hold details</strong></summary>
<p>Required only for “Belongs to another or future archive unit”. This records
a reversible holding decision; it does not create the target unit, assign an
identifier, copy bytes, or authorize deletion.</p>
<label>Target scope
<input name="cross_unit_target_scope" value="{e(hold.get('target_scope', ''))}" maxlength="80" placeholder="wf">
</label>
<label>Target subunit
<input name="cross_unit_target_subunit" value="{e(hold.get('target_subunit', ''))}" maxlength="120" placeholder="unresolved">
</label>
<label>Provisional group key
<input name="cross_unit_group_key" value="{e(hold.get('group_key', ''))}" maxlength="160" placeholder="wf-brigadebuch-fototron-img-20220701">
</label>
<label>Provisional compound-object title
<input name="cross_unit_provisional_title" value="{e(hold.get('provisional_title', ''))}" maxlength="1000">
</label>
<label>Provisional date
<input name="cross_unit_provisional_date_text" value="{e(hold.get('provisional_date_text', ''))}" maxlength="250">
</label>
<label>Object type
<input name="cross_unit_object_type" value="{e(hold.get('object_type', ''))}" maxlength="80" placeholder="compound_volume">
</label>
<label>Component sequence
<input name="cross_unit_component_sequence" type="number" min="1" value="{e(hold.get('component_sequence', ''))}">
</label>
<label>Component relationship
<select name="cross_unit_component_relationship">
{option('', 'Select a relationship', str(hold.get('component_relationship', '')))}
{option('ordered_scan_component', 'Ordered scan component', str(hold.get('component_relationship', '')))}
{option('supporting_variant', 'Supporting scan variant', str(hold.get('component_relationship', '')))}
{option('capture_instruction_note', 'Capture instruction note', str(hold.get('component_relationship', '')))}
{option('unresolved_component', 'Unresolved component relationship', str(hold.get('component_relationship', '')))}
</select></label>
</details>"""
        related_html = (
            f'<div><dt>Primary new-object candidate</dt><dd><a href="{url("/kurt-schwarz/object-candidate", id=item["related_candidate_id"])}"><code>{e(item["related_candidate_id"])}</code></a></dd></div>'
            if item.get("related_candidate_id") else ""
        )
        master_match_cards = []
        for match in master_matches[:6]:
            reference = match["reference"]
            reason = (
                "Whole-unit XXH3/Czkawka-style visual review: "
                f"{match['confidence']}; pHash={match['phash_distance']}; "
                f"dHash={match['dhash_distance']}; "
                f"SSIM={float(match['ssim']):.6f}. Curator confirmed identity."
            )
            action = (
                f"""<form method="post" action="/kurt-schwarz/object-census-decision">
{self.csrf()}<input type="hidden" name="candidate_id" value="{e(candidate_id)}">
<input type="hidden" name="disposition" value="existing_object">
<input type="hidden" name="target_reference" value="{e(match['reference_record_key'])}">
<input type="hidden" name="reason" value="{e(reason)}">
<button class="button button--quiet" type="submit">Mark this file Redundant under this object</button></form>"""
                if queue_class == "new_local_review"
                and match["confidence"]
                in {"exact_visual", "high_confidence", "likely_review"}
                else ""
            )
            master_match_cards.append(
                f"""<article class="panel object-census-detail__image">
<h3>{e(match['reference_inventory'])}</h3>
<img loading="lazy" src="{url('/kurt-schwarz/image', id=match['reference_candidate_id'], v=3)}" alt="Selected Master for {e(match['reference_inventory'])}">
<p><strong>{e(match['reference_title'])}</strong></p>
<p>{badge(match['confidence'])} {badge('selected Master')}</p>
<p class="technical">{e(match['reference_filename'])}<br>
pHash {int(match['phash_distance'])} · dHash {int(match['dhash_distance'])} ·
SSIM {float(match['ssim']):.6f}</p>{action}</article>"""
            )
        master_matches_html = (
            f"""<section class="page-heading"><p class="eyebrow">Whole-unit comparison</p>
<h2>Possible retained Master matches</h2>
<p>These are strict discovery candidates from the 1,668 canonical selected published Masters.
Confirm identity visually; hash proximity never makes the decision.</p></section>
<section class="object-census-detail">{''.join(master_match_cards)}</section>"""
            if master_match_cards
            else ""
        )
        group_links = []
        seen_groups = set()
        for pair in candidate_pairs:
            if pair["group_id"] in seen_groups:
                continue
            seen_groups.add(pair["group_id"])
            group_links.append(
                f'<li><a href="{url("/kurt-schwarz/object-census-duplicate-group", id=pair["group_id"])}">Review candidate group {e(pair["group_id"])}</a> · {e(pair["confidence"].replace("_", " "))}</li>'
            )
        if confirmed_group_master:
            confirmed_group_ids = sorted(
                {str(pair["group_id"]) for pair in confirmed_group_pairs}
            )
            confirmed_redundant_count = len(
                {
                    str((pair.get("decision") or {})["companion_candidate_id"])
                    for pair in confirmed_group_pairs
                }
            )
            confirmed_links = "".join(
                f'<li><a href="{url("/kurt-schwarz/object-census-duplicate-group", id=group_id)}">View or change group {e(group_id)}</a></li>'
                for group_id in confirmed_group_ids
            )
            candidate_groups_html = f"""<section class="panel">
<h2>Confirmed Master / Redundant group</h2>
<p>This file is the selected Master with {confirmed_redundant_count} Redundant
companion files. No further Master or copy decision is required.</p>
<ul>{confirmed_links}</ul>
<p class="technical">Use the group page only to change this decision or to attach
the entire confirmed set to a specific existing object.</p></section>"""
        else:
            candidate_groups_html = (
                f'<section class="panel"><h2>Possible new-object copies</h2><ul>{"".join(group_links)}</ul><p class="technical">A group can retain one candidate Master and mark selected copies Redundant. Unselected images remain separate.</p></section>'
                if group_links
                else ""
            )
        duplicate_editor = (
            f"""<details class="panel">
<summary><strong>Mark this file as a Redundant duplicate</strong></summary>
<p>You may name the retained census candidate that is the Master for the same
photograph, or leave it blank when that Master is not yet identified. This removes
only this Redundant candidate from the pending census. Its source
occurrences and drain-list evidence remain preserved; no file is deleted, moved,
copied, or removed from source disposition.</p>
<form class="form" method="post" action="/kurt-schwarz/object-census-manual-duplicate">
{self.csrf()}<input type="hidden" name="companion_candidate_id" value="{e(candidate_id)}">
<label>Retained candidate ID (optional)
<input name="retained_candidate_id" pattern="KSC-[A-Za-z0-9-]+" placeholder="KSC-…">
<small>Copy the Census candidate ID from the candidate that should remain as Master.</small>
</label>
<label>Duplicate identification reason (optional)
<textarea name="reason" rows="3" placeholder="Why these files depict the same photograph"></textarea>
</label>
<button class="button button--quiet" type="submit">Mark this file Redundant and keep drain evidence</button>
</form></details>"""
            if queue_class == "new_local_review"
            and not confirmed_group_master
            else ""
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Object census candidate</p>
<h1>{e(item['filename'])}</h1><p>{badge(item['review_state'])} {badge(item['queue_class'])} {badge('group_master') if confirmed_group_master else ''} {badge(current) if current else ''}</p></section>
{comparison}
<section class="object-census-detail">
{hero_image}
<div class="panel"><dl class="facts">
<div><dt>Census candidate</dt><dd><code>{e(candidate_id)}</code></dd></div>
<div><dt>Review class</dt><dd>{e(item['queue_class'].replace('_', ' '))}</dd></div>
{f'<div><dt>Existing Kurt object</dt><dd>{e(item["target_inventory"])} · <code>{e(item["target_record_key"])}</code></dd></div>' if item.get('target_record_key') else ''}
{related_html}
<div><dt>Dimensions</dt><dd>{e(dimensions)}</dd></div>
<div><dt>File size</dt><dd>{int(item['size_bytes']):,} bytes</dd></div>
<div><dt>Exact-byte digest</dt><dd><code>{e(item['content_sha256'])}</code></dd></div>
<div><dt>Preserved evidence</dt><dd><code>{e(item['final_path'])}</code></dd></div>
<div><dt>Review basis</dt><dd>{e(item['review_basis'])}</dd></div>
</dl></div></section>
<section class="panel"><h2>Historical source occurrences</h2><ul class="path-list">{source_paths}</ul></section>
{master_matches_html}
{candidate_groups_html}
<section class="page-heading"><p class="eyebrow">Selected interpretation</p><h2>Metadata proposed for this object</h2>
<p>Basis: {e(selected_metadata_basis)}. Conflicting raw spreadsheet rows remain preserved as audit evidence but are not presented as equal candidates.</p></section>
<section class="panel">{selected_metadata_html}</section>
<form class="panel form" method="post" action="/kurt-schwarz/object-census-decision">
{self.csrf()}<input type="hidden" name="candidate_id" value="{e(candidate_id)}">
<fieldset><legend>{e(legend)}</legend><div class="choice-grid">{radios}</div></fieldset>
{target_input}
{metadata_editor}
{cross_unit_editor}
<label>Reason or identification note<textarea name="reason" rows="4">{e(decision.get('reason', ''))}</textarea></label>
<button class="button" type="submit">Record census decision</button></form>
{duplicate_editor}"""
        self.send_html(
            f"Kurt Schwarz census {item['filename']}", body,
            active="object_census",
        )

    def page_kurt_schwarz_object_census_image(self) -> None:
        candidate_id = self.parameters().get("id", "")
        path = self.kurt_schwarz().object_census_preview(candidate_id)
        self.send_path(path, "image/jpeg")

    def page_kurt_schwarz_documents(self) -> None:
        parameters = self.parameters()
        state = parameters.get("state", "pending")
        if state not in {"pending", "decided", "all"}:
            state = "pending"
        family = parameters.get("family", "")
        query = parameters.get("q", "").strip().casefold()
        documents = self.kurt_schwarz().documents()
        families = sorted({item["content_family"] for item in documents})
        filtered = []
        for item in documents:
            if state != "all" and item["review_state"] != state:
                continue
            if family and item["content_family"] != family:
                continue
            haystack = " ".join(
                (
                    item["filename"],
                    item["final_path"],
                    item["content_family"],
                    item["text_evidence"],
                    item["review_basis"],
                )
            ).casefold()
            if query and query not in haystack:
                continue
            filtered.append(item)
        page_size = 100
        try:
            page = max(1, int(parameters.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, pages)
        visible = filtered[(page - 1) * page_size : page * page_size]
        cards = []
        for item in visible:
            decision = item.get("decision") or {}
            cards.append(
                f"""<article class="document-card">
<div class="document-card__body">
<p>{badge(item['review_state'])} {badge(item['content_family'])}
{badge(decision.get('disposition')) if decision else ''}
{badge('restricted') if item['access_restriction'] else ''}</p>
<h2><a href="{url('/kurt-schwarz/document', id=item['document_id'])}">{e(item['filename'])}</a></h2>
<p>{e(item['text_evidence'][:320]) or e(item['review_basis'])}</p>
<p class="technical">{e(item['extension'] or 'no extension')} · {int(item['size_bytes']):,} bytes<br>{e(item['final_path'])}</p>
</div></article>"""
            )
        base = {
            "state": state,
            "family": family,
            "q": parameters.get("q", ""),
        }
        pagination = (
            '<nav class="pagination" aria-label="Documentation pages">'
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/documents", **base, page=page - 1)}">Previous</a>'
                if page > 1
                else ""
            )
            + f"<span>Page {page} of {pages}</span>"
            + (
                f'<a class="button button--quiet" href="{url("/kurt-schwarz/documents", **base, page=page + 1)}">Next</a>'
                if page < pages
                else ""
            )
            + "</nav>"
        )
        summary = self.kurt_schwarz().document_queue_summary(documents)
        family_options = option("", "All document families", family) + "".join(
            option(value, value.replace("_", " "), family) for value in families
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Kurt Schwarz documentation reconciliation</p>
<h1>Review retained documents</h1>
<p>The final object catalog and historical-drafts ZIP are already staged in
<code>iss-archive-work</code>. This queue records only relevance and ownership
decisions; it does not move, rewrite, or delete documents.</p></section>
<section class="metrics">
<div><strong>{summary['document_candidates']}</strong><span>reviewed source documents</span></div>
<div><strong>{summary['document_review_pending']}</strong><span>visual decisions pending</span></div>
<div><strong>{summary['document_decisions']}</strong><span>append-only decisions</span></div>
</section>
<form class="filter-bar" method="get" action="/kurt-schwarz/documents">
<label>Search<input type="search" name="q" value="{e(parameters.get('q', ''))}" placeholder="Filename, path or extracted text"></label>
<label>State<select name="state">
{option('pending', 'Pending', state)}
{option('decided', 'Decided', state)}
{option('all', 'All', state)}
</select></label>
<label>Family<select name="family">{family_options}</select></label>
<button class="button" type="submit">Filter</button></form>
<p class="technical">Showing {len(visible)} of {len(filtered)} matching documents in bounded pages of {page_size}.</p>
<section class="document-grid">{''.join(cards) or '<div class="empty">No documents match this queue.</div>'}</section>
{pagination}"""
        self.send_html(
            "Kurt Schwarz documentation",
            body,
            active="documents",
        )

    def page_kurt_schwarz_document(self) -> None:
        document_id = self.parameters().get("id", "")
        item = self.kurt_schwarz().document(document_id)
        decision = item.get("decision") or {}
        file_url = url("/kurt-schwarz/document-file", id=document_id)
        preview_url = url("/kurt-schwarz/document-preview", id=document_id)
        if item["extension"] == ".pdf":
            preview = f'<iframe class="document-preview" src="{preview_url}" title="{e(item["filename"])}"></iframe>'
        elif item["extension"] == ".mp3":
            preview = f'<audio class="document-audio" controls preload="metadata" src="{preview_url}"></audio>'
        else:
            preview = f'<iframe class="document-preview" src="{preview_url}" title="{e(item["filename"])}"></iframe>'
        choices = (
            ("keep_direct", "Keep — direct project document"),
            ("keep_contextual", "Keep — contextual evidence"),
            ("cross_unit_reference", "Cross-unit reference only"),
            ("historical_package", "Historical draft/package only"),
            ("exclude", "Exclude from Kurt documentation"),
            ("defer", "Decide later"),
        )
        current = decision.get("disposition", "")
        radios = "".join(
            f'<label class="choice"><input type="radio" name="disposition" value="{value}"{" checked" if value == current else ""} required><span><strong>{e(label)}</strong></span></label>'
            for value, label in choices
        )
        restricted = (
            '<section class="notice notice--critical"><strong>Restricted project record.</strong> Review locally and do not expose publicly.</section>'
            if item["access_restriction"]
            else ""
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Documentation candidate</p>
<h1>{e(item['filename'])}</h1>
<p>{badge(item['review_state'])} {badge(item['content_family'])}
{badge(current) if current else ''}</p></section>
{restricted}
<section class="panel">
<dl class="facts">
<div><dt>Published evidence path</dt><dd><code>{e(item['final_path'])}</code></dd></div>
<div><dt>Original source</dt><dd><code>{e(item['source_path'])}</code></dd></div>
<div><dt>Stored digest</dt><dd><code>{e(item['content_sha256'])}</code></dd></div>
<div><dt>Proposed relation</dt><dd>{e(item['proposed_relationship'])}</dd></div>
<div><dt>Automated review basis</dt><dd>{e(item['review_basis'])}</dd></div>
</dl></section>
<section class="panel"><h2>Document preview</h2>{preview}<p><a class="button button--quiet" target="_blank" rel="noopener" href="{file_url}">Open original document</a></p></section>
<section class="panel"><h2>Extracted evidence</h2><p>{e(item['text_evidence']) or 'No extractable text; use the original document preview.'}</p></section>
<form class="panel form" method="post" action="/kurt-schwarz/document-decision">
{self.csrf()}
<input type="hidden" name="document_id" value="{e(document_id)}">
<fieldset><legend>Relationship and retention decision</legend><div class="choice-grid">{radios}</div></fieldset>
<label>Reason or correction note<textarea name="reason" rows="3">{e(decision.get('reason', ''))}</textarea></label>
<button class="button" type="submit">Record document decision</button>
</form>"""
        self.send_html(
            f"Kurt Schwarz document {item['filename']}",
            body,
            active="documents",
        )

    def page_kurt_schwarz_document_file(self) -> None:
        document_id = self.parameters().get("id", "")
        path = self.kurt_schwarz().document_path(document_id)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_path(path, content_type)

    def page_kurt_schwarz_document_preview(self) -> None:
        document_id = self.parameters().get("id", "")
        path, content_type = self.kurt_schwarz().document_preview(document_id)
        self.send_path(path, content_type)

    def page_kurt_schwarz_image(self) -> None:
        candidate_id = self.parameters().get("id", "")
        path = self.kurt_schwarz().preview(candidate_id)
        self.send_path(path, "image/jpeg")

    def action_kurt_schwarz_analyze(self, form: dict[str, str]) -> None:
        record_key = form.get("record_key", "")
        result = self.kurt_schwarz().analyze(record_key)
        self.redirect(
            url(
                "/kurt-schwarz/object",
                record=record_key,
                notice=(
                    f"Analyzed {result['analyzed_candidates']} candidates; "
                    f"states: {result['comparison_states']}."
                ),
            )
        )

    def action_kurt_schwarz_media_decision(
        self, form: dict[str, str]
    ) -> None:
        record_key = form.get("record_key", "")
        result = self.kurt_schwarz().save_media_decision(
            record_key=record_key,
            candidate_id=form.get("candidate_id", ""),
            disposition=form.get("disposition", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        if self.headers.get("X-Workbench-Async") == "1":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.redirect(
            url(
                "/kurt-schwarz/object",
                record=record_key,
                notice=(
                    f"Recorded {result['disposition']} for "
                    f"{result['candidate_id']}."
                ),
            )
        )

    def action_kurt_schwarz_media_decisions(
        self, form: dict[str, str]
    ) -> None:
        record_key = form.get("record_key", "")
        prefix = "disposition__"
        decisions = [
            {
                "candidate_id": key.removeprefix(prefix),
                "disposition": value,
                "reason": form.get(
                    "reason__" + key.removeprefix(prefix), ""
                ),
            }
            for key, value in form.items()
            if key.startswith(prefix) and value
        ]
        result = self.kurt_schwarz().save_media_decisions(
            record_key=record_key,
            decisions=decisions,
            actor=self.server.actor,
        )
        notice = f"Recorded {result['changed_count']} media decisions."
        next_record = next_pending_kurt_record(
            self.kurt_schwarz().records(), record_key
        )
        self.redirect(
            url("/kurt-schwarz/object", record=next_record, notice=notice)
            if next_record
            else url("/", state="pending", notice=notice)
        )

    def action_kurt_schwarz_metadata_decision(
        self, form: dict[str, str]
    ) -> None:
        record_key = form.get("record_key", "")
        result = self.kurt_schwarz().save_metadata_decision(
            record_key=record_key,
            field_key=form.get("field_key", ""),
            action=form.get("action", ""),
            actor=self.server.actor,
            corrected_value=form.get("corrected_value", ""),
            reason=form.get("reason", ""),
        )
        self.redirect(
            url(
                "/kurt-schwarz/object",
                record=record_key,
                notice=(
                    f"Recorded {result['action']} for "
                    f"{result['field_key']}."
                ),
            )
        )

    def action_kurt_schwarz_alias_decision(
        self, form: dict[str, str]
    ) -> None:
        alias_record_key = form.get("alias_record_key", "")
        result = self.kurt_schwarz().save_alias_decision(
            alias_record_key=alias_record_key,
            canonical_reference=form.get("canonical_reference", ""),
            action=form.get("action", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        if result["action"] == "same_object_alias":
            notice = (
                f"Recorded {result['alias_record_key']} as an alias of "
                f"{result['canonical_record_key']}."
            )
        else:
            notice = (
                f"Reopened {result['alias_record_key']} as an independent "
                "object."
            )
        self.redirect(
            url(
                "/kurt-schwarz/object",
                record=alias_record_key,
                notice=notice,
            )
        )

    def action_kurt_schwarz_candidate_link(
        self, form: dict[str, str]
    ) -> None:
        record_key = form.get("target_record_key", "").strip()
        if not record_key:
            record_key = self.kurt_schwarz().resolve_record_reference(
                form.get("target_reference", "")
            )
        result = self.kurt_schwarz().link_representation_candidate(
            target_record_key=record_key,
            final_path=form.get("final_path", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        notice = (
            "Candidate was already bound to this object."
            if result["unchanged"]
            else "Added manifested representation as a review candidate."
        )
        self.redirect(
            url(
                "/kurt-schwarz/object",
                record=record_key,
                notice=notice,
            )
        )

    def action_kurt_schwarz_document_decision(
        self, form: dict[str, str]
    ) -> None:
        document_id = form.get("document_id", "")
        result = self.kurt_schwarz().save_document_decision(
            document_id=document_id,
            disposition=form.get("disposition", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        documents = self.kurt_schwarz().documents()
        next_document = next_pending_kurt_document(documents, document_id)
        notice = f"Recorded {result['disposition']} for document."
        self.redirect(
            url(
                "/kurt-schwarz/document",
                id=next_document,
                notice=notice,
            )
            if next_document
            else url(
                "/kurt-schwarz/documents",
                state="pending",
                notice=notice,
            )
        )

    def action_kurt_schwarz_object_census_decision(
        self, form: dict[str, str]
    ) -> None:
        candidate_id = form.get("candidate_id", "")
        result = self.kurt_schwarz().save_object_census_decision(
            candidate_id=candidate_id,
            disposition=form.get("disposition", ""),
            target_reference=form.get("target_reference", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
            curated_title=(
                form.get("curated_title", "")
                if "curated_title" in form else None
            ),
            curated_date_text=(
                form.get("curated_date_text", "")
                if "curated_date_text" in form else None
            ),
            curated_description=(
                form.get("curated_description", "")
                if "curated_description" in form else None
            ),
            curated_tags=(
                form.get("curated_tags", "")
                if "curated_tags" in form else None
            ),
            cross_unit_target_scope=form.get("cross_unit_target_scope", ""),
            cross_unit_target_subunit=form.get("cross_unit_target_subunit", ""),
            cross_unit_group_key=form.get("cross_unit_group_key", ""),
            cross_unit_provisional_title=form.get(
                "cross_unit_provisional_title", ""
            ),
            cross_unit_provisional_date_text=form.get(
                "cross_unit_provisional_date_text", ""
            ),
            cross_unit_object_type=form.get("cross_unit_object_type", ""),
            cross_unit_component_sequence=form.get(
                "cross_unit_component_sequence", ""
            ),
            cross_unit_component_relationship=form.get(
                "cross_unit_component_relationship", ""
            ),
        )
        candidates = self.kurt_schwarz().object_census_candidates()
        next_candidate = next_pending_kurt_object_census(
            candidates, candidate_id
        )
        notice = f"Recorded {result['disposition']} for object candidate."
        self.redirect(
            url(
                "/kurt-schwarz/object-candidate",
                id=next_candidate,
                notice=notice,
            )
            if next_candidate
            else url(
                "/kurt-schwarz/object-census",
                state="pending",
                notice=notice,
            )
        )

    def action_kurt_schwarz_object_census_similarity_decision(
        self, form: dict[str, str]
    ) -> None:
        pair_id = form.get("pair_id", "")
        selection = form.get("decision", "")
        pair = next(
            (
                item
                for item in self.kurt_schwarz().object_census_similarity_pairs()
                if item["pair_id"] == pair_id
            ),
            None,
        )
        if pair is None:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                f"unknown object-census similarity pair: {pair_id}"
            )
        if selection in {"same_a", "redundant_b"}:
            outcome = "same_photo"
            primary_candidate_id = pair["candidate_a_id"]
        elif selection in {"same_b", "redundant_a"}:
            outcome = "same_photo"
            primary_candidate_id = pair["candidate_b_id"]
        elif selection in {"different_photo", "defer"}:
            outcome = selection
            primary_candidate_id = ""
        else:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                "select one Redundant file, Different photographs, or Decide later"
            )
        result = self.kurt_schwarz().save_object_census_similarity_decision(
            pair_id=pair_id,
            outcome=outcome,
            primary_candidate_id=primary_candidate_id,
            run_fingerprint=form.get("run_fingerprint", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        if result["outcome"] == "same_photo":
            companion = (
                pair["candidate_a"]
                if result["companion_candidate_id"] == pair["candidate_a_id"]
                else pair["candidate_b"]
            )
            primary = (
                pair["candidate_a"]
                if result["primary_candidate_id"] == pair["candidate_a_id"]
                else pair["candidate_b"]
            )
            notice = (
                f"Marked {companion['filename']} Redundant and retained "
                f"{primary['filename']} as Master."
            )
        else:
            notice = (
                "Recorded different photographs; both candidates remain separate."
                if result["outcome"] == "different_photo"
                else "Deferred this similarity pair."
            )
        self.redirect(
            url(
                "/kurt-schwarz/object-census-duplicates",
                state="pending",
                notice=notice,
            )
        )

    def action_kurt_schwarz_object_census_manual_duplicate(
        self, form: dict[str, str]
    ) -> None:
        companion_candidate_id = form.get("companion_candidate_id", "")
        result = self.kurt_schwarz().save_manual_object_census_duplicate_decision(
            companion_candidate_id=companion_candidate_id,
            retained_candidate_id=form.get("retained_candidate_id", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        receipt = self.kurt_schwarz().write_manual_object_census_duplicate_receipt(
            result
        )
        candidates = self.kurt_schwarz().object_census_candidates()
        next_candidate = next_pending_kurt_object_census(
            candidates, companion_candidate_id
        )
        if result["relationship_recorded"]:
            notice = (
                f"Marked {result['companion_filename']} Redundant beneath "
                f"{result['retained_filename']}; drain evidence is preserved. "
                f"Receipt: {receipt.name}."
            )
        else:
            notice = (
                f"Marked {result['companion_filename']} Redundant; drain "
                f"evidence is preserved. Receipt: {receipt.name}."
            )
        self.redirect(
            url(
                "/kurt-schwarz/object-candidate",
                id=next_candidate,
                notice=notice,
            )
            if next_candidate
            else url(
                "/kurt-schwarz/object-census",
                state="pending",
                notice=notice,
            )
        )

    def action_kurt_schwarz_object_census_similarity_group_decision(
        self, form: dict[str, str]
    ) -> None:
        roles = {
            key.removeprefix("role__"): value
            for key, value in form.items()
            if key.startswith("role__")
        }
        invalid = {
            value for value in roles.values() if value not in {"pending", "master", "redundant"}
        }
        if invalid:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                "invalid similarity-group role"
            )
        masters = [candidate for candidate, role in roles.items() if role == "master"]
        redundant = [
            candidate for candidate, role in roles.items() if role == "redundant"
        ]
        if len(masters) != 1:
            raise kurt_schwarz_reconciliation.KurtSchwarzReconciliationError(
                "select exactly one group Master"
            )
        result = self.kurt_schwarz().save_object_census_similarity_group_decision(
            group_id=form.get("group_id", ""),
            master_candidate_id=masters[0],
            redundant_candidate_ids=redundant,
            run_fingerprint=form.get("run_fingerprint", ""),
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        receipt = self.kurt_schwarz().write_object_census_similarity_group_receipt(
            result
        )
        notice = (
            f"Recorded {result['master_filename']} as group Master with "
            f"{len(result['redundant_candidate_ids'])} Redundant files."
            + (f" Receipt: {receipt.name}." if receipt else " No changes were needed.")
        )
        self.redirect(
            url(
                "/kurt-schwarz/object-census-duplicate-group",
                id=result["group_id"],
                notice=notice,
            )
        )

    def action_kurt_schwarz_object_census_similarity_group_existing_object(
        self, form: dict[str, str]
    ) -> None:
        result = (
            self.kurt_schwarz().assign_object_census_similarity_group_to_existing_object(
                group_id=form.get("group_id", ""),
                target_reference=form.get("target_reference", ""),
                actor=self.server.actor,
                run_fingerprint=form.get("run_fingerprint", ""),
                reason=form.get("reason", ""),
            )
        )
        receipt = self.kurt_schwarz().write_object_census_group_assignment_receipt(
            result
        )
        notice = (
            f"Moved {len(result['candidate_ids'])} confirmed copies beneath "
            f"{result['target_record_key']}."
            + (f" Receipt: {receipt.name}." if receipt else "")
        )
        self.redirect(
            url(
                "/kurt-schwarz/object-census",
                state="pending",
                notice=notice,
            )
        )

    def wf_sender(self) -> wf_sender_workbench.WfSenderReview:
        review = self.server.wf_sender_review
        if review is None:
            raise wf_sender_workbench.WfSenderWorkbenchError(
                "WF-Sender review mode is not active"
            )
        return review

    @staticmethod
    def _wf_category_label(category: str) -> str:
        return {
            "duplicate_inventory": "Duplicate inventory",
            "fallback_candidates": "Fallback candidates",
            "cross_candidate": "Metadata/masthead cross-candidate",
            "missing_local": "No local candidate yet",
            "local_only": "Local-only archive candidate",
        }.get(category, category.replace("_", " "))

    def page_wf_sender_queue(self) -> None:
        parameters = self.parameters()
        category = parameters.get("category", "")
        state = parameters.get("state", "pending")
        if state not in {"pending", "decided", "all"}:
            state = "pending"
        term = parameters.get("q", "").strip().casefold()
        records = self.wf_sender().records()
        totals = {
            key: sum(1 for item in records if item["category"] == key)
            for key in (
                "duplicate_inventory",
                "fallback_candidates",
                "cross_candidate",
                "missing_local",
                "local_only",
            )
        }
        decided = sum(1 for item in records if item["decision"])
        pending_local_only = sum(
            1
            for item in records
            if item["category"] == "local_only" and not item["decision"]
        )
        visible = [
            item
            for item in records
            if (not category or item["category"] == category)
            and (
                state == "all"
                or (state == "decided" and item["decision"])
                or (state == "pending" and not item["decision"])
            )
            and (
                not term
                or term
                in " ".join(
                    str(
                        item.get(key)
                        or ""
                    ).casefold()
                    for key in (
                        "inventory_raw",
                        "representative_inventory",
                        "title",
                        "md_object_id",
                    )
                )
            )
        ]
        cards = []
        for item in visible:
            inventory = (
                item.get("inventory_raw")
                or item.get("representative_inventory")
                or item["record_key"]
            )
            title = item.get("title") or "Local evidence without MD match"
            decision = item.get("decision")
            local_object_count = len(
                {
                    str(
                        candidate.get("local_object_id")
                        or candidate["candidate_id"]
                    )
                    for candidate in item["candidates"]
                }
            )
            decision_html = (
                badge(str(decision["decision"])) if decision else badge("pending")
            )
            cards.append(
                f"""<article class="browse-card">
  <div class="browse-card__heading">
    <div><p class="eyebrow">{e(self._wf_category_label(item["category"]))}</p>
      <h2><a href="{e(url("/compare", record=item["record_key"]))}">{e(inventory)}</a></h2>
      {f'<p class="record-identity">museum-digital object {e(item["md_object_id"])}</p>' if item.get("md_object_id") else '<p class="record-identity">local archive object</p>'}</div>
    {decision_html}
  </div>
  <p>{e(title)}</p>
  <dl class="metadata metadata--compact">
    <div><dt>Local objects</dt><dd>{local_object_count}</dd></div>
    <div><dt>Representations</dt><dd>{len(item["candidates"])}</dd></div>
    <div><dt>MD object</dt><dd>{e(item.get("md_object_id") or "none")}</dd></div>
    {f'<div><dt>Suggested MD</dt><dd>{len(item.get("md_suggestions") or [])}</dd></div>' if item.get("md_suggestions") else ''}
  </dl>
</article>"""
            )
        gallery_heading = ""
        if category == "local_only":
            gallery_cards = []
            for item in visible:
                inventory = str(item.get("representative_inventory") or "")
                decision = item.get("decision")
                for candidate in item["candidates"]:
                    candidate_id = str(candidate["candidate_id"])
                    gallery_cards.append(
                        f"""<article class="wf-gallery-card">
  <a class="wf-gallery-card__preview" href="{e(url("/compare", record=item["record_key"], candidate=candidate_id))}">
    <img loading="lazy" src="{e(url("/wf-sender/local-image", record=item["record_key"], candidate=candidate_id))}" alt="{e(inventory)} {e(candidate.get("source_family"))}">
  </a>
  <div class="wf-gallery-card__body">
    <p class="eyebrow">{e(candidate.get("source_family") or "local representation")}</p>
    <h2><a href="{e(url("/compare", record=item["record_key"], candidate=candidate_id))}">{e(inventory)}</a></h2>
    <p>{e(candidate.get("source_directory") or "")}</p>
    <p>{badge(str(decision["decision"])) if decision else badge("pending")}
       {f'{len(item.get("md_suggestions") or [])} suggested MD relationship(s)' if item.get("md_suggestions") else 'No suggested MD relationship'}</p>
  </div>
</article>"""
                    )
            gallery_heading = (
                f"<p><strong>{len(gallery_cards)}</strong> local PDF/TIFF "
                f"representations across <strong>{len(visible)}</strong> objects. "
                "Cards sharing an inventory are representations of one object, "
                "not duplicate object records. Open a card to inspect all evidence "
                "and any suggested MD links.</p>"
            )
            results_html = (
                f'<section class="wf-gallery">{"".join(gallery_cards)}</section>'
                if gallery_cards
                else '<div class="empty"><h2>No local-only evidence in this view</h2></div>'
            )
        elif category == "missing_local":
            gallery_cards = []
            for item in visible:
                inventory = str(item.get("inventory_raw") or "")
                decision = item.get("decision")
                gallery_cards.append(
                    f"""<article class="wf-gallery-card">
  <a class="wf-gallery-card__preview" href="{e(url("/compare", record=item["record_key"]))}">
    <img loading="lazy" src="{e(url("/wf-sender/md-image", record=item["record_key"]))}" alt="museum-digital {e(inventory)}">
  </a>
  <div class="wf-gallery-card__body">
    <p class="eyebrow">No local candidate yet · object {e(item["md_object_id"])}</p>
    <h2><a href="{e(url("/compare", record=item["record_key"]))}">{e(inventory)}</a></h2>
    <p>{e(item.get("title") or "")}</p>
    <p>{badge(str(decision["decision"])) if decision else badge("pending")}</p>
  </div>
</article>"""
                )
            gallery_heading = (
                f"<p><strong>{len(gallery_cards)}</strong> museum-digital objects "
                "remain without a candidate from the searches run so far. This "
                "is not proof that local evidence is absent. Previewing does not "
                "turn the cached public PDF into an accession.</p>"
            )
            results_html = (
                f'<section class="wf-gallery">{"".join(gallery_cards)}</section>'
                if gallery_cards
                else '<div class="empty"><h2>No unmatched MD records in this view</h2></div>'
            )
        else:
            standard_cards = (
                "".join(cards)
                if cards
                else '<div class="empty"><h2>No records in this view</h2></div>'
            )
            results_html = f'<section class="browse-grid">{standard_cards}</section>'
        body = f"""<section class="page-heading">
  <div><p class="eyebrow">WF-Sender · museum-digital collection 552</p>
    <h1>Visual reconciliation of disputed objects</h1>
    <p>Bounded review queue. Decisions are appended to the work database; no source file is copied, renamed, deleted, or accessioned.</p></div>
</section>
<section class="stats">
  <article><strong>{len(records)}</strong><span>disputed records</span></article>
  <article><strong>{len(records) - decided}</strong><span>pending</span></article>
  <article><strong>{decided}</strong><span>decided</span></article>
  <article><strong>{pending_local_only}</strong><span>pending local-only</span></article>
</section>
<form class="search" method="get" action="/">
  <label>Search <input name="q" value="{e(parameters.get("q", ""))}" placeholder="Inventory, title, object ID"></label>
  <label>Category <select name="category">
    {option("", "All disputed records", category)}
    {option("duplicate_inventory", f'Duplicate inventory ({totals["duplicate_inventory"]})', category)}
    {option("fallback_candidates", f'Fallback candidates ({totals["fallback_candidates"]})', category)}
    {option("cross_candidate", f'Cross-candidates ({totals["cross_candidate"]})', category)}
    {option("missing_local", f'No local candidate yet ({totals["missing_local"]})', category)}
    {option("local_only", f'Local-only review ({pending_local_only} pending / {totals["local_only"]} total)', category)}
  </select></label>
  <label>State <select name="state">
    {option("pending", "Pending", state)}
    {option("decided", "Decided", state)}
    {option("all", "All", state)}
  </select></label>
  <button class="button" type="submit">Filter</button>
</form>
{gallery_heading}
{results_html}"""
        self.send_html(
            "WF-Sender disputed objects",
            body,
            active=category or "queue",
        )

    def page_wf_sender_compare(self) -> None:
        parameters = self.parameters()
        record_key = parameters.get("record", "")
        record = self.wf_sender().record(record_key)
        candidates = record["candidates"]
        selected_id = wf_sender_candidate_selection(
            candidates,
            requested=parameters.get("candidate", ""),
            decision=record.get("decision"),
        )
        selected = (
            self.wf_sender().candidate(record_key, selected_id)
            if selected_id
            else None
        )
        inventory = (
            record.get("inventory_raw")
            or record.get("representative_inventory")
            or record_key
        )
        duplicate_siblings = [
            item
            for item in self.wf_sender().records()
            if item["record_key"] != record_key
            and item["category"] == "duplicate_inventory"
            and item.get("inventory_raw") == record.get("inventory_raw")
        ]
        duplicate_notice = ""
        if record["category"] == "duplicate_inventory":
            sibling_links = " · ".join(
                f'<a href="{e(url("/compare", record=item["record_key"]))}">'
                f'MD {e(item["md_object_id"])}: {e(item["title"])}</a>'
                for item in duplicate_siblings
            )
            duplicate_notice = f"""<section class="notice notice--warning">
  <h2>Shared inventory label — object IDs remain separate</h2>
  <p>You are reviewing <strong>MD {e(record["md_object_id"])}</strong>:
     {e(record["title"])}. The PDF and TIFF choices below are representations
     of the same local object, not additional museum-digital objects.</p>
  {f'<p>Other museum-digital row with this label: {sibling_links}</p>' if sibling_links else ''}
</section>"""
        md_visual = (
            f"""<div class="wf-document wf-document--image">
                  <img src="{e(url("/wf-sender/md-image", record=record_key))}"
                       alt="First page of museum-digital PDF">
                </div>
                <p><a target="_blank" href="{e(url("/wf-sender/md-pdf", record=record_key))}">Open full museum-digital PDF</a>
                   · <a target="_blank" rel="noreferrer" href="{e(record.get("object_url"))}">Object page</a></p>"""
            if record["resources"]
            else """<div class="empty wf-document"><h3>No museum-digital PDF</h3>
                    <p>This local-only record must be judged from local evidence and metadata.</p></div>"""
        )
        if selected:
            media_path, media_kind = self.wf_sender().candidate_media(
                record_key, selected_id
            )
            media_url = url(
                "/wf-sender/local-image",
                record=record_key,
                candidate=selected_id,
            )
            local_visual = f"""<div class="wf-document wf-document--image">
  <img src="{e(media_url)}" alt="First page of local candidate">
</div>
{f'<p><a target="_blank" href="{e(url("/wf-sender/local-file", record=record_key, candidate=selected_id))}">Open full local PDF</a></p>' if media_kind == "pdf" else ''}"""
        else:
            media_path = None
            local_visual = (
                '<div class="empty wf-document"><h3>No local candidate</h3>'
                "<p>The bounded primary and fallback searches found no path or container-member candidate.</p></div>"
            )
        candidate_rows = []
        for candidate in candidates:
            candidate_id = str(candidate["candidate_id"])
            source = (
                candidate.get("source_directory")
                or candidate.get("source_path")
                or (candidate.get("files") or [{}])[0].get("source_path")
                or ""
            )
            label = (
                f"{candidate.get('source_family') or candidate.get('alias_method') or 'fallback'} · "
                f"{Path(str(source)).name or candidate_id}"
            )
            suggestion_html = (
                f"""<span class="wf-suggestion">
  Suggested cross-match · {e(candidate.get("suggestion_confidence"))} confidence ·
  {e(candidate.get("suggestion_basis"))}
</span>"""
                if candidate.get("suggested_cross_match")
                else ""
            )
            candidate_rows.append(
                f"""<li class="wf-candidate{' is-selected' if candidate_id == selected_id else ''}">
  <a href="{e(url("/compare", record=record_key, candidate=candidate_id))}"><strong>{e(label)}</strong></a>
  <span>{e(source)}</span>
  {suggestion_html}
</li>"""
            )
        decision = record.get("decision")
        decision_detail = ""
        if decision:
            try:
                basis = json.loads(str(decision["basis"]))
                note = str(basis.get("note") or "")
                chosen = str(basis.get("candidate_id") or "")
            except (json.JSONDecodeError, AttributeError):
                note = str(decision["basis"])
                chosen = ""
            decision_label = {
                "confirm_local_candidate": "confirm selected MD/local object relationship",
                "reject_local_candidates": "reject selected MD/local object relationship",
                "linked_to_museum_digital": "linked to museum-digital",
                "accept_md_only_fallback": "accept MD PDF as fallback candidate",
                "needs_more_local_search": "continue local evidence search",
            }.get(
                str(decision["decision"]),
                str(decision["decision"]).replace("_", " "),
            )
            selection_warning = (
                f"""<p class="notice notice--warning">
  This latest decision applies to {e(chosen)}, not the currently selected
  {e(selected_id)}. The selected local object relationship is still undecided.
</p>"""
                if chosen and selected_id and chosen != selected_id
                else ""
            )
            decision_detail = f"""<section class="notice notice--safe">
  <h2>Latest decision: {e(decision_label)}</h2>
  <p>{e(decision["decided_utc"])} by {e(decision["actor"])}
     {f'· candidate {e(chosen)}' if chosen else ''}</p>
  {f'<p>{e(note)}</p>' if note else ''}
  {selection_warning}
</section>"""
        if record["category"] == "local_only":
            decision_options = """
              <label><input type="radio" name="decision" value="retain_as_archive_candidate" required> Retain as archive candidate</label>
              <label><input type="radio" name="decision" value="reject_as_duplicate_or_non_object" required> Reject as duplicate or non-object</label>
              <label><input type="radio" name="decision" value="needs_more_review" required> Needs more review</label>"""
        elif record["category"] == "missing_local":
            decision_options = """
              <label><input type="radio" name="decision" value="accept_md_only_fallback" required> Accept museum-digital PDF as fallback candidate</label>
              <label><input type="radio" name="decision" value="needs_more_local_search" required> Continue searching for local evidence</label>
              <label><input type="radio" name="decision" value="needs_more_review" required> Needs more review</label>"""
        else:
            decision_options = """
              <label><input type="radio" name="decision" value="confirm_local_candidate" required> Confirm this MD/local object relationship using the selected representation</label>
              <label><input type="radio" name="decision" value="reject_local_candidates" required> Reject this MD/local object relationship</label>
              <label><input type="radio" name="decision" value="needs_more_review" required> Needs more review</label>"""
        md_suggestions = record.get("md_suggestions") or []
        md_suggestions_html = ""
        if md_suggestions:
            suggestion_rows = "".join(
                f"""<li>
  <a href="{e(url("/compare", record="md:" + str(item["md_object_id"]), candidate=selected_id))}">
    <strong>MD {e(item["md_object_id"])} · {e(item["title"])}</strong>
  </a>
  {badge(str(item.get("relationship_decision") or "suggested-" + str(item["confidence"])))}
  <p>{e(item["basis"])}</p>
  {f'<p>Relationship reviewed {e(item.get("relationship_decided_utc") or "")}.</p>' if item.get("relationship_decision") else ''}
</li>"""
                for item in md_suggestions
            )
            md_suggestions_html = f"""<section class="notice notice--warning">
  <h2>Suggested museum-digital relationships</h2>
  <p>Metadata and first-page masthead text generated these discovery candidates.
     Reviewed relationships carry their recorded decision. Open an unresolved
     suggestion to compare both representations.</p>
  <ul class="wf-suggestion-list">{suggestion_rows}</ul>
</section>"""
        unresolved_md_suggestions = [
            item
            for item in md_suggestions
            if not item.get("relationship_decision")
        ]
        if record["category"] == "local_only" and unresolved_md_suggestions:
            decision_panel = """<section class="panel decision-panel">
  <div class="panel-heading"><div><h2>Resolve the suggested relationship first</h2>
    <p>Open a suggested museum-digital record above, compare it with this local
       representation, and record the relationship decision there. A confirmed
       link will mark this local-only row as linked automatically.</p></div></div>
</section>"""
        else:
            decision_panel = f"""<section class="panel decision-panel">
  <div class="panel-heading"><div><h2>Record a review decision</h2><p>This appends a decision only. It does not accession or alter evidence.</p></div></div>
  <form method="post" action="/wf-sender/decision">
    {self.csrf()}
    <input type="hidden" name="record" value="{e(record_key)}">
    <input type="hidden" name="candidate" value="{e(selected_id)}">
    <div class="decision-choice-grid">{decision_options}</div>
    <label>Review note <textarea name="note" rows="3" placeholder="Visual evidence, ambiguity, or follow-up needed"></textarea></label>
    <button class="button" type="submit">Append decision</button>
  </form>
</section>"""
        body = f"""<section class="page-heading">
  <div><p class="eyebrow">{e(self._wf_category_label(record["category"]))}</p>
    <h1>{e(inventory)}</h1>
    <p>{f'Museum-digital object {e(record["md_object_id"])} · ' if record.get("md_object_id") else ''}{e(record.get("title") or "Local object without museum-digital match")}</p></div>
  <a class="button button--quiet" href="/">Back to queue</a>
</section>
{duplicate_notice}
{md_suggestions_html}
{decision_detail}
<section class="panel">
  <div class="panel-heading"><div><h2>Metadata</h2><p>Identifiers and descriptive evidence remain distinct from the visual decision.</p></div></div>
  <dl class="metadata">
    <div><dt>Record</dt><dd>{e(record_key)}</dd></div>
    <div><dt>Inventory</dt><dd>{e(inventory)}</dd></div>
    <div><dt>Date</dt><dd>{e(record.get("date_text") or "not supplied")}</dd></div>
    <div><dt>Object type</dt><dd>{e(record.get("object_type") or "not supplied")}</dd></div>
    <div><dt>MD object ID</dt><dd>{e(record.get("md_object_id") or "none")}</dd></div>
    <div><dt>Local representations</dt><dd>{len(candidates)}</dd></div>
  </dl>
</section>
<section class="wf-compare-grid">
  <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Reference</p><h2>museum-digital</h2></div></div>{md_visual}</article>
  <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Protected source</p><h2>Local candidate</h2></div></div>{local_visual}
    {f'<p class="path">{e(media_path)}</p>' if media_path else ''}
  </article>
</section>
<section class="panel">
  <div class="panel-heading"><div><h2>Bound local representations</h2><p>Select the PDF or TIFF representation used for visual comparison. Representations with the same local object ID are not separate object candidates.</p></div></div>
  <ul class="wf-candidate-list">{''.join(candidate_rows) if candidate_rows else '<li>No candidate paths found.</li>'}</ul>
</section>
{decision_panel}"""
        self.send_html(
            f"Compare {inventory}",
            body,
            active=record["category"],
        )

    def page_wf_sender_local_file(self) -> None:
        parameters = self.parameters()
        source, media_kind = self.wf_sender().candidate_media(
            parameters.get("record", ""),
            parameters.get("candidate", ""),
        )
        if media_kind != "pdf":
            raise wf_sender_workbench.WfSenderWorkbenchError(
                "selected evidence is not a PDF"
            )
        self.send_path(source, "application/pdf")

    def page_wf_sender_md_pdf(self) -> None:
        record_key = self.parameters().get("record", "")
        self.send_path(self.wf_sender().cached_md_pdf(record_key), "application/pdf")

    def page_wf_sender_md_image(self) -> None:
        record_key = self.parameters().get("record", "")
        source = self.wf_sender().cached_md_pdf(record_key)
        preview = self.wf_sender().pdf_preview(source, "museum-digital", record_key)
        self.send_path(preview, "image/png")

    def page_wf_sender_local_image(self) -> None:
        parameters = self.parameters()
        record_key = parameters.get("record", "")
        candidate_id = parameters.get("candidate", "")
        source, media_kind = self.wf_sender().candidate_media(
            record_key, candidate_id
        )
        if media_kind == "pdf":
            preview = self.wf_sender().pdf_preview(
                source, "local", f"{record_key}:{candidate_id}"
            )
        else:
            preview = self.wf_sender().image_preview(
                record_key, candidate_id, source
            )
        self.send_path(preview, "image/png")

    def action_wf_sender_decision(self, form: dict[str, str]) -> None:
        record_key = form.get("record", "").strip()
        candidate_id = form.get("candidate", "").strip()
        record = self.wf_sender().record(record_key)
        self.wf_sender().save_decision(
            record_key=record_key,
            actor=self.server.actor,
            decision=form.get("decision", "").strip(),
            candidate_id=candidate_id,
            note=form.get("note", ""),
        )
        self.redirect(
            url(
                "/compare",
                record=record_key,
                candidate=candidate_id,
                notice=(
                    "Decision saved for "
                    + (
                        f"museum-digital object {record['md_object_id']}, "
                        if record.get("md_object_id")
                        else ""
                    )
                    + str(
                        record.get("title")
                        or record.get("representative_inventory")
                        or record_key
                    )
                    + "; this exact record and representation remain displayed."
                ),
            )
        )

    def stress_case(self, unit_id: str) -> dict[str, Any]:
        case = self.server.stress_cases.get(unit_id)
        if not case:
            raise reconciliation.ReconciliationError(
                f"unknown stress-pilot unit: {unit_id}"
            )
        return case

    def page_recovery_browse(self) -> None:
        parameters = self.parameters()
        term = parameters.get("q", "").casefold()
        structure = parameters.get("structure", "")
        view = parameters.get("view", "visual_review")
        if view not in {"visual_review", "ready", "reconciled", "rejected", "all"}:
            view = "visual_review"
        with closing(self.catalog(read_only=True)) as catalog:
            archive_records = {
                str(row["archival_label"]): dict(row)
                for row in catalog.execute(
                    """SELECT ao.archival_label, ao.descriptive_title,
                              (
                                SELECT fo.relative_path
                                FROM entity_files ef
                                JOIN file_occurrences fo
                                  ON fo.occurrence_id=ef.occurrence_id
                                WHERE ef.entity_id=ao.entity_id
                                  AND ef.status='active'
                                  AND fo.status='verified'
                                  AND ef.role IN (
                                      'access_derivative',
                                      'preservation_master',
                                      'preservation_surrogate'
                                  )
                                ORDER BY CASE ef.role
                                  WHEN 'access_derivative' THEN 0
                                  WHEN 'preservation_master' THEN 1
                                  ELSE 2 END
                                LIMIT 1
                              ) AS active_image
                       FROM archive_objects ao
                       WHERE ao.status='active'"""
                )
            }
        records = []
        confirmed_structure = self.server.recovery_structure()
        queue_states = self.server.recovery_queue_states()
        counts = {
            "all": len(queue_states),
            "visual_review": sum(
                status == "visual_review" for status in queue_states.values()
            ),
            "ready": sum(status == "ready" for status in queue_states.values()),
            "reconciled": sum(
                status == "reconciled" for status in queue_states.values()
            ),
            "rejected": sum(
                status == "rejected" for status in queue_states.values()
            ),
        }
        for source_record in self.server.recovery_records.values():
            record = dict(source_record)
            record.update(confirmed_structure[record["inventory"]])
            record["queue_state"] = queue_states[record["inventory"]]
            archive = archive_records.get(record["inventory"], {})
            record["archive_title"] = str(
                archive.get("descriptive_title") or ""
            )
            record["active_image"] = str(archive.get("active_image") or "")
            local, md = record["local"], record["md"]
            searchable = " ".join(
                (
                    record["inventory"],
                    record["archive_title"],
                    str(local.get("title", "")),
                    str(md.get("object_name", "")),
                )
            ).casefold()
            view_matches = view == "all" or record["queue_state"] == view
            if (not term or term in searchable) and view_matches and (not structure or record["record_kind"] == structure): records.append(record)
        cards = ""
        for record in records:
            image_files = [item for item in record["files"] if str(item["extension"]).casefold() in {"jpg","jpeg","png","webp","tif","tiff"}]
            thumb = (
                f'<img loading="lazy" src="{url("/recovery/active-image", record=record["inventory"])}" alt="">'
                if record["active_image"]
                else (
                    f'<img loading="lazy" src="{url("/recovery/image", record=record["inventory"], choice=1)}" alt="">'
                    if image_files
                    else '<div class="browse-card__missing">No local image</div>'
                )
            )
            title = (
                record["archive_title"]
                or record["md"].get("object_name")
                or record["local"].get("title")
                or "Untitled record"
            )
            structure_note = f'{len(record["components"])} components' if record["record_kind"] == "carrier" else f'Component of {record["carrier_id"]}' if record["carrier_id"] else 'Standalone record'
            review_label = {
                "visual_review": "visual review",
                "ready": "ready for accession",
                "reconciled": "reconciled",
                "rejected": "rejected",
            }.get(record["queue_state"], "visual review")
            if record["queue_state"] == "ready" and record.get(
                "correction_required"
            ):
                review_label = "ready for corrective re-accession"
            cards += f'<article class="browse-card"><a class="browse-card__image" href="{url("/compare", record=record["inventory"])}">{thumb}</a><div class="browse-card__body"><p>{badge(review_label)} {badge(record["record_kind"])} {badge(record["md_state"])}</p><h2><a href="{url("/compare", record=record["inventory"])}">{e(record["inventory"])}</a></h2><p>{e(title)}</p><p>{e(structure_note)} · {len(record["files"])} local files</p></div></article>'
        scope = self.server.recovery_scope or {}; summary = scope.get("summary", {})
        supplemental = self.server.recovery_supplemental_summary
        browse_menu = f'''<nav class="button-row" aria-label="Record queues">
<a class="button{' button--quiet' if view != 'visual_review' else ''}" href="{url('/browse', view='visual_review')}">Visual review ({counts['visual_review']})</a>
<a class="button{' button--quiet' if view != 'ready' else ''}" href="{url('/browse', view='ready')}">Ready for accession / correction ({counts['ready']})</a>
<a class="button{' button--quiet' if view != 'reconciled' else ''}" href="{url('/browse', view='reconciled')}">Reconciled ({counts['reconciled']})</a>
<a class="button{' button--quiet' if view != 'rejected' else ''}" href="{url('/browse', view='rejected')}">Rejected ({counts['rejected']})</a>
<a class="button{' button--quiet' if view != 'all' else ''}" href="{url('/browse', view='all')}">All records ({counts['all']})</a></nav>'''
        body = f'<section class="page-heading"><p class="eyebrow">Semi-real recovery · WF-PARAGON</p><h1>Browse Paragon</h1><p>{summary.get("files",0)} historical base files + {supplemental["files"]} supplemental TIFF references ({supplemental["linked"]} still available, {supplemental["unlinked"]} unresolved, {supplemental["retired"]} retired paths) · {supplemental["package_fallbacks"]} active-package browsing fallbacks · {len(self.server.recovery_records)} records. Carrier structure shown here is editor-confirmed catalog data; filenames are not used to assign components.</p></section>{browse_menu}<form class="form-grid editor-filters" method="get"><input type="hidden" name="view" value="{e(view)}"><label>Search<input name="q" value="{e(parameters.get("q",""))}"></label><label>Structure<select name="structure"><option value="">All</option>{option("carrier","Carriers",structure)}{option("component","Components",structure)}{option("standalone","Standalone",structure)}</select></label><button class="button">Filter</button></form><p class="result-count">{len(records)} records</p><section class="browse-grid">{cards}</section>'
        self.send_html("Browse Paragon", body, active=view)

    def _recovery_record(self) -> dict[str, Any]:
        key = self.parameters().get("record", ""); record = self.server.recovery_records.get(key)
        if not record: raise reconciliation.ReconciliationError("unknown Paragon recovery record")
        return record

    def _recovery_candidate_role(
        self,
        record: dict[str, Any],
        evidence: sqlite3.Row,
        local_evidence: list[sqlite3.Row],
    ) -> tuple[str, str]:
        return recovery_candidate_role(record, evidence, local_evidence)

    def page_recovery_compare(self) -> None:
        record = dict(self._recovery_record())
        confirmed_structure = self.server.recovery_structure()
        record.update(confirmed_structure[record["inventory"]])
        review_details = self.server.recovery_review_details()
        record["review_detail"] = review_details[record["inventory"]]
        record["review_state"] = record["review_detail"]["status"]
        record["correction_detail"] = self.server.recovery_correction_states().get(
            record["inventory"], {"state": "open"}
        )
        record["correction_state"] = str(
            record["correction_detail"].get("state") or "open"
        )
        record["queue_state"] = self.server.recovery_accession_plan(
            record["inventory"]
        )["state"]
        local, md = record["local"], record["md"]
        archive_values = {
            "descriptive_title": "",
            "description": "",
            "material_technique": "",
            "dimensions": "",
            "object_type": "",
            "tags": [],
        }
        candidate_reviews: dict[str, dict[str, Any]] = {}
        carrier_html = ""
        if record["carrier_id"]:
            carrier = dict(self.server.recovery_records[record["carrier_id"]])
            carrier.update(confirmed_structure[record["carrier_id"]])
            carrier_images = [item for item in carrier["files"] if str(item["extension"]).casefold() in {"jpg","jpeg","png","webp","tif","tiff"}]
            carrier_visuals = "".join(f'<figure class="editor-visual"><img src="{url("/recovery/image",record=carrier["inventory"],choice=i)}" alt=""><figcaption>{e(item["basename"])} · full carrier</figcaption></figure>' for i,item in enumerate(carrier_images,1)) or '<p>Carrier has no linked local image.</p>'
            siblings = " · ".join(f'<a href="{url("/compare",record=item)}">{e(item)}</a>' for item in carrier["components"])
            carrier_html = f'<section class="panel"><p class="eyebrow">Physical carrier</p><h2><a href="{url("/compare",record=carrier["inventory"])}">{e(carrier["inventory"])}</a></h2><p>The A3 sheet is evidence context. Its dimensions and publication metadata are not component metadata.</p><div class="editor-visual-grid">{carrier_visuals}</div><p><strong>Components:</strong> {siblings}</p></section>'
        elif record["record_kind"] == "carrier":
            children = "".join(f'<li><a href="{url("/compare",record=item)}">{e(item)}</a></li>' for item in record["components"])
            carrier_html = f'<section class="panel"><p class="eyebrow">Carrier structure</p><h2>{len(record["components"])} component photographs</h2><ul>{children}</ul><p>Carrier metadata remains separate from component metadata.</p></section>'
        candidate_options = "".join(
            option(
                inventory,
                f"{inventory} — {self.server.recovery_records[inventory]['local'].get('title') or self.server.recovery_records[inventory]['md'].get('object_name') or 'Untitled'}",
            )
            for inventory in sorted(self.server.recovery_records)
            if inventory != record["inventory"]
        )
        clear_assignment = (
            f'''<form class="form form--narrow" method="post" action="/recovery/carrier" data-async-save>{self.csrf()}
<input type="hidden" name="record" value="{e(record['inventory'])}"><input type="hidden" name="mode" value="clear_parent">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet" type="submit">Remove component assignment</button></form>'''
            if record["carrier_id"]
            else ""
        )
        carrier_controls = f'''<section class="panel"><p class="eyebrow">Editor-confirmed structure</p><h2>Identify carrier and components</h2>
<p>Choose records by looking at the images. Filenames are not used to create these relationships.</p>
<form class="form form--narrow" method="post" action="/recovery/carrier" data-async-save>{self.csrf()}
<input type="hidden" name="record" value="{e(record['inventory'])}"><input type="hidden" name="mode" value="set_parent">
<label>This record is a component of<select name="related_inventory" required><option value="">Choose the larger carrier…</option>{candidate_options}</select></label>
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Assign carrier</button></form>
{clear_assignment}
<form class="form form--narrow" method="post" action="/recovery/carrier" data-async-save>{self.csrf()}
<input type="hidden" name="record" value="{e(record['inventory'])}"><input type="hidden" name="mode" value="add_child">
<label>Assign a component to this carrier<select name="related_inventory" required><option value="">Choose the component…</option>{candidate_options}</select></label>
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Assign component</button></form></section>'''
        rejection_controls = ""
        if record["review_state"] != "rejected":
            rejection_controls = f'''<section class="panel"><p class="eyebrow">Identity disposition</p><h2>Reject or retire a duplicate record</h2>
<p>This hides the record from the working queue and records it for future drain review. It does not delete, move, or modify any source file and does not authorize later deletion.</p>
<form class="form form--narrow" method="post" action="/recovery/review-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="status" value="rejected">
<label>Retained record, when this is a duplicate<select name="related_inventory"><option value="">No retained duplicate identified</option>{candidate_options}</select></label>
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet" type="submit">Reject and hide record</button></form></section>'''
        tags = [
            str(item.get("tag_name") if isinstance(item, dict) else item).strip()
            for item in (md.get("object_tags") or [])
            if str(item.get("tag_name") if isinstance(item, dict) else item).strip()
        ]
        decision_html = '<section class="notice notice--yellow"><h2>Reconciliation is not prepared</h2><p>Run the bounded preparation command before recording decisions.</p></section>'
        metadata_controls = ""
        completion_controls = '<section class="notice notice--yellow"><strong>Visual review</strong><p>Confirm the visual identity and exact master or surrogate, or skip this record.</p></section>'
        try:
            with closing(self.campaign(read_only=True)) as campaign:
                campaign_record = campaign.execute(
                    """SELECT * FROM reconciliation_records
                       WHERE campaign_id = 'MD-RECOVERY-WF-PARAGON'
                         AND inventory_label = ?""",
                    (record["inventory"],),
                ).fetchone()
                bundle = (
                    reconciliation.record_bundle(
                        campaign, str(campaign_record["record_id"])
                    )
                    if campaign_record
                    else None
                )
        except reconciliation.ReconciliationError:
            bundle = None
        if bundle:
            campaign_record = bundle["record"]
            if record["review_state"] == "rejected":
                retained_inventory = str(
                    record["review_detail"].get("retained_inventory") or ""
                )
                retained_note = (
                    f'Retained record: <a href="{url("/compare", record=retained_inventory)}">{e(retained_inventory)}</a>.'
                    if retained_inventory
                    else "No retained duplicate was identified."
                )
                reason_note = (
                    f'<br>Reason: {e(record["review_detail"].get("reason"))}'
                    if record["review_detail"].get("reason")
                    else ""
                )
                completion_controls = f'''<section class="notice notice--yellow"><strong>Rejected — future drain review only</strong><p>This record is hidden from the working queue. {retained_note} Its source evidence remains untouched and no deletion is authorized.{reason_note}</p>
<form class="form form--narrow" method="post" action="/recovery/review-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="status" value="not_ready">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet" type="submit">Restore to work queue</button></form></section>'''
            elif record["queue_state"] == "reconciled":
                completion_controls = f'''<section class="notice notice--safe"><strong>Reconciled</strong><p>This record is hidden from the default work queue. It remains available under Reconciled and All records.</p>
<form class="form form--narrow" method="post" action="/recovery/review-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="status" value="not_ready">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet" type="submit">Reopen reconciliation</button></form></section>'''
            elif record["queue_state"] == "ready":
                completion_controls = '''<section class="notice notice--safe"><strong>Ready for accession</strong><p>The visual identity and exact representation are bound. Leave this record in the queue for the later reviewed batch.</p></section>'''
            elif campaign_record["identity_status"] == "resolved":
                completion_controls = '''<section class="notice notice--yellow"><strong>Visual review incomplete</strong><p>Choose the exact master or best-available surrogate. Do not mark the record reconciled manually.</p></section>'''
            else:
                missing_note = (
                    "Museum-digital evidence is missing. Confirm the exact local source identity and accept its local metadata before accession."
                    if record["md_state"] != "found"
                    else " Resolve visual identity before finishing this record."
                )
                completion_controls = f'<section class="notice notice--yellow"><strong>Visual review</strong><p>{e(missing_note.strip())}</p></section>'
            if record.get("correction_required"):
                correction_reason = e(record.get("correction_reason"))
                if record["correction_state"] == "complete":
                    completion_controls = f'''<section class="notice notice--safe"><strong>Corrective accession complete — metadata locked</strong><p>The active representation and embedded metadata are reconciled. Historical local-source values remain visible as read-only evidence; they are not the archive value.</p>
<form class="form form--narrow" method="post" action="/recovery/correction-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="state" value="open">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet" type="submit">Reopen correction to edit</button></form></section>'''
                elif record["queue_state"] == "correction_ready":
                    completion_controls = f'''<section class="notice notice--safe"><strong>Ready for corrective re-accession</strong><p>{correction_reason}</p><p>The catalog correction is reviewed. The existing TIFF is not yet treated as corrected; it remains queued for controlled regeneration and replacement.</p>
<form class="form form--narrow" method="post" action="/recovery/corrective-publish" onsubmit="return confirm('Run the controlled corrective re-accession now? A verified rollback backup will be created first.')">{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}">
<input type="hidden" name="reason" value="{correction_reason}">
<button class="button" type="submit">Run corrective re-accession</button></form>
<form class="form form--narrow" method="post" action="/recovery/correction-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="state" value="open">
<button class="button button--quiet" type="submit">Reopen correction</button></form></section>'''
                else:
                    completion_controls = f'''<section class="notice notice--yellow"><strong>Correction review</strong><p>{correction_reason}</p><p>When the catalog correction and source choice are reviewed, move this record to the corrective re-accession queue. This does not claim that the existing TIFF is already corrected.</p>
<form class="form form--narrow" method="post" action="/recovery/correction-status" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}"><input type="hidden" name="state" value="ready">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Mark correction ready</button></form></section>'''
            with closing(self.catalog(read_only=True)) as catalog:
                entities = list(
                    catalog.execute(
                        """SELECT e.entity_id, o.archival_label AS value,
                                  o.descriptive_title AS title
                           FROM entities e JOIN archive_objects o
                             ON o.entity_id = e.entity_id
                           WHERE e.kind='archive_object' AND e.status='active'
                             AND o.status='active'
                           ORDER BY o.archival_label, e.entity_id"""
                    )
                )
                entities = [
                    entity
                    for entity in entities
                    if str(entity["value"] or "")
                    in self.server.recovery_records
                ]
                accepted_extra = editor_reconcile.accepted_metadata(
                    catalog, str(campaign_record["entity_id"])
                )
                archive_row = catalog.execute(
                    """SELECT descriptive_title, description,
                              material_technique, dimensions
                       FROM archive_objects WHERE entity_id = ?""",
                    (campaign_record["entity_id"],),
                ).fetchone()
                if archive_row:
                    archive_values.update(dict(archive_row))
                archive_values.update(accepted_extra)
                candidate_reviews = catalog_core.source_candidate_reviews(
                    catalog, entity_id=str(campaign_record["entity_id"])
                )
            preferred_parent = ""
            if record["carrier_id"]:
                for entity in entities:
                    if str(entity["value"] or "") == record["carrier_id"]:
                        preferred_parent = str(entity["entity_id"])
                        break
            identity = self.render_identity_form(
                campaign_record,
                entities,
                bundle["identity_decisions"],
                preferred_parent=preferred_parent,
                return_to=url("/compare", record=record["inventory"]),
            )
            local_evidence = [
                item
                for item in bundle["visuals"]
                if item["role"] != "museum_digital_snapshot"
            ]
            local_digests: dict[str, int] = {}
            for item in local_evidence:
                digest = str(item["content_digest"] or "")
                local_digests[digest] = local_digests.get(digest, 0) + 1
            tiff_master_selected = any(
                review.get("state") == "selected_master"
                and review.get("candidate_role") == "master_tiff_candidate"
                for review in candidate_reviews.values()
            )
            has_tiff_candidate = any(
                Path(str(item["source_locator"] or "")).suffix.casefold()
                in {".tif", ".tiff"}
                for item in local_evidence
            )
            visual_confirmation_ready = recovery_visual_confirmation_ready(
                bundle["visuals"]
            )
            visual_cards: list[str] = []
            for item in bundle["visuals"]:
                if item["role"] == "museum_digital_snapshot":
                    visual_cards.append(
                        self.render_visual(
                            item,
                            display_role="Published evidence",
                            display_label="museum-digital published image",
                        )
                    )
                    continue
                candidate_role, candidate_label = self._recovery_candidate_role(
                    record, item, local_evidence
                )
                review = candidate_reviews.get(str(item["evidence_id"]), {})
                visual_cards.append(
                    self.render_visual(
                        item,
                        display_role="Local candidate",
                        display_label=candidate_label,
                        review_state=str(review.get("state") or "candidate"),
                        exact_duplicate=(
                            local_digests.get(str(item["content_digest"] or ""), 0)
                            > 1
                        ),
                        candidate_role=candidate_role,
                        tiff_master_selected=tiff_master_selected,
                        fast_confirm=(
                            campaign_record["identity_status"] != "resolved"
                            and (
                                visual_confirmation_ready
                                or (
                                    not str(campaign_record["external_id"] or "")
                                    and campaign_record["source_record_status"]
                                    == "exact_inventory"
                                )
                            )
                            and item["status"] == "available"
                            and bool(item["cache_relpath"])
                        ),
                        return_to=url("/compare", record=record["inventory"]),
                    )
                )
            visual_strip = "".join(visual_cards)
            tiff_notice = (
                ""
                if has_tiff_candidate
                else '<div class="notice notice--safe"><strong>No TIFF master was found; a reviewed JPEG surrogate is allowed.</strong><p>Select the best high-resolution JPEG as the surrogate source. Accession will create a metadata-enriched preservation-surrogate TIFF, retain the exact JPEG source, and record the missing TIFF as a provenance gap. Missing TIFF does not block accession.</p></div>'
            )
            decisions_by_field: dict[str, sqlite3.Row] = {}
            for item in reversed(bundle["field_decisions"]):
                decisions_by_field[str(item["field_key"])] = item
            fields_by_key: dict[str, list[sqlite3.Row]] = {}
            for item in bundle["fields"]:
                fields_by_key.setdefault(str(item["field_key"]), []).append(item)
            field_forms = "".join(
                self.render_field(
                    campaign_record,
                    key,
                    candidates,
                    decisions_by_field.get(key),
                    return_to=url("/compare", record=record["inventory"]),
                )
                for key, candidates in fields_by_key.items()
            )
            md_tags = "; ".join(tags)
            local_tags = [
                str(value).strip()
                for value in (local.get("tags") or [])
                if str(value).strip()
            ]
            extra = f"""<section class="panel metadata-editor"><p class="eyebrow">Archive wording</p><h2>Object type and tags</h2>
<p>Current accepted object type: {e(accepted_extra['object_type']) or 'Not accepted yet'}<br>Current accepted tags: {e('; '.join(accepted_extra['tags'])) or 'Not accepted yet'}</p>
<form class="form" method="post" action="/recovery/metadata-extra" data-async-save>{self.csrf()}
<input type="hidden" name="inventory" value="{e(record['inventory'])}">
<label>Object type decision<select name="object_type_source"><option value="">Leave unchanged</option>{option('local','Use local value')}{option('museum_digital','Use museum-digital value')}{option('corrected','Write a better value')}</select></label>
<p class="muted">Local: {e(local.get('object_type')) or 'Not supplied'} · museum-digital: {e(md.get('object_type')) or 'Not supplied'}</p>
<label>Corrected object type<input name="corrected_object_type"></label>
<label>Tag decision<select name="tag_source"><option value="">Leave unchanged</option>{option('archive','Keep accepted archive tags')}{option('local','Use local workbook tags')}{option('museum_digital','Use museum-digital tags')}{option('corrected','Write archive tags')}</select></label>
<p class="muted">Local: {e('; '.join(local_tags)) or 'Not supplied'} · museum-digital: {e(md_tags) or 'Not supplied'}</p>
<label>Corrected tags <span class="muted">(one per line or separated by semicolons)</span><textarea name="corrected_tags"></textarea></label>
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Save object type or tags</button></form></section>"""
            selected_master = [
                item for item in local_evidence
                if candidate_reviews.get(str(item["evidence_id"]), {}).get("state")
                == "selected_master"
            ]
            selected_derivative = [
                item for item in local_evidence
                if candidate_reviews.get(str(item["evidence_id"]), {}).get("state")
                == "selected_derivative"
            ]
            available_derivatives = [
                item for item in local_evidence
                if self._recovery_candidate_role(record, item, local_evidence)[0]
                == "local_derivative"
                and candidate_reviews.get(str(item["evidence_id"]), {}).get("state")
                != "redundant"
            ]
            generated_derivative_notice = (
                '<div class="notice notice--safe"><strong>Access JPEG will be generated.</strong><p>The selected TIFF is the master and every supplied local derivative was rejected. Accession will create a full-resolution JPEG in writable pilot storage; no protected source will be changed.</p></div>'
                if not available_derivatives
                and selected_master
                and Path(str(selected_master[0]["source_locator"])).suffix.casefold()
                in {".tif", ".tiff"}
                else ""
            )
            if campaign_record["identity_status"] == "resolved":
                decision_html = f'<section class="visual-case"><p class="eyebrow">Step 1</p><h2>Reconcile identity and local candidates</h2><p>Compare the local recovery candidates with the published MD image. Redundant marks are logged only; no source file is changed or deleted.</p>{tiff_notice}<div class="visual-strip">{visual_strip}</div>{generated_derivative_notice}{identity}</section>'
                config = catalog_core.load_config(self.root)
                independent = str(config.get("independent_generation_root") or "")
                master_source = (
                    str(selected_master[0]["source_locator"])
                    if selected_master
                    else ""
                )
                derivative_ready = recovery_derivative_ready(
                    master_source,
                    available_derivatives=len(available_derivatives),
                    selected_derivatives=len(selected_derivative),
                )
                ready = (
                    self.server.recovery_accession_plan(
                        record["inventory"]
                    )["state"]
                    == "ready"
                )
                selected_master_is_tiff = bool(
                    selected_master
                    and Path(str(selected_master[0]["source_locator"])).suffix.casefold()
                    in {".tif", ".tiff"}
                )
                selection_text = "Select one local master candidate above."
                if selected_master:
                    selection_text = (
                        "Master: " if selected_master_is_tiff else "Surrogate source: "
                    ) + e(Path(str(selected_master[0]["source_locator"])).name)
                if selected_derivative:
                    selection_text += f" · Derivative: {e(Path(str(selected_derivative[0]['source_locator'])).name)}"
                elif available_derivatives and selected_master_is_tiff:
                    selection_text += " · One local derivative still needs a decision below."
                elif selected_master_is_tiff:
                    selection_text += (
                        " · A full-resolution JPEG access derivative will be "
                        "generated from the selected TIFF."
                    )
                derivative_resolution = ""
                if (
                    selected_master_is_tiff
                    and available_derivatives
                    and not selected_derivative
                ):
                    derivative_actions = []
                    for item in available_derivatives:
                        derivative_name = Path(str(item["source_locator"])).name
                        common = f'''{self.csrf()}
<input type="hidden" name="record_id" value="{e(campaign_record['record_id'])}">
<input type="hidden" name="evidence_id" value="{e(item['evidence_id'])}">
<input type="hidden" name="return_to" value="{e(url('/compare', record=record['inventory']))}">'''
                        derivative_actions.append(f'''<div class="candidate-transition"><p><strong>{e(derivative_name)}</strong> · {e(item['width_px'])} × {e(item['height_px'])} px</p>
<form method="post" action="/recovery/candidate-review" data-async-save>{common}<input type="hidden" name="state" value="selected_derivative"><button class="button button--small" type="submit">Keep as access derivative</button></form>
<form method="post" action="/recovery/candidate-review" data-async-save>{common}<input type="hidden" name="state" value="redundant"><button class="button button--quiet button--small" type="submit">Mark redundant</button></form></div>''')
                    derivative_resolution = f'''<div class="notice notice--yellow"><strong>Decide the remaining local JPEG before accession.</strong><p>This—not the missing TIFF—is the current blocker. Keep it as the access derivative or mark it as redundant evidence.</p>{''.join(derivative_actions)}</div>'''
                batch_state = (
                    '<div class="notice notice--safe"><strong>Queued for the later accession batch.</strong><p>The reviewed batch will revalidate this exact selection before creating preservation files or publishing the catalog.</p></div>'
                    if ready
                    else '<div class="notice notice--yellow"><strong>Not queued yet.</strong><p>Finish the representation choice above; uncertain records should be skipped.</p></div>'
                )
                accession_panel = f'''<section class="panel accession-action"><p class="eyebrow">Batch state</p><h2>Mechanical accession happens later</h2>
<p>{selection_text}</p>{derivative_resolution}{batch_state}</section>'''
                metadata_controls = f'<section class="metadata-editor"><p class="eyebrow">Step 2</p><h2>Review or correct archive metadata</h2>{field_forms}</section>{extra}{accession_panel}'
            else:
                metadata_notice = (
                    '<section class="notice notice--safe"><strong>Exact museum-digital metadata is already assigned where available.</strong><p>Visual identity remains separate. Confirm the local representation or carrier relationship; corrections to archive wording open after that review.</p></section>'
                    if record["md_state"] == "found"
                    else '<section class="notice notice--yellow"><strong>No exact museum-digital metadata is available.</strong><p>Resolve the local identity and normalize this record manually.</p></section>'
                )
                decision_html = f'<section class="visual-case"><p class="eyebrow">Step 1</p><h2>Reconcile identity and local candidates</h2><p>Compare the local recovery candidates with the published MD image. Redundant marks are logged only; no source file is changed or deleted.</p>{tiff_notice}<div class="visual-strip">{visual_strip}</div>{generated_derivative_notice}{identity}</section>{metadata_notice}'
        accepted_cell = lambda value: (
            f'<strong class="metadata-accepted">{e(value)}</strong>'
            if value
            else '<span class="metadata-missing">Not accepted yet</span>'
        )
        if recovery_correction_is_locked(record["correction_state"]):
            decision_html = f"<fieldset disabled>{decision_html}</fieldset>"
            metadata_controls = (
                f"<fieldset disabled>{metadata_controls}</fieldset>"
                if metadata_controls
                else ""
            )
            carrier_controls = f"<fieldset disabled>{carrier_controls}</fieldset>"
            rejection_controls = (
                f"<fieldset disabled>{rejection_controls}</fieldset>"
                if rejection_controls
                else ""
            )
        review_label = {
            "visual_review": "visual review",
            "ready": "ready for accession",
            "correction_ready": "ready for corrective re-accession",
            "reconciled": "reconciled",
            "rejected": "rejected",
        }.get(record["queue_state"], "visual review")
        if record["review_state"] == "rejected":
            carrier_controls = f'<fieldset disabled>{carrier_controls}</fieldset>'
            decision_html = f'<fieldset disabled>{decision_html}</fieldset>'
            metadata_controls = f'<fieldset disabled>{metadata_controls}</fieldset>' if metadata_controls else ""
        workbook = local.get("workbook") if isinstance(local.get("workbook"), dict) else {}
        workbook_values = workbook.get("values") if isinstance(workbook.get("values"), dict) else {}
        workbook_rows = "".join(
            f"<tr><th>{e(key)}</th><td>{e(value)}</td></tr>"
            for key, value in workbook_values.items()
            if str(value).strip()
        )
        workbook_detail = (
            f'<details class="panel"><summary>Complete historical workbook row</summary><p class="technical">{e(workbook.get("path"))} · {e(workbook.get("sheet"))} row {e(workbook.get("row"))} · SHA-256 {e(workbook.get("sha256"))}</p><div class="table-wrap"><table><tbody>{workbook_rows}</tbody></table></div></details>'
            if workbook_rows
            else ""
        )
        body = f'<section class="page-heading"><p class="eyebrow">Paragon recovery record</p><h1>{e(record["inventory"])}</h1><p><strong>{e(archive_values["descriptive_title"])}</strong></p><p>{badge(review_label)} {badge(record["record_kind"])} {badge(record["md_state"])}</p></section>{completion_controls}{decision_html}<section class="panel"><h2>Metadata comparison</h2><p>The green column is the current authoritative archive value. Historical local-source and museum-digital columns remain unchanged, read-only evidence.</p><div class="table-wrap"><table><thead><tr><th>Field</th><th>Historical local source</th><th>museum-digital</th><th>Archive accepted</th></tr></thead><tbody><tr><td>Title</td><td>{self._metadata_cell(local.get("title"))}</td><td>{self._metadata_cell(md.get("object_name"))}</td><td>{accepted_cell(archive_values["descriptive_title"])}</td></tr><tr><td>Description</td><td>{self._metadata_cell(local.get("description"))}</td><td>{self._metadata_cell(md.get("object_description"))}</td><td>{accepted_cell(archive_values["description"])}</td></tr><tr><td>Object type</td><td>{self._metadata_cell(local.get("object_type"))}</td><td>{self._metadata_cell(md.get("object_type"))}</td><td>{accepted_cell(archive_values["object_type"])}</td></tr><tr><td>Material</td><td>{self._metadata_cell(local.get("material_technique"))}</td><td>{self._metadata_cell(md.get("object_material_technique"))}</td><td>{accepted_cell(archive_values["material_technique"])}</td></tr><tr><td>Dimensions</td><td>{self._metadata_cell(local.get("dimensions"))}</td><td>{self._metadata_cell(md.get("object_dimensions"))}</td><td>{accepted_cell(archive_values["dimensions"])}</td></tr><tr><td>Tags</td><td>{self._metadata_cell("; ".join(local_tags))}</td><td>{e(", ".join(tags)) or "Not supplied"}</td><td>{accepted_cell("; ".join(archive_values["tags"]))}</td></tr></tbody></table></div></section>{workbook_detail}{metadata_controls}{carrier_html}{carrier_controls}{rejection_controls}'
        self.send_html(record["inventory"], body, active="compare")

    def page_recovery_image(self) -> None:
        record = self._recovery_record(); images = [item for item in record["files"] if str(item["extension"]).casefold() in {"jpg","jpeg","png","webp","tif","tiff"}]
        try: item = images[int(self.parameters().get("choice","1"))-1]
        except (ValueError, IndexError): raise reconciliation.ReconciliationError("image choice is invalid") from None
        source = self._safe_editor_source(Path(item["path"])); browser = self._browser_image(source, record["inventory"], "recovery")
        self.send_bytes(browser.read_bytes(), mimetypes.guess_type(browser.name)[0] or "image/jpeg")

    def page_recovery_active_image(self) -> None:
        record = self._recovery_record()
        with closing(self.catalog(read_only=True)) as catalog:
            row = catalog.execute(
                """SELECT fo.relative_path
                   FROM archive_objects ao
                   JOIN entity_files ef ON ef.entity_id=ao.entity_id
                   JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
                   WHERE ao.archival_label=? AND ao.status='active'
                     AND ef.status='active' AND fo.status='verified'
                     AND ef.role IN (
                         'access_derivative',
                         'preservation_master',
                         'preservation_surrogate'
                     )
                   ORDER BY CASE ef.role
                     WHEN 'access_derivative' THEN 0
                     WHEN 'preservation_master' THEN 1
                     ELSE 2 END
                   LIMIT 1""",
                (record["inventory"],),
            ).fetchone()
        if not row:
            raise reconciliation.ReconciliationError(
                "record has no active archive representation"
            )
        source = self._safe_editor_source(
            self.root / str(row["relative_path"])
        )
        browser = self._browser_image(
            source, record["inventory"], "active-representation"
        )
        self.send_bytes(
            browser.read_bytes(),
            mimetypes.guess_type(browser.name)[0] or "image/jpeg",
        )

    @staticmethod
    def _surrogate_swap_key(candidate: dict[str, Any]) -> str:
        payload = "|".join(
            (
                str(candidate.get("path") or ""),
                str(candidate.get("size_bytes") or ""),
                str(candidate.get("mtime_ns") or ""),
            )
        ).encode("utf-8")
        return "SWAP-" + hashlib.sha256(payload).hexdigest().upper()

    @staticmethod
    def _aspect_ratio(width: Any, height: Any) -> str:
        width_int = int(width or 0)
        height_int = int(height or 0)
        if width_int <= 0 or height_int <= 0:
            return "unknown"
        return f"{width_int / height_int:.4f}"

    def _surrogate_swap_active_rows(self) -> dict[str, dict[str, Any]]:
        with closing(self.catalog(read_only=True)) as catalog:
            return {
                str(row["archival_label"]): dict(row)
                for row in catalog.execute(
                    """SELECT ao.entity_id, ao.archival_label,
                              ao.descriptive_title, ef.role,
                              fo.relative_path, fo.original_filename,
                              fo.content_digest, cb.size_bytes,
                              cb.width_px, cb.height_px
                       FROM archive_objects ao
                       JOIN entity_files ef ON ef.entity_id=ao.entity_id
                       JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
                       JOIN content_blobs cb ON cb.content_digest=fo.content_digest
                       WHERE ao.status='active'
                         AND ef.status='active'
                         AND ef.role='preservation_surrogate'
                         AND fo.status='verified'
                       ORDER BY ao.archival_label"""
                )
            }

    def _surrogate_swap_candidate(
        self,
        inventory: str,
        candidate_key: str,
    ) -> dict[str, Any]:
        for candidate in self.server.surrogate_swap_candidates.get(inventory, []):
            if self._surrogate_swap_key(candidate) == candidate_key:
                return candidate
        raise reconciliation.ReconciliationError(
            "surrogate-swap candidate is outside the bound source scan"
        )

    def _surrogate_swap_source(self, candidate: dict[str, Any]) -> Path:
        source = self._safe_editor_source(Path(str(candidate["path"])))
        stat = source.stat()
        if (
            stat.st_size != int(candidate["size_bytes"])
            or stat.st_mtime_ns != int(candidate["mtime_ns"])
        ):
            raise reconciliation.ReconciliationError(
                "surrogate-swap candidate changed after source scan"
            )
        return source

    def page_surrogate_swap_image(self) -> None:
        parameters = self.parameters()
        inventory = parameters.get("inventory", "").strip()
        side = parameters.get("side", "")
        if side == "current":
            active = self._surrogate_swap_active_rows().get(inventory)
            if not active:
                raise reconciliation.ReconciliationError(
                    "current surrogate is unavailable"
                )
            source = self._safe_editor_source(
                self.root / str(active["relative_path"])
            )
            kind = "surrogate-current"
        elif side == "candidate":
            candidate = self._surrogate_swap_candidate(
                inventory,
                parameters.get("candidate", "").strip(),
            )
            source = self._surrogate_swap_source(candidate)
            kind = "surrogate-candidate"
        else:
            raise reconciliation.ReconciliationError(
                "surrogate-swap image side is invalid"
            )
        browser = self._browser_image(source, inventory, kind)
        self.send_bytes(
            browser.read_bytes(),
            mimetypes.guess_type(browser.name)[0] or "image/jpeg",
        )

    def page_surrogate_swaps(self) -> None:
        if not self.server.surrogate_swap_source:
            self.send_html(
                "Surrogate swaps",
                '<section class="notice notice--yellow"><h1>No candidate source bound</h1><p>Start the workbench with --surrogate-swap-source pointing at a read-only TIFF source tree.</p></section>',
                active="surrogate_swaps",
            )
            return
        parameters = self.parameters()
        term = parameters.get("q", "").casefold()
        state_filter = parameters.get("state", "candidate")
        if state_filter not in {"candidate", "marked_for_swap", "rejected", "all"}:
            state_filter = "candidate"
        active = self._surrogate_swap_active_rows()
        with closing(self.catalog(read_only=True)) as catalog:
            reviews = catalog_core.surrogate_swap_reviews(catalog)
        cards: list[str] = []
        counts = {
            "candidate": 0,
            "marked_for_swap": 0,
            "rejected": 0,
            "all": 0,
        }
        for inventory in sorted(set(active) & set(self.server.surrogate_swap_candidates)):
            row = active[inventory]
            searchable = " ".join(
                (
                    inventory,
                    str(row.get("descriptive_title") or ""),
                    str(row.get("original_filename") or ""),
                )
            ).casefold()
            if term and term not in searchable:
                continue
            for candidate in self.server.surrogate_swap_candidates[inventory]:
                key = self._surrogate_swap_key(candidate)
                review = reviews.get(key, {})
                review_state = str(review.get("state") or "candidate")
                counts["all"] += 1
                counts[review_state] = counts.get(review_state, 0) + 1
                if state_filter != "all" and review_state != state_filter:
                    continue
                source = self._surrogate_swap_source(candidate)
                candidate_width, candidate_height = self._image_dimensions(source)
                current_ratio = self._aspect_ratio(
                    row.get("width_px"),
                    row.get("height_px"),
                )
                candidate_ratio = self._aspect_ratio(
                    candidate_width,
                    candidate_height,
                )
                current_image = url(
                    "/surrogate-swap/image",
                    inventory=inventory,
                    side="current",
                )
                candidate_image = url(
                    "/surrogate-swap/image",
                    inventory=inventory,
                    side="candidate",
                    candidate=key,
                )
                common = f'''{self.csrf()}
<input type="hidden" name="inventory" value="{e(inventory)}">
<input type="hidden" name="candidate_key" value="{e(key)}">'''
                controls = f'''<div class="candidate-disposition">
<p>{badge(review_state)} {e(review.get("reason", ""))}</p>
<form method="post" action="/surrogate-swap/review" data-async-save>{common}
<input type="hidden" name="state" value="marked_for_swap">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--small" type="submit">Mark for later swap</button>
</form>
<form method="post" action="/surrogate-swap/review" data-async-save>{common}
<input type="hidden" name="state" value="rejected">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet button--small" type="submit">Reject candidate</button>
</form>
<form method="post" action="/surrogate-swap/review" data-async-save>{common}
<input type="hidden" name="state" value="candidate">
<button class="button button--quiet button--small" type="submit">Restore as candidate</button>
</form></div>'''
                cards.append(f'''<article class="panel surrogate-swap-card">
<header><p class="eyebrow">Surrogate replacement review</p><h2>{e(inventory)}</h2><p>{e(row.get("descriptive_title") or "Untitled")}</p></header>
<div class="surrogate-swap-pair">
<figure class="visual-card"><header><p class="eyebrow">Current active surrogate</p><h3>{e(row["original_filename"])}</h3></header><a class="visual-card__image" href="{current_image}"><img src="{current_image}" alt=""></a><figcaption>{human_size(row["size_bytes"])} · {e(row["width_px"])} × {e(row["height_px"])} px · aspect {e(current_ratio)}<br><span class="technical">{e(row["content_digest"])}</span></figcaption></figure>
<figure class="visual-card"><header><p class="eyebrow">Candidate TIFF master</p><h3>{e(candidate["basename"])}</h3></header><a class="visual-card__image" href="{candidate_image}"><img src="{candidate_image}" alt=""></a><figcaption>{human_size(candidate["size_bytes"])} · {candidate_width or "?"} × {candidate_height or "?"} px · aspect {e(candidate_ratio)}<br><span class="technical">{e(candidate["path"])}</span></figcaption></figure>
</div>{controls}</article>''')
        menu = f'''<nav class="button-row" aria-label="Surrogate swap states">
<a class="button{' button--quiet' if state_filter != 'candidate' else ''}" href="{url('/surrogate-swaps', state='candidate')}">Candidates ({counts['candidate']})</a>
<a class="button{' button--quiet' if state_filter != 'marked_for_swap' else ''}" href="{url('/surrogate-swaps', state='marked_for_swap')}">Marked ({counts['marked_for_swap']})</a>
<a class="button{' button--quiet' if state_filter != 'rejected' else ''}" href="{url('/surrogate-swaps', state='rejected')}">Rejected ({counts['rejected']})</a>
<a class="button{' button--quiet' if state_filter != 'all' else ''}" href="{url('/surrogate-swaps', state='all')}">All ({counts['all']})</a></nav>'''
        body = f'''<section class="page-heading"><p class="eyebrow">WF-PARAGON source upgrade review</p><h1>Surrogate swaps</h1><p>Compare each current active preservation surrogate with same-label TIFF candidates from the bound read-only source tree. Marked records are queued only for later batch planning; this page performs no accession and changes no source file.</p><p class="technical">Source: {e(self.server.surrogate_swap_source)}</p></section>{menu}
<form class="form-grid editor-filters" method="get"><input type="hidden" name="state" value="{e(state_filter)}"><label>Search<input name="q" value="{e(parameters.get("q", ""))}"></label><button class="button">Filter</button></form>
<section class="surrogate-swap-list">{''.join(cards) or '<div class="empty">No surrogate swap candidates in this view.</div>'}</section>'''
        self.send_html("Surrogate swaps", body, active="surrogate_swaps")

    def visual_candidate(
        self,
        unit_id: str,
        choice: str,
    ) -> dict[str, Any]:
        query = self.server.visual_candidate_queries.get(unit_id)
        if not query:
            raise reconciliation.ReconciliationError(
                "no visual-candidate search is bound to this unit"
            )
        try:
            index = int(choice) - 1
            candidate = query["candidates"][index]
        except (ValueError, IndexError):
            raise reconciliation.ReconciliationError(
                "visual candidate is outside this unit's bounded result"
            ) from None
        if index < 0:
            raise reconciliation.ReconciliationError(
                "visual candidate is outside this unit's bounded result"
            )
        return candidate

    def visual_candidate_source(
        self,
        unit_id: str,
        choice: str,
    ) -> tuple[dict[str, Any], Path]:
        candidate = self.visual_candidate(unit_id, choice)
        source = self._safe_editor_source(Path(str(candidate["path"])))
        stat = source.stat()
        if (
            stat.st_size != int(candidate["size_bytes"])
            or stat.st_mtime_ns != int(candidate["mtime_ns"])
        ):
            raise reconciliation.ReconciliationError(
                "visual candidate changed after indexing"
            )
        return candidate, source

    def editor_case_context(self, unit_id: str) -> dict[str, Any]:
        case = self.stress_case(unit_id)
        with closing(self.catalog(read_only=True)) as connection:
            core = connection.execute(
                """SELECT u.unit_id, u.title AS unit_title, e.entity_id,
                          a.archival_label, a.descriptive_title, a.description,
                          a.material_technique, a.dimensions,
                          a.publication_status
                   FROM units u
                   JOIN entity_unit_memberships m ON m.unit_id = u.unit_id
                   JOIN entities e ON e.entity_id = m.entity_id
                   JOIN archive_objects a ON a.entity_id = e.entity_id
                   WHERE u.unit_id = ? AND m.role = 'primary'""",
                (unit_id,),
            ).fetchone()
            if not core:
                raise reconciliation.ReconciliationError(
                    f"stress-pilot unit has no archive object: {unit_id}"
                )
            archive_file = connection.execute(
                """SELECT ef.role, ef.status, fo.relative_path,
                          fo.original_filename, fo.content_digest,
                          b.size_bytes, b.media_type, b.width_px, b.height_px
                   FROM entity_files ef
                   JOIN file_occurrences fo
                     ON fo.occurrence_id = ef.occurrence_id
                   JOIN content_blobs b
                     ON b.content_digest = fo.content_digest
                   WHERE ef.entity_id = ? AND ef.status = 'active'
                     AND ef.role IN (
                         'preservation_master',
                         'preservation_surrogate'
                     )
                   ORDER BY CASE ef.role
                       WHEN 'preservation_master' THEN 1 ELSE 2 END
                   LIMIT 1""",
                (core["entity_id"],),
            ).fetchone()
            master_exists = bool(
                connection.execute(
                    """SELECT 1 FROM entity_files
                       WHERE entity_id = ?
                         AND role = 'preservation_master'
                         AND status = 'active'""",
                    (core["entity_id"],),
                ).fetchone()
            )
            accepted = editor_reconcile.accepted_metadata(
                connection,
                str(core["entity_id"]),
            )
        with closing(self.retained_index()) as retained:
            local = retained.execute(
                """SELECT inventory_number, title, description, object_type,
                          collection, preferred_source
                   FROM objects WHERE inventory_number = ?""",
                (case["inventory_label"],),
            ).fetchone()
            md = (
                retained.execute(
                    """SELECT md_id, object_name, object_description,
                              object_type, tags_json, raw_json, url
                       FROM online_md_objects WHERE md_id = ?""",
                    (case.get("museum_digital_id"),),
                ).fetchone()
                if case.get("museum_digital_id")
                else None
            )
            md_image = (
                retained.execute(
                    """SELECT quell_id, name, is_main, folder, filename_loc,
                              rights, owner, creator
                       FROM online_md_images WHERE md_id = ?
                       ORDER BY CASE lower(is_main)
                           WHEN 'j' THEN 0 WHEN '1' THEN 0 ELSE 1 END,
                           id
                       LIMIT 1""",
                    (case.get("museum_digital_id"),),
                ).fetchone()
                if case.get("museum_digital_id")
                else None
            )
            derivative = None
            source_text = str(case.get("source_path") or "")
            if case.get("action") == "image_surrogate" and source_text:
                source = Path(source_text).resolve()
                if source.is_file():
                    derivative = {
                        "path": str(source),
                        "basename": source.name,
                        "size": source.stat().st_size,
                    }
            if derivative is None:
                derivative = retained.execute(
                    """SELECT f.path, f.basename, f.size
                       FROM file_object_links l
                       JOIN files f ON f.id = l.file_id
                       WHERE l.inventory_number = ?
                         AND l.confidence = 95
                         AND lower(f.extension) IN (
                             'jpg', 'jpeg', 'png', 'webp'
                         )
                         AND f.path LIKE '/mnt/volume/archive/%'
                       ORDER BY f.size DESC, f.path
                       LIMIT 1""",
                    (case["inventory_label"],),
                ).fetchone()
        tags: list[str] = []
        if md and md["tags_json"]:
            try:
                payload = json.loads(md["tags_json"])
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list):
                tags = [
                    str(item.get("tag_name") or "").strip()
                    for item in payload
                    if isinstance(item, dict)
                    and str(item.get("tag_name") or "").strip()
                ]
        md_material = ""
        md_dimensions = ""
        if md and md["raw_json"]:
            try:
                raw = json.loads(md["raw_json"])
            except json.JSONDecodeError:
                raw = {}
            if isinstance(raw, dict):
                md_material = str(
                    raw.get("object_material_technique") or ""
                ).strip()
                md_dimensions = str(raw.get("object_dimensions") or "").strip()
        return {
            "case": case,
            "core": core,
            "archive_file": archive_file,
            "master_exists": master_exists,
            "accepted_object_type": accepted["object_type"],
            "accepted_tags": accepted["tags"],
            "local": local,
            "derivative": derivative,
            "md": md,
            "md_image": md_image,
            "md_tags": tags,
            "md_material": md_material,
            "md_dimensions": md_dimensions,
        }

    def editor_missing_labels(self, context: dict[str, Any]) -> list[str]:
        case = context["case"]
        core = context["core"]
        missing: list[str] = []
        if not context["master_exists"]:
            missing.append("Original master missing")
        if not context["archive_file"]:
            missing.append("Archive representation missing")
        if not context["derivative"]:
            missing.append("Local derivative missing")
        if not context["md"]:
            missing.append("Museum-digital record missing")
        if not str(core["descriptive_title"] or "").strip():
            missing.append("Title missing")
        if not str(core["description"] or "").strip():
            missing.append("Description missing")
        if not context["accepted_tags"] and not context["md_tags"]:
            missing.append("Tags / keywords missing")
        return missing

    def editor_cases(self) -> list[dict[str, Any]]:
        values = []
        for unit_id in self.server.stress_cases:
            context = self.editor_case_context(unit_id)
            context["missing"] = self.editor_missing_labels(context)
            values.append(context)
        return values

    def _image_dimensions(self, path: Path) -> tuple[int, int]:
        try:
            result = subprocess.run(
                ["magick", "identify", "-format", "%w|%h", str(path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            width, height = result.stdout.strip().split("|", 1)
            return int(width), int(height)
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
        ):
            return 0, 0

    def _safe_editor_source(self, path: Path) -> Path:
        resolved = path.expanduser().resolve(strict=True)
        for root in (*EDITOR_PREVIEW_ROOTS, self.root / "units"):
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                continue
            if resolved != root.resolve() and resolved.is_file():
                return resolved
        raise reconciliation.ReconciliationError(
            "editor preview source is outside protected evidence roots"
        )

    def _recovery_evidence_source(
        self,
        record: dict[str, Any],
        evidence: sqlite3.Row | dict[str, Any],
    ) -> Path:
        """Resolve live evidence, falling back only to identical package bytes."""

        original = Path(str(evidence["source_locator"] or "")).expanduser()
        if original.is_file():
            resolved = original.resolve(strict=True)
            allowed = {
                str(Path(str(item["path"])).resolve())
                for item in record["files"]
            }
            if str(resolved) in allowed:
                return self._safe_editor_source(resolved)
        expected = str(evidence["content_digest"] or "").removeprefix("sha256:")
        if expected:
            for item in record["files"]:
                candidate = Path(str(item["path"]))
                if not candidate.is_file():
                    continue
                digest = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for block in iter(
                        lambda: handle.read(8 * 1024 * 1024), b""
                    ):
                        digest.update(block)
                if digest.hexdigest() == expected:
                    return self._safe_editor_source(candidate)
        raise reconciliation.ReconciliationError(
            "the historical source was retired and no byte-identical active "
            "package fallback is available"
        )

    def _cached_md_image(self, context: dict[str, Any]) -> Path:
        row = context["md_image"]
        if not row:
            raise reconciliation.ReconciliationError(
                "museum-digital preview is unavailable"
            )
        url_value = md_capture._safe_asset_url(dict(row))
        response = md_capture.fetch_asset(url_value, timeout=30.0)
        media_type = str(response["media_type"])
        suffix = md_capture.ALLOWED_MEDIA_TYPES[media_type]
        cache = self.root / "work" / "editor-preview-cache"
        cache.mkdir(parents=True, exist_ok=True)
        destination = cache / (
            f"md-{context['case']['museum_digital_id']}-"
            f"{row['quell_id']}{suffix}"
        )
        if destination.is_file():
            return destination
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(response["body"])
                handle.flush()
                os.fsync(handle.fileno())
            if self._image_dimensions(temporary) == (0, 0):
                raise reconciliation.ReconciliationError(
                    "museum-digital preview is not a readable image"
                )
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _browser_image(self, source: Path, unit_id: str, kind: str) -> Path:
        if source.suffix.casefold() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
        }:
            return source
        stat = source.stat()
        safe_unit = "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in unit_id
        )
        cache = self.root / "work" / "editor-preview-cache"
        cache.mkdir(parents=True, exist_ok=True)
        destination = cache / (
            f"{safe_unit}-{kind}-{stat.st_size}-{stat.st_mtime_ns}.jpg"
        )
        if destination.is_file():
            return destination
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp.jpg"
        )
        try:
            if source.suffix.casefold() in {".tif", ".tiff"}:
                result = subprocess.run(
                    [
                        "vipsthumbnail",
                        str(source),
                        "--size",
                        "1600x1600",
                        "--output",
                        str(temporary),
                        "--vips-concurrency=1",
                        "--vips-disc-threshold=104857600",
                        "--vips-cache-max=100",
                        "--vips-cache-max-memory=268435456",
                        "--vips-cache-max-files=32",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            else:
                result = subprocess.run(
                    [
                        "magick",
                        str(source),
                        "-auto-orient",
                        "-thumbnail",
                        "1600x1600>",
                        "-strip",
                        "-quality",
                        "88",
                        f"jpg:{temporary}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            if result.returncode or not temporary.is_file():
                raise reconciliation.ReconciliationError(
                    "TIFF preview generation failed"
                )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def editor_visuals(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        unit_id = str(context["case"]["unit_id"])
        visuals: list[dict[str, Any]] = []
        archive = context["archive_file"]
        if archive:
            source = (self.root / str(archive["relative_path"])).resolve()
            try:
                source.relative_to(self.root)
            except ValueError as error:
                raise reconciliation.ReconciliationError(
                    "archive preview path escaped the pilot root"
                ) from error
            visuals.append(
                {
                    "kind": "archive",
                    "label": (
                        "Master TIFF"
                        if archive["role"] == "preservation_master"
                        else "Archive surrogate TIFF"
                    ),
                    "status": "available",
                    "name": archive["original_filename"],
                    "size": int(archive["size_bytes"] or 0),
                    "width": int(archive["width_px"] or 0),
                    "height": int(archive["height_px"] or 0),
                    "url": url(
                        "/editor/image",
                        unit=unit_id,
                        kind="archive",
                    ),
                }
            )
        else:
            visuals.append(
                {
                    "kind": "archive",
                    "label": "Master TIFF",
                    "status": "missing",
                    "detail": "No local archive representation exists.",
                }
            )
        derivative = context["derivative"]
        if derivative:
            source = self._safe_editor_source(Path(str(derivative["path"])))
            width, height = self._image_dimensions(source)
            visuals.append(
                {
                    "kind": "local",
                    "label": "Local derivative",
                    "status": "available",
                    "name": source.name,
                    "size": source.stat().st_size,
                    "width": width,
                    "height": height,
                    "url": url(
                        "/editor/image",
                        unit=unit_id,
                        kind="local",
                    ),
                }
            )
        else:
            visuals.append(
                {
                    "kind": "local",
                    "label": "Local derivative",
                    "status": "missing",
                    "detail": "No linked local derivative was found.",
                }
            )
        if context["md_image"]:
            try:
                source = self._cached_md_image(context)
                width, height = self._image_dimensions(source)
                visuals.append(
                    {
                        "kind": "md",
                        "label": "Museum-digital",
                        "status": "available",
                        "name": source.name,
                        "size": source.stat().st_size,
                        "width": width,
                        "height": height,
                        "url": url(
                            "/editor/image",
                            unit=unit_id,
                            kind="md",
                        ),
                    }
                )
            except (
                md_capture.MuseumDigitalCaptureError,
                reconciliation.ReconciliationError,
                OSError,
            ) as error:
                visuals.append(
                    {
                        "kind": "md",
                        "label": "Museum-digital",
                        "status": "unavailable",
                        "detail": str(error),
                    }
                )
        else:
            visuals.append(
                {
                    "kind": "md",
                    "label": "Museum-digital",
                    "status": "missing",
                    "detail": "No museum-digital image is linked.",
                }
            )
        return visuals

    def render_editor_visual(self, item: dict[str, Any]) -> str:
        if item["status"] == "available":
            dimensions = (
                f"{item['width']} × {item['height']} px"
                if item["width"] and item["height"]
                else "Dimensions unavailable"
            )
            quick = (
                f"<strong>{e(item['name'])}</strong>"
                f"<span>{e(human_size(item['size']))} · {e(dimensions)}</span>"
            )
            image = (
                f'<a class="visual-card__image" href="{e(item["url"])}">'
                f'<img loading="lazy" src="{e(item["url"])}" alt=""></a>'
            )
        else:
            quick = (
                f"<strong>{e(item['status'].replace('_', ' ').title())}</strong>"
                f"<span>{e(item.get('detail'))}</span>"
            )
            image = (
                f'<div class="visual-card__missing"><strong>'
                f'{e(item["status"])}</strong><span>'
                f'{e(item.get("detail"))}</span></div>'
            )
        return f"""<article class="visual-card visual-card--{e(item['status'])}">
<header><p class="eyebrow">{e(item['label'])}</p>
<div class="visual-identification">{quick}</div></header>
{image}</article>"""

    def page_editor_browse(self) -> None:
        parameters = self.parameters()
        term = parameters.get("q", "").strip().casefold()
        state = parameters.get("state", "")
        contexts = self.editor_cases()
        filtered: list[dict[str, Any]] = []
        for context in contexts:
            case = context["case"]
            core = context["core"]
            searchable = " ".join(
                (
                    str(case["inventory_label"]),
                    str(core["descriptive_title"] or ""),
                    str(core["description"] or ""),
                    str(case.get("museum_digital_id") or ""),
                )
            ).casefold()
            role = (
                str(context["archive_file"]["role"])
                if context["archive_file"]
                else "no_representation"
            )
            matches_state = (
                not state
                or (state == "master" and role == "preservation_master")
                or (state == "surrogate" and role == "preservation_surrogate")
                or (state == "no_local" and not context["archive_file"])
                or (state == "md_missing" and not context["md"])
                or (state == "needs_review" and bool(context["missing"]))
            )
            if (not term or term in searchable) and matches_state:
                filtered.append(context)
        cards = ""
        for context in filtered:
            case = context["case"]
            core = context["core"]
            archive = context["archive_file"]
            role = (
                "Master"
                if archive and archive["role"] == "preservation_master"
                else "Surrogate"
                if archive
                else "No local file"
            )
            thumbnail_kind = (
                "local"
                if context["derivative"]
                else "archive"
                if archive
                else "md"
                if context["md_image"]
                else ""
            )
            thumbnail = (
                f'<a class="browse-card__image" href="{url("/compare", unit=case["unit_id"])}">'
                f'<img loading="lazy" src="{url("/editor/image", unit=case["unit_id"], kind=thumbnail_kind)}" alt=""></a>'
                if thumbnail_kind
                else '<div class="browse-card__missing">No image available</div>'
            )
            missing = "".join(
                f'<span class="missing-chip">{e(label)}</span>'
                for label in context["missing"]
            )
            cards += f"""<article class="browse-card">{thumbnail}
<div class="browse-card__body"><p>{badge(role.casefold().replace(' ', '_'))}</p>
<h2><a href="{url('/compare', unit=case['unit_id'])}">{e(case['inventory_label'])}</a></h2>
<p>{e(core['descriptive_title']) or 'Untitled record'}</p>
<div class="missing-list">{missing or '<span class="complete-chip">No obvious gaps</span>'}</div>
</div></article>"""
        body = f"""
<section class="page-heading"><p class="eyebrow">Editor view · guarded pilot</p>
<h1>Browse archive</h1>
<p>Find an object, see its images and metadata, and identify missing master,
derivative, museum-digital, or descriptive information immediately.</p></section>
<form class="form-grid editor-filters" method="get" action="/browse">
<label>Search<input type="search" name="q" value="{e(parameters.get('q', ''))}" placeholder="Inventory, title, description, or MD ID"></label>
<label>Show<select name="state"><option value="">Everything</option>
{option('needs_review', 'Anything missing', state)}
{option('master', 'Has master', state)}
{option('surrogate', 'Surrogate only', state)}
{option('no_local', 'No local representation', state)}
{option('md_missing', 'No museum-digital record', state)}
</select></label><div><button class="button" type="submit">Filter</button></div></form>
<p class="result-count">{len(filtered)} of {len(contexts)} records</p>
<section class="browse-grid">{cards or '<div class="empty">No records match.</div>'}</section>"""
        self.send_html("Browse archive", body, active="browse")

    def _metadata_cell(self, value: Any, missing: str = "Missing") -> str:
        text = str(value or "").strip()
        return e(text) if text else f'<span class="metadata-missing">{e(missing)}</span>'

    def render_editor_metadata_form(
        self,
        context: dict[str, Any],
        fields: tuple[tuple[str, str, Any, Any, Any], ...],
    ) -> str:
        controls = ""
        for key, label, archive, local_value, md_value in fields:
            choices = ['<option value="">No change</option>']
            for value, source_label, candidate in (
                ("archive", "Keep archive value", archive),
                ("local", "Accept local record", local_value),
                ("museum_digital", "Accept museum-digital", md_value),
                ("corrected", "Enter corrected value", "available"),
            ):
                disabled = (
                    " disabled"
                    if value != "corrected" and not str(candidate or "").strip()
                    else ""
                )
                choices.append(
                    f'<option value="{value}"{disabled}>{e(source_label)}</option>'
                )
            controls += f"""<fieldset class="editor-field">
<legend>{e(label)}</legend>
<label>Accepted source<select name="{e(key)}_source">{''.join(choices)}</select></label>
<label>Corrected value<textarea name="{e(key)}_corrected"
placeholder="Used only when “Enter corrected value” is selected"></textarea></label>
</fieldset>"""
        current_tags = ", ".join(context["accepted_tags"])
        md_tags = ", ".join(context["md_tags"])
        tag_options = ['<option value="">No change</option>']
        if current_tags:
            tag_options.append('<option value="archive">Keep archive tags</option>')
        if md_tags:
            tag_options.append(
                '<option value="museum_digital">Accept museum-digital tags</option>'
            )
        tag_options.append('<option value="corrected">Enter corrected tags</option>')
        return f"""<section class="panel editor-reconciliation">
<div class="panel-heading"><div><p class="eyebrow">Editor decision</p>
<h2>Reconcile metadata and tags</h2></div>
<p>Choose only fields you want to accept. Unselected fields remain unchanged.</p></div>
<form class="form" method="post" action="/editor/metadata">{self.csrf()}
<input type="hidden" name="unit_id" value="{e(context['case']['unit_id'])}">
<div class="editor-decision-grid">{controls}</div>
<fieldset class="editor-field editor-field--wide"><legend>Tags / keywords</legend>
<p><strong>Archive:</strong> {e(current_tags) or 'None'}<br>
<strong>Museum-digital:</strong> {e(md_tags) or 'None'}</p>
<label>Accepted source<select name="tags_source">{''.join(tag_options)}</select></label>
<label>Corrected tags or keywords
<textarea name="tags_corrected" placeholder="Separate tags with commas or new lines"></textarea></label>
</fieldset>
<label>Reason for change <span class="muted">(optional)</span>
<textarea name="reason"></textarea></label>
<button class="button" type="submit">Save reconciliation</button>
</form></section>"""

    def render_editor_master_control(
        self,
        context: dict[str, Any],
    ) -> str:
        if context["master_exists"]:
            return """<section class="panel master-control master-control--complete">
<p class="eyebrow">Original master</p><h2>Preservation master is present</h2>
<p>No master submission is needed for this object.</p></section>"""
        unit_id = str(context["case"]["unit_id"])
        entity_id = str(context["core"]["entity_id"])
        has_surrogate = bool(
            context["archive_file"]
            and context["archive_file"]["role"] == "preservation_surrogate"
        )
        candidate = master_candidate.latest_candidate(
            self.root,
            unit_id=unit_id,
            entity_id=entity_id,
        )
        candidate_html = ""
        if candidate:
            technical = dict(candidate["technical"])
            item = {
                "status": "available",
                "label": "Validated master candidate",
                "name": candidate["original_filename"],
                "size": candidate["size_bytes"],
                "width": technical["width_px"],
                "height": technical["height_px"],
                "url": url(
                    "/editor/image",
                    unit=unit_id,
                    kind="candidate",
                ),
            }
            if candidate["approved"]:
                decision = (
                    '<p class="complete-chip">Candidate already approved</p>'
                )
            else:
                confirmation_text = (
                    "I reviewed this TIFF and approve it as the original "
                    "preservation master. Keep the existing surrogate as "
                    "provenance."
                    if has_surrogate
                    else (
                        "I reviewed this TIFF and approve it as the "
                        "preservation master."
                    )
                )
                approval_label = (
                    "Approve original master and retain surrogate"
                    if has_surrogate
                    else "Accession approved master"
                )
                decision = f"""<form class="form approval-form" method="post"
action="/editor/master/approve">{self.csrf()}
<input type="hidden" name="unit_id" value="{e(unit_id)}">
<input type="hidden" name="candidate_id" value="{e(candidate['candidate_id'])}">
<label class="check-line"><input type="checkbox" name="confirmation"
value="ACCESSION" required> {e(confirmation_text)}</label>
<label>Reason for change <span class="muted">(optional)</span>
<textarea name="reason"></textarea></label>
<button class="button" type="submit">{e(approval_label)}</button></form>"""
            candidate_html = f"""<div class="candidate-review">
{self.render_editor_visual(item)}{decision}</div>"""
        heading = (
            "Assign the original TIFF master"
            if has_surrogate
            else "Supply a TIFF master"
        )
        explanation = (
            "The generated surrogate remains preserved as provenance. An "
            "approved original becomes the preservation master and preferred "
            "archive representation."
            if has_surrogate
            else (
                "The upload is retained inside this mini-pilot and cannot "
                "replace an existing file. Accession requires a separate "
                "review and approval."
            )
        )
        return f"""<section class="panel master-control"><div class="panel-heading"><div>
<p class="eyebrow">Original master missing</p><h2>{e(heading)}</h2></div>
<p>{e(explanation)}</p></div>
{candidate_html}
<form class="form upload-form" method="post" action="/editor/master/upload"
enctype="multipart/form-data">{self.csrf()}
<input type="hidden" name="unit_id" value="{e(unit_id)}">
<label>TIFF master candidate
<input type="file" name="master_file" accept=".tif,.tiff,image/tiff" required></label>
<p class="muted">Pilot upload limit: 192 MB. The selected source file is never modified.</p>
<label>Reason for submission <span class="muted">(optional)</span>
<textarea name="reason"></textarea></label>
<button class="button button--quiet" type="submit">Upload and validate candidate</button>
</form></section>"""

    def render_visual_candidate_search(
        self,
        context: dict[str, Any],
    ) -> str:
        if context["master_exists"]:
            return ""
        unit_id = str(context["case"]["unit_id"])
        query = self.server.visual_candidate_queries.get(unit_id)
        if not query:
            return ""
        staged = master_candidate.latest_candidate(
            self.root,
            unit_id=unit_id,
            entity_id=str(context["core"]["entity_id"]),
        )
        has_surrogate = bool(
            context["archive_file"]
            and context["archive_file"]["role"] == "preservation_surrogate"
        )
        cards = ""
        for rank, candidate in enumerate(query["candidates"][:3], 1):
            band = str(candidate["distance_band"])
            label = {
                "very_close": "Very close candidate",
                "close": "Close candidate",
                "exploratory": "Explore candidate",
            }.get(band, "Candidate")
            item = {
                "status": "available",
                "label": label,
                "name": candidate["filename"],
                "size": candidate["size_bytes"],
                "width": candidate["width_px"],
                "height": candidate["height_px"],
                "url": url(
                    "/editor/image",
                    unit=unit_id,
                    kind="visual-candidate",
                    choice=rank,
                ),
            }
            action = (
                '<p class="muted">A candidate is already staged for review.</p>'
                if staged
                else f"""<form class="candidate-pin-form" method="post"
action="/editor/master/pin">{self.csrf()}
<input type="hidden" name="unit_id" value="{e(unit_id)}">
<input type="hidden" name="choice" value="{rank}">
<button class="button button--quiet" type="submit">{
    'Use as original master candidate'
    if has_surrogate
    else 'Stage this TIFF for review'
}</button>
</form>"""
            )
            cards += (
                '<div class="candidate-search-card">'
                f"{self.render_editor_visual(item)}{action}</div>"
            )
        transition = (
            "The current archive TIFF is a generated surrogate. Selecting a "
            "candidate does not relabel or delete it: the candidate is copied "
            "for validation, then a separate approval can assign the original "
            "master."
            if has_surrogate
            else (
                "Selecting a candidate copies it for validation and still "
                "requires separate approval."
            )
        )
        return f"""<section class="panel visual-candidate-search">
<div class="panel-heading"><div><p class="eyebrow">Missing-master search</p>
<h2>{'Assign original master' if has_surrogate else 'Possible TIFF masters'}</h2></div>
<p>Compare these bounded visual matches with the derivative and
museum-digital image above. Similarity is not an identity decision. The three
best matches are shown; administrators retain the complete result.</p></div>
<p class="candidate-transition">{e(transition)}</p>
<div class="candidate-search-grid">{cards}</div>
</section>"""

    def page_editor_compare(self) -> None:
        unit_id = self.parameters().get("unit", "")
        context = self.editor_case_context(unit_id)
        case = context["case"]
        core = context["core"]
        local = context["local"]
        md = context["md"]
        visuals = "".join(
            self.render_editor_visual(item)
            for item in self.editor_visuals(context)
        )
        fields = (
            (
                "title",
                "Title",
                core["descriptive_title"],
                local["title"] if local else "",
                md["object_name"] if md else "",
            ),
            (
                "description",
                "Description",
                core["description"],
                local["description"] if local else "",
                md["object_description"] if md else "",
            ),
            (
                "object_type",
                "Object type",
                context["accepted_object_type"],
                local["object_type"] if local else "",
                md["object_type"] if md else "",
            ),
            (
                "material",
                "Material / technique",
                core["material_technique"],
                "",
                context["md_material"],
            ),
            (
                "dimensions",
                "Dimensions",
                core["dimensions"],
                "",
                context["md_dimensions"],
            ),
            (
                "tags",
                "Tags / keywords",
                ", ".join(context["accepted_tags"]),
                "",
                ", ".join(context["md_tags"]),
            ),
        )
        rows = "".join(
            f"""<tr><th scope="row">{e(label)}</th>
<td>{self._metadata_cell(archive, 'Not stored')}</td>
<td>{self._metadata_cell(local_value)}</td>
<td>{self._metadata_cell(md_value)}</td></tr>"""
            for _key, label, archive, local_value, md_value in fields
        )
        missing = "".join(
            f'<span class="missing-chip">{e(label)}</span>'
            for label in self.editor_missing_labels(context)
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Master · derivative · museum-digital</p>
<h1>{e(case['inventory_label'])}</h1>
<p>{e(core['descriptive_title']) or 'Untitled record'}</p>
<div class="missing-list">{missing or '<span class="complete-chip">No obvious gaps</span>'}</div></section>
<section class="visual-case"><div class="visual-case__heading"><div>
<h2>Visual comparison</h2><p>Filename, file size and pixel dimensions are shown
for quick identification. Paths and hashes stay in the admin view.</p></div></div>
<div class="visual-strip visual-strip--three">{visuals}</div></section>
{self.render_visual_candidate_search(context)}
<section class="panel metadata-comparison"><h2>Metadata comparison</h2>
<p>Review the evidence columns, then accept only the fields that should become
archive metadata.</p><div class="table-wrap"><table>
<thead><tr><th>Field</th><th>Archive</th><th>Local records</th><th>Museum-digital</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>
{self.render_editor_metadata_form(context, fields[:-1])}
{self.render_editor_master_control(context)}
<p class="admin-evidence-link"><a href="{url('/admin', unit=unit_id)}">Admin technical evidence</a></p>"""
        self.send_html(str(case["inventory_label"]), body, active="compare")

    def _publish_editor_generation(self) -> dict[str, Any]:
        config = catalog_core.require_core_mode(self.root)
        independent_text = str(
            config.get("independent_generation_root") or ""
        ).strip()
        if not independent_text:
            raise reconciliation.ReconciliationError(
                "editor changes require the configured independent pilot copy"
            )
        return catalog_core.publish_generation(
            self.root,
            Path(independent_text),
            tool_version=f"core-workbench-{WORKBENCH_VERSION}",
            actor=self.server.actor,
        )

    def action_editor_metadata(self, form: dict[str, str]) -> None:
        unit_id = form.get("unit_id", "")
        context = self.editor_case_context(unit_id)
        core = context["core"]
        local = context["local"]
        md = context["md"]
        candidates = {
            "title": {
                "archive": core["descriptive_title"],
                "local": local["title"] if local else "",
                "museum_digital": md["object_name"] if md else "",
            },
            "description": {
                "archive": core["description"],
                "local": local["description"] if local else "",
                "museum_digital": md["object_description"] if md else "",
            },
            "object_type": {
                "archive": context["accepted_object_type"],
                "local": local["object_type"] if local else "",
                "museum_digital": md["object_type"] if md else "",
            },
            "material": {
                "archive": core["material_technique"],
                "local": "",
                "museum_digital": context["md_material"],
            },
            "dimensions": {
                "archive": core["dimensions"],
                "local": "",
                "museum_digital": context["md_dimensions"],
            },
        }
        decisions: dict[str, dict[str, str]] = {}
        for field, values in candidates.items():
            source = form.get(f"{field}_source", "").strip()
            if not source:
                continue
            value = (
                form.get(f"{field}_corrected", "").strip()
                if source == "corrected"
                else str(values.get(source, "") or "").strip()
            )
            decisions[field] = {"source": source, "value": value}
        tags_source = form.get("tags_source", "").strip()
        tag_decision: dict[str, Any] | None = None
        if tags_source:
            if tags_source == "archive":
                tags = list(context["accepted_tags"])
            elif tags_source == "museum_digital":
                tags = list(context["md_tags"])
            elif tags_source == "corrected":
                tags = [
                    item.strip()
                    for line in form.get("tags_corrected", "").splitlines()
                    for item in line.split(",")
                    if item.strip()
                ]
            else:
                raise reconciliation.ReconciliationError(
                    "unsupported editor tag source"
                )
            tag_decision = {"source": tags_source, "values": tags}
        with closing(self.catalog()) as connection:
            editor_reconcile.reconcile_metadata(
                connection,
                unit_id=unit_id,
                entity_id=str(core["entity_id"]),
                field_decisions=decisions,
                tag_decision=tag_decision,
                actor=self.server.actor,
                reason=form.get("reason", ""),
                evidence=self.server.stress_manifest_digest,
                external_id=str(
                    context["case"].get("museum_digital_id") or ""
                ),
            )
        self._publish_editor_generation()
        self.redirect(
            url(
                "/compare",
                unit=unit_id,
                notice="Metadata reconciliation saved and published.",
            )
        )

    def action_editor_master_upload(self) -> None:
        form, temporary, filename = self.upload_form()
        unit_id = form.get("unit_id", "")
        context = self.editor_case_context(unit_id)
        receipt = master_candidate.stage_candidate(
            self.root,
            unit_id=unit_id,
            entity_id=str(context["core"]["entity_id"]),
            uploaded_path=temporary,
            original_filename=filename,
            actor=self.server.actor,
            reason=form.get("reason", ""),
        )
        self.redirect(
            url(
                "/compare",
                unit=unit_id,
                notice=(
                    f"{receipt['original_filename']} uploaded and validated. "
                    "Review it before accession."
                ),
            )
        )

    def action_editor_master_pin(self, form: dict[str, str]) -> None:
        unit_id = form.get("unit_id", "")
        context = self.editor_case_context(unit_id)
        if context["master_exists"]:
            raise reconciliation.ReconciliationError(
                "this archive object already has a preservation master"
            )
        if master_candidate.latest_candidate(
            self.root,
            unit_id=unit_id,
            entity_id=str(context["core"]["entity_id"]),
        ):
            raise reconciliation.ReconciliationError(
                "review or approve the already staged master candidate first"
            )
        candidate, source = self.visual_candidate_source(
            unit_id,
            form.get("choice", ""),
        )
        staging = self.root / "work" / "editor-upload-staging"
        staging.mkdir(parents=True, exist_ok=True)
        temporary = staging / f"{uuid.uuid4().hex}.indexed-candidate.tif"
        source_stat = source.stat()
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = source.stat()
            if (
                after.st_size != source_stat.st_size
                or after.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise reconciliation.ReconciliationError(
                    "protected visual candidate changed while staging"
                )
            receipt = master_candidate.stage_candidate(
                self.root,
                unit_id=unit_id,
                entity_id=str(context["core"]["entity_id"]),
                uploaded_path=temporary,
                original_filename=str(candidate["filename"]),
                actor=self.server.actor,
                reason=form.get("reason", ""),
                max_bytes=master_candidate.MAX_INDEXED_CANDIDATE_BYTES,
                source_evidence={
                    "kind": "protected_visual_index_candidate",
                    "asset_id": candidate["asset_id"],
                    "source_path": str(source),
                    "size_bytes": source_stat.st_size,
                    "mtime_ns": source_stat.st_mtime_ns,
                    "candidate_report": str(
                        self.server.visual_candidate_report_path or ""
                    ),
                    "candidate_report_digest": (
                        self.server.visual_candidate_report_digest
                    ),
                },
            )
        finally:
            temporary.unlink(missing_ok=True)
        self.redirect(
            url(
                "/compare",
                unit=unit_id,
                notice=(
                    f"{receipt['original_filename']} copied and validated "
                    "inside the pilot. Review it before accession."
                ),
            )
        )

    def action_editor_master_approve(self, form: dict[str, str]) -> None:
        if form.get("confirmation", "") != "ACCESSION":
            raise reconciliation.ReconciliationError(
                "explicit master accession approval is required"
            )
        unit_id = form.get("unit_id", "")
        context = self.editor_case_context(unit_id)
        master_candidate.approve_candidate(
            self.root,
            unit_id=unit_id,
            entity_id=str(context["core"]["entity_id"]),
            candidate_id=form.get("candidate_id", ""),
            actor=self.server.actor,
            manifest_digest=self.server.stress_manifest_digest,
            museum_digital_id=str(
                context["case"].get("museum_digital_id") or ""
            ),
            reason=form.get("reason", ""),
        )
        self._publish_editor_generation()
        self.redirect(
            url(
                "/compare",
                unit=unit_id,
                notice="Approved TIFF accessioned as the preservation master.",
            )
        )

    def page_editor_image(self) -> None:
        unit_id = self.parameters().get("unit", "")
        kind = self.parameters().get("kind", "")
        context = self.editor_case_context(unit_id)
        if kind == "archive":
            archive = context["archive_file"]
            if not archive:
                raise reconciliation.ReconciliationError(
                    "archive representation is missing"
                )
            source = (self.root / str(archive["relative_path"])).resolve(
                strict=True
            )
            try:
                source.relative_to(self.root)
            except ValueError as error:
                raise reconciliation.ReconciliationError(
                    "archive preview path escaped the pilot root"
                ) from error
        elif kind == "local":
            derivative = context["derivative"]
            if not derivative:
                raise reconciliation.ReconciliationError(
                    "local derivative is missing"
                )
            source = self._safe_editor_source(Path(str(derivative["path"])))
        elif kind == "md":
            source = self._cached_md_image(context)
        elif kind == "candidate":
            candidate = master_candidate.latest_candidate(
                self.root,
                unit_id=unit_id,
                entity_id=str(context["core"]["entity_id"]),
            )
            if not candidate:
                raise reconciliation.ReconciliationError(
                    "master candidate is unavailable"
                )
            source = Path(str(candidate["candidate_path"])).resolve(strict=True)
            try:
                source.relative_to(self.root / "work")
            except ValueError as error:
                raise reconciliation.ReconciliationError(
                    "master candidate escaped pilot work"
                ) from error
        elif kind == "visual-candidate":
            _candidate, source = self.visual_candidate_source(
                unit_id,
                self.parameters().get("choice", ""),
            )
        else:
            raise reconciliation.ReconciliationError(
                "unknown editor preview kind"
            )
        cache_kind = kind
        if kind == "visual-candidate":
            cache_kind = (
                "visual-candidate-"
                + self.parameters().get("choice", "")
            )
        browser_path = self._browser_image(source, unit_id, cache_kind)
        content_type = (
            mimetypes.guess_type(browser_path.name)[0] or "image/jpeg"
        )
        self.send_bytes(browser_path.read_bytes(), content_type)

    def page_editor_admin(self) -> None:
        unit_id = self.parameters().get("unit", "")
        with closing(self.catalog(read_only=True)) as connection:
            state = catalog_core.catalog_state(connection)
            current = connection.execute(
                """SELECT generation_id, manifest_digest
                   FROM filesystem_generations WHERE status = 'current'"""
            ).fetchone()
            parameters: tuple[Any, ...] = ()
            where = ""
            if unit_id:
                self.stress_case(unit_id)
                where = "WHERE fo.unit_id = ?"
                parameters = (unit_id,)
            files = list(
                connection.execute(
                    f"""SELECT fo.unit_id, ef.role, fo.original_filename,
                               b.size_bytes, b.width_px, b.height_px,
                               fo.relative_path, fo.content_digest
                        FROM entity_files ef
                        JOIN file_occurrences fo
                          ON fo.occurrence_id = ef.occurrence_id
                        JOIN content_blobs b
                          ON b.content_digest = fo.content_digest
                        {where}
                        ORDER BY fo.unit_id, ef.role""",
                    parameters,
                )
            )
        rows = "".join(
            f"""<tr><td><a href="{url('/compare', unit=item['unit_id'])}">{e(item['unit_id'])}</a></td>
<td>{e(item['role'])}</td><td>{e(item['original_filename'])}</td>
<td>{e(human_size(item['size_bytes']))}</td>
<td>{e(item['width_px'])} × {e(item['height_px'])}</td>
<td class="technical">{e(item['relative_path'])}</td>
<td class="technical">{e(item['content_digest'])}</td></tr>"""
            for item in files
        )
        candidate_admin = ""
        query = self.server.visual_candidate_queries.get(unit_id)
        if query:
            candidate_rows = ""
            for rank, item in enumerate(query["candidates"], 1):
                alternates = item.get("alternate_occurrences", [])
                occurrence_evidence = "<br>".join(
                    [e(item["path"])]
                    + [e(alternate["path"]) for alternate in alternates]
                )
                candidate_rows += f"""<tr><td>{rank}</td><td>{e(item['distance_band'])}</td>
<td>{e(item['distance_total'])}</td>
<td>{e(str(item['distance_phash']))} / {e(str(item['distance_dhash']))} / {e(str(item['distance_ahash']))}</td>
<td>{e(item['filename'])}</td>
<td>{1 + len(alternates)}</td>
<td class="technical">{occurrence_evidence}</td>
<td class="technical">{e(item['asset_id'])}</td></tr>"""
            candidate_admin = f"""<section class="panel"><h2>Visual candidate evidence</h2>
<p class="technical">Report:
{e(self.server.visual_candidate_report_path)}<br>
{e(self.server.visual_candidate_report_digest)}</p>
<div class="table-wrap"><table><thead><tr><th>Rank</th><th>Band</th>
<th>Total distance</th><th>pHash / dHash / aHash</th><th>Name</th>
<th>Occurrences</th><th>Protected paths</th><th>Primary asset</th></tr></thead>
<tbody>{candidate_rows}</tbody></table></div></section>"""
        body = f"""
<section class="page-heading"><p class="eyebrow">Administrator view</p>
<h1>Technical evidence</h1><p>Fixity identifiers, filesystem paths and
publication state are intentionally separated from the editor workflow.</p></section>
<section class="health-banner"><div><p class="eyebrow">Catalog</p>
<h2>Revision {state['catalog_revision']}</h2></div>
<p>Published {state['published_revision']}<br>{e(current['generation_id'] if current else 'No generation')}</p></section>
<section class="panel"><h2>{e(unit_id) if unit_id else 'Pilot files'}</h2>
<div class="table-wrap"><table><thead><tr><th>Unit</th><th>Role</th><th>Name</th>
<th>Size</th><th>Dimensions</th><th>Path</th><th>SHA-256</th></tr></thead>
<tbody>{rows or '<tr><td colspan="7">No cataloged files.</td></tr>'}</tbody>
</table></div></section>{candidate_admin}"""
        self.send_html("Admin technical evidence", body, active="admin")

    def overview_state(self) -> dict[str, Any]:
        with closing(self.catalog(read_only=True)) as connection:
            state = catalog_core.catalog_state(connection)
            values = {
                "catalog_revision": int(state["catalog_revision"]),
                "published_revision": int(state["published_revision"]),
                "dirty_since": str(state["dirty_since"] or ""),
                "units": int(connection.execute("SELECT count(*) FROM units").fetchone()[0]),
                "entities": int(connection.execute("SELECT count(*) FROM entities").fetchone()[0]),
            }
        try:
            with closing(self.campaign(read_only=True)) as connection:
                values.update(reconciliation.campaign_summary(connection))
                with closing(self.catalog(read_only=True)) as catalog:
                    health = reconciliation.campaign_health(connection, catalog)
                values["campaign_health_status"] = health["status"]
                values["campaign_health_issues"] = health["issue_count"]
        except reconciliation.ReconciliationError:
            values.update({"campaigns": 0, "records": 0, "pending": 0, "review": 0, "complete": 0, "deferred": 0})
            values["campaign_health_status"] = "safe"
            values["campaign_health_issues"] = 0
        return values

    def page_overview(self) -> None:
        state = self.overview_state()
        current = state["catalog_revision"] == state["published_revision"]
        campaign_warning = (
            f"""<section class="notice notice--yellow"><h2>Campaign/core drift</h2>
<p>{state['campaign_health_issues']} cached campaign decision receipt(s) disagree with durable core state. Rebuild or investigate campaign working state before further review.</p></section>"""
            if state["campaign_health_status"] == "yellow"
            else ""
        )
        body = f"""
<section class="page-heading">
  <p class="eyebrow">Minimal core V2</p>
  <h1>Archive workbench</h1>
  <p>The catalog is a rebuildable working surface. Accepted changes remain unpublished until a verified open filesystem generation is created.</p>
</section>
<section class="health-banner {'health-banner--yellow' if not current else ''}">
  <div><p class="eyebrow">Filesystem freshness</p><h2>{'Current' if current else 'Unpublished changes'}</h2></div>
  <p>Catalog revision {state['catalog_revision']} · published revision {state['published_revision']}{f"<br>Dirty since {e(state['dirty_since'])}" if state['dirty_since'] else ""}</p>
</section>
{campaign_warning}
<section class="stats">
  <a href="/entities"><strong>{state['entities']}</strong><span>entities</span></a>
  <a href="/create"><strong>{state['units']}</strong><span>units</span></a>
  <a href="/reconcile"><strong>{state['records']}</strong><span>reconciliation records</span></a>
  <a href="/reconcile?status=review"><strong>{state.get('review', 0)}</strong><span>ready for field review</span></a>
</section>
<div class="two-column">
  <section class="panel"><h2>Permanent surface</h2><ul class="plain-list"><li>Browse canonical entities and evidence</li><li>Create units and entities through guarded operations</li><li>Show catalog publication freshness explicitly</li><li>Publish only to the configured independent generation root</li></ul></section>
  <section class="panel"><h2>Campaign surface</h2><ul class="plain-list"><li>Visual evidence stays in a replaceable local cache</li><li>Local and MD values are compared field by field</li><li>Only explicit acceptance reaches the core</li><li>No source file is moved, renamed, deleted, or rewritten</li></ul></section>
</div>"""
        self.send_html("Overview", body, active="overview")

    def page_entities(self) -> None:
        term = self.parameters().get("q", "").strip()
        with closing(self.catalog(read_only=True)) as connection:
            parameters: list[Any] = []
            where = ""
            if term:
                where = """WHERE e.entity_id LIKE ? OR n.value LIKE ?
                           OR a.archival_label LIKE ?"""
                like = f"%{term}%"
                parameters.extend((like, like, like))
            rows = list(
                connection.execute(
                    f"""SELECT e.entity_id, e.kind, e.status,
                               n.value AS canonical_name,
                               a.archival_label, a.descriptive_title
                        FROM entities e
                        LEFT JOIN entity_names n
                          ON n.entity_id = e.entity_id
                         AND n.name_type = 'canonical'
                         AND n.status = 'active'
                        LEFT JOIN archive_objects a ON a.entity_id = e.entity_id
                        {where}
                        ORDER BY n.value, e.entity_id
                        LIMIT 250""",
                    parameters,
                )
            )
        cards = "".join(
            f"""<article class="object-card"><div class="object-card__body">
  <p>{badge(row['kind'])} {badge(row['status'])}</p>
  <h2><a href="{url('/entity', id=row['entity_id'])}">{e(row['canonical_name'] or row['entity_id'])}</a></h2>
  <p>{e(row['archival_label'])}</p>
  <p class="technical">{e(row['entity_id'])}</p>
</div></article>"""
            for row in rows
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Identity and domains</p><h1>Entities</h1><p>Canonical ISS identifiers live on entities; external and historical identifiers remain aliases.</p></section>
<form class="search" method="get" action="/entities"><label><span class="visually-hidden">Search</span><input type="search" name="q" value="{e(term)}" placeholder="Name, label, alias, or ISS identifier"></label><button class="button" type="submit">Search</button></form>
<p class="result-count">{len(rows)} result(s)</p>
<section class="object-grid">{cards or '<div class="empty">No entities found.</div>'}</section>"""
        self.send_html("Entities", body, active="entities")

    def page_entity(self) -> None:
        entity_id = self.parameters().get("id", "")
        with closing(self.catalog(read_only=True)) as connection:
            row = connection.execute(
                """SELECT e.*, n.value AS canonical_name, a.*
                   FROM entities e
                   LEFT JOIN entity_names n
                     ON n.entity_id = e.entity_id
                    AND n.name_type = 'canonical' AND n.status = 'active'
                   LEFT JOIN archive_objects a ON a.entity_id = e.entity_id
                   WHERE e.entity_id = ?""",
                (entity_id,),
            ).fetchone()
            if not row:
                raise reconciliation.ReconciliationError(f"unknown entity: {entity_id}")
            aliases = list(
                connection.execute(
                    """SELECT namespace, value, role, status
                       FROM entity_identifiers
                       WHERE entity_id = ? ORDER BY namespace, value""",
                    (entity_id,),
                )
            )
            assertions = list(
                connection.execute(
                    """SELECT a.predicate, a.value_text, a.status, a.confidence,
                              s.source_type, s.system, s.external_id
                       FROM evidence_assertions a
                       LEFT JOIN evidence_sources s ON s.source_id = a.source_id
                       WHERE a.subject_entity_id = ?
                       ORDER BY a.asserted_at DESC LIMIT 100""",
                    (entity_id,),
                )
            )
        alias_html = "".join(
            f"<li><strong>{e(item['namespace'])}</strong>: {e(item['value'])} {badge(item['status'])}</li>"
            for item in aliases
        )
        assertion_html = "".join(
            f"<tr><td>{e(item['predicate'])}</td><td>{e(item['value_text'])}</td><td>{badge(item['status'])}</td><td>{e(item['system'] or item['source_type'])} {e(item['external_id'])}</td></tr>"
            for item in assertions
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">{e(row['kind'])}</p><h1>{e(row['canonical_name'] or entity_id)}</h1><p>{e(entity_id)} · {badge(row['status'])}</p></section>
<div class="two-column two-column--detail">
  <section class="panel"><h2>Archive object</h2><dl><dt>Archival label</dt><dd>{e(row['archival_label'])}</dd><dt>Descriptive title</dt><dd>{e(row['descriptive_title']) or '—'}</dd><dt>Description</dt><dd>{e(row['description']) or '—'}</dd><dt>Material / technique</dt><dd>{e(row['material_technique']) or '—'}</dd><dt>Dimensions</dt><dd>{e(row['dimensions']) or '—'}</dd></dl></section>
  <section class="panel"><h2>Alias identifiers</h2><ul class="plain-list">{alias_html or '<li>No aliases.</li>'}</ul></section>
</div>
<section class="panel"><h2>Evidence assertions</h2><div class="table-wrap"><table><thead><tr><th>Field</th><th>Value</th><th>Status</th><th>Source</th></tr></thead><tbody>{assertion_html or '<tr><td colspan="4">No assertions.</td></tr>'}</tbody></table></div></section>"""
        self.send_html(str(row["canonical_name"] or entity_id), body, active="entities")

    def page_create(self) -> None:
        with closing(self.catalog(read_only=True)) as connection:
            units = list(
                connection.execute(
                    "SELECT unit_id, title FROM units WHERE status = 'open' ORDER BY unit_id"
                )
            )
        unit_options = "".join(
            option(str(row["unit_id"]), f"{row['unit_id']} — {row['title']}")
            for row in units
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Guarded creation</p><h1>Create core records</h1><p>These forms call the same transactional operations as the CLI and leave the catalog visibly unpublished.</p></section>
<div class="two-column">
<section class="panel"><h2>New unit</h2>
<form class="form" method="post" action="/core/create-unit">{self.csrf()}
<label>Unit identifier<input name="unit_id" required placeholder="WF-PARAGON"></label>
<label>Title<input name="title" required></label>
<label>Kind<input name="kind" required value="retrospective"></label>
<label>Note<textarea name="note"></textarea></label>
<button class="button" type="submit">Create unit</button></form></section>
<section class="panel"><h2>New archive object</h2>
<form class="form" method="post" action="/core/create-entity">{self.csrf()}
<label>Canonical name<input name="canonical_name" required></label>
<label>Archival label<input name="archival_label" required placeholder="VLV-1112"></label>
<label>Unit<select name="unit_id" required><option value="">Choose…</option>{unit_options}</select></label>
<label>Assignment year<input name="assignment_year" type="number" min="1000" max="9999" required></label>
<label>Initial descriptive title<input name="descriptive_title"></label>
<label>Note<textarea name="note"></textarea></label>
<button class="button" type="submit" {'disabled' if not units else ''}>Create archive object</button></form></section>
</div>"""
        self.send_html("Create", body, active="create")

    def page_reconcile(self) -> None:
        parameters = self.parameters()
        status = parameters.get("status", "")
        term = parameters.get("q", "").strip()
        try:
            with closing(self.campaign(read_only=True)) as connection:
                campaigns = list(
                    connection.execute(
                        "SELECT campaign_id, title, status FROM campaigns ORDER BY created_at DESC"
                    )
                )
                rows = reconciliation.list_records(
                    connection,
                    campaign_id=parameters.get("campaign", ""),
                    queue_status=status,
                    query=term,
                    limit=100,
                )
        except reconciliation.ReconciliationError:
            command = (
                "python3 tools/archive-preservation/museum_digital_campaign.py "
                f"import-run --root {self.root} --run /path/to/comparison-run"
            )
            body = f"""
<section class="page-heading"><p class="eyebrow">Visual reconciliation</p><h1>No campaign imported</h1><p>Create the replaceable campaign database from an existing read-only museum-digital comparison run.</p></section>
<section class="panel"><pre>{e(command)}</pre><p>This copies only working snapshots and previews into the V2 campaign cache. It does not change source evidence or the core catalog.</p></section>"""
            self.send_html("Reconcile", body, active="reconcile")
            return
        campaign_options = "".join(
            option(str(row["campaign_id"]), str(row["title"]), parameters.get("campaign", ""))
            for row in campaigns
        )
        cards = "".join(
            f"""<article class="question-card">
<div class="question-card__content">
  <p>{badge(row['queue_status'])} {badge(row['identity_status'])}</p>
  <h2><a href="{url('/reconcile/record', id=row['record_id'])}">{e(row['inventory_label'] or row['local_archival_id'] or row['external_id'])}</a></h2>
  <p>Core: {e(row['entity_id']) or 'not linked'}<br>Museum-digital: {e(row['external_id']) or 'missing'}<br>Local source: {e(row['source_record_status'])}</p>
</div></article>"""
            for row in rows
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Visual reconciliation</p><h1>Editor queue</h1><p>Identity is decided before metadata. Exact identifiers are candidates only; local and MD wording remain visible field by field.</p></section>
<form class="form-grid" method="get" action="/reconcile">
<label>Campaign<select name="campaign"><option value="">All campaigns</option>{campaign_options}</select></label>
<label>Queue status<select name="status"><option value="">All states</option>{''.join(option(value, value.replace('_', ' ').title(), status) for value in ('pending','review','deferred','complete','error'))}</select></label>
<label>Search<input type="search" name="q" value="{e(term)}" placeholder="Inventory, ISS ID, or MD ID"></label>
<div><button class="button" type="submit">Filter</button></div>
</form>
<p class="result-count">{len(rows)} record(s), capped at 100 per page</p>
<section class="question-list">{cards or '<div class="empty">No records match this queue.</div>'}</section>"""
        self.send_html("Reconcile", body, active="reconcile")

    def page_reconciliation_record(self) -> None:
        record_id = self.parameters().get("id", "")
        with closing(self.campaign(read_only=True)) as connection:
            bundle = reconciliation.record_bundle(connection, record_id)
        record = bundle["record"]
        with closing(self.catalog(read_only=True)) as catalog:
            entities = list(
                catalog.execute(
                    """SELECT e.entity_id, n.value
                       FROM entities e
                       LEFT JOIN entity_names n
                         ON n.entity_id = e.entity_id
                        AND n.name_type = 'canonical' AND n.status = 'active'
                       WHERE e.kind = 'archive_object' AND e.status = 'active'
                       ORDER BY n.value, e.entity_id"""
                )
            )
        visuals = "".join(self.render_visual(item) for item in bundle["visuals"])
        identity_html = self.render_identity_form(record, entities, bundle["identity_decisions"])
        fields_by_key: dict[str, list[sqlite3.Row]] = {}
        for candidate in bundle["fields"]:
            fields_by_key.setdefault(str(candidate["field_key"]), []).append(candidate)
        decisions_by_field: dict[str, sqlite3.Row] = {}
        for decision in reversed(bundle["field_decisions"]):
            decisions_by_field[str(decision["field_key"])] = decision
        field_html = "".join(
            self.render_field(
                record,
                key,
                candidates,
                decisions_by_field.get(key),
            )
            for key, candidates in fields_by_key.items()
        )
        context_rows = "".join(
            f"<tr><th>{e(item['context_label'])}</th><td>{e(item['source_side'])}</td><td>{e(item['value_text']) or '—'}</td></tr>"
            for item in bundle["context"]
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Museum-digital comparison</p><h1>{e(record['inventory_label'] or record['local_archival_id'] or record['external_id'])}</h1><p>Core entity: {e(record['entity_id']) or 'not linked'} · MD {e(record['external_id']) or 'not found'} · {badge(record['queue_status'])}</p></section>
<section class="visual-case"><div class="visual-case__heading"><div><h2>Visual evidence</h2><p>Local and external imagery must both be available before identity can be accepted.</p></div>{badge(record['identity_status'])}</div><div class="visual-strip">{visuals or '<div class="empty">No visual evidence.</div>'}</div>{identity_html}</section>
{field_html if record['identity_status'] == 'resolved' else '<section class="notice notice--yellow"><h2>Metadata remains locked</h2><p>Resolve identity before choosing local, museum-digital, or corrected values.</p></section>'}
<details class="panel"><summary>Additional comparison context</summary><div class="table-wrap"><table><tbody>{context_rows}</tbody></table></div><p class="technical">Local source: {e(record['source_record_locator']) or 'not supplied'}<br>MD snapshot: {e(record['snapshot_digest']) or 'not captured'}<br>MD URL: {e(record['external_url'])}</p></details>"""
        self.send_html("Reconciliation record", body, active="reconcile")

    def render_visual(
        self,
        item: sqlite3.Row,
        *,
        display_role: str = "",
        display_label: str = "",
        review_state: str = "",
        exact_duplicate: bool = False,
        candidate_role: str = "",
        tiff_master_selected: bool = False,
        fast_confirm: bool = False,
        return_to: str = "",
    ) -> str:
        image = (
            f'<a class="visual-card__image" href="{url("/reconcile/image", id=item["evidence_id"])}"><img src="{url("/reconcile/image", id=item["evidence_id"])}" alt=""></a>'
            if item["status"] == "available" and item["cache_relpath"]
            else f'<div class="visual-card__missing"><strong>{e(item["status"])}</strong><span>{e(item["detail"])}</span></div>'
        )
        dimensions = (
            f"{item['width_px']} × {item['height_px']} px"
            if item["width_px"] and item["height_px"]
            else "Dimensions unavailable"
        )
        review_html = ""
        if candidate_role:
            state_html = badge(review_state or "candidate")
            duplicate_html = badge("exact_duplicate") if exact_duplicate else ""
            controls: list[str] = []
            if tiff_master_selected and candidate_role != "master_tiff_candidate":
                controls = []
            elif candidate_role == "master_tiff_candidate":
                controls.append((
                    "tiff_master_generated_derivative",
                    "Use TIFF master + generate derivative",
                ))
                if review_state in {"redundant", "selected_master"}:
                    controls.append(("candidate", "Restore as candidate"))
            elif review_state in {"redundant", "selected_master", "selected_derivative"}:
                controls.append(("candidate", "Restore as candidate"))
            else:
                if candidate_role == "high_resolution_jpeg_candidate":
                    controls.append(("selected_master", "Use as local master"))
                if candidate_role == "local_derivative":
                    controls.append(("selected_derivative", "Use as local derivative"))
                controls.append(("redundant", "Mark redundant"))
            fast_form = ""
            if fast_confirm and candidate_role in {
                "master_tiff_candidate",
                "high_resolution_jpeg_candidate",
            }:
                fast_form = f"""<form method="post" action="/recovery/visual-confirm">{self.csrf()}
<input type="hidden" name="record_id" value="{e(item['record_id'])}">
<input type="hidden" name="evidence_id" value="{e(item['evidence_id'])}">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--small" type="submit">Accept identity, metadata, and use this master</button>
</form>"""
            forms = "".join(f"""<form method="post" action="/recovery/candidate-review" data-async-save>{self.csrf()}
<input type="hidden" name="record_id" value="{e(item['record_id'])}">
<input type="hidden" name="evidence_id" value="{e(item['evidence_id'])}">
<input type="hidden" name="state" value="{e(state)}">
<input type="hidden" name="return_to" value="{e(return_to)}">
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button button--quiet button--small" type="submit">{e(label)}</button>
</form>""" for state, label in controls)
            review_html = f"""<div class="candidate-disposition"><p>{state_html} {duplicate_html}</p>{fast_form}{forms}</div>"""
        article_state = (
            f" visual-card--{e(review_state)}" if review_state else ""
        )
        return f"""<article class="visual-card visual-card--{e(item['status'])}{article_state}">
<header><p class="eyebrow">{e(display_role or item['role'].replace('_', ' '))}</p><h3>{e(display_label or item['label'])}</h3><p>{e(Path(str(item['source_locator'] or item['label'])).name)}</p></header>
{image}
<div class="visual-card__body"><p>{e(dimensions)}</p>{review_html}<details><summary>Technical evidence</summary><p class="technical">{e(item['content_digest'])}<br>{e(item['source_locator'])}<br>{e(item['detail'])}</p></details></div>
</article>"""

    def render_identity_form(
        self,
        record: sqlite3.Row,
        entities: list[sqlite3.Row],
        decisions: list[sqlite3.Row],
        *,
        preferred_parent: str = "",
        return_to: str = "",
    ) -> str:
        return_hidden = (
            f'<input type="hidden" name="return_to" value="{e(return_to)}">'
            if return_to
            else ""
        )
        if not record["entity_id"]:
            options = "".join(
                option(str(row["entity_id"]), f"{row['value'] or row['entity_id']} — {row['entity_id']}")
                for row in entities
            )
            return f"""<section class="decision-step"><h3>Link this campaign record</h3><p>Linking changes only replaceable campaign state. It does not assert that the MD record is the same object.</p>
<form class="form" method="post" action="/reconcile/link">{self.csrf()}{return_hidden}<input type="hidden" name="record_id" value="{e(record['record_id'])}"><label>Core entity<select name="entity_id" required><option value="">Choose…</option>{options}</select></label><button class="button" type="submit">Link for review</button></form></section>"""
        if decisions and decisions[0]["outcome"] in {"same_entity", "component_of"}:
            latest = decisions[0]
            note = f": {e(latest['reason'])}" if latest["reason"] else ""
            return f"""<section class="decision-step"><h3>Identity reviewed</h3><p>{badge(latest['outcome'])} by {e(latest['actor'])}{note}</p></section>"""
        local_only = not str(record["external_id"] or "")
        parent_options = "".join(
            option(str(row["entity_id"]), f"{row['value'] or row['entity_id']} — {row['entity_id']}", preferred_parent)
            for row in entities
            if row["entity_id"] != record["entity_id"]
        )
        previous = ""
        if decisions:
            latest = decisions[0]
            previous = f'<div class="notice notice--yellow"><strong>Previous identity answer: {e(latest["outcome"].replace("_", " "))}</strong><p>No identity was accepted. Review again after correcting the local master candidates.</p></div>'
        heading = (
            "Does the selected exact-inventory local source belong to this archive item?"
            if local_only
            else "Do these images show the same archive item?"
        )
        choices = (
            f"""
{option('same_entity', 'Yes — accept the selected local source identity')}
{option('wrong_master', 'The attached local file belongs to another record')}
{option('defer', 'I am not sure yet')}
"""
            if local_only
            else f"""
{option('same_entity', 'Yes — the images show the same item')}
{option('component_of', 'This photograph is part of a larger carrier')}
{option('wrong_master', 'The attached local file belongs to another record')}
{option('not_same', 'No — the images show different items')}
{option('defer', 'I am not sure yet')}
"""
        )
        parent_control = (
            ""
            if local_only
            else f"""<label>Larger carrier, only for component decisions<select name="parent_entity_id"><option value="">Not applicable</option>{parent_options}</select></label>"""
        )
        return f"""<section class="decision-step"><h3>{heading}</h3>{previous}
<form class="form" method="post" action="/reconcile/identity" data-async-save>{self.csrf()}
{return_hidden}<input type="hidden" name="record_id" value="{e(record['record_id'])}">
<label>Decision<select name="outcome" required>
<option value="">Choose…</option>
{choices}
</select></label>
{parent_control}
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Record identity decision</button></form></section>"""

    def render_field(
        self,
        record: sqlite3.Row,
        field_key: str,
        candidates: list[sqlite3.Row],
        decision: sqlite3.Row | None,
        *,
        return_to: str = "",
    ) -> str:
        rows = "".join(
            f"""<tr><th scope="row">{e(candidate['source_side'].replace('_', ' '))}</th><td>{e(candidate['value_text']) or '—'}</td><td>{badge(candidate['status'])}</td></tr>"""
            for candidate in candidates
        )
        label = str(candidates[0]["field_label"]) if candidates else field_key
        action_labels = {
            "keep_catalog": "Kept the existing archive value",
            "accept_local": "Accepted from the local source",
            "accept_external": "Accepted from museum-digital",
            "corrected": "Accepted editor wording",
            "defer": "Decision deferred",
        }
        available_sides = {
            str(candidate["source_side"])
            for candidate in candidates
            if candidate["status"] == "available"
            and str(candidate["value_text"] or "").strip()
        }

        def source_option(value: str, label_text: str, side: str) -> str:
            if side in available_sides:
                return option(value, label_text)
            return (
                f'<option value="{e(value)}" disabled>'
                f'{e(label_text)} — unavailable</option>'
            )

        current = ""
        if decision:
            reason = (
                f'<br><small>Reason: {e(decision["reason"])}</small>'
                if decision["reason"]
                else ""
            )
            value = e(decision["value_text"]) or "No value accepted"
            current = f'<div class="notice notice--safe"><strong>Archive currently: {value}</strong><p>{e(action_labels.get(decision["action"], decision["action"]))}{reason}</p></div>'
        form = f"""<form class="form" method="post" action="/reconcile/field" data-async-save>{self.csrf()}
<input type="hidden" name="return_to" value="{e(return_to)}"><input type="hidden" name="record_id" value="{e(record['record_id'])}"><input type="hidden" name="field_key" value="{e(field_key)}">
<label>Editor choice<select name="action" required>
<option value="">Choose…</option>
{source_option('keep_catalog', 'Keep current archive value', 'catalog')}
{source_option('accept_local', 'Use preserved local source value', 'local')}
{source_option('accept_external', 'Use museum-digital value', 'external')}
{option('corrected', 'Use corrected archive value')}
{option('defer', 'Decide later')}
</select></label>
<label>Corrected value, only when selected<textarea name="corrected_value"></textarea></label>
<details><summary>Add a reason (optional)</summary><label>Reason<textarea name="reason"></textarea></label></details>
<button class="button" type="submit">Record {e(label)} decision</button></form>"""
        controls = (
            f'<details class="change-decision"><summary>Change this decision</summary>{form}</details>'
            if decision
            else form
        )
        return f"""<section class="field-decision"><h3>{e(label)}</h3>{current}
<div class="comparison-fields table-wrap"><table><thead><tr><th>Source</th><th>Value</th><th>Availability</th></tr></thead><tbody>{rows}</tbody></table></div>
{controls}</section>"""

    def page_reconciliation_image(self) -> None:
        evidence_id = self.parameters().get("id", "")
        path = reconciliation.safe_cached_evidence_path(self.root, evidence_id)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.casefold() in {".ppm", ".pgm", ".pbm"}:
            content_type = "image/x-portable-pixmap"
        self.send_bytes(path.read_bytes(), content_type)

    def pdf_plan_dir(self) -> Path:
        return self.root / "work" / "reconciliation" / "pdf"

    def page_pdfs(self) -> None:
        rows: list[dict[str, Any]] = []
        directory = self.pdf_plan_dir()
        if directory.is_dir():
            for path in sorted(directory.glob("*.plan.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    rows.append({"path": path, "payload": payload})
        cards = "".join(
            f"""<article class="object-card"><div class="object-card__body">
  <p>{badge("ready_for_pdf_accession")}</p>
  <h2><a href="{url('/pdf', plan=row['path'].name)}">{e(row['payload'].get('archival_label') or row['payload'].get('entity_id'))}</a></h2>
  <p>{e(row['payload'].get('museum_digital', {}).get('title', ''))}</p>
  <p class="technical">{e(row['payload'].get('unit_id'))} · {e(row['payload'].get('selected_candidate'))}</p>
</div></article>"""
            for row in rows
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Document preservation</p><h1>PDF reconciliation</h1>
<p>Local and museum-digital PDF candidates are compared without changing either source. OCR remains a separate rebuildable indexing branch.</p></section>
<section class="object-grid">{cards or '<div class="empty">No PDF reconciliation plans yet.</div>'}</section>"""
        self.send_html("PDF reconciliation", body, active="pdfs")

    def page_pdf(self) -> None:
        name = self.parameters().get("plan", "")
        if Path(name).name != name or not name.endswith(".plan.json"):
            raise reconciliation.ReconciliationError("invalid PDF plan name")
        path = self.pdf_plan_dir() / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise reconciliation.ReconciliationError("invalid PDF plan")
        selected_id = str(payload.get("selected_candidate") or "")
        candidate_rows = ""
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            inspection = candidate.get("inspection")
            if not isinstance(inspection, dict):
                inspection = {}
            selected = candidate.get("candidate_id") == selected_id
            candidate_rows += f"""<tr>
<td>{badge("selected" if selected else "candidate")}</td>
<td>{e(candidate.get('candidate_id'))}</td>
<td>{e(candidate.get('source_kind'))}</td>
<td>{e(inspection.get('pages'))}</td>
<td>{e(inspection.get('size_bytes'))}</td>
<td><code>{e(inspection.get('sha256'))}</code></td>
<td>{e(inspection.get('text_status'))}</td>
</tr>"""
        md = payload.get("museum_digital")
        if not isinstance(md, dict):
            md = {}
        indexing = payload.get("indexing_branch")
        if not isinstance(indexing, dict):
            indexing = {}
        body = f"""
<section class="page-heading"><p class="eyebrow">PDF provenance restoration</p>
<h1>{e(payload.get('archival_label') or payload.get('entity_id'))}</h1>
<p>{e(md.get('title'))}</p></section>
<section class="health-banner"><div><p class="eyebrow">Selected source</p><h2>{e(selected_id)}</h2></div>
<p>Catalog revision {e(payload.get('catalog_revision'))}<br>OCR execution: {e(indexing.get('ocr_execution'))}</p></section>
<section class="panel"><h2>Candidate comparison</h2><div class="table-scroll"><table>
<thead><tr><th>State</th><th>Candidate</th><th>Source</th><th>Pages</th><th>Bytes</th><th>SHA-256</th><th>Text/index state</th></tr></thead>
<tbody>{candidate_rows}</tbody></table></div></section>
<section class="panel"><h2>Selection note</h2><p>{e(payload.get('selection_note') or 'No optional note supplied.')}</p>
<p class="technical">Museum-digital object {e(md.get('object_id'))} · inventory {e(md.get('inventory_number'))} · entity {e(payload.get('entity_id'))}</p></section>"""
        self.send_html("PDF plan", body, active="pdfs")

    def page_publication(self) -> None:
        config = catalog_core.load_config(self.root)
        preservation_root = str(config.get("independent_generation_root") or "")
        with closing(self.catalog(read_only=True)) as connection:
            state = catalog_core.catalog_state(connection)
            current = connection.execute(
                """SELECT generation_id, manifest_digest, catalog_revision
                   FROM filesystem_generations WHERE status = 'current'"""
            ).fetchone()
        is_current = int(state["catalog_revision"]) == int(state["published_revision"])
        form = (
            f"""<form class="form form--narrow" method="post" action="/core/publish">{self.csrf()}<label>Type PUBLISH to confirm<input name="confirmation" required></label><button class="button" type="submit" {'disabled' if is_current else ''}>Publish and verify generation</button></form>"""
            if preservation_root
            else '<div class="notice notice--yellow">No independent generation root is configured. Run the first publication through <code>archive.py core-publish</code>.</div>'
        )
        body = f"""
<section class="page-heading"><p class="eyebrow">Filesystem authority</p><h1>Publication</h1><p>Publication writes a complete open CSV/JSONL generation, verifies its independent copy, then advances filesystem CURRENT.</p></section>
<section class="health-banner {'health-banner--yellow' if not is_current else ''}"><div><p class="eyebrow">Freshness</p><h2>{'Current' if is_current else 'Unpublished'}</h2></div><p>Catalog {state['catalog_revision']} · published {state['published_revision']}</p></section>
<section class="panel"><h2>Current generation</h2><p>{e(current['generation_id'] if current else 'None')}</p><p class="technical">{e(current['manifest_digest'] if current else '')}</p><p>Independent root: {e(preservation_root) or 'not configured'}</p>{form}</section>"""
        self.send_html("Publication", body, active="publication")

    def action_create_unit(self, form: dict[str, str]) -> None:
        with closing(self.catalog()) as connection:
            result = catalog_core.create_unit(
                connection,
                unit_id=form.get("unit_id", "").strip(),
                title=form.get("title", "").strip(),
                kind=form.get("kind", "").strip(),
                actor=self.server.actor,
                note=form.get("note", "").strip(),
            )
        self.redirect(url("/create", notice=f"Unit {result['unit_id']} created; catalog is unpublished."))

    def action_create_entity(self, form: dict[str, str]) -> None:
        with closing(self.catalog()) as connection:
            result = catalog_core.create_entity(
                connection,
                kind="archive_object",
                canonical_name=form.get("canonical_name", "").strip(),
                unit_id=form.get("unit_id", "").strip(),
                membership_role="primary",
                actor=self.server.actor,
                assignment_year=int(form.get("assignment_year", "")),
                domain={
                    "archival_label": form.get("archival_label", "").strip(),
                    "descriptive_title": form.get("descriptive_title", "").strip(),
                    "publication_status": "unknown",
                    "note": form.get("note", "").strip(),
                },
                note=form.get("note", "").strip(),
            )
        self.redirect(url("/entity", id=result["entity_id"], notice="Entity created; catalog is unpublished."))

    def action_publish(self, form: dict[str, str]) -> None:
        if form.get("confirmation", "") != "PUBLISH":
            raise reconciliation.ReconciliationError("publication confirmation must be PUBLISH")
        config = catalog_core.load_config(self.root)
        preservation_root = str(config.get("independent_generation_root") or "")
        if not preservation_root:
            raise reconciliation.ReconciliationError("independent generation root is not configured")
        result = catalog_core.publish_generation(
            self.root,
            Path(preservation_root),
            tool_version=catalog_core.TOOL_VERSION,
            actor=self.server.actor,
        )
        self.redirect(url("/publication", notice=f"Generation {result['generation_id']} published and verified."))

    def action_link_record(self, form: dict[str, str]) -> None:
        with closing(self.campaign()) as campaign, closing(
            self.catalog(read_only=True)
        ) as catalog:
            reconciliation.link_record_entity(
                campaign,
                catalog,
                record_id=form.get("record_id", ""),
                entity_id=form.get("entity_id", ""),
            )
        self.redirect(url("/reconcile/record", id=form.get("record_id", ""), notice="Campaign record linked for identity review."))

    def action_identity(self, form: dict[str, str]) -> None:
        inventory = self.recovery_inventory_for_record(
            form.get("record_id", "")
        )
        if inventory:
            self.require_recovery_record_editable(inventory)
        with closing(self.campaign()) as campaign, closing(
            self.catalog()
        ) as catalog:
            campaign_record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE record_id=?""",
                (form.get("record_id", ""),),
            ).fetchone()
            if not campaign_record:
                raise reconciliation.ReconciliationError(
                    "unknown reconciliation record"
                )
            if not str(campaign_record["external_id"] or ""):
                result = reconciliation.review_local_identity(
                    campaign,
                    catalog,
                    record_id=form.get("record_id", ""),
                    outcome=form.get("outcome", ""),
                    actor=self.server.actor,
                    reason=form.get("reason", ""),
                )
            else:
                result = reconciliation.review_identity(
                    campaign,
                    catalog,
                    record_id=form.get("record_id", ""),
                    outcome=form.get("outcome", ""),
                    actor=self.server.actor,
                    reason=form.get("reason", ""),
                    parent_entity_id=form.get("parent_entity_id", ""),
                )
            if self.server.recovery_mode and result.get("state") != "unchanged":
                row = campaign.execute(
                    """SELECT campaign_id, inventory_label
                       FROM reconciliation_records WHERE record_id=?""",
                    (form.get("record_id", ""),),
                ).fetchone()
                if row and row["campaign_id"] == "MD-RECOVERY-WF-PARAGON":
                    catalog_core.set_reconciliation_review_status(
                        catalog,
                        entity_id=self.server.recovery_entity_id(
                            str(row["inventory_label"])
                        ),
                        status="not_ready",
                        actor=self.server.actor,
                    )
        destination = form.get("return_to", "")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = url("/reconcile/record", id=form.get("record_id", ""))
        separator = "&" if "?" in destination else "?"
        self.redirect(destination + separator + urllib.parse.urlencode({"notice": f"Identity decision recorded; core state is {result['state']}."}))

    def action_field(self, form: dict[str, str]) -> None:
        inventory = self.recovery_inventory_for_record(
            form.get("record_id", "")
        )
        if inventory:
            self.require_recovery_record_editable(inventory)
        with closing(self.campaign()) as campaign, closing(
            self.catalog()
        ) as catalog:
            result = reconciliation.decide_field(
                campaign,
                catalog,
                record_id=form.get("record_id", ""),
                field_key=form.get("field_key", ""),
                action=form.get("action", ""),
                actor=self.server.actor,
                reason=form.get("reason", ""),
                corrected_value=form.get("corrected_value", ""),
            )
            if self.server.recovery_mode and result.get("state") != "unchanged":
                row = campaign.execute(
                    """SELECT campaign_id, inventory_label
                       FROM reconciliation_records WHERE record_id=?""",
                    (form.get("record_id", ""),),
                ).fetchone()
                if row and row["campaign_id"] == "MD-RECOVERY-WF-PARAGON":
                    catalog_core.set_reconciliation_review_status(
                        catalog,
                        entity_id=self.server.recovery_entity_id(
                            str(row["inventory_label"])
                        ),
                        status="not_ready",
                        actor=self.server.actor,
                    )
        destination = form.get("return_to", "")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = url("/reconcile/record", id=form.get("record_id", ""))
        separator = "&" if "?" in destination else "?"
        self.redirect(destination + separator + urllib.parse.urlencode({"notice": f"Metadata decision recorded; core state is {result['state']}."}))

    def action_surrogate_swap_review(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode or not self.server.surrogate_swap_source:
            raise reconciliation.ReconciliationError(
                "surrogate-swap review requires bounded recovery mode with a source"
            )
        inventory = form.get("inventory", "").strip()
        candidate_key = form.get("candidate_key", "").strip()
        candidate = self._surrogate_swap_candidate(inventory, candidate_key)
        source = self._surrogate_swap_source(candidate)
        candidate_width, candidate_height = self._image_dimensions(source)
        active = self._surrogate_swap_active_rows().get(inventory)
        if not active:
            raise reconciliation.ReconciliationError(
                "record no longer has an active preservation surrogate"
            )
        with closing(self.catalog()) as catalog:
            result = catalog_core.review_surrogate_swap_candidate(
                catalog,
                entity_id=str(active["entity_id"]),
                candidate_key=candidate_key,
                candidate_path=str(source),
                candidate_size_bytes=int(candidate["size_bytes"]),
                candidate_width_px=candidate_width,
                candidate_height_px=candidate_height,
                active_digest=str(active["content_digest"]),
                active_size_bytes=int(active["size_bytes"]),
                active_width_px=int(active["width_px"] or 0),
                active_height_px=int(active["height_px"] or 0),
                state=form.get("state", ""),
                actor=self.server.actor,
                reason=form.get("reason", ""),
            )
        destination = url(
            "/surrogate-swaps",
            state=(
                "marked_for_swap"
                if result["state"] == "marked_for_swap"
                else result["state"]
            ),
        )
        self.redirect(
            destination
            + "&"
            + urllib.parse.urlencode(
                {
                    "notice": (
                        f"{inventory} surrogate-swap candidate marked "
                        f"{result['state']}. No archive file or source file "
                        "was changed."
                    )
                }
            )
        )

    def action_recovery_metadata_extra(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode:
            raise reconciliation.ReconciliationError(
                "recovery metadata action requires a bounded recovery scope"
            )
        inventory = form.get("inventory", "").strip()
        record = self.server.recovery_records.get(inventory)
        if not record:
            raise reconciliation.ReconciliationError("unknown recovery record")
        self.require_recovery_record_editable(inventory)
        with closing(self.campaign(read_only=True)) as campaign:
            campaign_record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE campaign_id = 'MD-RECOVERY-WF-PARAGON'
                     AND inventory_label = ?""",
                (inventory,),
            ).fetchone()
        if not campaign_record or campaign_record["identity_status"] != "resolved":
            raise reconciliation.ReconciliationError(
                "object type and tags require a resolved visual identity"
            )
        local = record["local"]
        md = record["md"]
        field_decisions: dict[str, dict[str, str]] = {}
        object_source = form.get("object_type_source", "").strip()
        if object_source:
            object_value = {
                "local": str(local.get("object_type") or "").strip(),
                "museum_digital": str(md.get("object_type") or "").strip(),
                "corrected": form.get("corrected_object_type", "").strip(),
            }.get(object_source, "")
            if not object_value:
                raise reconciliation.ReconciliationError(
                    "selected object type value is unavailable"
                )
            field_decisions["object_type"] = {
                "source": object_source,
                "value": object_value,
            }
        tag_decision: dict[str, Any] | None = None
        tag_source = form.get("tag_source", "").strip()
        if tag_source:
            if tag_source == "museum_digital":
                tag_values = [
                    str(item.get("tag_name") if isinstance(item, dict) else item).strip()
                    for item in (md.get("object_tags") or [])
                    if str(item.get("tag_name") if isinstance(item, dict) else item).strip()
                ]
            elif tag_source == "local":
                tag_values = [
                    str(value).strip()
                    for value in (local.get("tags") or [])
                    if str(value).strip()
                ]
            elif tag_source == "corrected":
                tag_values = [
                    value.strip()
                    for value in re.split(r"[\n;,]+", form.get("corrected_tags", ""))
                    if value.strip()
                ]
            elif tag_source == "archive":
                with closing(self.catalog(read_only=True)) as catalog:
                    tag_values = list(
                        editor_reconcile.accepted_metadata(
                            catalog, str(campaign_record["entity_id"])
                        )["tags"]
                    )
            else:
                raise reconciliation.ReconciliationError("unsupported tag source")
            tag_decision = {"source": tag_source, "values": tag_values}
        if not field_decisions and tag_decision is None:
            raise reconciliation.ReconciliationError(
                "choose an object type or tag decision"
            )
        with closing(self.catalog()) as catalog:
            result = editor_reconcile.reconcile_metadata(
                catalog,
                unit_id="WF-PARAGON",
                entity_id=str(campaign_record["entity_id"]),
                field_decisions=field_decisions,
                tag_decision=tag_decision,
                actor=self.server.actor,
                reason=form.get("reason", ""),
                evidence=str(campaign_record["snapshot_digest"] or ""),
                external_id=str(campaign_record["external_id"] or ""),
            )
            if result.get("state") != "unchanged":
                catalog_core.set_reconciliation_review_status(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    status="not_ready",
                    actor=self.server.actor,
                )
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=f"Object type or tags recorded; core state is {result['state']}.",
            )
        )

    def action_recovery_candidate_review(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode:
            raise reconciliation.ReconciliationError(
                "source-candidate review requires bounded recovery mode"
            )
        record_id = form.get("record_id", "").strip()
        evidence_id = form.get("evidence_id", "").strip()
        with closing(self.campaign(read_only=True)) as campaign:
            campaign_record = campaign.execute(
                "SELECT * FROM reconciliation_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            evidence = campaign.execute(
                """SELECT * FROM visual_evidence
                   WHERE evidence_id = ? AND record_id = ?""",
                (evidence_id, record_id),
            ).fetchone()
            local_evidence = list(
                campaign.execute(
                    """SELECT * FROM visual_evidence
                       WHERE record_id = ? AND role <> 'museum_digital_snapshot'""",
                    (record_id,),
                )
            )
        if (
            not campaign_record
            or campaign_record["campaign_id"] != "MD-RECOVERY-WF-PARAGON"
            or not evidence
            or evidence["role"] == "museum_digital_snapshot"
        ):
            raise reconciliation.ReconciliationError(
                "source-candidate evidence is unavailable"
            )
        inventory = str(campaign_record["inventory_label"])
        record = self.server.recovery_records.get(inventory)
        if not record:
            raise reconciliation.ReconciliationError("unknown recovery record")
        self.require_recovery_record_editable(inventory)
        source = self._recovery_evidence_source(record, evidence)
        candidate_role, _label = self._recovery_candidate_role(
            record, evidence, local_evidence
        )
        state = form.get("state", "")
        with closing(self.catalog()) as catalog:
            if state == "tiff_master_generated_derivative":
                candidates = []
                for item in local_evidence:
                    item_source = Path(str(item["source_locator"]))
                    role, _label = self._recovery_candidate_role(
                        record, item, local_evidence
                    )
                    candidates.append(
                        {
                            "evidence_id": str(item["evidence_id"]),
                            "content_digest": str(item["content_digest"]),
                            "candidate_role": role,
                            "source_name": item_source.name,
                        }
                    )
                chosen = next(
                    item for item in candidates if item["evidence_id"] == evidence_id
                )
                result = catalog_core.select_tiff_master_with_generated_derivative(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    master_candidate=chosen,
                    other_candidates=[
                        item for item in candidates if item["evidence_id"] != evidence_id
                    ],
                    actor=self.server.actor,
                    reason=form.get("reason", ""),
                )
            else:
                result = catalog_core.review_source_candidate(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    evidence_id=evidence_id,
                    content_digest=str(evidence["content_digest"]),
                    candidate_role=candidate_role,
                    source_name=source.name,
                    state=state,
                    actor=self.server.actor,
                    reason=form.get("reason", ""),
                )
            if result.get("operation_state") != "unchanged":
                catalog_core.set_reconciliation_review_status(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    status="not_ready",
                    actor=self.server.actor,
                )
        destination = form.get("return_to", "")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = url("/compare", record=inventory)
        separator = "&" if "?" in destination else "?"
        self.redirect(
            destination
            + separator
            + urllib.parse.urlencode(
                {
                    "notice": (
                        (
                            "TIFF selected as master; supplied JPEGs retained as "
                            "evidence and the archive derivative will be generated. "
                            if state == "tiff_master_generated_derivative"
                            else f"Source candidate marked {result['state']}. "
                        )
                        + "No source file was changed."
                    )
                }
            )
        )

    def action_recovery_visual_confirm(self, form: dict[str, str]) -> None:
        """Record identity, exact master, and available local metadata."""

        record_id = form.get("record_id", "").strip()
        evidence_id = form.get("evidence_id", "").strip()
        with closing(self.campaign()) as campaign, closing(self.catalog()) as catalog:
            campaign_record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE record_id=? AND campaign_id='MD-RECOVERY-WF-PARAGON'""",
                (record_id,),
            ).fetchone()
            evidence = campaign.execute(
                """SELECT * FROM visual_evidence
                   WHERE evidence_id=? AND record_id=?
                     AND role<>'museum_digital_snapshot'""",
                (evidence_id, record_id),
            ).fetchone()
            local_evidence = list(
                campaign.execute(
                    """SELECT * FROM visual_evidence
                       WHERE record_id=? AND role<>'museum_digital_snapshot'""",
                    (record_id,),
                )
            )
            if not campaign_record or not evidence:
                raise reconciliation.ReconciliationError(
                    "visual confirmation evidence is unavailable"
                )
            inventory = str(campaign_record["inventory_label"])
            record = self.server.recovery_records.get(inventory)
            if not record:
                raise reconciliation.ReconciliationError(
                    "visual confirmation escaped the bounded recovery"
                )
            self.require_recovery_record_editable(inventory)
            source = self._recovery_evidence_source(record, evidence)
            candidate_role, _label = recovery_candidate_role(
                record, evidence, local_evidence
            )
            if candidate_role not in {
                "master_tiff_candidate",
                "high_resolution_jpeg_candidate",
            }:
                raise reconciliation.ReconciliationError(
                    "visual confirmation requires a master or surrogate candidate"
                )
            reason = form.get("reason", "")
            if candidate_role == "master_tiff_candidate":
                candidates = []
                for item in local_evidence:
                    item_source = Path(str(item["source_locator"]))
                    role, _label = recovery_candidate_role(
                        record, item, local_evidence
                    )
                    candidates.append(
                        {
                            "evidence_id": str(item["evidence_id"]),
                            "content_digest": str(item["content_digest"]),
                            "candidate_role": role,
                            "source_name": item_source.name,
                        }
                    )
                chosen = next(
                    item for item in candidates if item["evidence_id"] == evidence_id
                )
                selection = catalog_core.select_tiff_master_with_generated_derivative(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    master_candidate=chosen,
                    other_candidates=[
                        item for item in candidates if item["evidence_id"] != evidence_id
                    ],
                    actor=self.server.actor,
                    reason=reason,
                )
            else:
                selection = catalog_core.review_source_candidate(
                    catalog,
                    entity_id=str(campaign_record["entity_id"]),
                    evidence_id=evidence_id,
                    content_digest=str(evidence["content_digest"]),
                    candidate_role=candidate_role,
                    source_name=source.name,
                    state="selected_master",
                    actor=self.server.actor,
                    reason=reason,
                )
            local_only = not str(campaign_record["external_id"] or "")
            if local_only:
                identity = reconciliation.review_local_identity(
                    campaign,
                    catalog,
                    record_id=record_id,
                    outcome="same_entity",
                    actor=self.server.actor,
                    reason=reason,
                )
                for field_key, _label, _catalog, _local, _external in (
                    reconciliation.FIELD_DEFINITIONS
                ):
                    candidate = campaign.execute(
                        """SELECT value_text, status FROM field_candidates
                           WHERE record_id=? AND field_key=?
                             AND source_side='local'""",
                        (record_id, field_key),
                    ).fetchone()
                    if (
                        candidate
                        and candidate["status"] == "available"
                        and str(candidate["value_text"] or "").strip()
                    ):
                        reconciliation.decide_field(
                            campaign,
                            catalog,
                            record_id=record_id,
                            field_key=field_key,
                            action="accept_local",
                            actor=self.server.actor,
                            reason=reason
                            or "Accepted with the exact local source review.",
                        )
                local = record["local"]
                object_type = str(local.get("object_type") or "").strip()
                local_tags = [
                    str(value).strip()
                    for value in (local.get("tags") or [])
                    if str(value).strip()
                ]
                if object_type or local_tags:
                    editor_reconcile.reconcile_metadata(
                        catalog,
                        unit_id="WF-PARAGON",
                        entity_id=str(campaign_record["entity_id"]),
                        field_decisions=(
                            {
                                "object_type": {
                                    "source": "local",
                                    "value": object_type,
                                }
                            }
                            if object_type
                            else {}
                        ),
                        tag_decision=(
                            {"source": "local", "values": local_tags}
                            if local_tags
                            else None
                        ),
                        actor=self.server.actor,
                        reason=reason
                        or "Accepted with the exact local source review.",
                        evidence=str(campaign_record["source_record_locator"] or ""),
                        external_id="",
                    )
            else:
                identity = reconciliation.review_identity(
                    campaign,
                    catalog,
                    record_id=record_id,
                    outcome="same_entity",
                    actor=self.server.actor,
                    reason=reason,
                )
            catalog_core.set_reconciliation_review_status(
                catalog,
                entity_id=str(campaign_record["entity_id"]),
                status="not_ready",
                actor=self.server.actor,
                reason=reason,
            )
        next_inventory = self.server.next_visual_inventory(inventory)
        destination = (
            url("/compare", record=next_inventory)
            if next_inventory
            else url("/browse", view="ready")
        )
        self.redirect(
            destination
            + ("&" if "?" in destination else "?")
            + urllib.parse.urlencode(
                {
                    "notice": (
                        f"{inventory} reviewed and queued for accession; "
                        f"identity {identity['state']}, master {selection['operation_state']}."
                    )
                }
            )
        )

    def action_recovery_carrier(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode:
            raise reconciliation.ReconciliationError(
                "carrier assignment requires a bounded recovery scope"
            )
        inventory = form.get("record", "").strip()
        mode = form.get("mode", "").strip()
        related = form.get("related_inventory", "").strip()
        if mode == "set_parent":
            component_inventory = inventory
            carrier_inventory = related
        elif mode == "add_child":
            component_inventory = related
            carrier_inventory = inventory
        elif mode == "clear_parent":
            component_inventory = inventory
            carrier_inventory = ""
        else:
            raise reconciliation.ReconciliationError(
                "unsupported carrier assignment action"
            )
        self.require_recovery_record_editable(component_inventory)
        if carrier_inventory:
            self.require_recovery_record_editable(carrier_inventory)
        component_entity_id = self.server.recovery_entity_id(component_inventory)
        carrier_entity_id = (
            self.server.recovery_entity_id(carrier_inventory)
            if carrier_inventory
            else ""
        )
        with closing(self.catalog()) as catalog:
            result = catalog_core.set_component_carrier(
                catalog,
                component_entity_id=component_entity_id,
                carrier_entity_id=carrier_entity_id,
                actor=self.server.actor,
                reason=form.get("reason", ""),
            )
            if result.get("state") != "unchanged":
                for changed_entity_id in {
                    component_entity_id,
                    carrier_entity_id,
                } - {""}:
                    catalog_core.set_reconciliation_review_status(
                        catalog,
                        entity_id=changed_entity_id,
                        status="not_ready",
                        actor=self.server.actor,
                    )
        notice = (
            f"Component assigned to {carrier_inventory}"
            if carrier_inventory
            else "Component assignment removed"
        )
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=f"{notice}; core state is {result['state']}.",
            )
        )

    def action_recovery_review_status(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode:
            raise reconciliation.ReconciliationError(
                "review status requires a bounded recovery scope"
            )
        inventory = form.get("inventory", "").strip()
        status = form.get("status", "").strip()
        related_inventory = form.get("related_inventory", "").strip()
        self.require_recovery_record_editable(inventory)
        entity_id = self.server.recovery_entity_id(inventory)
        retained_entity_id = ""
        if related_inventory:
            if status != "rejected":
                raise reconciliation.ReconciliationError(
                    "a retained duplicate is only valid when rejecting a record"
                )
            if self.server.recovery_review_states().get(related_inventory) == "rejected":
                raise reconciliation.ReconciliationError(
                    "the retained duplicate target is itself rejected"
                )
            retained_entity_id = self.server.recovery_entity_id(
                related_inventory
            )
        with closing(self.campaign(read_only=True)) as campaign:
            record = campaign.execute(
                """SELECT identity_status FROM reconciliation_records
                   WHERE campaign_id='MD-RECOVERY-WF-PARAGON'
                     AND inventory_label=?""",
                (inventory,),
            ).fetchone()
        if not record:
            raise reconciliation.ReconciliationError(
                "recovery record is not prepared"
            )
        if status == "reconciled" and record["identity_status"] != "resolved":
            raise reconciliation.ReconciliationError(
                "resolve visual identity before marking this record reconciled"
            )
        with closing(self.catalog()) as catalog:
            result = catalog_core.set_reconciliation_review_status(
                catalog,
                entity_id=entity_id,
                status=status,
                actor=self.server.actor,
                reason=form.get("reason", ""),
                retained_entity_id=retained_entity_id,
            )
        label = {
            "reconciled": "Reconciled",
            "rejected": "Rejected and recorded for future drain review",
        }.get(status, "Reopened")
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=f"{label}; workflow state is {result['state']}.",
            )
        )

    def action_recovery_correction_status(self, form: dict[str, str]) -> None:
        if not self.server.recovery_mode:
            raise reconciliation.ReconciliationError(
                "correction status requires a bounded recovery scope"
            )
        inventory = form.get("inventory", "").strip()
        state = form.get("state", "").strip()
        record = self.server.recovery_records.get(inventory)
        if not record or not record.get("correction_required"):
            raise reconciliation.ReconciliationError(
                "record is not an audited correction"
            )
        if state not in {"open", "ready"}:
            raise reconciliation.ReconciliationError(
                "unsupported correction workflow state"
            )
        with closing(self.campaign(read_only=True)) as campaign:
            campaign_record = campaign.execute(
                """SELECT identity_status FROM reconciliation_records
                   WHERE campaign_id='MD-RECOVERY-WF-PARAGON'
                     AND inventory_label=?""",
                (inventory,),
            ).fetchone()
        if (
            not campaign_record
            or campaign_record["identity_status"] != "resolved"
        ):
            raise reconciliation.ReconciliationError(
                "resolve visual identity before marking a correction ready"
            )
        with closing(self.catalog()) as catalog:
            result = catalog_core.set_recovery_correction_status(
                catalog,
                entity_id=self.server.recovery_entity_id(inventory),
                state=state,
                actor=self.server.actor,
                reason=form.get("reason", ""),
            )
        label = (
            "Correction queued for controlled re-accession"
            if state == "ready"
            else "Correction reopened"
        )
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=f"{label}; core state is {result['state']}.",
            )
        )

    def action_recovery_accession_publish(self, form: dict[str, str]) -> None:
        if not self.server.recovery_accession_lock.acquire(blocking=False):
            raise reconciliation.ReconciliationError(
                "another recovery accession is already running; wait for it to finish"
            )
        try:
            self._action_recovery_accession_publish(form)
        finally:
            self.server.recovery_accession_lock.release()

    def action_recovery_corrective_publish(self, form: dict[str, str]) -> None:
        if not self.server.recovery_accession_lock.acquire(blocking=False):
            raise reconciliation.ReconciliationError(
                "another recovery accession is already running; wait for it to finish"
            )
        try:
            inventory = form.get("inventory", "").strip()
            result = recovery_correction.apply(
                self.root,
                inventory=inventory,
                actor=self.server.actor,
                reason=(
                    form.get("reason", "").strip()
                    or "Editor-triggered controlled corrective re-accession."
                ),
            )
        finally:
            self.server.recovery_accession_lock.release()
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=(
                    f"{inventory} corrective accession completed as "
                    f"{result['operation']}; published generation "
                    f"{result['publication']['generation_id']}."
                ),
            )
        )

    def _action_recovery_accession_publish(self, form: dict[str, str]) -> None:
        inventory = form.get("inventory", "").strip()
        result = self.server.accession_recovery_record(
            inventory,
            reason=form.get("reason", ""),
        )
        self.redirect(
            url(
                "/compare",
                record=inventory,
                notice=(
                    f"{inventory} accessioned and published as generation "
                    f"{result['publication']['generation_id']}; CSV and XLSX refreshed."
                ),
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--stress-manifest",
        type=Path,
        help=(
            "serve the read-only editor browse/comparison surface for one "
            "matching stress-pilot manifest"
        ),
    )
    parser.add_argument(
        "--visual-candidates",
        type=Path,
        help=(
            "bind one immutable visual-candidate report matching the stress "
            "pilot; candidates remain review evidence until explicitly staged"
        ),
    )
    parser.add_argument(
        "--recovery-scope",
        type=Path,
        help="serve one bounded unit-recovery scope from captured evidence",
    )
    parser.add_argument(
        "--surrogate-swap-source",
        type=Path,
        help=(
            "bind one read-only TIFF source tree for visual surrogate-to-master "
            "swap review; no accession is performed"
        ),
    )
    parser.add_argument(
        "--wf-sender-run",
        type=Path,
        help="serve the bounded collection-552 primary reconciliation run",
    )
    parser.add_argument(
        "--wf-sender-fallback-run",
        type=Path,
        help="bind the matching bounded WF-Sender fallback-search run",
    )
    parser.add_argument(
        "--kurt-schwarz-unit",
        type=Path,
        help=(
            "serve bounded reconciliation for the published Kurt Schwarz unit; "
            "decisions remain under the supplied work root"
        ),
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    batch = parser.add_mutually_exclusive_group()
    batch.add_argument(
        "--batch-accession-dry-run",
        action="store_true",
        help="print the immutable-selection plan for ready recovery records",
    )
    batch.add_argument(
        "--batch-accession-run",
        action="store_true",
        help="run the serial recovery batch bound to a prior plan digest",
    )
    parser.add_argument(
        "--batch-plan-digest",
        default="",
        help="exact plan digest printed by --batch-accession-dry-run",
    )
    parser.add_argument(
        "--batch-inventory",
        action="append",
        default=[],
        help="limit the dry-run and run to one inventory; repeat as needed",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=0,
        help="maximum number of ready records in this bound batch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    batch_mode = args.batch_accession_dry_run or args.batch_accession_run
    if batch_mode and args.recovery_scope is None:
        raise SystemExit("batch accession requires --recovery-scope")
    if args.batch_limit < 0:
        raise SystemExit("--batch-limit must not be negative")
    server = CoreWorkbenchServer(
        ("127.0.0.1", 0 if batch_mode else args.port),
        Path(args.root).expanduser(),
        args.actor,
        args.stress_manifest,
        args.visual_candidates,
        args.recovery_scope,
        args.surrogate_swap_source,
        args.wf_sender_run,
        args.wf_sender_fallback_run,
        args.kurt_schwarz_unit,
    )
    if batch_mode:
        try:
            plan = server.recovery_batch_plan(
                inventories=args.batch_inventory,
                limit=args.batch_limit,
            )
            if args.batch_accession_dry_run:
                print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
                return 0
            if not args.batch_plan_digest:
                raise SystemExit(
                    "--batch-accession-run requires --batch-plan-digest from the dry-run"
                )
            if args.batch_plan_digest != plan["plan_digest"]:
                raise SystemExit(
                    "batch plan changed; run a new dry-run and review its digest"
                )
            successes = []
            failures = []
            for item in plan["items"]:
                inventory = str(item["inventory"])
                try:
                    result = server.accession_recovery_record(
                        inventory,
                        reason=f"Bound batch {plan['plan_digest']}",
                        expected_binding=str(item["binding"]),
                    )
                    successes.append(
                        {
                            "inventory": inventory,
                            "generation_id": str(
                                result["publication"]["generation_id"]
                            ),
                        }
                    )
                    print(
                        f"accessioned {inventory} as "
                        f"{result['publication']['generation_id']}",
                        flush=True,
                    )
                except (
                    catalog_core.CoreCatalogError,
                    reconciliation.ReconciliationError,
                    tiff_accession.TiffAccessionError,
                    recovery_accession.RecoveryAccessionError,
                    surrogate_accession.SurrogateAccessionError,
                    sqlite3.Error,
                    OSError,
                    ValueError,
                ) as error:
                    failures.append({"inventory": inventory, "error": str(error)})
                    print(f"failed {inventory}: {error}", flush=True)
                    break
            report = {
                "format": "iss-recovery-accession-batch-result-v1",
                "plan_digest": plan["plan_digest"],
                "planned": int(plan["count"]),
                "succeeded": successes,
                "failed": failures,
            }
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 1 if failures else 0
        finally:
            server.server_close()
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"ISS Archive Core Workbench {WORKBENCH_VERSION}: {address}")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
