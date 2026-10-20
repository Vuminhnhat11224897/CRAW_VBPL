import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

from bs4 import BeautifulSoup

from .http import allowed


LABELS = {
    "number": ["Số ký hiệu", "Số, ký hiệu", "Số hiệu"],
    "issued_date": ["Ngày ban hành", "Ban hành"],
    "effective_date": ["Ngày có hiệu lực", "Ngày hiệu lực", "Hiệu lực"],
    "document_type": ["Loại văn bản"],
    "issuer": ["Cơ quan ban hành"],
    "signer": ["Người ký"],
    "summary": ["Trích yếu"],
}


def canonical(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(sorted(query)), ""))


def document_id(source, url):
    p = urlsplit(url)
    params = {k.lower(): v for k, v in parse_qsl(p.query)}
    if "docid" in params:
        key = params["docid"]
    elif "itemid" in params:
        key = p.path.split("/")[1].lower() + ":" + params["itemid"]
    else:
        match = re.search(r"-(\d+)\.htm$", p.path)
        key = match[1] if match else canonical(url)
    return source + ":" + hashlib.sha256(key.encode()).hexdigest()[:24]


def links(soup, base):
    found = {}
    for node in soup.select("[href], iframe[src], embed[src], object[data], [data-url], [data-src], [data-href]"):
        for attr in ("href", "src", "data", "data-url", "data-src", "data-href"):
            value = node.get(attr, "").strip()
            if not value or value.startswith(("#", "javascript:", "mailto:")):
                continue
            url = canonical(urljoin(base, value))
            found[url] = node.get_text(" ", strip=True)
            # PDF viewers sometimes wrap the actual attachment in ?file= or ?url=.
            for key, target in parse_qsl(urlsplit(url).query):
                if key.lower() in {"file", "url"}:
                    found[canonical(urljoin(base, target))] = found[url]
    for node in soup.select("[onclick]"):
        for value in re.findall(r"['\"]([^'\"]+\.(?:pdf|docx?|rtf|zip)(?:\?[^'\"]*)?)['\"]", node["onclick"], re.I):
            found[canonical(urljoin(base, value))] = node.get_text(" ", strip=True)
    return found


def parse_date(value):
    match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", value)
    if match:
        try:
            return datetime(int(match[3]), int(match[2]), int(match[1])).date().isoformat()
        except ValueError:
            pass
    return None


def metadata(soup, config):
    for node in soup.select("script, style, noscript"):
        node.decompose()
    section = soup.select_one(config["metadata_selector"]) if config.get("metadata_selector") else None
    text = " ".join((section if section is not None else soup).stripped_strings)
    all_labels = [label for labels in LABELS.values() for label in labels]
    stop = "|".join(re.escape(label) for label in sorted(all_labels + ["Tài liệu đính kèm", "Tải về", "Công báo"], key=len, reverse=True))
    result = {field: None for field in LABELS}
    for field, labels in LABELS.items():
        for label in labels:
            matches = re.finditer(re.escape(label) + r"\s*:?\s*(.*?)(?=" + stop + r"|$)", text, re.I)
            for match in matches:
                value = match[1].strip(" :-[]")
                if field.endswith("_date"):
                    immediate = re.match(r"\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", value)
                    value = parse_date(immediate[0]) if immediate else None
                elif len(value) > 2000:
                    value = None
                if value:
                    result[field] = value
                    break
            if result[field]:
                break
    title = None
    for selector in config["title_selectors"]:
        node = soup.select_one(selector)
        if node and node.get_text(" ", strip=True):
            title = node.get_text(" ", strip=True)
            break
    result["title"] = result["summary"] or title
    return result


def parse_page(content, url, config):
    soup = BeautifulSoup(content, "html.parser")
    requires_javascript = bool(soup.select('script[src*="/_next/static/"]'))
    found = links(soup, url)
    details, listings, files, tabs = {}, [], [], []
    for target, label in found.items():
        if allowed(target, config["file_hosts"]) and re.search(config["attachment_pattern"], unquote(urlsplit(target).path), re.I):
            files.append(target)
        elif allowed(target, config["page_hosts"]):
            if re.search(config["detail_pattern"], target, re.I):
                details[target] = label
            elif re.search(config["listing_pattern"], target, re.I):
                listings.append(target)
            if re.search(config["tab_pattern"], target, re.I):
                tabs.append(target)
    postbacks = []
    for form in soup.select("form"):
        fields = {n["name"]: n.get("value", "") for n in form.select("input[name]")
                  if n.get("type", "").lower() not in {"submit", "button", "image", "checkbox", "radio", "file"}}
        for select in form.select("select[name]"):
            option = select.select_one("option[selected]") or select.select_one("option")
            if option:
                fields[select["name"]] = option.get("value", option.text)
        for node in form.select("a[href], [onclick]"):
            script = node.get("href", "") + node.get("onclick", "")
            match = re.search(r"__doPostBack\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]*)['\"]\)", script)
            label = node.get_text(" ", strip=True).lower()
            if match and (label.isdigit() or label in {">", ">>", "…", "...", "tiếp", "trang sau", "next"} or re.match(r"Page\$", match[2], re.I)):
                body = dict(fields, __EVENTTARGET=match[1], __EVENTARGUMENT=match[2])
                action = canonical(urljoin(url, form.get("action") or url))
                if allowed(action, config["page_hosts"]):
                    postbacks.append({"url": action, "method": "POST", "body": body})
    return {"details": details, "listings": listings, "files": files,
            "tabs": tabs, "postbacks": postbacks, "requires_javascript": requires_javascript,
            "metadata": metadata(soup, config)}
