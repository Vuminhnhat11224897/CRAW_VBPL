"""CRAWLER: page-level native/OCR routing, resumable evidence and review signals.

Only Vintern is used. Raw whole-page text is retained. Body verification never
replaces it; header repairs require exact source agreement and retain evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from . import ocr
from .ocr_quality import CONTENT_TARGET, comparison_text, page_issues
from .text import PDF_LOCK, pdf_text, read_document, validate_file

CACHE_SCHEMA = 1


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, suffix='.tmp', delete=False, encoding='utf-8') as f:
        temp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    temp.replace(path)


def _cache_read(path, kind):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema') != CACHE_SCHEMA:
            return None
        if kind == 'raw' and isinstance(data.get('text'), str):
            return data
        if kind == 'boxes' and isinstance(data.get('items'), list):
            if all(isinstance(i, (list, tuple)) and len(i) == 5 and isinstance(i[4], str) and
                   all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in i[:4]) for i in data['items']):
                return data
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        pass
    return None


class PageOCR:
    def __init__(self, cache, record=None):
        self.cache, self.record = cache, record
        self.configured = False
        self.checked = False

    def _configure(self):
        if self.configured:
            return
        try:
            self.scale = float(os.getenv('OCR_PDF_SCALE', '2.0'))
            self.review_scale = float(os.getenv('OCR_REVIEW_SCALE', '3.0'))
        except ValueError:
            raise ocr.OCRConfigurationError('OCR scales must be numbers') from None
        if not .5 <= self.scale <= 4 or not .5 <= self.review_scale <= 4:
            raise ocr.OCRConfigurationError('OCR scales must be between 0.5 and 4')
        self.retry_review = os.getenv('OCR_REVIEW_RETRY', 'yes').lower()
        if self.retry_review not in {'yes', 'no'}:
            raise ocr.OCRConfigurationError('OCR_REVIEW_RETRY must be yes or no')
        self.configured = True

    def _health(self):
        if not self.checked:
            ocr.health()
            self.checked = True

    def _raw(self, path, stem, scale):
        cached = _cache_read(self.cache / (stem + '.json'), 'raw')
        if cached is not None:
            return cached['text'], 0, True
        self._health()
        path = path() if callable(path) else path
        started = time.monotonic()
        text, _ = ocr.ocr_text(path, scale=scale, min_chars=0)
        elapsed = round(time.monotonic() - started, 3)
        _write_json(self.cache / (stem + '.json'), {'schema': CACHE_SCHEMA, 'text': text, 'seconds': elapsed})
        return text, elapsed, False

    def _boxes(self, path, stem):
        cached = _cache_read(self.cache / (stem + '.boxes.json'), 'boxes')
        if cached is not None:
            return cached['items']
        self._health()
        path = path() if callable(path) else path
        pages = ocr.ocr_boxes(path)
        if len(pages) != 1 or pages[0]['page'] != 1:
            raise ocr.OCRUnavailable('Single-page OCR returned unexpected page numbering')
        items = pages[0]['items']
        _write_json(self.cache / (stem + '.boxes.json'), {'schema': CACHE_SCHEMA, 'items': items})
        return items

    def __call__(self, page, number, reasons, want_dates):
        import pymupdf
        self._configure()
        stem = f'page-{number:04d}'
        with tempfile.TemporaryDirectory(prefix='legal-ocr-') as temp:
            original = Path(temp) / 'page.pdf'
            def original_file():
                # Cached runs do not need to copy the input PDF at all.
                if not original.exists():
                    with PDF_LOCK, pymupdf.open() as document:
                        document.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
                        document.save(original)
                return original
            text, seconds, cached = self._raw(original_file, stem, self.scale)
            with PDF_LOCK:
                native = page.get_text('text')
            issues = page_issues(text, native, record=self.record, first=number == 1)
            detail = {'method': 'ocr', 'routing_reasons': reasons, 'issues': issues,
                      'ocr_seconds': seconds, 'cached': cached, 'scale': self.scale,
                      'text_file': str(self.cache / (stem + '.txt'))}
            (self.cache / (stem + '.txt')).write_text(text, encoding='utf-8')
            if issues:
                # Keep an image even if an optional verification request fails.
                image = self.cache / (stem + '.png')
                if not image.exists():
                    with PDF_LOCK:
                        page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(image)
                detail['image_file'] = str(image)
                # A complete source record uses the stricter field-specific quarter reader below.
                identity_only = (self.record and self.record.get('number') and self.record.get('issued_date')
                                 and all(i.startswith('source_') for i in issues))
                if self.retry_review == 'yes' and not identity_only:
                    self._review(page, original_file, stem, text, detail)
            positions = self._boxes(original_file, stem) if want_dates else []
            _write_json(self.cache / (stem + '.report.json'), {'page': number, **detail})
            return text, detail, positions

    def _review(self, page, original, stem, baseline, detail):
        """One bounded verification read, localized when its position is known."""
        import pymupdf
        header_issue = any(i.startswith('source_') for i in detail['issues'])
        region = None
        if header_issue:
            with PDF_LOCK:
                region = pymupdf.Rect(0, 0, page.rect.width, page.rect.height * .45)
        elif 'table_layout' in detail['issues']:
            # Native vector tables have reliable geometry. Scanned tables may not.
            try:
                with PDF_LOCK:
                    tables = page.find_tables().tables
                    if tables:
                        region = pymupdf.Rect(tables[0].bbox) + (-8, -8, 8, 8)
                        region &= page.rect
            except (ValueError, RuntimeError):
                # Failure of optional localization must not discard a successful first read.
                pass
        region_key = hashlib.sha256(json.dumps(list(region)).encode()).hexdigest()[:12] if region is not None else ''
        candidate_stem = stem + ('.region-' + region_key if region is not None else '.verify')
        try:
            with tempfile.TemporaryDirectory(prefix='legal-ocr-review-') as temp:
                source = original
                if region is not None:
                    source = Path(temp) / 'region.pdf'
                    # Rasterize exactly the selected area; no text outside the crop can leak in.
                    with PDF_LOCK, pymupdf.open() as doc:
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.review_scale, self.review_scale), clip=region, alpha=False)
                        cropped = doc.new_page(width=region.width, height=region.height)
                        cropped.insert_image(cropped.rect, stream=pix.tobytes('png'))
                        doc.save(source)
                candidate, seconds, cached = self._raw(source, candidate_stem, self.review_scale)
                path = self.cache / (candidate_stem + '.txt')
                path.write_text(candidate, encoding='utf-8')
                detail['ocr_seconds'] += seconds
                detail['verification'] = {'text_file': str(path), 'cached': cached,
                                          'scope': 'region' if region is not None else 'page',
                                          'bounds': list(region) if region is not None else None,
                                          'scale': self.review_scale, 'accepted_automatically': False}
                if region is None and comparison_text(candidate) != comparison_text(baseline):
                    detail['issues'].append('verification_disagrees')
        except (ocr.OCRUnavailable, ValueError) as exc:
            # The original problem still requires review; do not lose the baseline evidence.
            detail['verification'] = {'error': str(exc)[:200], 'accepted_automatically': False}


def read(path, folder, *, record=None, progress=lambda *args: None):
    """Return (text, report, first/last-page items); no publishing or LLM calls."""
    path, folder = Path(path), Path(folder)
    if validate_file(path) != 'pdf':
        text, report = read_document(path, folder, progress)
        return text, report, []
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    profile = {'schema': CACHE_SCHEMA, 'file': digest, 'url': ocr.base_url(),
               'scale': os.getenv('OCR_PDF_SCALE', '2.0'), 'review_scale': os.getenv('OCR_REVIEW_SCALE', '3.0'),
               'revision': os.getenv('OCR_CACHE_VERSION', '1'), 'retry': os.getenv('OCR_REVIEW_RETRY', 'yes')}
    key = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:24]
    cache = folder / 'ocr' / key
    cache.mkdir(parents=True, exist_ok=True)
    from .reviewed_pages import reviewed_callback
    callback = reviewed_callback(PageOCR(cache, record), folder, digest)
    text, report = pdf_text(path, folder, progress, ocr_page=callback)
    positions = report.pop('date_pages')
    if record:
        from .ocr_identity import repair_header, verify
        identity = verify(path, folder, digest, record, report['page_reports'])
        report['identity'] = identity
        if identity['status'] == 'source_agreement':
            text, repairs = repair_header(text, identity)
            report['header_repairs'] = repairs
            first = report['page_reports'][0]
            first['issues'] = [i for i in first.get('issues', []) if not i.startswith('source_')]
        report['review_pages'] = [p['page'] for p in report['page_reports'] if p.get('issues')]
        if identity['issues'] and 1 not in report['review_pages']:
            report['review_pages'].insert(0, 1)
        report['warnings'] = [f"page_{p['page']}:{i}" for p in report['page_reports'] for i in p.get('issues', [])]
        report['warnings'].extend(identity['issues'])
        report['region_ocr_seconds'] = round(sum(r.get('seconds', 0) for r in identity['reads']), 3)
    report['content_accuracy_target'] = CONTENT_TARGET
    report['content_accuracy_certified'] = False
    report['content_sha256'] = digest
    report['quality_status'] = 'needs_review' if report['review_pages'] else 'no_issues_detected'
    report['evidence_dir'] = str(cache)
    if record:
        report['source_record'] = {k: record.get(k) for k in ('id', 'number', 'issued_date')}
    # Record-dependent quality decisions are always recomputed; only API output is cached.
    _write_json(folder / 'ocr-review.json', report)
    return text, report, positions
