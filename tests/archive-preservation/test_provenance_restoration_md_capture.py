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
from jobs.provenance_restoration import evidence as EVIDENCE  # noqa: E402
from jobs.provenance_restoration import md_capture as CAPTURE  # noqa: E402
from jobs.provenance_restoration import workbench as WORKBENCH  # noqa: E402
from jobs.provenance_restoration.safety import (  # noqa: E402
    ProtectedEvidenceError,
)


JPEG_BYTES = b"\xff\xd8\xfffixture museum-digital jpeg\xff\xd9"


class ProvenanceRestorationMDCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.catalog = self.base / "archive_index.sqlite"
        self.catalog.write_bytes(b"retained catalog fixture")
        self.local = self.base / "TFA-527856.jpg"
        self.local.write_bytes(b"local comparison jpeg")
        self.draft_path = self.base / "RESTORE-TFA-527856.draft.json"
        self.evidence_path = self.base / "MD-85974-59197.jpg"
        self.enriched_path = (
            self.base / "RESTORE-TFA-527856.enriched-draft.json"
        )
        self.draft = self._draft()
        BUNDLE.write_bundle(self.draft_path, self.draft)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _draft(self) -> dict:
        local_snapshot = {
            "catalog_fingerprint": EVIDENCE.file_fingerprint(self.catalog),
            "objects": [
                {
                    "inventory_number": "TFA-527856",
                    "title": "Local title",
                }
            ],
            "object_sources": [],
            "file_links": [],
            "provenance_until_md": [],
        }
        image = {
            "id": 1712,
            "md_id": "85974",
            "quell_id": "59197",
            "name": "Sitzungsraum, Bild 2",
            "is_main": "j",
            "folder": "images/202009",
            "filename_loc": "sitzungsraum-85974.jpg",
            "preview": "200w_sitzungsraum-85974.jpg",
        }
        md_snapshot = {
            "objects": [
                {
                    "md_id": "85974",
                    "inventory_number": "TFA-527856",
                    "object_name": "MD title",
                }
            ],
            "images": [image],
        }
        local_digest = EVIDENCE.file_digest(self.local)
        operation = {
            "catalog": str(self.catalog),
            "inventory_label": "TFA-527856",
            "mode": "fixture",
        }
        return {
            "format": BUNDLE.BUNDLE_FORMAT,
            "version": BUNDLE.BUNDLE_VERSION,
            "direction": BUNDLE.BUNDLE_DIRECTION,
            "case_id": "RESTORE-TFA-527856",
            "created_at": "2026-07-18T10:00:00Z",
            "inventory_label": "TFA-527856",
            "local_search_status": "exact_inventory",
            "derivation": {
                "kind": "source_extraction",
                "parent_bundle_digest": "",
                "operation_snapshot": operation,
                "operation_digest": BUNDLE.snapshot_digest(operation),
            },
            "lineage": {
                "state": "draft",
                "draft_bundle_digest": "",
                "decision_digest": "",
                "decision_snapshot": {},
            },
            "sources": [
                {
                    "source_id": "SOURCE-LOCAL-CATALOG",
                    "kind": "local_catalog_row",
                    "locator": "sqlite:fixture#inventory=TFA-527856",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": local_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(
                        local_snapshot
                    ),
                    "note": "Fixture local snapshot.",
                },
                {
                    "source_id": "SOURCE-MUSEUM-DIGITAL",
                    "kind": "museum_digital_snapshot",
                    "locator": "museum-digital:objects/85974",
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot": md_snapshot,
                    "snapshot_digest": BUNDLE.snapshot_digest(md_snapshot),
                    "note": "Fixture cached MD rows.",
                },
                {
                    "source_id": "SOURCE-LOCAL-FILE",
                    "kind": "local_filesystem",
                    "locator": str(self.local),
                    "captured_at": "2026-07-18T10:00:00Z",
                    "snapshot_digest": local_digest,
                    "note": "Fixture local file.",
                },
            ],
            "files": [
                {
                    "file_id": "FILE-LOCAL-DERIVATIVE",
                    "role": "local_derivative",
                    "source_id": "SOURCE-LOCAL-FILE",
                    "locator": str(self.local),
                    "content_digest": local_digest,
                    "size_bytes": self.local.stat().st_size,
                    "media_type": "image/jpeg",
                    "note": "",
                }
            ],
            "identity": {
                "state": "pending",
                "outcome": "unresolved",
                "reason": "",
                "actor": "",
                "decided_at": "",
                "evidence_source_ids": [
                    "SOURCE-LOCAL-CATALOG",
                    "SOURCE-MUSEUM-DIGITAL",
                    "SOURCE-LOCAL-FILE",
                ],
            },
            "metadata_decisions": [],
            "file_decision": {
                "state": "pending",
                "outcome": "unresolved",
                "selected_file_id": "",
                "verification_method": "",
                "verification_details": {},
                "reason": "",
                "actor": "",
                "decided_at": "",
            },
            "readiness": {
                "state": "draft",
                "file_state": "unresolved",
                "selected_file_id": "",
                "provenance_gaps": ["missing_local_master"],
                "blocking_gaps": [],
            },
        }

    def _fetch(self, url: str, timeout: float) -> dict:
        self.assertEqual(
            "https://asset.museum-digital.org/berlin/"
            "images/202009/sitzungsraum-85974.jpg",
            url,
        )
        self.assertEqual(30.0, timeout)
        return {
            "body": JPEG_BYTES,
            "http_status": 200,
            "requested_url": url,
            "final_url": url,
            "media_type": "image/jpeg",
            "content_length_header": len(JPEG_BYTES),
            "last_modified": "Fri, 25 Dec 2020 22:42:37 GMT",
            "etag": "",
        }

    def _verify_image(self, path: Path) -> dict:
        self.assertEqual(JPEG_BYTES, path.read_bytes())
        return {
            "format": "JPEG",
            "width": 200,
            "height": 150,
            "image_count": 1,
            "probe": "fixture",
        }

    def test_captures_bytes_and_creates_new_derived_draft(self) -> None:
        draft_before = EVIDENCE.file_fingerprint(self.draft_path)
        catalog_before = EVIDENCE.file_fingerprint(self.catalog)
        local_before = EVIDENCE.file_fingerprint(self.local)

        enriched, digest, capture = CAPTURE.capture_derivative(
            self.draft_path,
            self.evidence_path,
            self.enriched_path,
            captured_at="2026-07-18T12:00:00Z",
            fetcher=self._fetch,
            image_verifier=self._verify_image,
        )

        self.assertEqual(JPEG_BYTES, self.evidence_path.read_bytes())
        self.assertEqual(
            BUNDLE.bundle_digest(self.draft),
            enriched["derivation"]["parent_bundle_digest"],
        )
        self.assertEqual(
            "md_derivative_capture",
            enriched["derivation"]["kind"],
        )
        self.assertEqual(
            {"local_derivative", "museum_digital_derivative"},
            {file["role"] for file in enriched["files"]},
        )
        self.assertEqual(
            EVIDENCE.file_digest(self.evidence_path),
            capture["content_digest"],
        )
        self.assertEqual(digest, BUNDLE.bundle_digest(enriched))
        self.assertEqual(enriched, BUNDLE.read_bundle(self.enriched_path))
        self.assertEqual(
            draft_before,
            EVIDENCE.file_fingerprint(self.draft_path),
        )
        self.assertEqual(
            catalog_before,
            EVIDENCE.file_fingerprint(self.catalog),
        )
        self.assertEqual(
            local_before,
            EVIDENCE.file_fingerprint(self.local),
        )
        page = WORKBENCH.render_page(
            enriched,
            self.base / "reviewed.json",
            "fixture-token",
        )
        self.assertNotIn("Positive identity is disabled", page)
        self.assertNotIn('value="same_entity" disabled', page)

    def test_rejects_untrusted_redirect_before_writing(self) -> None:
        def redirected(url: str, timeout: float) -> dict:
            response = self._fetch(url, timeout)
            response["final_url"] = "https://example.invalid/image.jpg"
            return response

        with self.assertRaisesRegex(
            CAPTURE.MuseumDigitalCaptureError,
            "trusted asset boundary",
        ):
            CAPTURE.capture_derivative(
                self.draft_path,
                self.evidence_path,
                self.enriched_path,
                fetcher=redirected,
                image_verifier=self._verify_image,
            )
        self.assertFalse(self.evidence_path.exists())
        self.assertFalse(self.enriched_path.exists())

    def test_rejects_bad_signature_before_writing(self) -> None:
        def bad_bytes(url: str, timeout: float) -> dict:
            response = self._fetch(url, timeout)
            response["body"] = b"not a jpeg"
            return response

        with self.assertRaisesRegex(
            CAPTURE.MuseumDigitalCaptureError,
            "JPEG signature",
        ):
            CAPTURE.capture_derivative(
                self.draft_path,
                self.evidence_path,
                self.enriched_path,
                fetcher=bad_bytes,
                image_verifier=self._verify_image,
            )
        self.assertFalse(self.evidence_path.exists())

    def test_rejects_existing_or_protected_outputs_before_fetch(self) -> None:
        called = False

        def should_not_fetch(url: str, timeout: float) -> dict:
            nonlocal called
            called = True
            return self._fetch(url, timeout)

        self.evidence_path.write_bytes(b"existing")
        with self.assertRaisesRegex(
            CAPTURE.MuseumDigitalCaptureError,
            "already exist",
        ):
            CAPTURE.capture_derivative(
                self.draft_path,
                self.evidence_path,
                self.enriched_path,
                fetcher=should_not_fetch,
            )
        self.assertFalse(called)

        protected = (
            Path("/mnt/volume/archive")
            / f".md-capture-{self.base.name}.jpg"
        )
        with self.assertRaises(ProtectedEvidenceError):
            CAPTURE.capture_derivative(
                self.draft_path,
                protected,
                self.enriched_path,
                fetcher=should_not_fetch,
            )
        self.assertFalse(protected.exists())
        self.assertFalse(called)

    def test_rejects_ambiguous_cached_image_rows(self) -> None:
        draft = copy.deepcopy(self.draft)
        md_source = next(
            source
            for source in draft["sources"]
            if source["kind"] == "museum_digital_snapshot"
        )
        second = copy.deepcopy(md_source["snapshot"]["images"][0])
        second.update(
            {
                "id": 1713,
                "quell_id": "59198",
                "filename_loc": "second.jpg",
            }
        )
        md_source["snapshot"]["images"].append(second)
        md_source["snapshot_digest"] = BUNDLE.snapshot_digest(
            md_source["snapshot"]
        )
        BUNDLE.validate_bundle(draft)
        with self.assertRaisesRegex(
            CAPTURE.MuseumDigitalCaptureError,
            "ambiguous",
        ):
            CAPTURE.select_image(draft)


if __name__ == "__main__":
    unittest.main()
