# Vendored from data_vbpl (legal_ingest) commit 22d9796; logic unchanged. See docs/adr/0001-v3-cluster-v2-schema.md.
"""Four grounded candidate detectors and conservative target resolution."""

from collections import defaultdict
from datetime import date
import re

from .contracts import action_matches, authority_key, number_key
from .structure import NUMBER, refs


def frame(text):
    # Only the declaration before replacement content can describe the change.
    text=re.split(r'như sau\s*[:.]',text,maxsplit=1,flags=re.I)[0]
    text=re.sub(r'“[^”"]*[”"]|"[^"”]*["”]','',text)
    return re.split(r'[“"]',text,maxsplit=1)[0][:440]


def declared_scope(unit, parent='', action=None):
    """Resolve an explicit target location, inheriting only its parent article.

    Do not infer a whole provision when changing words or sentences within it.
    Insertion anchors ("vào sau điểm đ") are not the provision being added.
    """
    declaration=frame(unit)
    if not action_matches(declaration):return None
    if re.search(r'\b(?:cụm từ|từ ngữ|câu|đoạn(?!\s+đầu))\b',declaration,re.I):return None
    parent_articles=re.findall(r'\bđiều\s+(\d+[a-zđ]?)\b',frame(parent),re.I)
    inherited=parent_articles[0] if len(set(parent_articles))==1 else None
    parent_clauses=re.findall(r'\bkhoản\s+(\d+[a-zđ]?)\b',frame(parent),re.I)
    inherited_clause=parent_clauses[0] if len(set(parent_clauses))==1 else None
    unit_articles=re.findall(r'\bđiều\s+(\d+[a-zđ]?)\b',declaration,re.I)
    if len(set(unit_articles))==1:inherited=unit_articles[0]
    # Independent actions in one declaration can affect different provisions.
    parts=re.split(r'\s+và\s+(?=(?:bổ sung|sửa đổi|bãi bỏ|thay thế)\b)',declaration,flags=re.I)
    values=[]
    for part in parts:
        actions={a for _,_,a in action_matches(part)}
        if action and action not in actions:continue
        part=re.sub(r'\s+vào\s+(?:sau|trước)\s+(?:điểm\s+[a-zđ]\w*|khoản\s+\d+[a-zđ]?|điều\s+\d+[a-zđ]?)','',part,flags=re.I)
        article=re.search(r'\bđiều\s+(\d+[a-zđ]?)\b',part,re.I)
        article_number=article[1] if article else inherited
        point_label=r'(?:\d+(?:\.\d+)+|[a-zđ]\w*)'
        points=re.search(r'\bđiểm\s+('+point_label+r'(?:(?:\s*,\s*|\s+và\s+)(?:điểm\s+)?'+point_label+r')*)\b',part,re.I)
        clauses=re.search(r'\bkhoản\s+(\d+[a-zđ]?(?:(?:\s*,\s*|\s+và\s+)(?:khoản\s+)?\d+[a-zđ]?)*)\b',part,re.I)
        clause_number=clauses[1] if clauses else inherited_clause
        if points and clause_number and article_number:
            value=f'điểm {points[1]} khoản {clause_number} Điều {article_number}'
        elif clauses and article_number:
            prefix='đoạn đầu ' if re.search(r'đoạn\s+đầu\s+khoản',part,re.I) else ''
            value=f'{prefix}khoản {clauses[1]} Điều {article_number}'
        elif article and not points:
            value,unresolved=scope(part)
            if unresolved:return None
        else:return None
        values.append(value)
    return '; '.join(dict.fromkeys(values)) if values else None


def scope(text):
    fee=re.search(r'(?:bãi bỏ|hủy bỏ|huỷ bỏ|thay thế)\s+toàn bộ\s+((?:lệ phí|phí)\b[^.\n;]*)',text,re.I)
    if fee:return fee[1].strip(),False
    patterns=[r'điểm\s+\d+(?:\.\d+)+\s+khoản\s+\d+\s+điều\s+\d+',
        r'toàn (?:bộ|văn)',r'(?:số )?thứ tự\s+[\d, và]+',r'phụ lục\s+[IVXLCDM\d]+',
        r'điểm\s+[a-zđ](?:\s*[, và]+\s*[a-zđ])*\s+khoản\s+\d+[a-zđ]?\s+điều\s+\d+[a-zđ]?',
        r'khoản\s+\d+[a-zđ]?(?:\s*[, và]+\s*\d+[a-zđ]?)*\s+điều\s+\d+[a-zđ]?',
        r'điều\s+\d+[a-zđ]?(?:\s*[, và]+\s*\d+[a-zđ]?)*',r'khoản\s+\d+[a-zđ]?(?:\s*[, và]+\s*\d+[a-zđ]?)*',
        r'(?:sửa đổi tên|tên gọi)']
    for pattern in patterns:
        match=re.search(pattern,text,re.I)
        if match:
            value=match.group().strip()
            return ('toàn bộ' if value.casefold()=='toàn văn' else value),False
    return 'chưa xác định',True


