from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools/archive-preservation/kurt_schwarz_hash_inventory.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_hash_inventory", MODULE_PATH)
assert SPEC and SPEC.loader
hashes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hashes)


class KurtSchwarzHashInventoryTests(unittest.TestCase):
    def test_sha256_file_uses_prefixed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value"
            path.write_bytes(b"kurt-schwarz")
            self.assertEqual(
                "sha256:9fb97af55269d5f64ecfb839bd0c45dc73b1a0d3220789867aa5217dc18bb1ff",
                hashes.sha256_file(path),
            )


if __name__ == "__main__":
    unittest.main()
