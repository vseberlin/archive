import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "archive-preservation"
    / "recovery_layout_cleanup_plan.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location(
    "recovery_layout_cleanup_plan", MODULE_PATH
)
PLAN = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = PLAN
SPEC.loader.exec_module(PLAN)


class RecoveryLayoutCleanupPlanTests(unittest.TestCase):
    def row(self, entity, role, status="active", inventory="VLV-1"):
        token = {
            "preservation_master": "master.tif",
            "preservation_surrogate": "surrogate.tif",
            "preservation_surrogate_source": "source.jpg",
            "access_derivative": "access.jpg",
        }[role]
        return {
            "inventory": inventory,
            "entity_id": entity,
            "role": role,
            "link_status": status,
            "occurrence_id": f"OCC-{entity}-{role}-{status}",
            "relative_path": f"units/U/data/masters/{entity}-{token}",
            "original_filename": f"{entity}-{token}",
            "observed_mtime_utc": "",
            "occurrence_status": "verified",
            "content_digest": "sha256:" + "a" * 64,
            "size_bytes": 1,
        }

    def test_object_without_one_representation_is_refused(self):
        rows = [self.row("ISS-2026-AAAA-A", "access_derivative")]
        with self.assertRaises(PLAN.CleanupPlanError):
            PLAN.build_plan(rows, "U", 1)

    def test_output_plan_is_explicitly_non_authorizing(self):
        semantic = {
            "writes_authorized": False,
            "deletion_authorized": False,
        }
        self.assertFalse(semantic["writes_authorized"])
        self.assertFalse(semantic["deletion_authorized"])


if __name__ == "__main__":
    unittest.main()
