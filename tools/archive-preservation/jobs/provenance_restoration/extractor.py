#!/usr/bin/env python3
"""Extract one draft restoration bundle from retained read-only evidence."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from . import bundle as bundle_contract
    from .evidence import file_digest, file_fingerprint
    from .safety import ProtectedEvidenceError, require_safe_output
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bundle as bundle_contract
    from evidence import file_digest, file_fingerprint
    from safety import ProtectedEvidenceError, require_safe_output


MASTER_EXTENSIONS = {"tif", "tiff", "dng", "cr2", "psd"}
DERIVATIVE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
METADATA_EXTENSIONS = {"xml", "csv", "xlsx", "xls", "json", "lido"}
MD_ID_RE = re.compile(r"^\d+$")
MD_URL_ID_RE = re.compile(r"[?&](?:oges|object_id)=(\d+)")
REQUIRED_TABLES = {
    "objects",
    "files",
    "file_object_links",
    "object_sources",
    "online_md_objects",
    "online_md_images",
    "provenance_until_md",
}
class ProvenanceExtractionError(RuntimeError):
    """Raised when retained evidence cannot produce a truthful draft."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def connect_read_only(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ProvenanceExtractionError(f"retained catalog does not exist: {path}")
    uri_path = urllib.parse.quote(path.as_posix(), safe="/")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = REQUIRED_TABLES - tables
    if missing:
        connection.close()
        raise ProvenanceExtractionError(
            "retained catalog is missing required tables: "
            + ", ".join(sorted(missing))
        )
    return connection


def normalize_md_id(value: Any) -> str:
    text = str(value or "").strip()
    if MD_ID_RE.fullmatch(text):
        return text
    match = MD_URL_ID_RE.search(text)
    return match.group(1) if match else ""


def case_id(inventory_label: str) -> str:
    body = re.sub(r"[^A-Z0-9._-]+", "-", inventory_label.upper()).strip("-")
    if not body:
        raise ProvenanceExtractionError("inventory label cannot form a stable case ID")
    return f"RESTORE-{body}"


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...],
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _source(
    source_id: str,
    kind: str,
    locator: str,
    captured_at: str,
    snapshot: Any,
    note: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "kind": kind,
        "locator": locator,
        "captured_at": captured_at,
        "snapshot": snapshot,
        "snapshot_digest": bundle_contract.snapshot_digest(snapshot),
        "note": note,
    }


def _file_role(extension: str) -> str:
    extension = extension.casefold().lstrip(".")
    if extension in MASTER_EXTENSIONS:
        return "local_master_candidate"
    if extension in DERIVATIVE_EXTENSIONS:
        return "local_derivative"
    if extension in METADATA_EXTENSIONS:
        return "metadata_file"
    return ""


