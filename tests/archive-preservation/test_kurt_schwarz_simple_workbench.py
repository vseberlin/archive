import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2]
    / "tools/archive-preservation/kurt_schwarz_simple_workbench.py"
)
SPEC = importlib.util.spec_from_file_location("simple_workbench", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def item(path: str, digest: str) -> dict:
    return {
        "path": path,
        "relative_path": Path(path).name,
        "sha256": digest,
        "size_bytes": 10,
        "mtime_ns": 1,
        "extension": ".jpg",
    }


def test_frozen_scope_includes_md_recovery_master_outside_usb(tmp_path):
    source = tmp_path / "Elements" / "Kurt Schwarz"
    source.mkdir(parents=True)
    representations = tmp_path / "representations.csv"
    representations.write_text(
        "role,source_path\n"
        f"preservation_master,{source / 'master.tif'}\n"
        "preservation_master,/work/md-image-recovery/md-master.jpg\n"
        f"preservation_source_variant,{source / 'variant.tif'}\n",
        encoding="utf-8",
    )
    assert MODULE.frozen_master_paths(representations, source, 2) == {
        str(source / "master.tif"),
        "/work/md-image-recovery/md-master.jpg",
    }


def test_previous_simple_links_ignore_automatic_projection_rows(tmp_path):
    path = tmp_path / "workbench.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE decisions(path, disposition, master_path, actor)"
    )
    connection.executemany(
        "INSERT INTO decisions VALUES (?, 'redundant', '/source/master.tif', ?)",
        [
            ("/source/proven.jpg", "curator"),
            ("/source/similarity-only.jpg", "workbench"),
        ],
    )
    connection.commit()
    connection.close()
    assert MODULE.simple_decision_links(path) == {
        "/source/proven.jpg": {"/source/master.tif"}
    }


def test_groups_only_unresolved_and_combines_exact_with_czkawka():
    files = [
        item("/source/a.jpg", "sha256:1"),
        item("/source/b.jpg", "sha256:1"),
        item("/source/c.jpg", "sha256:2"),
        item("/source/resolved.jpg", "sha256:2"),
    ]
    legacy = {"/source/resolved.jpg": "selected_master"}
    czkawka = [
        [
            {"path": "/source/b.jpg", "difference": 0},
            {"path": "/source/c.jpg", "difference": 3},
            {"path": "/source/resolved.jpg", "difference": 3},
        ]
    ]
    groups, differences = MODULE.grouped_unresolved(files, legacy, czkawka)
    assert groups == [["/source/a.jpg", "/source/b.jpg", "/source/c.jpg"]]
    assert differences["/source/c.jpg"] == 3


def test_singletons_make_every_unreconciled_image_reviewable():
    files = [
        item("/source/grouped-a.jpg", "sha256:1"),
        item("/source/grouped-b.jpg", "sha256:1"),
        item("/source/single.jpg", "sha256:2"),
        item("/source/resolved.jpg", "sha256:3"),
    ]
    groups = [["/source/grouped-a.jpg", "/source/grouped-b.jpg"]]
    actual = MODULE.include_singleton_groups(
        groups, files, {"/source/resolved.jpg": "selected_master"}
    )
    assert actual == [
        ["/source/grouped-a.jpg", "/source/grouped-b.jpg"],
        ["/source/single.jpg"],
    ]


def test_selected_master_is_added_as_reference_to_unresolved_group():
    groups = [["/source/a.jpg", "/source/b.jpg"]]
    links = {
        "/source/a.jpg": {"/source/master.tif"},
        "/source/b.jpg": set(),
    }
    legacy = {"/source/master.tif": "selected_master"}
    assert MODULE.master_references(
        groups, links, legacy, {"/source/a.jpg", "/source/b.jpg", "/source/master.tif"}
    ) == [["/source/master.tif"]]


def complete_metadata() -> dict:
    return {
        "current_title": "Title",
        "current_description": "Description",
        "current_date_text": "1963",
        "current_material": "Negative",
        "current_dimensions": "35 mm",
        "current_places": "Berlin",
        "current_tags": "Restaurant",
    }


