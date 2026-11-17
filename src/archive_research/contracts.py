"""Readable research records, separate from Master selection and catalogue authority."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def text(value):
    return isinstance(value, str) and bool(value.strip())


def relative_path(value):
    require(text(value) and '\\' not in value and '\x00' not in value,
            'Expected a relative POSIX path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and
            str(path) == value and value != '.', 'Path must be canonical and relative')


def digest(value):
    require(isinstance(value, str) and (not value or re.fullmatch('[0-9a-f]{64}', value)),
            'Expected an empty or lowercase SHA-256 value')


CACHE_KEY_VERSION = 2


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


@dataclass(frozen=True)
class Source:
    unit: str
    object_id: str
    representation: str
    root: str
    path: str
    size_bytes: int
    mtime_ns: int
    sha256: str = ''
    original_path: str = ''
    role: str = 'unspecified'

    def __post_init__(self):
        require(all(text(v) for v in (self.unit, self.object_id, self.representation, self.root)),
                'Supply existing unit, object and representation identities and a named root')
        relative_path(self.path)
        require(integer(self.size_bytes) and integer(self.mtime_ns), 'Invalid source stamp')
        digest(self.sha256)
        require(isinstance(self.original_path, str), 'Invalid provenance path')
        require(text(self.role), 'Explicit source role required')

    @property
    def stamp(self):
        return {'size_bytes': self.size_bytes, 'mtime_ns': self.mtime_ns}


@dataclass(frozen=True)
class Page:
    source: Source
    member: int
    kind: str
    index: int
    width: int | float
    height: int | float
    logical_page: int | None = None
    printed_label: str = ''
    units: str = 'pixels'
    origin: str = 'top-left'

    def __post_init__(self):
        require(isinstance(self.source, Source), 'Expected a bound source')
        require(integer(self.member, 1), 'Member position must be supplied, starting at one')
        require(self.kind in ('pdf', 'tiff', 'image'), 'Unsupported page kind')
        require(integer(self.index, 1 if self.kind == 'pdf' else 0), 'Invalid page/frame index')
        require(self.kind != 'image' or self.index == 0, 'A single image uses index zero')
        require(self.units in ('pixels', 'pdf-points') and self.origin == 'top-left',
                'Unsupported source coordinate space')
        require(self.units != 'pdf-points' or self.kind == 'pdf', 'PDF points require a PDF page')
        require(all(type(v) in (int, float) and math.isfinite(v) and v > 0
                    for v in (self.width, self.height)), 'Source dimensions required')
        require(self.units != 'pixels' or (integer(self.width, 1) and integer(self.height, 1)),
                'Pixel dimensions must be integers')
        require(self.logical_page is None or integer(self.logical_page, 1), 'Invalid logical page')
        require(isinstance(self.printed_label, str), 'Invalid printed page label')


@dataclass(frozen=True)
class Region:
    x: float
    y: float
    width: float
    height: float
    units: str = 'normalized'
    origin: str = 'top-left'

    def __post_init__(self):
        values = (self.x, self.y, self.width, self.height)
        require(all(type(v) in (int, float) and math.isfinite(v) for v in values),
                'Region coordinates must be finite numbers')
        require(self.units == 'normalized' and self.origin == 'top-left', 'Unsupported region space')
        require(self.x >= 0 and self.y >= 0 and self.width > 0 and self.height > 0 and
                self.x + self.width <= 1 and self.y + self.height <= 1, 'Region outside source')


@dataclass(frozen=True)
class Transform:
    width: int | float
    height: int | float
    # Maps processed pixel (x, y) to Page.units: (a*x+b*y+c, d*x+e*y+f).
    to_source: tuple | None = None
    steps: tuple = ()
    units: str = 'pixels'

    def __post_init__(self):
        require(self.units in ('pixels', 'pdf-points'), 'Unsupported processed coordinate units')
        require(all(type(v) in (int, float) and math.isfinite(v) and v > 0
                    for v in (self.width, self.height)), 'Processed dimensions required')
        require(self.units != 'pixels' or (integer(self.width, 1) and integer(self.height, 1)),
                'Processed pixel dimensions must be integers')
        require(isinstance(self.steps, tuple) and all(text(s) for s in self.steps),
                'Transform steps must be immutable descriptions')
        if self.to_source is not None:
            require(isinstance(self.to_source, tuple) and len(self.to_source) == 6 and
                    all(type(v) in (int, float) and math.isfinite(v) for v in self.to_source),
                    'Expected six finite affine coefficients')
            a, b, _, d, e, _ = self.to_source
            require(a * e - b * d != 0, 'Singular coordinate transform')


@dataclass(frozen=True)
class Recipe:
    extractor: str
    version: str
    parameters_json: str

    def __post_init__(self):
        require(text(self.extractor) and text(self.version), 'Extractor name/version required')
        parameters = json.loads(self.parameters_json)
        require(isinstance(parameters, dict), 'Recipe parameters must be a JSON object')
        object.__setattr__(self, 'parameters_json', encoded(parameters))

    @classmethod
    def create(cls, extractor, version, **parameters):
        # Capture settings now so later caller mutations cannot change a cache key.
        return cls(extractor, version, encoded(parameters))

    def record(self):
        return {'extractor': self.extractor, 'version': self.version,
                'parameters': json.loads(self.parameters_json)}

    def cache_key(self, page, region=None, transform=None, *, version=CACHE_KEY_VERSION):
        """Version 2 binds identity and content, not today's locator or stamp.

        Version 1 (full page record, including path, root, mtime and descriptive
        labels) is kept only to find receipts written before version 2.
        """
        require(isinstance(page, Page), 'Cache keys require an explicit page/frame binding')
        require(region is None or isinstance(region, Region), 'Invalid region')
        require(transform is None or isinstance(transform, Transform), 'Invalid transform')
        require(version in (1, 2), 'Unknown cache key version')
        payload = {'page': asdict(page) if version == 1 else page_binding(page), 'recipe': self.record(),
                   'region': asdict(region) if region else None,
                   'transform': asdict(transform) if transform else None}
        if version == 2:
            payload['key_version'] = 2
        return hashlib.sha256(encoded(payload).encode()).hexdigest()

    def key_matches(self, page, region, key):
        """A receipt may be filed under the current key or a pre-version-2 key."""
        return key in (self.cache_key(page, region), self.cache_key(page, region, version=1))


def page_binding(page):
    """What an extraction result depends on: identity, content and page geometry.

    With a stored SHA-256 the locator (root, path), stamp, provenance path, role and
    printed labels are excluded, so touching, copying, moving or relabelling a file
    keeps its receipts. Without a hash the locator and stamp remain the only content
    evidence and stay bound.
    """
    source = page.source
    content = ({'sha256': source.sha256} if source.sha256 else
               {'root': source.root, 'path': source.path, 'size_bytes': source.size_bytes,
                'mtime_ns': source.mtime_ns})
    return {'unit': source.unit, 'object_id': source.object_id, 'representation': source.representation,
            'content': content, 'member': page.member, 'kind': page.kind, 'index': page.index,
            'width': page.width, 'height': page.height, 'units': page.units, 'origin': page.origin}


@dataclass(frozen=True)
class Artifact:
    root: str
    path: str
    kind: str
    sha256: str = ''

    def __post_init__(self):
        require(text(self.root) and text(self.kind), 'Artifact root and kind required')
        relative_path(self.path)
        digest(self.sha256)


@dataclass(frozen=True)
class Result:
    page: Page
    recipe: Recipe
    status: str
    elapsed_seconds: float
    artifacts: tuple = ()
    region: Region | None = None
    transform: Transform | None = None
    limitations: tuple = ()

    def __post_init__(self):
        require(isinstance(self.page, Page) and isinstance(self.recipe, Recipe), 'Invalid result binding')
        require(self.status in ('complete', 'partial', 'error', 'changed_source'), 'Invalid extraction outcome')
        require(type(self.elapsed_seconds) in (int, float) and math.isfinite(self.elapsed_seconds)
                and self.elapsed_seconds >= 0, 'Invalid elapsed time')
        require(isinstance(self.artifacts, tuple) and all(isinstance(a, Artifact) for a in self.artifacts),
                'Invalid result artifacts')
        require(self.region is None or isinstance(self.region, Region), 'Invalid region')
        require(self.transform is None or isinstance(self.transform, Transform), 'Invalid transform')
        require(isinstance(self.limitations, tuple) and all(text(s) for s in self.limitations),
                'Limitations must be immutable descriptions')
        require(self.status == 'complete' or bool(self.limitations), 'Incomplete outcomes require a reason')

    def record(self):
        value = asdict(self)
        value['recipe'] = self.recipe.record()
        value['format'] = 'archive-research-result-1'
        value['metadata_adopted'] = False
        # Return JSON-native lists, not tuples, without exposing mutable internals.
        return json.loads(encoded(value))

    @classmethod
    def from_record(cls, value):
        value = dict(value)
        require(value.pop('format') == 'archive-research-result-1', 'Unsupported result format')
        require(value.pop('metadata_adopted') is False, 'Research results cannot adopt metadata')
        page = dict(value.pop('page'))
        page['source'] = Source(**page['source'])
        recipe = dict(value.pop('recipe'))
        require(set(recipe) == {'extractor', 'version', 'parameters'}, 'Unexpected recipe fields')
        recipe = Recipe(recipe['extractor'], recipe['version'], encoded(recipe['parameters']))
        region = value.pop('region')
        transform = value.pop('transform')
        if transform:
            transform = dict(transform)
            transform['steps'] = tuple(transform['steps'])
            if transform['to_source'] is not None:
                transform['to_source'] = tuple(transform['to_source'])
            transform = Transform(**transform)
        return cls(page=Page(**page), recipe=recipe,
                   artifacts=tuple(Artifact(**a) for a in value.pop('artifacts')),
                   region=Region(**region) if region else None, transform=transform,
                   limitations=tuple(value.pop('limitations')), **value)
