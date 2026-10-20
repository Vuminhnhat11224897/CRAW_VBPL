import logging
import re
import time
from urllib.parse import quote, urljoin, urlsplit

import requests


class FetchError(RuntimeError):
    def __init__(self, message, *, rotate=False, response_body=None):
        super().__init__(message)
        self.rotate = rotate
        self.response_body = response_body


def allowed(url, hosts):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (parsed.scheme in {"http", "https"} and not parsed.username
            and not parsed.password and port in {None, 80, 443}
            and any(host == h or host.endswith("." + h) for h in hosts))


class TMProxy:
    def __init__(self, settings):
        if not settings.api_key:
            raise ValueError("Set TMPROXY_API_KEY in .env before crawling")
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False
        self.url = None
        self.rotate_after = 0
        self.expires = float("inf")

    def call(self, endpoint):
        body = {"api_key": self.settings.api_key}
        if endpoint == "get-new-proxy":
            body.update(id_location=self.settings.location, id_isp=self.settings.isp)
        try:
            with self.session.post(self.settings.api_url + "/" + endpoint,
                                   json=body, timeout=self.settings.timeout) as response:
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (requests.RequestException, ValueError):
            raise FetchError("TMProxy API request failed; check network and key") from None

    def use(self, result):
        data = result.get("data") or {}
        if result.get("code") != 0 or not isinstance(data, dict) or not data.get("https"):
            return False
        endpoint = data["https"]
        if not isinstance(endpoint, str):
            raise FetchError("Invalid TMProxy HTTP endpoint")
        parsed = urlsplit(endpoint if "://" in endpoint else "http://" + endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise FetchError("Invalid TMProxy HTTP endpoint")
        auth = ""
        if data.get("username"):
            auth = quote(data["username"], safe="") + ":" + quote(data.get("password", ""), safe="") + "@"
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        self.url = f"{parsed.scheme}://{auth}{host}:{parsed.port}"
        now = time.monotonic()
        self.rotate_after = now + max(0, int(data.get("next_request") or 0))
        lifetime = int(data.get("timeout") or 0)
        self.expires = now + lifetime if lifetime > 0 else float("inf")
        return True

    def get(self, rotate=False):
        now = time.monotonic()
        if self.url and now < self.expires and (not rotate or now < self.rotate_after):
            return self.url
        if not self.url and self.use(self.call("get-current-proxy")):
            return self.url
        wait = max(0, self.rotate_after - time.monotonic())
        if wait > self.settings.proxy_wait:
            raise FetchError("TMProxy rotation cooldown exceeds TMPROXY_MAX_WAIT")
        time.sleep(wait)
        result = self.call("get-new-proxy")
        if not self.use(result):
            # The provider may return cooldown data after another client rotated this key.
            current = self.call("get-current-proxy")
            if not self.use(current):
                raise FetchError("TMProxy did not return an active proxy; check key/quota")
        return self.url


class Client:
    def __init__(self, settings):
        self.settings = settings
        self.proxy = TMProxy(settings)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": settings.user_agent})
        self.last_request = 0

    def response(self, method, url, hosts, data=None, rotate=False, headers=None):
        proxy = self.proxy.get(rotate)
        for _ in range(6):
            if not allowed(url, hosts):
                raise FetchError("URL/redirect is outside configured source hosts")
            time.sleep(max(0, self.settings.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = self.session.request(method, url, data=data, headers=headers,
                proxies={"http": proxy, "https": proxy}, timeout=self.settings.timeout,
                allow_redirects=False, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                destination = response.headers.get("Location")
                response.close()
                if not destination:
                    raise FetchError("Redirect without Location")
                url = urljoin(url, destination)
                if response.status_code in {301, 302, 303}:
                    method, data = "GET", None
                continue
            if response.status_code >= 400:
                status = response.status_code
                retry_after = response.headers.get("Retry-After", "")
                try:
                    body = next(iter(response.iter_content(65536)), b"")[:65536]
                except requests.RequestException:
                    body = None
                finally:
                    response.close()
                if status == 429 and retry_after.isdigit():
                    time.sleep(min(int(retry_after), self.settings.proxy_wait))
                raise FetchError(f"HTTP {status}", rotate=status in {403, 407, 429}, response_body=body)
            return response
        raise FetchError("Too many redirects")

    def fetch(self, url, hosts, *, method="GET", data=None, destination=None, headers=None, max_bytes=None):
        limit = max_bytes if max_bytes is not None else (self.settings.max_file if destination else self.settings.max_html)
        errors = []
        response_body = None
        rotate = False
        for attempt in range(self.settings.retries):
            try:
                with self.response(method, url, hosts, data, rotate=rotate, headers=headers) as response:
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > limit:
                        raise FetchError("Response exceeds configured size limit")
                    chunks, size = [], 0
                    output = destination.open("wb") if destination else None
                    try:
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > limit:
                                raise FetchError("Response exceeds configured size limit")
                            if output:
                                output.write(chunk)
                            else:
                                chunks.append(chunk)
                    finally:
                        if output:
                            output.close()
                    if not size:
                        raise FetchError("Empty response")
                    return b"".join(chunks), response.url, response.headers.get("Content-Type", "")
            except (requests.RequestException, FetchError) as exc:
                # requests exceptions can contain proxy credentials; never persist their text.
                error = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
                if isinstance(exc, requests.exceptions.SSLError):
                    reason = re.search(r"\[SSL: ([A-Z0-9_]+)\]", str(exc))
                    if reason:
                        error += ": " + reason[1]
                errors.append(error)
                if isinstance(exc, FetchError) and exc.response_body:
                    response_body = exc.response_body
                rotate = exc.rotate if isinstance(exc, FetchError) else isinstance(exc, (requests.ConnectionError, requests.Timeout)) and not isinstance(exc, requests.exceptions.SSLError)
                logging.getLogger(__name__).warning("%s attempt %d/%d: %s", urlsplit(url).hostname, attempt + 1, self.settings.retries, error)
                if destination:
                    destination.unlink(missing_ok=True)
                if attempt + 1 < self.settings.retries:
                    time.sleep(min(2 ** attempt, 30))
        raise FetchError("; ".join(dict.fromkeys(errors)), response_body=response_body)

    def close(self):
        self.session.close()
        self.proxy.session.close()
