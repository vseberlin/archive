import unittest
import json
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
from archive_research.evaluation import page_xml, layout_coverage, critical_score, literal


class EvaluationTests(unittest.TestCase):
    def test_scientific_distinctions_survive_literal_normalization(self):
        self.assertNotEqual(literal('Sb₂S₃'),literal('Sb2S3'))
        self.assertNotEqual(literal('50 lx'),literal('20 Ix'))

    def test_repeated_critical_token_does_not_hide_omitted_occurrence(self):
        reference={'text':'1962 and 1962','critical':[{'start':0,'end':4,'text':'1962','category':'date'},
                                                   {'start':9,'end':13,'text':'1962','category':'date'}]}
        checks=critical_score(reference,'2962 and 1962')
        self.assertEqual([c['correct'] for c in checks],[False,True])

    def test_layout_merge_cannot_count_twice(self):
        a={'x':0,'y':0,'width':.5,'height':1};b={**a,'x':.5}
        result=layout_coverage([a,b],[{**a,'width':1}],.5)
        self.assertEqual(result['recall'],.5)

    def test_extra_digit_at_token_boundary_is_not_correct(self):
        reference={'text':'1962','critical':[{'start':0,'end':4,'text':'1962','category':'date'}]}
        self.assertFalse(critical_score(reference,'19620')[0]['correct'])
        self.assertFalse(critical_score(reference,'11962')[0]['correct'])

    def test_final_test_cannot_be_scored_before_freeze_and_reference_check(self):
        from archive_research.evaluation import evaluate
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'manifest.json'
            page={'id':'held-out','split':'final-test','readings':[{'id':'candidate'}]}
            for locked, message in [(True,'recipes'),(False,'operator')]:
                path.write_text(json.dumps({'final_test_locked':locked,'pages':[page]}))
                with self.assertRaisesRegex(ValueError,message):
                    evaluate(path,directory)

    def test_page_keeps_original_geometry_without_invented_lines(self):
        region={'region':{'x':.1,'y':.2,'width':.3,'height':.4},'text':'50 lx'}
        tree=ET.fromstring(page_xml('scan.png',(1000,2000),[region]))
        self.assertEqual(tree.find('.//{*}Coords').attrib['points'],'100,400 400,400 400,1200 100,1200')
        self.assertEqual(tree.find('.//{*}Unicode').text,'50 lx')
        self.assertIsNone(tree.find('.//{*}TextLine'))

    def test_layout_final_test_has_the_same_freeze_and_reference_gate(self):
        from archive_research.evaluation import evaluate
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'manifest.json'
            page={'id':'held-out','split':'final-test','layout_readings':[{'id':'candidate'}]}
            for locked,message in [(True,'recipes'),(False,'operator')]:
                path.write_text(json.dumps({'final_test_locked':locked,'pages':[page]}))
                with self.assertRaisesRegex(ValueError,message):evaluate(path,directory)


if __name__ == '__main__': unittest.main()
