"""One-off importer: racetecresults.com CSV exports (Merrell Belgrad Ultra
Istanbul, 2025 - 60K/30K/15K/5K individual distances) into the RaceResults
SQLite database, using the project's own models/store.

Run from the project root so `raceresults` package imports correctly:
    python3 import_merrell.py

Reads (expected in the same directory as this script):
    mb_60k.csv
    mb_30k.csv
    mb_15k.csv
    mb_5k.csv
"""
from __future__ import annotations

import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raceresults.models import Checkpoint, Course, Race, Runner, Split
from raceresults.store import connect, save_race

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RACERESULTS_DB_PATH") or os.path.join(HERE, "data", "raceresults.db")

SLUG = "merrell-belgrad-ultra-2025"
RACE_NAME = "Merrell Belgrad Ultra Istanbul"
RACE_DATE = "2025-09-06"
SOURCE_URL = "https://www.racetecresults.com/results.aspx?CId=19782&RId=118&e_name=Merrell%20Belgrad%20Ultra&e_year=2025"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_SEX_MAP = {"men": "M", "women": "F"}

# Name-cell status prefixes observed in this race's data (Turkish, informal):
#   DNS  = Başlamadı (did not start)
#   DNF  = did not finish
#   DQ / DSQ = disqualified
#   CUT  = cut-off (ultra time-limit elimination on a checkpoint)
#   QRY  = query - organizer-flagged result under review (rare, only seen in
#          the 15K distance: 3 runners with a Start time but no further data)
# Organizer-assigned prefixes always take priority over raw-time-derived
# status, even when the row still has partial/complete checkpoint data.
_NAME_PREFIX_RE = re.compile(r"^(DNS|DNF|DQ|DSQ|CUT|QRY)(.+)$")
_PREFIX_STATUS = {
    "DNS": "no_result",
    "DNF": "withdrawal",
    "DQ": "disqualified",
    "DSQ": "disqualified",
    "CUT": "withdrawal",
    "QRY": "no_result",
}

# Per-course checkpoint columns as they appear in each CSV, in source order.
# "Start" and "Finish" are included as checkpoints (consistent with the
# Winter Run importer) so the full progression is queryable via
# get_splits(), in addition to being mirrored onto Runner.finish_seconds /
# finish_time_text for convenience.
_COURSE_CHECKPOINT_COLS = {
    "60K": ["Start", "Göktürk", "Macera Parkı", "Gümüşdere", "Taşlı Yokuş", "Göktürk Dönüş", "Finish"],
    "30K": ["Start", "Göktürk", "Macera Parkı", "Göktürk Dönüş", "Finish"],
    "15K": ["Start", "Göktürk", "Finish"],
    "5K": ["Start", "2.5K", "Finish"],
}
_COURSE_DISTANCE_M = {"60K": 60000, "30K": 30000, "15K": 15000, "5K": 5000}
_COURSE_CSV = {
    "60K": os.path.join(HERE, "mb_60k.csv"),
    "30K": os.path.join(HERE, "mb_30k.csv"),
    "15K": os.path.join(HERE, "mb_15k.csv"),
    "5K": os.path.join(HERE, "mb_5k.csv"),
}
# Courses in the order we want them registered / point_ids assigned.
_COURSE_ORDER = ["60K", "30K", "15K", "5K"]


def parse_hms(value):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    m = _TIME_RE.match(value)
    if not m:
        return None
    h, mnt, s = (int(x) for x in m.groups())
    return float(h * 3600 + mnt * 60 + s)


