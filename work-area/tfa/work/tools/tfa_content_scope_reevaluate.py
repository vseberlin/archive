#!/usr/bin/env python3
"""Re-evaluate TFA documentation and metadata by content-supported unit scope."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


TOOL = Path(__file__).with_name("tfa_metadata_consolidation_prepare.py")
SPEC = importlib.util.spec_from_file_location("tfa_prepare_base", TOOL)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load TFA preparation helper")
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)

ROOT = base.REVIEW_ROOT
DOC_PLAN_IN = ROOT / "tfa-document-accession-plan.corrected.csv"
DOC_DUPLICATES = ROOT / "tfa-document-exact-duplicates.csv"
META_PLAN_IN = ROOT / "tfa-metadata-accession-plan.csv"
META_DISPOSITION_IN = ROOT / "tfa-metadata-source-disposition-proposal.csv"
CATALOG = Path("/mnt/volume/iss-archive/units/tfa/metadata/museum-digital-catalog.csv")

DOC_PLAN = ROOT / "tfa-document-accession-plan.scope-reviewed.csv"
DOC_DISPOSITION = ROOT / "tfa-document-source-disposition-proposal.scope-reviewed.csv"
DOC_CONTENTS = ROOT / "tfa-document-contents.scope-reviewed.md"
DOC_STAGE = ROOT / "tfa-document-accession-stage.scope-reviewed.zip"
DOC_SUMMARY = ROOT / "tfa-document-accession-preparation.scope-reviewed.json"
META_PLAN = ROOT / "tfa-metadata-accession-plan.scope-reviewed.csv"
META_DISPOSITION = ROOT / "tfa-metadata-source-disposition-proposal.scope-reviewed.csv"
META_CONTENTS = ROOT / "tfa-metadata-contents.scope-reviewed.md"
META_STAGE = ROOT / "tfa-metadata-accession-stage.scope-reviewed.zip"
META_SUMMARY = ROOT / "tfa-metadata-accession-preparation.scope-reviewed.json"

DOC_TO_METADATA = frozenset({
    "/media/vladimir/Elements/restore/archive/1958-Rest_1959-Rest_02_fehlende-Maße.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/5612982-6221889_(1957-1962)_Alex_ und_Bernd_Liebig_25.7.19.4.7.2019.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Archiv/Findbücher_1950er_02.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Vergleichstabelle_mit_ Gesamtliste_AUsstellung-mit-Zusaetzen.xlsx",
})
DOC_NOISE = frozenset({
    "/media/vladimir/Elements/restore/archive/Alle_verwendeten_TFA-FOT-XXXX-Suchliste.docx",
})
DOC_UNRESOLVED = frozenset()
DOC_INCLUDE = frozenset({
    "/media/vladimir/Elements/restore/archive/SCANS-fertig/KORREKTUREN-der-Scans.txt",
    "/media/vladimir/Elements/restore/archive/SCANS-fertig/Korrektur_2_Tranche/seitenverkehrt 2. Lieferung Industriesalon.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/1959-mussen-gescannt-werden.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Archiv/1954-Negativ-fehlt.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Antrag_2019_Industriesalon.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/2019-12-12_Digis-Abschluss.pptx",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/2019-12-16_Industriesalon.pptx",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/Dauerarchivierung/submission-manifest.txt",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/Jahreskonferenz/Poster-Entwurf.pptx",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Logo etc/Invoice_20220919.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/2019011_Industriesalon_Planfilme_geändert_190307.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Angebot-CDS_Gromke_2019-30121.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Angebot-Kulturgutscanner_2019054_Industriesalon_II.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Industriesalon-Schöneweide_3te-Tranche_Liefer-Empfangsbestaetigung_31-10-2019.doc",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Industriesalon-Schöneweide_3te-Tranche_Liefer-Empfangsbestaetigung_31-10-2019.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Industriesalon-Schöneweide_Liefer-Empfangsbestaetigung_18-7-2019.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/Liefer-Empfangsbestaetigung_17-7-2019.doc",
    "/media/vladimir/Elements/restore/archive/WF/Digis/Scanlieferschein/R19052_Industriesalon_3_Tranche.pdf",
    "/media/vladimir/Elements/restore/archive/WF/Freistellen/Für-Rosenau_seitenverkehrte2. Lieferung Industriesalon.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Freistellen/Rosenau_korrigiert/Industriesalon_unvollstaendige-Banderolen.xlsx",
    "/media/vladimir/Elements/restore/archive/WF/Thesaurus/14_neue-scans/Falsche scannummern.docx",
    "/media/vladimir/Elements/restore/archive/WF/transfer/Lightroom Export/Archiv/Bildliste-1958.txt",
    "/media/vladimir/Elements/restore/archive/WF/transfer/Lightroom Export/Archiv/Gebäude.txt",
    "/media/vladimir/Elements/restore/archive/WF/transfer/Lightroom Export/Archiv/Gebäude_Rest.txt",
})

NUMBER_RE = re.compile(r"(?<!\d)(\d{5,8}(?:[-_.]\d{1,2})?)(?!\d)")
TFA_RE = re.compile(r"(?i)\bTFA(?:-(?:FOT|NEG|KB))?[-_ ]?(\d{3,8}(?:[-_.]\d{1,2})?)")
OTHER_RE = re.compile(
    r"(?i)\b(?:WFS(?:-FB)?|KS|TRO(?:-FB)?|VLV|ALB|FEB(?:-[A-Z]+)?|BB|AEB|EBK|FVB|BIB)"
    r"[-_ ][A-Z]*[-_ ]?\d[\w.-]*"
)
DIRECT_RE = re.compile(r"(?i)\b(?:technisches fotoarchiv|fotostelle(?: des)? wf|tfa)\b")


class ScopeError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize_inventory(value: str) -> str:
    result = value.upper()
    for prefix in ("TFA-FOT-", "TFA-NEG-", "TFA-KB-", "TFA-"):
        result = result.replace(prefix, "")
    result = result.replace(".0", "")
    return re.sub(r"[_ .]+", "-", result).strip("-")


def require_outputs_absent() -> None:
    root = ROOT.resolve(strict=True)
    root.relative_to(base.WORK_ROOT.resolve(strict=True))
    for path in (DOC_PLAN, DOC_DISPOSITION, DOC_CONTENTS, DOC_STAGE, DOC_SUMMARY,
                 META_PLAN, META_DISPOSITION, META_CONTENTS, META_STAGE, META_SUMMARY):
        if path.parent.resolve(strict=True) != root:
            raise ScopeError(f"output escapes work root: {path}")
        if path.exists():
            raise ScopeError(f"refusing to overwrite scope-reviewed output: {path}")


def disposition_for(row: dict[str, str], status: str, basis: str) -> dict[str, str]:
    selected = status == "proposed_accession"
    noise = row["classification"] == "technical_noise"
    return base.source_disposition(
        row,
        role=row["classification"],
        final=row["final_relative_path"] if selected else "",
        status="proposed_redundant_noise" if noise else status,
        basis=basis,
        exact=False,
        drain="proposed_redundant" if noise else (
            "pending_accession_confirmation" if selected else (
            "pending_human_confirmation" if status == "unresolved" else "outside_tfa_document_scope"
            )
        ),
    )


def document_evaluation() -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, object], str]:
    original = read_csv(DOC_PLAN_IN)
    if len(original) != 63:
        raise ScopeError(f"document input scope drifted: {len(original)}/63")
    all_paths = {row["source_path"] for row in original}
    if (not DOC_INCLUDE <= all_paths or not DOC_UNRESOLVED <= all_paths
            or not DOC_NOISE <= all_paths or not DOC_TO_METADATA <= all_paths):
        raise ScopeError("curated document scope paths no longer bind to the plan")
    plan: list[dict[str, str]] = []
    moved_ids: set[str] = set()
    for row in original:
        path = row["source_path"]
        item = dict(row)
        if path in DOC_TO_METADATA:
            moved_ids.add(row["document_id"])
            continue
        if path in DOC_INCLUDE:
            item["proposed_decision"] = "proposed_accession"
            item["evidence_confidence"] = "strong"
            item["condition"] = "content_verified_tfa_document"
            item["selection_basis"] = (
                "Full-content review confirms a direct TFA digitization, scanning, correction, "
                "preservation, rights, delivery, or project-administration function"
            )
        elif path in DOC_NOISE:
            item["proposed_decision"] = "proposed_exclusion"
            item["evidence_confidence"] = "strong"
            item["classification"] = "technical_noise"
            item["final_relative_path"] = ""
            item["condition"] = "user_classified_noise"
            item["selection_basis"] = (
                "User classified this file as noise; propose redundant/drain disposition only; "
                "deletion remains unauthorized"
            )
        elif path in DOC_UNRESOLVED:
            item["proposed_decision"] = "unresolved"
            item["evidence_confidence"] = "ambiguous"
            item["classification"] = "unresolved_documentary_provenance"
            item["final_relative_path"] = ""
            item["condition"] = "tfa_file_list_without_identified_project_function"
            item["selection_basis"] = (
                "Contains TFA filenames but the creating project or documentary purpose is not identified"
            )
        else:
            item["proposed_decision"] = "proposed_exclusion"
            item["evidence_confidence"] = "strong"
            item["classification"] = "general_or_cross_project_reference"
            item["final_relative_path"] = ""
            item["condition"] = "content_reviewed_outside_tfa_document_scope"
            item["selection_basis"] = (
                "Full-content review identifies general WF history, institution-wide inventory, "
                "another project, generic reference material, or non-document object content"
            )
        item["deletion_authorized"] = "false"
        plan.append(item)
    if len(plan) != 59:
        raise ScopeError(f"document evaluated scope drifted: {len(plan)}/59")
    by_id = {row["document_id"]: row for row in plan}
    disposition: list[dict[str, str]] = []
    for row in plan:
        disposition.append(disposition_for(row, row["proposed_decision"], row["selection_basis"]))
    for duplicate in read_csv(DOC_DUPLICATES):
        representative = by_id.get(duplicate["representative_document_id"])
        if representative is None:
            continue
        status = representative["proposed_decision"]
        duplicate_row = {
            "source_path": duplicate["duplicate_source_path"],
            "source_sha256": duplicate["content_sha256"],
            "size_bytes": str(Path(duplicate["duplicate_source_path"]).stat().st_size),
        }
        if status == "proposed_accession":
            duplicate_status = "proposed_redundant_exact_copy"
            final = representative["final_relative_path"]
            drain = "pending_human_confirmation"
        elif status == "unresolved":
            duplicate_status = "unresolved_exact_copy"
            final = ""
            drain = "pending_human_confirmation"
        else:
            duplicate_status = "excluded_exact_copy_outside_tfa_document_scope"
            final = ""
            drain = "outside_tfa_document_scope"
        disposition.append(base.source_disposition(
            duplicate_row, role="exact_document_copy", final=final, status=duplicate_status,
            basis="SHA-256 equals the scope-reviewed representative; deletion is not authorized",
            exact=True, drain=drain,
        ))
    disposition.sort(key=lambda row: row["source_path"])
    if len({row["source_path"] for row in disposition}) != len(disposition):
        raise ScopeError("document disposition source paths are not unique")
    counts = Counter(row["proposed_decision"] for row in plan)
    summary = {
        "format": "iss-tfa-documentation-content-scope-review-v1", "created_at": now(),
        "status": "ready_for_human_confirmation", "admission_rule": "content_supported_tfa_documentary_function",
        "input_plan_sha256": base.digest(DOC_PLAN_IN), "input_scope": len(original),
        "reclassified_to_metadata": len(moved_ids), "scope_counts": dict(sorted(counts.items())),
        "document_review_scope": counts["unresolved"], "source_disposition_rows": len(disposition),
        "source_read_only": True, "source_modified": False, "published_tfa_unit_changed": False,
        "final_hashes_generated": False, "deletion_authorized": False, "stage_path": str(DOC_STAGE),
    }
    contents = "\n".join((
        "# TFA documentation content-scope review", "",
        "Admission requires a content-supported TFA-specific documentary function. A WF path, an isolated TFA token,",
        "a general institutional inventory, or a reference to TFA photographs is insufficient.", "",
        f"- Proposed TFA documents: {counts['proposed_accession']}",
        f"- Excluded general or cross-project documents: {counts['proposed_exclusion']}",
        f"- Reclassified object-level workbooks to metadata: {len(moved_ids)}",
        f"- Unresolved documentation: {counts['unresolved']}",
        "- No deletion is authorized and no final accession hashes were generated.", "",
    ))
    return plan, disposition, summary, contents


def current_inventories() -> set[str]:
    with CATALOG.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 5282 or any(row["metadata_reconciled"] != "true" for row in rows):
        raise ScopeError("authoritative TFA catalogue is not the reconciled 5,282-record scope")
    return {normalize_inventory(row["inventory"]) for row in rows}


def empty_evidence() -> dict[str, object]:
    return {"tfa": set(), "explicit": set(), "other": set(), "direct": 0, "text_chars": 0}


def merge_text(evidence: dict[str, object], text: str, inventories: set[str]) -> None:
    if not text:
        return
    evidence["text_chars"] = int(evidence["text_chars"]) + len(text)
    explicit = {normalize_inventory(value) for value in TFA_RE.findall(text)} & inventories
    bare = {normalize_inventory(value) for value in NUMBER_RE.findall(text)} & inventories
    evidence["explicit"].update(explicit)
    evidence["tfa"].update(explicit | bare)
    evidence["other"].update(match.group(0).upper() for match in OTHER_RE.finditer(text))
    evidence["direct"] = int(evidence["direct"]) + len(DIRECT_RE.findall(text))


def merge_zip(evidence: dict[str, object], path: Path, inventories: set[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        if bad := archive.testzip():
            raise ScopeError(f"working metadata ZIP CRC failure: {path}: {bad}")
        for info in archive.infolist():
            if info.is_dir() or Path(info.filename).suffix.casefold() not in {".csv", ".xml", ".txt"}:
                continue
            merge_text(evidence, archive.read(info).decode("utf-8", errors="replace"), inventories)


def indexed_by_paths(connection: sqlite3.Connection, paths: list[str]) -> dict[str, dict[str, object]]:
    return base.indexed_rows(connection, sorted(set(paths)))


def add_metadata_row(indexed: dict[str, object]) -> dict[str, str]:
    base.validate_indexed(indexed)
    path = str(indexed["source_path"])
    return {
        "metadata_id": base.metadata_id(path), "source_path": path, "filename": str(indexed["filename"]),
        "extension": str(indexed["extension"]).casefold(), "size_bytes": str(indexed["size_bytes"]),
        "source_sha256": str(indexed["content_sha256"]), "category": "tfa_working_metadata",
        "evidence_confidence": "ambiguous", "extraction_state": str(indexed["extraction_state"]),
        "parser": str(indexed["parser"]), "matched_tfa_inventory_ids": "",
        "evidence_basis": "pending full-content TFA scope review", "evidence_excerpt": "",
        "proposed_decision": "unresolved", "final_relative_path": "", "condition": "pending_scope_review",
        "selection_basis": "pending full-content TFA scope review", "deletion_authorized": "false",
    }


def scope_metadata_row(row: dict[str, str], evidence: dict[str, object]) -> dict[str, str]:
    item = dict(row)
    tfa = set(evidence["tfa"]); explicit = set(evidence["explicit"]); other = set(evidence["other"])
    direct = int(evidence["direct"]); chars = int(evidence["text_chars"])
    denominator = len(tfa) + len(other)
    tfa_ratio = len(tfa) / denominator if denominator else 0.0
    extension = row["extension"].casefold()
    if (
        (extension == ".xml" and len(tfa) >= 1)
        or (len(tfa) >= 5 and (not other or tfa_ratio >= 0.98))
        or (len(tfa) >= 1 and len(explicit) >= 1 and not other)
    ):
        decision = "proposed_accession"
        confidence = "strong"
        condition = "content_verified_tfa_metadata" if not other else "tfa_primary_mixed_scope_source"
        basis = (
            f"Content matches {len(tfa)} authoritative TFA inventories; "
            f"other-unit identifiers={len(other)}; TFA share={tfa_ratio:.4f}"
        )
        final = "metadata/tfa-working-metadata.zip"
    elif len(tfa) or direct:
        decision = "unresolved"
        confidence = "ambiguous"
        condition = "mixed_or_weak_tfa_scope_evidence"
        basis = (
            f"Human review required: authoritative TFA inventories={len(tfa)}, explicit TFA IDs={len(explicit)}, "
            f"other-unit identifiers={len(other)}, TFA subject hits={direct}"
        )
        final = ""
    elif chars == 0 or row["extraction_state"] == "error":
        decision = "unresolved"
        confidence = "ambiguous"
        condition = "content_unavailable_for_scope_decision"
        basis = "Human review required because indexed content is unavailable or extraction failed"
        final = ""
    elif len(other) >= 5:
        decision = "proposed_exclusion"
        confidence = "strong"
        condition = "other_unit_metadata_without_tfa_scope"
        basis = (
            f"Readable content has no authoritative TFA inventory or TFA subject evidence and contains "
            f"{len(other)} other-unit identifiers"
        )
        final = ""
    else:
        decision = "unresolved"
        confidence = "ambiguous"
        condition = "plausible_supporting_metadata_without_direct_tfa_binding"
        basis = (
            f"Human review required: readable metadata may support TFA description but has no direct TFA binding; "
            f"other-unit identifiers={len(other)}"
        )
        final = ""
    item.update({
        "evidence_confidence": confidence,
        "matched_tfa_inventory_ids": ";".join("TFA-" + value for value in sorted(tfa)[:100]),
        "evidence_basis": basis, "proposed_decision": decision, "final_relative_path": final,
        "condition": condition, "selection_basis": basis + "; content-scope review",
        "deletion_authorized": "false",
    })
    return item


def metadata_evaluation(connection: sqlite3.Connection) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, object], str]:
    inventories = current_inventories()
    original_plan = read_csv(META_PLAN_IN)
    original_disposition = read_csv(META_DISPOSITION_IN)
    extra_index = indexed_by_paths(connection, list(DOC_TO_METADATA))
    if len(extra_index) != len(DOC_TO_METADATA):
        raise ScopeError("a document-to-metadata source is missing from the completed index")
    plan_by_hash = {row["source_sha256"]: dict(row) for row in original_plan}
    extra_ids: dict[str, str] = {}
    for path in sorted(DOC_TO_METADATA):
        indexed = extra_index[path]
        extra = add_metadata_row(indexed)
        if extra["source_sha256"] in plan_by_hash:
            raise ScopeError(f"reclassified metadata unexpectedly duplicates an existing representative: {path}")
        plan_by_hash[extra["source_sha256"]] = extra
        extra_ids[extra["metadata_id"]] = extra["source_sha256"]
    working_hashes = {
        digest for digest, row in plan_by_hash.items() if row["category"] == "tfa_working_metadata"
    }
    group_paths: dict[str, set[str]] = defaultdict(set)
    for row in original_disposition:
        digest_value = row["source_sha256"].removeprefix("sha256:")
        if digest_value in working_hashes:
            group_paths[digest_value].add(row["source_path"])
    for path, indexed in extra_index.items():
        group_paths[str(indexed["content_sha256"])].add(path)
    doc_plan_original = {row["document_id"]: row for row in read_csv(DOC_PLAN_IN)}
    for duplicate in read_csv(DOC_DUPLICATES):
        representative = doc_plan_original.get(duplicate["representative_document_id"])
        if representative and representative["source_path"] in DOC_TO_METADATA:
            group_paths[duplicate["content_sha256"]].add(duplicate["duplicate_source_path"])
    all_group_paths = sorted({path for paths in group_paths.values() for path in paths})
    indexed = indexed_by_paths(connection, all_group_paths)
    if len(indexed) != len(all_group_paths):
        raise ScopeError("one or more metadata exact-copy paths are absent from the index")
    evidence_by_hash = {value: empty_evidence() for value in working_hashes}
    for digest_value, paths in group_paths.items():
        evidence = evidence_by_hash[digest_value]
        for path in sorted(paths):
            row = indexed[path]
            merge_text(evidence, str(row["header_text"]), inventories)
        representative = plan_by_hash[digest_value]
        if representative["extension"].casefold() == ".zip":
            merge_zip(evidence, Path(representative["source_path"]), inventories)
    plan: list[dict[str, str]] = []
    for digest_value, row in plan_by_hash.items():
        if row["category"] == "tfa_working_metadata":
            plan.append(scope_metadata_row(row, evidence_by_hash[digest_value]))
        else:
            fixed = dict(row)
            fixed["proposed_decision"] = "proposed_accession"
            fixed["evidence_confidence"] = "strong"
            fixed["selection_basis"] = (
                "Complete current collection 857 capture" if row["category"] == "museum_digital_current"
                else "Historical museum-digital collection export with verified TFA collection provenance"
            )
            plan.append(fixed)
    plan.sort(key=lambda row: (row["category"], row["proposed_decision"], row["source_path"]))
    decision_by_hash = {row["source_sha256"]: row for row in plan}
    disposition_by_path = {row["source_path"]: dict(row) for row in original_disposition}
    for path, row in indexed.items():
        digest_value = str(row["content_sha256"])
        if digest_value not in working_hashes or path in disposition_by_path:
            continue
        disposition_by_path[path] = {
            "source_path": path, "source_kind": "file", "unit": "tfa", "inventory": "",
            "source_role": "exact_working_metadata_copy", "final_path": "", "preservation_status": "unresolved",
            "preservation_basis": "Exact-copy group newly reclassified from documentation to metadata",
            "source_sha256": "sha256:" + digest_value, "final_sha256": "",
            "source_size_bytes": str(row["size_bytes"]), "exact_copy_proven": "true",
            "drain_state": "pending_human_confirmation", "deletion_authorized": "false",
            "notes": "Scope review only; source remains unchanged.",
        }
    representatives = {row["source_path"]: row for row in plan if row["category"] == "tfa_working_metadata"}
    for path, disposition in disposition_by_path.items():
        digest_value = disposition["source_sha256"].removeprefix("sha256:")
        representative = decision_by_hash.get(digest_value)
        if representative is None or representative["category"] != "tfa_working_metadata":
            continue
        decision = representative["proposed_decision"]
        is_representative = path == representative["source_path"]
        if decision == "proposed_accession":
            disposition["preservation_status"] = "proposed_accession" if is_representative else "proposed_redundant_exact_copy"
            disposition["final_path"] = "metadata/tfa-working-metadata.zip"
            disposition["drain_state"] = "pending_accession_confirmation" if is_representative else "pending_human_confirmation"
        elif decision == "proposed_exclusion":
            disposition["preservation_status"] = "proposed_exclusion_no_tfa_scope" if is_representative else "excluded_exact_copy_no_tfa_scope"
            disposition["final_path"] = ""
            disposition["drain_state"] = "outside_tfa_metadata_scope"
        else:
            disposition["preservation_status"] = "unresolved" if is_representative else "unresolved_exact_copy"
            disposition["final_path"] = ""
            disposition["drain_state"] = "pending_human_confirmation"
        disposition["source_role"] = "tfa_working_metadata" if is_representative else "exact_working_metadata_copy"
        disposition["preservation_basis"] = representative["selection_basis"]
        disposition["exact_copy_proven"] = str(not is_representative).lower()
        disposition["deletion_authorized"] = "false"
    disposition = sorted(disposition_by_path.values(), key=lambda row: row["source_path"])
    if len({row["source_path"] for row in disposition}) != len(disposition):
        raise ScopeError("metadata disposition source paths are not unique")
    counts = Counter(row["proposed_decision"] for row in plan)
    working_counts = Counter(
        row["proposed_decision"] for row in plan if row["category"] == "tfa_working_metadata"
    )
    summary = {
        "format": "iss-tfa-metadata-content-scope-review-v1", "created_at": now(),
        "status": "ready_for_human_confirmation", "authority": "museum-digital current collection 857 capture",
        "admission_rule": "content_supported_tfa_metadata_scope", "input_plan_sha256": base.digest(META_PLAN_IN),
        "plan_rows": len(plan), "scope_counts": dict(sorted(counts.items())),
        "working_metadata_scope_counts": dict(sorted(working_counts.items())),
        "metadata_review_scope": counts["unresolved"], "source_disposition_rows": len(disposition),
        "source_read_only": True, "source_modified": False, "published_tfa_unit_changed": False,
        "final_hashes_generated": False, "deletion_authorized": False, "stage_path": str(META_STAGE),
    }
    contents = "\n".join((
        "# TFA metadata content-scope review", "",
        "The current 5,282-record museum-digital collection capture remains authoritative. Historical exports retain",
        "collection provenance. Working metadata is admitted only through authoritative TFA inventory matches and",
        "content evidence; directory location alone is insufficient.", "",
        f"- Metadata plan rows: {len(plan)}",
        f"- Proposed accession rows: {counts['proposed_accession']}",
        f"- Proposed exclusions: {counts['proposed_exclusion']}",
        f"- Unresolved metadata: {counts['unresolved']}",
        f"- Working metadata decisions: {dict(sorted(working_counts.items()))}",
        "- Exact-copy relationships remain non-deletion evidence.",
        "- No final accession hashes were generated.", "",
    ))
    return plan, disposition, summary, contents


def build_document_stage(plan: list[dict[str, str]], disposition: list[dict[str, str]], summary: dict[str, object], contents: str) -> None:
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        grouped[row["classification"]].append(row)
    temporary: dict[str, Path] = {}
    stage_temp = DOC_STAGE.with_suffix(".zip.tmp")
    try:
        for classification, rows in grouped.items():
            if classification == "project_final":
                continue
            handle = tempfile.NamedTemporaryFile(prefix=".tfa-doc-scope-", suffix=".zip", dir=ROOT, delete=False)
            handle.close(); package = Path(handle.name); temporary[classification] = package
            base.build_simple_inner(package, rows, "document_id", f"TFA {classification}")
        with zipfile.ZipFile(stage_temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for row in grouped.get("project_final", []):
                archive.write(row["source_path"], row["final_relative_path"])
            for classification, package in temporary.items():
                final = "documents/historical/drafts-and-superseded.zip" if classification == "historical" else (
                    "documents/restricted/" + classification.removeprefix("restricted_") + ".zip"
                )
                archive.write(package, final)
            archive.writestr("documents/CONTENTS.md", contents.encode())
            archive.writestr("db-restore/manifests/documents.csv", base.csv_bytes(plan, base.DOC_FIELDS))
            archive.writestr("db-restore/manifests/source-disposition.csv", base.csv_bytes(disposition, base.DISPOSITION_FIELDS))
            archive.writestr("PREPARATION.json", json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        with zipfile.ZipFile(stage_temp) as archive:
            if bad := archive.testzip(): raise ScopeError(f"document stage CRC failure: {bad}")
        stage_temp.replace(DOC_STAGE)
    finally:
        for package in temporary.values(): package.unlink(missing_ok=True)
        stage_temp.unlink(missing_ok=True)


def build_metadata_stage(plan: list[dict[str, str]], disposition: list[dict[str, str]], summary: dict[str, object], contents: str) -> None:
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    current = [row for row in selected if row["category"] == "museum_digital_current"]
    historical = [row for row in selected if row["category"] in {"historical_md_export_archive", "historical_lido_snapshot"}]
    working = [row for row in selected if row["category"] == "tfa_working_metadata"]
    packages: list[Path] = []
    stage_temp = META_STAGE.with_suffix(".zip.tmp")
    try:
        for rows, title in ((current, "Current museum-digital collection 857 capture"),
                            (historical, "Historical museum-digital TFA exports"),
                            (working, "Content-verified TFA working metadata")):
            handle = tempfile.NamedTemporaryFile(prefix=".tfa-meta-scope-", suffix=".zip", dir=ROOT, delete=False)
            handle.close(); package = Path(handle.name); packages.append(package)
            base.build_simple_inner(package, rows, "metadata_id", title)
        with zipfile.ZipFile(stage_temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(packages[0], "metadata/museum-digital-current.zip")
            archive.write(packages[1], "metadata/museum-digital-historical-exports.zip")
            archive.write(packages[2], "metadata/tfa-working-metadata.zip")
            archive.writestr("metadata/METADATA-CONTENTS.md", contents.encode())
            archive.writestr("db-restore/manifests/metadata-files.csv", base.csv_bytes(plan, base.META_FIELDS))
            archive.writestr("db-restore/manifests/metadata-source-disposition.csv", base.csv_bytes(disposition, base.DISPOSITION_FIELDS))
            archive.writestr("METADATA-PREPARATION.json", json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        with zipfile.ZipFile(stage_temp) as archive:
            if bad := archive.testzip(): raise ScopeError(f"metadata stage CRC failure: {bad}")
        stage_temp.replace(META_STAGE)
    finally:
        for package in packages: package.unlink(missing_ok=True)
        stage_temp.unlink(missing_ok=True)


def main() -> int:
    require_outputs_absent()
    mount = base.mount_state()
    connection = base.index_connection()
    try:
        doc_plan, doc_disposition, doc_summary, doc_contents = document_evaluation()
        meta_plan, meta_disposition, meta_summary, meta_contents = metadata_evaluation(connection)
    finally:
        connection.close()
    doc_summary["source_mount"] = mount; meta_summary["source_mount"] = mount
    if any(row["deletion_authorized"] != "false" for row in doc_plan + doc_disposition + meta_plan + meta_disposition):
        raise ScopeError("scope review cannot authorize deletion")
    build_document_stage(doc_plan, doc_disposition, doc_summary, doc_contents)
    build_metadata_stage(meta_plan, meta_disposition, meta_summary, meta_contents)
    doc_summary["stage_size_bytes"] = DOC_STAGE.stat().st_size
    meta_summary["stage_size_bytes"] = META_STAGE.stat().st_size
    base.atomic_write(DOC_PLAN, base.csv_bytes(doc_plan, base.DOC_FIELDS))
    base.atomic_write(DOC_DISPOSITION, base.csv_bytes(doc_disposition, base.DISPOSITION_FIELDS))
    base.atomic_write(DOC_CONTENTS, doc_contents.encode())
    base.atomic_write(DOC_SUMMARY, json.dumps(doc_summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
    base.atomic_write(META_PLAN, base.csv_bytes(meta_plan, base.META_FIELDS))
    base.atomic_write(META_DISPOSITION, base.csv_bytes(meta_disposition, base.DISPOSITION_FIELDS))
    base.atomic_write(META_CONTENTS, meta_contents.encode())
    base.atomic_write(META_SUMMARY, json.dumps(meta_summary, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
    print(json.dumps({"documents": doc_summary, "metadata": meta_summary}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
