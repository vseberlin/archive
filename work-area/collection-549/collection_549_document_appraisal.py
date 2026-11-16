#!/usr/bin/env python3
"""Prepare a read-only, provenance-strict document appraisal for collection 549.

The USB archive and the published unit are inputs only.  Outputs are review
records under iss-archive-work; this script never changes source or unit files.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


INDEX = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
ARCHIVE_ROOT = Path("/media/vladimir/Elements/restore/archive")
CURRENT_DOCUMENTS = Path(
    "/mnt/volume/iss-archive/units/objektsammlung/00_MANIFESTS/documents.csv"
)
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/collection-549/work/reconciliation/"
    "unit-reconciliation-v1"
)

QUERIES = {
    "project_name": '"Objektsammlung"',
    "project_record_lead": '"Ausstellungsobjekte" OR "Sachtexte"',
    "unit_inventory": (
        '"Obj_10001" OR "Obj_10002" OR "Obj_10003" OR "Obj_10004" OR '
        '"Obj_10006" OR "Object_1007" OR "Object_1008" OR '
        '"Object_1009" OR "Object_1010"'
    ),
    "leningrad_t2": '"Leningrad T2"',
    "srs_4451": '"SRS 4451" OR SRS4451',
    "lv3n": "LV3N",
    "colorskop": "Colorskop",
    "campingleuchte": "Campingleuchte",
    "radiosonde": "Radiosonde OR Wettersonde",
    "blitzzusatzgeraet": 'Blitzzusatzgerat OR Blitzzusatzgerät OR "BZG 1" OR "BGZ 1"',
}

# These names identify an independently managed corpus or a broad contextual
# source.  They are not sufficient by themselves to bind a document to the
# physical Objektsammlung specimen.
CROSS_PROJECT_MARKERS = (
    "/findbuch",
    "/findbücher",
    "/forschungs- und entwicklungsberichte/",
    "/wf-sender",
    "/tfa_",
    "/thesaurus/",
    "/pressebildarchiv/",
    "/brigadebuch",
    "/buchprojekt",
    "/trafo/",
    "/kurt schwarz/",
    "/mediathek-backup/",
    "/mik-daten/",
    "/ausstellung-röhrenmaennchen/",
    "/ausstellung_kurt-schwarz/",
    "/digis/",
    "/export_29_",
)

EXHIBITION_MARKERS = (
    "/wf/ausstellung/",
    "/wf/archiv/wf-ausstellung konzept.docx",
)

DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".html", ".htm", ".json", ".ods", ".odt",
    ".pdf", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx", ".xml",
}
FILENAME_TERMS = re.compile(
    r"objekt|exponat|inventar|sachtext|ausstellung|leningrad|"
    r"srs[- _]*4451|srs4451|lv3n|colorskop|campingleuchte|"
    r"radiosonde|wettersonde|blitzzusatz|bzg[- _]*1|bgz[- _]*1",
    re.IGNORECASE,
)

CONTENT_PATTERNS = {
    "project_name": re.compile(r"objektsammlung", re.IGNORECASE),
    "unit_inventory": re.compile(
        r"Obj_1000(?:1|2|3|4|6)|Object_10(?:07|08|09|10)", re.IGNORECASE
    ),
    "leningrad_t2": re.compile(r"Leningrad\s*T2", re.IGNORECASE),
    "srs_4451": re.compile(r"SRS\s*4451", re.IGNORECASE),
    "lv3n": re.compile(r"LV3N", re.IGNORECASE),
    "colorskop": re.compile(r"Colorskop", re.IGNORECASE),
    "campingleuchte": re.compile(r"Campingleuchte", re.IGNORECASE),
    "radiosonde": re.compile(r"Radio(?:sonde)|Wettersonde", re.IGNORECASE),
    "blitzzusatzgeraet": re.compile(
        r"Blitzzusatzger|(?<![A-Za-z0-9])(?:BZG|BGZ)\s*1(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
}
DIRECT_TEXT_EXTENSIONS = {".csv", ".htm", ".html", ".json", ".rtf", ".txt", ".xml"}

CONTEXT_ONLY_NAMES = (
    "tektonik_achiv-industriesalon.docx",
    "tektonik_archiv-industriesalon.pdf",
    "tektonik_archiv-industriesalon_update-2021.pdf",
    "überlegungen zur weiteren vorgehensweise im archiv.docx",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def classify(path: str, terms: set[str]) -> tuple[str, str]:
    lower = path.casefold()
    name = Path(path).name.casefold()
    if "unit_inventory" in terms:
        return (
            "candidate_project_bound",
            "contains an exact Objektsammlung inventory identifier; requires human confirmation",
        )
    if name == "zeugnis-industriesalon_michael-schaible.docx":
        return (
            "exclude_different_object_collection",
            "text identifies an unrelated printing-company object collection",
        )
    if name in CONTEXT_ONLY_NAMES:
        return (
            "exclude_context_only",
            "archive-level planning or classification; mentions Objektsammlung but is not an object dossier",
        )
    if any(marker in lower for marker in CROSS_PROJECT_MARKERS):
        return (
            "exclude_cross_project",
            "independently managed project, catalogue export, or broad reference corpus",
        )
    if any(marker in lower for marker in EXHIBITION_MARKERS):
        return (
            "exclude_exhibition_or_photo_project",
            "exhibition or TFA-photo working record; no exact physical-object provenance link",
        )
    if "project_name" in terms:
        return (
            "unresolved_project_name_mention",
            "mentions Objektsammlung but exact document-to-object role is not yet established",
        )
    return (
        "exclude_topical_only",
        "device or subject match only; no Objektsammlung project or specimen provenance",
    )


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if not INDEX.is_file() or not CURRENT_DOCUMENTS.is_file() or not ARCHIVE_ROOT.is_dir():
        raise SystemExit("required read-only input is missing")
    mount = subprocess.run(
        ["findmnt", "-T", str(ARCHIVE_ROOT), "-no", "SOURCE,TARGET,FSTYPE,OPTIONS"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if " /media/vladimir/Elements " not in f" {mount} " or " ro," not in f" {mount},":
        raise SystemExit(f"protected USB mount guard failed: {mount}")
    OUTPUT.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{INDEX}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    hits: dict[str, dict[str, object]] = {}
    query_counts: dict[str, int] = {}
    for label, query in QUERIES.items():
        rows = con.execute(
            """
            SELECT document_id, filename, relative_path,
                   snippet(document_fts, 3, '[[', ']]', ' ... ', 32) AS snippet
              FROM document_fts
             WHERE document_fts MATCH ?
             ORDER BY rank
            """,
            (query,),
        ).fetchall()
        query_counts[label] = len(rows)
        for row in rows:
            item = hits.setdefault(
                row["document_id"],
                {
                    "document_id": row["document_id"],
                    "filename": row["filename"],
                    "relative_path": row["relative_path"],
                    "matched_terms": set(),
                    "snippets": [],
                },
            )
            item["matched_terms"].add(label)
            snippet = " ".join((row["snippet"] or "").split())
            if snippet and snippet not in item["snippets"]:
                item["snippets"].append(snippet)

    candidate_rows: list[dict[str, object]] = []
    for document_id, item in hits.items():
        detail = con.execute(
            """
            SELECT source_path, extension, size_bytes, content_sha256,
                   extraction_state, parser, extraction_error
              FROM documents WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        terms = item["matched_terms"]
        disposition, reason = classify(str(detail["source_path"]), terms)
        candidate_rows.append(
            {
                "document_id": document_id,
                "source_path": detail["source_path"],
                "extension": detail["extension"],
                "size_bytes": detail["size_bytes"],
                "content_sha256": detail["content_sha256"],
                "extraction_state": detail["extraction_state"],
                "matched_terms": ";".join(sorted(terms)),
                "appraisal": disposition,
                "reason": reason,
                "evidence_snippet": " || ".join(item["snippets"])[:4000],
                "human_confirmed": "false",
                "deletion_authorized": "false",
            }
        )
    indexed_source_paths = {row[0] for row in con.execute("SELECT source_path FROM documents")}
    con.close()
    candidate_rows.sort(key=lambda row: (str(row["appraisal"]), str(row["source_path"])))

    current_rows: list[dict[str, object]] = []
    with CURRENT_DOCUMENTS.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["md_object_id"] == "49945":
                appraisal = "remove_from_unit_wrong_project_findbuecher"
                reason = "Findbücher is a separate project, not Objektsammlung documentation"
            elif row["md_object_id"] == "57596":
                appraisal = "remove_from_unit_cross_project_feb"
                reason = "Campingleuchte FEB is a separate research-report object/corpus; topical relation is insufficient"
            elif row["md_object_id"] == "117742":
                appraisal = "remove_from_unit_context_only"
                reason = "weather-sonde lecture is topical reference without exact specimen or Objektsammlung provenance"
            else:
                appraisal = "review"
                reason = "no rule"
            current_rows.append(
                {
                    "document_id": row["document_id"],
                    "md_object_id": row["md_object_id"],
                    "inventory": row["inventory"],
                    "unit_relative_path": row["relative_path"],
                    "original_source_path": row["original_source_path"],
                    "sha256": row["sha256"],
                    "appraisal": appraisal,
                    "reason": reason,
                    "proposed_unit_action": "remove_from_objektsammlung_documents",
                    "source_action": "retain_unchanged",
                    "implemented": "false",
                    "deletion_authorized": "false",
                }
            )

    usb_files_seen = 0
    usb_document_files_seen = 0
    usb_scan_errors: list[str] = []
    filename_rows: list[dict[str, object]] = []
    unindexed_document_files = 0
    unindexed_nontext_paths: list[str] = []
    unindexed_content_rows: list[dict[str, object]] = []
    for directory, _subdirs, filenames in os.walk(ARCHIVE_ROOT, onerror=lambda e: usb_scan_errors.append(str(e))):
        for filename in filenames:
            usb_files_seen += 1
            source = Path(directory) / filename
            extension = source.suffix.casefold()
            if extension not in DOCUMENT_EXTENSIONS:
                continue
            usb_document_files_seen += 1
            source_string = str(source)
            if source_string not in indexed_source_paths:
                unindexed_document_files += 1
                if extension in DIRECT_TEXT_EXTENSIONS:
                    try:
                        content = source.read_bytes().decode("utf-8", errors="ignore")
                    except OSError as error:
                        usb_scan_errors.append(f"{source}: {error}")
                        content = ""
                    matched_content_terms = {
                        label for label, pattern in CONTENT_PATTERNS.items() if pattern.search(content)
                    }
                    if matched_content_terms:
                        first_match = min(
                            (
                                match
                                for pattern in CONTENT_PATTERNS.values()
                                if (match := pattern.search(content)) is not None
                            ),
                            key=lambda match: match.start(),
                        )
                        start = max(0, first_match.start() - 180)
                        end = min(len(content), first_match.end() + 300)
                        snippet = " ".join(content[start:end].split())
                        appraisal, reason = classify(source_string, matched_content_terms)
                        unindexed_content_rows.append(
                            {
                                "source_path": source_string,
                                "extension": extension,
                                "size_bytes": source.stat().st_size,
                                "matched_terms": ";".join(sorted(matched_content_terms)),
                                "appraisal": appraisal,
                                "reason": reason,
                                "evidence_snippet": snippet,
                                "human_confirmed": "false",
                                "deletion_authorized": "false",
                            }
                        )
                else:
                    unindexed_nontext_paths.append(source_string)
            matches = sorted({m.group(0) for m in FILENAME_TERMS.finditer(filename)}, key=str.casefold)
            if not matches:
                continue
            appraisal, reason = classify(str(source), set())
            try:
                size_bytes = source.stat().st_size
            except OSError as error:
                size_bytes = -1
                usb_scan_errors.append(f"{source}: {error}")
            filename_rows.append(
                {
                    "source_path": str(source),
                    "extension": extension,
                    "size_bytes": size_bytes,
                    "matched_filename_terms": ";".join(matches),
                    "appraisal": appraisal,
                    "reason": reason,
                    "human_confirmed": "false",
                    "deletion_authorized": "false",
                }
            )
    filename_rows.sort(key=lambda row: str(row["source_path"]))
    unindexed_content_rows.sort(key=lambda row: str(row["source_path"]))
    unindexed_nontext_paths.sort()

    candidate_path = OUTPUT / "object-document-search-candidates.csv"
    current_path = OUTPUT / "current-unit-document-correction-proposal.csv"
    filename_path = OUTPUT / "usb-document-filename-search-hits.csv"
    unindexed_content_path = OUTPUT / "usb-unindexed-document-content-hits.csv"
    write_csv(
        candidate_path,
        [
            "document_id", "source_path", "extension", "size_bytes",
            "content_sha256", "extraction_state", "matched_terms", "appraisal",
            "reason", "evidence_snippet", "human_confirmed", "deletion_authorized",
        ],
        candidate_rows,
    )
    write_csv(
        current_path,
        [
            "document_id", "md_object_id", "inventory", "unit_relative_path",
            "original_source_path", "sha256", "appraisal", "reason",
            "proposed_unit_action", "source_action", "implemented",
            "deletion_authorized",
        ],
        current_rows,
    )
    write_csv(
        filename_path,
        [
            "source_path", "extension", "size_bytes", "matched_filename_terms",
            "appraisal", "reason", "human_confirmed", "deletion_authorized",
        ],
        filename_rows,
    )
    write_csv(
        unindexed_content_path,
        [
            "source_path", "extension", "size_bytes", "matched_terms", "appraisal",
            "reason", "evidence_snippet", "human_confirmed", "deletion_authorized",
        ],
        unindexed_content_rows,
    )

    summary = {
        "generated_at": utc_now(),
        "scope": "collection 549 / Objektsammlung document appraisal",
        "protected_mount": mount,
        "authority_rule": (
            "Accession only a document whose own content or durable provenance binds it "
            "to collection 549 or an exact physical object in it. Topic, folder, model name, "
            "and project adjacency are insufficient."
        ),
        "index_path": str(INDEX),
        "index_size_bytes": INDEX.stat().st_size,
        "index_mtime_ns": INDEX.stat().st_mtime_ns,
        "index_query_counts": query_counts,
        "unique_index_hits": len(candidate_rows),
        "candidate_appraisal_counts": dict(Counter(str(r["appraisal"]) for r in candidate_rows)),
        "current_unit_documents": len(current_rows),
        "current_unit_correction_counts": dict(Counter(str(r["appraisal"]) for r in current_rows)),
        "proven_project_bound_candidates": sum(
            r["appraisal"] == "candidate_project_bound" for r in candidate_rows
        ),
        "usb_files_seen": usb_files_seen,
        "usb_document_files_seen": usb_document_files_seen,
        "usb_filename_hits": len(filename_rows),
        "usb_unindexed_document_files": unindexed_document_files,
        "usb_unindexed_direct_text_content_hits": len(unindexed_content_rows),
        "usb_unindexed_nontext_paths": unindexed_nontext_paths,
        "usb_scan_errors": usb_scan_errors,
        "published_unit_modified": False,
        "source_modified": False,
        "deletion_authorized": False,
        "outputs": [
            str(candidate_path), str(current_path), str(filename_path),
            str(unindexed_content_path),
        ],
    }
    (OUTPUT / "object-document-appraisal-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
