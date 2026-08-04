"""SQLite persistence for parsed races, plus the name-search query the future
app ("pick a race, type a name, see the result") will run against.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from typing import Dict, List, Optional, Tuple

from .models import Race

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    organizer TEXT,
    date TEXT,
    source_url TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    distance_m INTEGER,
    color TEXT,
    UNIQUE(race_id, code)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    abbr TEXT NOT NULL,
    name TEXT,
    age_min INTEGER,
    age_max INTEGER,
    sex TEXT,
    UNIQUE(race_id, abbr)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    point_id INTEGER NOT NULL,
    name TEXT,
    lat REAL,
    lon REAL,
    UNIQUE(race_id, point_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_courses (
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
    course_code TEXT NOT NULL,
    distance_m INTEGER,
    PRIMARY KEY (checkpoint_id, course_code)
);

CREATE TABLE IF NOT EXISTS runners (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    bib TEXT NOT NULL,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    club TEXT,
    birth_year INTEGER,
    sex TEXT,
    category TEXT,
    course_code TEXT,
    nationality TEXT,
    start_time_text TEXT,
    status TEXT NOT NULL,
    finish_time_text TEXT,
    finish_seconds REAL,
    pace REAL,
    finish_clock_text TEXT,
    gap_text TEXT,
    gap_seconds REAL,
    rank_course INTEGER,
    rank_category INTEGER,
    rank_gender INTEGER
    -- No UNIQUE(race_id, bib): save_race() always deletes a race's rows
    -- before reinserting, so this isn't needed for idempotency, and some
    -- providers (PlusTiming) legitimately reuse bib numbers across a
    -- race's distances, or have malformed rows (DNS/volunteer sections)
    -- that don't carry a real bib at all.
);

CREATE INDEX IF NOT EXISTS idx_runners_name ON runners(name_normalized);
CREATE INDEX IF NOT EXISTS idx_runners_race ON runners(race_id);

CREATE TABLE IF NOT EXISTS splits (
    runner_id INTEGER NOT NULL REFERENCES runners(id) ON DELETE CASCADE,
    point_id INTEGER NOT NULL,
    checkpoint_name TEXT,
    cumulative_seconds REAL,
    clock_time_text TEXT,
    split_seconds REAL,
    PRIMARY KEY (runner_id, point_id)
);
"""

_TURKISH_FOLD = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    }
)


