from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).parents[2] / "tools/archive-preservation"
sys.path.insert(0, str(TOOLS))
SCRIPT = TOOLS / "kurt_schwarz_object_census.py"
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_object_census", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzObjectCensusTests(unittest.TestCase):
    @staticmethod
    def avantmedia_row(
        filename: str,
        extension: str,
        queue: str = "new_local_review",
        target: str = "",
    ) -> dict[str, str]:
        role = "TIFF/001 Magazin 001" if extension == ".tif" else "Nutzungsderivate/001 Magazin 001"
        path = (
            "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
            f"SCANS/01_Avantmedia_Dias/{role}/{filename}"
        )
        return {
            "candidate_id": "KSC-" + filename,
            "filename": filename,
            "extension": extension,
            "primary_source_path": path,
            "source_paths_json": json.dumps([path]),
            "metadata_matches_json": "[]",
            "metadata_match_count": 0,
            "metadata_summary": "",
            "queue_class": queue,
            "target_record_key": target,
            "target_inventory": "KS-8-WF_1" if target else "",
            "target_media_candidate_id": "MEDIA-1" if target else "",
            "related_candidate_id": "",
            "selected_metadata_json": "{}",
            "selected_metadata_basis": "no_reliable_metadata_selected",
            "metadata_enrichment_json": "{}",
            "visual_match_json": "{}",
            "review_basis": "",
            "auto_decision_reason": "",
        }

    def test_avantmedia_identity_binds_descriptive_jpeg_to_numbered_tiff(self) -> None:
        tiff = self.avantmedia_row("Dia 00019.tif", ".tif")
        jpeg = self.avantmedia_row("a2_00019.jpg", ".jpg")
        self.assertEqual((19, "tiff"), MODULE.avantmedia_candidate_identity(tiff))
        self.assertEqual(
            (19, "derivative"), MODULE.avantmedia_candidate_identity(jpeg)
        )

    def test_avantmedia_policy_keeps_only_tiff_for_review(self) -> None:
        tiff = self.avantmedia_row("Dia 00019.tif", ".tif")
        jpeg = self.avantmedia_row(
            "a2_00019.jpg", ".jpg", "existing_object_reconciled", "md:1"
        )
        jpeg["metadata_matches_json"] = json.dumps(
            [{"inventory": "KS-8-WF_1", "title": "Known slide"}]
        )
        jpeg["metadata_match_count"] = 1
        result = MODULE.apply_avantmedia_tiff_primary_policy(
            [tiff, jpeg],
            record_context={
                "md:1": {
                    "record_key": "md:1",
                    "inventory": "KS-8-WF_1",
                    "media_candidate_id": "MEDIA-1",
                    "no_tiff_master": False,
                    "effective_metadata": {
                        "title": "Known slide",
                        "bag_label": (
                            "Dia aus Multivision, eigener Dateiname: "
                            "a2_00018.jpg"
                        ),
                    },
                }
            },
            existing_inventories={MODULE.normalized_stem("KS-8-WF_1")},
        )
        self.assertEqual("existing_object_reconciled", tiff["queue_class"])
        self.assertEqual("md:1", tiff["target_record_key"])
        self.assertEqual(1, tiff["metadata_match_count"])
        self.assertEqual(
            "Dia aus Multivision, eigener Dateiname: a2_00019.jpg",
            json.loads(tiff["selected_metadata_json"])["bag_label"],
        )
        self.assertEqual("new_local_companion_reconciled", jpeg["queue_class"])
        self.assertEqual(tiff["candidate_id"], jpeg["related_candidate_id"])
        self.assertEqual(1, result["derivatives_reconciled_as_companions"])

    def test_avantmedia_derivative_without_tiff_remains_reviewable(self) -> None:
        jpeg = self.avantmedia_row("b1_00001.jpg", ".jpg")
        result = MODULE.apply_avantmedia_tiff_primary_policy(
            [jpeg], record_context={}, existing_inventories=set()
        )
        self.assertEqual("new_local_review", jpeg["queue_class"])
        self.assertEqual([1], result["missing_tiff_sequences"])

    def test_spreadsheet_reference_requires_an_exact_candidate_identifier(self) -> None:
        targets = {"k06f061002", "d02t039001"}
        self.assertEqual(
            {"k06f061002"},
            MODULE.candidate_keys_in_value(
                "K06_F061_002.tif // 6.Upload, Kleinbild", targets
            ),
        )
        self.assertEqual(
            {"d02t039001"},
            MODULE.candidate_keys_in_value("D02_T039_001", targets),
        )
        self.assertEqual(set(), MODULE.candidate_keys_in_value("g.tif", targets))

    def test_jpeg_dimensions_are_read_without_image_decode(self) -> None:
        # SOI, baseline SOF with height 2000 / width 3000, and EOI.
        payload = (
            b"\xff\xd8"
            b"\xff\xc0\x00\x11\x08\x07\xd0\x0b\xb8"
            b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
            b"\xff\xd9"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.jpg"
            path.write_bytes(payload)
            self.assertEqual((3000, 2000), MODULE.jpeg_dimensions(path))

    def test_selected_metadata_ignores_stale_existing_md_mentions(self) -> None:
        matches = [
            {
                "inventory": "KS-6-BZ_0580-1",
                "title": "Existing MD object from a stale found column",
            },
            {
                "inventory": "KS-7-BZ_2000",
                "title": "New local photograph",
                "description": "Reliable local description",
            },
            {
                "inventory": "KS-7-BZ_2000.tif",
                "title": "New local photograph",
                "description": "Reliable local description",
            },
        ]
        selected, basis = MODULE.choose_selected_metadata(
            matches,
            filename="KS-7-BZ_2000.tif",
            existing_inventories={MODULE.normalized_stem("KS-6-BZ_0580-1")},
        )
        self.assertEqual("KS-7-BZ_2000", selected["inventory"])
        self.assertEqual("New local photograph", selected["title"])
        self.assertEqual("filename_and_spreadsheet_inventory_agree", basis)

    def test_visual_match_requires_one_unique_bounded_existing_object(self) -> None:
        representatives = [
            {
                "record_key": "md:1",
                "inventory": "KS-1",
                "media_candidate_id": "MEDIA-1",
                "phash": 0,
                "dhash": 0,
            },
            {
                "record_key": "md:2",
                "inventory": "KS-2",
                "media_candidate_id": "MEDIA-2",
                "phash": (1 << 20) - 1,
                "dhash": (1 << 20) - 1,
            },
        ]
        match, audit = MODULE.visual_match((0, 0), representatives)
        self.assertEqual("md:1", match["record_key"])
        self.assertEqual(1, audit["bounded_match_count"])
        representatives[1]["phash"] = 1
        representatives[1]["dhash"] = 1
        match, audit = MODULE.visual_match((0, 0), representatives)
        self.assertIsNone(match)
        self.assertEqual(2, audit["bounded_match_count"])

    def test_visual_match_allows_multiple_decided_representations_of_one_object(
        self,
    ) -> None:
        representatives = [
            {
                "record_key": "md:1",
                "inventory": "KS-1",
                "media_candidate_id": "MEDIA-MASTER",
                "matched_media_candidate_id": "MEDIA-MASTER",
                "matched_disposition": "selected_master",
                "phash": 0,
                "dhash": 0,
            },
            {
                "record_key": "md:1",
                "inventory": "KS-1",
                "media_candidate_id": "MEDIA-MASTER",
                "matched_media_candidate_id": "MEDIA-REDUNDANT",
                "matched_disposition": "redundant",
                "phash": 1,
                "dhash": 1,
            },
        ]
        match, audit = MODULE.visual_match((0, 0), representatives)
        self.assertEqual("md:1", match["record_key"])
        self.assertEqual(1, audit["bounded_match_count"])
        self.assertEqual(2, audit["bounded_representation_match_count"])

    def test_existing_metadata_enrichment_requires_an_untied_value(self) -> None:
        selected = MODULE.metadata_for_inventory(
            [
                {"inventory": "KS-1", "places": "Berlin", "title": "One"},
                {"inventory": "KS-1.tif", "places": "Berlin", "title": "Two"},
                {"inventory": "KS-2", "places": "Leipzig", "title": "Other"},
            ],
            "KS-1",
        )
        self.assertEqual("Berlin", selected["places"])
        self.assertNotIn("title", selected)


if __name__ == "__main__":
    unittest.main()
