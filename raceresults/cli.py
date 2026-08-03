"""Command-line entry point.

Examples:
    python -m raceresults.cli scrape \\
        "https://argeustiming.com/results/g-live/g-live.html?f=../uludagultra/2026/uput2026.clax" \\
        --slug uludagultra-2026

    python -m raceresults.cli search "Mustafa Dağdelen"
    python -m raceresults.cli search "Mustafa Dağdelen" --race uludagultra-2026

    python -m raceresults.cli export-json uludagultra-2026 --out exports/uludagultra-2026.json
    python -m raceresults.cli export-csv uludagultra-2026 --out-dir exports/uludagultra-2026
    python -m raceresults.cli list-races

    python -m raceresults.cli discover
    python -m raceresults.cli scrape-all
    python -m raceresults.cli scrape-all --only uludagultra,trabzonyarimaratonu

    python -m raceresults.cli import-html --slug cadde10k-2026 --name "Cadde 10K&21K" \\
        --date 2026-XX-XX saved_page1.html saved_page2.html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from urllib.parse import parse_qs, urlparse

from . import store
from .discover import discover_races
from .fetch import fetch_clax
from .hurratiming import scrape_hurratiming_race
from .parse import parse_race
from .plustiming import DEFAULT_CID as PLUSTIMING_DEFAULT_CID
from .plustiming import build_race_from_local_html, scrape_plustiming_race
from .timeutils import format_seconds

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "raceresults.db")


def cmd_scrape(args: argparse.Namespace) -> None:
    print(f"Fetching {args.url} ...", file=sys.stderr)
    parsed_url = urlparse(args.url)
    if "plustiming.com" in parsed_url.netloc:
        qs = parse_qs(parsed_url.query)
        rid = qs.get("RId", [None])[0]
        if not rid:
            raise SystemExit(f"No RId= parameter found in PlusTiming URL: {args.url}")
        cid = int(qs.get("CId", [PLUSTIMING_DEFAULT_CID])[0])
        race = scrape_plustiming_race(rid, cid=cid)
        race.slug = args.slug
    elif "hurratiming.com" in parsed_url.netloc:
        match = re.search(r"/event/(\d+)", parsed_url.path)
        if not match:
            raise SystemExit(f"No /event/<id> path found in HurraTiming URL: {args.url}")
        race = scrape_hurratiming_race(match.group(1))
        race.slug = args.slug
    else:
        fetched = fetch_clax(args.url)
        race = parse_race(fetched.xml_text, slug=args.slug, source_url=args.url)
        if fetched.title_hint:
            race.name = fetched.title_hint

    conn = store.connect(args.db)
    store.save_race(conn, race)

    finished = sum(1 for r in race.runners if r.status == "finished")
    print(
        f"Saved '{race.name}' ({args.slug}): {len(race.runners)} runners, "
        f"{finished} finished, {len(race.courses)} courses -> {args.db}"
    )


def cmd_import_html(args: argparse.Namespace) -> None:
    race = build_race_from_local_html(args.files, slug=args.slug, name=args.name, date=args.date)

    conn = store.connect(args.db)
    store.save_race(conn, race)

    finished = sum(1 for r in race.runners if r.status == "finished")
    print(
        f"Saved '{race.name}' ({args.slug}) from {len(args.files)} local file(s): "
        f"{len(race.runners)} runners, {finished} finished, {len(race.courses)} courses -> {args.db}"
    )


def cmd_search(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    rows = store.search_runners(conn, args.name, race_slug=args.race, limit=args.limit)
    if not rows:
        print("No matching runner found.")
        return

    for row in rows:
        time_str = format_seconds(row["finish_seconds"]) if row["finish_seconds"] else row["status"]
        print(
            f"[{row['race_name']}] Bib {row['bib']} - {row['name']} "
            f"({row['course_code']}, {row['category'] or '-'}) "
            f"-> {time_str}"
            + (f", genel {row['rank_course']}." if row["rank_course"] else "")
            + (f" kategori {row['rank_category']}." if row["rank_category"] else "")
        )
        if args.splits:
            for split in store.get_splits(conn, row["id"]):
                cumul = format_seconds(split["cumulative_seconds"])
                leg = format_seconds(split["split_seconds"])
                print(f"    {split['checkpoint_name']}: {leg or '-'} (toplam {cumul or '-'})")


def cmd_discover(args: argparse.Namespace) -> None:
    races = discover_races()
    print(f"Found {len(races)} events across known timing providers (argeus, passtiming, plustiming, hurratiming):")
    for race in races:
        print(f"[{race.provider}]\t{race.slug}\t{race.url}")


def cmd_scrape_all(args: argparse.Namespace) -> None:
    races = discover_races()
    only = [s.strip().lower() for s in args.only.split(",")] if args.only else None
    if only:
        races = [r for r in races if any(o in r.slug for o in only)]

    conn = store.connect(args.db)
    existing_slugs = {row["slug"] for row in store.list_races(conn)} if not args.force else set()

    ok = skipped = failed = 0
    for race in races:
        if race.slug in existing_slugs:
            print(f"[skip] {race.slug} (already stored, use --force to re-scrape)")
            skipped += 1
            continue
        try:
            if race.provider == "plustiming":
                qs = parse_qs(urlparse(race.url).query)
                rid = qs["RId"][0]
                cid = int(qs.get("CId", [PLUSTIMING_DEFAULT_CID])[0])
                parsed = scrape_plustiming_race(rid, cid=cid)
                parsed.slug = race.slug
            elif race.provider == "hurratiming":
                event_id = re.search(r"/event/(\d+)", urlparse(race.url).path).group(1)
                parsed = scrape_hurratiming_race(event_id)
                parsed.slug = race.slug
            else:
                fetched = fetch_clax(race.url)
                parsed = parse_race(fetched.xml_text, slug=race.slug, source_url=race.url)
                if fetched.title_hint:
                    parsed.name = fetched.title_hint
            store.save_race(conn, parsed)
            finished = sum(1 for r in parsed.runners if r.status == "finished")
            print(f"[ok]   {race.slug} — '{parsed.name}': {len(parsed.runners)} runners, {finished} finished")
            ok += 1
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            print(f"[fail] {race.slug}: {exc}")
            failed += 1

    print(f"\nDone: {ok} scraped, {skipped} skipped, {failed} failed (of {len(races)} discovered).")


def cmd_list_races(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    rows = store.list_races(conn)
    if not rows:
        print("No races stored yet.")
        return
    for row in rows:
        print(f"{row['slug']}\t{row['date']}\t{row['name']}\t({row['runner_count']} runners)")


def _race_id_for_slug(conn, slug: str) -> int:
    row = conn.execute("SELECT id FROM races WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise SystemExit(f"No race stored with slug '{slug}'. Run `scrape` first.")
    return row[0]


def cmd_export_json(args: argparse.Namespace) -> None:
    conn = store.connect(args.db)
    conn.row_factory = None
    import sqlite3

    conn.row_factory = sqlite3.Row
    race_id = _race_id_for_slug(conn, args.slug)

    race_row = conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    courses = conn.execute("SELECT * FROM courses WHERE race_id = ?", (race_id,)).fetchall()
    categories = conn.execute("SELECT * FROM categories WHERE race_id = ?", (race_id,)).fetchall()
    checkpoints = conn.execute("SELECT * FROM checkpoints WHERE race_id = ?", (race_id,)).fetchall()
    runners = conn.execute("SELECT * FROM runners WHERE race_id = ?", (race_id,)).fetchall()

    splits_by_runner = defaultdict(list)
    for split in conn.execute(
        "SELECT splits.* FROM splits JOIN runners ON runners.id = splits.runner_id "
        "WHERE runners.race_id = ? ORDER BY splits.point_id",
        (race_id,),
    ):
        splits_by_runner[split["runner_id"]].append(dict(split))

    data = {
        "race": dict(race_row),
        "courses": [dict(c) for c in courses],
        "categories": [dict(c) for c in categories],
        "checkpoints": [dict(c) for c in checkpoints],
        "runners": [
            {**dict(r), "splits": splits_by_runner.get(r["id"], [])} for r in runners
        ],
    }

    out_path = args.out or f"{args.slug}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Wrote {out_path}")


def cmd_export_csv(args: argparse.Namespace) -> None:
    import sqlite3

    conn = store.connect(args.db)
    conn.row_factory = sqlite3.Row
    race_id = _race_id_for_slug(conn, args.slug)

    runners = conn.execute(
        "SELECT * FROM runners WHERE race_id = ? ORDER BY course_code, rank_course", (race_id,)
    ).fetchall()

    by_course = defaultdict(list)
    for r in runners:
        by_course[r["course_code"] or ""].append(r)

    out_dir = args.out_dir or args.slug
    os.makedirs(out_dir, exist_ok=True)

    fields = [
        "rank_course", "rank_category", "rank_gender", "bib", "name", "club", "sex",
        "birth_year", "category", "nationality", "status", "finish_time_text",
        "pace", "gap_text",
    ]
    for course_code, course_runners in by_course.items():
        path = os.path.join(out_dir, f"{course_code or 'unknown'}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in course_runners:
                writer.writerow({k: r[k] for k in fields})
        print(f"Wrote {path} ({len(course_runners)} runners)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raceresults")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="Download and store a race's results")
    p_scrape.add_argument("url", help="G-Live viewer URL or direct .clax URL")
    p_scrape.add_argument("--slug", required=True, help="Short unique id for this race, e.g. uludagultra-2026")
    p_scrape.set_defaults(func=cmd_scrape)

    p_import_html = sub.add_parser(
        "import-html",
        help="Build a race from manually-saved Racetec-family results.aspx HTML files "
        "(for sites like racetecresults.com that block automated requests)",
    )
    p_import_html.add_argument("files", nargs="+", help="One or more saved .html files (any page/distance, any order)")
    p_import_html.add_argument("--slug", required=True, help="Short unique id for this race")
    p_import_html.add_argument("--name", required=True, help="Race display name")
    p_import_html.add_argument("--date", help="Race date, ISO format e.g. 2026-05-10")
    p_import_html.set_defaults(func=cmd_import_html)

    p_search = sub.add_parser("search", help="Look up a runner by name")
    p_search.add_argument("name", help="Full or partial name (Turkish-character-insensitive)")
    p_search.add_argument("--race", help="Restrict search to one race slug")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("--splits", action="store_true", help="Also print checkpoint splits")
    p_search.set_defaults(func=cmd_search)

    p_list = sub.add_parser("list-races", help="List stored races")
    p_list.set_defaults(func=cmd_list_races)

    p_discover = sub.add_parser("discover", help="List events found across known G-Live providers")
    p_discover.set_defaults(func=cmd_discover)

    p_scrape_all = sub.add_parser("scrape-all", help="Discover and scrape every event across known G-Live providers")
    p_scrape_all.add_argument(
        "--only", help="Comma-separated substrings to filter event slugs, e.g. uludagultra,trabzon"
    )
    p_scrape_all.add_argument("--force", action="store_true", help="Re-scrape races already stored")
    p_scrape_all.set_defaults(func=cmd_scrape_all)

    p_export_json = sub.add_parser("export-json", help="Export a full race to one JSON file")
    p_export_json.add_argument("slug")
    p_export_json.add_argument("--out", help="Output file path")
    p_export_json.set_defaults(func=cmd_export_json)

    p_export_csv = sub.add_parser("export-csv", help="Export a race to one CSV per course")
    p_export_csv.add_argument("slug")
    p_export_csv.add_argument("--out-dir", help="Output directory")
    p_export_csv.set_defaults(func=cmd_export_csv)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