def detect(doc_id,meta,parsed):
    if meta.get('legal_type')=='Văn bản hợp nhất':return []
    candidates=[];title_actions=action_matches(meta.get('title',''))
    def add(target,action,path,text,scope_text,mode='',extra_scope=None,in_appendix=False,unit_text=None,parent_text=''):
        value,unresolved=scope(scope_text)
        explicit=declared_scope(scope_text,parent_text,action)
        if explicit:value,unresolved=explicit,False
        if extra_scope:value,unresolved=extra_scope,False
        if not target:return
        target=re.sub(r'\s+','',target)
        if action in ('bai-bo','huy-bo','thay-the') and re.search(r'(?:bãi bỏ|hủy bỏ|huỷ bỏ|thay thế)\s+(?:toàn bộ\s+)?(?:Quyết định|Nghị quyết|Nghị định|Thông tư|Luật|Bộ luật)\s+(?:số\s*)?$',scope_text,re.I):
            value,unresolved='toàn bộ',False
        candidates.append({'target_doc':target,'action':action,'source_path':path,'scope':value,
            'unresolved':unresolved,'source_in_phu_luc':in_appendix,'_mode':mode,'_evidence':text[:4000],
            '_unit_evidence':(unit_text if unit_text is not None else text)[:4000],
            '_parent_evidence':parent_text[:2000]})
    articles=[(a,'',False) for a in parsed['dieu_luat']]
    articles += [(a,f'Phụ lục {i+1} ',True) for i,p in enumerate(parsed['phu_luc']) for a in p['dieu_luat']]
    for article,prefix,in_appendix in articles:
        if article.get('van_ban_kem_theo'):prefix+=f'Văn bản kèm theo {article["van_ban_kem_theo"]} '
        path=prefix+'Điều '+article['ky_hieu']
        head=article['tieu_de']+' '+article.get('mo_dau','')
        chapter=article.get('chuong','')
        head_refs=list(NUMBER.finditer(head));chapter_refs=refs(chapter)
        matches=action_matches(head)
        target=None;mode='' if title_actions else 'hidden'
        if title_actions:
            target=head_refs[0].group() if head_refs else (chapter_refs[0] if chapter_refs else None)
        else:
            for start,end,action in matches:
                if any(m.start()<start for m in head_refs):continue
                if re.search(r'(?:đã|đang|sẽ)\s+(?:được|bị)\s*$',head[max(0,start-40):start],re.I):continue
                following=[m for m in head_refs if m.start()>end]
                if following:target=following[0].group();break
                if chapter_refs:target=chapter_refs[0];break
        if target and target in head:
            target_position=head.find(target)
            matches=[m for m in matches if m[1]<=target_position]
        default=(matches or title_actions)
        if target and default:
            if article['khoan']:
                for clause in article['khoan']:
                    clause_path=path+' Khoản '+clause['ky_hieu']
                    units=[(clause_path+' điểm '+p['ky_hieu'],p['noi_dung']) for p in clause['diem']] if clause['diem'] else [(clause_path,clause['noi_dung'])]
                    for unit_path,unit in units:
                        declaration=frame(unit)
                        actions=action_matches(declaration) or default[:1]
                        for _,_,action in actions:
                            add(target,action,unit_path,chapter+'\n'+head+'\n'+clause.get('mo_dau','')+'\n'+unit,declaration,mode,in_appendix=in_appendix,unit_text=unit,parent_text=head)
            else:
                source=head+'\n'+article['noi_dung'];declaration=frame(source)
                before=declaration.split(target,1)[0]
                for _,_,action in (matches or default):
                    add(target,action,path,chapter+'\n'+source,before,mode,in_appendix=in_appendix,unit_text=source,parent_text=head)
        elif title_actions or matches or article['khoan']:
            units=[(path,article.get('mo_dau',''))]
            if not article['khoan']:units.append((path,head+' '+article['noi_dung']))
            for clause in article['khoan']:
                kpath=path+' Khoản '+clause['ky_hieu']
                if clause['diem']:
                    units.append((kpath,clause.get('mo_dau','')))
                    units.extend((kpath+' điểm '+p['ky_hieu'],p['noi_dung']) for p in clause['diem'])
                else:units.append((kpath,clause['noi_dung']))
            for unit_path,text in units:
                declaration=frame(text);match=NUMBER.search(declaration)
                if match:
                    direct=action_matches(declaration[:match.start()])
                    inherited=matches or title_actions[:1]
                    for _,_,action in (direct or inherited):
                        context=head+'\n'+text if matches else text
                        document_list=not direct and matches and action in ('bai-bo','huy-bo','thay-the') and re.search(r'(?:bãi bỏ|hủy bỏ|huỷ bỏ|thay thế)\s+các\s+(?:văn bản|Quyết định|Thông tư|Nghị định|Nghị quyết)\b',head,re.I)
                        add(match.group(),action,unit_path,context,declaration[:match.start()],extra_scope='toàn bộ' if document_list else None,in_appendix=in_appendix,unit_text=text,parent_text=head)
        units=[(path,head+' '+article['noi_dung'])] if not article['khoan'] else [(path,head)]+[(path+' Khoản '+k['ky_hieu'],k['noi_dung']) for k in article['khoan']]
        for unit_path,text in units:
            for match in re.finditer(r'hết\s*hiệu\s*lực(?:\s*thi\s*hành)?(?:\s*kể)?\s*từ\s*ngày',text,re.I):
                if not re.search(r'này\s+có\s+hiệu\s+lực',text[match.end():match.end()+60],re.I):continue
                before=text[max(0,match.start()-250):match.start()]
                before=re.sub(r'(?:đã\s+)?được\s+sửa đổi,?\s*bổ sung.{0,80}?(?:theo|tại|bởi).{0,80}?(?=[,;]|$)','',before,flags=re.I)
                targets=refs(before)
                if targets:add(targets[-1],'thay-the',unit_path,text,before,'het_hieu_luc','toàn bộ',in_appendix)
    intro=parsed.get('intro','')
    for start,end,action in action_matches(intro):
        if NUMBER.search(intro[:start]) or re.search(r'(?:đã|đang|sẽ)\s+(?:được|bị)\s*$',intro[max(0,start-40):start],re.I):continue
        match=NUMBER.search(intro,end)
        if match and 'BC-' not in match.group():
            add(match.group(),action,'intro',intro,intro[end:match.start()],'intro')
    seen=set();result=[]
    for candidate in candidates:
        key=tuple(candidate[k] for k in ('target_doc','action','source_path','scope'))
        if key not in seen:
            seen.add(key);result.append(candidate)
    result.sort(key=lambda x:(x['_mode'],x['source_path'],x['target_doc'],x['action'],x['scope']))
    counts=defaultdict(int)
    for candidate in result:
        mode=candidate.pop('_mode');i=counts[mode];counts[mode]+=1
        prefix={'hidden':'hidden_','het_hieu_luc':'hhl_','intro':'intro_'}.get(mode,'')
        candidate['_id']=f'{prefix}{doc_id}_{i}'
        candidate.update(source_id=int(doc_id),source_document_number=meta.get('document_number'),source_title=meta['title'],
            source_action=meta['action'],source_issuing_authority=meta['issuing_authority'],source_issuance_date=meta.get('issuance_date'),
            source_legal_type=meta['legal_type'],source_legal_sectors=', '.join(meta.get('legal_sectors',[])),source_url=meta.get('url'))
        if mode:candidate['detection_mode']=mode
    return result


