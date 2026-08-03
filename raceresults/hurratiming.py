"""Scrape HurraTiming (hurratiming.com) - a first-party JSON API, unrelated
to the G-Live (Argeus/PassTiming) or Racetec (PlusTiming/RaceTecResults)
families handled elsewhere in this package. No anti-bot protection.

Flow: fetch /event/{id} once to read the "Version" token (required and
validated server-side by the API - a plain 0 gets rejected) and the
display name from <title>. POST /gettracks (EventsId, Version) lists the
race's distances ("tracks"). For each track, POST /getcontent (EventsId,
TracksId, FilterGender=0, FilterAgeCategory=0, PageNumber, Version)
repeatedly, incrementing PageNumber, until an empty competitors list comes
back.

Each getcontent response also says which checkpoint IDs exist for that
track ("controlpoints") and implies their display names as the last N
entries of `headers` - the leading column set varies per track (category
columns only appear when that track has age categories), so checkpoint
names are sliced from the end of `headers` (immediately before the fixed
trailing "Brüt Zaman"/"Net Zaman" pair), not counted from the front.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from .discover import DiscoveredRace
from .fetch import USER_AGENT
from .models import Checkpoint, Course, Race, Runner, Split

BASE_URL = "https://hurratiming.com/"

_EVENT_LINK_RE = re.compile(r'<a href="/event/(\d+)" class="lbltitlenew">([^<]+)</a>')
_VERSION_RE = re.compile(r'id="Version" value="(\d+)"')
_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.DOTALL)
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_DISTANCE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[kK]")
_DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def _parse_clock(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _dotnet_date_to_iso(value: Optional[str]) -> Optional[str]:
    """Convert a JSON.NET "/Date(1234567890000)/" string to an ISO date."""
    if not value:
        return None
    match = _DOTNET_DATE_RE.search(value)
    if not match:
        return None
    millis = int(match.group(1))
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date().isoformat()


def _distance_m_from_label(label: str) -> Optional[int]:
    match = _DISTANCE_RE.search(label)
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")) * 1000)


def _post(path: str, data: dict, timeout: int) -> dict:
    resp = requests.post(urljoin(BASE_URL, path), data=data, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def discover_hurratiming_races(timeout: int = 30) -> List[DiscoveredRace]:
    """Fetch the hurratiming.com homepage and list every /event/{id} found."""
    resp = requests.get(BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    races = []
    seen = set()
    for event_id, _name in _EVENT_LINK_RE.findall(resp.text):
        if event_id in seen:
            continue
        seen.add(event_id)
        races.append(
            DiscoveredRace(
                url=urljoin(BASE_URL, f"event/{event_id}"),
                slug=f"hurratiming-{event_id}",
                provider="hurratiming",
            )
        )
    return races


def _event_meta(event_id: str, timeout: int) -> Tuple[Optional[str], Optional[str]]:
    """Return (name, version_token) read from the event page."""
    resp = requests.get(urljoin(BASE_URL, f"event/{event_id}"), headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    title_match = _TITLE_RE.search(resp.text)
    version_match = _VERSION_RE.search(resp.text)
    name = html.unescape(title_match.group(1).strip()) if title_match else None
    version = version_match.group(1) if version_match else None
    return name, version


def _track_runners(
    event_id: str, track_id: int, track_label: str, version: str, timeout: int
) -> Tuple[List[Runner], List[str]]:
    runners: List[Runner] = []
    checkpoint_names: List[str] = []
    checkpoint_ids: List[int] = []
    page = 1

    while True:
        data = _post(
            "getcontent",
            {
                "EventsId": event_id,
                "TracksId": track_id,
                "FilterGender": 0,
                "FilterAgeCategory": 0,
                "PageNumber": page,
                "Version": version,
            },
            timeout=timeout,
        )
        competitors = data.get("competitors") or []
        if not competitors:
            break

        if not checkpoint_ids and data.get("controlpoints"):
            checkpoint_ids = data["controlpoints"]
            headers = data.get("headers") or []
            n = len(checkpoint_ids)
            # "Brüt Zaman"/"Net Zaman" are always the last two headers; the N
            # checkpoint labels sit immediately before them. Leading columns
            # (Kategori, Kategori Sıra, Ülke, ...) vary per track, so this is
            # anchored from the end, not counted from the front.
            checkpoint_names = headers[-(2 + n) : -2] if n else []

        for competitor in competitors:
            net_time_text = competitor.get("NetTimeRaw")
            finish_seconds = _parse_clock(net_time_text)
            status = "finished" if finish_seconds is not None else f"status_{competitor.get('StatusId')}"

            sex = {1: "F", 2: "M"}.get(competitor.get("Gender"))

            cp_values: Dict[int, Optional[str]] = {
                cp["ControlPointsId"]: cp.get("TimeValue")
                for cp in (competitor.get("ControlPointsNetTimeList") or [])
            }

            splits: List[Split] = []
            for position, cp_id in enumerate(checkpoint_ids):
                cumulative = _parse_clock(cp_values.get(cp_id))
                splits.append(
                    Split(
                        point_id=position,
                        checkpoint_name=checkpoint_names[position] if position < len(checkpoint_names) else f"CP{cp_id}",
                        cumulative_seconds=cumulative,
                        clock_time_text=None,
                    )
                )
            if finish_seconds is not None:
                splits.append(
                    Split(
                        point_id=len(checkpoint_ids),
                        checkpoint_name="Finish",
                        cumulative_seconds=finish_seconds,
                        clock_time_text=None,
                    )
                )
            previous_cumulative = 0.0
            for split in splits:
                if split.cumulative_seconds is not None:
                    split.split_seconds = split.cumulative_seconds - previous_cumulative
                    previous_cumulative = split.cumulative_seconds

            name = f"{(competitor.get('Namex') or '').strip()} {(competitor.get('Surname') or '').strip()}".strip()
            runners.append(
                Runner(
                    bib=str(competitor.get("BibNumber") or ""),
                    name=name,
                    club=competitor.get("Team") or None,
                    birth_year=None,
                    sex=sex,
                    category=competitor.get("AgeCategory") or None,
                    course_code=track_label,
                    nationality=competitor.get("Country") or None,
                    start_time_text=None,
                    status=status,
                    finish_time_text=net_time_text,
                    finish_seconds=finish_seconds,
                    pace=None,
                    finish_clock_text=None,
                    gap_text=None,
                    gap_seconds=None,
                    splits=splits,
                    rank_course=competitor.get("RankGeneral"),
                    rank_category=competitor.get("RankAgeCategoryGender"),
                    rank_gender=competitor.get("RankGender"),
                )
            )
        page += 1

    return runners, checkpoint_names


def scrape_hurratiming_race(event_id: str, timeout: int = 30) -> Race:
    name, version = _event_meta(event_id, timeout)
    if not version:
        raise ValueError(f"Could not find the Version token on HurraTiming event page {event_id}")

    tracks_data = _post("gettracks", {"EventsId": event_id, "Version": version}, timeout=timeout)
    tracks = tracks_data.get("tracks") or []

    courses: List[Course] = []
    all_runners: List[Runner] = []
    all_checkpoint_names: List[str] = []
    earliest_start: Optional[str] = None

    for track in tracks:
        label = track.get("Namex") or str(track.get("Id"))
        courses.append(Course(code=label, distance_m=_distance_m_from_label(label) or 0))

        start_date = _dotnet_date_to_iso(track.get("StartGunTime"))
        if start_date and (earliest_start is None or start_date < earliest_start):
            earliest_start = start_date

        runners, checkpoint_names = _track_runners(event_id, track["Id"], label, version, timeout)
        all_runners.extend(runners)
        for cp_name in checkpoint_names:
            if cp_name not in all_checkpoint_names:
                all_checkpoint_names.append(cp_name)

    checkpoints = [Checkpoint(point_id=i, name=n) for i, n in enumerate(all_checkpoint_names)]

    return Race(
        slug=f"hurratiming-{event_id}",
        name=name or f"HurraTiming Event {event_id}",
        organizer=None,
        date=earliest_start,
        source_url=urljoin(BASE_URL, f"event/{event_id}"),
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=all_runners,
    )
