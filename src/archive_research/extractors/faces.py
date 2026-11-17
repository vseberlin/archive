"""Local face-region detection only: no identity vectors, names or clustering."""
from dataclasses import asdict
import importlib.metadata
import math
import time

from ..contracts import Recipe, Source, encoded, require
from ..geometry import inverse_rotation, source_region
from ..imaging import RENDER_POLICY


def recipe_for(task):
    from ..runner import tool_version
    settings = {'dpi':150, 'clockwise':0, 'tile':1024, 'overlap':256,
                'rotations':[0,90,180,270], 'score':.8, 'max_tiles':256,
                'timeout':240, **task.get('settings',{})}
    require(set(settings) == {'dpi','clockwise','tile','overlap','rotations','score','max_tiles','timeout','model'}, 'Unknown face detector setting')
    require(len(Source(**settings['model']).sha256)==64, 'Fingerprint the local detector')
    require(type(settings['tile']) is int and 320<=settings['tile']<=1600 and
            type(settings['overlap']) is int and 0<settings['overlap']<settings['tile'], 'Invalid detector tiling')
    require(settings['rotations'] and len(set(settings['rotations']))==len(settings['rotations']) and
            all(v in (0,90,180,270) for v in settings['rotations']), 'Invalid detection rotations')
    require(0<settings['score']<1 and type(settings['max_tiles']) is int and
            1<=settings['max_tiles']<=1024 and 1<=settings['timeout']<=600, 'Invalid detection bounds')
    return Recipe.create('faces','1',settings=settings,preparation=RENDER_POLICY,
        detector='opencv-yunet', identity_inference=False,
        dependencies={**{n:importlib.metadata.version(n) for n in ('Pillow','numpy','opencv-python-headless')},
                      'pdftoppm':tool_version(['pdftoppm','-v'])})


def validate_inputs(roots, work, task, recipe):
    roots.source(Source(**recipe.record()['parameters']['settings']['model']),verify_hash=True)


def starts(length, tile, overlap):
    return sorted(set([*range(0,max(1,length-tile+1),tile-overlap),max(0,length-tile)]))


def overlap(a,b):
    x,y,w,h=a;u,v,s,t=b
    area=max(0,min(x+w,u+s)-max(x,u))*max(0,min(y+h,v+t)-max(y,v))
    return area / max(1,w*h+s*t-area)


def detect(image, settings, detector):
    """Preserve native detail in tiles; overview catches faces larger than a tile."""
    import numpy as np
    from PIL import Image
    width,height=image.size; tile=settings['tile']; boxes=[]; done=0; began=time.monotonic()
    views=[(x,y,min(tile,width-x),min(tile,height-y),False)
           for y in starts(height,tile,settings['overlap']) for x in starts(width,tile,settings['overlap'])]
    if width>tile or height>tile:
        views.insert(0,(0,0,width,height,True))
    expected=len(views)*len(settings['rotations'])
    for x,y,w,h,overview in views:
        with image.crop((x,y,x+w,y+h)) as crop:
            if overview: crop.thumbnail((tile,tile),Image.Resampling.LANCZOS)
            for rotation in settings['rotations']:
                if done>=settings['max_tiles'] or time.monotonic()-began>settings['timeout']:
                    return boxes,done,expected
                with crop.rotate(-rotation,expand=True) as shown:
                    detector.setInputSize(shown.size)
                    _,found=detector.detect(np.asarray(shown)[:,:,::-1].copy())
                    transform=inverse_rotation(w,h,*shown.size,rotation)
                    a,b,c,d,e,f=transform.to_source
                    for face in (() if found is None else found):
                        if not all(math.isfinite(float(v)) for v in face): continue
                        left,top,bw,bh=map(float,face[:4]);score=float(face[-1])
                        if score<settings['score'] or bw<=0 or bh<=0: continue
                        points=[(a*px+b*py+c,d*px+e*py+f) for px,py in
                                ((left,top),(left+bw,top),(left,top+bh),(left+bw,top+bh))]
                        x0=max(0,min(p[0] for p in points));x1=min(w,max(p[0] for p in points))
                        y0=max(0,min(p[1] for p in points));y1=min(h,max(p[1] for p in points))
                        if x1>x0 and y1>y0:
                            boxes.append({'box':[x+x0,y+y0,x1-x0,y1-y0], 'score':score,'rotation':rotation})
                done+=1
    return boxes,done,expected


def extract(path, page, transform, roots, recipe):
    import cv2
    from PIL import Image
    settings=recipe.record()['parameters']['settings']
    model=Source(**settings['model']); model_path=roots.source(model,verify_hash=True)
    cv2.setNumThreads(1)
    detector=cv2.FaceDetectorYN.create(str(model_path),'',(320,320),settings['score'],.3,5000)
    with Image.open(path) as image:
        boxes,done,expected=detect(image,settings,detector)
    roots.source(model)
    kept=[]
    for candidate in sorted(boxes,key=lambda b:-b['score']):
        if all(overlap(candidate['box'],other['box'])<.4 for other in kept): kept.append(candidate)
    regions=[{'region':asdict(source_region(b['box'],transform,page)),
              'score':b['score'],'detection_rotation':b['rotation']} for b in kept]
    feature={'kind':'face-regions','regions':regions,'identity_inferred':False,
             'coverage':{'views_processed':done,'views_requested':expected,'scan_complete':done==expected},
             'limitations':['Detector regions only; no names, face embeddings or identity matching.',
                            'No detections does not establish that no people are pictured.']}
    return (encoded(feature)+'\n').encode(),feature
