"""Discover events hosted by G-Live-based timing providers.

Both known providers publish a page listing every event they've timed, but
in different shapes:

  Argeus Timing (argeustiming.com) - homepage is a Linktree-style bio page:
    a flat list of <a href> links, each pointing directly at a g-live viewer
    URL (.../g-live.html?f=...clax).

  PassTiming (passtiming.org) - the /yarisma-sonuclari archive page links to
    one marketing page per event (e.g. /konyaultra_2026); the g-live viewer
    is embedded in an <iframe> on that page (see fetch._find_embedded_viewer_url).

We don't trust any display name scraped from these listing pages - once a
race is actually fetched, its real name/date/organizer come from the
authoritative attributes in its own .clax document (see parse.parse_race),
or, for providers whose .clax `nom` is just a raw file slug, from the event
page's own <title> (see fetch.fetch_clax's title_hint).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from .fetch import USER_AGENT

ARGEUS_HOMEPAGE_URL = "https://argeustiming.com/"
PASSTIMING_ARCHIVE_URL = "https://www.passtiming.org/yarisma-sonuclari"
MERBE_LISTING_URL = "https://merbespor.com/event-listing-1/"

_ARGEUS_LINK_RE = re.compile(
    r'href="(https://argeustiming\.com/results/g-live/g-live\.html\?f=[^"]+)"'
)
_PASSTIMING_LINK_RE = re.compile(r'href="(/[a-zA-Z0-9_-]+)"')
_PASSTIMING_NAV_SLUGS = {
    "canli-sonuclar",
    "etkinlikler",
    "fotograflar",
    "hakkimizda",
    "iletisim",
    "yarisma-sonuclari",
    "yarisma-takvimi",
}
_MERBE_EVENT_LINK_RE = re.compile(r'href="https://merbespor\.com/event/([a-z0-9-]+)/"')


@dataclass
class DiscoveredRace:
    url: str
    slug: str
    provider: str


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()


def slug_from_viewer_url(viewer_url: str) -> str:
    """Derive a filesystem/db-safe slug from a g-live viewer URL's f= path.

    e.g. "...g-live.html?f=../uludagultra/2026/uput2026.clax"
         -> "uludagultra-2026-uput2026"
    """
    parsed = urlparse(viewer_url)
    f_param = parse_qs(parsed.query).get("f", [""])[0]
    path = f_param.lstrip("./")
    path = re.sub(r"\.clax$", "", path, flags=re.IGNORECASE)
    return _slugify(path)


def discover_argeus_races(homepage_url: str = ARGEUS_HOMEPAGE_URL, timeout: int = 30) -> List[DiscoveredRace]:
    """Fetch the argeustiming.com homepage and return every distinct event link found."""
    resp = requests.get(homepage_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    seen = set()
    races = []
    for url in _ARGEUS_LINK_RE.findall(html):
        if url in seen:
            continue
        seen.add(url)
        races.append(DiscoveredRace(url=url, slug=slug_from_viewer_url(url), provider="argeus"))
    return races


def discover_passtiming_races(archive_url: str = PASSTIMING_ARCHIVE_URL, timeout: int = 30) -> List[DiscoveredRace]:
    """Fetch the passtiming.org event archive and return every distinct event page found."""
    resp = requests.get(archive_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    seen = set()
    races = []
    for href in _PASSTIMING_LINK_RE.findall(html):
        slug_candidate = href.strip("/")
        if slug_candidate in seen or slug_candidate in _PASSTIMING_NAV_SLUGS:
            continue
        seen.add(slug_candidate)
        url = urljoin(archive_url, href)
        races.append(DiscoveredRace(url=url, slug=_slugify(slug_candidate), provider="passtiming"))
    return races


def discover_merbe_races(
    listing_url: str = MERBE_LISTING_URL, timeout: int = 20, max_pages: int = 30
) -> List[DiscoveredRace]:
    """Discover MerBe Timing (merbespor.com) races: another self-hosted G-Live
    instance. The marketing site paginates its /event/{slug}/ pages under
    /event-listing-1/[page/N/]; each event that has published results links
    to /results/{slug}/, a small wrapper page embedding the g-live viewer in
    an <iframe> (same shape fetch.fetch_clax already follows for PassTiming).
    Events without results yet (upcoming races) don't have a working
    /results/ page, so each candidate is probed and silently skipped if it
    doesn't actually embed a g-live viewer.
    """
    seen_slugs = set()
    page = 1
    while page <= max_pages:
        url = listing_url if page == 1 else urljoin(listing_url, f"page/{page}/")
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if resp.status_code != 200:
            break
        slugs = set(_MERBE_EVENT_LINK_RE.findall(resp.text))
        if not (slugs - seen_slugs) and page > 1:
            break
        seen_slugs |= slugs
        if f"page/{page + 1}/" not in resp.text:
            break
        page += 1

    races = []
    for slug in sorted(seen_slugs):
        results_url = urljoin("https://merbespor.com/results/", f"{slug}/")
        try:
            resp = requests.get(results_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and "g-live.html" in resp.text:
            races.append(DiscoveredRace(url=results_url, slug=f"merbe-{slug}", provider="merbe"))
    return races


def discover_races(
    providers: tuple = ("argeus", "passtiming", "plustiming", "hurratiming", "merbe")
) -> List[DiscoveredRace]:
    """Discover events across all (or a subset of) known timing providers."""
    races: List[DiscoveredRace] = []
    if "argeus" in providers:
        races.extend(discover_argeus_races())
    if "passtiming" in providers:
        races.extend(discover_passtiming_races())
    if "plustiming" in providers:
        from .plustiming import discover_plustiming_races  # local import: avoids a cycle

        races.extend(discover_plustiming_races())
    if "hurratiming" in providers:
        from .hurratiming import discover_hurratiming_races  # local import: avoids a cycle

        races.extend(discover_hurratiming_races())
    if "merbe" in providers:
        races.extend(discover_merbe_races())
    return races
