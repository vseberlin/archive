import importlib.util
import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[2] / "tools" / "archive-preservation"
SCRIPT = TOOLS / "jobs" / "provenance_restoration" / "unit_recovery.py"
SPEC = importlib.util.spec_from_file_location("unit_recovery", SCRIPT)
assert SPEC and SPEC.loader
unit_recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(unit_recovery)


class BoundedUnitRecoveryTest(unittest.TestCase):
    def test_bootstrap_and_resumable_md_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_parent = base / "protected"
            source = source_parent / "Paragon"
            source.mkdir(parents=True)
            (source / "VLV-1.jpg").write_bytes(b"one")
            (source / "VLV-2.tif").write_bytes(b"two")
            retained = base / "retained.sqlite"
            connection = sqlite3.connect(retained)
            connection.executescript(
                """CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT);
                   CREATE TABLE file_object_links (
                     file_id INTEGER, inventory_number TEXT
                   );"""
            )
            for index, name in enumerate(("VLV-1.jpg", "VLV-2.tif"), 1):
                connection.execute(
                    "INSERT INTO files VALUES (?, ?)",
                    (index, str(source / name)),
                )
                connection.execute(
                    "INSERT INTO file_object_links VALUES (?, ?)",
                    (index, f"VLV-{index}"),
                )
            connection.commit()
            connection.close()
            root = base / "recovery"
            unit_recovery.catalog_core.initialize_root(
                root, "test", "tester", retained
            )
            unit_recovery.PROTECTED_ROOTS = (source_parent,)
            result = unit_recovery.bootstrap(
                root=root,
                unit="WF-PARAGON",
                source_root=source,
                retained_index=retained,
            )
            self.assertEqual(2, result["files"])
            self.assertEqual(
                "unchanged",
                unit_recovery.bootstrap(
                    root=root,
                    unit="WF-PARAGON",
                    source_root=source,
                    retained_index=retained,
                )["state"],
            )

            def fetcher(query: str, _timeout: float):
                if query == "VLV-1":
                    return ([{
                        "object_id": 1,
                        "object_inventory_number": "VLV-1",
                    }], "")
                return ([], "HTTPError: HTTP Error 404: Not Found")

            captured = unit_recovery.capture_md(
                root=root,
                unit="WF-PARAGON",
                delay=0,
                fetcher=fetcher,
            )
            self.assertEqual({"found": 1, "not_found": 1}, captured["counts"])
            repeated = unit_recovery.capture_md(
                root=root,
                unit="WF-PARAGON",
                delay=0,
                fetcher=fetcher,
            )
            self.assertEqual(2, repeated["skipped_terminal"])
            evidence = json.loads(
                (root / "work/recovery/WF-PARAGON/museum-digital/objects/VLV-1.json").read_text()
            )
            self.assertEqual("found", evidence["state"])

    def test_prepare_reconciliation_is_resumable_and_keeps_sources_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_parent = base / "protected"
            source = source_parent / "Paragon"
            source.mkdir(parents=True)
            local_image = source / "VLV-1.jpg"
            local_image.write_bytes(b"local-image")
            supplemental = source_parent / "Alle Paragon tiffs"
            supplemental.mkdir()
            supplemental_tiff = supplemental / "VLV-1.tif"
            supplemental_tiff.write_bytes(b"supplemental-tiff")
            retained = base / "retained.sqlite"
            connection = sqlite3.connect(retained)
            connection.executescript(
                """CREATE TABLE files (
                     id INTEGER PRIMARY KEY, path TEXT, rel_path TEXT,
                     parent_dir TEXT, basename TEXT, stem TEXT,
                     extension TEXT, size INTEGER, mtime_ns INTEGER,
                     mtime_iso TEXT, kind TEXT
                   );
                   CREATE TABLE file_object_links (
                     id INTEGER PRIMARY KEY, file_id INTEGER,
                     inventory_number TEXT, link_type TEXT,
                     confidence INTEGER, source TEXT
                   );
                   CREATE TABLE objects (
                     inventory_number TEXT PRIMARY KEY, collection TEXT,
                     object_type TEXT, title TEXT, description TEXT,
                     md_id TEXT, preferred_source TEXT, updated_at TEXT
                   );"""
            )
            stat = local_image.stat()
            connection.execute(
                "INSERT INTO files VALUES (1,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(local_image), "VLV-1.jpg", str(source), "VLV-1.jpg",
                    "VLV-1", "jpg", stat.st_size, stat.st_mtime_ns, "", "image",
                ),
            )
            connection.execute(
                "INSERT INTO file_object_links VALUES (1,1,'VLV-1','metadata_asset_ref',95,'fixture')"
            )
            connection.execute(
                "INSERT INTO objects VALUES ('VLV-1','','Foto','Legacy title','','','','2026-01-01')"
            )
            connection.commit()
            connection.close()
            retained_before = retained.read_bytes()
            source_before = local_image.read_bytes()
            supplemental_before = supplemental_tiff.read_bytes()

            root = base / "recovery"
            unit_recovery.catalog_core.initialize_root(root, "test", "tester", retained)
            with unit_recovery.catalog_core.connect_catalog(
                root / "work" / "catalog.sqlite"
            ) as catalog:
                unit_recovery.catalog_core.create_unit(
                    catalog,
                    unit_id="WF-PARAGON",
                    title="Fixture recovery",
                    kind="retrospective",
                    actor="tester",
                )
            unit_recovery.PROTECTED_ROOTS = (source_parent,)
            unit_recovery.bootstrap(
                root=root,
                unit="WF-PARAGON",
                source_root=source,
                retained_index=retained,
            )
            supplemental_result = unit_recovery.capture_supplemental_source(
                root=root,
                unit="WF-PARAGON",
                source_root=supplemental,
            )
            self.assertEqual(1, supplemental_result["linked"])
            self.assertEqual(
                "unchanged",
                unit_recovery.capture_supplemental_source(
                    root=root,
                    unit="WF-PARAGON",
                    source_root=supplemental,
                )["state"],
            )
            supplemental_manifest = next(
                (root / "work/recovery/WF-PARAGON/supplemental-sources").glob(
                    "source-*.json"
                )
            )
            captured_manifest = json.loads(
                supplemental_manifest.read_text(encoding="utf-8")
            )
            captured_manifest["source"]["device"] += 1
            supplemental_manifest.write_text(
                json.dumps(captured_manifest, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                1,
                len(unit_recovery.supplemental_sources(
                    root / "work/recovery/WF-PARAGON"
                )),
            )
            terminal = (
                root / "work/recovery/WF-PARAGON/museum-digital/objects/VLV-1.json"
            )
            terminal.parent.mkdir(parents=True)
            terminal.write_text(
                json.dumps(
                    {
                        "state": "found",
                        "exact_objects": [
                            {
                                "object_id": "70001",
                                "object_inventory_number": "VLV-1",
                                "object_name": "Museum title",
                                "object_description": "Museum description",
                                "object_type": "Photograph",
                                "object_material_technique": "Paper",
                                "object_dimensions": "10 x 15 cm",
                                "object_tags": [{"tag_name": "Factory"}],
                                "object_images": [
                                    {
                                        "is_main": "j",
                                        "folder": "images/202001",
                                        "filename_loc": "fixture.jpg",
                                        "quell_id": 99,
                                        "name": "Museum image",
                                        "rights": "CC BY-SA",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def preview(_source: Path, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"preview")
                return True, ""

            def fetch(_image: dict, destination: Path, _timeout: float):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"museum-image")
                return True, "https://asset.museum-digital.org/fixture.jpg"

            patches = (
                mock.patch.object(unit_recovery.reconcile, "create_visual_preview", side_effect=preview),
                mock.patch.object(unit_recovery.reconcile, "fetch_external_image", side_effect=fetch),
                mock.patch.object(unit_recovery.reconcile, "image_dimensions", return_value=("100", "80")),
            )
            with patches[0], patches[1], patches[2]:
                first = unit_recovery.prepare_reconciliation(
                    root=root,
                    unit="WF-PARAGON",
                    actor="tester",
                )
                second = unit_recovery.prepare_reconciliation(
                    root=root,
                    unit="WF-PARAGON",
                    actor="tester",
                )
            self.assertEqual(1, first["created_entities"])
            self.assertEqual("imported", first["campaign_state"])
            self.assertEqual(0, second["created_entities"])
            self.assertEqual("unchanged", second["campaign_state"])
            with unit_recovery.catalog_core.connect_catalog(
                root / "work" / "catalog.sqlite"
            ) as catalog:
                entity_id = catalog.execute(
                    "SELECT entity_id FROM archive_objects"
                ).fetchone()[0]
                unit_recovery.editor_reconcile.reconcile_metadata(
                    catalog,
                    unit_id="WF-PARAGON",
                    entity_id=entity_id,
                    field_decisions={
                        "description": {
                            "source": "corrected",
                            "value": "Editor description",
                        }
                    },
                    tag_decision=None,
                    actor="tester",
                )
            planned = unit_recovery.accept_md_metadata_defaults(
                root=root,
                unit="WF-PARAGON",
                actor="tester",
            )
            self.assertEqual("planned", planned["state"])
            self.assertEqual(1, planned["records_planned"])
            self.assertEqual(4, planned["fields_planned"])
            self.assertEqual(1, planned["tags_planned"])
            self.assertEqual(1, planned["archive_keywords_planned"])
            applied = unit_recovery.accept_md_metadata_defaults(
                root=root,
                unit="WF-PARAGON",
                actor="tester",
                apply=True,
            )
            self.assertEqual("applied", applied["state"])
            self.assertEqual(1, applied["records_applied"])
            self.assertTrue(Path(applied["catalog_backup"]).is_file())
            repeated_defaults = unit_recovery.accept_md_metadata_defaults(
                root=root,
                unit="WF-PARAGON",
                actor="tester",
                apply=True,
            )
            self.assertEqual(0, repeated_defaults["records_planned"])
            self.assertEqual("", repeated_defaults["catalog_backup"])
            self.assertEqual(source_before, local_image.read_bytes())
            self.assertEqual(supplemental_before, supplemental_tiff.read_bytes())
            self.assertEqual(retained_before, retained.read_bytes())
            with unit_recovery.catalog_core.connect_catalog(
                root / "work" / "catalog.sqlite", read_only=True
            ) as catalog:
                row = catalog.execute(
                    """SELECT descriptive_title, description,
                              material_technique, dimensions
                       FROM archive_objects"""
                ).fetchone()
                self.assertEqual(
                    ("Museum title", "Editor description", "Paper", "10 x 15 cm"),
                    tuple(row),
                )
                entity_id = catalog.execute(
                    "SELECT entity_id FROM archive_objects"
                ).fetchone()[0]
                accepted = unit_recovery.editor_reconcile.accepted_metadata(
                    catalog, entity_id
                )
                self.assertEqual("Photograph", accepted["object_type"])
                self.assertEqual(["Factory", "Paragon"], accepted["tags"])
                paragon_source = catalog.execute(
                    """SELECT s.source_type, s.system, s.external_id
                       FROM evidence_assertions a
                       JOIN evidence_sources s ON s.source_id=a.source_id
                       WHERE a.subject_entity_id=?
                         AND a.predicate='archive_keyword'
                         AND a.status='accepted'
                         AND lower(a.value_text)='paragon'""",
                    (entity_id,),
                ).fetchone()
                self.assertEqual(("catalog", "", ""), tuple(paragon_source))
                self.assertEqual(
                    1, catalog.execute("SELECT count(*) FROM entities").fetchone()[0]
                )
            candidates = (
                root / "work/recovery/WF-PARAGON/source-candidates.csv"
            ).read_text(encoding="utf-8")
            self.assertIn('"unreviewed"', candidates)
            self.assertIn('"exact_filename_inventory"', candidates)
            self.assertIn('"supplemental:Alle Paragon tiffs/VLV-1.tif"', candidates)
            with unit_recovery.reconciliation.connect_campaign(
                root, read_only=True
            ) as campaign:
                self.assertEqual(
                    3,
                    campaign.execute(
                        "SELECT count(*) FROM visual_evidence"
                    ).fetchone()[0],
                )
                status = campaign.execute(
                    """SELECT identity_status, queue_status
                       FROM reconciliation_records"""
                ).fetchone()
                self.assertEqual(("pending", "pending"), tuple(status))


if __name__ == "__main__":
    unittest.main()
