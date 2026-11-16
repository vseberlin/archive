from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "tools" / "archive-preservation" / "catalog_core.py"
SPEC = importlib.util.spec_from_file_location("iss_archive_catalog_core", MODULE_PATH)
assert SPEC and SPEC.loader
CORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORE)


class MinimalCatalogCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "minimal-core-v2"
        self.preservation = self.base / "independent"
        self.retained = self.base / "retained-index.sqlite"
        connection = sqlite3.connect(self.retained)
        try:
            connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
            connection.execute("INSERT INTO evidence (value) VALUES ('broad search')")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self) -> None:
        CORE.initialize_root(
            self.root,
            tool_version="test",
            actor="fixture",
            retained_index=self.retained,
        )

    def publish(self) -> dict:
        with mock.patch.object(CORE, "assert_independent_device"):
            return CORE.publish_generation(
                self.root,
                self.preservation,
                tool_version="test",
                actor="fixture",
            )

    def test_frozen_entity_id_contract(self) -> None:
        self.assertEqual("J", CORE.entity_id_check_symbol(2026, "K7M3"))
        self.assertTrue(CORE.validate_entity_id("ISS-2026-K7M3-J"))
        self.assertTrue(CORE.validate_entity_id(" iss-2026-k7m3-j\n"))
        self.assertFalse(CORE.validate_entity_id("ISS-2026-K7M3-E"))
        self.assertFalse(CORE.validate_entity_id("ISS-2026-ILOU-J"))

        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            generated = {CORE.generate_entity_id(connection, 2026) for _ in range(100)}
        finally:
            connection.close()
        self.assertEqual(100, len(generated))
        self.assertTrue(all(CORE.validate_entity_id(value) for value in generated))

    def test_schema_guards_identity_aliases_and_logbook(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        now = CORE.utc_now()
        entity_id = "ISS-2026-K7M3-J"
        try:
            table_count = connection.execute(
                """SELECT count(*) FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
            ).fetchone()[0]
            self.assertEqual(23, table_count)
            self.assertEqual(
                len(CORE.DIRTY_TRACKED_TABLES) * 3,
                connection.execute(
                    """SELECT count(*) FROM sqlite_master
                       WHERE type = 'trigger'
                         AND name LIKE 'catalog_dirty_%'"""
                ).fetchone()[0],
            )
            self.assertEqual(
                ["archive_object", "historical_event", "physical_carrier"],
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT kind FROM entity_kinds ORDER BY kind"
                    )
                ],
            )
            connection.execute(
                """INSERT INTO entities (
                       entity_id, kind, status, created_at, updated_at
                   ) VALUES (?, 'archive_object', 'active', ?, ?)""",
                (entity_id, now, now),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE entities SET entity_id = ? WHERE entity_id = ?",
                    ("ISS-2026-K7M7-J", entity_id),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "entity_identifiers",
            ):
                connection.execute(
                    """INSERT INTO entity_identifiers (
                           identifier_id, entity_id, namespace, value,
                           normalized_value, role, status, created_at
                       ) VALUES (
                           'ID-1', ?, 'iss', ?, ?, 'external', 'active', ?
                       )""",
                    (entity_id, entity_id, entity_id, now),
                )
            log_id = connection.execute(
                "SELECT log_id FROM logbook LIMIT 1"
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE logbook SET note = 'changed' WHERE log_id = ?",
                    (log_id,),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_direct_catalog_mutations_cannot_skip_freshness_tracking(self) -> None:
        self.initialize()
        self.publish()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            baseline = int(CORE.catalog_state(connection)["catalog_revision"])
            connection.execute(
                """INSERT INTO units (
                       unit_id, title, kind, status, created_at
                   ) VALUES ('UNIT-DIRECT', 'Direct mutation', 'test',
                             'open', ?)""",
                (CORE.utc_now(),),
            )
            after_insert = int(CORE.catalog_state(connection)["catalog_revision"])
            connection.execute(
                """UPDATE units SET title = 'Direct mutation updated'
                   WHERE unit_id = 'UNIT-DIRECT'"""
            )
            after_update = int(CORE.catalog_state(connection)["catalog_revision"])
            connection.execute("DELETE FROM units WHERE unit_id = 'UNIT-DIRECT'")
            after_delete = int(CORE.catalog_state(connection)["catalog_revision"])
            connection.commit()
            self.assertEqual(
                (baseline + 1, baseline + 2, baseline + 3),
                (after_insert, after_update, after_delete),
            )
            state = CORE.catalog_state(connection)
            self.assertIsNotNone(state["dirty_since"])
            self.assertGreater(
                int(state["catalog_revision"]),
                int(state["published_revision"]),
            )
        finally:
            connection.close()

    def test_retained_index_is_fingerprinted_and_immutable_read_only(self) -> None:
        before = self.retained.stat()
        before_digest = hashlib.sha256(self.retained.read_bytes()).hexdigest()
        self.initialize()
        connection = CORE.connect_retained_index(self.root)
        try:
            self.assertEqual(
                "broad search",
                connection.execute("SELECT value FROM evidence").fetchone()[0],
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden (value TEXT)")
        finally:
            connection.close()
        after = self.retained.stat()
        after_digest = hashlib.sha256(self.retained.read_bytes()).hexdigest()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertEqual(before_digest, after_digest)

    def test_open_generation_round_trip_rebuilds_sqlite(self) -> None:
        self.initialize()
        catalog = self.root / "work" / "catalog.sqlite"
        connection = CORE.connect_catalog(catalog)
        entity_id = "ISS-2026-K7M3-J"
        now = CORE.utc_now()
        try:
            connection.execute(
                """INSERT INTO units (
                       unit_id, title, kind, status, created_at
                   ) VALUES ('TEST-PARAGON', 'Paragon test', 'retrospective',
                             'open', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO entities (
                       entity_id, kind, status, created_at, updated_at
                   ) VALUES (?, 'archive_object', 'active', ?, ?)""",
                (entity_id, now, now),
            )
            connection.execute(
                """INSERT INTO evidence_sources (
                       source_id, source_type, source_root_id, relative_path,
                       local_absolute_path
                   ) VALUES (
                       'SOURCE-1', 'spreadsheet', 'archive-intake',
                       'metadata/source.csv',
                       '/mnt/volume/archive-intake/private/source.csv'
                   )"""
            )
            connection.execute(
                """INSERT INTO entity_names (
                       name_id, entity_id, name_type, value, language, status,
                       source_id, created_at, updated_at
                   ) VALUES (
                       'NAME-1', ?, 'canonical', 'Paragon photograph', 'de',
                       'active', 'SOURCE-1', ?, ?
                   )""",
                (entity_id, now, now),
            )
            connection.execute(
                """INSERT INTO archive_objects (
                       entity_id, archival_label, descriptive_title,
                       created_at, updated_at
                   ) VALUES (?, 'VLV-1112', 'Paragon photograph', ?, ?)""",
                (entity_id, now, now),
            )
            connection.execute(
                """INSERT INTO entity_unit_memberships (
                       entity_id, unit_id, role, added_at
                   ) VALUES (?, 'TEST-PARAGON', 'primary', ?)""",
                (entity_id, now),
            )
            connection.commit()
        finally:
            connection.close()

        published = self.publish()
        generation = Path(published["generation"])
        self.assertEqual(
            published["manifest_digest"],
            CORE.verify_manifest(generation),
        )
        self.assertEqual(
            published["generation_id"],
            (self.root / "registry" / "CURRENT").read_text().strip(),
        )
        exported_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in generation.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("/mnt/volume/archive-intake/private", exported_text)

        rebuilt = self.base / "rebuilt.sqlite"
        CORE.rebuild_from_generation(generation, rebuilt)
        live_connection = CORE.connect_catalog(catalog, read_only=True)
        connection = CORE.connect_catalog(rebuilt, read_only=True)
        try:
            for table, _relative, excluded in CORE.CSV_EXPORTS:
                columns = CORE._table_columns(connection, table, excluded)
                selected = ", ".join(f'"{column}"' for column in columns)
                live_rows = [
                    tuple(row)
                    for row in live_connection.execute(
                        f'SELECT {selected} FROM "{table}" ORDER BY {selected}'
                    )
                ]
                rebuilt_rows = [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT {selected} FROM "{table}" ORDER BY {selected}'
                    )
                ]
                self.assertEqual(live_rows, rebuilt_rows, table)
            self.assertEqual(
                ("VLV-1112", "Paragon photograph"),
                tuple(
                    connection.execute(
                        """SELECT archival_label, descriptive_title
                           FROM archive_objects WHERE entity_id = ?""",
                        (entity_id,),
                    ).fetchone()
                ),
            )
            self.assertEqual(
                "",
                connection.execute(
                    """SELECT local_absolute_path
                       FROM evidence_sources WHERE source_id = 'SOURCE-1'"""
                ).fetchone()[0],
            )
            self.assertEqual(
                published["manifest_digest"],
                connection.execute(
                    """SELECT manifest_digest FROM filesystem_generations
                       WHERE status = 'current'"""
                ).fetchone()[0],
            )
        finally:
            live_connection.close()
            connection.close()

    def test_publication_freshness_and_filesystem_recovery(self) -> None:
        self.initialize()
        published = self.publish()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            state = CORE.catalog_state(connection)
            self.assertEqual(
                state["catalog_revision"],
                state["published_revision"],
            )
            CORE.create_unit(
                connection,
                unit_id="UNIT-ONE",
                title="Unit one",
                kind="retrospective",
                actor="fixture",
            )
            with self.assertRaisesRegex(
                CORE.CoreCatalogError,
                "filesystem projection is unpublished",
            ):
                CORE.require_filesystem_current(connection)
        finally:
            connection.close()
        current = CORE.status(self.root)
        self.assertEqual("unpublished", current["filesystem_freshness"])
        self.assertGreater(
            current["catalog_revision"],
            current["published_revision"],
        )

        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            connection.execute(
                """UPDATE filesystem_generations
                   SET status = 'failed'
                   WHERE generation_id = ?""",
                (published["generation_id"],),
            )
            connection.execute(
                """UPDATE catalog_state
                   SET published_revision = 0
                   WHERE singleton = 1"""
            )
            connection.commit()
        finally:
            connection.close()
        healed = CORE.status(self.root)
        self.assertTrue(healed["publication_state_healed"])
        self.assertEqual(published["generation_id"], healed["current_generation"])
        self.assertEqual("unpublished", healed["filesystem_freshness"])

    def test_guarded_entity_and_file_operations(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        digest_one = "sha256:" + "1" * 64
        digest_two = "sha256:" + "2" * 64
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-ONE",
                title="Unit one",
                kind="retrospective",
                actor="fixture",
            )
            CORE.create_unit(
                connection,
                unit_id="UNIT-TWO",
                title="Unit two",
                kind="retrospective",
                actor="fixture",
            )
            created = CORE.create_entity(
                connection,
                kind="archive_object",
                canonical_name="Fixture photograph",
                unit_id="UNIT-ONE",
                membership_role="primary",
                actor="fixture",
                assignment_year=2026,
                domain={"archival_label": "VLV-1"},
            )
            self.assertTrue(CORE.validate_entity_id(created["entity_id"]))
            self.assertEqual(
                1,
                connection.execute(
                    """SELECT count(*) FROM archive_objects
                       WHERE entity_id = ?""",
                    (created["entity_id"],),
                ).fetchone()[0],
            )

            first = CORE.record_file_occurrence(
                connection,
                content_digest=digest_one,
                size_bytes=10,
                occurrence_kind="source",
                source_root_id="source-a",
                relative_path="images/item.tif",
                original_filename="item.tif",
                observed_at_utc="2026-07-17T10:00:00Z",
                actor="fixture",
            )
            CORE.link_entity_file(
                connection,
                entity_id=created["entity_id"],
                occurrence_id=first["occurrence_id"],
                role="source_capture",
                actor="fixture",
            )
            repeated = CORE.record_file_occurrence(
                connection,
                content_digest=digest_one,
                size_bytes=10,
                occurrence_kind="source",
                source_root_id="source-a",
                relative_path="images/item.tif",
                original_filename="item.tif",
                observed_at_utc="2026-07-17T10:00:00Z",
                actor="fixture",
            )
            changed = CORE.record_file_occurrence(
                connection,
                content_digest=digest_two,
                size_bytes=11,
                occurrence_kind="source",
                source_root_id="source-a",
                relative_path="images/item.tif",
                original_filename="item.tif",
                observed_at_utc="2026-07-17T11:00:00Z",
                actor="fixture",
            )
            self.assertEqual(first["occurrence_id"], repeated["occurrence_id"])
            self.assertEqual("unchanged", repeated["state"])
            self.assertNotEqual(first["occurrence_id"], changed["occurrence_id"])
            self.assertEqual(
                first["occurrence_id"],
                connection.execute(
                    """SELECT occurrence_id
                       FROM entity_files
                       WHERE entity_id = ? AND role = 'source_capture'""",
                    (created["entity_id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                connection.execute(
                    """SELECT count(*) FROM file_occurrences
                       WHERE source_root_id = 'source-a'
                         AND relative_path = 'images/item.tif'
                         AND occurrence_kind = 'source'"""
                ).fetchone()[0],
            )

            archive_occurrences = []
            for unit_id in ("UNIT-ONE", "UNIT-TWO"):
                archive_occurrences.append(CORE.record_file_occurrence(
                    connection,
                    content_digest=digest_one,
                    size_bytes=10,
                    occurrence_kind="archive",
                    unit_id=unit_id,
                    relative_path="data/item.tif",
                    original_filename="item.tif",
                    observed_at_utc="2026-07-17T12:00:00Z",
                    actor="fixture",
                ))
            for occurrence in archive_occurrences:
                CORE.link_entity_file(
                    connection,
                    entity_id=created["entity_id"],
                    occurrence_id=occurrence["occurrence_id"],
                    role="master",
                    actor="fixture",
                )
            self.assertEqual(
                2,
                connection.execute(
                    """SELECT count(*)
                       FROM entity_files ef
                       JOIN file_occurrences fo
                         ON fo.occurrence_id = ef.occurrence_id
                       WHERE ef.entity_id = ?
                         AND ef.role = 'master'
                         AND fo.content_digest = ?""",
                    (created["entity_id"], digest_one),
                ).fetchone()[0],
            )
            self.assertEqual(
                {"UNIT-ONE", "UNIT-TWO"},
                {
                    row[0]
                    for row in connection.execute(
                        """SELECT fo.unit_id
                           FROM entity_files ef
                           JOIN file_occurrences fo
                             ON fo.occurrence_id = ef.occurrence_id
                           WHERE ef.entity_id = ? AND ef.role = 'master'""",
                    (created["entity_id"],),
                    )
                },
            )
        finally:
            connection.close()

    def test_editor_can_correct_component_carrier_without_deleting_history(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-CARRIERS",
                title="Carrier review",
                kind="retrospective",
                actor="fixture",
            )

            def archive_object(label: str) -> str:
                return str(
                    CORE.create_entity(
                        connection,
                        kind="archive_object",
                        canonical_name=label,
                        unit_id="UNIT-CARRIERS",
                        membership_role="primary",
                        actor="fixture",
                        assignment_year=2026,
                        domain={"archival_label": label},
                    )["entity_id"]
                )

            component = archive_object("VLV-COMPONENT")
            first_carrier = archive_object("VLV-CARRIER-ONE")
            second_carrier = archive_object("VLV-CARRIER-TWO")
            assigned = CORE.set_component_carrier(
                connection,
                component_entity_id=component,
                carrier_entity_id=first_carrier,
                actor="Fixture Archivist",
            )
            self.assertEqual("unpublished", assigned["state"])
            unchanged = CORE.set_component_carrier(
                connection,
                component_entity_id=component,
                carrier_entity_id=first_carrier,
                actor="Fixture Archivist",
            )
            self.assertEqual("unchanged", unchanged["state"])
            CORE.set_component_carrier(
                connection,
                component_entity_id=component,
                carrier_entity_id=second_carrier,
                actor="Fixture Archivist",
                reason="Corrected after visual review.",
            )
            relations = list(
                connection.execute(
                    """SELECT object_entity_id, status FROM entity_relations
                       WHERE subject_entity_id = ? AND predicate = 'component_of'
                       ORDER BY created_at, relation_id""",
                    (component,),
                )
            )
            self.assertEqual(
                [(first_carrier, "superseded"), (second_carrier, "accepted")],
                [tuple(row) for row in relations],
            )
            with self.assertRaisesRegex(CORE.CoreCatalogError, "cycle"):
                CORE.set_component_carrier(
                    connection,
                    component_entity_id=second_carrier,
                    carrier_entity_id=component,
                    actor="Fixture Archivist",
                )
            cleared = CORE.set_component_carrier(
                connection,
                component_entity_id=component,
                carrier_entity_id="",
                actor="Fixture Archivist",
            )
            self.assertEqual("unpublished", cleared["state"])
            cleared_again = CORE.set_component_carrier(
                connection,
                component_entity_id=component,
                carrier_entity_id="",
                actor="Fixture Archivist",
            )
            self.assertEqual("unchanged", cleared_again["state"])
            self.assertEqual(
                0,
                connection.execute(
                    """SELECT count(*) FROM entity_relations
                       WHERE subject_entity_id = ? AND predicate = 'component_of'
                         AND status = 'accepted'""",
                    (component,),
                ).fetchone()[0],
            )
            self.assertEqual(
                3,
                connection.execute(
                    """SELECT count(*) FROM logbook
                       WHERE action = 'set_component_carrier'"""
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_reconciliation_queue_is_explicit_reopenable_and_accession_aware(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-REVIEW",
                title="Review queue",
                kind="retrospective",
                actor="fixture",
            )
            entity_id = str(
                CORE.create_entity(
                    connection,
                    kind="archive_object",
                    canonical_name="Queue fixture",
                    unit_id="UNIT-REVIEW",
                    membership_role="primary",
                    actor="fixture",
                    assignment_year=2026,
                    domain={"archival_label": "QUEUE-1"},
                )["entity_id"]
            )
            self.assertNotIn(
                entity_id, CORE.reconciliation_review_statuses(connection)
            )
            completed = CORE.set_reconciliation_review_status(
                connection,
                entity_id=entity_id,
                status="reconciled",
                actor="Fixture Archivist",
            )
            self.assertEqual("reviewed", completed["state"])
            repeated = CORE.set_reconciliation_review_status(
                connection,
                entity_id=entity_id,
                status="reconciled",
                actor="Fixture Archivist",
            )
            self.assertEqual("unchanged", repeated["state"])
            CORE.set_reconciliation_review_status(
                connection,
                entity_id=entity_id,
                status="not_ready",
                actor="Fixture Archivist",
            )
            self.assertEqual(
                "not_ready",
                CORE.reconciliation_review_statuses(connection)[entity_id],
            )
            CORE.record_recovery_accession_provenance(
                connection,
                entity_id=entity_id,
                unit_id="UNIT-REVIEW",
                selection_digest="sha256:" + "1" * 64,
                master_digest="sha256:" + "2" * 64,
                derivative_digest="",
                museum_digital_id="70001",
                actor="Fixture Archivist",
            )
            self.assertEqual(
                "reconciled",
                CORE.reconciliation_review_statuses(connection)[entity_id],
            )
            retained_entity_id = str(
                CORE.create_entity(
                    connection,
                    kind="archive_object",
                    canonical_name="Retained duplicate",
                    unit_id="UNIT-REVIEW",
                    membership_role="primary",
                    actor="fixture",
                    assignment_year=2026,
                    domain={"archival_label": "QUEUE-2"},
                )["entity_id"]
            )
            rejected = CORE.set_reconciliation_review_status(
                connection,
                entity_id=entity_id,
                status="rejected",
                retained_entity_id=retained_entity_id,
                actor="Fixture Archivist",
                reason="Duplicate identity.",
            )
            self.assertEqual("reviewed", rejected["state"])
            detail = CORE.reconciliation_review_details(connection)[entity_id]
            self.assertEqual("rejected", detail["status"])
            self.assertEqual(retained_entity_id, detail["retained_entity_id"])
            self.assertEqual("Duplicate identity.", detail["reason"])
            payload = json.loads(
                connection.execute(
                    """SELECT after_json FROM logbook
                       WHERE action='set_reconciliation_review_status'
                         AND entity_id=? ORDER BY at_utc DESC, log_id DESC
                       LIMIT 1""",
                    (entity_id,),
                ).fetchone()[0]
            )
            self.assertTrue(payload["future_drain_review"])
            self.assertFalse(payload["deletion_authorized"])
            CORE.set_reconciliation_review_status(
                connection,
                entity_id=entity_id,
                status="not_ready",
                actor="Fixture Archivist",
            )
            self.assertEqual(
                "not_ready",
                CORE.reconciliation_review_statuses(connection)[entity_id],
            )
        finally:
            connection.close()

    def test_recovery_correction_readiness_is_explicit_and_reopenable(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-CORRECTION",
                title="Correction queue",
                kind="retrospective",
                actor="fixture",
            )
            entity_id = str(
                CORE.create_entity(
                    connection,
                    kind="archive_object",
                    canonical_name="Correction fixture",
                    unit_id="UNIT-CORRECTION",
                    membership_role="primary",
                    actor="fixture",
                    assignment_year=2026,
                    domain={"archival_label": "CORRECTION-1"},
                )["entity_id"]
            )
            self.assertNotIn(
                entity_id, CORE.recovery_correction_states(connection)
            )
            ready = CORE.set_recovery_correction_status(
                connection,
                entity_id=entity_id,
                state="ready",
                actor="Fixture Archivist",
                reason="Catalog correction reviewed.",
            )
            self.assertEqual("ready", ready["correction_state"])
            detail = CORE.recovery_correction_states(connection)[entity_id]
            self.assertEqual("ready", detail["state"])
            self.assertEqual("Catalog correction reviewed.", detail["reason"])
            repeated = CORE.set_recovery_correction_status(
                connection,
                entity_id=entity_id,
                state="ready",
                actor="Fixture Archivist",
            )
            self.assertEqual("unchanged", repeated["state"])
            CORE.set_recovery_correction_status(
                connection,
                entity_id=entity_id,
                state="open",
                actor="Fixture Archivist",
            )
            self.assertEqual(
                "open",
                CORE.recovery_correction_states(connection)[entity_id]["state"],
            )
        finally:
            connection.close()

    def test_merge_flattens_existing_incoming_redirects(self) -> None:
        self.initialize()
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            CORE.create_unit(
                connection,
                unit_id="UNIT-MERGE",
                title="Merge unit",
                kind="test",
                actor="fixture",
            )
            ids = []
            for label in ("A", "B", "C"):
                ids.append(
                    CORE.create_entity(
                        connection,
                        kind="archive_object",
                        canonical_name=f"Entity {label}",
                        unit_id="UNIT-MERGE",
                        membership_role="primary",
                        actor="fixture",
                        assignment_year=2026,
                        domain={"archival_label": label},
                    )["entity_id"]
                )
            entity_a, entity_b, entity_c = ids
            CORE.merge_entities(
                connection,
                former_entity_id=entity_b,
                canonical_entity_id=entity_a,
                actor="fixture",
                reason="B duplicates A",
            )
            CORE.merge_entities(
                connection,
                former_entity_id=entity_a,
                canonical_entity_id=entity_c,
                actor="fixture",
                reason="A duplicates C",
            )
            redirects = {
                row["former_entity_id"]: row["canonical_entity_id"]
                for row in connection.execute(
                    """SELECT former_entity_id, canonical_entity_id
                       FROM entity_redirects"""
                )
            }
            self.assertEqual(entity_c, redirects[entity_b])
            self.assertEqual(entity_c, redirects[entity_a])
            self.assertTrue(
                all(target not in redirects for target in redirects.values())
            )
        finally:
            connection.close()

    def test_same_device_publication_has_no_environment_bypass(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(CORE.CoreCatalogError, "different filesystem"):
            CORE.publish_generation(
                self.root,
                self.preservation,
                tool_version="test",
                actor="fixture",
            )

    def test_overlapping_publications_are_serialized(self) -> None:
        self.initialize()

        def publish() -> dict[str, object]:
            return CORE.publish_generation(
                self.root,
                self.preservation,
                tool_version="test",
                actor="fixture",
            )

        with mock.patch.object(CORE, "assert_independent_device"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: publish(), range(2)))

        self.assertEqual(2, len(results))
        connection = CORE.connect_catalog(self.root / "work" / "catalog.sqlite")
        try:
            current = connection.execute(
                "SELECT generation_id FROM filesystem_generations "
                "WHERE status='current'"
            ).fetchall()
            pending = connection.execute(
                "SELECT generation_id FROM filesystem_generations "
                "WHERE status='pending'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, len(current))
        self.assertEqual([], pending)
        self.assertEqual(
            current[0]["generation_id"],
            (self.root / "registry" / "CURRENT").read_text(encoding="utf-8").strip(),
        )

    def test_protected_source_roots_are_never_work_roots(self) -> None:
        for path in (
            Path("/mnt/volume/archive"),
            Path("/mnt/volume/archive-intake"),
            Path("/mnt/volume/archive/subdirectory"),
        ):
            with self.assertRaisesRegex(CORE.CoreCatalogError, "protected"):
                CORE.assert_safe_work_root(path)


if __name__ == "__main__":
    unittest.main()
