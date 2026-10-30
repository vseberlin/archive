import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "recovery_drain_retirement_execute.py"
)
SPEC = importlib.util.spec_from_file_location(
    "recovery_drain_retirement_execute", MODULE_PATH
)
EXECUTE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = EXECUTE
SPEC.loader.exec_module(EXECUTE)


class RecoveryDrainRetirementExecuteTests(unittest.TestCase):
    def test_candidate_must_be_inside_digest_bound_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "candidate.jpg"
            path.write_bytes(b"candidate")
            row = {
                "candidate_root": str(root),
                "candidate_path": str(path),
            }
            self.assertEqual(path, EXECUTE.validate_candidate(row, {root}))
            with self.assertRaises(EXECUTE.RetirementExecutionError):
                EXECUTE.validate_candidate(row, {root / "other"})

    def test_stable_hash_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"bytes")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(EXECUTE.RetirementExecutionError):
                EXECUTE.sha256_file_stable(link, 5)

    def test_plan_digest_must_match_explicit_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary)
            (plan / "retirement-path-plan.csv").write_text(
                "candidate_path\n/example\n", encoding="utf-8"
            )
            (plan / "summary.json").write_text("{}\n", encoding="utf-8")
            checksum_lines = []
            for name in ("retirement-path-plan.csv", "summary.json"):
                checksum_lines.append(
                    f"{EXECUTE.digest_file(plan / name)}  {name}"
                )
            (plan / "checksums.sha256").write_text(
                "\n".join(checksum_lines) + "\n", encoding="utf-8"
            )
            with self.assertRaises(EXECUTE.RetirementExecutionError):
                EXECUTE.load_plan(plan, "0" * 64)


if __name__ == "__main__":
    unittest.main()
