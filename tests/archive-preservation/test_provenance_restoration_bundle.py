from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
from jobs.provenance_restoration import bundle as BUNDLE  # noqa: E402


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
LOCAL_CATALOG_SNAPSHOT = {
    "object": {
        "inventory_number": "VLV-1112",
        "title": "Foto Günter Schubert; Foto, 1985",
    }
}
MD_SNAPSHOT = {
    "object": {
        "md_id": "70001",
        "object_name": "Foto Günter Schubert; Foto, 1985",
    }
}
DECISION_SNAPSHOT = {
    "format": "iss-provenance-restoration-review",
    "case_id": "RESTORE-VLV-1112",
}


def local_master_bundle() -> dict:
    return {
        "format": BUNDLE.BUNDLE_FORMAT,
        "version": BUNDLE.BUNDLE_VERSION,
        "direction": "sources_to_archive",
        "case_id": "RESTORE-VLV-1112",
        "created_at": "2026-07-18T10:00:00Z",
        "inventory_label": "VLV-1112",
        "local_search_status": "exact_inventory",
        "lineage": {
            "state": "reviewed",
            "draft_bundle_digest": SHA_A,
            "decision_digest": BUNDLE.snapshot_digest(DECISION_SNAPSHOT),
            "decision_snapshot": DECISION_SNAPSHOT,
        },
        "sources": [
            {
                "source_id": "SOURCE-LOCAL-CATALOG",
                "kind": "local_catalog_row",
                "locator": "sqlite:archive_index.sqlite#objects:VLV-1112",
                "captured_at": "2026-07-18T09:00:00Z",
                "snapshot": LOCAL_CATALOG_SNAPSHOT,
                "snapshot_digest": BUNDLE.snapshot_digest(
                    LOCAL_CATALOG_SNAPSHOT
                ),
                "note": "Exact row snapshot from the retained local catalog.",
            },
            {
                "source_id": "SOURCE-LOCAL-FILES",
                "kind": "local_filesystem",
                "locator": "archive:WF/Paragon/VLV-1112.tif",
                "captured_at": "2026-07-18T09:05:00Z",
                "snapshot_digest": SHA_B,
                "note": "Read-only local filesystem observation.",
            },
            {
                "source_id": "SOURCE-MD",
                "kind": "museum_digital_snapshot",
                "locator": "https://berlin.museum-digital.de/object/70001",
                "captured_at": "2026-07-18T09:10:00Z",
                "snapshot": MD_SNAPSHOT,
                "snapshot_digest": BUNDLE.snapshot_digest(MD_SNAPSHOT),
                "note": "Unchanged one-way MD JSON snapshot.",
            },
        ],
        "files": [
            {
                "file_id": "FILE-LOCAL-MASTER",
                "role": "local_master",
                "source_id": "SOURCE-LOCAL-FILES",
                "locator": "archive:WF/Paragon/VLV-1112.tif",
                "content_digest": SHA_B,
                "size_bytes": 23778664,
                "media_type": "image/tiff",
                "note": "",
            },
            {
                "file_id": "FILE-MD-DERIVATIVE",
                "role": "museum_digital_derivative",
                "source_id": "SOURCE-MD",
                "locator": "museum-digital:70001/image/main",
                "content_digest": SHA_C,
                "size_bytes": 1000,
                "media_type": "image/jpeg",
                "note": "Public derivative linked to the local master.",
            },
        ],
        "identity": {
            "state": "resolved",
            "outcome": "same_entity",
            "reason": "Inventory identifier and visual evidence agree.",
            "actor": "Fixture Archivist",
            "decided_at": "2026-07-18T10:05:00Z",
            "evidence_source_ids": [
                "SOURCE-LOCAL-CATALOG",
                "SOURCE-LOCAL-FILES",
                "SOURCE-MD",
            ],
        },
        "metadata_decisions": [
            {
                "field": "descriptive_title",
                "value": "Foto Günter Schubert; Foto, 1985",
                "action": "accept_museum_digital",
                "source_id": "SOURCE-MD",
                "source_locator": "object_name",
                "reason": "MD has the fuller verified title.",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:10:00Z",
            },
            {
                "field": "dimensions",
                "value": "181x240 mm",
                "action": "accept_local",
                "source_id": "SOURCE-LOCAL-CATALOG",
                "source_locator": "objects:VLV-1112/dimensions",
                "reason": "Local measurement is more precise.",
                "actor": "Fixture Archivist",
                "decided_at": "2026-07-18T10:11:00Z",
            },
        ],
        "file_decision": {
            "state": "resolved",
            "outcome": "verified_local_master",
            "selected_file_id": "FILE-LOCAL-MASTER",
            "verification_method": "sha256_and_stat_reverification",
            "verification_details": {
                "content_digest": SHA_B,
                "format": "TIFF",
            },
            "reason": "Candidate TIFF passed exact fixity verification.",
            "actor": "Fixture Archivist",
            "decided_at": "2026-07-18T10:12:00Z",
        },
        "readiness": {
            "state": "ready_for_ingest",
            "file_state": "verified_local_master",
            "selected_file_id": "FILE-LOCAL-MASTER",
            "provenance_gaps": [],
            "blocking_gaps": [],
        },
    }


