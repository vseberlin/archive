#!/usr/bin/env python3
"""Local human master-selection workbench for museum-digital collection 553.

The workbench writes only the existing master-decision and candidate-
disposition CSV projections below iss-archive-work. It never edits source or
reference payloads, performs accession, generates derivatives, changes the
published archive, or authorizes deletion.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import subprocess
import threading
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from PIL import Image, ImageOps


VERSION = "1.2.0-compound-md-master"
ROOT = Path("/mnt/volume/iss-archive-work/collection-553/work/reconciliation/unit-reconciliation-v1")
SOURCE_ROOT = Path("/media/vladimir/Elements")
OBJECTS_CSV = ROOT / "objects.csv"
CANDIDATES_CSV = ROOT / "master-candidates.csv"
DECISIONS_CSV = ROOT / "master-decisions.csv"
DISPOSITIONS_CSV = ROOT / "candidate-dispositions.csv"
RESOURCES_CSV = ROOT / "md-resources.csv"
SCENARIOS_CSV = ROOT / "preservation-scenarios.csv"
RELATIONSHIPS_CSV = ROOT / "object-relationships.csv"
EXCEPTIONS_CSV = ROOT / "integrity-exceptions.csv"
TITLE_LEADS_CSV = ROOT / "title-leads.csv"
PAGE_SCAN_EVIDENCE_CSV = ROOT / "page-scan-evidence.csv"
SUMMARY_JSON = ROOT / "preparation-summary.json"

DECISION_FIELDS = [
    "md_object_id",
    "inventory",
    "decision_state",
    "selected_candidate_id",
    "master_representation_type",
    "selected_resource_ids",
    "representation_note",
    "decision_note",
    "actor",
    "decided_at",
    "deletion_authorized",
]
DISPOSITION_FIELDS = [
    "candidate_id",
    "md_object_id",
    "candidate_path",
    "disposition",
    "reason",
    "deletion_authorized",
]
ALLOWED_DECISION_STATES = {"unresolved", "selected_master"}
ALLOWED_REPRESENTATION_TYPES = {"", "single_file", "ordered_md_resource_set"}
COMPOUND_MD_SELECTION = "__ordered_md_resource_set__"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fields,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise WorkbenchError("Elements does not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options") or "").split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise WorkbenchError(f"protected Elements mount guard failed: {row}")
    return f"{row['source']} {row['target']} {row['fstype']} {row['options']}"


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def human_size(value: str) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def short_hash(value: str) -> str:
    return f"{value[:16]}…" if value else "not indexed"


def normalized_note(value: str, limit: int = 500) -> str:
    return " ".join(value.split())[:limit]


def candidate_claims(candidate: dict[str, str]) -> set[str]:
    claims = {"path:" + str(Path(candidate["candidate_path"]).resolve())}
    digest = candidate.get("known_sha256", "").casefold()
    if digest:
        claims.add("sha256:" + digest)
    return claims


def resource_claims(resource: dict[str, str]) -> set[str]:
    claims = {"path:" + str(Path(resource["work_path"]).resolve())}
    digest = resource.get("sha256", "").casefold()
    if digest:
        claims.add("sha256:" + digest)
    return claims


def parsed_resource_ids(decision: dict[str, str]) -> list[str] | None:
    raw = decision.get("selected_resource_ids", "")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return value


class Workbench:
    def __init__(self, actor: str) -> None:
        self.actor = normalized_note(actor, 120)
        if not self.actor:
            raise WorkbenchError("a decision actor is required")
        self.lock = threading.Lock()
        self.summary = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.objects = read_csv(OBJECTS_CSV)
        self.candidates = read_csv(CANDIDATES_CSV)
        self.resources = read_csv(RESOURCES_CSV)
        self.scenarios = read_csv(SCENARIOS_CSV)
        self.relationships = read_csv(RELATIONSHIPS_CSV)
        self.exceptions = read_csv(EXCEPTIONS_CSV)
        self.title_leads = read_csv(TITLE_LEADS_CSV)
        self.page_scan_evidence = read_csv(PAGE_SCAN_EVIDENCE_CSV)
        self.objects_by_id = {row["md_object_id"]: row for row in self.objects}
        self.candidates_by_id = {row["candidate_id"]: row for row in self.candidates}
        self.resources_by_id = {row["resource_id"]: row for row in self.resources}
        self.scenarios_by_id = {row["md_object_id"]: row for row in self.scenarios}
        self.candidates_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.resources_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.relationships_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.exceptions_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.title_leads_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.page_scan_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.candidates:
            self.candidates_by_object[row["md_object_id"]].append(row)
        for row in self.resources:
            self.resources_by_object[row["md_object_id"]].append(row)
        for row in self.relationships:
            self.relationships_by_object[row["left_md_object_id"]].append(row)
            self.relationships_by_object[row["right_md_object_id"]].append(row)
        for row in self.exceptions:
            self.exceptions_by_object[row["md_object_id"]].append(row)
        for row in self.title_leads:
            self.title_leads_by_object[row["md_object_id"]].append(row)
        for row in self.page_scan_evidence:
            self.page_scan_by_object[row["md_object_id"]].append(row)
        self.validate_static()

    def validate_static(self) -> None:
        source_guard()
        if self.summary.get("format") != "iss-collection-553-reconciliation-preparation-v1":
            raise WorkbenchError("unsupported collection-553 preparation summary")
        expected = {
            "objects": 54,
            "resources": 66,
            "master_candidates": 244,
            "integrity_exceptions": 4,
        }
        for key, value in expected.items():
            if self.summary.get(key) != value:
                raise WorkbenchError(f"preparation summary {key} changed from {value}")
        if len(self.objects) != 54 or len(self.objects_by_id) != 54:
            raise WorkbenchError("workbench requires exactly 54 unique collection-553 objects")
        if len(self.candidates) != 244 or len(self.candidates_by_id) != 244:
            raise WorkbenchError("workbench requires exactly 244 unique prepared candidates")
        if len(self.resources) != 66 or len(self.resources_by_id) != 66:
            raise WorkbenchError("workbench requires all 66 unique MD resources")
        if len(self.scenarios) != 54 or set(self.scenarios_by_id) != set(self.objects_by_id):
            raise WorkbenchError("preservation scenarios do not cover exactly 54 objects")
        if set(self.objects_by_id) != set(self.candidates_by_object):
            raise WorkbenchError("each object must have at least one master candidate")
        if set(self.objects_by_id) != set(self.resources_by_object):
            raise WorkbenchError("each object must have at least one MD resource")
        for object_id, rows in self.resources_by_object.items():
            if sum(row["is_main"] == "true" for row in rows) != 1:
                raise WorkbenchError(f"MD {object_id} does not have exactly one main resource")
        evidence = (
            self.objects
            + self.candidates
            + self.resources
            + self.scenarios
            + self.relationships
            + self.exceptions
            + self.title_leads
            + self.page_scan_evidence
        )
        if any(row.get("deletion_authorized") != "false" for row in evidence):
            raise WorkbenchError("prepared evidence carries deletion authority")
        exception_paths = {row["source_path"] for row in self.exceptions}
        for candidate in self.candidates:
            path = Path(candidate["candidate_path"])
            if not path.is_file() or not (inside(path, SOURCE_ROOT) or inside(path, ROOT)):
                raise WorkbenchError(f"unsafe or missing candidate: {path}")
            digest = candidate.get("known_sha256", "")
            if digest and not HEX64.fullmatch(digest):
                raise WorkbenchError(f"invalid candidate SHA-256: {candidate['candidate_id']}")
            if candidate["candidate_path"] in exception_paths:
                raise WorkbenchError(f"integrity exception appears as a master candidate: {path}")
        for resource in self.resources:
            path = Path(resource["work_path"])
            if not path.is_file() or not inside(path, ROOT):
                raise WorkbenchError(f"unsafe or missing MD resource: {path}")
            if not HEX64.fullmatch(resource.get("sha256", "")):
                raise WorkbenchError(f"invalid resource SHA-256: {resource['resource_id']}")
        if len(self.exceptions) != 4 or any(
            row.get("disposition") != "hold_outside_master_and_drain_selection"
            for row in self.exceptions
        ):
            raise WorkbenchError("integrity exceptions are not in the expected hold state")
        audit = self.summary.get("page_scan_hash_audit") or {}
        if audit != {
            "pdf_resources": 62,
            "pdf_pages": 975,
            "usb_images_compared": 144966,
            "multi_page_complete_sequences": 0,
            "isolated_parent_page_matches": 4,
            "one_page_objects_with_candidates": 3,
            "scan_with_text_layer": 46,
            "image_only_scan": 16,
            "text_native_pdfs": 0,
            "all_pages_have_raster_images": True,
        }:
            raise WorkbenchError("page-scan hash audit summary changed")
        if len(self.page_scan_evidence) != 22:
            raise WorkbenchError("page-scan evidence must contain exactly 22 strong match rows")
        for row in self.page_scan_evidence:
            object_id = row.get("md_object_id", "")
            path = Path(row.get("source_path", ""))
            scope = row.get("candidate_scope", "")
            if object_id not in self.objects_by_id or not path.is_file() or not inside(path, SOURCE_ROOT):
                raise WorkbenchError(f"unsafe or missing page-scan evidence: {path}")
            if scope not in {"selectable_for_this_one_page_object", "related_component_evidence_only"}:
                raise WorkbenchError(f"invalid page-scan candidate scope: {scope}")
            selectable_paths = {
                candidate["candidate_path"] for candidate in self.candidates_by_object[object_id]
            }
            if scope == "selectable_for_this_one_page_object" and str(path) not in selectable_paths:
                raise WorkbenchError(f"selectable page-scan evidence lacks a candidate: {path}")
            if scope == "related_component_evidence_only" and str(path) in selectable_paths:
                raise WorkbenchError(f"parent component evidence became selectable: {path}")
            if int(row["pdf_page_count"]) > 1 and scope != "related_component_evidence_only":
                raise WorkbenchError("multi-page PDF scan evidence became a master candidate")

    def decisions(self) -> dict[str, dict[str, str]]:
        rows = read_csv(DECISIONS_CSV)
        if len(rows) != 54 or {row["md_object_id"] for row in rows} != set(self.objects_by_id):
            raise WorkbenchError("decision projection does not cover exactly 54 objects")
        return {row["md_object_id"]: row for row in rows}

    def ordered_resources(self, object_id: str) -> list[dict[str, str]]:
        return sorted(
            self.resources_by_object[object_id],
            key=lambda row: int(row["resource_position"]),
        )

    def main_md_candidate(self, object_id: str) -> dict[str, str]:
        rows = [
            row
            for row in self.candidates_by_object[object_id]
            if row["source_kind"] == "museum-digital_reference"
            and row["match_basis"] == "captured_md_main_resource"
        ]
        if len(rows) != 1:
            raise WorkbenchError(f"MD {object_id} does not have exactly one main-resource candidate")
        return rows[0]

    def decision_claims(self, object_id: str, decision: dict[str, str]) -> set[str]:
        selected_id = decision.get("selected_candidate_id", "")
        if not selected_id:
            return set()
        if decision.get("master_representation_type") == "ordered_md_resource_set":
            claims: set[str] = set()
            for resource in self.ordered_resources(object_id):
                claims.update(resource_claims(resource))
            return claims
        candidate = self.candidates_by_id.get(selected_id)
        return candidate_claims(candidate) if candidate else set()

    def dispositions_for(self, decisions: dict[str, dict[str, str]]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for candidate in sorted(
            self.candidates,
            key=lambda row: (int(row["md_object_id"]), row["candidate_id"]),
        ):
            decision = decisions[candidate["md_object_id"]]
            selected = decision["selected_candidate_id"]
            if selected:
                disposition = "selected_master" if candidate["candidate_id"] == selected else "redundant"
                reason = (
                    (
                        "anchor member of the human-selected ordered museum-digital resource-set Master"
                        if decision.get("master_representation_type") == "ordered_md_resource_set"
                        else "human selected this file as the single-file Master"
                    )
                    if disposition == "selected_master"
                    else "not selected after human confirmation of the one Master representation"
                )
            else:
                disposition = "pending"
                reason = "object left unresolved; awaiting human master decision"
            rows.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "md_object_id": candidate["md_object_id"],
                    "candidate_path": candidate["candidate_path"],
                    "disposition": disposition,
                    "reason": reason,
                    "deletion_authorized": "false",
                }
            )
        return rows

    def selected_conflicts(
        self,
        decisions: dict[str, dict[str, str]],
        current_object: str,
    ) -> dict[str, str]:
        selected_claims: dict[str, str] = {}
        for object_id, decision in decisions.items():
            selected_id = decision.get("selected_candidate_id", "")
            if object_id == current_object or not selected_id:
                continue
            for claim in self.decision_claims(object_id, decision):
                selected_claims[claim] = object_id
        conflicts: dict[str, str] = {}
        for candidate in self.candidates_by_object[current_object]:
            matching = sorted({selected_claims[claim] for claim in candidate_claims(candidate) if claim in selected_claims})
            if matching:
                conflicts[candidate["candidate_id"]] = matching[0]
        return conflicts

    def validate_decisions(self, decisions: dict[str, dict[str, str]]) -> list[str]:
        errors: list[str] = []
        claims: dict[str, str] = {}
        for object_id, decision in decisions.items():
            selected = decision.get("selected_candidate_id", "")
            state = decision.get("decision_state", "")
            representation_type = decision.get("master_representation_type", "")
            resource_ids = parsed_resource_ids(decision)
            if state not in ALLOWED_DECISION_STATES:
                errors.append(f"invalid decision state for {object_id}")
            if representation_type not in ALLOWED_REPRESENTATION_TYPES:
                errors.append(f"invalid Master representation type for {object_id}")
            if resource_ids is None:
                errors.append(f"invalid selected resource list for {object_id}")
                resource_ids = []
            allowed = {row["candidate_id"] for row in self.candidates_by_object[object_id]}
            if selected and selected not in allowed:
                errors.append(f"foreign selected candidate for {object_id}")
                continue
            if bool(selected) != (state == "selected_master"):
                errors.append(f"decision state disagreement for {object_id}")
            if not selected and (representation_type or resource_ids):
                errors.append(f"unresolved decision carries a Master representation for {object_id}")
            if decision.get("deletion_authorized") != "false":
                errors.append(f"decision carries deletion authority for {object_id}")
            if selected:
                if not normalized_note(decision.get("decision_note", "")):
                    errors.append(f"selected master lacks a decision note for {object_id}")
                if not decision.get("actor") or not decision.get("decided_at"):
                    errors.append(f"selected master lacks actor or time for {object_id}")
                candidate = self.candidates_by_id[selected]
                if representation_type == "single_file":
                    expected_resource_ids = []
                    if candidate["source_kind"] == "museum-digital_reference":
                        matching = [
                            resource["resource_id"]
                            for resource in self.ordered_resources(object_id)
                            if resource["sha256"] == candidate["known_sha256"]
                            and resource["is_main"] == "true"
                        ]
                        expected_resource_ids = matching
                    if resource_ids != expected_resource_ids:
                        errors.append(f"single-file Master resource binding changed for {object_id}")
                elif representation_type == "ordered_md_resource_set":
                    resources = self.ordered_resources(object_id)
                    expected_resource_ids = [resource["resource_id"] for resource in resources]
                    if len(resources) < 2:
                        errors.append(f"compound Master has fewer than two resources for {object_id}")
                    if candidate["candidate_id"] != self.main_md_candidate(object_id)["candidate_id"]:
                        errors.append(f"compound Master anchor is not the main MD candidate for {object_id}")
                    if resource_ids != expected_resource_ids:
                        errors.append(f"compound Master member order changed for {object_id}")
                else:
                    errors.append(f"selected Master lacks a representation type for {object_id}")
                for claim in self.decision_claims(object_id, decision):
                    other = claims.get(claim)
                    if other and other != object_id:
                        errors.append(f"same candidate identity selected for {other} and {object_id}")
                    claims[claim] = object_id
        return errors

    def save(
        self,
        md_id: str,
        selected: str,
        representation_note: str,
        decision_note: str,
    ) -> dict[str, object]:
        source_guard()
        if md_id not in self.objects_by_id:
            raise WorkbenchError("unknown collection-553 object")
        allowed = {row["candidate_id"] for row in self.candidates_by_object[md_id]}
        compound_selection = selected == COMPOUND_MD_SELECTION
        if selected and not compound_selection and selected not in allowed:
            raise WorkbenchError("selected master is not a candidate for this object")
        if compound_selection and len(self.resources_by_object[md_id]) < 2:
            raise WorkbenchError("a compound Master requires at least two linked MD resources")
        representation_note = normalized_note(representation_note)
        decision_note = normalized_note(decision_note)
        if selected and not decision_note:
            raise WorkbenchError("record a short decision note before selecting a master")
        with self.lock:
            decisions = self.decisions()
            conflicts = self.selected_conflicts(decisions, md_id)
            if not compound_selection and selected in conflicts:
                other = conflicts[selected]
                inventory = self.objects_by_id[other]["inventory"]
                raise WorkbenchError(
                    f"this file or hash is already selected for {inventory}; resolve the object identity first"
                )
            selected_candidate_id = selected
            representation_type = "single_file" if selected else ""
            selected_resource_ids: list[str] = []
            if compound_selection:
                selected_candidate_id = self.main_md_candidate(md_id)["candidate_id"]
                representation_type = "ordered_md_resource_set"
                selected_resource_ids = [
                    resource["resource_id"] for resource in self.ordered_resources(md_id)
                ]
            elif selected:
                candidate = self.candidates_by_id[selected]
                if candidate["source_kind"] == "museum-digital_reference":
                    selected_resource_ids = [
                        resource["resource_id"]
                        for resource in self.ordered_resources(md_id)
                        if resource["is_main"] == "true"
                        and resource["sha256"] == candidate["known_sha256"]
                    ]
            row = decisions[md_id]
            row.update(
                {
                    "decision_state": "selected_master" if selected_candidate_id else "unresolved",
                    "selected_candidate_id": selected_candidate_id,
                    "master_representation_type": representation_type,
                    "selected_resource_ids": json.dumps(selected_resource_ids, separators=(",", ":")),
                    "representation_note": representation_note,
                    "decision_note": decision_note,
                    "actor": self.actor,
                    "decided_at": utc_now(),
                    "deletion_authorized": "false",
                }
            )
            errors = self.validate_decisions(decisions)
            if errors:
                raise WorkbenchError("; ".join(errors))
            ordered = [decisions[obj["md_object_id"]] for obj in self.ordered_objects()]
            dispositions = self.dispositions_for(decisions)
            atomic_csv(DECISIONS_CSV, DECISION_FIELDS, ordered)
            atomic_csv(DISPOSITIONS_CSV, DISPOSITION_FIELDS, dispositions)
        return self.check()

    def check(self) -> dict[str, object]:
        mount = source_guard()
        decisions = self.decisions()
        dispositions = read_csv(DISPOSITIONS_CSV)
        errors = self.validate_decisions(decisions)
        expected = self.dispositions_for(decisions)
        if len(dispositions) != len(self.candidates):
            errors.append("candidate disposition projection does not cover all candidates")
        actual_by_id = {row.get("candidate_id", ""): row for row in dispositions}
        if len(actual_by_id) != len(dispositions):
            errors.append("candidate disposition identifiers are not unique")
        for expected_row in expected:
            actual = actual_by_id.get(expected_row["candidate_id"])
            if not actual:
                errors.append(f"missing disposition {expected_row['candidate_id']}")
                continue
            for key in (
                "candidate_id",
                "md_object_id",
                "candidate_path",
                "disposition",
                "deletion_authorized",
            ):
                if actual.get(key) != expected_row[key]:
                    errors.append(f"disposition disagreement {expected_row['candidate_id']}:{key}")
        if errors:
            raise WorkbenchError("; ".join(errors))
        counts = Counter(row["disposition"] for row in dispositions)
        resolved_count = sum(bool(row["selected_candidate_id"]) for row in decisions.values())
        unresolved_count = len(decisions) - resolved_count
        return {
            "result": "ok",
            "version": VERSION,
            "mount": mount,
            "objects": len(self.objects),
            "museum_digital_resources": len(self.resources),
            "master_candidates": len(self.candidates),
            "resolved_objects": resolved_count,
            "unresolved_objects": unresolved_count,
            "candidate_dispositions": dict(sorted(counts.items())),
            "identity_relationship_warnings": len(self.relationships),
            "integrity_exceptions": len(self.exceptions),
            "deletion_authorized": False,
            "accession_performed": False,
            "derivatives_generated": 0,
        }

    def ordered_objects(self) -> list[dict[str, str]]:
        def key(row: dict[str, str]) -> tuple[int, int, str, int]:
            match = re.search(r"(\d{5,})", row["inventory"])
            return (
                0 if self.review_category(row["md_object_id"]) else 1,
                int(match.group(1)) if match else 99999999,
                row["inventory"].casefold(),
                int(row["md_object_id"]),
            )

        return sorted(self.objects, key=key)

    def review_category(self, md_id: str) -> str:
        scenario = self.scenarios_by_id[md_id]["scenario"]
        if scenario == "pdf_quality_variant_review":
            return "PDF quality comparison"
        if scenario == "local_image_original_review":
            return "Original-scan comparison"
        if scenario.startswith("multipart_") or scenario == "incomplete_documentary_aggregate_review":
            return "Identity / completeness hold"
        if self.exceptions_by_object[md_id]:
            return "Integrity hold"
        return ""

    def adjacent(self, md_id: str, delta: int) -> str:
        objects = self.ordered_objects()
        index = next(index for index, row in enumerate(objects) if row["md_object_id"] == md_id)
        return objects[(index + delta) % len(objects)]["md_object_id"]

    def next_unresolved(self, after: str = "") -> str:
        decisions = self.decisions()
        objects = self.ordered_objects()
        start = next((index + 1 for index, row in enumerate(objects) if row["md_object_id"] == after), 0)
        for row in objects[start:] + objects[:start]:
            if not decisions[row["md_object_id"]]["selected_candidate_id"]:
                return row["md_object_id"]
        return after or objects[0]["md_object_id"]


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--paper:#f4f0e7;--ink:#20282a;--muted:#627174;--line:#c9c2b4;--blue:#185d73;--green:#286446;--amber:#8b6416;--red:#8c3434}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.43 system-ui,sans-serif}}a{{color:var(--blue)}}
header{{position:sticky;top:0;z-index:5;display:flex;gap:18px;align-items:center;justify-content:space-between;padding:11px 18px;background:#fff;border-bottom:1px solid var(--line)}}
.layout{{max-width:1800px;margin:auto;display:grid;grid-template-columns:245px minmax(0,1fr);gap:16px;padding:16px}}aside{{position:sticky;top:70px;height:calc(100vh - 86px);overflow:auto;background:#fff;border:1px solid var(--line);border-radius:8px;padding:9px}}
.nav-group{{margin:8px 7px 4px;font-size:.72rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}.nav-item{{display:block;padding:6px 7px;margin:2px 0;border-radius:4px;text-decoration:none;color:var(--ink)}}.nav-item:hover,.nav-item.current{{background:#e5eef0}}.nav-title{{display:block;font-size:.76rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
main{{min-width:0}}h1{{font-size:1.55rem;margin:.2rem 0}}h2{{font-size:1.15rem;margin:.2rem 0 .7rem}}h3{{font-size:1rem;margin:.3rem 0}}.muted{{color:var(--muted)}}
.panel{{margin:0 0 14px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}}.card{{border:2px solid var(--line);border-radius:7px;padding:10px;min-width:0}}.card.master{{border-color:var(--green);background:#f1faf4}}.card.redundant{{border-color:#98917d;background:#faf9f5}}.card.conflict{{border-color:var(--red);background:#fff5f3}}
.status{{display:inline-block;padding:3px 7px;border-radius:4px;background:#eee;font-size:.8rem}}.resolved{{background:#d8eadf;color:#174c32}}.pending{{background:#f3e8c8;color:#684a08}}.warning{{background:#f5dfd8;color:#742e25}}
.facts{{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px}}.facts dt{{font-weight:700}}.facts dd{{margin:0;overflow-wrap:anywhere}}.path{{font:12px/1.35 ui-monospace,monospace;overflow-wrap:anywhere}}
.choice{{display:block;margin-top:9px;padding:8px;border:1px solid var(--line);border-radius:5px;cursor:pointer}}textarea{{width:100%;min-height:64px;padding:8px}}button{{border:0;border-radius:5px;padding:10px 15px;color:#fff;background:var(--blue);cursor:pointer}}
.preview{{display:block;width:100%;height:340px;object-fit:contain;background:#171717;border:0;border-radius:4px}}.preview-box{{margin-bottom:8px}}.preview-box summary{{cursor:pointer;color:var(--blue);padding:6px 0}}.preview-link{{display:inline-block;padding:7px 10px;border:1px solid var(--line);border-radius:4px;text-decoration:none}}
.savebar{{position:sticky;bottom:0;background:#fff;border:1px solid var(--line);border-radius:7px;padding:12px;box-shadow:0 -4px 12px #0001;display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end}}
.warning-list,.hash-audit{{border-left:4px solid var(--amber);padding-left:12px}}.exception-list{{border-left:4px solid var(--red);padding-left:12px}}.top-nav{{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}}.top-nav a{{padding:6px 9px;background:#fff;border:1px solid var(--line);border-radius:4px;text-decoration:none}}
@media(max-width:900px){{.layout{{display:block}}aside{{position:static;height:auto;max-height:220px;margin-bottom:14px}}.savebar{{position:static;grid-template-columns:1fr}}header{{position:static}}.preview{{height:260px}}}}
</style></head><body>{body}<script>
document.addEventListener('toggle',function(event){{const box=event.target;if(!(box instanceof HTMLDetailsElement)||!box.open||box.dataset.loaded)return;const slot=box.querySelector('.preview-slot');const frame=document.createElement('iframe');frame.className='preview';frame.loading='lazy';frame.src=box.dataset.src;frame.title=box.dataset.title||'PDF preview';slot.appendChild(frame);box.dataset.loaded='1';}},true);
</script></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server: "WorkbenchServer"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self.send_bytes(
                    json.dumps(self.server.workbench.check(), indent=2).encode(),
                    "application/json; charset=utf-8",
                )
            elif parsed.path == "/candidate":
                self.asset("candidate", query.get("id", [""])[0])
            elif parsed.path == "/resource":
                self.asset("resource", query.get("id", [""])[0])
            elif parsed.path == "/":
                md_id = query.get("md", [""])[0] or self.server.workbench.next_unresolved()
                self.index(md_id, query.get("saved", [""])[0] == "1")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.send_bytes(
                json.dumps({"result": "error", "error": str(error)}).encode(),
                "application/json; charset=utf-8",
                503,
            )

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 128 * 1024:
                raise WorkbenchError("form is too large")
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            md_id = fields.get("md_object_id", [""])[0]
            selected = fields.get("master_candidate_id", [""])[0]
            representation_note = fields.get("representation_note", [""])[0]
            decision_note = fields.get("decision_note", [""])[0]
            self.server.workbench.save(md_id, selected, representation_note, decision_note)
            target = self.server.workbench.next_unresolved(md_id)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?" + urlencode({"md": target, "saved": "1"}))
            self.end_headers()
        except Exception as error:
            self.send_bytes(
                page(
                    "Decision not saved",
                    f"<main style='max-width:800px;margin:30px auto'><h1>Decision not saved</h1>"
                    f"<p>{html.escape(str(error))}</p><p><a href='/'>Return to the workbench</a></p></main>",
                ),
                "text/html; charset=utf-8",
                400,
            )

    def asset(self, kind: str, item_id: str) -> None:
        wb = self.server.workbench
        if kind == "candidate":
            row = wb.candidates_by_id.get(item_id)
            path = Path(row["candidate_path"]) if row else None
        else:
            row = wb.resources_by_id.get(item_id)
            path = Path(row["work_path"]) if row else None
        if not row or path is None or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.suffix.casefold() == ".pdf":
            self.send_file(path, "application/pdf")
            return
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((1800, 1500), Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, "JPEG", quality=87, optimize=True)
        self.send_bytes(output.getvalue(), "image/jpeg")

    def send_file(self, path: Path, content_type: str) -> None:
        size = path.stat().st_size
        start = 0
        end = size - 1
        partial = False
        header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip()) if header else None
        if match:
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            elif match.group(2):
                length = int(match.group(2))
                start = max(0, size - length)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def media(self, endpoint: str, item_id: str, is_pdf: bool, alt: str) -> str:
        url = f"/{endpoint}?{urlencode({'id': item_id})}"
        escaped_url = html.escape(url, quote=True)
        escaped_alt = html.escape(alt, quote=True)
        if is_pdf:
            return (
                f'<details class="preview-box" data-src="{escaped_url}" data-title="{escaped_alt}">'
                '<summary>Load PDF preview</summary><div class="preview-slot"></div></details>'
                f'<a class="preview-link" href="{escaped_url}" target="_blank">Open PDF in a new tab</a>'
            )
        return f'<img class="preview" loading="lazy" src="{escaped_url}" alt="{escaped_alt}">'

    def index(self, md_id: str, saved: bool) -> None:
        wb = self.server.workbench
        if md_id not in wb.objects_by_id:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        decisions = wb.decisions()
        decision = decisions[md_id]
        representation_type = decision.get("master_representation_type", "")
        obj = wb.objects_by_id[md_id]
        scenario = wb.scenarios_by_id[md_id]
        conflicts = wb.selected_conflicts(decisions, md_id)
        resolved_count = sum(bool(row["selected_candidate_id"]) for row in decisions.values())

        nav = ['<p class="nav-group">Required review</p>']
        standard_heading_added = False
        for row in wb.ordered_objects():
            object_id = row["md_object_id"]
            category = wb.review_category(object_id)
            if not category and not standard_heading_added:
                nav.append('<p class="nav-group">Standard one-file decisions</p>')
                standard_heading_added = True
            marker = "✓" if decisions[object_id]["selected_candidate_id"] else "·"
            current = " current" if object_id == md_id else ""
            nav.append(
                f'<a class="nav-item{current}" href="/?{urlencode({"md": object_id})}">'
                f'{marker} {html.escape(row["inventory"])}<span class="nav-title">'
                f'{html.escape(category or row["title"])}</span></a>'
            )

        resource_cards = []
        for resource in wb.ordered_resources(md_id):
            is_pdf = resource["format"].casefold() == "pdf"
            technical = (
                f"{resource['page_count']} page(s)"
                if resource["page_count"]
                else f"{resource['width_px']} × {resource['height_px']} px"
            )
            resource_cards.append(
                f"""<article class="card">
{self.media('resource', resource['resource_id'], is_pdf, resource['description'] or obj['title'])}
<h3>{'Main MD projection' if resource['is_main'] == 'true' else 'Related MD component/view'} · {html.escape(resource['resource_id'])}</h3>
<span class="status">{html.escape(resource['representation_role'].replace('_', ' '))}</span>
<dl class="facts"><dt>Technical</dt><dd>{html.escape(technical)}, {human_size(resource['size_bytes'])}</dd><dt>SHA-256</dt><dd class="path">{html.escape(short_hash(resource['sha256']))}</dd><dt>Rights</dt><dd>{html.escape(resource['rights'] or '[not recorded]')}</dd><dt>Creator</dt><dd>{html.escape(resource['creator'] or '[not recorded]')}</dd></dl>
<p>{html.escape(resource['description'] or 'No separate resource description')}</p></article>"""
            )

        current_dispositions = {
            row["candidate_id"]: row["disposition"]
            for row in wb.dispositions_for(decisions)
        }
        hash_counts = Counter(
            row["known_sha256"] for row in wb.candidates_by_object[md_id] if row["known_sha256"]
        )
        resource_hashes = {row["sha256"] for row in wb.resources_by_object[md_id] if row["sha256"]}
        candidate_cards = []
        for candidate in wb.candidates_by_object[md_id]:
            candidate_id = candidate["candidate_id"]
            disposition = current_dispositions[candidate_id]
            conflict_object = conflicts.get(candidate_id, "")
            classes = "card"
            if disposition == "selected_master":
                classes += " master"
            elif disposition == "redundant":
                classes += " redundant"
            if conflict_object:
                classes += " conflict"
            checked = (
                " checked"
                if representation_type == "single_file"
                and decision["selected_candidate_id"] == candidate_id
                else ""
            )
            disabled = " disabled" if conflict_object else ""
            is_pdf = candidate["format"].casefold() == "pdf"
            technical = (
                f"{candidate['page_count']} page(s)"
                if candidate["page_count"]
                else f"{candidate['width_px']} × {candidate['height_px']} px"
            )
            duplicate_note = ""
            digest = candidate["known_sha256"]
            if digest and hash_counts[digest] > 1:
                duplicate_note = f'<p class="muted">Same content occurs in {hash_counts[digest]} candidate paths for this object.</p>'
            exact_md_note = ""
            if digest and digest in resource_hashes and candidate["source_kind"] != "museum-digital_reference":
                exact_md_note = '<p class="status resolved">Byte-identical to a retained MD resource — no quality difference.</p>'
            conflict_note = ""
            if conflict_object:
                other = wb.objects_by_id[conflict_object]
                conflict_note = (
                    f'<p class="status warning">Already selected for '
                    f'<a href="/?{urlencode({"md": conflict_object})}">{html.escape(other["inventory"])}</a>; resolve identity first.</p>'
                )
            candidate_cards.append(
                f"""<article class="{classes}">
{self.media('candidate', candidate_id, is_pdf, Path(candidate['candidate_path']).name)}
<h3>{html.escape(Path(candidate['candidate_path']).name)}</h3>
<span class="status">{html.escape(disposition.replace('_', ' '))}</span>{conflict_note}
<dl class="facts"><dt>Role</dt><dd>{html.escape(candidate['candidate_role'].replace('_', ' '))}</dd><dt>Evidence</dt><dd>{html.escape(candidate['match_basis'].replace('_', ' '))}</dd><dt>Technical</dt><dd>{html.escape(technical)}, {human_size(candidate['size_bytes'])}</dd><dt>SHA-256</dt><dd class="path">{html.escape(short_hash(digest))}</dd><dt>Source</dt><dd>{html.escape(candidate['source_kind'].replace('_', ' '))}</dd></dl>
{exact_md_note}{duplicate_note}<p class="path">{html.escape(candidate['candidate_path'])}</p>
<label class="choice"><input type="radio" name="master_candidate_id" value="{html.escape(candidate_id, quote=True)}"{checked}{disabled}> Select this file as the single master</label>
</article>"""
            )

        compound_choice_html = ""
        if len(wb.resources_by_object[md_id]) > 1:
            compound_checked = " checked" if representation_type == "ordered_md_resource_set" else ""
            compound_class = "card master" if compound_checked else "card"
            members = "".join(
                f'<li>{html.escape(resource["resource_position"])}. resource '
                f'{html.escape(resource["resource_id"])} · {html.escape(resource["format"].upper())} · '
                f'{html.escape(resource["page_count"] or (resource["width_px"] + " × " + resource["height_px"]))}</li>'
                for resource in wb.ordered_resources(md_id)
            )
            compound_choice_html = (
                f'<article class="{compound_class}"><h3>All linked MD resources as one compound Master</h3>'
                '<p>The ordered members retain parts, appendices, paired images, or variants without '
                'creating multiple Masters.</p><ol>' + members + '</ol>'
                f'<label class="choice"><input type="radio" name="master_candidate_id" '
                f'value="{COMPOUND_MD_SELECTION}"{compound_checked}> '
                'Select this ordered MD resource set as the single Master representation</label></article>'
            )

        warning_rows = []
        for relationship in wb.relationships_by_object[md_id]:
            other_id = (
                relationship["right_md_object_id"]
                if relationship["left_md_object_id"] == md_id
                else relationship["left_md_object_id"]
            )
            other = wb.objects_by_id[other_id]
            warning_rows.append(
                f'<li><strong>{html.escape(relationship["relationship"].replace("_", " "))}</strong>: '
                f'{html.escape(relationship["evidence"])} — '
                f'<a href="/?{urlencode({"md": other_id})}">{html.escape(other["inventory"])}</a></li>'
            )
        warnings = (
            '<section class="panel warning-list"><h2>Object-identity warnings</h2><ul>'
            + "".join(warning_rows)
            + "</ul><p>Do not select the same file or hash for two objects. Resolve the identity relationship first.</p></section>"
            if warning_rows
            else ""
        )

        exception_rows = []
        for exception in wb.exceptions_by_object[md_id]:
            exception_rows.append(
                f'<li><strong>{html.escape(exception["integrity_state"].replace("_", " "))}</strong>: '
                f'{html.escape(exception["technical_error"])}<br><span class="path">{html.escape(exception["source_path"])}</span></li>'
            )
        exceptions = (
            '<section class="panel exception-list"><h2>Integrity exceptions — not selectable and not drain candidates</h2><ul>'
            + "".join(exception_rows)
            + "</ul></section>"
            if exception_rows
            else ""
        )

        scan_rows = wb.page_scan_by_object[md_id]
        page_scan_html = ""
        if scan_rows:
            scan_items = "".join(
                f'<li>PDF page {html.escape(row["pdf_page"])} / {html.escape(row["pdf_page_count"])} '
                f'→ {html.escape(row["width_px"])} × {html.escape(row["height_px"])} px '
                f'({html.escape(row["evidence_role"].replace("_", " "))}, visual-hash distance '
                f'{html.escape(row["distance_total"])})<br><span class="path">'
                f'{html.escape(row["source_path"])}</span></li>'
                for row in scan_rows
            )
            page_scan_html = (
                '<section class="panel hash-audit"><h2>Large visual-hash index evidence</h2><ul>'
                + scan_items
                + '</ul><p>Related component evidence for a multi-page PDF is not selectable. '
                'Complete one-page matches appear below as candidates for that one-page object only.</p></section>'
            )

        title_leads = wb.title_leads_by_object[md_id]
        title_lead_html = ""
        if title_leads:
            items = "".join(
                f'<li>{html.escape(row["filename"])}<br><span class="path">{html.escape(row["source_path"])}</span></li>'
                for row in title_leads
            )
            title_lead_html = (
                f'<details class="panel"><summary>{len(title_leads)} title-only OCR/path lead(s) — evidence only</summary>'
                '<p>These are deliberately not master candidates because title similarity does not bind them to this object.</p>'
                f'<ul>{items}</ul></details>'
            )

        status_class = "resolved" if decision["selected_candidate_id"] else "pending"
        status_text = "Master selected" if decision["selected_candidate_id"] else "Unresolved"
        representation_badge = (
            f' <span class="status resolved">{html.escape(representation_type.replace("_", " "))}</span>'
            if representation_type
            else ""
        )
        saved_note = (
            '<p class="status resolved">Decision saved. No accession or deletion was performed.</p>'
            if saved
            else ""
        )
        previous_id = wb.adjacent(md_id, -1)
        next_id = wb.adjacent(md_id, 1)
        next_unresolved = wb.next_unresolved(md_id)
        review_category = wb.review_category(md_id)
        review_badge = (
            f' <span class="status warning">{html.escape(review_category)}</span>'
            if review_category
            else ""
        )
        audit = wb.summary["page_scan_hash_audit"]
        body = f"""<header><div><strong>Archiv WF · collection 553 reconciliation</strong><br><span class="muted">{resolved_count}/54 masters selected · actor {html.escape(wb.actor)}</span></div><a href="/health">health/check</a></header>
