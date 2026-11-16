import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_shared_image_review.py"
)
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_shared_image_review", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzSharedImageReviewTests(unittest.TestCase):
    def test_probable_visual_match_becomes_confirmed_alias(self):
        state, key, _ = MODULE.reviewed_state(
            {
                "mapping_state": "probable_visual_alias_review",
                "mapped_record_key": "md:123",
                "historical_inventory": "KS-6-BZ_0001",
            }
        )
        self.assertEqual("confirmed_visual_alias", state)
        self.assertEqual("md:123", key)

    def test_known_unresolved_member_becomes_local_only_object(self):
        state, key, _ = MODULE.reviewed_state(
            {
                "mapping_state": "unresolved_visual_review",
                "mapped_record_key": "",
                "historical_inventory": "KS-6-BZ_0629-1",
            }
        )
        self.assertEqual("reviewed_local_only_object", state)
        self.assertEqual("local-shared-container:KS-6-BZ_0629-1", key)

    def test_unknown_unresolved_member_is_rejected(self):
        with self.assertRaises(MODULE.ReviewError):
            MODULE.reviewed_state(
                {
                    "mapping_state": "unresolved_visual_review",
                    "mapped_record_key": "",
                    "historical_inventory": "KS-6-BZ_9999",
                }
            )


if __name__ == "__main__":
    unittest.main()
