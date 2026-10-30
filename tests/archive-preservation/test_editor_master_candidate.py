from __future__ import annotations

import json
import subprocess
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
import tiff_accession  # noqa: E402
from archive_core.layout import object_relative_path  # noqa: E402
from workflows import editor_reconcile, master_candidate  # noqa: E402


class EditorMasterCandidateTest(unittest.TestCase):
    def test_guarded_candidate_accession_embeds_reconciled_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "pilot"
            independent = base / "independent"
            catalog_core.initialize_root(
                root,
                tool_version="test",
                actor="Fixture Archivist",
            )
            config = catalog_core.load_config(root)
            config["independent_generation_root"] = str(independent)
            catalog_core.save_config(root, config)
            with closing(
                catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
            ) as catalog:
                catalog_core.create_unit(
                    catalog,
                    unit_id="UNIT-MISSING-MASTER",
                    title="Missing-master fixture",
                    kind="pilot",
                    actor="Fixture Archivist",
                )
                entity = catalog_core.create_entity(
                    catalog,
                    kind="archive_object",
                    canonical_name="Candidate fixture",
                    unit_id="UNIT-MISSING-MASTER",
                    membership_role="primary",
                    actor="Fixture Archivist",
                    assignment_year=2026,
                    domain={
                        "archival_label": "INV-42",
                        "descriptive_title": "Reconciled title",
                    },
                )
                editor_reconcile.reconcile_metadata(
                    catalog,
                    unit_id="UNIT-MISSING-MASTER",
                    entity_id=entity["entity_id"],
                    field_decisions={},
                    tag_decision={
                        "source": "corrected",
                        "values": ["Industry", "Factory"],
                    },
                    actor="Fixture Archivist",
                    reason="",
                    evidence="sha256:" + "b" * 64,
                )
                surrogate_relative = object_relative_path(
                    unit_id="UNIT-MISSING-MASTER",
                    inventory="INV-42",
                    entity_id=str(entity["entity_id"]),
                    role="preservation_surrogate",
                    suffix=".tif",
                ).as_posix()
                surrogate = root / surrogate_relative
                surrogate.parent.mkdir(parents=True)
                subprocess.run(
                    [
                        "magick",
                        "-size",
                        "12x8",
                        "xc:#756451",
                        "-depth",
                        "8",
                        "-compress",
                        "none",
                        str(surrogate),
                    ],
                    check=True,
                    capture_output=True,
                )
                independent_surrogate = independent / surrogate_relative
                independent_surrogate.parent.mkdir(parents=True)
                independent_surrogate.write_bytes(surrogate.read_bytes())
                surrogate.chmod(0o444)
                independent_surrogate.chmod(0o444)
                surrogate_digest = (
                    "sha256:" + catalog_core.sha256_file(surrogate)
                )
                catalog.execute(
                    """INSERT INTO content_blobs (
                           content_digest, size_bytes, media_type, width_px,
                           height_px, bit_depth, color_space, compression,
                           page_count, duration_ms, created_at
                       ) VALUES (?, ?, 'image/tiff', 12, 8, 8, 'RGB',
                                 'Uncompressed', 1, NULL, ?)""",
                    (
                        surrogate_digest,
                        surrogate.stat().st_size,
                        "2026-07-19T00:00:00Z",
                    ),
                )
                catalog.execute(
                    """INSERT INTO file_occurrences (
                           occurrence_id, unit_id, batch_id, content_digest,
                           occurrence_kind, source_root_id, relative_path,
                           original_filename, observed_mtime_utc,
                           observed_at_utc, status, note
                       ) VALUES (
                           'OCC-PRIOR-SURROGATE',
                           'UNIT-MISSING-MASTER', NULL, ?, 'archive', '',
                           ?, 'fixture-surrogate.tif', '', ?,
                           'verified', ''
                       )""",
                    (
                        surrogate_digest,
                        surrogate_relative,
                        "2026-07-19T00:00:00Z",
                    ),
                )
                catalog.execute(
                    """INSERT INTO entity_files (
                           entity_id, occurrence_id, role, status, created_at
                       ) VALUES (?, 'OCC-PRIOR-SURROGATE',
                                 'preservation_surrogate', 'active', ?)""",
                    (
                        entity["entity_id"],
                        "2026-07-19T00:00:00Z",
                    ),
                )
                catalog.commit()
            with mock.patch.object(
                catalog_core,
                "assert_independent_device",
            ):
                catalog_core.publish_generation(
                    root,
                    independent,
                    tool_version="test",
                    actor="Fixture Archivist",
                )
                upload = root / "work" / "editor-upload-staging" / "upload.tif"
                upload.parent.mkdir(parents=True)
                subprocess.run(
                    [
                        "magick",
                        "-size",
                        "24x16",
                        "xc:#8a765f",
                        "-depth",
                        "8",
                        "-compress",
                        "none",
                        str(upload),
                    ],
                    check=True,
                    capture_output=True,
                )
                before = catalog_core.sha256_file(upload)
                receipt = master_candidate.stage_candidate(
                    root,
                    unit_id="UNIT-MISSING-MASTER",
                    entity_id=entity["entity_id"],
                    uploaded_path=upload,
                    original_filename="editor master.tif",
                    actor="Fixture Archivist",
                    reason="",
                )
                self.assertEqual("", receipt["reason"])
                self.assertEqual(before, catalog_core.sha256_file(upload))
                current = master_candidate.latest_candidate(
                    root,
                    unit_id="UNIT-MISSING-MASTER",
                    entity_id=entity["entity_id"],
                )
                self.assertIsNotNone(current)
                result = master_candidate.approve_candidate(
                    root,
                    unit_id="UNIT-MISSING-MASTER",
                    entity_id=entity["entity_id"],
                    candidate_id=receipt["candidate_id"],
                    actor="Fixture Archivist",
                    manifest_digest="sha256:" + "c" * 64,
                    reason="",
                )
                catalog_core.publish_generation(
                    root,
                    independent,
                    tool_version="test",
                    actor="Fixture Archivist",
                )
            embedded = tiff_accession._exif_values(
                Path(result["master"]),
                ("XMP-dc:Subject",),
            )
            self.assertEqual("Factory, Industry", embedded["XMP-dc:Subject"])
            approval = json.loads(
                Path(result["approval"]).read_text(encoding="utf-8")
            )
            self.assertEqual("", approval["reason"])
            self.assertEqual(
                "accessioned_as_preservation_master",
                approval["state"],
            )
            self.assertEqual(
                {
                    "assigned_role": "preservation_master",
                    "preferred_representation_after_approval": (
                        "preservation_master"
                    ),
                    "prior_active_surrogates": 1,
                    "surrogates_retained": 1,
                },
                approval["representation_transition"],
            )
            with closing(
                catalog_core.connect_catalog(
                    root / "work" / "catalog.sqlite",
                    read_only=True,
                )
            ) as catalog:
                roles = catalog.execute(
                    """SELECT role, status FROM entity_files
                       WHERE entity_id = ?
                       ORDER BY role""",
                    (entity["entity_id"],),
                ).fetchall()
            self.assertIn(
                ("preservation_master", "active"),
                [tuple(row) for row in roles],
            )
            self.assertIn(
                ("preservation_surrogate", "active"),
                [tuple(row) for row in roles],
            )


if __name__ == "__main__":
    unittest.main()
