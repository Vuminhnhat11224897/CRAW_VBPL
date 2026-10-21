# Vendored from data_vbpl (legal_ingest) commit 22d9796. Keep logic identical to the ES v2 writer;
# only the changes marked "CRAWLER:" differ. See docs/adr/0001-v3-cluster-v2-schema.md.
"""Deterministic line parser and ES-compatible legal chunks."""

from collections import Counter
import re
import unicodedata

NUMBER = re.compile(r'(?<![\w:/])\d{1,6}/(?:\d{4}/)?[A-ZĐ][A-Za-zÀ-ỹĐ0-9]*(?:[ \t]*[-./][ \t]*[A-Za-zÀ-ỹĐ0-9]+)*')
ARTICLE = re.compile(r'^(?:Điều|ĐIỀU)\s+(\d{1,4})([a-zđ]{0,2})\s*([.:–—-])?\s*(.*)$')
CLAUSE = re.compile(r'^(\d{1,3})([a-zđ]{0,2})\.\s+(.*)$')
POINT = re.compile(r'^([a-zđ][a-zđ0-9]?)\)\s+(.*)$')
CHAPTER = re.compile(r'^(Chương|CHƯƠNG|Mục|MỤC)\s+([IVXLCDM]+|\d+)\b\s*[.:–—-]?\s*(.*)$')
APPENDIX = re.compile(r'^(?:PHỤ LỤC|PHU LUC|Phụ lục)(?:\s+(?:SỐ\s+|số\s+)?([IVXLCDM]+|\d+[A-Za-z]?))?\s*[.:]?\s*(.*)$')
ATTACHED = re.compile(r'^(?:QUY ĐỊNH|QUY CHẾ|QUY TRÌNH|QUY TẮC|QUY CHUẨN|QUY HOẠCH|DANH MỤC|DANH SÁCH|BẢNG|ĐỀ ÁN|ĐỀ CƯƠNG|CHƯƠNG TRÌNH|ĐIỀU LỆ|NỘI QUY|BIỂU MẪU|MẪU SỐ|PHỤ BIỂU|TIÊU CHÍ|KẾ HOẠCH|THUYẾT MINH|HƯỚNG DẪN|TIÊU CHUẨN)\b')
TAIL = re.compile(r'^(?:Nơi nhận\s*:|T\.?M[./]|T/M|K\.?T[./]|K/T|KT\.|TM\.)', re.I)
VI_LETTERS = 'abcdđeghiklmnopqrstuvxy'


def refs(text):
    text = re.sub(r'-\s+(?=[A-ZĐ])', '-', text)
    return list(dict.fromkeys(re.sub(r'\s+','',m.group()) for m in NUMBER.finditer(text)))


def normalize(text):
    # PDF extraction can emit NULs; PostgreSQL text/JSONB cannot store them.
    text = text.replace('\x00', '')
    text = unicodedata.normalize('NFC', text).replace('Ð', 'Đ').replace('ð', 'đ')
    text = text.replace('\u200b', '').replace('\ufeff', '').replace('\xa0', ' ')
    text = re.sub(r'(?<=[A-ZĐ])-\s*\n\s*(?=[A-ZĐ])', '-', text)
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.replace('\f', '\n').splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def structural_line(line):
    """CRAWLER: interpret OCR Markdown on legal labels, preserving source text elsewhere."""
    candidate = re.sub(r'^#{1,6}[ \t]+', '', line)
    if candidate != line:
        candidate = re.sub(r'[ \t]+#{1,6}$', '', candidate).rstrip()
    for marker in ('**', '__', '*', '_'):
        if not candidate.startswith(marker):
            continue
        closing = candidate.find(marker, len(marker) + 1)
        if closing != -1:
            candidate = candidate[len(marker):closing] + candidate[closing + len(marker):]
            break
    if any(pattern.match(candidate) for pattern in (ARTICLE, CLAUSE, POINT, CHAPTER, APPENDIX, TAIL, ATTACHED)):
        return candidate
    return line


