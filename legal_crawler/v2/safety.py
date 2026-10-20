"""Hard write guards for cluster 23's LAN/Tailscale endpoints and the three v2 indexes."""
import os
from urllib.parse import urlsplit

# Absolutely never written by this project (standing instruction, 2026-10-19).
FORBIDDEN_HOSTS = {"10.1.10.10"}
# Cluster 23's Tailscale endpoint was explicitly supplied by the user.
ALLOWED_WRITE_HOSTS = {"10.1.10.23", "100.77.4.35"}
ALLOWED_INDEXES = {"legal-docs", "legal-chunk-section-v2", "legal-changed-v2"}


class WriteForbidden(RuntimeError):
    pass


def host(url):
    return (urlsplit(url).hostname or "").lower()


def forbid_cluster_10(url):
    """Call on every URL this project talks to, reads included: cluster 10 is not used at all any more."""
    if host(url) in FORBIDDEN_HOSTS:
        raise WriteForbidden(f"Cluster {host(url)} is forbidden for this project")


def check_write(url, *indexes):
    forbid_cluster_10(url)
    if host(url) not in ALLOWED_WRITE_HOSTS:
        raise WriteForbidden(f"Writes are only allowed on {sorted(ALLOWED_WRITE_HOSTS)}, not {host(url)!r}")
    for index in indexes:
        if index not in ALLOWED_INDEXES:
            raise WriteForbidden(f"Index {index!r} is not an allowed write target")


def writes_enabled():
    return os.getenv("ES_WRITE_ENABLED", "no").lower() == "yes"
