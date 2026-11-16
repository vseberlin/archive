"""Publication, sealing, recovery, and health workflows."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import catalog_core
import source_registry

from archive_core import ArchiveError
from archive_core.catalog import (
    CATALOG_STATE_TABLES,
    LOCAL_ONLY_COLUMNS,
    LOGGED_TABLES,
    PROJECTED_TABLES,
    SCHEMA_SQL,
    assert_integrity,
    connect_db,
    connect_registry_db,
    create_schema,
    install_edit_views,
    install_log_triggers,
    set_context,
    table_columns,
    write_unit_catalog,
)
from archive_core.filesystem import (
    json_text,
    require_safe_work_root,
    require_under,
    root_paths,
    safe_relative_path,
    sha256_file,
    utc_now,
)
from archive_core.identity import require_unit, validate_archival_id
from archive_core.metadata import projection_payload, record_id_for
from archive_core.validation import (
    actual_payload_paths,
    declared_payload_paths,
    read_catalog_projection,
    validate_bag,
    validate_internal,
)


TOOL_VERSION = catalog_core.TOOL_VERSION


SEVERITY_ORDER = {"safe": 0, "yellow": 1, "red": 2, "critical": 3}

def export_projections(connection: sqlite3.Connection, unit_id: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    catalog_path = output / "catalog.csv"
    with catalog_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("record_type", "record_id", "unit_id", "payload_json"))
        for table in PROJECTED_TABLES:
            primary_key = LOGGED_TABLES[table]
            rows = connection.execute(
                f'SELECT * FROM "{table}" WHERE unit_id = ? ORDER BY "{primary_key}"',
                (unit_id,),
            ).fetchall()
            for row in rows:
                writer.writerow((table, record_id_for(table, row), unit_id, json_text(projection_payload(table, row))))
    identifiers_path = output / "identifiers.csv"
    identifier_columns = table_columns(connection, "identifiers")
    with identifiers_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=identifier_columns, lineterminator="\n")
        writer.writeheader()
        for row in connection.execute(
            "SELECT * FROM identifiers WHERE unit_id = ? ORDER BY identifier_id", (unit_id,)
        ):
            writer.writerow(dict(row))
    logbook_path = output / "logbook.jsonl"
    with logbook_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in connection.execute(
            "SELECT * FROM logbook WHERE unit_id = ? ORDER BY at_utc, log_id", (unit_id,)
        ):
            handle.write(json_text(dict(row)) + "\n")

def archive_paths_for_files(rows: Iterable[sqlite3.Row]) -> dict[str, str]:
    rows = list(rows)
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        folder = "masters" if row["role"] == "master" else "sources"
        key = (folder, row["original_filename"].casefold())
        counts[key] = counts.get(key, 0) + 1
    result: dict[str, str] = {}
    used: set[str] = set()
    for row in sorted(rows, key=lambda item: item["source_file_id"]):
        folder = "masters" if row["role"] == "master" else "sources"
        filename = row["original_filename"]
        if any(char in filename for char in ("/", "\\", "\x00", "\r", "\n", "%")):
            raise ArchiveError(f"unsafe archive filename: {filename!r}")
        key = (folder, filename.casefold())
        if counts[key] > 1:
            suffix = row["source_file_id"].removeprefix("SRC-")[-8:]
            path = Path(filename)
            filename = f"{path.stem}--{suffix}{path.suffix}"
        archive_path = str(PurePosixPath("data", folder, filename))
        if archive_path.casefold() in used:
            raise ArchiveError(f"unresolved archive filename collision: {archive_path}")
        used.add(archive_path.casefold())
        result[row["source_file_id"]] = archive_path
    return result

def create_catalog_state_generation(connection: sqlite3.Connection, backup_dir: Path, unit_id: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    final = backup_dir / f"{stamp}-{unit_id}.sqlite"
    temporary = backup_dir / f".{stamp}-{unit_id}.sqlite.partial"
    if temporary.exists():
        raise ArchiveError(f"catalog-state temporary path already exists: {temporary}")
    with closing(sqlite3.connect(temporary)) as backup:
        backup.row_factory = sqlite3.Row
        backup.executescript(SCHEMA_SQL)
        for table in CATALOG_STATE_TABLES:
            columns = table_columns(connection, table)
            quoted = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            rows = connection.execute(f'SELECT {quoted} FROM "{table}"')
            backup.executemany(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                (tuple(row[column] for column in columns) for row in rows),
            )
        install_log_triggers(backup)
        install_edit_views(backup)
        backup.commit()
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ArchiveError("catalog-state generation failed integrity check")
    os.replace(temporary, final)
    return final

def create_catalog_database(connection: sqlite3.Connection, destination: Path) -> Path:
    if destination.exists():
        raise ArchiveError(f"catalog destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    if temporary.exists():
        raise ArchiveError(f"catalog temporary path already exists: {temporary}")
    with closing(sqlite3.connect(temporary)) as catalog:
        catalog.row_factory = sqlite3.Row
        catalog.executescript(SCHEMA_SQL)
        for table in CATALOG_STATE_TABLES:
            columns = table_columns(connection, table)
            quoted = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            rows = connection.execute(f'SELECT {quoted} FROM "{table}"')
            catalog.executemany(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                (tuple(row[column] for column in columns) for row in rows),
            )
        install_log_triggers(catalog)
        install_edit_views(catalog)
        catalog.commit()
        if catalog.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ArchiveError("separated catalog failed integrity check")
        if catalog.execute("PRAGMA foreign_key_check").fetchall():
            raise ArchiveError("separated catalog failed foreign-key check")
    os.replace(temporary, destination)
    return destination

def payload_manifest(bag: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for path in sorted((bag / "data").rglob("*")):
        if path.is_symlink():
            raise ArchiveError(f"symlink in bag payload: {path}")
        if path.is_file():
            relative = path.relative_to(bag).as_posix()
            safe_relative_path(relative)
            rows.append((sha256_file(path), relative))
    return rows

def write_bag_tags(bag: Path, unit: sqlite3.Row) -> None:
    rows = payload_manifest(bag)
    (bag / "bagit.txt").write_text(
        "BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
        encoding="utf-8",
        newline="\n",
    )
    payload_bytes = sum((bag / Path(path)).stat().st_size for _, path in rows)
    (bag / "bag-info.txt").write_text(
        "\n".join(
            (
                f"External-Identifier: {unit['unit_id']}",
                f"Bagging-Date: {datetime.now(UTC).date().isoformat()}",
                f"Bag-Software-Agent: ISS archive.py {TOOL_VERSION}",
                f"Payload-Oxum: {payload_bytes}.{len(rows)}",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    readme = f"""# {unit['unit_id']}: {unit['title']}

