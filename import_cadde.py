"""One-off importer: racetecresults.com CSV exports (Cadde 10K & 21K, 2026)
into the RaceResults SQLite database, using the project's own models/store.

Run from the project root so `raceresults` package imports correctly:
    python3 import_cadde.py

Reads:
    Cadde_10K_Sonuclar.csv
    Cadde_21K_Sonuclar.csv
(both expected in the same directory as this script, unless overridden below)
"""
from __future__ import annotations

import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raceresults.models import Category, Checkpoint, Course, Race, Runner, Split
from raceresults.store import connect, save_race

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_10K = os.path.join(HERE, "Cadde_10K_Sonuclar.csv")
CSV_21K = os.path.join(HERE, "Cadde_21K_Sonuclar.csv")
DB_PATH = os.path.join(HERE, "data", "raceresults.db")

SLUG = "cadde10k-2026"
RACE_NAME = "Cadde 10K&21K"
RACE_DATE = "2026-05-24"
SOURCE_URL = "https://www.racetecresults.com/results.aspx?CId=19782&RId=126&e_name=Cadde%2010K&21K&e_year=2026"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")

# Global checkpoint registry shared by both courses. "Start" and "Finish"
# are included as regular checkpoints (point 0 and the last point) so the
# full progression is queryable via get_splits(), in addition to being
# mirrored onto Runner.finish_seconds/finish_time_text for convenience.
_CHECKPOINT_DEFS = [
    (0, "Start", {"10K": 0, "21K": 0}),
    (1, "1.7K", {"10K": 1700}),
    (2, "6.5K", {"10K": 6500}),
    (3, "7K", {"21K": 7000}),
    (4, "12.5K", {"21K": 12500}),
    (5, "17.5K", {"21K": 17500}),
    (6, "Finish", {"10K": 10000, "21K": 21000}),
]
# Per-course ordered list of (point_id, csv_column_name)
_COURSE_CHECKPOINTS = {
    "10K": [(0, "Start"), (1, "1.7K"), (2, "6.5K"), (6, "Finish")],
    "21K": [(0, "Start"), (3, "7K"), (4, "12.5K"), (5, "17.5K"), (6, "Finish")],
}

_SEX_MAP = {"men": "M", "women": "F"}


def parse_hms(value: str):
    """Parse "H:MM:SS" / "HH:MM:SS" into total seconds, or None if blank/unparseable."""
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


def format_hms(total_seconds):
    if total_seconds is None:
        return None
    total = int(round(total_seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def to_int(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_course_runners(csv_path: str, course_code: str):
    """Parse one racetecresults.com CSV export into a list of Runner objects."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    checkpoint_order = _COURSE_CHECKPOINTS[course_code]
    runners = []
    for row in rows:
        bib = (row.get("Göğüs No") or "").strip()
        name = (row.get("Ad Soyad") or "").strip()
        if not bib and not name:
            continue
        if not name:
            # A handful of source rows have a bib but no name (name hidden by
            # the runner, or a row the site's own table left blank) - keep
            # the record rather than silently dropping a real participant.
            name = f"(İsimsiz) {bib}" if bib else "(İsimsiz)"

        kategori = (row.get("Kategori") or "").strip()
        ikincil = (row.get("İkincil Kategori") or "").strip()
        category = f"{kategori} / {ikincil}" if kategori and ikincil else (kategori or None)

        sex_raw = (row.get("Cinsiyet") or "").strip().lower()
        sex = _SEX_MAP.get(sex_raw)  # "Unknown" (a few 21K rows) falls through to None

        start_seconds = parse_hms(row.get("Start"))
        # NOTE: verified against two other races on this same platform
        # (Saucony Istanbul 10K, Winter Run İstanbul) that both expose an
        # independent "Net Süre" column - in both, the CP-series "Finish"
        # value matches "Net Süre" exactly, proving these checkpoint/finish
        # values are already net-elapsed-from-own-start. "Start" is only the
        # gun-to-mat crossing delay for reference, not meant to be
        # subtracted from the other checkpoint columns. (An earlier version
        # of this script incorrectly subtracted Start here.)
        net_finish_seconds = parse_hms(row.get("Finish"))

        any_time_present = any(parse_hms(row.get(col)) is not None for _, col in checkpoint_order)
        if net_finish_seconds is not None:
            status = "finished"
        elif any_time_present:
            status = "withdrawal"
        else:
            status = "no_result"

        splits = []
        for point_id, col in checkpoint_order:
            raw_text = (row.get(col) or "").strip()
            seconds = parse_hms(raw_text)
            cumulative = seconds
            splits.append(
                Split(
                    point_id=point_id,
                    checkpoint_name=col,
                    cumulative_seconds=cumulative,
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
                finish_time_text=format_hms(net_finish_seconds),
                finish_seconds=net_finish_seconds,
                pace=None,
                finish_clock_text=(row.get("Finish") or "").strip() or None,
                gap_text=None,
                gap_seconds=None,
                splits=splits,
                rank_course=to_int(row.get("Sıra")),
                rank_category=to_int(row.get("Kategori Sırası (Net)")),
                rank_gender=to_int(row.get("Cinsiyet Sıralaması")),
            )
        )
    return runners


def main():
    for path in (CSV_10K, CSV_21K):
        if not os.path.exists(path):
            print(f"ERROR: expected CSV not found: {path}", file=sys.stderr)
            sys.exit(1)

    runners_10k = load_course_runners(CSV_10K, "10K")
    runners_21k = load_course_runners(CSV_21K, "21K")

    courses = [
        Course(code="10K", distance_m=10000),
        Course(code="21K", distance_m=21000),
    ]
    checkpoints = [
        Checkpoint(point_id=pid, name=name, course_distances=dist)
        for pid, name, dist in _CHECKPOINT_DEFS
    ]

    race = Race(
        slug=SLUG,
        name=RACE_NAME,
        organizer="Kadıköy Belediyesi",
        date=RACE_DATE,
        source_url=SOURCE_URL,
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=runners_10k + runners_21k,
    )

    conn = connect(DB_PATH)
    race_id = save_race(conn, race)

    finished = sum(1 for r in race.runners if r.status == "finished")
    print(
        f"Saved '{race.name}' (slug={race.slug}, race_id={race_id}): "
        f"{len(race.runners)} runners, {finished} finished, {len(race.courses)} courses "
        f"(10K: {len(runners_10k)}, 21K: {len(runners_21k)})"
    )


if __name__ == "__main__":
    main()
