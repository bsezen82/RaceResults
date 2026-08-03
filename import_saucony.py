"""One-off importer: racetecresults.com CSV exports (Saucony Istanbul 10K, 2026 -
5K and 10K individual distances; the team-based "Bayrak Yarışı" relay is not
included since it doesn't fit the individual-runner schema) into the
RaceResults SQLite database.

Run from the project root so `raceresults` package imports correctly:
    python3 import_saucony.py

Reads (expected in the same directory as this script):
    Saucony_5K_Sonuclar.csv
    Saucony_10K_Sonuclar.csv
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
CSV_5K = os.path.join(HERE, "Saucony_5K_Sonuclar.csv")
CSV_10K = os.path.join(HERE, "Saucony_10K_Sonuclar.csv")
DB_PATH = os.path.join(HERE, "data", "raceresults.db")

SLUG = "saucony-istanbul-10k-2026"
RACE_NAME = "Saucony Istanbul 10K"
RACE_DATE = "2026-05-09"
SOURCE_URL = "https://www.racetecresults.com/results.aspx?CId=19782&RId=125&e_name=Saucony%20Istanbul%2010K&e_year=2026"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")
_SEX_MAP = {"men": "M", "women": "F"}

# Per-course checkpoint columns as they appear in each CSV (generic CP1..CPn,
# no distance labels given by the site for this race).
_COURSE_CHECKPOINTS = {
    "5K": ["CP1", "CP2", "CP3"],
    "10K": ["CP1", "CP2", "CP3", "CP4", "CP5"],
}
_COURSE_DISTANCE_M = {"5K": 5000, "10K": 10000}


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

    checkpoint_cols = _COURSE_CHECKPOINTS[course_code]
    runners = []
    for row in rows:
        bib = (row.get("Göğüs No") or "").strip()
        name = (row.get("Ad Soyad") or "").strip()
        if not bib and not name:
            continue
        if not name:
            name = f"(İsimsiz) {bib}" if bib else "(İsimsiz)"

        kategori = (row.get("Kategori") or "").strip()
        ikincil = (row.get("İkincil Kategori") or "").strip()
        category = f"{kategori} / {ikincil}" if kategori and ikincil else (kategori or None)

        sex_raw = (row.get("Cinsiyet") or "").strip().lower()
        sex = _SEX_MAP.get(sex_raw)

        net_text = (row.get("Net Süre") or "").strip()
        finish_seconds = parse_hms(net_text)

        if finish_seconds is not None:
            status = "finished"
        elif net_text:
            # e.g. "Baslamadi" (Başlamadı / did not start) - a non-time status
            # word the site puts directly in the time columns.
            status = "no_result"
        else:
            status = "no_result"

        splits = []
        for idx, col in enumerate(checkpoint_cols):
            raw_text = (row.get(col) or "").strip()
            seconds = parse_hms(raw_text)
            splits.append(
                Split(
                    point_id=idx,
                    checkpoint_name=col,
                    cumulative_seconds=seconds,
                    clock_time_text=raw_text or None,
                )
            )
        # Final checkpoint: Finish (same as net finish time for this provider -
        # no separate start-offset column exists to subtract here).
        finish_raw = (row.get("Finish") or "").strip()
        splits.append(
            Split(
                point_id=len(checkpoint_cols),
                checkpoint_name="Finish",
                cumulative_seconds=parse_hms(finish_raw),
                clock_time_text=finish_raw or None,
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
                start_time_text=None,
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
    return runners


def main():
    for path in (CSV_5K, CSV_10K):
        if not os.path.exists(path):
            print(f"ERROR: expected CSV not found: {path}", file=sys.stderr)
            sys.exit(1)

    runners_5k = load_course_runners(CSV_5K, "5K")
    runners_10k = load_course_runners(CSV_10K, "10K")

    courses = [
        Course(code="5K", distance_m=_COURSE_DISTANCE_M["5K"]),
        Course(code="10K", distance_m=_COURSE_DISTANCE_M["10K"]),
    ]
    # Global checkpoint registry: 5K's CP1-CP3 and 10K's CP1-CP5 are physically
    # different mat locations despite the shared generic naming, so they get
    # distinct point_ids per course rather than being merged.
    checkpoints = []
    pid = 0
    for course_code in ("5K", "10K"):
        cols = _COURSE_CHECKPOINTS[course_code] + ["Finish"]
        for col in cols:
            checkpoints.append(
                Checkpoint(point_id=pid, name=f"{course_code} {col}", course_distances={course_code: 0})
            )
            pid += 1

    # Re-map runner splits' point_id to the global registry above (per-course offset).
    offset = {"5K": 0, "10K": len(_COURSE_CHECKPOINTS["5K"]) + 1}
    for runners in (runners_5k, runners_10k):
        for runner in runners:
            base = offset[runner.course_code]
            for split in runner.splits:
                split.point_id += base
                split.checkpoint_name = f"{runner.course_code} {split.checkpoint_name}"

    race = Race(
        slug=SLUG,
        name=RACE_NAME,
        organizer=None,
        date=RACE_DATE,
        source_url=SOURCE_URL,
        courses=courses,
        checkpoints=checkpoints,
        categories=[],
        runners=runners_5k + runners_10k,
    )

    conn = connect(DB_PATH)
    race_id = save_race(conn, race)

    finished = sum(1 for r in race.runners if r.status == "finished")
    print(
        f"Saved '{race.name}' (slug={race.slug}, race_id={race_id}): "
        f"{len(race.runners)} runners, {finished} finished, {len(race.courses)} courses "
        f"(5K: {len(runners_5k)}, 10K: {len(runners_10k)})"
    )


if __name__ == "__main__":
    main()
