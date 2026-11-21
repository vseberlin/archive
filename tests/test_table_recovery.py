"""Bordered tables, negative controls and exact retained-part reuse."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from archive_research.contracts import Page,Source,Transform,Recipe,Region
from archive_research.extractors.ruled_table import aligned_table,bordered_tables
from archive_research.extractors.document_layout import (
    refinement_inputs,block_origins,table_refinement_task,downstream_plan,TEXT_SCOPE)
from archive_research.extractors.document_text import inputs
from archive_research.evaluation import part_score
from test_document_parts import block
import test_positioned_extraction as positioned


def words_grid():
    words=[]
    for y in range(6):
        for x,text,width in [(0.13,'Part'+chr(65+y),.12),(.43,'Q'+chr(65+y),.07),(.75,str(20+y),.04)]:
            if y==2 and x==.43:continue
            words.append({'text':text,'region':{'x':x,'y':.35+y*.055,'width':width,'height':.025}})
    return words


class TableRecoveryTests(unittest.TestCase):
    def page(self):return Page(Source('test','test','image','source','scan.png',1,1),1,'image',0,1000,1000)

    def test_wrapped_property_and_units_share_cell_without_correcting_ocr(self):
        words=[]
        def add(x,y,text):
            words.append({'text':text,'region':{'x':x,'y':y,'width':.08,'height':.01}})
        for y,label,a,b in [(.3,'Biegefestigkeit','130','240'),(.4,'wärmeleit','4,2','40'),
                             (.52,'Farbe','weiß','grau'),(.62,'Temperatur','1300','1700')]:
            for x,text in [(.12,label),(.42,a),(.72,b)]:add(x,y,text)
        for x,y,text in [(.12,.32,'N/mm'),(.12,.42,'fähigkeit'),(.12,.44,'W/mekK'),(.12,.64,'°C')]:add(x,y,text)
        before=deepcopy(words);region={'x':.1,'y':.25,'width':.8,'height':.45}
        old=aligned_table(words,region,[.1,.35,.65,.9])
        result=aligned_table(words,region,[.1,.35,.65,.9],group_wrapped_rows=True)
        self.assertEqual(result['rows'],4)
        self.assertEqual(result['cells'][3]['text'],'wärmeleit\nfähigkeit\nW/mekK')
        self.assertEqual(result['cells'][4]['text'],'4,2')
        self.assertEqual(result['cells'][5]['text'],'40')
        self.assertEqual(result['cells'][-3]['text'],'Temperatur\n°C')
        self.assertEqual(result['text_rows']['cells'],old['cells'])
        self.assertEqual(sorted(i for c in result['cells'] for i in c['word_indices']),list(range(len(words))))
        self.assertEqual(words,before)
        # A distant label-only line might be a new sparse record: abstain.
        add(.12,.69,'unresolved')
        uncertain=aligned_table(words,region,[.1,.35,.65,.9],group_wrapped_rows=True)
        self.assertEqual(uncertain['row_grouping'],'text-bands')

    def test_unruled_columns_do_not_trigger_property_row_grouping(self):
        words=words_grid()
        old=aligned_table(words,{'x':.1,'y':.3,'width':.8,'height':.5})
        new=aligned_table(words,{'x':.1,'y':.3,'width':.8,'height':.5},group_wrapped_rows=True)
        self.assertEqual(new['cells'],old['cells'])
        self.assertEqual(new['row_grouping'],'text-bands')

    def test_alignment_keeps_empty_cell_without_fabricating_text(self):
        words=words_grid();before=deepcopy(words)
        result=aligned_table(words,{'x':.1,'y':.3,'width':.8,'height':.5})
        self.assertEqual((result['rows'],result['columns']),(6,3))
        empty=next(c for c in result['cells'] if c['row']==2 and c['column']==1)
        self.assertTrue(empty['no_source_words']);self.assertEqual(empty['word_indices'],[])
        self.assertEqual(words,before)
        assigned=[i for c in result['cells'] for i in c['word_indices']]
        self.assertEqual(sorted(assigned),list(range(len(words))))
        self.assertFalse(result['table_structure_verified'])

    def test_border_alone_does_not_make_boxed_prose_a_table(self):
        from PIL import Image,ImageDraw
        words=[{'text':'ordinary','region':{'x':.13+i*.1,'y':.35+j*.055,'width':.08,'height':.025}}
               for j in range(6) for i in range(6)]
        with TemporaryDirectory() as d:
            path=Path(d)/'page.png';image=Image.new('L',(1000,1000),255)
            ImageDraw.Draw(image).rectangle((100,300,900,800),outline=0,width=5);image.save(path)
            tables,rejected=bordered_tables(path,self.page(),Transform(1000,1000,(1,0,0,0,1,0)),words,[])
            self.assertEqual(tables,[]);self.assertTrue(rejected)

    def test_numbered_paragraphs_are_not_numeric_data_columns(self):
        words=[]
        for row in range(6):
            for x,text,width in [(.13,str(row+1),.02),(.23,'Continuous paragraph wording',.55)]:
                words.append({'text':text,'region':{'x':x,'y':.35+row*.06,'width':width,'height':.025}})
        self.assertIsNone(aligned_table(words,{'x':.1,'y':.3,'width':.8,'height':.5}))

    def test_long_column_rules_retain_centered_values_with_variable_widths(self):
        from PIL import Image,ImageDraw
        words=[]
        for row in range(5):
            for col,x in enumerate((.13,.4,.65)):
                text=('Item' if col==0 else str(10**row+col));width=.02*len(text)
                left=x if col==0 else x+(.1-width)/2
                words.append({'text':text,'region':{'x':left,'y':.35+row*.07,'width':width,'height':.025}})
        with TemporaryDirectory() as d:
            path=Path(d)/'page.png';image=Image.new('L',(1000,1000),255);draw=ImageDraw.Draw(image)
            draw.rectangle((100,300,900,800),outline=0,width=5)
            for x in (350,600):draw.line((x,300,x,800),fill=0,width=5)
            image.save(path)
            tables,_=bordered_tables(path,self.page(),Transform(1000,1000,(1,0,0,0,1,0)),words,[])
        self.assertEqual(len(tables),1)
        self.assertEqual((tables[0]['structure']['rows'],tables[0]['structure']['columns']),(5,3))
        self.assertIn('column rulings',tables[0]['structure']['basis'])

    def test_real_border_geometry_finds_table_and_protects_figures_forms_and_existing_tables(self):
        from PIL import Image,ImageDraw
        with TemporaryDirectory() as d:
            path=Path(d)/'page.png';image=Image.new('L',(1000,1000),255)
            ImageDraw.Draw(image).rectangle((100,300,900,800),outline=0,width=5);image.save(path)
            transform=Transform(1000,1000,(1,0,0,0,1,0))
            tables,_=bordered_tables(path,self.page(),transform,words_grid(),[])
            self.assertEqual(len(tables),1)
            self.assertAlmostEqual(tables[0]['region']['x'],.105,delta=.005)
            self.assertEqual(tables[0]['structure']['columns'],3)
            found,rejected=bordered_tables(path,self.page(),transform,words_grid(),[block(0,'figure',(.1,.3,.8,.5))])
            self.assertEqual(found,[]);self.assertTrue(rejected)
            found,_=bordered_tables(path,self.page(),transform,words_grid(),[block(0,'table',(.1,.3,.8,.5))])
            self.assertEqual(len(found),1);self.assertEqual(found[0]['existing_block'],0)
            from archive_research.extractors.document_layout import visual_selection
            conflicting=[block(0,'figure',(.1,.3,.8,.5)),block(1,'table',(.1,.3,.8,.5)),
                         block(2,'figure',(.1,.05,.4,.15))]
            found,_=bordered_tables(path,self.page(),transform,words_grid(),conflicting)
            self.assertEqual(len(found),1);self.assertEqual(found[0]['existing_block'],1)
            figures,excluded=visual_selection(conflicting)
            self.assertEqual(figures,[2]);self.assertEqual(excluded[0]['table_blocks'],[1])
            form=[{'text':'Vorschlag für den Dokumentationsdienst','region':{'x':.1,'y':.1,'width':.7,'height':.05}},*words_grid()]
            found,rejected=bordered_tables(path,self.page(),transform,form,[])
            self.assertEqual(found,[]);self.assertIn('form',rejected[0]['reason'])

    def test_unbordered_table_remains_explicitly_unsupported(self):
        from PIL import Image
        with TemporaryDirectory() as d:
            path=Path(d)/'page.png';Image.new('L',(1000,1000),255).save(path)
            found,_=bordered_tables(path,self.page(),Transform(1000,1000,(1,0,0,0,1,0)),words_grid(),[])
            self.assertEqual(found,[])

    def test_rotated_source_mapping_is_retained(self):
        from archive_research.geometry import source_region
        from PIL import Image,ImageDraw
        transform=Transform(1000,1000,(0,1,0,-1,0,1000))
        words=words_grid()
        for word in words:
            r=word['region'];word['region']=asdict(source_region(tuple(r[k]*1000 for k in ('x','y','width','height')),transform,self.page()))
        with TemporaryDirectory() as d:
            path=Path(d)/'page.png';image=Image.new('L',(1000,1000),255)
            ImageDraw.Draw(image).rectangle((100,300,900,800),outline=0,width=5);image.save(path)
            tables,_=bordered_tables(path,self.page(),transform,words,[])
        self.assertEqual(len(tables),1)
        self.assertEqual((tables[0]['structure']['rows'],tables[0]['structure']['columns']),(6,3))
        self.assertAlmostEqual(tables[0]['region']['x'],.3,delta=.01)
        self.assertAlmostEqual(tables[0]['region']['y'],.1,delta=.01)


class RecoveryIntegrationTests(unittest.TestCase):
    setUp=positioned.PositionedTests.setUp
    bind=positioned.PositionedTests.bind

    def test_refinement_reuses_ocr_and_preserves_raw_regions_and_assembly(self):
        from PIL import Image,ImageDraw,ImageFont
        from archive_research.preparation import inspect_page
        from archive_research.runner import extract_one,recipe_for
        from archive_research.receipts import read_current
        from archive_research.result_records import origin
        from archive_research.extractors.document_text import geometric_order
        path=self.input/'table.png';image=Image.new('RGB',(1000,1000),'white');draw=ImageDraw.Draw(image)
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',24)
        draw.text((100,100),'OUTSIDE ARTICLE',font=font,fill='black')
        draw.rectangle((100,300,900,800),outline='black',width=5)
        for w in words_grid():draw.text((round(w['region']['x']*1000),round(w['region']['y']*1000)),w['text'],font=font,fill='black')
        image.save(path);page=inspect_page(self.roots,self.bind(path),1,'image',0)
        from archive_research.extractors.document_layout import text_selection
        blocks=[block(0,'plain text',(.08,.08,.7,.08)),block(1,'plain text',(.12,.33,.15,.35)),
                block(2,'plain text',(.42,.33,.12,.35)),block(3,'plain text',(.74,.33,.12,.35))]
        base_recipe=Recipe.create('document-layout','fixture',settings={'dpi':300,'clockwise':0,'model':asdict(page.source)})
        with patch('archive_research.extractors.document_layout.recipe_for',return_value=base_recipe), \
             patch('archive_research.extractors.document_layout._extract',return_value=(b'{}',[],{'blocks':blocks,'coverage':{}},[])):
            base=extract_one(self.roots,'artifacts',{'id':'base','extractor':'document-layout','page':asdict(page)})
        reading=extract_one(self.roots,'artifacts',{'id':'reading','extractor':'ocr','page':asdict(page),
                            'settings':{'psm':6,'languages':'eng'}})
        self.assertEqual(reading['state'],'complete',reading)
        task=table_refinement_task(self.roots,'artifacts',base['key'],reading['key'])
        # Geometry refinement must never call YOLO or another OCR processor.
        with patch('archive_research.extractors.document_layout._extract',side_effect=AssertionError('no detector')), \
             patch('archive_research.extractors.ocr.extract',side_effect=AssertionError('no OCR')):
            refined=extract_one(self.roots,'artifacts',task)
        self.assertEqual(refined['state'],'partial',refined)
        row,_,_=read_current(self.roots,'artifacts',refined['key'])
        self.assertEqual(row['feature']['blocks'][:4],blocks)
        self.assertEqual(len(row['feature']['table_recovery']['tables']),1)
        self.assertEqual(text_selection(row['feature']['blocks'],TEXT_SCOPE)[0],[0])
        old=downstream_plan(self.roots,'artifacts',base['key'],profile='historical_newspaper',languages='eng')
        new=downstream_plan(self.roots,'artifacts',refined['key'],profile='historical_newspaper',languages='eng')
        self.assertEqual(recipe_for(old['tasks'][0]),recipe_for(new['tasks'][0]))
        self.assertEqual(new['parts'][-1]['kind'],'table')
        self.assertIn('table_structure',new['parts'][-1])
        ocr=extract_one(self.roots,'artifacts',old['tasks'][0])
        self.assertTrue(extract_one(self.roots,'artifacts',new['tasks'][0])['reused'])
        rr,_,digest=read_current(self.roots,'artifacts',ocr['key'])
        assembly={'id':'assembled','extractor':'document-text','page':asdict(page),'ocr_profile':new['material_profile'],
            'settings':{'layout_origin':new['layout_origin'],'text_scope':TEXT_SCOPE,'order_method':'geometric-cuts-v1',
                        'block_order':[0],'ocr_origins':[origin(self.roots,'artifacts',ocr['key'],digest)]}}
        outcome=extract_one(self.roots,'artifacts',assembly)
        self.assertEqual(outcome['state'],'partial',outcome)
        with self.assertRaisesRegex(ValueError,'do not stack'):
            table_refinement_task(self.roots,'artifacts',refined['key'],reading['key'])
        # Same geometry on a different page must not supply table text.
        changed=deepcopy(task['settings']);changed['ocr_origin']['receipt_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'changed'):refinement_inputs(self.roots,'artifacts',page,changed)
        corrupt=deepcopy(row);corrupt['feature']['blocks'][0]['region']['x']=.09
        _,result,digest=read_current(self.roots,'artifacts',refined['key'])
        with self.assertRaisesRegex(ValueError,'changed retained'):block_origins(self.roots,'artifacts',refined['key'],corrupt,result,digest)


if __name__=='__main__':unittest.main()