class ProvenanceRestorationBundleTest(unittest.TestCase):
    def test_local_master_bundle_round_trips_without_source_writes(self) -> None:
        bundle = local_master_bundle()
        first_digest = BUNDLE.bundle_digest(bundle)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "RESTORE-VLV-1112.json"
            written_digest = BUNDLE.write_bundle(path, bundle)
            self.assertEqual(first_digest, written_digest)
            self.assertEqual(bundle, BUNDLE.read_bundle(path))
            with self.assertRaisesRegex(
                BUNDLE.ProvenanceBundleError,
                "refusing to replace",
            ):
                BUNDLE.write_bundle(path, bundle)

    def test_missing_local_master_may_select_explicit_md_surrogate(self) -> None:
        bundle = local_master_bundle()
        bundle["files"] = [
            file
            for file in bundle["files"]
            if file["role"] == "museum_digital_derivative"
        ]
        bundle["readiness"] = {
            "state": "ready_for_ingest",
            "file_state": "best_available_surrogate",
            "selected_file_id": "FILE-MD-DERIVATIVE",
            "provenance_gaps": ["missing_local_master"],
            "blocking_gaps": [],
        }
        bundle["file_decision"].update(
            {
                "outcome": "best_available_surrogate",
                "selected_file_id": "FILE-MD-DERIVATIVE",
                "reason": "No verified local master survives.",
            }
        )
        BUNDLE.validate_bundle(bundle)

    def test_md_derivative_cannot_be_declared_a_verified_local_master(self) -> None:
        bundle = local_master_bundle()
        bundle["readiness"]["selected_file_id"] = "FILE-MD-DERIVATIVE"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "requires a selected local_master",
        ):
            BUNDLE.validate_bundle(bundle)

    def test_md_metadata_requires_completed_unambiguous_local_search(self) -> None:
        for status in ("not_supplied", "ambiguous"):
            with self.subTest(status=status):
                bundle = local_master_bundle()
                bundle["local_search_status"] = status
                with self.assertRaisesRegex(
                    BUNDLE.ProvenanceBundleError,
                    "completed, unambiguous local search",
                ):
                    BUNDLE.validate_bundle(bundle)

    def test_ready_bundle_cannot_hide_blocking_or_deferred_work(self) -> None:
        blocked = local_master_bundle()
        blocked["readiness"]["blocking_gaps"] = ["identity_conflict"]
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "must not contain blocking gaps",
        ):
            BUNDLE.validate_bundle(blocked)

        deferred = copy.deepcopy(local_master_bundle())
        deferred["metadata_decisions"][0]["action"] = "defer"
        deferred["metadata_decisions"][0]["value"] = ""
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "must not contain deferred metadata",
        ):
            BUNDLE.validate_bundle(deferred)

    def test_bundle_rejects_reverse_or_sync_direction(self) -> None:
        bundle = local_master_bundle()
        bundle["direction"] = "bidirectional_sync"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "one-way from sources to archive",
        ):
            BUNDLE.validate_bundle(bundle)

    def test_bundle_requires_timezone_and_matching_file_source_kind(self) -> None:
        bundle = local_master_bundle()
        bundle["created_at"] = "2026-07-18T10:00:00"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "must include a timezone",
        ):
            BUNDLE.validate_bundle(bundle)

        bundle = local_master_bundle()
        bundle["files"][0]["source_id"] = "SOURCE-MD"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "local role",
        ):
            BUNDLE.validate_bundle(bundle)

    def test_embedded_structured_snapshot_must_match_its_digest(self) -> None:
        bundle = local_master_bundle()
        bundle["sources"][0]["snapshot"]["object"]["title"] = "Changed later"
        with self.assertRaisesRegex(
            BUNDLE.ProvenanceBundleError,
            "structured snapshot digest does not match",
        ):
            BUNDLE.validate_bundle(bundle)

    def test_review_comments_are_optional(self) -> None:
        bundle = local_master_bundle()
        bundle["identity"]["reason"] = ""
        bundle["file_decision"]["reason"] = ""
        for decision in bundle["metadata_decisions"]:
            decision["reason"] = ""
        BUNDLE.validate_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
