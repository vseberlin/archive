"""Synthetic contract/root tests; no live archive roots or database writers."""
from dataclasses import replace
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]

from archive_research import Artifact, Page, Recipe, Region, Result, Root, Roots, Source, Transform
from archive_research.visual import preview


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.input = self.base / 'input'
        self.output = self.base / 'output'
        self.input.mkdir()
        self.output.mkdir()
        (self.output / 'previews').mkdir()
        self.path = self.input / 'scan.tif'
        self.path.write_bytes(b'synthetic source')
        stat = self.path.stat()
        self.source = Source('existing-unit', 'existing-unit#record:7', 'chosen-representation',
                             'archive', 'scan.tif', stat.st_size, stat.st_mtime_ns,
                             hashlib.sha256(self.path.read_bytes()).hexdigest(),
                             '/historical/source/scan.tif')
        self.page = Page(self.source, 3, 'tiff', 0, 1600, 800, 8, 'VII')
        self.roots = Roots({'archive': Root(self.input, 'read'), 'artifacts': Root(self.output, 'work')})
        self.recipe = Recipe.create('synthetic-ocr', '1', language='deu', dpi=300,
                                    coverage={'frame': 0}, dependencies={'tesseract': 'fixture'})


class ResearchTests(Fixture):
    def test_explicit_toml_and_no_cwd_or_home_defaults(self):
        config = self.base / 'roots.toml'
        config.write_text(f'[roots.archive]\npath = "{self.input}"\naccess = "read"\n'
                          f'[roots.artifacts]\npath = "{self.output}"\naccess = "work"\n')
        roots = Roots.from_toml(config)
        self.assertEqual(roots.source(self.source), self.path)
        with self.assertRaisesRegex(ValueError, 'Undeclared'):
            roots.source(replace(self.source, root='unconfigured'))
        config.write_text('fallback = "home"\n')
        with self.assertRaises(ValueError):
            Roots.from_toml(config)

    def test_missing_work_root_not_created(self):
        missing = self.base / 'missing'
        roots = Roots({'artifacts': Root(missing, 'work')})
        with self.assertRaisesRegex(ValueError, 'missing'):
            roots.root('artifacts', 'work')
        self.assertFalse(missing.exists())

    def test_mount_directory_exists_but_volume_is_absent(self):
        root = Root(self.output, 'work', mount_target=str(self.base))
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'filesystems': [
            {'target': '/', 'source': '/dev/system', 'fstype': 'ext4', 'options': 'rw'}]}))
        with patch('archive_research.sources.subprocess.run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'mount is absent'):
                root.checked()

    def test_volume_paths_require_mount_constraint(self):
        with self.assertRaisesRegex(ValueError, 'volume mount'):
            Root('/mnt/volume/iss-archive-work', 'work')

    def test_broad_read_root_cannot_bypass_member_mount_guard(self):
        roots = Roots({'archive': Root('/media/vladimir', 'read')})
        source = replace(self.source, path='Elements/restore/archive/scan.tif')
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'filesystems': [
            {'target': '/', 'source': '/dev/system', 'fstype': 'ext4', 'options': 'rw'}]}))
        with patch('archive_research.sources.Root.checked', return_value=Path('/media/vladimir')), \
                patch('archive_research.sources.subprocess.run', return_value=response), \
                patch('archive_research.sources.Path.resolve', side_effect=AssertionError('guard too late')):
            with self.assertRaises(ValueError):
                roots.source(source)

    def test_elements_mount_checked_before_any_source_stat(self):
        root = Root('/media/vladimir/Elements/restore/archive', 'read')
        for mounted in [
            {'source': '/dev/sdf1', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'rw'},
            {'source': '/dev/other', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'ro'},
            {'source': '/dev/sdf1', 'target': '/', 'fstype': 'ntfs3', 'options': 'ro'},
        ]:
            response = SimpleNamespace(returncode=0, stdout=json.dumps({'filesystems': [mounted]}))
            with self.subTest(mounted=mounted), patch('archive_research.sources.subprocess.run', return_value=response), \
                    patch('archive_research.sources.Path.resolve', side_effect=AssertionError('guard too late')):
                with self.assertRaises(ValueError):
                    root.checked()
        with self.assertRaisesRegex(ValueError, 'Alternate'):
            Root('/media/vladimir/Elements1', 'read')

    def test_output_protected_roots_and_read_write_overlap_refused(self):
        with self.assertRaises(ValueError):
            Root('/mnt/volume/iss-archive/output', 'work', mount_target='/mnt/volume')
        with self.assertRaisesRegex(ValueError, 'overlap'):
            Roots({'archive': Root(self.input, 'read'), 'output': Root(self.input / 'output', 'work')})
        with self.assertRaisesRegex(ValueError, 'Wrong root access'):
            self.roots.root('archive', 'work')

    def test_insufficient_space_fails_before_renderer(self):
        root = Root(self.output, 'work', min_free_bytes=100)
        with patch('archive_research.sources.shutil.disk_usage', return_value=SimpleNamespace(free=99)):
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                root.checked()

    def test_changed_source_rejected(self):
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.roots.source(self.source)

    def test_hash_verification_is_explicit_and_detects_same_stamp_change(self):
        self.assertEqual(self.roots.source(self.source, verify_hash=True), self.path)
        self.path.write_bytes(b'X' * self.source.size_bytes)
        os.utime(self.path, ns=(self.source.mtime_ns, self.source.mtime_ns))
        # A stamp check does not claim to be a new cryptographic verification.
        self.assertEqual(self.roots.source(self.source), self.path)
        with self.assertRaisesRegex(ValueError, 'content differs'):
            self.roots.source(self.source, verify_hash=True)

    def test_symlink_and_relative_escape_refused(self):
        for value in ('../scan.tif', '/scan.tif', 'dir/../scan.tif', './scan.tif', 'dir//scan.tif'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.source, path=value)
        link = self.input / 'link.tif'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'Symlinked'):
            self.roots.source(replace(self.source, path=link.name))
        link_root = self.base / 'linked-root'
        link_root.symlink_to(self.input)
        with self.assertRaises(ValueError):
            Root(link_root, 'read').checked()

    def test_remapping_preserves_identity_provenance_and_key(self):
        relocated = self.base / 'relocated'
        relocated.mkdir()
        shutil.copy2(self.path, relocated / self.path.name)
        roots = Roots({'archive': Root(relocated, 'read')})
        before = self.recipe.cache_key(self.page)
        self.assertEqual(roots.source(self.source, verify_hash=True), relocated / self.path.name)
        self.assertEqual(self.source.object_id, 'existing-unit#record:7')
        self.assertEqual(self.source.original_path, '/historical/source/scan.tif')
        self.assertEqual(before, self.recipe.cache_key(self.page))

    def test_pdf_page_frame_and_logical_order_are_distinct(self):
        self.assertEqual((self.page.member, self.page.index, self.page.logical_page, self.page.printed_label),
                         (3, 0, 8, 'VII'))
        with self.assertRaises(ValueError):
            replace(self.page, kind='pdf', index=0)
        with self.assertRaises(ValueError):
            replace(self.page, member=0)
        with self.assertRaises(ValueError):
            replace(self.page, index=True)
        self.assertNotEqual(self.recipe.cache_key(self.page),
                            self.recipe.cache_key(replace(self.page, kind='pdf', index=1)))

    def test_pdf_native_points_are_explicit_and_distinct_from_pixels(self):
        page = replace(self.page, kind='pdf', index=1, width=595.28, height=841.89, units='pdf-points')
        self.assertEqual(page.units, 'pdf-points')
        with self.assertRaises(ValueError):
            replace(page, kind='tiff')
        with self.assertRaises(ValueError):
            replace(page, units='pixels')
        result = Result(page, self.recipe, 'complete', 0)
        self.assertEqual(Result.from_record(result.record()), result)

    def test_cache_recipe_and_geometry_invalidation(self):
        key = self.recipe.cache_key(self.page)
        variants = [Recipe.create('synthetic-ocr', '2', language='deu'),
                    Recipe.create('synthetic-ocr', '1', language='eng'),
                    Recipe.create('synthetic-ocr', '1', language='deu', dpi=600)]
        for recipe in variants:
            self.assertNotEqual(key, recipe.cache_key(self.page))
        for page in [replace(self.page, index=1), replace(self.page, member=4),
                     replace(self.page, source=replace(self.source, sha256='f'*64))]:
            self.assertNotEqual(key, self.recipe.cache_key(page))
        self.assertNotEqual(key, self.recipe.cache_key(self.page, Region(0, 0, .5, .5)))
        self.assertNotEqual(key, self.recipe.cache_key(self.page, transform=Transform(800, 400, (2, 0, 0, 0, 2, 0))))
        unrelated = Recipe.create('unrelated-embedding', '9', settings='new')
        self.assertNotEqual(key, unrelated.cache_key(self.page))
        self.assertEqual(key, self.recipe.cache_key(self.page))

    def test_cache_key_binds_identity_and_content_not_locator(self):
        hashed = replace(self.page, source=replace(self.source, sha256='a'*64))
        key = self.recipe.cache_key(hashed)
        for source in [replace(hashed.source, mtime_ns=hashed.source.mtime_ns + 1),
                       replace(hashed.source, path='moved/' + hashed.source.path),
                       replace(hashed.source, root='other-root', original_path='/old/place', role='master')]:
            moved = replace(hashed, source=source, printed_label='p. 7')
            self.assertEqual(key, self.recipe.cache_key(moved))
            self.assertTrue(self.recipe.key_matches(moved, None, key))
        unhashed = replace(self.page, source=replace(self.source, sha256=''))
        self.assertNotEqual(self.recipe.cache_key(unhashed),
                            self.recipe.cache_key(replace(unhashed, source=replace(unhashed.source, mtime_ns=1))))
        self.assertNotEqual(key, self.recipe.cache_key(replace(hashed, source=replace(hashed.source, object_id='other'))))
        legacy = self.recipe.cache_key(hashed, version=1)
        self.assertNotEqual(key, legacy)
        self.assertTrue(self.recipe.key_matches(hashed, None, legacy))

    def test_recipe_captures_mutable_settings_and_rejects_nan(self):
        settings = {'languages': ['deu']}
        recipe = Recipe.create('ocr', '1', settings=settings)
        settings['languages'].append('eng')
        self.assertEqual(recipe.record()['parameters']['settings']['languages'], ['deu'])
        with self.assertRaises(ValueError):
            Recipe.create('ocr', '1', dpi=float('nan'))

    def test_region_transform_and_outcome_validation(self):
        for args in [(0, 0, 0, 1), (.9, 0, .2, 1), (float('nan'), 0, 1, 1)]:
            with self.assertRaises(ValueError):
                Region(*args)
        with self.assertRaises(ValueError):
            Transform(10, 10, (0, 0, 0, 0, 0, 0))
        with self.assertRaises(ValueError):
            Result(self.page, self.recipe, 'partial', 1)
        with self.assertRaises(ValueError):
            Result(self.page, self.recipe, 'accepted', 1)

    def test_readable_result_roundtrip_and_no_metadata_adoption(self):
        result = Result(self.page, self.recipe, 'partial', .5,
                        (Artifact('artifacts', 'words.alto.xml', 'ocr-alto'),),
                        region=Region(0, 0, .5, .5),
                        transform=Transform(800, 400, (2, 0, 0, 0, 2, 0), ('scale 0.5',)),
                        limitations=('Second region timed out.',))
        record = json.loads(json.dumps(result.record()))
        self.assertEqual(Result.from_record(record), result)
        self.assertFalse(record['metadata_adopted'])
        record['metadata_adopted'] = True
        with self.assertRaises(ValueError):
            Result.from_record(record)

    def test_core_import_has_no_optional_dependencies_or_io(self):
        import archive_research
        code = '''import sys
sys.path.insert(0, sys.argv[1])
def guard(event, args):
    if event in ('subprocess.Popen', 'os.mkdir', 'sqlite3.connect', 'socket.connect'):
        raise AssertionError(event)
sys.addaudithook(guard)
import archive_research
import archive_research.visual
assert not any(n.split('.')[0] in {'django', 'numpy', 'torch', 'PIL', 'cv2', 'visual_research', 'workbench'} for n in sys.modules)
'''
        package_root = Path(archive_research.__file__).resolve().parent.parent
        process = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(package_root)],
                                 cwd=self.base, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)


