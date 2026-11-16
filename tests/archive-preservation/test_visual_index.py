from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "visual_index.py"
)
visual_index = None
if Image is not None:
    SPEC = importlib.util.spec_from_file_location("visual_index", SCRIPT)
    assert SPEC and SPEC.loader
    visual_index = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(visual_index)


@unittest.skipUnless(
    Image is not None and visual_index is not None,
    "visual-index benchmark dependencies are not in the pinned validator",
)
class VisualIndexBenchmarkTest(unittest.TestCase):
    def test_hashes_are_fixed_width_and_resize_stable(self) -> None:
        source = Image.new("RGB", (160, 100), "white")
        drawing = ImageDraw.Draw(source)
        drawing.rectangle((20, 15, 110, 80), fill="#654321")
        drawing.ellipse((80, 25, 145, 90), fill="#cc2222")
        resized = source.resize((800, 500), Image.Resampling.LANCZOS)
        first = visual_index.feature_hashes(source)
        second = visual_index.feature_hashes(resized)
        self.assertEqual(
            {"ahash_64", "dhash_64", "phash_dct_64"},
            set(first),
        )
        self.assertTrue(all(len(value) == 16 for value in first.values()))
        self.assertLessEqual(
            visual_index.hamming_distance(
                first["ahash_64"],
                second["ahash_64"],
            ),
            2,
        )
        self.assertLessEqual(
            visual_index.hamming_distance(
                first["dhash_64"],
                second["dhash_64"],
            ),
            2,
        )
        self.assertLessEqual(
            visual_index.hamming_distance(
                first["phash_dct_64"],
                second["phash_dct_64"],
            ),
            2,
        )

    def test_benchmark_writes_only_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "protected" / "images"
            output_root = base / "pilot-work"
            source.mkdir(parents=True)
            output_root.mkdir()
            image = Image.new("RGB", (64, 48), "#71828f")
            image.save(source / "master.tif")
            image.save(source / "derivative.jpg", quality=75)
            before = {
                path: (
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in source.iterdir()
            }
            original_sources = visual_index.PROTECTED_SOURCE_ROOTS
            original_outputs = visual_index.OUTPUT_ROOTS
            try:
                visual_index.PROTECTED_SOURCE_ROOTS = (base / "protected",)
                visual_index.OUTPUT_ROOTS = (output_root,)
                output = output_root / "benchmark.json"
                report = visual_index.benchmark(
                    source,
                    output,
                    limit=2,
                    timeout=30,
                )
            finally:
                visual_index.PROTECTED_SOURCE_ROOTS = original_sources
                visual_index.OUTPUT_ROOTS = original_outputs
            self.assertEqual(2, report["walk"]["images_seen"])
            self.assertEqual(2, report["sample"]["successful"])
            self.assertIn(
                "libvips",
                report["extractor"]["normalization"],
            )
            self.assertEqual(
                ["ahash-64-v2", "dhash-64-v2", "phash-dct-64-v2"],
                report["extractor"]["features"],
            )
            self.assertTrue(output.is_file())
            after = {
                path: (
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in source.iterdir()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
