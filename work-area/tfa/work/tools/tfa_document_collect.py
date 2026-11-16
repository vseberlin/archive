#!/usr/bin/env python3
"""Collect evidence-only TFA documentation candidates from the shared index."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work")
OUTPUT_ROOT = WORK_ROOT / "reconciliation/unit-reconciliation"
DATABASE = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
TFA_CATALOG = Path("/mnt/volume/iss-archive/units/tfa/CATALOG.csv")
SOURCE_ROOT = Path("/media/vladimir/Elements")
CANDIDATES = OUTPUT_ROOT / "tfa-document-candidates.csv"
DUPLICATES = OUTPUT_ROOT / "tfa-document-exact-duplicates.csv"
EXCLUSIONS = OUTPUT_ROOT / "tfa-document-exclusions.csv"
SUMMARY = OUTPUT_ROOT / "tfa-document-collection-summary.json"

DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".md", ".ods", ".odt", ".pdf", ".ppt",
    ".pptx", ".rtf", ".tsv", ".txt", ".xls", ".xlsm", ".xlsx", ".zip",
}
METADATA_EXTENSIONS = {".json", ".jsonl", ".xml", ".xmp"}
FTS_QUERIES = (
    "TFA",
    '"Technisches Fotoarchiv"',
    '"Archiv der Bildstelle"',
    '"Bildstelle des WF"',
    '"Fotostelle WF"',
    "relative_path:Thesaurus",
    "relative_path:TFA*",
)
TECHNICAL_PATH_MARKERS = (
    "/$recycle.bin/", "/program files/", "/program files (x86)/",
    "/windows/", "/appdata/", "/__macosx/", "/wp-content/",
)
COMPLETED_PATH_MARKERS = (
    "/wf-sender projekt/", "/produktion-propaganda-wf-sender/",
    "/namenssuche+wf-sender/",
    "/kurt-schwarz/", "/kurt schwarz/", "/trafo/",
)
DOCUMENT_NAME_MARKERS = (
    "bericht", "brief", "correspond", "dokument", "findbuch", "inventar",
    "korrektur", "liste", "manifest", "metadaten", "projekt", "rechnung",
    "scan", "schlagwort", "submission", "thesaurus", "upload",
)
SPECIFIC_PHRASES = (
    "technisches fotoarchiv", "archiv der bildstelle", "bildstelle des wf",
    "fotostelle wf", "tfa bilder", "tfa-bilder",
)
TFA_ID_PATTERN = re.compile(
    r"\[?TFA\]?(?:-(?:FOT|NEG|KB))?-(\d+(?:[.-]\d+)?)",
    re.IGNORECASE,
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Elements mount cannot be resolved")
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise RuntimeError("Elements mount resolution is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise RuntimeError(f"Elements is not protected ntfs3 read-only: {mount}")
    return {
        "source": str(mount.get("source", "")),
        "target": str(mount.get("target", "")),
        "fstype": str(mount.get("fstype", "")),
        "options": str(mount.get("options", "")),
    }


def require_boundaries() -> None:
    root = WORK_ROOT.resolve(strict=True)
    output = OUTPUT_ROOT.resolve(strict=True)
    output.relative_to(root)
    for path in (CANDIDATES, DUPLICATES, EXCLUSIONS, SUMMARY):
        if path.parent.resolve(strict=True) != output:
            raise RuntimeError(f"output escapes approved TFA work root: {path}")


def load_inventory_ids() -> set[str]:
    values: set[str] = set()
    with TFA_CATALOG.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            inventory = str(row.get("inventory", "")).strip().upper()
            if inventory:
                values.add(inventory)
    return values


def batched(values: list[str], size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def matched_ids(
    connection: sqlite3.Connection,
) -> tuple[set[str], dict[str, set[str]], dict[str, list[str]]]:
    matches: set[str] = set()
    bases: dict[str, set[str]] = defaultdict(set)
    snippets: dict[str, list[str]] = defaultdict(list)
    for query in FTS_QUERIES:
        for row in connection.execute(
            "SELECT document_id,'' FROM document_fts WHERE document_fts MATCH ?",
            (query,),
        ):
            document_id = row[0]
            matches.add(document_id)
            bases[document_id].add(query)
            snippet = re.sub(r"\s+", " ", str(row[1] or "")).strip()
            if snippet and snippet not in snippets[document_id]:
                snippets[document_id].append(snippet)
    return matches, bases, snippets


def load_documents(connection: sqlite3.Connection, ids: set[str]) -> dict[str, sqlite3.Row]:
    result: dict[str, sqlite3.Row] = {}
    ordered = sorted(ids)
    for batch in batched(ordered):
        id_placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"""
            SELECT document_id,source_path,filename,extension,size_bytes,
                   content_sha256,observed_state,extraction_state,parser
            FROM documents WHERE document_id IN ({id_placeholders})
            """,
            batch,
        ):
            result[row["document_id"]] = row
    return result


def load_unit_links(connection: sqlite3.Connection, ids: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    ordered = sorted(ids)
    for batch in batched(ordered):
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"SELECT document_id,unit,relation_state FROM unit_links WHERE document_id IN ({placeholders})",
            batch,
        ):
            result[row["document_id"]].append(f"{row['unit']}:{row['relation_state']}")
    return result


def known_inventory_ids(text: str, inventory_ids: set[str]) -> list[str]:
    values: set[str] = set()
    for match in TFA_ID_PATTERN.finditer(text):
        normalized = "TFA-" + match.group(1).replace(".", "-")
        if normalized in inventory_ids:
            values.add(normalized)
    return sorted(values)


def evidence(
    row: sqlite3.Row,
    query_bases: set[str],
    inventory_ids: set[str],
    snippets: list[str],
) -> dict[str, object]:
    path = row["source_path"]
    path_lower = path.casefold()
    filename_lower = row["filename"].casefold()
    text = " … ".join(snippets)
    text_lower = text.casefold()
    known_ids = known_inventory_ids(text, inventory_ids)
    phrases = [phrase for phrase in SPECIFIC_PHRASES if phrase in text_lower]
    tfa_hits = len(re.findall(r"\btfa\b", text_lower))
    direct_tfa_match = "TFA" in query_bases
    phrase_query_matches = [
        query.strip('"').casefold() for query in query_bases
        if query.startswith('"')
    ]
    in_thesaurus = "/wf/thesaurus/" in path_lower
    in_tfa_export = "tfa_export" in path_lower or "/tfa-" in path_lower
    name_markers = [marker for marker in DOCUMENT_NAME_MARKERS if marker in filename_lower]
    score = 0
    if known_ids:
        score += 5
    if phrases:
        score += 3
    if phrase_query_matches:
        score += 4
    if in_tfa_export:
        score += 3
    if in_thesaurus:
        score += 3
    if tfa_hits >= 3:
        score += 2
    elif tfa_hits:
        score += 1
    if direct_tfa_match:
        score += 3
    if name_markers:
        score += 2
    if row["extension"] in DOCUMENT_EXTENSIONS:
        score += 1
    evidence_parts = []
    if known_ids:
        evidence_parts.append(f"catalogue_inventory_ids={len(known_ids)}")
    if phrases:
        evidence_parts.append("phrases=" + ";".join(phrases))
    if phrase_query_matches:
        evidence_parts.append("fts_phrase_matches=" + ";".join(sorted(phrase_query_matches)))
    if in_thesaurus:
        evidence_parts.append("known_tfa_working_corpus=WF/Thesaurus")
    if in_tfa_export:
        evidence_parts.append("tfa_path_marker")
    if tfa_hits:
        evidence_parts.append(f"tfa_text_hits={tfa_hits}")
    if direct_tfa_match:
        evidence_parts.append("fts_content_match=TFA")
    if name_markers:
        evidence_parts.append("document_name_markers=" + ";".join(name_markers))
    evidence_parts.append("fts=" + ";".join(sorted(query_bases)))
    excerpt = re.sub(r"\s+", " ", text).strip()[:500]
    return {
        "score": score,
        "confidence": "strong" if score >= 5 else "ambiguous",
        "known_ids": known_ids,
        "basis": " | ".join(evidence_parts),
        "excerpt": excerpt,
    }


def exclusion_reason(
    row: sqlite3.Row,
    unit_links: list[str],
    query_bases: set[str],
) -> str:
    path_lower = row["source_path"].casefold()
    filename_lower = row["filename"].casefold()
    if row["observed_state"] != "present":
        return "not_currently_observed_on_elements"
    if unit_links:
        return "completed_unit_document_link:" + ";".join(sorted(unit_links))
    if row["extraction_state"] == "skipped_known_unit":
        return "exact_or_path-resolved_completed_unit_document"
    if row["extension"] in METADATA_EXTENSIONS:
        return "object_metadata_or_sidecar_not_project_documentation"
    if any(marker in path_lower for marker in TECHNICAL_PATH_MARKERS):
        return "technical_or_software_noise"
    if any(marker in path_lower for marker in COMPLETED_PATH_MARKERS):
        return "completed_unit_path_scope"
    if (
        row["parser"] == "binary-strings"
        and query_bases == {"TFA"}
        and "/restore/archive/wf/" not in path_lower
        and "tfa" not in filename_lower
    ):
        return "weak_binary_string_reference_outside_tfa_corpus"
    if row["extension"] not in DOCUMENT_EXTENSIONS:
        return "unsupported_documentation_format"
    return ""


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> int:
    require_boundaries()
    mount = source_mount()
    inventory_ids = load_inventory_ids()
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    extraction_status = connection.execute(
        "SELECT value FROM meta WHERE key='extraction_status'"
    ).fetchone()
    document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    fts_count = connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0]
    if foreign or extraction_status is None or extraction_status[0] != "complete" or document_count != fts_count:
        raise RuntimeError(
            "document index validation failed: "
            f"foreign={len(foreign)}, extraction_status={extraction_status}, "
            f"documents={document_count}, fts={fts_count}"
        )
    ids, query_bases, snippets = matched_ids(connection)
    documents = load_documents(connection, ids)
    unit_links = load_unit_links(connection, ids)
    connection.close()

    included: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for document_id, row in documents.items():
        proof = evidence(row, query_bases[document_id], inventory_ids, snippets.get(document_id, []))
        reason = exclusion_reason(row, unit_links.get(document_id, []), query_bases[document_id])
        common = {
            "document_id": document_id,
            "source_path": row["source_path"],
            "filename": row["filename"],
            "extension": row["extension"],
            "size_bytes": row["size_bytes"],
            "content_sha256": row["content_sha256"],
            "observed_state": row["observed_state"],
            "extraction_state": row["extraction_state"],
            "parser": row["parser"],
            "evidence_score": proof["score"],
            "evidence_confidence": proof["confidence"],
            "matched_tfa_inventory_ids": ";".join(proof["known_ids"][:100]),
            "evidence_basis": proof["basis"],
            "evidence_excerpt": proof["excerpt"],
            "deletion_authorized": "false",
        }
        if reason:
            excluded.append({**common, "exclusion_reason": reason})
        else:
            included.append(common)

    by_hash: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in included:
        key = str(row["content_sha256"]) or "path:" + str(row["source_path"])
        by_hash[key].append(row)

    candidate_rows: list[dict[str, object]] = []
    duplicate_rows: list[dict[str, object]] = []
    for group in by_hash.values():
        group.sort(
            key=lambda row: (
                -int(row["evidence_score"]),
                0 if row["evidence_confidence"] == "strong" else 1,
                len(str(row["source_path"])),
                str(row["source_path"]),
            )
        )
        representative = group[0]
        candidate_rows.append({
            **representative,
            "candidate_role": "unique" if len(group) == 1 else "representative_of_exact_copies",
            "exact_copy_count": len(group) - 1,
            "proposed_action": "review_for_tfa_documentation",
        })
        for duplicate in group[1:]:
            duplicate_rows.append({
                "content_sha256": duplicate["content_sha256"],
                "representative_document_id": representative["document_id"],
                "representative_source_path": representative["source_path"],
                "duplicate_document_id": duplicate["document_id"],
                "duplicate_source_path": duplicate["source_path"],
                "disposition_suggestion": "exact_copy_redundant_after_human_confirmation",
                "deletion_authorized": "false",
            })

    candidate_rows.sort(key=lambda row: (-int(row["evidence_score"]), str(row["source_path"])))
    duplicate_rows.sort(key=lambda row: (str(row["representative_source_path"]), str(row["duplicate_source_path"])))
    excluded.sort(key=lambda row: (str(row["exclusion_reason"]), str(row["source_path"])))

    candidate_fields = (
        "document_id", "source_path", "filename", "extension", "size_bytes",
        "content_sha256", "observed_state", "extraction_state", "parser", "evidence_score",
        "evidence_confidence", "matched_tfa_inventory_ids", "evidence_basis",
        "evidence_excerpt", "candidate_role", "exact_copy_count", "proposed_action",
        "deletion_authorized",
    )
    duplicate_fields = (
        "content_sha256", "representative_document_id", "representative_source_path",
        "duplicate_document_id", "duplicate_source_path", "disposition_suggestion",
        "deletion_authorized",
    )
    exclusion_fields = (
        "document_id", "source_path", "filename", "extension", "size_bytes",
        "content_sha256", "observed_state", "extraction_state", "parser", "evidence_score",
        "evidence_confidence", "matched_tfa_inventory_ids", "evidence_basis",
        "evidence_excerpt", "deletion_authorized", "exclusion_reason",
    )
    write_csv(CANDIDATES, candidate_fields, candidate_rows)
    write_csv(DUPLICATES, duplicate_fields, duplicate_rows)
    write_csv(EXCLUSIONS, exclusion_fields, excluded)

    confidence = Counter(str(row["evidence_confidence"]) for row in candidate_rows)
    exclusions = Counter(str(row["exclusion_reason"]).split(":", 1)[0] for row in excluded)
    summary = {
        "format": "iss-tfa-document-candidate-collection-v1",
        "generated_at": now(),
        "result": "ok",
        "scope": "evidence-only TFA project documentation review; no accession decision",
        "source_index": str(DATABASE),
        "source_index_validation": {
            "extraction_status": extraction_status[0],
            "documents": document_count,
            "fts_rows": fts_count,
            "foreign_key_violations": len(foreign),
        },
        "tfa_catalog": str(TFA_CATALOG),
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "deletion_authorized": False,
        "matched_index_documents": len(documents),
        "candidate_source_paths_before_exact_deduplication": len(included),
        "candidate_documents_for_review": len(candidate_rows),
        "candidate_confidence": dict(sorted(confidence.items())),
        "exact_copy_suggestions": len(duplicate_rows),
        "excluded_matches": len(excluded),
        "exclusions": dict(sorted(exclusions.items())),
        "metadata_formats_excluded": sorted(METADATA_EXTENSIONS),
        "completed_units_excluded": ["wf-sender", "trafo", "kurt-schwarz"],
        "outputs": {
            "candidates": str(CANDIDATES),
            "exact_duplicates": str(DUPLICATES),
            "exclusions": str(EXCLUSIONS),
        },
    }
    temporary = SUMMARY.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SUMMARY)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
