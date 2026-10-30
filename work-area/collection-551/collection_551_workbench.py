#!/usr/bin/env python3
"""Direct localhost Master-review workbench for collection 551.

The workbench may update only the prepared human decision, candidate disposition,
and drain projections below iss-archive-work. It never changes source files,
downloads new evidence, creates derivatives, accessions an archive unit, or
authorizes deletion.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import subprocess
import threading
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


VERSION = "2.1.0-semantic-consolidation"
ROOT = Path("/mnt/volume/iss-archive-work/collection-551/work/reconciliation/unit-reconciliation-v1")
SOURCE_ROOT = Path("/media/vladimir/Elements")
ACCESSION_TARGET = Path('/mnt/volume/iss-archive/units/FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF')
OBJECTS_CSV = ROOT / "objects.csv"
RESOURCES_CSV = ROOT / "md-resources.csv"
CANDIDATES_CSV = ROOT / "master-candidates.csv"
PROPOSALS_CSV = ROOT / "master-proposals.csv"
DECISIONS_CSV = ROOT / "master-decisions.csv"
DISPOSITIONS_CSV = ROOT / "candidate-dispositions.csv"
DRAIN_CSV = ROOT / "drain-list.csv"
DRAIN_EVIDENCE_CSV = ROOT / "drain-source-evidence.csv"
METADATA_CSV = ROOT / "metadata-comparison.csv"
METADATA_EVIDENCE_CSV = ROOT / "metadata-evidence.csv"
METADATA_DIFFERENCES_CSV = ROOT / "metadata-differences.csv"
BINARY_DIFFERENCES_CSV = ROOT / "binary-differences.csv"
PREVIEW_MANIFEST_CSV = ROOT / "preview-manifest.csv"
VISUAL_CSV = ROOT / "visual-leads.csv"
RELATIONSHIPS_CSV = ROOT / "object-relationships.csv"
SUMMARY_JSON = ROOT / "preparation-summary.json"
COMPOUND_MD_SELECTION = "__ordered_md_resource_set__"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

DECISION_FIELDS = [
    "md_object_id", "inventory", "decision_state", "selected_candidate_id",
    "master_representation_type", "selected_resource_ids", "metadata_authority",
    "representation_note", "decision_note", "actor", "decided_at", "deletion_authorized",
]
DISPOSITION_FIELDS = [
    "candidate_id", "md_object_id", "candidate_path", "disposition", "reason", "deletion_authorized",
]
DRAIN_FIELDS = [
    "md_object_id", "inventory", "source_path", "source_class", "source_kind", "classification",
    "relationship", "evidence", "sha256", "size_bytes", "selected_master_candidate_id",
    "covered_by_selected_master", "coverage_state", "drain_state", "deletion_authorized", "note",
]
METADATA_DIFFERENCE_FIELDS = [
    "difference_id", "md_object_id", "inventory", "field", "md_value",
    "local_value", "evidence_sources", "decision_action", "effective_value",
    "decision_note", "actor", "decided_at", "deletion_authorized",
]
METADATA_ACTIONS = {"pending", "accept", "ignore", "substitute", "add"}
GERMAN_MONTHS = {
    "jan": 1, "januar": 1, "feb": 2, "februar": 2,
    "mar": 3, "mär": 3, "märz": 3, "marz": 3, "maerz": 3,
    "apr": 4, "april": 4, "mai": 5, "jun": 6, "juni": 6,
    "jul": 7, "juli": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11, "dez": 12, "dezember": 12,
}


class WorkbenchError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(value: str, limit: int = 500) -> str:
    return " ".join(value.split())[:limit]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def document_is_unrelated(row):
    return row.get('decision') == 'excluded' and row.get('review_note', '').startswith('Unrelated to Collection 551:')


def unrelated_document_paths(rows=None):
    if rows is None:
        path = ROOT / 'accession-document-review.csv'
        rows = read_csv(path) if path.exists() else []
    return {row['source_path'] for row in rows if document_is_unrelated(row)}


def noise_document_rows():
    path = ROOT / 'accession-document-review.csv'
    return [r for r in (read_csv(path) if path.exists() else [])
            if r.get('decision') == 'redundant' and r.get('review_note', '').startswith('Noise:')]


def csv_payload(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def atomic_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise WorkbenchError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options") or "").split(","))
    if row.get("target") != str(SOURCE_ROOT) or row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
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


def parsed_ids(value: str) -> list[str] | None:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or any(not isinstance(item, str) or not item for item in decoded):
        return None
    return decoded


def candidate_claims(candidate: dict[str, str]) -> set[str]:
    claims = {"path:" + str(Path(candidate["candidate_path"]).resolve())}
    digest = candidate.get("known_sha256", "").casefold()
    if digest:
        claims.add("sha256:" + digest)
    return claims


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ß", "ss")
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def normalized_date_parts(value: str) -> tuple[str, ...]:
    parts = []
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if re.fullmatch(r"\d{5}", part):
            converted = (datetime(1899, 12, 30) + timedelta(days=int(part))).date()
            parts.append(f"{converted.year:04d}-{converted.month:02d}")
            continue
        month_year = re.search(r"([A-Za-zÄÖÜäöü]+)\.?\s+(\d{2,4})", part)
        if month_year:
            month = GERMAN_MONTHS.get(normalized_text(month_year.group(1)))
            if month:
                year = int(month_year.group(2))
                if year < 100:
                    year += 1900
                parts.append(f"{year:04d}-{month:02d}")
                continue
        full_date = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", part)
        if full_date:
            parts.append(f"{int(full_date.group(3)):04d}-{int(full_date.group(2)):02d}")
            continue
        parts.append("raw:" + normalized_text(part))
    return tuple(sorted(parts))


def technical_codes(value: str) -> frozenset[str]:
    return frozenset(
        re.findall(r"\b[A-ZÄÖÜ]{1,6}\s*[-]?\s*\d[0-9A-ZÄÖÜ/-]*\b", value.upper())
    )


def tag_set(value: str) -> set[str]:
    return {normalized_text(part) for part in re.split(r"[;,]", value) if normalized_text(part)}


def semantically_same(field: str, left: str, right: str) -> bool:
    if field == "date":
        return normalized_date_parts(left) == normalized_date_parts(right)
    left_normalized, right_normalized = normalized_text(left), normalized_text(right)
    if left_normalized == right_normalized:
        return True
    if field == "tags":
        left_tags, right_tags = tag_set(left), tag_set(right)
        return left_tags <= right_tags or right_tags <= left_tags
    if field == "storage_location":
        return left_normalized in right_normalized or right_normalized in left_normalized
    if field == "description":
        left_codes, right_codes = technical_codes(left), technical_codes(right)
        if left_codes and right_codes and left_codes.isdisjoint(right_codes):
            return False
        contained = min(len(left_normalized), len(right_normalized)) >= 20 and (
            left_normalized in right_normalized or right_normalized in left_normalized
        )
        return contained or difflib.SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.78
    if field == "title":
        return (
            left_normalized in right_normalized or right_normalized in left_normalized
            or difflib.SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.88
        )
    return False


def equivalent_to_md(field: str, md_value: str, local_value: str) -> bool:
    if field == "date":
        return normalized_date_parts(md_value) == normalized_date_parts(local_value)
    if field == "tags":
        return tag_set(md_value) == tag_set(local_value)
    return normalized_text(md_value) == normalized_text(local_value)


def preferred_metadata_row(rows: list[dict[str, str]]) -> dict[str, str]:
    field = rows[0]["field"]

    def score(row: dict[str, str]) -> tuple[int, int, int]:
        value = row["local_value"]
        human_date = int(field != "date" or not bool(re.fullmatch(r"[\d; ]+", value)))
        sources = len(json.loads(row["evidence_sources"] or "[]"))
        return human_date, len(value), sources

    return max(rows, key=score)


class Workbench:
    def __init__(self, actor: str, *, verify_hashes: bool = False) -> None:
        self.actor = compact(actor, 120)
        if not self.actor:
            raise WorkbenchError("a decision actor is required")
        self.lock = threading.Lock()
        self.summary = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
        self.objects = read_csv(OBJECTS_CSV)
        self.resources = read_csv(RESOURCES_CSV)
        self.candidates = read_csv(CANDIDATES_CSV)
        self.proposals = read_csv(PROPOSALS_CSV)
        self.metadata = read_csv(METADATA_CSV)
        self.metadata_evidence = read_csv(METADATA_EVIDENCE_CSV)
        self.metadata_differences = read_csv(METADATA_DIFFERENCES_CSV)
        self.binary_differences = read_csv(BINARY_DIFFERENCES_CSV)
        self.previews = read_csv(PREVIEW_MANIFEST_CSV)
        self.visual = read_csv(VISUAL_CSV)
        self.relationships = read_csv(RELATIONSHIPS_CSV)
        self.drain_base = read_csv(DRAIN_EVIDENCE_CSV if DRAIN_EVIDENCE_CSV.exists() else DRAIN_CSV)
        self.objects_by_id = {row["md_object_id"]: row for row in self.objects}
        self.resources_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.candidates_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.evidence_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.visual_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.relationships_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.metadata_differences_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.binary_differences_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.resources:
            self.resources_by_object[row["md_object_id"]].append(row)
        for row in self.candidates:
            self.candidates_by_object[row["md_object_id"]].append(row)
        for row in self.metadata_evidence:
            if row.get("md_object_id"):
                self.evidence_by_object[row["md_object_id"]].append(row)
        for row in self.visual:
            self.visual_by_object[row["md_object_id"]].append(row)
        for row in self.relationships:
            self.relationships_by_object[row["left_md_object_id"]].append(row)
            self.relationships_by_object[row["right_md_object_id"]].append(row)
        for row in self.metadata_differences:
            self.metadata_differences_by_object[row["md_object_id"]].append(row)
        for row in self.binary_differences:
            self.binary_differences_by_object[row["md_object_id"]].append(row)
        self.candidates_by_id = {row["candidate_id"]: row for row in self.candidates}
        self.resources_by_id = {row["resource_id"]: row for row in self.resources}
        self.proposals_by_id = {row["md_object_id"]: row for row in self.proposals}
        self.metadata_by_id = {row["md_object_id"]: row for row in self.metadata}
        self.previews_by_candidate = {row["candidate_id"]: row for row in self.previews}
        self.metadata_differences_by_id = {row["difference_id"]: row for row in self.metadata_differences}
        self.review_ids = {
            row["md_object_id"] for row in self.proposals
            if row["proposal_state"] == "local_binary_enrichment_review_required"
        }
        self.rebuild_metadata_groups()
        self.validate_static(verify_hashes=verify_hashes)

    def rebuild_metadata_groups(self) -> None:
        self.metadata_groups_by_object: dict[str, list[dict[str, object]]] = defaultdict(list)
        self.metadata_groups_by_id: dict[str, dict[str, object]] = {}
        total_groups = 0
        hidden_equivalent = 0
        mixed_groups = 0
        for object_id in sorted(self.review_ids, key=int):
            groups: list[list[dict[str, str]]] = []
            for row in self.metadata_differences_by_object.get(object_id, []):
                for group in groups:
                    if group[0]["field"] == row["field"] and any(
                        semantically_same(row["field"], row["local_value"], member["local_value"])
                        for member in group
                    ):
                        group.append(row)
                        break
                else:
                    groups.append([row])
            total_groups += len(groups)
            for members in groups:
                preferred = preferred_metadata_row(members)
                if equivalent_to_md(preferred["field"], preferred["md_value"], preferred["local_value"]):
                    hidden_equivalent += 1
                    continue
                member_ids = sorted(row["difference_id"] for row in members)
                group_id = "group-" + hashlib.sha256("\0".join(member_ids).encode()).hexdigest()[:16]
                actions = {row["decision_action"] for row in members}
                action = next(iter(actions)) if len(actions) == 1 else "mixed"
                if action == "mixed":
                    mixed_groups += 1
                variants = list(dict.fromkeys(row["local_value"] for row in members))
                sources_by_key = {}
                for row in members:
                    for source in json.loads(row["evidence_sources"] or "[]"):
                        key = (source.get("path", ""), source.get("sheet", ""), source.get("row", ""))
                        sources_by_key[key] = source
                group_row: dict[str, object] = {
                    "group_id": group_id, "md_object_id": object_id,
                    "field": preferred["field"], "md_value": preferred["md_value"],
                    "local_value": preferred["local_value"], "local_variants": variants,
                    "evidence_sources": list(sources_by_key.values()), "action": action,
                    "members": members, "preferred_difference_id": preferred["difference_id"],
                }
                self.metadata_groups_by_object[object_id].append(group_row)
                self.metadata_groups_by_id[group_id] = group_row
        self.metadata_group_stats = {
            "raw_rows": len(self.metadata_differences), "semantic_groups": total_groups,
            "visible_groups": len(self.metadata_groups_by_id),
            "hidden_equivalent_groups": hidden_equivalent,
            "consolidated_rows": len(self.metadata_differences) - total_groups,
            "mixed_action_groups": mixed_groups,
        }

    def validate_static(self, *, verify_hashes: bool) -> None:
        source_guard()
        if self.summary.get("format") != "iss-collection-551-reconciliation-preparation-v1":
            raise WorkbenchError("unsupported preparation summary")
        expected = {"objects": 79, "resources": 112, "master_candidates": 271, "review_queue_objects": 13}
        for key, value in expected.items():
            if self.summary.get(key) != value:
                raise WorkbenchError(f"preparation summary {key} changed from {value}")
        if len(self.objects) != 79 or len(self.objects_by_id) != 79:
            raise WorkbenchError("workbench requires 79 unique objects")
        if len(self.resources) != 112 or len(self.resources_by_id) != 112:
            raise WorkbenchError("workbench requires 112 unique resources")
        if len(self.candidates) != 271 or len(self.candidates_by_id) != 271:
            raise WorkbenchError("workbench requires 271 unique candidates")
        if len(self.proposals_by_id) != 79 or len(self.metadata_by_id) != 79:
            raise WorkbenchError("proposal and metadata projections must cover all objects")
        if len(self.review_ids) != 13:
            raise WorkbenchError("difference-first queue must contain exactly 13 vague objects")
        if len(self.drain_base) != self.summary.get("drain_rows"):
            raise WorkbenchError("drain projection does not match preparation summary")
        if len(self.binary_differences) != self.summary.get("binary_difference_rows"):
            raise WorkbenchError("binary difference projection does not match preparation summary")
        if len(self.metadata_differences) != self.summary.get("metadata_difference_rows"):
            raise WorkbenchError("metadata difference projection does not match preparation summary")
        if len(self.previews) != self.summary.get("preview_rows"):
            raise WorkbenchError("preview projection does not match preparation summary")
        evidence_sets = [self.objects, self.resources, self.candidates, self.proposals, self.metadata, self.metadata_evidence, self.metadata_differences, self.binary_differences, self.previews, self.visual, self.relationships, self.drain_base]
        if any(row.get("deletion_authorized") != "false" for rows in evidence_sets for row in rows):
            raise WorkbenchError("prepared evidence carries deletion authority")
        if set(self.resources_by_object) != set(self.objects_by_id) or set(self.candidates_by_object) != set(self.objects_by_id):
            raise WorkbenchError("resource or candidate coverage does not match object scope")
        for object_id, resources in self.resources_by_object.items():
            resources.sort(key=lambda row: (int(row["resource_order"]), int(row["resource_id"])))
            if sum(row["is_main"] == "true" for row in resources) != 1:
                raise WorkbenchError(f"MD {object_id} does not have exactly one main resource")
        for resource in self.resources:
            path = Path(resource["work_path"])
            if not path.is_file() or not inside(path, ROOT) or path.stat().st_size != int(resource["size_bytes"]):
                raise WorkbenchError(f"missing or changed MD capture: {path}")
            if not HEX64.fullmatch(resource["sha256"]):
                raise WorkbenchError(f"invalid resource SHA-256: {resource['resource_id']}")
            if verify_hashes:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != resource["sha256"]:
                    raise WorkbenchError(f"MD capture hash mismatch: {path}")
        for candidate in self.candidates:
            path = Path(candidate["candidate_path"])
            if not path.is_file() or not (inside(path, ROOT) or inside(path, SOURCE_ROOT)):
                raise WorkbenchError(f"unsafe or missing candidate: {path}")
            digest = candidate.get("known_sha256", "")
            if digest and not HEX64.fullmatch(digest):
                raise WorkbenchError(f"invalid candidate SHA-256: {candidate['candidate_id']}")
        if set(self.binary_differences_by_object) != self.review_ids:
            raise WorkbenchError("every vague object must have a binary difference")
        if any(row["decision_action"] not in METADATA_ACTIONS for row in self.metadata_differences):
            raise WorkbenchError("metadata difference carries an invalid action")
        for preview in self.previews:
            path = Path(preview["preview_path"])
            if not path.is_file() or not inside(path, ROOT) or path.stat().st_size != int(preview["preview_size_bytes"]):
                raise WorkbenchError(f"missing or changed preview: {path}")
            if not HEX64.fullmatch(preview["preview_sha256"]):
                raise WorkbenchError(f"invalid preview SHA-256: {path}")
            if preview["candidate_id"] not in self.candidates_by_id:
                raise WorkbenchError(f"preview has no candidate: {path}")
        for row in self.drain_base:
            path = Path(row["source_path"])
            allowed = inside(path, ROOT) if row["source_class"] == "work_capture" else inside(path, SOURCE_ROOT)
            if not path.is_file() or not allowed or path.stat().st_size != int(row["size_bytes"]):
                raise WorkbenchError(f"unsafe or changed drain evidence: {path}")
            if row["drain_state"] not in {"blocked_until_human_decisions_and_validated_accession", "blocked_until_validated_accession", "retain_selected_master_source"}:
                raise WorkbenchError(f"unsafe drain state: {row['drain_state']}")
        errors = self.validate_decisions(self.decisions())
        if errors:
            raise WorkbenchError("; ".join(errors))

    def decisions(self) -> dict[str, dict[str, str]]:
        rows = read_csv(DECISIONS_CSV)
        result = {row["md_object_id"]: row for row in rows}
        if len(rows) != 79 or set(result) != set(self.objects_by_id):
            raise WorkbenchError("decision file does not cover exactly 79 objects")
        return result

    def selected_candidates(self, object_id: str, decision: dict[str, str]) -> list[dict[str, str]]:
        if decision["decision_state"] != "selected_master":
            return []
        if decision["selected_candidate_id"] == COMPOUND_MD_SELECTION:
            resource_ids = parsed_ids(decision["selected_resource_ids"]) or []
            by_resource = {
                row["resource_id"]: row for row in self.candidates_by_object[object_id]
                if row["source_kind"] == "museum_digital_capture"
            }
            return [by_resource[resource_id] for resource_id in resource_ids]
        return [self.candidates_by_id[decision["selected_candidate_id"]]]

    def validate_decisions(self, decisions: dict[str, dict[str, str]]) -> list[str]:
        errors: list[str] = []
        claims: dict[str, str] = {}
        for object_id, decision in decisions.items():
            if decision.get("deletion_authorized") != "false":
                errors.append(f"MD {object_id}: deletion authority is forbidden")
            state = decision.get("decision_state")
            selected = decision.get("selected_candidate_id", "")
            representation = decision.get("master_representation_type", "")
            resource_ids = parsed_ids(decision.get("selected_resource_ids", ""))
            if resource_ids is None:
                errors.append(f"MD {object_id}: selected_resource_ids is invalid JSON")
                continue
            if state == "unresolved":
                if selected or representation or resource_ids:
                    errors.append(f"MD {object_id}: unresolved decision carries a selection")
                continue
            if state != "selected_master":
                errors.append(f"MD {object_id}: invalid decision state {state}")
                continue
            if selected == COMPOUND_MD_SELECTION:
                ordered = [row["resource_id"] for row in self.resources_by_object[object_id]]
                if representation != "ordered_md_resource_set" or len(ordered) < 2 or resource_ids != ordered:
                    errors.append(f"MD {object_id}: compound selection does not bind every ordered resource")
                    continue
            else:
                candidate = self.candidates_by_id.get(selected)
                if not candidate or candidate["md_object_id"] != object_id:
                    errors.append(f"MD {object_id}: selected candidate is foreign or missing")
                    continue
                if candidate["master_eligible"] != "true" or representation != "single_file":
                    errors.append(f"MD {object_id}: selected candidate is not an eligible single-file Master")
                    continue
                expected_resource_ids = [candidate["resource_id"]] if candidate["source_kind"] == "museum_digital_capture" else []
                if resource_ids != expected_resource_ids:
                    errors.append(f"MD {object_id}: selected resource binding is incorrect")
                    continue
            for candidate in self.selected_candidates(object_id, decision):
                for claim in candidate_claims(candidate):
                    prior = claims.get(claim)
                    if prior and prior != object_id:
                        errors.append(f"MD {object_id}: selected Master identity is already used by MD {prior}")
                    claims[claim] = object_id
        return errors

    def dispositions_for(self, decisions: dict[str, dict[str, str]]) -> list[dict[str, str]]:
        rows = []
        for candidate in self.candidates:
            decision = decisions[candidate["md_object_id"]]
            selected_ids = {row["candidate_id"] for row in self.selected_candidates(candidate["md_object_id"], decision)}
            if decision["decision_state"] == "unresolved":
                disposition, reason = "pending", "awaiting explicit human Master decision"
            elif candidate["candidate_id"] in selected_ids:
                disposition = "selected_master_member" if len(selected_ids) > 1 else "selected_master"
                reason = "explicitly selected by human decision"
            else:
                disposition, reason = "redundant", "not selected after explicit human Master decision"
            rows.append({
                "candidate_id": candidate["candidate_id"], "md_object_id": candidate["md_object_id"],
                "candidate_path": candidate["candidate_path"], "disposition": disposition,
                "reason": reason, "deletion_authorized": "false",
            })
        return rows

    def drain_for(self, decisions: dict[str, dict[str, str]], *, excluded_paths=None) -> list[dict[str, str]]:
        if excluded_paths is None:
            excluded_paths = unrelated_document_paths()
        selected_by_path: dict[str, tuple[str, str]] = {}
        for object_id, decision in decisions.items():
            for candidate in self.selected_candidates(object_id, decision):
                selected_by_path[str(Path(candidate["candidate_path"]).resolve())] = (candidate["candidate_id"], candidate["source_kind"])
        rows = []
        for base in self.drain_base:
            if base['source_path'] in excluded_paths:
                continue
            row = dict(base)
            selected = selected_by_path.get(str(Path(row["source_path"]).resolve()))
            if selected:
                row["selected_master_candidate_id"] = selected[0]
                row["covered_by_selected_master"] = "true"
                if row["source_class"] == "protected_usb_source":
                    row["relationship"] = "selected_master_source"
                    row["coverage_state"] = "selected_pending_accession"
                    row["drain_state"] = "retain_selected_master_source"
                else:
                    row["relationship"] = "selected_master_input"
                    row["coverage_state"] = "selected_pending_accession"
                    row["drain_state"] = "blocked_until_validated_accession"
            else:
                row["selected_master_candidate_id"] = ""
                row["covered_by_selected_master"] = "false"
                row["relationship"] = "derivative_or_transient_cleanup_candidate"
                row["coverage_state"] = "not_yet_accessioned"
                row["drain_state"] = "blocked_until_human_decisions_and_validated_accession"
            row["deletion_authorized"] = "false"
            rows.append(row)
        by_path = {r['source_path']: r for r in rows}
        for doc in noise_document_rows():
            path = doc['source_path']
            if path in excluded_paths:
                continue
            if path in selected_by_path:
                raise WorkbenchError('A selected Master source cannot be documentation noise.')
            row = by_path.get(path, {field: '' for field in DRAIN_FIELDS})
            row.update(source_path=path, source_class='protected_usb_source' if inside(Path(path), SOURCE_ROOT) else 'work_capture',
                       source_kind=Path(path).suffix.lstrip('.'), classification='documentation_noise',
                       relationship='redundant_documentation', evidence=doc['content_evidence'],
                       sha256=doc['content_sha256'], size_bytes=doc['size_bytes'],
                       selected_master_candidate_id='', covered_by_selected_master='false',
                       coverage_state='not_yet_accessioned', drain_state='blocked_until_human_decisions_and_validated_accession',
                       deletion_authorized='false', note=doc['review_note'])
            by_path[path] = row
        rows = list(by_path.values())
        return rows

    def check(self) -> dict[str, object]:
        decisions = self.decisions()
        errors = self.validate_decisions(decisions)
        if errors:
            raise WorkbenchError("; ".join(errors))
        dispositions = self.dispositions_for(decisions)
        drain = self.drain_for(decisions)
        resolved = sum(row["decision_state"] == "selected_master" for row in decisions.values())
        excluded_paths = unrelated_document_paths()
        expected_paths = ({r['source_path'] for r in self.drain_base} | {r['source_path'] for r in noise_document_rows()}) - excluded_paths
        if len(dispositions) != 271 or len(drain) != len(expected_paths) or {r['source_path'] for r in drain} != expected_paths:
            raise WorkbenchError("decision projections changed prepared scope")
        if any(row["deletion_authorized"] != "false" for row in dispositions + drain):
            raise WorkbenchError("decision projections carry deletion authority")
        return {
            "result": "ok", "version": VERSION, "objects": 79, "resources": 112,
            "master_candidates": 271, "resolved_objects": resolved, "unresolved_objects": 79 - resolved,
            "md_origin_proposals_hidden": 66, "local_binary_reviews": 13,
            "review_queue_unresolved": sum(decisions[object_id]["decision_state"] == "unresolved" for object_id in self.review_ids),
            "binary_differences": len(self.binary_differences),
            "metadata_evidence_rows": len(self.metadata_differences),
            "metadata_review_groups": self.metadata_group_stats["visible_groups"],
            "metadata_rows_consolidated": self.metadata_group_stats["consolidated_rows"],
            "metadata_equivalent_groups_hidden": self.metadata_group_stats["hidden_equivalent_groups"],
            "metadata_mixed_action_groups": self.metadata_group_stats["mixed_action_groups"],
            "preview_rows": len(self.previews),
            "drain_rows": len(drain),
            "drain_source_evidence_rows": len(self.drain_base),
            "selected_source_files_in_drain": sum(row["drain_state"] == "retain_selected_master_source" for row in drain),
            "deletion_authorized": False, "accession_performed": bool(accession_receipt()),
            "accession_target": str(ACCESSION_TARGET) if accession_receipt() else '',
        }

    def effective_metadata_value(self, row: dict[str, str], action: str) -> str:
        md_value = row["md_value"]
        local_value = row["local_value"]
        if action == "pending":
            return ""
        if action in {"accept", "ignore"}:
            return md_value or (local_value if action == "accept" else "")
        if action == "substitute":
            return local_value
        if action == "add":
            if not md_value:
                return local_value
            if local_value.casefold() in md_value.casefold():
                return md_value
            return f"{md_value}; {local_value}"
        raise WorkbenchError(f"invalid metadata action: {action}")

    def save(self, object_id: str, selection: str, note: str, metadata_group_actions: dict[str, str] | None = None) -> None:
        require_reconciliation_open()
        if object_id not in self.review_ids:
            raise WorkbenchError("object is not in the vague review queue")
        note = compact(note)
        metadata_group_actions = metadata_group_actions or {}
        with self.lock:
            decisions = self.decisions()
            decision = decisions[object_id]
            if selection == "unresolved":
                decision.update({
                    "decision_state": "unresolved", "selected_candidate_id": "",
                    "master_representation_type": "", "selected_resource_ids": "[]",
                    "representation_note": "", "decision_note": note,
                    "actor": self.actor, "decided_at": now(), "deletion_authorized": "false",
                })
            elif selection == "accept_md":
                resources = self.resources_by_object[object_id]
                md_candidates = [
                    row for row in self.candidates_by_object[object_id]
                    if row["source_kind"] == "museum_digital_capture"
                ]
                if len(resources) == 1 and len(md_candidates) == 1 and md_candidates[0]["master_eligible"] == "true":
                    selected = md_candidates[0]["candidate_id"]
                    representation = "single_file"
                    resource_ids = [resources[0]["resource_id"]]
                elif len(resources) > 1:
                    selected = COMPOUND_MD_SELECTION
                    representation = "ordered_md_resource_set"
                    resource_ids = [row["resource_id"] for row in resources]
                else:
                    raise WorkbenchError("MD representation is not eligible for acceptance")
                decision.update({
                    "decision_state": "selected_master", "selected_candidate_id": selected,
                    "master_representation_type": representation,
                    "selected_resource_ids": json.dumps(resource_ids),
                    "representation_note": "human accepted the complete MD representation after difference review",
                    "decision_note": note,
                    "actor": self.actor, "decided_at": now(), "deletion_authorized": "false",
                })
            else:
                if selection.startswith("substitute:"):
                    selection = selection.split(":", 1)[1]
                candidate = self.candidates_by_id.get(selection)
                displayed = {row["local_candidate_id"] for row in self.binary_differences_by_object[object_id]}
                if not candidate or candidate["candidate_id"] not in displayed or candidate["md_object_id"] != object_id or candidate["master_eligible"] != "true":
                    raise WorkbenchError("candidate is not eligible for this object")
                resource_ids = [candidate["resource_id"]] if candidate["source_kind"] == "museum_digital_capture" else []
                decision.update({
                    "decision_state": "selected_master", "selected_candidate_id": candidate["candidate_id"],
                    "master_representation_type": "single_file", "selected_resource_ids": json.dumps(resource_ids),
                    "representation_note": f"single-file Master from {candidate['source_kind']}", "decision_note": note,
                    "actor": self.actor, "decided_at": now(), "deletion_authorized": "false",
                })
            errors = self.validate_decisions(decisions)
            if errors:
                raise WorkbenchError("; ".join(errors))
            differences = [dict(row) for row in self.metadata_differences]
            by_difference = {row["difference_id"]: row for row in differences}
            allowed_groups = {
                row["group_id"] for row in self.metadata_groups_by_object.get(object_id, [])
            }
            if set(metadata_group_actions) - allowed_groups:
                raise WorkbenchError("metadata group action belongs to another object")
            decided_at = now()
            for group_id, action in metadata_group_actions.items():
                if action not in METADATA_ACTIONS:
                    raise WorkbenchError(f"invalid metadata action: {action}")
                group = self.metadata_groups_by_id[group_id]
                preferred_id = str(group["preferred_difference_id"])
                for member in group["members"]:
                    difference_id = member["difference_id"]
                    row = by_difference[difference_id]
                    member_action = action
                    if action in {"substitute", "add"} and difference_id != preferred_id:
                        member_action = "ignore"
                    row["decision_action"] = member_action
                    row["effective_value"] = self.effective_metadata_value(row, member_action)
                    row["decision_note"] = note
                    row["actor"] = self.actor
                    row["decided_at"] = decided_at
                    row["deletion_authorized"] = "false"
            ordered = [decisions[row["md_object_id"]] for row in self.objects]
            dispositions = self.dispositions_for(decisions)
            drain = self.drain_for(decisions)
            atomic_replace(DECISIONS_CSV, csv_payload(DECISION_FIELDS, ordered))
            atomic_replace(DISPOSITIONS_CSV, csv_payload(DISPOSITION_FIELDS, dispositions))
            atomic_replace(DRAIN_CSV, csv_payload(DRAIN_FIELDS, drain))
            atomic_replace(METADATA_DIFFERENCES_CSV, csv_payload(METADATA_DIFFERENCE_FIELDS, differences))
            self.metadata_differences = differences
            self.metadata_differences_by_id = by_difference
            self.metadata_differences_by_object = defaultdict(list)
            for row in differences:
                self.metadata_differences_by_object[row["md_object_id"]].append(row)
            self.rebuild_metadata_groups()

    def ordered_objects(self) -> list[dict[str, str]]:
        decisions = self.decisions()
        return sorted((row for row in self.objects if row["md_object_id"] in self.review_ids and (
            decisions[row["md_object_id"]]["decision_state"] != "selected_master"
            or any(
                member["decision_action"] == "pending"
                for group in self.metadata_groups_by_object.get(row["md_object_id"], [])
                for member in group["members"]
            )
        )), key=lambda row: (
            decisions[row["md_object_id"]]["decision_state"] == "selected_master",
            row["inventory"].casefold(),
        ))

    def next_object(self, current: str) -> str | None:
        ordered = self.ordered_objects()
        ids = [row["md_object_id"] for row in ordered]
        if not ids:
            return None
        if current not in ids:
            return ids[0]
        return ids[(ids.index(current) + 1) % len(ids)]


STYLE = """
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;margin:0;background:#f3f0e8;color:#20231f;line-height:1.42}a{color:#184f3b}
header{background:#173c30;color:white;padding:.8rem 1.25rem;position:sticky;top:0;z-index:3}header a{color:white}
main{max-width:1280px;margin:auto;padding:1rem}.summary,.panel,.card{background:white;border:1px solid #d2cdbc;border-radius:9px;padding:1rem;margin-bottom:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:.75rem}.card{margin:0}.muted{color:#666}.good{color:#17653c}.warn{color:#994c00}.bad{color:#9c2222}
.pill{display:inline-block;padding:.15rem .48rem;border-radius:999px;background:#ece7d9;margin:.1rem;font-size:.82rem}.pill.warn{background:#fff0d7}
.preview-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem}.preview-card{border:1px solid #d8d5ca;border-radius:7px;padding:.55rem;background:#faf9f5}
.preview{display:block;width:100%;height:330px;object-fit:contain;background:#ecebe7;border:1px solid #c9c6bd}.comparison{border:2px solid #d7b66f;border-radius:9px;padding:.8rem;margin:.85rem 0;background:#fffcf3}
.facts{display:flex;gap:.4rem;flex-wrap:wrap;margin:.45rem 0}.difference{font-weight:650;color:#7b3e00}.path{font-size:.78rem;color:#666;overflow-wrap:anywhere}
.metadata-row{border-top:1px solid #ddd;padding:.75rem 0}.values{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin:.4rem 0}.value{background:#f5f4ef;padding:.55rem;border-radius:5px;white-space:pre-wrap;overflow-wrap:anywhere}
.choices{display:flex;gap:.35rem;flex-wrap:wrap}.choice{border:1px solid #bbb;border-radius:999px;padding:.35rem .55rem;background:white;cursor:pointer}.choice:has(input:checked){border-color:#17653c;background:#e7f4eb;color:#0b4f2c;font-weight:650}
button{background:#173c30;color:white;border:0;border-radius:6px;padding:.65rem .9rem;cursor:pointer;font-weight:650}input[type=text],textarea{width:100%;padding:.55rem}textarea{min-height:4rem}
.savebar{position:sticky;bottom:0;background:#f7f3e8;border:1px solid #cfc6ad;border-radius:8px;padding:.7rem;box-shadow:0 -3px 14px #0002}.master-choice{display:block;border:1px solid #bbb;border-radius:7px;padding:.65rem;margin:.55rem 0;background:white}.master-choice:has(input:checked){border:2px solid #17653c;background:#eef8f1}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:.45rem;overflow-wrap:anywhere}code{font-size:.8rem;overflow-wrap:anywhere}.error{background:#ffe3df;border-color:#c33}
@media(max-width:650px){.preview{height:260px}.values{grid-template-columns:1fr}main{padding:.65rem}}
"""


def accession_receipt() -> dict:
    path = ACCESSION_TARGET / '00_MANIFESTS/accession-completion.json'
    if not path.is_file():
        return {}
    receipt = json.loads(path.read_text())
    return receipt if receipt.get('accession_performed') and receipt.get('validation_result') == 'ok' else {}


def require_reconciliation_open() -> None:
    if accession_receipt():
        raise WorkbenchError('Collection 551 is accessioned. Saved decisions are available for read-only review.')


def page(title: str, body: str) -> bytes:
    if accession_receipt():
        body = re.sub(r'''(<form\b(?=[^>]*\bmethod=['"]post['"])[^>]*>)(.*?)</form>''',
                      lambda m: m[1] + '<fieldset disabled>' + m[2] + '</fieldset></form>', body, flags=re.I | re.S)
        links = ' · '.join('<a href="' + html.escape('/file?' + urlencode({'path': str(ACCESSION_TARGET / relative)}), quote=True) + '">' + label + '</a>' for relative, label in [
            ('00_README.md', 'Accession summary'), ('CATALOG.xlsx', 'Catalogue'),
            ('00_MANIFESTS/drain-list.csv', 'Drain list (359 paths)'),
            ('00_MANIFESTS/source-disposition.csv', 'Complete source register (553 paths)')])
        body = '<section class="panel"><h2>Collection 551 accessioned</h2><p>79 objects · 108 Master files · 21 documents · 108 access previews. Saved review decisions are read-only. Sources remain in place; deletion is not authorized.</p><p>' + links + '</p></section>' + body
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
            "<header><a href='/'>Collection 551</a> · <a href='/'>Master review</a> · <a href='/documents'>Documentation</a></header>"
            f"<main>{body}</main></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "Collection551Workbench/1.0"

    @property
    def workbench(self) -> Workbench:
        return self.server.workbench  # type: ignore[attr-defined]

    def send_html(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.index()
            elif parsed.path == "/object":
                object_id = parse_qs(parsed.query).get("id", [""])[0]
                self.object_page(object_id)
            elif parsed.path == "/file":
                self.file_response(parse_qs(parsed.query).get("path", [""])[0])
            elif parsed.path == "/documents":
                from collection_551_documents import STYLE as DOCUMENT_STYLE
                args = parse_qs(parsed.query)
                self.send_html(page("Documentation - Collection 551", "<style>" + DOCUMENT_STYLE + "</style>" + self.server.documents.index(args.get("q", [""])[0], args.get("status", ["pending"])[0])))
            elif parsed.path == "/document":
                from collection_551_documents import STYLE as DOCUMENT_STYLE
                args = parse_qs(parsed.query)
                body = self.server.documents.detail(args.get("id", [""])[0], args.get("compare", [""])[0], int(args.get("page", ["1"])[0]), int(args.get("compare_page", ["1"])[0]), args.get("saved", [""])[0] == "1")
                self.send_html(page("Document comparison - Collection 551", "<style>" + DOCUMENT_STYLE + "</style>" + body))
            elif parsed.path == "/document-preview":
                args = parse_qs(parsed.query)
                self.file_response(str(self.server.documents.image_path(args.get("id", [""])[0], int(args.get("page", ["1"])[0]))))
            elif parsed.path == "/health":
                payload = (json.dumps(self.workbench.check(), sort_keys=True) + "\n").encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
            else:
                self.send_error(404)
        except (WorkbenchError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            self.send_html(page("Error", f"<section class='panel error'><h1>Error</h1><p>{html.escape(str(error))}</p></section>"), 400)

    def do_POST(self) -> None:
        if accession_receipt():
            self.send_error(409, 'Collection 551 is accessioned; review decisions are read-only.')
            return
        if urlparse(self.path).path == "/document-save":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 32_768:
                    raise WorkbenchError("invalid document form size")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                identifier = form.get("id", [""])[0]
                self.server.documents.save(identifier, form.get("action", [""])[0], form.get("source", [""])[0], form.get("note", [""])[0])
                self.redirect("/documents")
            except (WorkbenchError, OSError, ValueError) as error:
                self.send_html(page("Document choice not saved", "<section class='panel error'><h1>Choice not saved</h1><p>" + html.escape(str(error)) + "</p></section>"), 400)
            return
        if urlparse(self.path).path != "/save":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 32_768:
                raise WorkbenchError("form is too large")
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            object_id = form.get("object_id", [""])[0]
            selection = form.get("selection", ["unresolved"])[0]
            note = form.get("note", [""])[0]
            metadata_group_actions = {
                key.removeprefix("metadata_group__"): values[0]
                for key, values in form.items() if key.startswith("metadata_group__")
            }
            self.workbench.save(object_id, selection, note, metadata_group_actions)
            next_id = self.workbench.next_object(object_id)
            self.redirect("/object?" + urlencode({"id": next_id}) if next_id else "/")
        except (WorkbenchError, OSError, ValueError) as error:
            self.send_html(page("Error", f"<section class='panel error'><h1>Decision not saved</h1><p>{html.escape(str(error))}</p></section>"), 400)

    def index(self) -> None:
        status = self.workbench.check()
        decisions = self.workbench.decisions()
        remaining = self.workbench.ordered_objects()
        cards = []
        for obj in remaining:
            decision = decisions[obj["md_object_id"]]
            state = "metadata pending" if decision["decision_state"] == "selected_master" else "Master pending"
            binary_count = len(self.workbench.binary_differences_by_object[obj["md_object_id"]])
            metadata_count = len(self.workbench.metadata_groups_by_object.get(obj["md_object_id"], []))
            md_candidate = next(
                row for row in self.workbench.candidates_by_object[obj["md_object_id"]]
                if row["source_kind"] == "museum_digital_capture"
            )
            preview = self.workbench.previews_by_candidate[md_candidate["candidate_id"]]
            preview_url = "/file?" + urlencode({"path": preview["preview_path"]})
            cards.append(
                "<article class='card'>"
                f"<a href='/object?{urlencode({'id':obj['md_object_id']})}'><img class='preview' src='{preview_url}' alt='Preview'></a>"
                f"<h3><a href='/object?{urlencode({'id':obj['md_object_id']})}'>{html.escape(obj['inventory'])}</a></h3>"
                f"<p>{html.escape(obj['title'])}</p>"
                f"<span class='pill'>{state}</span><span class='pill warn'>{binary_count} binary difference(s)</span>"
                f"<p class='muted'>{obj['resource_count']} MD file(s) · {metadata_count} metadata difference(s)</p>"
                "</article>"
            )
        review_resolved = len(self.workbench.review_ids) - len(remaining)
        hidden_ids = set(decisions) - self.workbench.review_ids
        hidden_unconfirmed = sum(decisions[oid]["decision_state"] == "unresolved" for oid in hidden_ids)
        body = (
            "<section class='summary'><h1>Pending review</h1>"
            f"<p><b>{review_resolved}</b> reviewed · <b>{len(remaining)}</b> remaining. "
            f"The {len(hidden_ids)} clear MD-default objects are hidden: "
            f"{len(hidden_ids) - hidden_unconfirmed} confirmed, {hidden_unconfirmed} unconfirmed.</p>"
            "</section>"
            "<section class='panel'><h2>Documentation</h2><p>Preview the proposed documentation, compare related versions, and review damaged candidates.</p><a href='/documents'>Open documentation review →</a></section>"
            + ("<section class='grid'>" + "".join(cards) + "</section>" if cards else
               "<p>No pending reviews among the 13 local-binary review objects.</p>")
        )
        self.send_html(page("Collection 551", body))

    def object_page(self, object_id: str) -> None:
        if object_id not in self.workbench.review_ids:
            raise WorkbenchError("object is not in the vague review queue")
        obj = self.workbench.objects_by_id[object_id]
        decision = self.workbench.decisions()[object_id]
        saved_requested = bool(parse_qs(urlparse(self.path).query).get("saved"))
        saved = (
            "<p class='good'><b>Decision saved.</b></p>"
            if saved_requested and decision.get("decided_at") else ""
        )
        md_cards = []
        md_candidates = [
            row for row in self.workbench.candidates_by_object[object_id]
            if row["source_kind"] == "museum_digital_capture"
        ]
        for position, candidate in enumerate(md_candidates, 1):
            preview = self.workbench.previews_by_candidate[candidate["candidate_id"]]
            preview_url = "/file?" + urlencode({"path": preview["preview_path"]})
            source_url = "/file?" + urlencode({"path": candidate["candidate_path"]})
            md_cards.append(
                "<article class='preview-card'>"
                f"<a href='{source_url}' target='_blank'><img class='preview' src='{preview_url}' alt='MD page 1'></a>"
                f"<p><b>MD file {position}</b> · {candidate['format'].upper()} · {human_size(candidate['size_bytes'])} · "
                f"pages {candidate['page_count'] or 'image'} · {candidate['width_px'] or '—'}×{candidate['height_px'] or '—'}</p>"
                f"<p class='path'>{html.escape(Path(candidate['candidate_path']).name)}</p></article>"
            )

        selected = decision["selected_candidate_id"]
        md_selected = selected == COMPOUND_MD_SELECTION or (
            selected in self.workbench.candidates_by_id
            and self.workbench.candidates_by_id[selected]["source_kind"] == "museum_digital_capture"
        )
        comparison_cards = []
        for difference in self.workbench.binary_differences_by_object[object_id]:
            candidate = self.workbench.candidates_by_id[difference["local_candidate_id"]]
            preview = self.workbench.previews_by_candidate[candidate["candidate_id"]]
            preview_url = "/file?" + urlencode({"path": preview["preview_path"]})
            source_url = "/file?" + urlencode({"path": candidate["candidate_path"]})
            duplicate_count = len(parsed_ids(difference["duplicate_candidate_ids"]) or [])
            checked = " checked" if selected == candidate["candidate_id"] else ""
            dimensions = (
                f"{difference['local_width_px']}×{difference['local_height_px']} px"
                if difference["local_width_px"] and difference["local_height_px"] else "PDF"
            )
            comparison_cards.append(
                "<article class='comparison'><div class='preview-grid'><div class='preview-card'>"
                f"<a href='{source_url}' target='_blank'><img class='preview' src='{preview_url}' alt='Local candidate page 1'></a>"
                f"<p><b>Local alternative</b> · {difference['local_format'].upper()} · {human_size(difference['local_size_bytes'])} · "
                f"pages {difference['local_page_count'] or 'image'} · {dimensions}</p></div>"
                "<div><h3>Difference from complete MD representation</h3>"
                f"<p class='difference'>{html.escape(difference['difference_summary'])}</p>"
                f"<div class='facts'><span class='pill'>MD {human_size(difference['md_size_bytes'])}</span>"
                f"<span class='pill'>Local {human_size(difference['local_size_bytes'])}</span>"
                f"<span class='pill'>{duplicate_count + 1} occurrence(s)</span></div>"
                f"<p class='path'>{html.escape(difference['local_path'])}</p>"
                f"<label class='master-choice'><input type='radio' name='selection' value='substitute:{html.escape(candidate['candidate_id'], quote=True)}'{checked}> "
                "<b>Substitute</b> - use this local file as the single Master</label>"
                "</div></div></article>"
            )

        metadata_rows = []
        for difference in self.workbench.metadata_groups_by_object.get(object_id, []):
            name = "metadata_group__" + str(difference["group_id"])
            action = str(difference["action"])
            sources = difference["evidence_sources"]
            choices = []
            for value, label in (("accept", "Accept"), ("ignore", "Ignore"), ("substitute", "Substitute"), ("add", "Add")):
                checked = " checked" if action == value else ""
                choices.append(
                    f"<label class='choice'><input type='radio' name='{name}' value='{value}'{checked}> {label}</label>"
                )
            pending = " checked" if action == "pending" else ""
            choices.append(f"<label class='choice muted'><input type='radio' name='{name}' value='pending'{pending}> Pending</label>")
            variants = difference["local_variants"]
            variants_html = ""
            if len(variants) > 1:
                variants_html = (
                    f"<details><summary>{len(variants)} equivalent source forms consolidated</summary><ul>"
                    + "".join(f"<li>{html.escape(str(value))}</li>" for value in variants)
                    + "</ul></details>"
                )
            mixed = (
                "<p class='warn'><b>Mixed saved actions preserved.</b> Choose one action only if you want to replace them for this consolidated group.</p>"
                if action == "mixed" else ""
            )
            metadata_rows.append(
                "<div class='metadata-row'>"
                f"<h3>{html.escape(str(difference['field']))}</h3><div class='values'>"
                f"<div class='value'><b>MD</b><br>{html.escape(str(difference['md_value']) or '[empty]')}</div>"
                f"<div class='value'><b>Consolidated local evidence</b><br>{html.escape(str(difference['local_value']) or '[empty]')}</div></div>"
                f"<p class='muted'>{len(sources)} source row(s)</p>{variants_html}{mixed}<div class='choices'>{''.join(choices)}</div></div>"
            )

        accept_checked = " checked" if md_selected else ""
        unresolved_checked = " checked" if decision["decision_state"] == "unresolved" else ""
        selected = decision["selected_candidate_id"]
        body = (
            saved + f"<section class='panel'><p><a href='/'>← vague objects</a></p><h1>{html.escape(obj['inventory'])}</h1>"
            f"<h2>{html.escape(obj['title'])}</h2><p>{html.escape(obj['description'])}</p>"
            f"<p><a href='{html.escape(obj['object_url'], quote=True)}' target='_blank' rel='noreferrer'>museum-digital object {object_id}</a></p></section>"
            "<form method='post' action='/save'><input type='hidden' name='object_id' value='" + html.escape(object_id, quote=True) + "'>"
            "<section class='panel'><h2>MD representation</h2><p class='muted'>Rendered page 1; multipart files remain in recorded order.</p>"
            "<div class='preview-grid'>" + "".join(md_cards) + "</div>"
            f"<label class='master-choice'><input type='radio' name='selection' value='accept_md'{accept_checked}> <b>Accept</b> - use this complete MD representation as the Master</label></section>"
            "<section class='panel'><h2>Only material binary differences</h2>" + "".join(comparison_cards) +
            f"<label class='master-choice'><input type='radio' name='selection' value='unresolved'{unresolved_checked}> <b>Ignore for now</b> - leave this object unresolved</label></section>"
            "<section class='panel'><h2>Metadata differences</h2>"
            "<p class='muted'><b>Accept</b> keeps MD wording and records local corroboration; <b>Ignore</b> rejects that local evidence; "
            "<b>Substitute</b> uses the local value instead; <b>Add</b> keeps MD and appends the local value.</p>" +
            ("".join(metadata_rows) if metadata_rows else "<p class='muted'>No local metadata difference for this object.</p>") + "</section>"
            "<section class='savebar'>"
            f"<label>Short note<textarea name='note'>{html.escape(decision['decision_note'])}</textarea></label>"
            "<p class='muted'>Save writes reconciliation CSVs only. No accession, source change, derivative generation, or deletion.</p>"
            "<button type='submit'>Save and next vague object</button></section></form>"
        )
        self.send_html(page(obj["inventory"], body))

    def file_response(self, requested: str) -> None:
        path = Path(requested)
        if not path.is_file() or not (inside(path, ROOT) or inside(path, SOURCE_ROOT) or (accession_receipt() and inside(path, ACCESSION_TARGET))):
            raise WorkbenchError("unsafe or missing file")
        total = path.stat().st_size
        start, end = 0, total - 1
        status = 200
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_error(416)
                return
            if match.group(1):
                start = int(match.group(1)); end = int(match.group(2) or total - 1)
            else:
                length = int(match.group(2)); start = max(0, total - length)
            end = min(end, total - 1)
            if start > end or start >= total:
                self.send_error(416)
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
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

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workbench: Workbench):
        super().__init__(address, Handler)
        self.workbench = workbench
        from collection_551_documents import Documents
        self.documents = Documents(workbench)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--actor", default="Vladimir")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8771)
    result.add_argument("--check", action="store_true")
    result.add_argument("--verify-hashes", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    workbench = Workbench(args.actor, verify_hashes=args.verify_hashes)
    if args.check:
        print(json.dumps(workbench.check(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    server = Server((args.host, args.port), workbench)
    print(f"collection-551 workbench {VERSION}: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
