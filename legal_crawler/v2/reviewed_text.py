"""Apply explicitly reviewed OCR corrections bound to PDF and input text hashes.

The local review artifact is authored after reading the PDF, never by the OCR
service. Exact anchors prevent a stale review from modifying another OCR run.
"""
import hashlib
import json
from pathlib import Path


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def apply_review(text, digest, folder):
    path = Path(folder) / 'reviewed-corrections.json'
    if not path.exists():
        return text, None
    review = json.loads(path.read_text(encoding='utf-8'))
    if (review.get('pdf_sha256') != digest or
            review.get('input_text_sha256') != text_hash(text) or
            not review.get('reviewer') or not review.get('evidence')):
        raise ValueError('OCR review does not match this PDF/text or lacks attribution')
    patches = review.get('patches')
    if not isinstance(patches, list) or not patches:
        raise ValueError('OCR review has no corrections')
    for patch in patches:
        old, new = patch['old'], patch['new']
        if (not isinstance(old, str) or not old or not isinstance(new, str)
                or type(patch.get('count')) is not int or patch['count'] < 1
                or not patch.get('page') or not patch.get('reason')
                or text.count(old) != patch['count']):
            raise ValueError('OCR review anchor/count is invalid: ' + repr(old)[:100])
        text = text.replace(old, new)
    if review.get('output_text_sha256') != text_hash(text):
        raise ValueError('OCR review output checksum mismatch')
    return text, {'file': str(path), 'reviewer': review['reviewer'],
                  'evidence': review['evidence'], 'patches': len(patches),
                  'input_text_sha256': review['input_text_sha256'],
                  'output_text_sha256': text_hash(text)}
