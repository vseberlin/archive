from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_discovery.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_discovery", MODULE_PATH)
assert SPEC and SPEC.loader
discovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discovery)


class KurtSchwarzDiscoveryTests(unittest.TestCase):
    def test_dedicated_project_roots_are_exclusive_candidates(self) -> None:
        state, basis, root = discovery.classify_path(
            "archive/WF/Kurt Schwarz/UPLOADS/3.Upload/KS-6-BZ_0001.tif"
        )
        self.assertEqual("exclusive_candidate", state)
        self.assertEqual("dedicated_kurt_schwarz_root", basis)
        self.assertEqual("archive/WF/Kurt Schwarz", root)

    def test_lightroom_match_is_separate_review(self) -> None:
        state, basis, _ = discovery.classify_path(
            "archive/WF/Lightroom-Katalog_nur-WF/Kurt Schwarz/photo.jpg"
        )
        self.assertEqual("shared_source_separate_review", state)
        self.assertEqual("mixed_project_source", basis)

    def test_live_lightroom_presets_are_separate_review(self) -> None:
        state, basis, _ = discovery.classify_path(
            "archive/WF/Daten/Lightroom-Kataloge-live/"
            "Lightroom Settings/Metadata Presets/Kurt Schwarz.lrtemplate"
        )
        self.assertEqual("shared_source_separate_review", state)
        self.assertEqual("mixed_project_source", basis)

    def test_nested_kurt_lightroom_backup_is_not_exclusive(self) -> None:
        state, basis, _ = discovery.classify_path(
            "archive/WF/Kurt Schwarz/Backup_Lightroom_KS/2021-07-30/"
            "Lightroom-Katalog_WF_nur-WF.lrcat.zip"
        )
        self.assertEqual("shared_source_separate_review", state)
        self.assertEqual("mixed_project_source", basis)

    def test_exhibition_lightroom_package_is_not_exclusive(self) -> None:
        state, basis, _ = discovery.classify_path(
            "archive/WF/Ausstellung_Kurt-Schwarz/Reihenfolge_Ausstellung-KS/"
            "Ausstellung_Reihenfolge/Ausstellung_Reihenfolge.lrcat"
        )
        self.assertEqual("shared_source_separate_review", state)
        self.assertEqual("mixed_project_source", basis)

    def test_kurt_schwabe_is_not_a_kurt_schwarz_match(self) -> None:
        state, _, _ = discovery.classify_path(
            "archive/MEDIATHEK-Backup/Kurt-Schwabe.pjp"
        )
        self.assertEqual("", state)

    def test_inventory_normalization_removes_only_image_extension(self) -> None:
        self.assertEqual(
            "KS-6-BZ_0213",
            discovery.normalize_inventory(" KS-6-BZ_0213.tif "),
        )
        self.assertEqual(
            "KS-6-BZ_0213.final",
            discovery.normalize_inventory("KS-6-BZ_0213.final"),
        )

    def test_subdivision_excludes_parent_collection_only(self) -> None:
        row = discovery.flatten_md_object(
            {
                "object_id": 1,
                "object_inventory_number": "KS-6-BZ_1",
                "object_collection": [
                    {"collection_id": 783, "collection_name": "Kurt Schwarz Fotografie"},
                    {"collection_id": 849, "collection_name": "Street"},
                ],
                "object_images": [
                    {"quell_id": 7, "is_main": "j", "filename_loc": "a.jpg"}
                ],
            }
        )
        self.assertEqual("849", row["subdivision_ids"])
        self.assertEqual("Street", row["subdivisions"])

    def test_original_and_b_variants_are_bounded_to_exact_base(self) -> None:
        index = {
            "ks-6-bz_0580-1_or": [{"source_path": "original"}],
            "ks-6-bz_0580-1b": [{"source_path": "variant"}],
            "ks-6-bz_0580-2_or": [{"source_path": "other-object"}],
        }
        rows = discovery.variant_matches(index, ["KS-6-BZ_0580-1"])
        self.assertEqual(
            {"original", "variant"},
            {row["source_path"] for row in rows},
        )

    def test_explicit_workbook_owner_blocks_cross_object_variant(self) -> None:
        candidates = [
            {
                "source_path": "wrong",
                "relative_path": "root/KS-6-BZ_1047-3_OR.tif",
            }
        ]
        self.assertEqual(
            [],
            discovery.filter_variant_ownership(
                candidates,
                "96929",
                {"ks-6-bz_1047-3_or": {"98639"}},
            ),
        )


if __name__ == "__main__":
    unittest.main()
