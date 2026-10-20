"""Metadata normalisation copied from the ES v2 writer (contracts.py / metadata.py)."""
# Vendored from data_vbpl (legal_ingest) commit 22d9796; logic unchanged.
# See docs/adr/0001-v3-cluster-v2-schema.md.
from datetime import date
import re
import unicodedata

from .structure import normalize


def norm(text):
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "-", text)
    return " ".join(unicodedata.normalize("NFC", text).split())


def signer_parts(value):
    """Keep signature roles and names in source order, separated by commas."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if any(not isinstance(v, str) for v in values):
        raise ValueError('signers must contain strings')
    return [norm(part) for v in values for part in re.split(r'[,;\n]+|\s+[–—-]\s+', v) if norm(part)]



ACTIONS=[('bai-bo',r'b[ãả]i\s*bỏ'),('huy-bo',r'hu[yỷỹ]\s*bỏ'),('thay-the',r'thay\s*thế'),('sua-doi',r'sửa\s*đổi'),('bo-sung',r'bổ\s*sung')]


def number_key(value):
    value=unicodedata.normalize('NFC',value or '')
    value=''.join(c for c in value if not c.isspace() and c not in '\u200b\u200c\u200d\u2060\ufeff')
    return re.sub(r'^0+(?=\d+/)', '', value.replace('–','-').replace('—','-')).casefold()


def authority_key(value):
    value=unicodedata.normalize('NFD',(value or '').casefold()).replace('đ','d')
    value=''.join(c for c in value if not unicodedata.combining(c))
    value=re.sub(r'\bubnd\b','uy ban nhan dan',value)
    value=re.sub(r'\bhdnd\b','hoi dong nhan dan',value)
    value=re.sub(r'\s+',' ',value).strip()
    return ', '.join(sorted(part.strip() for part in value.split(',')))


def catalog_authority_key(value):
    value=authority_key(value)
    return re.sub(r'^(?:(?:chu tich )?(?:uy ban nhan dan|hoi dong nhan dan) )(?=(?:tinh|thanh pho|huyen|quan|thi xa|xa|phuong|thi tran) )','',value)


def action_matches(text):
    matches=[]
    for action,pattern in ACTIONS:
        for match in re.finditer(pattern,text,re.I):
            if action=='bo-sung' and not (re.search(r'sửa\s*đổi\s*,?\s*$',text[max(0,match.start()-20):match.start()],re.I)
                or re.match(r'\s*(?:một số|điều\s+\d|khoản\s+\d|điểm\s+[a-zđ]|nội dung|tên gọi)',text[match.end():],re.I)):
                continue
            matches.append((match.start(),match.end(),action));break
    return sorted(matches)


def document_action(title,legal_type):
    if legal_type=='Văn bản hợp nhất':return 'hopnhat'
    if legal_type=='Công văn':return 'congvan'
    if legal_type.startswith('Tiêu chuẩn'):return 'tcvn'
    matches=action_matches(title)
    if matches:return matches[0][2]
    return 'huongdan' if 'hướng dẫn' in title.casefold() else 'other'


def normalize_metadata(meta,catalog,no_number=False):
    result=dict(meta)
    for key in ['title','legal_type','issuing_authority','issuance_date','document_number','tom_tat']:
        value=result.get(key)
        if value is not None and not isinstance(value,str):raise ValueError(f'{key} must be string or null')
        result[key]=normalize(value) if value else None
    if not result.get('title') or not result.get('legal_type') or not result.get('issuing_authority'):
        raise ValueError('title, legal_type and issuing_authority are required')
    if result['legal_type'] not in catalog['legal_type']:
        raise ValueError('legal_type must be in the ES catalog')
    value=result.get('document_number')
    if number_key(value) in ('','khôngsố','khongso','n/a','null','-'):
        if not no_number:raise ValueError('Missing document number requires acknowledge_no_number=true')
        result['document_number']=None
    else:
        result['document_number']=re.sub(r'\s+','',value).replace('–','-').replace('—','-')
    if result.get('issuance_date'):
        value=result['issuance_date']
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}',value):
            day,month,year=map(int,value.split('/'));value=date(year,month,day).isoformat()
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('issuance_date must be YYYY-MM-DD')
        result['issuance_date']=date.fromisoformat(value).isoformat()
    else:
        raise ValueError('issuance_date is required for confirmed metadata')
    sectors=result.get('legal_sectors',[])
    if not isinstance(sectors,list) or any(not isinstance(s,str) for s in sectors):
        raise ValueError('legal_sectors must be a string array')
    if set(sectors)-set(catalog['legal_sectors']):raise ValueError('Unknown legal_sectors')
    result['legal_sectors']=list(dict.fromkeys(sectors))
    authority=result['issuing_authority']
    candidates=[v for v in catalog['issuing_authority'] if authority_key(v)==authority_key(authority)]
    if not candidates:
        candidates=[v for v in catalog['issuing_authority'] if catalog_authority_key(v)==catalog_authority_key(authority)]
    if len(candidates)==1:result['issuing_authority']=candidates[0]
    result['signers']=', '.join(signer_parts(result.get('signers'))) or None
    result['action']=document_action(result['title'],result['legal_type'])
    return result


