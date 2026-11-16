from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
import wf_sender_workbench as WORKBENCH  # noqa: E402


SCHEMA = """
CREATE TABLE md_objects (
  md_object_id TEXT PRIMARY KEY, inventory_raw TEXT, title TEXT,
  object_type TEXT, date_text TEXT, object_url TEXT,
  duplicate_inventory INTEGER, local_candidate_count INTEGER,
  local_search_state TEXT, review_state TEXT
);
CREATE TABLE md_resources (
  md_object_id TEXT, resource_id TEXT, resource_url TEXT, resource_role TEXT
);
CREATE TABLE local_objects (
  local_object_id INTEGER PRIMARY KEY, local_object_key TEXT,
  representative_inventory TEXT, group_count INTEGER,
  md_candidate_count INTEGER, local_only_archive_candidate INTEGER,
  review_state TEXT
);
CREATE TABLE local_groups (
  local_group_id INTEGER PRIMARY KEY, local_object_id INTEGER,
  source_family TEXT, issue_key_raw TEXT, year_directory TEXT,
  source_directory TEXT, source_relative_directory TEXT, file_count INTEGER,
  total_size_bytes INTEGER, md_candidate_count INTEGER,
  local_only_archive_candidate INTEGER, review_state TEXT
);
CREATE TABLE local_files (
  local_file_id INTEGER PRIMARY KEY, local_group_id INTEGER,
  source_path TEXT, source_relative_path TEXT, filename TEXT, extension TEXT,
  size_bytes INTEGER, mtime_ns INTEGER, file_role TEXT, page_sequence INTEGER
);
CREATE TABLE candidate_matches (
  md_object_id TEXT, local_group_id INTEGER, match_method TEXT,
  comparison_key TEXT, match_basis TEXT, review_state TEXT
);
CREATE TABLE decisions (
  decision_id INTEGER PRIMARY KEY, decided_utc TEXT, actor TEXT,
  subject_type TEXT, subject_id TEXT, decision TEXT, basis TEXT
);
"""


