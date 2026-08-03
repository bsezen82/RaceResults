"""Download a G-Live (.clax) results feed.

G-Live is a white-labeled timing viewer used by multiple timing companies
under their own domains (argeustiming.com, passtiming.org, ...). A viewer
page at .../g-live.html?f=<path>.clax looks like a paginated app, but the
?f= query string actually points at one plain (BOM-prefixed) XML document
served with an octet-stream content type, containing the entire event:
entrants, results, checkpoints, categories. No pagination or browser
rendering is needed - a single GET is enough.

Some timing sites (e.g. passtiming.org/<event-slug>) instead show a
marketing/embed page with the g-live.html viewer inside an <iframe>. We
follow that automatically so a user can paste whatever URL they see in
their browser, not just the underlying viewer link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; RaceResultsScraper/1.0)"

_IFRAME_RE = re.compile(r'<iframe[^>]*\ssrc="([^"]*g-live\.html[^"]*)"', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class FetchResult:
    xml_text: str
    # Some timing sites (e.g. Argeus) put a real event name in the .clax
    # itself; others (e.g. PassTiming) only put the raw file slug there. When
    # we had to follow a marketing/embed page to find the viewer, that page's
    # <title> is often a nicer name - passed back for the caller to prefer.
    title_hint: Optional[str] = None


def _extract_title(html: str) -> Optional[str]:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    # Common pattern: "Event Name | Site Brand" / "Event Name - Site Brand"
    for sep in (" | ", " - "):
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    return title or None


def clax_url_from_viewer_url(viewer_url: str) -> str:
    """Given a g-live.html?f=... viewer URL, resolve the actual .clax data URL."""
    parsed = urlparse(viewer_url)
    qs = parse_qs(parsed.query)
    f_param = qs.get("f", [None])[0]
    if not f_param:
        raise ValueError(f"No f= parameter found in viewer URL: {viewer_url}")
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return urljoin(base, f_param)


def _find_embedded_viewer_url(page_url: str, html: str) -> str:
    match = _IFRAME_RE.search(html)
    if not match:
        raise ValueError(
            f"No .clax link or G-Live <iframe> found on page: {page_url}"
        )
    return urljoin(page_url, match.group(1))


def fetch_clax(url: str, timeout: int = 30) -> FetchResult:
    """Fetch a .clax URL, a direct g-live.html viewer URL, or a page that embeds
    one in an <iframe> (e.g. a timing company's per-event marketing page).
    """
    title_hint = None
    # Check "g-live.html" membership before the .clax suffix: a viewer URL's
    # own f= query parameter ends in ".clax" too (.../g-live.html?f=...clax),
    # so testing suffix first would wrongly treat it as an already-resolved
    # data URL and skip resolving it via clax_url_from_viewer_url.
    if "g-live.html" in url:
        url = clax_url_from_viewer_url(url)
    elif not url.lower().endswith(".clax"):
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        title_hint = _extract_title(resp.text)
        viewer_url = _find_embedded_viewer_url(url, resp.text)
        url = clax_url_from_viewer_url(viewer_url)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    # File is UTF-8 with a BOM; requests' apparent-encoding guess isn't reliable
    # for an octet-stream content-type, so decode explicitly.
    xml_text = resp.content.decode("utf-8-sig")
    return FetchResult(xml_text=xml_text, title_hint=title_hint)
