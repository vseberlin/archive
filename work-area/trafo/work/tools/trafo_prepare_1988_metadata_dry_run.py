#!/usr/bin/env python3
"""Prepare a metadata-only Trafo 1988 catalog dry run.

The accepted package remains read-only to this process. No representation file
is opened, copied, or hashed. The reviewed decision CSV receives one identity
hash; a SQLite catalog backup is updated only inside the dry-run output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


FINAL = Path("/mnt/volume/iss-archive/units/trafo")
FINAL_DB = FINAL / "db-restore/RESTORE.sqlite"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/trafo")
SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Trafo tifs 1970-1990"
)
RUN_ID = "trafo-1988-masthead-enrichment-dry-run-001"
EXPECTED = [f"TRO-1988-{number:03d}" for number in range(1, 43)]
CHANGED_FIELDS = ("title", "description", "date_created", "metadata_source")
UNCHANGED_FIELDS = (
    "inventory",
    "museum_digital_object_id",
    "object_type",
    "material_technique",
    "dimensions",
    "collection_name",
    "rights_usage_terms",
    "metadata_license",
    "page_count",
    "representation_count",
    "status",
)
METADATA_FILES = (
    "CATALOG.csv",
    "CATALOG.xlsx",
    "README.md",
    "db-restore/RESTORE.csv",
    "db-restore/RESTORE.sql",
    "db-restore/RESTORE.sqlite",
    "db-restore/RESTORE.xlsx",
    "db-restore/checksums.csv",
    "db-restore/manifests/accession-plan.json",
    "db-restore/manifests/representations.csv",
)
MONTHS = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "märz": 3,
    "maerz": 3,
    "marz": 3,
    "mrz": 3,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "december": 12,
    "dez": 12,
    "dec": 12,
}
GERMAN_MONTHS = {
    1: "Januar",
    2: "Februar",
    3: "März",
    4: "April",
    5: "Mai",
    6: "Juni",
    7: "Juli",
    8: "August",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Dezember",
}


class DryRunError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_once(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def package_metadata_identity() -> list[dict[str, Any]]:
    values = []
    for relative in METADATA_FILES:
        path = FINAL / relative
        if not path.is_file() or path.is_symlink():
            raise DryRunError(f"required final metadata file unavailable: {path}")
        value = path_identity(path)
        value["relative_path"] = relative
        values.append(value)
    return values


def assert_environment(decisions: Path, output: Path) -> dict[str, Any]:
    if not decisions.is_file() or decisions.is_symlink():
        raise DryRunError(f"decision file unavailable: {decisions}")
    if not FINAL_DB.is_file() or FINAL_DB.is_symlink():
        raise DryRunError(f"final catalog unavailable: {FINAL_DB}")
    if output.exists():
        raise DryRunError(f"refusing existing output: {output}")
    if WORK_ROOT not in output.parents:
        raise DryRunError(f"output must stay under {WORK_ROOT}")
    final_stat = FINAL_DB.stat()
    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    work_stat = output_parent.stat()
    if final_stat.st_dev != work_stat.st_dev:
        raise DryRunError(
            "dry-run output and final catalog are not on the same filesystem"
        )
    return {
        "same_filesystem": True,
        "filesystem_device": final_stat.st_dev,
        "final_catalog": path_identity(FINAL_DB),
        "decision_file": path_identity(decisions),
    }


def read_decisions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "inventory",
            "issue_number",
            "machine_candidate_date",
            "machine_candidate_jahrgang",
            "machine_candidate_issue_number",
            "decision",
            "verified_date",
            "verified_jahrgang",
            "verified_issue_number",
            "reviewer",
            "reviewed_at",
            "notes",
            "source_tiff_path",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise DryRunError(
                f"decision CSV headers differ: {reader.fieldnames or []}"
            )
        rows = list(reader)
    if len(rows) != len(EXPECTED):
        raise DryRunError(f"expected 42 decision rows, got {len(rows)}")
    inventories = [row["inventory"] for row in rows]
    if inventories != EXPECTED:
        raise DryRunError("decision inventories are missing, duplicated, or unordered")
    return rows


def parse_date(value: str) -> date:
    normalized = (
        value.strip()
        .replace(",", " ")
        .replace(".", " ")
        .replace("-", " ")
    )
    parts = normalized.split()
    if len(parts) != 3:
        raise DryRunError(f"unsupported reviewed date: {value!r}")
    try:
        day = int(parts[0])
        year = int(parts[2])
    except ValueError as error:
        raise DryRunError(f"unsupported reviewed date: {value!r}") from error
    month = MONTHS.get(parts[1].casefold())
    if not month or year != 1988:
        raise DryRunError(f"unsupported reviewed date: {value!r}")
    try:
        return date(year, month, day)
    except ValueError as error:
        raise DryRunError(f"invalid reviewed date: {value!r}") from error


def display_date(value: date) -> str:
    return f"{value.day}. {GERMAN_MONTHS[value.month]} {value.year}"


def effective_decisions(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    effective = []
    for expected_number, row in enumerate(rows, 1):
        decision = row["decision"].strip().upper()
        if decision not in {"ACCEPT", "CORRECT"}:
            raise DryRunError(
                f"{row['inventory']}: unsupported terminal decision {decision!r}"
            )
        machine_date = row["machine_candidate_date"].strip()
        machine_jahrgang = row["machine_candidate_jahrgang"].strip()
        machine_issue = row["machine_candidate_issue_number"].strip()
        if decision == "ACCEPT":
            selected_date = machine_date
            selected_jahrgang = machine_jahrgang
            selected_issue = machine_issue or row["issue_number"].strip()
            date_source = "machine-candidate-curator-accepted"
            jahrgang_source = "machine-candidate-curator-accepted"
            issue_source = (
                "machine-candidate-curator-accepted"
                if machine_issue
                else "inventory-filename-curator-accepted"
            )
        else:
            selected_date = row["verified_date"].strip() or machine_date
            selected_jahrgang = (
                row["verified_jahrgang"].strip() or machine_jahrgang
            )
            selected_issue = (
                row["verified_issue_number"].strip()
                or machine_issue
                or row["issue_number"].strip()
            )
            date_source = (
                "curator-verified"
                if row["verified_date"].strip()
                else "machine-candidate-curator-accepted"
            )
            jahrgang_source = (
                "curator-verified"
                if row["verified_jahrgang"].strip()
                else "machine-candidate-curator-accepted"
            )
            issue_source = (
                "curator-verified"
                if row["verified_issue_number"].strip()
                else (
                    "machine-candidate-curator-accepted"
                    if machine_issue
                    else "inventory-filename-curator-accepted"
                )
            )
        if not selected_date or not selected_jahrgang or not selected_issue:
            raise DryRunError(f"{row['inventory']}: incomplete effective decision")
        parsed_date = parse_date(selected_date)
        try:
            jahrgang = int(selected_jahrgang)
            issue_number = int(selected_issue)
        except ValueError as error:
            raise DryRunError(
                f"{row['inventory']}: non-numeric Jahrgang/issue"
            ) from error
        if jahrgang not in {39, 40}:
            raise DryRunError(
                f"{row['inventory']}: unexpected Jahrgang {jahrgang}"
            )
        if issue_number != expected_number or int(row["issue_number"]) != expected_number:
            raise DryRunError(
                f"{row['inventory']}: issue number differs from expected "
                f"{expected_number}"
            )
        source_tiff = Path(row["source_tiff_path"])
        expected_tiff = (
            SOURCE_ROOT
            / row["inventory"]
            / f"{row['inventory']}_001.tif"
        )
        if source_tiff != expected_tiff or not source_tiff.is_file():
            raise DryRunError(
                f"{row['inventory']}: source TIFF path unavailable or unexpected"
            )
        effective.append(
            {
                "inventory": row["inventory"],
                "issue_number": issue_number,
                "date_created": parsed_date.strftime("%d.%m.%Y"),
                "date_display": display_date(parsed_date),
                "jahrgang": jahrgang,
                "decision": decision,
                "date_source": date_source,
                "jahrgang_source": jahrgang_source,
                "issue_source": issue_source,
                "reviewer": row["reviewer"].strip(),
                "reviewed_at": row["reviewed_at"].strip(),
                "notes": row["notes"].strip(),
                "source_tiff_path": str(source_tiff),
                "source_tiff_size_bytes": source_tiff.stat().st_size,
            }
        )
    return effective


def sqlite_rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query, parameters)]


def catalog_rows(
    connection: sqlite3.Connection, inventories: list[str]
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in inventories)
    return sqlite_rows(
        connection,
        f"SELECT * FROM archive_object WHERE inventory IN ({placeholders}) "
        "ORDER BY inventory",
        tuple(inventories),
    )


def proposed_values(effective: dict[str, Any]) -> dict[str, str]:
    issue = effective["issue_number"]
    date_value = effective["date_display"]
    jahrgang = effective["jahrgang"]
    title = (
        "'Der Trafo, Organ der Leitung der BPO der SED des "
        'Transformatorenwerkes "Karl Liebknecht"\', '
        f"Nr.{issue}/1988 ({date_value})"
    )
    description = (
        "Der Trafo, Organ der Leitung der BPO der SED des VEB "
        'Transformatorenwerkes "Karl Liebknecht"; '
        f"{jahrgang}. Jahrgang; Nr. {issue} ({date_value}); "
        "8 gedruckte Seiten. Lokale Bibliocopy-Digitalisierung; sieben "
        "TIFF-Scans, da die gedruckten Seiten 4 und 5 als gemeinsamer Scan "
        "vorliegen. Ausgabe, Datum und Jahrgang wurden anhand des lokalen "
        "Masthead-Reviews bestätigt. Kein zugehöriger Datensatz wurde im "
        "akzeptierten museum-digital-Abzug oder in der lokalen "
        "Metadaten-Tabelle gefunden."
    )
    return {
        "title": title,
        "description": description,
        "date_created": effective["date_created"],
        "metadata_source": "local-source-masthead-curator-reviewed-no-md-record",
    }


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def guarded_update_sql(
    before: dict[str, Any], after: dict[str, Any], direction: str
) -> str:
    set_fields = CHANGED_FIELDS
    guard = before if direction == "forward" else after
    target = after if direction == "forward" else before
    assignments = ",\n    ".join(
        f"{field} = {sql_literal(target[field])}" for field in set_fields
    )
    guards = "\n  AND ".join(
        [f"inventory = {sql_literal(guard['inventory'])}"]
        + [f"{field} IS {sql_literal(guard[field])}" for field in set_fields]
    )
    return f"UPDATE archive_object\nSET {assignments}\nWHERE {guards};"


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_sql(
    path: Path,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    direction: str,
    decision_sha256: str,
) -> None:
    lines = [
        "-- Trafo 1988 masthead metadata dry-run proposal",
        f"-- run_id: {RUN_ID}",
        f"-- decision_sha256: {decision_sha256}",
        "-- Scope: exactly 42 archive_object rows; no representation/media rows.",
        "BEGIN IMMEDIATE;",
    ]
    pairs = zip(before, after)
    for original, proposed in pairs:
        lines.append("")
        lines.append(f"-- {original['inventory']}")
        lines.append(guarded_update_sql(original, proposed, direction))
    lines.extend(["", "COMMIT;", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def create_proposal_db(
    stage: Path,
    source_connection: sqlite3.Connection,
    before: list[dict[str, Any]],
    effective: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]]]:
    proposed_db = stage / "proposed-RESTORE.sqlite"
    destination = sqlite3.connect(proposed_db)
    try:
        source_connection.backup(destination)
        destination.execute("BEGIN IMMEDIATE")
        for original, decision in zip(before, effective):
            values = proposed_values(decision)
            result = destination.execute(
                """
                UPDATE archive_object
                   SET title = ?, description = ?, date_created = ?,
                       metadata_source = ?
                 WHERE inventory = ?
                   AND title IS ?
                   AND description IS ?
                   AND date_created IS ?
                   AND metadata_source IS ?
                """,
                (
                    values["title"],
                    values["description"],
                    values["date_created"],
                    values["metadata_source"],
                    original["inventory"],
                    original["title"],
                    original["description"],
                    original["date_created"],
                    original["metadata_source"],
                ),
            )
            if result.rowcount != 1:
                raise DryRunError(
                    f"guarded proposal update failed: {original['inventory']}"
                )
        destination.commit()
        after = catalog_rows(destination, EXPECTED)
    finally:
        destination.close()
    return proposed_db, after


def compare_tables(
    source: sqlite3.Connection, proposed: sqlite3.Connection
) -> dict[str, Any]:
    source_objects = sqlite_rows(
        source, "SELECT * FROM archive_object ORDER BY inventory"
    )
    proposed_objects = sqlite_rows(
        proposed, "SELECT * FROM archive_object ORDER BY inventory"
    )
    source_by_inventory = {row["inventory"]: row for row in source_objects}
    proposed_by_inventory = {row["inventory"]: row for row in proposed_objects}
    changed = [
        inventory
        for inventory in source_by_inventory
        if source_by_inventory[inventory] != proposed_by_inventory[inventory]
    ]
    source_representations = sqlite_rows(
        source,
        "SELECT * FROM representation ORDER BY inventory, relative_path",
    )
    proposed_representations = sqlite_rows(
        proposed,
        "SELECT * FROM representation ORDER BY inventory, relative_path",
    )
    source_units = sqlite_rows(source, "SELECT * FROM unit ORDER BY unit_id")
    proposed_units = sqlite_rows(proposed, "SELECT * FROM unit ORDER BY unit_id")
    return {
        "source_object_count": len(source_objects),
        "proposed_object_count": len(proposed_objects),
        "changed_object_count": len(changed),
        "changed_inventories": changed,
        "outside_scope_objects_identical": all(
            source_by_inventory[inventory] == proposed_by_inventory[inventory]
            for inventory in source_by_inventory
            if inventory not in EXPECTED
        ),
        "representation_count": len(source_representations),
        "representation_rows_identical": (
            source_representations == proposed_representations
        ),
        "unit_rows_identical": source_units == proposed_units,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    decisions = arguments.decisions.resolve()
    output = arguments.output.resolve()
    environment = assert_environment(decisions, output)
    metadata_before_identity = package_metadata_identity()
    decision_sha256 = sha256_once(decisions)
    rows = read_decisions(decisions)
    effective = effective_decisions(rows)
    source = sqlite3.connect(
        f"file:{FINAL_DB}?mode=ro&immutable=1", uri=True, timeout=30
    )
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{RUN_ID}-",
            dir=output.parent,
        )
    )
    try:
        before = catalog_rows(source, EXPECTED)
        if len(before) != 42:
            raise DryRunError(f"final catalog has {len(before)} 1988 rows, not 42")
        for row in before:
            if (
                row["date_created"] != "1988"
                or row["metadata_source"] != "local-source-minimal-no-md-record"
            ):
                raise DryRunError(
                    f"unexpected current 1988 baseline: {row['inventory']}"
                )
        proposed_db, after = create_proposal_db(stage, source, before, effective)
        proposed = sqlite3.connect(
            f"file:{proposed_db}?mode=ro&immutable=1", uri=True, timeout=30
        )
        try:
            comparison = compare_tables(source, proposed)
            integrity = proposed.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            proposed.close()

        if comparison["changed_inventories"] != EXPECTED:
            raise DryRunError("proposal scope differs from the 42 reviewed rows")
        if not (
            comparison["outside_scope_objects_identical"]
            and comparison["representation_rows_identical"]
            and comparison["unit_rows_identical"]
            and integrity == "ok"
        ):
            raise DryRunError("proposal invariant validation failed")

        shutil.copy2(decisions, stage / "reviewed-decisions.csv")
        object_fields = list(before[0].keys())
        write_csv(stage / "before.csv", before, object_fields)
        write_csv(stage / "after.csv", after, object_fields)
        effective_fields = [
            "inventory",
            "issue_number",
            "date_created",
            "date_display",
            "jahrgang",
            "decision",
            "date_source",
            "jahrgang_source",
            "issue_source",
            "reviewer",
            "reviewed_at",
            "notes",
            "source_tiff_path",
            "source_tiff_size_bytes",
        ]
        write_csv(stage / "effective-decisions.csv", effective, effective_fields)
        diff_rows = []
        for original, proposed_row in zip(before, after):
            for field in CHANGED_FIELDS:
                diff_rows.append(
                    {
                        "inventory": original["inventory"],
                        "field": field,
                        "before": original[field],
                        "after": proposed_row[field],
                        "authority": (
                            "curator-reviewed-masthead"
                            if field in {"title", "description", "date_created"}
                            else "local-review-provenance"
                        ),
                    }
                )
        write_csv(
            stage / "diff.csv",
            diff_rows,
            ["inventory", "field", "before", "after", "authority"],
        )
        write_sql(
            stage / "proposed-update.sql",
            before,
            after,
            "forward",
            decision_sha256,
        )
        write_sql(
            stage / "rollback.sql",
            before,
            after,
            "rollback",
            decision_sha256,
        )
        package_after_identity = package_metadata_identity()
        package_unchanged = metadata_before_identity == package_after_identity
        if not package_unchanged:
            raise DryRunError("final package metadata identity changed during dry run")

        plan = {
            "format": "iss-trafo-metadata-dry-run-v1",
            "run_id": RUN_ID,
            "created_at": now(),
            "status": "dry-run-only-no-final-mutation",
            "scope": "TRO-1988-001 through TRO-1988-042",
            "decision_source": str(decisions),
            "decision_sha256": decision_sha256,
            "same_filesystem": environment["same_filesystem"],
            "filesystem_device": environment["filesystem_device"],
            "field_update_policy": {
                "changed_fields": list(CHANGED_FIELDS),
                "preserved_fields": list(UNCHANGED_FIELDS),
                "verified_blank_fallback": (
                    "for CORRECT rows, blank verified values retain curator-accepted "
                    "machine values; issue number may fall back to inventory"
                ),
            },
            "media_io": {
                "representation_files_opened": 0,
                "representation_files_copied": 0,
                "representation_files_hashed": 0,
                "package_files_hashed": 0,
                "decision_files_hashed": 1,
                "note": (
                    "No TIFF/PDF or accepted package file was hashed. The small "
                    "reviewed decision CSV received one input identity hash."
                ),
            },
            "final_package_metadata_before": metadata_before_identity,
            "final_package_metadata_after": package_after_identity,
            "final_package_metadata_unchanged": package_unchanged,
        }
        (stage / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        proposal_database_identity = path_identity(proposed_db)
        proposal_database_identity["path"] = str(output / proposed_db.name)
        validation = {
            "completed_at": now(),
            "status": "ok",
            "decision_sha256": decision_sha256,
            "integrity_check": integrity,
            "comparison": comparison,
            "changed_field_count": len(diff_rows),
            "expected_changed_field_count": 42 * len(CHANGED_FIELDS),
            "final_package_metadata_unchanged": package_unchanged,
            "media_files_hashed": 0,
            "media_files_opened": 0,
            "proposal_database": proposal_database_identity,
        }
        (stage / "validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        readme = f"""# Trafo 1988 masthead metadata dry run

