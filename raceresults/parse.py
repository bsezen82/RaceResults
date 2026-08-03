"""Parse a G-Live .clax XML document into a `Race` object.

Schema notes (reverse-engineered from real event data, French-named tags):
  Epreuve            root: race metadata (nom, organisateur, dt1, ...)
    Parcours/Pcs       one per course: nom (code), distance (m), clh (color)
    Categories/G/C     age/gender category definitions (nom, abr, agemin, agemax, sx)
                       (also holds per-course "C crs=..." marker entries, no age fields)
    Etapes/Etape
      Pointages/Pointage   checkpoints: id, nom, lt/lg (GPS), pcs (csv of course codes),
                           distpcs (":"-separated distances, aligned with pcs list)
      Engages/E          registered entrants: d (bib), n (name), c (club), a (birth year),
                         x (sex M/F), ca (category abbr), p (course code), na (nationality),
                         h (start time text). Team-only entries have no "d" - skipped.
      Resultats/R        one per entrant with a recorded result, keyed by d (bib):
                         t (net time text, or "Withdrawal"/"Disqualified"), m (pace),
                         b (finish clock text), g (gap text), pN (net elapsed time at
                         checkpoint id N, same "HHhMM'SS" format as t - NOT a leg-only
                         split), pbN (wall-clock time-of-day at checkpoint N, format
                         inconsistent). NOT grouped or sorted by course - it's sorted
                         by absolute finish clock time across all courses.
                         Leg-only split durations aren't given directly; we derive them
                         by diffing consecutive pN values per runner.

An entrant with no matching Resultats/R entry is recorded with status "no_result"
(did not start, or hasn't finished yet for a live event).

Ranks (overall/course/category/gender) aren't given explicitly - the viewer computes
them client-side - so we derive them here by sorting finishers within each course by
net finish time.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .models import Category, Checkpoint, Course, Race, Runner, Split
from .timeutils import parse_gap, parse_pace, parse_time, status_for

_EXCEL_EPOCH = datetime(1899, 12, 30)


def _race_date(epreuve: Dict[str, str]) -> Optional[str]:
    """Resolve the race date as an ISO string.

    Newer events carry an explicit dt1="2026-07-18". Many older ones only
    carry date="45605" - an Excel-style serial day count - which we decode
    the same way Excel/Sheets would (epoch 1899-12-30, inclusive of its
    leap-year quirk).
    """
    dt1 = epreuve.get("dt1")
    if dt1:
        return dt1
    serial = epreuve.get("date")
    if serial:
        try:
            return (_EXCEL_EPOCH + timedelta(days=float(serial))).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _clean_name(name: str) -> str:
    # Entrant names use a non-breaking space between first and last name.
    return " ".join(name.replace("\xa0", " ").split())


def _parse_courses(root: ET.Element) -> List[Course]:
    return [
        Course(
            code=pcs.attrib["nom"],
            distance_m=_to_int(pcs.attrib.get("distance")) or 0,
            color=pcs.attrib.get("clh"),
        )
        for pcs in root.findall("./Parcours/Pcs")
    ]


def _parse_categories(root: ET.Element) -> List[Category]:
    categories = []
    for c in root.findall("./Categories/G/C"):
        if "abr" not in c.attrib:
            continue  # course-marker entries (crs=...) rather than age/gender categories
        sx = c.attrib.get("sx")
        sex = {"1": "M", "2": "F"}.get(sx)
        categories.append(
            Category(
                abbr=c.attrib["abr"],
                name=c.attrib.get("nom", c.attrib["abr"]),
                age_min=_to_int(c.attrib.get("agemin")),
                age_max=_to_int(c.attrib.get("agemax")),
                sex=sex,
            )
        )
    return categories


def _parse_checkpoints(etape: ET.Element) -> List[Checkpoint]:
    checkpoints = []
    for p in etape.findall("./Pointages/Pointage"):
        course_codes = [c for c in p.attrib.get("pcs", "").split(",") if c]
        distances = [d for d in p.attrib.get("distpcs", "").split(":") if d != ""]
        course_distances = {}
        for code, dist in zip(course_codes, distances):
            parsed = _to_int(dist)
            if parsed is not None:
                course_distances[code] = parsed
        checkpoints.append(
            Checkpoint(
                point_id=_to_int(p.attrib["id"]),
                name=p.attrib.get("nom", ""),
                lat=_to_float(p.attrib.get("lt")),
                lon=_to_float(p.attrib.get("lg")),
                course_distances=course_distances,
            )
        )
    return checkpoints


def _split_point_ids(attrib: Dict[str, str]) -> List[int]:
    ids = set()
    for key in attrib:
        if key.startswith("pb") and key[2:].isdigit():
            ids.add(int(key[2:]))
        elif key.startswith("p") and key[1:].isdigit():
            ids.add(int(key[1:]))
    return sorted(ids)


def parse_race(xml_text: str, slug: str, source_url: str) -> Race:
    root = ET.fromstring(xml_text)
    etape = root.find("./Etapes/Etape")
    if etape is None:
        raise ValueError("Unexpected .clax document: no <Etapes><Etape> found")

    checkpoints = _parse_checkpoints(etape)
    checkpoint_names = {cp.point_id: cp.name for cp in checkpoints}

    results_by_bib: Dict[str, ET.Element] = {}
    for r in etape.findall("./Resultats/R"):
        bib = r.attrib.get("d")
        if bib:
            results_by_bib[bib] = r

    runners: List[Runner] = []
    for e in etape.findall("./Engages/E"):
        bib = e.attrib.get("d")
        if not bib:
            continue  # team-only registration entry, not an individual runner

        r = results_by_bib.get(bib)
        splits: List[Split] = []
        status = "no_result"
        finish_time_text = finish_clock_text = gap_text = None
        finish_seconds = pace = gap_seconds = None

        if r is not None:
            t_raw = r.attrib.get("t")
            known_status = status_for(t_raw)
            if known_status:
                status = known_status
                finish_time_text = t_raw
            else:
                status = "finished"
                finish_time_text = t_raw
                finish_seconds = parse_time(t_raw)

            finish_clock_text = r.attrib.get("b")
            gap_text = r.attrib.get("g")
            gap_seconds = parse_gap(gap_text)
            pace = parse_pace(r.attrib.get("m"))

            for point_id in _split_point_ids(r.attrib):
                # p{id}: this runner's net elapsed time (since their own start) at the
                # checkpoint. pb{id}: wall-clock time-of-day of that passage (format is
                # inconsistent - e.g. the first checkpoint sometimes carries a date
                # prefix like "20260718D06h48'40,910" - so it's kept as raw text rather
                # than parsed into seconds).
                cumul_val = r.attrib.get(f"p{point_id}")
                clock_val = r.attrib.get(f"pb{point_id}")
                if cumul_val is None and clock_val is None:
                    continue
                splits.append(
                    Split(
                        point_id=point_id,
                        checkpoint_name=checkpoint_names.get(point_id, f"CP{point_id}"),
                        cumulative_seconds=parse_time(cumul_val),
                        clock_time_text=clock_val,
                    )
                )
            splits.sort(key=lambda s: s.point_id)

            previous_cumulative = 0.0
            for split in splits:
                if split.cumulative_seconds is not None:
                    split.split_seconds = split.cumulative_seconds - previous_cumulative
                    previous_cumulative = split.cumulative_seconds

        runners.append(
            Runner(
                bib=bib,
                name=_clean_name(e.attrib.get("n", "")),
                club=e.attrib.get("c"),
                birth_year=_to_int(e.attrib.get("a")),
                sex=e.attrib.get("x"),
                category=e.attrib.get("ca"),
                course_code=e.attrib.get("p"),
                nationality=e.attrib.get("na"),
                start_time_text=e.attrib.get("h"),
                status=status,
                finish_time_text=finish_time_text,
                finish_seconds=finish_seconds,
                pace=pace,
                finish_clock_text=finish_clock_text,
                gap_text=gap_text,
                gap_seconds=gap_seconds,
                splits=splits,
            )
        )

    _assign_ranks(runners)

    epreuve = root.attrib
    return Race(
        slug=slug,
        name=epreuve.get("nom", slug),
        organizer=epreuve.get("organisateur"),
        date=_race_date(epreuve),
        source_url=source_url,
        courses=_parse_courses(root),
        checkpoints=checkpoints,
        categories=_parse_categories(root),
        runners=runners,
    )


def _assign_ranks(runners: List[Runner]) -> None:
    by_course: Dict[str, List[Runner]] = defaultdict(list)
    for runner in runners:
        by_course[runner.course_code or ""].append(runner)

    for course_runners in by_course.values():
        finishers = [r for r in course_runners if r.status == "finished" and r.finish_seconds is not None]
        finishers.sort(key=lambda r: r.finish_seconds)

        by_category: Dict[str, int] = defaultdict(int)
        by_gender: Dict[str, int] = defaultdict(int)
        for i, runner in enumerate(finishers, start=1):
            runner.rank_course = i
            by_category[runner.category or ""] += 1
            runner.rank_category = by_category[runner.category or ""]
            by_gender[runner.sex or ""] += 1
            runner.rank_gender = by_gender[runner.sex or ""]
