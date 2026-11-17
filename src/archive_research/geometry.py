"""Orthogonal pixel/source transforms; regions always refer to the original page."""
from .contracts import Region, Transform, require


def inverse_rotation(width, height, rendered_width, rendered_height, clockwise, units='pixels'):
    require(clockwise in (0, 90, 180, 270), 'Only verified orthogonal rotations are supported')
    w, h = rendered_width, rendered_height
    matrices = {
        0: (width / w, 0, 0, 0, height / h, 0),
        90: (0, width / h, 0, -height / w, 0, height),
        180: (-width / w, 0, width, 0, -height / h, height),
        270: (0, -width / h, width, height / w, 0, 0),
    }
    return Transform(w, h, matrices[clockwise], (f'orthogonal clockwise rotation {clockwise}',), units)


def source_region(box, transform, page):
    require(transform.to_source is not None, 'Unknown inverse transform')
    x, y, width, height = box
    require(width > 0 and height > 0, 'Empty word box')
    a, b, c, d, e, f = transform.to_source
    points = [(a * px + b * py + c, d * px + e * py + f)
              for px, py in ((x, y), (x + width, y), (x, y + height), (x + width, y + height))]
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    # Small renderer rounding is clipped; a materially out-of-page box is an error.
    require(x0 >= -1 and y0 >= -1 and x1 <= page.width + 1 and y1 <= page.height + 1,
            'Word box lies outside its source page')
    x0, x1 = max(0, x0 / page.width), min(1, x1 / page.width)
    y0, y1 = max(0, y0 / page.height), min(1, y1 / page.height)
    return Region(x0, y0, x1 - x0, y1 - y0)


def displayed_region(region, clockwise):
    x, y, w, h = region.x, region.y, region.width, region.height
    values = {0: (x, y, w, h), 90: (1-y-h, x, h, w),
              180: (1-x-w, 1-y-h, w, h), 270: (y, 1-x-w, h, w)}
    require(clockwise in values, 'Unsupported display rotation')
    return values[clockwise]


def processed_region(region, transform, page):
    """Map a source region into a prepared image, including crop offsets."""
    require(transform is not None and transform.to_source is not None,
            'Unknown inverse transform')
    a, b, c, d, e, f = transform.to_source
    determinant = a * e - b * d
    points = []
    for x, y in ((region.x, region.y), (region.x + region.width, region.y),
                 (region.x, region.y + region.height),
                 (region.x + region.width, region.y + region.height)):
        sx, sy = x * page.width - c, y * page.height - f
        points.append(((e * sx - b * sy) / determinant,
                       (-d * sx + a * sy) / determinant))
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    require(x0 >= -1 and y0 >= -1 and x1 <= transform.width + 1
            and y1 <= transform.height + 1, 'Region outside prepared image')
    x0, x1 = max(0, x0 / transform.width), min(1, x1 / transform.width)
    y0, y1 = max(0, y0 / transform.height), min(1, y1 / transform.height)
    return Region(x0, y0, x1 - x0, y1 - y0)
