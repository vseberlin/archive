from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
import tiff_metadata as METADATA  # noqa: E402


class TiffMetadataProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.core = {
            "entity_id": "ISS-2026-A5QM-B",
            "archival_label": "TFA-527856",
            "descriptive_title": "Technisches Kabinett Bild 2; Foto, 1952",
            "description": "Raum des Technischen Kabinetts.",
            "material_technique": "",
            "dimensions": "",
        }
        self.provenance = {
            "case_id": "RESTORE-TFA-527856",
            "source_file_id": "FILE-LOCAL-168162",
            "source_digest": "sha256:" + "a" * 64,
            "reviewed_bundle_digest": "sha256:" + "b" * 64,
            "museum_digital_id": "85974",
            "object_type": "Foto",
        }

    def test_v1_mapping_remains_frozen(self) -> None:
        complete = {
            **self.core,
            "material_technique": "Fotoabzug",
            "dimensions": "181x240 mm",
        }
        tags = METADATA.expected_tags(METADATA.PROFILE_V1, complete)
        self.assertEqual(
            "Material/Technik: Fotoabzug; Maße: 181x240 mm",
            tags["XMP-iptcExt:ArtworkPhysicalDescription"],
        )
        self.assertNotIn("XMP-dc:Source", tags)

    def test_v2_embeds_identity_description_type_and_exact_provenance(self) -> None:
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            self.provenance,
        )
        self.assertEqual(
            "ISS-2026-A5QM-B",
            tags["XMP-dc:Identifier"],
        )
        self.assertEqual("TFA-527856", tags[
            "XMP-iptcExt:ArtworkSourceInventoryNo"
        ])
        self.assertEqual("Foto", tags["XMP-dc:Type"])
        self.assertIn(
            "RESTORE-TFA-527856",
            tags["XMP-dc:Source"],
        )
        self.assertIn(
            self.provenance["source_digest"],
            tags["XMP-dc:Source"],
        )
        self.assertIn(
            self.provenance["reviewed_bundle_digest"],
            tags["XMP-dc:Relation"],
        )

    def test_v2_omits_unknown_optional_fields_without_empty_tags(self) -> None:
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            self.provenance,
        )
        self.assertNotIn(
            "XMP-iptcExt:ArtworkPhysicalDescription",
            tags,
        )
        self.assertNotIn("XMP-dc:Rights", tags)
        self.assertNotIn("XMP-xmpRights:UsageTerms", tags)
        self.assertTrue(all(tags.values()))

    def test_v2_keeps_long_title_in_xmp_without_truncated_iptc_copy(
        self,
    ) -> None:
        title = (
            "Mitarbeiterin in der Bildröhrenproduktion beim Überprüfen "
            "eines Bildschirmkolbens im WF, Foto © Kurt Schwarz, ca. 1976."
        )
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            {**self.core, "descriptive_title": title},
            self.provenance,
        )
        self.assertGreater(
            len(title.encode("utf-8")),
            METADATA.IPTC_OBJECT_NAME_MAX_BYTES,
        )
        self.assertEqual(title, tags["XMP-dc:Title"])
        self.assertEqual(title, tags["XMP-iptcExt:ArtworkTitle"])
        self.assertNotIn("IPTC:ObjectName", tags)

    def test_v2_embeds_rights_only_when_accepted(self) -> None:
        provenance = {
            **self.provenance,
            "rights_statement": "Copyright protected",
            "rights_holder": "Industriesalon Schöneweide",
            "rights_usage_terms": "Permission required",
            "rights_web_statement": "https://example.invalid/rights",
            "credit_line": "Industriesalon Schöneweide",
        }
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            provenance,
        )
        self.assertIn(
            "Industriesalon Schöneweide",
            tags["XMP-dc:Rights"],
        )
        self.assertEqual(
            "Permission required",
            tags["XMP-xmpRights:UsageTerms"],
        )
        self.assertEqual(
            "https://example.invalid/rights",
            tags["XMP-xmpRights:WebStatement"],
        )
        self.assertEqual(
            "Industriesalon Schöneweide",
            tags["XMP-photoshop:Credit"],
        )

    def test_v2_embeds_normalized_workbook_context_and_row_binding(self) -> None:
        provenance = {
            **self.provenance,
            "creator": "VEB Kassenblock",
            "date_created": "1964",
            "coverage": "Berlin (DDR)",
            "contributor": "VLV; VEB Kassenblock",
            "source_collection": "Sammlung Seiffert",
            "previous_owner": "Klaus Seiffert",
            "image_reference": "VLV-1094.jpg",
            "certainty_context": "related_time_sure1",
            "workbook_source": "Bildtexte.xlsx#Tabelle1!row=127",
            "workbook_sha256": "sha256:" + "c" * 64,
            "workbook_record_digest": "sha256:" + "d" * 64,
        }
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            provenance,
        )
        self.assertEqual("VEB Kassenblock", tags["XMP-dc:Creator"])
        self.assertEqual("1964", tags["XMP-dc:Date"])
        self.assertEqual("Berlin (DDR)", tags["XMP-dc:Coverage"])
        self.assertEqual(
            "VLV; VEB Kassenblock",
            tags["XMP-dc:Contributor"],
        )
        self.assertIn("Klaus Seiffert", tags["XMP-dc:Source"])
        self.assertIn("VLV-1094.jpg", tags["XMP-dc:Source"])
        self.assertIn(
            provenance["workbook_record_digest"],
            tags["XMP-dc:Relation"],
        )

    def test_v2_keeps_long_creator_and_rights_in_xmp_only(self) -> None:
        creator = "VEB Werk für Fernsehelektronik, Abteilung Bildröhre"
        rights = ("Copyright protected " * 10).strip()
        credit = "Industriesalon Schöneweide, Berlin"
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            {
                **self.provenance,
                "creator": creator,
                "rights_statement": rights,
                "credit_line": credit,
            },
        )
        self.assertEqual(creator, tags["XMP-dc:Creator"])
        self.assertEqual(rights, tags["XMP-dc:Rights"])
        self.assertEqual(credit, tags["XMP-photoshop:Credit"])
        self.assertNotIn("IPTC:By-line", tags)
        self.assertNotIn("IPTC:CopyrightNotice", tags)
        self.assertNotIn("IPTC:Credit", tags)

    def test_v2_rejects_missing_or_noncanonical_bindings(self) -> None:
        missing = dict(self.provenance)
        missing["reviewed_bundle_digest"] = ""
        with self.assertRaisesRegex(
            METADATA.TiffMetadataError,
            "reviewed accession-bundle digest is required",
        ):
            METADATA.expected_tags(METADATA.PROFILE_V2, self.core, missing)
        malformed = dict(self.provenance)
        malformed["source_digest"] = "abc"
        with self.assertRaisesRegex(
            METADATA.TiffMetadataError,
            "sha256",
        ):
            METADATA.expected_tags(
                METADATA.PROFILE_V2,
                self.core,
                malformed,
            )

    def test_v2_embeds_surrogate_role_and_missing_master_gap(self) -> None:
        provenance = {
            **self.provenance,
            "representation_role": "preservation_surrogate",
            "provenance_gaps": ["missing_local_master"],
        }
        tags = METADATA.expected_tags(
            METADATA.PROFILE_V2,
            self.core,
            provenance,
        )
        self.assertIn(
            "representation preservation_surrogate",
            tags["XMP-dc:Source"],
        )
        self.assertIn(
            "provenance gaps missing_local_master",
            tags["XMP-dc:Source"],
        )
        self.assertIn(
            "iss:provenance-gap:missing_local_master",
            tags["XMP-dc:Relation"],
        )


if __name__ == "__main__":
    unittest.main()
