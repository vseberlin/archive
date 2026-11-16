"""Intake and entity-creation workflow for the legacy archive surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import catalog_core
import source_registry

from archive_core import ArchiveError
from archive_core.catalog import connect_db, create_schema, set_context
from archive_core.filesystem import (
    LEGACY_FIXTURE_MODE,
    copy_and_verify,
    create_root_readme,
    require_under,
    root_paths,
    safe_relative_path,
    sha256_file,
    utc_now,
)
from archive_core.identity import (
    UNIT_ID_RE,
    entity_exists,
    generate_archival_id,
    require_unit,
    source_file_identifier,
)
from archive_core.layout import object_relative_path
from archive_core.documents import (
    PDF_PLAN_FORMAT,
    atomic_copy_verified,
    backup_catalog,
    canonical_json,
    assess_document_stub,
    inspect_pdf,
    mark_read_only,
    sha256_path,
)


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root), allow_uninitialized=True)
    if paths["catalog_db"].exists() or paths["legacy_db"].exists():
        raise ArchiveError("working catalog already exists")
    for key in ("backups", "intake", "seal", "units", "manifests", "derived", "gaps", "transient"):
        paths[key].mkdir(parents=True, exist_ok=True)
    (paths["root"] / "work" / "catalog-mode.json").write_text(
        json.dumps(
            {
                "catalog_mode": LEGACY_FIXTURE_MODE,
                "purpose": "tests and preservation fixtures only",
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    create_root_readme(paths["root"])
    (paths["manifests"] / "units.csv").write_text(
        "unit_id,title,kind,status,sealed_at\n", encoding="utf-8", newline="\n"
    )
    workstation = args.workstation_id or socket.gethostname()
    connection = create_schema(paths["catalog_db"], workstation, args.actor)
    connection.close()
    return {
        "result": "pass",
        "root": str(paths["root"]),
        "db": str(paths["catalog_db"]),
        "registry_db": str(paths["dedicated_registry_db"]),
        "workstation_id": workstation,
    }


def _ensure_pdf_copy(source: Path, destination: Path, digest: str) -> str:
    if destination.exists():
        if not destination.is_file() or sha256_path(destination) != digest:
            raise ArchiveError(
                f"existing PDF preservation path differs: {destination}"
            )
    else:
        atomic_copy_verified(source, destination, digest)
    return mark_read_only(destination)


def command_core_document_assess(args: argparse.Namespace) -> dict[str, Any]:
    return assess_document_stub(Path(args.path))


def command_core_pdf_accession(args: argparse.Namespace) -> dict[str, Any]:
    """Accession the explicitly selected PDF from one immutable plan."""

    root = catalog_core.assert_safe_work_root(Path(args.root))
    config = catalog_core.require_core_mode(root)
    plan_path = Path(args.plan).expanduser().resolve(strict=True)
    plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArchiveError(f"cannot read PDF reconciliation plan: {plan_path}") from error
    if not isinstance(plan, dict) or plan.get("format") != PDF_PLAN_FORMAT:
        raise ArchiveError("PDF reconciliation plan format is invalid")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list):
        raise ArchiveError("PDF reconciliation candidates are invalid")
    selected_id = str(plan.get("selected_candidate") or "")
    selected = next(
        (
            value
            for value in candidates
            if isinstance(value, dict)
            and value.get("candidate_id") == selected_id
        ),
        None,
    )
    if not selected:
        raise ArchiveError("selected PDF candidate does not resolve")
    source_kind = str(selected.get("source_kind") or "")
    representation_role = (
        "preservation_document"
        if source_kind == "local"
        else "preservation_document_surrogate"
    )
    if source_kind == "local":
        source = Path(str(selected.get("source_locator") or "")).resolve(
            strict=True
        )
    elif source_kind == "museum_digital":
        source = Path(str(selected.get("captured_path") or "")).resolve(
            strict=True
        )
    else:
        raise ArchiveError(f"unsupported PDF source kind: {source_kind}")
    inspection = inspect_pdf(source)
    expected = selected.get("inspection")
    if not isinstance(expected, dict):
        raise ArchiveError("selected PDF inspection is missing")
    for key in (
        "sha256",
        "size_bytes",
        "media_type",
        "pdf_version",
        "pages",
        "encrypted",
        "text_status",
        "observed_mtime_utc",
        "device",
        "inode",
    ):
        if inspection[key] != expected.get(key):
            raise ArchiveError(
                f"selected PDF changed after reconciliation: {key}"
            )
    catalog_path = root / "work" / "catalog.sqlite"
    catalog = catalog_core.connect_catalog(catalog_path)
    try:
        catalog_core.assert_catalog_integrity(catalog)
        catalog_core.reconcile_current_generation(catalog, root)
        catalog_core.require_filesystem_current(catalog)
        state = catalog_core.catalog_state(catalog)
        if int(state["catalog_revision"]) != int(plan["catalog_revision"]):
            raise ArchiveError(
                "catalog revision differs from the PDF reconciliation plan"
            )
        entity = catalog.execute(
            """SELECT a.archival_label
               FROM archive_objects a
               JOIN entity_unit_memberships m ON m.entity_id = a.entity_id
               JOIN units u ON u.unit_id = m.unit_id
               WHERE a.entity_id = ? AND m.unit_id = ? AND u.status = 'open'""",
            (plan["entity_id"], plan["unit_id"]),
        ).fetchone()
        if not entity:
            raise ArchiveError("PDF plan entity is not an open unit member")
        relative = object_relative_path(
            unit_id=str(plan["unit_id"]),
            inventory=str(entity["archival_label"]),
            entity_id=str(plan["entity_id"]),
            role=representation_role,
            suffix=".pdf",
        )
        filename = relative.name
        primary = root / Path(*relative.parts)
        independent_root = Path(
            str(config.get("independent_generation_root") or "")
        ).resolve()
        if not str(config.get("independent_generation_root") or ""):
            raise ArchiveError(
                "PDF accession requires a configured independent root"
            )
        independent_root.mkdir(parents=True, exist_ok=True)
        catalog_core.assert_independent_device(root, independent_root)
        independent = independent_root / Path(*relative.parts)
        independent_protection = _ensure_pdf_copy(
            source,
            independent,
            inspection["sha256"],
        )
        primary_protection = _ensure_pdf_copy(
            source,
            primary,
            inspection["sha256"],
        )
        backup = backup_catalog(root, str(plan["entity_id"]))
        technical_profile = {
            key: inspection[key]
            for key in (
                "pdf_version",
                "pages",
                "encrypted",
                "page_size",
                "tagged",
                "form",
                "javascript",
                "text_status",
                "indexing_status",
                "validation_tool",
            )
        }
        md = plan.get("museum_digital")
        if not isinstance(md, dict):
            raise ArchiveError("PDF plan museum-digital evidence is missing")
        resource = selected.get("resource")
        resource_id = (
            str(resource.get("resource_id") or "")
            if isinstance(resource, dict)
            else ""
        )
        provenance = {
            "plan_digest": f"sha256:{plan_digest}",
            "selected_candidate": selected_id,
            "source_kind": source_kind,
            "source_locator": str(selected.get("source_locator") or ""),
            "museum_digital_id": str(md.get("object_id") or ""),
            "museum_digital_resource_id": resource_id,
            "selection_note": str(plan.get("selection_note") or ""),
        }
        result = catalog_core.register_preservation_document(
            catalog,
            entity_id=str(plan["entity_id"]),
            unit_id=str(plan["unit_id"]),
            content_digest=f"sha256:{inspection['sha256']}",
            size_bytes=int(inspection["size_bytes"]),
            relative_path=relative.as_posix(),
            original_filename=filename,
            observed_mtime_utc=str(inspection["observed_mtime_utc"]),
            page_count=int(inspection["pages"]),
            expected_catalog_revision=int(plan["catalog_revision"]),
            representation_role=representation_role,
            technical_profile=technical_profile,
            provenance=provenance,
            actor=args.actor,
        )
        catalog_core.assert_catalog_integrity(catalog)
    finally:
        catalog.close()
    report = {
        "result": "ok",
        "unit_id": plan["unit_id"],
        "entity_id": plan["entity_id"],
        "plan": str(plan_path),
        "plan_digest": f"sha256:{plan_digest}",
        "source": str(source),
        "source_kind": source_kind,
        "primary": str(primary),
        "independent": str(independent),
        "sha256": inspection["sha256"],
        "pages": inspection["pages"],
        "text_status": inspection["text_status"],
        "primary_protection": primary_protection,
        "independent_protection": independent_protection,
        "catalog_backup": str(backup),
        "catalog_revision": result["catalog_revision"],
        "representation_role": result["representation_role"],
        "state": "unpublished",
    }
    reports = root / "work" / "accession-reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / (
        f"PDF-{plan['unit_id']}-{plan['entity_id']}.json"
    )
    if report_path.exists():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            existing.get("sha256") != report["sha256"]
            or existing.get("plan_digest") != report["plan_digest"]
        ):
            raise ArchiveError(f"refusing to replace PDF report: {report_path}")
    else:
        report_path.write_text(
            canonical_json(report) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    report["report"] = str(report_path)
    return report

def command_create_unit(args: argparse.Namespace) -> dict[str, Any]:
    if not UNIT_ID_RE.fullmatch(args.unit):
        raise ArchiveError(f"invalid unit ID: {args.unit}")
    paths = root_paths(Path(args.root))
    if paths["mode"] == LEGACY_FIXTURE_MODE and (
        args.kind != "fixture" or not args.unit.startswith("TEST-")
    ):
        raise ArchiveError("legacy mode may create TEST- fixture units only")
    with connect_db(paths["db"]) as connection:
        set_context(connection, args.actor, note=args.note)
        connection.execute(
            "INSERT INTO units (unit_id, title, kind, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (args.unit, args.title, args.kind, utc_now()),
        )
        connection.commit()
    return {"result": "pass", "unit_id": args.unit}

def command_create_object(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        object_id = generate_archival_id(connection, "object", args.assignment_year)
        connection.execute(
            """
            INSERT INTO objects (
                object_id, unit_id, display_identifier, object_type, archival_label,
                publication_status, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                args.unit,
                args.display_identifier,
                args.object_type,
                args.label,
                args.publication_status,
                args.status,
                args.note,
                utc_now(),
            ),
        )
        connection.commit()
    return {"result": "pass", "object_id": object_id}

