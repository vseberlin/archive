from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "tools"
    / "archive-preservation"
    / "wf_sender_collection_fallback_search.py"
)
SPEC = importlib.util.spec_from_file_location("wf_sender_fallback", SCRIPT)
assert SPEC and SPEC.loader
FALLBACK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FALLBACK
SPEC.loader.exec_module(FALLBACK)


class WfSenderFallbackSearchTest(unittest.TestCase):
    def target(self, key: str) -> dict:
        return {
            "md_object_id": "1",
            "inventory_raw": key,
            "aliases": [
                {
                    "comparison_key": key.casefold().replace("_", "-"),
                    "method": "documented_alias",
                    "basis": "fixture",
                    "pattern": FALLBACK.alias_pattern(
                        key.casefold().replace("_", "-")
                    ),
                }
            ],
        }

    def test_alias_pattern_handles_special_number_wording(self) -> None:
        pattern = FALLBACK.alias_pattern("wfs-1954-sa-01")
        self.assertIsNotNone(
            pattern.search("Scans/WFS_1954_Sonderausgabe_01/page-001.tif")
        )
        self.assertIsNone(pattern.search("Scans/WFS-1954-SA-010/page.tif"))

    def test_match_targets_is_bounded_by_numeric_token(self) -> None:
        targets = [self.target("WFS-FB-1953-48")]
        self.assertEqual(
            FALLBACK.match_targets("PDF/WFS-FB-1953-48.pdf", targets)[0][
                "md_object_id"
            ],
            "1",
        )
        self.assertEqual(
            FALLBACK.match_targets("PDF/WFS-FB-1953-480.pdf", targets),
            [],
        )

    def test_saved_upload_crosswalk_recovers_renamed_special_editions(self) -> None:
        aliases = FALLBACK.documented_alias_overrides("WFS-1955_SA-03-1")
        self.assertEqual(aliases[0]["comparison_key"], "wfs-1951-54-sa-03-1")
        target = {
            "md_object_id": "87094",
            "inventory_raw": "WFS-1955_SA-03-1",
            "aliases": aliases,
        }
        matches = FALLBACK.match_targets(
            "Uploads_alle-pdf-geordnet/WFS-1951-54_SA-03-1.pdf",
            [target],
        )
        self.assertEqual(matches[0]["md_object_id"], "87094")
        self.assertEqual(matches[0]["alias_method"], "documented_crosswalk")

    def test_wrong_year_inventory_crosswalk_uses_masthead_issue(self) -> None:
        aliases = FALLBACK.documented_alias_overrides("WFS-1990-SN-24")
        self.assertEqual(aliases[0]["comparison_key"], "wfs-1989-24")
        self.assertIsNotNone(
            aliases[0]["pattern"].search("PDF/1989/WFS-1989-24.pdf")
        )

    def test_undemonstrated_1990_special_issue_is_not_crosswalked(self) -> None:
        self.assertEqual(
            FALLBACK.documented_alias_overrides("WFS-1990_SN"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
