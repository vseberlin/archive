"""Explicit full-line regions and two local recognizers; no inferred transcription.

CRNN preprocessing/vocabulary follow OpenCV Zoo text_recognition_crnn (Apache-2.0).
Models and licenses are explicitly bound private artifacts, never downloaded here.
"""
from dataclasses import asdict
import hashlib
import importlib.metadata
import math
import time

from ..imaging import RENDER_POLICY
from ..contracts import Artifact, Recipe, Region, Transform, encoded, require, text
from ..geometry import inverse_rotation
from .text_regions import ENHANCEMENTS, bounds, compose, enhance_crop, rectified_crop, recognition_status, recognition_coverage


def settings_for(task):
    defaults = {'dpi': 300, 'clockwise': 0, 'languages': 'rus+eng',
                'enhancements': ['original', 'levels', 'levels-unsharp'],
                'recognition_angles': [0, 180], 'polarities': ['original', 'inverted'],
                'psms': [7, 13], 'timeout': 600}
    value = {**defaults, **task.get('settings', {})}
    require(set(value) == set(defaults) | {'lines', 'tesseract_models', 'crnn_model'}, 'Unknown or missing line OCR settings')
    require(task['page']['kind'] == 'image' and task.get('region') is None and value['clockwise'] == 0,
            'Line comparison requires a complete unrotated source image')
    require(type(value['timeout']) is int and 1 <= value['timeout'] <= 900, 'Invalid line OCR timeout')
    require(value['languages'] in ('eng', 'rus', 'rus+eng', 'eng+rus'), 'Unsupported comparison language')
    for key, allowed in [('enhancements', ENHANCEMENTS), ('recognition_angles', (0, 90, 180, 270)),
                         ('polarities', ('original', 'inverted')), ('psms', (7, 13))]:
        selected = value[key]
        require(isinstance(selected, list) and selected and all(x in allowed for x in selected) and
                len(selected) == len(set(selected)), 'Invalid '+key)
    require(value['enhancements'][0] == 'original', 'Keep original control first')
    lines = value['lines']
    require(isinstance(lines, list) and 1 <= len(lines) <= 8, 'Select 1-8 explicit lines')
    for line in lines:
        require(set(line) == {'label', 'polygon'} and text(line['label']), 'Line label and polygon required')
        points = line['polygon']
        require(isinstance(points, list) and len(points) == 4 and
                all(isinstance(p, list) and len(p) == 2 and
                    all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in p) for p in points),
                'Four normalized source points required')
        cross = []
        for i in range(4):
            a,b,c = points[i],points[(i+1)%4],points[(i+2)%4]
            cross.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
        require(all(v > 1e-10 for v in cross) or all(v < -1e-10 for v in cross), 'Line polygon must be convex and nonzero')
    models = value['tesseract_models']
    require(isinstance(models, dict) and set(models) == set(value['languages'].split('+')), 'Bind every selected language model')
    for item in [*models.values(), value['crnn_model']]:
        require(len(Artifact(**item).sha256) == 64, 'Recognizer model fingerprint required')
    return value


def recipe_for(task):
    from ..runner import tool_version
    settings = settings_for(task)
    return Recipe.create('line-ocr', '1', settings=settings, preparation=RENDER_POLICY,
                         enhancement_recipes={n: ENHANCEMENTS[n] for n in settings['enhancements']},
                         dependencies={n: importlib.metadata.version(n) for n in ('opencv-python-headless', 'numpy', 'Pillow')},
                         tesseract=tool_version(['tesseract', '--version']),
                         crnn={'size': [100,32], 'mean': 127.5, 'scale': 1/127.5,
                               'vocabulary': '0123456789abcdefghijklmnopqrstuvwxyz', 'decode': 'CTC-greedy'},
                         output='explicit-lines-with-independent-recognizers')


def model_paths(roots, settings):
    """Verify even on cache reuse; reject redirected/mismatched model files."""
    paths = {}
    for name, item in {**settings['tesseract_models'], 'crnn': settings['crnn_model']}.items():
        artifact = Artifact(**item); path = roots.artifact(artifact)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256, 'Recognizer model changed: '+name)
        paths[name] = path
    languages = settings['languages'].split('+')
    require(len({paths[n].parent for n in languages}) == 1 and
            all(paths[n].name == n+'.traineddata' for n in languages), 'Language files must share their bound tessdata directory')
    return paths


