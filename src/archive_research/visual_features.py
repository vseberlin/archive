"""Local OpenCLIP features for a bound page or crop; no model download or search."""
import importlib.metadata
import math

from .contracts import Recipe, Source, encoded, require


def recipe_for(task):
    from .runner import tool_version
    settings = {'dpi': 150, 'clockwise': 0, 'threads': 2, 'region_origin': None, **task.get('settings', {})}
    require(set(settings) == {'dpi', 'clockwise', 'threads', 'model', 'region_origin'}, 'Bind an explicit local visual model')
    require(type(settings['threads']) is int and 1 <= settings['threads'] <= 8, 'Invalid thread count')
    model = Source(**settings['model'])
    require(len(model.sha256) == 64, 'Fingerprint the local safetensors model')
    return Recipe.create('visual', '1', settings=settings,
                         encoder='ViT-B-32', dimensions=512, device='cpu', mkldnn=False,
                         preprocessing={'size': 224, 'resize': 'bicubic-shortest-edge', 'crop': 'center',
                                        'mean': [.48145466, .4578275, .40821073],
                                        'std': [.26862954, .26130258, .27577711]},
                         dependencies={**{n: importlib.metadata.version(n) for n in
                                       ('torch', 'torchvision', 'open_clip_torch', 'safetensors', 'timm', 'Pillow', 'numpy')},
                                       'pdftoppm':tool_version(['pdftoppm','-v'])})


def validate_inputs(roots, work, task, recipe):
    from .receipts import read_current
    from .runner import page_from_record
    from .contracts import Region
    settings=recipe.record()['parameters']['settings']; model_path(roots,settings)
    origin=settings['region_origin']
    if origin is not None:
        require(set(origin)=={'key','figure','receipt_sha256'},'Invalid region origin')
        row,result,digest=read_current(roots,work,origin['key'])
        require(result.recipe.extractor in ('pdf-layout','scan-layout') and digest==origin['receipt_sha256'] and
                result.page==page_from_record(task['page']),'Layout origin changed or belongs to another page')
        figures=row['feature']['figures'];index=origin['figure']
        require(type(index) is int and 0<=index<len(figures),'Unknown layout figure')
        require(task.get('region') and Region(**task['region'])==Region(**figures[index]['region']),
                'Visual crop differs from layout figure')


def model_path(roots, settings):
    return roots.source(Source(**settings['model']), verify_hash=True)


def extract(image_path, roots, recipe):
    import open_clip
    import torch
    from PIL import Image
    from safetensors.torch import load_file
    config = recipe.record()['parameters']; settings = config['settings']
    path = model_path(roots, settings)
    torch.set_num_threads(settings['threads'])
    torch.backends.mkldnn.enabled = False
    # Build from bundled architecture config, then load safe tensors explicitly.
    # Cached HF blobs need not have a suffix; no symlink or remote resolver is used.
    model, _, transform = open_clip.create_model_and_transforms(
        config['encoder'], pretrained=None, device='cpu',
        image_mean=config['preprocessing']['mean'], image_std=config['preprocessing']['std'])
    model.load_state_dict(load_file(str(path),device='cpu'),strict=True)
    model.eval()
    with Image.open(image_path) as image:
        tensor = transform(image.convert('RGB')).unsqueeze(0)
    with torch.inference_mode():
        vector = model.encode_image(tensor).float()[0]
        norm = vector.norm()
        require(torch.isfinite(vector).all().item() and norm.item() > 0, 'Invalid visual embedding')
        values = (vector / norm).tolist()
    require(len(values) == config['dimensions'] and all(math.isfinite(v) for v in values),
            'Unexpected visual dimensions')
    roots.source(Source(**settings['model']))
    result = {'encoder': config['encoder'], 'model_sha256': settings['model']['sha256'],
              'dimensions': len(values), 'vector': values, 'preprocess': str(transform),
              'identity_confirmed': False,
              'limitations': ['Similarity is a retrieval lead, not identity.',
                              'Center-crop preprocessing can omit edge details; index detail crops separately.']}
    return (encoded(result)+'\n').encode(), result
