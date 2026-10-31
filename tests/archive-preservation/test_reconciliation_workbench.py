"""Portable decision, recovery and HTTP tests; all evidence is synthetic."""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PATH = Path(__file__).resolve().parents[2] / "tools/archive-preservation/reconciliation_workbench.py"
SPEC = importlib.util.spec_from_file_location("reconciliation_workbench", PATH)
workbench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workbench)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        workbench.create_demo(self.root)
        self.app = workbench.Workbench(self.root, "Test curator")
        self.input = self.root / "reconciliation-input.json"
        self.original_media = {p.name: p.read_bytes() for p in self.root.iterdir() if p.suffix in (".png", ".pdf")}

    def payload(self, oid="demo:image", cid="scan", **extra):
        row = self.app.snapshot(oid)
        return {"object_id": oid, "selected_candidate_id": cid, "metadata_choices": {},
                "note": "Human comparison", "revision": row["revision"], **extra}

    def edit_input(self, fn):
        data = json.loads(self.input.read_text())
        fn(data)
        self.input.write_text(json.dumps(data))

    def test_one_master_and_separate_metadata_survive_restart_with_sources_unchanged(self):
        result = self.app.save(self.payload(metadata_choices={"Title": "local"}))
        self.assertEqual(result["decision"]["selected_candidate_id"], "scan")
        restored = workbench.Workbench(self.root, "Another curator")
        saved = restored.snapshot("demo:image")["decision"]
        self.assertEqual(saved["metadata_values"]["Title"]["value"], "Laboratory staff")
        self.assertFalse(saved["deletion_authorized"])
        rows = list(csv.DictReader(io.StringIO((self.root / workbench.PROJECTION).read_text())))
        image_rows = [r for r in rows if r["object_id"] == "demo:image"]
        self.assertEqual([r["disposition"] for r in image_rows], ["selected_master", "redundant", "redundant"])
        self.assertTrue(all(r["deletion_authorized"] == "false" for r in rows))
        self.assertEqual(self.original_media, {name: (self.root / name).read_bytes() for name in self.original_media})
        # Clearing a choice is explicit and metadata stays separately recorded.
        restored.save(self.payload(cid="", metadata_choices={"Title": "local"}))
        self.assertEqual(restored.snapshot("demo:image")["decision"]["selected_candidate_id"], "")

    def test_stale_tab_and_changed_metadata_evidence_refused_without_overwrite(self):
        stale = self.payload()
        self.app.save(self.payload(note="First tab"))
        before = (self.root / workbench.DECISIONS).read_bytes()
        with self.assertRaisesRegex(workbench.ReviewError, "changed"):
            self.app.save(stale)
        self.assertEqual(before, (self.root / workbench.DECISIONS).read_bytes())
        stale = self.payload()
        self.edit_input(lambda d: d["objects"][0]["metadata"][0]["choices"][0].update(value="New researched caption"))
        with self.assertRaisesRegex(workbench.ReviewError, "changed"):
            self.app.save(stale)

    def test_ineligible_cross_object_incomplete_and_unknown_metadata_refused(self):
        for cid in ("reference", "pdf", "unknown"):
            with self.subTest(cid=cid), self.assertRaises(workbench.ReviewError):
                self.app.save(self.payload(cid=cid))
        with self.assertRaisesRegex(workbench.ReviewError, "metadata"):
            self.app.save(self.payload(metadata_choices={"Title": "invented"}))
        self.edit_input(lambda d: d["objects"][0]["candidates"][0].update(complete=False))
        with self.assertRaisesRegex(workbench.ReviewError, "complete"):
            self.app.save(self.payload())
        self.assertFalse((self.root / workbench.DECISIONS).exists())

    def test_shared_source_identity_is_not_selected_for_two_objects(self):
        self.app.save(self.payload(cid="alternative"))
        with self.assertRaisesRegex(workbench.ReviewError, "another object"):
            self.app.save(self.payload("local:pages", "pages"))

    def test_stored_hash_collision_is_refused_even_at_a_different_path(self):
        copy = self.root / "another-copy.png"
        copy.write_bytes((self.root / "original.png").read_bytes())
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        stamp = copy.stat()
        def prepare(data):
            data["objects"][0]["candidates"][0]["members"][0]["sha256"] = digest
            data["objects"][2]["candidates"][0]["members"] = [{
                "path": str(copy), "size_bytes": stamp.st_size, "mtime_ns": stamp.st_mtime_ns,
                "pages": 1, "sha256": digest}]
        self.edit_input(prepare)
        self.app.save(self.payload())
        with self.assertRaisesRegex(workbench.ReviewError, "another object"):
            self.app.save(self.payload("local:pages", "pages"))

    def test_saved_metadata_cannot_silently_change_when_master_is_unresolved(self):
        self.app.save(self.payload(cid="", metadata_choices={"Title": "local"}))
        self.edit_input(lambda d: d["objects"][0]["metadata"][0]["choices"][1].update(value="Changed value"))
        with self.assertRaisesRegex(workbench.ReviewError, "metadata evidence changed"):
            self.app.snapshot()

    def test_duplicate_saved_decisions_are_refused(self):
        self.app.save(self.payload())
        path = self.root / workbench.DECISIONS
        with path.open("ab") as handle:
            handle.write(path.read_bytes().splitlines()[0] + b"\n")
        with self.assertRaisesRegex(workbench.ReviewError, "duplicate"):
            self.app.snapshot()

    def test_changed_source_stat_and_symlink_cannot_be_saved_or_previewed(self):
        (self.root / "original.png").write_bytes(b"changed")
        with self.assertRaisesRegex(workbench.ReviewError, "changed"):
            self.app.save(self.payload())
        with self.assertRaisesRegex(workbench.ReviewError, "changed"):
            self.app.media("scan", 0, 1, 1200)
        with tempfile.TemporaryDirectory() as outside:
            victim = Path(outside) / "image.png"
            victim.write_bytes(b"unapproved")
            (self.root / "original.png").unlink()
            (self.root / "original.png").symlink_to(victim)
            with self.assertRaisesRegex(workbench.ReviewError, "symlink"):
                self.app.snapshot()

    def test_projection_failure_keeps_readable_authority_and_can_be_rebuilt(self):
        original = workbench.atomic
        def fail_csv(path, payload):
            if path.name == workbench.PROJECTION:
                raise OSError("Simulated interrupted derived write")
            original(path, payload)
        with mock.patch.object(workbench, "atomic", side_effect=fail_csv):
            with self.assertRaisesRegex(workbench.ReviewError, "Choices were saved"):
                self.app.save(self.payload())
        restarted = workbench.Workbench(self.root, "Curator")
        self.assertEqual(restarted.snapshot("demo:image")["decision"]["selected_candidate_id"], "scan")
        self.assertFalse(restarted.snapshot()["projection_agrees"])
        restarted.repair_projection()
        self.assertTrue(restarted.snapshot()["projection_agrees"])

    def test_receipt_or_read_only_input_freezes_decisions(self):
        payload = self.payload()
        (self.root / "accession-receipt.json").write_text("Even malformed receipts freeze edits")
        with self.assertRaisesRegex(workbench.ReviewError, "frozen"):
            self.app.save(payload)
        self.assertTrue(self.app.snapshot()["read_only"])
        self.assertFalse((self.root / workbench.DECISIONS).exists())
        (self.root / "accession-receipt.json").unlink()
        self.edit_input(lambda d: d.update(read_only=True))
        with self.assertRaisesRegex(workbench.ReviewError, "read-only"):
            self.app.repair_projection()

    def test_ordered_set_survives_reload_and_cannot_silently_change(self):
        self.app.save(self.payload("local:pages", "pages"))
        row = self.app.snapshot("local:pages")["decision"]
        self.assertEqual([Path(m["path"]).name for m in row["selected_members"]], ["alternative.png", "reference.png"])
        self.edit_input(lambda d: d["objects"][2]["candidates"][0]["members"].reverse())
        with self.assertRaisesRegex(workbench.ReviewError, "representation evidence changed"):
            self.app.snapshot()

    def test_preview_and_pdf_pages_are_real_images_and_out_of_range_refused(self):
        image, mime = self.app.media("scan", 0, 1, 1200)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(image.startswith(b"\xff\xd8"))
        with self.assertRaisesRegex(workbench.ReviewError, "Page outside"):
            self.app.media("pdf", 0, 3, 1200)
        if not shutil.which("pdftoppm"):
            self.skipTest("PDF rendering needs the documented Poppler executable")
        page, mime = self.app.media("pdf", 0, 2, 1200)
        self.assertEqual(mime, "image/png")
        self.assertTrue(page.startswith(b"\x89PNG"))

    def test_protected_mount_requires_exact_device_target_type_and_ro(self):
        good = {"source": "/dev/sdf1", "target": "/media/vladimir/Elements", "fstype": "ntfs3", "options": "ro,relatime"}
        for change in ({}, {"source": "/dev/sdg1"}, {"target": "/media/vladimir/Elements1"}, {"options": "rw"}):
            result = mock.Mock(returncode=0, stdout=json.dumps({"filesystems": [{**good, **change}]}))
            with mock.patch.object(workbench.subprocess, "run", return_value=result):
                if not change:
                    workbench.elements_guard([workbench.ELEMENTS / "sample.tif"])
                else:
                    with self.assertRaises(workbench.ReviewError):
                        workbench.elements_guard([workbench.ELEMENTS / "sample.tif"])

    def test_input_cannot_use_output_as_a_source_or_duplicate_decision_rows(self):
        self.edit_input(lambda d: d["objects"][0]["candidates"][0]["members"][0].update(path=str(self.root / workbench.PROJECTION)))
        with self.assertRaisesRegex(workbench.ReviewError, "overlaps"):
            self.app.snapshot()

    def test_csv_adapter_preserves_existing_confirmed_choice_without_rewriting_original(self):
        manifest = json.loads(self.input.read_text())
        obj = manifest["objects"][0]
        self.input.unlink()
        (self.root / "objects.csv").write_bytes(workbench.csv_payload(
            [{"object_key": obj["id"], "title": obj["title"]}], ["object_key", "title"]))
        rows = [{"object_key": obj["id"], "candidate_id": c["id"], "eligible": str(c["eligible"]).lower(),
                 "role": c["role"], "members_json": json.dumps(c["members"])} for c in obj["candidates"]]
        (self.root / "candidates.csv").write_bytes(workbench.csv_payload(rows, list(rows[0])))
        initial = {"object_key": obj["id"], "selected_candidate_id": "scan", "note": "Earlier accepted choice", "deletion_authorized": "false"}
        path = self.root / "master-decisions.csv"
        path.write_bytes(workbench.csv_payload([initial], list(initial)))
        original = path.read_bytes()
        app = workbench.Workbench(self.root, "Test")
        self.assertEqual(app.snapshot(obj["id"])["decision"]["selected_candidate_id"], "scan")
        app.save(self.payload(note="Updated review note"))
        self.assertEqual(path.read_bytes(), original)

    def test_http_origin_token_scope_and_functional_save(self):
        server = workbench.Server(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        def get(path):
            with urlopen(base + path) as response:
                return response.read(), response.headers
        overview = json.loads(get("/api/overview")[0])
        payload = self.payload()
        def post(**headers):
            request = Request(base + "/api/decision", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", **headers})
            with urlopen(request) as response:
                return json.load(response)
        for headers in ({}, {"Origin": "https://other.invalid", "X-Review-Token": overview["token"]}):
            with self.assertRaises(HTTPError) as failure:
                post(**headers)
            self.assertEqual(failure.exception.code, 403)
        saved = post(Origin=base, **{"X-Review-Token": overview["token"]})
        self.assertEqual(saved["decision"]["selected_candidate_id"], "scan")
        with self.assertRaises(HTTPError):
            get("/media?candidate=/etc/passwd")
        html, headers = get("/")
        self.assertIn(b"Archive reconciliation", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
