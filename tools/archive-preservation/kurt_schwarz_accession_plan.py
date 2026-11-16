#!/usr/bin/env python3
"""Build the complete, no-copy Kurt Schwarz accession and USB disposition plan."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-kurt-schwarz-accession-plan-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / (
    "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
)
DEFAULT_HASHES = WORK_ROOT / (
    "work/source-hashes/runs/"
    "20260730T003000Z-kurt-schwarz-source-hashes"
)
DEFAULT_OBJECTS = WORK_ROOT / (
    "work/reconciliation/object-plans/"
    "20260730T104000Z-kurt-schwarz-object-plan-final"
)
DEFAULT_DEEP_REVIEW = WORK_ROOT / (
    "work/reconciliation/deep-review/"
    "20260730T100000Z-kurt-schwarz-deep-review-with-metadata"
)
DEFAULT_CONTAINER = WORK_ROOT / (
    "work/reconciliation/shared-containers/"
    "20260730T091000Z-google-drive-kurt-schwarz"
)
DEFAULT_SHARED_REVIEW = WORK_ROOT / (
    "work/reconciliation/shared-image-review/"
    "20260730T103000Z-google-drive-kurt-schwarz-image-review"
)
DEFAULT_SHARED_WORKBOOKS = WORK_ROOT / (
    "work/reconciliation/shared-workbooks/"
    "20260730T093000Z-google-drive-kurt-schwarz-workbooks"
)
DEFAULT_SUBDIVISIONS = WORK_ROOT / (
    "work/reconciliation/subdivisions/"
    "20260730T002000Z-kurt-schwarz-subdivisions"
)
DEFAULT_VISUAL_REVIEW = WORK_ROOT / (
    "work/reconciliation/visual-review/"
    "20260730T090000Z-kurt-schwarz-visual-review"
)
DEFAULT_MD = WORK_ROOT / (
    "work/reconciliation/museum-digital/runs/"
    "20260729T175740Z-kurt-schwarz"
)
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"}
DOCUMENT_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".txt",
    ".xml",
    ".json",
    ".jsonl",
    ".html",
    ".htm",
    ".sql",
    ".log",
    ".md",
    ".xmp",
    ".ini",
    ".cfg",
    ".yaml",
    ".yml",
    ".rtf",
    ".xlsx",
    ".xlsm",
    ".ods",
    ".docx",
    ".odt",
    ".pptx",
    ".xls",
    ".doc",
    ".ppt",
    ".pdf",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tgz",
    ".db",
    ".dbf",
    ".fpt",
    ".cdx",
    ".sqlite",
    ".lrcat",
    ".url",
    ".mp3",
}
REPRESENTATION_FIELDS = (
    "representation_key",
    "record_keys",
    "inventories",
    "role",
    "source_kind",
    "source_path",
    "source_member",
    "source_size_bytes",
    "source_sha256",
    "final_relative_path",
    "copy_policy",
    "basis",
    "relationship",
    "access_restriction",
)
DISPOSITION_FIELDS = (
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
)


class PlanError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise PlanError(f"path escaped allowed root: {resolved}") from error
    return resolved


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(16 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def safe_component(value: str) -> str:
    value = re.sub(r"[\x00-\x1f/\\]+", "_", value).strip(" .")
    return value or "unidentified"


def is_restricted(relative: str) -> bool:
    lower = relative.casefold()
    return any(
        value in lower
        for value in (
            "rechnung",
            "stundenabrechnung",
            "finanz",
            "mittelabruf",
            "mindestlohn",
            "arbeitszeugnis",
            "tätigkeiten-für-arbeitszeugnis",
            "personal",
        )
    )


def source_policy(relative: str, extension: str) -> tuple[bool, str, str]:
    lower = relative.casefold()
    filename = Path(relative).name.casefold()
    if (
        extension in {".download", ".tmp", ".ini"}
        or "/listviewimgs/" in lower
        or "/uploads/alle-md-bilder-als-960px-jpgs/" in lower
        or ("jahreskonferenz_2021/" in lower and "_files/" in lower)
        or filename
        in {
            "vg-bildkunst_reproduktionsrechte.csv",
            "folgerechte.csv",
            "onlinerechte.csv",
        }
    ):
        return False, "reviewed_noise", "generated_or_unrelated_reference_noise"
    if "/alle-ks-bilder_5000px/" in lower and extension in {".jpg", ".jpeg"}:
        return True, "access_image", "curated_5000px_access_copy"
    if extension in {".tif", ".tiff"}:
        return True, "preservation_source_tiff", "all_distinct_tiff_source_states_preserved"
    if extension == ".psd":
        return True, "preservation_source_psd", "editable_source_state_preserved"
    if extension in IMAGE_EXTENSIONS:
        return True, "project_image", "project_image_source_preserved"
    if extension in DOCUMENT_EXTENSIONS or not extension:
        return True, "project_document", "project_record_preserved"
    return True, "project_file", "bounded_project_source_preserved"


def joined(values: Iterable[str]) -> str:
    return "; ".join(sorted({value for value in values if value}))


def final_path_for(
    item: dict[str, Any],
    object_rows: list[dict[str, str]],
    *,
    selected: bool,
) -> tuple[str, str, str]:
    relative = item["relative_path"]
    extension = Path(relative).suffix.casefold()
    filename = safe_component(Path(relative).name)
    if object_rows:
        primary = sorted(
            object_rows,
            key=lambda row: (
                row["current_inventory"]
                or row["historical_inventory"]
                or row["record_key"]
            ).casefold(),
        )[0]
        inventory = (
            primary["current_inventory"]
            or primary["historical_inventory"]
            or primary["record_key"]
        )
        object_dir = safe_component(inventory)
        if selected and extension in IMAGE_EXTENSIONS:
            return (
                f"masters/objects/{object_dir}/data/{filename}",
                "preservation_master",
                "",
            )
        if extension in {".tif", ".tiff", ".psd"}:
            return (
                f"masters/objects/{object_dir}/data/variants/{filename}",
                "preservation_source_variant",
                "",
            )
        if extension in IMAGE_EXTENSIONS:
            return (
                f"export/access/objects/{object_dir}/{filename}",
                "access_or_working_image",
                "",
            )
    if extension in IMAGE_EXTENSIONS:
        area = "export/access/unassigned" if extension in {".jpg", ".jpeg", ".png", ".webp"} else "masters/unassigned"
        return f"{area}/{relative}", "unassigned_project_image", ""
    if is_restricted(relative):
        return (
            f"documents/restricted/usb/{relative}",
            "restricted_project_document",
            "restricted",
        )
    if "/uploads/" in relative.casefold() and extension in {
        ".csv",
        ".xlsx",
        ".xls",
        ".ods",
    }:
        return (
            f"export/md/historical-usb/{relative}",
            "museum_digital_submission_record",
            "",
        )
    return f"documents/usb/{relative}", "project_document", ""


def atomic_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]
) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build(
    discovery: Path,
    hashes: Path,
    objects_run: Path,
    deep_review: Path,
    container_run: Path,
    shared_review: Path,
    shared_workbooks: Path,
    subdivisions: Path,
    visual_review: Path,
    md_run: Path,
    output: Path,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    hash_summary_path = hashes / "summary.json"
    if not hash_summary_path.is_file():
        raise PlanError("exclusive source hash inventory is not complete")
    hash_summary = json.loads(hash_summary_path.read_text(encoding="utf-8"))
    if hash_summary["scope_rows"] != 14688:
        raise PlanError("exclusive source hash scope differs from discovery")

    object_path = objects_run / "object-plan-final.csv"
    object_rows = read_csv(object_path)
    object_by_key = {row["record_key"]: row for row in object_rows}
    if len(object_by_key) != len(object_rows):
        raise PlanError("final object plan contains duplicate record keys")
    selected_paths: dict[str, list[dict[str, str]]] = defaultdict(list)
    candidate_paths: dict[str, list[dict[str, str]]] = defaultdict(list)
    inventory_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in object_rows:
        if row["selected_source_path"]:
            selected_paths[row["selected_source_path"]].append(row)
        for value in row["all_candidate_paths"].split("; "):
            if value and "::" not in value:
                candidate_paths[value].append(row)
        for value in (
            row["current_inventory"],
            row["historical_inventory"],
            *row["batch_alias_inventories"].split("; "),
            *row["additional_alias_inventories"].split("; "),
        ):
            if value:
                inventory_index[value.casefold()].append(row)

    discovery_rows = {
        row["source_path"]: row
        for row in read_csv(discovery / "usb-exclusive-candidates.csv")
    }
    hash_rows = {
        row["source_path"]: row for row in read_csv(hashes / "source-hashes.csv")
    }
    if set(discovery_rows) != set(hash_rows):
        raise PlanError("exclusive discovery and hash path sets differ")

    items: list[dict[str, Any]] = []
    disposition_meta: dict[str, dict[str, Any]] = {}
    for source, candidate in discovery_rows.items():
        hashed = hash_rows[source]
        extension = candidate["extension"].casefold()
        include, policy_role, policy_basis = source_policy(
            candidate["relative_path"], extension
        )
        item = {
            "source_kind": "file",
            "source_path": source,
            "source_member": "",
            "relative_path": candidate["relative_path"],
            "size_bytes": int(candidate["size_bytes"]),
            "sha256": hashed["sha256"],
            "hash_state": hashed["hash_state"],
            "include": include and hashed["hash_state"] == "hashed",
            "policy_role": policy_role,
            "policy_basis": policy_basis,
            "relationship": "exclusive_usb_scope",
            "access_restriction": (
                "restricted" if is_restricted(candidate["relative_path"]) else ""
            ),
            "object_rows": [],
            "selected": source in selected_paths,
        }
        mapped = selected_paths.get(source) or candidate_paths.get(source)
        if not mapped and extension in IMAGE_EXTENSIONS:
            mapped = inventory_index.get(Path(source).stem.casefold(), [])
        item["object_rows"] = list({row["record_key"]: row for row in (mapped or [])}.values())
        items.append(item)
        disposition_meta[source] = {
            "source_kind": "file",
            "size_bytes": candidate["size_bytes"],
            "sha256": hashed["sha256"],
            "hash_state": hashed["hash_state"],
            "policy_role": policy_role,
            "policy_basis": policy_basis,
            "relationship": "exclusive_usb_scope",
            "object_rows": item["object_rows"],
        }

    # Selected shared sources and reviewed cross-project documents.
    deep_rows = read_csv(deep_review / "deep-hit-review.csv")
    selected_shared = {
        path
        for path in selected_paths
        if path not in discovery_rows and path.startswith("/media/")
    }
    for row in deep_rows:
        source = row["source_path"]
        include = row["unit_action"] == "copy_exact_document" or source in selected_shared
        if source in disposition_meta:
            continue
        path = Path(source)
        digest = sha256_file(path) if include else ""
        mapped = selected_paths.get(source, [])
        item = {
            "source_kind": "file",
            "source_path": source,
            "source_member": "",
            "relative_path": row["relative_path"],
            "size_bytes": path.stat().st_size,
            "sha256": digest,
            "hash_state": "hashed" if digest else "not_hashed_shared_hold",
            "include": include,
            "policy_role": (
                "preservation_source_tiff"
                if source in selected_shared
                else "contextual_document"
            ),
            "policy_basis": (
                "reviewed_selected_shared_master"
                if source in selected_shared
                else row["review_basis"]
            ),
            "relationship": row["relationship"],
            "access_restriction": (
                "restricted" if row["final_area"] == "documents/restricted" else ""
            ),
            "object_rows": mapped,
            "selected": source in selected_shared,
            "forced_final_area": row["final_area"],
            "deep_state": row["review_state"],
            "deep_action": row["unit_action"],
            "deep_drain": row["drain_state"],
        }
        items.append(item)
        disposition_meta[source] = {
            "source_kind": "file",
            "size_bytes": str(path.stat().st_size),
            "sha256": digest,
            "hash_state": item["hash_state"],
            "policy_role": item["policy_role"],
            "policy_basis": item["policy_basis"],
            "relationship": row["relationship"],
            "object_rows": mapped,
            "deep_state": row["review_state"],
            "deep_action": row["unit_action"],
            "deep_drain": row["drain_state"],
        }

    # Exact project documents inside the shared ZIP are accessioned; member
    # images remain represented by their hash/alias manifest and current masters.
    container_members = read_csv(container_run / "members.csv")
    reviewed_members = {
        row["member"]: row
        for row in read_csv(shared_review / "reviewed-shared-images.csv")
    }
    container_source = container_members[0]["source_container"]
    for member in container_members:
        reference = f"{container_source}::{member['member']}"
        include = member["source_role"] == "project_metadata_document"
        reviewed = reviewed_members.get(member["member"])
        mapped_rows = (
            [object_by_key[reviewed["accepted_record_key"]]]
            if reviewed and reviewed["accepted_record_key"] in object_by_key
            else []
        )
        relative = str(Path(*member["member"].split("/")[1:]))
        item = {
            "source_kind": "zip_member",
            "source_path": container_source,
            "source_member": member["member"],
            "relative_path": relative,
            "size_bytes": int(member["member_size_bytes"]),
            "sha256": member["member_sha256"],
            "hash_state": "hashed",
            "include": include,
            "policy_role": member["source_role"],
            "policy_basis": (
                "direct_project_document_in_shared_container"
                if include
                else "historical_access_derivative_manifested_with_alias"
            ),
            "relationship": member["relationship"],
            "access_restriction": (
                "restricted" if is_restricted(relative) else ""
            ),
            "object_rows": mapped_rows,
            "selected": False,
            "forced_final_area": "documents/google-drive",
        }
        items.append(item)
        disposition_meta[reference] = {
            "source_kind": "zip_member",
            "size_bytes": member["member_size_bytes"],
            "sha256": member["member_sha256"],
            "hash_state": "hashed",
            "policy_role": member["source_role"],
            "policy_basis": item["policy_basis"],
            "relationship": member["relationship"],
            "object_rows": mapped_rows,
            "member_include": include,
        }

    # Work evidence and current MD capture are authoritative unit records but
    # are not USB drain candidates.
    evidence_files = [
        object_path,
        shared_workbooks / "shared-workbook-records.csv",
        container_run / "members.csv",
        deep_review / "deep-hit-review.csv",
        shared_review / "reviewed-shared-images.csv",
        shared_review / "local-only-object-additions.csv",
        subdivisions / "subdivision-register.csv",
        subdivisions / "subdivision-memberships.csv",
        visual_review / "reviewed-visual-exceptions.csv",
        hashes / "source-hashes.csv",
        hashes / "summary.json",
    ]
    for path in sorted(md_run.iterdir()):
        if path.is_file():
            evidence_files.append(path)
    for path in evidence_files:
        if not path.is_file():
            raise PlanError(f"required evidence file is absent: {path}")
        if path.parent == md_run:
            final = f"export/md/current/{path.name}"
            role = "current_museum_digital_export"
        else:
            final = f"db-restore/manifests/evidence/{path.name}"
            role = "reconciliation_evidence"
        items.append(
            {
                "source_kind": "work_file",
                "source_path": str(path),
                "source_member": "",
                "relative_path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "hash_state": "hashed",
                "include": True,
                "policy_role": role,
                "policy_basis": "reviewed_current_control_record",
                "relationship": "unit_control_evidence",
                "access_restriction": "",
                "object_rows": [],
                "selected": False,
                "forced_final_path": final,
            }
        )
    md_recovery = WORK_ROOT / "work/md-image-recovery/51888/md-51888.jpg"
    recovery_object = selected_paths.get(str(md_recovery), [])
    items.append(
        {
            "source_kind": "work_file",
            "source_path": str(md_recovery),
            "source_member": "",
            "relative_path": md_recovery.name,
            "size_bytes": md_recovery.stat().st_size,
            "sha256": sha256_file(md_recovery),
            "hash_state": "hashed",
            "include": True,
            "policy_role": "museum_digital_recovery_master",
            "policy_basis": "current_md_only_full_image_recovery",
            "relationship": "current_museum_digital_object",
            "access_restriction": "",
            "object_rows": recovery_object,
            "selected": True,
        }
    )

    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item["include"]:
            if not item["sha256"]:
                raise PlanError(f"included item lacks SHA-256: {item['source_path']}")
            by_sha[item["sha256"]].append(item)
    representations: list[dict[str, Any]] = []
    final_by_sha: dict[str, str] = {}
    used_final: dict[str, str] = {}
    for digest, group in sorted(by_sha.items()):
        canonical = sorted(
            group,
            key=lambda item: (
                0 if item["selected"] else 1,
                0 if item["object_rows"] else 1,
                0 if "5000px" in item["relative_path"] else 1,
                len(item["relative_path"]),
                item["relative_path"],
            ),
        )[0]
        all_objects = {
            row["record_key"]: row
            for item in group
            for row in item["object_rows"]
        }
        if canonical.get("forced_final_path"):
            final = canonical["forced_final_path"]
            role = canonical["policy_role"]
            restriction = canonical["access_restriction"]
        elif canonical.get("forced_final_area"):
            final = (
                canonical["forced_final_area"].rstrip("/")
                + "/"
                + canonical["relative_path"]
            )
            role = canonical["policy_role"]
            restriction = canonical["access_restriction"]
        else:
            final, role, restriction = final_path_for(
                canonical,
                list(all_objects.values()),
                selected=any(item["selected"] for item in group),
            )
        if final in used_final and used_final[final] != digest:
            path = Path(final)
            final = str(
                path.with_name(f"{path.stem}--{digest[7:15]}{path.suffix}")
            )
        used_final[final] = digest
        final_by_sha[digest] = final
        inventories = [
            row["current_inventory"] or row["historical_inventory"]
            for row in all_objects.values()
        ]
        representations.append(
            {
                "representation_key": "sha256:" + digest.split(":", 1)[1],
                "record_keys": joined(all_objects),
                "inventories": joined(inventories),
                "role": role,
                "source_kind": canonical["source_kind"],
                "source_path": canonical["source_path"],
                "source_member": canonical["source_member"],
                "source_size_bytes": canonical["size_bytes"],
                "source_sha256": digest,
                "final_relative_path": final,
                "copy_policy": "exact_source_bytes",
                "basis": canonical["policy_basis"],
                "relationship": canonical["relationship"],
                "access_restriction": restriction,
            }
        )

    disposition: list[dict[str, Any]] = []
    for source, meta in disposition_meta.items():
        final = final_by_sha.get(meta["sha256"], "") if meta["sha256"] else ""
        inventories = [
            row["current_inventory"] or row["historical_inventory"]
            for row in meta["object_rows"]
        ]
        if meta["hash_state"] == "error":
            status = "source_error_logged"
            drain = "retain_unreadable_source"
            note = "Non-destructive source hash error; source remains untouched."
        elif final:
            status = "planned_exact_accession"
            drain = "pending_accession"
            note = "Final checksum and exact-copy proof are filled by accession."
        elif meta.get("deep_state"):
            status = "metadata_cataloged_source_retained"
            drain = meta.get("deep_drain", "retain_shared_source")
            note = "Cross-project or boundary source is not a Kurt unit drain target."
        elif meta.get("member_include") is False:
            status = "historical_derivative_manifested"
            drain = "retain_shared_container"
            note = "Member hash and alias retained; richer current/local master is accessioned."
        elif meta["policy_role"] == "reviewed_noise":
            status = "reviewed_noise_not_accessioned"
            drain = "reviewed_noise_pending_explicit_drain_decision"
            note = "No deletion is authorized by this classification."
        else:
            status = "retain_source_unresolved"
            drain = "retain_source"
            note = "No accepted final representation is assigned."
        disposition.append(
            {
                "source_path": source,
                "source_kind": meta["source_kind"],
                "unit": "kurt-schwarz",
                "inventory": joined(inventories),
                "source_role": meta["policy_role"],
                "final_path": final,
                "preservation_status": status,
                "preservation_basis": meta["policy_basis"],
                "source_sha256": meta["sha256"],
                "final_sha256": "",
                "source_size_bytes": meta["size_bytes"],
                "exact_copy_proven": "false",
                "drain_state": drain,
                "deletion_authorized": "false",
                "notes": note,
            }
        )
    disposition.sort(key=lambda row: row["source_path"])
    if len(disposition) != len({row["source_path"] for row in disposition}):
        raise PlanError("source disposition contains duplicate source paths")
    representations.sort(key=lambda row: row["final_relative_path"])
    atomic_csv(output / "objects.csv", tuple(object_rows[0]), object_rows)
    atomic_csv(
        output / "representations.csv", REPRESENTATION_FIELDS, representations
    )
    atomic_csv(
        output / "source-disposition.csv", DISPOSITION_FIELDS, disposition
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "object_rows": len(object_rows),
        "exclusive_source_rows": len(discovery_rows),
        "source_disposition_rows": len(disposition),
        "representation_rows": len(representations),
        "planned_payload_bytes": sum(
            int(row["source_size_bytes"]) for row in representations
        ),
        "representation_role_counts": dict(
            Counter(row["role"] for row in representations)
        ),
        "preservation_status_counts": dict(
            Counter(row["preservation_status"] for row in disposition)
        ),
        "hash_error_rows": sum(
            row["preservation_status"] == "source_error_logged"
            for row in disposition
        ),
        "fatal_safety_errors": 0,
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--hashes", type=Path, default=DEFAULT_HASHES)
    parser.add_argument("--objects", type=Path, default=DEFAULT_OBJECTS)
    parser.add_argument("--deep-review", type=Path, default=DEFAULT_DEEP_REVIEW)
    parser.add_argument("--container", type=Path, default=DEFAULT_CONTAINER)
    parser.add_argument("--shared-review", type=Path, default=DEFAULT_SHARED_REVIEW)
    parser.add_argument(
        "--shared-workbooks", type=Path, default=DEFAULT_SHARED_WORKBOOKS
    )
    parser.add_argument("--subdivisions", type=Path, default=DEFAULT_SUBDIVISIONS)
    parser.add_argument("--visual-review", type=Path, default=DEFAULT_VISUAL_REVIEW)
    parser.add_argument("--md-run", type=Path, default=DEFAULT_MD)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(
            args.discovery.expanduser().resolve(),
            args.hashes.expanduser().resolve(),
            args.objects.expanduser().resolve(),
            args.deep_review.expanduser().resolve(),
            args.container.expanduser().resolve(),
            args.shared_review.expanduser().resolve(),
            args.shared_workbooks.expanduser().resolve(),
            args.subdivisions.expanduser().resolve(),
            args.visual_review.expanduser().resolve(),
            args.md_run.expanduser().resolve(),
            output,
        )
    except (OSError, csv.Error, json.JSONDecodeError, ValueError, PlanError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
