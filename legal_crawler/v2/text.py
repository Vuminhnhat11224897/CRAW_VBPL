"""Bounded PDF/DOCX text reading, identical to the v2 writer's native (non-OCR) path."""
# Vendored from data_vbpl (legal_ingest) commit 22d9796. Keep logic identical to the ES v2 writer;
# only the changes marked "CRAWLER:" differ. See docs/adr/0001-v3-cluster-v2-schema.md.

import os
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

from .structure import normalize

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


class NeedsOCR(ValueError):
    def __init__(self, page):
        super().__init__(f'Page {page} needs OCR (scan, multi-column or unreadable text layer)')
        self.page = page


def content_length(text):
    # Ignore common digital-signature labels when measuring useful page text.
    body = re.sub(r"(?im)^[ \t]*(?:Người ký|Cơ quan phát hành|Cơ quan|Email|Thời gian ký)[ \t]*:.*$", "", text)
    return len(" ".join(body.split()))


def xml(archive, path):
    if path not in archive.namelist():
        return ET.Element('empty')
    raw = archive.read(path)
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ValueError('XML entities are not allowed')
    try:return ET.fromstring(raw)
    except ET.ParseError as error:raise ValueError('DOCX contains invalid XML') from error


def validate_file(path):
    suffix = path.suffix.lower()
    with path.open('rb') as stream:
        prefix = stream.read(1024)
    if suffix == '.pdf' and b'%PDF-' in prefix:
        return 'pdf'
    if suffix == '.doc' and prefix.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        return 'doc'
    if suffix == '.docx' and prefix.startswith(b'PK'):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(x.file_size for x in infos) > 200 * 1024 * 1024:
                raise ValueError('DOCX decompressed size exceeds 200 MiB')
            if any(x.file_size > 50 * 1024 * 1024 or x.flag_bits & 1 for x in infos):
                raise ValueError('DOCX contains oversized or encrypted entries')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('DOCX does not contain word/document.xml')
            xml(archive, 'word/document.xml')
        return 'docx'
    raise ValueError('File content must match PDF, DOCX or DOC extension')


def word_number(value, kind):
    if kind == 'decimalZero':
        return f'{value:02}'
    if kind in ('lowerLetter', 'upperLetter'):
        label = ''
        while value:
            value, digit = divmod(value - 1, 26)
            label = chr(97 + digit) + label
        return label.upper() if kind == 'upperLetter' else label
    if kind in ('upperRoman', 'lowerRoman'):
        result = ''
        for number, symbol in [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),(50,'L'),(40,'XL'),(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]:
            while value >= number:
                result += symbol; value -= number
        return result.lower() if kind == 'lowerRoman' else result
    return str(value)


