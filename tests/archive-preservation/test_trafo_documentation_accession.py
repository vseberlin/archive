import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "tools"
    / "archive-preservation"
    / "trafo_documentation_accession.py"
)
SPEC = importlib.util.spec_from_file_location(
    "trafo_documentation_accession", SCRIPT
)
TRAFO = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = TRAFO
SPEC.loader.exec_module(TRAFO)


def test_scope_is_exact_and_shallow():
    TRAFO.validate_specs()
    assert len(TRAFO.DOCUMENTS) == 21
    assert len({item.document_id for item in TRAFO.DOCUMENTS}) == 21
    assert len({item.relative_path for item in TRAFO.DOCUMENTS}) == 21
    assert all(
        item.relative_path.startswith(("documents/", "export/md/"))
        for item in TRAFO.DOCUMENTS
    )


def test_disposition_never_self_authorizes_deletion():
    with patch.object(TRAFO, "source_stat", return_value=(100, 200)):
        rows = TRAFO.document_disposition_rows("planned")
    assert len(rows) == 24
    assert {row["deletion_authorized"] for row in rows} == {"false"}
    assert {row["drain_state"] for row in rows} == {"not_yet_eligible"}


def test_source_loss_is_blocked_not_drain_candidate():
    row = TRAFO.disposition_from_representation(
        "/media/vladimir/Elements/source.tif",
        "TRO-test",
        "source_tiff_page",
        "sha256:source",
        "100",
        [],
    )
    assert row["preservation_status"] == "not_preserved_source_loss"
    assert row["drain_state"] == "retain_not_preserved"
    assert row["deletion_authorized"] == "false"


def test_enriched_master_is_not_claimed_as_exact_copy():
    row = TRAFO.disposition_from_representation(
        "/media/vladimir/Elements/source.tif",
        "TRO-test",
        "source_tiff_page",
        "sha256:source",
        "100",
        [
            {
                "relative_path": "masters/TRO-test/pages/001.tif",
                "content_sha256": "sha256:enriched",
            }
        ],
    )
    assert row["preservation_status"] == "preserved_as_metadata_enriched_master"
    assert row["exact_copy_proven"] == "false"
    assert row["drain_state"] == "candidate_after_separate_review"
    assert row["deletion_authorized"] == "false"
