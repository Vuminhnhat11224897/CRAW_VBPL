"""Bounded, raster-only regional reads with persistent evidence."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from . import ocr
from .text import PDF_LOCK

REGIONS = {
    'number': (0, 0, .5, .5),
    'issue_date': (.5, 0, 1, .5),
    'signature': (0, .5, 1, 1),
    'number_line': (0, .06, .55, .28),
    'date_line': (.45, .06, 1, .28),
    'header': (0, 0, 1, .5),
    'left': (0, 0, .5, 1),
    'right': (.5, 0, 1, 1),
    'top': (0, 0, 1, .5),
    'bottom': (0, .5, 1, 1),
}


def crop_pdf(page, bounds, path, *, scale=3, zoom=2, image=None):
    """Coordinates refer to the displayed page, including rotation. No hidden text leaks."""
    import pymupdf
    if (len(bounds) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError('Invalid normalized crop bounds')
    with PDF_LOCK, pymupdf.open() as doc:
        rect = page.rect
        clip = pymupdf.Rect(bounds[0] * rect.width, bounds[1] * rect.height,
                            bounds[2] * rect.width, bounds[3] * rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        if image:
            pix.save(image)
        target = doc.new_page(width=clip.width * zoom, height=clip.height * zoom)
        target.insert_image(target.rect, stream=pix.tobytes('png'))
        doc.save(path)


class RegionOCR:
    def __init__(self, cache, digest, *, scale=None):
        self.root = Path(cache)
        self.digest = digest
        self.scale_input = scale

    def read(self, page, name, bounds=None):
        from .reader import _write_json
        try:
            scale = float(self.scale_input if self.scale_input is not None else os.getenv('OCR_REGION_SCALE', '3'))
        except (ValueError, TypeError):
            raise ocr.OCRConfigurationError('OCR_REGION_SCALE must be a number') from None
        if not .5 <= scale <= 4:
            raise ocr.OCRConfigurationError('OCR_REGION_SCALE must be between 0.5 and 4')
        bounds = tuple(bounds if bounds is not None else REGIONS[name])
        with PDF_LOCK:
            number = page.number + 1
        profile = {'schema': 1, 'digest': self.digest, 'url': ocr.base_url(), 'scale': scale,
                   'zoom': 2, 'page': number, 'bounds': bounds,
                   'revision': os.getenv('OCR_CACHE_VERSION', '1')}
        key = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:24]
        folder = self.root / key
        folder.mkdir(parents=True, exist_ok=True)
        result_file, image_file, text_file = folder / 'result.json', folder / 'crop.png', folder / 'text.txt'
        text, cached, seconds = None, False, 0
        try:
            saved = json.loads(result_file.read_text())
            if saved.get('profile') == json.loads(json.dumps(profile)) and isinstance(saved.get('text'), str):
                text, cached = saved['text'], True
        except (OSError, ValueError, AttributeError):
            pass
        if text is None or not image_file.exists():
            with tempfile.TemporaryDirectory(prefix='legal-ocr-crop-') as temp:
                source = Path(temp) / 'crop.pdf'
                crop_pdf(page, bounds, source, scale=scale, image=image_file)
                if text is None:
                    started = time.monotonic()
                    text, _ = ocr.ocr_text(source, scale=scale, min_chars=0)
                    seconds = round(time.monotonic() - started, 3)
                    _write_json(result_file, {'profile': profile, 'text': text, 'seconds': seconds})
        text_file.write_text(text, encoding='utf-8')
        return {'name': name, 'page': number, 'bounds': list(bounds), 'scale': scale,
                'zoom': 2, 'text': text, 'cached': cached, 'seconds': seconds,
                'image_file': str(image_file), 'text_file': str(text_file)}
