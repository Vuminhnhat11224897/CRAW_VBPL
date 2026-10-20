"""Read the document's own header; citations and Gazette stamps are not identity."""
import re
import unicodedata

from ..es import fold
from .contracts import number_key
from .dates import PLACE_DATE, VN, dates_in

NUMBER_LINE = re.compile(
    r'(?:^|[\s|])Số\s*[:：]\s*'
    r'([A-ZĐ0-9]+(?:\s*/\s*[A-ZĐ0-9]+){1,3}(?:\s*[-–—]\s*[A-ZĐ0-9]+)*)', re.I)


def signature_page_hint(text):
    text = unicodedata.normalize('NFC', text)
    return bool(re.search(r'Nơi\s*nhận\s*:|XÁC THỰC VĂN BẢN HỢP NHẤT|(?m:^\s*(?:KT\.|TM\.|TL\.|TUQ\.))', text, re.I))


def signature_usable(text):
    if re.search(r'Chỉ trả về nguyên văn|Không tự sửa lỗi chính tả|Không thêm lời dẫn', text, re.I):
        return False
    return bool(re.search(
        r'KT\.|TM\.|TL\.|TUQ\.|Ngày ký|Thời gian ký|'
        r'(?m:^\s*(?:BỘ TRƯỞNG|THỨ TRƯỞNG|PHÓ THỦ TƯỚNG|PHÓ THỐNG ĐỐC|THỐNG ĐỐC|THỦ TƯỚNG|CHỦ TỊCH|GIÁM ĐỐC)\s*$)',
        text, re.I))


def read_signature(regional, page):
    bottom = regional.read(page, 'signature')
    reads = [bottom]
    chosen = bottom
    if not signature_usable(bottom['text']):
        # A short act may finish in the upper half. Retain the failed bottom read too.
        chosen = regional.read(page, 'signature_upper', (0, 0, 1, .55))
        reads.append(chosen)
    if not signature_usable(chosen['text']):
        chosen = regional.read(page, 'signature_right', (.45, 0, 1, 1))
        reads.append(chosen)
    chosen = {**chosen, 'usable': signature_usable(chosen['text'])}
    return chosen, reads


def header_fields(text):
    text = unicodedata.normalize('NFC', fold(text or ''))
    # Own header must precede the legal bases; a cited document cannot verify identity.
    text = re.split(r'Căn cứ', text, maxsplit=1, flags=re.I)[0][:4500]
    text = re.sub(r'[*`#]', '', text)
    numbers = []
    for m in NUMBER_LINE.finditer(text):
        value = re.sub(r'\s+', '', m[1]).replace('–', '-').replace('—', '-')
        numbers.append({'value': value, 'quote': m[0].strip()})
    issued = []
    for m in PLACE_DATE.finditer(text):
        values = dates_in(m[0])
        if len(values) == 1:
            issued.append({'value': values[0], 'quote': m[0].strip()})
    return {'number': numbers, 'issued_date': issued}


def field_matches(field, value, expected):
    if not expected:
        return False
    return number_key(fold(value)) == number_key(fold(expected)) if field == 'number' else value == expected


def choose(field, observations, expected):
    """No fuzzy matching, digit correction, majority vote or source-value injection."""
    matches = [o for o in observations if field_matches(field, o['value'], expected)]
    if not expected:
        return {'status': 'reference_missing', 'value': None, 'observations': observations}
    if not matches:
        return {'status': 'conflict' if observations else 'unreadable', 'value': None,
                'expected': expected, 'observations': observations}
    # Within a single read, two different own-header candidates are ambiguous.
    origin = matches[-1].get('origin')
    same_read = [o for o in observations if o.get('origin') == origin]
    if any(not field_matches(field, o['value'], expected) for o in same_read):
        return {'status': 'ambiguous', 'value': None, 'expected': expected, 'observations': observations}
    return {'status': 'source_agreement', 'value': matches[-1]['value'],
            'evidence': matches[-1], 'observations': observations}


def repair_header(text, identity):
    """Only replace the own header, using an actual matching crop; preserve raw OCR in cache."""
    if identity.get('status') != 'source_agreement':
        return text, []
    cut = re.search(r'Căn cứ', text, re.I)
    limit = min(cut.start() if cut else len(text), 4500)
    head, tail = text[:limit], text[limit:]
    repairs = []
    fields = header_fields(head)
    for field in ('number', 'issued_date'):
        verified = identity[field]
        value = verified['value']
        if any(field_matches(field, o['value'], value) for o in fields[field]):
            continue
        if field == 'number':
            match = NUMBER_LINE.search(head)
            replacement = value
            span = match.span(1) if match else None
        else:
            match = PLACE_DATE.search(head)
            date_match = VN.search(match[0]) if match else None
            y, m, d = value.split('-')
            replacement = f'ngày {d} tháng {m} năm {y}'
            span = (match.start() + date_match.start(), match.start() + date_match.end()) if date_match else None
        if span:
            before = head[span[0]:span[1]]
            head = head[:span[0]] + replacement + head[span[1]:]
        else:
            before = None
            # The source was actually read from a crop, not copied from web metadata.
            head = (f'Số: {value}' if field == 'number' else verified['evidence']['quote']) + '\n' + head
        repairs.append({'field': field, 'before': before, 'after': replacement,
                        'evidence': verified['evidence']})
    return head + tail, repairs