def extract(image_path, page, transform, roots, settings, temporary, key):
    import cv2 as cv
    import numpy as np
    from .ocr import extract as recognize
    paths = model_paths(roots, settings)
    cv.setNumThreads(2)
    crnn = cv.dnn_TextRecognitionModel(str(paths['crnn']))
    crnn.setPreferableBackend(cv.dnn.DNN_BACKEND_OPENCV); crnn.setPreferableTarget(cv.dnn.DNN_TARGET_CPU)
    crnn.setDecodeType('CTC-greedy'); crnn.setVocabulary(list('0123456789abcdefghijklmnopqrstuvwxyz'))
    crnn.setInputParams(scale=1/127.5, size=(100,32), mean=(127.5,127.5,127.5))
    image = cv.imread(str(image_path)); require(image is not None, 'Prepared source unavailable')
    height,width = image.shape[:2]; require(width*height <= 40_000_000, 'Image exceeds line comparison limit')
    start=time.monotonic();deadline=start+settings['timeout'];crops=[];regions=[];words=[]
    expected=len(settings['enhancements'])*len(settings['recognition_angles'])*len(settings['polarities'])*(len(settings['psms'])+1)
    for index,line in enumerate(settings['lines']):
        polygon=line['polygon'];x,y,w,h=bounds(polygon);region=Region(x,y,w,h)
        item={'label':line['label'],'selection':'manual full-line region','region':asdict(region),'polygon':polygon,
              'observations':[],'detection_score':None,'crop':None,'variants':[],'enhancements':[]}
        if time.monotonic() < deadline:
            crop,mapped=rectified_crop(image,[[x*width,y*height] for x,y in polygon],transform,cv,np)
            name=f'{key}.region-{index}.png';ok,data=cv.imencode('.png',crop);require(ok,'Crop encoding failed')
            crops.append((name,data.tobytes(),f'text-region-crop-{index}'));item.update(crop=name,crop_transform=asdict(mapped))
            for enhancement in settings['enhancements']:
                if time.monotonic() >= deadline:break
                prepared,info=enhance_crop(crop,enhancement,cv,np);preview=name
                if enhancement != 'original':
                    preview=f'{key}.region-{index}.{enhancement}.png';ok,data=cv.imencode('.png',prepared);require(ok,'Enhancement encoding failed')
                    crops.append((preview,data.tobytes(),f'text-region-crop-{index}-{enhancement}'))
                item['enhancements'].append({**info,'crop':preview})
                for angle in settings['recognition_angles']:
                    rotated=np.ascontiguousarray(np.rot90(prepared,-angle//90));rh,rw=rotated.shape[:2]
                    mapping=compose(mapped.to_source,inverse_rotation(crop.shape[1],crop.shape[0],rw,rh,angle).to_source)
                    for polarity in settings['polarities']:
                        current=rotated if polarity=='original' else 255-rotated
                        # Scale complete lines to at least 64 pixels high, never more than 3x.
                        scale=min(3,max(1,64/rh));pw,ph=round(rw*scale),round(rh*scale)
                        current=cv.resize(current,(pw,ph),interpolation=cv.INTER_CUBIC)
                        current_map=Transform(pw,ph,compose(mapping,(rw/pw,0,0,0,rh/ph,0)),
                                              mapped.steps+(f'{enhancement}; clockwise {angle}; {polarity}; resize {pw}x{ph}',))
                        path=temporary/'line-reading.png';require(cv.imwrite(str(path),current),'Reading image unavailable')
                        for engine,psm in [('tesseract-best',p) for p in settings['psms']]+[('crnn-en',None)]:
                            remaining=deadline-time.monotonic()
                            if remaining <= 0:continue
                            variant={'engine':engine,'enhancement':enhancement,'clockwise':angle,'polarity':polarity,
                                     'psm':psm,'status':'error','text':'','transform':asdict(current_map)}
                            try:
                                if engine=='tesseract-best':
                                    raw,readings=recognize(path,page,current_map,languages=settings['languages'],psm=psm,
                                                          tessdata_dir=paths[settings['languages'].split('+')[0]].parent,
                                                          timeout=min(20,max(.1,remaining)))
                                    variant.update(text=' '.join(w['text'] for w in readings),raw_alto=raw.decode(),words=readings,
                                                   languages=settings['languages'])
                                else:
                                    gray=cv.cvtColor(current,cv.COLOR_BGR2GRAY) if current.ndim==3 else current
                                    variant.update(text=crnn.recognize(gray),languages='Latin/digits only',
                                                   recognizer_input_size=[100,32],word_geometry='whole selected line only')
                                variant['status']='complete'
                            except Exception as exc:variant['error']=str(exc)[:1000]
                            item['variants'].append(variant)
        item['status'],item['readings']=recognition_status(item['variants'],expected)
        words.extend({'text':t,'region':asdict(region),'confidence':None} for t in item['readings'])
        regions.append(item)
    coverage={'mode':'manual_lines','status':'selected_lines_only','scan_complete':False,
              'requested_passes':0,'completed_passes':0,'omitted_tiles':0,'recognition_limit':len(regions),
              'expected_variants_per_region':expected,'selected_regions':len(regions),
              **recognition_coverage(regions,expected,len(regions))}
    details={'format':'archive-text-regions-1','metadata_adopted':False,'coverage':coverage,'passes':[],
             'regions':regions,'elapsed_seconds':time.monotonic()-start,
             'limitations':['Explicit manual line regions only; no automatic whole-image coverage.',
                            'CRNN supports Latin letters/digits only; it cannot confirm Cyrillic.',
                            'Model disagreement remains visible; no catalogue hint or accepted transcription.']}
    return encoded(details).encode(),words,details,crops
