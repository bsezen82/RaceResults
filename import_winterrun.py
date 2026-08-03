"""One-off importer: racetecresults.com CSV export (Winter Run Istanbul, 2026,
single 10K distance) into the RaceResults SQLite database.

Run from the project root so `raceresults` package imports correctly:
    python3 import_winterrun.py

Reads (expected in the same directory as this script):
    WinterRun_Sonuclar.csv
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
CSV_PATH = os.path.join(HERE, "WinterRun_Sonuclar.csv")
DB_PATH = os.path.join(HERE, "data", "raceresults.db")

SLUG = "winterrun-istanbul-2026"
RACE_NAME = "Winter Run İstanbul"
RACE_DATE = "2026-02-15"
SOURCE_URL = "https://www.racetecresults.com/results.aspx?CId=19782&RId=124&e_name=Winter%20Run%20Istanbul&e_year=2026"
COURSE_CODE = "10K"
COURSE_DISTANCE_M = 10000

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_SEX_MAP = {"men": "M", "women": "F"}
_CHECKPOINT_COLS = ["Start", "3.5K", "6K", "8K", "Finish"]
_NAME_PREFIX_RE = re.compile(r"^(DNS|DNF|DQ|DSQ)(.+)$")


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


def main():
    if not os.path.exists(CSV_PATH):
        print(f"ERROR: expected CSV not found: {CSV_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    runners = []
    for row in rows:
        bib = (row.get("Göğüs No") or "").strip()
        raw_name = (row.get("Ad Soyad") or "").strip()
        if not bib and not raw_name:
            continue

        # A minority of rows carry an explicit "DNS"/"DNF" prefix baked into
        # the name cell itself (e.g. "DNSElif Arslan") - the site's own way
        # of overriding the usual blank/"Baslamadi" (Başlamadı = did not
        # start) convention with a more specific status.
        name_status = None
        m = _NAME_PREFIX_RE.match(raw_name)
        if m:
            prefix, rest = m.groups()
            name_status = "no_result" if prefix in ("DNS", "DQ", "DSQ") else "withdrawal"
            name = rest.strip()
        else:
            name = raw_name
        if not name:
            name = f"(İsimsiz) {bib}" if bib else "(İsimsiz)"

        kategori = (row.get("Kategori") or "").strip()
        category = kategori or None

        sex_raw = (row.get("Cinsiyet") or "").strip().lower()
        sex = _SEX_MAP.get(sex_raw)

        club = (row.get("Takım İsmi") or "").strip() or None

        net_text = (row.get("Net Süre") or "").strip()
        net_seconds = parse_hms(net_text)
        # "00:00:00" is a non-starter sentinel here (always paired with a
        # DNS/DNF-prefixed name in this data), not a real zero-second finish.
        if net_seconds == 0.0:
            net_seconds = None

        if name_status is not None:
            status = name_status
        elif net_seconds is not None:
            status = "finished"
        else:
            # blank or "Baslamadi" (Başlamadı / did not start)
            status = "no_result"
        finish_seconds = net_seconds if status == "finished" else None

        # Checkpoint columns on this race (Start/3.5K/6K/8K/Finish) are
        # already net-elapsed-from-own-start values (confirmed: the "Finish"
        # checkpoint matches the independently-given "Net Süre" column
        # exactly), unlike the separate "Finish Süresi" column which is the
        # gross/gun time - so no Start-subtraction is needed here.
        splits = []
        for idx, col in enumerate(_CHECKPOINT_COLS):
            raw_text = (row.get(col) or "").strip()
            seconds = parse_hms(raw_text)
            if seconds == 0.0 and col != "Start":
                seconds = None
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
                club=club,
                birth_year=None,
                sex=sex,
                category=category,
                course_code=COURSE_CODE,
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
                rank_category=to_int(row.get("Kategori Sırası (Net)")),
                rank_gender=to_int(row.get("Cinsiyet Sıralaması")),
            )
        )

    courses = [Course(code=COURSE_CODE, distance_m=COURSE_DISTANCE_M)]
    checkpoints = [
        Checkpoint(point_id=i, name=col, course_distances={COURSE_CODE: 0})
        for i, col in enumerate(_CHECKPOINT_COLS)
    ]

    race = Race(
        slug=SLUG,
        name=RACE_NAME,
        organizer=None,
        date=RACE_DATE,
        source_url=SOURCE_URL,
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=runners,
    )

    conn = connect(DB_PATH)
    race_id = save_race(conn, race)

    finished = sum(1 for r in race.runners if r.status == "finished")
    withdrawn = sum(1 for r in race.runners if r.status == "withdrawal")
    no_result = sum(1 for r in race.runners if r.status == "no_result")
    print(
        f"Saved '{race.name}' (slug={race.slug}, race_id={race_id}): "
        f"{len(race.runners)} runners - {finished} finished, {withdrawn} withdrawal, {no_result} no_result"
    )


if __name__ == "__main__":
    main()
