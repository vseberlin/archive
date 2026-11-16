from __future__ import annotations

import argparse
import csv
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOL_ROOT))

import catalog_core  # noqa: E402
from archive_core.catalog import (  # noqa: E402
    UNIT_CATALOG_COLUMNS,
    unit_catalog_csv_bytes,
    unit_catalog_rows,
    unit_catalog_xlsx_bytes,
    write_unit_catalog,
)
from archive_core.layout import object_relative_path  # noqa: E402
from workflows.publish import command_core_export_unit_catalog  # noqa: E402


SHEET_NS = {
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
}


class UnitCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = catalog_core.create_catalog(
            self.root / "catalog.sqlite",
            catalog_core.TOOL_VERSION,
            "Unit catalog test",
        )
        catalog_core.create_unit(
            self.connection,
            unit_id="UNIT-4000",
            title="Large, filterable unit",
            kind="fixture",
            actor="Unit catalog test",
        )
        result = catalog_core.create_entity(
            self.connection,
            kind="archive_object",
            canonical_name='Pump, "Red"\nSeries',
            unit_id="UNIT-4000",
            membership_role="primary",
            actor="Unit catalog test",
            assignment_year=2026,
            domain={
                "archival_label": "A,001",
                "descriptive_title": '=A title, with "quotes"',
                "description": "Line one\nLine two, still one field",
                "material_technique": "Gelatin silver, paper",
                "dimensions": "10 × 15 cm",
                "publication_status": "unknown",
                "note": "Optional note",
            },
        )
        self.entity_id = result["entity_id"]

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_object_representations_use_flat_readable_role_directories(self) -> None:
        master = object_relative_path(
            unit_id="UNIT-4000",
            inventory="A,001",
            entity_id=self.entity_id,
            role="preservation_master",
            suffix=".tif",
        )
        derivative = object_relative_path(
            unit_id="UNIT-4000",
            inventory="A,001",
            entity_id=self.entity_id,
            role="access_derivative",
            suffix=".jpg",
        )
        self.assertEqual(
            "units/UNIT-4000/data/masters",
            master.parent.as_posix(),
        )
        self.assertEqual(
            "units/UNIT-4000/data/derivatives",
            derivative.parent.as_posix(),
        )
        self.assertEqual(
            f"A-001__{self.entity_id}__master.tif",
            master.name,
        )
        self.assertEqual(
            f"A-001__{self.entity_id}__access.jpg",
            derivative.name,
        )

    def _add_representation(self, role: str, filename: str, suffix: str) -> None:
        digest = f"sha256:{suffix * 64}"
        now = catalog_core.utc_now()
        occurrence_id = f"OCC-{role.upper()}"
        self.connection.execute(
            """INSERT INTO content_blobs (
                   content_digest, size_bytes, media_type, width_px, height_px,
                   bit_depth, color_space, compression, page_count,
                   duration_ms, created_at
               ) VALUES (?, 1234, 'image/tiff', 100, 200, 16, 'RGB',
                         'LZW', 1, NULL, ?)""",
            (digest, now),
        )
        self.connection.execute(
            """INSERT INTO file_occurrences (
                   occurrence_id, unit_id, batch_id, content_digest,
                   occurrence_kind, source_root_id, relative_path,
                   original_filename, observed_mtime_utc, observed_at_utc,
                   status, note
               ) VALUES (?, 'UNIT-4000', NULL, ?, 'archive', '',
                         ?, ?, '2026-01-02T03:04:05Z', ?, 'verified',
                         'selected source, checksum verified')""",
            (
                occurrence_id,
                digest,
                f"units/UNIT-4000/data/{role}/{self.entity_id}/{filename}",
                filename,
                now,
            ),
        )
        self.connection.execute(
            """INSERT INTO entity_files (
                   entity_id, occurrence_id, role, status, created_at
               ) VALUES (?, ?, ?, 'active', ?)""",
            (self.entity_id, occurrence_id, role, now),
        )
        self.connection.commit()

    def test_csv_round_trip_preserves_commas_quotes_newlines_and_unicode(self) -> None:
        rows = unit_catalog_rows(self.connection, "UNIT-4000")
        payload = unit_catalog_csv_bytes(rows)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b'""Red""', payload)
        self.assertIn(b"\r\n", payload)
        decoded = list(
            csv.DictReader(
                io.StringIO(payload.decode("utf-8-sig"), newline="")
            )
        )
        self.assertEqual(rows, decoded)
        self.assertEqual(tuple(decoded[0]), UNIT_CATALOG_COLUMNS)
        self.assertEqual("Pump, \"Red\"\nSeries", decoded[0]["canonical_name"])
        self.assertEqual("no_representation", decoded[0]["record_state"])

    def test_repeats_object_metadata_for_each_representation(self) -> None:
        self._add_representation("preservation_master", "master, one.tif", "a")
        self._add_representation("access_derivative", "access.jpg", "b")
        rows = unit_catalog_rows(self.connection, "UNIT-4000")
        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"access_derivative", "preservation_master"},
            {row["representation_role"] for row in rows},
        )
        self.assertEqual(
            {'=A title, with "quotes"'},
            {row["descriptive_title"] for row in rows},
        )
        self.assertTrue(all(row["record_state"] == "represented" for row in rows))

    def test_access_derivative_exports_publication_provenance_chain(self) -> None:
        result = catalog_core.register_access_derivative(
            self.connection,
            entity_id=self.entity_id,
            unit_id="UNIT-4000",
            content_digest="sha256:" + "d" * 64,
            size_bytes=4321,
            relative_path=object_relative_path(
                unit_id="UNIT-4000",
                inventory="A,001",
                entity_id=self.entity_id,
                role="access_derivative",
                suffix=".jpg",
            ).as_posix(),
            original_filename=f"A-001__{self.entity_id}__access.jpg",
            observed_mtime_utc="2026-07-21T10:00:00Z",
            media_type="image/jpeg",
            width_px=658,
            height_px=800,
            master_digest="sha256:" + "e" * 64,
            museum_digital_id="65343",
            museum_digital_filename="published-65343.jpg",
            museum_digital_url="https://berlin.museum-digital.de/object/65343",
            actor="Unit catalog test",
        )
        self.assertEqual("unpublished", result["state"])
        rows = unit_catalog_rows(self.connection, "UNIT-4000")
        self.assertEqual("access_derivative", rows[0]["representation_role"])
        self.assertIn("65343", rows[0]["provenance_note"])
        self.assertIn("published-65343.jpg", rows[0]["provenance_note"])

    def test_xlsx_is_values_only_filterable_and_deterministic(self) -> None:
        self._add_representation("preservation_master", "master.tif", "c")
        rows = unit_catalog_rows(self.connection, "UNIT-4000")
        first = unit_catalog_xlsx_bytes(rows)
        second = unit_catalog_xlsx_bytes(rows)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as workbook:
            names = set(workbook.namelist())
            self.assertIn("xl/tables/table1.xml", names)
            self.assertFalse(any("externalLink" in name for name in names))
            sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            self.assertIsNotNone(sheet.find("x:autoFilter", SHEET_NS))
            self.assertIsNotNone(sheet.find(".//x:pane[@state='frozen']", SHEET_NS))
            self.assertEqual([], sheet.findall(".//x:f", SHEET_NS))
            values = [
                text.text or ""
                for text in sheet.findall(".//x:row[@r='2']/x:c/x:is/x:t", SHEET_NS)
            ]
            expected = [rows[0][column] for column in UNIT_CATALOG_COLUMNS]
            self.assertEqual(expected, values)
            title_index = UNIT_CATALOG_COLUMNS.index("descriptive_title")
            self.assertEqual('=A title, with "quotes"', values[title_index])

    def test_writer_replaces_both_derived_views(self) -> None:
        destination = self.root / "unit"
        result = write_unit_catalog(
            self.connection,
            "UNIT-4000",
            destination,
        )
        self.assertEqual(1, result["rows"])
        self.assertTrue((destination / "CATALOG.csv").is_file())
        self.assertTrue((destination / "CATALOG.xlsx").is_file())
        repeat = write_unit_catalog(
            self.connection,
            "UNIT-4000",
            destination,
        )
        self.assertEqual(result["csv_sha256"], repeat["csv_sha256"])
        self.assertEqual(result["xlsx_sha256"], repeat["xlsx_sha256"])

    def test_cli_workflow_refuses_unpublished_then_exports_current_catalog(
        self,
    ) -> None:
        workflow_root = self.root / "workflow-root"
        initialized = catalog_core.initialize_root(
            workflow_root,
            tool_version=catalog_core.TOOL_VERSION,
            actor="Unit catalog test",
        )
        connection = catalog_core.connect_catalog(Path(initialized["catalog"]))
        try:
            catalog_core.create_unit(
                connection,
                unit_id="UNIT-WORKFLOW",
                title="Workflow unit",
                kind="fixture",
                actor="Unit catalog test",
            )
            catalog_core.create_entity(
                connection,
                kind="archive_object",
                canonical_name="Workflow record",
                unit_id="UNIT-WORKFLOW",
                membership_role="primary",
                actor="Unit catalog test",
                assignment_year=2026,
                domain={"archival_label": "WF-1"},
            )
            arguments = argparse.Namespace(
                root=str(workflow_root),
                unit="UNIT-WORKFLOW",
            )
            with self.assertRaisesRegex(
                catalog_core.CoreCatalogError,
                "unpublished",
            ):
                command_core_export_unit_catalog(arguments)
            state = catalog_core.catalog_state(connection)
            connection.execute(
                """UPDATE catalog_state
                   SET published_revision = catalog_revision,
                       dirty_since = NULL
                   WHERE singleton = 1"""
            )
            connection.commit()
            self.assertGreater(int(state["catalog_revision"]), 0)
        finally:
            connection.close()
        result = command_core_export_unit_catalog(arguments)
        self.assertEqual("ok", result["result"])
        self.assertEqual(1, result["rows"])
        self.assertTrue(
            (workflow_root / "units" / "UNIT-WORKFLOW" / "CATALOG.csv").is_file()
        )
        with self.assertRaisesRegex(
            catalog_core.CoreCatalogError,
            "controlled unit ID",
        ):
            command_core_export_unit_catalog(
                argparse.Namespace(root=str(workflow_root), unit="../escape")
            )


if __name__ == "__main__":
    unittest.main()
