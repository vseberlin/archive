"""Discovery/recognition separation and source geometry; generated images only."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
from archive_research import Artifact,Page,Root,Roots,Source,Transform
from archive_research.extractors.text_regions import (compose,extract,merge_candidates,recognition_status,
                                          rectified_crop,settings_for,tile_windows,unrotate_point,
                                          enhance_crop,ENHANCEMENTS,recipe_for)


class GeometryTests(unittest.TestCase):
    def test_tiles_cover_all_pixels_with_overlap_and_edge_alignment(self):
        for width,height in ((200,100),(1500,901),(736,736)):
            windows=tile_windows(width,height,736,160)
            for y in range(height):
                intervals=sorted((x,x+w) for x,t,w,h in windows if t<=y<t+h)
                end=0
                for start,stop in intervals:
                    self.assertLessEqual(start,end);end=max(end,stop)
                self.assertEqual(end,width)

    def test_rotations_return_original_tile_points(self):
        expected=(20,30)
        for angle,point in ((0,(20,30)),(90,(70,20)),(180,(180,70)),(270,(30,180))):
            self.assertEqual(unrotate_point(*point,200,100,angle),expected)

    def test_duplicates_keep_all_observations(self):
        values=[{'polygon':[[10,10],[100,10],[100,40],[10,40]],'score':.9,'pass':0},
                {'polygon':[[11,10],[101,10],[101,40],[11,40]],'score':.8,'pass':1}]
        found=merge_candidates(values)
        self.assertEqual(len(found),1);self.assertEqual(found[0]['observations'],[0,1])

    def test_reading_states_do_not_treat_detection_as_readability(self):
        empty={'status':'complete','text':''}; a={'status':'complete','text':'18 LK 15'}
        b={'status':'complete','text':'15 IK 15'}
        self.assertEqual(recognition_status([empty,empty],2)[0],'unreadable')
        self.assertEqual(recognition_status([a,b],2)[0],'conflicting_readings')
        self.assertEqual(recognition_status([a,a],2)[0],'text_extracted')
        self.assertEqual(recognition_status([],2)[0],'not_attempted')
        self.assertEqual(recognition_status([a],2)[0],'partial_recognition')
        self.assertEqual(recognition_status([a,{'status':'error','text':''}],2)[0],'recognition_failed')

    def test_settings_reject_unbounded_or_unfingerprinted_model(self):
        with self.assertRaises(ValueError):settings_for({'settings':{}})
        with self.assertRaises(ValueError):settings_for({'settings':{'model':{'root':'artifacts','path':'model.onnx','kind':'model'},'max_regions':10000}})


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        import cv2 as cv,numpy as np
        self.cv,self.np=cv,np
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.output=base/'output';self.output.mkdir()
        source=base/'source';source.mkdir()
        self.roots=Roots({'source':Root(source,'read'),'artifacts':Root(self.output,'work')})
        self.image=source/'image.png';cv.imwrite(str(self.image),np.full((200,400,3),255,np.uint8))
        info=self.image.stat();self.page=Page(Source('test','test','test','source','image.png',info.st_size,info.st_mtime_ns),1,'image',0,400,200)
        self.transform=Transform(400,200,(1,0,0,0,1,0))
        model=self.output/'model.onnx';model.write_bytes(b'injected synthetic detector')
        self.settings=settings_for({'settings':{'model':asdict(Artifact('artifacts','model.onnx','model',hashlib.sha256(model.read_bytes()).hexdigest())),
                                               'recognition_angles':[0],'polarities':['original'],'psms':[7], 'max_regions':1}})
        class FakeDetector:
            def __init__(this):this.cv=cv;this.np=np;this.calls=0
            def detect(this,image):
                this.calls+=1
                h,w=image.shape[:2]
                return [([[.075*w,.15*h],[.35*w,.15*h],[.35*w,.25*h],[.075*w,.25*h]],.9),
                        ([[.575*w,.45*h],[.85*w,.45*h],[.85*w,.55*h],[.575*w,.55*h]],.8)],False
        self.detector=FakeDetector()

    def run_extract(self):
        return extract(self.image,self.page,self.transform,self.roots,self.settings,self.output,'a'*64,self.detector)

    def test_unreadable_and_budget_omitted_candidates_survive(self):
        with patch('archive_research.extractors.ocr.extract',return_value=(b'<alto/>',[])):
            raw,words,result,crops=self.run_extract()
        self.assertFalse(words)
        self.assertTrue(any(r['status']=='unreadable' for r in result['regions']))
        self.assertTrue(any(r['status']=='not_attempted' for r in result['regions']))
        self.assertTrue(crops)
        self.assertTrue(result['coverage']['scan_complete'])

    def test_failed_detector_pass_is_explicit_incomplete_coverage(self):
        self.detector.detect=lambda image: (_ for _ in ()).throw(RuntimeError('synthetic detector failure'))
        _,_,result,_=self.run_extract()
        self.assertFalse(result['coverage']['scan_complete'])
        self.assertEqual(result['coverage']['status'],'incomplete_scan')
        self.assertTrue(all(p['state']=='error' for p in result['passes']))

    def test_empty_detection_is_not_a_blank_page_claim(self):
        self.detector.detect=lambda image:([],False)
        _,_,result,_=self.run_extract()
        self.assertEqual(result['coverage']['status'],'no_regions_detected')
        self.assertIn('No detections does not prove no text',result['limitations'][1])

    def test_tile_limit_reports_unscanned_area(self):
        self.settings.update(tile_size=256, overlap=0, max_tiles=1)
        self.detector.detect=lambda image:([],False)
        _,_,result,_=self.run_extract()
        self.assertEqual(result['coverage']['omitted_tiles'],1)
        self.assertFalse(result['coverage']['scan_complete'])

    def test_recognizer_failure_retains_detected_region_and_crop(self):
        with patch('archive_research.extractors.ocr.extract',side_effect=RuntimeError('unavailable recognizer')):
            _,words,result,crops=self.run_extract()
        self.assertFalse(words);self.assertTrue(crops)
        self.assertEqual(result['regions'][0]['status'],'recognition_failed')
        self.assertIn('unavailable recognizer',result['regions'][0]['variants'][0]['error'])

    def test_model_replacement_refused_before_inference(self):
        (self.output/'model.onnx').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'model changed'):self.run_extract()
        self.assertEqual(self.detector.calls,0)

    def test_affine_rectified_crop_centre_maps_to_original(self):
        np,cv=self.np,self.cv
        image=np.full((200,400,3),255,np.uint8)
        polygon=cv.boxPoints(((180,90),(120,25),35)).tolist()
        crop,mapped=rectified_crop(image,polygon,self.transform,cv,np)
        a,b,c,d,e,f=mapped.to_source;x,y=mapped.width/2,mapped.height/2
        self.assertAlmostEqual(a*x+b*y+c,180,places=4)
        self.assertAlmostEqual(d*x+e*y+f,90,places=4)

    def test_enhancements_preserve_source_and_geometry_including_flat_crop(self):
        np,cv=self.np,self.cv
        crop=np.full((40,180,3),90,np.uint8)
        cv.putText(crop,'AB 012',(5,30),cv.FONT_HERSHEY_SIMPLEX,.7,(115,115,115),1)
        before=crop.copy()
        for name in ENHANCEMENTS:
            output,details=enhance_crop(crop,name,cv,np)
            self.assertEqual(output.shape[:2],crop.shape[:2])
            self.assertEqual(output.dtype,np.uint8)
            self.assertTrue(np.array_equal(crop,before))
            self.assertFalse(np.shares_memory(output,crop))
        output,details=enhance_crop(np.full_like(crop,90),'levels',cv,np)
        self.assertTrue(np.all(output==90));self.assertIn('levels_skipped',details)

    def test_recipe_keys_separate_enhancements_and_require_original_control(self):
        baseline=recipe_for({'settings':self.settings})
        changed={**self.settings,'enhancements':['original','levels']}
        enhanced=recipe_for({'settings':changed})
        self.assertNotEqual(baseline.cache_key(self.page),enhanced.cache_key(self.page))
        for choices in ([],['levels'],['original','unknown'],['original','original']):
            with self.assertRaises(ValueError):settings_for({'settings':{**self.settings,'enhancements':choices}})

    def test_each_enhancement_has_bound_preview_and_separate_reading(self):
        self.settings['enhancements']=['original','levels','unsharp']
        with patch('archive_research.extractors.ocr.extract',return_value=(b'<alto/>',[])):
            _,_,result,crops=self.run_extract()
        region=result['regions'][0]
        self.assertEqual(result['coverage']['expected_variants_per_region'],3)
        self.assertEqual([v['enhancement'] for v in region['variants']],self.settings['enhancements'])
        self.assertEqual(len(region['enhancements']),3)
        self.assertEqual(region['enhancements'][0]['crop'],region['crop'])
        self.assertTrue(all(e['crop'] in {c[0] for c in crops} for e in region['enhancements']))
        transforms=[v['transform']['to_source'] for v in region['variants']]
        self.assertTrue(all(t==transforms[0] for t in transforms))


if __name__=='__main__':unittest.main()
