"""Coverage, queue disposition and input preservation using generated images."""
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, TiffImagePlugin

from archive_common import ReviewError
from matching import stamp
from pixel_coverage import compare_pixels, compare_archive_photo
import test_contract as fixtures


def tiff16(path, pixels, rows=7, endian='<'):
    """Small uncompressed RGB16 TIFF with controllable strip layout and byte order."""
    height, width, _ = pixels.shape
    counts = [min(rows, height - y) * width * 6 for y in range(0, height, rows)]
    tags = [(256, 4, [width]), (257, 4, [height]), (258, 3, [16, 16, 16]),
            (259, 3, [1]), (262, 3, [2]), (273, 4, [0] * len(counts)),
            (277, 3, [3]), (278, 4, [rows]), (279, 4, counts), (284, 3, [1])]
    extra_size = sum(len(v) * (2 if t == 3 else 4) for _, t, v in tags if len(v) * (2 if t == 3 else 4) > 4)
    data_start = 8 + 2 + 12 * len(tags) + 4 + extra_size
    offsets = [data_start + sum(counts[:i]) for i in range(len(counts))]
    tags[5] = (273, 4, offsets)
    body, extra = b'', b''
    for tag, kind, values in tags:
        raw = struct.pack(endian + ('H' if kind == 3 else 'I') * len(values), *values)
        field = raw.ljust(4, b'\0') if len(raw) <= 4 else struct.pack(endian + 'I', 8 + 2 + 12 * len(tags) + 4 + len(extra))
        body += struct.pack(endian + 'HHI', tag, kind, len(values)) + field
        if len(raw) > 4:
            extra += raw
    header = (b'II' if endian == '<' else b'MM') + struct.pack(endian + 'HI', 42, 8)
    path.write_bytes(header + struct.pack(endian + 'H', len(tags)) + body + b'\0' * 4 + extra + pixels.astype(endian + 'u2').tobytes())


class PixelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='archive-pixel-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.a, self.b = self.root / 'a.tif', self.root / 'b.tif'
        self.pixels = np.random.default_rng(9).integers(10, 65520, (173, 131, 3), dtype=np.uint16)

    def test_native_rgb16_endianness_strip_boundaries_and_inversion_rounding(self):
        tiff16(self.a, self.pixels, rows=7)
        for inverted in (False, True):
            values = 65535 - self.pixels if inverted else self.pixels
            if inverted:
                values = values + (np.indices(values.shape).sum(axis=0) % 2).astype(np.uint16)
            tiff16(self.b, values, rows=173, endian='>')
            before = [(stamp(p), p.read_bytes()) for p in (self.a, self.b)]
            result = compare_pixels(self.a, self.b)
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(result['transform'], 'inverted_uint16' if inverted else 'direct')
            self.assertEqual(result['max_sample_error'], int(inverted))
            self.assertEqual(result['compared_samples'], self.pixels.size)
            self.assertTrue(result['full_raster_compared'])
            self.assertEqual(before, [(stamp(p), p.read_bytes()) for p in (self.a, self.b)])

    def test_retained_photo_checks_full_native_raster_across_rotation_and_inversion(self):
        tiff16(self.a, self.pixels)
        tiff16(self.b, 65535 - np.rot90(self.pixels))
        before = [(stamp(p), p.read_bytes()) for p in (self.a, self.b)]
        result = compare_archive_photo(self.a, self.b)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['compared_samples'], self.pixels.size)
        self.assertTrue(result['inverted'])
        self.assertEqual(before, [(stamp(p), p.read_bytes()) for p in (self.a, self.b)])
        changed = self.pixels.copy(); changed[-1,-1,-1] ^= 500
        tiff16(self.b, 65535 - np.rot90(changed))
        self.assertEqual(compare_archive_photo(self.a, self.b)['status'], 'not_equivalent')

    def test_retained_photo_ignores_icc_and_compression_but_keeps_extra_content_unresolved(self):
        pixels = (self.pixels >> 8).astype(np.uint8)
        Image.fromarray(pixels).save(self.a)
        Image.fromarray(pixels).save(self.b, compression='tiff_lzw', icc_profile=b'other-profile')
        self.assertEqual(compare_archive_photo(self.a, self.b)['status'], 'verified')
        Image.fromarray(pixels).save(self.b, save_all=True, append_images=[Image.fromarray(255-pixels)])
        self.assertEqual(compare_archive_photo(self.a, self.b)['status'], 'unsupported')

    def test_one_changed_last_sample_is_not_hidden_by_proxy_or_early_success(self):
        tiff16(self.a, self.pixels)
        for inverse, change in ((False, 1), (True, 2)):
            values = 65535 - self.pixels if inverse else self.pixels.copy()
            values[-1, -1, -1] += change
            tiff16(self.b, values)
            result = compare_pixels(self.a, self.b)
            self.assertEqual(result['status'], 'not_equivalent')
            self.assertTrue(result['full_raster_compared'])

    def test_orientation_profile_layers_and_extra_pages_need_review(self):
        base = Image.new('RGB', (80, 60), (30, 70, 90))
        base.save(self.b)
        for tags in ({274: 2}, {34675: b'different colour profile'}, {37724: b'layers'}, {34377: b'opaque resources'}):
            base.save(self.a, tiffinfo=tags)
            self.assertNotEqual(compare_pixels(self.a, self.b)['status'], 'verified')
        for reduced in (False, True):
            thumbnail = Image.new('RGB', (8, 6), (30, 70, 90))
            buffer = io.BytesIO()
            with TiffImagePlugin.AppendingTiffWriter(buffer, new=True) as writer:
                base.save(writer, format='TIFF')
                writer.newFrame()
                thumbnail.save(writer, format='TIFF', tiffinfo={254: 1} if reduced else {})
            del writer  # Pillow 11.1 closes twice; keep its caller-owned buffer alive.
            self.a.write_bytes(buffer.getvalue())
            result = compare_pixels(self.a, self.b)
            self.assertEqual(result['status'], 'verified' if reduced else 'unsupported')

    def test_compressed_16bit_and_8bit_inversion_are_not_coverage(self):
        Image.fromarray(self.pixels[:, :, 0]).save(self.a, compression='tiff_lzw')
        Image.fromarray(self.pixels[:, :, 0]).save(self.b)
        self.assertEqual(compare_pixels(self.a, self.b)['status'], 'unsupported')
        Image.new('RGB', (40, 30), (20, 70, 130)).save(self.a)
        Image.new('RGB', (40, 30), (235, 185, 125)).save(self.b)
        self.assertEqual(compare_pixels(self.a, self.b)['status'], 'not_equivalent')


class CoveragePreparationTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    picture = fixtures.ContractTests.picture
    unit = fixtures.ContractTests.unit
    relate = fixtures.ContractTests.relate
    run_prepare = fixtures.ContractTests.run_prepare
    save = fixtures.ContractTests.save

    def seed(self, caption=None, surrogate=False):
        master = self.unit(surrogate=surrogate)
        source = self.picture(self.source / 'source.tif', caption=caption)
        tid = str(self.units[0]) + '#same-key'
        # Deliberately untrusted visual discovery. Coverage must come from pixels.
        self.leads = patch('search.visual_matches', return_value=[{'engine': 'cached-pda',
            'target_id': tid, 'phash_distance': 0, 'dhash_distance': 0, 'ahash_distance': 0}])
        self.leads.start(); self.addCleanup(self.leads.stop)
        return source, master

    def test_default_pass_skips_covered_case_and_reuses_it_without_sift_or_discovery(self):
        source, master = self.seed()
        before = [(stamp(p), p.read_bytes()) for p in (source, master)]
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'redundant': 1})
        self.assertFalse(result['sift']['enabled'])
        self.assertFalse(app.objects)
        self.assertEqual(result['pixel_coverage']['verified'], 1)
        self.args.prior_review = [self.args.work_root]
        self.leads.stop()
        with patch('pixel_coverage.compare_pixels', side_effect=AssertionError('unchanged evidence should be reused')):
            repeated, _ = self.run_prepare('reused')
        self.assertEqual(repeated['counts'], {'redundant': 1})
        self.assertEqual(repeated['pixel_coverage']['reused_pairs'], 1)
        self.assertEqual(before, [(stamp(p), p.read_bytes()) for p in (source, master)])
        self.assertFalse(repeated['deletion_authorized'])

    def test_metadata_question_remains_visible(self):
        self.seed(caption='Unrecorded source caption')
        result, app = self.run_prepare()
        self.assertEqual(result['counts'], {'metadata_review': 1})
        self.assertTrue(next(iter(app.objects.values()))['metadata_review_required'])

    def test_same_pixels_do_not_override_surrogate_quality(self):
        self.seed(surrogate=True)
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'master_upgrade': 1})

    def test_false_phash_match_stays_unresolved(self):
        source, _ = self.seed()
        Image.new('RGB', (80, 60), (10, 20, 30)).save(source)
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'similarity_review': 1})

    def test_negative_human_decision_is_preserved(self):
        source, master = self.seed()
        self.run_prepare()
        self.args.prior_review = [self.args.work_root]
        self.relate(source, master, kind='different_photo')
        result, app = self.run_prepare('rejected-after-coverage')
        self.assertNotIn('redundant', result['counts'])
        self.assertFalse(next(iter(app.objects.values()))['confirmed_reference_ids'])

    def test_saved_source_proposal_is_not_silently_replaced_or_hidden(self):
        source, master = self.seed()
        with patch('pixel_coverage.compare_pixels', return_value={'status': 'unsupported', 'reason': 'synthetic old preparation'}):
            _, app = self.run_prepare()
        oid = next(iter(app.objects))
        self.save(app, oid, selected_candidate_id='source', destination_unit=str(self.units[0]))
        self.args.prior_review = [self.args.work_root]
        with patch('pixel_coverage.PROFILE', 'changed-profile-recheck'):
            result, app = self.run_prepare('new')
        self.assertEqual(result['counts'], {'evidence_conflict': 1})
        self.assertTrue(next(iter(app.objects.values()))['previous_proposals'])

    def test_uncovered_group_member_cannot_be_hidden(self):
        source, _ = self.seed()
        extra = self.source / 'layered.tif'
        Image.new('RGB', (80, 60), (40, 80, 130)).save(extra, tiffinfo={37724: b'extra layers'})
        self.relate(source, extra)
        result, _ = self.run_prepare()
        self.assertEqual(result['counts'], {'master_quality_review': 1})

    def test_catalogue_checksum_mismatch_aborts_without_changing_inputs(self):
        source, master = self.seed()
        # Alter metadata only so the pixels still match but the catalogue hash does not.
        self.picture(master, caption='Changed after catalogue creation')
        before = [(stamp(p), p.read_bytes()) for p in (source, master)]
        with self.assertRaisesRegex(ReviewError, 'Stored archive hash disagrees'):
            self.run_prepare()
        self.assertEqual(before, [(stamp(p), p.read_bytes()) for p in (source, master)])

    def test_stale_pair_cache_is_not_coverage(self):
        source, _ = self.seed()
        self.run_prepare()
        self.args.prior_review = [self.args.work_root]
        Image.new('RGB', (80, 60), (1, 2, 3)).save(source)
        result, _ = self.run_prepare('changed')
        self.assertEqual(result['counts'], {'similarity_review': 1})
        self.assertEqual(result['pixel_coverage']['compared_pairs'], 1)
        self.assertNotIn('reused_pairs', result['pixel_coverage'])

    def test_file_change_during_comparison_aborts_before_publication(self):
        source, _ = self.seed()
        def change_during_read(a, b):
            result = compare_pixels(a, b)
            self.picture(source, caption='Synthetic concurrent modification')
            return result
        with patch('pixel_coverage.compare_pixels', side_effect=change_during_read):
            with self.assertRaisesRegex(ReviewError, 'changed during pixel coverage'):
                self.run_prepare()
        self.assertFalse((self.args.work_root / 'workbench-input.json').exists())


if __name__ == '__main__':
    unittest.main()
