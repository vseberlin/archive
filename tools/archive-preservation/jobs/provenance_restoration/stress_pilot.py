#!/usr/bin/env python3
"""Build and execute one bounded, manifest-driven archive stress pilot.

The job reads retained evidence and protected source files, but it may write
only to a fresh pilot root below the configured pilot workspace and to a fresh
independent root. It never replaces an existing path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import tiff_accession
import tiff_metadata

if __package__:
    from . import surrogate_accession
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import surrogate_accession


FORMAT = "iss-provenance-stress-pilot-v1"
REPORT_FORMAT = "iss-provenance-stress-pilot-report-v1"
TOOL_VERSION = "provenance-stress-pilot-0.1.0"
MIN_CASES = 20
MAX_CASES = 30
DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
PROTECTED_SOURCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
)
DEFAULT_PILOT_PARENT = Path(
    "/mnt/volume/archive-pilot-work/minimal-core-v2/work/stress-pilots"
)
DEFAULT_INDEPENDENT_PARENT = Path(
    "/home/vladimir/archive-core-v2-preservation/stress-pilots"
)


class StressPilotError(RuntimeError):
    """Raised when a stress pilot cannot remain inside its frozen boundary."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256(path),
    }


def _is_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def _fresh_output(path: Path, parent: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    allowed = parent.expanduser().resolve()
    if not _is_below(resolved, allowed):
        raise StressPilotError(f"{label} must be below {allowed}")
    if resolved.exists():
        raise StressPilotError(f"refusing to replace existing {label}: {resolved}")
    return resolved


def _protected_source(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise StressPilotError(f"source is not a regular file: {resolved}")
    if not any(
        _is_below(resolved, root.resolve()) for root in PROTECTED_SOURCE_ROOTS
    ):
        raise StressPilotError(
            f"source is outside protected evidence roots: {resolved}"
        )
    return resolved


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _exclusive_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(value)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _index_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{resolved}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _case_metadata(row: sqlite3.Row) -> dict[str, str]:
    local_title = str(row["local_title"] or "").strip()
    local_description = str(row["local_description"] or "").strip()
    md_title = str(row["md_title"] or "").strip()
    md_description = str(row["md_description"] or "").strip()
    return {
        "descriptive_title": local_title or md_title,
        "description": local_description or md_description,
        "material_technique": str(row["md_material"] or "").strip(),
        "dimensions": str(row["md_dimensions"] or "").strip(),
        "metadata_rule": (
            "local_then_museum_digital_fallback"
            if local_title or local_description
            else "museum_digital_only"
        ),
    }


def _base_query() -> str:
    return """
        SELECT p.inventory_number, p.md_id, p.chain_status,
               p.local_title, COALESCE(o.description, '') AS local_description,
               p.md_title,
               COALESCE(m.object_description, '') AS md_description,
               COALESCE(
                   json_extract(m.raw_json, '$.object_material_technique'),
                   ''
               ) AS md_material,
               COALESCE(
                   json_extract(m.raw_json, '$.object_dimensions'),
                   ''
               ) AS md_dimensions
        FROM provenance_until_md p
        LEFT JOIN objects o ON o.inventory_number = p.inventory_number
        LEFT JOIN online_md_objects m ON m.md_id = p.md_id
    """


def _linked_cases(
    connection: sqlite3.Connection,
    *,
    status: str,
    extensions: tuple[str, ...],
    count: int,
    largest: bool,
    excluded_paths: set[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in extensions)
    direction = "DESC" if largest else "ASC"
    query = (
        "WITH candidates AS ("
        + _base_query().replace(
            "FROM provenance_until_md p",
            """,
               f.path, f.size, f.extension
        FROM provenance_until_md p""",
        )
        + f"""
          JOIN file_object_links l
            ON l.inventory_number = p.inventory_number
          JOIN files f ON f.id = l.file_id
          WHERE p.chain_status = ?
            AND l.confidence = 95
            AND lower(f.extension) IN ({placeholders})
            AND f.path LIKE '/mnt/volume/archive/%'
        ), ranked AS (
          SELECT *, ROW_NUMBER() OVER (
              PARTITION BY inventory_number
              ORDER BY size {direction}, path
          ) AS choice
          FROM candidates
        )
        SELECT * FROM ranked
        WHERE choice = 1
        ORDER BY size {direction}, inventory_number
        LIMIT ?
        """
    )
    rows = connection.execute(
        query,
        (status, *extensions, count * 10),
    ).fetchall()
    result = []
    for row in rows:
        source = _protected_source(Path(str(row["path"])))
        if str(source) in excluded_paths:
            continue
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(source.suffix.casefold(), "image/tiff")
        result.append(
            {
                "inventory_label": str(row["inventory_number"]),
                "museum_digital_id": str(row["md_id"] or ""),
                "scenario": status,
                "action": (
                    "tiff_master"
                    if media_type == "image/tiff"
                    else "image_surrogate"
                ),
                "source_path": str(source),
                "source_media_type": media_type,
                "metadata": _case_metadata(row),
            }
        )
        excluded_paths.add(str(source))
        if len(result) == count:
            break
    if len(result) != count:
        raise StressPilotError(
            f"retained index supplied {len(result)} of {count} {status} cases"
        )
    return result


def _metadata_only_cases(
    connection: sqlite3.Connection,
    count: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        _base_query()
        + """
          WHERE p.chain_status = 'md_ok_no_local_file_link'
          ORDER BY p.inventory_number
          LIMIT ?
        """,
        (count,),
    ).fetchall()
    if len(rows) != count:
        raise StressPilotError(
            f"retained index supplied {len(rows)} of {count} metadata-only cases"
        )
    return [
        {
            "inventory_label": str(row["inventory_number"]),
            "museum_digital_id": str(row["md_id"]),
            "scenario": "md_ok_no_local_file_link",
            "action": "metadata_only",
            "source_path": "",
            "source_media_type": "",
            "metadata": _case_metadata(row),
        }
        for row in rows
    ]


def build_manifest(
    retained_index: Path,
    output: Path,
    pilot_root: Path,
    independent_root: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    """Select the frozen 22-case matrix from the retained read-only index."""

    output = output.expanduser().resolve()
    if output.exists():
        raise StressPilotError(f"refusing to replace manifest: {output}")
    pilot = _fresh_output(pilot_root, DEFAULT_PILOT_PARENT, "pilot root")
    independent = _fresh_output(
        independent_root,
        DEFAULT_INDEPENDENT_PARENT,
        "independent root",
    )
    with closing(_index_connection(retained_index)) as connection:
        cases = []
        selected_paths: set[str] = set()
        cases.extend(
            _linked_cases(
                connection,
                status="local_master_to_md",
                extensions=("tif", "tiff"),
                count=8,
                largest=False,
                excluded_paths=selected_paths,
            )
        )
        cases.extend(
            _linked_cases(
                connection,
                status="local_without_md_id",
                extensions=("tif", "tiff"),
                count=4,
                largest=False,
                excluded_paths=selected_paths,
            )
        )
        cases.extend(
            _linked_cases(
                connection,
                status="md_ok_no_local_master_link",
                extensions=("jpg", "jpeg", "png", "webp"),
                count=6,
                largest=True,
                excluded_paths=selected_paths,
            )
        )
        cases.extend(_metadata_only_cases(connection, 4))
    for index, case in enumerate(cases, 1):
        case["case_id"] = f"STRESS-{index:02d}-{case['inventory_label']}"
        case["unit_id"] = f"STRESS-{index:02d}-{case['inventory_label']}".upper()
    manifest = {
        "format": FORMAT,
        "created_at": _utc_now(),
        "purpose": "disposable pilot evidence; not curatorial acceptance",
        "retained_index": _fingerprint(retained_index.expanduser().resolve()),
        "pilot_root": str(pilot),
        "independent_root": str(independent),
        "limits": {
            "minimum_cases": MIN_CASES,
            "maximum_cases": MAX_CASES,
            "maximum_source_bytes": max_source_bytes,
        },
        "write_allowlist": [str(pilot), str(independent)],
        "protected_source_roots": [
            str(path.resolve()) for path in PROTECTED_SOURCE_ROOTS
        ],
        "cases": cases,
    }
    preflight = validate_manifest(manifest, include_fingerprints=True)
    manifest["preflight"] = preflight
    _exclusive_json(output, manifest)
    return {
        "result": "prepared",
        "manifest": str(output),
        "manifest_sha256": _sha256(output),
        **preflight,
    }


def read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        manifest = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StressPilotError(f"cannot read stress-pilot manifest: {resolved}") from error
    if not isinstance(manifest, dict):
        raise StressPilotError("stress-pilot manifest must be one JSON object")
    return manifest, _sha256(resolved)


def validate_manifest(
    manifest: dict[str, Any],
    *,
    include_fingerprints: bool,
) -> dict[str, Any]:
    if manifest.get("format") != FORMAT:
        raise StressPilotError("stress-pilot manifest format is invalid")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not MIN_CASES <= len(cases) <= MAX_CASES:
        raise StressPilotError("stress pilot requires 20-30 cases")
    _fresh_output(
        Path(str(manifest.get("pilot_root") or "")),
        DEFAULT_PILOT_PARENT,
        "pilot root",
    )
    _fresh_output(
        Path(str(manifest.get("independent_root") or "")),
        DEFAULT_INDEPENDENT_PARENT,
        "independent root",
    )
    unit_ids: set[str] = set()
    case_ids: set[str] = set()
    source_bytes = 0
    fingerprints: dict[str, dict[str, Any]] = {}
    action_counts: dict[str, int] = {}
    source_paths: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise StressPilotError("stress-pilot case must be an object")
        case_id = str(case.get("case_id") or "")
        unit_id = str(case.get("unit_id") or "")
        inventory = str(case.get("inventory_label") or "")
        action = str(case.get("action") or "")
        if not case_id or case_id in case_ids:
            raise StressPilotError(f"duplicate or empty case ID: {case_id}")
        if not unit_id or unit_id in unit_ids:
            raise StressPilotError(f"duplicate or empty unit ID: {unit_id}")
        if not inventory:
            raise StressPilotError(f"case has no inventory label: {case_id}")
        if action not in {"tiff_master", "image_surrogate", "metadata_only"}:
            raise StressPilotError(f"unsupported case action: {action}")
        case_ids.add(case_id)
        unit_ids.add(unit_id)
        action_counts[action] = action_counts.get(action, 0) + 1
        source_text = str(case.get("source_path") or "")
        if action == "metadata_only":
            if source_text:
                raise StressPilotError("metadata-only case must not name a source file")
            continue
        source = _protected_source(Path(source_text))
        if str(source) in source_paths:
            raise StressPilotError(f"duplicate source path: {source}")
        source_paths.add(str(source))
        if action == "tiff_master" and source.suffix.casefold() not in {
            ".tif",
            ".tiff",
        }:
            raise StressPilotError(f"TIFF case has a non-TIFF source: {source}")
        source_bytes += source.stat().st_size
        if include_fingerprints:
            fingerprints[case_id] = _fingerprint(source)
    maximum = int(
        dict(manifest.get("limits") or {}).get(
            "maximum_source_bytes",
            DEFAULT_MAX_SOURCE_BYTES,
        )
    )
    if source_bytes > maximum:
        raise StressPilotError(
            f"source byte cap exceeded: {source_bytes} > {maximum}"
        )
    return {
        "cases": len(cases),
        "action_counts": action_counts,
        "source_bytes": source_bytes,
        "maximum_source_bytes": maximum,
        "source_fingerprints": fingerprints,
        "state": "preflight_passed",
    }


def execute_manifest(
    manifest_path: Path,
    *,
    actor: str,
) -> dict[str, Any]:
    actor = actor.strip()
    if not actor:
        raise StressPilotError("named actor is required")
    manifest, manifest_digest = read_manifest(manifest_path)
    preflight = validate_manifest(manifest, include_fingerprints=True)
    root = Path(str(manifest["pilot_root"])).resolve()
    independent = Path(str(manifest["independent_root"])).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    independent.parent.mkdir(parents=True, exist_ok=True)
    catalog_core.initialize_root(root, TOOL_VERSION, actor)
    config = catalog_core.load_config(root)
    config["independent_generation_root"] = str(independent)
    catalog_core.save_config(root, config)

    case_results: list[dict[str, Any]] = []
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    ) as catalog:
        for case in manifest["cases"]:
            catalog_core.create_unit(
                catalog,
                unit_id=case["unit_id"],
                title=f"Stress pilot: {case['inventory_label']}",
                kind="provenance_stress_pilot",
                actor=actor,
                note=(
                    f"{case['scenario']}; {case['action']}; "
                    "pilot fixture, not curatorial acceptance"
                ),
            )
            metadata = case["metadata"]
            entity = catalog_core.create_entity(
                catalog,
                kind="archive_object",
                canonical_name=(
                    metadata.get("descriptive_title")
                    or case["inventory_label"]
                ),
                unit_id=case["unit_id"],
                membership_role="primary",
                actor=actor,
                assignment_year=datetime.now(UTC).year,
                domain={
                    "archival_label": case["inventory_label"],
                    "descriptive_title": metadata.get("descriptive_title", ""),
                    "description": metadata.get("description", ""),
                    "material_technique": metadata.get(
                        "material_technique", ""
                    ),
                    "dimensions": metadata.get("dimensions", ""),
                    "publication_status": "blocked",
                    "note": (
                        f"Stress-pilot scenario {case['scenario']}; "
                        f"representation action {case['action']}; "
                        "not curatorial acceptance"
                    ),
                },
                note=f"Bound to pilot manifest sha256:{manifest_digest}",
            )
            catalog_core.add_alias_identifier(
                catalog,
                entity_id=entity["entity_id"],
                namespace="inventory",
                value=case["inventory_label"],
                normalized_value=case["inventory_label"].upper(),
                role="legacy",
                actor=actor,
                note="Stress-pilot evidence only",
            )
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "unit_id": case["unit_id"],
                    "entity_id": entity["entity_id"],
                    "scenario": case["scenario"],
                    "action": case["action"],
                    "state": "cataloged",
                }
            )
    catalog_core.publish_generation(
        root,
        independent,
        tool_version=TOOL_VERSION,
        actor=actor,
    )

    by_case = {item["case_id"]: item for item in case_results}
    for case in manifest["cases"]:
        result = by_case[case["case_id"]]
        action = case["action"]
        if action == "metadata_only":
            result["state"] = "expected_no_local_representation"
            continue
        source = Path(case["source_path"])
        source_digest = preflight["source_fingerprints"][case["case_id"]][
            "sha256"
        ]
        provenance = {
            "case_id": case["case_id"],
            "source_file_id": f"STRESS-SOURCE-{case['case_id']}",
            "source_digest": f"sha256:{source_digest}",
            "reviewed_bundle_digest": f"sha256:{manifest_digest}",
            "museum_digital_id": case.get("museum_digital_id", ""),
            "representation_role": (
                "preservation_master"
                if action == "tiff_master"
                else "preservation_surrogate"
            ),
            "provenance_gaps": (
                [] if action == "tiff_master" else ["missing_local_master"]
            ),
        }
        try:
            if action == "tiff_master":
                accession = tiff_accession.create_preservation_master(
                    root,
                    source,
                    result["entity_id"],
                    actor,
                    embedding_profile=tiff_metadata.PROFILE_V2,
                    provenance_metadata=provenance,
                )
            else:
                accession = surrogate_accession.create_preservation_surrogate(
                    root,
                    source,
                    case["source_media_type"],
                    result["entity_id"],
                    actor,
                    provenance_metadata=provenance,
                )
        except (
            tiff_accession.TiffAccessionError,
            surrogate_accession.SurrogateAccessionError,
        ) as error:
            result["state"] = "guarded_failure"
            result["error"] = str(error)
        else:
            result["state"] = "representation_preserved"
            result["accession_report"] = accession["report"]
        catalog_core.publish_generation(
            root,
            independent,
            tool_version=TOOL_VERSION,
            actor=actor,
        )

    after: dict[str, dict[str, Any]] = {}
    for case in manifest["cases"]:
        if case["action"] != "metadata_only":
            after[case["case_id"]] = _fingerprint(Path(case["source_path"]))
    if after != preflight["source_fingerprints"]:
        raise StressPilotError("one or more protected source files changed")

    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as catalog:
        fixity = catalog_core.verify_preservation_masters(
            catalog,
            root,
            independent,
        )
    status = catalog_core.status(root)
    report = {
        "format": REPORT_FORMAT,
        "completed_at": _utc_now(),
        "actor": actor,
        "manifest": str(manifest_path.expanduser().resolve()),
        "manifest_sha256": manifest_digest,
        "root": str(root),
        "independent_root": str(independent),
        "preflight": preflight,
        "protected_sources_modified": False,
        "cases": case_results,
        "fixity": fixity,
        "status": status,
        "state": "complete",
    }
    report_path = root / "work" / "stress-pilot-report.json"
    _exclusive_json(report_path, report)
    report["report"] = str(report_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--retained-index", type=Path)
    parser.add_argument("--pilot-root", type=Path)
    parser.add_argument("--independent-root", type=Path)
    parser.add_argument("--actor", default="")
    parser.add_argument(
        "--max-source-bytes",
        type=int,
        default=DEFAULT_MAX_SOURCE_BYTES,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.prepare == args.execute:
        raise SystemExit("error: choose exactly one of --prepare or --execute")
    try:
        if args.prepare:
            if not args.retained_index or not args.pilot_root or not args.independent_root:
                raise StressPilotError(
                    "--prepare requires --retained-index, --pilot-root, "
                    "and --independent-root"
                )
            result = build_manifest(
                args.retained_index,
                args.manifest,
                args.pilot_root,
                args.independent_root,
                max_source_bytes=args.max_source_bytes,
            )
        else:
            result = execute_manifest(args.manifest, actor=args.actor)
    except (
        StressPilotError,
        catalog_core.CoreCatalogError,
        tiff_accession.TiffAccessionError,
        surrogate_accession.SurrogateAccessionError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
