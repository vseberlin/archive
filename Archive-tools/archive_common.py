"""Compatibility helpers; shared guards live in archive_research.common."""
from archive_research.common import *

def preview(path, page, edge, as_text=False):
    """Render a requested page in memory; no archive derivative is created."""
    if path.suffix.lower() == '.pdf':
        args = (['pdftotext', '-f', str(page), '-l', str(page), str(path), '-'] if as_text else
                ['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile',
                 '-scale-to', str(edge), '-png', str(path)])
        result = subprocess.run(args, capture_output=True, timeout=30, check=False)
        require(result.returncode == 0 and (as_text or result.stdout), 'PDF preview failed')
        return (result.stdout[:200000], 'text/plain; charset=utf-8') if as_text else (result.stdout, 'image/png')
    require(not as_text, 'Embedded text is available only for PDF files')
    if path.suffix.lower() in ('.tif', '.tiff') and page == 1:
        from matching import comparison_buffer
        image, _ = comparison_buffer(path, edge, prefer_thumbnail=False)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        return output.getvalue(), 'image/jpeg'
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        require(page <= getattr(image, 'n_frames', 1), 'Page is not present in source')
        image.seek(page - 1)
        image.thumbnail((edge, edge))
        image = ImageOps.exif_transpose(image).convert('RGB')
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        return output.getvalue(), 'image/jpeg'
