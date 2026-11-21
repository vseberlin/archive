import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_research.extractors.scientific_document import (
    correction_proposal,route_blocks,table_proposal,structured_table_proposal,render_formula,formula_similarity)
from archive_research.profiles import research_profiles


def box(x=0,y=0,width=.2,height=.1):
    return {'x':x,'y':y,'width':width,'height':height,'units':'normalized','origin':'top-left'}


class ScientificDocumentTests(unittest.TestCase):
    def test_profile_and_unresolved_layout_are_explicit(self):
        self.assertTrue(research_profiles(['rd_reports']))
        blocks=[{'index':i,'label':label,'region':box()} for i,label in enumerate(
            ['figure_caption','isolate_formula','table','figure','abandon'])]
        routes=route_blocks(blocks)
        self.assertEqual([r['kind'] for r in routes],['text','formula','table','illustration','unresolved'])
        self.assertEqual(routes[0]['inline_formula_coverage'],'not_assessed')
        self.assertTrue(all(r['review_required'] for r in routes))

    def test_formula_region_protects_even_dropped_subscript(self):
        words=[{'text':'SbS','region':box()},{'text':'Aufnahne','region':box(x=.4)},
               {'text':'50','region':box(x=.7)}];original=copy.deepcopy(words)
        result=correction_proposal(words,[{'word':0,'before':'SbS','after':'Satz'},
            {'word':1,'before':'Aufnahne','after':'Aufnahme'},
            {'word':2,'before':'50','after':'20'}],[box()])
        self.assertEqual(words,original)
        self.assertEqual([w['text'] for w in result['words']],['SbS','Aufnahme','50'])
        self.assertEqual(len(result['blocked']),2)
        self.assertFalse(result['metadata_adopted'])
        with self.assertRaises(ValueError):
            correction_proposal(words,[{'word':1,'before':'different','after':'Aufnahme'}],[])

    def test_table_spans_preserved_and_active_html_refused(self):
        result=table_proposal('<table><tr><th colspan="2">V &amp; A</th></tr><tr><td>1</td><td>2</td></tr></table>')
        self.assertTrue(result['structure_valid']);self.assertEqual(result['cells'][0]['colspan'],2)
        self.assertEqual(result['cells'][0]['text'],'V & A');self.assertTrue(result['review_required'])
        for raw in ['<table onclick="x"><tr><td>1</td></tr></table>',
                    '<table><tr><td><script>x</script></td></tr></table>',
                    '<table><tr><td>unfinished', '<td>orphan</td>']:
            self.assertFalse(table_proposal(raw)['structure_valid'])

    def test_unicode_subscripts_are_not_spelling_errors(self):
        result=correction_proposal([{'text':'Sb₂S₃','region':box()}],
            [{'word':0,'before':'Sb₂S₃','after':'Satz'}],[])
        self.assertEqual(result['words'][0]['text'],'Sb₂S₃');self.assertEqual(len(result['blocked']),1)

    def test_otsl_preserves_empty_cells_and_both_spans(self):
        raw='<fcel>A<lcel><ecel><nl><ucel><xcel><fcel>B & C<nl>'
        result=structured_table_proposal(raw)
        self.assertTrue(result['structure_valid']);self.assertEqual(result['grid_columns'],3)
        self.assertEqual(result['cells'][0]['rowspan'],2);self.assertEqual(result['cells'][0]['colspan'],2)
        self.assertEqual(result['cells'][1]['text'],'');self.assertIn('B &amp; C',result['html'])
        self.assertTrue(result['review_required'])
        observed='<fcel>WF<fcel>Heading<nl><fcel>VEB<ucel><nl>'
        result=structured_table_proposal(observed)
        self.assertEqual(result['cells'][1]['rowspan'],2);self.assertEqual(result['grid_columns'],2)

    def test_invalid_otsl_is_not_silently_repaired(self):
        for raw in ['<fcel>A<nl><fcel>B<ecel><nl>', '<ucel><nl>',
                    '<fcel>A<lcel><nl><ucel><fcel>B<nl>', '<fcel>A']:
            self.assertFalse(structured_table_proposal(raw)['structure_valid'])

    def test_render_comparison_never_certifies_a_formula(self):
        try:import matplotlib,cv2
        except ImportError:self.skipTest('Optional rendering environment absent')
        rendered=render_formula(r'\mathrm{Sb}_{2}\mathrm{S}_{3}')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'formula.png';path.write_bytes(rendered)
            a=formula_similarity(path,rendered);b=formula_similarity(path,rendered)
        self.assertEqual(a,b);self.assertEqual(a['foreground_iou'],1)
        self.assertTrue(a['review_required']);self.assertFalse(a['threshold_calibrated'])
        with self.assertRaises(ValueError):render_formula(r'\input{secret}')
        with self.assertRaises(ValueError):render_formula(r'\ce{Sb2S3}')

    def test_handwriting_does_not_invoke_recognizer(self):
        from archive_research.extractors.scientific_region import extract
        class Recipe:
            def record(self):
                return {'parameters':{'settings':{'kind':'formula','handwriting':True,
                    'selection_basis':'Explicit marginal annotation'}}}
        with patch('archive_research.extractors.scientific_region.model_directory',side_effect=AssertionError('Must not load model')):
            raw,words,feature,artifacts=extract(None,None,None,None,Recipe(),None,'key')
        self.assertEqual(words,[]);self.assertEqual(artifacts,[])
        self.assertIsNone(feature['raw_output']);self.assertTrue(feature['review_required'])


if __name__=='__main__':unittest.main()
