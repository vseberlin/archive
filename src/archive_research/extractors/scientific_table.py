"""Normalize retained table-model output without repeating model inference."""
from ..contracts import Recipe,Region,encoded,require
from ..imaging import RENDER_POLICY
from ..receipts import read_origin
from .scientific_document import structured_table_proposal


def recipe_for(task):
    from ..runner import tool_version
    settings={'dpi':300,'clockwise':0,**task.get('settings',{})}
    require(task.get('region') is not None and set(settings)=={'dpi','clockwise','recognition_origin'},
            'Explicit table recognition origin and region required')
    return Recipe.create('scientific-table','1',settings=settings,preparation=RENDER_POLICY,
                         pdftoppm=tool_version(['pdftoppm','-v']),
                         output='inert table HTML from retained HTML/OTSL; no model inference')


def reading(roots,work,task,recipe):
    from ..runner import page_from_record
    origin=recipe.record()['parameters']['settings']['recognition_origin']
    row,result,digest=read_origin(roots,work,origin,page_from_record(task['page']))
    require(result.recipe.extractor=='scientific-region' and
            result.region==Region(**task['region']),'Table recognition binding differs')
    f=row['feature']
    require(f['kind']=='table' and not f['handwriting_flag'] and
            isinstance(f.get('raw_output'),str),'No table recognition to interpret')
    return f


def validate_inputs(roots,work,task,recipe):
    reading(roots,work,task,recipe)


def extract(path,page,transform,roots,recipe,temporary,key,work):
    from dataclasses import asdict
    # Use the exact requested region rather than the outward-rounded image box.
    origin=recipe.record()['parameters']['settings']['recognition_origin']
    row,result,digest=read_origin(roots,work,origin,page)
    f=reading(roots,work,{'page':asdict(page),'region':asdict(result.region)},recipe)
    table=structured_table_proposal(f['raw_output'])
    feature={'kind':'table','recognition_origin':origin,'raw_output':f['raw_output'],
             'table':table,'model_rerun':False,'review_required':True,'metadata_adopted':False,
             'literal_ocr_replaced':False}
    extra=[]
    if table['html'] is not None:extra.append((key+'.table.html',table['html'].encode(),'table-html-proposal'))
    return (encoded(feature)+'\n').encode(),[],feature,extra
