from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "archive-preservation"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

ARCHIVE_SPEC = importlib.util.spec_from_file_location("iss_archive_source_registry_test", TOOL_DIR / "archive.py")
assert ARCHIVE_SPEC and ARCHIVE_SPEC.loader
ARCHIVE = importlib.util.module_from_spec(ARCHIVE_SPEC)
ARCHIVE_SPEC.loader.exec_module(ARCHIVE)


class SourceRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.archive_root = self.base / "archive"
        self.source_a = self.base / "source-a"
        self.source_b = self.base / "source-b"
        self.source_a.mkdir()
        self.source_b.mkdir()
        ARCHIVE.command_init(SimpleNamespace(root=str(self.archive_root), actor="fixture", note="", workstation_id=None))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def scan(self) -> dict[str, object]:
        return ARCHIVE.command_scan_source_registry(
            SimpleNamespace(
                root=str(self.archive_root),
                actor="fixture",
                note="duplicate-aware fixture",
                source_root=[f"source-a={self.source_a}", f"source-b={self.source_b}"],
                max_hash_bytes=1024 * 1024,
                extract_pdf=False,
                replace_derived_index=False,
                output="",
            )
        )

    def test_exact_duplicates_share_blob_but_conflicting_assertions_survive(self) -> None:
        original = (
            "inventory_number;object_title;object_description;printed_when_1\n"
            "VLV-1130-1;Wall newspaper;Concert on 1 May;1987\n"
        )
        changed = (
            "inventory_number;object_title;object_description;printed_when_1\n"
            "VLV-1130-1;Concert photograph;Concert on 1 May 1974;1974\n"
        )
        (self.source_a / "Bildtexte.csv").write_text(original, encoding="utf-8")
        (self.source_b / "Bildtexte-copy.csv").write_text(original, encoding="utf-8")
        (self.source_b / "Bildtexte-revised.csv").write_text(changed, encoding="utf-8")

        result = self.scan()
        self.assertEqual(3, result["occurrences_recorded"])
        self.assertEqual(2, result["unique_blobs"])
        self.assertEqual(1, result["duplicate_groups"])
        self.assertGreaterEqual(result["assertions_extracted"], 8)

        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            blobs = connection.execute("SELECT COUNT(*) FROM source_registry_blobs").fetchone()[0]
            occurrences = connection.execute("SELECT COUNT(*) FROM source_registry_occurrences").fetchone()[0]
            duplicate = connection.execute(
                """SELECT COUNT(*) FROM source_registry_occurrences
                   WHERE blob_id=(SELECT blob_id FROM source_registry_occurrences
                                  WHERE original_filename='Bildtexte.csv')"""
            ).fetchone()[0]
            dates = connection.execute(
                """SELECT normalized_value, COUNT(*) records
                   FROM source_registry_assertions
                   WHERE entity_hint='VLV-1130-1' AND field_name='printed_when_1'
                   GROUP BY normalized_value ORDER BY normalized_value"""
            ).fetchall()
            conflict_groups = connection.execute(
                """SELECT COUNT(*) FROM (
                       SELECT entity_hint, field_name FROM source_registry_assertions
                       GROUP BY entity_hint, field_name
                       HAVING COUNT(DISTINCT normalized_value) > 1
                   )"""
            ).fetchone()[0]
        self.assertEqual(2, blobs)
        self.assertEqual(3, occurrences)
        self.assertEqual(2, duplicate)
        self.assertEqual([("1974", 1), ("1987", 1)], [tuple(row) for row in dates])
        self.assertGreaterEqual(conflict_groups, 1)

        rerun = self.scan()
        self.assertEqual(2, rerun["unique_blobs"])
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM source_registry_occurrences").fetchone()[0])
            self.assertEqual(8, connection.execute("SELECT COUNT(*) FROM source_registry_assertions").fetchone()[0])

        self.assertEqual(original, (self.source_a / "Bildtexte.csv").read_text(encoding="utf-8"))

    def test_core_mode_scans_only_the_dedicated_registry(self) -> None:
        core_root = self.base / "minimal-core"
        ARCHIVE.catalog_core.initialize_root(
            core_root,
            tool_version="test",
            actor="fixture",
        )
        (self.source_a / "core.csv").write_text(
            "inventory_number;title\nVLV-CORE;Core registry evidence\n",
            encoding="utf-8",
        )
        paths = ARCHIVE.root_paths(core_root)
        self.assertTrue(paths["registry_writable"])
        self.assertEqual(paths["dedicated_registry_db"], paths["registry_db"])
        before = ARCHIVE.catalog_core.connect_catalog(
            core_root / "work" / "catalog.sqlite",
            read_only=True,
        )
        try:
            revision = int(
                ARCHIVE.catalog_core.catalog_state(before)["catalog_revision"]
            )
        finally:
            before.close()

        result = ARCHIVE.command_scan_source_registry(
            SimpleNamespace(
                root=str(core_root),
                actor="fixture",
                note="core dedicated registry",
                source_root=[f"source-a={self.source_a}"],
                max_hash_bytes=1024 * 1024,
                extract_pdf=False,
                replace_derived_index=False,
                output="",
            )
        )

        self.assertEqual(1, result["occurrences_recorded"])
        self.assertTrue(paths["dedicated_registry_db"].is_file())
        self.assertFalse((core_root / "work" / "index.sqlite").exists())
        after = ARCHIVE.catalog_core.connect_catalog(
            core_root / "work" / "catalog.sqlite",
            read_only=True,
        )
        try:
            self.assertEqual(
                revision,
                int(ARCHIVE.catalog_core.catalog_state(after)["catalog_revision"]),
            )
        finally:
            after.close()

    def test_large_candidate_is_registered_without_hashing(self) -> None:
        candidate = self.source_a / "large-metadata.txt"
        candidate.write_text("VLV-9999 metadata", encoding="utf-8")
        result = ARCHIVE.command_scan_source_registry(
            SimpleNamespace(
                root=str(self.archive_root), actor="fixture", note="deferred fixture",
                source_root=[f"source-a={self.source_a}"], max_hash_bytes=4,
                extract_pdf=False, replace_derived_index=False, output="",
            )
        )
        self.assertEqual(1, result["occurrences_recorded"])
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            row = connection.execute(
                "SELECT hash_status, blob_id, note FROM source_registry_occurrences WHERE original_filename=?",
                (candidate.name,),
            ).fetchone()
        self.assertEqual(("deferred", None, "larger than hash limit 4"), row)

    def test_writable_usb_root_is_refused(self) -> None:
        candidate = self.source_a / "metadata.csv"
        candidate.write_text("inventory_number;title\nVLV-1;Fixture\n", encoding="utf-8")
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "mounted read-only"):
            ARCHIVE.command_scan_source_registry(
                SimpleNamespace(
                    root=str(self.archive_root),
                    actor="fixture",
                    note="unsafe USB fixture",
                    source_root=[f"usb-fixture={self.source_a}"],
                    max_hash_bytes=1024 * 1024,
                    extract_pdf=False,
                    replace_derived_index=False,
                    output="",
                )
            )
        self.assertEqual(
            "inventory_number;title\nVLV-1;Fixture\n",
            candidate.read_text(encoding="utf-8"),
        )

    def test_pdf_text_extraction_is_explicitly_deferred(self) -> None:
        candidate = self.source_a / "catalog.pdf"
        candidate.write_bytes(b"%PDF-1.4\nfixture only\n")
        result = self.scan()
        self.assertEqual(1, result["occurrences_recorded"])
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            row = connection.execute(
                "SELECT extraction_status, parser, extraction_error FROM source_registry_blobs"
            ).fetchone()
        self.assertEqual(
            ("identified", "candidate-only", "PDF text extraction deferred"), row
        )

    def test_pdf_hash_can_be_staged_after_occurrence_census(self) -> None:
        candidate = self.source_a / "large-set.pdf"
        candidate.write_bytes(b"%PDF-1.4\nfixture only\n")
        result = ARCHIVE.command_scan_source_registry(
            SimpleNamespace(
                root=str(self.archive_root), actor="fixture", note="PDF census",
                source_root=[f"source-a={self.source_a}"],
                max_hash_bytes=1024 * 1024, defer_hash_suffix=[".pdf"],
                extract_pdf=False, replace_derived_index=False, output="",
            )
        )
        self.assertEqual(1, result["hashes_deferred"])
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            row = connection.execute(
                "SELECT hash_status, blob_id, note FROM source_registry_occurrences"
            ).fetchone()
        self.assertEqual(("deferred", None, "hash deferred by suffix .pdf"), row)

    def test_unknown_xml_encoding_is_reported_without_aborting_scan(self) -> None:
        malformed = self.source_a / "unknown-encoding.xml"
        malformed.write_bytes(
            b'<?xml version="1.0" encoding="unicode"?><record><id>VLV-1</id></record>'
        )
        valid = self.source_a / "metadata.csv"
        valid.write_text("inventory_number;title\nVLV-2;Fixture\n", encoding="utf-8")

        result = self.scan()

        self.assertEqual(2, result["occurrences_recorded"])
        self.assertEqual(1, result["errors"])
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            error_row = connection.execute(
                """SELECT b.extraction_status, b.extraction_error
                   FROM source_registry_blobs b
                   JOIN source_registry_occurrences o ON o.blob_id=b.blob_id
                   WHERE o.original_filename=?""",
                (malformed.name,),
            ).fetchone()
            run_status = connection.execute(
                "SELECT status FROM source_registry_runs ORDER BY started_at_utc DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual("error", error_row[0])
        self.assertIn("LookupError: unknown encoding: unicode", error_row[1])
        self.assertEqual("complete", run_status)
        self.assertEqual(
            b'<?xml version="1.0" encoding="unicode"?><record><id>VLV-1</id></record>',
            malformed.read_bytes(),
        )

    def test_derived_registry_reset_keeps_sources_and_recreates_schema(self) -> None:
        candidate = self.source_a / "metadata.csv"
        candidate.write_text("inventory_number;title\nVLV-1;Fixture\n", encoding="utf-8")
        self.scan()
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            secure_delete_before = connection.execute("PRAGMA secure_delete").fetchone()[0]
            ARCHIVE.source_registry.clear_derived_registry(connection)
            secure_delete_after = connection.execute("PRAGMA secure_delete").fetchone()[0]
            counts = [
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "source_registry_runs",
                    "source_registry_roots",
                    "source_registry_blobs",
                    "source_registry_occurrences",
                    "source_registry_records",
                    "source_registry_assertions",
                )
            ]
        self.assertEqual(secure_delete_before, secure_delete_after)
        self.assertEqual([0, 0, 0, 0, 0, 0], counts)
        self.assertEqual(
            "inventory_number;title\nVLV-1;Fixture\n",
            candidate.read_text(encoding="utf-8"),
        )

    def test_xlsx_terminal_formatting_column_is_ignored(self) -> None:
        candidate = self.source_a / "formatted-tail.xlsx"
        with zipfile.ZipFile(candidate, "w") as archive:
            archive.writestr(
                "xl/workbook.xml",
                '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>''',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>''',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>inventory_number</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c><c r="XFD1" t="inlineStr"><is><t>formatting-only</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>VLV-1</t></is></c><c r="B2" t="inlineStr"><is><t>Fixture</t></is></c><c r="XFD2" t="inlineStr"><is><t>x</t></is></c></row></sheetData></worksheet>''',
            )
        self.scan()
        db = self.archive_root / "work" / "source-registry" / "index.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            row = connection.execute(
                "SELECT entity_hint, raw_text FROM source_registry_records"
            ).fetchone()
        self.assertEqual("VLV-1", row[0])
        self.assertNotIn("formatting-only", row[1])


if __name__ == "__main__":
    unittest.main()
