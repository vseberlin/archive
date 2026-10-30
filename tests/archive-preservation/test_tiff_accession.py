from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
import catalog_core as CORE  # noqa: E402
import tiff_accession as ACCESSION  # noqa: E402


class TiffSampleDepthTest(unittest.TestCase):
    def test_accepts_uniform_multichannel_depth(self) -> None:
        self.assertEqual(8, ACCESSION._scalar_sample_depth("8 8 8"))
        self.assertEqual(16, ACCESSION._scalar_sample_depth([16, 16, 16]))

    def test_rejects_mixed_multichannel_depth(self) -> None:
        with self.assertRaisesRegex(
            ACCESSION.TiffAccessionError,
            "uniform scalar sample depth",
        ):
            ACCESSION._scalar_sample_depth("5 6 5")


@unittest.skipUnless(
    shutil.which("exiftool") and shutil.which("magick"),
    "ExifTool and ImageMagick are required",
)
class TiffAccessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "core"
        self.independent = self.base / "independent"
        CORE.initialize_root(
            self.root,
            tool_version="test",
            actor="Fixture Archivist",
        )
        with mock.patch.object(CORE, "assert_independent_device"):
            CORE.publish_generation(
                self.root,
                self.independent,
                tool_version="test",
                actor="Fixture Archivist",
            )
        catalog = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                catalog,
                unit_id="UNIT-TIFF",
                title="TIFF fixture unit",
                kind="retrospective",
                actor="Fixture Archivist",
            )
            created = CORE.create_entity(
                catalog,
                kind="archive_object",
                canonical_name="VLV-1112",
                unit_id="UNIT-TIFF",
                membership_role="primary",
                actor="Fixture Archivist",
                assignment_year=2026,
                domain={
                    "archival_label": "VLV-1112",
                    "descriptive_title": "Foto Günter Schubert; Foto, 1985",
                    "description": "Beschreibung mit Umlaut: Günter.",
                    "material_technique": "Fotoabzug",
                    "dimensions": "181x240 mm",
                },
            )
            self.entity_id = created["entity_id"]
        finally:
            catalog.close()
        with mock.patch.object(CORE, "assert_independent_device"):
            CORE.publish_generation(
                self.root,
                self.independent,
                tool_version="test",
                actor="Fixture Archivist",
            )
        self.source = self.base / "source.tif"
        subprocess.run(
            [
                "magick",
                "-size",
                "8x6",
                "gradient:",
                "-colors",
                "16",
                "-type",
                "Palette",
                "-compress",
                "None",
                str(self.source),
            ],
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_metadata_enriched_master_is_verified_linked_and_copied(self) -> None:
        before = (
            self.source.stat().st_ino,
            self.source.stat().st_size,
            self.source.stat().st_mtime_ns,
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )
        with mock.patch.object(
            ACCESSION.catalog_core,
            "assert_independent_device",
        ):
            result = ACCESSION.create_preservation_master(
                self.root,
                self.source,
                self.entity_id,
                "Fixture Archivist",
            )
        master = Path(result["master"])
        independent = Path(result["independent_copy"])
        self.assertEqual("masters", master.parent.name)
        self.assertEqual(
            f"VLV-1112__{self.entity_id}__master.tif",
            master.name,
        )
        self.assertTrue(master.is_file())
        self.assertTrue(independent.is_file())
        self.assertEqual(
            hashlib.sha256(master.read_bytes()).hexdigest(),
            hashlib.sha256(independent.read_bytes()).hexdigest(),
        )
        self.assertEqual(0o444, master.stat().st_mode & 0o777)
        self.assertEqual(0o444, independent.stat().st_mode & 0o777)
        after = (
            self.source.stat().st_ino,
            self.source.stat().st_size,
            self.source.stat().st_mtime_ns,
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )
        self.assertEqual(before, after)
        embedded = ACCESSION._exif_values(master, ACCESSION.EMBEDDED_TAGS)
        self.assertEqual(self.entity_id, embedded["XMP-dc:Identifier"])
        self.assertEqual("VLV-1112", embedded["XMP-iptcExt:ArtworkSourceInventoryNo"])
        self.assertEqual(
            "Beschreibung mit Umlaut: Günter.",
            embedded["IPTC:Caption-Abstract"],
        )
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual("iss-tiff-v1", report["embedding_profile"])
        self.assertEqual(
            report["source_digest"],
            f"sha256:{before[3]}",
        )
        catalog = CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite",
            read_only=True,
        )
        try:
            linked = catalog.execute(
                """SELECT ef.role, fo.relative_path, fo.content_digest,
                          cb.width_px, cb.height_px, cb.page_count
                   FROM entity_files ef
                   JOIN file_occurrences fo USING (occurrence_id)
                   JOIN content_blobs cb USING (content_digest)
                   WHERE ef.entity_id = ?""",
                (self.entity_id,),
            ).fetchone()
            self.assertEqual("preservation_master", linked["role"])
            self.assertEqual(
                (
                    8,
                    6,
                    1,
                ),
                (
                    linked["width_px"],
                    linked["height_px"],
                    linked["page_count"],
                ),
            )
            state = CORE.catalog_state(catalog)
            self.assertGreater(
                int(state["catalog_revision"]),
                int(state["published_revision"]),
            )
        finally:
            catalog.close()
        catalog = CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite",
            read_only=True,
        )
        try:
            health = CORE.verify_preservation_masters(
                catalog,
                self.root,
                self.independent,
            )
            self.assertEqual("ok", health["fixity"])
            self.assertEqual(1, health["masters"])
        finally:
            catalog.close()
        with mock.patch.object(CORE, "assert_independent_device"):
            publication = CORE.publish_generation(
                self.root,
                self.independent,
                tool_version="test",
                actor="Fixture Archivist",
            )
        self.assertEqual("current", publication["status"])
        status = CORE.status(self.root)
        self.assertEqual(
            status["catalog_revision"],
            status["published_revision"],
        )

    def test_v2_omits_unknown_physical_and_rights_but_binds_provenance(
        self,
    ) -> None:
        catalog = CORE.connect_catalog(
            self.root / "work" / "catalog.sqlite"
        )
        try:
            created = CORE.create_entity(
                catalog,
                kind="archive_object",
                canonical_name="TFA-527856",
                unit_id="UNIT-TIFF",
                membership_role="primary",
                actor="Fixture Archivist",
                assignment_year=2026,
                domain={
                    "archival_label": "TFA-527856",
                    "descriptive_title": (
                        "Technisches Kabinett Bild 2; Foto, 1952"
                    ),
                    "description": (
                        "Raum des Technischen Kabinetts im HF."
                    ),
                    "material_technique": "",
                    "dimensions": "",
                },
            )
            entity_id = created["entity_id"]
        finally:
            catalog.close()
        with mock.patch.object(CORE, "assert_independent_device"):
            CORE.publish_generation(
                self.root,
                self.independent,
                tool_version="test",
                actor="Fixture Archivist",
            )
        with mock.patch.object(
            ACCESSION.catalog_core,
            "assert_independent_device",
        ):
            result = ACCESSION.create_preservation_master(
                self.root,
                self.source,
                entity_id,
                "Fixture Archivist",
                embedding_profile="iss-tiff-v2",
                provenance_metadata={
                    "case_id": "RESTORE-TFA-527856",
                    "source_file_id": "FILE-LOCAL-168162",
                    "reviewed_bundle_digest": "sha256:" + "b" * 64,
                    "museum_digital_id": "85974",
                    "object_type": "Foto",
                },
            )
        expected = result["embedded_metadata"]
        master = Path(result["master"])
        self.assertEqual(
            expected,
            ACCESSION._exif_values(master, tuple(expected)),
        )
        self.assertEqual("iss-tiff-v2", result["embedding_profile"])
        self.assertEqual("Foto", expected["XMP-dc:Type"])
        self.assertIn(
            result["source_digest"],
            expected["XMP-dc:Source"],
        )
        self.assertNotIn(
            "XMP-iptcExt:ArtworkPhysicalDescription",
            expected,
        )
        self.assertNotIn("XMP-dc:Rights", expected)
        self.assertEqual(
            result["source_digest"],
            result["provenance_metadata"]["source_digest"],
        )


if __name__ == "__main__":
    unittest.main()
