import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "recovery_drain_retirement_plan.py"
)
SPEC = importlib.util.spec_from_file_location(
    "recovery_drain_retirement_plan", MODULE_PATH
)
RETIREMENT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = RETIREMENT
SPEC.loader.exec_module(RETIREMENT)


class RecoveryDrainRetirementPlanTests(unittest.TestCase):
    def test_candidate_must_be_beneath_exact_approved_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = root / "candidate.tif"
            candidate.write_bytes(b"candidate")
            self.assertEqual(
                candidate,
                RETIREMENT.validate_candidate_path(
                    str(candidate), str(root), {root}
                ),
            )
            with self.assertRaises(RETIREMENT.RetirementPlanError):
                RETIREMENT.validate_candidate_path(
                    str(candidate), str(root), {root / "different"}
                )

    def test_candidate_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "target.tif"
            target.write_bytes(b"target")
            link = root / "link.tif"
            link.symlink_to(target)
            with self.assertRaises(RETIREMENT.RetirementPlanError):
                RETIREMENT.validate_candidate_path(str(link), str(root), {root})

    def test_stable_hash_refuses_wrong_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate.jpg"
            path.write_bytes(b"bytes")
            with self.assertRaises(RETIREMENT.RetirementPlanError):
                RETIREMENT.sha256_file_stable(path, 4)


if __name__ == "__main__":
    unittest.main()
