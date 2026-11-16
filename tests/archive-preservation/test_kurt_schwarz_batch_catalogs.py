from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_batch_catalogs.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_batch_catalogs", MODULE_PATH)
assert SPEC and SPEC.loader
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class KurtSchwarzBatchCatalogTests(unittest.TestCase):
    def test_inventory_normalization_covers_legacy_wf_separator(self) -> None:
        self.assertEqual(
            "KS-7-WF_0002",
            batch.normalize_inventory("KS-7-WF-0002.tif"),
        )

    def test_title_normalization_bounds_spacing_and_double_period(self) -> None:
        self.assertEqual(
            "titel",
            batch.normalized_title(" Titel.. "),
        )

    def test_scan_signature_stops_before_dimensions(self) -> None:
        self.assertEqual(
            "P01_T105_003",
            batch.scan_signature(
                {"remarks_short": "P01_T105_003.tif // Kleinbild 35mm"}
            ),
        )

    def test_title_core_removes_representation_suffix(self) -> None:
        self.assertEqual(
            "radio ddr-stand, 1959",
            batch.title_core(
                "Radio DDR-Stand, 1959. SW-Foto © Kurt Schwarz."
            ),
        )

    def test_wf_inventory_normalization_preserves_serial_for_mapping(self) -> None:
        match = __import__("re").match(
            r"(?i)^KS-[5-9]-WF_(.+)$",
            batch.normalize_inventory("KS-8-WF-0032"),
        )
        self.assertEqual("0032", match.group(1))


if __name__ == "__main__":
    unittest.main()
