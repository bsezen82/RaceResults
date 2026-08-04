"""Scrape PlusTiming (sonuc.plustiming.com) - a "Racetec"-branded ASP.NET
results site, structurally unrelated to the G-Live providers (Argeus/
PassTiming) handled elsewhere in this package.

Differences from the G-Live scraper that shaped this module:
  - Results are plain server-rendered HTML tables, genuinely paginated via a
    PageNo= query parameter (~50 rows/page) - the real "page by page"
    scraping the G-Live sites turned out not to need.
  - A race (RId) can have several "events" (EId) - separate distances -
    each paginated independently, found via the <ul id="...divEvents"> tabs.
  - The column set (especially checkpoint names) varies per race, so
    headers are read dynamically by name rather than assumed fixed.
  - Overall/category/gender rank are given directly in the grid (Pos/Net
    Cat Pos/Gen Pos) - no need to derive them like with G-Live.
  - Checkpoint cells hold wall-clock time-of-day, not elapsed time. Net
    elapsed-per-checkpoint is derived by subtracting each runner's own
    "Start" column (also wall-clock) - avoiding a per-runner detail-page
    fetch (myresults.aspx) that would otherwise be needed for splits.

Everything is mapped into the same models.Race/Runner/Split shapes the
G-Live scraper produces, so storage (store.py) and the app stay
provider-agnostic.
"""
from __future__ import annotations

import functools
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from .discover import DiscoveredRace
from .fetch import USER_AGENT
from .models import Checkpoint, Course, Race, Runner, Split

BASE_URL = "https://sonuc.plustiming.com/"
DEFAULT_CID = 16389
_LISTING_PAGE_SIZE = 20

