"""Best-effort `<title>` extraction for bookmarks saved without one.

Fetching a user-supplied URL from the server is a server-side request forgery
risk: "save http://169.254.169.254/latest/meta-data" would otherwise turn the
bookmark tool into a proxy for cloud metadata. So every hop is validated:

- only http and https,
- the hostname must resolve exclusively to public addresses,
- redirects are followed manually so each new target is re-validated rather
  than trusted because the first one passed,
- the body is streamed and capped, so a huge or endless response cannot exhaust
  memory.

Every failure returns None. A title is a nicety; it must never stop a bookmark
being saved.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 64 * 1024
MAX_TITLE_LENGTH = 200

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_USER_AGENT = "StashBookmarks/1.0 (+https://github.com/)"


def _resolves_to_public_address(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False

    addresses = {result[4][0] for result in results}
    if not addresses:
        return False

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _extract(markup: str) -> str | None:
    match = _TITLE_RE.search(markup)
    if not match:
        return None
    title = html.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None
    return title[:MAX_TITLE_LENGTH]


def fetch_title(url: str) -> str | None:
    """Return the page's title, or None if it cannot be fetched safely."""
    current = url
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https"):
                    return None
                if not _resolves_to_public_address(parsed.hostname):
                    logger.info("Refusing to fetch non-public host %r", parsed.hostname)
                    return None

                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue

                    if response.status_code >= 400:
                        return None
                    if "html" not in response.headers.get("content-type", "").lower():
                        return None

                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BODY_BYTES:
                            break

                    markup = b"".join(chunks)[:MAX_BODY_BYTES].decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                    return _extract(markup)
    except (httpx.HTTPError, UnicodeDecodeError, ValueError) as exc:
        logger.info("Could not read a title from %s: %s", url, exc)
        return None

    # Ran out of redirect budget.
    return None
