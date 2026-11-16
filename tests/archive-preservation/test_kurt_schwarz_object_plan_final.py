import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_object_plan_final.py"
)
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_object_plan_final", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzObjectPlanFinalTests(unittest.TestCase):
    def test_joined_deduplicates_and_sorts(self):
        self.assertEqual("a; b", MODULE.joined(["b", "a", "b", ""]))


if __name__ == "__main__":
    unittest.main()