_RACE_CARD_RE = re.compile(r'<div class="race-card-border.*?</div>\s*</div>\s*</div>', re.DOTALL)
_CARD_RID_RE = re.compile(r"RId=(\d+)")
_CARD_NAME_RE = re.compile(r'RId=\d+">\s*([^<]*\S[^<]*)</a>')
_CARD_DATE_RE = re.compile(r"<span>([^<]+)</span>")
# Only the "ALL RACE RESULTS" pager's links carry a From=/To= pair (the
# per-race links use RId=, not From/To), so this is safe to search for
# across the whole page rather than scoping to one container.
_PAGER_RANGE_RE = re.compile(r"From=(\d+)&(?:amp;)?To=(\d+)")
_EVENTS_UL_RE = re.compile(r'<ul id="[^"]*divEvents"[^>]*>(.*?)</ul>', re.DOTALL)
_EVENT_LINK_RE = re.compile(r"EId=(\d+)\"[^>]*>([^<]+)</a>")
_PAGER_RE = re.compile(r"Page \d+ of (\d+)")
_TABLE_RE = re.compile(r'<table id="[^"]*tblResults"[^>]*>(.*?)</table>', re.DOTALL)
_ROW_RE = re.compile(r"<tr.*?</tr>", re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_DISTANCE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[kK](?![a-zA-Z])")

# Known fixed-role columns; anything else in the header is a checkpoint,
# in left-to-right (chronological) order. "Name" appears twice (a desktop
# and a combined mobile variant) - both copies must be excluded here so the
# second one isn't misread as a checkpoint column.
#
# Column naming and order both vary per race (e.g. "Cat Pos" vs "Net Cat
# Pos"; "Share" before or after "Race No") - each role lists every header
# text seen in the wild, checked in order, and any header not matching a
# role here is treated as a checkpoint column.
_ROLE_ALIASES = {
    "pos": ["Pos"],
    "bib": ["Race No", "Bib No", "Bib"],
    "name": ["Name"],
    "net_time": ["Net Time"],
    "gun_time": ["Time"],
    "category": ["Category"],
    "cat_pos": ["Net Cat Pos", "Cat Pos"],
    "gender": ["Gender", "Sex"],
    "gen_pos": ["Gen Pos", "Net Gen Pos"],
}
_METADATA_COLUMNS = {"Fav", "Share"} | {alias for names in _ROLE_ALIASES.values() for alias in names}

# A finish/net time of exactly zero is a non-starter sentinel, not a real
# result - no runner finishes a timed race in 0 seconds.
_ZERO_TIME_SENTINEL = "00:00:00"


def _clean_cell(html_cell: str) -> str:
    text = _TAG_RE.sub(" ", html_cell).replace("&nbsp", " ")
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


def _fetch(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _parse_card_date(date_text: Optional[str]) -> Optional[str]:
    if not date_text:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(date_text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _ingest_cards(html: str, cards_by_rid: Dict[str, Dict[str, Optional[str]]]) -> None:
    for card in _RACE_CARD_RE.findall(html):
        rid_match = _CARD_RID_RE.search(card)
        if not rid_match:
            continue
        rid = rid_match.group(1)
        if rid in cards_by_rid:
            continue
        name_match = _CARD_NAME_RE.search(card)
        date_match = _CARD_DATE_RE.search(card)
        name = name_match.group(1).strip() if name_match else None
        date_text = date_match.group(1).strip() if date_match else None
        cards_by_rid[rid] = {"name": name, "date": _parse_card_date(date_text)}


@functools.lru_cache(maxsize=8)
def _all_race_cards(cid: int) -> Dict[str, Dict[str, Optional[str]]]:
    """Fetch every page of the site's paginated "ALL RACE RESULTS" listing
    (StartPage.aspx?From=&To=, 20 races/page) and return {rid: {name, date}}
    for every race found - not just the homepage's small "recent races"
    excerpt, which is all discover_plustiming_races used to look at.

    Cached per CId for the life of the process: scraping many races in one
    run would otherwise re-fetch all ~10+ listing pages once per race just
    to look up its name/date.
    """
    cards_by_rid: Dict[str, Dict[str, Optional[str]]] = {}

    first_html = _fetch(
        urljoin(BASE_URL, f"StartPage.aspx?CId={cid}&From=1&To={_LISTING_PAGE_SIZE}")
    )
    _ingest_cards(first_html, cards_by_rid)

    range_pairs = _PAGER_RANGE_RE.findall(first_html)
    total_races = max((int(to) for _from, to in range_pairs), default=_LISTING_PAGE_SIZE)
    total_pages = (total_races + _LISTING_PAGE_SIZE - 1) // _LISTING_PAGE_SIZE

    for page in range(2, total_pages + 1):
        from_n = (page - 1) * _LISTING_PAGE_SIZE + 1
        to_n = page * _LISTING_PAGE_SIZE
        html = _fetch(urljoin(BASE_URL, f"StartPage.aspx?CId={cid}&From={from_n}&To={to_n}"))
        _ingest_cards(html, cards_by_rid)

    return cards_by_rid


def discover_plustiming_races(cid: int = DEFAULT_CID, timeout: int = 30) -> List[DiscoveredRace]:
    """List every event in the site's paginated "ALL RACE RESULTS" listing."""
    races = []
    for rid in _all_race_cards(cid):
        url = urljoin(BASE_URL, f"results.aspx?CId={cid}&RId={rid}")
        races.append(DiscoveredRace(url=url, slug=f"plustiming-{rid}", provider="plustiming"))
    return races


def _race_card_meta(cid: int, rid: str, timeout: int) -> Dict[str, Optional[str]]:
    """Pull this one race's name+date, from the (cached) full listing scan."""
    return _all_race_cards(cid).get(rid, {"name": None, "date": None})


def _fetch_events(cid: int, rid: str, timeout: int) -> List[Tuple[Optional[str], str]]:
    """Return [(EId, distance_label), ...] in tab order; EId is None if the
    race has no distance tabs at all (single-event race)."""
    html = _fetch(urljoin(BASE_URL, f"results.aspx?CId={cid}&RId={rid}"), timeout=timeout)
    ul_match = _EVENTS_UL_RE.search(html)
    if not ul_match:
        return [(None, "Race")]
    return [(eid, _clean_cell(label)) for eid, label in _EVENT_LINK_RE.findall(ul_match.group(1))]


def _distance_m_from_label(label: str) -> Optional[int]:
    match = _DISTANCE_RE.search(label)
    if not match:
        return None
    return int(float(match.group(1).replace(",", ".")) * 1000)


def _parse_results_page(html: str) -> Tuple[List[str], List[List[str]]]:
    table_match = _TABLE_RE.search(html)
    if not table_match:
        return [], []
    rows = _ROW_RE.findall(table_match.group(1))
    if not rows:
        return [], []
    header = [_clean_cell(c) for c in _CELL_RE.findall(rows[0])]
    data_rows = []
    for row in rows[1:]:
        cells = _CELL_RE.findall(row)
        if cells:
            data_rows.append([_clean_cell(c) for c in cells])
    return header, data_rows


def _total_pages(html: str) -> int:
    match = _PAGER_RE.search(html)
    return int(match.group(1)) if match else 1


def _event_url(cid: int, rid: str, eid: Optional[str], page: int) -> str:
    eid_part = f"&EId={eid}" if eid else ""
    return urljoin(BASE_URL, f"results.aspx?CId={cid}&RId={rid}{eid_part}&dt=0&PageNo={page}")


def _rows_to_runners(header: List[str], rows: List[List[str]], course_label: str) -> Tuple[List[Runner], List[str]]:
    """Convert one parsed results-table (header + data rows) into Runners.

    Shared by the live scraper (one page at a time, over HTTP) and the local
    HTML importer (one manually-saved page at a time) - both ultimately hand
    this the same (header, rows) shape from `_parse_results_page`.
    """
    name_idx = header.index("Name") if "Name" in header else None
    role_idx: Dict[str, int] = {}
    for role, aliases in _ROLE_ALIASES.items():
        for alias in aliases:
            if alias in header:
                role_idx[role] = header.index(alias)
                break
    checkpoint_idx = [i for i, name in enumerate(header) if name not in _METADATA_COLUMNS]
    checkpoint_names = [header[i] for i in checkpoint_idx]

    runners: List[Runner] = []
    for cells in rows:
        if name_idx is None or name_idx >= len(cells) or not cells[name_idx]:
            continue

        def cell(role: str) -> str:
            idx = role_idx.get(role)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        net_time_text = cell("net_time")
        finish_seconds = None if net_time_text == _ZERO_TIME_SENTINEL else _parse_clock(net_time_text)
        if finish_seconds is not None:
            status = "finished"
        elif net_time_text and net_time_text != _ZERO_TIME_SENTINEL:
            status = net_time_text.lower()
        else:
            status = "no_result"

        gender_raw = cell("gender").strip().lower()
        sex = {"male": "M", "female": "F", "erkek": "M", "kadın": "F", "kadin": "F"}.get(gender_raw)
        if sex is None and gender_raw:
            sex = gender_raw[:1].upper()

        splits: List[Split] = []
        start_seconds = None
        for position, i in enumerate(checkpoint_idx):
            if i >= len(cells):
                continue
            clock_text = cells[i]
            clock_seconds = _parse_clock(clock_text)
            if position == 0:
                start_seconds = clock_seconds
            cumulative = (
                clock_seconds - start_seconds
                if clock_seconds is not None and start_seconds is not None
                else None
            )
            splits.append(
                Split(
                    point_id=position,
                    checkpoint_name=header[i],
                    cumulative_seconds=cumulative,
                    clock_time_text=clock_text or None,
                )
            )
        previous_cumulative = 0.0
        for split in splits:
            if split.cumulative_seconds is not None:
                split.split_seconds = split.cumulative_seconds - previous_cumulative
                previous_cumulative = split.cumulative_seconds

        runners.append(
            Runner(
                bib=cell("bib") or "",
                name=cells[name_idx],
                club=None,
                birth_year=None,
                sex=sex,
                category=cell("category") or None,
                course_code=course_label,
                nationality=None,
                start_time_text=None,
                status=status,
                finish_time_text=net_time_text or None,
                finish_seconds=finish_seconds,
                pace=None,
                finish_clock_text=None,
                gap_text=None,
                gap_seconds=None,
                splits=splits,
                rank_course=_to_int(cell("pos")),
                rank_category=_to_int(cell("cat_pos")),
                rank_gender=_to_int(cell("gen_pos")),
            )
        )

    return runners, checkpoint_names


def _parse_event_runners(cid: int, rid: str, eid: Optional[str], course_label: str, timeout: int) -> Tuple[List[Runner], List[str]]:
    all_runners: List[Runner] = []
    all_checkpoint_names: List[str] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        html = _fetch(_event_url(cid, rid, eid, page), timeout=timeout)
        if page == 1:
            total_pages = _total_pages(html)
        header, rows = _parse_results_page(html)
        if not header:
            break

        runners, checkpoint_names = _rows_to_runners(header, rows, course_label)
        all_runners.extend(runners)
        if not all_checkpoint_names:
            all_checkpoint_names = checkpoint_names
        page += 1

    return all_runners, all_checkpoint_names


def scrape_plustiming_race(rid: str, cid: int = DEFAULT_CID, timeout: int = 30) -> Race:
    meta = _race_card_meta(cid, rid, timeout)
    events = _fetch_events(cid, rid, timeout)

    courses: List[Course] = []
    all_runners: List[Runner] = []
    all_checkpoint_names: List[str] = []

    for eid, label in events:
        courses.append(Course(code=label, distance_m=_distance_m_from_label(label) or 0))
        runners, checkpoint_names = _parse_event_runners(cid, rid, eid, label, timeout)
        all_runners.extend(runners)
        for name in checkpoint_names:
            if name not in all_checkpoint_names:
                all_checkpoint_names.append(name)

    checkpoints = [Checkpoint(point_id=i, name=name) for i, name in enumerate(all_checkpoint_names)]

    return Race(
        slug=f"plustiming-{rid}",
        name=meta["name"] or f"PlusTiming Race {rid}",
        organizer=None,
        date=meta["date"],
        source_url=urljoin(BASE_URL, f"results.aspx?CId={cid}&RId={rid}"),
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=all_runners,
    )


# --- Locally-saved HTML import -------------------------------------------
#
# Some Racetec-branded sites (e.g. racetecresults.com) sit behind an active
# Cloudflare bot challenge that blocks plain HTTP requests. We don't attempt
# to defeat that - but the page format is the same one this module already
# parses, so if a *person* saves the page(s) from their own browser (one
# file per distance/page, "Save As -> Webpage, HTML only"), we can still
# turn those into a Race the same way. The active distance tab
# (class="... ltw-activeeventtab") tells us which course each saved page
# belongs to, so files don't need to be named any particular way.

_ACTIVE_TAB_RE = re.compile(r'class="[^"]*ltw-activeeventtab[^"]*"[^>]*>([^<]+)</a>')


def _active_course_label(html: str) -> Optional[str]:
    match = _ACTIVE_TAB_RE.search(html)
    return _clean_cell(match.group(1)) if match else None


def parse_local_results_html(html: str, default_course_label: str = "Race") -> Tuple[str, List[Runner], List[str]]:
    """Parse one manually-saved results page. Returns (course_label, runners, checkpoint_names)."""
    course_label = _active_course_label(html) or default_course_label
    header, rows = _parse_results_page(html)
    if not header:
        return course_label, [], []
    runners, checkpoint_names = _rows_to_runners(header, rows, course_label)
    return course_label, runners, checkpoint_names


def build_race_from_local_html(
    paths: List[str],
    slug: str,
    name: str,
    date: Optional[str] = None,
    default_course_label: str = "Race",
) -> Race:
    """Build a Race from one or more manually-saved results.aspx HTML files.

    Each file is one page of one distance (pass every PageNo and every
    distance tab you saved - duplicates and any order are fine, rows are
    deduplicated by (course, bib, name)).
    """
    seen_runner_keys = set()
    all_runners: List[Runner] = []
    course_labels: List[str] = []
    all_checkpoint_names: List[str] = []

    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
        course_label, runners, checkpoint_names = parse_local_results_html(html, default_course_label)
        if course_label not in course_labels:
            course_labels.append(course_label)
        for name_ in checkpoint_names:
            if name_ not in all_checkpoint_names:
                all_checkpoint_names.append(name_)
        for runner in runners:
            key = (course_label, runner.bib, runner.name)
            if key in seen_runner_keys:
                continue
            seen_runner_keys.add(key)
            all_runners.append(runner)

    courses = [Course(code=label, distance_m=_distance_m_from_label(label) or 0) for label in course_labels]
    checkpoints = [Checkpoint(point_id=i, name=n) for i, n in enumerate(all_checkpoint_names)]

    return Race(
        slug=slug,
        name=name,
        organizer=None,
        date=date,
        source_url="local-import",
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=all_runners,
    )
