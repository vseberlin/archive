import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_visual_review.py"
)
SPEC = importlib.util.spec_from_file_location("kurt_schwarz_visual_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class KurtSchwarzVisualReviewTests(unittest.TestCase):
    def test_review_freezes_all_six_exceptions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "visual.csv"
            fields = (
                "md_object_id",
                "current_inventory",
                "local_jpeg_path",
                "visual_state",
            )
            states = {
                "103958": "probable_visual_match_review",
                "109416": "visual_mismatch_review",
                "98451": "visual_mismatch_review",
                "98390": "visual_mismatch_review",
                "103603": "no_curated_local_jpeg",
                "51888": "no_curated_local_jpeg",
            }
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for object_id, state in states.items():
                    writer.writerow(
                        {
                            "md_object_id": object_id,
                            "current_inventory": f"inventory-{object_id}",
                            "local_jpeg_path": f"/source/{object_id}.jpg",
                            "visual_state": state,
                        }
                    )
            output = root / "out"
            summary = MODULE.run(source, output)
            self.assertEqual(6, summary["reviewed_rows"])
            self.assertEqual(0, summary["unresolved_rows"])
            with (output / "reviewed-visual-exceptions.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(
                all(row["deletion_authorized"] == "false" for row in rows)
            )
            alias = next(row for row in rows if row["md_object_id"] == "103603")
            self.assertEqual("confirmed_visual_alias", alias["reviewed_visual_state"])

    def test_review_rejects_missing_decision_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "visual.csv"
            source.write_text(
                "md_object_id,current_inventory,local_jpeg_path,visual_state\n"
                "51888,inventory,,no_curated_local_jpeg\n",
                encoding="utf-8",
            )
            with self.assertRaises(MODULE.ReviewError):
                MODULE.run(source, root / "out")


if __name__ == "__main__":
    unittest.main()
