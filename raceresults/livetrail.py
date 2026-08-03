"""Scrape LiveTrail (livetrail.net) - a French live-tracking/results
platform hosting many international trail races, unrelated to the other
providers in this package. No anti-bot protection.

Each race edition lives at /histo/{slug}/ (e.g. utcappadocia_2021) and
serves a custom XML+XSL format (like G-Live's .clax, but a different
schema). The home page (/histo/{slug}/) lists that edition's courses and
their podium only. The full per-course results table comes from a
different endpoint: POST classement.php with course=<id>&cat=scratch&
pays=all returns every finisher for that course in one response (no
pagination needed in practice - courses here run in the hundreds/low
thousands of finishers, all returned in a single call).

Scope note: this module only scrapes results (name, time, overall/category
rank) - not per-checkpoint splits. LiveTrail exposes those per runner via a
separate detail page, which would mean one extra HTTP request per finisher;
skipped to keep scraping cost reasonable. Category rank comes straight from
the API (`classcat`); gender rank isn't given directly (the same query
scoped to cat=scratchH/scratchF would give it, at the cost of 2x the
requests) so we derive it locally by sorting each course's finishers by
sex and time.

This module also doesn't attempt to discover *all* of livetrail.net's
races (it's a large, general international platform) - just the editions
of one named event, found by probing /histo/{base_slug}_{year}/ across a
year range.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests

from .discover import DiscoveredRace
from .fetch import USER_AGENT
from .models import Course, Race, Runner

_TIME_RE = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})$")
_COURSE_ENTRY_RE = re.compile(r'<e id="(\w+)"[^>]*titre="([^"]+)"[^>]*sstitre="([^"]*)"')
_DISTANCE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*km", re.IGNORECASE)
_INFO_RE = re.compile(r'<info nomdep="[^"]*" dtdep="(\d{4}-\d{2}-\d{2})[^"]*"')
_RESULT_ROW_RE = re.compile(r"<c ([^>]*)></c>")
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _distance_m_from_sstitre(sstitre: str) -> Optional[int]:
    match = _DISTANCE_RE.search(sstitre)
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")) * 1000)


def _parse_clock(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _base_url(event_slug: str) -> str:
    return f"https://livetrail.net/histo/{event_slug}/"


def edition_exists(base_slug: str, year: int, timeout: int = 15) -> bool:
    resp = requests.get(_base_url(f"{base_slug}_{year}"), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    return resp.status_code == 200 and resp.text.lstrip().startswith("<?xml")


def discover_livetrail_editions(
    base_slug: str, year_range: range = range(2010, 2027), timeout: int = 15
) -> List[DiscoveredRace]:
    """Probe /histo/{base_slug}_{year}/ across a year range for existing editions."""
    races = []
    for year in year_range:
        slug = f"{base_slug}_{year}"
        if edition_exists(base_slug, year, timeout=timeout):
            races.append(
                DiscoveredRace(url=_base_url(slug), slug=f"livetrail-{slug}", provider="livetrail")
            )
    return races


_TRAILING_DISTANCE_TOKEN_RE = re.compile(r"\s+[A-Za-z]*\d[A-Za-z0-9]*$")


def _strip_course_suffix(title: str, course_id: str) -> str:
    """Strip a trailing course label from a course title, e.g.
    "Salomon Cappadocia Ultra-Trail® CUT" -> "Salomon Cappadocia Ultra-Trail®".

    The course id doesn't always appear verbatim as that suffix (some
    editions use id "CUT110K" while the title only ends in "110K"), so if
    an exact-id strip doesn't change anything, fall back to stripping any
    trailing token that contains a digit (distance labels like "110K",
    "60K" always do; genuine race-name words never do).
    """
    stripped = re.sub(r"\s*" + re.escape(course_id) + r"$", "", title).strip()
    if stripped != title:
        return stripped
    return _TRAILING_DISTANCE_TOKEN_RE.sub("", title).strip()


def _home_page_data(event_slug: str, timeout: int) -> tuple:
    """Return (race_name, race_date_iso, [(course_id, course_title, distance_m), ...])."""
    resp = requests.get(_base_url(event_slug), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    courses = [
        (cid, title, _distance_m_from_sstitre(sstitre))
        for cid, title, sstitre in _COURSE_ENTRY_RE.findall(html)
        if cid
    ]
    dates = _INFO_RE.findall(html)
    race_date = min(dates) if dates else None

    race_name = None
    if courses:
        first_id, first_title, _ = courses[0]
        race_name = _strip_course_suffix(first_title, first_id)

    return race_name, race_date, courses


def _course_results(event_slug: str, course_id: str, timeout: int) -> List[Runner]:
    resp = requests.post(
        urljoin(_base_url(event_slug), "classement.php"),
        data={"course": course_id, "cat": "scratch", "pays": "all"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    xml = resp.text

    match = re.search(r"<classement[^>]*>(.*?)</classement>", xml, re.DOTALL)
    if not match:
        return []

    runners = []
    for row in _RESULT_ROW_RE.findall(match.group(1)):
        attrs: Dict[str, str] = dict(_ATTR_RE.findall(row))

        finish_seconds = _parse_clock(attrs.get("tps"))
        status = "finished" if finish_seconds is not None else "no_result"

        sex_raw = (attrs.get("sx") or "").strip().upper()
        sex = {"H": "M", "F": "F"}.get(sex_raw)

        surname = (attrs.get("nom") or "").strip()
        first_name = (attrs.get("prenom") or "").strip()
        name = f"{first_name} {surname}".strip() or surname or first_name

        runners.append(
            Runner(
                bib=attrs.get("doss") or "",
                name=name,
                club=attrs.get("club") or None,
                birth_year=None,
                sex=sex,
                category=attrs.get("cat") or None,
                course_code=course_id,
                nationality=attrs.get("cod") or None,
                start_time_text=None,
                status=status,
                finish_time_text=attrs.get("tps"),
                finish_seconds=finish_seconds,
                pace=None,
                finish_clock_text=None,
                gap_text=attrs.get("ecart"),
                gap_seconds=None,
                splits=[],
                rank_course=int(attrs["class"]) if (attrs.get("class") or "").isdigit() else None,
                rank_category=int(attrs["classcat"]) if (attrs.get("classcat") or "").isdigit() else None,
                rank_gender=None,  # not given directly - filled in by _assign_gender_ranks
            )
        )
    return runners


def _assign_gender_ranks(runners: List[Runner]) -> None:
    finishers = [r for r in runners if r.status == "finished" and r.finish_seconds is not None]
    finishers.sort(key=lambda r: r.finish_seconds)
    counters: Dict[Optional[str], int] = {}
    for runner in finishers:
        counters[runner.sex] = counters.get(runner.sex, 0) + 1
        runner.rank_gender = counters[runner.sex]


def scrape_livetrail_race(event_slug: str, timeout: int = 30) -> Race:
    race_name, race_date, courses = _home_page_data(event_slug, timeout)
    if not courses:
        raise ValueError(f"No courses found on LiveTrail edition page: {event_slug}")

    all_runners: List[Runner] = []
    course_objs: List[Course] = []
    for course_id, course_title, distance_m in courses:
        course_objs.append(Course(code=course_id, distance_m=distance_m or 0))
        runners = _course_results(event_slug, course_id, timeout)
        _assign_gender_ranks(runners)
        all_runners.extend(runners)

    return Race(
        slug=f"livetrail-{event_slug}",
        name=race_name or event_slug,
        organizer=None,
        date=race_date,
        source_url=_base_url(event_slug),
        courses=course_objs,
        checkpoints=[],
        categories=[],
        runners=all_runners,
    )