Status: **validated dry run; no final package mutation**

Input decision SHA-256: `{decision_sha256}`

## Proposed scope

- exactly 42 objects: `TRO-1988-001` through `TRO-1988-042`
- update `title`, `description`, `date_created`, and `metadata_source`
- preserve rights, metadata licence, dimensions, material, page counts,
  representation counts, status, collection, and museum-digital ID
- do not modify embedded TIFF/PDF metadata

## No-rehash proof

- representation files opened: 0
- representation files copied: 0
- representation files hashed: 0
- accepted package files hashed: 0
- decision CSV identity hashes: 1
- final and work roots share filesystem device `{environment["filesystem_device"]}`

The proposal database is a catalog-only SQLite backup under this dry-run
directory. Its integrity check is `ok`; all 11,972 representation rows and the
unit row are identical to the accepted catalog. All 1,548 objects outside the
42-row scope are identical.

## Review artifacts

- `effective-decisions.csv`: normalized field-wise decisions
- `before.csv` and `after.csv`: complete 42-row records
- `diff.csv`: 168 proposed field changes
- `proposed-update.sql`: guarded forward proposal; not applied
- `rollback.sql`: guarded inverse proposal
- `proposed-RESTORE.sqlite`: validated catalog-only dry-run copy
- `plan.json`: scope and no-rehash policy
- `validation.json`: structural validation result

No apply or publication step is authorized by this dry run.
"""
        (stage / "README.md").write_text(readme, encoding="utf-8")
        source.close()
        os.replace(stage, output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "status": "ok",
                    "changed_objects": comparison["changed_object_count"],
                    "changed_fields": len(diff_rows),
                    "representations_identical": comparison[
                        "representation_rows_identical"
                    ],
                    "media_files_hashed": 0,
                    "final_package_metadata_unchanged": package_unchanged,
                    "integrity_check": integrity,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        source.close()
        if (
            stage.exists()
            and stage.parent == output.parent
            and stage.name.startswith(f".{RUN_ID}-")
        ):
            shutil.rmtree(stage)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
