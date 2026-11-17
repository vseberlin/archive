"""Explicit display conversion shared by research consumers; originals stay intact."""
from .contracts import require


RENDER_POLICY = {'version': 3, 'unsigned_grayscale_16': 'linear-0-65535-to-0-255',
                 'min_is_white_16': 'inverted', 'greyscale_storage': 'L',
                 'other_high_depth': 'refuse', 'contrast_stretch': False}

GREY_MODES = ('1', 'L', 'LA')


def display_image(original):
    """Return an 8-bit L (greyscale sources) or RGB image plus a readable description.

    Convert before resizing/EXIF operations, while TIFF sample tags are available.
    uint16 greyscale is mapped linearly without clipping, in strips to avoid a
    full-size float copy; 16-bit MinIsWhite is inverted (Pillow only does this for
    8-bit). The observed value range is recorded, never stretched. This is a display
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
        inverted = tags.get(262) == 0
        grey = Image.new('L', original.size)
        low, top_value = 65535, 0
        try:
            for top in range(0, original.height, 128):
                with original.crop((0, top, original.width, min(top+128, original.height))) as strip:
                    with strip.convert('F') as floating:
                        a, b = floating.getextrema()
                        low, top_value = min(low, int(a)), max(top_value, int(b))
                        mapping = (lambda value: (65535 - value) / 257) if inverted else (lambda value: value / 257)
                        with floating.point(mapping) as scaled:
                            with scaled.convert('L') as eight:
                                grey.paste(eight, (0, top))
        except Exception:
            grey.close()
            raise
        note = (f'uint16 grayscale: linear 0..65535 to 0..255; observed {low}..{top_value}'
                + ('; MinIsWhite inverted' if inverted else '') + '; no contrast stretch')
        return grey, note
    require(not high and original.mode not in ('I', 'F') and not original.mode.startswith('I;'),
            'Unsupported high-depth image; explicit decoder/range required (refusing clipped RGB)')
    if original.mode in GREY_MODES:
        return original.convert('L'), '8-bit display conversion: ' + original.mode + ' to L'
    return original.convert('RGB'), '8-bit display conversion: ' + original.mode + ' to RGB'


def display_rgb(original):
    """RGB for consumers that need three channels (encoders, external requests)."""
    image, note = display_image(original)
    if image.mode == 'RGB':
        return image, note
    try:
        return image.convert('RGB'), note + '; L to RGB for consumer'
    finally:
        image.close()
