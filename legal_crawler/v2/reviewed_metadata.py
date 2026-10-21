"""Explicit PDF review evidence for signatures, new issuers and OCR quote boundaries."""
import hashlib
import json
from pathlib import Path
from .reviewed_text import text_hash


def load_review(folder, digest, text):
    path = Path(folder) / 'reviewed-metadata.json'
    if not path.exists():
        return {}
    review = json.loads(path.read_text(encoding='utf-8'))
    if review.get('pdf_sha256') != digest or review.get('text_sha256') != text_hash(text) or not review.get('reviewer') or not review.get('reason'):
        raise ValueError('Metadata/structure review does not match PDF/text or lacks attribution')
    evidence = review.get('images')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('Metadata/structure review lacks visual evidence')
    for item in evidence:
        image = Path(item['path'])
        if hashlib.sha256(image.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Metadata/structure review evidence hash differs')
    resets = review.get('quote_resets', [])
    from .structure import normalize, structural_line, ARTICLE, APPENDIX
    lines = [structural_line(line) for line in normalize(text).splitlines()]
    if not isinstance(resets, list) or any(not isinstance(line, str) or lines.count(line) != 1
            or not (ARTICLE.match(line) or APPENDIX.match(line)) for line in resets):
        raise ValueError('Reviewed structural heading is missing, ambiguous or invalid')
    accepted = review.get('accepted_point_issues', [])
    if not isinstance(accepted, list) or any(not isinstance(item, str) or not item for item in accepted):
        raise ValueError('Invalid reviewed point issue')
    signature = review.get('signature')
    if signature is not None:
        if type(signature.get('page')) is not int or signature['page'] < 1 or not isinstance(signature.get('text'), str) or not signature['text'].strip():
            raise ValueError('Invalid reviewed signature')
        signature = {**signature, 'method': 'reviewed_pdf', 'review_file': str(path)}
    return {**review, 'signature': signature, 'file': str(path)}
