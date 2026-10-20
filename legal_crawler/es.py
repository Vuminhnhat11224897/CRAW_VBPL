"""Duplicate check against the existing Elasticsearch document index (read-only)."""
import re
import unicodedata

import requests

from .http import FetchError
from .v2 import safety


# Sources sometimes type Cyrillic look-alikes inside Latin numbers (e.g. "NĐ-CР"); fold them for matching.
HOMOGLYPHS = str.maketrans("АВЕКМНОРСТХаеорсух", "ABEKMHOPCTXaeopcyx")


def fold(number):
    return unicodedata.normalize("NFC", number or "").translate(HOMOGLYPHS)


def normalize(number):
    return re.sub(r"\s+", "", fold(number)).upper()


class ESDedupe:
    def __init__(self, settings):
        safety.forbid_cluster_10(settings.es_url)
        self.url = settings.es_url.rstrip("/")
        self.index = settings.es_index
        self.timeout = settings.timeout
        self.session = requests.Session()
        self.session.trust_env = False  # internal network: never via TMProxy

    def find(self, number, issued_date):
        """Return the ES _id of a document with the same number AND issue date, else None."""
        if not number or not issued_date:
            return None
        number = unicodedata.normalize("NFC", number)
        variants = list(dict.fromkeys(v for n in (number, fold(number)) for v in (n, n.strip(), re.sub(r"\s+", "", n))))
        body = {"size": 20, "_source": ["document_number", "issuance_date"], "query": {"bool": {"filter": [
            {"terms": {"document_number": variants}},
            {"range": {"issuance_date": {"gte": issued_date, "lte": issued_date, "format": "yyyy-MM-dd"}}}]}}}
        try:
            response = self.session.post(f"{self.url}/{self.index}/_search", json=body, timeout=self.timeout)
            response.raise_for_status()
            hits = response.json()["hits"]["hits"]
        except (requests.RequestException, KeyError, ValueError):
            raise FetchError("Elasticsearch duplicate check failed; will retry") from None
        for hit in hits:
            if normalize(hit["_source"].get("document_number")) == normalize(number):
                return hit["_id"]
        return None

    def close(self):
        self.session.close()
