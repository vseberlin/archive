"""Geometry and coverage of detection-only face regions; no biometric matching."""
import unittest
from PIL import Image
import numpy as np
from archive_research.extractors.faces import detect, starts, overlap

class Detector:
    def __init__(self): self.sizes=[]
    def setInputSize(self,size): self.sizes.append(size)
    def detect(self,image): return None,np.asarray([[10,20,20,30,*([0]*10),.9]])

class FaceRegionsTests(unittest.TestCase):
    def settings(self,**changes):
        return {'tile':320,'overlap':80,'rotations':[90],'score':.8,'max_tiles':256,'timeout':20,**changes}
    def test_rotation_maps_to_original_non_square_pixels(self):
        with Image.new('RGB',(120,80)) as image:
            boxes,done,expected=detect(image,self.settings(),Detector())
        self.assertEqual((done,expected),(1,1))
        self.assertEqual(boxes[0]['box'],[20.,50.,30.,20.])
    def test_large_scan_uses_native_tiles_and_explicit_partial_coverage(self):
        detector=Detector()
        with Image.new('RGB',(2000,1000)) as image:
            boxes,done,expected=detect(image,self.settings(rotations=[0],max_tiles=3),detector)
        self.assertEqual(done,3);self.assertGreater(expected,done)
        self.assertEqual(detector.sizes[0],(320,160))
        self.assertEqual(detector.sizes[1:],[(320,320),(320,320)])
        self.assertEqual(boxes[1]['box'][2:],[20.,30.])
    def test_last_tile_covers_edge_and_overlap_does_not_chain_people(self):
        positions=starts(1000,320,80)
        self.assertEqual(positions[-1]+320,1000)
        self.assertLessEqual(max(b-a for a,b in zip(positions,positions[1:])),240)
        self.assertEqual(overlap([0,0,20,20],[21,0,20,20]),0)

if __name__=='__main__':unittest.main()
