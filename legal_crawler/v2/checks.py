"""Evidence checks for LLM metadata (from data_vbpl metadata.py)."""
# Vendored from data_vbpl (legal_ingest) commit 22d9796; logic unchanged except where marked "CRAWLER:".
# See docs/adr/0001-v3-cluster-v2-schema.md.
from datetime import date
import re
import unicodedata

from .contracts import norm, signer_parts

TEMPLATE = {
    "action": None, "document_number": None, "title": None,
    "legal_type": None, "legal_sectors": [], "issuing_authority": None,
    "issuance_date": None, "signers": [], "tom_tat": None,
    "can_cu": [], "can_cu_refs": [], "noi_nhan": [],
    "intro": None, "has_tail": None,
    "phu_luc": [],
}




def quote_in_text(quote, text):
    if not isinstance(quote, str) or not quote.strip():
        return False
    if norm(quote).casefold() in norm(text).casefold():
        return True
    parts = [norm(line).casefold() for line in quote.splitlines() if line.strip()]
    if len(parts) < 2:
        return False

    def column(part, line):
        match = re.search(r"\s+".join(map(re.escape, part.split())), line)
        return match.start() if match else None

    # Match nearby lines in one PDF column for evidence validation.
    for page in text.split("\f"):
        lines = [unicodedata.normalize("NFC", line).casefold() for line in page.splitlines()]
        for index, line in enumerate(lines):
            start = column(parts[0], line)
            if start is None:
                continue
            last = index
            for part in parts[1:]:
                for next_line in range(last + 1, min(last + 9, len(lines))):
                    offset = column(part, lines[next_line])
                    if offset is not None and abs(offset - start) <= 8:
                        last = next_line
                        break
                else:
                    break
            else:
                return True
    return False




def check_extraction(payload, text, actions, sectors):
    if not isinstance(payload, dict) or set(payload) != {"metadata", "evidence"}:
        raise ValueError("LLM phải trả metadata và evidence")
    data, evidence = payload["metadata"], payload["evidence"]
    if not isinstance(data, dict) or set(data) != set(TEMPLATE):
        raise ValueError("LLM trả sai tập trường metadata")
    if not isinstance(evidence, dict):
        raise ValueError("evidence phải là object")
    for key, example in TEMPLATE.items():
        value = data[key]
        if isinstance(example, list):
            if not isinstance(value, list):
                raise ValueError(f"{key} phải là danh sách")
            if key != "phu_luc" and any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError(f"{key} phải chứa chuỗi không rỗng")
        elif key == "has_tail":
            if value is not None and type(value) is not bool:
                raise ValueError("has_tail phải là boolean hoặc null")
        elif value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} phải là chuỗi hoặc null")
    if not data["title"] or not data["tom_tat"]:
        raise ValueError("Thiếu tên hoặc tóm tắt văn bản")
    if data["action"] is not None and data["action"] not in actions:
        raise ValueError("action không nằm trong danh mục ES")
    if set(data["legal_sectors"]) - set(sectors):
        raise ValueError("legal_sectors có giá trị ngoài danh mục ES")
    if data["issuance_date"] is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["issuance_date"]):
            raise ValueError("issuance_date phải là YYYY-MM-DD")
        date.fromisoformat(data["issuance_date"])
    for key in ("document_number", "issuance_date", "issuing_authority", "signers"):
        if data[key]:
            quotes = evidence.get(key)
            if not isinstance(quotes, list) or not quotes:
                raise ValueError(f"Thiếu dẫn chứng {key}")
            for quote in quotes:
                if not quote_in_text(quote, text):
                    raise ValueError(f"Dẫn chứng {key} không có trong tài liệu")
    signature_quotes=list(evidence.get('signers', []))
    # Evidence may represent one wrapped title as multiple consecutive source lines.
    original_quotes=list(signature_quotes)
    for start in range(len(original_quotes)):
        for end in range(start+2,len(original_quotes)+1):
            joined='\n'.join(original_quotes[start:end])
            if quote_in_text(joined,text):signature_quotes.append(joined)
            else:break
    for part in signer_parts(data["signers"]):
        if not quote_in_text(part, text) or not any(quote_in_text(part, quote) for quote in signature_quotes):
            raise ValueError("Thành phần chữ ký không có trong nguồn/dẫn chứng")
    if data["document_number"] and not any(
        re.sub(r"\s+", "", data["document_number"]).casefold() in re.sub(r"\s+", "", quote).casefold()
        for quote in evidence["document_number"]
    ):
        raise ValueError("Số hiệu không khớp dẫn chứng")
    if data["issuance_date"]:
        dates = set()
        for quote in evidence["issuance_date"]:
            for day, month, year in re.findall(r"(?:ngày\s*)?(\d{1,2})\s*(?:/|-|tháng)\s*(\d{1,2})\s*(?:/|-|năm)\s*(\d{4})", quote, re.I):
                dates.add(f"{int(year):04}-{int(month):02}-{int(day):02}")
            dates.update(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", quote))
        if data["issuance_date"] not in dates:
            raise ValueError("Ngày ban hành không khớp dẫn chứng")
    for key in ("can_cu", "noi_nhan"):
        for value in data[key]:
            if not quote_in_text(value, text):
                raise ValueError(f"{key} phải trích nguyên văn từ nguồn")
    for ref in data["can_cu_refs"]:
        if re.sub(r"\s+", "", ref).casefold() not in re.sub(r"\s+", "", " ".join(data["can_cu"])).casefold():
            raise ValueError("can_cu_refs chứa số hiệu ngoài phần căn cứ")
    for appendix in data["phu_luc"]:
        if not isinstance(appendix, dict) or set(appendix) != {"tieu_de", "noi_dung"}:
            raise ValueError("phu_luc phải có tieu_de và noi_dung")
        for value in appendix.values():
            if not quote_in_text(value, text):
                raise ValueError("Phụ lục phải được trích nguyên văn từ nguồn")
    return data