def resolve(candidate,hits):
    hits=[h for h in hits if number_key(h['_source'].get('document_number'))==number_key(candidate['target_doc'])]
    candidate['target_candidates_count']=len(hits)
    eligible=[h for h in hits if h['_id'].isdigit() and int(h['_id'])!=candidate['source_id'] and
              h['_source'].get('issuance_date') and candidate.get('source_issuance_date') and h['_source']['issuance_date']<=candidate['source_issuance_date']]
    evidence=candidate.get('_evidence','')
    cited=next((m for m in NUMBER.finditer(evidence) if number_key(m.group())==number_key(candidate['target_doc'])),None)
    citation=evidence[cited.end():cited.end()+240] if cited else ''
    cited_date=candidate.get('target_issuance_date')
    match=re.search(r'ngày\s+(\d{1,2})(?:\s+tháng\s+|[/-])(\d{1,2})(?:\s+năm\s+|[/-])(\d{4})',citation,re.I)
    if match:
        try:cited_date=date(int(match[3]),int(match[2]),int(match[1])).isoformat()
        except ValueError:pass
    if cited_date:
        candidate['target_issuance_date']=cited_date
        eligible=[h for h in eligible if h['_source']['issuance_date']==cited_date]
    authority=candidate.get('target_issuing_authority')
    if not authority and re.search(r'\bcủa\b',citation,re.I):
        tail=authority_key(re.split(r'\bcủa\b',citation,maxsplit=1,flags=re.I)[-1])
        matches={h['_source'].get('issuing_authority') for h in hits if authority_key(h['_source'].get('issuing_authority')) and tail.startswith(authority_key(h['_source'].get('issuing_authority')))}
        if len(matches)==1:authority=matches.pop()
    if authority:
        candidate['target_issuing_authority']=authority
        eligible=[h for h in eligible if authority_key(h['_source'].get('issuing_authority'))==authority_key(authority)]
    if len(eligible)>1:
        same=[h for h in eligible if authority_key(h['_source'].get('issuing_authority'))==authority_key(candidate.get('source_issuing_authority'))]
        if same:eligible=same
        else:eligible=[]
    if len(eligible)>1:
        nearest=max(h['_source']['issuance_date'] for h in eligible)
        eligible=[h for h in eligible if h['_source']['issuance_date']==nearest]
    candidate['target_matched_id']=int(eligible[0]['_id']) if len(eligible)==1 else None
    return candidate