def appendix_header(line):
    m = APPENDIX.match(line)
    if m and re.match(r'^(?:kèm theo|ban hành kèm theo|của|tại|quy định tại)\b',m[2],re.I):return False
    return bool(m and (line.isupper() or not m[2] or m[2].isupper() or len(line) < 60))


def unquoted(lines, quote_resets=()):
    """Keep quoted amendment text in bodies without treating its labels as source labels."""
    depth=0;straight=False;result=[]
    for line in lines:
        if line in quote_resets:
            depth, straight = 0, False
        result.append(not depth and not straight)
        for i,char in enumerate(line):
            if char=='“':depth+=1
            elif char=='”':
                if depth:depth-=1
                else:straight=False
            elif char=='"' and (not i or line[i-1]!='\\'):
                # OCR may open with a curly quote and close with an ASCII quote.
                # A closing quote at the end of the line must end the curly block,
                # while embedded ASCII pairs inside it remain nested quotations.
                if depth and not straight and re.fullmatch(r'\s*[.;,:]?\s*',line[i+1:]):
                    depth-=1
                else:straight=not straight
    return result


def numeric_sequence(lines, pattern, restart=False, start_mid=False, visibility=None):
    candidates = []
    for i, (line,visible) in enumerate(zip(lines, unquoted(lines) if visibility is None else visibility)):
        if not visible or line.startswith('|'):
            continue
        m = pattern.match(line)
        if not m:
            continue
        if pattern is ARTICLE and not m[3] and m[4] and (m[4][0].islower() or re.match(r'^(?:Quyết định|Nghị định|Nghị quyết|Thông tư|Luật|Bộ luật)\b',m[4])):
            continue
        candidates.append((i, m))
    accepted, last, suffix, segment = [], 0, '', 0
    for pos, (i, m) in enumerate(candidates):
        number, extra = int(m[1]), m[2]
        next_number = int(candidates[pos + 1][1][1]) if pos + 1 < len(candidates) else None
        if not accepted:
            valid = number == 1 or (start_mid and next_number == number + 1)
        elif restart and number == 1 and next_number == 2:
            segment += 1
            valid = True
        elif extra:
            valid = (number == last and next_point(suffix,extra)) or (number == last + 1 and extra == 'a')
        else:
            valid = number == last + 1 or (number > last + 1 and next_number == number + 1)
        if valid:
            accepted.append((i, m, segment))
            last, suffix = number, extra
    return accepted


def next_point(previous, current):
    if not previous:
        return current == 'a'
    if current[0] == previous[0] and current[1:].isdigit():
        return int(current[1:]) == (int(previous[1:]) + 1 if previous[1:].isdigit() else 1)
    if current == 'aa' and previous[0] in 'xyz':
        return True
    if len(previous) == len(current) == 2 and previous[0] == previous[1] and current[0] == current[1]:
        return ord(current[0]) == ord(previous[0]) + 1
    if len(current) > 1 and current[1:] != '1':
        return False
    return any(a in alphabet and b in alphabet and alphabet.index(b) == alphabet.index(a) + 1
               for alphabet in (VI_LETTERS, 'abcdefghijklmnopqrstuvwxyz') for a, b in [(previous[0], current[0])])


def split_points(text):
    lines = text.splitlines()
    candidates, previous = [], ''
    for i, (line,visible) in enumerate(zip(lines,unquoted(lines))):
        m = POINT.match(line)
        if visible and m and next_point(previous, m[1]):
            candidates.append((i, m))
            previous = m[1]
    if len(candidates) < 2:
        return '', []
    points = []
    for pos, (i, m) in enumerate(candidates):
        end = candidates[pos + 1][0] if pos + 1 < len(candidates) else len(lines)
        body = '\n'.join([m[2], *lines[i + 1:end]]).strip()
        points.append({'ky_hieu': m[1], 'noi_dung': body, 'so_van_ban_refs': refs(body)})
    return '\n'.join(lines[:candidates[0][0]]).strip(), points


