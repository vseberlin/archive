from __future__ import annotations

import http.cookiejar
import csv
import importlib.util
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "archive-preservation"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

ARCHIVE_SPEC = importlib.util.spec_from_file_location("iss_archive_workbench_test_archive", TOOL_DIR / "archive.py")
assert ARCHIVE_SPEC and ARCHIVE_SPEC.loader
ARCHIVE = importlib.util.module_from_spec(ARCHIVE_SPEC)
ARCHIVE_SPEC.loader.exec_module(ARCHIVE)

WORKBENCH_SPEC = importlib.util.spec_from_file_location("iss_archive_workbench", TOOL_DIR / "workbench.py")
assert WORKBENCH_SPEC and WORKBENCH_SPEC.loader
WORKBENCH = importlib.util.module_from_spec(WORKBENCH_SPEC)
WORKBENCH_SPEC.loader.exec_module(WORKBENCH)


class ArchiveWorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.source = self.base / "source"
        self.source.mkdir()
        self.image = self.source / "new-object.ppm"
        self.image.write_bytes(b"P6\n2 2\n255\n" + bytes((180, 30, 20, 30, 130, 70, 20, 50, 160, 220, 190, 50)))

        ARCHIVE.command_init(SimpleNamespace(root=str(self.root), actor="fixture", note="", workstation_id=None))

        self.server = WORKBENCH.create_server(self.root, port=0, validator_python=sys.executable)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path: str, form: dict[str, str] | None = None) -> tuple[bytes, str]:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(self.base_url + path, data=data)
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.client.open(request, timeout=15) as response:
            return response.read(), response.headers.get_content_type()

    def test_interactive_health_does_not_launch_deep_catalog_check(self) -> None:
        with mock.patch.object(
            WORKBENCH.archive,
            "assert_integrity",
            side_effect=AssertionError("interactive request launched deep integrity check"),
        ):
            overview, overview_type = self.request("/")
            health, health_type = self.request("/health")

        self.assertEqual("text/html", overview_type)
        self.assertEqual("text/html", health_type)
        self.assertIn(b"Archive overview", overview)
        self.assertIn(b"fast interactive read-only sweep", health)

    def test_source_registry_uses_completed_summary_for_global_totals(self) -> None:
        (self.source / "registry.csv").write_text(
            "inventory_number;object_title;printed_when_1\n"
            "VLV-1130-1;Wall newspaper;1987\n",
            encoding="utf-8",
        )
        result = ARCHIVE.command_scan_source_registry(SimpleNamespace(
            root=str(self.root), actor="fixture", note="summary fixture",
            source_root=[f"fixture-registry={self.source}"], max_hash_bytes=1024 * 1024,
            output="",
        ))
        summary_path = Path(str(result["output"])) / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.update({
            "unique_blobs": 12345,
            "duplicate_groups": 2345,
            "conflict_groups": 3456,
            "hashes_deferred": 4567,
        })
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        page, content_type = self.request("/sources?q=VLV-1130-1")

        self.assertEqual("text/html", content_type)
        self.assertIn(b"12345", page)
        self.assertIn(b"2345", page)
        self.assertIn(b"3456", page)
        self.assertIn(b"4567", page)
        self.assertIn(b"1987", page)

    def test_newcomer_intake_reconcile_browse_and_preview(self) -> None:
        body, content_type = self.request("/")
        self.assertEqual("text/html", content_type)
        self.assertIn(b"Archive overview", body)

        registry_text = (
            "inventory_number;object_title;printed_when_1\n"
            "VLV-1130-1;Wall newspaper;1987\n"
        )
        (self.source / "registry.csv").write_text(registry_text, encoding="utf-8")
        (self.source / "registry-copy.csv").write_text(registry_text, encoding="utf-8")
        ARCHIVE.command_scan_source_registry(SimpleNamespace(
            root=str(self.root), actor="fixture", note="workbench source registry fixture",
            source_root=[f"fixture-registry={self.source}"], max_hash_bytes=1024 * 1024,
            output="",
        ))
        body, _ = self.request("/sources?q=VLV-1130-1")
        self.assertIn(b"Source registry", body)
        self.assertIn(b"1987", body)
        self.assertIn(b"Exact duplicate paths are grouped", body)
        broad_body, _ = self.request("/sources?q=1987")
        self.assertIn(b"1987", broad_body)
        self.assertIn(b"Keyword results are deliberately bounded", broad_body)

        self.request("/session", {"actor": "New Archivist", "return_to": "/"})
        self.assertTrue(any(cookie.name == "iss_archive_actor" for cookie in self.cookies))

        body, _ = self.request("/unit/create", {
            "unit": "TEST-WORKBENCH",
            "title": "Workbench fixture",
            "kind": "fixture",
            "note": "Newcomer opened a practice unit",
        })
        self.assertIn(b"TEST-WORKBENCH is open", body)

        body, _ = self.request("/intake", {
            "unit": "TEST-WORKBENCH",
            "role": "master",
            "source_root": str(self.source),
            "path": self.image.name,
            "batch": "workbench-fixture",
            "source_root_id": "fixture-source",
            "evidence": "Newcomer fixture transfer",
            "note": "First workbench intake",
        })
        self.assertIn(b"File copied and verified", body)

        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            source_file_id = connection.execute("SELECT source_file_id FROM source_files").fetchone()[0]

        body, _ = self.request("/source-files?root_id=fixture-source")
        self.assertIn(b"new-object.ppm", body)

        body, _ = self.request("/reconcile/create-object", {
            "source_file_id": source_file_id,
            "unit": "TEST-WORKBENCH",
            "display_identifier": "NEW-001",
            "object_type": "photograph",
            "label": "Newcomer-created object",
            "assignment_year": "2026",
            "publication_status": "unknown",
            "evidence": "Filename and source note",
        })
        self.assertIn(b"Newcomer-created object", body)

        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            archive_object = connection.execute("SELECT * FROM objects").fetchone()
            relationship = connection.execute("SELECT * FROM relationships").fetchone()
            actors = {row[0] for row in connection.execute("SELECT actor FROM logbook")}
        self.assertEqual(source_file_id, relationship["object_id"])
        self.assertEqual(archive_object["object_id"], relationship["subject_id"])
        self.assertIn("New Archivist", actors)

        carrier = ARCHIVE.command_create_object(SimpleNamespace(
            root=str(self.root), actor="fixture", note="Carrier hierarchy fixture",
            evidence="Component belongs to the complete carrier", unit="TEST-WORKBENCH",
            assignment_year=2026, display_identifier="NEW-CARRIER",
            object_type="wall_newspaper_carrier", label="Complete fixture carrier",
            publication_status="unknown", status="active",
        ))
        ARCHIVE.command_link(SimpleNamespace(
            root=str(self.root), actor="fixture", note="Carrier hierarchy fixture",
            evidence="The high-resolution photograph is mounted on this carrier",
            unit="TEST-WORKBENCH", subject_type="object",
            subject_id=archive_object["object_id"], predicate="component_of",
            object_type="object", object_id=carrier["object_id"],
            status="accepted", confidence="certain",
        ))

        identifier = ARCHIVE.command_add_identifier(SimpleNamespace(
            root=str(self.root), actor="fixture", note="Needs newcomer decision", evidence="fixture conflict",
            unit="TEST-WORKBENCH", entity_type="object", entity_id=archive_object["object_id"],
            scheme="inventory", value="NEW-CONFLICT", status="unresolved",
        ))
        body, _ = self.request("/reconcile")
        self.assertIn(b"NEW-CONFLICT", body)
        self.request("/reconcile/status", {
            "kind": "identifier",
            "record_id": identifier["identifier_id"],
            "unit": "TEST-WORKBENCH",
            "status": "rejected",
        })
        with closing(sqlite3.connect(db)) as connection:
            status = connection.execute("SELECT status FROM identifiers WHERE identifier_id = ?", (identifier["identifier_id"],)).fetchone()[0]
            decision_actor = connection.execute("SELECT actor FROM logbook WHERE entity_type = 'identifiers' AND entity_id = ? ORDER BY at_utc DESC LIMIT 1", (identifier["identifier_id"],)).fetchone()[0]
        self.assertEqual("rejected", status)
        self.assertEqual("New Archivist", decision_actor)

        body, _ = self.request("/browse?q=NEW-001")
        self.assertIn(b"Newcomer-created object", body)
        preview, preview_type = self.request(f"/preview?id={urllib.parse.quote(source_file_id)}")
        self.assertTrue(preview_type.startswith("image/"))
        self.assertGreater(len(preview), 100)

        run = self.root / "work" / "reconciliation" / "museum-digital" / "20260714T000000Z"
        visual = run / "visual"
        visual.mkdir(parents=True)
        (visual / "source.ppm").write_bytes(self.image.read_bytes())
        (visual / "md.ppm").write_bytes(self.image.read_bytes())
        record = {
            "local_archival_id": archive_object["object_id"],
            "local_inventory": "NEW-001",
            "museum_digital_id": "70001",
            "local_archival_label": "Newcomer-created object",
            "source_record_status": "exact_inventory",
            "source_record_file": str(self.source / "registry.csv"),
            "source_record_sheet": "",
            "source_record_row": "2",
            "source_inventory": "VLV-1130-1",
            "source_title": "Source title",
            "museum_digital_title": "External title",
            "source_to_museum_digital_title": "different",
            "source_object_type": "Foto",
            "local_object_type": "photograph",
            "museum_digital_object_type": "Foto",
            "type_comparison": "different",
            "source_description": "Source description",
            "museum_digital_description": "External description",
            "source_to_museum_digital_description": "different",
            "source_material_technique": "Fotopapier",
            "museum_digital_material_technique": "Fotoabzug",
            "source_to_museum_digital_material": "different",
            "source_dimensions": "DIN A2",
            "museum_digital_dimensions": "",
            "source_to_museum_digital_dimensions": "not_available",
            "source_date": "1987",
            "museum_digital_event_times": "1974",
            "source_to_museum_digital_date": "different",
            "source_image_rights": "CC BY-SA",
            "museum_digital_primary_image_rights": "CC BY-NC-SA",
            "source_to_museum_digital_image_rights": "different",
        }
        with (run / "records.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(record))
            writer.writeheader()
            writer.writerow(record)
        image_rows = [
            {
                "image_id": "VIS-MASTER",
                "local_archival_id": archive_object["object_id"],
                "local_inventory": "NEW-001",
                "role": "preservation_master",
                "label": "new-object.ppm",
                "source_file_id": source_file_id,
                "original_path": "new-object.ppm",
                "snapshot_relpath": "",
                "evidence_relpath": "",
                "sha256": "a" * 64,
                "size_bytes": str(len(self.image.read_bytes())),
                "width": "2",
                "height": "2",
                "status": "available",
                "detail": "primary master",
            },
            {
                "image_id": "VIS-SOURCE",
                "local_archival_id": archive_object["object_id"],
                "local_inventory": "NEW-001",
                "role": "source_record_image",
                "label": "Source row image",
                "source_file_id": "",
                "original_path": "/fixture/source.ppm",
                "snapshot_relpath": "visual/source.ppm",
                "evidence_relpath": "",
                "sha256": "b" * 64,
                "size_bytes": str(len(self.image.read_bytes())),
                "width": "2",
                "height": "2",
                "status": "available",
                "detail": "",
            },
            {
                "image_id": "VIS-MD",
                "local_archival_id": archive_object["object_id"],
                "local_inventory": "NEW-001",
                "role": "museum_digital_snapshot",
                "label": "Museum-digital snapshot",
                "source_file_id": "",
                "original_path": "https://asset.museum-digital.org/fixture",
                "snapshot_relpath": "visual/md.ppm",
                "evidence_relpath": "",
                "sha256": "c" * 64,
                "size_bytes": str(len(self.image.read_bytes())),
                "width": "2",
                "height": "2",
                "status": "available",
                "detail": "",
            },
        ]
        with (run / "visual-comparison.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(image_rows[0]))
            writer.writeheader()
            writer.writerows(image_rows)

        visual_page, visual_type = self.request("/reconcile/visual")
        self.assertEqual("text/html", visual_type)
        self.assertIn(b"Review one archive item at a time", visual_page)
        self.assertIn(b"Source row image", visual_page)
        self.assertIn(b"Museum-digital snapshot", visual_page)
        self.assertIn(b"Yes \xe2\x80\x94 they show the same item", visual_page)
        self.assertIn(b"Do these images show the same archive item?", visual_page)
        self.assertNotIn(b"Identity outcome", visual_page)
        self.assertNotIn(b"Evidence and reason", visual_page)
        self.assertIn(b"/reconcile/identity", visual_page)
        self.assertIn(b"Preserved source record: registry.csv", visual_page)
        self.assertIn(b"Registry locator", visual_page)
        self.assertIn(b"row:2", visual_page)
        self.assertIn(b"High-resolution component of NEW-CARRIER", visual_page)
        self.assertIn(
            b"carrier-level value, not applicable to this component",
            visual_page,
        )
        record_link = re.search(rb'href="(/sources\?record=[^"]+)"', visual_page)
        self.assertIsNotNone(record_link)
        exact_source_page, exact_source_type = self.request(
            record_link.group(1).decode("ascii")
        )
        self.assertEqual("text/html", exact_source_type)
        self.assertIn(b"Exact preserved evidence", exact_source_page)
        self.assertIn(b"VLV-1130-1", exact_source_page)
        self.assertIn(b"Wall newspaper", exact_source_page)
        self.assertIn(b"registry.csv", exact_source_page)
        reviewed_page, reviewed_type = self.request("/reconcile/identity", {
            "run": "20260714T000000Z",
            "unit": "TEST-WORKBENCH",
            "local_object_id": archive_object["object_id"],
            "external_system": "museum-digital",
            "external_id": "70001",
            "snapshot_digest": "c" * 64,
            "outcome": "same_object",
            "related_object_id": "",
            "target_relationship_id": "",
            "confidence": "certain",
            "editor_note": "All three visual representations show the same photograph.",
        })
        self.assertEqual("text/html", reviewed_type)
        self.assertIn(b"Image-match answer", reviewed_page)
        self.assertIn(b"Choose what the archive should say", reviewed_page)
        self.assertIn(b"When did the scene in the photograph happen?", reviewed_page)
        self.assertNotIn(b"Archive assignment year", reviewed_page)
        self.assertNotIn(b"Starts on", reviewed_page)
        self.assertIn(b"/reconcile/metadata", reviewed_page)
        with self.assertRaises(urllib.error.HTTPError) as blocked_dimensions:
            self.request("/reconcile/metadata", {
                "run": "20260714T000000Z",
                "unit": "TEST-WORKBENCH",
                "object_id": archive_object["object_id"],
                "field_name": "dimensions",
                "action": "keep_local",
            })
        self.assertEqual(409, blocked_dimensions.exception.code)
        self.assertIn(
            b"carrier-level source metadata cannot be accepted as component metadata",
            blocked_dimensions.exception.read(),
        )
        with self.assertRaises(urllib.error.HTTPError) as blocked_corrected_dimensions:
            self.request("/reconcile/metadata", {
                "run": "20260714T000000Z",
                "unit": "TEST-WORKBENCH",
                "object_id": archive_object["object_id"],
                "field_name": "dimensions",
                "action": "corrected",
                "accepted_value": "DIN A2",
            })
        self.assertEqual(409, blocked_corrected_dimensions.exception.code)
        self.assertIn(
            b"DIN A2 describes the complete carrier, not this component",
            blocked_corrected_dimensions.exception.read(),
        )
        with closing(sqlite3.connect(db)) as connection:
            review = connection.execute(
                "SELECT outcome, status, actor FROM external_identity_reviews"
            ).fetchone()
            md_alias = connection.execute(
                """
                SELECT entity_id, status FROM identifiers
                WHERE scheme = 'museum-digital' AND value = '70001'
                """
            ).fetchone()
        self.assertEqual(("same_object", "resolved", "New Archivist"), review)
        self.assertEqual((archive_object["object_id"], "accepted"), md_alias)

        metadata_page, metadata_type = self.request("/reconcile/metadata", {
            "run": "20260714T000000Z",
            "unit": "TEST-WORKBENCH",
            "object_id": archive_object["object_id"],
            "external_system": "museum-digital",
            "external_id": "70001",
            "snapshot_digest": "c" * 64,
            "field_name": "description",
            "action": "accept_external",
            "source_value": "Source description",
            "local_value": "",
            "external_value": "Tampered hidden value",
            "accepted_value": "",
            "editor_note": "The external description precisely describes the reviewed image.",
        })
        self.assertEqual("text/html", metadata_type)
        self.assertIn(b"Description saved to the archive catalog", metadata_page)

        event_page, event_type = self.request("/reconcile/metadata", {
            "run": "20260714T000000Z",
            "unit": "TEST-WORKBENCH",
            "object_id": archive_object["object_id"],
            "external_system": "museum-digital",
            "external_id": "70001",
            "snapshot_digest": "c" * 64,
            "field_name": "depicted_event",
            "action": "split_event",
            "source_value": "1987",
            "local_value": "",
            "external_value": "1974",
            "accepted_value": "",
            "event_title": "1 May demonstration",
            "starts_on": "1974-05-01",
            "ends_on": "",
            "event_description": "The image depicts the 1974 event; 1987 describes its carrier.",
            "location": "",
            "editor_note": "The dates describe different archival scopes.",
        })
        self.assertEqual("text/html", event_type)
        self.assertIn(b"Date of the scene saved to the archive catalog", event_page)
        with closing(sqlite3.connect(db)) as connection:
            canonical_description = connection.execute(
                "SELECT description FROM objects WHERE object_id = ?",
                (archive_object["object_id"],),
            ).fetchone()[0]
            depicted_event = connection.execute(
                """
                SELECT e.starts_on, r.predicate
                FROM events e JOIN relationships r ON r.object_id = e.event_id
                WHERE r.subject_id = ? AND r.predicate = 'depicts_event'
                """,
                (archive_object["object_id"],),
            ).fetchone()
            metadata_decisions = connection.execute(
                "SELECT COUNT(*) FROM metadata_decisions WHERE object_id = ?",
                (archive_object["object_id"],),
            ).fetchone()[0]
        self.assertEqual("External description", canonical_description)
        self.assertEqual(("1974-05-01", "depicts_event"), depicted_event)
        self.assertEqual(2, metadata_decisions)

        with ARCHIVE.connect_db(db) as connection:
            ARCHIVE.set_context(
                connection,
                "fixture",
                evidence="carrier interface fixture",
                note="Represent this object as a compound carrier",
            )
            connection.execute(
                "UPDATE objects SET object_type = 'wall_newspaper_carrier' WHERE object_id = ?",
                (archive_object["object_id"],),
            )
            connection.commit()
        carrier_page, _ = self.request(
            f"/reconcile/visual?run=20260714T000000Z&object={archive_object['object_id']}"
        )
        self.assertIn(b"Does this complete carrier have one scene date?", carrier_page)
        self.assertIn(b"Use publication and component dates", carrier_page)
        self.assertIn(b"This replaces the current object-wide scene date", carrier_page)
        resolved_carrier_page, _ = self.request("/reconcile/metadata", {
            "run": "20260714T000000Z",
            "unit": "TEST-WORKBENCH",
            "object_id": archive_object["object_id"],
            "external_system": "museum-digital",
            "external_id": "70001",
            "snapshot_digest": "c" * 64,
            "field_name": "depicted_event",
            "action": "component_events",
            "editor_note": "The complete carrier contains separately dated photographs.",
        })
        self.assertIn(
            b"No single carrier scene date \xe2\x80\x94 component dates apply",
            resolved_carrier_page,
        )
        with closing(sqlite3.connect(db)) as connection:
            active_carrier_events = connection.execute(
                """
                SELECT COUNT(*) FROM relationships
                WHERE subject_id = ? AND predicate = 'depicts_event'
                  AND status = 'accepted'
                """,
                (archive_object["object_id"],),
            ).fetchone()[0]
            carrier_decision = connection.execute(
                """
                SELECT action, status FROM metadata_decisions
                WHERE object_id = ? AND action = 'component_events'
                LIMIT 1
                """,
                (archive_object["object_id"],),
            ).fetchone()
        self.assertEqual(0, active_carrier_events)
        self.assertEqual(("component_events", "accepted"), carrier_decision)

        source_snapshot, source_snapshot_type = self.request(
            "/reconcile/visual-image?run=20260714T000000Z&image=VIS-SOURCE"
        )
        self.assertEqual("image/x-portable-pixmap", source_snapshot_type)
        self.assertEqual(self.image.read_bytes(), source_snapshot)

        image_rows[2]["status"] = "missing"
        image_rows[2]["snapshot_relpath"] = ""
        image_rows[2]["detail"] = "External image snapshot unavailable"
        with (run / "visual-comparison.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(image_rows[0]))
            writer.writeheader()
            writer.writerows(image_rows)
        blocked_page, _ = self.request("/reconcile/visual")
        self.assertIn(b"The image match cannot be decided", blocked_page)
        self.assertIn(b"External image snapshot unavailable", blocked_page)

        source_after = self.image.read_bytes()
        self.assertEqual(b"P6\n2 2\n255\n" + bytes((180, 30, 20, 30, 130, 70, 20, 50, 160, 220, 190, 50)), source_after)


if __name__ == "__main__":
    unittest.main()
