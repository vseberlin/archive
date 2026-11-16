from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_object_plan.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_object_plan", MODULE_PATH)
assert SPEC and SPEC.loader
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)


class KurtSchwarzObjectPlanTests(unittest.TestCase):
    def test_original_title_scan_signature_is_bounded_before_dimensions(self) -> None:
        self.assertEqual(
            "E05_T035_001",
            plan.scan_from_original_title("E05_T035_001.tif // 6x6 cm"),
        )

    def test_inventory_named_tiff_precedes_scan_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "KS-6-BZ_0001.tif"
            scan = root / "A01_T001_001.tif"
            inventory.write_bytes(b"i")
            scan.write_bytes(b"scan")
            selected, fmt, basis, state = plan.select_source(
                current_inventory="KS-6-BZ_0001",
                historical_inventory="KS-6-BZ_0001",
                scan_signature="A01_T001_001",
                original_title_filenames="KS-6-BZ_0001.tif",
                alternate_inventories="",
                candidates=[str(scan), str(inventory)],
            )
            self.assertEqual(str(inventory), selected)
            self.assertEqual("tiff", fmt)
            self.assertEqual("current_inventory_tiff", basis)
            self.assertEqual("provisional_multiple_tiff_review", state)

    def test_curated_5000px_jpeg_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curated_dir = root / "Alle-KS-Bilder_5000px"
            curated_dir.mkdir()
            curated = curated_dir / "KS-8-WF_1.jpg"
            other = root / "KS-8-WF_1.jpg"
            curated.write_bytes(b"curated")
            other.write_bytes(b"other-other")
            selected, fmt, basis, state = plan.select_source(
                current_inventory="KS-8-WF_1",
                historical_inventory="KS-8-WF_1",
                scan_signature="",
                original_title_filenames="KS-8-WF_1.jpg",
                alternate_inventories="",
                candidates=[str(other), str(curated)],
            )
            self.assertEqual(str(curated), selected)
            self.assertEqual("jpeg", fmt)
            self.assertEqual("curated_5000px_jpeg", basis)
            self.assertEqual("provisional_jpeg_only", state)

    def test_unresolved_record_requires_md_image(self) -> None:
        selected, fmt, basis, state = plan.select_source(
            current_inventory="KS-8-WF_X",
            historical_inventory="",
            scan_signature="",
            original_title_filenames="",
            alternate_inventories="",
            candidates=[],
        )
        self.assertEqual("", selected)
        self.assertEqual("", fmt)
        self.assertEqual("museum_digital_image_required", basis)
        self.assertEqual("unresolved_no_local_image", state)

    def test_explicit_original_tiff_precedes_current_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "KS-6-BZ_0254.tif"
            original = root / "KS-6-BZ_1047-3_OR.tif"
            current.write_bytes(b"current")
            original.write_bytes(b"original")
            selected, _, basis, _ = plan.select_source(
                current_inventory="KS-6-BZ_0254",
                historical_inventory="KS-6-BZ_0254",
                scan_signature="A08_T174_001",
                original_title_filenames=(
                    "KS-6-BZ_0254.tif; KS-6-BZ_1047-3_OR.tif"
                ),
                alternate_inventories="",
                candidates=[str(current), str(original)],
            )
            self.assertEqual(str(original), selected)
            self.assertEqual("original_title_original_tiff", basis)

    def test_unlisted_original_variant_precedes_current_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "KS-6-BZ_0580-1.tif"
            original = root / "KS-6-BZ_0580-1_OR.tif"
            current.write_bytes(b"current")
            original.write_bytes(b"original")
            selected, _, basis, _ = plan.select_source(
                current_inventory="KS-6-BZ_0580-1",
                historical_inventory="KS-6-BZ_0580-1",
                scan_signature="E18_T163_001",
                original_title_filenames="KS-6-BZ_0580-1.tif",
                alternate_inventories="",
                candidates=[str(current), str(original)],
            )
            self.assertEqual(str(original), selected)
            self.assertEqual("explicit_original_variant_tiff", basis)


if __name__ == "__main__":
    unittest.main()
