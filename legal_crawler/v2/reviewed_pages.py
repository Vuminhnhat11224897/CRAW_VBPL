"""Explicit visual page reviews, bound to the PDF, raw OCR and evidence image."""
import hashlib
import json
from pathlib import Path

from .ocr_quality import page_issues
from .reviewed_text import text_hash


def reviewed_callback(callback, folder, digest):
    path = Path(folder) / 'reviewed-pages.json'
    if not path.exists():
        return callback
    review = json.loads(path.read_text(encoding='utf-8'))
    if review.get('pdf_sha256') != digest or not review.get('reviewer'):
        raise ValueError('Page review PDF hash or reviewer is invalid')
    entries = review.get('pages')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Page review is empty')
    if any(type(e.get('page')) is not int or e['page'] < 1 for e in entries):
        raise ValueError('Invalid reviewed page number')
    pages = {e['page']: e for e in entries}
    if len(pages) != len(entries):
        raise ValueError('Duplicate page reviews')

    def read(page, number, reasons, want_dates):
        raw, detail, positions = callback(page, number, reasons, want_dates)
        if number not in pages:
            return raw, detail, positions
        entry = pages[number]
        text = entry.get('text')
        evidence = Path(entry.get('image', ''))
        approved = entry.get('accepted_issues')
        if (entry.get('raw_text_sha256') != text_hash(raw) or not isinstance(text, str)
                or entry.get('text_sha256') != text_hash(text) or not entry.get('reason')
                or not isinstance(approved, list) or any(not isinstance(i, str) for i in approved)
                or not evidence.is_file()
                or hashlib.sha256(evidence.read_bytes()).hexdigest() != entry.get('image_sha256')):
            raise ValueError('Page review does not match raw OCR/text/evidence')
        # Recompute checks after the correction; new problems are never silently waived.
        from .text import PDF_LOCK
        with PDF_LOCK:
            native = page.get_text('text')
        current = page_issues(text, native)
        if set(approved) - (set(current) | set(detail.get('issues', []))):
            raise ValueError('Page review names an unobserved issue')
        detail = {**detail, 'issues': [i for i in current if i not in approved],
                  'manual_review': {'file': str(path), 'reviewer': review['reviewer'],
                                    'reason': entry['reason'], 'image': str(evidence),
                                    'raw_text_sha256': text_hash(raw), 'text_sha256': text_hash(text),
                                    'accepted_issues': approved}}
        return text, detail, positions

    return read