def extract_draft_bundle(
    catalog_path: Path,
    inventory_label: str,
) -> dict[str, Any]:
    """Read one case and return a decision-free, validated draft bundle."""

    inventory_label = inventory_label.strip()
    if not inventory_label:
        raise ProvenanceExtractionError("inventory label is required")
    catalog_path = catalog_path.expanduser().resolve()
    catalog_before = file_fingerprint(catalog_path)
    captured_at = utc_now()
    connection = connect_read_only(catalog_path)
    try:
        objects = _rows(
            connection,
            "SELECT * FROM objects WHERE inventory_number = ?",
            (inventory_label,),
        )
        if len(objects) != 1:
            raise ProvenanceExtractionError(
                f"expected one local object for {inventory_label!r}; "
                f"found {len(objects)}"
            )
        source_rows = _rows(
            connection,
            """SELECT * FROM object_sources
               WHERE inventory_number = ?
               ORDER BY source_path, row_number, id""",
            (inventory_label,),
        )
        linked_files = _rows(
            connection,
            """SELECT
                   l.id AS link_id,
                   l.inventory_number,
                   l.link_type,
                   l.confidence,
                   l.source AS link_source,
                   f.*
               FROM file_object_links l
               JOIN files f ON f.id = l.file_id
               WHERE l.inventory_number = ?
               ORDER BY f.rel_path, f.id""",
            (inventory_label,),
        )
        provenance = _rows(
            connection,
            """SELECT * FROM provenance_until_md
               WHERE inventory_number = ?""",
            (inventory_label,),
        )

        md_ids = {
            candidate
            for candidate in (
                normalize_md_id(objects[0].get("md_id")),
                *(
                    normalize_md_id(row.get("md_id"))
                    for row in source_rows
                ),
                *(
                    normalize_md_id(row.get("md_id"))
                    for row in provenance
                ),
            )
            if candidate
        }
        md_objects: list[dict[str, Any]] = []
        if md_ids:
            placeholders = ",".join("?" for _ in md_ids)
            md_objects = _rows(
                connection,
                f"""SELECT * FROM online_md_objects
                    WHERE md_id IN ({placeholders})
                    ORDER BY md_id""",
                tuple(sorted(md_ids)),
            )
        if not md_objects:
            md_objects = _rows(
                connection,
                """SELECT * FROM online_md_objects
                   WHERE normalized_inventory = ? OR inventory_number = ?
                   ORDER BY md_id""",
                (inventory_label, inventory_label),
            )
        resolved_md_ids = sorted(
            {
                normalize_md_id(row.get("md_id"))
                for row in md_objects
                if normalize_md_id(row.get("md_id"))
            }
        )
        md_images: list[dict[str, Any]] = []
        if resolved_md_ids:
            placeholders = ",".join("?" for _ in resolved_md_ids)
            md_images = _rows(
                connection,
                f"""SELECT * FROM online_md_images
                    WHERE md_id IN ({placeholders})
                    ORDER BY md_id, id""",
                tuple(resolved_md_ids),
            )
    finally:
        connection.close()

    catalog_after = file_fingerprint(catalog_path)
    if catalog_after != catalog_before:
        raise ProvenanceExtractionError(
            "retained catalog changed during read-only extraction"
        )

    local_snapshot = {
        "catalog_fingerprint": catalog_before,
        "objects": objects,
        "object_sources": source_rows,
        "file_links": linked_files,
        "provenance_until_md": provenance,
    }
    sources = [
        _source(
            "SOURCE-LOCAL-CATALOG",
            "local_catalog_row",
            f"sqlite:{catalog_path}#inventory={inventory_label}",
            captured_at,
            local_snapshot,
            "Exact rows captured through SQLite mode=ro&immutable=1.",
        )
    ]
    if md_objects:
        md_snapshot = {
            "objects": md_objects,
            "images": md_images,
        }
        sources.append(
            _source(
                "SOURCE-MUSEUM-DIGITAL",
                "museum_digital_snapshot",
                "museum-digital:objects/" + ",".join(resolved_md_ids),
                captured_at,
                md_snapshot,
                "Cached MD rows from the retained local restoration index; no network request.",
            )
        )

    files: list[dict[str, Any]] = []
    provenance_gaps: list[str] = []
    blocking_gaps: list[str] = []
    master_candidates = 0
    for row in linked_files:
        role = _file_role(str(row.get("extension") or ""))
        if not role:
            continue
        path = Path(str(row["path"]))
        file_id = f"FILE-LOCAL-{int(row['id'])}"
        if path.is_symlink():
            provenance_gaps.append(f"symlink_not_hashed:{file_id}")
            blocking_gaps.append(f"unverified_linked_file:{file_id}")
            continue
        if not path.is_file():
            provenance_gaps.append(f"missing_linked_file:{file_id}")
            blocking_gaps.append(f"missing_linked_file:{file_id}")
            continue
        before = file_fingerprint(path)
        digest = file_digest(path)
        after = file_fingerprint(path)
        if after != before:
            raise ProvenanceExtractionError(
                f"linked source changed while hashing: {path}"
            )
        source_id = f"SOURCE-LOCAL-FILE-{int(row['id'])}"
        sources.append(
            {
                "source_id": source_id,
                "kind": "local_filesystem",
                "locator": str(path),
                "captured_at": captured_at,
                "snapshot_digest": digest,
                "note": (
                    "Exact read-only file observation; database role is a "
                    "candidate until curatorial and technical review."
                ),
            }
        )
        files.append(
            {
                "file_id": file_id,
                "role": role,
                "source_id": source_id,
                "locator": str(path),
                "content_digest": digest,
                "size_bytes": int(before["size"]),
                "media_type": {
                    "local_master_candidate": "image/tiff",
                    "local_derivative": (
                        "image/png"
                        if str(row["extension"]).casefold() == "png"
                        else "image/webp"
                        if str(row["extension"]).casefold() == "webp"
                        else "image/jpeg"
                    ),
                    "metadata_file": "application/octet-stream",
                }[role],
                "note": (
                    f"retained link_type={row['link_type']}; "
                    f"confidence={row['confidence']}; source={row['link_source']}"
                ),
            }
        )
        master_candidates += int(role == "local_master_candidate")

    if not source_rows:
        provenance_gaps.append("local_metadata_not_found")
    if not md_objects:
        provenance_gaps.append("museum_digital_snapshot_not_found")
    if len(md_objects) > 1:
        blocking_gaps.append("multiple_museum_digital_candidates")
    if not master_candidates:
        provenance_gaps.append("missing_local_master")
    if not files:
        blocking_gaps.append("no_verified_local_file")

    local_search_status = "exact_inventory" if source_rows else "not_found"
    draft = {
        "format": bundle_contract.BUNDLE_FORMAT,
        "version": bundle_contract.BUNDLE_VERSION,
        "direction": bundle_contract.BUNDLE_DIRECTION,
        "case_id": case_id(inventory_label),
        "created_at": captured_at,
        "inventory_label": inventory_label,
        "local_search_status": local_search_status,
        "derivation": {
            "kind": "source_extraction",
            "parent_bundle_digest": "",
            "operation_snapshot": {
                "catalog": str(catalog_path),
                "inventory_label": inventory_label,
                "mode": "sqlite_ro_immutable_query_only",
            },
            "operation_digest": "",
        },
        "lineage": {
            "state": "draft",
            "draft_bundle_digest": "",
            "decision_digest": "",
            "decision_snapshot": {},
        },
        "sources": sources,
        "files": files,
        "identity": {
            "state": "pending",
            "outcome": "unresolved",
            "reason": "",
            "actor": "",
            "decided_at": "",
            "evidence_source_ids": [source["source_id"] for source in sources],
        },
        "metadata_decisions": [],
        "file_decision": {
            "state": "pending",
            "outcome": "unresolved",
            "selected_file_id": "",
            "verification_method": "",
            "verification_details": {},
            "reason": "",
            "actor": "",
            "decided_at": "",
        },
        "readiness": {
            "state": "draft",
            "file_state": "unresolved",
            "selected_file_id": "",
            "provenance_gaps": sorted(set(provenance_gaps)),
            "blocking_gaps": sorted(set(blocking_gaps)),
        },
    }
    draft["derivation"]["operation_digest"] = (
        bundle_contract.snapshot_digest(
            draft["derivation"]["operation_snapshot"]
        )
    )
    bundle_contract.validate_bundle(draft)
    return draft


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        output_path = require_safe_output(arguments.output)
        draft = extract_draft_bundle(
            arguments.catalog,
            arguments.inventory,
        )
        digest = bundle_contract.write_bundle(output_path, draft)
    except (
        ProvenanceExtractionError,
        ProtectedEvidenceError,
        bundle_contract.ProvenanceBundleError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(
            json.dumps(
                {"result": "fail", "error": str(error)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "result": "ok",
                "case_id": draft["case_id"],
                "bundle": str(output_path),
                "bundle_digest": digest,
                "readiness": draft["readiness"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