<div class="layout"><aside>{''.join(nav)}</aside><main>{saved_note}
<div class="top-nav"><a href="/?{urlencode({'md': previous_id})}">← Previous</a><a href="/?{urlencode({'md': next_id})}">Next →</a><a href="/?{urlencode({'md': next_unresolved})}">Next unresolved</a></div>
<section class="panel hash-audit"><strong>Full content comparison:</strong> {audit['pdf_resources']} PDFs / {audit['pdf_pages']} pages against {audit['usb_images_compared']:,} indexed USB images. All PDFs are raster scans: {audit['scan_with_text_layer']} have extractable OCR/text, {audit['image_only_scan']} are image-only, and none are text-native. No complete standalone image sequence was found for a multi-page PDF; isolated component matches remain evidence only.</section>
<section class="panel"><span class="status {status_class}">{status_text}</span>{representation_badge} <span class="status">{html.escape(scenario['scenario'].replace('_', ' '))}</span>{review_badge}<h1>{html.escape(obj['title'])}</h1>
<dl class="facts"><dt>Inventory</dt><dd>{html.escape(obj['inventory'])}</dd><dt>MD object</dt><dd><a href="{html.escape(obj['object_url'], quote=True)}" target="_blank" rel="noreferrer">{html.escape(md_id)}</a></dd><dt>Type / date</dt><dd>{html.escape(obj['object_type'])} · {html.escape(obj['date'])}</dd><dt>Material</dt><dd>{html.escape(obj['material_technique'] or '[not recorded]')}</dd><dt>Dimensions</dt><dd>{html.escape(obj['dimensions'] or '[not recorded]')}</dd></dl>
<p>{html.escape(obj['description'])}</p><h2>Preservation scenario</h2><p><strong>{html.escape(scenario['basis'])}</strong></p><p>{html.escape(scenario['required_human_decision'])}</p></section>
{warnings}{exceptions}{page_scan_html}
<section class="panel"><h2>museum-digital resources</h2><p class="muted">{'All listed resources are ordered members of the one selected compound Master representation.' if representation_type == 'ordered_md_resource_set' else 'Additional parts or views remain separate evidence unless the explicitly approved compound-set choice is selected.'}</p><div class="grid">{''.join(resource_cards)}</div></section>
{title_lead_html}
<form method="post" action="/save"><input type="hidden" name="md_object_id" value="{html.escape(md_id, quote=True)}">
<section class="panel"><h2>Master candidates</h2><p>Select exactly one complete file or one approved ordered MD resource set, or leave the object unresolved. Selecting one Master representation records every other candidate as redundant but never authorizes deletion.</p>
<label class="choice"><input type="radio" name="master_candidate_id" value=""{' checked' if not decision['selected_candidate_id'] else ''}> Leave this object unresolved</label>
<div class="grid">{compound_choice_html}{''.join(candidate_cards)}</div></section>
<div class="savebar"><label>Representation note<br><textarea name="representation_note" maxlength="500" placeholder="Components, corresponding page scans, or surrogate status">{html.escape(decision['representation_note'])}</textarea></label><label>Decision note — required when selecting a master<br><textarea name="decision_note" maxlength="500">{html.escape(decision['decision_note'])}</textarea></label><div><button type="submit">Save and continue</button><p class="muted">No source changes<br>No accession<br>No deletion</p></div></div></form></main></div>"""
        self.send_bytes(page(f"{obj['inventory']} — collection 553", body), "text/html; charset=utf-8")


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workbench: Workbench):
        super().__init__(address, Handler)
        self.workbench = workbench


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default="Vladimir", help="name recorded with decisions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("collection-553 workbench is localhost-only")
    workbench = Workbench(args.actor)
    if args.check:
        print(json.dumps(workbench.check(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    server = WorkbenchServer((args.host, args.port), workbench)
    print(f"Collection 553 workbench: http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