def docx_text(path):
    validate_file(path)
    with zipfile.ZipFile(path) as archive:
        numbering = xml(archive, 'word/numbering.xml')
        abstracts, nums, styles = {}, {}, {}
        def level_definition(level,index):
            value = lambda key, default: level.find(W + key).get(W + 'val', default) if level.find(W + key) is not None else default
            return {'start':int(value('start','1')), 'format':value('numFmt','decimal'),
                    'text':value('lvlText',f'%{index+1}.'), 'restart':int(value('lvlRestart',str(index)))}
        for abstract in numbering.findall(W + 'abstractNum'):
            levels = {}
            for level in abstract.findall(W + 'lvl'):
                index = int(level.get(W + 'ilvl'))
                levels[index] = level_definition(level,index)
            abstracts[abstract.get(W + 'abstractNumId')] = levels
        for num in numbering.findall(W + 'num'):
            abstract = num.find(W + 'abstractNumId')
            levels = {k:dict(v) for k,v in abstracts.get(abstract.get(W+'val') if abstract is not None else '',{}).items()}
            for override in num.findall(W + 'lvlOverride'):
                i = int(override.get(W + 'ilvl')); start = override.find(W + 'startOverride')
                level=override.find(W+'lvl')
                if level is not None:levels[i]=level_definition(level,i)
                if start is not None and i in levels:
                    levels[i]['start'] = int(start.get(W + 'val'))
            nums[num.get(W + 'numId')] = levels
        for style in xml(archive,'word/styles.xml').findall(W+'style'):
            styles[style.get(W+'styleId')] = style
        counters = {}

        def num_properties(paragraph):
            props = paragraph.find(W+'pPr')
            if props is None:
                return None
            num = props.find(W+'numPr')
            values={element.tag:element.get(W+'val') for element in num} if num is not None else {}
            style = props.find(W+'pStyle'); visited = set()
            while style is not None and style.get(W+'val') not in visited:
                sid = style.get(W+'val'); visited.add(sid)
                base = styles.get(sid)
                if base is None:
                    break
                inherited = base.find(W+'pPr/'+W+'numPr')
                if inherited is not None:
                    for element in inherited:values.setdefault(element.tag,element.get(W+'val'))
                style = base.find(W+'basedOn')
            return values

        def paragraph_text(paragraph):
            fragments = []
            for element in paragraph.iter():
                if element.tag == W+'t':
                    fragments.append(element.text or '')
                elif element.tag in (W+'tab',W+'br',W+'cr'):
                    fragments.append(' ')
            text = ''.join(fragments).strip()
            num = num_properties(paragraph)
            if num:
                number_id = num.get(W+'numId','0')
                level = int(num.get(W+'ilvl','0'))
                definitions = nums.get(number_id,{})
                definition = definitions.get(level)
                if definition:
                    key = (number_id,level)
                    counters[key] = counters.get(key,definition['start']-1)+1
                    for deeper, item in definitions.items():
                        if deeper > level and item['restart'] != 0 and level < item['restart']:
                            counters.pop((number_id,deeper),None)
                    label = definition['text']
                    for i, item in definitions.items():
                        value = counters.get((number_id,i),item['start'])
                        label = label.replace(f'%{i+1}', word_number(value,item['format']))
                    if definition['format'] == 'bullet':
                        label = '-'
                    text = label + ' ' + text
            return text

        body = xml(archive,'word/document.xml').find(W+'body')
        if body is None:
            raise ValueError('DOCX body is missing')
        result = []
        for element in body:
            if element.tag == W+'p':
                result.append(paragraph_text(element))
            elif element.tag == W+'tbl':
                for row in element.findall(W+'tr'):
                    cells = [' '.join(paragraph_text(p) for p in cell.iter(W+'p')).replace('|','\\|') for cell in row.findall(W+'tc')]
                    result.append('| ' + ' | '.join(cells) + ' |')
        return normalize('\n\n'.join(result))




