#!/usr/bin/env python3
"""Accession the fully reconciled Objektsammlung collection-549 unit."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageOps


SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-549")
RECON = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
ARCHIVE_ROOT = Path("/mnt/volume/iss-archive")
UNIT = ARCHIVE_ROOT / "units/objektsammlung"
ALLOWED_DIRS = {"00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"}
DOCUMENT_FIELDS = [
    "document_id",
    "md_object_id",
    "inventory",
    "relative_path",
    "original_source_path",
    "sha256",
    "size_bytes",
    "source_occurrences",
    "provenance",
    "deletion_authorized",
]


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def guarded_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise AccessionError("Elements did not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise AccessionError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def safe_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return cleaned.strip("._") or "unnamed"


def copy_new(source: Path, destination: Path) -> None:
    if destination.exists():
        raise AccessionError(f"refusing to replace accession payload: {destination}")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, 8 * 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def access_derivative(source: Path, destination: Path) -> tuple[int, int]:
    if source.suffix.casefold() == ".pdf":
        prefix = destination.with_suffix("")
        subprocess.run(
            ["pdftoppm", "-f", "1", "-singlefile", "-jpeg", "-r", "150", str(source), str(prefix)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    else:
        with Image.open(source) as decoded:
            image = ImageOps.exif_transpose(decoded).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=88, optimize=True)
    with Image.open(destination) as check:
        check.load()
        return check.width, check.height


def image_dimensions(path: Path) -> tuple[str, str]:
    if path.suffix.casefold() == ".pdf":
        return "", ""
    with Image.open(path) as image:
        return str(image.width), str(image.height)


def main() -> int:
    created_at = now()
    mount_before = guarded_mount()
    if UNIT.exists():
        raise AccessionError(f"final unit already exists: {UNIT}")

    objects = read_csv(RECON / "objects.csv")
    resources = read_csv(RECON / "related-md-resources.csv")
    candidates = {row["candidate_id"]: row for row in read_csv(RECON / "master-candidates.csv")}
    decisions = read_csv(RECON / "master-decisions.csv")
    metadata_decisions = read_csv(RECON / "metadata-review.csv")
    drain = [
        row
        for row in read_csv(RECON / "drain-list.csv")
        if row["relationship"] != "related_local_documentation"
    ]
    if len(objects) != 10 or len(resources) != 18 or len(decisions) != 10:
        raise AccessionError("collection scope is not exactly 10 objects and 18 MD resources")
    if any(row["decision_state"] != "selected_master" for row in decisions):
        raise AccessionError("all ten objects must have a selected Master")
    if any(row["decision"] != "keep_current" or row["final_value"] != row["current_value"] for row in metadata_decisions):
        raise AccessionError("museum-digital metadata decisions are incomplete or altered")
    if any(row["deletion_authorized"] != "false" for row in decisions + metadata_decisions + drain):
        raise AccessionError("an input unexpectedly carries deletion authority")

    source_snapshot: dict[str, tuple[int, int]] = {}
    for row in drain:
        path = Path(row["source_path"])
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise AccessionError(f"local trace size changed: {path}")
        source_snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)

    for name in ALLOWED_DIRS:
        (UNIT / name).mkdir(parents=True, exist_ok=False)

    object_by_id = {row["md_object_id"]: row for row in objects}
    resource_by_object: dict[str, list[dict[str, str]]] = {}
    for row in resources:
        resource_by_object.setdefault(row["md_object_id"], []).append(row)

    representation_rows: list[dict[str, object]] = []
    catalogue_rows: list[dict[str, object]] = []
    master_final_by_object: dict[str, str] = {}
    selected_candidate_by_object: dict[str, str] = {}
    final_hashes: dict[str, str] = {}

    def final_sha(path: Path) -> str:
        key = str(path)
        if key not in final_hashes:
            final_hashes[key] = sha256(path)
        return final_hashes[key]

    for decision in sorted(decisions, key=lambda row: row["inventory"].casefold()):
        object_id = decision["md_object_id"]
        obj = object_by_id[object_id]
        candidate = candidates[decision["selected_candidate_id"]]
        source = Path(candidate["candidate_path"])
        if not source.is_file():
            raise AccessionError(f"selected Master is unavailable: {source}")
        suffix = source.suffix.casefold() or ".bin"
        master_name = f"{safe_name(decision['inventory'])}__md-{object_id}{suffix}"
        master_relative = f"masters/{master_name}"
        master_final = UNIT / master_relative
        copy_new(source, master_final)
        master_hash = final_sha(master_final)
        expected_master_hash = candidate["known_sha256"].removeprefix("sha256:")
        if expected_master_hash and master_hash != expected_master_hash:
            raise AccessionError(f"selected Master hash changed since reconciliation: {source}")
        width, height = image_dimensions(master_final)
        master_final_by_object[object_id] = master_relative
        selected_candidate_by_object[object_id] = decision["selected_candidate_id"]
        main_resource = next((row for row in resource_by_object[object_id] if row["is_main"] == "true"), None)
        representation_rows.append(
            {
                "md_object_id": object_id,
                "inventory": decision["inventory"],
                "role": "selected_master",
                "format": suffix.lstrip(".").upper(),
                "relative_path": master_relative,
                "original_source_path": str(source),
                "source_url": main_resource["source_url"] if decision["selected_candidate_id"].startswith("md-") and main_resource else "",
                "sha256": master_hash,
                "size_bytes": master_final.stat().st_size,
                "width_px": width,
                "height_px": height,
                "derivative_of": "",
                "provenance": candidate["candidate_role"],
                "deletion_authorized": "false",
            }
        )
        access_relative = f"export/{safe_name(decision['inventory'])}__md-{object_id}__access.jpg"
        access_final = UNIT / access_relative
        access_width, access_height = access_derivative(master_final, access_final)
        representation_rows.append(
            {
                "md_object_id": object_id,
                "inventory": decision["inventory"],
                "role": "generated_access_derivative",
                "format": "JPEG",
                "relative_path": access_relative,
                "original_source_path": str(source),
                "source_url": "",
                "sha256": final_sha(access_final),
                "size_bytes": access_final.stat().st_size,
                "width_px": access_width,
                "height_px": access_height,
                "derivative_of": master_relative,
                "provenance": "generated during accession from selected Master",
                "deletion_authorized": "false",
            }
        )
        catalogue_rows.append(
            {
                "archival_object_id": decision["inventory"],
                "md_object_id": object_id,
                "inventory": decision["inventory"],
                "title": obj["title"],
                "object_type": obj["object_type"],
                "selected_master_path": master_relative,
                "access_derivative_path": access_relative,
                "metadata_authority": "objektsammlung unit; museum-digital is an external projection",
                "deletion_authorized": "false",
            }
        )

    for resource in sorted(resources, key=lambda row: (row["md_object_id"], int(row["resource_position"]))):
        selected = candidates[selected_candidate_by_object[resource["md_object_id"]]]
        if Path(resource["work_path"]) == Path(selected["candidate_path"]):
            continue
        suffix = Path(resource["work_path"]).suffix.casefold() or ".bin"
        name = (
            f"{safe_name(resource['inventory'])}__md-{resource['md_object_id']}"
            f"__related-{int(resource['resource_position']):02d}__resource-{resource['resource_id']}{suffix}"
        )
        relative = f"export/{name}"
        final = UNIT / relative
        copy_new(Path(resource["work_path"]), final)
        width, height = image_dimensions(final)
        related_hash = final_sha(final)
        if related_hash != resource["sha256"].removeprefix("sha256:"):
            raise AccessionError(f"museum-digital resource hash changed: {resource['work_path']}")
        representation_rows.append(
            {
                "md_object_id": resource["md_object_id"],
                "inventory": resource["inventory"],
                "role": "related_museum_digital_projection_resource",
                "format": resource["format"].upper(),
                "relative_path": relative,
                "original_source_path": resource["work_path"],
                "source_url": resource["source_url"],
                "sha256": related_hash,
                "size_bytes": final.stat().st_size,
                "width_px": width,
                "height_px": height,
                "derivative_of": "",
                "provenance": resource["resource_role"],
                "deletion_authorized": "false",
            }
        )

    retained_rows = [row for row in drain if row["relationship"] == "local_image_trace"]
    source_hashes: dict[str, str] = {}
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in retained_rows:
        path = Path(row["source_path"])
        digest = sha256(path)
        source_hashes[str(path)] = digest
        groups.setdefault((row["source_kind"], digest), []).append(row)

    local_final_by_source: dict[str, str] = {}
    document_rows: list[dict[str, object]] = []
    document_index = 0
    related_index = 0
    for (kind, digest), group in sorted(groups.items(), key=lambda item: item[1][0]["source_path"].casefold()):
        source = Path(sorted(group, key=lambda row: row["source_path"].casefold())[0]["source_path"])
        object_id = group[0]["md_object_id"]
        inventory = group[0]["inventory"]
        if kind == "image":
            related_index += 1
            name = f"{safe_name(inventory)}__related-local-{related_index:03d}__{safe_name(source.name)}"
            relative = f"export/{name}"
        else:
            document_index += 1
            name = f"DOC-{document_index:04d}__{safe_name(source.name)}"
            relative = f"documents/{name}"
        final = UNIT / relative
        copy_new(source, final)
        final_hash = final_sha(final)
        if final_hash != digest:
            raise AccessionError(f"copied local trace hash mismatch: {source}")
        for row in group:
            local_final_by_source[row["source_path"]] = relative
        if kind == "image":
            width, height = image_dimensions(final)
            representation_rows.append(
                {
                    "md_object_id": object_id,
                    "inventory": inventory,
                    "role": "retained_unique_local_related_image",
                    "format": source.suffix.lstrip(".").upper(),
                    "relative_path": relative,
                    "original_source_path": str(source),
                    "source_url": "",
                    "sha256": final_hash,
                    "size_bytes": final.stat().st_size,
                    "width_px": width,
                    "height_px": height,
                    "derivative_of": "",
                    "provenance": "unique local trace retained before source drain proposal",
                    "deletion_authorized": "false",
                }
            )
        else:
            document_rows.append(
                {
                    "document_id": f"DOC-{document_index:04d}",
                    "md_object_id": object_id,
                    "inventory": inventory,
                    "relative_path": relative,
                    "original_source_path": str(source),
                    "sha256": final_hash,
                    "size_bytes": final.stat().st_size,
                    "source_occurrences": len(group),
                    "provenance": "local documentation retained unchanged",
                    "deletion_authorized": "false",
                }
            )

    drain_fields = list(drain[0])
    for row in drain:
        source = row["source_path"]
        if row["relationship"] == "selected_master_source" or row["relationship"] == "redundant_local_copy":
            final_path = master_final_by_object[row["md_object_id"]]
            coverage = "exact_master_copy_validated"
        elif source in local_final_by_source:
            final_path = local_final_by_source[source]
            coverage = "unique_content_retained_or_exact_duplicate_mapped"
        else:
            final_path = ""
            coverage = "generated_cache_redundant"
        row["source_sha256"] = (
            "sha256:" + source_hashes[source] if source in source_hashes else row["source_sha256"]
        )
        row["covered_by_unit"] = "true"
        row["coverage_state"] = coverage
        row["final_archive_path"] = final_path
        row["drain_state"] = "proposed_redundant_after_validated_accession"
        row["deletion_authorized"] = "false"
        row["note"] = "Covered by the validated Objektsammlung unit; source remains protected and deletion is not authorized."

    metadata_object_rows = [dict(row, master_decision_state="selected_master") for row in objects]
    write_csv(
        UNIT / "metadata/museum-digital-objects.csv",
        list(metadata_object_rows[0]),
        metadata_object_rows,
    )
    shutil.copy2(RECON / "related-md-resources.csv", UNIT / "metadata/museum-digital-resources.csv")
    shutil.copy2(RECON / "metadata-review.csv", UNIT / "metadata/metadata-decisions.csv")
    write_csv(UNIT / "CATALOG.csv", list(catalogue_rows[0]), catalogue_rows)
    write_csv(UNIT / "00_MANIFESTS/representations.csv", list(representation_rows[0]), representation_rows)
    write_csv(UNIT / "00_MANIFESTS/documents.csv", DOCUMENT_FIELDS, document_rows)
    write_csv(UNIT / "00_MANIFESTS/drain-list.csv", drain_fields, drain)
    write_csv(UNIT / "00_MANIFESTS/source-disposition.csv", drain_fields, drain)

    database = UNIT / "db-restore/RESTORE.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE objects (
            archival_object_id TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL UNIQUE,
            inventory TEXT NOT NULL,
            title TEXT NOT NULL,
            object_type TEXT NOT NULL,
            selected_master_path TEXT NOT NULL UNIQUE,
            access_derivative_path TEXT NOT NULL UNIQUE,
            metadata_authority TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE representations (
            relative_path TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL REFERENCES objects(md_object_id),
            inventory TEXT NOT NULL,
            role TEXT NOT NULL,
            format TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            derivative_of TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL REFERENCES objects(md_object_id),
            relative_path TEXT NOT NULL UNIQUE,
            original_source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE source_disposition (
            source_path TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL REFERENCES objects(md_object_id),
            relationship TEXT NOT NULL,
            final_archive_path TEXT NOT NULL,
            drain_state TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        """
    )
    connection.executemany(
        "INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,0)",
        [(row["archival_object_id"], row["md_object_id"], row["inventory"], row["title"], row["object_type"], row["selected_master_path"], row["access_derivative_path"], row["metadata_authority"]) for row in catalogue_rows],
    )
    connection.executemany(
        "INSERT INTO representations VALUES (?,?,?,?,?,?,?,?,0)",
        [(row["relative_path"], row["md_object_id"], row["inventory"], row["role"], row["format"], row["sha256"], int(row["size_bytes"]), row["derivative_of"]) for row in representation_rows],
    )
    connection.executemany(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,0)",
        [(row["document_id"], row["md_object_id"], row["relative_path"], row["original_source_path"], row["sha256"], int(row["size_bytes"])) for row in document_rows],
    )
    connection.executemany(
        "INSERT INTO source_disposition VALUES (?,?,?,?,?,0)",
        [(row["source_path"], row["md_object_id"], row["relationship"], row["final_archive_path"], row["drain_state"]) for row in drain],
    )
    connection.commit()
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    dump = "\n".join(connection.iterdump()) + "\n"
    connection.close()
    if quick_check != "ok" or foreign_keys:
        raise AccessionError(f"SQLite validation failed: quick_check={quick_check}, foreign_keys={foreign_keys}")
    (UNIT / "db-restore/RESTORE.sql").write_text(dump, encoding="utf-8")
    write_csv(UNIT / "db-restore/RESTORE.csv", list(catalogue_rows[0]), catalogue_rows)

    (UNIT / "00_README.md").write_text(
        "# Objektsammlung\n\n"
        "This unit is the human-readable archival authority for museum-digital collection 549. "
        "Museum-digital is retained as an external projection and provenance source.\n\n"
        "Each of the ten objects has exactly one selected Master in `masters/`. Access derivatives "
        "generated during accession and retained related resources are in `export/`. No local documentation "
        "is accessioned in this unit. Museum-digital-derived metadata and review decisions in `metadata/` "
        "are supporting evidence. Essential decisions are in `CATALOG.csv` and "
        "`00_MANIFESTS/`; SQLite is rebuildable catalogue support only.\n\n"
        "Source paths remain read-only. Drain rows are proposals and carry `deletion_authorized=false`.\n",
        encoding="utf-8",
    )

    payload_rows: list[dict[str, object]] = []
    for directory in ("masters", "export", "metadata", "documents"):
        for path in sorted((UNIT / directory).iterdir(), key=lambda item: item.name.casefold()):
            if path.is_file():
                payload_rows.append({"relative_path": str(path.relative_to(UNIT)), "sha256": final_sha(path), "size_bytes": path.stat().st_size})
    write_csv(UNIT / "00_MANIFESTS/payload-checksums.csv", list(payload_rows[0]), payload_rows)

    current_top = {path.name for path in UNIT.iterdir() if path.is_dir()}
    if current_top != ALLOWED_DIRS:
        raise AccessionError(f"unexpected unit directories: {sorted(current_top)}")
    if Counter(row["role"] for row in representation_rows)["selected_master"] != 10:
        raise AccessionError("unit does not have exactly ten selected Masters")
    if len({row["md_object_id"] for row in representation_rows if row["role"] == "selected_master"}) != 10:
        raise AccessionError("unit does not have exactly one Master per object")
    if any(final_hashes[str(UNIT / str(row["relative_path"]))] != row["sha256"] for row in payload_rows):
        raise AccessionError("payload checksum projection disagreement")
    for source, before in source_snapshot.items():
        stat = Path(source).stat()
        if (stat.st_size, stat.st_mtime_ns) != before:
            raise AccessionError(f"protected source changed during accession: {source}")
    mount_after = guarded_mount()
    if mount_after != mount_before:
        raise AccessionError("protected source mount changed during accession")

    completion = {
        "format": "iss-objektsammlung-accession-completion-v1",
        "started_at": created_at,
        "completed_at": now(),
        "result": "ok",
        "unit": str(UNIT),
        "objects": len(catalogue_rows),
        "selected_masters": Counter(row["role"] for row in representation_rows)["selected_master"],
        "generated_access_derivatives": Counter(row["role"] for row in representation_rows)["generated_access_derivative"],
        "related_museum_digital_resources": Counter(row["role"] for row in representation_rows)["related_museum_digital_projection_resource"],
        "retained_unique_local_related_images": Counter(row["role"] for row in representation_rows)["retained_unique_local_related_image"],
        "retained_unique_documents": len(document_rows),
        "source_disposition_rows": len(drain),
        "payload_files": len(payload_rows),
        "payload_bytes": sum(int(row["size_bytes"]) for row in payload_rows),
        "sqlite_quick_check": quick_check,
        "sqlite_foreign_key_violations": len(foreign_keys),
        "metadata_authority": "objektsammlung unit",
        "museum_digital_role": "external projection",
        "source_mount": mount_after,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
    }
    write_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    atomic_csv(RECON / "drain-list.csv", drain_fields, drain)
    print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
