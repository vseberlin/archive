#!/usr/bin/env python3
"""Prepare, but do not accession, the reviewed TFA documentation package."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work")
OUTPUT_ROOT = WORK_ROOT / "reconciliation/unit-reconciliation"
SOURCE_ROOT = Path("/media/vladimir/Elements")
DATABASE = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
CANDIDATES = OUTPUT_ROOT / "tfa-document-candidates.csv"
DUPLICATES = OUTPUT_ROOT / "tfa-document-exact-duplicates.csv"
EXCLUSIONS = OUTPUT_ROOT / "tfa-document-exclusions.csv"
PLAN = OUTPUT_ROOT / "tfa-document-accession-plan.csv"
DISPOSITION = OUTPUT_ROOT / "tfa-document-source-disposition-proposal.csv"
CONTENTS = OUTPUT_ROOT / "tfa-document-contents.prepared.md"
STAGE = OUTPUT_ROOT / "tfa-document-accession-stage.zip"
SUMMARY = OUTPUT_ROOT / "tfa-document-accession-preparation.json"

PROJECT_FINAL = {
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/2019-12-12_Digis-Abschluss.pptx": (
        "documents/project-final/01-project-completion-presentation.pptx",
        "TFA digitization project completion presentation",
    ),
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/2019-12-16_Industriesalon.pptx": (
        "documents/project-final/02-project-presentation.pptx",
        "TFA digitization project presentation",
    ),
    "/media/vladimir/Elements/restore/archive/WF/Thesaurus/Findbuch_1953-1960.xlsx": (
        "documents/project-final/03-finding-aid-1953-1960.xlsx",
        "Consolidated TFA finding aid for 1953-1960",
    ),
    "/media/vladimir/Elements/restore/archive/WF/Thesaurus/Schlagwoerter/Thesaurus-Probe-Industriesalon.xlsx": (
        "documents/project-final/04-thesaurus-reference.xlsx",
        "TFA thesaurus reference workbook",
    ),
    "/media/vladimir/Elements/restore/archive/WF/Digis/Digis-Berichte/Dauerarchivierung/submission-manifest.txt": (
        "documents/project-final/05-preservation-submission-manifest.txt",
        "TFA digitization preservation submission manifest",
    ),
    "/media/vladimir/Elements/restore/archive/SCANS-fertig/KORREKTUREN-der-Scans.txt": (
        "documents/project-final/06-scan-corrections.txt",
        "TFA scan-correction record",
    ),
}

PLAN_FIELDS = (
    "document_id", "source_path", "filename", "extension", "size_bytes",
    "source_sha256", "evidence_confidence", "extraction_state",
    "proposed_decision", "classification", "final_relative_path", "condition",
    "selection_basis", "deletion_authorized",
)
DISPOSITION_FIELDS = (
    "source_path", "source_kind", "unit", "inventory", "source_role",
    "final_path", "preservation_status", "preservation_basis", "source_sha256",
    "final_sha256", "source_size_bytes", "exact_copy_proven", "drain_state",
    "deletion_authorized", "notes",
)
PACKAGE_MANIFEST_FIELDS = (
    "member", "document_id", "original_filename", "source_path",
    "source_sha256", "size_bytes", "condition", "classification",
    "deletion_authorized",
)


class PrepareError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: list[dict[str, object]], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def sha256_small(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PrepareError("Elements mount cannot be resolved")
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise PrepareError("Elements mount resolution is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements is not protected ntfs3 read-only: {mount}")
    return {
        "source": str(mount.get("source", "")),
        "target": str(mount.get("target", "")),
        "fstype": str(mount.get("fstype", "")),
        "options": str(mount.get("options", "")),
    }


def require_boundaries() -> None:
    work = WORK_ROOT.resolve(strict=True)
    output = OUTPUT_ROOT.resolve(strict=True)
    output.relative_to(work)
    for path in (PLAN, DISPOSITION, CONTENTS, STAGE, SUMMARY):
        if path.parent.resolve(strict=True) != output:
            raise PrepareError(f"output escapes approved TFA work root: {path}")
    if STAGE.exists() or SUMMARY.exists():
        raise PrepareError("TFA documentation preparation already exists; preserve it and review before rerunning")


def safe_filename(name: str) -> str:
    value = re.sub(r"[/\\\x00-\x1f]", "_", name).strip(" .")
    return value or "unnamed"


def selected_by_project_chain(row: dict[str, str]) -> bool:
    path = row["source_path"].casefold()
    filename = row["filename"].casefold()
    return (
        "/restore/archive/wf/" in path
        or "/restore/archive/scans-fertig/" in path
        or "tfa" in filename
        or filename == "1958-rest_1959-rest_02_fehlende-maße.xlsx".casefold()
    )


def restricted_group(path: str) -> str:
    lower = path.casefold()
    finance = ("invoice", "rechnung", "abrechnung", "honorar", "budget", "kosten", "/r19052_")
    contracts = (
        "vertrag", "angebot", "auftrag", "empfangsbestaetigung", "liefer-empfang",
        "bescheid", "projektantrag", "digis-antrag", "/antrag ", "/antrag-",
    )
    personal = ("personal", "arbeitsvertrag", "lebenslauf", "bewerbung", "lohn")
    if any(token in lower for token in finance):
        return "finance"
    if any(token in lower for token in personal):
        return "personal"
    if any(token in lower for token in contracts):
        return "contracts"
    return ""


def classify(row: dict[str, str]) -> tuple[str, str, str]:
    source = row["source_path"]
    if source in PROJECT_FINAL:
        final_path, description = PROJECT_FINAL[source]
        return "project_final", final_path, description
    restricted = restricted_group(source)
    if restricted:
        return (
            f"restricted_{restricted}",
            f"documents/restricted/{restricted}.zip",
            f"Restricted TFA project {restricted} documentation",
        )
    return (
        "historical",
        "documents/historical/drafts-and-superseded.zip",
        "TFA working inventories, drafts, version chains, and contextual project evidence",
    )


def load_source_state(paths: set[str]) -> dict[str, sqlite3.Row]:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    status = connection.execute("SELECT value FROM meta WHERE key='extraction_status'").fetchone()
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if status is None or status[0] != "complete" or foreign:
        raise PrepareError(f"shared document index is not ready: status={status}, foreign={len(foreign)}")
    result: dict[str, sqlite3.Row] = {}
    ordered = sorted(paths)
    for start in range(0, len(ordered), 400):
        batch = ordered[start:start + 400]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"""
            SELECT source_path,size_bytes,mtime_ns,content_sha256,observed_state,
                   extraction_state,extraction_error
            FROM documents WHERE source_path IN ({placeholders})
            """,
            batch,
        ):
            result[row["source_path"]] = row
    connection.close()
    return result


def validate_sources(paths: set[str], state: dict[str, sqlite3.Row]) -> None:
    root = SOURCE_ROOT.resolve(strict=True)
    for raw in sorted(paths):
        indexed = state.get(raw)
        if indexed is None or indexed["observed_state"] != "present":
            raise PrepareError(f"selected source is not currently indexed as present: {raw}")
        path = Path(raw).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise PrepareError(f"source resolves outside Elements: {path}") from error
        stat = path.stat()
        if stat.st_size != indexed["size_bytes"] or stat.st_mtime_ns != indexed["mtime_ns"]:
            raise PrepareError(f"source changed since document scan: {path}")
        digest = str(indexed["content_sha256"])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise PrepareError(f"selected source lacks a valid indexed SHA-256: {path}")


def build_plan(
    candidates: list[dict[str, str]],
    state: dict[str, sqlite3.Row],
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    rows: list[dict[str, str]] = []
    by_document: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        selected = selected_by_project_chain(candidate)
        if selected:
            classification, final_path, description = classify(candidate)
            decision = "proposed_accession"
            condition = (
                "parser_error_preserve_exact_source_bytes"
                if candidate["extraction_state"] == "error"
                else "indexed_and_readable"
            )
            basis = (
                description
                + "; project-chain and indexed TFA evidence; human confirmation still required"
            )
        else:
            classification = "cross_unit_reference"
            final_path = ""
            decision = "proposed_reviewed_not_accessioned"
            condition = "readable"
            basis = "TFA citation without direct TFA project-chain evidence"
        row = {
            "document_id": candidate["document_id"],
            "source_path": candidate["source_path"],
            "filename": candidate["filename"],
            "extension": candidate["extension"],
            "size_bytes": candidate["size_bytes"],
            "source_sha256": candidate["content_sha256"],
            "evidence_confidence": candidate["evidence_confidence"],
            "extraction_state": candidate["extraction_state"],
            "proposed_decision": decision,
            "classification": classification,
            "final_relative_path": final_path,
            "condition": condition,
            "selection_basis": basis,
            "deletion_authorized": "false",
        }
        indexed = state[candidate["source_path"]]
        if row["source_sha256"] != indexed["content_sha256"]:
            raise PrepareError(f"candidate/index hash mismatch: {candidate['source_path']}")
        rows.append(row)
        by_document[row["document_id"]] = row
    rows.sort(key=lambda row: (row["proposed_decision"], row["classification"], row["source_path"]))
    return rows, by_document


def build_disposition(
    plan: list[dict[str, str]],
    by_document: dict[str, dict[str, str]],
    duplicates: list[dict[str, str]],
    state: dict[str, sqlite3.Row],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in plan:
        selected = item["proposed_decision"] == "proposed_accession"
        rows.append({
            "source_path": item["source_path"],
            "source_kind": "file",
            "unit": "tfa",
            "inventory": "",
            "source_role": item["classification"],
            "final_path": item["final_relative_path"],
            "preservation_status": item["proposed_decision"],
            "preservation_basis": item["selection_basis"],
            "source_sha256": "sha256:" + item["source_sha256"],
            "final_sha256": "",
            "source_size_bytes": item["size_bytes"],
            "exact_copy_proven": "false",
            "drain_state": "pending_accession_confirmation" if selected else "cross_unit_reference",
            "deletion_authorized": "false",
            "notes": "Preparation only; final hash and accession decision not yet written.",
        })
    for duplicate in duplicates:
        representative = by_document.get(duplicate["representative_document_id"])
        indexed = state.get(duplicate["duplicate_source_path"])
        if representative is None or indexed is None:
            raise PrepareError(f"duplicate row cannot be resolved: {duplicate}")
        selected = representative["proposed_decision"] == "proposed_accession"
        rows.append({
            "source_path": duplicate["duplicate_source_path"],
            "source_kind": "file",
            "unit": "tfa",
            "inventory": "",
            "source_role": "exact_document_copy",
            "final_path": representative["final_relative_path"] if selected else "",
            "preservation_status": (
                "proposed_redundant_exact_copy" if selected else "unresolved_exact_copy"
            ),
            "preservation_basis": (
                "SHA-256 equals proposed representative; redundant only after human confirmation"
            ),
            "source_sha256": "sha256:" + indexed["content_sha256"],
            "final_sha256": "",
            "source_size_bytes": str(indexed["size_bytes"]),
            "exact_copy_proven": "true",
            "drain_state": "pending_human_confirmation",
            "deletion_authorized": "false",
            "notes": "Exact-copy suggestion is not deletion authority.",
        })
    rows.sort(key=lambda row: row["source_path"])
    if len({row["source_path"] for row in rows}) != len(rows):
        raise PrepareError("source disposition contains duplicate source paths")
    return rows


def package_manifest_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    for number, row in enumerate(sorted(rows, key=lambda item: item["source_path"]), 1):
        result.append({
            "member": f"{number:04d}-{safe_filename(row['filename'])}",
            "document_id": row["document_id"],
            "original_filename": row["filename"],
            "source_path": row["source_path"],
            "source_sha256": "sha256:" + row["source_sha256"],
            "size_bytes": row["size_bytes"],
            "condition": row["condition"],
            "classification": row["classification"],
            "deletion_authorized": "false",
        })
    return result


def build_inner_package(path: Path, rows: list[dict[str, str]], title: str) -> None:
    manifest = package_manifest_rows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for row, item in zip(sorted(rows, key=lambda value: value["source_path"]), manifest):
            archive.write(row["source_path"], item["member"])
        archive.writestr("MANIFEST.csv", csv_bytes(manifest, PACKAGE_MANIFEST_FIELDS))
        archive.writestr(
            "README.md",
            (
                f"# {title}\n\n"
                "This proposed package preserves exact source bytes. MANIFEST.csv records "
                "original paths and indexed source hashes. Preparation is not accession or "
                "deletion authority.\n"
            ).encode("utf-8"),
        )
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise PrepareError(f"ZIP CRC failure in {path}: {bad}")


def contents_text(plan: list[dict[str, str]], disposition: list[dict[str, str]]) -> str:
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    by_class = Counter(row["classification"] for row in selected)
    lines = [
        "# TFA documentation accession preparation",
        "",
        "This is a proposed accession package modelled on the Kurt Schwarz documentation layout. ",
        "Selection uses TFA content and project-chain evidence. It does not change the published TFA unit, ",
        "does not generate final hashes, and does not authorize source deletion.",
        "",
        "## Project-final records",
        "",
    ]
    for row in selected:
        if row["classification"] != "project_final":
            continue
        lines.extend([
            f"### `{Path(row['final_relative_path']).name}`", "",
            PROJECT_FINAL[row["source_path"]][1], "",
            f"- Original source: `{row['source_path']}`",
            f"- Condition: {row['condition']}",
            "- Status: proposed; human confirmation required", "",
        ])
    lines.extend(["## Restricted packages", ""])
    for group in ("contracts", "finance", "personal"):
        count = by_class.get(f"restricted_{group}", 0)
        if count:
            lines.extend([
                f"### `{group}.zip`", "",
                f"- Proposed unique source documents: {count}",
                "- Original paths and conditions: package `MANIFEST.csv`",
                "- Access: restricted", "",
            ])
    lines.extend([
        "## Historical documentation", "",
        "### `drafts-and-superseded.zip`", "",
        f"- Proposed unique source documents: {by_class.get('historical', 0)}",
        "- Includes working inventories, drafts, version chains, thesaurus work, and condition-noted malformed originals.",
        "- Original paths and conditions: package `MANIFEST.csv`", "",
        "## Pending review", "",
        f"- Proposed accession sources: {len(selected)}",
        f"- Proposed exact redundant copies: {sum(row['preservation_status']=='proposed_redundant_exact_copy' for row in disposition)}",
        f"- Reviewed but not proposed for accession: {sum(row['proposed_decision']!='proposed_accession' for row in plan)}",
        "- Final SHA-256 generation is deferred until accession.",
        "- `deletion_authorized=false` for every source.", "",
    ])
    return "\n".join(lines)


def build_stage(
    plan: list[dict[str, str]],
    disposition: list[dict[str, str]],
    contents: str,
    preparation: dict[str, object],
) -> None:
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        grouped[row["classification"]].append(row)
    temporary_packages: list[Path] = []
    stage_temp = STAGE.with_suffix(".zip.tmp")
    try:
        for classification, title in (
            ("restricted_contracts", "TFA restricted contracts and applications"),
            ("restricted_finance", "TFA restricted finance records"),
            ("restricted_personal", "TFA restricted personal records"),
            ("historical", "TFA historical drafts and superseded documentation"),
        ):
            if not grouped.get(classification):
                continue
            handle = tempfile.NamedTemporaryFile(
                prefix=".tfa-document-package-", suffix=".zip", dir=OUTPUT_ROOT, delete=False
            )
            handle.close()
            path = Path(handle.name)
            temporary_packages.append(path)
            build_inner_package(path, grouped[classification], title)
        with zipfile.ZipFile(stage_temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for row in grouped.get("project_final", []):
                archive.write(row["source_path"], row["final_relative_path"])
            package_order = [
                classification for classification in (
                    "restricted_contracts", "restricted_finance", "restricted_personal", "historical"
                ) if grouped.get(classification)
            ]
            for classification, path in zip(package_order, temporary_packages):
                if classification == "historical":
                    final = "documents/historical/drafts-and-superseded.zip"
                else:
                    final = "documents/restricted/" + classification.removeprefix("restricted_") + ".zip"
                archive.write(path, final)
            archive.writestr("documents/CONTENTS.md", contents.encode("utf-8"))
            archive.writestr("db-restore/manifests/documents.csv", csv_bytes(plan, PLAN_FIELDS))
            archive.writestr(
                "db-restore/manifests/source-disposition.csv",
                csv_bytes(disposition, DISPOSITION_FIELDS),
            )
            archive.writestr(
                "PREPARATION.json",
                json.dumps(preparation, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
            )
        with zipfile.ZipFile(stage_temp) as archive:
            bad = archive.testzip()
            if bad:
                raise PrepareError(f"stage ZIP CRC failure: {bad}")
        stage_temp.replace(STAGE)
    finally:
        for path in temporary_packages:
            path.unlink(missing_ok=True)
        stage_temp.unlink(missing_ok=True)


def main() -> int:
    require_boundaries()
    mount = source_mount()
    candidates = csv_rows(CANDIDATES)
    duplicates = csv_rows(DUPLICATES)
    paths = {row["source_path"] for row in candidates}
    paths.update(row["duplicate_source_path"] for row in duplicates)
    state = load_source_state(paths)
    validate_sources(paths, state)
    plan, by_document = build_plan(candidates, state)
    disposition = build_disposition(plan, by_document, duplicates, state)
    selected = [row for row in plan if row["proposed_decision"] == "proposed_accession"]
    selected_hashes = [row["source_sha256"] for row in selected]
    if len(selected_hashes) != len(set(selected_hashes)):
        raise PrepareError("selected plan contains duplicate content hashes")
    if any(row["deletion_authorized"] != "false" for row in plan + disposition):
        raise PrepareError("preparation must not authorize deletion")
    contents = contents_text(plan, disposition)
    preparation = {
        "format": "iss-tfa-documentation-accession-preparation-v1",
        "created_at": now(),
        "status": "ready_for_human_confirmation",
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "published_tfa_unit_changed": False,
        "final_hashes_generated": False,
        "deletion_authorized": False,
        "candidate_input_sha256": sha256_small(CANDIDATES),
        "duplicate_input_sha256": sha256_small(DUPLICATES),
        "exclusion_input_sha256": sha256_small(EXCLUSIONS),
        "proposed_accession_sources": len(selected),
        "proposed_accession_bytes": sum(int(row["size_bytes"]) for row in selected),
        "proposed_classifications": dict(sorted(Counter(row["classification"] for row in selected).items())),
        "condition_counts": dict(sorted(Counter(row["condition"] for row in selected).items())),
        "proposed_exact_redundant_copies": sum(
            row["preservation_status"] == "proposed_redundant_exact_copy" for row in disposition
        ),
        "reviewed_not_proposed": sum(row["proposed_decision"] != "proposed_accession" for row in plan),
        "source_disposition_rows": len(disposition),
        "stage_path": str(STAGE),
        "next_action": "human confirmation, then accession into the existing TFA unit and one final SHA-256 inventory",
    }
    build_stage(plan, disposition, contents, preparation)
    atomic_write(PLAN, csv_bytes(plan, PLAN_FIELDS))
    atomic_write(DISPOSITION, csv_bytes(disposition, DISPOSITION_FIELDS))
    atomic_write(CONTENTS, contents.encode("utf-8"))
    preparation["stage_size_bytes"] = STAGE.stat().st_size
    atomic_write(SUMMARY, json.dumps(preparation, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    print(json.dumps(preparation, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
