"""Scrape results.splittime.nl ("iPico Wiz Results") - yet another ASP.NET
results engine, structurally close to Racetec/PlusTiming (paginated
server-rendered HTML tables) but with two differences that shaped this
module:

  - There is no listing/homepage (directory browsing is forbidden and there's
    no Default.aspx) and the results page itself never prints the race's
    name or date - only an opaque EventID. So, unlike every other provider
    here, there's no discover_*() and the caller must supply name/date
    (mirrors the local-HTML importer's --name/--date args in plustiming.py).
  - The actual table lives in a plain iframe target (GetData.aspx?EventID=
    &Distance=&Page=), reachable with a bare GET - no ASP.NET postback/
    viewstate needed to page through results despite the outer ShowEvent.aspx
    shell being a full postback form.
  - Checkpoint columns hold cumulative elapsed-since-gun time directly
    (confirmed: a "Start" column near 0:00:02 and "Finish" matching the Gun
    Time column) - no wall-clock-to-elapsed conversion needed, unlike
    PlusTiming.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from .fetch import USER_AGENT
from .models import Checkpoint, Course, Race, Runner, Split

BASE_URL = "https://results.splittime.nl/results/"

_DISTANCE_SELECT_RE = re.compile(r'id="cmbDistances"[^>]*>(.*?)</select>', re.DOTALL)
_OPTION_VALUE_RE = re.compile(r'<option[^>]*\bvalue="([^"]+)"')
_TABLE_RE = re.compile(r'<table class="TableResult">(.*?)</table>', re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
# The site's checkpoint header cells open with <th> but (a markup bug on
# their end) close with </td> instead of </th>, so header cells are found by
# scanning up to the next <th> or the row's end rather than requiring a
# matching close tag.
_TH_RE = re.compile(r"<th[^>]*>(.*?)(?=<th[^>]*>|$)", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_DISTANCE_LABEL_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[kK](?![a-zA-Z])")

# Fixed-role columns; everything else in the header is a checkpoint, in
# left-to-right (chronological) order.
_ROLE_COLUMNS = {"Pl.", "Bib", "Name", "Cat", "Aff", "Gun Time", "Mat Time"}


def _fetch(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _clean_cell(cell_html: str) -> str:
    text = unescape(_TAG_RE.sub(" ", cell_html))
    return re.sub(r"\s+", " ", text).strip()


def _parse_clock(value: str) -> Optional[int]:
    match = _CLOCK_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _distance_m_from_label(label: str) -> Optional[int]:
    match = _DISTANCE_LABEL_RE.search(label)
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")) * 1000)


def _reorder_name(raw: str) -> str:
    """Names come as "SURNAME, First Middle" - reorder to "First Middle
    Surname" to match the convention every other provider in this project
    uses."""
    if "," not in raw:
        return raw
    surname, _, rest = raw.partition(",")
    reordered = f"{rest.strip()} {surname.strip()}".strip()
    return reordered or raw


def _sex_from_category(cat: str) -> Optional[str]:
    upper = cat.strip().upper()
    if upper.startswith("F"):
        return "F"
    if upper.startswith("M"):
        return "M"
    return None


def discover_distances(event_id: str, timeout: int = 30) -> List[str]:
    html = _fetch(urljoin(BASE_URL, f"ShowEvent.aspx?EventID={event_id}"), timeout)
    match = _DISTANCE_SELECT_RE.search(html)
    if not match:
        return []
    return [v for v in _OPTION_VALUE_RE.findall(match.group(1)) if v != "Select"]


def _parse_results_page(html: str) -> Tuple[List[str], List[List[str]]]:
    table_match = _TABLE_RE.search(html)
    if not table_match:
        return [], []
    header: List[str] = []
    data_rows: List[List[str]] = []
    for row_html in _ROW_RE.findall(table_match.group(1)):
        th_cells = _TH_RE.findall(row_html)
        if len(th_cells) > 1:
            header = [_clean_cell(c) for c in th_cells]
            continue
        td_cells = _TD_RE.findall(row_html)
        if td_cells:
            data_rows.append([_clean_cell(c) for c in td_cells])
    return header, data_rows


def _rows_to_runners(header: List[str], rows: List[List[str]], course_label: str) -> Tuple[List[Runner], List[str]]:
    col_idx: Dict[str, int] = {name: i for i, name in enumerate(header)}
    checkpoint_idx = [i for i, name in enumerate(header) if name not in _ROLE_COLUMNS]
    checkpoint_names = [header[i] for i in checkpoint_idx]

    runners: List[Runner] = []
    for cells in rows:
        def cell(col: str) -> str:
            i = col_idx.get(col)
            return cells[i] if i is not None and i < len(cells) else ""

        name_raw = cell("Name")
        if not name_raw:
            continue

        gun_text = cell("Gun Time")
        finish_seconds = _parse_clock(gun_text)
        category = cell("Cat") or None

        splits: List[Split] = []
        previous_cumulative: float = 0.0
        for position, i in enumerate(checkpoint_idx):
            clock_text = cells[i] if i < len(cells) else ""
            cumulative = _parse_clock(clock_text)
            split_seconds = (cumulative - previous_cumulative) if cumulative is not None else None
            if cumulative is not None:
                previous_cumulative = cumulative
            splits.append(
                Split(
                    point_id=position,
                    checkpoint_name=header[i],
                    cumulative_seconds=cumulative,
                    clock_time_text=clock_text or None,
                    split_seconds=split_seconds,
                )
            )

        runners.append(
            Runner(
                bib=cell("Bib") or "",
                name=_reorder_name(name_raw),
                club=None,
                birth_year=None,
                sex=_sex_from_category(category) if category else None,
                category=category,
                course_code=course_label,
                nationality=cell("Aff") or None,
                start_time_text=None,
                status="finished" if finish_seconds is not None else "no_result",
                finish_time_text=gun_text or None,
                finish_seconds=finish_seconds,
                pace=None,
                finish_clock_text=None,
                gap_text=None,
                gap_seconds=None,
                splits=splits,
                rank_course=_to_int(cell("Pl.")),
                rank_category=None,
                rank_gender=None,
            )
        )

    return runners, checkpoint_names


def _parse_distance(event_id: str, distance: str, timeout: int) -> Tuple[List[Runner], List[str]]:
    all_runners: List[Runner] = []
    checkpoint_names: List[str] = []
    page = 0  # site's Page= param is 0-indexed
    while True:
        html = _fetch(
            urljoin(BASE_URL, f"GetData.aspx?EventID={event_id}&Distance={distance}&SortOn=GUN&Page={page}"),
            timeout,
        )
        header, rows = _parse_results_page(html)
        if not rows:
            break
        runners, cp_names = _rows_to_runners(header, rows, distance)
        all_runners.extend(runners)
        if not checkpoint_names:
            checkpoint_names = cp_names
        page += 1
    return all_runners, checkpoint_names


def scrape_splittime_race(event_id: str, name: str, date: Optional[str] = None, timeout: int = 30) -> Race:
    distances = discover_distances(event_id, timeout)
    if not distances:
        raise ValueError(f"No distances found for splittime.nl EventID={event_id}")

    courses: List[Course] = []
    all_runners: List[Runner] = []
    all_checkpoint_names: List[str] = []

    for distance in distances:
        runners, checkpoint_names = _parse_distance(event_id, distance, timeout)
        courses.append(Course(code=distance, distance_m=_distance_m_from_label(distance) or 0))
        all_runners.extend(runners)
        for cp_name in checkpoint_names:
            if cp_name not in all_checkpoint_names:
                all_checkpoint_names.append(cp_name)

    checkpoints = [Checkpoint(point_id=i, name=n) for i, n in enumerate(all_checkpoint_names)]

    return Race(
        slug=f"splittime-{event_id}",
        name=name,
        organizer=None,
        date=date,
        source_url=urljoin(BASE_URL, f"ShowEvent.aspx?EventID={event_id}"),
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=all_runners,
    )
