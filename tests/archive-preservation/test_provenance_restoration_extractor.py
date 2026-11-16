from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools" / "archive-preservation"
sys.path.insert(0, str(TOOLS))
from jobs.provenance_restoration import bundle as BUNDLE  # noqa: E402
from jobs.provenance_restoration import extractor as EXTRACTOR  # noqa: E402


SCHEMA = """
CREATE TABLE objects (
    inventory_number TEXT PRIMARY KEY,
    collection TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    md_id TEXT NOT NULL DEFAULT '',
    preferred_source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    parent_dir TEXT NOT NULL,
    basename TEXT NOT NULL,
    stem TEXT NOT NULL,
    extension TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    mtime_iso TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE file_object_links (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    inventory_number TEXT NOT NULL,
    link_type TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(file_id, inventory_number, link_type, source)
);
CREATE TABLE object_sources (
    id INTEGER PRIMARY KEY,
    inventory_number TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_type TEXT NOT NULL,
    row_number INTEGER,
    md_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    image_name TEXT NOT NULL DEFAULT '',
    master_name TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT ''
);
CREATE TABLE online_md_objects (
    md_id TEXT PRIMARY KEY,
    inventory_number TEXT NOT NULL DEFAULT '',
    normalized_inventory TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL DEFAULT '',
    object_description TEXT NOT NULL DEFAULT '',
    object_last_updated TEXT NOT NULL DEFAULT '',
    institution_id TEXT NOT NULL DEFAULT '',
    institution_name TEXT NOT NULL DEFAULT '',
    collection_json TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '',
    events_json TEXT NOT NULL DEFAULT '',
    licence_json TEXT NOT NULL DEFAULT '',
    image_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT ''
);
CREATE TABLE online_md_images (
    id INTEGER PRIMARY KEY,
    md_id TEXT NOT NULL,
    inventory_number TEXT NOT NULL DEFAULT '',
    quell_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    is_main TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    filename_loc TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    rights TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    creator TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '',
    UNIQUE(md_id, quell_id, filename_loc)
);
CREATE TABLE provenance_until_md (
    inventory_number TEXT PRIMARY KEY,
    md_id TEXT NOT NULL DEFAULT '',
    chain_status TEXT NOT NULL,
    local_title TEXT NOT NULL DEFAULT '',
    md_title TEXT NOT NULL DEFAULT '',
    md_inventory_number TEXT NOT NULL DEFAULT '',
    md_last_updated TEXT NOT NULL DEFAULT '',
    linked_files INTEGER NOT NULL DEFAULT 0,
    master_files INTEGER NOT NULL DEFAULT 0,
    derivative_files INTEGER NOT NULL DEFAULT 0,
    metadata_files INTEGER NOT NULL DEFAULT 0,
    local_source_rows INTEGER NOT NULL DEFAULT 0,
    md_image_count INTEGER NOT NULL DEFAULT 0,
    sample_master_paths TEXT NOT NULL DEFAULT '',
    sample_derivative_paths TEXT NOT NULL DEFAULT '',
    sample_metadata_paths TEXT NOT NULL DEFAULT '',
    md_url TEXT NOT NULL DEFAULT '',
    rebuilt_at TEXT NOT NULL
);
"""


class ProvenanceRestorationExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.master = self.base / "VLV-1112.tif"
        self.derivative = self.base / "VLV-1112.jpg"
        self.master.write_bytes(b"fixture TIFF master")
        self.derivative.write_bytes(b"fixture JPEG derivative")
        self.database = self.base / "archive_index.sqlite"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                """INSERT INTO objects VALUES (
                    'VLV-1112', '', 'Foto',
                    'Local title', 'Local description', '70001',
                    'fixture.xlsx', '2026-07-18T10:00:00'
                )"""
            )
            connection.execute(
                """INSERT INTO object_sources VALUES (
                    1, 'VLV-1112', 'fixture.xlsx', 'xlsx', 104, '70001',
                    'Local title', 'VLV-1112.jpg', 'VLV-1112.tif',
                    '{"inventory":"VLV-1112"}'
                )"""
            )
            for file_id, path, extension in (
                (1, self.master, "tif"),
                (2, self.derivative, "jpg"),
            ):
                stat = path.stat()
                connection.execute(
                    """INSERT INTO files VALUES (
                        ?, ?, ?, '', ?, 'VLV-1112', ?, ?, ?, ?, 'asset'
                    )""",
                    (
                        file_id,
                        str(path),
                        path.name,
                        path.name,
                        extension,
                        stat.st_size,
                        stat.st_mtime_ns,
                        "2026-07-18T10:00:00",
                    ),
                )
                connection.execute(
                    """INSERT INTO file_object_links VALUES (
                        ?, ?, 'VLV-1112', 'metadata_asset_ref', 95, 'fixture.xlsx'
                    )""",
                    (file_id, file_id),
                )
            connection.execute(
                """INSERT INTO online_md_objects VALUES (
                    '70001', 'VLV-1112', 'VLV-1112', 'MD title', 'Foto',
                    'MD description', '2026-07-01', '29', 'Industriesalon',
                    '[]', '[]', '[]', '{}', 1, '2026-07-18T09:00:00',
                    'ok', 'https://example.invalid/object/70001', '', '{"id":70001}'
                )"""
            )
            connection.execute(
                """INSERT INTO online_md_images VALUES (
                    1, '70001', 'VLV-1112', 'image-1', 'main', 'y', 'folder',
                    'VLV-1112.jpg', 'https://example.invalid/preview.jpg',
                    '', '', '', '{"image":"image-1"}'
                )"""
            )
            connection.execute(
                """INSERT INTO provenance_until_md VALUES (
                    'VLV-1112', '70001', 'local_master_to_md',
                    'Local title', 'MD title', 'VLV-1112', '2026-07-01',
                    2, 1, 1, 0, 1, 1, '[]', '[]', '[]',
                    'https://example.invalid/object/70001',
                    '2026-07-18T09:30:00'
                )"""
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_extracts_decision_free_draft_from_read_only_inputs(self) -> None:
        database_before = EXTRACTOR.file_fingerprint(self.database)
        master_before = EXTRACTOR.file_fingerprint(self.master)
        derivative_before = EXTRACTOR.file_fingerprint(self.derivative)

        draft = EXTRACTOR.extract_draft_bundle(
            self.database,
            "VLV-1112",
        )

        BUNDLE.validate_bundle(draft)
        self.assertEqual("sources_to_archive", draft["direction"])
        self.assertEqual("pending", draft["identity"]["state"])
        self.assertEqual([], draft["metadata_decisions"])
        self.assertEqual("draft", draft["readiness"]["state"])
        self.assertEqual(
            {"local_master_candidate", "local_derivative"},
            {file["role"] for file in draft["files"]},
        )
        self.assertIn(
            "museum_digital_snapshot",
            {source["kind"] for source in draft["sources"]},
        )
        self.assertEqual(database_before, EXTRACTOR.file_fingerprint(self.database))
        self.assertEqual(master_before, EXTRACTOR.file_fingerprint(self.master))
        self.assertEqual(
            derivative_before,
            EXTRACTOR.file_fingerprint(self.derivative),
        )

    def test_missing_master_and_md_snapshot_remain_visible_gaps(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "DELETE FROM file_object_links WHERE file_id = 1"
            )
            connection.execute("DELETE FROM online_md_images")
            connection.execute("DELETE FROM online_md_objects")
            connection.execute(
                "UPDATE objects SET md_id = '' WHERE inventory_number = 'VLV-1112'"
            )
            connection.execute(
                """UPDATE object_sources SET md_id = ''
                   WHERE inventory_number = 'VLV-1112'"""
            )
            connection.execute(
                """UPDATE provenance_until_md SET md_id = ''
                   WHERE inventory_number = 'VLV-1112'"""
            )
            connection.commit()
        finally:
            connection.close()

        draft = EXTRACTOR.extract_draft_bundle(
            self.database,
            "VLV-1112",
        )
        self.assertIn(
            "missing_local_master",
            draft["readiness"]["provenance_gaps"],
        )
        self.assertIn(
            "museum_digital_snapshot_not_found",
            draft["readiness"]["provenance_gaps"],
        )
        self.assertNotIn(
            "museum_digital_snapshot",
            {source["kind"] for source in draft["sources"]},
        )

    def test_cli_writes_new_bundle_without_mutating_catalog(self) -> None:
        output = self.base / "RESTORE-VLV-1112.json"
        before = EXTRACTOR.file_fingerprint(self.database)
        result = EXTRACTOR.main(
            [
                "--catalog",
                str(self.database),
                "--inventory",
                "VLV-1112",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(0, result)
        self.assertEqual(before, EXTRACTOR.file_fingerprint(self.database))
        BUNDLE.read_bundle(output)

    def test_cli_rejects_output_inside_protected_evidence_root(self) -> None:
        output = (
            Path("/mnt/volume/archive")
            / f".protected-output-{self.base.name}.json"
        )
        before = EXTRACTOR.file_fingerprint(self.database)
        result = EXTRACTOR.main(
            [
                "--catalog",
                str(self.database),
                "--inventory",
                "VLV-1112",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(3, result)
        self.assertFalse(output.exists())
        self.assertEqual(before, EXTRACTOR.file_fingerprint(self.database))


if __name__ == "__main__":
    unittest.main()
