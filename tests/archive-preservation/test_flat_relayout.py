from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))

import catalog_core  # noqa: E402
from workflows import relayout  # noqa: E402


class FlatRepresentationRelayoutTest(unittest.TestCase):
    def test_old_paths_are_copied_relinked_and_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "primary"
            independent = base / "independent"
            catalog_core.initialize_root(root, "test", "Fixture Archivist")
            independent.mkdir()
            config = catalog_core.load_config(root)
            config["independent_generation_root"] = str(independent)
            catalog_core.save_config(root, config)
            with closing(
                catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
            ) as catalog:
                catalog_core.create_unit(
                    catalog,
                    unit_id="UNIT-FLAT",
                    title="Flat layout fixture",
                    kind="fixture",
                    actor="Fixture Archivist",
                )
                entity_id = str(catalog_core.create_entity(
                    catalog,
                    kind="archive_object",
                    canonical_name="Fixture object",
                    unit_id="UNIT-FLAT",
                    membership_role="primary",
                    actor="Fixture Archivist",
                    assignment_year=2026,
                    domain={"archival_label": "TFA-1"},
                )["entity_id"])
                payload = b"fixture derivative bytes"
                digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                now = catalog_core.utc_now()
                old_relative = (
                    f"units/UNIT-FLAT/data/derivatives/{entity_id}/TFA-1.jpg"
                )
                catalog.execute(
                    """INSERT INTO content_blobs (
                           content_digest, size_bytes, media_type, width_px,
                           height_px, bit_depth, color_space, compression,
                           page_count, duration_ms, created_at
                       ) VALUES (?, ?, 'image/jpeg', 10, 10, 8, 'RGB',
                                 'JPEG', 1, NULL, ?)""",
                    (digest, len(payload), now),
                )
                catalog.execute(
                    """INSERT INTO file_occurrences (
                           occurrence_id, unit_id, batch_id, content_digest,
                           occurrence_kind, source_root_id, relative_path,
                           original_filename, observed_mtime_utc,
                           observed_at_utc, status, note
                       ) VALUES ('OCC-OLD', 'UNIT-FLAT', NULL, ?, 'archive',
                                 '', ?, 'TFA-1.jpg', ?, ?, 'verified', '')""",
                    (digest, old_relative, now, now),
                )
                catalog.execute(
                    """INSERT INTO entity_files (
                           entity_id, occurrence_id, role, status, created_at
                       ) VALUES (?, 'OCC-OLD', 'access_derivative', 'active', ?)""",
                    (entity_id, now),
                )
                catalog.commit()
            for base_root in (root, independent):
                old = base_root / old_relative
                old.parent.mkdir(parents=True)
                old.write_bytes(payload)
                old.chmod(0o444)
            with mock.patch.object(
                catalog_core, "assert_independent_device", return_value=None
            ):
                result = relayout.relayout_unit(
                    root, "UNIT-FLAT", "Fixture Archivist"
                )
                repeated = relayout.relayout_unit(
                    root, "UNIT-FLAT", "Fixture Archivist"
                )
            self.assertEqual(1, result["representations"])
            self.assertEqual(0, repeated["representations"])
            self.assertTrue((root / old_relative).is_file())
            self.assertTrue((independent / old_relative).is_file())
            with closing(
                catalog_core.connect_catalog(
                    root / "work" / "catalog.sqlite", read_only=True
                )
            ) as catalog:
                rows = catalog.execute(
                    """SELECT ef.status, fo.relative_path
                       FROM entity_files ef JOIN file_occurrences fo
                         ON fo.occurrence_id=ef.occurrence_id
                       WHERE ef.entity_id=? ORDER BY ef.status""",
                    (entity_id,),
                ).fetchall()
            active = next(row for row in rows if row["status"] == "active")
            superseded = next(
                row for row in rows if row["status"] == "superseded"
            )
            self.assertIn("/data/derivatives/", active["relative_path"])
            self.assertEqual(
                "derivatives",
                Path(active["relative_path"]).parent.name,
            )
            self.assertEqual(old_relative, superseded["relative_path"])
            self.assertTrue((root / active["relative_path"]).is_file())
            self.assertTrue((independent / active["relative_path"]).is_file())
            self.assertTrue(
                (root / "units" / "UNIT-FLAT" / "CATALOG.xlsx").is_file()
            )


if __name__ == "__main__":
    unittest.main()
