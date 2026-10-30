from __future__ import annotations

import csv
from contextlib import closing
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "archive-preservation" / "archive.py"
SPEC = importlib.util.spec_from_file_location("iss_archive_pilot", SCRIPT)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


class ArchiveMiniPilotTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.source = self.base / "source"
        self.preservation = self.base / "preservation"
        self.source.mkdir()
        self.env = dict(os.environ)
        self.env["ISS_ARCHIVE_FIXTURE_SAME_DEVICE"] = "1"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str, expect: int = 0) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, arguments)],
            cwd=REPO_ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(expect, result.returncode, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        output = result.stdout if result.returncode == 0 else result.stderr
        return json.loads(output)

    def create_object(self, display: str, label: str) -> str:
        result = self.run_cli(
            "create-object",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--assignment-year", "2026",
            "--display-identifier", display,
            "--object-type", "photograph",
            "--label", label,
        )
        return result["object_id"]

    def intake(self, relative: str, role: str) -> str:
        result = self.run_cli(
            "intake",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--batch", "fixture-batch",
            "--source-root-id", "fixture-source",
            "--source-root", self.source,
            "--path", relative,
            "--role", role,
        )
        return result["source_file_id"]

    def link(self, subject_type: str, subject_id: str, predicate: str, object_type: str, object_id: str) -> None:
        self.run_cli(
            "link",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--subject-type", subject_type,
            "--subject-id", subject_id,
            "--predicate", predicate,
            "--object-type", object_type,
            "--object-id", object_id,
            "--status", "accepted",
            "--confidence", "certain",
        )

    def test_four_symbol_archival_id_format(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE objects (object_id TEXT)")
            connection.execute("CREATE TABLE events (event_id TEXT)")
            value = ARCHIVE.generate_archival_id(connection, "object", 2026)
        self.assertRegex(value, r"^ISS-A-2026-[0-9A-HJKMNP-TV-Z]{4}$")
        self.assertTrue(ARCHIVE.validate_archival_id(value))
        self.assertFalse(ARCHIVE.validate_archival_id(value + "[4]"))
        self.assertFalse(ARCHIVE.validate_archival_id("ISS-A-2026-ILOU"))

    def test_catalog_state_generation_excludes_rebuildable_source_registry(self) -> None:
        self.run_cli("init", "--root", self.root, "--actor", "fixture")
        self.run_cli(
            "create-unit",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--title", "Compact catalog-state fixture",
            "--kind", "fixture",
        )
        db = self.root / "work" / "catalog.sqlite"
        with ARCHIVE.connect_db(db) as connection:
            generation = ARCHIVE.create_catalog_state_generation(
                connection,
                self.root / "work" / "backups",
                "COMPACT-FIXTURE",
            )
        with closing(sqlite3.connect(generation)) as backup:
            tables = {
                row[0]
                for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            unit = backup.execute(
                "SELECT title, status FROM units WHERE unit_id = 'TEST-FIXTURE-V2'"
            ).fetchone()
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(("Compact catalog-state fixture", "open"), unit)
        self.assertEqual("ok", integrity)
        self.assertFalse(any(name.startswith("source_registry_") for name in tables))
        self.assertIn("metadata_decisions", tables)

    def test_legacy_combined_database_splits_without_changing_it(self) -> None:
        legacy_root = self.base / "legacy"
        legacy = legacy_root / "work" / "index.sqlite"
        legacy.parent.mkdir(parents=True)
        connection = ARCHIVE.create_schema(legacy, "fixture", "fixture")
        connection.execute(
            """INSERT INTO units (
                   unit_id, title, kind, status, created_at, sealed_at
               ) VALUES (?, ?, ?, 'open', ?, NULL)""",
            ("TEST-SPLIT", "Split fixture", "fixture", ARCHIVE.utc_now()),
        )
        ARCHIVE.source_registry.ensure_schema(connection)
        connection.commit()
        connection.close()
        (legacy_root / "work" / "catalog-mode.json").write_text(
            json.dumps(
                {
                    "catalog_mode": ARCHIVE.LEGACY_FIXTURE_MODE,
                    "purpose": "split fixture",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        before = ARCHIVE.sha256_file(legacy)

        result = ARCHIVE.command_split_working_databases(
            type("Args", (), {"root": str(legacy_root)})()
        )

        catalog = legacy_root / "work" / "catalog.sqlite"
        self.assertEqual(str(catalog), result["catalog_db"])
        self.assertEqual(before, ARCHIVE.sha256_file(legacy))
        with closing(sqlite3.connect(catalog)) as separated:
            self.assertEqual(
                ("Split fixture",),
                separated.execute(
                    "SELECT title FROM units WHERE unit_id='TEST-SPLIT'"
                ).fetchone(),
            )
            self.assertFalse(
                separated.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='source_registry_runs'"
                ).fetchone()
            )

    def test_catalog_mode_never_falls_back_to_legacy_index(self) -> None:
        root = self.base / "wrong-database-guard"
        work = root / "work"
        work.mkdir(parents=True)
        connection = ARCHIVE.create_schema(
            work / "index.sqlite",
            "fixture",
            "fixture",
        )
        connection.close()
        with self.assertRaisesRegex(
            ARCHIVE.ArchiveError,
            "refusing existence-based fallback",
        ):
            ARCHIVE.root_paths(root)

    def test_archive_modules_use_the_same_timestamp_precision(self) -> None:
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
        self.assertRegex(ARCHIVE.utc_now(), pattern)
        self.assertRegex(ARCHIVE.catalog_core.utc_now(), pattern)

    def test_unmarked_legacy_catalog_is_read_only(self) -> None:
        root = self.base / "legacy-read-only"
        catalog = root / "work" / "catalog.sqlite"
        catalog.parent.mkdir(parents=True)
        connection = ARCHIVE.create_schema(catalog, "fixture", "fixture")
        connection.close()
        paths = ARCHIVE.root_paths(root)
        self.assertEqual("legacy-read-only", paths["mode"])
        with ARCHIVE.connect_db(catalog, read_only=True) as connection:
            self.assertEqual(
                "ok",
                connection.execute("PRAGMA integrity_check").fetchone()[0],
            )
        with self.assertRaisesRegex(
            ARCHIVE.ArchiveError,
            "legacy catalog writes are frozen",
        ):
            ARCHIVE.connect_db(catalog)

    def test_protected_source_trees_cannot_be_work_roots(self) -> None:
        with self.assertRaisesRegex(
            ARCHIVE.ArchiveError, "protected source tree"
        ):
            ARCHIVE.root_paths(Path("/mnt/volume/archive"))

    def test_intake_refuses_writable_usb_source(self) -> None:
        usb = self.base / "usb"
        usb.mkdir()
        (usb / "source.tif").write_bytes(b"fixture")
        args = type(
            "Args",
            (),
            {
                "root": str(self.root),
                "source_root": str(usb),
                "source_root_id": "usb-fixture",
                "path": "source.tif",
            },
        )()
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "mounted read-only"):
            ARCHIVE.command_intake(args)

    def test_contract_fixture_end_to_end(self) -> None:
        duplicate_bytes = b"fixture master bytes\n" * 20
        files = {
            "masters/a.tif": duplicate_bytes,
            "duplicates/a.tif": duplicate_bytes,
            "raw/museum-digital.json": b'{"inventory":"CONFLICT-A"}\n',
            "raw/testimony.txt": "Das war ungefähr im Frühjahr.".encode(),
        }
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        source_before = {relative: (path.stat().st_size, ARCHIVE.sha256_file(path)) for relative in files for path in [self.source / relative]}

        self.run_cli("init", "--root", self.root, "--actor", "fixture")
        self.run_cli(
            "create-unit",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--title", "Accepted-model contract fixture",
            "--kind", "fixture",
        )

        incomplete = self.root / "work" / "seal" / "TEST-FIXTURE-V2.incomplete"
        incomplete.mkdir()
        preseal = self.run_cli("status", "--root", self.root)
        self.assertEqual("critical", preseal["overall"])
        self.assertEqual(
            {"safe", "yellow", "red", "critical"},
            {condition["severity"] for condition in preseal["conditions"]},
        )

        first = self.create_object("FIX-001", "First fixture object")
        missing = self.create_object("FIX-002", "Object with unresolved missing master")
        conflict = self.create_object("FIX-003", "Object with conflicting inventory assertions")
        for object_id in (first, missing, conflict):
            self.assertTrue(ARCHIVE.validate_archival_id(object_id))

        event = self.run_cli(
            "create-event",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--assignment-year", "2026",
            "--title", "Fixture event",
            "--starts-on", "1970",
            "--circa",
        )["event_id"]
        testimony = self.run_cli(
            "add-testimony",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--event-id", event,
            "--contributor", "Fixture contributor",
            "--recorded-at", "2026-07-13",
            "--verbatim", "Das war ungefähr im Frühjahr.",
            "--confidence", "possible",
            "--status", "unresolved",
        )["testimony_id"]

        for value, status in (("CONFLICT-A", "accepted"), ("CONFLICT-B", "unresolved")):
            self.run_cli(
                "add-identifier",
                "--root", self.root,
                "--actor", "fixture",
                "--unit", "TEST-FIXTURE-V2",
                "--entity-type", "object",
                "--entity-id", conflict,
                "--scheme", "inventory",
                "--value", value,
                "--status", status,
            )

        first_master = self.intake("masters/a.tif", "master")
        duplicate_master = self.intake("duplicates/a.tif", "master")
        raw_metadata = self.intake("raw/museum-digital.json", "source")
        raw_testimony = self.intake("raw/testimony.txt", "source")
        missing_source = self.source / "raw" / "testimony.txt"
        missing_bytes = missing_source.read_bytes()
        missing_source.unlink()
        missing_report = self.run_cli("status", "--root", self.root)
        self.assertIn(
            "expected_source_file_missing",
            {condition["condition"] for condition in missing_report["conditions"]},
        )
        missing_source.write_bytes(missing_bytes)
        self.link("object", first, "primary_master", "source_file", first_master)
        self.link("object", conflict, "primary_master", "source_file", duplicate_master)
        self.link("object", conflict, "source_metadata", "source_file", raw_metadata)
        self.link("testimony", testimony, "source_record", "source_file", raw_testimony)
        self.run_cli(
            "add-gap",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--entity-type", "object",
            "--entity-id", missing,
            "--gap-type", "missing_master",
            "--detail", "No master relationship is known.",
            "--next-action", "Retain as explicit unresolved evidence.",
            "--status", "open",
        )

        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            before = connection.execute("SELECT COUNT(*) FROM logbook").fetchone()[0]
            connection.execute(
                "UPDATE session_context SET actor = 'db-browser-fixture', evidence = 'manual review', note = 'view write' WHERE singleton = 1"
            )
            connection.execute("UPDATE edit_objects SET note = 'Reviewed through writable view' WHERE object_id = ?", (first,))
            connection.execute("UPDATE edit_identifiers SET status = 'rejected' WHERE value = 'CONFLICT-B'")
            connection.commit()
            after = connection.execute("SELECT COUNT(*) FROM logbook").fetchone()[0]
        self.assertGreaterEqual(after - before, 2)

        sealed = self.run_cli(
            "seal",
            "--root", self.root,
            "--actor", "fixture",
            "--unit", "TEST-FIXTURE-V2",
            "--preservation-root", self.preservation,
            "--validator-python", sys.executable,
        )
        self.assertEqual(1, sealed["duplicate_occurrences"])
        primary = Path(sealed["primary"])
        preservation = Path(sealed["preservation_copy"])
        self.assertTrue(primary.is_dir())
        self.assertTrue(preservation.is_dir())
        self.assertFalse(incomplete.exists())

        refreshed = self.run_cli(
            "refresh-controls",
            "--root", self.root,
            "--actor", "fixture",
            "--note", "Refresh DB Browser guards",
        )
        self.assertIn("edit_identifiers", refreshed["controls"])
        self.assertTrue(Path(refreshed["before_generation"]).is_file())
        self.assertTrue(Path(refreshed["after_generation"]).is_file())

        with closing(sqlite3.connect(db)) as connection:
            connection.execute(
                "UPDATE session_context SET actor = 'db-browser-fixture', evidence = 'sealed edit test', note = 'must fail' WHERE singleton = 1"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "sealed units are read-only"):
                connection.execute("UPDATE edit_objects SET note = 'Forbidden sealed edit' WHERE object_id = ?", (first,))
            connection.rollback()

        with (primary / "data" / "catalog.csv").open("r", encoding="utf-8", newline="") as handle:
            catalog = list(csv.DictReader(handle))
        source_rows = [json.loads(row["payload_json"]) for row in catalog if row["record_type"] == "source_files"]
        duplicate_rows = [row for row in source_rows if row["file_id"] == f"sha256:{ARCHIVE.sha256_file(self.source / 'masters/a.tif')}"]
        self.assertEqual(2, len(duplicate_rows))
        self.assertEqual(2, len({row["archive_path"] for row in duplicate_rows}))

        rebuilt = self.base / "rebuilt.sqlite"
        self.run_cli(
            "rebuild",
            "--bag", primary,
            "--output-db", rebuilt,
            "--actor", "fixture-rebuild",
        )
        with closing(sqlite3.connect(rebuilt)) as connection:
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
            self.assertEqual(4, connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM gaps").fetchone()[0])

        unexpected = primary / "data" / ".~lock.catalog.csv#"
        unexpected.write_text("fixture editor lock", encoding="utf-8")
        unexpected_status = self.run_cli("status", "--root", self.root)
        self.assertEqual("critical", unexpected_status["overall"])
        self.assertIn("payload_inventory", {row["condition"] for row in unexpected_status["conditions"]})
        unexpected.unlink()

        final_status = self.run_cli("status", "--root", self.root)
        self.assertEqual("safe", final_status["overall"])

        source_after = {relative: (path.stat().st_size, ARCHIVE.sha256_file(path)) for relative in files for path in [self.source / relative]}
        self.assertEqual(source_before, source_after)

        corrupt = self.base / "corrupt"
        shutil.copytree(primary, corrupt)
        corrupt_master = next((corrupt / "data" / "masters").iterdir())
        corrupt_master.chmod(0o644)
        with corrupt_master.open("ab") as handle:
            handle.write(b"corruption")
        failed = self.run_cli(
            "validate",
            "--bag", corrupt,
            "--validator-python", sys.executable,
            expect=3,
        )
        self.assertEqual("fail", failed["result"])


if __name__ == "__main__":
    unittest.main()