class WfSenderWorkbenchTest(unittest.TestCase):
    def build_fixture(self, root: Path) -> tuple[Path, Path]:
        primary = root / "work/reconciliation/primary"
        fallback = root / "work/reconciliation/fallback"
        primary.mkdir(parents=True)
        fallback.mkdir(parents=True)
        connection = sqlite3.connect(primary / "reconciliation.sqlite")
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO md_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("1", "WFS-1", "Duplicate", "Zeitung", "1951", "https://berlin.museum-digital.de/object/1", 1, 1, "primary_candidate_found", "needs_review"),
                ("2", "WFS-2", "Fallback", "Zeitung", "1952", "https://berlin.museum-digital.de/object/2", 0, 0, "primary_searched_fallback_not_searched", "needs_review"),
                ("3", "WFS-3", "Missing", "Zeitung", "1953", "https://berlin.museum-digital.de/object/3", 0, 0, "primary_searched_fallback_not_searched", "needs_review"),
            ],
        )
        connection.execute(
            "INSERT INTO local_objects VALUES (1, 'local-1', 'LOCAL-1', 1, 0, 1, 'needs_review')"
        )
        connection.executemany(
            "INSERT INTO local_groups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, "PDF", "LOCAL-1", "1951", "/evidence/local-1", "local-1", 1, 10, 0, 1, "needs_review"),
                (2, 1, "PDF", "WFS-1", "1951", "/evidence/wfs-1", "wfs-1", 1, 10, 1, 0, "needs_review"),
            ],
        )
        connection.executemany(
            "INSERT INTO local_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, "/evidence/local-1.pdf", "local-1.pdf", "local-1.pdf", ".pdf", 10, 1, "access_pdf", None),
                (2, 2, "/evidence/wfs-1.pdf", "wfs-1.pdf", "wfs-1.pdf", ".pdf", 10, 1, "access_pdf", None),
            ],
        )
        connection.execute(
            "INSERT INTO candidate_matches VALUES ('1', 2, 'exact', 'wfs-1', 'exact', 'needs_review')"
        )
        connection.commit()
        connection.close()
        with (fallback / "target-objects.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "md_object_id",
                    "inventory_raw",
                    "title",
                    "object_url",
                    "fallback_candidate_count",
                    "fallback_search_state",
                    "review_state",
                ],
            )
            writer.writeheader()
            writer.writerows(
                [
                    {
                        "md_object_id": "2",
                        "inventory_raw": "WFS-2",
                        "title": "Fallback",
                        "object_url": "https://example.test/2",
                        "fallback_candidate_count": "1",
                        "fallback_search_state": "fallback_candidate_found",
                        "review_state": "needs_review",
                    },
                    {
                        "md_object_id": "3",
                        "inventory_raw": "WFS-3",
                        "title": "Missing",
                        "object_url": "https://example.test/3",
                        "fallback_candidate_count": "0",
                        "fallback_search_state": "fallback_searched_no_path_or_member_candidate",
                        "review_state": "needs_review",
                    },
                ]
            )
        with (fallback / "filesystem-candidates.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            fields = [
                "md_object_id",
                "inventory_raw",
                "comparison_key",
                "alias_method",
                "match_basis",
                "fallback_root",
                "source_path",
                "source_relative_path",
                "filename",
                "extension",
                "size_bytes",
                "mtime_ns",
                "review_state",
                "note",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            common = {
                    "md_object_id": "2",
                    "inventory_raw": "WFS-2",
                    "comparison_key": "wfs-2",
                    "alias_method": "exact_inventory",
                    "match_basis": "path",
                    "fallback_root": "fallback",
                    "size_bytes": "10",
                    "mtime_ns": "1",
                    "review_state": "needs_review",
                    "note": "",
                }
            writer.writerow(
                {
                    **common,
                    "source_path": "/evidence/wfs-2.css",
                    "source_relative_path": "wfs-2.css",
                    "filename": "wfs-2.css",
                    "extension": ".css",
                }
            )
            writer.writerow(
                {
                    **common,
                    "alias_method": "documented_alias",
                    "source_path": "/evidence/generic-wfs-2.pdf",
                    "source_relative_path": "generic-wfs-2.pdf",
                    "filename": "generic-wfs-2.pdf",
                    "extension": ".pdf",
                }
            )
            writer.writerow(
                {
                    **common,
                    "source_path": "/evidence/wfs-2.pdf",
                    "source_relative_path": "wfs-2.pdf",
                    "filename": "wfs-2.pdf",
                    "extension": ".pdf",
                }
            )
        return primary, fallback

    def test_queue_is_bounded_and_decisions_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary, fallback = self.build_fixture(root)
            review = WORKBENCH.WfSenderReview(root, primary, fallback)
            records = review.records()
            self.assertEqual(
                {
                    "duplicate_inventory",
                    "fallback_candidates",
                    "missing_local",
                    "local_only",
                },
                {record["category"] for record in records},
            )
            review.save_decision(
                record_key="md:2",
                actor="tester",
                decision="confirm_local_candidate",
                candidate_id="fallback:3",
                note="Visual identity confirmed.",
            )
            self.assertEqual(
                ["fallback:3", "fallback:2"],
                [
                    candidate["candidate_id"]
                    for candidate in review.record("md:2")["candidates"]
                ],
            )
            decision = review.record("md:2")["decision"]
            self.assertEqual("confirm_local_candidate", decision["decision"])
            with closing(
                sqlite3.connect(primary / "reconciliation.sqlite")
            ) as connection:
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                )

    def test_cross_candidate_is_visible_on_md_and_local_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary, fallback = self.build_fixture(root)
            (primary.parents[1] / "cross-candidates.json").write_text(
                json.dumps(
                    {
                        "format": "iss-wf-sender-cross-candidates-v1",
                        "candidates": [
                            {
                                "md_object_id": "1",
                                "local_object_id": 1,
                                "method": "masthead_issue_title",
                                "basis": "Matching masthead issue and title.",
                                "confidence": "high",
                                "review_state": "needs_review",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            review = WORKBENCH.WfSenderReview(root, primary, fallback)
            md = review.record("md:1")
            self.assertIn(
                "group:1",
                {
                    candidate["candidate_id"]
                    for candidate in md["candidates"]
                    if candidate.get("suggested_cross_match")
                },
            )
            local = review.record("local:1")
            self.assertEqual("1", local["md_suggestions"][0]["md_object_id"])
            review.save_decision(
                record_key="md:1",
                actor="tester",
                decision="confirm_local_candidate",
                candidate_id="group:1",
                note="Visual identity confirmed.",
            )
            self.assertEqual(
                "linked_to_museum_digital",
                review.record("local:1")["decision"]["decision"],
            )

    def test_rejected_cross_candidate_is_marked_on_local_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary, fallback = self.build_fixture(root)
            (primary.parents[1] / "cross-candidates.json").write_text(
                json.dumps(
                    {
                        "format": "iss-wf-sender-cross-candidates-v1",
                        "candidates": [
                            {
                                "md_object_id": "1",
                                "local_object_id": 1,
                                "method": "masthead_issue_title",
                                "basis": "Conflicting masthead ordinal.",
                                "confidence": "low",
                                "review_state": "needs_review",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            review = WORKBENCH.WfSenderReview(root, primary, fallback)
            review.save_decision(
                record_key="md:1",
                actor="tester",
                decision="reject_local_candidates",
                candidate_id="group:1",
                note="Different issue.",
            )
            local = review.record("local:1")
            self.assertIsNone(local["decision"])
            self.assertEqual(
                "reject_local_candidates",
                local["md_suggestions"][0]["relationship_decision"],
            )

    def test_missing_local_record_accepts_md_fallback_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary, fallback = self.build_fixture(root)
            review = WORKBENCH.WfSenderReview(root, primary, fallback)
            self.assertEqual("missing_local", review.record("md:3")["category"])
            review.save_decision(
                record_key="md:3",
                actor="tester",
                decision="accept_md_only_fallback",
                candidate_id="",
                note="Bounded local search found no representation.",
            )
            self.assertEqual(
                "accept_md_only_fallback",
                review.record("md:3")["decision"]["decision"],
            )


if __name__ == "__main__":
    unittest.main()
