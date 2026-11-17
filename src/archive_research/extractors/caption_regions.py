"""Explicit geometric caption-band OCR tasks derived from verified layout figures."""
from dataclasses import asdict

from ..contracts import Region,require
from ..geometry import processed_region,source_region
from ..receipts import read_current


def band(page,transform,figure,side):
    require(side in ('above','below'),'Unknown caption side')
    shown=processed_region(Region(**figure['region']),transform,page)
    width=min(1.,max(.32,shown.width*2))
    x=max(0.,min(1.-width,shown.x+shown.width/2-width/2))
    y=max(0.,shown.y-.15) if side=='above' else min(1.,shown.y+shown.height+.008)
    bottom=max(0.,shown.y-.008) if side=='above' else min(1.,y+.14)
    require(bottom-y>.005,'No room for a caption band')
    return source_region((x*transform.width,y*transform.height,width*transform.width,
                          (bottom-y)*transform.height),transform,page)


def caption_tasks(roots,work,key,*,figures=None,sides=('below','above')):
    row,result,digest=read_current(roots,work,key)
    require(result.recipe.extractor in ('pdf-layout','scan-layout'),'Expected a layout receipt')
    found=row['feature']['figures']
    indices=list(range(len(found))) if figures is None else figures
    require(isinstance(indices,list) and len(indices)==len(set(indices)) and
            all(type(i) is int and 0<=i<len(found) for i in indices),'Unknown selected figures')
    require(sides and len(sides)==len(set(sides)) and all(s in ('above','below') for s in sides),'Invalid caption sides')
    tasks=[]
    for i in indices:
        for side in sides:
            try:region=band(result.page,result.transform,found[i],side)
            except ValueError:continue
            tasks.append({'id':f'{key}-figure-{i}-caption-{side}','extractor':'ocr','page':asdict(result.page),
                          'region':asdict(region),'settings':{'clockwise':result.recipe.record()['parameters']['settings']['clockwise'],
                          'psm':6,'thresholding_method':0,'caption_origin':{
                              'key':key,'figure':i,'side':side,'receipt_sha256':digest}}})
    return tasks


def validate_inputs(roots,work,task,recipe):
    from ..runner import page_from_record
    origin=recipe.record()['parameters']['settings']['caption_origin']
    require(set(origin)=={'key','figure','side','receipt_sha256'},'Invalid caption origin')
    row,result,digest=read_current(roots,work,origin['key'])
    require(result.recipe.extractor in ('pdf-layout','scan-layout') and digest==origin['receipt_sha256'] and
            result.page==page_from_record(task['page']),'Caption origin changed')
    index=origin['figure'];figures=row['feature']['figures']
    require(type(index) is int and 0<=index<len(figures),'Unknown caption figure')
    require(task.get('region') and Region(**task['region'])==band(result.page,result.transform,figures[index],origin['side']),
            'Caption crop differs from its derived band')
