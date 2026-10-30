#!/usr/bin/env python3
"""Project-provenance suggestions for selected local WF image objects.

The protected archive is read-only.  This tool does not change object, Master,
metadata, or accession decisions.  It records separately whether a project is
suggested by an exact inventory reference in a project document or merely by a
visually related copy living in a project working folder.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.3.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
SOURCE_ARCHIVE = SOURCE_ROOT / "restore/archive"
SOURCE_WF = SOURCE_ARCHIVE / "WF"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
BOOK_CANDIDATES = REVIEW_ROOT / "buchprojekt-unbound-object-candidates.csv"
BOOK_DECISIONS = REVIEW_ROOT / "buchprojekt-unbound-object-decisions.csv"
ORPHAN_DECISIONS = REVIEW_ROOT / "orphan-object-decisions.csv"
CLASSIFICATION = REVIEW_ROOT / "local-object-project-classification.csv"
EVIDENCE = REVIEW_ROOT / "local-object-project-evidence.csv"
RECEIPT = REVIEW_ROOT / "local-object-project-scan.json"

CLASSIFICATION_FIELDS = (
    "record_key", "inventory", "selected_master_path", "current_decision_source",
    "proposed_project", "assignment_state", "confidence", "project_evidence_count",
    "project_candidates_json", "metadata_document_count", "visual_path_count",
    "classification_note", "human_confirmed", "deletion_authorized",
)
EVIDENCE_FIELDS = (
    "record_key", "inventory", "project", "evidence_kind", "evidence_path",
    "evidence_detail", "weight", "source_read_only", "deletion_authorized",
)

INVENTORY_RE = re.compile(
    rb"(?i)(?:^|[^A-Z0-9])TFA(?:-(?:FOT|NEG|KB))?[-_ ]?"
    rb"(\d{5,8}[a-z]?(?:[-_.]\d{1,2}[a-z]?)?)"
)

# These are existing archive project/working-corpus names, not a new authority.
PROJECT_RULES = (
    ("brigade-books", ("/wf/brigadebuch-projekt/", "brigadebuch-projekt/")),
    ("industriesalon-book", (
        "/wf/buchprojekt_industriesalon-schöneweide/", "/wf/buchprojekt/",
        "buchprojekt_industriesalon-schöneweide/", "buchprojekt/",
        "/freistellen/buchprojekt/", "/freistellen/buch_fehlende-tiffs/",
    )),
    ("albert-book-selection", ("/wf/buch/", "buch/auswahlliste_albert")),
    ("kurt-schwarz", ("/wf/kurt schwarz/", "kurt schwarz/")),
    ("baecker", ("/wf/baecker/", "baecker/")),
    ("pressebildarchiv", ("/wf/pressebildarchiv/", "pressebildarchiv/")),
    ("paragon", ("/wf/paragon fertig für md/", "/freistellen/paragon")),
    ("trafo", ("/wf/trafo/", "trafo/")),
    ("wf-sender", (
        "/wf/wf-sender projekt/", "/wf/wf-sender/", "wf-sender projekt/",
        "/wf/namenssuche+wf-sender/", "/wf/namenssuche+wf-sender+fbs/",
    )),
    ("wf-thesaurus", ("/wf/thesaurus/", "thesaurus/")),
    ("research-reports", ("/wf/forschungs- und entwicklungsberichte/", "forschungs- und entwicklungsberichte/")),
    ("exhibition", ("/wf/ausstellung", "/freistellen/ausstellung")),
    ("roehrenmaennchen-exhibition", ("röhrenmaennchen", "röhrenmännchen")),
    ("koehler", ("/wf/köhler/", "köhler/")),
    ("terminuhr-project", ("/wf/terminuhr_", "terminuhr_")),
)

# Live source roots are explicit so the scan remains bounded and does not walk
# the whole 200k-file WF tree.  These names already exist in the archive.
PROJECT_DOCUMENT_ROOTS = (
    ("brigade-books", SOURCE_WF / "BRIGADEBUCH-Projekt"),
    ("industriesalon-book", SOURCE_WF / "Buchprojekt"),
    ("industriesalon-book", SOURCE_WF / "Buchprojekt_Industriesalon-Schöneweide"),
    ("industriesalon-book", SOURCE_ARCHIVE / "Buchprojekt_Industriesalon-Schöneweide_Originaldateien-für-Sutton-Verlag"),
    ("albert-book-selection", SOURCE_WF / "Buch"),
    ("kurt-schwarz", SOURCE_WF / "Kurt Schwarz"),
    ("baecker", SOURCE_WF / "Baecker"),
    ("pressebildarchiv", SOURCE_WF / "Pressebildarchiv"),
    ("paragon", SOURCE_WF / "Paragon fertig für md"),
    ("trafo", SOURCE_WF / "Trafo"),
    ("wf-sender", SOURCE_WF / "WF-Sender"),
    ("wf-sender", SOURCE_WF / "WF-Sender Projekt"),
    ("wf-sender", SOURCE_WF / "Namenssuche+WF-Sender"),
    ("wf-sender", SOURCE_WF / "Namenssuche+WF-Sender+FBs"),
    ("wf-thesaurus", SOURCE_WF / "Thesaurus"),
    ("research-reports", SOURCE_WF / "Forschungs- und Entwicklungsberichte"),
    ("exhibition", SOURCE_WF / "Ausstellung"),
    ("exhibition", SOURCE_WF / "Ausstellung Unvollendete Metropole"),
    ("exhibition", SOURCE_WF / "Ausstellung_Kurt-Schwarz"),
    ("roehrenmaennchen-exhibition", SOURCE_WF / "Ausstellung-Röhrenmaennchen"),
    ("koehler", SOURCE_WF / "Köhler"),
    ("terminuhr-project", SOURCE_WF / "Terminuhr_XX_Jahre_DDR_1969_Fotos-Guido-Schlootz"),
)


class ScanError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL, extrasaction="ignore")
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_mount() -> dict[str, str]:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    rows = json.loads(completed.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise ScanError("Elements did not resolve to one mount")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise ScanError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def project_for_path(path: str) -> str:
    folded = "/" + path.replace("\\", "/").casefold().lstrip("/")
    for project, tokens in PROJECT_RULES:
        if any(token.casefold() in folded for token in tokens):
            return project
    return ""


def normalize_inventory(raw: bytes) -> str:
    body = re.sub(r"[_ .]+", "-", raw.decode("ascii", "ignore")).strip("-")
    return "TFA-" + body


def document_bytes(path: Path, extension: str) -> bytes:
    if extension == "csv":
        return path.read_bytes()[:30_000_000]
    if extension in {"xlsx", "xlsm"}:
        output = bytearray()
        with zipfile.ZipFile(path) as archive:
            members = [
                item for item in archive.infolist()
                if item.filename.endswith("sharedStrings.xml")
                or (item.filename.startswith("xl/worksheets/") and item.filename.endswith(".xml"))
            ]
            for item in members:
                if item.file_size <= 50_000_000 and len(output) < 30_000_000:
                    output.extend(archive.read(item)[:30_000_000 - len(output)])
        return bytes(output)
    if extension == "docx":
        output = bytearray()
        with zipfile.ZipFile(path) as archive:
            members = [
                item for item in archive.infolist()
                if item.filename.startswith("word/") and item.filename.endswith(".xml")
            ]
            for item in members:
                if item.file_size <= 50_000_000 and len(output) < 30_000_000:
                    output.extend(archive.read(item)[:30_000_000 - len(output)])
        return bytes(output)
    if extension == "odt":
        with zipfile.ZipFile(path) as archive:
            item = archive.getinfo("content.xml")
            return archive.read(item)[:30_000_000]
    if extension == "xls":
        completed = subprocess.run(
            ["xls2csv", str(path)], capture_output=True, timeout=20, check=False,
        )
        return completed.stdout[:30_000_000]
    return b""


def selected_objects() -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    objects: dict[str, dict[str, str]] = {}
    candidate_paths: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            """SELECT d.record_key,o.inventory,c.source_path
               FROM local_object_decisions d
               JOIN objects o ON o.record_key=d.record_key
               JOIN candidates c ON c.candidate_id=d.selected_candidate_id
               WHERE d.outcome='new_local_object'"""
        ):
            objects[row["record_key"]] = {
                "record_key": row["record_key"], "inventory": row["inventory"],
                "selected_master_path": row["source_path"], "current_decision_source": "sqlite_local_object_decision",
            }
        placeholders = ",".join("?" for _ in objects)
        for row in connection.execute(
            f"SELECT record_key,source_path,match_basis FROM candidates WHERE record_key IN ({placeholders})",
            tuple(objects),
        ):
            candidate_paths[row["record_key"]].append({
                "source_path": row["source_path"], "detail": row["match_basis"],
            })

    book_by_key: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(BOOK_CANDIDATES):
        book_by_key[row["record_key"]].append(row)
    for decision in read_csv(BOOK_DECISIONS):
        if decision["decision"] not in {"apply_metadata", "apply_edited_metadata"}:
            continue
        key = decision["record_key"]
        inventory = book_by_key[key][0]["inventory"]
        objects[key] = {
            "record_key": key, "inventory": inventory,
            "selected_master_path": decision["selected_master_path"],
            "current_decision_source": "buchprojekt_review_decision",
        }
        for row in book_by_key[key]:
            candidate_paths[key].append({"source_path": row["source_path"], "detail": "exact_book_visual_group"})

    for decision in read_csv(ORPHAN_DECISIONS):
        if decision["decision"] != "selected_master":
            continue
        key = decision["record_key"]
        objects[key] = {
            "record_key": key, "inventory": decision["inventory"],
            "selected_master_path": decision["selected_source_path"],
            "current_decision_source": "remaining_hold_review_decision",
        }
        candidate_paths[key].append({
            "source_path": decision["selected_source_path"], "detail": "human_selected_remaining_hold_master",
        })
    return objects, dict(candidate_paths)


def spreadsheet_evidence(
    inventory_to_keys: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    evidence: list[dict[str, Any]] = []
    stats = {
        "documents_considered": 0, "documents_read": 0,
        "documents_skipped_large": 0, "document_errors": 0,
    }
    documents: list[tuple[Path, str, str]] = []
    accepted = {"xlsx", "xlsm", "xls", "csv", "docx", "odt"}
    seen_paths: set[Path] = set()
    for project, scan_root in PROJECT_DOCUMENT_ROOTS:
        if not scan_root.is_dir():
            continue
        for root, _directories, filenames in os.walk(scan_root):
            for filename in filenames:
                extension = Path(filename).suffix.lower().lstrip(".")
                path = Path(root) / filename
                if extension not in accepted or path in seen_paths:
                    continue
                seen_paths.add(path)
                documents.append((path, extension, project))
    stats["documents_considered"] = len(documents)
    wanted = set(inventory_to_keys)
    for path, extension, project in sorted(documents):
        try:
            if path.stat().st_size >= 50_000_000:
                stats["documents_skipped_large"] += 1
                continue
            content = document_bytes(path, extension)
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError):
            stats["document_errors"] += 1
            continue
        stats["documents_read"] += 1
        inventories = {normalize_inventory(match.group(1)) for match in INVENTORY_RE.finditer(content)}
        for inventory in inventories & wanted:
            for record_key in inventory_to_keys[inventory]:
                evidence.append({
                    "record_key": record_key, "inventory": inventory, "project": project,
                    "evidence_kind": "exact_inventory_in_project_document",
                    "evidence_path": str(path), "evidence_detail": str(path.relative_to(SOURCE_ARCHIVE)),
                    "weight": 3, "source_read_only": "true", "deletion_authorized": "false",
                })
    return evidence, stats


def visual_index_evidence(objects: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_by_relative: dict[str, dict[str, str]] = {}
    prefix = str(SOURCE_ROOT) + "/"
    for obj in objects.values():
        path = obj["selected_master_path"]
        if path.startswith(prefix):
            selected_by_relative[path[len(prefix):]] = obj

    features: defaultdict[str, dict[str, str]] = defaultdict(dict)
    paths: dict[str, str] = {}
    with closing(sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True)) as connection:
        root_row = connection.execute(
            "SELECT root_id FROM visual_roots WHERE root_path=?", (str(SOURCE_ROOT),)
        ).fetchone()
        if not root_row:
            raise ScanError("Elements visual-index root is missing")
        root_id = root_row[0]
        latest = connection.execute(
            "SELECT max(completed_at) FROM index_runs WHERE status='complete' AND roots_json LIKE ?",
            (f"%{SOURCE_ROOT}%",),
        ).fetchone()[0]
        for asset_id, relative_path, extractor_id, feature_hex in connection.execute(
            """SELECT a.asset_id,a.relative_path,f.extractor_id,f.feature_hex
               FROM visual_assets a JOIN visual_features f ON f.asset_id=a.asset_id
               WHERE a.root_id=? AND a.status='indexed'""", (root_id,),
        ):
            paths[asset_id] = relative_path
            features[asset_id][extractor_id] = feature_hex

    selected_signatures: dict[str, tuple[str, str, str]] = {}
    project_by_signature: defaultdict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    project_assets: defaultdict[str, int] = defaultdict(int)
    for asset_id, values in features.items():
        signature = (
            values.get("ahash-64-v2", ""), values.get("dhash-64-v2", ""),
            values.get("phash-dct-64-v2", ""),
        )
        if not all(signature):
            continue
        relative_path = paths[asset_id]
        if relative_path in selected_by_relative:
            selected_signatures[relative_path] = signature
        project = project_for_path(prefix + relative_path)
        if project:
            project_assets[project] += 1
            project_by_signature[signature].append((project, relative_path))

    evidence: list[dict[str, Any]] = []
    matched_objects: defaultdict[str, set[str]] = defaultdict(set)
    for selected_path, signature in selected_signatures.items():
        obj = selected_by_relative[selected_path]
        seen: set[tuple[str, str]] = set()
        for project, relative_path in project_by_signature.get(signature, []):
            marker = (project, relative_path)
            if relative_path == selected_path or marker in seen:
                continue
            seen.add(marker)
            matched_objects[project].add(obj["record_key"])
            evidence.append({
                "record_key": obj["record_key"], "inventory": obj["inventory"], "project": project,
                "evidence_kind": "exact_visual_signature_in_project_index",
                "evidence_path": prefix + relative_path,
                "evidence_detail": "same ahash+dhash+phash signature in the existing visual index",
                "weight": 1, "source_read_only": "true", "deletion_authorized": "false",
            })
    stats = {
        "index_latest_completed_at": latest,
        "selected_master_paths": len(selected_by_relative),
        "selected_masters_indexed": len(selected_signatures),
        "project_asset_counts": dict(sorted(project_assets.items())),
        "exact_three_hash_object_matches": {
            project: len(matched_objects.get(project, set())) for project in sorted(project_assets)
        },
        "ownership_inference": False,
    }
    return evidence, stats


def prepare() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    mount = source_mount()
    objects, candidates = selected_objects()
    inventory_to_keys: defaultdict[str, list[str]] = defaultdict(list)
    for row in objects.values():
        inventory_to_keys[row["inventory"]].append(row["record_key"])

    evidence: list[dict[str, Any]] = []
    for record_key, paths in candidates.items():
        obj = objects[record_key]
        if obj["current_decision_source"] == "buchprojekt_review_decision":
            evidence.append({
                "record_key": record_key, "inventory": obj["inventory"],
                "project": "industriesalon-book",
                "evidence_kind": "project_bound_book_decision",
                "evidence_path": str(BOOK_DECISIONS),
                "evidence_detail": "applied metadata review decision from the Industriesalon book scope",
                "weight": 3, "source_read_only": "true", "deletion_authorized": "false",
            })
        seen: set[tuple[str, str]] = set()
        for row in paths:
            project = project_for_path(row["source_path"])
            marker = (project, row["source_path"])
            if not project or marker in seen:
                continue
            seen.add(marker)
            evidence.append({
                "record_key": record_key, "inventory": obj["inventory"], "project": project,
                "evidence_kind": "visual_copy_in_project_path",
                "evidence_path": row["source_path"], "evidence_detail": row["detail"],
                "weight": 1,
                "source_read_only": "true", "deletion_authorized": "false",
            })
    spreadsheet_rows, document_stats = spreadsheet_evidence(dict(inventory_to_keys))
    evidence.extend(spreadsheet_rows)
    visual_rows, visual_stats = visual_index_evidence(objects)
    evidence.extend(visual_rows)

    evidence_by_key: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        evidence_by_key[row["record_key"]].append(row)
    classification: list[dict[str, Any]] = []
    state_counts: defaultdict[str, int] = defaultdict(int)
    project_counts: defaultdict[str, int] = defaultdict(int)
    for record_key in sorted(objects):
        obj = objects[record_key]
        rows = evidence_by_key.get(record_key, [])
        scores: defaultdict[str, int] = defaultdict(int)
        for row in rows:
            # Repeated references measure coverage, not ownership strength.  Score
            # each evidence class once per project so a large workbook corpus does
            # not overpower a human project-bound review decision.
            scores[row["project"]] = max(scores[row["project"]], int(row["weight"]))
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        proposed = ranked[0][0] if ranked else ""
        top_ties = sum(score == ranked[0][1] for _project, score in ranked) if ranked else 0
        projects = sorted(scores)
        metadata_count = sum(row["evidence_kind"] == "exact_inventory_in_project_document" for row in rows)
        visual_count = sum(row["evidence_kind"] != "exact_inventory_in_project_document" for row in rows)
        if not ranked:
            state, confidence, note = "unresolved", "none", "reference/freistellen corpus only; no identifiable project evidence"
        elif top_ties > 1:
            state, confidence, note = "conflict", "review", "equally weighted evidence names multiple projects"
            proposed = ""
        elif len(projects) > 1:
            state, confidence, note = "review", "mixed", "one project leads but other project-use evidence is present"
        elif metadata_count:
            state, confidence, note = "suggested", "high", "exact TFA inventory appears in a project spreadsheet"
        elif obj["current_decision_source"] == "buchprojekt_review_decision":
            state, confidence, note = "suggested", "high", "human book-project review decision is bound to this image"
        else:
            state, confidence, note = "suggested", "medium", "a visually related copy occurs in a named project working path"
        state_counts[state] += 1
        if proposed:
            project_counts[proposed] += 1
        classification.append({
            **obj, "proposed_project": proposed, "assignment_state": state,
            "confidence": confidence, "project_evidence_count": len(rows),
            "project_candidates_json": json.dumps(projects, ensure_ascii=False),
            "metadata_document_count": metadata_count, "visual_path_count": visual_count,
            "classification_note": note, "human_confirmed": "false",
            "deletion_authorized": "false",
        })
    receipt = {
        "format": "iss-tfa-local-object-project-provenance-review-v1", "version": VERSION,
        "result": "ok", "prepared_at": now(), "source_mount": mount,
        "source_read_only": True, "source_modified": False,
        "review_projection_only": True, "human_project_decisions_written": 0,
        "master_decisions_changed": False, "metadata_decisions_changed": False,
        "accession_changed": False, "deletion_authorized": False,
        "selected_local_object_count": len(objects), "classification_row_count": len(classification),
        "evidence_row_count": len(evidence), "assignment_state_counts": dict(sorted(state_counts.items())),
        "leading_project_counts": dict(sorted(project_counts.items())),
        "document_scan": document_stats,
        "visual_index_crosscheck": visual_stats,
        "evidence_policy": {
            "exact_inventory_in_project_document": "high-confidence project suggestion, still not a human decision",
            "project_bound_book_decision": "high-confidence project suggestion from current reviewed book scope",
            "visual_copy_in_project_path": "project-use evidence only; location does not prove ownership",
            "exact_visual_signature_in_project_index": "exact whole-image visual-copy evidence only; location does not prove ownership",
            "embedded_pdf_or_page_image_matching": "not included in this pass",
        },
        "outputs": {
            "classification": str(CLASSIFICATION), "evidence": str(EVIDENCE), "receipt": str(RECEIPT),
        },
    }
    return classification, sorted(evidence, key=lambda row: (row["record_key"], row["project"], row["evidence_kind"], row["evidence_path"])), receipt


def apply(replace_projection: bool = False) -> dict[str, Any]:
    existing = [path for path in (CLASSIFICATION, EVIDENCE, RECEIPT) if path.exists()]
    if existing:
        if not replace_projection:
            raise ScanError(f"refusing to overwrite existing project projection: {existing[0]}")
        if len(existing) != 3:
            raise ScanError("refusing partial project-projection replacement")
        check()
    classification, evidence, receipt = prepare()
    atomic_csv(CLASSIFICATION, CLASSIFICATION_FIELDS, classification)
    atomic_csv(EVIDENCE, EVIDENCE_FIELDS, evidence)
    receipt["output_sha256"] = {
        CLASSIFICATION.name: sha256(CLASSIFICATION), EVIDENCE.name: sha256(EVIDENCE),
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def check() -> dict[str, Any]:
    mount = source_mount()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    classification = read_csv(CLASSIFICATION)
    evidence = read_csv(EVIDENCE)
    if (
        receipt.get("result") != "ok" or receipt.get("review_projection_only") is not True
        or receipt.get("human_project_decisions_written") != 0
        or receipt.get("master_decisions_changed") is not False
        or receipt.get("metadata_decisions_changed") is not False
        or receipt.get("accession_changed") is not False
        or receipt.get("deletion_authorized") is not False
        or len(classification) != int(receipt.get("classification_row_count", -1))
        or len(evidence) != int(receipt.get("evidence_row_count", -1))
        or len({row["record_key"] for row in classification}) != len(classification)
        or any(row["human_confirmed"] != "false" or row["deletion_authorized"] != "false" for row in classification)
        or any(row["source_read_only"] != "true" or row["deletion_authorized"] != "false" for row in evidence)
        or sha256(CLASSIFICATION) != receipt["output_sha256"][CLASSIFICATION.name]
        or sha256(EVIDENCE) != receipt["output_sha256"][EVIDENCE.name]
    ):
        raise ScanError("project-provenance projection failed validation")
    return {
        "result": "ok", "classification_rows": len(classification), "evidence_rows": len(evidence),
        "source_mount": mount, "review_projection_only": True,
        "human_project_decisions_written": 0, "deletion_authorized": False,
    }


def self_test() -> None:
    assert project_for_path("WF/BRIGADEBUCH-Projekt/Metadaten/BB-01.xlsx") == "brigade-books"
    assert project_for_path("WF/Buch/Auswahlliste_Albert.xlsx") == "albert-book-selection"
    assert project_for_path("WF/Freistellen/x.tif") == ""
    assert normalize_inventory(b"5511230_13") == "TFA-5511230-13"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-projection", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if sum((args.apply, args.replace_projection, args.check, args.self_test)) != 1:
        parser.error("choose exactly one action")
    if args.self_test:
        self_test(); print("self-test: ok"); return 0
    result = apply(replace_projection=args.replace_projection) if (args.apply or args.replace_projection) else check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
