"""Bounded inspection and rendering; optional Pillow is imported only when used."""
import re
import subprocess

from .contracts import Page, Transform, require
from .geometry import inverse_rotation, processed_region


def pdf_info(path, index):
    process = subprocess.run(['pdfinfo', '-f', str(index), '-l', str(index), str(path)],
                             capture_output=True, text=True, timeout=60)
    require(process.returncode == 0, 'PDF inspection failed: ' + process.stderr[-400:])
    count = re.search(r'^Pages:\s+(\d+)', process.stdout, re.M)
    size = re.search(r'Page\s+(?:\d+\s+)?size:\s+([\d.]+) x ([\d.]+) pts', process.stdout)
    rotation = re.search(r'Page\s+(?:\d+\s+)?rot:\s+(-?\d+)', process.stdout)
    require(count and size and rotation, 'PDF page geometry unavailable')
    require(1 <= index <= int(count[1]), 'PDF page outside source')
    angle = int(rotation[1]) % 360
    require(angle in (0, 90, 180, 270), 'Unsupported embedded PDF rotation')
    return float(size[1]), float(size[2]), angle, int(count[1])


def inspect_page(roots, source, member, kind, index, logical_page=None, printed_label=''):
    path = roots.source(source)
    if kind == 'pdf':
        width, height, _, _ = pdf_info(path, index)
        page = Page(source, member, kind, index, width, height, logical_page, printed_label, 'pdf-points')
    else:
        from PIL import Image
        with Image.open(path) as image:
            require(0 <= index < getattr(image, 'n_frames', 1), 'Image frame outside source')
            image.seek(index)
            require(image.getexif().get(274, 1) == 1,
                    'Nontrivial image EXIF orientation needs a verified adapter')
            width, height = image.size
        page = Page(source, member, kind, index, width, height, logical_page, printed_label)
    roots.source(source)
    return page


def render(roots, page, directory, *, dpi=300, clockwise=0, region=None):
    from PIL import Image
    require(type(dpi) is int and 72 <= dpi <= 600, 'DPI outside bounded trial range')
    require(clockwise in (0, 90, 180, 270), 'Unsupported editorial rotation')
    path = roots.source(page.source)
    embedded_rotation = 0
    if page.kind == 'pdf':
        width, height, embedded_rotation, _ = pdf_info(path, page.index)
        require(abs(width-page.width) < .01 and abs(height-page.height) < .01,
                'PDF geometry differs from bound page')
        target = directory / 'render'
        process = subprocess.run(['pdftoppm', '-f', str(page.index), '-l', str(page.index),
                                  '-singlefile', '-r', str(dpi), '-png', str(path), str(target)],
                                 capture_output=True, timeout=180)
        require(process.returncode == 0, 'PDF render failed: ' + process.stderr.decode(errors='replace')[-400:])
        with Image.open(target.with_suffix('.png')) as original:
            image = original.convert('RGB')
    else:
        with Image.open(path) as original:
            original.seek(page.index)
            require(original.getexif().get(274, 1) == 1, 'Unverified EXIF orientation')
            require(original.size == (page.width, page.height), 'Source image geometry changed')
            image = original.convert('RGB')
    try:
        if clockwise:
            rotated = image.rotate(-clockwise, expand=True)
            image.close()
            image = rotated
        transform = inverse_rotation(page.width, page.height, *image.size,
                                     (embedded_rotation + clockwise) % 360)
        if region is not None:
            import math
            shown = processed_region(region, transform, page)
            # Avoid adding a pixel for floating-point noise at an integer edge.
            left, top = math.floor(shown.x * image.width + 1e-9), math.floor(shown.y * image.height + 1e-9)
            right = min(image.width, math.ceil((shown.x + shown.width) * image.width - 1e-9))
            bottom = min(image.height, math.ceil((shown.y + shown.height) * image.height - 1e-9))
            cropped = image.crop((left, top, right, bottom))
            image.close()
            image = cropped
            a, b, c, d, e, f = transform.to_source
            transform = Transform(*image.size, (a, b, c + a*left + b*top,
                                                d, e, f + d*left + e*top),
                                  transform.steps + ('explicit source-region crop; outward pixel rounding',))
        target = directory / 'processed.png'
        image.save(target)
    finally:
        image.close()
    roots.source(page.source)
    return target, transform, (embedded_rotation + clockwise) % 360
