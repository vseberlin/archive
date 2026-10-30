import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_similarity.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_similarity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzSimilarityTests(unittest.TestCase):
    def test_pair_id_is_order_independent(self) -> None:
        self.assertEqual(
            MODULE.pair_id("KSC-ONE", "KSC-TWO"),
            MODULE.pair_id("KSC-TWO", "KSC-ONE"),
        )

    def test_connected_groups_do_not_change_pair_evidence(self) -> None:
        pairs = [
            {"candidate_a_id": "A", "candidate_b_id": "B"},
            {"candidate_a_id": "B", "candidate_b_id": "C"},
            {"candidate_a_id": "D", "candidate_b_id": "E"},
        ]
        groups = MODULE.connected_group_ids(pairs)
        self.assertEqual(groups["A"], groups["B"])
        self.assertEqual(groups["B"], groups["C"])
        self.assertNotEqual(groups["A"], groups["D"])

    def test_extended_hashes_and_metrics_rank_same_image_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.png"
            reencoded = root / "reencoded.jpg"
            different = root / "different.png"
            image = Image.new("RGB", (320, 240), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 30, 250, 190), fill="navy")
            draw.ellipse((90, 60, 180, 150), fill="gold")
            image.save(original)
            image.save(reencoded, quality=93)
            other = Image.new("RGB", (320, 240), "white")
            other_draw = ImageDraw.Draw(other)
            other_draw.rectangle((40, 30, 250, 190), fill="navy")
            other_draw.rectangle((90, 60, 180, 150), fill="crimson")
            other.save(different)

            original_hashes = MODULE.extended_hashes(original)
            reencoded_hashes = MODULE.extended_hashes(reencoded)
            self.assertLessEqual(
                MODULE.hamming(original_hashes[0], reencoded_hashes[0]), 4
            )
            same_ssim = MODULE.compare_metric(original, reencoded, "SSIM")
            different_ssim = MODULE.compare_metric(original, different, "SSIM")
            self.assertGreater(same_ssim, different_ssim)

    def test_xxh3_and_czkawka_hashes_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "fixture.png"
            image = Image.new("RGB", (64, 64), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((8, 8, 48, 48), fill="navy")
            image.save(image_path)
            hashes = MODULE.extended_hashes(image_path)
            self.assertEqual(3, len(hashes))
            self.assertEqual(hashes, MODULE.extended_hashes(image_path))
            xxh3 = MODULE.XXH3()
            self.assertEqual(xxh3.file(image_path), xxh3.file(image_path))
            self.assertEqual(16, len(xxh3.file(image_path)))

    def test_policy_keeps_perceptual_candidates_strict(self) -> None:
        self.assertEqual(8, MODULE.POLICY["phash_maximum"])
        self.assertEqual(10, MODULE.POLICY["dhash_maximum"])
        self.assertIn("blockhash", MODULE.POLICY["czkawka_hashes"])
        self.assertFalse(MODULE.POLICY["automatic_decisions"])

    def test_confidence_never_implies_an_automatic_decision(self) -> None:
        exact = {
            "decoded_pixel_state": "identical",
            "exact_preview": False,
            "ssim": 1.0,
            "ncc": 1.0,
            "trimmed_ssim": 1.0,
            "trimmed_ncc": 1.0,
        }
        likely = {
            **exact,
            "decoded_pixel_state": "not_checked",
            "ssim": 0.98,
            "ncc": 0.995,
            "trimmed_ssim": 0.98,
            "trimmed_ncc": 0.995,
        }
        self.assertEqual("exact_visual", MODULE.confidence(exact))
        self.assertEqual("likely_review", MODULE.confidence(likely))
        self.assertFalse(MODULE.POLICY["automatic_decisions"])


if __name__ == "__main__":
    unittest.main()