class VisualAdapterTests(Fixture):
    # Keep contract tests in ResearchTests only; this class's cases exercise the
    # callback boundary using synthetic files, then one real legacy renderer.
    def row(self):
        return {'id': self.source.object_id, 'path': str(self.path), 'stamp': self.source.stamp,
                'side': 'candidate', 'metadata': {'retained': 'reviewer wording'}}

    def fake_renderer(self, root, row, override):
        target = root / 'previews' / 'fixture.jpg'
        target.write_bytes(b'synthetic preview')
        return {**row, 'preview': 'previews/fixture.jpg', 'preview_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'preview_size': [100, 50], 'recipe': {'orientation': 'unknown EXIF'}, 'status': 'ok'}

    def call(self, renderer=None, **kwargs):
        return preview(self.roots, kwargs.pop('page', self.page), 'artifacts', kwargs.pop('row', self.row()), {},
                       renderer=renderer or self.fake_renderer, version='1', dependencies={'vips': 'fixture'}, **kwargs)

    def test_wrapper_preserves_legacy_result_and_review_fields(self):
        expected = self.fake_renderer(self.output, self.row(), {})
        wrapped = self.call()
        self.assertEqual(wrapped.legacy, expected)
        self.assertEqual(wrapped.legacy['metadata'], {'retained': 'reviewer wording'})
        self.assertIsNone(wrapped.result.transform.to_source)
        self.assertEqual(wrapped.result.page.source.object_id, self.source.object_id)

    def test_wrapper_refuses_changed_source_before_callback(self):
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.call(renderer=lambda *args: self.fail('renderer must not run'))

    def test_wrapper_refuses_source_change_during_callback(self):
        def renderer(*args):
            result = self.fake_renderer(*args)
            self.path.write_bytes(b'changed')
            return result
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.call(renderer)

    def test_wrapper_refuses_unmapped_frame_identity_and_members(self):
        for page in [replace(self.page, index=1), replace(self.page, kind='pdf', index=1)]:
            with self.assertRaises(ValueError):
                self.call(page=page)
        for row in [{**self.row(), 'id': 'different'}, {**self.row(), 'members': ['a', 'b']}]:
            with self.assertRaises(ValueError):
                self.call(row=row)

    def test_output_redirection_and_hardlink_are_refused(self):
        (self.output / 'previews').rmdir()
        (self.output / 'previews').symlink_to(self.input)
        with self.assertRaises(ValueError):
            self.call(renderer=lambda *args: self.fail('renderer must not run'))
        (self.output / 'previews').unlink()
        (self.output / 'previews').mkdir()
        def renderer(root, row, override):
            os.link(self.path, root / 'previews' / 'fixture.jpg')
            return {**row, 'preview': 'previews/fixture.jpg', 'preview_sha256': self.source.sha256,
                    'preview_size': [100, 50], 'recipe': {}, 'status': 'ok'}
        with self.assertRaisesRegex(ValueError, 'Hard-linked'):
            self.call(renderer)

    def test_actual_legacy_preview_output_compatibility(self):
        from PIL import Image
        original_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        sys.path.insert(0, str(REPOSITORY / 'Archive-tools'))
        from visual_research import VERSION, make_preview
        Image.new('RGB', (1600, 800), (20, 40, 80)).save(self.path)
        stat = self.path.stat()
        self.source = replace(self.source, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                              sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.page = replace(self.page, source=self.source)
        before = self.path.read_bytes()
        expected = make_preview(self.output, self.row(), {})
        wrapped = preview(self.roots, self.page, 'artifacts', self.row(), {}, renderer=make_preview,
                          version=VERSION, dependencies={'Pillow': importlib.metadata.version('Pillow'),
                          'vips': subprocess.check_output(['vips', '--version'], text=True).strip()})
        self.assertEqual(expected, wrapped.legacy)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(wrapped.result.transform.width, 1024)
        self.assertEqual(wrapped.result.transform.height, 512)


if __name__ == '__main__':
    unittest.main()
