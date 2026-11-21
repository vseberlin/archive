"""Critical digit, geometry, routing and worker-lifetime regressions."""
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from archive_research import Page, Source, Transform
from archive_research.extractors.scientific_tokens import (
    ELEMENTS, formula, compare_readings, quantity, quantity_route,
    positioned_hocr, candidates)
from archive_research.extractors.ruled_table import classify_document, detect_cells, uncovered_words


class ScientificTokenTests(TestCase):
    def test_element_validation_and_case(self):
        self.assertEqual(len(ELEMENTS),118)
        result=formula(r'\[ S b_{ 2 }S_{ 3 } \]',latex=True)['alternatives'][0]
        self.assertEqual(result['normalized'],'Sb2S3')
        self.assertEqual(result['unicode'],'Sb₂S₃')
        self.assertNotEqual(formula('CO'),formula('Co'))
        self.assertEqual(len(formula('CO')['alternatives'][0]['tokens']),2)
        self.assertEqual(len(formula('Co')['alternatives'][0]['tokens']),1)
        for text in ('Xx2S3','Sb0S3',r'\ce{Sb2S3}','Ca(OH)2','Sb2S3+','sb2s3'):
            self.assertEqual(formula(text,latex=True)['alternatives'],[])

    def test_wrong_digit_is_reviewed_even_when_structure_matches(self):
        a=compare_readings('Sb2S3',r'S b_{2} S_{3}')
        self.assertEqual(a['review_priority'],'lower')
        self.assertTrue(a['review_required']);self.assertFalse(a['metadata_adopted'])
        b=compare_readings('Sb2S7',r'S b_{2} S_{3}')
        self.assertEqual(b['review_priority'],'review');self.assertIsNone(b['search_normalized'])

    def test_confusion_requires_independent_geometry_and_ambiguity_survives(self):
        for raw in ('Sb,S3','SbZS3','Sb(S3'):
            self.assertEqual(compare_readings(raw,'Sb2S3')['review_priority'],'review')
            a=compare_readings(raw,'Sb2S3',subscript_evidence={2:True})
            self.assertEqual(a['review_priority'],'lower')
            self.assertEqual(a['tesseract']['raw'],raw)
        ambiguous=compare_readings('C1O','C10',subscript_evidence={2:True})
        self.assertEqual(ambiguous['review_priority'],'review')
        self.assertEqual(len(ambiguous['tesseract']['alternatives']),2)
        self.assertEqual(formula('Cl2',ocr=True)['alternatives'][0]['normalized'],'Cl2')

    def test_units_and_uncertain_number_led_text_never_become_formula_candidates(self):
        for raw,expected in [('50 lx','50 lx'),('50 Ix','50 lx'),('50 |x','50 lx'),('250°C','250 °C'),
                             ('0,07 uA','0.07 µA'),('2,5 Torr','2.5 Torr'),('20 Ω','20 Ω')]:
            self.assertEqual(quantity(raw)['normalized'],expected)
        self.assertIsNone(quantity('50 LX'))
        for damaged in ('501x','5b2S57','70 Ls','18 LK15'):
            self.assertIsNone(quantity_route(damaged))
        self.assertIsNone(quantity_route('Sb2S3'))

    def reading(self,zero=False,line_class='ocr_line'):
        page=Page(Source('synthetic','object','image','input','synthetic.png',0,0),1,'image',0,100,100)
        tr=Transform(100,100,(1,0,0,0,1,0))
        chars=''.join(f'<span class="ocrx_cinfo" title="x_bboxes {x} {y} {x+w} {y+h}">{c}</span>'
            for c,x,y,w,h in [('S',10,10,10,20),('b',20,10,10,20),
                              ('2',30,24,0 if zero else 8,12),('S',38,10,10,20),('3',48,24,8,12)])
        raw=f'<html><div class="ocr_page" title="bbox 0 0 100 100"><span class="{line_class}" title="bbox 10 10 56 36; baseline 0 -6"><span class="ocrx_word" title="bbox 10 10 56 36">{chars}</span></span></div></html>'
        return positioned_hocr(raw,page,tr),page,tr

    def test_character_baseline_and_source_box(self):
        reading,page,tr=self.reading()
        chars=reading['words'][0]['characters']
        self.assertFalse(chars[0]['below_baseline']);self.assertTrue(chars[2]['below_baseline'])
        self.assertTrue(chars[2]['small_glyph']);self.assertAlmostEqual(chars[2]['region']['x'],.3)
        candidate=candidates(reading,page,tr)['formula_candidates'][0]
        self.assertEqual(candidate['raw'],'Sb2S3');self.assertEqual(candidate['pixel_box'],[8,8,50,30])
        self.assertTrue(candidate['geometry_signal'])

    def test_degenerate_character_preserves_text_without_inventing_coordinates(self):
        reading,page,tr=self.reading(zero=True)
        self.assertEqual(reading['words'][0]['text'],'Sb2S3')
        self.assertIsNone(reading['words'][0]['characters'][2]['region'])
        result=candidates(reading,page,tr)
        self.assertEqual(result['formula_candidates'],[])
        self.assertEqual(result['unlocated_formula_candidates'][0]['raw'],'Sb2S3')

    def test_caption_header_and_floating_text_are_not_dropped(self):
        for cl in ('ocr_caption','ocr_header','ocr_textfloat'):
            self.assertEqual(self.reading(line_class=cl)[0]['words'][0]['text'],'Sb2S3')

    def test_forms_are_not_tables(self):
        result=classify_document('Vorschlag für den Dokumentationsdienst\nPlan-Nr.: 123\nDatum: 1965')
        self.assertEqual(result['kind'],'form')
        self.assertEqual(result['fields'][0]['raw_value'],'123')
        self.assertEqual(classify_document('Spannung Strom 10 20')['kind'],'unresolved')
        damaged=classify_document('Dokumentationsdienst innerhalb\nDatum geprüft\nDatum geprüft')
        self.assertEqual(damaged['kind'],'form');self.assertEqual(len(damaged['fields']),4)
        self.assertIsNone(damaged['fields'][0]['raw_value'])

    def test_missing_cells_expose_original_positioned_words(self):
        regions=[{'x':0,'y':0,'width':1,'height':.2},{'x':0,'y':.2,'width':.2,'height':.8}]
        words=[{'text':'header','region':{'x':.4,'y':.05,'width':.1,'height':.1}},
               {'text':'130','region':{'x':.4,'y':.4,'width':.1,'height':.1}}]
        self.assertEqual(uncovered_words(words,regions),[words[1]])

    def test_closed_grid_keeps_empty_and_merged_cells_without_inventing_lines(self):
        from PIL import Image,ImageDraw
        with TemporaryDirectory() as d:
            p=Path(d)/'grid.png';im=Image.new('L',(640,440),255);draw=ImageDraw.Draw(im)
            for y in (40,160,280,400):draw.line((40,y,600,y),fill=0,width=3)
            for x in (40,600):draw.line((x,40,x,400),fill=0,width=3)
            draw.line((320,160,320,400),fill=0,width=3);im.save(p)
            cells,_=detect_cells(p);self.assertEqual(len(cells),5)
            self.assertGreater(cells[0]['pixel_box'][2],500)
            im=Image.new('L',(640,440),255);im.save(p)
            self.assertEqual(detect_cells(p)[0],[])
            draw=ImageDraw.Draw(im);draw.line((40,40,600,40),fill=0,width=3);im.save(p)
            self.assertEqual(detect_cells(p)[0],[])

    def test_model_reused_within_worker_and_invalidated_for_changed_settings(self):
        from archive_research.extractors.scientific_region import extract
        class Recipe:
            def __init__(self,threads):self.threads=threads
            def record(self):return {'parameters':{'settings':{'kind':'formula','handwriting':False,
                'selection_basis':'synthetic test','model_manifest':{'sha256':'a'*64},
                'threads':self.threads,'max_pixels':262144,'max_new_tokens':128}}}
        with patch('archive_research.extractors.scientific_region.model_directory',return_value=Path('/synthetic')), \
             patch('archive_research.extractors.scientific_region.Encoder') as encoder:
            encoder.return_value.load_seconds=7
            encoder.return_value.recognize.side_effect=lambda *a:('Sb2S3',5,{'generation_seconds':2})
            cache={}
            results=[extract(None,None,None,None,Recipe(t),None,'key',encoders=cache)[2] for t in (2,2,3)]
            self.assertEqual(encoder.call_count,2)
            self.assertEqual([r['timing']['model_loaded_this_crop'] for r in results],[True,False,True])
            self.assertEqual([r['timing']['model_load_seconds'] for r in results],[7,0,7])

    def test_recognized_quantity_and_form_bypass_model_before_loading(self):
        from archive_research.extractors.scientific_region import extract
        class Recipe:
            def record(self):return {'parameters':{'settings':{'kind':'formula','handwriting':False,
                'selection_basis':'synthetic negative control','ocr_origin':{}}}}
        for text in ('50 Ix','50 |x','Vorschlag für den Dokumentationsdienst'):
            with patch('archive_research.receipts.read_origin',return_value=({},SimpleNamespace(region=None),None)), \
                 patch('archive_research.extractors.scientific_region.ocr_reading',return_value={'words':[{'text':text}]}), \
                 patch('archive_research.extractors.scientific_region.model_directory',side_effect=AssertionError('No model needed')):
                f=extract(None,None,None,None,Recipe(),None,'key')[2]
                self.assertIsNone(f['raw_output']);self.assertIn('recognition_skipped',f)

    def test_formula_ocr_origin_must_match_exact_crop(self):
        from archive_research import Region
        from archive_research.extractors.scientific_region import ocr_reading
        source=SimpleNamespace(recipe=SimpleNamespace(extractor='ocr'),region=Region(.1,.1,.2,.2))
        with patch('archive_research.receipts.read_origin',return_value=({'feature':{'character_reading':{}}},source,None)):
            with self.assertRaisesRegex(ValueError,'crop binding'):
                ocr_reading(None,None,None,Region(.2,.1,.2,.2),{'ocr_origin':{}})