def point_sequence_issues(text, quote_resets=()):
    """Reject orphan/gapped point lists instead of silently flattening them."""
    lines = [structural_line(line) for line in normalize(text).splitlines()]
    visible_lines = unquoted(lines, quote_resets)
    article_positions = {i for i, _, _ in numeric_sequence(lines, ARTICLE, restart=True, start_mid=True, visibility=visible_lines)}
    previous, location, issues = '', '', []
    for i, (line, visible) in enumerate(zip(lines, visible_lines)):
        if not visible:
            continue
        if i in article_positions or any(p.match(line) for p in (CLAUSE, CHAPTER, APPENDIX, TAIL)):
            previous, location = '', line[:100]
        match = POINT.match(line)
        if match:
            if not next_point(previous, match[1]):
                issues.append(f'{location}: unexpected point {match[1]} after {previous or "start"}')
            previous = match[1]
    return issues


def split_clauses(text):
    lines = text.splitlines()
    candidates = numeric_sequence(lines, CLAUSE)
    clauses = []
    for pos, (i, m, _) in enumerate(candidates):
        end = candidates[pos + 1][0] if pos + 1 < len(candidates) else len(lines)
        body = '\n'.join([m[3], *lines[i + 1:end]]).strip()
        opening, points = split_points(body)
        clause = {'so_khoan': int(m[1]), 'ky_hieu': m[1] + m[2], 'noi_dung': body,
                  'so_van_ban_refs': refs(body), 'diem': points}
        if points:
            clause['mo_dau'] = opening
        clauses.append(clause)
    return ('\n'.join(lines[:candidates[0][0]]).strip() if candidates else ''), clauses


def chapter_marks(lines, visibility=None):
    marks = []
    for i, (line,visible) in enumerate(zip(lines, unquoted(lines) if visibility is None else visibility)):
        m = CHAPTER.match(line)
        if not visible or not m:
            continue
        names = [m[3]] if m[3] else []
        for next_line in [x for x in lines[i + 1:] if x][:3]:
            if not next_line.isupper() or ARTICLE.match(next_line) or CHAPTER.match(next_line) or next_line.startswith('|'):
                break
            names.append(next_line)
        kind = 'chuong' if m[1].casefold() == 'chương' else 'muc'
        marks.append((i, kind, f'{"Chương" if kind == "chuong" else "Mục"} {m[2]} - {" ".join(names)}'.rstrip(' -')))
    return marks


def parse_articles(lines, visibility=None):
    candidates = numeric_sequence(lines, ARTICLE, restart=True, start_mid=True, visibility=visibility)
    marks = chapter_marks(lines, visibility)
    articles = []
    for pos, (i, m, segment) in enumerate(candidates):
        end = candidates[pos + 1][0] if pos + 1 < len(candidates) else len(lines)
        end = min([end, *[j for j, _, _ in marks if i < j < end]])
        body = '\n'.join(lines[i + 1:end]).strip()
        following=next((x for x in body.splitlines() if x),'')
        title=m[4] or (following if not CLAUSE.match(following) and not POINT.match(following) else '')
        opening, clauses = split_clauses(body)
        article = {'so_dieu': int(m[1]), 'ky_hieu': m[1] + m[2], 'tieu_de': title,
                   'noi_dung': body, 'so_van_ban_refs': refs(title + '\n' + body), 'khoan': clauses}
        if clauses:
            article['mo_dau'] = opening
        if segment:
            article['van_ban_kem_theo'] = segment
        chapter, section = None, None
        for j, kind, name in marks:
            if j >= i:
                break
            if kind == 'chuong':
                chapter, section = name, None
            else:
                section = name
        if chapter:
            article['chuong'] = chapter
        if section:
            article['muc'] = section
        articles.append(article)
    return articles, candidates


def roman(value):
    values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    return sum(-values[c] if i + 1 < len(value) and values[c] < values[value[i + 1]] else values[c] for i, c in enumerate(value))


