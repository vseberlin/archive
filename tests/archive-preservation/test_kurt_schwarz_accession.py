import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_accession.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_accession", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzAccessionTests(unittest.TestCase):
    def test_safe_component_removes_separators(self):
        self.assertEqual("Menschen_Objekte", MODULE.safe_component("Menschen/Objekte"))

    def test_object_directories_disambiguate_collisions(self):
        objects = [
            {
                "record_key": "md:1",
                "current_inventory": "KS-1",
                "historical_inventory": "",
            },
            {
                "record_key": "local:1",
                "current_inventory": "KS-1",
                "historical_inventory": "",
            },
        ]
        result = MODULE.object_directories(objects)
        self.assertEqual(2, len(set(result.values())))
        self.assertTrue(all(value.startswith("KS-1--") for value in result.values()))

    def test_representation_links_preserve_multiple_objects(self):
        receipts = [
            {
                "record_keys": "md:1; md:2",
                "final_relative_path": "masters/shared.tif",
                "role": "preservation_master",
                "final_sha256": "sha256:" + "a" * 64,
                "size_bytes": 12,
                "basis": "exact duplicate",
                "relationship": "direct",
            }
        ]
        result = MODULE.representation_links(receipts)
        self.assertEqual(result["md:1"], result["md:2"])


if __name__ == "__main__":
    unittest.main()