def test_existing_master_automatically_closes_only_directly_linked_candidates():
    rows = MODULE.automatic_redundant_decisions(
        "g",
        ["/source/a.jpg", "/source/b.jpg"],
        ["/source/master.tif"],
        {
            "/source/a.jpg": {"/source/master.tif"},
            "/source/b.jpg": set(),
        },
        {"/source/a.jpg": "noise"},
        "2026-08-06T12:00:00+00:00",
    )
    assert rows == []


def test_automatic_closure_requires_direct_unique_link_and_one_frozen_master():
    assert MODULE.automatic_redundant_decisions(
        "g",
        ["/source/a.jpg"],
        ["/source/master.tif"],
        {"/source/a.jpg": set()},
        {},
        "now",
    ) == []
    assert MODULE.automatic_redundant_decisions(
        "g",
        ["/source/a.jpg"],
        ["/source/master-1.tif", "/source/master-2.tif"],
        {"/source/a.jpg": {"/source/master-1.tif"}},
        {},
        "now",
    ) == []
    rows = MODULE.automatic_redundant_decisions(
        "g",
        ["/source/a.jpg"],
        ["/source/master.tif"],
        {"/source/a.jpg": {"/source/master.tif"}},
        {},
        "now",
    )
    assert len(rows) == 1
    assert rows[0][0] == "/source/a.jpg"
    assert rows[0][2] == "redundant"
    assert rows[0][3] == "/source/master.tif"