def split_sections(text):
    lines = text.splitlines()
    for pattern in (r'^([IVXLCDM]+)[.)]\s+(.*)$', r'^(\d+)[.)]\s+(.*)$'):
        accepted, last = [], 0
        for i, (line,visible) in enumerate(zip(lines,unquoted(lines))):
            m = re.match(pattern, line)
            if visible and m:
                number = int(m[1]) if m[1].isdigit() else roman(m[1])
                if number == last + 1:
                    accepted.append((i, m)); last = number
        if len(accepted) >= 2:
            result = []
            for pos, (i, m) in enumerate(accepted):
                end = accepted[pos + 1][0] if pos + 1 < len(accepted) else len(lines)
                body = '\n'.join([m[2], *lines[i + 1:end]]).strip()
                if pos == 0 and i:
                    body = '\n'.join(lines[:i]) + '\n' + body
                result.append({'so_muc': m[1], 'noi_dung': body, 'so_van_ban_refs': refs(body)})
            return result
    return []


def parse(text, quote_resets=()):
    # Separate introductory declarations even if OCR joined them to a basis line.
    text = re.sub(r';\s*(?=Theo đề nghị\b|Chính phủ ban hành\b)', ';\n', text)
    text = re.sub(r'\bCăn\s+cứ\s+(?:các\s+)?(?:quy\s+định\s+)?(?:nêu\s+)?trên\b', lambda m:'\n'+re.sub(r'\s+',' ',m.group()), text)
    lines = [structural_line(line) for line in normalize(text).splitlines()]
    visible=unquoted(lines, quote_resets)
    labeled = [i for i, line in enumerate(lines) if visible[i] and appendix_header(line)]
    probe = lines[:labeled[0]] if labeled else lines
    _, candidates = parse_articles(probe, visible[:len(probe)])
    anchor = max((i for i, _, seg in candidates if not seg), default=0)
    recipients = next((i for i in range(anchor, len(probe)) if visible[i] and re.match(r'^Nơi nhận\s*:', probe[i], re.I)), None)
    tail_start = recipients
    if tail_start is None:
        tail_start = next((i if TAIL.match(probe[i]) else i+1 for i in range(anchor, len(probe)) if visible[i] and (TAIL.match(probe[i]) or './.' in probe[i])), None)
    appendix_start = labeled[0] if labeled else None
    if tail_start is not None and appendix_start is None:
        appendix_start = next((i for i in range(tail_start + 1, len(lines)) if lines[i].isupper() and ATTACHED.match(lines[i])), None)
    if appendix_start is not None and (tail_start is None or appendix_start < tail_start):
        body_end, tail = appendix_start, ''
    else:
        body_end = tail_start if tail_start is not None else (appendix_start if appendix_start is not None else len(lines))
        tail = '\n'.join(lines[body_end:appendix_start]).strip() if tail_start is not None else ''
    main = lines[:body_end]
    articles, candidates = parse_articles(main, visible[:body_end])
    first_article = candidates[0][0] if candidates else len(main)
    basis, basis_end = [], 0
    i = 0
    while i < first_article:
        if main[i].startswith('Căn cứ ') and not re.match(r'^Căn cứ (?:các )?(?:quy định )?(?:nêu )?trên\b', main[i], re.I):
            start = i; i += 1
            while i < first_article and main[i] and not main[i-1].rstrip().endswith(';') and not main[i].startswith(('Căn cứ ', 'Theo đề nghị ', 'Chính phủ ban hành ')) and not CHAPTER.match(main[i]):
                i += 1
            basis.append(' '.join(main[start:i]).strip().rstrip(';'))
            basis_end = i
        else:
            i += 1
    marks = [i for i, _, _ in chapter_marks(main, visible[:body_end]) if basis_end <= i < first_article]
    intro_end = marks[-1] if marks else first_article
    intro = '\n'.join(main[basis_end:intro_end]).strip()
    appendices = []
    if appendix_start is not None:
        region = lines[appendix_start:]
        boundaries = [i for i, line in enumerate(region) if appendix_header(line)]
        if not boundaries or boundaries[0] != 0:
            boundaries.insert(0, 0)
        for pos, start in enumerate(boundaries):
            end = boundaries[pos + 1] if pos + 1 < len(boundaries) else len(region)
            part = region[start:end]
            title_lines = [part[0]] if part else []
            offset = 1
            for line in part[1:]:
                if not line:
                    offset += 1; continue
                if len(title_lines) >= 5 or line.startswith('|') or not (line.isupper() or line.startswith(('(Ban hành', '(Kèm theo'))):
                    break
                title_lines.append(line); offset += 1
                if line.endswith(')'):
                    break
            content = '\n'.join(part[offset:]).strip()
            appendix_articles, _ = parse_articles(part[offset:])
            rows = [line for line in part if line.startswith('|')]
            table_groups = re.findall(r'(?:^\|.*(?:\n|$)(?:\s*\n)*){2,}', '\n'.join(part), re.M)
            appendices.append({'tieu_de': ' '.join(title_lines), 'noi_dung': content,
                               'loai': 'labeled' if part and appendix_header(part[0]) else 'unlabeled',
                               'has_table': len(rows) >= 2, 'n_tables': len(table_groups), 'n_table_rows': len(rows),
                               'dieu_luat': appendix_articles})
    places = []
    if tail:
        for item in re.split(r'(?:\n\s*-|;)', re.sub(r'^.*?Nơi nhận\s*:', '', tail, flags=re.S | re.I)):
            if TAIL.match(item.strip()) or not item.strip():
                continue
            value = re.split(r'\n(?:KT\.|TM\.|T/M|K/T)', item, maxsplit=1)[0].strip().strip('- ;.,')
            if value:
                places.append(value)
    result = {'can_cu': basis, 'can_cu_refs': refs(' '.join(basis)), 'intro': intro,
              'dieu_luat': articles, 'noi_nhan': places, 'phu_luc': appendices,
              'has_tail': any(TAIL.match(line) or './.' in line for line in lines)}
    if not articles:
        # A letter's bases and quoted rules are part of its reasoning, not a removable preamble.
        letter_content='\n'.join(main).strip()
        result.update(noi_dung=letter_content, noi_dung_refs=refs(letter_content), noi_dung_sections=split_sections(letter_content))
    all_articles = articles + [a for p in appendices for a in p['dieu_luat']]
    seq_ok, last, seg = True, 0, 0
    for a in articles:
        current_seg = a.get('van_ban_kem_theo', 0)
        if current_seg != seg:
            last, seg = 0, current_seg
        if a['so_dieu'] not in (last, last + 1):
            seq_ok = False
        last = a['so_dieu']
    if 'bãi bỏ' in text.casefold():
        seq_ok = True
    clauses = [k for a in all_articles for k in a['khoan']]
    count = lambda value: len(re.sub(r'\s', '', value))
    reconstructed = '\n'.join(main[:basis_end]) + intro + tail
    reconstructed += ''.join(a['tieu_de'] + a['noi_dung'] for a in articles)
    reconstructed += ''.join(p['tieu_de'] + p['noi_dung'] for p in appendices)
    covered = sum(count(a.get('mo_dau', '')) + sum(count(k['noi_dung']) for k in a['khoan']) for a in all_articles if a['khoan'])
    denominator = sum(count(a['noi_dung']) for a in all_articles if a['khoan'])
    grade = 'full' if result['has_tail'] and ((articles and seq_ok) or result.get('noi_dung_sections')) else ('partial' if all_articles or appendices or result.get('noi_dung_sections') else 'minimal')
    stats = {'grade': grade, 'n_dieu': sum(not a.get('van_ban_kem_theo') for a in articles),
             'n_dieu_total': len(all_articles), 'n_khoan': len(clauses), 'n_diem': sum(len(k['diem']) for k in clauses),
             'n_phu_luc': len(appendices), 'n_muc': len(result.get('noi_dung_sections', [])),
             'dieu_seq_ok': seq_ok, 'has_tail': result['has_tail'],
             'coverage': min(1, count(reconstructed) / max(1, count(text))),
             'khoan_coverage': min(1, covered / max(1, denominator)) if denominator else 1}
    return result, stats