def normalize_name(name: str) -> str:
    """Fold case, Turkish characters, and any other Latin diacritics (é, ñ,
    ...) so name search/dedup is accent- and case-insensitive regardless of
    whether a name came from a Turkish or foreign-language source.

    The Turkish-specific table runs first because "ı" (dotless i) has no
    Unicode decomposition to fold generically - everything else (ö, ü, ç,
    ğ, ş, and non-Turkish letters like é or ñ) does, via NFKD + stripping
    combining marks.
    """
    folded = name.translate(_TURKISH_FOLD).lower()
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(ch))
    return " ".join(folded.replace("\xa0", " ").split())


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: callers that cache this connection across requests
    # (e.g. Streamlit's st.cache_resource) may reuse it from a different thread on
    # each rerun. Reads/writes here are simple enough that this is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_race(conn: sqlite3.Connection, race: Race) -> int:
    """Insert or fully replace a race's data (idempotent re-scrape by slug)."""
    from datetime import datetime, timezone

    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM races WHERE slug = ?", (race.slug,)).fetchone()
    if existing:
        cur.execute("DELETE FROM races WHERE id = ?", (existing[0],))

    cur.execute(
        "INSERT INTO races (slug, name, organizer, date, source_url, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            race.slug,
            race.name,
            race.organizer,
            race.date,
            race.source_url,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    race_id = cur.lastrowid

    cur.executemany(
        "INSERT INTO courses (race_id, code, distance_m, color) VALUES (?, ?, ?, ?)",
        [(race_id, c.code, c.distance_m, c.color) for c in race.courses],
    )

    cur.executemany(
        "INSERT OR IGNORE INTO categories (race_id, abbr, name, age_min, age_max, sex) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(race_id, c.abbr, c.name, c.age_min, c.age_max, c.sex) for c in race.categories],
    )

    for cp in race.checkpoints:
        cur.execute(
            "INSERT INTO checkpoints (race_id, point_id, name, lat, lon) VALUES (?, ?, ?, ?, ?)",
            (race_id, cp.point_id, cp.name, cp.lat, cp.lon),
        )
        checkpoint_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO checkpoint_courses (checkpoint_id, course_code, distance_m) "
            "VALUES (?, ?, ?)",
            [(checkpoint_id, code, dist) for code, dist in cp.course_distances.items()],
        )

    for runner in race.runners:
        cur.execute(
            """INSERT INTO runners (
                race_id, bib, name, name_normalized, club, birth_year, sex, category,
                course_code, nationality, start_time_text, status, finish_time_text,
                finish_seconds, pace, finish_clock_text, gap_text, gap_seconds,
                rank_course, rank_category, rank_gender
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                race_id,
                runner.bib,
                runner.name,
                normalize_name(runner.name),
                runner.club,
                runner.birth_year,
                runner.sex,
                runner.category,
                runner.course_code,
                runner.nationality,
                runner.start_time_text,
                runner.status,
                runner.finish_time_text,
                runner.finish_seconds,
                runner.pace,
                runner.finish_clock_text,
                runner.gap_text,
                runner.gap_seconds,
                runner.rank_course,
                runner.rank_category,
                runner.rank_gender,
            ),
        )
        runner_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO splits (runner_id, point_id, checkpoint_name, cumulative_seconds, "
            "clock_time_text, split_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    runner_id,
                    s.point_id,
                    s.checkpoint_name,
                    s.cumulative_seconds,
                    s.clock_time_text,
                    s.split_seconds,
                )
                for s in runner.splits
            ],
        )

    conn.commit()
    return race_id


def search_runners(
    conn: sqlite3.Connection,
    query: str,
    race_slug: Optional[str] = None,
    year: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 25,
) -> List[sqlite3.Row]:
    """Find runners by (accent/case-insensitive) name, word-order-independent.

    The query is split into words and every word must appear somewhere in
    the stored name - not just as one exact substring - so "Ad Soyad" also
    matches a runner stored as "Soyad Ad" (some providers give names in
    surname-first order).

    With no race_slug/year, searches across every stored race. `year` (e.g.
    "2025") narrows to races whose date falls in that year; ignored if
    race_slug is given. `status` (e.g. "finished") filters in SQL rather
    than after fetching - callers that only want finished results MUST pass
    this instead of filtering the return value themselves, otherwise a
    common name with many DNF/DNS rows can fill up `limit` before any
    finished ones are counted, silently dropping real results.
    """
    conn.row_factory = sqlite3.Row
    tokens = normalize_name(query).split()
    if not tokens:
        return []
    sql = """
        SELECT runners.*, races.slug AS race_slug, races.name AS race_name,
               races.date AS race_date, courses.distance_m AS course_distance_m,
               (SELECT COUNT(*) FROM runners r2
                WHERE r2.race_id = runners.race_id AND r2.course_code = runners.course_code
               ) AS course_participant_count
        FROM runners
        JOIN races ON races.id = runners.race_id
        LEFT JOIN courses ON courses.race_id = runners.race_id AND courses.code = runners.course_code
        WHERE """ + " AND ".join(["runners.name_normalized LIKE ?"] * len(tokens))
    params: list = [f"%{token}%" for token in tokens]
    if status:
        sql += " AND runners.status = ?"
        params.append(status)
    if race_slug:
        sql += " AND races.slug = ?"
        params.append(race_slug)
    elif year:
        sql += " AND races.date LIKE ?"
        params.append(f"{year}%")
    sql += " ORDER BY runners.name LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_splits(conn: sqlite3.Connection, runner_id: int) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM splits WHERE runner_id = ? ORDER BY point_id", (runner_id,)
    ).fetchall()


def list_races(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT races.*, (SELECT COUNT(*) FROM runners WHERE runners.race_id = races.id) AS runner_count "
        "FROM races ORDER BY date DESC"
    ).fetchall()


def yearly_stats(conn: sqlite3.Connection) -> Tuple[List[Dict], Dict]:
    """Per-year rollup for the summary table: races, race-distance
    combinations (courses), total results, and unique runners (deduped by
    normalized name). Returns (year_rows, grand_total_row).
    """
    conn.row_factory = sqlite3.Row

    def _by_year(sql: str) -> Dict[str, int]:
        return {row["year"]: row["n"] for row in conn.execute(sql).fetchall()}

    races_by_year = _by_year(
        "SELECT substr(date, 1, 4) AS year, COUNT(*) AS n FROM races "
        "WHERE date IS NOT NULL GROUP BY year"
    )
    courses_by_year = _by_year(
        "SELECT substr(races.date, 1, 4) AS year, COUNT(*) AS n FROM courses "
        "JOIN races ON races.id = courses.race_id "
        "WHERE races.date IS NOT NULL GROUP BY year"
    )
    results_by_year = _by_year(
        "SELECT substr(races.date, 1, 4) AS year, COUNT(*) AS n FROM runners "
        "JOIN races ON races.id = runners.race_id "
        "WHERE races.date IS NOT NULL GROUP BY year"
    )
    unique_by_year = _by_year(
        "SELECT substr(races.date, 1, 4) AS year, COUNT(DISTINCT runners.name_normalized) AS n "
        "FROM runners JOIN races ON races.id = runners.race_id "
        "WHERE races.date IS NOT NULL GROUP BY year"
    )

    years = sorted(races_by_year, reverse=True)
    year_rows = [
        {
            "year": year,
            "races": races_by_year.get(year, 0),
            "courses": courses_by_year.get(year, 0),
            "results": results_by_year.get(year, 0),
            "unique_runners": unique_by_year.get(year, 0),
        }
        for year in years
    ]

    total_row = {
        "year": "Toplam",
        "races": conn.execute("SELECT COUNT(*) FROM races").fetchone()[0],
        "courses": conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
        "results": conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0],
        "unique_runners": conn.execute(
            "SELECT COUNT(DISTINCT name_normalized) FROM runners"
        ).fetchone()[0],
    }

    return year_rows, total_row


def top_runners_by_race_count(
    conn: sqlite3.Connection, limit: int = 10, status: Optional[str] = "finished"
) -> List[sqlite3.Row]:
    """Runners who've appeared in the most distinct races, deduped by
    normalized name. `status="finished"` (the default) only counts races
    they actually finished; pass None to count any participation (DNF/DNS
    included).

    Excludes a handful of providers' generic placeholder names for entrants
    who opted out of public display (e.g. "***** UNKNOWN COMPETITOR"),
    which would otherwise collapse many different anonymous people into one
    fake "runner" and dominate the leaderboard.
    """
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT name_normalized, MIN(name) AS name, COUNT(DISTINCT race_id) AS race_count "
        "FROM runners WHERE name NOT LIKE '%UNKNOWN%'"
    )
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " GROUP BY name_normalized ORDER BY race_count DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def top_runners_by_distance(
    conn: sqlite3.Connection, limit: int = 10, status: Optional[str] = "finished"
) -> List[sqlite3.Row]:
    """Runners with the most total distance across their races (only races
    where the course's distance is known are counted), deduped by
    normalized name.
    """
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT runners.name_normalized, MIN(runners.name) AS name, "
        "SUM(courses.distance_m) AS total_distance_m "
        "FROM runners "
        "JOIN courses ON courses.race_id = runners.race_id AND courses.code = runners.course_code "
        "WHERE runners.name NOT LIKE '%UNKNOWN%' AND courses.distance_m > 0"
    )
    params: list = []
    if status:
        sql += " AND runners.status = ?"
        params.append(status)
    sql += " GROUP BY runners.name_normalized ORDER BY total_distance_m DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def top_races_by_participants(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    """The single race editions with the most total participants, summed
    across all of that edition's distances (courses)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT races.name, races.date, races.slug, "
        "(SELECT COUNT(*) FROM runners WHERE runners.race_id = races.id) AS runner_count "
        "FROM races ORDER BY runner_count DESC LIMIT ?",
        (limit,),
    ).fetchall()


def top_race_courses_by_participants(conn: sqlite3.Connection, limit: int = 10) -> List[sqlite3.Row]:
    """The single race+distance combinations (e.g. "İstanbul Maratonu 2025 -
    42K") with the most participants - unlike top_races_by_participants,
    this doesn't sum a race edition's distances together.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT races.name AS race_name, races.date, runners.course_code, "
        "COUNT(*) AS participant_count "
        "FROM runners JOIN races ON races.id = runners.race_id "
        "GROUP BY runners.race_id, runners.course_code "
        "ORDER BY participant_count DESC LIMIT ?",
        (limit,),
    ).fetchall()
