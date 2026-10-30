#!/usr/bin/env python3
"""Relate Kurt Schwarz physical finding-aid groups to digital archive records.

The Gesamtverzeichnis describes physical boxes and negative/slide groups.  A
group signature can provide a bag description and date context for a digital
scan, but it does not prove that two frames are the same object.  This tool
therefore enriches only the selected review interpretation and never assigns
an ISS/KS object identifier or changes a review disposition.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kurt_schwarz_shared_workbooks import read_xlsx


csv.field_size_limit(sys.maxsize)

FORMAT = "iss-kurt-schwarz-physical-inventory-relations-v1"
SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
GROUP_SIGNATURE = re.compile(
    r"(?i)^K(?P<box>\d{1,2})[_-](?P<letter>[A-Z])(?P<section>\d{1,2})"
    r"[_-]T0*(?P<title>\d+)(?:[_-](?P<variant>[A-Z]))?$"
)
FLAT_SIGNATURE = re.compile(
    r"(?i)^K(?P<box>\d{1,2})[_-](?P<letter>[A-Z])0*(?P<number>\d+)"
    r"(?:[_-](?P<variant>[A-Z]))?$"
)
DIGITAL_GROUP = re.compile(
    r"(?i)(?<![A-Z0-9])(?P<letter>[A-Q])(?P<section>\d{1,2})"
    r"[-_]T0*(?P<title>\d+)(?:[-_](?P<variant>[A-Z]))?"
)
DIGITAL_FLAT = re.compile(
    r"(?i)(?<![A-Z0-9])K(?P<box>\d{1,2})[-_]"
    r"(?P<letter>[A-Z])0*(?P<number>\d+)(?:[-_](?P<variant>[A-Z]))?"
)
RELATION_FIELDS = (
    "physical_signature",
    "box",
    "digital_group",
    "section_title",
    "bag_description",
    "year",
    "note",
    "workbook_sheet",
    "workbook_row",
    "existing_record_keys",
    "existing_ks_inventories",
    "existing_scan_signatures",
    "existing_object_count",
    "census_candidate_ids",
    "census_filenames",
    "census_queue_classes",
    "census_candidate_count",
    "relation_state",
    "deletion_authorized",
)
VERSION_FIELDS = (
    "source_document",
    "sha256",
    "byte_duplicate_count",
    "readable",
    "physical_group_count",
    "groups_shared_with_authoritative",
    "groups_added_in_authoritative",
    "groups_absent_from_authoritative",
    "section_text_differences",
    "bag_text_differences",
    "year_differences",
    "note_differences",
    "error",
    "deletion_authorized",
)


class RelationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise RelationError(f"path escaped allowed root: {resolved}") from error
    return resolved


def root_is_read_only(path: Path) -> bool:
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def physical_relation_key(value: str) -> tuple[Any, ...] | None:
    value = str(value).strip()
    match = GROUP_SIGNATURE.fullmatch(value)
    if match:
        return (
            "group",
            match.group("letter").upper(),
            int(match.group("section")),
            int(match.group("title")),
            (match.group("variant") or "").upper(),
        )
    match = FLAT_SIGNATURE.fullmatch(value)
    if match:
        return (
            "flat",
            int(match.group("box")),
            match.group("letter").upper(),
            int(match.group("number")),
            (match.group("variant") or "").upper(),
        )
    return None


def candidate_relation_keys(value: str) -> list[tuple[Any, ...]]:
    """Return exact then base physical-group keys for a digital filename."""

    stem = Path(str(value).strip()).stem
    keys: list[tuple[Any, ...]] = []
    group = DIGITAL_GROUP.search(stem)
    if group:
        key = (
            "group",
            group.group("letter").upper(),
            int(group.group("section")),
            int(group.group("title")),
            (group.group("variant") or "").upper(),
        )
        keys.append(key)
        if key[-1]:
            keys.append((*key[:-1], ""))
    flat = DIGITAL_FLAT.search(stem)
    if flat:
        key = (
            "flat",
            int(flat.group("box")),
            flat.group("letter").upper(),
            int(flat.group("number")),
            (flat.group("variant") or "").upper(),
        )
        keys.append(key)
        if key[-1]:
            keys.append((*key[:-1], ""))
    return list(dict.fromkeys(keys))


def signature_from_record(record: dict[str, str]) -> str:
    for key, value in record.items():
        if key.strip().casefold() == "signatur" and str(value).strip():
            return str(value).strip()
    return ""


def first(record: dict[str, str], *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for key, value in record.items():
        if key.strip().casefold() in wanted and str(value).strip():
            return " ".join(str(value).split())
    return ""


def read_physical_inventory(workbook: Path) -> list[dict[str, str]]:
    parsed = read_xlsx(workbook)
    sheet_names = {sheet for sheet, _, _ in parsed}
    if "Tabellen einzeln" in sheet_names:
        selected_sheet = "Tabellen einzeln"
    elif "Findbuch" in sheet_names:
        selected_sheet = "Findbuch"
    elif "Tabelle1" in sheet_names:
        selected_sheet = "Tabelle1"
    else:
        raise RelationError("finding aid has no recognized inventory sheet")
    rows = []
    seen = set()
    for sheet, row_number, record in parsed:
        if sheet != selected_sheet:
            continue
        signature = signature_from_record(record)
        key = physical_relation_key(signature)
        if key is None:
            # One known formula cell lost its box prefix (``-L01_T001``),
            # while the adjacent source columns still retain K10/L01/T001.
            box_value = first(record, "Karton")
            section_value = first(record, "Abschnitt Nr.")
            title_value = first(record, "Titel Nr.")
            reconstructed = "_".join(
                value for value in (box_value, section_value, title_value) if value
            )
            reconstructed_key = physical_relation_key(reconstructed)
            if reconstructed_key is not None:
                signature = reconstructed
                key = reconstructed_key
        if key is None:
            continue
        if key in seen:
            raise RelationError(f"duplicate physical relation: {signature}")
        seen.add(key)
        section = first(record, "Abschnitt Titel", "Teil Titel")
        bag = first(record, "Teil-Titel", "Teil Titel")
        box = first(record, "Karton") or re.split(r"[_-]", signature, 1)[0]
        rows.append(
            {
                "physical_signature": signature,
                "box": box,
                "section_title": section,
                "bag_description": bag,
                "year": first(record, "Jahr"),
                "note": first(record, "Anmerkung"),
                "workbook_sheet": sheet,
                "workbook_row": str(row_number),
                "relation_key": key,
            }
        )
    if not rows:
        raise RelationError("finding aid produced no physical signatures")
    return rows


def resolve_relation(
    value: str,
    physical_index: dict[tuple[Any, ...], dict[str, str]],
    aliases: dict[tuple[Any, ...], tuple[Any, ...]] | None = None,
) -> dict[str, str] | None:
    for key in candidate_relation_keys(value):
        if key in physical_index:
            return physical_index[key]
    for key in candidate_relation_keys(value):
        physical_key = (aliases or {}).get(key)
        if physical_key in physical_index:
            return physical_index[physical_key]
    return None


def bag_label(row: dict[str, str]) -> str:
    value = row["physical_signature"]
    if row["section_title"]:
        value += "; " + row["section_title"]
    if row["bag_description"]:
        value += " | Taschenbeschriftung: " + row["bag_description"]
    return value


def physical_evidence(row: dict[str, str], workbook: Path) -> dict[str, str]:
    return {
        "evidence_type": "physical_finding_aid_group",
        "source_document": str(workbook),
        "sheet_name": row["workbook_sheet"],
        "row_number": row["workbook_row"],
        "inventory": "",
        "mapped_md_object_ids": "",
        "mapped_record_keys": "",
        "mapping_state": "physical_group_relation",
        "title": row["bag_description"] or row["section_title"],
        "description": "",
        "tags": "",
        "date_text": row["year"],
        "places": "",
        "material": "",
        "dimensions": "",
        "scan_signature": row["physical_signature"],
        "bag_label": bag_label(row),
        "match_basis": "normalized_physical_to_digital_scan_signature",
    }


def enrich_candidate(
    candidate: dict[str, str], physical: dict[str, str], workbook: Path
) -> tuple[dict[str, str], bool]:
    result = dict(candidate)
    matches = json.loads(result.get("metadata_matches_json") or "[]")
    evidence = physical_evidence(physical, workbook)
    identity = (
        evidence["source_document"],
        evidence["sheet_name"],
        evidence["row_number"],
        evidence["scan_signature"],
    )
    identities = {
        (
            str(item.get("source_document", "")),
            str(item.get("sheet_name", "")),
            str(item.get("row_number", "")),
            str(item.get("scan_signature", "")),
        )
        for item in matches
    }
    if identity not in identities:
        matches.append(evidence)
    result["metadata_matches_json"] = json.dumps(
        matches, ensure_ascii=False, sort_keys=True
    )
    result["metadata_match_count"] = str(len(matches))
    if result.get("queue_class") != "new_local_review":
        return result, False
    selected = json.loads(result.get("selected_metadata_json") or "{}")
    additions = {
        "title": physical["bag_description"] or physical["section_title"],
        "date_text": physical["year"],
        "scan_signature": physical["physical_signature"],
        "bag_label": bag_label(physical),
    }
    changed = False
    for field, value in additions.items():
        if value and not str(selected.get(field, "")).strip():
            selected[field] = value
            changed = True
    if changed:
        result["selected_metadata_json"] = json.dumps(
            selected, ensure_ascii=False, sort_keys=True
        )
        result["selected_metadata_basis"] = "physical_finding_aid_group_signature"
        result["metadata_summary"] = selected.get("title", bag_label(physical))
        result["review_basis"] = (
            "The digital scan signature resolves to one physical finding-aid "
            "group. The group description is selected as review context, not "
            "as proof that every listed subject appears in this frame."
        )
    return result, changed


def write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def workbook_version_audit(
    authoritative: Path, authoritative_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Compare bounded Gesamtverzeichnis versions without treating copies as votes."""

    candidates = {
        path.resolve()
        for directory in (authoritative.parent, authoritative.parent / "Archiv")
        if directory.is_dir()
        for path in directory.glob("Kurt-Schwarz_GESAMTVERZEICHNIS*.xlsx")
        if not path.name.startswith("~$")
    }
    digests = {path: sha256_file(path) for path in candidates}
    digest_counts = Counter(digests.values())
    reference = {row["physical_signature"]: row for row in authoritative_rows}
    output = []
    for path in sorted(candidates, key=lambda item: str(item).casefold()):
        error = ""
        rows: list[dict[str, str]] = []
        try:
            rows = read_physical_inventory(path)
        except Exception as caught:  # preserve unreadable versions as unknown
            error = f"{type(caught).__name__}: {str(caught)[:500]}"
        current = {row["physical_signature"]: row for row in rows}
        common = set(current) & set(reference)
        differences = Counter()
        for signature in common:
            for field in ("section_title", "bag_description", "year", "note"):
                left = " ".join(current[signature][field].split())
                right = " ".join(reference[signature][field].split())
                differences[field] += int(left != right)
        output.append(
            {
                "source_document": str(path),
                "sha256": digests[path],
                "byte_duplicate_count": str(digest_counts[digests[path]]),
                "readable": str(not error).lower(),
                "physical_group_count": str(len(current)),
                "groups_shared_with_authoritative": str(len(common)),
                "groups_added_in_authoritative": str(len(set(reference) - set(current))),
                "groups_absent_from_authoritative": str(len(set(current) - set(reference))),
                "section_text_differences": str(differences["section_title"]),
                "bag_text_differences": str(differences["bag_description"]),
                "year_differences": str(differences["year"]),
                "note_differences": str(differences["note"]),
                "error": error,
                "deletion_authorized": "false",
            }
        )
    return output


