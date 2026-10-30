import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_shared_image_reconcile.py"
)
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_shared_image_reconcile", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzSharedImageReconcileTests(unittest.TestCase):
    def test_inventory_tail_ignores_prefix_generation(self):
        self.assertEqual(
            "bz_0148-2", MODULE.inventory_tail("KS-7-BZ_0148-2")
        )

    def test_invalid_inventory_has_no_tail(self):
        self.assertEqual("", MODULE.inventory_tail("not-an-inventory"))


if __name__ == "__main__":
    unittest.main()