def to_int(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_course_runners(csv_path: str, course_code: str):
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    checkpoint_cols = _COURSE_CHECKPOINT_COLS[course_code]
    runners = []
    for row in rows:
        bib = (row.get("Göğüs No") or "").strip()
        raw_name = (row.get("Ad Soyad") or "").strip()
        if not bib and not raw_name:
            continue

        name_status = None
        m = _NAME_PREFIX_RE.match(raw_name)
        if m:
            prefix, rest = m.groups()
            name_status = _PREFIX_STATUS[prefix]
            name = rest.strip()
        else:
            name = raw_name
        if not name:
            name = f"(İsimsiz) {bib}" if bib else "(İsimsiz)"

        category = (row.get("Kategori") or "").strip() or None

        sex_raw = (row.get("Cinsiyet") or "").strip().lower()
        sex = _SEX_MAP.get(sex_raw)

        # Checkpoint columns (Start/.../Finish) are already net-elapsed-from
        # -own-start values, confirmed by cross-checking: for ordinary
        # finishers, "Finish Süresi" (gross/gun) minus the checkpoint
        # "Finish" (net) equals "Start" exactly (e.g. 60K bib 177:
        # Finish Süresi 05:06:54, Start 00:00:09, checkpoint Finish
        # 05:06:45). So no Start-subtraction is needed here, consistent
        # with every other race on this platform.
        net_text = (row.get("Finish") or "").strip()
        net_seconds = parse_hms(net_text)
        if net_seconds == 0.0:
            net_seconds = None  # non-starter sentinel, not a real 0s finish

        if name_status is not None:
            status = name_status
        elif net_seconds is not None:
            status = "finished"
        else:
            # blank or "Baslamadi" (Başlamadı / did not start) in Finish Süresi
            status = "no_result"
        finish_seconds = net_seconds if status == "finished" else None

        splits = []
        for idx, col in enumerate(checkpoint_cols):
            raw_text = (row.get(col) or "").strip()
            seconds = parse_hms(raw_text)
            if seconds == 0.0 and col != "Start":
                seconds = None  # sentinel for "not reached", Start=0 is legit
            splits.append(
                Split(
                    point_id=idx,
                    checkpoint_name=col,
                    cumulative_seconds=seconds,
                    clock_time_text=raw_text or None,
                )
            )
        previous = 0.0
        for split in splits:
            if split.cumulative_seconds is not None:
                split.split_seconds = split.cumulative_seconds - previous
                previous = split.cumulative_seconds

        runners.append(
            Runner(
                bib=bib,
                name=name,
                club=None,
                birth_year=None,
                sex=sex,
                category=category,
                course_code=course_code,
                nationality=None,
                start_time_text=(row.get("Start") or "").strip() or None,
                status=status,
                finish_time_text=net_text or None,
                finish_seconds=finish_seconds,
                pace=None,
                finish_clock_text=(row.get("Finish Süresi") or "").strip() or None,
                gap_text=None,
                gap_seconds=None,
                splits=splits,
                rank_course=to_int(row.get("Sıra")),
                rank_category=to_int(row.get("Kategori Sırası")),
                rank_gender=to_int(row.get("Cinsiyet Sıralaması")),
            )
        )
    return runners


def main():
    for course_code in _COURSE_ORDER:
        path = _COURSE_CSV[course_code]
        if not os.path.exists(path):
            print(f"ERROR: expected CSV not found: {path}", file=sys.stderr)
            sys.exit(1)

    runners_by_course = {
        course_code: load_course_runners(_COURSE_CSV[course_code], course_code)
        for course_code in _COURSE_ORDER
    }

    courses = [
        Course(code=course_code, distance_m=_COURSE_DISTANCE_M[course_code])
        for course_code in _COURSE_ORDER
    ]

    # Global checkpoint registry: each course's checkpoints get distinct
    # point_ids (course-prefixed names), since "Göktürk" etc. are physically
    # different progressions per distance despite the shared naming -
    # consistent with the Saucony importer's per-course offset scheme.
    checkpoints = []
    pid = 0
    offset = {}
    for course_code in _COURSE_ORDER:
        offset[course_code] = pid
        for col in _COURSE_CHECKPOINT_COLS[course_code]:
            checkpoints.append(
                Checkpoint(point_id=pid, name=f"{course_code} {col}", course_distances={course_code: 0})
            )
            pid += 1

    all_runners = []
    for course_code in _COURSE_ORDER:
        base = offset[course_code]
        for runner in runners_by_course[course_code]:
            for split in runner.splits:
                split.point_id += base
                split.checkpoint_name = f"{course_code} {split.checkpoint_name}"
        all_runners.extend(runners_by_course[course_code])

    race = Race(
        slug=SLUG,
        name=RACE_NAME,
        organizer=None,
        date=RACE_DATE,
        source_url=SOURCE_URL,
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=all_runners,
    )

    conn = connect(DB_PATH)
    race_id = save_race(conn, race)

    finished = sum(1 for r in all_runners if r.status == "finished")
    withdrawn = sum(1 for r in all_runners if r.status == "withdrawal")
    disqualified = sum(1 for r in all_runners if r.status == "disqualified")
    no_result = sum(1 for r in all_runners if r.status == "no_result")
    print(
        f"Saved '{race.name}' (slug={race.slug}, race_id={race_id}): "
        f"{len(all_runners)} runners - {finished} finished, {withdrawn} withdrawal, "
        f"{disqualified} disqualified, {no_result} no_result, {len(courses)} courses"
    )
    for course_code in _COURSE_ORDER:
        rs = runners_by_course[course_code]
        f = sum(1 for r in rs if r.status == "finished")
        print(f"  {course_code}: {len(rs)} runners, {f} finished")


if __name__ == "__main__":
    main()
