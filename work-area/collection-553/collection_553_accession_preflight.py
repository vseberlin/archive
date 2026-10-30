#!/usr/bin/env python3
"""Run a read-only accession preflight for museum-digital collection 553.

The preflight validates the existing reconciliation decisions and selected
inputs.  It never creates an archive unit, generates a derivative, updates a
manifest/database, or changes source material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

import collection_553_workbench as workbench_module


SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-553")
ARCHIVE_ROOT = Path("/mnt/volume/iss-archive")
ARCHIVE_UNITS = ARCHIVE_ROOT / "units"
REQUIRED_ARCHIVE_DIRS = {
    "00_MANIFESTS",
    "units",
    "90_UNRECONCILED",
    "99_TRANSIENT_REFERENCE",
}
REQUIRED_ARCHIVE_FILES = {"00_README.md"}
UNIT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
HEX64 = re.compile(r"[0-9a-f]{64}")


class PreflightError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def projection_workbench() -> workbench_module.Workbench:
    """Load the decision model without bypassing any decision validation."""
    instance = workbench_module.Workbench.__new__(workbench_module.Workbench)
    instance.actor = "accession-preflight"
    instance.summary = json.loads(workbench_module.SUMMARY_JSON.read_text(encoding="utf-8"))
    instance.objects = workbench_module.read_csv(workbench_module.OBJECTS_CSV)
    instance.candidates = workbench_module.read_csv(workbench_module.CANDIDATES_CSV)
    instance.resources = workbench_module.read_csv(workbench_module.RESOURCES_CSV)
    instance.scenarios = workbench_module.read_csv(workbench_module.SCENARIOS_CSV)
    instance.relationships = workbench_module.read_csv(workbench_module.RELATIONSHIPS_CSV)
    instance.exceptions = workbench_module.read_csv(workbench_module.EXCEPTIONS_CSV)
    instance.title_leads = workbench_module.read_csv(workbench_module.TITLE_LEADS_CSV)
    instance.page_scan_evidence = workbench_module.read_csv(
        workbench_module.PAGE_SCAN_EVIDENCE_CSV
    )
    instance.objects_by_id = {row["md_object_id"]: row for row in instance.objects}
    instance.candidates_by_id = {
        row["candidate_id"]: row for row in instance.candidates
    }
    instance.resources_by_id = {
        row["resource_id"]: row for row in instance.resources
    }
    instance.scenarios_by_id = {
        row["md_object_id"]: row for row in instance.scenarios
    }
    instance.candidates_by_object = defaultdict(list)
    instance.resources_by_object = defaultdict(list)
    for row in instance.candidates:
        instance.candidates_by_object[row["md_object_id"]].append(row)
    for row in instance.resources:
        instance.resources_by_object[row["md_object_id"]].append(row)
    return instance


def selected_members(
    instance: workbench_module.Workbench,
    decisions: dict[str, dict[str, str]],
) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    for object_id, decision in decisions.items():
        representation = decision["master_representation_type"]
        if representation == "ordered_md_resource_set":
            resource_ids = workbench_module.parsed_resource_ids(decision)
            if resource_ids is None:
                raise PreflightError(f"invalid selected resource list for {object_id}")
            for resource_id in resource_ids:
                resource = instance.resources_by_id[resource_id]
                members.append(
                    {
                        "md_object_id": object_id,
                        "inventory": decision["inventory"],
                        "representation_type": representation,
                        "member_id": f"md-resource:{resource_id}",
                        "path": resource["work_path"],
                        "format": resource["format"].casefold(),
                        "size_bytes": int(resource["size_bytes"]),
                        "sha256": resource["sha256"].casefold(),
                        "page_count": resource["page_count"],
                        "width_px": resource["width_px"],
                        "height_px": resource["height_px"],
                    }
                )
        else:
            candidate = instance.candidates_by_id[decision["selected_candidate_id"]]
            members.append(
                {
                    "md_object_id": object_id,
                    "inventory": decision["inventory"],
                    "representation_type": representation,
                    "member_id": candidate["candidate_id"],
                    "path": candidate["candidate_path"],
                    "format": candidate["format"].casefold(),
                    "size_bytes": int(candidate["size_bytes"]),
                    "sha256": candidate["known_sha256"].casefold(),
                    "page_count": candidate["page_count"],
                    "width_px": candidate["width_px"],
                    "height_px": candidate["height_px"],
                }
            )
    return members


def pdf_page_count(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise PreflightError(f"pdfinfo did not report a page count: {path}")


def technical_error(member: dict[str, object], path: Path) -> str | None:
    file_format = str(member["format"])
    try:
        if file_format == "pdf":
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return "missing PDF signature"
            expected_pages = str(member["page_count"])
            actual_pages = pdf_page_count(path)
            if expected_pages and actual_pages != int(expected_pages):
                return f"page count {actual_pages} != {expected_pages}"
            return None
        with Image.open(path) as image:
            image.load()
            width, height = image.size
        expected_width = str(member["width_px"])
        expected_height = str(member["height_px"])
        if expected_width and width != int(expected_width):
            return f"width {width} != {expected_width}"
        if expected_height and height != int(expected_height):
            return f"height {height} != {expected_height}"
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return str(error)


def run_preflight(target_unit_name: str) -> dict[str, object]:
    if not UNIT_NAME.fullmatch(target_unit_name):
        raise PreflightError(
            "target unit name must contain lowercase letters, digits, and single hyphens only"
        )
    target_unit = ARCHIVE_UNITS / target_unit_name
    blockers: list[str] = []
    warnings: list[str] = []
    checks: dict[str, object] = {}

    instance = projection_workbench()
    summary = instance.summary
    decisions = instance.decisions()
    disposition_rows = workbench_module.read_csv(workbench_module.DISPOSITIONS_CSV)

    expected_counts = {
        "objects": 54,
        "resources": 66,
        "master_candidates": 244,
        "integrity_exceptions": 4,
    }
    count_errors = {
        key: {"expected": value, "actual": summary.get(key)}
        for key, value in expected_counts.items()
        if summary.get(key) != value
    }
    if len(instance.objects) != 54 or len(instance.objects_by_id) != 54:
        count_errors["object_rows"] = {
            "expected": 54,
            "actual": len(instance.objects),
        }
    if len(instance.candidates) != 244 or len(instance.candidates_by_id) != 244:
        count_errors["candidate_rows"] = {
            "expected": 244,
            "actual": len(instance.candidates),
        }
    if len(instance.resources) != 66 or len(instance.resources_by_id) != 66:
        count_errors["resource_rows"] = {
            "expected": 66,
            "actual": len(instance.resources),
        }
    checks["scope_counts"] = {"ok": not count_errors, "errors": count_errors}
    if count_errors:
        blockers.append("collection scope counts changed")

    decision_errors = instance.validate_decisions(decisions)
    expected_dispositions = instance.dispositions_for(decisions)
    disposition_errors: list[str] = []
    actual_by_id = {row.get("candidate_id", ""): row for row in disposition_rows}
    if len(actual_by_id) != len(disposition_rows):
        disposition_errors.append("candidate disposition identifiers are not unique")
    if len(disposition_rows) != len(instance.candidates):
        disposition_errors.append("candidate disposition coverage is incomplete")
    for expected in expected_dispositions:
        actual = actual_by_id.get(expected["candidate_id"])
        if actual != expected:
            disposition_errors.append(expected["candidate_id"])
    checks["master_decisions"] = {
        "ok": not decision_errors and len(decisions) == 54,
        "objects": len(decisions),
        "selected": sum(
            row.get("decision_state") == "selected_master" for row in decisions.values()
        ),
        "representation_types": dict(
            sorted(Counter(row["master_representation_type"] for row in decisions.values()).items())
        ),
        "errors": decision_errors,
    }
    checks["candidate_dispositions"] = {
        "ok": not disposition_errors,
        "counts": dict(sorted(Counter(row["disposition"] for row in disposition_rows).items())),
        "errors": disposition_errors,
    }
    if decision_errors or len(decisions) != 54:
        blockers.append("Master decisions are incomplete or invalid")
    if disposition_errors:
        blockers.append("candidate dispositions disagree with Master decisions")

    evidence_tables = {
        "objects": instance.objects,
        "candidates": instance.candidates,
        "resources": instance.resources,
        "scenarios": instance.scenarios,
        "relationships": instance.relationships,
        "integrity_exceptions": instance.exceptions,
        "title_leads": instance.title_leads,
        "page_scan_evidence": instance.page_scan_evidence,
        "decisions": list(decisions.values()),
        "candidate_dispositions": disposition_rows,
    }
    deletion_violations = {
        name: sum(row.get("deletion_authorized") != "false" for row in rows)
        for name, rows in evidence_tables.items()
    }
    deletion_violations = {
        name: count for name, count in deletion_violations.items() if count
    }
    checks["deletion_authority"] = {
        "ok": not deletion_violations,
        "violations": deletion_violations,
    }
    if deletion_violations:
        blockers.append("reconciliation evidence carries deletion authority")

    exception_paths = {row["source_path"] for row in instance.exceptions}
    candidate_paths = {row["candidate_path"] for row in instance.candidates}
    exception_overlap = sorted(exception_paths & candidate_paths)
    exception_state_errors = [
        row["source_path"]
        for row in instance.exceptions
        if row.get("disposition") != "hold_outside_master_and_drain_selection"
    ]
    checks["integrity_exceptions"] = {
        "ok": len(instance.exceptions) == 4
        and not exception_overlap
        and not exception_state_errors,
        "count": len(instance.exceptions),
        "candidate_overlap": exception_overlap,
        "state_errors": exception_state_errors,
    }
    if not checks["integrity_exceptions"]["ok"]:
        blockers.append("integrity exceptions are not safely isolated")

    required_metadata = ("inventory", "title", "object_type", "description", "object_url")
    missing_required = {
        field: [row["md_object_id"] for row in instance.objects if not row.get(field, "").strip()]
        for field in required_metadata
    }
    missing_required = {field: ids for field, ids in missing_required.items() if ids}
    missing_dates = [
        row["md_object_id"] for row in instance.objects if not row.get("date", "").strip()
    ]
    metadata_authority = summary.get("metadata_authority")
    checks["metadata"] = {
        "ok": not missing_required
        and metadata_authority == "museum-digital descriptive object records",
        "authority": metadata_authority,
        "missing_required": missing_required,
        "unknown_date_object_ids": missing_dates,
    }
    if not checks["metadata"]["ok"]:
        blockers.append("required descriptive metadata is incomplete or its authority changed")
    if missing_dates:
        warnings.append(
            "museum-digital supplies no date for object IDs " + ", ".join(missing_dates)
        )

    members = selected_members(instance, decisions)
    input_errors: list[dict[str, str]] = []
    snapshots: dict[Path, tuple[int, int]] = {}
    verified_bytes = 0
    verified_files = 0
    for member in members:
        path = Path(str(member["path"]))
        lexically_allowed = path.is_absolute() and (
            path.is_relative_to(SOURCE_ROOT) or path.is_relative_to(WORK_ROOT)
        )
        if not lexically_allowed:
            input_errors.append(
                {
                    "member_id": str(member["member_id"]),
                    "path": str(path),
                    "error": "path is outside the approved source/work roots",
                }
            )
            continue
        if not path.is_file():
            input_errors.append(
                {"member_id": str(member["member_id"]), "path": str(path), "error": "missing"}
            )
            continue
        if not (
            workbench_module.inside(path, SOURCE_ROOT)
            or workbench_module.inside(path, WORK_ROOT)
        ):
            input_errors.append(
                {
                    "member_id": str(member["member_id"]),
                    "path": str(path),
                    "error": "resolved path escapes the approved source/work roots",
                }
            )
            continue
        stat = path.stat()
        snapshots[path] = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size != int(member["size_bytes"]):
            input_errors.append(
                {
                    "member_id": str(member["member_id"]),
                    "path": str(path),
                    "error": f"size {stat.st_size} != {member['size_bytes']}",
                }
            )
            continue
        expected_hash = str(member["sha256"])
        if not HEX64.fullmatch(expected_hash):
            input_errors.append(
                {
                    "member_id": str(member["member_id"]),
                    "path": str(path),
                    "error": "invalid recorded SHA-256",
                }
            )
            continue
        actual_hash = sha256(path)
        if actual_hash != expected_hash:
            input_errors.append(
                {
                    "member_id": str(member["member_id"]),
                    "path": str(path),
                    "error": "SHA-256 mismatch",
                }
            )
            continue
        error = technical_error(member, path)
        if error:
            input_errors.append(
                {"member_id": str(member["member_id"]), "path": str(path), "error": error}
            )
            continue
        verified_files += 1
        verified_bytes += stat.st_size
    changed_during_check = [
        str(path)
        for path, before in snapshots.items()
        if not path.is_file() or (path.stat().st_size, path.stat().st_mtime_ns) != before
    ]
    if changed_during_check:
        for path in changed_during_check:
            input_errors.append(
                {"member_id": "", "path": path, "error": "changed during preflight"}
            )
    checks["selected_inputs"] = {
        "ok": not input_errors and verified_files == len(members),
        "master_representations": len(decisions),
        "representation_members": len(members),
        "expected_bytes": sum(int(member["size_bytes"]) for member in members),
        "verified_files": verified_files,
        "verified_bytes": verified_bytes,
        "errors": input_errors,
    }
    if input_errors or verified_files != len(members):
        blockers.append("one or more selected Master inputs are unavailable or invalid")

    try:
        mount = workbench_module.source_guard()
        checks["protected_source_mount"] = {"ok": True, "mount": mount}
    except (OSError, ValueError, subprocess.SubprocessError, workbench_module.WorkbenchError) as error:
        checks["protected_source_mount"] = {"ok": False, "error": str(error)}
        blockers.append("protected source mount guard failed")

    if checks["protected_source_mount"]["ok"]:
        try:
            authoritative = workbench_module.Workbench("accession-preflight").check()
            checks["authoritative_workbench_check"] = {
                "ok": True,
                "result": authoritative,
            }
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
            workbench_module.WorkbenchError,
        ) as error:
            checks["authoritative_workbench_check"] = {"ok": False, "error": str(error)}
            blockers.append("authoritative workbench check failed")
    else:
        checks["authoritative_workbench_check"] = {
            "ok": False,
            "error": "not run because the protected source mount guard failed",
        }

    present_dirs = {path.name for path in ARCHIVE_ROOT.iterdir() if path.is_dir()}
    present_files = {path.name for path in ARCHIVE_ROOT.iterdir() if path.is_file()}
    missing_dirs = sorted(REQUIRED_ARCHIVE_DIRS - present_dirs)
    missing_files = sorted(REQUIRED_ARCHIVE_FILES - present_files)
    unexpected_entries = sorted(
        (present_dirs - REQUIRED_ARCHIVE_DIRS) | (present_files - REQUIRED_ARCHIVE_FILES)
    )
    checks["archive_structure"] = {
        "ok": not missing_dirs
        and not missing_files
        and not unexpected_entries
        and os.access(ARCHIVE_ROOT, os.W_OK),
        "archive_root": str(ARCHIVE_ROOT),
        "writable": os.access(ARCHIVE_ROOT, os.W_OK),
        "missing_directories": missing_dirs,
        "missing_files": missing_files,
        "unexpected_entries": unexpected_entries,
    }
    if not checks["archive_structure"]["ok"]:
        blockers.append("archive root does not match the approved structure")

    target_exists = target_unit.exists()
    checks["target_unit"] = {
        "ok": not target_exists,
        "proposed_path": str(target_unit),
        "exists": target_exists,
        "created": False,
    }
    if target_exists:
        blockers.append("proposed target unit already exists")

    free_bytes = shutil.disk_usage(ARCHIVE_ROOT).free
    required_input_bytes = sum(int(member["size_bytes"]) for member in members)
    checks["destination_capacity"] = {
        "ok": free_bytes > required_input_bytes,
        "free_bytes": free_bytes,
        "selected_input_bytes": required_input_bytes,
        "note": "lower-bound check only; generated derivatives also require space",
    }
    if free_bytes <= required_input_bytes:
        blockers.append("archive destination lacks space for the selected inputs")

    required_tools = ("pdfinfo", "pdftoppm", "exiftool")
    tool_paths = {name: shutil.which(name) for name in required_tools}
    checks["existing_dependencies"] = {"ok": all(tool_paths.values()), "paths": tool_paths}
    if not checks["existing_dependencies"]["ok"]:
        blockers.append("an existing accession dependency is unavailable")

    accession_programs = sorted(
        str(path)
        for path in WORK_ROOT.glob("*.py")
        if "accession" in path.name and path.name != Path(__file__).name
    )
    checks["accession_implementation"] = {
        "ok": bool(accession_programs),
        "programs": accession_programs,
    }
    if not accession_programs:
        blockers.append("no collection-553 accession implementation exists")

    accession_authorized = summary.get("accession_authorized") is True
    checks["accession_authority"] = {
        "ok": accession_authorized,
        "authorized": accession_authorized,
        "note": "preflight approval is not accession approval",
    }
    if not accession_authorized:
        blockers.append("accession is not explicitly authorized")

    unique_blockers = list(dict.fromkeys(blockers))
    return {
        "format": "iss-collection-553-accession-preflight-v1",
        "checked_at": utc_now(),
        "mode": "read_only",
        "result": "ok" if not unique_blockers else "blocked",
        "accession_ready": not unique_blockers,
        "collection_id": 553,
        "target_unit_name_status": "proposed_only",
        "checks": checks,
        "warnings": warnings,
        "blockers": unique_blockers,
        "writes_performed": 0,
        "derivatives_generated": 0,
        "unit_created": False,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run the read-only preflight")
    parser.add_argument(
        "--target-unit-name",
        required=True,
        help="proposed flat unit directory name; no directory is created",
    )
    arguments = parser.parse_args()
    if not arguments.check:
        parser.error("use --check")
    try:
        result = run_preflight(arguments.target_unit_name)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, PreflightError) as error:
        print(json.dumps({"result": "error", "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["accession_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
