"""Bounded PP-OCR detection plus independent Tesseract readings; no identity decisions.

Model/preprocessing reference: OpenCV Zoo text_detection_ppocr (Apache-2.0),
https://github.com/opencv/opencv_zoo/tree/main/models/text_detection_ppocr.
The model and its licence stay in explicitly configured private work storage.
"""
from dataclasses import asdict
import hashlib
import importlib.metadata
import math
import time

from ..imaging import RENDER_POLICY
from ..contracts import Artifact, Recipe, Region, Transform, encoded, require
from ..geometry import inverse_rotation, source_region


# Fixed, bounded recipes; parameter changes are included in the receipt cache key.
ENHANCEMENTS = {
    'original': {},
    'levels': {'percentiles': [1, 99]},
    'gamma-light': {'gamma': .75},
    'gamma-dark': {'gamma': 1.35},
    'clahe': {'clip_limit': 2.0, 'tile_grid': [4, 4]},
    'unsharp': {'sigma': 1.0, 'amount': .75},
    'levels-unsharp': {'percentiles': [1, 99], 'sigma': 1.0, 'amount': .75},
}

DEFAULTS = {'dpi': 300, 'clockwise': 0, 'tile_size': 736, 'overlap': 160,
            'max_tiles': 64, 'max_regions': 32, 'scan_angles': [0, 90],
            'binary_threshold': .3, 'polygon_threshold': .5,
            'languages': 'deu+eng', 'recognition_angles': [0, 180],
            'psms': [7, 11], 'polarities': ['original', 'inverted'],
            'recognition_timeout': 15, 'timeout': 240,
            'enhancements': ['original']}


def settings_for(task):
    value = {**DEFAULTS, **task.get('settings', {})}
    require(set(value) == set(DEFAULTS) | {'model'}, 'Text discovery needs an explicit model and known settings')
    model = Artifact(**value['model'])
    require(len(model.sha256) == 64, 'Detector model fingerprint required')
    for key, low, high in [('tile_size', 256, 1536), ('overlap', 0, 512), ('max_tiles', 1, 128),
                           ('max_regions', 1, 64), ('recognition_timeout', 1, 60), ('timeout', 1, 900)]:
        require(type(value[key]) is int and low <= value[key] <= high, 'Invalid '+key)
    require(value['overlap'] < value['tile_size'], 'Tile overlap must be smaller than tile')
    for key in ('scan_angles', 'recognition_angles'):
        angles = value[key]
        require(isinstance(angles, list) and angles and len(angles) == len(set(angles)) and
                all(type(a) is int and a in (0,90,180,270) for a in angles), 'Invalid '+key)
    require(value['psms'] and len(value['psms']) == len(set(value['psms'])) and
            all(type(p) is int and p in (7, 11, 13) for p in value['psms']), 'Invalid recognition modes')
    require(value['polarities'] and len(value['polarities']) == len(set(value['polarities'])) and
            all(p in ('original', 'inverted') for p in value['polarities']), 'Invalid polarities')
    choices = value['enhancements']
    require(isinstance(choices, list) and choices and choices[0] == 'original' and
            all(isinstance(p, str) and p in ENHANCEMENTS for p in choices) and
            len(choices) == len(set(choices)), 'Enhancements must retain original first and use distinct known recipes')
    for key in ('binary_threshold', 'polygon_threshold'):
        require(type(value[key]) in (int,float) and .05 <= value[key] <= .95, 'Invalid '+key)
    require(isinstance(value['languages'],str) and value['languages'] and
            all(s.isalpha() for s in value['languages'].split('+')), 'Invalid OCR languages')
    return value


def recipe_for(task):
    from ..runner import tool_version
    settings = settings_for(task)
    return Recipe.create('text-regions', '2', settings=settings, preparation=RENDER_POLICY,
                         enhancement_recipes={name: ENHANCEMENTS[name] for name in settings['enhancements']},
                         dependencies={name: importlib.metadata.version(name) for name in
                                       ('opencv-python-headless', 'numpy', 'Pillow')},
                         tesseract=tool_version(['tesseract','--version']),
                         pdftoppm=tool_version(['pdftoppm','-v']),
                         output='detection-polygons-and-independent-ocr-variants')


def tile_windows(width, height, size, overlap):
    def starts(length):
        if length <= size:
            return [0]
        return sorted(set([*range(0, length-size+1, size-overlap), length-size]))
    return [(x,y,min(size,width-x),min(size,height-y)) for y in starts(height) for x in starts(width)]


