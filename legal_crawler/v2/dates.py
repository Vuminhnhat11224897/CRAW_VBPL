"""Locate issue / signing / effective dates by page quadrant (rules given for the nightly flow).

  issue date      top-right quadrant of the first page   ("Hà Nội, ngày 01 tháng 10 năm 2026")
  signing date    bottom-right quadrant of the last page, or a digital-signature stamp ("Ngày ký: ...")
  effective date  left quadrants, only in a sentence that says "hiệu lực" (otherwise None, never guessed)

The effective date taken from the source web page always wins; this is the fallback when the page has none.
Every result keeps the page, position and quoted line so the comparison report can show why.
"""
import re
from datetime import date

VN = re.compile(r"ngày\s*(\d{1,2})\s*tháng\s*(\d{1,2})\s*năm\s*(\d{4})", re.I)
SLASH = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")
ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
STAMP = re.compile(r"Ngày ký|Thời gian ký|Ký ngày", re.I)
# Only the document's own sentence ("Nghị định này có hiệu lực ..."), never a quoted or cited one.
OWN_EFFECT = r"\bnày\s+(?:có\s+)?hiệu lực"
# A Gazette (Công báo) stamp is the Gazette's signature, not the issuing body's.
PLACE_DATE = re.compile(r"[^\W\d_][^,]{1,40},\s*ngày\s*\d{1,2}\s*tháng\s*\d{1,2}\s*năm\s*\d{4}", re.I)
SIGN_WINDOW_DAYS = 60
GAZETTE = re.compile(r"CÔNG BÁO", re.I)


def dates_in(text):
    found = []
    for pattern, order in ((VN, "dmy"), (SLASH, "dmy"), (ISO, "ymd")):
        for match in pattern.finditer(text):
            a, b, c = (int(x) for x in match.groups())
            day, month, year = (a, b, c) if order == "dmy" else (c, b, a)
            try:
                found.append(date(year, month, day).isoformat())
            except ValueError:
                continue
    return found


def pdf_items(path):
    """Text-layer PDF -> [{'page', 'items': [(x, y, w, h, text)]}] on the first and last page only."""
    import pymupdf
    document = pymupdf.open(path)
    pages = []
    for index in sorted({0, len(document) - 1}):
        page = document[index]
        width, height = page.rect.width, page.rect.height
        items = [(b[0] / width, b[1] / height, (b[2] - b[0]) / width, (b[3] - b[1]) / height, " ".join(b[4].split()))
                 for b in page.get_text("blocks") if b[4].strip()]
        pages.append({"page": index + 1, "items": items, "last": index == len(document) - 1})
    document.close()
    return pages


def locate(pages):
    """Apply the quadrant rules to pages ({'page','items'}, first and last page are enough)."""
    pages = sorted(pages, key=lambda p: p["page"])
    first, last = pages[0], pages[-1]
    result = {"issue": None, "sign": None, "effective": None}

    def hit(value, page, item):
        return {"date": value, "page": page["page"], "x": round(item[0], 3), "y": round(item[1], 3), "line": item[4][:160]}

    for item in first["items"]:  # issue date: top-right of page 1, a place+date line, not a "Căn cứ ... ngày" citation
        if item[0] + item[2] / 2 > 0.5 and item[1] < 0.5 and VN.search(item[4]) and not re.search(r"căn cứ|số\s*\d", item[4], re.I):
            result["issue"] = hit(VN_first(item[4]), first, item)
            break
    for page in (first, last):  # signing date: digital stamp anywhere, else bottom-right of the last page
        for item in page["items"]:
            if STAMP.search(item[4]) and dates_in(item[4]) and not GAZETTE.search(item[4]):
                result["sign"] = hit(dates_in(item[4])[-1], page, item)
                break
        if result["sign"]:
            break
    if not result["sign"]:  # bottom-right of the last page: only a "<place>, ngày d tháng m năm y" line counts
        for item in last["items"]:
            if item[0] + item[2] / 2 > 0.5 and item[1] > 0.5 and PLACE_DATE.search(item[4]):
                result["sign"] = hit(dates_in(item[4])[-1], last, item)
                break
    if result["sign"] and result["issue"]:  # a signing date far from the issue date is body text, not a signature
        gap = abs((date.fromisoformat(result["sign"]["date"]) - date.fromisoformat(result["issue"]["date"])).days)
        if gap > SIGN_WINDOW_DAYS:
            result["sign"] = None
    for page in (first, last):  # effective date: left quadrants, only with an explicit "hiệu lực" sentence
        for item in page["items"]:
            own = re.search(OWN_EFFECT, item[4], re.I)
            if item[0] + item[2] / 2 < 0.5 and own:
                after = item[4][own.end():]
                if dates_in(after):
                    result["effective"] = hit(dates_in(after)[0], page, item)
                    break
        if result["effective"]:
            break
    if not result["effective"]:  # "có hiệu lực kể từ ngày ký ban hành": effective on the signing / issue date
        for page in (first, last):
            for item in page["items"]:
                if item[0] + item[2] / 2 < 0.5 and re.search(OWN_EFFECT + r".*(?:ngày ký|ngày ban hành)", item[4], re.I):
                    source = result["sign"] or result["issue"]
                    if source:
                        result["effective"] = {**source, "line": item[4][:160], "page": page["page"], "derived_from": "sign" if result["sign"] else "issue"}
                    break
            if result["effective"]:
                break
    return result


def VN_first(text):
    return dates_in(VN.search(text).group(0))[0]
