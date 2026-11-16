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
SPEC = importlib.util.spec_from_file_location("iss_archive_metadata_decision", SCRIPT)
assert SPEC and SPEC.loader
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


class MetadataDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "archive"
        ARCHIVE.command_init(
            SimpleNamespace(root=str(self.root), actor="fixture", workstation_id="fixture")
        )
        ARCHIVE.command_create_unit(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                unit="TEST-METADATA",
                title="Metadata decision fixture",
                kind="fixture",
            )
        )
        self.object_id = ARCHIVE.command_create_object(
            SimpleNamespace(
                root=str(self.root),
                actor="fixture",
                note="",
                evidence="",
                unit="TEST-METADATA",
                assignment_year=2026,
                display_identifier="VLV-1130-1",
                object_type="photograph",
                label="Archival inventory label",
                publication_status="unknown",
                status="active",
            )
        )["object_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def review_identity(self) -> None:
        ARCHIVE.command_review_external_identity(
            SimpleNamespace(
                root=str(self.root),
                actor="curator",
                note="",
                evidence="visual comparison fixture",
                unit="TEST-METADATA",
                local_object_id=self.object_id,
                external_system="museum-digital",
                external_id="65256",
                snapshot_digest="a" * 64,
                outcome="same_object",
                related_object_id="",
                target_relationship_id="",
                confidence="certain",
                reason="All compared images show the same photograph.",
            )
        )

    def mark_as_carrier(self) -> None:
        db = self.root / "work" / "catalog.sqlite"
        with ARCHIVE.connect_db(db) as connection:
            ARCHIVE.set_context(
                connection,
                "fixture",
                evidence="compound carrier fixture",
                note="Represent object as a wall-newspaper carrier",
            )
            connection.execute(
                "UPDATE objects SET object_type = 'wall_newspaper_carrier' WHERE object_id = ?",
                (self.object_id,),
            )
            connection.commit()

    def decide(self, **overrides: object) -> dict:
        values: dict[str, object] = {
            "root": str(self.root),
            "actor": "curator",
            "note": "",
            "evidence": "visual reconciliation fixture",
            "unit": "TEST-METADATA",
            "object_id": self.object_id,
            "field_name": "description",
            "action": "accept_external",
            "accepted_value": "Photograph of the 1 May 1974 demonstration.",
            "external_system": "museum-digital",
            "external_id": "65256",
            "snapshot_digest": "a" * 64,
            "reason": "The external description matches the reviewed photograph.",
            "assignment_year": 1974,
            "event_title": "",
            "starts_on": "",
            "ends_on": "",
            "circa": False,
            "location": "",
            "event_description": "",
            "confidence": "certain",
        }
        values.update(overrides)
        return ARCHIVE.command_decide_metadata(SimpleNamespace(**values))

    def test_accept_external_updates_canonical_field_and_logs_decision(self) -> None:
        self.review_identity()
        result = self.decide()
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            archive_object = connection.execute(
                "SELECT archival_label, description FROM objects WHERE object_id = ?",
                (self.object_id,),
            ).fetchone()
            decision = connection.execute(
                "SELECT * FROM metadata_decisions WHERE decision_id = ?",
                (result["decision_id"],),
            ).fetchone()
            decision_log = connection.execute(
                """
                SELECT actor FROM logbook
                WHERE entity_type = 'metadata_decisions' AND entity_id = ?
                """,
                (result["decision_id"],),
            ).fetchone()
        self.assertEqual("Archival inventory label", archive_object["archival_label"])
        self.assertEqual(result["accepted_value"], archive_object["description"])
        self.assertEqual("accept_external", decision["action"])
        self.assertEqual("accepted", decision["status"])
        self.assertEqual("curator", decision_log["actor"])

    def test_corrected_value_reaches_canonical_object_record(self) -> None:
        self.review_identity()
        result = self.decide(
            field_name="material_technique",
            action="corrected",
            accepted_value="Gelatin silver print on photographic paper",
            reason="Curatorial inspection corrected both compared labels.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            value = connection.execute(
                "SELECT material_technique FROM objects WHERE object_id = ?",
                (self.object_id,),
            ).fetchone()[0]
        self.assertEqual(result["accepted_value"], value)

    def test_split_event_preserves_1987_carrier_context_and_1974_depicted_event(self) -> None:
        self.review_identity()
        carrier_event = ARCHIVE.command_create_event(
            SimpleNamespace(
                root=str(self.root),
                actor="curator",
                note="Carrier publication context",
                evidence="preserved local source row",
                unit="TEST-METADATA",
                assignment_year=1987,
                title="Wall-newspaper issue",
                starts_on="1987",
                ends_on="",
                circa=False,
                location="",
                description="The carrier was produced in 1987.",
                status="active",
            )
        )["event_id"]
        ARCHIVE.command_link(
            SimpleNamespace(
                root=str(self.root),
                actor="curator",
                note="Carrier context",
                evidence="preserved local source row",
                unit="TEST-METADATA",
                subject_type="object",
                subject_id=self.object_id,
                predicate="published_in_context",
                object_type="event",
                object_id=carrier_event,
                status="accepted",
                confidence="certain",
            )
        )
        result = self.decide(
            field_name="depicted_event",
            action="split_event",
            accepted_value="",
            assignment_year=1974,
            event_title="1 May demonstration",
            starts_on="1974-05-01",
            event_description="The photograph depicts the 1 May 1974 event.",
            reason="1987 describes the carrier; 1974 describes the depicted event.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT event_id, starts_on FROM events ORDER BY starts_on"
            ).fetchall()
            relationship = connection.execute(
                "SELECT * FROM relationships WHERE relationship_id = ?",
                (result["relationship_id"],),
            ).fetchone()
            decision = connection.execute(
                "SELECT event_id, action, status FROM metadata_decisions WHERE decision_id = ?",
                (result["decision_id"],),
            ).fetchone()
        self.assertEqual(["1974-05-01", "1987"], [row["starts_on"] for row in events])
        self.assertEqual("depicts_event", relationship["predicate"])
        self.assertEqual(result["event_id"], relationship["object_id"])
        self.assertEqual((result["event_id"], "split_event", "accepted"), tuple(decision))

    def test_defer_records_working_decision_without_changing_canonical_value(self) -> None:
        self.review_identity()
        result = self.decide(
            action="defer",
            accepted_value="",
            reason="Description evidence is still ambiguous.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            description = connection.execute(
                "SELECT description FROM objects WHERE object_id = ?",
                (self.object_id,),
            ).fetchone()[0]
            status = connection.execute(
                "SELECT status FROM metadata_decisions WHERE decision_id = ?",
                (result["decision_id"],),
            ).fetchone()[0]
        self.assertEqual("", description)
        self.assertEqual("deferred", status)

    def test_later_split_event_supersedes_the_prior_canonical_event(self) -> None:
        self.review_identity()
        first = self.decide(
            field_name="depicted_event",
            action="split_event",
            accepted_value="",
            assignment_year=1974,
            event_title="First interpretation",
            starts_on="1974",
            reason="Initial evidenced event interpretation.",
        )
        second = self.decide(
            field_name="depicted_event",
            action="split_event",
            accepted_value="",
            assignment_year=1975,
            event_title="Corrected interpretation",
            starts_on="1975",
            reason="Later evidence corrects the depicted-event date.",
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            first_event_status = connection.execute(
                "SELECT status FROM events WHERE event_id = ?",
                (first["event_id"],),
            ).fetchone()[0]
            first_relationship_status = connection.execute(
                "SELECT status FROM relationships WHERE relationship_id = ?",
                (first["relationship_id"],),
            ).fetchone()[0]
            second_event_status = connection.execute(
                "SELECT status FROM events WHERE event_id = ?",
                (second["event_id"],),
            ).fetchone()[0]
        self.assertEqual("superseded", first_event_status)
        self.assertEqual("rejected", first_relationship_status)
        self.assertEqual("active", second_event_status)

    def test_component_events_resolves_carrier_without_one_depicted_date(self) -> None:
        self.review_identity()
        self.mark_as_carrier()
        prior = self.decide(
            field_name="depicted_event",
            action="split_event",
            accepted_value="",
            assignment_year=1987,
            event_title="Mistaken carrier-wide date",
            starts_on="1987",
            reason="Initial carrier-wide interpretation.",
        )
        result = self.decide(
            field_name="depicted_event",
            action="component_events",
            accepted_value="",
            reason=(
                "The carrier is a montage; publication context and component "
                "photograph dates remain separately scoped."
            ),
        )
        db = self.root / "work" / "catalog.sqlite"
        with closing(sqlite3.connect(db)) as connection:
            prior_event_status = connection.execute(
                "SELECT status FROM events WHERE event_id = ?",
                (prior["event_id"],),
            ).fetchone()[0]
            prior_relationship_status = connection.execute(
                "SELECT status FROM relationships WHERE relationship_id = ?",
                (prior["relationship_id"],),
            ).fetchone()[0]
            decision = connection.execute(
                """
                SELECT action, accepted_value, event_id, status
                FROM metadata_decisions WHERE decision_id = ?
                """,
                (result["decision_id"],),
            ).fetchone()
        self.assertEqual("superseded", prior_event_status)
        self.assertEqual("rejected", prior_relationship_status)
        self.assertEqual(
            (
                "component_events",
                "No single carrier-level depicted date; dates belong to linked components.",
                None,
                "accepted",
            ),
            decision,
        )

    def test_component_events_upgrades_existing_metadata_decision_schema(self) -> None:
        self.review_identity()
        self.mark_as_carrier()
        db = self.root / "work" / "catalog.sqlite"
        with ARCHIVE.connect_db(db) as connection:
            for action in ("insert", "update", "delete"):
                connection.execute(
                    f'DROP TRIGGER IF EXISTS "log_metadata_decisions_{action}"'
                )
            connection.execute(
                "DROP INDEX IF EXISTS metadata_decisions_object_field_idx"
            )
            connection.execute(
                "ALTER TABLE metadata_decisions RENAME TO metadata_decisions_new"
            )
            connection.executescript(
                """
                CREATE TABLE metadata_decisions (
                    decision_id TEXT PRIMARY KEY,
                    unit_id TEXT NOT NULL REFERENCES units(unit_id),
                    object_id TEXT NOT NULL REFERENCES objects(object_id),
                    field_name TEXT NOT NULL CHECK (field_name IN (
                        'descriptive_title', 'description', 'material_technique',
                        'dimensions', 'depicted_event'
                    )),
                    action TEXT NOT NULL CHECK (action IN (
                        'keep_local', 'accept_external', 'corrected',
                        'split_event', 'defer'
                    )),
                    accepted_value TEXT NOT NULL DEFAULT '',
                    external_system TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    snapshot_digest TEXT NOT NULL DEFAULT '',
                    event_id TEXT REFERENCES events(event_id),
                    reason TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    decided_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('accepted', 'deferred')),
                    CHECK (
                        (action = 'split_event' AND field_name = 'depicted_event'
                            AND event_id IS NOT NULL)
                        OR (action = 'defer' AND event_id IS NULL)
                        OR (action IN (
                            'keep_local', 'accept_external', 'corrected'
                        ) AND field_name != 'depicted_event' AND event_id IS NULL)
                    )
                );
                DROP TABLE metadata_decisions_new;
                CREATE INDEX metadata_decisions_object_field_idx
                    ON metadata_decisions(object_id, field_name, decided_at_utc);
                """
            )
            connection.commit()

        result = self.decide(
            field_name="depicted_event",
            action="component_events",
            accepted_value="",
            reason="The old catalog is upgraded before recording carrier scope.",
        )
        with closing(sqlite3.connect(db)) as connection:
            table_sql = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'metadata_decisions'
                """
            ).fetchone()[0]
            decision = connection.execute(
                "SELECT action, status FROM metadata_decisions WHERE decision_id = ?",
                (result["decision_id"],),
            ).fetchone()
        self.assertIn("'component_events'", table_sql)
        self.assertEqual(("component_events", "accepted"), decision)

    def test_component_events_refuses_an_ordinary_photograph(self) -> None:
        self.review_identity()
        with self.assertRaisesRegex(
            ARCHIVE.ArchiveError,
            "explicitly represented compound carrier",
        ):
            self.decide(
                field_name="depicted_event",
                action="component_events",
                accepted_value="",
                reason="This ordinary photograph must retain photograph semantics.",
            )

    def test_identity_and_open_unit_gates_refuse_unsafe_decisions(self) -> None:
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "resolved external identity"):
            self.decide()
        self.review_identity()
        db = self.root / "work" / "catalog.sqlite"
        with ARCHIVE.connect_db(db) as connection:
            ARCHIVE.set_context(connection, "fixture", evidence="fixture", note="seal guard")
            connection.execute(
                "UPDATE units SET status = 'sealed', sealed_at = ? WHERE unit_id = 'TEST-METADATA'",
                (ARCHIVE.utc_now(),),
            )
            connection.commit()
        with self.assertRaisesRegex(ARCHIVE.ArchiveError, "unit is not open"):
            self.decide()


if __name__ == "__main__":
    unittest.main()
