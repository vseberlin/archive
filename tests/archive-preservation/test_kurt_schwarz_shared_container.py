import importlib.util
import unittest
from pathlib import Path, PurePosixPath


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_shared_container.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_shared_container", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzSharedContainerTests(unittest.TestCase):
    def test_accepts_member_below_explicit_project_root(self):
        member = MODULE.safe_member_path(
            "Kurt Schwarz Fotografie/1. Upload/image.jpg"
        )
        self.assertEqual(
            PurePosixPath("Kurt Schwarz Fotografie/1. Upload/image.jpg"), member
        )

    def test_rejects_traversal(self):
        with self.assertRaises(MODULE.ContainerError):
            MODULE.safe_member_path(
                "Kurt Schwarz Fotografie/../../outside.jpg"
            )

    def test_rejects_other_project_member(self):
        with self.assertRaises(MODULE.ContainerError):
            MODULE.safe_member_path("Another Project/file.jpg")


if __name__ == "__main__":
    unittest.main()
