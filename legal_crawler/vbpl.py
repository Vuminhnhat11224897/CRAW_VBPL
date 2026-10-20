import base64
import binascii
import hashlib
import json
import re
import unicodedata
from datetime import date
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .http import FetchError
from .parser import parse_date


def action_result(content):
    """Decode the JSON/text rows used by VBPL's public Next server actions."""
    rows = {}
    position = 0
    row_header = re.compile(rb"([0-9a-f]+):")
    while position < len(content):
        if content[position:position + 1] in {b"\n", b"\r"}:
            position += 1
            continue
        match = row_header.match(content, position)
        if not match:
            raise FetchError("VBPL action returned an unsupported response; inspect saved .flight")
        key = match[1].decode()
        position = match.end()
        if content[position:position + 1] == b"T":
            end = content.find(b",", position)
            try:
                length = int(content[position + 1:end], 16)
                position = end + 1
                value = content[position:position + length].decode("utf-8")
                if len(content[position:position + length]) != length:
                    raise ValueError()
                position += length
            except (ValueError, UnicodeDecodeError):
                raise FetchError("VBPL returned an invalid text row") from None
            rows[key] = value
        else:
            end = content.find(b"\n", position)
            if end < 0:
                end = len(content)
            value = content[position:end]
            position = min(end + 1, len(content))
            if value.startswith(b"E"):
                message = "VBPL server action failed"
                try:
                    error = json.loads(value[1:])
                    digest = error.get("digest") if isinstance(error, dict) else None
                    if isinstance(digest, str) and re.fullmatch(r"[0-9]{1,20}", digest):
                        message += " (digest " + digest + ")"
                except (ValueError, UnicodeDecodeError):
                    pass
                rows[key] = FetchError(message)
            else:
                try:
                    rows[key] = json.loads(value)
                except (ValueError, UnicodeDecodeError):
                    # React module/debug rows are not part of the action's data result.
                    continue

    def resolve(value, visiting=frozenset()):
        if isinstance(value, FetchError):
            raise value
        if isinstance(value, list):
            return [resolve(v, visiting) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v, visiting) for k, v in value.items()}
        if isinstance(value, str):
            if value.startswith("$$"):
                return value[1:]
            if value == "$undefined":
                return None
            if value.startswith("$D"):
                return value[2:]
            match = re.fullmatch(r"\$@?([0-9a-f]+)", value)
            if match:
                key = match[1]
                if key not in rows or key in visiting:
                    raise FetchError("VBPL action contains an unresolved/cyclic reference")
                return resolve(rows[key], visiting | {key})
        return value

    root = rows.get("0")
    if isinstance(root, dict) and "a" in root:
        return resolve(root["a"])
    if isinstance(root, list) and root:
        return resolve(root[0])
    raise FetchError("VBPL action result was not found; inspect saved .flight")


def issued_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return parse_date(value)