def build_chunks(doc_id, metadata, parsed):
    common = {k: metadata[k] for k in ('document_number', 'title', 'action', 'legal_type', 'issuing_authority', 'issuance_date', 'url', 'signers') if metadata.get(k)}
    common['doc_id'] = str(doc_id)
    if metadata.get('legal_sectors'):
        common['legal_sectors'] = ', '.join(metadata['legal_sectors'])
    chunks, seen = [], Counter()

    def emit(uid, kind, title, body, fields, references=None, full=False):
        if not (title + body).strip():
            return
        seen[uid] += 1
        key = uid if seen[uid] == 1 else f'{uid}_{seen[uid]}'
        source = {**common, **fields, 'chunk_type': kind, 'tieu_de': title, 'noi_dung': body,
                  'so_van_ban_refs': references if references is not None else refs(title + ' ' + body)}
        if full:
            source['is_full'] = True
        chunks.append({'_id': key, '_source': source})

    def article_chunks(article, prefix, extra):
        segment = article.get('van_ban_kem_theo', 0)
        uid = prefix + (f'_s{segment}' if segment else '') + '_d' + article['ky_hieu']
        fields = {**extra, 'dieu_uid': uid, 'segment': segment, 'so_dieu': article['so_dieu'], 'ky_hieu_dieu': article['ky_hieu']}
        for key, target in [('chuong', 'chuong'), ('muc', 'muc_lon')]:
            if article.get(key):
                fields[target] = article[key]
        title = article['tieu_de']
        clauses = article['khoan']
        emit(uid + ('_full' if clauses else ''), 'dieu_full' if clauses else 'dieu', title, article['noi_dung'], fields, article['so_van_ban_refs'], True)
        if len(article.get('mo_dau', '')) >= 40:
            emit(uid + '_md', 'dieu_mo_dau', title, article['mo_dau'], fields, [])
        for clause in clauses:
            kid = uid + '_k' + clause['ky_hieu']
            kfields = {**fields, 'khoan_uid': kid, 'so_khoan': clause['so_khoan'], 'ky_hieu_khoan': clause['ky_hieu']}
            points = clause['diem']
            emit(kid + ('_full' if points else ''), 'khoan_full' if points else 'khoan', title, clause['noi_dung'], kfields, clause['so_van_ban_refs'], True)
            if len(clause.get('mo_dau', '')) >= 40:
                emit(kid + '_md', 'khoan_mo_dau', title, clause['mo_dau'], kfields, [])
            for point in points:
                emit(kid + '_p' + point['ky_hieu'], 'diem', title, point['noi_dung'], {**kfields, 'so_diem': point['ky_hieu']}, point['so_van_ban_refs'])

    for article in parsed['dieu_luat']:
        article_chunks(article, str(doc_id), {})
    for i, appendix in enumerate(parsed['phu_luc']):
        prefix = f'{doc_id}_pl{i}'
        extra = {'in_phu_luc': True, 'phu_luc_tieu_de': appendix['tieu_de'][:500], 'has_table': appendix['has_table']}
        if appendix['dieu_luat']:
            for article in appendix['dieu_luat']:
                article_chunks(article, prefix, extra)
        else:
            emit(prefix, 'phu_luc', appendix['tieu_de'], appendix['noi_dung'], extra)
    if not parsed['dieu_luat']:
        sections = parsed.get('noi_dung_sections', [])
        for section in sections:
            emit(f'{doc_id}_m{section["so_muc"]}', 'muc', '', section['noi_dung'], {'so_muc': section['so_muc']}, section['so_van_ban_refs'])
        if not sections and parsed.get('noi_dung'):
            emit(f'{doc_id}_nd', 'noi_dung', '', parsed['noi_dung'], {}, parsed.get('noi_dung_refs', []))
    return chunks
