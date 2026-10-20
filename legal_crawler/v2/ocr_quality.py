"""Signals for review, never a spelling corrector or an OCR accuracy score."""
from collections import Counter
import re
import unicodedata

from .contracts import number_key
from .dates import dates_in
from .text import content_length


def comparison_text(text):
    text = unicodedata.normalize('NFKC', text)
    text = text.translate(str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"}))
    return ' '.join(re.sub(r'[*`#]', '', text).split())


def page_issues(text, native='', *, record=None, first=False):
    """Only flag observable problems. Matching two reads is not verification."""
    issues = []
    if content_length(text) < 50:
        issues.append('insufficient_page_text')
    if '\ufffd' in text or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        issues.append('invalid_characters')
    paragraphs = [comparison_text(p) for p in re.split(r'\n\s*\n', text)]
    repeated = Counter(p for p in paragraphs if len(p) >= 100)
    if any(n > 1 for n in repeated.values()):
        issues.append('repeated_paragraph')
    # Also catch duplicated long lines from a model that does not emit blank lines.
    lines = Counter(comparison_text(line) for line in text.splitlines() if len(line.strip()) >= 100)
    if any(n > 1 for n in lines.values()):
        issues.append('repeated_line')
    if re.search(r'(?im)^\s*\|.*\|\s*$', text):
        issues.append('table_layout')
    if re.search(r'\bcông thức\b', text, re.I) and re.search(r'=|\d\s*%|\\frac', text):
        issues.append('formula_layout')
    valid = sum(c.isalnum() or c.isspace() or c in '.,;:()/-' for c in native) / max(1, len(native))
    if content_length(native) >= 100 and valid >= .85:
        reference = Counter(re.findall(r'\w+', comparison_text(native)))
        actual = Counter(re.findall(r'\w+', comparison_text(text)))
        missing = sum((reference - actual).values())
        if missing / max(1, sum(reference.values())) > .15:
            issues.append('native_text_missing')
        if Counter(re.findall(r'\d+', native)) - Counter(re.findall(r'\d+', text)):
            issues.append('native_numbers_missing')
    if first and record:
        # Fold the Cyrillic look-alikes used on some official source pages.
        from ..es import fold
        expected = number_key(fold(record.get('number')))
        actual = number_key(fold(comparison_text(text)))
        if expected and expected not in actual:
            issues.append('source_number_missing')
        issued = record.get('issued_date')
        if issued and issued not in dates_in(text):
            issues.append('source_issue_date_missing')
    return list(dict.fromkeys(issues))
