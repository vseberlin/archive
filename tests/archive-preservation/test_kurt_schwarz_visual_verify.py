from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_visual_verify.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_visual_verify", MODULE_PATH)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class KurtSchwarzVisualVerifyTests(unittest.TestCase):
    def test_hashes_are_stable_for_identical_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            second = Path(directory) / "second.jpg"
            image = Image.new("RGB", (80, 60), "white")
            for x in range(10, 70):
                for y in range(15, 45):
                    image.putpixel((x, y), (x * 3 % 255, y * 4 % 255, 40))
            image.save(first)
            image.save(second, quality=95)
            first_hashes = verify.image_hashes(first)
            second_hashes = verify.image_hashes(second)
            self.assertLessEqual(verify.hamming(first_hashes[0], second_hashes[0]), 8)
            self.assertLessEqual(verify.hamming(first_hashes[1], second_hashes[1]), 12)

    def test_visual_state_bounds(self) -> None:
        self.assertEqual(
            "confirmed_visual_match",
            verify.visual_state(8, 12)[0],
        )
        self.assertEqual(
            "probable_visual_match_review",
            verify.visual_state(12, 18)[0],
        )
        self.assertEqual(
            "visual_mismatch_review",
            verify.visual_state(20, 30)[0],
        )


if __name__ == "__main__":
    unittest.main()
