from __future__ import annotations

import csv
from contextlib import closing
import importlib.util
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "archive-preservation" / "archive.py"
SPEC = importlib.util.spec_from_file_location("iss_archive_museum_digital", SCRIPT)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


def md_payload(object_id: int, inventory: str, title: str) -> dict:
    return {
        "object_id": object_id,
        "object_inventory_number": inventory,
        "object_type": "Foto",
        "object_name": title,
        "object_description": "Öffentliche Beschreibung des Objekts.",
        "object_material_technique": "Fotoabzug",
        "object_dimensions": "181x240 mm",
        "object_last_updated": "2025-02-05 19:21:44",
        "object_institution": {
            "institution_id": 29,
            "institution_name": "Industriesalon Schöneweide",
        },
        "langs": ["de"],
        "object_collection": [{"collection_id": 593, "collection_name": "VEB Kassenblock"}],
        "object_events": [
            {
                "event_id": 1,
                "event_type_name": "Hergestellt",
                "people": {"displayname": "VLV-Vordruck-Leitverlag"},
                "place": {"place_name": "Berlin-Oberschöneweide"},
                "time": {"time_name": "1985"},
            }
        ],
        "object_tags": [{"tag_id": 1, "tag_name": "Druckerei"}],
        "licence": {
            "metadata_rights_holder": "www.industriesalon.de",
            "metadata_rights_status": "CC0",
        },
        "object_images": [
            {
                "quell_id": 1,
                "is_main": "j",
                "rights": "CC BY-NC-SA",
                "owner": "www.industriesalon.de",
            }
        ],
    }


class MuseumDigitalReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "archive"
        ARCHIVE.command_init(SimpleNamespace(root=str(self.root), actor="fixture", workstation_id="fixture"))
        ARCHIVE.command_create_unit(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                unit="TEST-MD-FIXTURE",
                title="Museum-digital fixture",
                kind="fixture",
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_object(self, inventory: str, label: str) -> str:
        result = ARCHIVE.command_create_object(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                evidence="",
                unit="TEST-MD-FIXTURE",
                assignment_year=2026,
                display_identifier=inventory,
                object_type="photograph",
                label=label,
                publication_status="unknown",
                status="active",
            )
        )
        return result["object_id"]

    def test_read_only_exact_inventory_reconciliation(self) -> None:
        object_id = self.create_object("VLV-1112", "Paragon photograph VLV-1112")
        source_root = Path(self.temp.name) / "original"
        source_root.mkdir()
        source_records = source_root / "original-records.csv"
        source_records.write_text(
            "inventory_number;object_title;object_type;object_description;"
            "object_material_technique;object_dimensions;printed_when_1;image_rights1\n"
            "VLV-1112;Foto Günter Schubert;Foto;Öffentliche Beschreibung des Objekts.;"
            "Fotoabzug;181x240 mm;1985;CC BY-SA\n",
            encoding="utf-8",
        )
        intake = ARCHIVE.command_intake(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="original metadata",
                evidence="",
                unit="TEST-MD-FIXTURE",
                batch="source-record-fixture",
                source_root_id="fixture-source",
                source_root=str(source_root),
                path=source_records.name,
                role="source",
            )
        )
        ARCHIVE.command_link(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                evidence="",
                unit="TEST-MD-FIXTURE",
                subject_type="object",
                subject_id=object_id,
                predicate="source_metadata",
                object_type="source_file",
                object_id=intake["source_file_id"],
                status="accepted",
                confidence="certain",
            )
        )
        master = source_root / "complete-reference.ppm"
        master.write_bytes(b"P6\n1 1\n255\n" + bytes((20, 40, 60)))
        master_intake = ARCHIVE.command_intake(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="Complete carrier image available only at low resolution.",
                evidence="fixture provenance",
                unit="TEST-MD-FIXTURE",
                batch="master-fixture",
                source_root_id="fixture-source",
                source_root=str(source_root),
                path=master.name,
                role="master",
            )
        )
        ARCHIVE.command_link(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                evidence="fixture provenance",
                unit="TEST-MD-FIXTURE",
                subject_type="object",
                subject_id=object_id,
                predicate="complete_reference",
                object_type="source_file",
                object_id=master_intake["source_file_id"],
                status="accepted",
                confidence="certain",
            )
        )
        part = source_root / "high-resolution-part.ppm"
        part.write_bytes(b"P6\n2 1\n255\n" + bytes((20, 40, 60, 80, 100, 120)))
        part_intake = ARCHIVE.command_intake(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="Original high-resolution part of the carrier.",
                evidence="fixture provenance",
                unit="TEST-MD-FIXTURE",
                batch="part-fixture",
                source_root_id="fixture-source",
                source_root=str(source_root),
                path=part.name,
                role="master",
            )
        )
        ARCHIVE.command_link(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="Upper section of the carrier.",
                evidence="fixture provenance",
                unit="TEST-MD-FIXTURE",
                subject_type="object",
                subject_id=object_id,
                predicate="high_resolution_part",
                object_type="source_file",
                object_id=part_intake["source_file_id"],
                status="accepted",
                confidence="certain",
            )
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            before_identifiers = connection.execute("SELECT COUNT(*) FROM identifiers").fetchone()[0]
            before_log = connection.execute("SELECT COUNT(*) FROM logbook").fetchone()[0]

        payload = md_payload(65236, "VLV-1112", "Foto Günter Schubert; Foto, 1985")
        with mock.patch.object(ARCHIVE, "fetch_museum_digital_query", return_value=([payload], "")):
            result = ARCHIVE.command_reconcile_museum_digital(
                SimpleNamespace(
                    root=str(self.root),
                    output="",
                    unit="TEST-MD-FIXTURE",
                    institution_id=29,
                    limit=25,
                    timeout=1.0,
                    delay=0.0,
                    source_records=[],
                )
            )

        self.assertEqual("read_only_stub", result["mode"])
        self.assertEqual({"exact_inventory": 1}, result["counts"])
        output = Path(result["output"])
        with (output / "records.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(1, len(rows))
        self.assertEqual(object_id, rows[0]["local_archival_id"])
        self.assertEqual("65236", rows[0]["museum_digital_id"])
        self.assertEqual("review_metadata_and_link_id", rows[0]["sync_stub_action"])
        self.assertEqual("exact_inventory", rows[0]["source_record_status"])
        self.assertIn("work/intake/source-record-fixture", rows[0]["source_record_file"])
        self.assertEqual("Foto Günter Schubert", rows[0]["source_title"])
        self.assertEqual("same", rows[0]["source_to_museum_digital_description"])
        self.assertEqual("same", rows[0]["source_to_museum_digital_material"])
        self.assertEqual("different", rows[0]["source_to_museum_digital_image_rights"])
        self.assertEqual("different", rows[0]["title_comparison"])
        self.assertEqual("CC0", rows[0]["museum_digital_metadata_rights"])
        self.assertEqual("CC BY-NC-SA", rows[0]["museum_digital_primary_image_rights"])
        self.assertEqual("VEB Kassenblock", rows[0]["museum_digital_collections"])
        self.assertTrue((output / "raw" / "65236.json").is_file())
        self.assertTrue((output / "field-coverage.csv").is_file())
        self.assertTrue((output / "visual-comparison.csv").is_file())
        with (output / "visual-comparison.csv").open(encoding="utf-8", newline="") as handle:
            visual_rows = list(csv.DictReader(handle))
        self.assertEqual(
            {
                "complete_carrier_reference",
                "high_resolution_part",
                "source_record_image",
                "museum_digital_snapshot",
            },
            {row["role"] for row in visual_rows},
        )
        master_row = next(
            row for row in visual_rows
            if row["role"] == "complete_carrier_reference"
        )
        self.assertEqual("available", master_row["status"])
        self.assertEqual(
            "complete reference — Complete carrier image available only at low resolution",
            master_row["detail"],
        )
        part_row = next(
            row for row in visual_rows if row["role"] == "high_resolution_part"
        )
        self.assertEqual("available", part_row["status"])
        self.assertEqual(
            "high resolution part — Upper section of the carrier",
            part_row["detail"],
        )
        self.assertEqual(
            {"missing"},
            {
                row["status"]
                for row in visual_rows
                if row["role"]
                in {"source_record_image", "museum_digital_snapshot"}
            },
        )

        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(before_identifiers, connection.execute("SELECT COUNT(*) FROM identifiers").fetchone()[0])
            self.assertEqual(before_log, connection.execute("SELECT COUNT(*) FROM logbook").fetchone()[0])

    def test_reads_original_xlsx_without_office_dependency(self) -> None:
        workbook = Path(self.temp.name) / "original-records.xlsx"
        workbook_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Records" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
        relationships_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
        worksheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData>
  <row r="1"><c r="A1" t="inlineStr"><is><t>inventory_number</t></is></c><c r="B1" t="inlineStr"><is><t>object_title</t></is></c></row>
  <row r="2"><c r="A2" t="inlineStr"><is><t>VLV-1112</t></is></c><c r="B2" t="inlineStr"><is><t>Original title</t></is></c></row>
 </sheetData>
</worksheet>"""
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", relationships_xml)
            archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        rows = ARCHIVE.read_tabular_source_rows(workbook)
        self.assertEqual(1, len(rows))
        self.assertEqual("VLV-1112", rows[0]["inventory_number"])
        self.assertEqual("Original title", rows[0]["object_title"])
        self.assertEqual("Records", rows[0]["_source_sheet"])

    def test_museum_digital_fallback_requires_completed_local_search(self) -> None:
        self.create_object("ONLY-MD-001", "Object without a source row")
        searched_table = Path(self.temp.name) / "searched-originals.csv"
        searched_table.write_text(
            "inventory_number;object_title\nOTHER-001;A different object\n",
            encoding="utf-8",
        )
        payload = md_payload(70001, "ONLY-MD-001", "Surviving MD description")
        arguments = {
            "root": str(self.root),
            "output": "",
            "unit": "TEST-MD-FIXTURE",
            "institution_id": 29,
            "limit": 25,
            "timeout": 1.0,
            "delay": 0.0,
        }
        with mock.patch.object(ARCHIVE, "fetch_museum_digital_query", return_value=([payload], "")):
            searched = ARCHIVE.command_reconcile_museum_digital(
                SimpleNamespace(**arguments, source_records=[str(searched_table)])
            )
            unsearched = ARCHIVE.command_reconcile_museum_digital(
                SimpleNamespace(**arguments, source_records=[])
            )
        with Path(searched["records_csv"]).open(encoding="utf-8", newline="") as handle:
            searched_row = next(csv.DictReader(handle))
        with Path(unsearched["records_csv"]).open(encoding="utf-8", newline="") as handle:
            unsearched_row = next(csv.DictReader(handle))
        self.assertEqual("not_found", searched_row["source_record_status"])
        self.assertEqual(
            "accept_museum_digital_fallback_and_link_id",
            searched_row["sync_stub_action"],
        )
        self.assertEqual("not_supplied", unsearched_row["source_record_status"])
        self.assertEqual(
            "locate_local_source_before_metadata_review",
            unsearched_row["sync_stub_action"],
        )

    def test_ambiguous_inventory_is_not_linked(self) -> None:
        self.create_object("DUP-001", "Ambiguous object")
        candidates = [
            md_payload(1, "DUP-001", "First"),
            md_payload(2, "DUP-001", "Second"),
        ]
        with mock.patch.object(ARCHIVE, "fetch_museum_digital_query", return_value=(candidates, "")):
            result = ARCHIVE.command_reconcile_museum_digital(
                SimpleNamespace(
                    root=str(self.root),
                    output="",
                    unit="TEST-MD-FIXTURE",
                    institution_id=29,
                    limit=25,
                    timeout=1.0,
                    delay=0.0,
                )
            )
        self.assertEqual({"ambiguous": 1}, result["counts"])
        with Path(result["records_csv"]).open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual("manual_identity_review", row["sync_stub_action"])
        self.assertEqual("", row["museum_digital_id"])


if __name__ == "__main__":
    unittest.main()