def pdf_text(path, folder, progress=lambda *args: None):
    import pymupdf
    document = pymupdf.open(path)
    if document.is_encrypted:
        raise ValueError('Password-protected PDF is not supported')
    if not 0 < len(document) <= int(os.getenv('REALTIME_MAX_PAGES','1000')):
        raise ValueError('PDF exceeds REALTIME_MAX_PAGES')
    report = {'pages':len(document), 'ocr_pages':[], 'method':'text_layer', 'warnings':[]}
    pages, repeated = [], Counter()
    for page in document:
        blocks = page.get_text('dict')['blocks']
        items = []
        for block in blocks:
            for line in block.get('lines',[]):
                text = ''.join(span['text'] for span in line['spans']).strip()
                if text:
                    box = line['bbox']; items.append((box,text))
                    if box[1] < page.rect.height * .08 or box[3] > page.rect.height * .92:
                        repeated[re.sub(r'\d','',text)] += 1
        pages.append(items)
    output = []
    for i,page in enumerate(document):
        progress(i+1,len(document))
        native = page.get_text('text')
        valid = sum(c.isalnum() or c.isspace() or c in '.,;:()/-' for c in native)/max(1,len(native))
        image_area = sum(pymupdf.Rect(info['bbox']).get_area() for info in page.get_image_info())/max(1,page.rect.get_area())
        body_boxes=[box for box,_ in pages[i] if page.rect.height*.25<box[1]<page.rect.height*.85]
        left=[box for box in body_boxes if box[0]<page.rect.width*.2 and box[2]<page.rect.width*.55]
        right=[box for box in body_boxes if box[0]>page.rect.width*.5]
        multicolumn=len(left)>=4 and len(right)>=4 and min(max(b[3] for b in left),max(b[3] for b in right))-max(min(b[1] for b in left),min(b[1] for b in right))>page.rect.height*.2
        needs_ocr = multicolumn or (content_length(native)<50 and (not native.strip() or image_area>.05)) or valid<.85 or (image_area>.55 and content_length(native)<2000)
        if needs_ocr:
            # CRAWLER: v2 reads such pages with LLM vision OCR. Accuracy comes first, so report instead of guessing.
            raise NeedsOCR(i+1)
        candidate_tables = [
            table for table in page.find_tables().tables
            if 2 <= table.col_count <= 20 and table.row_count >= 2
            and ((table.bbox[2] - table.bbox[0]) / max(1, table.col_count)) >= 25
        ]
        table_boxes = [pymupdf.Rect(table.bbox) for table in candidate_tables]
        lines = []
        for box,text in pages[i]:
            rect = pymupdf.Rect(box)
            if any(table.intersects(rect) and table.contains(rect.tl) for table in table_boxes):
                continue
            page_number = re.fullmatch(r'(?:Trang\s*)?-?\s*\d+(?:/\d+)?\s*-?',text)
            edge = rect.y0<page.rect.height*.08 or rect.y1>page.rect.height*.92
            if edge and (page_number or (len(document)>1 and repeated[re.sub(r'\d','',text)]>len(document)/2)):
                continue
            lines.append((rect.y0,rect.x0,rect.y1,text))
        for table in candidate_tables:
            rows = ['| '+' | '.join(' '.join((value or '').split()).replace('|','\\|') for value in row)+' |' for row in table.extract()]
            lines.append((table.bbox[1],table.bbox[0],table.bbox[3],'\n'.join(rows)))
        lines.sort(key=lambda item:(round(item[0]/3),item[1]))
        # Handle two-column headers; OCR is the fallback for complex layouts.
        header = [line for line in lines if i==0 and line[0]<page.rect.height*.24]
        if any(line[1]>page.rect.width*.5 for line in header):
            lines = sorted(header,key=lambda item:(item[1]>page.rect.width*.5,item[0])) + [line for line in lines if line not in header]
        text_lines = []
        structural = re.compile(r'^(Điều\b|Chương\b|Mục\b|\d+[a-zđ]?\.\s|[a-zđ][a-zđ0-9]?\)\s|PHỤ LỤC|Phụ lục|Căn cứ|Nơi nhận|[-+•|])',re.I)
        previous = None
        for y,x,bottom,text in lines:
            if (previous and text_lines and not structural.match(text) and not text.isupper()
                and not text_lines[-1].endswith(('.',':',';','!','?','|')) and abs(x-previous[1])<35
                and 0<=y-previous[2]<max(4,(previous[2]-previous[0])*.6)):
                text_lines[-1] += ' '+text
            else:
                text_lines.append(text)
            previous = (y,x,bottom)
        output.append('\n'.join(text_lines))
    document.close()
    text = normalize('\n\n'.join(output))
    if content_length(text)<50:
        raise ValueError('Document has insufficient readable text')
    return text,report



def read_document(path, folder=None, progress=lambda *args: None):
    """Return (text, report). DOC is not converted here: LibreOffice is not part of the nightly image yet."""
    kind = validate_file(path)
    if kind == 'docx':
        return docx_text(path), {'method': 'docx', 'pages': None, 'ocr_pages': [], 'warnings': []}
    if kind == 'pdf':
        return pdf_text(path, folder, progress)
    raise ValueError(f'{kind} is not supported without LibreOffice')