def run(
    *, workbook: Path, census: Path, object_plan: Path, output: Path
) -> dict[str, Any]:
    workbook = inside(workbook, SOURCE_ROOT)
    output = inside(output, WORK_ROOT)
    if not root_is_read_only(SOURCE_ROOT):
        raise RelationError("restore archive is not on a read-only filesystem")
    output.mkdir(parents=True, exist_ok=False)
    physical_rows = read_physical_inventory(workbook)
    physical_index = {row["relation_key"]: row for row in physical_rows}
    if len(physical_index) != len(physical_rows):
        raise RelationError("physical relation keys are not unique")

    plan_relations: dict[tuple[Any, ...], list[dict[str, str]]] = defaultdict(list)
    alias_targets: dict[tuple[Any, ...], set[tuple[Any, ...]]] = defaultdict(set)
    for record in read_csv(object_plan):
        physical = resolve_relation(record.get("scan_signature", ""), physical_index)
        signature = re.match(
            r"(?i)^K\d{1,2}[_-][A-Z]\d{1,3}(?:[_-]T\d+)?(?:[_-][A-Z])?",
            record.get("bag_label", ""),
        )
        bag_key = physical_relation_key(signature.group(0)) if signature else None
        bag_physical = physical_index.get(bag_key) if bag_key is not None else None
        if physical is None:
            physical = bag_physical
        if physical is not None:
            plan_relations[physical["relation_key"]].append(record)
        if bag_physical is not None:
            for digital_key in candidate_relation_keys(record.get("scan_signature", "")):
                alias_targets[digital_key].add(bag_physical["relation_key"])
    aliases = {
        digital: next(iter(targets))
        for digital, targets in alias_targets.items()
        if len(targets) == 1
    }

    census_rows = read_csv(census)
    candidate_relations: dict[tuple[Any, ...], list[dict[str, str]]] = defaultdict(list)
    enriched_rows = []
    enriched_count = 0
    related_candidate_count = 0
    alias_related_candidate_count = 0
    for candidate in census_rows:
        direct = resolve_relation(candidate["filename"], physical_index)
        physical = direct or resolve_relation(
            candidate["filename"], physical_index, aliases
        )
        if physical is None:
            enriched_rows.append(candidate)
            continue
        related_candidate_count += 1
        alias_related_candidate_count += int(direct is None)
        candidate_relations[physical["relation_key"]].append(candidate)
        enriched, changed = enrich_candidate(candidate, physical, workbook)
        enriched_count += int(changed)
        enriched_rows.append(enriched)

    relation_rows = []
    relation_states: Counter[str] = Counter()
    for physical in physical_rows:
        key = physical["relation_key"]
        candidates = candidate_relations.get(key, [])
        records = plan_relations.get(key, [])
        if records and candidates:
            state = "physical_existing_and_pending_digital"
        elif records:
            state = "physical_and_existing_digital"
        elif candidates:
            state = "physical_and_pending_digital"
        else:
            state = "physical_only_no_bounded_digital_relation"
        relation_states[state] += 1
        relation_rows.append(
            {
                "physical_signature": physical["physical_signature"],
                "box": physical["box"],
                "digital_group": "; ".join(
                    sorted({r.get("scan_signature", "") for r in records if r.get("scan_signature")})
                ),
                "section_title": physical["section_title"],
                "bag_description": physical["bag_description"],
                "year": physical["year"],
                "note": physical["note"],
                "workbook_sheet": physical["workbook_sheet"],
                "workbook_row": physical["workbook_row"],
                "existing_record_keys": "; ".join(sorted({r["record_key"] for r in records})),
                "existing_ks_inventories": "; ".join(
                    sorted({r["current_inventory"] or r["historical_inventory"] for r in records if r["current_inventory"] or r["historical_inventory"]})
                ),
                "existing_scan_signatures": "; ".join(
                    sorted({r["scan_signature"] for r in records if r["scan_signature"]})
                ),
                "existing_object_count": str(len({r["record_key"] for r in records})),
                "census_candidate_ids": "; ".join(sorted({r["candidate_id"] for r in candidates})),
                "census_filenames": "; ".join(sorted({r["filename"] for r in candidates})),
                "census_queue_classes": "; ".join(sorted({r["queue_class"] for r in candidates})),
                "census_candidate_count": str(len({r["candidate_id"] for r in candidates})),
                "relation_state": state,
                "deletion_authorized": "false",
            }
        )

    version_rows = workbook_version_audit(workbook, physical_rows)
    census_path = output / "object-census.csv"
    relation_path = output / "physical-digital-relations.csv"
    version_path = output / "workbook-versions.csv"
    write_csv(census_path, enriched_rows, tuple(census_rows[0]))
    write_csv(relation_path, relation_rows, RELATION_FIELDS)
    write_csv(version_path, version_rows, VERSION_FIELDS)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "source_read_only": True,
        "workbook": str(workbook),
        "workbook_sha256": sha256_file(workbook),
        "workbook_physical_groups": len(physical_rows),
        "workbook_versions_considered": len(version_rows),
        "workbook_versions_readable": sum(row["readable"] == "true" for row in version_rows),
        "workbook_versions_unreadable": sum(row["readable"] == "false" for row in version_rows),
        "workbook_versions_csv": str(version_path),
        "workbook_versions_sha256": sha256_file(version_path),
        "input_census": str(census),
        "input_census_sha256": sha256_file(census),
        "output_census": str(census_path),
        "output_census_sha256": sha256_file(census_path),
        "candidate_count": len(enriched_rows),
        "candidate_queue_counts": dict(Counter(row["queue_class"] for row in enriched_rows)),
        "candidates_with_physical_relation": related_candidate_count,
        "candidate_relations_via_unanimous_existing_alias": alias_related_candidate_count,
        "new_local_candidates_enriched": enriched_count,
        "object_plan": str(object_plan),
        "object_plan_sha256": sha256_file(object_plan),
        "existing_objects_with_physical_relation": len(
            {r["record_key"] for rows in plan_relations.values() for r in rows}
        ),
        "unanimous_digital_to_physical_aliases": len(aliases),
        "ambiguous_digital_to_physical_aliases": sum(
            len(targets) > 1 for targets in alias_targets.values()
        ),
        "relation_state_counts": dict(relation_states),
        "physical_relations_csv": str(relation_path),
        "physical_relations_sha256": sha256_file(relation_path),
        "identifiers_assigned": 0,
        "review_dispositions_changed": 0,
        "deletion_authorized": False,
    }
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**summary, "summary_path": str(summary_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--object-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(
            workbook=args.workbook,
            census=args.census.resolve(strict=True),
            object_plan=args.object_plan.resolve(strict=True),
            output=args.output,
        )
    except (OSError, ValueError, csv.Error, RelationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