class VBPL:
    def __init__(self, settings, client, store):
        self.settings, self.client, self.store = settings, client, store
        self.config = settings.configs["vbpl"]

    def request(self, action, arguments):
        return {"method": "POST", "data": json.dumps(arguments, ensure_ascii=False),
                "headers": {"Next-Action": self.config["actions"][action],
                            "Accept": "text/x-component", "Content-Type": "text/plain;charset=UTF-8",
                            "Next-Router-State-Tree": quote(json.dumps(self.config["router_state"], separators=(",", ":")), safe=""),
                            "Origin": "https://vbpl.vn", "Referer": self.config["action_url"]}}

    def action(self, name, arguments):
        key = hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()
        path = self.settings.data / "raw/vbpl" / (key + ".flight")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            content, _, _ = self.client.fetch(self.config["action_url"], self.config["page_hosts"],
                                             **self.request(name, arguments))
        except FetchError as exc:
            if exc.response_body:
                error_path = path.with_suffix(".error")
                error_path.write_bytes(exc.response_body)
                reason = str(exc)
                try:
                    action_result(exc.response_body)
                except FetchError as source_error:
                    reason += "; " + str(source_error)
                raise FetchError(f"{reason}; response saved to {error_path}", response_body=exc.response_body) from None
            raise
        path.write_bytes(content)
        return action_result(content), str(path.relative_to(self.settings.data))

    def page(self, payload):
        url = payload["url"]
        match = re.fullmatch(r"/van-ban/chi-tiet/([A-Za-z0-9_-]+)", urlsplit(url).path)
        result = {"details": {}, "listings": [], "files": [], "tabs": [], "postbacks": [], "metadata": {}}
        if not match:
            try:
                page_number = int(parse_qs(urlsplit(url).query).get("pageNumber", ["1"])[0])
                if page_number < 1:
                    raise ValueError()
            except ValueError:
                raise FetchError("Invalid VBPL page number") from None
            filters = {"pageNumber": page_number, "pageSize": self.settings.vbpl_page_size,
                       "sortBy": "issueDate", "sortDirection": "desc"}
            if self.settings.date_from:
                filters["issueDateFrom"] = self.settings.date_from + "T00:00:00"
            if self.settings.date_to:
                filters["issueDateTo"] = self.settings.date_to + "T23:59:59"
            data, result["raw_path"] = self.action("listing", [filters])
            if not isinstance(data, dict) or not isinstance(data.get("items"), list) or type(data.get("total")) is not int or data["total"] < 0:
                raise FetchError("VBPL listing schema changed: expected items and total")
            for item in data["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item["id"]):
                    raise FetchError("VBPL listing returned an invalid document ID")
                result["details"]["https://vbpl.vn/van-ban/chi-tiet/" + item["id"]] = item.get("title")
            if page_number * self.settings.vbpl_page_size < data["total"]:
                if not data["items"]:
                    raise FetchError("VBPL returned an empty page before its reported total")
                result["listings"].append(self.config["action_url"] + "?" + urlencode({"pageNumber": page_number + 1}))
            result["empty_listing"] = not data["items"] and data["total"] == 0
            return result
        identifier = match[1]
        document, result["raw_path"] = self.action("detail", [identifier])
        if not isinstance(document, dict) or str(document.get("id")) != identifier:
            raise FetchError("VBPL detail schema changed or document ID mismatch")
        result["metadata"] = {
            "title": document.get("title"), "number": document.get("docNum"),
            "summary": document.get("title"), "document_type": document.get("docTypeName"),
            "issuer": document.get("agencyName"), "signer": document.get("signer"),
            "issued_date": issued_date(document.get("issueDate")),
            "effective_date": issued_date(document.get("effFrom")),
        }
        published = result["metadata"]["issued_date"]
        if (self.settings.date_from or self.settings.date_to) and not published:
            return result
        if published and ((self.settings.date_from and published < self.settings.date_from)
                          or (self.settings.date_to and published > self.settings.date_to)):
            return result
        files, raw = self.action("files", [identifier])
        result["extra_raw_pages"] = [raw]
        if not isinstance(files, list):
            raise FetchError("VBPL file listing schema changed: expected a list")
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("fileName"), str) or not item["fileName"]:
                raise FetchError("VBPL returned an invalid filename")
            if str(item.get("relatedType")) not in {"3", "5"}:
                result["files"].append(self.config["action_url"] + "#" + urlencode({"documentId": identifier, "fileName": item["fileName"]}))
        return result

    def download(self, url):
        cached = self.store.cached_file("vbpl", url)
        if cached:
            return cached
        params = parse_qs(urlsplit(url).fragment)
        identifier, filename = params["documentId"][0], params["fileName"][0]
        # The listing may return decomposed (NFD) names, and the storage key is either the composed (NFC)
        # name or its percent-encoded form depending on how the file was uploaded; try each spelling.
        composed = unicodedata.normalize("NFC", filename)
        names = list(dict.fromkeys([composed, quote(composed), filename, quote(filename)])) if not filename.isascii() else [filename]
        arguments = [{"bucketName": "vbpl", "folderName": identifier, "objectName": names[0]}]
        directory = self.settings.data / "files" / "vbpl"
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / (hashlib.sha256(url.encode()).hexdigest() + ".part")
        try:
            for index, name in enumerate(names):
                arguments = [{"bucketName": "vbpl", "folderName": identifier, "objectName": name}]
                try:
                    _, final, _ = self.client.fetch(self.config["action_url"], self.config["file_hosts"],
                        destination=temp, max_bytes=4 * ((self.settings.max_file + 2) // 3) + self.settings.max_html,
                        **self.request("download", arguments))
                    break
                except FetchError:
                    if index + 1 == len(names):
                        raise
            result = action_result(temp.read_bytes())
            if not isinstance(result, dict) or not isinstance(result.get("data"), str):
                raise FetchError("VBPL file action returned an invalid payload")
            try:
                content = base64.b64decode(result["data"], validate=True)
            except (ValueError, binascii.Error):
                raise FetchError("VBPL file action returned invalid base64") from None
            if not content or len(content) > self.settings.max_file:
                raise FetchError("VBPL decoded file is empty or exceeds MAX_FILE_MB")
            temp.write_bytes(content)
            request = {"method": "POST", "url": self.config["action_url"],
                       "action": self.config["actions"]["download"], "arguments": arguments}
            return self.store.save_download("vbpl", temp, url, final, result.get("mimeType") or "application/octet-stream", request)
        finally:
            temp.unlink(missing_ok=True)
