from __future__ import annotations

from contextlib import closing
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "archive-preservation" / "archive.py"
SPEC = importlib.util.spec_from_file_location("iss_archive_external_identity", SCRIPT)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


class ExternalIdentityReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.source = self.base / "source"
        self.source.mkdir()
        self.master = self.source / "carrier.tif"
        self.master.write_bytes(b"II*\x00fixture-tiff-payload")
        ARCHIVE.command_init(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                workstation_id="fixture",
            )
        )
        ARCHIVE.command_create_unit(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                unit="TEST-IDENTITY",
                title="Identity review fixture",
                kind="fixture",
            )
        )
        self.component = self.create_object("VLV-1130-1", "Concert photograph")
        self.carrier = self.create_object("VLV-1130", "Wall-newspaper carrier")
        intake = ARCHIVE.command_intake(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="candidate master",
                evidence="fixture source",
                unit="TEST-IDENTITY",
                batch="identity-fixture",
                source_root_id="fixture-source",
                source_root=str(self.source),
                path=self.master.name,
                role="master",
            )
        )
        self.master_link = ARCHIVE.command_link(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="candidate link",
                evidence="fixture filename",
                unit="TEST-IDENTITY",
                subject_type="object",
                subject_id=self.component,
                predicate="primary_master",
                object_type="source_file",
                object_id=intake["source_file_id"],
                status="accepted",
                confidence="certain",
            )
        )["relationship_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_object(self, inventory: str, label: str) -> str:
        return ARCHIVE.command_create_object(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                evidence="",
                unit="TEST-IDENTITY",
                assignment_year=2026,
                display_identifier=inventory,
                object_type="photograph",
                label=label,
                publication_status="unknown",
                status="active",
            )
        )["object_id"]

    def review(self, **overrides: str) -> dict:
        values = {
            "root": str(self.root),
            "actor": "curator",
            "note": "",
            "evidence": "side-by-side visual comparison",
            "unit": "TEST-IDENTITY",
            "local_object_id": self.component,
            "external_system": "museum-digital",
            "external_id": "65256",
            "snapshot_digest": "a" * 64,
            "outcome": "same_object",
            "related_object_id": "",
            "target_relationship_id": "",
            "confidence": "certain",
            "reason": "Images and descriptive scope match.",
        }
        values.update(overrides)
        return ARCHIVE.command_review_external_identity(SimpleNamespace(**values))

    def test_same_object_accepts_external_alias_and_logs_review(self) -> None:
        result = self.review()
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            review = connection.execute(
                "SELECT * FROM external_identity_reviews WHERE review_id = ?",
                (result["review_id"],),
            ).fetchone()
            identifier = connection.execute(
                "SELECT * FROM identifiers WHERE identifier_id = ?",
                (result["identifier_id"],),
            ).fetchone()
            review_log = connection.execute(
                """
                SELECT * FROM logbook
                WHERE entity_type = 'external_identity_reviews' AND entity_id = ?
                """,
                (result["review_id"],),
            ).fetchone()
        self.assertEqual("resolved", review["status"])
        self.assertEqual("same_object", review["outcome"])
        self.assertEqual("sha256:" + "a" * 64, review["snapshot_digest"])
        self.assertEqual("curator", review["actor"])
        self.assertEqual(self.component, identifier["entity_id"])
        self.assertEqual("museum-digital", identifier["scheme"])
        self.assertEqual("65256", identifier["value"])
        self.assertEqual("accepted", identifier["status"])
        self.assertEqual("curator", review_log["actor"])

    def test_component_of_carrier_accepts_alias_and_relationship(self) -> None:
        result = self.review(
            outcome="component_of_carrier",
            related_object_id=self.carrier,
            reason="The external record is the photograph contained in the full-page carrier.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            relationship = connection.execute(
                "SELECT * FROM relationships WHERE relationship_id = ?",
                (result["relationship_id"],),
            ).fetchone()
        self.assertEqual("component_of", relationship["predicate"])
        self.assertEqual(self.component, relationship["subject_id"])
        self.assertEqual(self.carrier, relationship["object_id"])
        self.assertEqual("accepted", relationship["status"])

    def test_wrong_master_rejects_only_the_link_and_never_changes_source(self) -> None:
        original = self.master.read_bytes()
        result = self.review(
            outcome="wrong_master",
            target_relationship_id=self.master_link,
            reason="The TIFF shows the carrier, not the component photograph.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            status = connection.execute(
                "SELECT status FROM relationships WHERE relationship_id = ?",
                (self.master_link,),
            ).fetchone()[0]
            identifiers = connection.execute(
                "SELECT COUNT(*) FROM identifiers WHERE scheme = 'museum-digital' AND value = '65256'"
            ).fetchone()[0]
        self.assertEqual(self.master_link, result["relationship_id"])
        self.assertEqual("rejected", status)
        self.assertEqual(0, identifiers)
        self.assertEqual(original, self.master.read_bytes())

    def test_not_same_rejects_existing_candidate_alias_and_defer_changes_nothing(self) -> None:
        candidate = ARCHIVE.command_add_identifier(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="candidate only",
                evidence="exact inventory",
                unit="TEST-IDENTITY",
                entity_type="object",
                entity_id=self.component,
                scheme="museum-digital",
                value="70001",
                status="unresolved",
            )
        )
        rejected = self.review(
            external_id="70001",
            outcome="not_same",
            snapshot_digest="",
            reason="The visible content does not match.",
        )
        deferred = self.review(
            external_id="70002",
            outcome="defer",
            snapshot_digest="",
            reason="The comparison image is insufficient.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            candidate_status = connection.execute(
                "SELECT status FROM identifiers WHERE identifier_id = ?",
                (candidate["identifier_id"],),
            ).fetchone()[0]
            deferred_row = connection.execute(
                "SELECT status FROM external_identity_reviews WHERE review_id = ?",
                (deferred["review_id"],),
            ).fetchone()[0]
            deferred_aliases = connection.execute(
                "SELECT COUNT(*) FROM identifiers WHERE scheme = 'museum-digital' AND value = '70002'"
            ).fetchone()[0]
        self.assertEqual("resolved", rejected["status"])
        self.assertEqual("rejected", candidate_status)
        self.assertEqual("deferred", deferred_row)
        self.assertEqual(0, deferred_aliases)

    def test_sealed_unit_refuses_review(self) -> None:
        db = self.root / "work" / "catalog.sqlite"
        with ARCHIVE.connect_db(db) as connection:
            ARCHIVE.set_context(connection, "fixture", evidence="fixture", note="seal guard")
            connection.execute(
                "UPDATE units SET status = 'sealed', sealed_at = ? WHERE unit_id = 'TEST-IDENTITY'",
                (ARCHIVE.utc_now(),),
            )
            connection.commit()
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "unit is not open"):
            self.review()


if __name__ == "__main__":
    unittest.main()
