"""Explicit display conversion shared by research consumers; originals stay intact."""
from .contracts import require


RENDER_POLICY = {'version': 2, 'unsigned_grayscale_16': 'linear-0-65535-to-0-255',
                 'other_high_depth': 'refuse', 'contrast_stretch': False}


def display_rgb(original):
    """Return RGB plus a readable conversion description, without clipping uint16.

    Convert before resizing/EXIF operations, while TIFF sample tags are available.
    Work in strips to avoid an additional full-size float image. This is a display
    derivative, not an archival conversion or automatic contrast enhancement.
    """
    from PIL import Image
    tags = getattr(original, 'tag_v2', {})
    bits = tags.get(258, ())
    bits = (bits,) if isinstance(bits, int) else tuple(bits)
    sample = tags.get(339, (1,))
    sample = (sample,) if isinstance(sample, int) else tuple(sample)
    high = max(bits, default=8) > 8
    unsigned16 = original.mode in ('I;16', 'I;16L', 'I;16B', 'I;16N') or (
        original.mode == 'I' and bits == (16,) and sample == (1,))
    if unsigned16:
        require(not bits or bits == (16,), 'Unsupported high-depth channel layout')
        require(sample == (1,), 'Signed/float samples need an explicit display range')
        grey = Image.new('L', original.size)
        try:
            for top in range(0, original.height, 128):
                with original.crop((0, top, original.width, min(top+128, original.height))) as strip:
                    with strip.convert('F') as floating:
                        with floating.point(lambda value: value / 257) as scaled:
                            with scaled.convert('L') as eight:
                                grey.paste(eight, (0, top))
            return grey.convert('RGB'), 'uint16 grayscale: linear 0..65535 to 0..255; no contrast stretch'
        finally:
            grey.close()
    require(not high and original.mode not in ('I', 'F') and not original.mode.startswith('I;'),
            'Unsupported high-depth image; explicit decoder/range required (refusing clipped RGB)')
    return original.convert('RGB'), '8-bit display conversion: ' + original.mode + ' to RGB'