`data/masters/` contains byte-frozen preservation masters. `data/sources/`
contains unchanged raw evidence. `data/catalog.csv`, `identifiers.csv`, and
`logbook.jsonl` are generated text projections of the working catalog. Start
with an object or event ID, follow catalog relationships to a source file, then
verify its `sha256:` file ID against `manifest-sha256.txt`. SQLite, HTML, and
derivatives are not required to understand this bag.
"""
    (bag / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    (bag / "manifest-sha256.txt").write_text(
        "".join(f"{digest}  {path}\n" for digest, path in rows),
        encoding="utf-8",
        newline="\n",
    )
    tag_files = ("bagit.txt", "bag-info.txt", "manifest-sha256.txt", "README.md")
    (bag / "tagmanifest-sha256.txt").write_text(
        "".join(f"{sha256_file(bag / name)}  {name}\n" for name in tag_files),
        encoding="utf-8",
        newline="\n",
    )

def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    return validate_bag(Path(args.bag), args.validator_python)

def update_units_index(connection: sqlite3.Connection, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("unit_id", "title", "kind", "status", "sealed_at"))
        for row in connection.execute("SELECT * FROM units ORDER BY unit_id"):
            writer.writerow((row["unit_id"], row["title"], row["kind"], row["status"], row["sealed_at"] or ""))

def command_seal(args: argparse.Namespace) -> dict[str, Any]:
    paths = root_paths(Path(args.root))
    preservation_root = Path(args.preservation_root).resolve()
    preservation_root.mkdir(parents=True, exist_ok=True)
    candidate = paths["seal"] / f"{args.unit}.incomplete"
    primary = paths["units"] / args.unit
    preservation_units = preservation_root / "units"
    preservation_units.mkdir(parents=True, exist_ok=True)
    preservation_final = preservation_units / args.unit
    preservation_temporary = preservation_units / f".{args.unit}.partial"
    if primary.exists() or preservation_final.exists():
        raise ArchiveError("primary or preservation unit already exists; re-sealing is outside the mini-pilot")
    if candidate.exists():
        require_under(candidate, paths["seal"])
        shutil.rmtree(candidate)
    if preservation_temporary.exists():
        require_under(preservation_temporary, preservation_units)
        shutil.rmtree(preservation_temporary)
    connection = connect_db(paths["db"])
    sealed_changed = False
    try:
        unit = require_unit(connection, args.unit, open_only=True)
        same_device = paths["root"].stat().st_dev == preservation_root.stat().st_dev
        fixture_override = (
            unit["kind"] == "fixture"
            and unit["unit_id"].startswith("TEST-")
            and os.environ.get("ISS_ARCHIVE_FIXTURE_SAME_DEVICE") == "1"
        )
        if same_device and not fixture_override:
            raise ArchiveError("preservation copy must be on a different filesystem device")
        assert_integrity(connection)
        set_context(connection, args.actor, evidence="seal", note=args.note)
        create_catalog_state_generation(connection, paths["backups"], args.unit)
        files = connection.execute(
            "SELECT * FROM source_files WHERE unit_id = ? AND status = 'active' ORDER BY source_file_id",
            (args.unit,),
        ).fetchall()
        if not files:
            raise ArchiveError("cannot seal a unit without selected source files")
        archive_paths = archive_paths_for_files(files)
        for row in files:
            intake_file = paths["root"] / row["intake_relpath"]
            if not intake_file.is_file() or sha256_file(intake_file) != row["file_id"].removeprefix("sha256:"):
                raise ArchiveError(f"working intake fixity failed: {row['source_file_id']}")
            connection.execute(
                "UPDATE source_files SET archive_path = ? WHERE source_file_id = ?",
                (archive_paths[row["source_file_id"]], row["source_file_id"]),
            )
        sealed_at = utc_now()
        connection.execute("UPDATE units SET status = 'sealed', sealed_at = ? WHERE unit_id = ?", (sealed_at, args.unit))
        connection.commit()
        sealed_changed = True
        unit = connection.execute("SELECT * FROM units WHERE unit_id = ?", (args.unit,)).fetchone()
        candidate.mkdir(parents=True)
        (candidate / "data" / "masters").mkdir(parents=True)
        (candidate / "data" / "sources").mkdir(parents=True)
        files = connection.execute(
            "SELECT * FROM source_files WHERE unit_id = ? AND status = 'active' ORDER BY source_file_id",
            (args.unit,),
        ).fetchall()
        for row in files:
            source = paths["root"] / row["intake_relpath"]
            destination = candidate / Path(*PurePosixPath(row["archive_path"]).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if sha256_file(destination) != row["file_id"].removeprefix("sha256:"):
                raise ArchiveError(f"seal copy fixity failed: {row['source_file_id']}")
            destination.chmod(0o444)
        export_projections(connection, args.unit, candidate / "data")
        write_bag_tags(candidate, unit)
        candidate_result = validate_bag(candidate, args.validator_python)
        shutil.copytree(candidate, preservation_temporary)
        preservation_result = validate_bag(preservation_temporary, args.validator_python)
        if candidate_result["manifest_sha256"] != preservation_result["manifest_sha256"]:
            raise ArchiveError("candidate and preservation copy manifests differ")
        os.replace(preservation_temporary, preservation_final)
        os.replace(candidate, primary)
        set_context(connection, args.actor, evidence="verified preservation copy", note=args.note)
        connection.execute(
            "INSERT OR REPLACE INTO preservation_copies VALUES (?, ?, ?, 'verified')",
            (args.unit, str(preservation_final), utc_now()),
        )
        manifest = candidate_result["manifest_sha256"]
        for role in ("primary", "preservation"):
            connection.execute(
                "INSERT INTO fixity_checks VALUES (?, ?, ?, ?, 'pass', ?, ?)",
                (uuid.uuid4().hex, args.unit, role, utc_now(), manifest, "initial mini-pilot seal"),
            )
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('preservation_root', ?)", (str(preservation_root),))
        connection.commit()
        create_catalog_state_generation(connection, paths["backups"], args.unit)
        update_units_index(connection, paths["manifests"] / "units.csv")
        return {
            **candidate_result,
            "bag": str(primary),
            "primary": str(primary),
            "preservation_copy": str(preservation_final),
            "catalog_state_backups": len(list(paths["backups"].glob(f"*-{args.unit}.sqlite"))),
        }
    except Exception:
        if candidate.exists():
            shutil.rmtree(candidate)
        if preservation_temporary.exists():
            shutil.rmtree(preservation_temporary)
        if sealed_changed and not primary.exists():
            set_context(connection, args.actor, evidence="seal rollback", note="automatic mini-pilot rollback")
            connection.execute("UPDATE units SET status = 'open', sealed_at = NULL WHERE unit_id = ?", (args.unit,))
            connection.execute("UPDATE source_files SET archive_path = '' WHERE unit_id = ?", (args.unit,))
            connection.commit()
        raise
    finally:
        connection.close()

def batch_releasable(connection: sqlite3.Connection, batch_id: str) -> bool:
    rows = connection.execute(
        """
        SELECT DISTINCT u.status AS unit_status, pc.status AS copy_status
        FROM source_files sf
        JOIN units u ON u.unit_id = sf.unit_id
        LEFT JOIN preservation_copies pc ON pc.unit_id = sf.unit_id
        WHERE sf.batch_id = ?
        """,
        (batch_id,),
    ).fetchall()
    return bool(rows) and all(row["unit_status"] == "sealed" and row["copy_status"] == "verified" for row in rows)

def status_report(root: Path, verify_catalog: bool = True) -> dict[str, Any]:
    paths = root_paths(root)
    conditions: list[dict[str, str]] = [
        {
            "severity": "safe",
            "condition": "status_board_generated",
            "detail": (
                "read-only sweep and deep catalog integrity check completed"
                if verify_catalog
                else "fast interactive read-only sweep completed; deep catalog integrity is checked separately"
            ),
        }
    ]
    with connect_db(paths["db"], read_only=True) as connection:
        if verify_catalog:
            try:
                assert_integrity(connection)
            except ArchiveError as error:
                conditions.append({"severity": "critical", "condition": "catalog_integrity", "detail": str(error)})
        workstation = connection.execute("SELECT value FROM meta WHERE key = 'workstation_id'").fetchone()
        if workstation and workstation[0] != socket.gethostname():
            conditions.append(
                {
                    "severity": "critical",
                    "condition": "workstation_fork",
                    "detail": f"catalog={workstation[0]} current={socket.gethostname()}",
                }
            )
        incomplete = list(paths["seal"].glob("*.incomplete"))
        for path in incomplete:
            conditions.append({"severity": "critical", "condition": "incomplete_seal", "detail": str(path)})
        backups = list(paths["backups"].glob("*.sqlite"))
        if not backups:
            conditions.append({"severity": "red", "condition": "catalog_state_backup_missing", "detail": "no verified state generation"})
        for unit in connection.execute("SELECT * FROM units ORDER BY unit_id"):
            if unit["status"] == "open":
                conditions.append({"severity": "yellow", "condition": "unit_unsealed", "detail": unit["unit_id"]})
            else:
                primary = paths["units"] / unit["unit_id"]
                copy = connection.execute("SELECT * FROM preservation_copies WHERE unit_id = ?", (unit["unit_id"],)).fetchone()
                if not primary.is_dir():
                    conditions.append({"severity": "critical", "condition": "primary_bag_missing", "detail": unit["unit_id"]})
                if not copy or copy["status"] != "verified" or not Path(copy["bag_path"]).is_dir():
                    conditions.append({"severity": "red", "condition": "preservation_copy_missing", "detail": unit["unit_id"]})
                bag_locations = [("primary", primary)]
                if copy and Path(copy["bag_path"]).is_dir():
                    bag_locations.append(("preservation", Path(copy["bag_path"])))
                for role, bag in bag_locations:
                    if not bag.is_dir():
                        continue
                    try:
                        declared = declared_payload_paths(bag)
                        actual = actual_payload_paths(bag)
                    except ArchiveError as error:
                        conditions.append({"severity": "critical", "condition": "payload_inventory", "detail": f"{role}:{unit['unit_id']}:{error}"})
                        continue
                    if declared != actual:
                        added = sorted(actual - declared)
                        missing = sorted(declared - actual)
                        conditions.append(
                            {
                                "severity": "critical",
                                "condition": "payload_inventory",
                                "detail": f"{role}:{unit['unit_id']}:unmanifested={added[:5]} missing={missing[:5]}",
                            }
                        )
        invalid_objects = [
            row[0] for row in connection.execute("SELECT object_id FROM objects ORDER BY object_id")
            if not validate_archival_id(row[0]) or not row[0].startswith("ISS-A-")
        ]
        invalid_events = [
            row[0] for row in connection.execute("SELECT event_id FROM events ORDER BY event_id")
            if not validate_archival_id(row[0]) or not row[0].startswith("ISS-E-")
        ]
        if invalid_objects or invalid_events:
            invalid = invalid_objects + invalid_events
            sample = ", ".join(invalid[:5])
            conditions.append(
                {
                    "severity": "critical",
                    "condition": "archival_id_format",
                    "detail": f"{len(invalid)} identifier(s) do not match the current four-symbol format: {sample}",
                }
            )
        for batch in connection.execute("SELECT * FROM source_batches ORDER BY batch_id"):
            root_path = Path(batch["source_root_path"]) if batch["source_root_path"] else None
            releasable = batch_releasable(connection, batch["batch_id"])
            if root_path and not releasable:
                if not root_path.exists():
                    conditions.append(
                        {"severity": "critical", "condition": "expected_source_missing", "detail": batch["batch_id"]}
                    )
                else:
                    for source_file in connection.execute(
                        "SELECT relative_path FROM source_files WHERE batch_id = ? ORDER BY relative_path",
                        (batch["batch_id"],),
                    ):
                        relative = safe_relative_path(source_file["relative_path"])
                        expected = root_path / Path(*relative.parts)
                        if not expected.is_file():
                            conditions.append(
                                {
                                    "severity": "critical",
                                    "condition": "expected_source_file_missing",
                                    "detail": f"{batch['batch_id']}:{relative}",
                                }
                            )
        usage = shutil.disk_usage(paths["root"])
        if usage.free < 1024 * 1024 * 1024:
            conditions.append({"severity": "red", "condition": "free_space_low", "detail": str(usage.free)})
    overall = max(conditions, key=lambda row: SEVERITY_ORDER[row["severity"]])["severity"]
    return {"result": "pass", "overall": overall, "checked_at_utc": utc_now(), "conditions": conditions}

def command_status(args: argparse.Namespace) -> dict[str, Any]:
    return status_report(Path(args.root))

def command_refresh_controls(args: argparse.Namespace) -> dict[str, Any]:
    """Refresh tool-owned triggers and views without changing archival records."""
    paths = root_paths(Path(args.root))
    with connect_db(paths["db"]) as connection:
        set_context(connection, args.actor, evidence="catalog control refresh", note=args.note)
        assert_integrity(connection)
        before = create_catalog_state_generation(connection, paths["backups"], "CATALOG-CONTROLS-BEFORE")
        install_log_triggers(connection)
        install_edit_views(connection)
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('tool_version', ?)", (TOOL_VERSION,))
        connection.commit()
        assert_integrity(connection)
        after = create_catalog_state_generation(connection, paths["backups"], "CATALOG-CONTROLS-AFTER")
    return {
        "result": "pass",
        "tool_version": TOOL_VERSION,
        "before_generation": str(before),
        "after_generation": str(after),
        "controls": sorted(f"edit_{table}" for table in ("objects", "events", "testimonies", "gaps", "relationships", "identifiers")),
    }

def command_scan_source_registry(args: argparse.Namespace) -> dict[str, Any]:
    """Index metadata-bearing source occurrences without changing source files."""
    paths = root_paths(Path(args.root))
    if not paths["registry_writable"]:
        raise ArchiveError(
            "source-registry writes are frozen for this catalog mode"
        )
    if (
        paths["mode"] == catalog_core.CATALOG_MODE
        and paths["registry_db"] != paths["dedicated_registry_db"]
    ):
        raise ArchiveError(
            "core mode may write only work/source-registry/index.sqlite"
        )
    root_specs: list[tuple[str, Path]] = []
    seen_ids: set[str] = set()
    for value in args.source_root:
        try:
            root_id, source_root = source_registry.parse_root_spec(value)
        except ValueError as error:
            raise ArchiveError(str(error)) from error
        if root_id in seen_ids:
            raise ArchiveError(f"duplicate source root label: {root_id}")
        if (
            source_registry.root_kind(root_id, source_root) == "usb"
            and not source_registry.root_is_read_only(source_root)
        ):
            raise ArchiveError(f"USB source root must be mounted read-only: {source_root}")
        seen_ids.add(root_id)
        root_specs.append((root_id, source_root))
    output = Path(args.output).expanduser().resolve() if args.output else paths["root"] / "work" / "source-registry" / "runs"
    require_under(output, paths["root"])
    with connect_registry_db(paths["registry_db"]) as connection:
        if getattr(args, "replace_derived_index", False):
            source_registry.clear_derived_registry(connection)
        connection.commit()
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            result = source_registry.scan_registry(
                connection,
                root_specs,
                actor=args.actor,
                note=args.note,
                max_hash_bytes=args.max_hash_bytes,
                defer_hash_suffixes=set(getattr(args, "defer_hash_suffix", [])),
                extract_pdf=getattr(args, "extract_pdf", False),
                output_dir=output,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        except BaseException:
            connection.rollback()
            connection.execute(f"PRAGMA synchronous={int(synchronous)}")
            raise
        connection.execute(f"PRAGMA synchronous={int(synchronous)}")
    return result

def command_split_working_databases(args: argparse.Namespace) -> dict[str, Any]:
    """Copy authoritative catalog tables out of a legacy combined database."""
    paths = root_paths(Path(args.root))
    if paths["catalog_db"].exists():
        raise ArchiveError(f"separated catalog already exists: {paths['catalog_db']}")
    if not paths["legacy_db"].is_file():
        raise ArchiveError(f"legacy combined database does not exist: {paths['legacy_db']}")
    with connect_db(paths["legacy_db"], read_only=True) as connection:
        catalog = create_catalog_database(connection, paths["catalog_db"])
    with connect_db(catalog, read_only=True) as connection:
        assert_integrity(connection)
        objects = connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        units = connection.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    return {
        "result": "pass",
        "catalog_db": str(catalog),
        "registry_db": str(paths["legacy_db"]),
        "legacy_combined_db_retained": str(paths["legacy_db"]),
        "units": units,
        "objects": objects,
    }

def command_core_init(args: argparse.Namespace) -> dict[str, Any]:
    root = require_safe_work_root(Path(args.root).expanduser())
    retained_index = (
        Path(args.retained_index).expanduser()
        if args.retained_index
        else None
    )
    return catalog_core.initialize_root(
        root,
        tool_version=TOOL_VERSION,
        actor=args.actor,
        retained_index=retained_index,
    )

def command_core_publish(args: argparse.Namespace) -> dict[str, Any]:
    return catalog_core.publish_generation(
        require_safe_work_root(Path(args.root).expanduser()),
        require_safe_work_root(Path(args.preservation_root).expanduser()),
        tool_version=TOOL_VERSION,
        actor=args.actor,
    )

def command_core_export_unit_catalog(args: argparse.Namespace) -> dict[str, Any]:
    root = require_safe_work_root(Path(args.root).expanduser())
    catalog_core.require_core_mode(root)
    if (
        not args.unit
        or Path(args.unit).name != args.unit
        or args.unit in (".", "..")
    ):
        raise catalog_core.CoreCatalogError(
            "unit catalog export requires one controlled unit ID"
        )
    destination = root / "units" / args.unit
    require_under(destination, root / "units")
    connection = catalog_core.connect_catalog(
        root / "work" / "catalog.sqlite",
        read_only=True,
    )
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        return write_unit_catalog(
            connection,
            args.unit,
            destination,
        )
    finally:
        connection.close()

def command_core_migrate(args: argparse.Namespace) -> dict[str, Any]:
    return catalog_core.migrate_empty_catalog(
        require_safe_work_root(Path(args.root).expanduser()),
        require_safe_work_root(Path(args.preservation_root).expanduser()),
        tool_version=TOOL_VERSION,
        actor=args.actor,
    )

def _core_mutation_connection(root_value: str) -> tuple[Path, sqlite3.Connection]:
    root = require_safe_work_root(Path(root_value).expanduser())
    catalog_core.require_core_mode(root)
    connection = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    catalog_core.reconcile_current_generation(connection, root)
    return root, connection

def command_core_create_unit(args: argparse.Namespace) -> dict[str, Any]:
    _root, connection = _core_mutation_connection(args.root)
    try:
        return catalog_core.create_unit(
            connection,
            unit_id=args.unit,
            title=args.title,
            kind=args.kind,
            actor=args.actor,
            note=args.note,
        )
    finally:
        connection.close()

def command_core_create_entity(args: argparse.Namespace) -> dict[str, Any]:
    _root, connection = _core_mutation_connection(args.root)
    domain: dict[str, Any]
    if args.kind == "archive_object":
        domain = {
            "archival_label": args.archival_label,
            "descriptive_title": args.descriptive_title,
            "description": args.description,
            "material_technique": args.material_technique,
            "dimensions": args.dimensions,
            "publication_status": args.publication_status,
            "note": args.note,
        }
    elif args.kind == "historical_event":
        domain = {
            "title": args.event_title or args.name,
            "starts_on": args.starts_on,
            "ends_on": args.ends_on,
            "circa": args.circa,
            "description": args.description,
            "note": args.note,
        }
    else:
        domain = {
            "carrier_type": args.carrier_type,
            "dimensions_text": args.dimensions,
            "condition_note": args.condition_note,
        }
    try:
        return catalog_core.create_entity(
            connection,
            kind=args.kind,
            canonical_name=args.name,
            unit_id=args.unit,
            membership_role=args.role,
            actor=args.actor,
            assignment_year=args.assignment_year,
            domain=domain,
            language=args.language,
            note=args.note,
        )
    finally:
        connection.close()

def command_core_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    output_db = Path(args.output_db).expanduser()
    require_safe_work_root(output_db.parent)
    return catalog_core.rebuild_from_generation(
        Path(args.generation).expanduser(),
        output_db,
    )

def command_core_status(args: argparse.Namespace) -> dict[str, Any]:
    return catalog_core.status(
        require_safe_work_root(Path(args.root).expanduser())
    )

def insert_projection_row(connection: sqlite3.Connection, table: str, payload: dict[str, Any]) -> None:
    columns = table_columns(connection, table)
    values = dict(payload)
    for local in LOCAL_ONLY_COLUMNS.get(table, set()):
        values.setdefault(local, "")
    selected = [column for column in columns if column in values]
    placeholders = ",".join("?" for _ in selected)
    quoted = ",".join(f'"{column}"' for column in selected)
    connection.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        tuple(values[column] for column in selected),
    )

def command_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    bags = [Path(path).resolve() for path in args.bag]
    output = Path(args.output_db).resolve()
    if output.exists():
        raise ArchiveError(f"rebuild output already exists: {output}")
    connection = create_schema(output, args.workstation_id or socket.gethostname(), args.actor)
    equivalence: dict[str, str] = {}
    try:
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'import_mode'")
        connection.commit()
        for bag in bags:
            validate_internal(bag)
            records = read_catalog_projection(bag / "data" / "catalog.csv")
            for table in PROJECTED_TABLES:
                for payload in records[table]:
                    insert_projection_row(connection, table, payload)
            with (bag / "data" / "identifiers.csv").open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    insert_projection_row(connection, "identifiers", row)
            with (bag / "data" / "logbook.jsonl").open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        insert_projection_row(connection, "logbook", json.loads(line))
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'import_mode'")
        connection.commit()
        assert_integrity(connection)
        for bag in bags:
            unit_id = read_catalog_projection(bag / "data" / "catalog.csv")["units"][0]["unit_id"]
            check_dir = output.parent / f".projection-check-{unit_id}-{uuid.uuid4().hex}"
            try:
                export_projections(connection, unit_id, check_dir)
                for filename in ("catalog.csv", "identifiers.csv", "logbook.jsonl"):
                    if (check_dir / filename).read_bytes() != (bag / "data" / filename).read_bytes():
                        raise ArchiveError(f"rebuilt projection differs for {unit_id}: {filename}")
                equivalence[unit_id] = "pass"
            finally:
                if check_dir.exists():
                    shutil.rmtree(check_dir)
    finally:
        connection.close()
    return {
        "result": "pass",
        "output_db": str(output),
        "bags": [str(path) for path in bags],
        "projection_equivalence": equivalence,
    }
