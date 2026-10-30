import csv
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_reconciliation.py"
)
SPEC = importlib.util.spec_from_file_location(
    "kurt_schwarz_reconciliation", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class KurtSchwarzReconciliationTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        unit = root / "units" / "kurt-schwarz"
        work = root / "work" / "review"
        master = unit / "masters/objects/KS-1/data/KS-1.tif"
        variant = unit / "masters/objects/KS-1/data/variants/KS-2.tif"
        master.parent.mkdir(parents=True, exist_ok=True)
        variant.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"master")
        variant.write_bytes(b"variant")
        catalog = {
            "record_key": "md:1",
            "md_object_id": "1",
            "current_inventory": "KS-1",
            "historical_inventory": "KS-2",
            "current_title": "Museum title",
            "historical_title": "Local title",
            "current_description": "Same description.",
            "historical_description": "Same description",
            "current_tags": "Street; Berlin",
            "historical_tags": "Berlin, Street",
            "current_places": "Berlin-Mitte",
            "historical_places": "Berlin (DDR); Berlin-Mitte",
            "current_date_text": "1960",
            "historical_date_text": "1960",
            "current_material": "Negative",
            "historical_material": "Negative",
            "current_dimensions": "35 mm",
            "historical_dimensions": "35 mm",
            "selection_state": "provisional_multiple_tiff_review",
            "selected_source_format": "tiff",
            "selected_source_path": "/source/KS-1.tif",
            "tiff_candidate_count": "2",
            "jpeg_candidate_count": "0",
            "object_directory": "KS-1",
        }
        write_csv(unit / "CATALOG.csv", tuple(catalog), [catalog])
        representation_fields = (
            "record_keys",
            "final_relative_path",
            "role",
            "final_sha256",
            "size_bytes",
        )
        write_csv(
            unit / "db-restore/manifests/representations.csv",
            representation_fields,
            [
                {
                    "record_keys": "md:1",
                    "final_relative_path": "masters/objects/KS-1/data/KS-1.tif",
                    "role": "preservation_master",
                    "final_sha256": "sha256:" + "1" * 64,
                    "size_bytes": "6",
                },
                {
                    "record_keys": "md:1",
                    "final_relative_path": (
                        "masters/objects/KS-1/data/variants/KS-2.tif"
                    ),
                    "role": "preservation_source_variant",
                    "final_sha256": "sha256:" + "2" * 64,
                    "size_bytes": "7",
                },
            ],
        )
        disposition_fields = (
            "source_path",
            "final_path",
            "source_sha256",
            "source_size_bytes",
            "preservation_status",
            "drain_state",
            "deletion_authorized",
        )
        write_csv(
            unit / "db-restore/manifests/source-disposition.csv",
            disposition_fields,
            [
                {
                    "source_path": "/usb/KS-1.tif",
                    "final_path": "masters/objects/KS-1/data/KS-1.tif",
                    "source_sha256": "sha256:" + "1" * 64,
                    "source_size_bytes": "6",
                    "preservation_status": "preserved_exact_copy",
                    "drain_state": "candidate_after_separate_review",
                    "deletion_authorized": "false",
                },
                {
                    "source_path": "/usb/copy/KS-1.tif",
                    "final_path": "masters/objects/KS-1/data/KS-1.tif",
                    "source_sha256": "sha256:" + "1" * 64,
                    "source_size_bytes": "6",
                    "preservation_status": "preserved_exact_copy",
                    "drain_state": "candidate_after_separate_review",
                    "deletion_authorized": "false",
                },
            ],
        )
        return unit, work

    def add_alias_object(self, unit: Path) -> None:
        catalog_path = unit / "CATALOG.csv"
        catalog_rows = MODULE.read_csv(catalog_path)
        alias = dict(catalog_rows[0])
        alias.update(
            {
                "record_key": "md:2",
                "md_object_id": "2",
                "current_inventory": "KS-ALIAS",
                "historical_inventory": "KS-ALIAS-OLD",
                "current_title": "Alias museum title",
                "historical_title": "Alias local title",
                "selected_source_path": "/source/KS-ALIAS.tif",
                "object_directory": "KS-ALIAS",
            }
        )
        write_csv(
            catalog_path,
            tuple(catalog_rows[0]),
            [*catalog_rows, alias],
        )

        master = (
            unit / "masters/objects/KS-ALIAS/data/KS-ALIAS.tif"
        )
        variant = (
            unit
            / "masters/objects/KS-ALIAS/data/variants/KS-ALIAS-variant.tif"
        )
        master.parent.mkdir(parents=True, exist_ok=True)
        variant.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"alias-master")
        variant.write_bytes(b"alias-variant")
        representations_path = (
            unit / "db-restore/manifests/representations.csv"
        )
        representation_rows = MODULE.read_csv(representations_path)
        fields = tuple(representation_rows[0])
        write_csv(
            representations_path,
            fields,
            [
                *representation_rows,
                {
                    "record_keys": "md:2",
                    "final_relative_path": (
                        "masters/objects/KS-ALIAS/data/KS-ALIAS.tif"
                    ),
                    "role": "preservation_master",
                    "final_sha256": "sha256:" + "3" * 64,
                    "size_bytes": str(master.stat().st_size),
                },
                {
                    "record_keys": "md:2",
                    "final_relative_path": (
                        "masters/objects/KS-ALIAS/data/variants/"
                        "KS-ALIAS-variant.tif"
                    ),
                    "role": "preservation_source_variant",
                    "final_sha256": "sha256:" + "4" * 64,
                    "size_bytes": str(variant.stat().st_size),
                },
            ],
        )

    def test_projection_groups_occurrences_and_normalizes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            record = review.record("md:1")
            self.assertEqual(2, len(record["candidates"]))
            master = next(
                item
                for item in record["candidates"]
                if item["content_role"] == "preservation_master"
            )
            self.assertEqual(2, len(master["source_occurrences"]))
            fields = {
                item["field_key"]: item for item in record["metadata"]
            }
            self.assertEqual("same", fields["tags"]["comparison_state"])
            self.assertEqual("conflict", fields["places"]["comparison_state"])
            self.assertEqual("conflict", fields["title"]["comparison_state"])
            self.assertEqual("same", fields["description"]["comparison_state"])

    def test_master_wall_previews_access_derivative_without_changing_master(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            derivative = unit / "export/access/KS-1.jpg"
            derivative.parent.mkdir(parents=True, exist_ok=True)
            derivative.write_bytes(b"jpeg-derivative")
            path = unit / "db-restore/manifests/representations.csv"
            rows = MODULE.read_csv(path)
            write_csv(
                path,
                tuple(rows[0]),
                [
                    *rows,
                    {
                        "record_keys": "md:1",
                        "final_relative_path": "export/access/KS-1.jpg",
                        "role": "access_derivative",
                        "final_sha256": "sha256:" + "3" * 64,
                        "size_bytes": str(derivative.stat().st_size),
                    },
                ],
            )
            review = MODULE.KurtSchwarzReview(unit, work)
            record = review.records()[0]
            candidates = {
                item["candidate_id"]: item for item in review.record("md:1")[
                    "candidates"
                ]
            }
            self.assertEqual(
                "preservation_master",
                candidates[record["thumbnail_candidate_id"]]["content_role"],
            )
            self.assertEqual(
                "access_derivative",
                candidates[record["thumbnail_preview_candidate_id"]][
                    "content_role"
                ],
            )

    def test_projection_uses_restore_objects_for_representation_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            objects = MODULE.read_csv(unit / "CATALOG.csv")
            restore = unit / "db-restore/RESTORE.sqlite"
            with sqlite3.connect(restore) as connection:
                columns = ", ".join(f'"{key}" TEXT NOT NULL' for key in objects[0])
                connection.execute(f"CREATE TABLE objects ({columns})")
                fields = tuple(objects[0])
                placeholders = ", ".join("?" for _field in fields)
                connection.execute(
                    f"INSERT INTO objects VALUES ({placeholders})",
                    tuple(objects[0][field] for field in fields),
                )
            write_csv(
                unit / "CATALOG.csv",
                ("record_key", "file_role", "final_relative_path"),
                [
                    {
                        "record_key": "md:1",
                        "file_role": "preservation_master",
                        "final_relative_path": "masters/KS-1.tif",
                    },
                    {
                        "record_key": "md:1",
                        "file_role": "access_derivative",
                        "final_relative_path": "export/access/KS-1.jpg",
                    },
                ],
            )

            review = MODULE.KurtSchwarzReview(unit, work)

            self.assertEqual("KS-1", review.record("md:1")["current_inventory"])

    def test_document_review_import_is_bound_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            representations = unit / "db-restore/manifests/representations.csv"
            rows = MODULE.read_csv(representations)
            fields = tuple(rows[0])
            review_rows = []
            proposed = [
                "package_historical",
                "retain_contextual",
                "cross_unit_reference",
            ]
            for index in range(484):
                relative = f"documents/review/document-{index:03d}.pdf"
                payload = f"document-{index}".encode()
                path = unit / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                digest = "sha256:" + f"{index:064x}"[-64:]
                rows.append(
                    {
                        "record_keys": "",
                        "final_relative_path": relative,
                        "role": "project_document",
                        "final_sha256": digest,
                        "size_bytes": str(len(payload)),
                    }
                )
                review_rows.append(
                    {
                        "final_path": relative,
                        "filename": path.name,
                        "extension": ".pdf",
                        "size_bytes": str(len(payload)),
                        "sha256": digest,
                        "source_path": f"/source/{path.name}",
                        "content_family": "fixture",
                        "proposed_relationship": "direct",
                        "proposed_disposition": (
                            proposed[index]
                            if index < len(proposed)
                            else "needs_visual_review"
                        ),
                        "text_evidence": "fixture evidence",
                        "review_basis": "fixture review",
                        "access_restriction": "",
                        "deletion_authorized": "false",
                    }
                )
            write_csv(representations, fields, rows)
            review_csv = root / "work/document-review.csv"
            write_csv(review_csv, tuple(review_rows[0]), review_rows)
            review = MODULE.KurtSchwarzReview(unit, work)
            dry = review.import_document_review(
                review_csv, actor="tester", apply=False
            )
            self.assertEqual("dry_run", dry["state"])
            self.assertEqual(3, dry["seed_decisions"])
            applied = review.import_document_review(
                review_csv, actor="tester", apply=True
            )
            self.assertEqual("applied", applied["state"])
            self.assertEqual(481, applied["pending_visual_review"])
            documents = review.documents()
            self.assertEqual(484, len(documents))
            self.assertEqual(
                481,
                sum(row["review_state"] == "pending" for row in documents),
            )
            target = next(
                row for row in documents if row["review_state"] == "pending"
            )
            decision = review.save_document_decision(
                document_id=target["document_id"],
                disposition="keep_direct",
                actor="tester",
                reason="confirmed",
            )
            self.assertEqual("keep_direct", decision["disposition"])
            self.assertEqual(
                480,
                sum(
                    row["review_state"] == "pending"
                    for row in review.documents()
                ),
            )

    def test_document_preview_falls_back_to_manifested_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            relative = "documents/review/source-only.docx"
            source_root = root / "source-archive"
            source = source_root / "WF/Kurt Schwarz/source-only.docx"
            source.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    "<document><p><t>Readable source document</t></p></document>",
                )
            digest = MODULE.sha256_file(source)
            representations = unit / "db-restore/manifests/representations.csv"
            rows = MODULE.read_csv(representations)
            write_csv(
                representations,
                tuple(rows[0]),
                [
                    *rows,
                    {
                        "record_keys": "",
                        "final_relative_path": relative,
                        "role": "project_document",
                        "final_sha256": digest,
                        "size_bytes": str(source.stat().st_size),
                    },
                ],
            )
            row = {
                "final_path": relative,
                "filename": source.name,
                "extension": ".docx",
                "size_bytes": str(source.stat().st_size),
                "sha256": digest,
                "source_path": str(source),
                "content_family": "fixture",
                "proposed_relationship": "direct",
                "proposed_disposition": "needs_visual_review",
                "text_evidence": "fixture evidence",
                "review_basis": "fixture review",
                "access_restriction": "",
                "deletion_authorized": "false",
            }
            review = MODULE.KurtSchwarzReview(unit, work)
            document_id = MODULE.document_candidate_id(relative)
            with closing(review.connect()) as connection:
                connection.execute(
                    """INSERT INTO document_candidates (
                           document_id, final_path, filename, extension,
                           size_bytes, content_sha256, source_path,
                           content_family, proposed_relationship,
                           proposed_disposition, text_evidence, review_basis,
                           access_restriction, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_id, relative, source.name, ".docx",
                        source.stat().st_size, digest, str(source), "fixture",
                        "direct", "needs_visual_review", "fixture evidence",
                        "fixture review", "", json.dumps(row),
                    ),
                )
                connection.commit()
            with mock.patch.object(MODULE, "SOURCE_ARCHIVE_ROOT", source_root):
                self.assertEqual(source, review.document_path(document_id))
                preview, content_type = review.document_preview(document_id)
            self.assertEqual("text/html; charset=utf-8", content_type)
            self.assertIn(
                "Readable source document",
                preview.read_text(encoding="utf-8"),
            )

    def test_object_census_path_uses_bounded_source_without_full_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            review = MODULE.KurtSchwarzReview(unit, work)
            source_root = root / "source-archive"
            kurt_root = source_root / "WF/Kurt Schwarz"
            source = kurt_root / "source-only.tif"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"source-only")
            with closing(review.connect()) as connection:
                connection.execute(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256,
                           size_bytes, filename, extension, width_px, height_px,
                           final_path, primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "KSC-SOURCE-ONLY", "master_without_metadata",
                        MODULE.sha256_file(source), source.stat().st_size,
                        source.name, ".tif", None, None,
                        "masters/unassigned/source-only.tif", str(source),
                        json.dumps([str(source)]), "[]", 0, "fixture",
                        "fixture", "{}",
                    ),
                )
                connection.commit()
            with (
                mock.patch.object(MODULE, "SOURCE_ARCHIVE_ROOT", source_root),
                mock.patch.object(MODULE, "SOURCE_KURT_ROOT", kurt_root),
                mock.patch.object(
                    review,
                    "object_census_candidate",
                    side_effect=AssertionError("full queue must not be loaded"),
                ),
            ):
                self.assertEqual(
                    source, review.object_census_path("KSC-SOURCE-ONLY")
                )

    def test_object_census_import_and_decisions_are_bounded_and_append_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            disposition_path = (
                unit / "db-restore/manifests/source-disposition.csv"
            )
            disposition = MODULE.read_csv(disposition_path)
            fields = tuple(disposition[0])
            prefix = (
                "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
            )
            specifications = (
                (
                    "KSC-ONE", "master_with_metadata", "one.tif",
                    "masters/unassigned/one.tif", "1", "", "",
                ),
                (
                    "KSC-TWO", "master_without_metadata", "two.psd",
                    "masters/unassigned/two.psd", "2", "", "",
                ),
                (
                    "KSC-THREE", "hires_jpeg_without_master", "three.jpg",
                    "export/access/unassigned/three.jpg", "3", "3000", "2000",
                ),
            )
            census_rows = []
            for candidate, queue, filename, final_path, digit, width, height in specifications:
                payload = f"fixture-{candidate}".encode()
                stored = unit / final_path
                stored.parent.mkdir(parents=True, exist_ok=True)
                stored.write_bytes(payload)
                digest = "sha256:" + digit * 64
                source = prefix + filename
                disposition.append(
                    {
                        "source_path": source,
                        "final_path": final_path,
                        "source_sha256": digest,
                        "source_size_bytes": str(len(payload)),
                        "preservation_status": "preserved_exact_copy",
                        "drain_state": "candidate_after_separate_review",
                        "deletion_authorized": "false",
                    }
                )
                metadata = (
                    [{"inventory": "KS-FIXTURE", "title": "Fixture title"}]
                    if queue == "master_with_metadata" else []
                )
                census_rows.append(
                    {
                        "candidate_id": candidate,
                        "queue_class": queue,
                        "content_sha256": digest,
                        "size_bytes": str(len(payload)),
                        "filename": filename,
                        "extension": Path(filename).suffix,
                        "width_px": width,
                        "height_px": height,
                        "final_path": final_path,
                        "primary_source_path": source,
                        "source_paths_json": json.dumps([source]),
                        "metadata_matches_json": json.dumps(metadata),
                        "metadata_match_count": str(len(metadata)),
                        "metadata_summary": "Fixture evidence",
                        "review_basis": "Fixture review",
                        "deletion_authorized": "false",
                    }
                )
            write_csv(disposition_path, fields, disposition)
            census_csv = root / "work/object-census.csv"
            write_csv(census_csv, tuple(census_rows[0]), census_rows)
            review = MODULE.KurtSchwarzReview(unit, work)
            dry = review.import_object_census(
                census_csv, actor="tester", apply=False
            )
            self.assertEqual("dry_run", dry["state"])
            self.assertEqual(3, dry["candidates"])
            applied = review.import_object_census(
                census_csv, actor="tester", apply=True
            )
            self.assertEqual("applied", applied["state"])
            self.assertEqual(
                {
                    "master_with_metadata",
                    "master_without_metadata",
                    "hires_jpeg_without_master",
                },
                {row["queue_class"] for row in review.object_census_candidates()},
            )
            result = review.save_object_census_decision(
                candidate_id="KSC-ONE",
                disposition="existing_object",
                target_reference="KS-1",
                actor="tester",
                reason="same depicted object",
            )
            self.assertEqual("md:1", result["target_record_key"])
            review.save_object_census_decision(
                candidate_id="KSC-ONE",
                disposition="new_local_object",
                actor="tester",
                reason="revised after review",
            )
            candidate = review.object_census_candidate("KSC-ONE")
            self.assertEqual(
                "new_local_object", candidate["decision"]["disposition"]
            )
            with closing(review.connect(read_only=True)) as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) FROM object_census_decisions"
                    ).fetchone()[0],
                )
            reconciled_rows = []
            for row in census_rows:
                existing = row["candidate_id"] == "KSC-ONE"
                companion = row["candidate_id"] == "KSC-TWO"
                reconciled_rows.append(
                    {
                        **row,
                        "queue_class": (
                            "existing_object_reconciled"
                            if existing else "new_local_review"
                        ),
                        "target_record_key": "md:1" if existing else "",
                        "target_inventory": "KS-1" if existing else "",
                        "target_media_candidate_id": "",
                        "related_candidate_id": (
                            "KSC-THREE" if companion else ""
                        ),
                        "selected_metadata_json": json.dumps(
                            {"inventory": "KS-1" if existing else row["filename"]}
                        ),
                        "selected_metadata_basis": (
                            "existing_kurt_effective_metadata"
                            if existing else "filename_only"
                        ),
                        "metadata_enrichment_json": "{}",
                        "visual_match_json": json.dumps(
                            {"bounded_match_count": 1 if existing else 0}
                        ),
                        "auto_decision_reason": (
                            "Unique bounded fixture visual match" if existing else ""
                        ),
                    }
                )
                if companion:
                    reconciled_rows[-1]["queue_class"] = (
                        "new_local_companion_reconciled"
                    )
                    reconciled_rows[-1]["auto_decision_reason"] = (
                        "Exact fixture preview match"
                    )
            reconciled_csv = root / "work/object-census-reconciled.csv"
            write_csv(
                reconciled_csv,
                tuple(reconciled_rows[0]),
                reconciled_rows,
            )
            refresh = review.import_object_census(
                reconciled_csv, actor="tester", apply=False, refresh=True
            )
            self.assertEqual(1, refresh["automatic_existing_decisions"])
            self.assertEqual(1, refresh["automatic_companion_decisions"])
            self.assertEqual(1, refresh["superseded_census_decisions"])
            applied_refresh = review.import_object_census(
                reconciled_csv, actor="tester", apply=True, refresh=True
            )
            self.assertTrue(Path(applied_refresh["rollback_database"]).is_file())
            refreshed = review.object_census_candidate("KSC-ONE")
            self.assertEqual("existing_object_reconciled", refreshed["queue_class"])
            self.assertEqual("existing_object", refreshed["decision"]["disposition"])
            self.assertEqual("md:1", refreshed["decision"]["target_record_key"])
            companion = review.object_census_candidate("KSC-TWO")
            self.assertEqual(
                "same_new_candidate", companion["decision"]["disposition"]
            )
            described = review.save_object_census_decision(
                candidate_id="KSC-THREE",
                disposition="new_local_metadata_right",
                actor="tester",
                reason="title and date accepted",
                curated_title="  One depicted object   ",
                curated_date_text="  vermutlich 1970 bis 1973  ",
                curated_description=(
                    "  One depicted object only; the physical group lists "
                    "several others.  "
                ),
                curated_tags="Everyday; River; everyday; Boat\nLeisure",
            )
            self.assertEqual(
                "One depicted object",
                described["curated_title"],
            )
            self.assertEqual(
                "vermutlich 1970 bis 1973",
                described["curated_date_text"],
            )
            self.assertEqual(
                "Everyday; River; Boat; Leisure",
                described["curated_tags"],
            )
            self.assertEqual(
                "One depicted object only; the physical group lists several others.",
                described["curated_description"],
            )
            described_candidate = review.object_census_candidate("KSC-THREE")
            self.assertEqual(
                described["curated_title"],
                described_candidate["decision"]["curated_title"],
            )
            self.assertEqual(
                described["curated_description"],
                described_candidate["decision"]["curated_description"],
            )
            self.assertEqual(
                described["curated_date_text"],
                described_candidate["decision"]["curated_date_text"],
            )
            self.assertEqual(
                described["curated_tags"],
                described_candidate["decision"]["curated_tags"],
            )
            queue_candidate = next(
                row
                for row in review.object_census_queue_candidates()
                if row["candidate_id"] == "KSC-THREE"
            )
            self.assertEqual("decided", queue_candidate["review_state"])
            self.assertEqual(
                " · ".join(
                    (
                        "One depicted object",
                        "vermutlich 1970 bis 1973",
                        "One depicted object only; the physical group lists "
                        "several others.",
                        "Everyday; River; Boat; Leisure",
                    )
                ),
                queue_candidate["metadata_summary"],
            )
            ambiguous_rows = []
            for row in reconciled_rows:
                ambiguous = row["candidate_id"] == "KSC-ONE"
                ambiguous_rows.append(
                    {
                        **row,
                        "queue_class": (
                            "new_local_review" if ambiguous else row["queue_class"]
                        ),
                        "target_record_key": "" if ambiguous else row["target_record_key"],
                        "target_inventory": "" if ambiguous else row["target_inventory"],
                        "selected_metadata_json": "{}" if ambiguous else row["selected_metadata_json"],
                        "selected_metadata_basis": (
                            "ambiguous_existing_matches"
                            if ambiguous else row["selected_metadata_basis"]
                        ),
                        "visual_match_json": (
                            json.dumps({"bounded_match_count": 2})
                            if ambiguous else row["visual_match_json"]
                        ),
                        "auto_decision_reason": "" if ambiguous else row["auto_decision_reason"],
                    }
                )
            ambiguous_csv = root / "work/object-census-ambiguous.csv"
            write_csv(ambiguous_csv, tuple(ambiguous_rows[0]), ambiguous_rows)
            ambiguous_dry = review.import_object_census(
                ambiguous_csv, actor="tester", apply=False, refresh=True
            )
            self.assertEqual(1, ambiguous_dry["automatic_ambiguous_reopens"])
            review.import_object_census(
                ambiguous_csv, actor="tester", apply=True, refresh=True
            )
            reopened = review.object_census_candidate("KSC-ONE")
            self.assertEqual("new_local_review", reopened["queue_class"])
            self.assertEqual("defer", reopened["decision"]["disposition"])
            self.assertEqual("pending", reopened["review_state"])

    def test_similarity_review_groups_and_reopens_companion_append_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            review = MODULE.KurtSchwarzReview(unit, work)
            candidates = []
            for index, candidate_id in enumerate(("KSC-A", "KSC-B"), 1):
                review_payload = {
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "related_candidate_id": "",
                    "selected_metadata_basis": "filename_only",
                    "auto_decision_reason": "",
                    "selected_metadata_json": "{}",
                    "metadata_enrichment_json": "{}",
                    "visual_match_json": "{}",
                }
                candidates.append(
                    (
                        candidate_id,
                        "new_local_review",
                        f"sha256:{index * 64:064d}"[-71:],
                        index,
                        f"candidate-{index}.tif",
                        ".tif",
                        None,
                        None,
                        f"masters/unassigned/candidate-{index}.tif",
                        f"/source/candidate-{index}.tif",
                        json.dumps([f"/source/candidate-{index}.tif"]),
                        "[]",
                        0,
                        "Fixture candidate",
                        "Similarity fixture",
                        json.dumps(review_payload),
                    )
                )
            with closing(review.connect()) as connection:
                connection.executemany(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256, size_bytes,
                           filename, extension, width_px, height_px, final_path,
                           primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    candidates,
                )
                connection.commit()
            pair = "KSPAIR-FIXTURE"
            run_fingerprint = "sha256:" + "9" * 64
            run = work / "object-census-similarity/runs/fixture"
            run.mkdir(parents=True)
            (run / "similarity-pairs.json").write_text(
                json.dumps(
                    {
                        "format": "iss-kurt-schwarz-census-similarity-v1",
                        "run_fingerprint": run_fingerprint,
                        "deletion_authorized": False,
                        "pairs": [
                            {
                                "pair_id": pair,
                                "candidate_a_id": "KSC-A",
                                "candidate_b_id": "KSC-B",
                                "confidence": "high_confidence",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            grouped = review.save_object_census_similarity_decision(
                pair_id=pair,
                outcome="same_photo",
                primary_candidate_id="KSC-A",
                run_fingerprint=run_fingerprint,
                actor="tester",
                reason="same photograph after side-by-side review",
            )
            self.assertEqual("KSC-B", grouped["companion_candidate_id"])
            companion = review.object_census_candidate("KSC-B")
            self.assertEqual(
                "new_local_companion_reconciled", companion["queue_class"]
            )
            self.assertEqual("KSC-A", companion["related_candidate_id"])
            self.assertEqual("decided", companion["review_state"])
            corrected = review.save_object_census_similarity_decision(
                pair_id=pair,
                outcome="different_photo",
                run_fingerprint=run_fingerprint,
                actor="tester",
                reason="layout is similar but the depicted photograph differs",
            )
            self.assertEqual("different_photo", corrected["outcome"])
            reopened = review.object_census_candidate("KSC-B")
            self.assertEqual("new_local_review", reopened["queue_class"])
            self.assertEqual("wrong_match", reopened["decision"]["disposition"])
            self.assertEqual("pending", reopened["review_state"])
            with closing(review.connect(read_only=True)) as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) FROM object_census_similarity_decisions"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT COUNT(*) FROM object_census_decisions"
                    ).fetchone()[0],
                )

    def test_manual_duplicate_keeps_retained_candidate_and_drain_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            review = MODULE.KurtSchwarzReview(unit, work)
            review_payload = json.dumps(
                {
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "related_candidate_id": "",
                    "selected_metadata_basis": "filename_only",
                    "auto_decision_reason": "",
                    "selected_metadata_json": "{}",
                    "metadata_enrichment_json": "{}",
                    "visual_match_json": "{}",
                }
            )
            candidates = []
            for index, candidate_id in enumerate(("KSC-MASTER", "KSC-COPY"), 1):
                candidates.append(
                    (
                        candidate_id,
                        "new_local_review",
                        "sha256:" + str(index) * 64,
                        index,
                        f"candidate-{index}.tif",
                        ".tif",
                        None,
                        None,
                        f"masters/unassigned/candidate-{index}.tif",
                        f"/source/candidate-{index}.tif",
                        json.dumps([f"/source/candidate-{index}.tif"]),
                        "[]",
                        0,
                        "Fixture candidate",
                        "Manual duplicate fixture",
                        review_payload,
                    )
                )
            with closing(review.connect()) as connection:
                connection.executemany(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256, size_bytes,
                           filename, extension, width_px, height_px, final_path,
                           primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    candidates,
                )
                connection.commit()
            result = review.save_manual_object_census_duplicate_decision(
                companion_candidate_id="KSC-COPY",
                retained_candidate_id="KSC-MASTER",
                actor="tester",
                reason="The two candidates depict the same photograph.",
            )
            self.assertEqual("KSC-MASTER", result["retained_candidate_id"])
            self.assertEqual("KSC-COPY", result["companion_candidate_id"])
            self.assertFalse(result["deletion_authorized"])
            companion = review.object_census_candidate("KSC-COPY")
            self.assertEqual(
                "new_local_companion_reconciled", companion["queue_class"]
            )
            self.assertEqual("KSC-MASTER", companion["related_candidate_id"])
            self.assertEqual("decided", companion["review_state"])
            retained = review.object_census_candidate("KSC-MASTER")
            self.assertEqual("new_local_review", retained["queue_class"])
            self.assertEqual("pending", retained["review_state"])
            queue = {
                row["candidate_id"]: row
                for row in review.object_census_queue_candidates()
            }
            self.assertEqual("decided", queue["KSC-COPY"]["review_state"])
            self.assertEqual("pending", queue["KSC-MASTER"]["review_state"])
            receipt = review.write_manual_object_census_duplicate_receipt(result)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(
                "iss-kurt-schwarz-census-manual-duplicate-v1",
                payload["format"],
            )
            self.assertEqual("Redundant", payload["disposition"])
            self.assertFalse(payload["drain_list_changed"])
            self.assertTrue(payload["drain_evidence_preserved"])
            self.assertFalse(payload["deletion_authorized"])
            with self.assertRaises(MODULE.KurtSchwarzReconciliationError):
                review.save_manual_object_census_duplicate_decision(
                    companion_candidate_id="KSC-COPY",
                    retained_candidate_id="KSC-MASTER",
                    actor="tester",
                    reason="Repeated relationship.",
                )

    def test_manual_redundant_allows_empty_master_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            review = MODULE.KurtSchwarzReview(unit, work)
            review_payload = json.dumps(
                {
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "related_candidate_id": "",
                    "selected_metadata_basis": "filename_only",
                    "auto_decision_reason": "",
                    "selected_metadata_json": "{}",
                    "metadata_enrichment_json": "{}",
                    "visual_match_json": "{}",
                }
            )
            with closing(review.connect()) as connection:
                connection.execute(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256, size_bytes,
                           filename, extension, width_px, height_px, final_path,
                           primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "KSC-REDUNDANT",
                        "new_local_review",
                        "sha256:" + "9" * 64,
                        1,
                        "redundant.tif",
                        ".tif",
                        None,
                        None,
                        "masters/unassigned/redundant.tif",
                        "/source/redundant.tif",
                        json.dumps(["/source/redundant.tif"]),
                        "[]",
                        0,
                        "Fixture candidate",
                        "Standalone Redundant fixture",
                        review_payload,
                    ),
                )
                connection.commit()
            result = review.save_manual_object_census_duplicate_decision(
                companion_candidate_id="KSC-REDUNDANT",
                retained_candidate_id="",
                actor="tester",
                reason="",
            )
            self.assertEqual("", result["retained_candidate_id"])
            self.assertFalse(result["relationship_recorded"])
            self.assertIsNone(result["similarity_decision_id"])
            candidate = review.object_census_candidate("KSC-REDUNDANT")
            self.assertEqual("redundant", candidate["decision"]["disposition"])
            self.assertEqual("decided", candidate["review_state"])
            self.assertEqual("new_local_review", candidate["queue_class"])
            receipt = review.write_manual_object_census_duplicate_receipt(result)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual("Redundant", payload["disposition"])
            self.assertFalse(payload["relationship_recorded"])
            self.assertTrue(payload["drain_evidence_preserved"])
            self.assertFalse(payload["deletion_authorized"])

    def test_similarity_group_replaces_pair_roles_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit, work = self.fixture(root)
            review = MODULE.KurtSchwarzReview(unit, work)
            review_payload = json.dumps(
                {
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "related_candidate_id": "",
                    "selected_metadata_basis": "filename_only",
                    "auto_decision_reason": "",
                    "selected_metadata_json": "{}",
                    "metadata_enrichment_json": "{}",
                    "visual_match_json": "{}",
                }
            )
            candidates = []
            for index, candidate_id in enumerate(("KSC-A", "KSC-B", "KSC-C"), 1):
                candidates.append(
                    (
                        candidate_id,
                        "new_local_review",
                        "sha256:" + str(index) * 64,
                        index,
                        f"candidate-{index}.tif",
                        ".tif",
                        None,
                        None,
                        f"masters/unassigned/candidate-{index}.tif",
                        f"/source/candidate-{index}.tif",
                        json.dumps([f"/source/candidate-{index}.tif"]),
                        "[]",
                        0,
                        "Fixture candidate",
                        "Similarity fixture",
                        review_payload,
                    )
                )
            with closing(review.connect()) as connection:
                connection.executemany(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256, size_bytes,
                           filename, extension, width_px, height_px, final_path,
                           primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    candidates,
                )
                connection.commit()
            run_fingerprint = "sha256:" + "8" * 64
            group_id = "KSGROUP-FIXTURE"
            run = work / "object-census-similarity/runs/fixture"
            run.mkdir(parents=True)
            pair_specs = (
                ("KSPAIR-AB", "KSC-A", "KSC-B"),
                ("KSPAIR-AC", "KSC-A", "KSC-C"),
                ("KSPAIR-BC", "KSC-B", "KSC-C"),
            )
            (run / "similarity-pairs.json").write_text(
                json.dumps(
                    {
                        "format": "iss-kurt-schwarz-census-similarity-v1",
                        "run_fingerprint": run_fingerprint,
                        "deletion_authorized": False,
                        "pairs": [
                            {
                                "pair_id": pair_id,
                                "group_id": group_id,
                                "candidate_a_id": candidate_a,
                                "candidate_b_id": candidate_b,
                                "confidence": "high_confidence",
                            }
                            for pair_id, candidate_a, candidate_b in pair_specs
                        ],
                    }
                ),
                encoding="utf-8",
            )
            review.save_object_census_similarity_decision(
                pair_id="KSPAIR-AB",
                outcome="same_photo",
                primary_candidate_id="KSC-A",
                run_fingerprint=run_fingerprint,
                actor="tester",
            )
            result = review.save_object_census_similarity_group_decision(
                group_id=group_id,
                master_candidate_id="KSC-C",
                redundant_candidate_ids=["KSC-A"],
                run_fingerprint=run_fingerprint,
                actor="tester",
                reason="C is the retained group Master; A is Redundant.",
            )
            self.assertEqual(["KSPAIR-AB"], result["reopened_pair_ids"])
            self.assertEqual("KSC-C", result["master_candidate_id"])
            self.assertEqual(["KSC-A"], result["redundant_candidate_ids"])
            self.assertEqual(
                "new_local_companion_reconciled",
                review.object_census_candidate("KSC-A")["queue_class"],
            )
            self.assertEqual(
                "KSC-C",
                review.object_census_candidate("KSC-A")["related_candidate_id"],
            )
            self.assertEqual(
                "pending",
                review.object_census_candidate("KSC-B")["review_state"],
            )
            self.assertEqual(
                "wrong_match",
                review.object_census_candidate("KSC-B")["decision"]["disposition"],
            )
            pair_states = {
                pair["pair_id"]: pair["review_state"]
                for pair in review.object_census_similarity_pairs()
            }
            self.assertEqual("superseded", pair_states["KSPAIR-AB"])
            self.assertEqual("decided", pair_states["KSPAIR-AC"])
            self.assertEqual("pending", pair_states["KSPAIR-BC"])
            receipt = review.write_object_census_similarity_group_receipt(result)
            self.assertTrue(receipt.is_file())
            self.assertFalse(json.loads(receipt.read_text())["deletion_authorized"])
            assigned = (
                review.assign_object_census_similarity_group_to_existing_object(
                    group_id=group_id,
                    target_reference="md:1",
                    actor="tester",
                    run_fingerprint=run_fingerprint,
                    reason="The retained set depicts the published object.",
                )
            )
            self.assertEqual(["KSC-A", "KSC-C"], assigned["candidate_ids"])
            self.assertEqual("md:1", assigned["target_record_key"])
            assignment_receipt = (
                review.write_object_census_group_assignment_receipt(assigned)
            )
            self.assertTrue(assignment_receipt.is_file())
            self.assertFalse(
                json.loads(assignment_receipt.read_text())["deletion_authorized"]
            )
            for candidate_id in assigned["candidate_ids"]:
                candidate = review.object_census_candidate(candidate_id)
                self.assertEqual("existing_object", candidate["decision"]["disposition"])
                self.assertEqual("md:1", candidate["decision"]["target_record_key"])
            with closing(review.connect(read_only=True)) as connection:
                latest = review._latest_object_census_similarity_decisions(connection)
                self.assertEqual("defer", latest["KSPAIR-AB"]["outcome"])
                self.assertEqual("same_photo", latest["KSPAIR-AC"]["outcome"])
                self.assertEqual(3, len(connection.execute(
                    "SELECT decision_id FROM object_census_similarity_decisions"
                ).fetchall()))
                self.assertEqual(5, len(connection.execute(
                    "SELECT decision_id FROM object_census_decisions"
                ).fetchall()))

    def test_cross_unit_hold_groups_components_without_accession(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            candidates = []
            for sequence in (1, 2):
                filename = f"IMG_20220701_{sequence:04d}.tif"
                source_path = f"/read-only-usb/WF/SCANS/{filename}"
                candidates.append(
                    (
                        f"KSC-HOLD-{sequence}",
                        "new_local_review",
                        "sha256:" + str(sequence) * 64,
                        sequence * 100,
                        filename,
                        ".tif",
                        4896,
                        6976,
                        f"masters/unassigned/{filename}",
                        source_path,
                        json.dumps([source_path]),
                        "[]",
                        0,
                        "",
                        "cross-unit fixture",
                        "{}",
                    )
                )
            with closing(review.connect()) as connection:
                connection.executemany(
                    """INSERT INTO object_census_candidates (
                           candidate_id, queue_class, content_sha256,
                           size_bytes, filename, extension, width_px, height_px,
                           final_path, primary_source_path, source_paths_json,
                           metadata_matches_json, metadata_match_count,
                           metadata_summary, review_basis, review_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    candidates,
                )
                connection.commit()
            evidence = work / "cross-unit-holds/review-evidence/ocr-0001.txt"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Brigadebuch Fototron", encoding="utf-8")
            receipt = work / "cross-unit-holds/wf-brigadebuch.json"
            parameters = {
                "candidate_ids": ["KSC-HOLD-1", "KSC-HOLD-2"],
                "target_scope": "wf",
                "target_subunit": "unresolved",
                "group_key": "wf-brigadebuch-fototron-img-20220701",
                "provisional_title": "Brigadebuch des Kollektivs Fototron",
                "provisional_date_text": "1971–1983",
                "object_type": "compound_volume",
                "actor": "tester",
                "reason": "WF material encountered during Kurt review",
                "receipt_path": receipt,
                "evidence_files": [evidence],
                "component_relationships": {
                    "KSC-HOLD-2": "capture_instruction_note"
                },
            }
            dry = review.save_object_census_cross_unit_hold(**parameters)
            self.assertEqual("dry_run", dry["state"])
            self.assertEqual(2, dry["component_count"])
            self.assertFalse(dry["deletion_authorized"])
            self.assertFalse(receipt.exists())
            applied = review.save_object_census_cross_unit_hold(
                **parameters, apply=True
            )
            self.assertEqual("applied", applied["state"])
            self.assertTrue(receipt.is_file())
            self.assertTrue(Path(applied["rollback_database"]).is_file())
            self.assertEqual(1, applied["decision_id_last"] - applied["decision_id_first"])
            decided = [
                review.object_census_candidate(candidate_id)
                for candidate_id in parameters["candidate_ids"]
            ]
            self.assertTrue(all(row["review_state"] == "decided" for row in decided))
            hold = json.loads(decided[1]["decision"]["cross_unit_hold_json"])
            self.assertEqual("wf", hold["target_scope"])
            self.assertEqual("unresolved", hold["target_subunit"])
            self.assertEqual(2, hold["component_sequence"])
            self.assertEqual(
                "capture_instruction_note", hold["component_relationship"]
            )
            self.assertFalse(hold["deletion_authorized"])
            with closing(review.connect(read_only=True)) as connection:
                self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_new_master_marks_other_media_redundant_and_metadata_is_logged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            record = review.record("md:1")
            master = record["candidates"][0]
            variant = record["candidates"][1]
            first = review.save_media_decision(
                record_key="md:1",
                candidate_id=master["candidate_id"],
                disposition="selected_master",
                actor="tester",
            )
            self.assertEqual(1, first["auto_redundant_count"])
            first_states = {
                item["candidate_id"]: item["decision"]["disposition"]
                for item in review.record("md:1")["candidates"]
            }
            self.assertEqual("redundant", first_states[variant["candidate_id"]])
            review.save_media_decision(
                record_key="md:1",
                candidate_id=variant["candidate_id"],
                disposition="selected_master",
                actor="tester",
                reason="better source",
            )
            updated = review.record("md:1")
            states = {
                item["candidate_id"]: item["decision"]["disposition"]
                for item in updated["candidates"]
            }
            self.assertEqual("redundant", states[master["candidate_id"]])
            self.assertEqual("selected_master", states[variant["candidate_id"]])
            decision = review.save_metadata_decision(
                record_key="md:1",
                field_key="places",
                action="combine",
                actor="tester",
            )
            self.assertEqual(
                "Berlin (DDR); Berlin-Mitte",
                decision["accepted_value"],
            )

    def test_master_selection_preserves_explicit_wrong_object_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            record = review.record("md:1")
            master = record["candidates"][0]
            wrong = record["candidates"][1]
            result = review.save_media_decisions(
                record_key="md:1",
                decisions=[
                    {
                        "candidate_id": wrong["candidate_id"],
                        "disposition": "wrong_object",
                        "reason": "belongs to another archival object",
                    },
                    {
                        "candidate_id": master["candidate_id"],
                        "disposition": "selected_master",
                        "reason": "",
                    },
                ],
                actor="tester",
            )
            self.assertEqual(0, result["auto_redundant_count"])
            states = {
                item["candidate_id"]: item["decision"]["disposition"]
                for item in review.record("md:1")["candidates"]
            }
            self.assertEqual("selected_master", states[master["candidate_id"]])
            self.assertEqual("wrong_object", states[wrong["candidate_id"]])

    def test_single_candidate_requires_explicit_master_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            representations_path = (
                unit / "db-restore/manifests/representations.csv"
            )
            rows = MODULE.read_csv(representations_path)[:1]
            write_csv(representations_path, tuple(rows[0]), rows)
            review = MODULE.KurtSchwarzReview(unit, work)

            pending = review.records()[0]
            self.assertTrue(pending["media_pending"])
            self.assertEqual("pending", pending["review_state"])

            candidate = review.record("md:1")["candidates"][0]
            review.save_media_decision(
                record_key="md:1",
                candidate_id=candidate["candidate_id"],
                disposition="selected_master",
                actor="tester",
            )
            decided = review.records()[0]
            self.assertFalse(decided["media_pending"])
            self.assertFalse(decided["direct_media_pending"])

    def test_manifested_representation_can_be_linked_as_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            orphan = unit / "masters/unassigned/KS-ORPHAN.tif"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"orphan")
            representations_path = (
                unit / "db-restore/manifests/representations.csv"
            )
            rows = MODULE.read_csv(representations_path)
            rows.append(
                {
                    "record_keys": "",
                    "final_relative_path": "masters/unassigned/KS-ORPHAN.tif",
                    "role": "unassigned_project_image",
                    "final_sha256": "sha256:" + "3" * 64,
                    "size_bytes": str(orphan.stat().st_size),
                }
            )
            write_csv(representations_path, tuple(rows[0]), rows)
            review = MODULE.KurtSchwarzReview(unit, work)

            matches = review.search_representations(
                "KS-ORPHAN",
                target_record_key="md:1",
            )
            self.assertEqual(1, len(matches))
            self.assertFalse(matches[0]["already_bound"])

            linked = review.link_representation_candidate(
                target_record_key="md:1",
                final_path=matches[0]["final_path"],
                actor="tester",
                reason="filename and visual review lead",
            )
            self.assertFalse(linked["unchanged"])
            candidate = next(
                item
                for item in review.record("md:1")["candidates"]
                if item["candidate_id"] == linked["candidate_id"]
            )
            self.assertEqual("unassigned_project_image", candidate["content_role"])
            self.assertEqual("tester", candidate["link"]["actor"])
            self.assertEqual(
                "filename and visual review lead",
                candidate["link"]["reason"],
            )

            repeated = review.link_representation_candidate(
                target_record_key="md:1",
                final_path=matches[0]["final_path"],
                actor="tester",
                reason="same bounded candidate",
            )
            self.assertTrue(repeated["unchanged"])
            with closing(review.connect(read_only=True)) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM media_candidate_links"
                    ).fetchone()[0],
                )

    def test_wrong_candidate_can_be_linked_to_another_object_for_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            self.add_alias_object(unit)
            review = MODULE.KurtSchwarzReview(unit, work)
            source_candidate = next(
                item
                for item in review.record("md:1")["candidates"]
                if item["filename"] == "KS-1.tif"
            )
            review.save_media_decision(
                record_key="md:1",
                candidate_id=source_candidate["candidate_id"],
                disposition="wrong_object",
                actor="tester",
                reason="belongs with KS-ALIAS",
            )

            matches = review.search_representations(
                "KS-1.tif",
                target_record_key="md:2",
            )
            self.assertEqual(1, len(matches))
            self.assertFalse(matches[0]["already_bound"])
            linked = review.link_representation_candidate(
                target_record_key="md:2",
                final_path=matches[0]["final_path"],
                actor="tester",
                reason="Wrong on md:1; visual identity matches md:2",
            )

            target_candidate = next(
                item
                for item in review.record("md:2")["candidates"]
                if item["candidate_id"] == linked["candidate_id"]
            )
            self.assertEqual("md:1", target_candidate["link"]["source_record_keys"])
            original = next(
                item
                for item in review.record("md:1")["candidates"]
                if item["candidate_id"] == source_candidate["candidate_id"]
            )
            self.assertEqual("wrong_object", original["decision"]["disposition"])

    def test_batch_rejects_more_than_one_master(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            candidates = review.record("md:1")["candidates"]
            with self.assertRaisesRegex(
                MODULE.KurtSchwarzReconciliationError,
                "only one authoritative master",
            ):
                review.save_media_decisions(
                    record_key="md:1",
                    decisions=[
                        {
                            "candidate_id": candidate["candidate_id"],
                            "disposition": "selected_master",
                            "reason": "",
                        }
                        for candidate in candidates
                    ],
                    actor="tester",
                )

    def test_record_list_exposes_current_representative_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            record = review.record("md:1")
            master = next(
                item
                for item in record["candidates"]
                if item["content_role"] == "preservation_master"
            )
            variant = next(
                item
                for item in record["candidates"]
                if item["content_role"] == "preservation_source_variant"
            )
            overview = review.records()[0]
            self.assertEqual(
                master["candidate_id"],
                overview["thumbnail_candidate_id"],
            )
            self.assertEqual(
                "preservation_master",
                overview["thumbnail_role"],
            )
            self.assertFalse(overview["md_missing"])
            self.assertEqual(1, overview["thumbnail_duplicate_count"])

            review.save_media_decision(
                record_key="md:1",
                candidate_id=variant["candidate_id"],
                disposition="selected_master",
                actor="tester",
            )
            overview = review.records()[0]
            self.assertEqual(
                variant["candidate_id"],
                overview["thumbnail_candidate_id"],
            )
            self.assertEqual(
                "curator_selected_master",
                overview["thumbnail_role"],
            )

    def test_same_object_alias_is_logged_and_removes_repeat_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            self.add_alias_object(unit)
            review = MODULE.KurtSchwarzReview(unit, work)
            before = {
                row["record_key"]: row for row in review.records()
            }
            self.assertEqual("pending", before["md:2"]["review_state"])

            decision = review.save_alias_decision(
                alias_record_key="md:2",
                canonical_reference="KS-1",
                action="same_object_alias",
                actor="tester",
                reason="curator-confirmed duplicate record",
            )
            self.assertEqual("md:1", decision["canonical_record_key"])
            self.assertFalse(decision["unchanged"])

            repeated = review.save_alias_decision(
                alias_record_key="md:2",
                canonical_reference="md:1",
                action="same_object_alias",
                actor="tester",
                reason="duplicate submission",
            )
            self.assertTrue(repeated["unchanged"])

            after = {
                row["record_key"]: row for row in review.records()
            }
            self.assertEqual("aliased", after["md:2"]["review_state"])
            self.assertFalse(after["md:2"]["media_pending"])
            self.assertEqual(0, after["md:2"]["metadata_pending_count"])
            self.assertEqual("md:1", after["md:2"]["alias_of"])
            self.assertEqual("md:2", after["md:1"]["aliases"][0]["record_key"])
            detail = review.record("md:2")
            self.assertEqual("md:1", detail["canonical"]["record_key"])
            self.assertEqual(1, review.summary()["aliased_objects"])

            with self.assertRaises(MODULE.KurtSchwarzReconciliationError):
                review.save_alias_decision(
                    alias_record_key="md:1",
                    canonical_reference="md:2",
                    action="same_object_alias",
                    actor="tester",
                    reason="would create an alias chain",
                )

            reopened = review.save_alias_decision(
                alias_record_key="md:2",
                canonical_reference="md:2",
                action="independent",
                actor="tester",
                reason="curator reversed the relation",
            )
            self.assertFalse(reopened["unchanged"])
            reopened_state = {
                row["record_key"]: row for row in review.records()
            }
            self.assertEqual("pending", reopened_state["md:2"]["review_state"])

    def test_existing_database_adds_alias_schema_without_reprojection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            review.save_metadata_decision(
                record_key="md:1",
                field_key="title",
                action="use_md",
                actor="tester",
            )
            with closing(review.connect()) as connection:
                connection.execute("DROP TABLE object_alias_decisions")
                connection.commit()

            migrated = MODULE.KurtSchwarzReview(unit, work)
            with closing(migrated.connect(read_only=True)) as connection:
                table = connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='object_alias_decisions'"""
                ).fetchone()
                metadata_count = connection.execute(
                    "SELECT COUNT(*) FROM metadata_decisions"
                ).fetchone()[0]
            self.assertIsNotNone(table)
            self.assertEqual(1, metadata_count)

    def test_routine_metadata_policy_prefers_md_without_repeating_decisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            catalog_path = unit / "CATALOG.csv"
            rows = MODULE.read_csv(catalog_path)
            rows[0]["historical_inventory"] = rows[0]["current_inventory"]
            write_csv(catalog_path, tuple(rows[0]), rows)
            review = MODULE.KurtSchwarzReview(unit, work)
            dry_run = review.auto_resolve_metadata(
                actor="tester",
                apply=False,
            )
            self.assertEqual("dry_run", dry_run["state"])
            self.assertEqual(2, dry_run["decision_count"])
            self.assertEqual({"use_md": 2}, dry_run["action_counts"])
            self.assertEqual(0, dry_run["obvious_record_count"])

            applied = review.auto_resolve_metadata(
                actor="tester",
                apply=True,
            )
            self.assertEqual("applied", applied["state"])
            self.assertEqual(2, applied["decision_count"])
            decisions = {
                field["field_key"]: field["decision"]
                for field in review.record("md:1")["metadata"]
                if field["decision"]
            }
            self.assertEqual("use_md", decisions["title"]["action"])
            self.assertEqual("Museum title", decisions["title"]["accepted_value"])
            self.assertEqual("use_md", decisions["places"]["action"])
            self.assertEqual(
                0,
                review.auto_resolve_metadata(
                    actor="tester",
                    apply=False,
                )["decision_count"],
            )

    def test_display_title_uses_accepted_local_metadata_without_rewriting_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            catalog_path = unit / "CATALOG.csv"
            rows = MODULE.read_csv(catalog_path)
            rows[0]["current_title"] = ""
            write_csv(catalog_path, tuple(rows[0]), rows)
            review = MODULE.KurtSchwarzReview(unit, work)

            review.save_metadata_decision(
                record_key="md:1",
                field_key="title",
                action="use_local",
                actor="tester",
            )

            list_record = review.records()[0]
            detail_record = review.record("md:1")
            self.assertEqual("", list_record["current_title"])
            self.assertEqual("Local title", list_record["display_title"])
            self.assertEqual("Local title", detail_record["display_title"])
            self.assertEqual(
                "Local title",
                detail_record["effective_metadata"]["title"],
            )

    def test_obvious_metadata_mismatch_is_held_for_curator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            catalog_path = unit / "CATALOG.csv"
            rows = MODULE.read_csv(catalog_path)
            rows[0]["historical_inventory"] = rows[0]["current_inventory"]
            rows[0]["current_title"] = "Steam locomotive at a station"
            rows[0]["historical_title"] = "Family swimming at the seaside"
            write_csv(catalog_path, tuple(rows[0]), rows)

            review = MODULE.KurtSchwarzReview(unit, work)
            plan = review.auto_resolve_metadata(
                actor="tester",
                apply=False,
            )
            self.assertEqual(1, plan["obvious_record_count"])
            self.assertEqual(2, plan["held_field_count"])
            self.assertEqual(0, plan["decision_count"])
            self.assertEqual(
                "title_token_overlap_0.00",
                plan["obvious_records"][0]["signals"][0]["reason"],
            )

    def test_obvious_date_gap_requires_five_years(self) -> None:
        self.assertEqual(
            "",
            MODULE.obvious_metadata_mismatch_reason(
                "date_text", "1969", "1968"
            ),
        )
        self.assertEqual(
            "date_gap_10_years",
            MODULE.obvious_metadata_mismatch_reason(
                "date_text", "1973", "Sep 63"
            ),
        )

    def test_projection_refuses_changed_published_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            MODULE.KurtSchwarzReview(unit, work)
            with (unit / "CATALOG.csv").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            with self.assertRaises(MODULE.KurtSchwarzReconciliationError):
                MODULE.KurtSchwarzReview(unit, work)

    def test_preview_forces_jpeg_output_for_tiff_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            candidate_id = review.record("md:1")["candidates"][0]["candidate_id"]

            def create_preview(command, **_kwargs):
                self.assertTrue(command[-1].startswith("jpeg:"))
                Path(command[-1].removeprefix("jpeg:")).write_bytes(
                    b"\xff\xd8\xff\xe0preview"
                )
                return mock.Mock()

            with mock.patch.object(
                MODULE.shutil,
                "which",
                return_value=None,
            ), mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=create_preview,
            ):
                preview = review.preview(candidate_id)

            self.assertEqual(b"\xff\xd8", preview.read_bytes()[:2])
            self.assertEqual(".jpg", preview.suffix)

    def test_preview_falls_back_to_magick_when_vips_rejects_tiff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            candidate_id = review.record("md:1")["candidates"][0]["candidate_id"]
            commands = []

            def create_preview(command, **_kwargs):
                commands.append(command)
                if command[0] == "vipsthumbnail":
                    raise subprocess.CalledProcessError(
                        255,
                        command,
                        stderr=b"invalid TIFF directory",
                    )
                Path(command[-1].removeprefix("jpeg:")).write_bytes(
                    b"\xff\xd8\xff\xe0fallback"
                )
                return mock.Mock()

            with mock.patch.object(
                MODULE.shutil,
                "which",
                return_value="/usr/bin/vipsthumbnail",
            ), mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=create_preview,
            ):
                preview = review.preview(candidate_id)

            self.assertEqual(
                ["vipsthumbnail", "magick"],
                [command[0] for command in commands],
            )
            self.assertEqual(b"\xff\xd8", preview.read_bytes()[:2])

    def test_representative_preview_warmup_is_resumable_and_receipted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, work = self.fixture(Path(temporary))
            review = MODULE.KurtSchwarzReview(unit, work)
            cache = work / "fixture-preview.jpg"

            def create_preview(_candidate_id):
                cache.write_bytes(b"\xff\xd8fixture")
                return cache

            with mock.patch.object(
                review,
                "preview_cache_path",
                return_value=cache,
            ), mock.patch.object(
                review,
                "preview",
                side_effect=create_preview,
            ):
                first = review.warm_representative_previews()
                second = review.warm_representative_previews()

            self.assertEqual("complete", first["state"])
            self.assertEqual(1, first["generated"])
            self.assertEqual(1, second["cached_before"])
            receipt = MODULE.json.loads(
                (work / "preview-warmup.json").read_text(encoding="utf-8")
            )
            self.assertEqual("complete", receipt["state"])
            self.assertEqual(0, receipt["remaining"])


if __name__ == "__main__":
    unittest.main()