def unrotate_point(x, y, width, height, angle):
    return {0:(x,y), 90:(y,height-x), 180:(width-x,height-y), 270:(width-y,x)}[angle]


def bounds(polygon):
    xs,ys = zip(*polygon)
    return min(xs),min(ys),max(xs)-min(xs),max(ys)-min(ys)


def overlap(first, second):
    x,y,w,h=first; a,b,c,d=second
    intersection=max(0,min(x+w,a+c)-max(x,a))*max(0,min(y+h,b+d)-max(y,b))
    union=w*h+c*d-intersection
    return intersection/union if union else 0


def merge_candidates(candidates):
    kept=[]
    for candidate in sorted(candidates,key=lambda c:-c['score']):
        match=next((c for c in kept if overlap(bounds(c['polygon']),bounds(candidate['polygon'])) >= .35),None)
        if match is None:
            kept.append({**candidate,'observations':[candidate['pass']]})
        else:
            match['observations'].append(candidate['pass'])
    return kept


def recognition_status(variants, expected):
    readings=list(dict.fromkeys(v['text'] for v in variants if v['status']=='complete' and v['text']))
    if len(variants) < expected:
        return 'not_attempted' if not variants else 'partial_recognition',readings
    if any(v['status']=='error' for v in variants):
        return 'recognition_failed',readings
    if len(readings)>1:
        return 'conflicting_readings',readings
    return ('text_extracted' if readings else 'unreadable'),readings


def compose(outer, inner):
    a,b,c,d,e,f=outer; g,h,i,j,k,l=inner
    return (a*g+b*j,a*h+b*k,a*i+b*l+c,d*g+e*j,d*h+e*k,d*i+e*l+f)


def enhance_crop(crop, name, cv, np):
    """Photometric variants only; preserve geometry and never mutate source pixels."""
    require(name in ENHANCEMENTS, 'Unknown enhancement')
    if name == 'original':
        return crop.copy(), {'recipe': name, 'parameters': {}}
    params = ENHANCEMENTS[name]
    gray = cv.cvtColor(crop, cv.COLOR_BGR2GRAY) if crop.ndim == 3 else crop.copy()
    output = gray.copy()
    details = {'recipe': name, 'parameters': params, 'grayscale': 'OpenCV BGR2GRAY'}
    if 'percentiles' in params:
        low, high = np.percentile(gray, params['percentiles'])
        details['measured_levels'] = [float(low), float(high)]
        if high > low:
            output = np.clip((gray.astype(np.float32)-low)*255/(high-low), 0, 255).round().astype(np.uint8)
        else:
            details['levels_skipped'] = 'constant intensity'
    if 'gamma' in params:
        lut = np.round(255*(np.arange(256)/255)**params['gamma']).astype(np.uint8)
        output = cv.LUT(output, lut)
    if name == 'clahe':
        output = cv.createCLAHE(clipLimit=params['clip_limit'], tileGridSize=tuple(params['tile_grid'])).apply(output)
    if 'sigma' in params:
        blur = cv.GaussianBlur(output, (0, 0), params['sigma'])
        output = cv.addWeighted(output, 1+params['amount'], blur, -params['amount'], 0)
    details['black_fraction'] = float(np.mean(output == 0))
    details['white_fraction'] = float(np.mean(output == 255))
    return output, details


class Detector:
    def __init__(self, path, settings):
        import cv2 as cv
        import numpy as np
        self.cv,self.np=cv,np
        cv.setNumThreads(2)
        self.net=cv.dnn_TextDetectionModel_DB(str(path))
        self.net.setPreferableBackend(cv.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv.dnn.DNN_TARGET_CPU)
        self.net.setInputSize((736,736))
        self.net.setInputMean((123.675,116.28,103.53))
        self.net.setInputScale(1/255/np.array([.229,.224,.225]))
        self.net.setBinaryThreshold(settings['binary_threshold'])
        self.net.setPolygonThreshold(settings['polygon_threshold'])
        self.net.setUnclipRatio(2.0)
        self.net.setMaxCandidates(200)

    def detect(self, image):
        cv,np=self.cv,self.np
        height,width=image.shape[:2]; scale=736/max(width,height)
        rw,rh=max(1,round(width*scale)),max(1,round(height*scale))
        canvas=np.full((736,736,3),255,np.uint8)
        canvas[:rh,:rw]=cv.resize(image,(rw,rh))
        polygons,scores=self.net.detect(canvas)
        results=[]
        for polygon,score in zip(polygons,scores):
            if polygon[:,0].mean() >= rw or polygon[:,1].mean() >= rh:
                continue
            points=[[max(0,min(width,float(x)*width/rw)),max(0,min(height,float(y)*height/rh))]
                    for x,y in polygon]
            if min(bounds(points)[2:]) >= 2:
                results.append((points,float(score)))
        return results,len(polygons)>=200