def command_create_event(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        event_id = generate_archival_id(connection, "event", args.assignment_year)
        connection.execute(
            """
            INSERT INTO events (
                event_id, unit_id, title, starts_on, ends_on, circa, location,
                description, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                args.unit,
                args.title,
                args.starts_on,
                args.ends_on,
                int(args.circa),
                args.location,
                args.description,
                args.status,
                args.note,
                utc_now(),
            ),
        )
        connection.commit()
    return {"result": "pass", "event_id": event_id}

def command_add_testimony(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        if args.event_id and not connection.execute(
            "SELECT 1 FROM events WHERE event_id = ? AND unit_id = ?", (args.event_id, args.unit)
        ).fetchone():
            raise ArchiveError(f"event does not belong to unit: {args.event_id}")
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        testimony_id = "ISS-T-" + uuid.uuid4().hex[:16].upper()
        connection.execute(
            """
            INSERT INTO testimonies (
                testimony_id, unit_id, event_id, contributor, recorded_at,
                verbatim, confidence, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                testimony_id,
                args.unit,
                args.event_id or None,
                args.contributor,
                args.recorded_at,
                args.verbatim,
                args.confidence,
                args.status,
                args.note,
                utc_now(),
            ),
        )
        connection.commit()
    return {"result": "pass", "testimony_id": testimony_id}

def command_add_identifier(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        if not entity_exists(connection, args.entity_type, args.entity_id, args.unit):
            raise ArchiveError(f"unknown entity: {args.entity_type}:{args.entity_id}")
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        identifier_id = "ID-" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier_id, args.unit, args.entity_type, args.entity_id, args.scheme, args.value, args.status, args.note),
        )
        connection.commit()
    return {"result": "pass", "identifier_id": identifier_id}

