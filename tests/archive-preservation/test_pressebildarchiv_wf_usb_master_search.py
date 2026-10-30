from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/pressebildarchiv_wf_usb_master_search.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location(
    "pressebildarchiv_wf_usb_master_search", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class PressebildarchivUsbMasterSearchTests(unittest.TestCase):
    def test_asset_url_uses_only_captured_bounded_path(self) -> None:
        item = {
            "object_images": [
                {
                    "is_main": "j",
                    "folder": "images/fixture",
                    "filename_loc": "fixture image.jpg",
                }
            ]
        }
        self.assertEqual(
            "https://asset.museum-digital.org/berlin/images/fixture/fixture%20image.jpg",
            MODULE.asset_url(item),
        )
        item["object_images"][0]["folder"] = "../outside"
        with self.assertRaises(MODULE.SearchError):
            MODULE.asset_url(item)

    def test_filename_candidates_use_inventory_token_and_keep_format(self) -> None:
        objects = [
            {"inventory": "IS50709", "md_object_id": "53331"},
            {"inventory": "IS50730", "md_object_id": "53350"},
        ]
        fields = (
            "source_path",
            "relative_path",
            "size_bytes",
            "mtime_ns",
            "extension",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "source_path": "/source/IS50709_0001.tif",
                            "relative_path": "archive/IS50709_0001.tif",
                            "size_bytes": "100",
                            "mtime_ns": "1",
                            "extension": ".tif",
                        },
                        {
                            "source_path": "/source/IS50730.jpg",
                            "relative_path": "archive/IS50730.jpg",
                            "size_bytes": "50",
                            "mtime_ns": "2",
                            "extension": ".jpg",
                        },
                        {
                            "source_path": "/source/IS507300.jpg",
                            "relative_path": "archive/IS507300.jpg",
                            "size_bytes": "50",
                            "mtime_ns": "3",
                            "extension": ".jpg",
                        },
                    ]
                )
            result = MODULE.filename_candidates(objects, path)
        self.assertEqual([".tif"], [row["extension"] for row in result["53331"]])
        self.assertEqual([".jpg"], [row["extension"] for row in result["53350"]])


if __name__ == "__main__":
    unittest.main()
