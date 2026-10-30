from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_subdivisions.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_subdivisions", MODULE_PATH)
assert SPEC and SPEC.loader
subdivisions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subdivisions)


class KurtSchwarzSubdivisionTests(unittest.TestCase):
    def test_slug_preserves_german_label_identity(self) -> None:
        self.assertEqual(
            "menschen-und-menschliches",
            subdivisions.slug("Menschen und Menschliches"),
        )

    def test_exhibition_labels_include_menschen_series(self) -> None:
        self.assertIn(
            "Menschen und Menschliches",
            subdivisions.EXHIBITION_SERIES,
        )


if __name__ == "__main__":
    unittest.main()
