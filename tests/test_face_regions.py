"""Geometry and coverage of detection-only face regions; no biometric matching."""
import unittest
import json
from dataclasses import asdict
from unittest.mock import patch
from PIL import Image
import numpy as np
from archive_research.extractors.faces import detect, starts, overlap
import test_positioned_extraction as positioned

class Detector:
    def __init__(self): self.sizes=[]
    def setInputSize(self,size): self.sizes.append(size)
    def detect(self,image): return None,np.asarray([[10,20,20,30,*([0]*10),.9]])

class FaceRegionsTests(unittest.TestCase):
    def settings(self,**changes):
        return {'tile':320,'overlap':80,'rotations':[90],'tile_rotations':[0],'score':.8,'max_tiles':256,'timeout':20,**changes}
    def test_rotation_maps_to_original_non_square_pixels(self):
        with Image.new('RGB',(120,80)) as image:
            boxes,done,expected,stopped=detect(image,self.settings(),Detector())
        self.assertEqual((done,expected,stopped),(1,1,None))
        self.assertEqual(boxes[0]['box'],[20.,50.,30.,20.])
    def test_large_scan_uses_native_tiles_and_explicit_partial_coverage(self):
        detector=Detector()
        with Image.new('RGB',(2000,1000)) as image:
            boxes,done,expected,stopped=detect(image,self.settings(rotations=[0],max_tiles=3),detector)
        self.assertEqual((done,stopped),(3,'budget'));self.assertGreater(expected,done)
        self.assertEqual(detector.sizes[0],(320,160))
        self.assertEqual(detector.sizes[1:],[(320,320),(320,320)])
        self.assertEqual(boxes[1]['box'][2:],[20.,30.])
    def test_orientation_search_on_whole_view_keeps_large_scans_in_budget(self):
        class Rotated(Detector):
            def detect(self,image):  # faces only visible when the view is turned to portrait
                return None,(np.asarray([[10,20,20,30,*([0]*10),.9]]) if image.shape[0]>image.shape[1] else None)
        detector=Rotated()
        with Image.new('L',(8000,6000)) as image:
            settings=self.settings(tile=1024,overlap=256,rotations=[0,90,180,270],max_tiles=400)
            boxes,done,expected,stopped=detect(image,settings,detector)
        tiles=len(starts(8000,1024,256))*len(starts(6000,1024,256))
        self.assertEqual(stopped,None)
        self.assertEqual(expected,4+2*tiles)  # upright plus the orientation the overview found
        self.assertEqual(done,expected)
        self.assertTrue(any(b['rotation']==90 for b in boxes))
    def test_last_tile_covers_edge_and_overlap_does_not_chain_people(self):
        positions=starts(1000,320,80)
        self.assertEqual(positions[-1]+320,1000)
        self.assertLessEqual(max(b-a for a,b in zip(positions,positions[1:])),240)
        self.assertEqual(overlap([0,0,20,20],[21,0,20,20]),0)

class FaceCropTests(unittest.TestCase):
    setUp=positioned.PositionedTests.setUp
    bind=positioned.PositionedTests.bind

    def test_cropped_detection_plan_maps_boxes_back_to_original_source(self):
        from archive_research.contracts import Region
        from archive_research.preparation import inspect_page
        from archive_research.runner import load_plan,extract_one
        from archive_research.receipts import read_current
        p=self.input/'page.png';Image.new('RGB',(200,100),'white').save(p)
        page=inspect_page(self.roots,self.bind(p),1,'image',0)
        model=self.input/'model.onnx';model.write_bytes(b'fixture')
        task={'id':'crop','extractor':'faces','page':asdict(page),
              'region':asdict(Region(.2,.1,.5,.5)),
              'settings':{'model':asdict(self.bind(model)),'rotations':[0]}}
        plan=self.input/'plan.json';plan.write_text(json.dumps(
            {'format':'archive-research-plan-1','metadata_adopted':False,'tasks':[task]}))
        self.assertEqual(load_plan(plan)['tasks'],[task])
        def detected(image,settings,detector):
            self.assertEqual(image.size,(100,50))
            return [{'box':[10,5,20,10],'score':.9,'rotation':0}],1,1,None
        with patch('cv2.FaceDetectorYN'),patch('archive_research.extractors.faces.detect',side_effect=detected):
            result=extract_one(self.roots,'artifacts',task)
        self.assertIn(result['state'],('complete','partial'),result)
        row,_,_=read_current(self.roots,'artifacts',result['key'])
        region=row['feature']['regions'][0]['region']
        for name,expected in [('x',.25),('y',.15),('width',.1),('height',.1)]:
            self.assertAlmostEqual(region[name],expected)
        self.assertEqual(region['units'],'normalized')
        self.assertFalse(row['feature']['identity_inferred'])

if __name__=='__main__':unittest.main()
