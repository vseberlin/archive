"""Reference diagnostics: baselines, holdouts and review export read only the projection."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from archive_research.reference import connect, load_matrix, store
from archive_research.reference_catalogue import heldout_mask
from archive_research import reference_diagnostics as rd

SPACE = "space-test"
CLUSTERS = (("tag-a", "Technisches Fotoarchiv (TFA)", 1960), ("tag-b", "Kurt Schwarz Fotografie", 1975),
            ("tag-c", "Sonstige", 1990))


def fixture(root: Path, per_cluster=40, seed=3):
    rng = np.random.default_rng(seed)
    centres = np.eye(3, 16, dtype=np.float32) * 4
    connection = connect(root)
    for c, (tag, collection, year) in enumerate(CLUSTERS):
        for n in range(per_cluster):
            vector = centres[c] + rng.normal(0, 1, 16).astype(np.float32)
            vector /= np.linalg.norm(vector)
            number = c * 1000 + n
            meta = {"md_object_id": f"{number:05d}", "inventory": f"F {number}", "title": f"t{number}",
                    "object_type": "Foto", "tags": [tag, "Elektrotechnik"], "places": ["Oberschöneweide"],
                    "collections": [collection], "people": [], "date_text": f"{year + n % 3}",
                    "year_start": year + n % 3, "year_end": year + n % 3,
                    "md_url": f"https://example.invalid/{number}", "preview_sha256": f"h{number}",
                    "preview_path": f"cache/{number}.jpg"}
            store(connection, "ref", meta["md_object_id"], SPACE, "k", vector, "", meta)
    connection.commit()
    connection.close()


class ReferenceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        fixture(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_vectorised_holdout_matches_evaluator_mask(self):
        connection = connect(self.root)
        ids, matrix, meta = load_matrix(connection, "ref", SPACE)
        connection.close()
        holdouts = rd.Holdouts(matrix, meta)
        for index in (0, 41, 119):
            expected = heldout_mask(index, matrix, meta, series_window=20, near_duplicate_similarity=.95, hide_series=True)
            got = holdouts.mask(index, window=20, near=.95, session=False)
            self.assertEqual(expected.tolist(), got.tolist())
            plain = heldout_mask(index, matrix, meta, series_window=20, near_duplicate_similarity=.95, hide_series=False)
            self.assertEqual(plain.tolist(), holdouts.mask(index, window=None, near=None, session=False).tolist())

    def test_session_holdout_hides_same_date_text_within_collection(self):
        connection = connect(self.root)
        _, matrix, meta = load_matrix(connection, "ref", SPACE)
        connection.close()
        mask = rd.Holdouts(matrix, meta).mask(0, window=None, near=None, session=True)
        same = [i for i, m in enumerate(meta) if m["collections"] == meta[0]["collections"] and m["date_text"] == meta[0]["date_text"]]
        self.assertEqual(sorted(np.flatnonzero(mask).tolist()), same)

    def test_diagnose_reports_matched_baselines_without_images_or_model(self):
        self.assertEqual(rd.main(["diagnose", "--work-root", str(self.root), "--sample", "60",
                                  "--windows", "5", "50", "--run", "t1", "--common-labels", "2"]), 0)
        report = json.loads((self.root / "diagnostics-t1.json").read_text())
        self.assertEqual((report["images_decoded"], report["models_loaded"], report["metadata_adopted"]), (0, 0, False))
        main = report["series_hidden"]
        # Common labels on every record: visual and prevalence baseline both trivially agree.
        self.assertEqual(main["visual"]["places"]["precision"], 1.0)
        self.assertEqual(main["prevalence_baseline_matched_count"]["places"]["precision"], 1.0)
        # Cluster-specific tags: visual beats a matched-count frequency baseline.
        self.assertGreater(main["visual"]["tags"]["precision"], main["prevalence_baseline_matched_count"]["tags"]["precision"])
        self.assertIn("elektrotechnik", report["common_labels_excluded"]["tags"])
        self.assertLess(main["date"]["all"]["visual_mae"], main["date"]["all"]["pool_median_mae"])
        self.assertEqual(set(main["strata"]), {"Technisches Fotoarchiv", "Kurt Schwarz", "other"})
        self.assertEqual(len(report["holdout_sweep"]), 4)
        self.assertEqual(len(report["sample_ids"]), 60)
        self.assertEqual(rd.main(["diagnose", "--work-root", str(self.root), "--sample", "60", "--run", "t1"]), 2)

    def test_preview_gap_reasons(self):
        self.assertEqual(rd.preview_gap({}), "no_object_images")
        self.assertEqual(rd.preview_gap({"object_images": [{"type": "audio"}]}), "only_non_image_types:audio")
        self.assertEqual(rd.preview_gap({"object_images": [{"type": "image", "filename_loc": "a.jpg"}]}),
                         "selected_image_without_folder")
        self.assertEqual(rd.preview_gap({"object_images": [{"type": "image", "is_main": "j", "folder": "f"},
                                                           {"type": "image", "folder": "f", "preview": "b.jpg"}]}),
                         "selected_image_without_filename_other_image_usable")
        self.assertEqual(rd.preview_gap({"object_images": [{"folder": "f", "preview": "a.jpg"}]}), "ok")

    def test_review_export_and_score(self):
        self.assertEqual(rd.main(["review-export", "--work-root", str(self.root), "--queries", "4", "--top", "3",
                                  "--run", "r1"]), 0)
        path = self.root / "review-r1.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 12)
        for row in rows:
            row["judgement"] = "same_type" if row["rank"] == "1" else "unrelated"
        judged = self.root / "judged.csv"
        with judged.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(rd.main(["review-score", "--review", str(judged)]), 0)
        rows[0]["judgement"] = "maybe"
        with judged.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(rd.main(["review-score", "--review", str(judged)]), 2)


if __name__ == "__main__":
    unittest.main()