def command_add_gap(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        if not entity_exists(connection, args.entity_type, args.entity_id, args.unit):
            raise ArchiveError(f"unknown entity: {args.entity_type}:{args.entity_id}")
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        gap_id = "GAP-" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (gap_id, args.unit, args.entity_type, args.entity_id, args.gap_type, args.detail, args.next_action, args.status),
        )
        connection.commit()
    return {"result": "pass", "gap_id": gap_id}

def command_intake(args: argparse.Namespace) -> dict[str, Any]:
    relative = safe_relative_path(args.path)
    source_root = Path(args.source_root).resolve()
    if (
        source_registry.root_kind(args.source_root_id, source_root) == "usb"
        and not source_registry.root_is_read_only(source_root)
    ):
        raise ArchiveError(f"USB source root must be mounted read-only: {source_root}")
    paths = root_paths(Path(args.root))
    source = require_under(source_root / Path(*relative.parts), source_root)
    if source.is_symlink() or not source.is_file():
        raise ArchiveError(f"source is not a regular non-symlink file: {source}")
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        existing = connection.execute(
            "SELECT * FROM source_files WHERE batch_id = ? AND relative_path = ?",
            (args.batch, str(relative)),
        ).fetchone()
        if existing:
            intake_path = paths["root"] / existing["intake_relpath"]
            if intake_path.is_file() and sha256_file(intake_path) == existing["file_id"].removeprefix("sha256:"):
                return {"result": "pass", "state": "already_intaken", "source_file_id": existing["source_file_id"]}
            raise ArchiveError(f"existing intake row is not healthy: {args.batch}:{relative}")
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        batch = connection.execute("SELECT * FROM source_batches WHERE batch_id = ?", (args.batch,)).fetchone()
        if not batch:
            connection.execute(
                "INSERT INTO source_batches VALUES (?, ?, ?, ?, 'active', ?)",
                (args.batch, args.unit, args.source_root_id, str(source_root), utc_now()),
            )
        elif batch["unit_id"] != args.unit or Path(batch["source_root_path"]).resolve() != source_root:
            raise ArchiveError(f"source batch mapping changed: {args.batch}")
        source_file_id = source_file_identifier(args.batch, str(relative))
        intake_relpath = PurePosixPath("work", "intake", args.batch, source_file_id, relative.name)
        destination = paths["root"] / Path(*intake_relpath.parts)
        size, digest = copy_and_verify(source, destination)
        mtime = datetime.fromtimestamp(source.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            INSERT INTO source_files (
                source_file_id, unit_id, batch_id, relative_path, original_filename,
                role, file_id, size_bytes, observed_mtime_utc, observed_at_utc,
                intake_relpath, status, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                source_file_id,
                args.unit,
                args.batch,
                str(relative),
                relative.name,
                args.role,
                f"sha256:{digest}",
                size,
                mtime,
                utc_now(),
                str(intake_relpath),
                args.note,
            ),
        )
        connection.commit()
    return {
        "result": "pass",
        "state": "copied_and_verified",
        "source_file_id": source_file_id,
        "file_id": f"sha256:{digest}",
        "size_bytes": size,
    }

def command_link(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        require_unit(connection, args.unit, open_only=True)
        if not entity_exists(connection, args.subject_type, args.subject_id, args.unit):
            raise ArchiveError(f"unknown subject: {args.subject_type}:{args.subject_id}")
        if not entity_exists(connection, args.object_type, args.object_id, args.unit):
            raise ArchiveError(f"unknown object: {args.object_type}:{args.object_id}")
        set_context(connection, args.actor, evidence=args.evidence, note=args.note)
        relationship_id = "REL-" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                relationship_id,
                args.unit,
                args.subject_type,
                args.subject_id,
                args.predicate,
                args.object_type,
                args.object_id,
                args.status,
                args.confidence,
                args.note,
            ),
        )
        connection.commit()
    return {"result": "pass", "relationship_id": relationship_id}