def verify(path, folder, digest, record, page_reports):
    """Native/whole read first; on conflict zoom the requested quarter, then a tighter strip."""
    from pathlib import Path
    import pymupdf
    from . import ocr
    from .ocr_regions import RegionOCR
    from .text import PDF_LOCK

    record = record or {}
    regional = RegionOCR(Path(folder) / 'ocr-regions', digest)
    with PDF_LOCK:
        doc = pymupdf.open(path)
        first = doc[0]
        if page_reports[0].get('text_file'):
            initial = Path(page_reports[0]['text_file']).read_text()
        else:
            initial = first.get_text('text', sort=True)
    try:
        fields = header_fields(initial)
        observations = {f: [{**o, 'origin': 'first_page', 'page': 1} for o in fields[f]] for f in fields}
        result = {'status': 'needs_review', 'issues': [], 'reads': [], 'signing_date': None}
        attachment = not fields['number'] and bool(re.search(
            r'(?im)^\s*(?:Phụ\s+lục\b|QCVN\s*[:\d])', initial[:1000]))
        complete_reference = bool(record.get('number') and record.get('issued_date'))
        for field, regions in [('number', ('number', 'number_line', 'header')),
                               ('issued_date', ('issue_date', 'date_line', 'header'))]:
            decision = choose(field, observations[field], record.get(field))
            if complete_reference and not attachment and decision['status'] != 'source_agreement':
                for name in regions:
                    try:
                        read = regional.read(first, name)
                    except (ocr.OCRUnavailable, ocr.OCRRejected) as exc:
                        result['reads'].append({'name': name, 'page': 1, 'error': str(exc)[:200]})
                        break
                    result['reads'].append(read)
                    for item in header_fields(read['text'])[field]:
                        observations[field].append({**item, 'origin': name, 'page': 1,
                                                    'image_file': read['image_file'],
                                                    'text_file': read['text_file']})
                    decision = choose(field, observations[field], record.get(field))
                    if decision['status'] == 'source_agreement':
                        break
            result[field] = decision
            if decision['status'] != 'source_agreement':
                result['issues'].append('identity_' + field + '_' + decision['status'])
        if attachment:
            result['issues'].append('primary_act_missing')
        if not result['issues']:
            result['status'] = 'source_agreement'
        # Locate the primary signature before appendices, rather than assuming PDF last page.
        if not attachment and complete_reference:
            for item in page_reports:
                with PDF_LOCK:
                    page = doc[item['page'] - 1]
                    body = (Path(item['text_file']).read_text() if item.get('text_file') else page.get_text('text'))
                if not signature_page_hint(body):
                    continue
                if item['method'] == 'ocr':
                    try:
                        signature, reads = read_signature(regional, page)
                        result['reads'].extend(reads)
                    except (ocr.OCRUnavailable, ocr.OCRRejected) as exc:
                        result['signature'] = {'page': item['page'], 'error': str(exc)[:200]}
                        break
                else:
                    with PDF_LOCK:
                        w, h = page.rect.width, page.rect.height
                        text = page.get_text('text', clip=pymupdf.Rect(0, h / 2, w, h))
                    signature = {'page': item['page'], 'bounds': [0, .5, 1, 1],
                                 'text': text, 'method': 'text_layer'}
                    if not signature_usable(text):
                        with PDF_LOCK:
                            signature['text'] = page.get_text('text', clip=pymupdf.Rect(0, 0, w, h * .55))
                        signature['bounds'] = [0, 0, 1, .55]
                    signature['usable'] = signature_usable(signature['text'])
                result['signature'] = signature
                # Never turn a publishing/SAO Y stamp into an authoritative signing date.
                # Without a separate reference the candidate remains absent from canonical metadata.
                stamp_dates = []
                for line in signature['text'].splitlines() if signature['usable'] else []:
                    if re.search(r'Ngày ký|Thời gian ký|Ký ngày', line, re.I):
                        stamp_dates.extend(dates_in(line))
                result['signing_date_candidates'] = list(dict.fromkeys(stamp_dates))
                expected = record.get('signing_date')
                if expected and result['signing_date_candidates'] == [expected]:
                    result['signing_date'] = {'date': expected, 'page': item['page'],
                                              'line': signature['text'][:200], 'status': 'source_agreement'}
                elif expected:
                    result['issues'].append('identity_signing_date_unverified')
                    result['status'] = 'needs_review'
                break
        if record.get('signing_date') and result['signing_date'] is None:
            if 'identity_signing_date_unverified' not in result['issues']:
                result['issues'].append('identity_signing_date_unverified')
            result['status'] = 'needs_review'
        return result
    finally:
        with PDF_LOCK:
            doc.close()
