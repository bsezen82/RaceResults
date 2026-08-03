"""One-off importer: racetecresults.com CSV exports (Summer Run Istanbul,
2025-08-30 - 21K/10K/5K individual distances) into the RaceResults SQLite
database, using the project's own models/store.

Run from the project root so `raceresults` package imports correctly:
    python3 import_summerrun.py

Reads (expected in the same directory as this script):
    sr_21k.csv
    sr_10k.csv
    sr_5k.csv
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

SLUG = "summerrun-istanbul-2025"
RACE_NAME = "Summer Run Istanbul"
RACE_DATE = "2025-08-30"
SOURCE_URL = "https://www.racetecresults.com/results.aspx?CId=19782&RId=117&e_name=Summer%20Run%20III&e_year=2025"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_SEX_MAP = {"men": "M", "women": "F"}

# Name-cell status prefixes (same convention observed across this platform's
# races). Only DNF and plain "Baslamadi" occur in this race's actual data,
# but the full set is kept for consistency/robustness with other importers.
_NAME_PREFIX_RE = re.compile(r"^(DNS|DNF|DQ|DSQ|CUT|QRY)(.+)$")
_PREFIX_STATUS = {
    "DNS": "no_result",
    "DNF": "withdrawal",
    "DQ": "disqualified",
    "DSQ": "disqualified",
    "CUT": "withdrawal",
    "QRY": "no_result",
}

_COURSE_CHECKPOINT_COLS = {
    "21K": ["Start", "10.5K", "16K", "Finish"],
    "10K": ["Start", "5K", "Finish"],
    "5K": ["Start", "2.5K", "Finish"],
}
_COURSE_DISTANCE_M = {"21K": 21000, "10K": 10000, "5K": 5000}
_COURSE_CSV = {
    "21K": os.path.join(HERE, "sr_21k.csv"),
    "10K": os.path.join(HERE, "sr_10k.csv"),
    "5K": os.path.join(HERE, "sr_5k.csv"),
}
_COURSE_ORDER = ["21K", "10K", "5K"]


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
        # -own-start values, consistent with every other race on this
        # platform: "Finish Süresi" (gross/gun) minus checkpoint "Finish"
        # (net) equals "Start".
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
