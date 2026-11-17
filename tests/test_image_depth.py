"""Actual unsigned-16-bit fixtures: tonal mapping, rejection and source geometry."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from archive_research import Root, Roots, Source, Region
from archive_research.imaging import display_rgb, RENDER_POLICY
from archive_research.preparation import inspect_page, render
from archive_research.runner import recipe_for


class ImageDepthTests(unittest.TestCase):
    def test_little_and_big_endian_ramps_preserve_tonal_range(self):
        for mode, endian in [('I;16', 'little'), ('I;16B', 'big')]:
            values = [0, 257, 16384, 32768, 49152, 65535]
            original = Image.frombytes(mode, (6, 1), b''.join(v.to_bytes(2, endian) for v in values))
            result, description = display_rgb(original)
            try:
                self.assertEqual([p[0] for p in result.getdata()], [0, 1, 63, 127, 191, 255])
                self.assertIn('65535', description)
                self.assertEqual(list(original.getdata()), values)
            finally:
                result.close();original.close()

    def test_eight_bit_output_is_unchanged(self):
        for mode in ('L', 'RGB', 'RGBA', 'P'):
            with Image.new(mode, (4, 2)) as original:
                converted, _ = display_rgb(original)
                self.assertEqual(converted.tobytes(), original.convert('RGB').tobytes())
                converted.close()

    def test_ambiguous_float_and_high_depth_rgb_fail_explicitly(self):
        with Image.new('F', (2, 2), .5) as original:
            with self.assertRaisesRegex(ValueError, 'high-depth'):
                display_rgb(original)
        with Image.new('RGB', (2, 2)) as original:
            original.tag_v2 = {258: (16, 16, 16)}
            with self.assertRaisesRegex(ValueError, 'high-depth'):
                display_rgb(original)

    def test_selected_tiff_frame_crop_rotation_and_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);source_dir=root/'input';source_dir.mkdir();output=root/'output';output.mkdir()
            file=source_dir/'two-frames.tif'
            first=Image.new('I;16',(4,2),0)
            second=Image.frombytes('I;16',(4,2),b''.join(v.to_bytes(2,'little') for v in [0,16384,32768,65535]*2))
            first.save(file,save_all=True,append_images=[second]);first.close();second.close()
            before=hashlib.sha256(file.read_bytes()).hexdigest();st=file.stat()
            source=Source('synthetic','depth','frames','source',file.name,st.st_size,st.st_mtime_ns,before)
            roots=Roots({'source':Root(source_dir,'read'),'artifacts':Root(output,'work')})
            page=inspect_page(roots,source,2,'tiff',1)
            path,transform,angle=render(roots,page,output,clockwise=90,region=Region(.5,0,.5,1))
            with Image.open(path) as converted:
                self.assertEqual(converted.size,(2,2))
                self.assertEqual(set(converted.getchannel('R').getdata()),{127,255})
            self.assertEqual(angle,90);self.assertIsNotNone(transform.to_source)
            self.assertTrue(any('uint16' in s for s in transform.steps))
            from archive_research.prepared_views import prepared_view
            first,_,_=prepared_view(roots,page,root,output,dpi=150,clockwise=0)
            with patch('archive_research.prepared_views.render',side_effect=AssertionError('TIFF DPI must not trigger decoding')):
                second,_,_=prepared_view(roots,page,root,output,dpi=300,clockwise=0)
            self.assertEqual(first,second)
            recipe=recipe_for({'page':asdict(page),'extractor':'ocr'})
            self.assertEqual(recipe.record()['parameters']['preparation'],RENDER_POLICY)
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