def database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "workbench.sqlite"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE files (
          path TEXT PRIMARY KEY, relative_path TEXT, sha256 TEXT,
          size_bytes INTEGER, mtime_ns INTEGER, extension TEXT,
          width_px INTEGER, height_px INTEGER, legacy_state TEXT, metadata_json TEXT
        );
        CREATE TABLE groups (
          group_id TEXT, member_path TEXT, difference INTEGER,
          is_master_reference INTEGER, evidence TEXT,
          PRIMARY KEY(group_id,member_path)
        );
        CREATE TABLE decisions (
          path TEXT PRIMARY KEY, group_id TEXT, disposition TEXT,
          master_path TEXT, note TEXT, actor TEXT, decided_at TEXT
        );
        CREATE TABLE metadata_edits (
          master_path TEXT, field_name TEXT, value TEXT, group_id TEXT,
          actor TEXT, decided_at TEXT, PRIMARY KEY(master_path, field_name)
        );
        """
    )
    for name in ("a", "b", "c"):
        path_value = f"/source/{name}.jpg"
        connection.execute(
            "INSERT INTO files VALUES (?, ?, ?, 10, 1, '.jpg', 100, 100, 'pending', '{}')",
            (path_value, f"{name}.jpg", f"sha256:{name}"),
        )
        connection.execute(
            "INSERT INTO groups VALUES ('g', ?, 0, 0, 'Czkawka')", (path_value,)
        )
    connection.commit()
    return connection


def test_closeout_rejects_new_master_selection(tmp_path):
    connection = database(tmp_path)
    try:
        MODULE.save_decision(
            connection,
            tmp_path,
            "g",
            "/source/a.jpg",
            {"/source/c.jpg"},
            "reviewed",
            "curator",
        )
    except MODULE.WorkbenchError as error:
        assert str(error) == "only a frozen existing Master may be confirmed"
    else:
        raise AssertionError("new Master selection was accepted")
    assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_noise_only_leaves_other_images_unresolved(tmp_path):
    connection = database(tmp_path)
    MODULE.save_decision(
        connection,
        tmp_path,
        "g",
        None,
        {"/source/c.jpg"},
        "not an archival image",
        "curator",
    )
    assert dict(connection.execute("SELECT path, disposition FROM decisions")) == {
        "/source/c.jpg": "noise"
    }
    assert MODULE.pending_group_ids(connection) == []


def test_md_closeout_queue_requires_frozen_master_reference(tmp_path):
    connection = database(tmp_path)
    master = "/source/master.tif"
    connection.execute(
        "INSERT INTO files VALUES (?, 'master.tif', 'sha256:m', 100, 1, '.tif', 500, 500, 'selected_master', '{}')",
        (master,),
    )
    connection.execute(
        "INSERT INTO groups VALUES ('g', ?, 0, 1, 'previous decision')", (master,)
    )
    connection.commit()

    assert MODULE.pending_group_ids(connection) == ["g"]
    assert len(MODULE.browse_rows(connection)) == 3


def test_cross_unit_is_paused_for_md_closeout(tmp_path):
    connection = database(tmp_path)
    try:
        MODULE.save_single_classification(
            connection,
            tmp_path,
            "g",
            "/source/a.jpg",
            "cross_unit",
            "curator",
        )
    except MODULE.WorkbenchError as error:
        assert str(error) == "invalid browse decision"
    else:
        raise AssertionError("cross-unit decision was accepted")


def test_deficiency_classification_flags_only_high_resolution_residuals():
    base = {
        "has_master": 1,
        "metadata_json": "{}",
        "extension": ".jpg",
    }
    assert MODULE.deficiency_keys(
        {**base, "width_px": 674, "height_px": 960}
    ) == []
    assert MODULE.deficiency_keys(
        {**base, "width_px": 3000, "height_px": 2000}
    ) == ["high_resolution_residual"]
    assert MODULE.deficiency_keys(
        {
            **base,
            "extension": ".tif",
            "width_px": 5000,
            "height_px": 5000,
        }
    ) == ["high_resolution_residual"]


def test_existing_frozen_master_makes_confirmed_candidates_redundant(
    tmp_path,
):
    connection = database(tmp_path)
    master = "/source/master.tif"
    connection.execute(
        "INSERT INTO files VALUES (?, 'master.tif', 'sha256:m', 100, 1, '.tif', 500, 500, 'selected_master', ?)",
        (master, '{"current_title":"Existing title"}'),
    )
    connection.execute(
        "INSERT INTO groups VALUES ('g', ?, 0, 1, 'previous decision')", (master,)
    )
    connection.commit()

    MODULE.save_decision(
        connection,
        tmp_path,
        "g",
        master,
        {"/source/c.jpg"},
        "reviewed",
        "curator",
    )

    actual = dict(connection.execute("SELECT path, disposition FROM decisions"))
    assert actual == {
        "/source/a.jpg": "redundant",
        "/source/b.jpg": "redundant",
        "/source/c.jpg": "noise",
    }
    assert connection.execute("SELECT COUNT(*) FROM metadata_edits").fetchone()[0] == 0


def test_indexed_metadata_cannot_be_overwritten(tmp_path):
    connection = database(tmp_path)
    master = "/source/existing-master.tif"
    connection.execute(
        "INSERT INTO files VALUES (?, 'existing-master.tif', 'sha256:m', 100, 1, '.tif', 500, 500, 'selected_master', ?)",
        (master, '{"current_title":"Indexed title"}'),
    )
    connection.execute(
        "INSERT INTO groups VALUES ('g', ?, 22, 2, 'crop-aware suggestion')",
        (master,),
    )
    connection.commit()
    try:
        MODULE.save_decision(
            connection,
            tmp_path,
            "g",
            master,
            set(),
            "",
            "curator",
            {"title": "Replacement"},
        )
    except MODULE.WorkbenchError as error:
        assert str(error) == "metadata is frozen for this MD closeout"
    else:
        raise AssertionError("metadata overwrite was accepted")


def test_suggested_existing_master_requires_confirmation_then_marks_redundant(
    tmp_path,
):
    connection = database(tmp_path)
    master = "/source/existing-master.tif"
    connection.execute(
        "INSERT INTO files VALUES (?, 'existing-master.tif', 'sha256:m', 100, 1, '.tif', 500, 500, 'selected_master', '{}')",
        (master,),
    )
    connection.execute(
        "INSERT INTO groups VALUES ('g', ?, 22, 2, 'crop-aware suggestion')",
        (master,),
    )
    connection.commit()
    assert MODULE.pending_group_ids(connection) == []
    assert MODULE.browse_rows(connection) == []
    MODULE.save_decision(
        connection,
        tmp_path,
        "g",
        master,
        set(),
        "same photograph",
        "curator",
    )
    assert dict(connection.execute("SELECT path, disposition FROM decisions")) == {
        "/source/a.jpg": "redundant",
        "/source/b.jpg": "redundant",
        "/source/c.jpg": "redundant",
    }


def test_filename_tokens_bridge_reinhard_to_reinhardtstrasse():
    left = MODULE.search_tokens(
        "01-03_Berlin_Mitte_1967_M_ Reinhard_Str_Ausschnitt.jpg"
    )
    right = MODULE.search_tokens(
        "Pause in der Reinhardtstraße, Berlin-Mitte, 1967"
    )
    assert MODULE.matching_tokens(left, right) == {"1967", "reinhard"}


def test_list_view_parser_preserves_metadata_and_legacy_note(tmp_path):
    source = tmp_path / "List View.html"
    source.write_text(
        """<table><tr><th></th><th></th><th>Dateiname</th></tr>
        <tr><td>7</td><td><img src='preview.jpg'></td>
        <td>KS-1.tif</td><td>123</td><td>A title</td>
        <td>A caption /// Taschenbeschriftung: Box 2</td>
        <td>Tag A, Tag B</td><td>6x6 cm</td><td>Berlin</td><td>1968</td></tr></table>""",
        encoding="utf-8",
    )
    rows = MODULE.read_legacy_list_view(source)
    assert rows == [
        {
            "row_number": "7",
            "preview": "",
            "filename": "KS-1.tif",
            "md_object_id": "123",
            "title": "A title",
            "description": "A caption /// Taschenbeschriftung: Box 2",
            "tags": "Tag A, Tag B",
            "source_context": "6x6 cm",
            "places": "Berlin",
            "date_text": "1968",
        }
    ]
    assert MODULE.split_legacy_description(rows[0]["description"]) == (
        "A caption",
        "Taschenbeschriftung: Box 2",
    )


def test_metadata_discrepancies_reconcile_description_and_source_note_only():
    legacy = [
        {
            "row_number": "1",
            "filename": "KS-1.tif",
            "md_object_id": "999",
            "title": "Legacy title that must stay out of this pass",
            "description": "Same description /// Originaldateiname: scan.tif",
            "tags": "Tag",
            "source_context": "6x6 cm",
            "places": "Berlin",
            "date_text": "1968",
        }
    ]
    current = [
        {
            "record_key": "md:123",
            "inventory": "KS-1",
            "md_object_id": "123",
            "title": "Current title",
            "description": "Same description",
            "tags": "Tag",
            "places": "Berlin",
            "date_text": "1968",
            "material": "Negative",
            "dimensions": "6x6 cm",
        }
    ]
    rows = MODULE.derive_metadata_discrepancies(legacy, current)
    fields = {(row["kind"], row["field_name"]) for row in rows}
    assert fields == {("legacy_source_note", "description_enrichment")}
    assert all(row["record_key"] == "md:123" for row in rows)
    assert rows[0]["current_value"] == "Same description"
    assert rows[0]["legacy_value"] == "Originaldateiname: scan.tif"


def test_metadata_discrepancy_decisions_are_append_only_and_exported(tmp_path):
    connection = sqlite3.connect(tmp_path / "workbench.sqlite")
    connection.row_factory = sqlite3.Row
    MODULE.ensure_metadata_discrepancy_schema(connection)
    connection.execute(
        "INSERT INTO metadata_discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "KSMD-test",
            "md:123",
            "KS-1",
            "123",
            "field_difference",
            "title",
            "Current",
            "Legacy",
            "KS-1.tif",
            "1",
            "/source/List View.html",
            "Different assertions.",
        ),
    )
    connection.commit()
    MODULE.save_metadata_discrepancy_decision(
        connection, tmp_path, "KSMD-test", "unresolved", "check source", "curator"
    )
    MODULE.save_metadata_discrepancy_decision(
        connection, tmp_path, "KSMD-test", "propose_legacy", "confirmed", "curator"
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM metadata_discrepancy_decisions"
    ).fetchone()[0] == 2
    row = MODULE.metadata_discrepancy_rows(connection)[0]
    assert row["status"] == "reviewed"
    assert row["disposition"] == "propose_legacy"
    exported = (tmp_path / "metadata-discrepancy-decisions.csv").read_text(
        encoding="utf-8"
    )
    assert "legacy_source_path,disposition" in exported
    assert "catalog_modified,deletion_authorized" in exported
    assert exported.count("KSMD-test") == 2


def test_published_access_derivatives_are_selected_for_thumbnails(tmp_path):
    unit = tmp_path / "unit"
    preview = unit / "export/access/KS-1.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpeg")
    catalog = unit / "CATALOG.csv"
    catalog.write_text(
        "record_key,file_role,final_relative_path\n"
        "md:1,preservation_master,masters/KS-1.tif\n"
        "md:1,access_derivative,export/access/KS-1.jpg\n",
        encoding="utf-8",
    )
    assert MODULE.published_access_derivatives(catalog, unit) == {
        "md:1": preview
    }


def test_discrepancy_cell_renders_the_actual_changed_tokens():
    rendered = MODULE.metadata_difference_html(
        {
            "kind": "field_difference",
            "field_name": "date_text",
            "current_value": "1990-1992",
            "legacy_value": "1970-1979",
        }
    )
    assert "Changed value" in rendered
    assert "− Current:</strong> 1990-1992" in rendered
    assert "+ Legacy:</strong> 1970-1979" in rendered


def test_source_note_enrichment_starts_with_saved_description_only():
    assert MODULE.initial_description_enrichment(
        "Saved description."
    ) == "Saved description."


def test_reviewed_enrichment_saves_the_exact_proposed_description(tmp_path):
    connection = sqlite3.connect(tmp_path / "workbench.sqlite")
    connection.row_factory = sqlite3.Row
    MODULE.ensure_metadata_discrepancy_schema(connection)
    connection.execute(
        "INSERT INTO metadata_discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "KSMD-enrich",
            "md:123",
            "KS-1",
            "123",
            "legacy_source_note",
            "description_enrichment",
            "Saved description.",
            "Originaldateiname: scan.tif",
            "KS-1.tif",
            "1",
            "/source/List View.html",
            "Optional enrichment.",
        ),
    )
    connection.commit()
    MODULE.save_metadata_discrepancy_decision(
        connection,
        tmp_path,
        "KSMD-enrich",
        "propose_legacy",
        "reviewed",
        "curator",
        "Saved description. /// Source: scan.tif",
    )
    assert connection.execute(
        "SELECT chosen_value FROM metadata_discrepancy_decisions"
    ).fetchone()[0] == "Saved description. /// Source: scan.tif"


def test_unchanged_description_must_be_recorded_as_retain_current(tmp_path):
    connection = sqlite3.connect(tmp_path / "workbench.sqlite")
    connection.row_factory = sqlite3.Row
    MODULE.ensure_metadata_discrepancy_schema(connection)
    connection.execute(
        "INSERT INTO metadata_discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "KSMD-no-change",
            "md:123",
            "KS-1",
            "123",
            "legacy_source_note",
            "description_enrichment",
            "Saved description.",
            "Container note",
            "KS-1.tif",
            "1",
            "/source/List View.html",
            "Optional enrichment.",
        ),
    )
    connection.commit()
    try:
        MODULE.save_metadata_discrepancy_decision(
            connection,
            tmp_path,
            "KSMD-no-change",
            "propose_legacy",
            "",
            "curator",
            "Saved description.",
        )
    except MODULE.WorkbenchError as error:
        assert str(error) == "description was not enriched; retain current instead"
    else:
        raise AssertionError("unchanged description was accepted as enrichment")
