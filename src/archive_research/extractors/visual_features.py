"""Local OpenCLIP features for a bound page or crop; no model download or search."""
import importlib.metadata
import math

from ..imaging import RENDER_POLICY
from ..contracts import Recipe, Source, encoded, require


def recipe_for(task):
    from ..runner import tool_version
    settings = {'dpi': 150, 'clockwise': 0, 'threads': 2, 'region_origin': None, 'framing': 'letterbox', **task.get('settings', {})}
    require(set(settings) == {'dpi', 'clockwise', 'threads', 'model', 'region_origin', 'framing'}, 'Bind an explicit local visual model')
    require(type(settings['threads']) is int and 1 <= settings['threads'] <= 8, 'Invalid thread count')
    require(settings['framing'] in ('letterbox', 'center'), 'Unknown visual framing')
    model = Source(**settings['model'])
    require(len(model.sha256) == 64, 'Fingerprint the local safetensors model')
    return Recipe.create('visual', '1', settings=settings, preparation=RENDER_POLICY,
                         encoder='ViT-B-32', dimensions=512, device='cpu', mkldnn=False,
                         preprocessing={'size': 224, 'resize': 'bicubic-shortest-edge', 'crop': settings['framing'],
                                        'mean': [.48145466, .4578275, .40821073],
                                        'std': [.26862954, .26130258, .27577711]},
                         dependencies={**{n: importlib.metadata.version(n) for n in
                                       ('torch', 'torchvision', 'open_clip_torch', 'safetensors', 'timm', 'Pillow', 'numpy')},
                                       'pdftoppm':tool_version(['pdftoppm','-v'])})


def validate_inputs(roots, work, task, recipe):
    from ..receipts import read_origin
    from ..runner import page_from_record
    from ..contracts import Region
    settings=recipe.record()['parameters']['settings']; model_path(roots,settings)
    origin=settings['region_origin']
    if origin is not None:
        require(set(origin) in ({'key','figure','receipt_sha256'}, {'key','figure','receipt_sha256','evidence_sha256'},
                               {'key','block','receipt_sha256'}, {'key','block','receipt_sha256','evidence_sha256'}),'Invalid region origin')
        try:
            row,result,digest=read_origin(roots,work,origin,page_from_record(task['page']))
        except ValueError as error:
            raise ValueError('Layout origin: ' + str(error)) from error
        require(result.recipe.extractor in ('pdf-layout','scan-layout','document-layout') and digest==origin['receipt_sha256'],'Layout origin changed or belongs to another page')
        if result.recipe.extractor=='document-layout':
            require('block' in origin,'Document figure needs its block origin')
            figures=row['feature']['blocks'];index=origin['block']
            require(type(index) is int and 0<=index<len(figures) and figures[index]['label']=='figure',
                    'Document block is not a figure')
        else:
            require('figure' in origin,'Layout figure needs its figure origin')
            figures=row['feature']['figures'];index=origin['figure']
        require(type(index) is int and 0<=index<len(figures),'Unknown layout figure')
        require(task.get('region') and Region(**task['region'])==Region(**figures[index]['region']),
                'Visual crop differs from layout figure')


def model_path(roots, settings):
    return roots.source(Source(**settings['model']), verify_hash=True)


class Encoder:
    """One verified local model shared across a bounded batch; no remote resolver."""
    def __init__(self, roots, recipe):
        import open_clip
        import torch
        from safetensors.torch import load_file
        self.roots, self.recipe, self.torch = roots, recipe, torch
        self.config = recipe.record()['parameters']
        self.settings = self.config['settings']
        path = model_path(roots, self.settings)
        torch.set_num_threads(self.settings['threads'])
        torch.backends.mkldnn.enabled = False
        self.model, _, self.transform = open_clip.create_model_and_transforms(
            self.config['encoder'], pretrained=None, device='cpu',
            image_mean=self.config['preprocessing']['mean'], image_std=self.config['preprocessing']['std'])
        self.model.load_state_dict(load_file(str(path), device='cpu'), strict=True)
        self.model.eval()

    def encode_images(self, images):
        from PIL import Image, ImageOps
        from ..imaging import display_rgb
        tensors = []
        for image in images:
            rgb, _ = display_rgb(image)
            try:
                if self.config['preprocessing']['crop'] == 'letterbox':
                    padded = ImageOps.pad(rgb, (224, 224), method=Image.Resampling.BICUBIC,
                                          color=tuple(round(x*255) for x in self.config['preprocessing']['mean']))
                    rgb.close(); rgb = padded
                tensors.append(self.transform(rgb))
            finally:
                rgb.close()
        with self.torch.inference_mode():
            vectors = self.model.encode_image(self.torch.stack(tensors)).float()
            norms = vectors.norm(dim=1, keepdim=True)
            require(self.torch.isfinite(vectors).all().item() and (norms > 0).all().item(), 'Invalid visual embedding')
            vectors = vectors / norms
        self.roots.source(Source(**self.settings['model']))
        return vectors.numpy()


def space_id(recipe):
    import hashlib
    config = recipe.record()['parameters']
    space = {k:config[k] for k in ('encoder', 'dimensions', 'preprocessing', 'dependencies')}
    space['model_sha256'] = config['settings']['model']['sha256']
    if 'preparation' in config:
        space['preparation'] = config['preparation']
    return hashlib.sha256(encoded(space).encode()).hexdigest()


def extract(image_path, roots, recipe, encoder=None):
    from PIL import Image
    encoder = encoder or Encoder(roots, recipe)
    require(space_id(encoder.recipe) == space_id(recipe), 'Shared encoder belongs to another vector space')
    config = encoder.config; settings = encoder.settings
    with Image.open(image_path) as image:
        values = encoder.encode_images([image])[0].tolist()
    require(len(values) == config['dimensions'] and all(math.isfinite(v) for v in values),
            'Unexpected visual dimensions')
    result = {'encoder': config['encoder'], 'model_sha256': settings['model']['sha256'],
              'dimensions': len(values), 'vector': values, 'preprocess': str(encoder.transform),
              'framing': config['preprocessing']['crop'], 'identity_confirmed': False,
              'limitations': ['Similarity is a retrieval lead, not identity.',
                              'Small details need source-region views; whole-image scaling reduces their size.']}
    return (encoded(result)+'\n').encode(), result
