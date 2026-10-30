from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_restore_search.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_restore_search", MODULE_PATH)
assert SPEC and SPEC.loader
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


class KurtSchwarzRestoreSearchTests(unittest.TestCase):
    def test_project_root_is_known_exclusive_scope(self) -> None:
        self.assertEqual(
            "exclusive_project_root",
            search.known_scope("archive/WF/Kurt Schwarz/docs/catalog.xlsx"),
        )

    def test_live_lightroom_is_shared_scope(self) -> None:
        self.assertEqual(
            "shared_source",
            search.known_scope(
                "archive/WF/Daten/Lightroom-Kataloge-live/catalog.lrcat"
            ),
        )

    def test_nested_project_lightroom_is_shared_before_parent_scope(self) -> None:
        self.assertEqual(
            "shared_source",
            search.known_scope(
                "archive/WF/Kurt Schwarz/Backup_Lightroom_KS/2021-07-30/"
                "Lightroom-Katalog_WF_nur-WF.lrcat.zip"
            ),
        )

    def test_inventory_pattern_does_not_match_kurt_schwabe(self) -> None:
        self.assertEqual([], search.path_reasons("archive/Kurt-Schwabe/image.jpg"))

    def test_content_patterns_match_name_and_inventory(self) -> None:
        self.assertEqual("kurt_schwarz", search.first_match("Fotograf Kurt Schwarz")[0])
        self.assertEqual(
            "ks_inventory",
            search.first_match("Inventarnummer KS-6-BZ_0588-3")[0],
        )

    def test_utf16_content_is_normalized(self) -> None:
        payload = "Kurt Schwarz".encode("utf-16")
        self.assertIn("Kurt Schwarz", search.normalized_text(payload))


if __name__ == "__main__":
    unittest.main()
