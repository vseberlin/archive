from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOL_ROOT))

import catalog_core  # noqa: E402
from archive_core.catalog import unit_catalog_rows  # noqa: E402
from archive_core.documents import (  # noqa: E402
    DocumentError,
    assess_document_stub,
    canonical_json,
    inspect_pdf,
)
from archive_core.layout import object_relative_path  # noqa: E402
from workflows.ingest import command_core_pdf_accession  # noqa: E402
from workflows.reconcile import command_core_pdf_reconcile  # noqa: E402


def write_pdf(path: Path, text: str = "Archive PDF fixture text") -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode())
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    path.write_bytes(payload)


class PdfPreservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.pdf = self.base / "source.pdf"
        write_pdf(self.pdf)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pdf_inspection_and_other_document_stub(self) -> None:
        inspection = inspect_pdf(self.pdf)
        self.assertEqual("application/pdf", inspection["media_type"])
        self.assertEqual(1, inspection["pages"])
        self.assertEqual(hashlib.sha256(self.pdf.read_bytes()).hexdigest(), inspection["sha256"])
        self.assertEqual("embedded_text_detected_sample", inspection["text_status"])
        word = self.base / "record.docx"
        word.write_bytes(b"fixture")
        stub = assess_document_stub(word)
        self.assertEqual("stub", stub["result"])
        self.assertEqual(
            "accession_not_implemented_for_format",
            stub["accession_status"],
        )

    def test_invalid_pdf_is_rejected(self) -> None:
        invalid = self.base / "invalid.pdf"
        invalid.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(DocumentError, "signature"):
            inspect_pdf(invalid)

    def _root_with_current_entity(self) -> tuple[Path, str]:
        root = self.base / "root"
        independent = self.base / "independent"
        initialized = catalog_core.initialize_root(
            root,
            tool_version=catalog_core.TOOL_VERSION,
            actor="PDF test",
        )
        config_path = root / "work" / "catalog-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["independent_generation_root"] = str(independent)
        catalog_core.save_config(root, config)
        connection = catalog_core.connect_catalog(Path(initialized["catalog"]))
        try:
            catalog_core.create_unit(
                connection,
                unit_id="TEST-PDF",
                title="PDF test",
                kind="fixture",
                actor="PDF test",
            )
            entity = catalog_core.create_entity(
                connection,
                kind="archive_object",
                canonical_name="PDF test record",
                unit_id="TEST-PDF",
                membership_role="primary",
                actor="PDF test",
                assignment_year=2026,
                domain={
                    "archival_label": "PDF-1",
                    "descriptive_title": "PDF fixture",
                },
            )["entity_id"]
            connection.execute(
                """UPDATE catalog_state
                   SET published_revision = catalog_revision,
                       dirty_since = NULL
                   WHERE singleton = 1"""
            )
            connection.commit()
        finally:
            connection.close()
        return root, entity

    def test_reconciliation_captures_md_and_records_explicit_selection(self) -> None:
        root, entity = self._root_with_current_entity()
        md_payload = {
            "object_id": 86406,
            "object_inventory_number": "WFS-1971-01",
            "object_name": "PDF fixture",
            "object_description": "Fixture description",
            "object_images": [
                {
                    "quell_id": 102809,
                    "type": "text",
                    "filename_loc": (
                        "https://berlin.museum-digital.de/data/berlin/"
                        "resources/documents/202011/WFS-1971-01.pdf"
                    ),
                    "name": "PDF",
                    "rights": "CC BY-SA",
                    "owner": "ISS",
                    "order": 1,
                }
            ],
        }

        def capture(_url: str, destination: Path) -> dict:
            destination.write_bytes(self.pdf.read_bytes())
            inspection = inspect_pdf(destination)
            inspection["source_url"] = _url
            return inspection

        arguments = argparse.Namespace(
            root=str(root),
            actor="PDF test",
            note="Selected verified local original",
            unit="TEST-PDF",
            entity=entity,
            local_pdf=[str(self.pdf)],
            md_id="86406",
            select="local:1",
            timeout=1.0,
        )
        with mock.patch(
            "workflows.reconcile.fetch_museum_digital_query",
            return_value=([md_payload], ""),
        ), mock.patch(
            "workflows.reconcile.capture_museum_digital_pdf",
            side_effect=capture,
        ):
            result = command_core_pdf_reconcile(arguments)
        self.assertEqual("ready_for_pdf_accession", result["state"])
        self.assertEqual(2, result["candidates"])
        plan = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
        self.assertEqual("local:1", plan["selected_candidate"])
        self.assertEqual("not_implemented", plan["indexing_branch"]["ocr_execution"])

    def test_accession_reuses_verified_preexisting_copies_and_catalogs_pdf(self) -> None:
        root, entity = self._root_with_current_entity()
        inspection = inspect_pdf(self.pdf)
        plan = {
            "format": "iss-pdf-reconciliation-v1",
            "unit_id": "TEST-PDF",
            "entity_id": entity,
            "archival_label": "PDF-1",
            "catalog_revision": 8,
            "actor": "PDF test",
            "selection_note": "Selected local original",
            "selected_candidate": "local:1",
            "museum_digital": {"object_id": "86406"},
            "candidates": [
                {
                    "candidate_id": "local:1",
                    "source_kind": "local",
                    "source_locator": str(self.pdf),
                    "inspection": inspection,
                }
            ],
        }
        connection = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        try:
            revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        finally:
            connection.close()
        plan["catalog_revision"] = revision
        plan_path = self.base / "plan.json"
        plan_path.write_text(canonical_json(plan) + "\n", encoding="utf-8")
        relative = Path(*object_relative_path(
            unit_id="TEST-PDF",
            inventory="PDF-1",
            entity_id=entity,
            role="preservation_document",
            suffix=".pdf",
        ).parts)
        independent = self.base / "independent" / relative
        primary = root / relative
        independent.parent.mkdir(parents=True)
        primary.parent.mkdir(parents=True)
        independent.write_bytes(self.pdf.read_bytes())
        primary.write_bytes(self.pdf.read_bytes())
        arguments = argparse.Namespace(
            root=str(root),
            plan=str(plan_path),
            actor="PDF test",
        )
        with mock.patch.object(
            catalog_core,
            "assert_independent_device",
            return_value=None,
        ):
            result = command_core_pdf_accession(arguments)
        self.assertEqual("preservation_document", result["representation_role"])
        self.assertEqual(inspection["sha256"], result["sha256"])
        connection = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        try:
            rows = unit_catalog_rows(connection, "TEST-PDF")
        finally:
            connection.close()
        self.assertEqual("application/pdf", rows[0]["media_type"])
        self.assertEqual("1", rows[0]["page_count"])
        self.assertEqual(inspection["pdf_version"], rows[0]["pdf_version"])
        self.assertEqual(
            "embedded_text_detected_sample",
            rows[0]["pdf_text_status"],
        )


if __name__ == "__main__":
    unittest.main()