def rectified_crop(image, polygon, transform, cv, np):
    (cx,cy),(width,height),angle=cv.minAreaRect(np.array(polygon,dtype=np.float32))
    if height>width:
        width,height=height,width;angle+=90
    width,height=max(4,math.ceil(width*1.15)),max(4,math.ceil(height*1.3))
    radians=math.radians(angle);c,s=math.cos(radians),math.sin(radians)
    mapping=(c,-s,cx-c*width/2+s*height/2,s,c,cy-s*width/2-c*height/2)
    image=cv.warpAffine(image,np.array(mapping).reshape(2,3),(width,height),
                       flags=cv.INTER_CUBIC|cv.WARP_INVERSE_MAP,borderMode=cv.BORDER_CONSTANT,borderValue=(255,255,255))
    mapped=Transform(width,height,compose(transform.to_source,mapping),
                     transform.steps+(f'detected rectangle affine rotation {angle:.6f}; padded crop',))
    return image,mapped


def extract(image_path, page, transform, roots, settings, temporary, key, detector=None):
    """Return candidates independently of recognition, plus prepared crop artifacts."""
    from .ocr import extract as recognize
    model=Artifact(**settings['model']);model_path=roots.artifact(model)
    require(hashlib.sha256(model_path.read_bytes()).hexdigest()==model.sha256, 'Detector model changed')
    try:
        detector=detector or Detector(model_path,settings)
    except Exception as exc:
        raise ValueError('Detector initialization failed: '+str(exc)) from exc
    cv,np=detector.cv,detector.np
    image=cv.imread(str(image_path));require(image is not None,'Prepared page unavailable')
    height,width=image.shape[:2]
    require(width*height<=40_000_000,'Page exceeds bounded detector pixel limit')
    start=time.monotonic();deadline=start+settings['timeout']
    tiles=tile_windows(width,height,settings['tile_size'],settings['overlap'])
    windows=[('overview',(0,0,width,height))]+[('tile',r) for r in tiles[:settings['max_tiles']]]
    passes=[];found=[]
    for mode,(x,y,w,h) in windows:
        for angle in settings['scan_angles']:
            job={'kind':mode,'window':[x,y,w,h],'clockwise':angle,'state':'not_attempted'}
            if time.monotonic()<deadline:
                try:
                    view=np.ascontiguousarray(np.rot90(image[y:y+h,x:x+w],-angle//90))
                    predictions,limited=detector.detect(view)
                    for polygon,score in predictions:
                        points=[unrotate_point(px,py,w,h,angle) for px,py in polygon]
                        points=[[px+x,py+y] for px,py in points]
                        found.append({'polygon':points,'score':score,'pass':len(passes)})
                    job.update(state='complete',regions=len(predictions),candidate_limit_reached=limited)
                except Exception as exc:
                    job.update(state='error',error=str(exc)[:1000])
            passes.append(job)
    candidates=merge_candidates(found)
    crops=[];words=[];regions=[]
    expected=len(settings['recognition_angles'])*len(settings['polarities'])*len(settings['psms'])*len(settings['enhancements'])
    for index,candidate in enumerate(candidates):
        box=bounds(candidate['polygon']);region=source_region(box,transform,page)
        a,b,c,d,e,f=transform.to_source
        polygon=[[max(0,min(1,(a*x+b*y+c)/page.width)),max(0,min(1,(d*x+e*y+f)/page.height))]
                 for x,y in candidate['polygon']]
        item={'region':asdict(region),'polygon':polygon,'detection_score':candidate['score'],
              'observations':candidate['observations'],'variants':[],'crop':None}
        if index<settings['max_regions'] and time.monotonic()<deadline:
            crop,crop_transform=rectified_crop(image,candidate['polygon'],transform,cv,np)
            name=f'{key}.region-{index}.png'
            ok,content=cv.imencode('.png',crop);require(ok,'Failed to encode region crop')
            crops.append((name,content.tobytes(),f'text-region-crop-{index}'))
            item.update(crop=name,crop_transform=asdict(crop_transform))
            item['enhancements'] = []
            for enhancement in settings['enhancements']:
                if time.monotonic() >= deadline:
                    break
                enhanced, parameters = enhance_crop(crop, enhancement, cv, np)
                preview = name
                if enhancement != 'original':
                    preview = f'{key}.region-{index}.{enhancement}.png'
                    ok, content = cv.imencode('.png', enhanced)
                    require(ok, 'Failed to encode enhanced crop')
                    crops.append((preview, content.tobytes(), f'text-region-crop-{index}-{enhancement}'))
                item['enhancements'].append({**parameters, 'crop': preview})
                for angle in settings['recognition_angles']:
                    rotated=np.ascontiguousarray(np.rot90(enhanced,-angle//90))
                    rh,rw=rotated.shape[:2]
                    orientation=inverse_rotation(crop.shape[1],crop.shape[0],rw,rh,angle)
                    mapping=compose(crop_transform.to_source,orientation.to_source)
                    for polarity in settings['polarities']:
                        prepared=rotated if polarity=='original' else 255-rotated
                        # Only geometric enlargement, never generative reconstruction.
                        scale=min(3,max(1,32/rh));pw,ph=max(1,round(rw*scale)),max(1,round(rh*scale))
                        prepared=cv.resize(prepared,(pw,ph),interpolation=cv.INTER_CUBIC)
                        mapped=Transform(pw,ph,compose(mapping,(rw/pw,0,0,0,rh/ph,0)),
                                         crop_transform.steps+(f'enhancement {enhancement}', f'clockwise {angle}; {polarity}; resize {pw}x{ph}',))
                        path=temporary/'region-reading.png';require(cv.imwrite(str(path),prepared),'Failed to prepare OCR crop')
                        for psm in settings['psms']:
                            remaining=deadline-time.monotonic()
                            if remaining<=0:
                                continue
                            variant={'enhancement':enhancement,'clockwise':angle,'polarity':polarity,'psm':psm,
                                     'transform':asdict(mapped),'status':'error','text':''}
                            try:
                                raw,reading=recognize(path,page,mapped,languages=settings['languages'],psm=psm,
                                                      timeout=min(settings['recognition_timeout'],max(.1,remaining)))
                                variant.update(status='complete',text=' '.join(w['text'] for w in reading),
                                               words=reading,raw_alto=raw.decode())
                            except Exception as exc:
                                variant['error']=str(exc)[:1000]
                            item['variants'].append(variant)
        item['status'],item['readings']=recognition_status(item['variants'],expected)
        for reading in item['readings']:
            words.append({'text':reading,'region':asdict(region),'confidence':None})
        regions.append(item)
    complete=sum(p['state']=='complete' for p in passes)
    coverage={'requested_passes':(len(tiles)+1)*len(settings['scan_angles']),
              'scheduled_passes':len(passes),'completed_passes':complete,
              'omitted_tiles':max(0,len(tiles)-settings['max_tiles']),
              'detected_regions':len(regions),'recognition_limit':settings['max_regions'],
              'expected_variants_per_region':expected,
              'states':{state:sum(r['status']==state for r in regions) for state in
                        ('text_extracted','conflicting_readings','unreadable','recognition_failed',
                         'not_attempted','partial_recognition')}}
    coverage['scan_complete']=complete==coverage['requested_passes'] and not any(p.get('candidate_limit_reached') for p in passes)
    coverage['status']=('incomplete_scan' if not coverage['scan_complete'] else
                        'regions_detected' if regions else 'no_regions_detected')
    details={'format':'archive-text-regions-1','metadata_adopted':False,'model':settings['model'],
             'coverage':coverage,'passes':passes,'regions':regions,'elapsed_seconds':time.monotonic()-start,
             'limitations':['Detection is separate from recognition; empty readings remain visible.',
                            'No detections does not prove no text; logos and non-text objects are outside this detector.',
                            'Readings are machine proposals; agreement is not accuracy.',
                            'Enhancement may amplify grain, clip strokes or introduce halos; compare the unchanged crop.']}
    return encoded(details).encode(),words,details,crops
